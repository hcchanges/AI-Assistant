"""情绪判定后端（v2-social-emotion-backend）：把「这段状态是什么情绪」抽象成可插拔的一层。

**为什么要有这一层**：影视情绪判定原来散在三条腿上——本地中文 CLIP（mood_cv）、
云端多模态（mood_cloud）、字幕关键词（monitor）。谁想提速 / 换服务，就得动采样链。
这里把「判定」收成一个口子：统一的输入 `state`、统一的输出 `EmotionVerdict`，
后面挂三个后端，按有没有钥匙自动挑，调用方不关心是谁判的。

**统一结构**（`EmotionVerdict`）：

    valence        -1 ~ 1      负=负面，正=正面
    arousal         0 ~ 1      越大越激动
    emotion_label   场景名     horror / angry / festive / neutral（沿用既有三态）
    confidence      0 ~ 1
    intention_hint  一句话     给下游回复方看的提示

`emotion_label` **沿用现有场景名**（`life/screen/scenes.py`），不是另起一套标签——
这样判定结果能直接跟 mood_fuse / 场景状态机那套接上。`valence` / `arousal` 没有既有
来源，用**情绪环锚点**：每个场景一个定值（恐怖=负价高唤醒、笑容=正价中唤醒、
愤怒=负价高唤醒、neutral=零点附近），见 `_ANCHORS`。接入方若自己给了连续值，
就用它给的；没给才回落到锚点。

**三个后端，优先级 jev > llm > local**：

- `JevBackend`：有 `JEV_API_KEY` + `JEV_ENDPOINT` 才启用。把 state 组装成
  `{state, questions:[Noul/Choice/Score]}` 发出去，按固定 JSON 结构解析（非流式文本）。
  **真实 endpoint / 认证待补**，本批只把接口、请求形状、解析与回退写死；没 key 自动跳过。
- `LLMBackend`：有现成的 LLM 钥匙就用主聊天那套。prompt 要求**只回 JSON 结构化判定**
  （Noul/Score 思路，避免自由文本），慢但通用。
- `LocalBackend`：复用现有本地 CLIP + 字幕关键词逻辑，零外部依赖，**默认兜底**。

`EMOTION_BACKEND=jev|llm|local` 可以强制指定；强制的那档没有钥匙时**退回 local**，
不报错。没有任何钥匙、也没有指定 → local（对外发布零门槛，不绑特定 API）。

**本批的边界（说清楚，别当成已经接上了）**：这里只提供「可插拔判定层」本身。
`monitor.py` 仍按 `screen.mood` 档位直接建 `MoodCvReader` / `MoodCloudReader`——
那两处被既有影视情绪测试钉着（`test_mood.py` 断言 `monitor.mood_reader` 的类型），
本批不动它们。下游（情绪云映射→关系调速→帮聊生成）同样一行未改。接线是下一步。
"""

from __future__ import annotations

import json
import logging
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR

logger = logging.getLogger(__name__)

BACKEND_JEV = "jev"
BACKEND_LLM = "llm"
BACKEND_LOCAL = "local"
BACKENDS: tuple[str, ...] = (BACKEND_JEV, BACKEND_LLM, BACKEND_LOCAL)

ENV_BACKEND = "EMOTION_BACKEND"
ENV_JEV_KEY = "JEV_API_KEY"
ENV_JEV_ENDPOINT = "JEV_ENDPOINT"

NEUTRAL_LABEL = "neutral"

# 情绪环锚点：场景 → (valence, arousal, intention_hint)。
# 没有既有来源可沿用，就按情绪环上各态的常见位置定一个保守值——它只负责「有个连续值
# 可用」，不冒充标定过的概率。接入方自己给了 valence/arousal 时以接入方为准。
_ANCHORS: dict[str, tuple[float, float, str]] = {
    SCENE_HORROR: (-0.80, 0.90, "画面/话里是紧张惊悚：语气放稳，先陪着，别调侃"),
    SCENE_ANGRY: (-0.70, 0.85, "有火气：先顺着情绪、别争对错、别火上浇油"),
    SCENE_FESTIVE: (0.80, 0.55, "轻松的开心劲：跟着轻快一点，别端着"),
    NEUTRAL_LABEL: (0.0, 0.30, "平常：自然接住就好，别加戏"),
}


# ---------- 统一输出结构 ----------


def _clamp(value: Any, low: float, high: float, default: float = 0.0) -> float:
    """收成 [low, high] 里的浮点数；不是数就用 default。"""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:          # NaN
        return float(default)
    return max(low, min(high, number))


@dataclass(frozen=True)
class EmotionVerdict:
    """一次情绪判定的统一结果。空 label + 0 置信度表示「这一拍没把握」。"""

    valence: float = 0.0
    arousal: float = 0.0
    emotion_label: str = ""
    confidence: float = 0.0
    intention_hint: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "valence": self.valence,
            "arousal": self.arousal,
            "emotion_label": self.emotion_label,
            "confidence": self.confidence,
            "intention_hint": self.intention_hint,
        }

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None, *, default_hint: str = "") -> "EmotionVerdict":
        """把模型/后端给的字典收成统一结构。缺的字段用锚点或默认值补。"""
        source = dict(data) if isinstance(data, Mapping) else {}
        raw_label = str(
            source.get("emotion_label") or source.get("emotion") or source.get("label") or ""
        ).strip()
        scene = _norm_scene(raw_label)
        label = scene or raw_label or NEUTRAL_LABEL
        anchor = _ANCHORS.get(scene or (NEUTRAL_LABEL if label == NEUTRAL_LABEL else ""),
                              (0.0, 0.30, ""))
        valence = _clamp(source.get("valence"), -1.0, 1.0, anchor[0])
        arousal = _clamp(source.get("arousal"), 0.0, 1.0, anchor[1])
        confidence = _clamp(source.get("confidence"), 0.0, 1.0, 1.0 if scene else 0.0)
        hint = str(source.get("intention_hint") or source.get("hint") or "").strip()
        return cls(
            valence=valence,
            arousal=arousal,
            emotion_label=label,
            confidence=confidence,
            intention_hint=hint or default_hint or anchor[2],
        )


def verdict_for(scene: str | None, confidence: float = 0.0) -> EmotionVerdict:
    """场景名 → 统一结构。scene=None 表示没把握：空 label、0 置信度。"""
    if not scene:
        return EmotionVerdict()
    anchor = _ANCHORS.get(scene)
    if anchor is None:
        return EmotionVerdict(confidence=_clamp(confidence, 0.0, 1.0))
    valence, arousal, hint = anchor
    return EmotionVerdict(
        valence=valence,
        arousal=arousal,
        emotion_label=scene,
        confidence=_clamp(confidence, 0.0, 1.0),
        intention_hint=hint,
    )


# ---------- 场景名归一 ----------

_SCENE_TITLE_ALIASES: dict[str, str] = {
    "恐怖": SCENE_HORROR,
    "惊悚": SCENE_HORROR,
    "愤怒": SCENE_ANGRY,
    "生气": SCENE_ANGRY,
    "笑容": SCENE_FESTIVE,
    "开心": SCENE_FESTIVE,
    "喜悦": SCENE_FESTIVE,
    "喜庆": SCENE_FESTIVE,
}
_SCENE_IDS = (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE)


def _norm_scene(label: Any) -> str | None:
    """中文名 / 场景 id → 场景 id；认不出（含 neutral / 平常）返回 None。"""
    key = str(label or "").strip().lower()
    if not key:
        return None
    if key in _SCENE_IDS:
        return key
    return _SCENE_TITLE_ALIASES.get(str(label).strip())


# ---------- 环境（os.environ > .env） ----------

_DOTENV_NAME = ".env"


def dotenv_path() -> Path:
    """项目根下的 `.env`。"""
    return Path(__file__).resolve().parents[2] / _DOTENV_NAME


def load_env() -> dict[str, str]:
    """读环境变量：`os.environ` 优先，`.env` 只补缺。

    项目里没有 python-dotenv（零新依赖那条线），所以这里只做一个够用的解析：
    忽略空行与 `#` 注释，`KEY=VALUE` 去掉两边的引号。`.env` 不存在就只回 `os.environ`。
    """
    values: dict[str, str] = dict(os.environ)
    try:
        raw = dotenv_path().read_text(encoding="utf-8")
    except OSError:
        return values
    for line in raw.splitlines():
        text = line.strip()
        if not text or text.startswith("#") or "=" not in text:
            continue
        key, _, value = text.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in values:
            values[key] = value
    return values


# ---------- 状态 → 文本 / 载荷 ----------


def _state_payload(state: Mapping[str, Any] | None) -> dict[str, Any]:
    """从 state 里挑出能序列化的那几格（text / subtitle / labels / features）。"""
    source = state if isinstance(state, Mapping) else {}
    payload: dict[str, Any] = {}
    for key in ("text", "subtitle", "labels", "features"):
        value = source.get(key)
        if value is None or value == "" or value == []:
            continue
        payload[key] = value
    return payload


def _state_to_text(state: Mapping[str, Any] | None) -> str:
    """state → 给 LLM 看的一段 JSON 文本。"""
    payload = _state_payload(state)
    try:
        return json.dumps(payload, ensure_ascii=False)[:2000]
    except (TypeError, ValueError):
        return str(payload)[:2000]


# ---------- 后端接口 ----------


class EmotionJudge:
    """情绪判定后端的统一接口。"""

    name: str = ""

    def available(self) -> bool:
        """现在能不能用这一档。**便宜**：只查钥匙/配置，不联网、不建模型。"""
        return True

    def judge(self, state: Mapping[str, Any]) -> EmotionVerdict | None:
        """一段状态 → 统一判定。判不了返回 None（调用方可回退到 local）。"""
        raise NotImplementedError

    @classmethod
    def from_config(
        cls,
        config: Any = None,
        *,
        env: Mapping[str, str] | None = None,
        llm_available: Callable[[], bool] | None = None,
        provider: Any = None,
        cv_reader: Any = None,
    ) -> "EmotionJudge":
        """按优先级挑后端：jev（有 key）> llm（有 key）> local。

        `EMOTION_BACKEND` 可强制指定；强制的那档没有钥匙时退回 local，不报错。
        `env` / `llm_available` / `provider` / `cv_reader` 都是给测试与调用方注入用的。
        """
        values = load_env() if env is None else dict(env)
        forced = str(values.get(ENV_BACKEND) or "").strip().lower()
        has_jev = bool(str(values.get(ENV_JEV_KEY) or "").strip()) and bool(
            str(values.get(ENV_JEV_ENDPOINT) or "").strip()
        )
        has_llm = bool(llm_available()) if llm_available is not None else _has_llm_key(config)

        if forced == BACKEND_LOCAL:
            return LocalBackend(cv_reader=cv_reader)
        if forced == BACKEND_JEV:
            if has_jev:
                return JevBackend(
                    api_key=str(values.get(ENV_JEV_KEY) or ""),
                    endpoint=str(values.get(ENV_JEV_ENDPOINT) or ""),
                )
            return LocalBackend(cv_reader=cv_reader)
        if forced == BACKEND_LLM:
            if has_llm:
                return LLMBackend(config=config, provider=provider)
            return LocalBackend(cv_reader=cv_reader)

        if has_jev:
            return JevBackend(
                api_key=str(values.get(ENV_JEV_KEY) or ""),
                endpoint=str(values.get(ENV_JEV_ENDPOINT) or ""),
            )
        if has_llm:
            return LLMBackend(config=config, provider=provider)
        return LocalBackend(cv_reader=cv_reader)


def _has_llm_key(config: Any) -> bool:
    """主聊天那把钥匙在不在。取不到就当没有。"""
    if config is None:
        return False
    try:
        return bool(config.get_api_key())
    except Exception:      # noqa: BLE001 —— 取钥匙出岔子就是「没钥匙」
        return False


# ---------- 本地后端 ----------


class LocalBackend(EmotionJudge):
    """本地兜底：画面标签 > 本地 CLIP > 字幕关键词。零外部依赖、不发网络。"""

    name = BACKEND_LOCAL

    def __init__(self, cv_reader: Any = None, keyword_scene: Callable[[str], str | None] | None = None):
        self._cv = cv_reader
        self._keyword_scene = keyword_scene or _scene_from_keywords

    def judge(self, state: Mapping[str, Any]) -> EmotionVerdict:
        scene, confidence = self._scene_from(state if isinstance(state, Mapping) else {})
        return verdict_for(scene, confidence)

    def _scene_from(self, state: Mapping[str, Any]) -> tuple[str | None, float]:
        # 1) 调用方已经给了画面标签：直接用
        labels = state.get("labels")
        if isinstance(labels, (list, tuple)):
            for label in labels:
                scene = _norm_scene(label)
                if scene:
                    return scene, 1.0
        elif isinstance(labels, str):
            scene = _norm_scene(labels)
            if scene:
                return scene, 1.0

        # 2) 本地 CLIP（复用 mood_cv 的判读器）
        frame = state.get("image")
        if frame is None:
            frame = state.get("frame")
        if self._cv is not None and frame is not None:
            try:
                verdict = self._cv.classify_array(frame)
            except Exception as exc:      # noqa: BLE001 —— 判读炸了退回关键词
                logger.debug("本地情绪判定：CLIP 这一帧没成（%s）", exc)
                verdict = None
            if verdict is not None and getattr(verdict, "scene", None):
                return verdict.scene, float(getattr(verdict, "score", 1.0))

        # 3) 字幕关键词（复用 monitor 那张表）
        text = state.get("text")
        if text is None:
            text = state.get("subtitle")
        scene = self._keyword_scene(str(text or ""))
        return scene, 1.0 if scene else 0.0


def _scene_from_keywords(text: str) -> str | None:
    """字幕文字 → 场景。口径与 `monitor._count_scene` 一致：数命中、过最低票、并列按序。

    表本身不复制一份——直接从 monitor 取，免得两处口径漂移。局部导入是为了
    避免模块级把 PySide6 拖进来（测试只要纯判定时更轻）。
    """
    if not text:
        return None
    from life.screen.monitor import SCENE_KEYWORDS, SCENE_MIN_HITS
    from life.screen.scenes import SCENE_ORDER

    best: str | None = None
    best_hits = 0
    for scene in SCENE_ORDER:
        hits = sum(text.count(word) for word in SCENE_KEYWORDS.get(scene, ()))
        if hits < SCENE_MIN_HITS.get(scene, 1):
            continue
        if hits > best_hits:
            best, best_hits = scene, hits
    return best


# ---------- LLM 后端 ----------


LLM_SYSTEM = "你是情绪判定器。只输出 JSON，不要解释、不要 Markdown 代码块。"

LLM_PROMPT = (
    "判断下面这段状态里的情绪，只回一个 JSON 对象。字段：\n"
    '  "valence": -1 到 1 的浮点数（负=负面，正=正面）\n'
    '  "arousal": 0 到 1 的浮点数（越大越激动）\n'
    '  "emotion_label": "horror" / "angry" / "festive" / "neutral" 之一\n'
    '  "confidence": 0 到 1 的浮点数\n'
    '  "intention_hint": 一句给回复方看的中文提示\n'
    "状态：{state}"
)


class LLMBackend(EmotionJudge):
    """通用后端：用主聊天那把 LLM 钥匙，要求模型只回结构化 JSON。慢，但不挑服务商。"""

    name = BACKEND_LLM

    def __init__(self, config: Any = None, provider: Any = None,
                 provider_factory: Callable[[], Any] | None = None):
        self._config = config
        self._provider = provider
        self._provider_factory = provider_factory

    def available(self) -> bool:
        if self._provider is not None:
            return True
        if self._provider_factory is not None:
            return True
        return _has_llm_key(self._config)

    def _get_provider(self):
        if self._provider is None:
            if self._provider_factory is not None:
                self._provider = self._provider_factory()
            else:
                from brain.llm.factory import LLMFactory

                self._provider = LLMFactory.create(self._config)
        return self._provider

    def judge(self, state: Mapping[str, Any]) -> EmotionVerdict | None:
        try:
            provider = self._get_provider()
            prompt = LLM_PROMPT.format(state=_state_to_text(state))
            if hasattr(provider, "chat_json"):
                data = provider.chat_json(prompt, system=LLM_SYSTEM)
            else:
                from brain.llm.base import parse_json_object

                data = parse_json_object(provider.chat(prompt, system=LLM_SYSTEM))
        except Exception as exc:      # noqa: BLE001 —— 慢/断网/格式歪都退回 local
            logger.info("LLM 情绪判定没成（%s），退回 local", exc)
            return None
        if not isinstance(data, Mapping):
            return None
        return EmotionVerdict.from_mapping(data)


# ---------- Jev 后端（占位：真实 endpoint / 认证待补） ----------


JEV_QUESTIONS: tuple[dict[str, Any], ...] = (
    {"type": "Choice", "name": "emotion_label",
     "options": [SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE, NEUTRAL_LABEL]},
    {"type": "Score", "name": "valence", "min": -1.0, "max": 1.0},
    {"type": "Score", "name": "arousal", "min": 0.0, "max": 1.0},
    {"type": "Score", "name": "confidence", "min": 0.0, "max": 1.0},
    {"type": "Noul", "name": "intention_hint", "max_length": 80},
)


def _post_json(url: str, payload: dict[str, Any], headers: dict[str, str], timeout: float) -> Any:
    """POST 一份 JSON，读回 JSON。只用标准库 urllib。"""
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8", "replace")
    return json.loads(raw)


class JevBackend(EmotionJudge):
    """Jev 后端（占位）。

    请求形状固定为 `{"state": {...}, "questions": [Noul/Choice/Score...]}`，
    响应按固定 JSON 结构解析（不是流式文本）。

    TODO（真接的时候补这三样，别改调用方）：
      1. 真实的 endpoint 路径与鉴权头（现在只按 `Authorization: Bearer <JEV_API_KEY>` 发）；
      2. 真实响应字段名（现在对 `answers` / `result` / `data` / `output` 都做兼容）；
      3. 超时、限流与重试口径。
    在补齐之前，没配 key 时 `from_config` 会直接跳过它、回退 local。
    """

    name = BACKEND_JEV

    def __init__(self, api_key: str = "", endpoint: str = "",
                 post: Callable[..., Any] | None = None, timeout: float = 5.0):
        self._api_key = str(api_key or "").strip()
        self._endpoint = str(endpoint or "").strip()
        self._post = post
        self._timeout = float(timeout)

    def available(self) -> bool:
        return bool(self._api_key and self._endpoint)

    def build_payload(self, state: Mapping[str, Any] | None) -> dict[str, Any]:
        """把 state 组装成 Jev 的请求形状。"""
        return {"state": _state_payload(state), "questions": [dict(q) for q in JEV_QUESTIONS]}

    def judge(self, state: Mapping[str, Any]) -> EmotionVerdict | None:
        if not self.available():
            return None
        payload = self.build_payload(state)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._post is not None:
                response = self._post(self._endpoint, payload, headers, self._timeout)
            else:
                response = _post_json(self._endpoint, payload, headers, self._timeout)
        except Exception as exc:      # noqa: BLE001 —— 占位阶段不报错，退回 local
            logger.info("Jev 情绪判定没成（%s），退回 local", exc)
            return None
        return _parse_jev_response(response)


def _parse_jev_response(response: Any) -> EmotionVerdict | None:
    """Jev 响应 → 统一结构。认不出任何字段就返回 None。"""
    if isinstance(response, (str, bytes)):
        try:
            response = json.loads(response)
        except (TypeError, ValueError):
            return None
    if not isinstance(response, Mapping):
        return None

    node: Any = response
    for key in ("answers", "result", "data", "output"):
        candidate = response.get(key)
        if isinstance(candidate, (Mapping, list, tuple)):
            node = candidate
            break

    flat: dict[str, Any] = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            flat[str(key)] = _value_of(value)
    elif isinstance(node, (list, tuple)):
        for item in node:
            if not isinstance(item, Mapping):
                continue
            name = item.get("name") or item.get("key") or item.get("id")
            if name:
                flat[str(name)] = _value_of(item)
    if not flat:
        return None
    return EmotionVerdict.from_mapping(flat)


def _value_of(item: Any) -> Any:
    """Jev 的一项回答可能是裸值，也可能是 {"value"/"choice"/"score"/"text": ...}。"""
    if isinstance(item, Mapping):
        for key in ("value", "choice", "score", "text", "answer"):
            if key in item:
                return item[key]
        return item
    return item


__all__ = [
    "BACKEND_JEV",
    "BACKEND_LLM",
    "BACKEND_LOCAL",
    "BACKENDS",
    "ENV_BACKEND",
    "ENV_JEV_ENDPOINT",
    "ENV_JEV_KEY",
    "EmotionJudge",
    "EmotionVerdict",
    "JEV_QUESTIONS",
    "JevBackend",
    "LLMBackend",
    "LLM_PROMPT",
    "LLM_SYSTEM",
    "LocalBackend",
    "NEUTRAL_LABEL",
    "dotenv_path",
    "load_env",
    "verdict_for",
]
