"""
安抚语料库（指令 26-A）：三级情绪 × 五个主题的共情话术，存 data/comfort_library.json，管理员可编辑。

两个用途：
1. 注入提示词——按这次的级别与主题挑 2~3 条当参考示例，让模型照着这个分寸说自己的话
   （是"分寸标尺"，不是照念，见 comfort_hint）；
2. 判级判题——情绪分切三级，关键词兜住"崩溃 / 连续失眠"这类一听就重的说法，
   话题归到五个主题之一（都没命中就按"情绪低落"给最泛用的那套）。

阈值的坐标系与出卡是一致的：heavy 就是 life/cards/threshold.py 里的强情绪线（0.7），
免得"重到该有人陪着"和"够格出卡"各说各话。
"""

from __future__ import annotations

import json
import logging
import random
from dataclasses import dataclass
from pathlib import Path

from core.paths import bundle_dir

logger = logging.getLogger(__name__)

# 语料库文件：跟别的语料一样放在 data/ 下，改语气只改这个 JSON
LIBRARY_PATH = bundle_dir() / "data" / "comfort_library.json"

LIGHT = "light"
MEDIUM = "medium"
HEAVY = "heavy"
LEVELS = (LIGHT, MEDIUM, HEAVY)

MEDIUM_SCORE = 0.4      # 到这个分数算"中"（明显不好受）
HEAVY_SCORE = 0.7       # 到这个分数算"重"（与卡牌的强情绪线同一条）

# 主题：五个，判不出就按最后这个给最泛用的一套
DEFAULT_TOPIC = "情绪低落"

# 三级在提示词里的说法（写在示例前面，让模型知道这次该用什么分寸）
LEVEL_LABELS = {LIGHT: "轻（有点累、有点烦）", MEDIUM: "中（压力大、焦虑）", HEAVY: "重（崩溃、连续失眠）"}

# 一听就重的说法：不看分数，直接按"重"处理
HEAVY_WORDS: tuple[str, ...] = (
    "崩溃", "撑不住", "撑不下去", "扛不住", "熬不住", "绝望",
    "连续失眠", "好几晚", "几天没睡", "整夜没睡", "喘不过气", "受不了了",
)

TOPIC_WORDS: dict[str, tuple[str, ...]] = {
    "加班": (
        "加班", "赶工", "通宵", "方案", "报告", "汇报", "项目", "开会", "会议",
        "老板", "领导", "客户", "指标", "考核", "绩效", "交付",
    ),
    "失眠": (
        "失眠", "睡不着", "睡不好", "没睡好", "熬夜", "翻来覆去", "半夜", "噩梦",
        "睡不够", "早醒", "醒得早", "睡不踏实",
    ),
    "通勤": (
        "地铁", "公交", "打车", "堵车", "通勤", "排队", "迟到", "挤", "路上", "班车",
    ),
    "饮食挫败": (
        "外卖", "油腻", "奶茶", "做饭", "体重", "减肥", "胖", "吃", "没胃口",
        "凑合", "夜宵", "泡面", "节食", "嘴馋",
    ),
    "情绪低落": (
        "难过", "委屈", "想哭", "没劲", "提不起", "低落", "难受", "烦躁",
        "孤独", "空落", "烦", "不想说话", "压抑", "没意思",
    ),
}


@dataclass(frozen=True)
class ComfortSample:
    """一条参考话术：empathy 是先共情，company 是陪着（不说教）。"""

    empathy: str
    company: str


def level_of(emotion_score: float, text: str = "") -> str:
    """这句话该按哪一级安抚：轻（有点累/烦）/ 中（压力大/焦虑）/ 重（崩溃、连续失眠）。

    "崩溃""撑不下去"这类说法分量不看分数，直接判重——它们常常分数不高，
    但说出来的人和听的人都知道这不是小事。
    """
    if any(word in (text or "") for word in HEAVY_WORDS):
        return HEAVY
    if emotion_score >= HEAVY_SCORE:
        return HEAVY
    if emotion_score >= MEDIUM_SCORE:
        return MEDIUM
    return LIGHT


def topic_of(text: str) -> str:
    """这句话落在哪个主题：命中最多的那个；都没命中就用最泛用的「情绪低落」。"""
    best = DEFAULT_TOPIC
    best_hits = 0
    for topic, words in TOPIC_WORDS.items():
        hits = sum(1 for word in words if word in (text or ""))
        if hits > best_hits:
            best, best_hits = topic, hits
    return best


class ComfortLibrary:
    """安抚语料库：按（级别，主题）取参考话术。

    文件是给管理员改的，读坏了只记一条日志、当作空库——少几句参考话术，
    话还是照说，不能让一次手抖把聊天整个卡住。
    """

    def __init__(self, path: Path | str = LIBRARY_PATH):
        self._path = Path(path)
        self._data: dict[str, dict[str, list[ComfortSample]]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("读不到安抚语料库 %s：%s", self._path, exc)
            return
        for level, topics in (raw or {}).items():
            if level.startswith("_") or level not in LEVELS or not isinstance(topics, dict):
                continue
            self._data[level] = {
                str(topic): [
                    ComfortSample(
                        empathy=str(item.get("empathy") or "").strip(),
                        company=str(item.get("company") or "").strip(),
                    )
                    for item in (items or [])
                    if isinstance(item, dict)
                ]
                for topic, items in topics.items()
            }

    def samples(self, level: str, topic: str, limit: int = 3) -> tuple[ComfortSample, ...]:
        """按级别与主题随机抽几条（同一句话问几次，参考的示例也不同）。"""
        by_topic = self._data.get(level) or {}
        pool = by_topic.get(topic) or by_topic.get(DEFAULT_TOPIC) or []
        if not pool:
            return ()
        picked = random.sample(pool, k=min(limit, len(pool)))
        return tuple(picked)

    def comfort_hint(self, emotion_score: float, text: str, limit: int = 3) -> str:
        """拼出要注入提示词的那一段：这次是几级、该照什么分寸说。

        没情绪（分数为 0，平静或开心）就不注入——那种话不需要安抚口径。
        """
        if emotion_score <= 0:
            return ""
        level = level_of(emotion_score, text)
        topic = topic_of(text)
        samples = self.samples(level, topic, limit=limit)
        if not samples:
            return ""
        lines = "\n".join(f"- 共情：{item.empathy} 陪伴：{item.company}" for item in samples)
        return (
            f"【这次的语气分寸：{LEVEL_LABELS[level]}｜话题：{topic}】\n"
            "下面几条是同分寸的参考话术，只看分寸、不要照抄；你要说自己的话：\n"
            f"{lines}\n"
            "还有一件要紧的：你记得的事里如果和这次有关，用你自己的话带一句，"
            "别生硬复述、也别硬凑。"
        )


# 全局一份就够：语料是只读的，重复读文件没意义
_library: ComfortLibrary | None = None


def library() -> ComfortLibrary:
    """取那份共用的语料库（第一次用时加载）。"""
    global _library
    if _library is None:
        _library = ComfortLibrary()
    return _library
