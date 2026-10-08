"""
清空记忆与好感度（v2-reset-state）的自动测试。

对着指令的三条：

1. **关系与记忆真的清干净了** —— pet.db 的对话记录 / 长期记忆、mind.db 的
   好感度 / 碎片 / 记忆簿 / 关系总述，外加两处向量索引，全都归零；
2. **别的东西一件不动** —— 卡片与打卡、提醒、活动历史、情绪记录、星念、
   结构画像、配置都还在；
3. **设置页那个入口要问两次** —— 第一次拒绝就什么都不动，两次都点头才清。

外加两条自己钉住的边界：

- **提醒是"你要我做的事"，不是"它记着的事"**：清完之后提醒还在，只是那根指回
  原话的线（`reminder.utter_id`）剪断了；
- **清单是照真实条数报的**，不是写死的文案（预览数 == 真清的数）。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QMessageBox

from harness import check, drain_threads, make_env, pump, section, summary

from core.reset import MemoryReset, ResetReport

#: 这一轮塞进库里、等着被清掉的东西
TALKS = ["今天有点累", "楼下那家包子铺不错", "周末想去爬山"]


# ---------- 造数据 / 读数 ----------


def _seed(app) -> None:
    """把"该清的"和"该留的"都摆上。两边都要有，才验得出清的是不是只有一边。"""
    user_id = app.user.id

    first = None
    for line in TALKS:
        utter = app.utterance_dao.add(user_id, line)
        first = first or utter.id
    app.memory_store.remember("fact", "爱吃楼下包子铺的鲜肉包", 0.8)
    app.memory_store.remember("preference", "周末喜欢去爬山", 0.6)

    app.mind_store.add_fragment("楼下那家包子铺的鲜肉包真不错", ["早饭"])
    app.mind_store.save_intimacy(3, 120, ["closer_tone", "frequent"], "")
    app.mind_store.save_memory_book("2026-10-03", "记下 1 件事", ["楼下那家包子铺的鲜肉包真不错"])
    app.mind_store.save_ep(rel_state="在慢慢熟起来", emotion_snapshot="工作压力大")
    # 不该清的两件：星念与画像
    app.mind_store.add_star_card("第一次一起看电影", "milestone")
    app.mind_store.save_profile(name="何澄", occupation="工程师")

    # 提醒：留着，但它指着的那句原话要被清掉
    app.reminder_dao.add(
        user_id, datetime.now() + timedelta(hours=2), "提醒我喝水", first
    )
    # 活动历史：留着。起点**不许越过今天零点**：这条测试半夜跑（比如 00:13）时，
    # "now - 30 分"会落到昨天，today_total_seconds() 就数成 0——本来是"该留的东西
    # 还在不在"的红，会假报成"活动历史被清掉了"。夹到零点，多晚跑都数得出来。
    now = datetime.now()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    app.activity_dao.record(
        user_id, "code.exe", "开发", max(midnight, now - timedelta(minutes=30)), now
    )
    # 卡片：留着
    app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")

    # 提炼缓冲区：攒着还没提炼的一句话，清完不该自己长回库里
    app.config.mind.enabled = True
    app.mind_distiller.note_turn("今天有点累")

    # 建卡那条线是**后台**写记忆的（见 life/cards/service.py 的 _remember_card）：
    # 不等它落地，"清前有多少条"就会被异步写插队，数字对不上。这里等干净了再数——
    # 要验的是清空这一下清得准不准，不是异步调度快不快。
    drain_threads()
    pump(0.2)


def _pet_counts(app) -> dict[str, int]:
    return {
        "utterance": app.utterance_dao.count_all(),
        "memory": app.memory_dao.count_all(),
        "reminder": len(app.reminder_dao.list_all(limit=999)),
        "card": len(app.card_service.plaza()),
        "activity": app.activity_dao.today_total_seconds(),
        "star_cards": app.mind_store.star_card_count(),
    }


def _mind_counts(app) -> dict[str, int]:
    return dict(app.mind_store.memory_counts())


def _vector_counts(app) -> dict[str, int]:
    return {
        "memory": app.vector_store.count(),
        "fragment": app.mind_index.count(),
    }


# ---------- ① 服务本身 ----------


def test_reset_clears_exactly_the_relationship_and_memory() -> None:
    section("R-① 清空：该清的都清了，该留的一件没动")
    box, stub, app = make_env()
    try:
        _seed(app)
        before = _pet_counts(app)
        mind_before = _mind_counts(app)
        vec_before = _vector_counts(app)

        check("对话记录真攒上了", before["utterance"] == len(TALKS), str(before))
        check("长期记忆真写进去了（正文 + 向量一一对上）",
              before["memory"] >= 2 and vec_before["memory"] == before["memory"],
              f"pet.db={before['memory']} 向量={vec_before['memory']}")
        check("记的那两条真在（不是数字对、内容空）",
              {"爱吃楼下包子铺的鲜肉包", "周末喜欢去爬山"}
              <= {m.content for m in app.memory_store.recent(50)},
              str([m.content for m in app.memory_store.recent(50)]))
        check("碎片与它的向量都在", mind_before["fragment"] == 1 and vec_before["fragment"] == 1,
              f"{mind_before} / {vec_before}")
        check("好感度是 L3", app.mind_store.intimacy()["level"] == 3,
              str(app.mind_store.intimacy()))

        preview = app.memory_reset.preview()
        check("**清单照真实条数报**：每一项都跟库里数出来的一样",
              (preview.utterance, preview.memory, preview.intimacy, preview.fragment,
               preview.memory_book, preview.ep_summary, preview.pending_turns)
              == (app.utterance_dao.count_all(), app.memory_dao.count_all(),
                  mind_before["intimacy"], mind_before["fragment"],
                  mind_before["memory_book"], mind_before["ep_summary"],
                  app.mind_distiller.pending),
              f"{preview} / 库={before} {mind_before}")
        check("清单上真列着六件以上（不是没数到东西就报空）",
              preview.total() >= 9 and len(preview.rows()) >= 5,
              f"共 {preview.total()} 条：{preview.rows()}")
        check("向量那两项也在清单里，且与两个库各自对得上",
              (preview.memory_vectors, preview.fragment_vectors)
              == (vec_before["memory"], vec_before["fragment"]),
              f"记忆向量={preview.memory_vectors} 碎片向量={preview.fragment_vectors}")

        done = app.memory_reset.reset()
        check("reset() 报的是**清之前**的条数（清完再数就全是 0 了）",
              done == preview, f"清前={preview} 返回={done}")

        after = _pet_counts(app)
        check("对话记录清零", after["utterance"] == 0, str(after["utterance"]))
        check("长期记忆清零", after["memory"] == 0, str(after["memory"]))
        check("两处向量索引都清零",
              _vector_counts(app) == {"memory": 0, "fragment": 0}, str(_vector_counts(app)))
        check("碎片 / 记忆簿 / 关系总述都清零",
              _mind_counts(app) == {"intimacy": 0, "fragment": 0, "memory_book": 0,
                                    "ep_summary": 0},
              str(_mind_counts(app)))
        check("好感度回到刚认识那档（L1 / 0 经验）",
              app.mind_store.intimacy()["level"] == 0 and app.mind_store.intimacy()["exp"] == 0,
              str(app.mind_store.intimacy()))
        check("关系总述读回来是空的（不是残留上一句）",
              app.mind_store.ep()["rel_state"] == "" and not app.mind_store.ep()["events"],
              str(app.mind_store.ep()))
        check("记忆簿读回来也没有了", app.mind_store.memory_books() == [],
              str(app.mind_store.memory_books()))
        check("提炼缓冲区也丢了（不然过几分钟记忆自己长回来）",
              app.mind_distiller.pending == 0, f"pending={app.mind_distiller.pending}")

        kept = _pet_counts(app)
        check("卡片一张不少",
              kept["card"] == before["card"] and kept["card"] >= 3
              and any(card.title == "少喝奶茶" for card, _pos in app.card_service.plaza()),
              f"清前={before['card']} 清后={kept['card']}")
        check("活动历史还在（清前清后秒数一样，且真数得出来）",
              kept["activity"] == before["activity"] and kept["activity"] > 0,
              f"清前={before['activity']} 清后={kept['activity']}")
        check("提醒还在", kept["reminder"] == before["reminder"] == 1, str(kept["reminder"]))
        check("星念还在", kept["star_cards"] == before["star_cards"] == 1,
              str(kept["star_cards"]))
        check("结构画像还在（清的是记忆，不是「你是谁」）",
              app.mind_store.profile().get("name") == "何澄", str(app.mind_store.profile()))

        reminders = app.reminder_dao.list_all(limit=9)
        check("提醒那根指回原话的线剪断了（utter_id 置空，提醒本身留着）",
              reminders and reminders[0].utter_id is None,
              f"utter_id={reminders[0].utter_id if reminders else '（没有提醒）'}")
        check("提醒的内容一个字没动", reminders and reminders[0].content == "提醒我喝水",
              reminders[0].content if reminders else "")
    finally:
        box.cleanup()
        drain_threads()


def test_reset_is_idempotent_and_notifies() -> None:
    section("R-② 再清一次不炸；清完通知界面刷新")
    box, stub, app = make_env()
    try:
        _seed(app)
        hits: list[int] = []
        app.bus.memory_changed.connect(lambda: hits.append(1))

        app.memory_reset.reset()
        check("清完发了 memory_changed（设置页那几个列表靠它刷新）",
              len(hits) == 1, f"{len(hits)} 次")
        check("清完 has_anything() 变假", app.memory_reset.has_anything() is False)

        again = app.memory_reset.reset()
        check("空着再清一次：不抛错，报出来是 0", again.total() == 0, f"{again}")
        check("报告里那句老实话", again.plain() == "本来就没有记着的东西", again.plain())
    finally:
        box.cleanup()
        drain_threads()


def test_missing_collaborators_do_not_break_it() -> None:
    section("R-③ 协作者没接上（库打不开那台机器）也照样能用")
    empty = MemoryReset()
    check("什么都没接：清单是空的、能清、报 0",
          empty.preview().total() == 0 and empty.reset().total() == 0)
    check("东西一件都没接时不说「清掉了 0 条」这种废话",
          empty.preview().rows() == [], str(empty.preview().rows()))

    class Broken:
        """数不出条数（库读不动），但清得动。"""

        def count_all(self) -> int:
            raise RuntimeError("库读不动")

        def clear_all(self) -> int:
            return 0

    broken = MemoryReset(utterance_dao=Broken())
    check("有一件取不到数：清单少那一项，不炸", broken.preview().utterance == 0)
    check("清单整份还是能用（不是抛错就是空对象）",
          broken.preview().total() == 0 and broken.preview().plain() == "本来就没有记着的东西",
          broken.preview().plain())
    broken.reset()      # 不该抛
    check("那件清不动也不抛（清的是别人，不该被它带塌）", True)

    class Exploding:
        """一清就炸。**这种错必须往外抛**——半清的状态不能让上层以为清干净了。"""

        def clear_all(self) -> int:
            raise RuntimeError("删不动")

    raised = ""
    try:
        MemoryReset(utterance_dao=Exploding()).reset()
    except RuntimeError as exc:
        raised = str(exc)
    check("清到一半真炸了：错抛出来，不吞", raised == "删不动", raised or "（被吞了）")

    check("报告只列真的清到东西的那几项",
          [name for name, _ in ResetReport(utterance=3, memory=0).rows()] == ["对话记录"],
          str(ResetReport(utterance=3, memory=0).rows()))
    check("报告那句话把数字带出来",
          "对话记录 3 条" in ResetReport(utterance=3).plain(), ResetReport(utterance=3).plain())


def test_counts_are_really_read_from_the_store() -> None:
    section("R-④ 清单不是写死的文案（换一个真实的库，数字跟着变）")
    box, stub, app = make_env()
    try:
        _seed(app)
        base = app.memory_reset.preview()
        app.utterance_dao.add(app.user.id, "又多说了一句")
        preview = app.memory_reset.preview()
        check("多写一句对话，清空清单跟着 +1",
              preview.utterance == base.utterance + 1 == len(TALKS) + 1,
              f"{preview.utterance}（原 {base.utterance}）")
        check("同一次预览里别项没被带偏", preview.memory == base.memory,
              f"记忆 {base.memory} → {preview.memory}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- ② 设置页那个入口 ----------


def _dialog(app):
    import ui.settings as settings_mod

    return settings_mod.SettingsDialog(
        config=app.config, bus=app.bus,
        pet_profile_dao=app.pet_profile_dao, activity_dao=app.activity_dao,
        category_dao=app.app_category_dao, memory_store=app.memory_store,
        reminder_dao=app.reminder_dao, jobs=app.jobs, user_id=app.user.id,
        intimacy=app.mind_intimacy, memory_book=app.mind_memory_book,
        reset=app.memory_reset,
    )


def _answer(answer: int):
    """把两个确认框都换成"照剧本回答"，返回恢复用的原函数对。"""
    import ui.settings as settings_mod

    original = (settings_mod.QMessageBox.question, settings_mod.QMessageBox.warning)
    settings_mod.QMessageBox.question = staticmethod(lambda *a, **k: answer)
    settings_mod.QMessageBox.warning = staticmethod(lambda *a, **k: answer)
    return original


def _restore(original) -> None:
    import ui.settings as settings_mod

    settings_mod.QMessageBox.question, settings_mod.QMessageBox.warning = original


def test_settings_entry_asks_twice() -> None:
    section("R-⑤ 设置页：第一次就拒绝 → 一件都没动")
    box, stub, app = make_env()
    dialog = None
    original = _answer(QMessageBox.Cancel)
    try:
        _seed(app)
        cards_before = len(app.card_service.plaza())
        dialog = _dialog(app)
        check("记忆管理页上有那个按钮",
              hasattr(dialog, "_reset_button")
              and dialog._reset_button.text() == "清空记忆与好感度",
              getattr(getattr(dialog, "_reset_button", None), "text", lambda: "（没有）")())
        check("还没清时清单里有东西（按钮不是摆设）",
              app.memory_reset.preview().total() > 0)

        dialog._reset_button.click()
        check("点了「取消」：对话记录一条不少",
              app.utterance_dao.count_all() == len(TALKS),
              str(app.utterance_dao.count_all()))
        check("好感度也还在 L3", app.mind_store.intimacy()["level"] == 3,
              str(app.mind_store.intimacy()["level"]))
        check("状态栏说明白了没清",
              "没有清" in dialog._status.text() or "不用清" in dialog._status.text(),
              dialog._status.text())

        section("R-⑥ 设置页：两次都点头 → 清完并刷新界面")
        _restore(original)
        original = _answer(QMessageBox.Yes)
        dialog._reset_button.click()
        check("对话记录清零", app.utterance_dao.count_all() == 0,
              str(app.utterance_dao.count_all()))
        check("好感度归零", app.mind_store.intimacy()["exp"] == 0,
              str(app.mind_store.intimacy()))
        check("「它记着的」那个列表当场空了",
              dialog._memory_list.count() == 0, f"{dialog._memory_list.count()} 行")
        # 记忆簿那页头（「陌生 · 还差 30 到熟络」）不是记忆，是"现在到哪一档了"，
        # 清完之后它反而更该在——所以这里判的是"一天的记录一行都不剩"，
        # 不是"列表空了"（列表还留着那行页头）。
        book_rows = [dialog._book_list.item(i).text() for i in range(dialog._book_list.count())]
        check("记忆簿里一天的记录都不剩了（页头那句阶段话还在）",
              not any("2026-" in row for row in book_rows) and len(book_rows) <= 1,
              str(book_rows))
        check("页头说的是刚认识那档", any("陌生" in row for row in book_rows), str(book_rows))
        check("状态栏报了清掉什么（带真实数字）",
              "已清空" in dialog._status.text() and "对话记录" in dialog._status.text(),
              dialog._status.text())
        check("卡片照样在（不在这个开关的范围内）",
              len(app.card_service.plaza()) == cards_before >= 3
              and any(card.title == "少喝奶茶" for card, _pos in app.card_service.plaza()),
              f"清前={cards_before} 清后={len(app.card_service.plaza())}")
    finally:
        _restore(original)
        if dialog is not None:
            dialog.deleteLater()
        box.cleanup()
        drain_threads()


def test_settings_button_absent_when_not_wired() -> None:
    section("R-⑦ 没接这个服务时，记忆管理页跟以前一模一样（按钮不出现）")
    box, stub, app = make_env()
    import ui.settings as settings_mod

    dialog = settings_mod.SettingsDialog(
        config=app.config, bus=app.bus,
        pet_profile_dao=app.pet_profile_dao, activity_dao=app.activity_dao,
        category_dao=app.app_category_dao, memory_store=app.memory_store,
        reminder_dao=app.reminder_dao, jobs=app.jobs, user_id=app.user.id,
        intimacy=app.mind_intimacy, memory_book=app.mind_memory_book,
    )
    try:
        check("没传 reset：压根没有那个按钮", not hasattr(dialog, "_reset_button"))
        check("别的块照旧（它记着的 / 提醒都还在）",
              hasattr(dialog, "_memory_list") and hasattr(dialog, "_reminder_list"))
    finally:
        dialog.deleteLater()
        box.cleanup()
        drain_threads()


def test_bootstrap_wires_the_service() -> None:
    section("R-⑧ 装配：应用里那个服务接的是同一个库、同一个索引")
    box, stub, app = make_env()
    try:
        check("应用上有 memory_reset", hasattr(app, "memory_reset"))
        app.utterance_dao.add(app.user.id, "随手说一句")
        check("它读的就是这只宠物的库（不是新开一个）",
              app.memory_reset.preview().utterance == 1
              and app.memory_reset.preview().utterance == app.utterance_dao.count_all(),
              f"清单={app.memory_reset.preview().utterance} 库={app.utterance_dao.count_all()}")
        app.mind_store.add_fragment("随手记一条")
        check("碎片写进去，清单当场就看得见（接的是同一个 mind.db）",
              app.memory_reset.preview().fragment == 1,
              str(app.memory_reset.preview().fragment))
    finally:
        box.cleanup()
        drain_threads()


if __name__ == "__main__":
    for case in (
        test_reset_clears_exactly_the_relationship_and_memory,
        test_reset_is_idempotent_and_notifies,
        test_missing_collaborators_do_not_break_it,
        test_counts_are_really_read_from_the_store,
        test_settings_entry_asks_twice,
        test_settings_button_absent_when_not_wired,
        test_bootstrap_wires_the_service,
    ):
        case()
    # 把失败数带成退出码：run_all.py 是按子进程退出码判"这个模块过没过"的，
    # 只调 summary() 的话退出码恒为 0——哪怕有红也会被记成"通过"（口径缺口）。
    raise SystemExit(summary())
