"""记忆簿（B 养成）：把一天里记下的碎片和关系总述，收成一页。

**一天一页，不是一篇日记。** 这里不写"今天真是充实的一天"——那种话得靠模型编，
编出来是假的，还漏。这一页只做一件事：把那天**确实记下来的东西**归拢到一起，
一句话概括，底下列出原话。概括那句话是拼的，不是生成的：

    记下 3 件事；关系：在慢慢熟起来；情绪：工作压力大

这样写有两个好处：一来一个字都不上模型（跟主动搭话那条线同一条规矩），
二来它**可核对**——"记下 3 件事"底下就该有 3 条，数与内容对不上就是有 bug，
而编出来的散文没法核对。

**当天反复重写，不是追加。** 一天没过完，每提炼一轮就把这页重写一遍，
最后留下的自然是最全的那版。所以 `save_memory_book` 是 upsert 而不是 insert——
不然聊一天能攒出十几页半截的日记。跨天由 `date` 自动分开。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Callable

from life.mind.store import MindStore

logger = logging.getLogger(__name__)

# 一页最多列几条原话：日记是给人回看的，不是碎片库的副本
MAX_ENTRIES = 12
# 读碎片时最多往回看多少条（个人库远到不了这个量级）
SCAN_LIMIT = 500
# 概括里关系/情绪各留多少字
_CLIP = 28


class MemoryBook:
    """当日一页。接在提炼回调上（见 distill.MindDistiller._on_distilled）。"""

    def __init__(
        self,
        store: MindStore,
        now_provider: Callable[[], datetime] = datetime.now,
        intimacy: Any = None,
    ):
        self._store = store
        self._now = now_provider
        # 好感度（B+ 语义阶段）：只用来在页头写一句"现在到哪一档了"。
        # 不传就没有页头——记忆簿本来的样子，一行都不变
        self._intimacy = intimacy

    # ---------- 页头 ----------

    def header(self) -> str:
        """页头的一行：「知己 · 还差 180 到挚友」。没接好感度就是空串。

        记忆簿本来是"一天一页的事实归拢"，阶段名是唯一从外面借来的一句话——
        它标的是**这段关系现在处在哪**，不是这一天发生了什么，所以放页眉不放正文。
        """
        if self._intimacy is None:
            return ""
        try:
            return self._intimacy.stage_line()
        except Exception as exc:  # noqa: BLE001 —— 页头是附属信息，取不到不该让整页塌掉
            logger.error("记忆簿页头取不到阶段（%s），这页不写页头", exc)
            return ""

    # ---------- 写 ----------

    def record(self) -> dict[str, Any] | None:
        """把"今天"这页重写一遍。这天什么都没记下就不写（不产生空页）。

        幂等：同一天调多少次，库里都只有一行，内容是最后一次的样子。
        """
        day = self._now().date().isoformat()
        fragments = self._fragments_of(day)
        ep = self._store.ep()
        if not fragments and not ep.get("rel_state") and not ep.get("emotion_snapshot"):
            return None
        entries = [str(f.get("text") or "") for f in fragments if str(f.get("text") or "")]
        summary = _summarize(len(entries), ep)
        self._store.save_memory_book(day, summary, entries[:MAX_ENTRIES])
        logger.info("记忆簿：%s 这一页写好了（%d 条）", day, len(entries))
        return {"date": day, "summary": summary, "entries": entries[:MAX_ENTRIES]}

    # ---------- 读 ----------

    def today(self) -> dict[str, Any] | None:
        return self._store.memory_book(self._now().date().isoformat())

    def page(self, date: str) -> dict[str, Any] | None:
        return self._store.memory_book(date)

    def pages(self, limit: int = 7) -> list[dict[str, Any]]:
        return self._store.memory_books(limit=limit)

    def timeline(self, limit: int = 7) -> list[dict[str, Any]]:
        """给界面看的：每页带上原话明细（时间 + 关键词）。**纯读，不写库。**

        `pages()` 那份里 `entries` 只有文本——memory_book 表就存了这么几列。
        时间和关键词在 `fragment` 表里（`src_time` / `tags`），这里按「日期 + 原话」
        把两张表对上，**只为了显示**：表结构一个字不改，也不新增任何写库。

        对不上的原话**不丢**，只是时间和关键词留空——原话是这一页的主体，
        配不上注脚不该让整条消失。

        一次把碎片读出来建索引（而不是每页各扫一遍）：七页各扫 500 行就是七次解密。
        """
        index: dict[tuple[str, str], dict[str, Any]] = {}
        for fragment in self._store.fragments(limit=SCAN_LIMIT):
            text = str(fragment.get("text") or "")
            if not text:
                continue
            day = str(fragment.get("src_time") or "")[:10]
            # fragments() 是最新的在前，所以同日同文取到的是最新那条
            index.setdefault((day, text), fragment)

        out: list[dict[str, Any]] = []
        for page in self.pages(limit=limit):
            day = str(page.get("date") or "")
            entries: list[dict[str, Any]] = []
            for raw in page.get("entries") or []:
                text = str(raw)
                fragment = index.get((day, text))
                entries.append({
                    "text": text,
                    "time": str(fragment.get("src_time") or "") if fragment else "",
                    "tags": [str(tag) for tag in (fragment.get("tags") or [])] if fragment else [],
                })
            out.append({
                "date": day,
                "summary": str(page.get("summary") or ""),
                "entries": entries,
            })
        return out

    # ---------- 内部 ----------

    def _fragments_of(self, day: str) -> list[dict[str, Any]]:
        """那一天的碎片。按 `src_time` 的日期部分归堆——
        落库时间不算数，"昨天说的话今天被提炼出来"该记在昨天。"""
        return [
            fragment
            for fragment in self._store.fragments(limit=SCAN_LIMIT)
            if str(fragment.get("src_time") or "")[:10] == day
        ]


def _summarize(count: int, ep: dict[str, Any]) -> str:
    """拼一句概括。拼不出任何东西时给一句老实话，不留空串。"""
    bits: list[str] = []
    if count:
        bits.append(f"记下 {count} 件事")
    rel = _clip(ep.get("rel_state", ""))
    if rel:
        bits.append(f"关系：{rel}")
    emotion = _clip(ep.get("emotion_snapshot", ""))
    if emotion:
        bits.append(f"情绪：{emotion}")
    return "；".join(bits) if bits else "这一天没留下什么"


def _clip(text: Any, limit: int = _CLIP) -> str:
    body = " ".join(str(text or "").split())
    return body if len(body) <= limit else body[: limit - 1] + "…"
