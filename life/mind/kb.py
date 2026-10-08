"""知识库问答（v2-rag）：用户问"我之前记过的 X"，把本机存着的片段翻出来当上下文。

**这不是接了外网知识库，是把"你自己记过的东西"翻回去。** 三处来源，按可信度排：

    碎片（life/mind 加密库）  用户原话，A+ 那套双路召回；最该信的就是它
    笔记（data/notes/*.md）   抓屏笔记，只读扫盘；是"认出来的字"的产物，次一等
    记忆（pet.db memory 表）  老那条线留下的，A+ 那轮之后**已经不再生长**，可能空

后两条腿都靠**字面词**过滤（切词见 `_tokens`）：一个实词都不沾的就不算翻到。
碎片那条不用，因为 MindRecall 自己带字面打分；但 `MemoryStore.recall` 走的是
向量最近邻、**不带阈值**，库里只要有一条记忆它就必然回 top_k 条——不拦一道，
"我一条都没记过"这个分支就永远走不到。

**三条纪律。**

1. **检索全在本机。** 碎片走 chroma + bge，笔记走本地文件，一个字节都不出本机——
   "用户数据不外泄"是红线，不是取舍。所以这里没有、也不会有"检索走 API"的分支。
   出本机的只有最后拼好的那几行上下文，交给既有那条生成链（本地优先、无模型才回落 API）。
2. **只传检索到的那几条**，不传全库。条数与字数都有硬上限（`max_items` 条，每条 80 字）。
3. **宁可漏判。** 判"这是不是在问我记过的事"只用一张小词表：认不出来就当普通聊天，
   走原来那条路，一个字都不变。多认一句错的，代价是宠物张口就说"我记得你…"却记错了人。

**认出是问句但一条都没翻到**，那也要说清楚——返回的是"确实没记过"的交代，
不是空串。空串会让模型拿常识硬编一段"你上次说…"出来，那比不回答更糟。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from core.config import Config

logger = logging.getLogger(__name__)

# ---------- 判"这句是不是在问我记过的事" ----------

# 内置词表。都是**指回过去**的说法——「我之前记的」「我上次说」「你还记得」。
# 故意不收的（收进来就会误判成回忆）：
#   「我记得」——"我记得明天要开会"是在说事，不是在问我；
#   「帮我查」「找一下」——那更像要联网检索，不是翻我自己的库。
DEFAULT_ASK_WORDS: tuple[str, ...] = (
    "我之前记", "我之前说", "我之前提", "我之前写",
    "我上次说", "我上回说", "我上次提", "我上回提",
    "我之前跟你说", "我跟你说过", "我告诉过你", "我说过的", "我提过的",
    "我记过什么", "我记过的事", "我记过啥",
    "你还记得", "记不记得", "你还记着", "没忘吧",
    "帮你记着", "我存过", "我记下来了",
)

# 每条最多多少字：碎片本来就是短句，笔记那一行可能很长
_ITEM_CLIP = 80
# 扫笔记时最多读多少个文件（最新的在前）
DEFAULT_NOTE_FILES = 20
# 笔记文件里最多命中几行
_NOTE_LINES = 4
# 知识库目录（v2-kb-init）认哪些扩展名、最多扫几个文件、一份文档最多出几条、整条腿最多几条
_KB_SUFFIXES = (".md", ".txt")
DEFAULT_KB_FILES = 50
# 一份文档最多出几条：**不能是 1**。制度/手册类文档开头常有一句"本文档为公司……规定"，
# 它照样含"年假"这种关键词，一条的上限让这句前言把真正的条款挤掉——用户问"满 1 年几天"，
# 上下文里只剩前言，模型自然答不出来（v2-kb-init 真机查不到的根因）。3 条够覆盖
# "小节标题下的两三条要点"这个最常见的答案形状。
_KB_PER_FILE = 3
_KB_LINES = 8

# 泛词：**哪份文档里都可能有**的词，不该当检索线索。两处用（触发判定 + 行匹配）。
# 不滤的话，"我们公司几点下班"会因为制度文档里撞见"我们""公司"就被当成知识型问答，
# 把整段文档灌给模型——那正是用户说的"跑题"。
_GENERIC_WORDS = frozenset({
    "我们", "你们", "他们", "咱们", "大家", "自己",
    "公司", "单位", "部门", "这边", "那边",
    "怎么", "什么", "怎样", "如何", "为啥", "为什么",
    "多少", "几个", "几天", "几年", "多久",
    "有没有", "是不是", "能不能", "可不可以", "可以", "需要", "应该",
    "现在", "今天", "明天", "昨天", "时候", "哪里", "哪个", "这个", "那个",
    "一下", "请问", "告诉", "知道", "觉得", "的话", "还是", "然后",
})

# 拼给模型的那段话。**说清楚"这是原话、别改写"**——上下文是拿来说事的，不是拿来润色的
_HEAD = "他自己记过下面这些（是他的原话，别改写、别添油加醋）："
_EMPTY = (
    "关于这件事，我这边一条记录都没有。\n"
    "**如实说没记过**，不要编一条出来，也不要含糊其辞地装作记得。"
)


@dataclass(frozen=True)
class Hit:
    """翻到的一条。source 只用来给模型交代出处，不往界面上报。"""

    text: str
    source: str      # 碎片 / 笔记 / 记忆
    when: str        # 记下来的时间（拿不到就是空串）


class KnowledgeBase:
    """翻本机存着的东西。**只读**，不写任何一个库。"""

    def __init__(self, config: Config, recall: Any = None, memory_store: Any = None):
        self._config = config
        # 碎片那条腿（MindRecall）。不传就没有这一路——退化成只翻笔记与旧记忆
        self._recall = recall
        # pet.db 的长期记忆（A+ 之后已不再写入，留着是为了接住老数据）
        self._memory_store = memory_store

    # ---------- 入口 ----------

    @property
    def enabled(self) -> bool:
        return bool(self._config.rag.enabled)

    def is_question(self, text: str) -> bool:
        """这句话是不是在问我记过的事。命中词表就算，认不出来一律当不是。

        大小写不敏感（见 life/intent.py 的 _has 同款做法），中文不受影响。
        """
        if not self.enabled:
            return False
        body = (text or "").strip()
        if not body:
            return False
        lowered = body.lower()
        return any(word and word.lower() in lowered for word in self._ask_words())

    def context_if_question(self, text: str) -> str:
        """不是问"我记过的"、也不是"问知识库文档"就回空串。**这是接进回话链的口子**。

        单独立一个方法而不是让调用方自己先判：判与翻用的是同一张词表、同一个开关，
        分成两处迟早会走岔（判用了配置词表、翻用了内置词表这种）。

        v2-kb-init 加的"知识型问答"：回忆词表认不出的「年假怎么算」，靠问句实词命中
        知识库文档来认（`_knowledge_question`），命中了同样翻库。
        """
        if not self.is_question(text) and not self._knowledge_question(text):
            return ""
        return self.context(text)

    def context(self, text: str) -> str:
        """翻一遍三处来源，拼成一段给模型的上下文。**永远不返回空串**。

        翻到了就列出来；一条都没翻到就给"确实没记过"的交代——两者必须分得开，
        不然模型对着空串会自己编一段回忆出来。
        """
        hits = self.collect(text)
        if not hits:
            return _EMPTY
        lines = [_HEAD]
        for hit in hits:
            stamp = f"（{hit.source}{' ' + hit.when if hit.when else ''}）"
            lines.append(f"- {hit.text}{stamp}")
        return "\n".join(lines)

    def card_context(self, text: str, kb_root: Path | str | None) -> str:
        """卡内小助手的知识库上下文（v2-card-kb）：卡库优先、全局兜底，**只扫知识库目录**。

        跟 `context()` 的分工：那边是全局问答，四条腿全走、翻不到给"没记过"的交代；
        这条是卡窗专用——卡窗本来就该跟 pet.db / mind.db 隔离，所以这里只有
        "读 .md/.txt 文档"这一件事，碎片/笔记/旧记忆那三条腿一条都不碰。

        规则（护栏①）：卡库有命中就只给卡库；卡库为空 / 查不到 → 回退扫全局知识库；
        两边都查不到 → 回空串（**不是** `_EMPTY` 那种"没有记录"的交代——卡窗里查不到
        就该照常聊天，别冒一句"没有知识库"的错误）。
        """
        words = _key_tokens(_tokens(text))
        if not words:
            return ""
        hits = self._from_knowledge(words, kb_root)
        if not hits and kb_root:
            hits = self._from_knowledge(words)
        if not hits:
            return ""
        lines = ["这张卡的知识库里查到这些原文："]
        for hit in hits:
            lines.append(f"- {hit.text}")
        return "\n".join(lines)

    def collect(self, text: str) -> list[Hit]:
        """三处来源各翻一遍，去重后按上限截断。

        顺序就是可信度顺序：碎片在前（用户原话），笔记次之，旧记忆最后。
        截断**只按条数**——两条检索腿都不吐分数（见 recall.py 的说明），
        所以这里做不了"按相关度截断"，只能按来源优先级 + 上限条数。
        """
        hits: list[Hit] = []
        seen: set[str] = set()
        # 切一次词给下面两条腿共用（碎片那条腿自己有切词，不重复算）
        words = _tokens(text)
        # 知识库那条腿**另用一份滤过泛词的**：它匹配的是用户主动放进来的整份文档，
        # 拿"我们/公司"这种词去撞必然撞上，撞上就把整段文档灌给模型（跑题）。
        # 笔记/旧记忆那两条腿照旧用原词——这一轮只动知识库，不碰它们的口径。
        key = _key_tokens(words)
        for found in (
            self._from_fragments(text),
            self._from_knowledge(key),
            self._from_notes(words),
            self._from_memories(text, words),
        ):
            for hit in found:
                key = _clip(hit.text, _ITEM_CLIP)
                if not key or key in seen:
                    continue
                seen.add(key)
                hits.append(hit)
                if len(hits) >= self._config.rag.max_items:
                    return hits
        return hits

    # ---------- 三处来源 ----------

    def _from_fragments(self, text: str) -> list[Hit]:
        """碎片那条腿：直接复用 A+ 的双路召回（向量主召回 + 字面兜底）。

        复用而不是另起一个索引，有两个理由：一是它内部对向量腿有 try/except
        （向量塌了自动退回字面），二是"正文不进 chroma"那条隐私约定已经在这儿落实了。
        """
        if self._recall is None:
            return []
        try:
            found = self._recall.search(text, limit=self._config.rag.max_items)
        except Exception as exc:      # noqa: BLE001 —— 一条腿塌了不该让整次问答塌掉
            logger.error("知识库：碎片检索失败（%s），这次跳过这一路", exc)
            return []
        return [
            Hit(_clip(str(item.get("text") or ""), _ITEM_CLIP), "碎片",
                str(item.get("src_time") or "")[:10])
            for item in found
            if str(item.get("text") or "").strip()
        ]

    def _from_notes(self, words: list[str]) -> list[Hit]:
        """笔记那条腿：只读扫盘，最新的几个文件里找带这个词的行。

        笔记是 Markdown（## 时间　标题 / 列表要点），本来就是一行一件事，
        所以按行匹配就够，不用建索引。**只读**：open 之后一个字都不写回去。
        """
        root = Path(self._config.notes_dir)
        if not root.is_dir() or not words:
            return []
        hits: list[Hit] = []
        try:
            files = sorted(
                (path for path in root.glob("*.md") if path.is_file()),
                key=lambda path: path.name, reverse=True,
            )[:DEFAULT_NOTE_FILES]
        except OSError as exc:
            logger.error("知识库：笔记目录读不了（%s）", exc)
            return []
        for path in files:
            if len(hits) >= _NOTE_LINES:
                break
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("知识库：这条笔记读不了（%s）", exc)
                continue
            for line in body.splitlines():
                line = line.strip().lstrip("-*0123456789. ").strip()
                if not line or not any(word in line for word in words):
                    continue
                # 文件名就是那天的日期（YYYY-MM-DD.md），拿它当"什么时候记的"
                hits.append(Hit(_clip(line, _ITEM_CLIP), "笔记", path.stem))
                break          # 一个文件最多出一条，免得同一天的笔记把上下文占满
        return hits

    def _from_knowledge(self, words: list[str], root: Path | str | None = None) -> list[Hit]:
        """知识库目录那条腿（v2-kb-init）：只读扫盘，读用户放进去的 .md/.txt 文档。

        跟笔记腿一个套路：按行匹配实词，命中就出一条。区别有三——

        - 知识库文档是用户**主动放进去**的（公司制度、说明、手册这类），不是抓屏认字
          的产物，所以排在笔记前面、只排在用户原话（碎片）后面；
        - 一份文档**最多出 `_KB_PER_FILE` 条**，不是一条（真机的坑就在这儿，见那个常量的说明）；
        - 同一个文档里按**命中的实词个数**从多到少排，同分保持原文顺序——命中多的排前面。

        文件名当出处（来源标"知识库"）。`words` 是**滤过泛词**的那一份（见 collect）。
        `root` 给的是要扫的目录（v2-card-kb 的卡级库）；不给就扫全局 `knowledge_dir`。
        """
        root = Path(root) if root is not None else Path(self._config.knowledge_dir)
        if not root.is_dir() or not words:
            return []
        hits: list[Hit] = []
        try:
            files = self._kb_files(root)
        except OSError as exc:
            logger.error("知识库：知识库目录读不了（%s）", exc)
            return []
        for path in files:
            if len(hits) >= _KB_LINES:
                break
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                logger.debug("知识库：这份文档读不了（%s）", exc)
                continue
            for line in _match_lines(body, words)[:_KB_PER_FILE]:
                hits.append(Hit(_clip(line, _ITEM_CLIP), "知识库", path.stem))
                if len(hits) >= _KB_LINES:
                    break
        return hits

    @staticmethod
    def _kb_files(root: Path) -> list[Path]:
        """知识库目录里认得的文档（.md/.txt），按名字倒序（最新的在前）。

        卡级技能注册表（v2-life-skill 的 `cards/<卡>/skills.md`）也在这目录里，但它是
        **技能的元数据不是知识**——不该每次查库都灌给模型，这里按名跳过（文件名唯一一份
        在 `life.cards.skills.SKILLS_FILE`，此处只在跳过时引用）。
        """
        from life.cards.skills import SKILLS_FILE

        return sorted(
            (path for path in root.glob("*")
             if path.is_file() and path.suffix.lower() in _KB_SUFFIXES
             and path.name != SKILLS_FILE),
            key=lambda path: path.name, reverse=True,
        )[:DEFAULT_KB_FILES]

    def _from_memories(self, text: str, words: list[str]) -> list[Hit]:
        """旧记忆那条腿：pet.db 的长期记忆表。

        **A+ 那轮之后这张表已经不再写入了**（提炼改走加密库），所以它多半是空的；
        留着这一路只是为了接住升级前存下的老数据，别让它们白丢。

        **这条腿必须自己过一遍字面关。** `MemoryStore.recall` 走的是向量最近邻，
        **不返回分数、也没有阈值**——库里只要有一条记忆，它就一定会回 top_k 条，
        哪怕问的是八竿子打不着的南极科考站。照单全收的话，"我一条都没记过"这个
        分支就永远不会走到（context 总能有东西可列），模型反而会被塞进一堆不相干的
        旧记忆，张口就说"我记得你…"。所以这里按字面词过滤一道：一个实词都不沾的，
        就不算翻到。碎片那条腿不需要这道关——MindRecall 自己就带字面打分。
        """
        if self._memory_store is None or not words:
            return []
        try:
            found = self._memory_store.recall(text)
        except Exception as exc:      # noqa: BLE001 —— 见上
            logger.error("知识库：旧记忆检索失败（%s），这次跳过这一路", exc)
            return []
        hits: list[Hit] = []
        for item in found:
            content = str(getattr(item, "content", "") or "")
            if not content.strip() or not any(word in content for word in words):
                continue
            hits.append(Hit(_clip(content, _ITEM_CLIP), "记忆", ""))
        return hits

    # ---------- 内部 ----------

    def _knowledge_question(self, text: str) -> bool:
        """问句是不是在问知识库里的文档。只判"有没有命中"，不构建结果。

        v2-kb-init 加的触发：回忆词表认不出的「年假怎么算」，靠"问句实词命中知识库
        文档"来认。轻量判——找到第一个命中的文件就返回；完整结果由 `_from_knowledge`
        在 collect 里构建。普通聊天问不出文档里的词，自然不触发。

        判据用的是**滤过泛词的实词**（`_key_tokens`），不看"我们""公司""怎么"这类
        哪份文档都有的词——不然"我们公司几点下班"会因为在制度文档里撞见"公司"两个字
        就被当成知识型问答，把整段文档灌给模型。全句都是泛词时判 False，不触发。
        """
        words = _key_tokens(_tokens(text))
        if not words:
            return False
        root = Path(self._config.knowledge_dir)
        if not root.is_dir():
            return False
        try:
            files = self._kb_files(root)
        except OSError:
            return False
        for path in files:
            try:
                body = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if any(word in body for word in words):
                return True
        return False

    def _ask_words(self) -> tuple[str, ...]:
        """词表：配置里给了就用配置的（管理员可编辑），留空就用内置那张。"""
        words = tuple(self._config.rag.ask_words or ())
        return words or DEFAULT_ASK_WORDS


def _tokens(text: str) -> list[str]:
    """给笔记那一路切词：先把"问我记过的事"那层壳去掉，剩下的才是要找的实词。

    不去壳的话，"我之前记的"整串都成了关键词，笔记里一个字都对不上。
    直接用 recall.keywords 也不行——它会给长词补 2~3 字滑窗，
    "我之前记的好吃的"能切出"之前""记的"这种噪声词，白白把不相关的行捞进来。
    """
    from life.mind.recall import keywords

    body = text or ""
    for word in DEFAULT_ASK_WORDS:
        body = body.replace(word, " ")
    return [word for word in keywords(body) if len(word) >= 2]


def _clip(text: str, limit: int) -> str:
    body = " ".join((text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"


def _key_tokens(words: list[str]) -> list[str]:
    """把"我们 / 公司 / 怎么"这类**哪份文档里都可能有**的泛词滤掉，只留实词。

    只给知识库那条腿用（触发判定 + 行匹配），笔记与旧记忆那两条腿照旧用原词。
    实词滤光了就返回空表——"全句都是泛词"意味着这句话本来就没有可检索的线索，
    宁可判"不是在问知识库"，也别拿整份文档去撞。
    """
    return [word for word in words if word not in _GENERIC_WORDS]


def _match_lines(body: str, words: list[str]) -> list[str]:
    """一份文档里命中了实词的那些行，按"命中几个实词"从多到少排（同分保持原文顺序）。

    权重用**命中的实词个数**、不用命中次数：一行里"年假"出现三次并不比出现一次更贴题，
    但一行里同时含"年假""折算"两个词，明显更可能是答案那一条。

    空行与 Markdown 标题行（`#` 开头）不算正文——标题命中只说明这份文档讲这个主题，
    答案在它下面的条目里。
    """
    scored: list[tuple[int, int, str]] = []
    for index, raw in enumerate((body or "").splitlines()):
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        line = stripped.lstrip("-*0123456789. ").strip()
        if not line:
            continue
        score = sum(1 for word in words if word in line)
        if not score:
            continue
        scored.append((score, index, line))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [line for _, _, line in scored]


__all__ = ["DEFAULT_ASK_WORDS", "Hit", "KnowledgeBase"]
