"""
按配置创建 LLM 实例。

目前只实现 OpenAI 兼容协议一种（DeepSeek、通义千问、月之暗面等都兼容），
要接新协议时在这里加分支即可。
"""

from __future__ import annotations

import base64
import logging
import time
from typing import Any, Callable

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from openai import APIConnectionError

from brain.llm.base import LLMError, LLMOfflineError, LLMProvider, parse_json_object
from brain.llm.search import DashScopeSearch
from core.config import Config

logger = logging.getLogger(__name__)

REQUEST_TIMEOUT = 60
MAX_RETRIES = 2

# 断网降级（v2-offline-fallback）：网络层失败后，这个时长内不再发起 API，直接走本地
# 兜底；到期自动回 API（联网恢复无需重启）。单位秒，跟 REQUEST_TIMEOUT 同级。
OFFLINE_COOLDOWN = 60.0

# 记录"上次断网"持续到什么时候（monotonic 秒，模块级跨实例共享）。
_offline_until = 0.0


def _is_network_failure(exc: BaseException) -> bool:
    """网络层失败（Connection error / 超时）才算断网；4xx/5xx 不算。

    langchain_openai 把 openai 的异常包一层再抛，但那层包装类多重继承自
    openai.APIConnectionError（含 APITimeoutError，见其 MRO），isinstance 就能同时
    兜住包装版与原生版；APIStatusError（4xx/5xx）、AuthenticationError（Key 错）、
    RateLimitError（限流）都不在 APIConnectionError 之下，不会误判成断网。
    """
    return isinstance(exc, APIConnectionError)


def _mark_offline() -> None:
    """记一笔：刚遇到网络层失败，接下来 OFFLINE_COOLDOWN 秒内先别打 API。"""
    global _offline_until
    _offline_until = time.monotonic() + OFFLINE_COOLDOWN


def _offline_now() -> bool:
    """现在是不是还在"断网冷却"里（在这期间直接走本地兜底、不发 API）。"""
    return time.monotonic() < _offline_until


# 多轮消息的角色 → LangChain 消息类
_MESSAGE_CLASSES = {
    "system": SystemMessage,
    "user": HumanMessage,
    "assistant": AIMessage,
}


def _to_langchain_messages(messages: list[dict[str, str]]) -> list[Any]:
    """把 {"role", "content"} 列表转成 LangChain 的消息对象。"""
    result: list[Any] = []
    for message in messages:
        role = str(message.get("role") or "").strip()
        message_class = _MESSAGE_CLASSES.get(role)
        if message_class is None:
            raise LLMError(f"不认识的消息角色: {role!r}")
        result.append(message_class(content=str(message.get("content") or "")))
    return result


def _message_text(message: Any) -> str:
    """把模型返回的消息取成纯文本（兼容字符串内容与分块内容两种形态）。"""
    content = getattr(message, "content", message)
    if isinstance(content, str):
        return content.strip()
    parts: list[str] = []
    for block in content or []:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts).strip()


def _loads_json(text: str) -> dict[str, Any]:
    """解析模型返回的 JSON（实现在 base 里，联网那条通道也用同一份）。"""
    return parse_json_object(text)


class OpenAICompatibleProvider(LLMProvider):
    def __init__(self, model: ChatOpenAI, name: str, model_name: str):
        self._model = model
        self.name = name
        self.model_name = model_name

    def _messages(self, prompt: str, system: str | None) -> list[Any]:
        messages: list[Any] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=prompt))
        return messages

    def _invoke(self, call: Callable[[], Any], fail: str) -> Any:
        """统一调用壳：断网冷却里不发 API，直接抛 LLMOfflineError 走本地兜底。

        网络层失败（Connection error / 超时）标记离线并抛 LLMOfflineError，让调用方换
        本地话术；4xx/5xx（Key 错、限流、接口报错）照旧抛 LLMError——那不是断网，兜底
        也兜不住，界面仍按原来的方式提示。
        """
        if _offline_now():
            raise LLMOfflineError(f"{self.name}/{self.model_name} 断网，改走本地兜底")
        try:
            return call()
        except Exception as exc:      # noqa: BLE001 —— 网络/鉴权/格式错误都要接
            if _is_network_failure(exc):
                _mark_offline()
                raise LLMOfflineError(f"{self.name}/{self.model_name} 断网: {exc}") from exc
            raise LLMError(f"{self.name}/{self.model_name} {fail}: {exc}") from exc

    def chat(self, prompt: str, system: str | None = None) -> str:
        result = self._invoke(
            lambda: self._model.invoke(self._messages(prompt, system)),
            fail="调用失败",
        )
        text = _message_text(result)
        if not text:
            raise LLMError(f"{self.name}/{self.model_name} 返回了空内容")
        return text

    def chat_json(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        model = self._model.bind(response_format={"type": "json_object"})
        result = self._invoke(
            lambda: model.invoke(self._messages(prompt, system)),
            fail="调用失败",
        )
        return _loads_json(_message_text(result))

    def chat_with_image(self, prompt: str, image: bytes, system: str | None = None) -> str:
        """发一张图 + 一段提示（OpenAI 兼容的多模态消息格式）。"""
        data_uri = "data:image/jpeg;base64," + base64.b64encode(image).decode("ascii")
        content = [
            {"type": "text", "text": prompt},
            {"type": "image_url", "image_url": {"url": data_uri}},
        ]
        messages: list[Any] = []
        if system:
            messages.append(SystemMessage(content=system))
        messages.append(HumanMessage(content=content))
        result = self._invoke(
            lambda: self._model.invoke(messages),
            fail="看图失败",
        )
        text = _message_text(result)
        if not text:
            raise LLMError(f"{self.name}/{self.model_name} 返回了空内容")
        return text


    def chat_messages(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """多轮对话：角色卡的 few-shot 与生成参数都从这里走。"""
        overrides: dict[str, Any] = {}
        if temperature is not None:
            overrides["temperature"] = temperature
        if top_p is not None:
            overrides["top_p"] = top_p
        if max_tokens is not None:
            overrides["max_tokens"] = max_tokens
        model = self._model.bind(**overrides) if overrides else self._model
        result = self._invoke(
            lambda: model.invoke(_to_langchain_messages(messages)),
            fail="调用失败",
        )
        text = _message_text(result)
        if not text:
            raise LLMError(f"{self.name}/{self.model_name} 返回了空内容")
        return text


class LLMFactory:
    @staticmethod
    def create(config: Config, provider_name: str | None = None) -> LLMProvider:
        """按配置构造模型实例；配置缺 Key 时抛 LLMError 由界面提示。"""
        provider = config.llm.provider(provider_name)
        api_key = config.get_api_key(provider)
        if not api_key:
            raise LLMError(f"还没填 {provider.name} 的 API Key，请到设置页填写")

        model = ChatOpenAI(
            model=provider.model,
            api_key=api_key,
            base_url=provider.base_url,
            temperature=config.llm.temperature,
            timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        logger.debug("已创建模型实例: %s / %s", provider.name, provider.model)
        return OpenAICompatibleProvider(model, provider.name, provider.model)

    @staticmethod
    def create_vision(config: Config) -> LLMProvider:
        """游戏陪玩"看图"用的多模态模型：复用某供应商的地址与 Key，换成能看图的模型名。"""
        vision = config.vision
        provider = config.llm.provider(vision.provider)
        api_key = config.get_api_key(provider)
        if not api_key:
            raise LLMError(
                f"还没填 {provider.name} 的 API Key（陪玩要看图），请到设置页的「陪玩」里填"
            )

        model = ChatOpenAI(
            model=vision.model,
            api_key=api_key,
            base_url=provider.base_url,
            temperature=config.llm.temperature,
            timeout=REQUEST_TIMEOUT,
            max_retries=MAX_RETRIES,
        )
        logger.debug("已创建看图模型实例: %s / %s", provider.name, vision.model)
        return OpenAICompatibleProvider(model, provider.name, vision.model)

    @staticmethod
    def create_search(config: Config) -> DashScopeSearch:
        """联网检索客户端（指令 16）：按 work.provider 取接口地址与 Key。

        它不走 OpenAI 兼容协议（兼容模式不回带结构化来源），所以是另一个对象，
        由工作模块的链自己创建——链内部短期用一次就丢，不做池化。
        """
        provider = config.llm.provider(config.work.provider)
        api_key = config.get_api_key(provider)
        if not api_key:
            raise LLMError(
                f"还没填 {provider.name} 的 API Key（工作卡片要联网检索），请到设置页填写"
            )
        logger.debug("已创建联网检索客户端: %s / %s", provider.name, provider.model)
        return DashScopeSearch(
            api_key=api_key,
            base_url=provider.base_url,
            model=provider.model,
        )

    @staticmethod
    def test_connection(config: Config, provider_name: str | None = None) -> str:
        """设置页“测试连接”：发一句极短的话，返回模型回复。"""
        from brain.prompts.templates import CONNECT_TEST_PROMPT

        provider = LLMFactory.create(config, provider_name)
        return provider.chat(CONNECT_TEST_PROMPT)

    @staticmethod
    def test_vision(config: Config, image: bytes) -> str:
        """设置页“测试看图”：把一张截图发给看图模型，返回它对画面的描述。"""
        from brain.prompts.templates import VISION_TEST_PROMPT

        provider = LLMFactory.create_vision(config)
        return provider.chat_with_image(VISION_TEST_PROMPT, image)
