"""云端精细档（S5，可选）：用户**明确授权**之后，才把这一帧发给多模态 API。

**默认关，且关着的时候一个字节都不发。** 这不是"发之前先看一眼开关"那种温柔说法，
是硬闸：没授权时 `classify_array` 在第一行就返回 None，连 provider 都不建——
所以"没授权"这件事在代码路径上是可证的，不是靠自觉（tests/test_mood.py 里就按
"provider 被建了几次"来验这条）。

三道闸缺一不可，最后一道是用户本人：

    screen.mood == "cloud"      ← 档位选的是云端
    screen.mood_cloud == True   ← 用户主动打开过授权
    （且是用户在设置页看到 CONSENT_NOTICE 那段话之后自己点的）

**告知必须说人话**：授权开关旁边要原样显示 `CONSENT_NOTICE`——传的是什么（这一帧
画面）、传给谁（你自己配的那个多模态服务商）、本地档本来能做什么。含糊其辞的
"改善体验"不算告知。

走的是项目里已有的那条视觉通道（`LLMFactory.create_vision` + `chat_with_image`），
不新开协议、不新增依赖。画面**编码在内存里**（PNG 字节）直接递进去，**连临时文件
都不落**——比"落 temp 再 finally 删"又少了一步。

**音频腿到这一档仍然是空的**，原因不在授权而在音源：本机拿不到电影的声音——
麦克风是"按住才收音"（见 life/voice/capture.py 开头），要常驻开麦或抓系统声回环
就得踩"不常驻监听"或"零新依赖"这两条线。所以 `transmit_audio` 只留了接口与闸门，
真正能跑要等音源那条线先解决。这不是本轮能绕过去的，报告里照实写了。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

from core.config import Config
from life.screen.mood_cv import MoodVerdict
from life.screen.scenes import SCENE_FESTIVE, SCENE_HORROR

logger = logging.getLogger(__name__)

# 授权旁边原样显示的告知。别改成含糊的"改善体验"——告知的意义就在于说清楚代价。
CONSENT_NOTICE = (
    "开启后，宠物会把当前正在看的**这一帧画面**（约每秒一次里的一帧）发给"
    "你自己配置的那个多模态服务商，用来判断画面在演什么。\n"
    "· 不开启时：一个字节都不传，完全本地判断（本地档本来就跑得动）。\n"
    "· 画面只在内存里编码后发走，不落盘、不落库、不留副本。\n"
    "· 想停随时关掉这个开关，立刻生效。"
)

CLOUD_PROMPT = (
    "看这一帧画面，判断它属于下面哪一类，只回一个词，不要解释：\n"
    "恐怖 / 笑容 / 平常\n"
    "（恐怖=惊悚吓人的场面；笑容=人脸微笑或大笑；"
    "平常=没有明显情绪的普通画面）"
)

# 模型回的话 → 场景。认不出（或回了"平常"）就是没把握，不硬塞一个场景。
# v2-mood-screen ④：这一档从"喜庆"改成"笑容"（识别人脸微笑/大笑）。
# v2-mood-detect：愤怒（只认字幕文本）、幽默（已删）都从画面腿移走，云端档同样不判。
# v2-mood-tri：哭泣整态删除——提示词里不再问它，映射表里也不再有它那一格。
_WORD_TO_SCENE: dict[str, str] = {
    "恐怖": SCENE_HORROR,
    "笑容": SCENE_FESTIVE,
}


@dataclass(frozen=True)
class Consent:
    """授权现在是什么状态。`allowed=False` 时 reason 说明卡在哪一道。"""

    allowed: bool
    reason: str


def consent_state(config: Config) -> Consent:
    """三道闸一起看。**任何一道没过就是不许传**，不给默认放行。"""
    mood = str(getattr(config.screen, "mood", "off") or "off").strip().lower()
    if mood != "cloud":
        return Consent(False, f"档位不是云端（现在是 {mood or 'off'}）")
    if not bool(getattr(config.screen, "mood_cloud", False)):
        return Consent(False, "用户没有开启传屏授权")
    return Consent(True, "用户已授权")


class MoodCloudReader:
    """把一帧发给多模态 API 判场景（恐怖 / 笑容 / 平常）。没有授权时**什么都不做**。

    跟 `MoodCvReader` 同一个接口（`available` / `classify_array`），
    所以 monitor 那边两条路可以互换，融合层也不用知道这一帧是谁判的。
    """

    def __init__(
        self,
        config: Config,
        provider_factory: Callable[[Config], Any] | None = None,
    ) -> None:
        self._config = config
        self._provider_factory = provider_factory or _default_provider
        self._sent = 0          # 真发出去的次数（测试与设置页提示用）

    @property
    def available(self) -> bool:
        """现在能不能走云端。**只查闸门与钥匙，不建 provider、不联网。**"""
        if not consent_state(self._config).allowed:
            return False
        try:
            return bool(self._config.get_api_key())
        except Exception:      # noqa: BLE001 —— 取钥匙出岔子就是"没钥匙"
            return False

    @property
    def sent(self) -> int:
        """累计真发出去几帧。没授权时它永远是 0。"""
        return self._sent

    def classify_array(self, array) -> MoodVerdict | None:
        """一帧 → 三态。没授权就返回 None，**并且一个请求都不发**。"""
        state = consent_state(self._config)
        if not state.allowed:
            # 这一行就是那条红线：没授权，连收拾画面的活儿都不干
            logger.debug("云端档没走：%s", state.reason)
            return None
        if array is None:
            return None
        try:
            payload = _png_bytes(array)
        except Exception as exc:      # noqa: BLE001 —— 编不出来就当这一帧没判
            logger.debug("云端档：这一帧编码失败（%s）", exc)
            return None
        try:
            text = self._provider_factory(self._config).chat_with_image(CLOUD_PROMPT, payload)
        except Exception as exc:      # noqa: BLE001 —— 网络炸了退回本地那几条腿
            logger.info("云端档这一趟没成（%s），用本地结果", exc)
            return None
        self._sent += 1
        return _verdict_from_text(text)


def _default_provider(config: Config):
    from brain.llm.factory import LLMFactory

    return LLMFactory.create_vision(config)


def _png_bytes(array) -> bytes:
    """numpy 图 → PNG 字节。**只在内存里**，不落任何文件。"""
    import numpy as np
    from io import BytesIO
    from PIL import Image

    buffer = BytesIO()
    Image.fromarray(np.asarray(array, dtype=np.uint8)).convert("RGB").save(buffer, "PNG")
    return buffer.getvalue()


def _verdict_from_text(text: object) -> MoodVerdict | None:
    """模型回的话 → 判读结果。认不出就当没把握。"""
    if not isinstance(text, str):
        return None
    for word, scene in _WORD_TO_SCENE.items():
        if word in text:
            return MoodVerdict(scene, 1.0, {scene: 1.0})
    return MoodVerdict(None, 0.0, {})


def transmit_audio(config: Config, wav: bytes) -> dict[str, float] | None:
    """音频情绪腿（S5 预留）。**本轮恒返回 None**——不是没授权，是没音源。

    留着这个函数是为了把话说清楚：音频这条腿缺的是"从哪拿到电影的声音"，不是
    "让不让传"。真接的时候在这里换成一次音频 API 调用，融合层那边不用动
    （它本来就是"哪条腿有就按哪条腿权重归一"）。
    """
    state = consent_state(config)
    logger.debug("音频情绪腿：本轮没有音源（授权状态：%s）", state.reason)
    return None


__all__ = [
    "CLOUD_PROMPT",
    "CONSENT_NOTICE",
    "Consent",
    "MoodCloudReader",
    "consent_state",
    "transmit_audio",
]
