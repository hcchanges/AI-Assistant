"""场景标识符的唯一定义处：恐怖 / 愤怒 / 笑容。

抽成独立小模块是因为它被两条线同时要：monitor 那条 OCR 线拿它判场景，
mood_cv 那条 CV 线拿它当零-shot 的类别标签。两边都去 import 对方就成环了，
所以标识符落在这个谁都不依赖的叶子上，谁都能拿。

monitor 照旧把它们再导出一次（`from life.screen.scenes import SCENE_HORROR` 之后
名字仍然绑在 monitor 的命名空间里），所以原先 `from life.screen.monitor import
SCENE_HORROR` 那些写法一个都不用改。

**v2-mood-tri（用户决定）**：`SCENE_CRY`（哭泣）已**整态删除**，标识符不再存在。
理由不是"它不好认"，而是四态在 CLIP 嵌入空间里相互挤——悲伤夹在愤怒与恐惧中间
（怒↔悲 0.0638、悲↔恐 0.0716 是六对里最近的两对），既是这四态里弃权率最高的一态
（单帧 42.65%），也是把另外两态往下拽的那块。砍掉它换三态分离度，值。
**样例图不删**（`E:\\电子宠物\\样例\\表情样例\\悲伤\\` 原样躺在盘上），只是不再参与编码；
哪天要恢复，重新接回这四个口子即可。详见收尾报告第九十六节。
"""

from __future__ import annotations

SCENE_HORROR = "horror"
SCENE_ANGRY = "angry"
SCENE_FESTIVE = "festive"

# 并列时的优先级：命中数一样多，按这个顺序取（恐怖最要紧，先让位给惊悚）。
# v2-mood-detect：幽默（SCENE_FUNNY）已删；v2-mood-tri：哭泣（SCENE_CRY）已删，只剩三态。
SCENE_ORDER: tuple[str, ...] = (
    SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE,
)

SCENE_TITLES: dict[str, str] = {
    SCENE_HORROR: "恐怖",
    SCENE_ANGRY: "愤怒",
    # v2-mood-screen ④：这一档的语义从"喜庆"改成了"笑容"（识别人脸微笑/大笑，
    # 不再靠红色喜庆画面）。标识符仍是 SCENE_FESTIVE，只是中文名换。
    SCENE_FESTIVE: "笑容",
}

__all__ = [
    "SCENE_ANGRY",
    "SCENE_FESTIVE",
    "SCENE_HORROR",
    "SCENE_ORDER",
    "SCENE_TITLES",
]
