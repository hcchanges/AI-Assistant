"""清空记忆与好感度（v2-reset-state）：把"它记住的事"与"你们处到哪儿了"一起归零。

**这个开关只动关系与记忆，别的一概不碰。** 说清楚边界比实现本身重要——用户按下
这个按钮时最怕的是"顺手把卡片/提醒/知识库也删了"，所以这里把两边都列全：

    清掉：pet.db 的 utterance（对话记录）、memory（长期记忆）
          mind.db 的 intimacy（好感度）、fragment（生活碎片）、
                  memory_book（记忆簿）、ep_summary（关系总述）
          两处向量索引（data/chroma 里的 memories 与 mind_fragments）

    留着：卡片与打卡、提醒、活动历史、情绪事件、星念（star_cards）、
          结构画像（struct_profile）、配置、知识库、技能、代码

为什么画像和星念留着：画像是"你是谁"（姓名职业家庭），星念是"一路走来的里程碑"，
两者都不是"它记着的某件事"——清了等于连宠物对这个人的基本认识一起抹掉，那是另一个
开关该干的事。为什么提醒留着：提醒是"你要我做的事"，不是"它记得的事"；对话记录没了
之后提醒上那根指回原话的线（`reminder.utter_id`）剪断，提醒本身照旧。

**没有回收站。** 清完就是清完，所以设置页那道按钮要连问两次（见 ui/settings.py）。
那条确认链是给用户看的，而这个模块不做任何"要不要清"的判断——调了 reset() 就真清。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, fields
from typing import Any

logger = logging.getLogger(__name__)


#: 每一项的中文名：给确认框列清单用。键与 ResetReport 的字段名一一对应，
#: 顺序就是清单上从上到下的顺序（对话 → 记忆 → 关系那几张表）。
ITEM_LABELS: dict[str, str] = {
    "utterance": "对话记录",
    "memory": "记住的事",
    "memory_vectors": "记忆的向量索引",
    "intimacy": "好感度",
    "fragment": "生活碎片",
    "fragment_vectors": "碎片的向量索引",
    "memory_book": "记忆簿",
    "ep_summary": "关系与情绪的总述",
    "pending_turns": "攒着还没提炼的对话",
}


@dataclass(frozen=True)
class ResetReport:
    """清掉了多少（每项都是**清之前**的条数）。只当返回值用，不做判断。"""

    utterance: int = 0
    memory: int = 0
    memory_vectors: int = 0
    intimacy: int = 0
    fragment: int = 0
    fragment_vectors: int = 0
    memory_book: int = 0
    ep_summary: int = 0
    pending_turns: int = 0

    def rows(self) -> list[tuple[str, int]]:
        """(中文名, 条数) 清单。**只列真的清掉了东西的那几项**——
        确认框里摆一排"0 条"不但没用，还让人以为要清很多。"""
        return [
            (ITEM_LABELS[f.name], int(getattr(self, f.name)))
            for f in fields(self)
            if int(getattr(self, f.name)) > 0
        ]

    def total(self) -> int:
        return sum(int(getattr(self, f.name)) for f in fields(self))

    def plain(self) -> str:
        """一句话交代：清掉了什么。什么都没清到就老实说没清到什么。"""
        rows = self.rows()
        if not rows:
            return "本来就没有记着的东西"
        return "清掉了 " + "、".join(f"{name} {count} 条" for name, count in rows)

    def detail(self) -> str:
        """多行清单（确认框里那几行）。"""
        return "\n".join(f"　· {name}：{count} 条" for name, count in self.rows())


class MemoryReset:
    """清关系与记忆。所有协作者都是**注入**的：哪个没接上就跳过哪一块。

    依赖全可选是有意的——情感记忆库（mind.db）打不开时程序照样能跑（见
    core/bootstrap.py 里那个 try），那时候这个开关也该能用，只是清单上少几项，
    而不是整个按钮消失。
    """

    def __init__(
        self,
        *,
        utterance_dao: Any = None,
        memory_dao: Any = None,
        memory_vectors: Any = None,
        mind_store: Any = None,
        mind_index: Any = None,
        distiller: Any = None,
        bus: Any = None,
    ):
        self._utterance_dao = utterance_dao
        self._memory_dao = memory_dao
        self._memory_vectors = memory_vectors
        self._mind_store = mind_store
        self._mind_index = mind_index
        self._distiller = distiller
        self._bus = bus

    # ---------- 只读 ----------

    def preview(self) -> ResetReport:
        """现在清的话会清掉多少。**纯读**，确认框拿它列清单。"""
        counts = self._mind_counts()
        return ResetReport(
            utterance=self._count(self._utterance_dao, "count_all"),
            memory=self._count(self._memory_dao, "count_all"),
            memory_vectors=self._count(self._memory_vectors, "count"),
            intimacy=counts.get("intimacy", 0),
            fragment=counts.get("fragment", 0),
            fragment_vectors=self._count(self._mind_index, "count"),
            memory_book=counts.get("memory_book", 0),
            ep_summary=counts.get("ep_summary", 0),
            pending_turns=int(getattr(self._distiller, "pending", 0) or 0),
        )

    def has_anything(self) -> bool:
        """有没有可清的东西。没有的话界面上那句提示换一种说法。"""
        return self.preview().total() > 0

    # ---------- 写 ----------

    def reset(self) -> ResetReport:
        """真清，返回**清之前**的条数（不是清完之后——那时全是 0，报不出东西）。

        顺序是"先对话、再记忆、最后关系"。这三者没有依赖，但按这个顺序读日志最顺：
        上面那层没了，下面那层才有意义。任何一步抛错都直接往外抛——半清的状态要让人
        知道，不能吞掉假装清完了。
        """
        before = self.preview()

        if self._utterance_dao is not None:
            self._utterance_dao.clear_all()
        if self._memory_dao is not None:
            self._memory_dao.clear_all()
        if self._memory_vectors is not None:
            self._memory_vectors.clear()
        if self._mind_store is not None:
            self._mind_store.clear_relationship_and_memory()
        if self._mind_index is not None:
            self._mind_index.clear()
        # 缓冲区最后丢：前面几步中间抛错的话，攒着的对话还在，不至于连"刚才说的那句"
        # 都跟着没了（那反倒不是用户按按钮时想要的结果）
        if self._distiller is not None:
            self._distiller.discard_pending()

        logger.info("记忆与好感度已清空：%s", before.plain())
        if self._bus is not None:
            # 设置页那两个列表（它记着的 / 记忆簿 / 亲密度）都挂着这条信号
            self._bus.memory_changed.emit()
        return before

    # ---------- 内部 ----------

    def _mind_counts(self) -> dict[str, int]:
        if self._mind_store is None:
            return {}
        try:
            return dict(self._mind_store.memory_counts())
        except Exception as exc:  # noqa: BLE001 —— 库读不动只是清单少几行，不该让确认框打不开
            logger.error("清空清单：情感记忆库读不出条数（%s）", exc)
            return {}

    @staticmethod
    def _count(target: Any, method: str) -> int:
        """取一个协作者的条数。没接上（None）或取不到都算 0——
        清单是给用户看的提示，不该因为它炸掉整个对话框。"""
        if target is None:
            return 0
        try:
            return int(getattr(target, method)() or 0)
        except Exception as exc:  # noqa: BLE001
            logger.error("清空清单：%s.%s() 取不到（%s）", type(target).__name__, method, exc)
            return 0
