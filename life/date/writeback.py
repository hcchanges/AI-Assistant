"""线下约会观察写回对象卡（v2-date-card-writeback）。

**只做一件事**：一次线下约会结束后，把「窗口 / 类型 / 阶段 / 已做测试」写成一段
带标记的文字，追加到**已存在的**那张对象攻略卡的 `strategy` 末尾。

**为什么追加而不是新开字段**：卡模型没有线下记录列，也不想为了这一批改表。`strategy`
本来就是一个自由文本的攻略字段，追加带 `【线下记录 …】` 前缀的块，既能保留线上
写下的原文，又能让后续起草把线下进展读进去。

**红线**：

- 只用 `find_card_by_peer(peer_name)` 找卡，**没有卡就不写、绝不新建**；
- 按 `peer_name` 隔离，不会把 A 的记录写到 B 的卡上；
- 只调用 `set_profile(card_id, strategy=...)` 改一个字段；likes / dislikes /
  relation_stage / summary 等线上字段一个字不碰；
- 模块本身不 import sqlite / DAO——真实调用方传 `CardService`，测试传假的即可。

用法：

    writer = DateWriteback(card_service)
    result = writer.write(DateObservation(
        peer_name="小A", window="18:00-21:00", date_type="咖啡厅",
        stage="warmup", tests_done=("nail",),
    ))
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Mapping, Protocol

from life.cards import date_rules as dr

logger = logging.getLogger(__name__)

MARKER = "【线下记录 "
MAX_TEXT = 40          # 窗口 / 类型这类短字段的上限
MAX_NOTE = 120         # 备注上限
MAX_STRATEGY = 4000    # 追加后的 strategy 上限（超出就截断本次块，不删旧文）


def _clean(value: Any, limit: int = MAX_TEXT) -> str:
    """压成一行：换行/制表/连续空白都变一个空格，再按长度截断。"""
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    return text[:limit]


def _moment(at: Any, clock: Callable[[], Any] | None = None) -> datetime | None:
    value = at if at is not None else (clock() if callable(clock) else None)
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        try:
            return datetime.fromtimestamp(float(value))
        except (OverflowError, OSError, ValueError):
            return None
    return None


@dataclass(frozen=True)
class DateObservation:
    """一次线下约会的结构化观察。字段都是短文本 / 枚举，不放原始媒体。"""

    peer_name: str
    window: str = ""
    date_type: str = ""
    stage: str = ""
    tests_done: tuple[str, ...] = ()
    note: str = ""
    at: Any = None          # datetime / 时间戳；不给就用 clock()

    @classmethod
    def from_mapping(cls, data: Mapping[str, Any] | None) -> "DateObservation":
        source = data if isinstance(data, Mapping) else {}
        tests = source.get("tests_done") or ()
        if isinstance(tests, str):
            tests = tuple(item for item in tests.split(",") if item.strip())
        elif not isinstance(tests, (list, tuple)):
            tests = ()
        return cls(
            peer_name=str(source.get("peer_name") or ""),
            window=str(source.get("window") or ""),
            date_type=str(source.get("date_type") or source.get("type") or ""),
            stage=str(source.get("stage") or ""),
            tests_done=tuple(str(item) for item in tests),
            note=str(source.get("note") or ""),
            at=source.get("at"),
        )


@dataclass(frozen=True)
class WritebackResult:
    written: bool = False
    created: bool = False          # 永远是 False：这一层没有建卡路径
    card_id: int | None = None
    note: str = ""
    reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "written": self.written,
            "created": self.created,
            "card_id": self.card_id,
            "note": self.note,
            "reason": self.reason,
        }


class CardServiceLike(Protocol):
    """只依赖卡服务这两个方法（真实的是 `life.cards.service.CardService`）。"""

    def find_card_by_peer(self, peer_name: str) -> Any: ...

    def set_profile(self, card_id: int, **fields: str) -> None: ...


def render_observation(observation: DateObservation,
                       clock: Callable[[], Any] | None = None) -> str:
    """观察 → 一段带标记的文字块。纯函数，测试直接调。"""
    moment = _moment(observation.at, clock)
    day = moment.strftime("%Y-%m-%d") if moment is not None else "日期未记"
    window = _clean(observation.window)
    date_type = _clean(observation.date_type)
    raw_stage = _clean(observation.stage)
    stage_name = "未记"
    if raw_stage:
        if raw_stage.lower() in dr.STAGES:
            stage_name = dr.STAGES[raw_stage.lower()].name
        else:
            for key, stage in dr.STAGES.items():
                if raw_stage == stage.name:
                    stage_name = stage.name
                    break

    names: list[str] = []
    for key in observation.tests_done:
        test = dr.TESTS.get(str(key).strip())
        if test is not None and test.name not in names:
            names.append(test.name)
    tests_text = "、".join(names) or "无"

    header = f"{MARKER}{day}"
    if window:
        header += f"｜{window}"
    header += "】"
    parts = []
    if date_type:
        parts.append(f"类型：{date_type}")
    parts.append(f"阶段：{stage_name}")
    parts.append(f"已做测试：{tests_text}")
    note = _clean(observation.note, MAX_NOTE)
    if note:
        parts.append(f"备注：{note}")
    block = header + "；".join(parts)
    return block[:MAX_STRATEGY]


def append_note(existing: Any, note: str) -> str:
    """把新块追加到旧 strategy 后面；原块原样保留，完全相同的块不重复写。"""
    current = str(existing or "").rstrip()
    block = str(note or "").strip()
    if not block:
        return current
    if block in current:
        return current
    if not current:
        return block
    return current + "\n\n" + block


class DateWriteback:
    """对外的写回入口：找卡 → 追加 → 只改 strategy。失败只返回结果，不抛。"""

    def __init__(self, cards: CardServiceLike,
                 clock: Callable[[], Any] | None = None):
        self._cards = cards
        self._clock = clock

    def write(self, observation: DateObservation | Mapping[str, Any]) -> WritebackResult:
        obs = observation if isinstance(observation, DateObservation) \
            else DateObservation.from_mapping(observation)
        peer = _clean(obs.peer_name, 80)
        if not peer:
            return WritebackResult(reason="没有对象名，找不到卡（不新建）")
        try:
            card = self._cards.find_card_by_peer(peer)
        except Exception as exc:      # noqa: BLE001 —— 查卡失败不崩
            logger.info("线下写回查卡失败：%s", exc)
            return WritebackResult(reason=f"查卡失败：{exc}")
        if card is None:
            return WritebackResult(reason="没有这张对象卡（不新建卡）")

        card_id = getattr(card, "id", None)
        if not isinstance(card_id, int):
            return WritebackResult(reason="对象卡没有可用的 id")
        note = render_observation(obs, self._clock)
        existing = getattr(card, "strategy", "")
        merged = append_note(existing, note)
        if merged == str(existing or "").rstrip():
            return WritebackResult(card_id=card_id, note=note,
                                   reason="这条记录已经在卡上，不重复写")
        try:
            self._cards.set_profile(card_id, strategy=merged)
        except Exception as exc:      # noqa: BLE001 —— 写失败不崩
            logger.info("线下写回保存失败：%s", exc)
            return WritebackResult(card_id=card_id, note=note,
                                   reason=f"保存失败：{exc}")
        return WritebackResult(written=True, card_id=card_id, note=note)


__all__ = [
    "MARKER",
    "DateObservation",
    "WritebackResult",
    "DateWriteback",
    "render_observation",
    "append_note",
]
