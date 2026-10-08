"""情绪判读的融合（S3）：OCR 字幕 + CV 画面（+ 可选的音频）合成一个三态。

三条腿各有各的毛病，所以谁都不能单独说了算：

- **OCR 那条**（monitor 自带）认的是字幕**字面**：快、省，但"血"字可能是"血拼"，
  "喜"字可能是"喜欢"，而且画面里没字就什么都认不到；
- **CV 那条**（mood_cv）认的是画面**样子**：不识字也认得出阴森和喜庆，但它是零-shot，
  分数不标定，含蓄的画面它不敢开口（见 mood_cv 的 DEFAULT_MIN_SCORE）；
- **音频那条**（本轮**没有**，见下）本该认语气，可本机没有音源——要有音源就得常驻开麦
  或者新增系统声回环依赖，两条都踩线，所以本地档这根腿是空的。

**冲突时精细优先**：CV 是"看画面"认出来的，比 OCR 认字面更接近"这场戏在演什么"，
所以两边指向不同场景时，听 CV 的（`source` 会标成 `"cv"`）。两边一致就是 `"both"`。

**缺腿不塌**：权重只在**真有的腿**上归一化。音频腿是空的时候，它的权重不会变成
一个把总分压低 1/3 的空洞，而是按比例分给另外两条——所以今天（本地无音频）与
将来（云端补上音频）走的是同一段代码，不用改判据。

融合结果是**只存内存**的一个字符串加几个分数，不落盘、不落库、不发网络。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from life.screen.mood_cv import MoodVerdict
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR

logger = logging.getLogger(__name__)

# 三条腿的默认分量。CV 给满，因为它看的是画面本身；OCR 只认字面，给一半——
# 两边指向不同场景时，就算不看"精细优先"那条规则，光按分数也是 CV 赢。
DEFAULT_CV_WEIGHT = 1.0
DEFAULT_OCR_WEIGHT = 0.5
DEFAULT_AUDIO_WEIGHT = 1.0

SOURCE_CV = "cv"
SOURCE_OCR = "ocr"
SOURCE_BOTH = "both"
SOURCE_AUDIO = "audio"

# v2-mood-tri：只剩三态。`_SCENES` 是融合向量的**维度表**，哭泣整态删除后它也
# 跟着少一维——留着它会凭空多出一格恒为 0 的分数，赢家判定虽然不受影响，
# 但它会以"某个不存在的场景"的名义出现在日志与分数表里，属于挂起死配置。
_SCENES = (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE)


@dataclass(frozen=True)
class Fusion:
    """这一拍的判读结论。scene=None 表示三条腿都没说话（这一拍不成立）。"""

    scene: str | None = None
    source: str = ""                       # cv / both / ocr / audio / 空
    scores: dict[str, float] = field(default_factory=dict)

    @property
    def resolved(self) -> bool:
        return self.scene is not None


def fuse(
    ocr_scene: str | None,
    cv: MoodVerdict | None = None,
    audio_scores: dict[str, float] | None = None,
    *,
    cv_weight: float = DEFAULT_CV_WEIGHT,
    ocr_weight: float = DEFAULT_OCR_WEIGHT,
    audio_weight: float = DEFAULT_AUDIO_WEIGHT,
) -> Fusion:
    """把几条腿的判读合成一个三态。

    `ocr_scene` 是 monitor 那条 OCR 线认出来的（没有给 None）；
    `cv` 是 mood_cv 在这一帧上的判读（没把握 / 没模型给 None）；
    `audio_scores` 是三态名 → 分数（本轮恒为 None，留给云端档接）。
    """
    legs: list[tuple[str, dict[str, float], float]] = []

    if cv is not None and cv.scene:
        legs.append((SOURCE_CV, _scene_scores(cv.scene, cv.scores), cv_weight))
    if ocr_scene:
        legs.append((SOURCE_OCR, _scene_scores(ocr_scene), ocr_weight))
    if audio_scores:
        legs.append((SOURCE_AUDIO, _scene_scores(None, audio_scores), audio_weight))

    if not legs:
        return Fusion(None, "", {})

    # 只在真有的腿上归一：少一根腿时另外几根按比例顶上，总分不塌
    total_weight = sum(weight for _s, _v, weight in legs) or 1.0
    combined = {scene: 0.0 for scene in _SCENES}
    for _source, vector, weight in legs:
        share = weight / total_weight
        for scene in _SCENES:
            combined[scene] += vector.get(scene, 0.0) * share

    # 精细优先：CV 开了口就以它为准（它看的是画面，比认字面更接近"在演什么"）
    cv_scene = cv.scene if cv is not None and cv.scene else None
    if cv_scene:
        source = SOURCE_BOTH if cv_scene == ocr_scene else SOURCE_CV
        return Fusion(cv_scene, source, combined)

    # CV 没开口，那就看剩下的腿按分数谁赢
    winner = max(combined, key=lambda s: combined[s])
    if combined[winner] <= 0:
        return Fusion(None, "", combined)
    source = legs[0][0] if len(legs) == 1 else SOURCE_BOTH
    return Fusion(winner, source, combined)


def _scene_scores(scene: str | None, scores: dict[str, float] | None = None) -> dict[str, float]:
    """把一条腿的判断摊成三态向量。

    有完整的三态分数（CV 那种）就照抄；只有一个场景名（OCR 那种）就给那一档满分。
    """
    if scores and any(scores.get(s) for s in _SCENES):
        return {s: float(scores.get(s, 0.0)) for s in _SCENES}
    if scene in _SCENES:
        return {s: (1.0 if s == scene else 0.0) for s in _SCENES}
    return {s: 0.0 for s in _SCENES}


__all__ = [
    "DEFAULT_AUDIO_WEIGHT",
    "DEFAULT_CV_WEIGHT",
    "DEFAULT_OCR_WEIGHT",
    "Fusion",
    "SOURCE_AUDIO",
    "SOURCE_BOTH",
    "SOURCE_CV",
    "SOURCE_OCR",
    "fuse",
]
