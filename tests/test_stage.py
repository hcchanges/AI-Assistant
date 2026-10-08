"""B+ 语义阶段 + 知己深化的自动测试。

覆盖：
- S1 阶段映射：L1~L5 → 陌生/熟络/朋友/知己/挚友；stage_line 的进度说法；
- S2 知己档：L4 起 proactive 有"分享由头"（**宠物说自己的想法，不接用户的话**）、
  遇"求助/犹豫"反问拿主意、L5 用私密称呼 + 上面都还在；
- S3 展示：记忆簿页头、设置页亲密度块的阶段标签；
- S4 红线：pet.db 零写入、危机句仍最高优先（连模型都不调）、无新依赖。

等级用 `mind_intimacy.add()` 直接喂到指定档——等级映射本身是这一轮要验的东西，
"怎么涨经验"在 test_mind.py 里已经用真实信号压过了。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.dialog import router
from life.mind import intimacy as intimacy_mod
from life.mind import proactive as proactive_mod

LEVEL_EXP = {1: 0, 2: 30, 3: 100, 4: 220, 5: 400}


def say(app, stub, text: str) -> None:
    stub.parse_result = {
        "scene": text[:12], "time_hint": "", "time_iso": "", "time_confidence": "none",
        "emotion": "平静", "module": "其他", "command": "", "reply": "嗯嗯",
    }
    app.chatter_service.handle(text)
    wait_for(lambda: app.mind_distiller.pending > 0, timeout=3.0)


def _memory_rows(pet_path: Path) -> int:
    conn = sqlite3.connect(str(pet_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    finally:
        conn.close()


def _goto(app, level: int) -> None:
    """把好感度喂到某一档（幂等：只补差额）。"""
    state = app.mind_intimacy.state()
    need = LEVEL_EXP[level] - state["exp"]
    if need > 0:
        app.mind_intimacy.add(need, "测试用")


def main() -> int:
    section("S1 阶段映射：L1~L5 → 五档说法")
    want = {1: "陌生", 2: "熟络", 3: "朋友", 4: "知己", 5: "挚友"}
    for level, name in want.items():
        rule = intimacy_mod.rule_for_exp(LEVEL_EXP[level])
        check(f"L{level} → {name}", rule.level == level and rule.title == name,
              f"L{rule.level}「{rule.title}」")
    check("STAGE_NAMES 就是那五档", intimacy_mod.STAGE_NAMES == want, str(intimacy_mod.STAGE_NAMES))
    check("stage_for_level 越界也兜得住",
          intimacy_mod.stage_for_level(9) == "挚友" and intimacy_mod.stage_for_level(0) == "陌生",
          f"{intimacy_mod.stage_for_level(0)} / {intimacy_mod.stage_for_level(9)}")
    check("门槛前一点还在上一档", intimacy_mod.rule_for_exp(219).level == 3,
          f"219 → L{intimacy_mod.rule_for_exp(219).level}")
    check("title 与 stage 是同一套词（不存在两套名字）",
          all(r.title == intimacy_mod.STAGE_NAMES[r.level] for r in intimacy_mod.LEVELS), "")

    section("S1 各档解锁什么")
    unlocks = {r.level: set(r.unlocks) for r in intimacy_mod.LEVELS}
    check("L3 没有分享/反问/昵称",
          not ({"share_topic", "ask_back", "nickname"} & unlocks[3]), str(sorted(unlocks[3])))
    check("L4 有分享与反问、还没有昵称",
          {"share_topic", "ask_back"} <= unlocks[4] and "nickname" not in unlocks[4],
          str(sorted(unlocks[4])))
    check("L5 全都有（昵称 + 前面那些强化项）",
          {"share_topic", "ask_back", "nickname"} <= unlocks[5], str(sorted(unlocks[5])))
    check("低档不会反过来多出高档的东西",
          unlocks[1] == set() and "closer_tone" not in unlocks[1], str(sorted(unlocks[1])))

    box, stub, app = make_env()
    try:
        pet_path = app.config.db_path
        mind = app.mind_intimacy
        pro = app.mind_proactive
        # 时间钉死：分享由头是从表里轮着挑的（起点跟日期走），得让它是可复现的
        pro._now = lambda: datetime(2026, 10, 2, 15, 0, 0)

        section("S1/S3 stage_line：进度说法")
        check("L1 起步就是「陌生」", mind.stage_line().startswith("陌生"), mind.stage_line())
        _goto(app, 4)
        check("L4 显示「知己 · 还差 180 到挚友」",
              mind.stage_line() == "知己 · 还差 180 到挚友", mind.stage_line())
        _goto(app, 5)
        check("L5 满档不报数字", mind.stage_line() == "挚友 · 已经是最亲近的了", mind.stage_line())

        section("S2 分享由头：宠物说自己的想法（L4+）")
        # 先回到 L3，确认这条由头**根本不存在**（不是降级说别的）
        app.mind_store.save_intimacy(3, LEVEL_EXP[3], list(intimacy_mod.LEVELS[2].unlocks), "")
        pro._last_topic, pro._last_topic_at = "", None
        check("L3 时分享由头不出现", pro._share() == ("", ""), str(pro._share()))

        _goto(app, 4)
        pro._last_topic, pro._last_topic_at = "", None
        topic, line = pro._share()
        check("L4 起有分享由头了", bool(topic and line), f"{topic} / {line}")
        check("说的是宠物自己的想法", line in proactive_mod.SHARE_LINES, line)
        check("不接用户的话（不出现「上次你说」）", "上次你说" not in line, line)
        check("话题带 share: 前缀，好做重复抑制", topic.startswith("share:"), topic)

        pro._last_topic, pro._last_topic_at = topic, pro._now()
        topic2, line2 = pro._share()
        check("同一句不追着重复说", topic2 != topic and line2 != line, f"{topic2} / {line2}")

        # 真走一遍 check()：没有正事的时候，由头真能排出去
        pro._last_topic, pro._last_topic_at = "", None
        pro._live_emotion = ""
        busy = [name for name, fn in (("跟进", pro._follow_up), ("情绪", pro._emotion_care),
                                      ("纪念日", pro._anniversary), ("久别", pro._reunion))
                if fn()[1]]
        check("这会儿没有正事由头抢在前面", busy == [], str(busy))
        spoken: list[str] = []
        pro._queue_speak = spoken.append
        line3 = pro.check()
        check("check() 真把分享排了出去（不是只出现在函数里）",
              bool(line3) and line3 in proactive_mod.SHARE_LINES, line3)
        check("排出去的话真的到了说话那条线", spoken == [line3], str(spoken))

        # 上面那步是猴子补丁在收话。撤掉补丁走真出口再看一遍：
        # check() → ProactiveService.queue_mind → 下个检查点 → bus.pet_speak_proactive
        # （v2-proactive-voice 起，宠物自己开口那条线跟回话的 pet_speak 分开了）
        pro._queue_speak = app.proactive_service.queue_mind
        svc = app.proactive_service
        app.config.schedule.quiet_hours = ["23:59", "23:59"]   # 别撞上免打扰
        app.config.activity.remind = True                      # 那是活动提醒的总闸，不是这条线的
        svc._is_user_present = lambda: True                    # 这儿当人就在电脑前
        svc._last_spoken = float("-inf")
        svc._last_interaction = float("-inf")
        svc._count_today, svc._count_date = 0, datetime.now().date()
        heard: list[str] = []
        app.bus.pet_speak_proactive.connect(heard.append)
        pro._last_topic, pro._last_topic_at = "", None
        line4 = pro.check()
        check("排进了主动搭话那条线自己的队列", bool(line4) and svc._mind_pending == line4,
              f"pending={svc._mind_pending!r}")
        svc._check()
        check("下个检查点真说出来了（一路到主动说话那个出口）", heard == [line4], str(heard))
        check("说完队列清空、今天开口次数记上",
              svc._mind_pending == "" and svc._count_today == 1,
              f"pending={svc._mind_pending!r} count={svc._count_today}")

        section("S2 遇纠结反问拿主意（L4+ 的 ask_back）")
        help_route = router.classify("这事怎么办啊")
        hes_route = router.classify("我该不该换工作")
        calm_route = router.classify("今天天气不错")
        check("「怎么办」判成求助", help_route.intent == router.INTENT_HELP, help_route.intent)
        check("「该不该」判成犹豫（不是新意图，是犹豫词）",
              router.is_hesitating("我该不该换工作") and hes_route.intent != router.INTENT_HELP,
              f"hesitating={router.is_hesitating('我该不该换工作')} intent={hes_route.intent}")
        check("普通话不判成犹豫", not router.is_hesitating("今天天气不错"), "")

        hint_help = mind.ask_back_hint(help_route, "这事怎么办啊")
        hint_hes = mind.ask_back_hint(hes_route, "我该不该换工作")
        check("求助 → 反问提示", "想听听你的主意" in hint_help, hint_help[:40])
        check("犹豫 → 也反问", "想听听你的主意" in hint_hes, hint_hes[:40])
        check("普通话 → 一个字都不注入", mind.ask_back_hint(calm_route, "今天天气不错") == "", "")

        app.mind_store.save_intimacy(3, LEVEL_EXP[3], list(intimacy_mod.LEVELS[2].unlocks), "")
        check("L3 时就算求助也不反问", mind.ask_back_hint(help_route, "这事怎么办啊") == "", "")
        _goto(app, 4)

        section("S2 反问提示真进了提示词（走真实回话链）")
        stub.messages.clear()
        app.psych_service.respond("我该不该换工作")
        wait_for(lambda: bool(stub.messages), timeout=5.0)
        drain_threads()
        pump(0.1)
        blob = "\n".join(m.get("content", "") for m in (stub.messages[-1] if stub.messages else []))
        check("模型收到的提示里有反问那句", "想听听你的主意" in blob, blob[-80:] if blob else "（没发出去）")

        stub.messages.clear()
        app.psych_service.respond("今天天气不错")
        wait_for(lambda: bool(stub.messages), timeout=5.0)
        drain_threads()
        pump(0.1)
        blob2 = "\n".join(m.get("content", "") for m in (stub.messages[-1] if stub.messages else []))
        check("普通话那趟没注入反问", "想听听你的主意" not in blob2, "")

        section("S2 昵称：L5 才用，没给过就不硬凑")
        _goto(app, 5)
        app.config.mind.intimate_nickname = ""
        tone_no_name = mind.tone_suffix()
        check("没给昵称 → 不出现「私底下你叫他」", "私底下你叫他" not in tone_no_name, tone_no_name[-40:])
        check("没给昵称也还是最亲近那档", "最亲近" in tone_no_name, tone_no_name[-40:])

        app.config.mind.intimate_nickname = "小鱼"
        tone_named = mind.tone_suffix()
        check("给了昵称 → 提示词里带上它", "「小鱼」" in tone_named, tone_named[-40:])
        check("L5 分享/反问仍在（强化，不是替换）",
              "说说你自己" in tone_named and mind.has("ask_back") and mind.has("share_topic"), "")

        app.mind_store.save_intimacy(4, LEVEL_EXP[4], list(intimacy_mod.LEVELS[3].unlocks), "")
        check("L4 时昵称不出现（那是 L5 的）", "「小鱼」" not in mind.tone_suffix(), "")
        _goto(app, 5)

        section("S3 记忆簿页头")
        app.mind_store.add_fragment("楼下那家包子铺的鲜肉包真不错", ["早饭"])
        app.mind_memory_book.record()
        check("记忆簿页头 == 阶段标签", app.mind_memory_book.header() == mind.stage_line(),
              app.mind_memory_book.header())
        check("页头就是当前档", "挚友" in app.mind_memory_book.header(), app.mind_memory_book.header())

        section("S3 设置页亲密度块")
        import ui.settings as settings_mod
        # 先把昵称写进配置，再看设置页会不会把它回填出来（不是手工 setText 出来的）
        app.config.mind.intimate_nickname = "小鱼"
        dialog = settings_mod.SettingsDialog(
            config=app.config, bus=app.bus,
            pet_profile_dao=app.pet_profile_dao, activity_dao=app.activity_dao,
            category_dao=app.app_category_dao, memory_store=app.memory_store,
            reminder_dao=app.reminder_dao, jobs=app.jobs, user_id=app.user.id,
            intimacy=app.mind_intimacy, memory_book=app.mind_memory_book,
        )
        try:
            check("设置页显示阶段标签", dialog._stage_label.text() == mind.stage_line(),
                  dialog._stage_label.text())
            rows = [dialog._book_list.item(i).text() for i in range(dialog._book_list.count())]
            check("记忆簿列表带一行页头", any(mind.stage_line() in r for r in rows), str(rows[:2]))
            today = app.mind_memory_book.today() or {}
            check("记忆簿列表有当天那页（列出概括那行）",
                  bool(today) and any(today.get("summary", "") in r for r in rows), str(rows[:3]))
            check("当天那页确实收进了那条碎片",
                  today.get("entries") == ["楼下那家包子铺的鲜肉包真不错"],
                  str(today.get("entries")))
            check("设置页把已给的昵称回填了出来",
                  dialog._nickname_edit.text() == "小鱼", dialog._nickname_edit.text() or "（空的）")
            dialog._nickname_edit.setText("阿宝")
            ok = dialog._collect_fields()
            check("改完昵称保存 → 真写进配置",
                  ok and app.config.mind.intimate_nickname == "阿宝",
                  f"collect={ok} nickname={app.config.mind.intimate_nickname!r}")
        finally:
            dialog.deleteLater()
        app.config.mind.intimate_nickname = "小鱼"

        section("S4 红线：危机句仍然最高优先")
        stub.messages.clear()
        spoken_crisis: list[str] = []
        app.bus.pet_speak.connect(spoken_crisis.append)
        app.psych_service.respond("我不想活了，该不该跟家里说")   # 带犹豫词，也还是危机优先
        drain_threads()
        pump(0.2)
        check("危机句连模型都没调（反问那条更没机会）", stub.messages == [], f"{len(stub.messages)} 次")
        check("走的是危机话术", bool(spoken_crisis) and "12356" in spoken_crisis[-1],
              spoken_crisis[-1][:40] if spoken_crisis else "（没说话）")

        section("S4 红线：pet.db 零写入、无新依赖")
        say(app, stub, "今天过得还行")
        drain_threads()
        pump(0.3)
        before = _memory_rows(pet_path)
        before_stat = pet_path.stat()
        mind.add(5, "测试用")
        app.mind_memory_book.record()
        pro.check()
        drain_threads()
        check("pet.db 的 memory 表一行没多", _memory_rows(pet_path) == before,
              f"{before} → {_memory_rows(pet_path)}")
        check("pet.db 文件没被动过",
              pet_path.stat().st_size == before_stat.st_size
              and pet_path.stat().st_mtime == before_stat.st_mtime,
              f"size {before_stat.st_size}→{pet_path.stat().st_size}")
        root = Path(__file__).resolve().parents[1]
        req = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        check("requirements 没动过（无新依赖）",
              not any(pkg in req for pkg in ("llama", "transformers", "jieba", "nltk")), "")
    finally:
        box.cleanup()
        drain_threads()

    return summary("B+ 语义阶段")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
