# -*- coding: utf-8 -*-
"""线下观察写回对象卡（v2-date-card-writeback）的自动测试。

**这一批验什么**：

1. 观察 → 带标记的文字块：窗口 / 类型 / 阶段 / 已做测试都在，乱输入被压成一行；
2. 只追加到已存在的对象卡 `strategy`，线上字段（喜好 / 关系阶段 / 摘要）一个字不动；
3. 按 `peer_name` 隔离；没卡不建、查卡/保存失败不崩；
4. 同一条记录重复写不重复追加；
5. 测试只用假卡服务，不碰活库；源码没有建卡与 sqlite/DAO 路径。
"""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.date import writeback
from life.date.writeback import DateObservation, DateWriteback


class FakeCard:
    def __init__(self, card_id: int, peer: str, strategy: str = "",
                 archived: bool = False):
        self.id = card_id
        self.peer_name = peer
        self.strategy = strategy
        self.likes = "咖啡"
        self.dislikes = "烟味"
        self.relation_stage = "warmup"
        self.summary = "线上写下的摘要"
        self.archived = archived


class FakeCards:
    def __init__(self, cards):
        self.cards = list(cards)
        self.lookups: list[str] = []
        self.profile_calls: list[tuple[int, dict]] = []
        self.create_calls = 0
        self.fail_find = False
        self.fail_save = False

    def find_card_by_peer(self, peer_name):
        self.lookups.append(peer_name)
        if self.fail_find:
            raise RuntimeError("库炸了")
        for card in self.cards:
            if card.peer_name == peer_name and not card.archived:
                return card
        return None

    def set_profile(self, card_id, **fields):
        if self.fail_save:
            raise RuntimeError("写不进去")
        self.profile_calls.append((card_id, dict(fields)))
        for card in self.cards:
            if card.id == card_id:
                for key, value in fields.items():
                    setattr(card, key, value)

    def create_manual_card(self, *args, **kwargs):
        self.create_calls += 1
        raise AssertionError("写回不允许建卡")


def fixed_clock():
    return datetime(2026, 10, 22, 18, 30)


def main() -> int:
    section("① 观察 → 文字块")

    note = writeback.render_observation(DateObservation(
        peer_name="小A", window="18:00-21:00", date_type="咖啡厅",
        stage="warmup", tests_done=("nail", "hand_hold"),
    ), clock=fixed_clock)
    check("★ 带日期与窗口", "2026-10-22" in note and "18:00-21:00" in note, note)
    check("★ 类型 / 阶段 / 已做测试都在",
          "类型：咖啡厅" in note and "阶段：升温" in note
          and "指甲测试" in note and "过马路牵手" in note, note)
    check("带固定标记（便于后续检索）", note.startswith(writeback.MARKER), note[:20])
    check("整块没有换行", "\n" not in note and "\r" not in note, note)

    bare = writeback.render_observation(DateObservation(peer_name="小B"),
                                        clock=fixed_clock)
    check("没给的阶段写“未记”，不硬猜破冰", "阶段：未记" in bare, bare)
    check("没做测试写“无”", "已做测试：无" in bare, bare)
    check("没给类型就不写类型格", "类型：" not in bare, bare)

    unknown = writeback.render_observation(DateObservation(
        peer_name="小C", stage="不认识的阶段", tests_done=("不存在", "nail"),
    ), clock=fixed_clock)
    check("未知阶段不冒充破冰", "阶段：未记" in unknown, unknown)
    check("未知测试项忽略，已知项保留",
          "指甲测试" in unknown and "不存在" not in unknown, unknown)

    injected = writeback.render_observation(DateObservation(
        peer_name="小D", window="18:00\n21:00", date_type="咖啡\r\n厅",
        note="第一行\n第二行",
    ), clock=fixed_clock)
    check("★ 换行注入被压成一行", "\n" not in injected and "\r" not in injected,
          injected)
    check("备注也在块里", "备注：第一行 第二行" in injected, injected)

    section("② 追加语义")

    original = "线上原文：先聊她养的猫。"
    merged = writeback.append_note(original, note)
    check("★ 追加后原文完整保留", merged.startswith(original) and note in merged, "")
    check("重复追加同一块不变", writeback.append_note(merged, note) == merged, "")
    check("空原文 → 只留新块", writeback.append_note("", note) == note, "")
    check("空块 → 原样返回", writeback.append_note(original, "") == original, "")

    section("③ 写回成功：只动 strategy")

    card = FakeCard(7, "小A", strategy="线上原文：先聊她养的猫。")
    service = FakeCards([card, FakeCard(8, "小B", strategy="小B的原文")])
    writer = DateWriteback(service, clock=fixed_clock)
    result = writer.write(DateObservation(
        peer_name="小A", window="18:00-21:00", date_type="咖啡厅",
        stage="warmup", tests_done=("nail",),
    ))
    check("★ 写回成功、没建卡", result.written is True and result.created is False
          and result.card_id == 7, str(result.as_dict()))
    check("★ strategy 追加、原文还在", card.strategy.startswith("线上原文")
          and "【线下记录" in card.strategy, card.strategy)
    check("★ 只调用一次 set_profile，且只传 strategy",
          len(service.profile_calls) == 1
          and service.profile_calls[0][0] == 7
          and list(service.profile_calls[0][1]) == ["strategy"], str(service.profile_calls))
    check("★ 线上字段一个字没动",
          card.likes == "咖啡" and card.dislikes == "烟味"
          and card.relation_stage == "warmup" and card.summary == "线上写下的摘要", "")
    check("★ 没有走建卡路径", service.create_calls == 0, str(service.create_calls))
    check("★ 按 peer_name 查卡", service.lookups == ["小A"], str(service.lookups))

    section("④ 重复写 / 隔离 / 缺卡")

    before = card.strategy
    again = writer.write(DateObservation(
        peer_name="小A", window="18:00-21:00", date_type="咖啡厅",
        stage="warmup", tests_done=("nail",),
    ))
    check("★ 同一条记录不重复追加",
          again.written is False and card.strategy == before
          and len(service.profile_calls) == 1,
          f"{again.written}/{len(service.profile_calls)}")

    check("★ 另一张卡完全不受影响",
          service.cards[1].strategy == "小B的原文", service.cards[1].strategy)

    missing = writer.write(DateObservation(peer_name="小Z"))
    check("★ 没卡：不写、不建", missing.written is False and missing.created is False
          and "不新建" in missing.reason and service.create_calls == 0,
          missing.reason)

    lookups_before = len(service.lookups)
    blank = writer.write(DateObservation(peer_name="   "))
    check("★ 空对象名：连查都不查", blank.written is False
          and len(service.lookups) == lookups_before, str(service.lookups))

    archived = FakeCard(9, "小H", strategy="旧", archived=True)
    service2 = FakeCards([archived])
    out = DateWriteback(service2, clock=fixed_clock).write(
        DateObservation(peer_name="小H"))
    check("归档卡不算“已有卡”（与卡服务口径一致）",
          out.written is False and out.created is False, out.reason)

    section("⑤ 失败降级：查卡 / 保存 / 无 id")

    bad_find = FakeCards([FakeCard(1, "小A")])
    bad_find.fail_find = True
    out = DateWriteback(bad_find, clock=fixed_clock).write(
        DateObservation(peer_name="小A"))
    check("★ 查卡抛异常 → 返回失败，不崩",
          out.written is False and "查卡失败" in out.reason, out.reason)

    bad_save = FakeCards([FakeCard(2, "小A", strategy="原文")])
    bad_save.fail_save = True
    out = DateWriteback(bad_save, clock=fixed_clock).write(
        DateObservation(peer_name="小A", stage="warmup"))
    check("★ 保存抛异常 → 返回失败，不崩",
          out.written is False and "保存失败" in out.reason, out.reason)
    check("保存失败时原文没被改", bad_save.cards[0].strategy == "原文", "")

    class NoId:
        peer_name = "小A"
        strategy = ""

    class NoIdCards:
        def find_card_by_peer(self, peer_name):
            return NoId()

        def set_profile(self, card_id, **fields):
            raise AssertionError("没有 id 不该写")

    out = DateWriteback(NoIdCards(), clock=fixed_clock).write(
        DateObservation(peer_name="小A"))
    check("卡没有 int id → 不写", out.written is False and "id" in out.reason, out.reason)

    section("⑥ 输入收口 / 时间 / 红线")

    mapped = DateWriteback(FakeCards([FakeCard(3, "小M", strategy="")]),
                           clock=fixed_clock).write({
        "peer_name": "小M", "window": "晚上", "type": "散步",
        "stage": "破冰", "tests_done": "nail,unknown",
    })
    check("★ 支持字典输入 / tests_done 逗号串",
          mapped.written is True and "类型：散步" in mapped.note
          and "指甲测试" in mapped.note, mapped.note)

    ts_writer_card = FakeCard(4, "小T", strategy="")
    ts_writer = DateWriteback(FakeCards([ts_writer_card]), clock=fixed_clock)
    ts_writer.write(DateObservation(
        peer_name="小T", at=datetime(2026, 10, 22, 19, 0).timestamp()))
    check("时间戳输入能转成日期", "2026-" in ts_writer_card.strategy,
          ts_writer_card.strategy)

    long_note = writeback.render_observation(DateObservation(
        peer_name="小L", window="x" * 200, note="y" * 500,
    ), clock=fixed_clock)
    check("长输入被截断，块不无限膨胀",
          len(long_note) <= writeback.MAX_STRATEGY
          and long_note.count("x") <= writeback.MAX_TEXT, str(len(long_note)))

    result_dict = result.as_dict()
    try:
        json.dumps(result_dict, ensure_ascii=False)
        serializable = True
    except TypeError:
        serializable = False
    check("写回结果可 JSON 序列化", serializable, "")

    source = Path(writeback.__file__).read_text(encoding="utf-8")
    check("★ 源码没有建卡路径", "create_manual_card" not in source
          and "create_manual" not in source, "")
    check("★ 源码不碰 sqlite / DAO",
          not any(token in source for token in
                  ("sqlite3", "from data.dao", "import data.dao")), "")
    check("只依赖卡服务的 find_card_by_peer / set_profile",
          "find_card_by_peer" in source and "set_profile" in source, "")

    return summary("线下观察写回对象卡（v2-date-card-writeback）")


if __name__ == "__main__":
    raise SystemExit(main())
