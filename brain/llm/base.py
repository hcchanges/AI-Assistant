"""
LLM 提供者抽象接口。

上层（链、服务）只依赖这个接口，不依赖任何具体厂商 SDK，
因此换模型只改 config.yaml 与 factory，不动业务代码。
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any


class LLMError(Exception):
    """模型调用失败（网络、鉴权、返回格式非法等）。"""


class LLMOfflineError(LLMError):
    """网络层失败（断网 / 超时）：调用方应改走本地兜底话术，而不是报「没听懂」。

    它是 LLMError 的子类，老代码里 `except LLMError` 的地方照样接得住；想要区分
    「断网该兜底」与「别的调用失败（Key 错 / 限流 / 接口报错）」时，再单独 catch 它。
    """


def parse_json_object(text: str) -> dict[str, Any]:
    """解析模型返回的 JSON，容忍 ```json 代码块包裹与前后多余的话。

    联网检索那条通道也要用它（模型给的正文里带引用角标 [1]，所以只截最外层的花括号）。
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        cleaned = cleaned.rsplit("```", 1)[0]
    cleaned = cleaned.strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start == -1 or end == -1:
        raise LLMError(f"模型未返回 JSON: {text[:200]}")
    try:
        data = json.loads(cleaned[start : end + 1])
    except json.JSONDecodeError as exc:
        raise LLMError(f"模型返回的 JSON 无法解析: {exc}；原文: {text[:200]}") from exc
    if not isinstance(data, dict):
        raise LLMError(f"模型返回的 JSON 不是对象: {text[:200]}")
    return data


class LLMProvider(ABC):
    """统一的大模型接口。"""

    #: 供应商名，例如 deepseek
    name: str
    #: 模型名，例如 deepseek-flash
    model_name: str

    @abstractmethod
    def chat(self, prompt: str, system: str | None = None) -> str:
        """普通对话，返回纯文本。"""

    @abstractmethod
    def chat_json(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        """要求模型输出 JSON 并解析为字典。"""

    @abstractmethod
    def chat_with_image(self, prompt: str, image: bytes, system: str | None = None) -> str:
        """带一张图片（JPEG 字节）的对话，返回纯文本。只有多模态模型能调。"""

    @abstractmethod
    def chat_messages(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """多轮对话，返回纯文本。角色卡用：要喂 few-shot 示范，还要带这张卡自己的生成参数。

        messages 形如 [{"role": "system"/"user"/"assistant", "content": "..."}]；
        三个参数为 None 时用配置里的默认值。
        """
