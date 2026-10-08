"""社交回复的风格范例（v2-social-fewshot）：全局范例 + 对象攻略卡范例，双路 few-shot。

起草社交回话时，除了"对方说了什么"（那条走 `social.py` 的截屏 OCR），还差一层
**口吻**——同一句话，不同的人有不同的说法。这一层用两路范例喂：

1. **全局范例**：用户在 `<项目上一级>/聊天范例/` 里放自己的聊天片段（.md / .txt），
   按与这次对话的字面重合挑最相关的几条。
2. **卡级范例**：对象攻略卡专属知识库里的《聊天范例.md》——用户放**跟这一个人**的
   历史对话片段。哪张卡是"这一个人"，靠**名字 / 关键词**认：拿这次对话的原话去比
   各张攻略卡《对方信息.md》的内容，命中词最多的那张算数。

**只读、只学语气。** 两路都只读用户自己放进来的文件，不写回任何一个字节；范例进
提示词时明说"只学语气口吻、别照抄内容"——范例里括号里的策略分析（废测 / 框架 /
可得性……）**原样保留**，当"回复 + 解析"的格式锚让模型学。**零新依赖**：切词复用
`life.mind.recall.keywords`。

一条都不命中就回空串——提示词里那格填"没有可参考的范例"，**绝不现编**。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from core.config import Config

logger = logging.getLogger(__name__)

#: 对象攻略卡的专属知识库里，"这一个人是谁"记在这份文件里（跟 `life/cards/chat.py`
#: 的 `GUIDE_KB_FILE` 同名同物——那张卡是靠它才算"对象攻略卡"）。
GUIDE_KB_FILE = "对方信息.md"
#: 卡级范例的落点（同一目录下，用户自己放）。
CARD_EXAMPLE_FILE = "聊天范例.md"

# 认哪些扩展名、最多读几个文件、整段范例最多几条、每条最多多少字
_EXAMPLE_SUFFIXES = (".md", ".txt")
MAX_EXAMPLE_FILES = 20
MAX_EXAMPLE_ITEMS = 4
ITEM_CLIP = 220


def _read_chunks(path: Path) -> list[str]:
    """把一份范例文件拆成一条一条：**按行切**，每行算一条。

    用户的范例（聊天回复案例-范例版.md）是**连续对话转写**——一行一句、段间几乎没有
    空行，策略分析用括号挂在回复行上（"……（废测 / 框架 / 可得性……）"）。按空行分块
    会把整份转写塌成一段、再被截字，括号分析全丢。所以这里按行切：每个非空行算一条，
    回复行连同括号里的策略分析**原样保留**（当"回复 + 解析"的格式锚）；标题 / 分隔线
    （`#`、`---`、`>` 开头的）不算。旧的"段间空行、一段一句"写法照样能读——那类文件
    每段通常就是一行，按行切与按空行切结果相同。

    读不出来只记日志、回空表——范例是辅助，丢了不该挡住起草。
    """
    try:
        body = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("社交范例：%s 读不出来（%s），跳过", path, exc)
        return []
    out: list[str] = []
    for line in body.splitlines():
        text = " ".join(line.split())
        if not text or text.startswith(("#", "---", ">")):
            continue
        out.append(text[:ITEM_CLIP])
    return out


def _keywords(text: str) -> set[str]:
    """切出用于"像不像同一个人/同一个话题"的实词（2 字以上）。

    复用回忆那条链的切词（`life.mind.recall.keywords`），不另造一套——多一套切词就多
    一处会跟别处走岔的地方。切不出来（空串 / 全标点）就是空集，不硬凑。
    """
    from life.mind.recall import keywords

    return {word for word in keywords(text or "") if len(word) >= 2}


class SocialExamples:
    """两路风格范例的取用口。**只读盘，不写任何库。**"""

    def __init__(
        self,
        config: Config,
        cards_provider: Callable[[], list] | None = None,
    ):
        self._config = config
        # 攻略卡从哪儿来（装配处接 `CardService` 的卡册）。不接就只有全局范例这一路。
        self._cards = cards_provider if cards_provider is not None else (lambda: [])

    # ---------- 全局范例 ----------

    def global_chunks(self) -> list[str]:
        """`<聊天范例目录>` 下所有 .md/.txt，按文件名倒序（新的在前）读成一条条。"""
        root = Path(self._config.social_examples_dir)
        if not root.is_dir():
            return []
        chunks: list[str] = []
        try:
            files = sorted(
                (p for p in root.glob("*") if p.is_file() and p.suffix.lower() in _EXAMPLE_SUFFIXES),
                key=lambda p: p.name,
                reverse=True,
            )[:MAX_EXAMPLE_FILES]
        except OSError as exc:
            logger.debug("社交范例：目录读不了（%s）", exc)
            return []
        for path in files:
            chunks.extend(_read_chunks(path))
        return chunks

    # ---------- 卡级范例 ----------

    def guide_card_for(self, query: str):
        """这次对话是跟哪一个"对象"——拿 `query` 去撞各张攻略卡的《对方信息.md》。

        认法（用户定的）：切出 `query` 的实词，跟每张卡《对方信息.md》的实词求交，
        命中最多、且不为零的那张算数。**零命中就不认**（宁可不摆卡级范例，也不拿
        别人的聊天片段去凑）；否则两张卡各沾一个词也会乱配。

        只认"有《对方信息.md》"的卡——那正是对象攻略卡的标志，别的卡没有这一路。
        """
        words = _keywords(query)
        if not words:
            return None
        best, best_score = None, 0
        for card in self._cards():
            root = getattr(card, "kb_path", None)
            if not root:
                continue
            info = Path(root) / GUIDE_KB_FILE
            try:
                if not info.is_file():
                    continue
                body = info.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("社交范例：%s 读不出来（%s）", info, exc)
                continue
            score = len(words & _keywords(body))
            if score > best_score:
                best, best_score = card, score
        return best

    def card_chunks(self, query: str) -> list[str]:
        """命中的那张攻略卡的《聊天范例.md》（没命中就空表）。"""
        card = self.guide_card_for(query)
        if card is None:
            return []
        root = getattr(card, "kb_path", None)
        if not root:
            return []
        return _read_chunks(Path(root) / CARD_EXAMPLE_FILE)

    # ---------- 拼给提示词 ----------

    def for_query(self, query: str) -> str:
        """两路范例合起来，挑最相关的几条拼成一段。一条都没有就回空串。

        排序按"跟这次对话的实词重合数"从多到少，同分保持原序（全局在前、卡级在后）。
        `query` 一般给"对方那条 + 用户这次的处境"——两边都得沾上才算相关。
        """
        words = _keywords(query)
        if not words:
            # 这次对话一个实词都切不出来（空串 / 全标点）：**别硬塞范例**。没有线索就
            # 挑不出"最相关的几条"，随便摆几条反而会把不相干的语气带进来。
            return ""
        chunks = self.global_chunks() + self.card_chunks(query)
        if not chunks:
            return ""
        order = sorted(range(len(chunks)), key=lambda i: -len(words & _keywords(chunks[i])))
        return "\n".join(f"- {chunks[i]}" for i in order[:MAX_EXAMPLE_ITEMS])


__all__ = [
    "CARD_EXAMPLE_FILE",
    "GUIDE_KB_FILE",
    "MAX_EXAMPLE_ITEMS",
    "SocialExamples",
]
