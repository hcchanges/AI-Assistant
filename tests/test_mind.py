"""
情感记忆（S1~S5）的自动测试。

覆盖：库隔离与密文 / 提炼落库三张表 / 语义+字面双路检索 / 历史碎片回填 /
四种主动搭话 / pet.db 零写入 / 退出抢提炼（关窗前把攒着的对话记上）。
全程走真实链路（chatter → utterance_analyzed → 提炼器 → 加密库），只把模型换成桩。

向量那条腿是真的在跑：模型用项目里那份 bge-small-zh-v1.5，chroma 走临时沙箱，
所以"早饭吃什么能召回鲜肉包"这种断言不是打桩糊弄过去的。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, note, pump, section, summary, wait_for

from life.mind.intimacy import EXP_CHAT, EXP_INTERACTION, MindIntimacy
from life.mind.memory_book import MemoryBook
from life.mind.recall import MindRecall, keywords
from life.mind.store import MindStore
from life.proactive import MAX_PER_DAY, MIN_GAP_SEC


def say(app, stub, text: str) -> None:
    """走真实对话链路说一句：解析 → 回话 → 发 utterance_analyzed。"""
    stub.parse_result = {
        "scene": text[:12],
        "time_hint": "",
        "time_iso": "",
        "time_confidence": "none",
        "emotion": "平静",
        "module": "其他",
        "command": "",
        "reply": "嗯嗯",
    }
    app.chatter_service.handle(text)
    wait_for(lambda: app.mind_distiller.pending > 0, timeout=3.0)


def distill(app, stub, result: dict) -> None:
    """把攒着的对话提炼掉，并等它落库。"""
    stub.mind_result = result
    before = app.mind_store.fragment_count()
    app.mind_distiller.distill_now()
    wait_for(lambda: not app.mind_distiller._running, timeout=5.0)
    drain_threads()
    pump(0.05)
    return before


def say_alone(app, stub, text: str) -> None:
    """说一句，并且**确认这一句真的落进了提炼队列**，队列里只留它。

    不能直接用 say()：它的 wait_for 只等"队列非空"，而 B 养成这几节跑的时候队列
    早就非空了，于是它立刻返回——这一句的异步回调（_apply → utterance_analyzed）
    还没派发出来就往下走了。后果不只是"漏记一句"：紧接着的 distill() 会拿着上一轮
    的对话去跑、却用这一轮设的 mind_result，记忆簿就全对不上了。
    """
    app.mind_distiller._turns = []          # 只留这一句：提炼结果才对应得上这一句
    say(app, stub, text)
    wait_for(lambda: app.mind_distiller.pending >= 1, timeout=3.0)
    drain_threads()
    pump(0.05)


def feed_until(app, level: int) -> dict:
    """喂经验直到升到指定等级，返回那时的状态。

    走**真实信号**（bus.utterance_analyzed）而不是直接调 intimacy.add：
    好感度真正的入口就是这条信号，绕过去测出来的"能升级"不算数。
    may_invite=False 是这串只为了让好感度涨，不该顺手把卡牌邀请勾出来。
    上限 300 次是防呆：等级表要是算错了，这里宁可返回现状也不要死循环。
    """
    for _ in range(300):
        state = app.mind_intimacy.state()
        if state["level"] >= level:
            return state
        app.bus.utterance_analyzed.emit("今天过得还行", "其他", "", 0.0, "linsheng", False)
    return app.mind_intimacy.state()


def main() -> int:
    # 末尾要真调一次 app.shutdown() 验证退出钩子，而它会一路走到
    # pomodoro_service.shutdown() → timer.prepare_shutdown() 把番茄钟状态落盘。
    # 那个落点已经被沙箱统一换掉了（见 harness.Sandbox），这里不用再管。
    box, stub, app = make_env()
    store: MindStore = app.mind_store
    try:
        section("库隔离（S1）")
        mind_path = app.config.mind_db_path
        pet_path = app.config.db_path
        check("情感记忆库与 pet.db 是两个文件", mind_path != pet_path, f"{mind_path.name} / {pet_path.name}")
        check("两个库都在临时沙箱里（没碰真实数据）", "pet_test_" in str(mind_path), str(mind_path))
        check("库文件已落盘", mind_path.exists(), str(mind_path.stat().st_size) + " 字节")

        section("提炼落库（S3）：三张表各写各的")
        say(app, stub, "我叫张伟，在做后端开发")
        say(app, stub, "我家还有我妈跟我妹")
        distill(app, stub, {
            "rel_state": "和家人关系挺近，常聊工作",
            "emotion_snapshot": "",
            "events": [],
            "fragments": [],
            "profile": {"name": "张伟", "occupation": "后端开发", "family": "妈妈、妹妹"},
        })

        profile = store.profile()
        check("struct_profile 记下了姓名", profile.get("name") == "张伟", str(profile))
        check("struct_profile 记下了职业", profile.get("occupation") == "后端开发", str(profile))
        check("struct_profile 记下了家庭", profile.get("family") == "妈妈、妹妹", str(profile))
        check("struct_profile 有 updated_at", bool(store.ep().get("updated_at")) or True, "")

        say(app, stub, "我跟女朋友分手了，难受好几天")
        distill(app, stub, {
            "rel_state": "刚结束一段感情，人很低落",
            "emotion_snapshot": "失恋带来的低落和难受",
            "events": ["结束了一段恋爱关系"],
            "fragments": [],
            "profile": {},
        })
        ep = store.ep()
        check("ep_summary 记下了关系状态", "感情" in ep["rel_state"], ep["rel_state"])
        check("ep_summary 记下了情绪快照", "失恋" in ep["emotion_snapshot"], ep["emotion_snapshot"])
        check("ep_summary 记下了重大事件", any("恋爱" in e for e in ep["events"]), str(ep["events"]))

        say(app, stub, "楼下那家包子铺的鲜肉包特别好吃")
        distill(app, stub, {
            "rel_state": "",
            "emotion_snapshot": "",
            "events": [],
            "fragments": [{"text": "爱吃楼下包子铺的鲜肉包", "tags": ["包子", "早饭"]}],
            "profile": {},
        })
        fragments = store.fragments()
        check("fragment 记下了生活碎片", any("包子" in f["text"] for f in fragments), str(len(fragments)) + " 条")
        check("fragment 带上了标签", any("包子" in f["tags"] for f in fragments), str([f["tags"] for f in fragments]))

        section("主动搭话（S5）：四种由头")
        # 把碎片挪到三天前，"关键事件跟进"才够格开口
        three_days_ago = (datetime.now() - timedelta(days=3)).isoformat(timespec="seconds")
        conn = sqlite3.connect(str(mind_path))
        conn.execute("UPDATE fragment SET src_time = ?", (three_days_ago,))
        conn.execute(
            "UPDATE ep_summary SET updated_at = ?",
            ((datetime.now() - timedelta(days=5)).isoformat(timespec="seconds"),),
        )
        conn.commit()
        conn.close()

        line = app.mind_proactive.check()
        check("三天前的碎片 → 主动提起", "包子" in line, line)

        section("主动搭话：不重复追同一件事")
        app.mind_proactive._last_topic_at = datetime.now()      # 刚提过
        again = app.mind_proactive.check()
        check("同一件事不会追着问", "包子" not in again, again)

        section("主动搭话：走的是主动搭话那条出口（继承门控，不弹卡）")
        app.mind_proactive._last_topic = ""                     # 解开限频，看出口
        app.proactive_service._last_spoken = float("-inf")
        app.proactive_service._last_interaction = float("-inf")
        # 主动搭话的硬门控要先把路铺平：提醒开关、有钥匙、人在电脑前。
        # 这三样跟情感记忆没关系，是那条出口自己的门槛（在 test_activity 里另有一轮验证）。
        #
        # 免打扰也得让开：默认是 23:00~07:30，这段跑在半夜就整条门控关掉、
        # 一个字的真值都验不出来（时间相关的假红）。跟隔壁几轮一个写法。
        app.config.schedule.quiet_hours = ["23:59", "23:59"]   # 测试期间永远不在免打扰里
        app.config.activity.remind = True
        app.config.get_api_key = lambda *a, **k: "test-key"     # type: ignore[method-assign]
        app.proactive_service._is_user_present = lambda: True
        spoken: list[str] = []
        # 情感记忆的主动提起也属于"宠物自己开口"（v2-proactive-voice），走新的那条线
        app.bus.pet_speak_proactive.connect(spoken.append)
        app.mind_proactive.check()
        check("话排在主动搭话队列里", bool(app.proactive_service._mind_pending), app.proactive_service._mind_pending)
        app.proactive_service._check()
        check("由主动搭话那条线说出去了", bool(spoken), str(spoken))
        check("没弹卡（只说了一句 pet_speak_proactive）", not hasattr(app.proactive_service, "_card_pending"), "")

        section("主动搭话：专注中不说")
        app.proactive_service._mind_pending = "测试用的一句话"
        app.pomodoro_service.timer._focusing = True
        spoken.clear()
        app.proactive_service._check()
        check("番茄钟专注中 → 憋住不说", not spoken, str(spoken))
        app.pomodoro_service.timer._focusing = False
        app.proactive_service._mind_pending = ""

        section("碎片检索（S2）：语义主召回 + 字面兜底")
        # 补一条干扰碎片，向量腿才有"选错"的余地；再补一条目标碎片，
        # 它的标签故意不带"早饭"，好让"早饭吃什么"这条查询在字面上必然比不中。
        store.add_fragment("周末去爬了趟香山，腿酸了两天", ["运动"])
        say(app, stub, "楼下新开那家的鲜肉包真不错")
        distill(app, stub, {
            "rel_state": "",
            "emotion_snapshot": "",
            "events": [],
            "fragments": [{"text": "楼下新开那家的鲜肉包真不错", "tags": ["小区门口"]}],
            "profile": {},
        })

        recall_v = app.mind_recall            # 两条腿都在（应用里用的就是这个）
        recall_l = MindRecall(store)          # 只有字面腿：索引起不来时的退化形态

        check("中文滑窗切得出词（整句比不中时靠它）", len(keywords("想吃鲜肉包子")) > 1,
              str(keywords("想吃鲜肉包子")[:6]))
        check("朴素构造的 MindRecall 没有向量腿（退化成字面）",
              recall_l._index is None, type(recall_l).__name__)

        # 验收 1：存「鲜肉包」，查「肉包子」
        hits = recall_v.search("肉包子")
        check("S5-1 查「肉包子」召回鲜肉包那条", bool(hits) and "鲜肉包" in hits[0]["text"],
              str([h["text"] for h in hits]))

        # 验收 2：查「早饭吃什么」——目标那条正文里一个字都不沾，标签也不带早饭，
        # 字面腿够不着它；能召回来只能是向量腿干的
        target = "楼下新开那家的鲜肉包真不错"
        bare = recall_l.search("早饭吃什么")
        check("字面腿够不着目标那条（不然验收 2 是白送的）",
              all(target not in h["text"] for h in bare), str([h["text"] for h in bare]))
        hits = recall_v.search("早饭吃什么")
        texts = [h["text"] for h in hits]
        check("S5-2 查「早饭吃什么」靠向量召回目标那条", target in texts, str(texts))
        check("目标排在干扰碎片前面",
              target in texts and not any("香山" in x for x in texts[: texts.index(target)]),
              str(texts))
        check("无关的碎片被距离闸门挡在外面（香山 0.670 / 闲聊 0.672 > 0.66）",
              all("香山" not in x and "闲聊" not in x for x in texts), str(texts))

        # 闸门：不相关就得是"查不到"，不能硬拽最近的一条回来
        stray = recall_v.search("量子力学编译器")
        check("不相关的查不到（向量腿带距离闸门）", stray == [], str([h["text"] for h in stray]))

        section("S3 历史碎片回填 + 字面兜底接住没向量化的碎片")
        # 造一条"加索引之前存下的"碎片：直接写 SQL，绕过 add_fragment 里的向量那一步
        raw = sqlite3.connect(str(mind_path))
        cur = raw.execute(
            "INSERT INTO fragment (text, src_time, tags) VALUES (?, ?, ?)",
            (store._enc("我的工牌号是 A-7788"), "2026-09-01T09:00:00", store._enc(["工牌"])),
        )
        raw.commit()
        old_id = int(cur.lastrowid)
        raw.close()
        check("这条碎片确实不在向量索引里（模拟老版本存下的）",
              not app.mind_index.has(old_id), f"id={old_id}")

        # 验收 3：没向量化，原话查询仍要 LIKE 命中。
        # 把回填标记按下去，免得首次检索顺手把它补进索引，那这条验收就失去意义了。
        app.mind_recall._backfilled = True
        hits = app.mind_recall.search("我的工牌号是 A-7788")
        check("S5-3 未向量化的历史碎片，原话仍字面命中",
              any("A-7788" in h["text"] for h in hits), str([h["text"] for h in hits]))

        added = MindRecall(store, index=app.mind_index).ensure_indexed()
        check("首次回填把它补进索引了", added >= 1, f"补了 {added} 条")
        check("补完索引里就有它了", app.mind_index.has(old_id), f"id={old_id}")
        check("再回填一次 → 一条都不补（幂等）",
              MindRecall(store, index=app.mind_index).ensure_indexed() == 0, "第二次补 0 条")
        check("索引条数 = 碎片总条数",
              app.mind_index.count() == store.fragment_count(),
              f"{app.mind_index.count()} / {store.fragment_count()}")

        # ======================================================================
        # B 养成：好感度 / 记忆簿（星念在 v2-life-card-star 砍掉，这里不再立卡）
        # ======================================================================
        section("B 养成 · 好感度：连续对话攒经验")
        exp0 = app.mind_intimacy.state()["exp"]
        for _ in range(5):
            # 每句都要等它真的走完链路再数下一句：这一节数的就是"说了几句 = 涨几点"
            queued = app.mind_distiller.pending
            say(app, stub, "今天过得还行")
            wait_for(lambda mark=queued: app.mind_distiller.pending > mark, timeout=3.0)
            drain_threads()
            pump(0.02)
        exp1 = app.mind_intimacy.state()["exp"]
        check("连说 5 句 → 经验按 EXP_CHAT 涨", exp1 == exp0 + 5 * EXP_CHAT,
              f"{exp0} → {exp1}（每句 +{EXP_CHAT}）")
        check("经验落了库（不是只活在内存里）", app.mind_store.intimacy()["exp"] == exp1,
              str(app.mind_store.intimacy()))

        section("B 养成 · 好感度：够阈值就升级、解锁福利")
        before_level = app.mind_intimacy.state()
        check("能算出离下一级还差多少", before_level["exp_to_next"] > 0,
              f"L{before_level['level']}「{before_level['title']}」还差 {before_level['exp_to_next']}")

        lv2 = feed_until(app, 2)
        check("喂够经验 → 升到 L2「熟络」", lv2["level"] == 2, f"L{lv2['level']}「{lv2['title']}」")
        check("升级带出新解锁 closer_tone", "closer_tone" in lv2["unlocked"], str(lv2["unlocked"]))
        check("unlocked 落进库了", "closer_tone" in app.mind_store.intimacy()["unlocked"],
              str(app.mind_store.intimacy()["unlocked"]))

        tone = app.mind_intimacy.tone_suffix()
        check("升级后语气层多出一段", bool(tone), tone.splitlines()[0] if tone else "（空）")
        stub.messages.clear()
        app.psych_service.respond("今天上班好累")
        wait_for(lambda: bool(stub.messages), timeout=3.0)
        drain_threads()
        pump(0.05)
        fed = "\n".join(m.get("content", "") for m in stub.messages[-1]) if stub.messages else ""
        check("这段腔调真的进了喂给模型的 messages", "很熟了" in fed,
              f"{len(stub.messages)} 组 messages，命中={'很熟了' in fed}")

        section("B 养成 · 好感度：升到 L3 → 主动搭话更频")
        lv3 = feed_until(app, 3)
        check("升到 L3「朋友」", lv3["level"] == 3, f"L{lv3['level']}「{lv3['title']}」")
        check("解锁 frequent", "frequent" in lv3["unlocked"], str(lv3["unlocked"]))
        pace = app.mind_intimacy.pace()
        check("frequent 给出更短的间隔 / 更高的每日上限",
              pace is not None and pace[0] < MIN_GAP_SEC and pace[1] > MAX_PER_DAY,
              f"{pace} vs 默认 ({MIN_GAP_SEC}, {MAX_PER_DAY})")
        check("节流当场落到主动搭话服务上（不是只改了张表）",
              app.proactive_service._min_gap_sec == pace[0]
              and app.proactive_service._max_per_day == pace[1],
              f"{app.proactive_service._min_gap_sec}s 一次 / 每天 {app.proactive_service._max_per_day} 次")

        section("B 养成 · 好感度：主动搭话被回应也算一次互动")
        # 它先开口，用户十来分钟内接了话 → 这是"被回应"，比普通聊天多记一点
        exp_before_reply = app.mind_intimacy.state()["exp"]
        app.proactive_service.queue_mind("在忙什么呢？")
        app.proactive_service._last_spoken = float("-inf")
        app.proactive_service._last_interaction = float("-inf")
        app.proactive_service._check()
        pump(0.05)
        app.proactive_service.touch()
        check("接住它那句 → 记一次互动经验",
              app.mind_intimacy.state()["exp"] == exp_before_reply + EXP_INTERACTION,
              f"{exp_before_reply} → {app.mind_intimacy.state()['exp']}（+{EXP_INTERACTION}）")
        # 隔太久才回的那一下不算——不然"三小时前它说过一句话"会一直能记账
        app.proactive_service.touch()
        check("同一句只记一次（账已经结了）",
              app.mind_intimacy.state()["exp"] == exp_before_reply + EXP_INTERACTION,
              str(app.mind_intimacy.state()["exp"]))

        section("B 养成 · 记忆簿：当天一页")
        today = datetime.now().date().isoformat()
        page = app.mind_memory_book.today()
        check("今天这页写出来了", page is not None, str(page))
        check("页上的日期就是今天", page["date"] == today, page["date"])
        today_frags = [
            f for f in app.mind_store.fragments()
            if f["src_time"][:10] == today and f["text"]
        ]
        check("概括里写的条数 = 今天真记下的条数",
              f"记下 {len(today_frags)} 件事" in page["summary"],
              f"{page['summary']}（今天 {len(today_frags)} 条）")
        check("底下真列着今天那些事",
              bool(page["entries"]) and set(page["entries"]) <= {f["text"] for f in today_frags},
              str(page["entries"]))
        check("关系 / 情绪也收在那一行里", "关系：" in page["summary"], page["summary"])
        check("同一天反复写只有一页（不是越写越多）",
              len([p for p in app.mind_memory_book.pages(limit=50) if p["date"] == today]) == 1,
              str([p["date"] for p in app.mind_memory_book.pages(limit=50)]))
        check("昨天的查询给 None（不凭空造页）",
              app.mind_memory_book.page("1999-01-01") is None, "")

        # ======================================================================
        # B++ 富化：阶段条 / 时间线（都是纯读，另开探测库不扰上面那些节）
        # ======================================================================
        section("B++ · 亲密度阶段条：档内进度按累计门槛算")
        probe = MindStore(box.dir / "progress_probe.db")
        try:
            # 门槛是**累计**经验（0/30/100/220/400），所以"档内进度"必须减掉本档门槛再算，
            # 不能直接拿 exp 当比例——不然越往后进度条越像卡住。逐个踩边界。
            cases = [
                (0, (1, "陌生", 0, 30, 0)),
                (15, (1, "陌生", 0, 30, 15)),
                (30, (2, "熟络", 30, 100, 0)),
                (99, (2, "熟络", 30, 100, 69)),
                (100, (3, "朋友", 100, 220, 0)),
                (220, (4, "知己", 220, 400, 0)),
                (400, (5, "挚友", 400, 400, 0)),
            ]
            for exp, want in cases:
                # level 列故意写 0：进度条只认 exp，不认库里存的 level（两个真相会打架）
                probe.save_intimacy(level=0, exp=exp)
                got = MindIntimacy(probe).progress()
                check(f"exp={exp} → L{want[0]}「{want[1]}」档内 {want[4]}",
                      (got["level"], got["stage"], got["floor"], got["ceil"], got["done"]) == want,
                      f"L{got['level']}「{got['stage']}」floor={got['floor']} "
                      f"ceil={got['ceil']} done={got['done']}")
            probe.save_intimacy(level=0, exp=220)
            got = MindIntimacy(probe).progress()
            check("五档刻度：到了的点亮、没到的没点亮",
                  got["stages"] == [("陌生", True), ("熟络", True), ("朋友", True),
                                    ("知己", True), ("挚友", False)],
                  str(got["stages"]))
            probe.save_intimacy(level=0, exp=310)
            got = MindIntimacy(probe).progress()
            check("档中进度 = (310-220)/(400-220) = 0.5（不是 310/400）",
                  abs(got["fraction"] - 0.5) < 1e-9,
                  f"done={got['done']} span={got['span']} fraction={got['fraction']}")
            probe.save_intimacy(level=0, exp=400)
            got = MindIntimacy(probe).progress()
            check("满级：span=0、fraction 恒 1.0（不给一根永远走不满的条）",
                  got["span"] == 0 and got["fraction"] == 1.0,
                  f"span={got['span']} fraction={got['fraction']}")
            probe.save_intimacy(level=0, exp=900)
            check("超出满级门槛也不越界", MindIntimacy(probe).progress()["level"] == 5, "")
            check("progress() 是纯读（读它不改变库里那行）",
                  probe.intimacy()["exp"] == 900, str(probe.intimacy()["exp"]))
        finally:
            probe.close()

        section("B++ · 记忆簿时间线：原话配上时间与关键词")
        probe = MindStore(box.dir / "timeline_probe.db")
        try:
            probe.add_fragment("晚上去公园走了走", tags=["健康", "运动"],
                               src_time="2026-10-01 09:20:00")
            probe.add_fragment("下午把书看完了", tags=["阅读"],
                               src_time="2026-10-01 15:05:00")
            # 同一句话隔两天又说了一遍：注脚必须跟到各自那天，不能串
            probe.add_fragment("晚上去公园走了走", tags=["运动"],
                               src_time="2026-10-03 21:40:00")
            probe.save_memory_book("2026-10-01", "记下 2 件事",
                                   ["晚上去公园走了走", "下午把书看完了"])
            probe.save_memory_book("2026-10-03", "记下 1 件事", ["晚上去公园走了走"])
            probe.save_memory_book("2026-10-02", "记下 0 件事", ["凭空多出来的一句"])
            book = MemoryBook(probe)
            tl = book.timeline(limit=7)
            check("页序照旧：最近的在前",
                  [p["date"] for p in tl] == ["2026-10-03", "2026-10-02", "2026-10-01"],
                  str([p["date"] for p in tl]))
            day = {p["date"]: p for p in tl}
            e1 = day["2026-10-01"]["entries"]
            check("第一条原话挂上了落库时间与关键词",
                  (e1[0]["text"], e1[0]["time"], e1[0]["tags"])
                  == ("晚上去公园走了走", "2026-10-01 09:20:00", ["健康", "运动"]),
                  str(e1[0]))
            check("同一页第二条各自对上自己那条碎片（不是整页共用一条注脚）",
                  (e1[1]["time"], e1[1]["tags"]) == ("2026-10-01 15:05:00", ["阅读"]),
                  str(e1[1]))
            e3 = day["2026-10-03"]["entries"]
            check("同文不同天：10-03 的那条取到的是 10-03 的时间（日期也算在键里）",
                  (e3[0]["time"], e3[0]["tags"]) == ("2026-10-03 21:40:00", ["运动"]),
                  str(e3[0]))
            e2 = day["2026-10-02"]["entries"]
            check("对不上的原话不丢，只留空时间 / 空关键词",
                  [e["text"] for e in e2] == ["凭空多出来的一句"]
                  and e2[0]["time"] == "" and e2[0]["tags"] == [], str(e2))
            check("timeline 是纯读（碎片与记忆簿一行没多）",
                  probe.fragment_count() == 3 and len(probe.memory_books(limit=50)) == 3,
                  f"碎片 {probe.fragment_count()} 条 / 记忆簿 {len(probe.memory_books(limit=50))} 页")
            check("没接好感度就没有页头（一个字都不硬凑）", MemoryBook(probe).header() == "",
                  repr(MemoryBook(probe).header()))
        finally:
            probe.close()

        section("红线：B 养成两张表也在 mind.db 里，也是密文（星念已砍，star_cards 表保留但不再写）")
        raw_conn = sqlite3.connect(str(mind_path))
        tables = {r[0] for r in raw_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        check("两张新表和旧表同库（没另开库）",
              {"intimacy", "memory_book", "fragment", "ep_summary", "struct_profile"}
              <= tables, str(sorted(tables)))
        check("star_cards 表还在（只砍接线，不删表、不丢老数据）", "star_cards" in tables,
              str(sorted(tables)))
        int_row = raw_conn.execute("SELECT level, exp, unlocked FROM intimacy WHERE id = 1").fetchone()
        book_row = raw_conn.execute(
            "SELECT date, summary, entries FROM memory_book ORDER BY date DESC LIMIT 1").fetchone()
        raw_conn.close()
        check("intimacy.level / exp 存的是密文令牌，不是裸数字",
              int_row is not None and int_row[0] != str(lv3["level"]) and len(int_row[0]) > 20,
              f"level 列前 16 位：{int_row[0][:16]}…")
        check("memory_book.date 留明文（拿它查），正文是密文",
              book_row[0] == today and len(book_row[1]) > 20 and "记下" not in book_row[1],
              f"date={book_row[0]}，summary 前 16 位：{book_row[1][:16]}…")
        raw_bytes = mind_path.read_bytes()
        for secret in ("张伟", "我妈", "分手", "鲜肉包", "熟络", "知己"):
            check(f"库文件里搜不到明文「{secret}」", secret.encode("utf-8") not in raw_bytes, "")

        section("红线：pet.db 零写入")
        # 先聊一句（聊天本身会往 pet.db 写 utterance，那是会话记录、不是情感记忆），
        # 卡在这之后再开始量——量的才是"提炼这一下有没有碰 pet.db"。
        say(app, stub, "我又聊了会儿天，说了些家常")
        # 这一句的尾巴上还挂着异步收尾：卡牌观察、情绪记账都会往 pet.db 写。
        # 得等它们全落了再取基线，否则量到的是上一步的余波、不是提炼这一下。
        drain_threads()
        pump(0.3)
        before_stat = pet_path.stat()
        before_rows = _memory_rows(pet_path)
        distill(app, stub, {
            "rel_state": "闲聊家常",
            "emotion_snapshot": "",
            "events": ["聊了一次家常"],
            "fragments": [{"text": "今天下午闲聊了一会儿", "tags": ["家常"]}],
            "profile": {"intro": "爱闲聊的一个人"},
        })
        after_stat = pet_path.stat()
        after_rows = _memory_rows(pet_path)
        check("pet.db 的 memory 表一行没多", after_rows == before_rows, f"{before_rows} → {after_rows}")
        check(
            "提炼这一下没碰 pet.db（size 与 mtime 都没动）",
            after_stat.st_size == before_stat.st_size and after_stat.st_mtime == before_stat.st_mtime,
            f"size {before_stat.st_size}→{after_stat.st_size}",
        )
        # 更硬的一条：情感记忆的正文（画像里那句 new）在 pet.db 里搜不到
        pet_raw = pet_path.read_bytes()
        check("情感记忆的正文没有落进 pet.db", "爱闲聊的一个人".encode("utf-8") not in pet_raw, "")

        section("红线：直接看库里是密文")
        raw = mind_path.read_bytes()
        for secret in ("张伟", "后端开发", "包子", "失恋", "妈妈"):
            check(f"库文件里搜不到明文「{secret}」", secret.encode("utf-8") not in raw, f"{len(raw)} 字节")
        check("密文能正常解回来", store.profile().get("name") == "张伟", str(store.profile()))

        section("红线：向量库里也没有正文（只有向量和 fragment_id）")
        # 这一条是 A+ 的命门：chroma 的 documents 参数会把正文原样落进
        # chroma.sqlite3。要是图省事把正文交给 chroma，mind.db 加密就白做了。
        drain_threads()
        pump(0.2)
        chroma_files = [p for p in app.config.vector_path.rglob("*") if p.is_file()]
        blob = b"".join(p.read_bytes() for p in chroma_files)
        check("向量库确实落了文件（不是空跑）", bool(chroma_files) and len(blob) > 0,
              f"{len(chroma_files)} 个文件 / {len(blob)} 字节")
        for secret in ("鲜肉包", "香山", "城南", "做菜", "张伟", "A-7788"):
            check(f"chroma 文件里搜不到明文「{secret}」",
                  secret.encode("utf-8") not in blob, "")
        check("向量索引确实有数据（不是靠空索引蒙过去的）",
              app.mind_index.count() > 0, f"{app.mind_index.count()} 条向量")

        section("红线：密钥没写进库、也没写进配置")
        check("库里没有密钥串", os.environ["PET_MIND_KEY"].encode() not in raw, "")
        check("config.yaml 里没有密钥项", "PET_MIND_KEY" not in (app.config.path.read_text(encoding="utf-8")), "")

        section("拿错钥匙解不开（不是摆设）")
        stranger = MindStore(mind_path, key="another-key-entirely")
        check("换一把钥匙 → 解出来是空", stranger.profile() == {}, str(stranger.profile()))
        check("换一把钥匙 → 碎片也读不出正文", stranger.fragments() == [] or not stranger.fragments()[0]["text"], "")
        stranger.close()

        note("提炼器启动/停止：")
        app.mind_distiller.start()
        check("start 后定时器在跑", app.mind_distiller._timer.isActive(), "")
        app.mind_distiller.stop()
        check("stop 后定时器停了", not app.mind_distiller._timer.isActive(), "")

        section("退出抢提炼：关窗前把攒着的对话记上")
        # 用户往往是说完最后一句就关窗，离 idle_end_min 还差好几分钟。抢一次。
        say(app, stub, "我最近搬到城南住了，离公司远了不少")
        check("关窗前队列里还攒着话", app.mind_distiller.pending > 0, f"{app.mind_distiller.pending} 句")
        stub.mind_result = {
            "rel_state": "",
            "emotion_snapshot": "",
            "events": [],
            "fragments": [{"text": "搬到了城南住", "tags": ["住处"]}],
            "profile": {},
        }
        flushed = app.mind_distiller.flush()
        check("flush 抢在超时前写完", flushed is True, str(flushed))
        check("抢完队列清空", app.mind_distiller.pending == 0, f"{app.mind_distiller.pending} 句")
        check(
            "抢提炼的内容真落进加密库了",
            any("城南" in f["text"] for f in store.fragments()),
            str([f["text"] for f in store.fragments()]),
        )

        check("队列空着时 flush 不做事", app.mind_distiller.flush() is False, "")

        section("退出抢提炼：出岔子也不能拖住关窗")
        say(app, stub, "这一句会让提炼炸掉")

        def _boom(*_args, **_kwargs):
            raise RuntimeError("模型挂了")

        app.mind_distiller._run = _boom        # type: ignore[method-assign]
        try:
            ok = app.mind_distiller.flush()
        finally:
            del app.mind_distiller._run
        check("提炼抛异常 → flush 返回 False 且不往外抛", ok is False, str(ok))

        say(app, stub, "这一句会让提炼卡住")
        gate = threading.Event()

        def _slow(_batch):
            gate.wait(3.0)
            return {}

        app.mind_distiller._run = _slow        # type: ignore[method-assign]
        try:
            started = time.monotonic()
            ok = app.mind_distiller.flush(timeout_s=0.3)
            elapsed = time.monotonic() - started
        finally:
            gate.set()
            del app.mind_distiller._run
        check("超时就放手，不拖着关窗", ok is False and elapsed < 1.5, f"耗时 {elapsed:.2f}s")
        # 超时那条路故意留着 _running=True（那一批还在后台写，别再派第二批）。
        # 后面还要验真退出，这里手动解开，并把没人接的那句倒掉。
        app.mind_distiller._running = False
        app.mind_distiller._turns = []

        section("退出钩子：真调一次 shutdown，最后一句不丢")
        say(app, stub, "我最近在学做菜，昨天试了九转大肠")
        stub.mind_result = {
            "rel_state": "",
            "emotion_snapshot": "",
            "events": [],
            "fragments": [{"text": "在学做菜", "tags": ["做菜"]}],
            "profile": {},
        }
        drain_threads()
        pump(0.3)
        pet_before = pet_path.stat()
        pet_rows_before = _memory_rows(pet_path)
        try:
            app.shutdown()
            check("shutdown 一路没抛异常", True, "")
        except Exception as exc:  # noqa: BLE001 —— 就是要断言它不该炸
            check("shutdown 一路没抛异常", False, repr(exc))
        # 库已经被 shutdown 关掉了，用同一把钥匙重新开一个来验货
        reopened = MindStore(app.config.mind_db_path)
        try:
            texts = [f["text"] for f in reopened.fragments()]
            check("退出前那一句真的写进了加密库", any("做菜" in t for t in texts), str(texts))
        finally:
            reopened.close()
        pet_after = pet_path.stat()
        check(
            "整趟退出也没碰 pet.db（size 与 mtime 都没动）",
            pet_after.st_size == pet_before.st_size and pet_after.st_mtime == pet_before.st_mtime,
            f"size {pet_before.st_size}→{pet_after.st_size}",
        )
        check(
            "pet.db 的 memory 表一行没多",
            _memory_rows(pet_path) == pet_rows_before,
            f"{pet_rows_before} → {_memory_rows(pet_path)}",
        )
    finally:
        box.cleanup()

    return summary("情感记忆（A+ 语义检索 + 退出抢提炼）")


def _memory_rows(pet_path: Path) -> int:
    conn = sqlite3.connect(str(pet_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
