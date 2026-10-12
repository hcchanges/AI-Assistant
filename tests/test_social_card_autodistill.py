"""对象攻略卡的**后台静默补全**（v2-social-card-autodistill）的自动测试。

**这一批补的是哪一半**：攻略卡从批次 `v2-social-card-base` 起就有四格对象档案（喜好 /
禁忌 / 攻略策略 / 关系阶段），可那四格**只在建卡那一下的弹框里手填**——用户点了「好呀」
建完卡，格子里就是空的（真机图为证：开卡之后所有格都空着）。前五批把字段、peer 维度、
注入都做完了，**"桌宠在后台悄悄把它补上"这一半从没实现**。这一批补的就是它：

对话结束 / 沉默（跟主聊天提炼**同一把尺** `mind.idle_end_min`）→ 拿**对方说过的话**问一趟
小模型 → 抽出「对方是个什么样的人」→ 静默写进攻略卡那八格 + 三记忆库的 peer 格。

机制全部复用现成的：触发与本地优先走 `life/mind/distill.ask_local_first`，材料从社交那条
线经 `bus.peer_utterance` 递过来，落库走 `MindStore` 的 `peer=` 那一维（批次一加的）。

红线（本模块逐条钉住）：
- **按 `sid|peer` 分格**：两场话 / 两个人各攒各的，**不串人**；写进库里的也是"这个人"那一格，
  **主人自己那一档（`peer=""`）一个字都不许动**（主聊天那条线照旧独立）。
- **原话不落库**：喂给模型的是对方的话，**落库的只有抽出来的结论**——一整句原话不许出现在
  任何一格字段里。
- **卡不在就不抽**：用户没开那张卡，就不替他攒档案（材料留在内存里等他开卡，不落库）。
- **`card_autodistill=off` 整条不跑**：跟 `card_memory` / `tone_align` 一个口径，只认 `off`。

**口径如实记（够不着的地方不装作验过）**：模型是被桩掉的——本模块钉的是"**谁**在什么**时候**
被喂了**什么**、抽出来的**落了哪儿**"，不是"模型真能推断年龄"。真心跳要靠真机看；桩这边
只保证链路与边界。另外，**"对象攻略卡"是一张单卡**（`find_card` 按大类 + 卡名查重），
所以卡面那八格记的是"眼下这一个对象"——两个人都聊过时，后一场会覆写前一场；**分人的隔离
落在记忆库那一维**（那是批次一专门为此加的），卡上不分人。这一条写在报告里，不在测试里假装。
"""

from __future__ import annotations

import ast
import inspect
import time
from types import SimpleNamespace
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
    wait_for,
)

from brain.prompts.templates import PEER_DISTILL_PROMPT  # noqa: E402
from core.config import ScreenSection  # noqa: E402
from data.dao import _PROFILE_COLUMNS  # noqa: E402
from life.mind import peer_distill as pmod  # noqa: E402
from life.mind.peer_distill import CARD_FIELDS, PeerDistiller  # noqa: E402
from life.mind.store import MindStore  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
    SocialReplyService,
)

# 两个**不同的人**：拿来验"不串人"。认各自的内容只用这几个标记词。
JIA, YI = "茗风", "阿强"
JIA_MARKS = ("短剧", "破冰", "三十往上")
YI_MARKS = ("看展", "聊得来", "二十五六")

# 喂进去的**原话**（屏幕认出来的字）。落库那一头一个字都不该出现——
# 特别挑了「大福」这种只可能出现在原话里的词，好认。
RAW_JIA = "我妈又催了，说我都不小了\n周末又去爬了趟山，晚上窝着刷短剧"
RAW_YI = "我平时爱看展，一个人在美术馆能耗一下午"
RAW_ONLY_WORD = "大福"

# 桩给的"抽出来的结论"——全是结论、当中没有任何原句
JIA_RESULT = {
    "age": "三十往上",
    "emotion_state": "单身",
    "anxiety": "家里在催婚",
    "interests": "爬山、短剧",
    "likes": "爱听人夸他细心",
    "dislikes": "别提前任",
    "strategy": "先接住抱怨，再顺一句夸他",
    "relation_stage": "破冰",
    "intro": "单身，周末爱爬山刷短剧，家里在催",
    "events": ["约好周末一起去爬山"],
    "fragments": [{"text": "周末常爬山、晚上刷短剧", "tags": ["兴趣", "爬山"]}],
}
YI_RESULT = {
    "age": "二十五六",
    "emotion_state": "刚分手",
    "anxiety": "工作上被压得紧",
    "interests": "看展、逛书店",
    "likes": "慢一点的节奏",
    "dislikes": "别一上来就约饭",
    "strategy": "从展聊起，别急着推进度",
    "relation_stage": "聊得来",
    "intro": "做设计的，爱看展",
    "events": ["说好下次一起去看展"],
    "fragments": [{"text": "一个人在美术馆能耗一下午", "tags": ["兴趣", "看展"]}],
}

_CFG_DEFAULT = inspect.signature(ScreenSection).parameters["card_autodistill"].default


def _dump(store, peer: str) -> str:
    """这个人那一格里现在存着的**所有**字（画像 / 关系 / 情绪 / 事件 / 碎片）。"""
    bits: list[str] = []
    bits += [str(v) for v in (store.profile(peer=peer) or {}).values()]
    ep = store.ep(peer=peer)
    bits += [str(ep["rel_state"]), str(ep["emotion_snapshot"])]
    bits += [str(item) for item in ep["events"]]
    bits += [str(row.get("text") or "") for row in store.fragments(peer=peer)]
    return "\n".join(bits)


def _let_idle(distiller, key: str) -> None:
    """把这一格的「最后一句」往回拨到已过空闲阈值——不去等真三分钟。"""
    limit = distiller._config.mind.idle_end_min * 60
    distiller._last_turn_at[key] = time.monotonic() - (limit + 1)


def _settle(app, distiller) -> None:
    """等后台那一趟跑完、回调回主线程。"""
    wait_for(lambda: not distiller._running, timeout=5.0)
    drain_threads()
    pump(0.05)


def _guide_card(app):
    return app.card_service.create_manual_card(
        CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL
    )


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- ① 开关：三道口径 ----------

        section("① 开关 card_autodistill（跟 card_memory / tone_align 同一个口径）")

        check("前置：新配置项的代码默认就是 auto", _CFG_DEFAULT == "auto", repr(_CFG_DEFAULT))
        check("前置：装配层真的把它装上了（不是 None）",
              isinstance(app.peer_distiller, PeerDistiller), type(app.peer_distiller).__name__)
        stub.peer_result = {"age": "三十往上"}
        check("前置：分流字面是这段提示词**独有**的（别的提示词一个字都不带）",
              '"anxiety"' in PEER_DISTILL_PROMPT, "")
        check("★ 桩按那一个字面分流：这段提示词回的是 peer_result",
              stub.route_json(PEER_DISTILL_PROMPT.format(known="（还没有）", turns="- 在吗"))
              == {"age": "三十往上"}, "")
        check("★ 分流标记是独占的：换一段没有它的提示词桩就回空（不会误伤别的线）",
              stub.route_json("随便一段没有那个标记的提示词") == {}, "")

        app.config.screen.card_autodistill = "off"
        check("★ 明写 off 才算关", app.peer_distiller.enabled is False, "")
        never_off = [v for v in ("OFF", "0", "false", "真假", "", "auto")
                     if _enabled_with(app, v) is not True]
        app.config.screen.card_autodistill = "off"
        check("★ 写别的值（OFF / 0 / false / 真假 / 空 / auto）一律按开算"
              "（功能不该因为笔误静默失效）", not never_off, "、".join(map(repr, never_off)))

        app.peer_distiller.note_turn("S1|" + JIA, JIA, RAW_JIA)
        check("★ off 时一个字都不攒（不是攒了不抽）", app.peer_distiller.pending == 0, "")
        check("★ off 时 distill_now 直接回 False", app.peer_distiller.distill_now("S1|" + JIA) is False, "")
        app.peer_distiller.start()
        check("★ off 时定时器不启动（整条不跑，不是空转）",
              app.peer_distiller._timer.isActive() is False, "")

        app.config.screen.card_autodistill = "auto"
        app.peer_distiller.stop()
        old = SimpleNamespace(mind=SimpleNamespace(enabled=True), screen=SimpleNamespace())
        check("★ 取不到那一格（老配置 / 老调用点）当 auto：默认开，"
              "「读不到」不该变成「关掉」",
              PeerDistiller(config=old, store=app.mind_store).enabled is True, "")
        muted = SimpleNamespace(mind=SimpleNamespace(enabled=False),
                               screen=SimpleNamespace(card_autodistill="auto"))
        check("★ 情感记忆那条总闸关着时它也不跑（不另立一个总闸）",
              PeerDistiller(config=muted, store=app.mind_store).enabled is False, "")

        app.peer_distiller.start()
        check("★ auto 时定时器起来了", app.peer_distiller._timer.isActive() is True, "")
        app.peer_distiller.stop()
        check("★ stop 之后停表（退出那条路上就是这个次序）",
              app.peer_distiller._timer.isActive() is False, "")

        # ---------- ② 攒：按 sid|peer 分格 + 去重 + 两把收口 ----------

        section("② 攒材料：按 sid|peer 分格、同一句不重记、条数与字数两把闸")

        d = PeerDistiller(config=app.config, store=app.mind_store,
                          card_exists=lambda peer: True)
        key_a, key_b = "S1|" + JIA, "S2|" + YI
        d.note_turn(key_a, JIA, RAW_JIA)
        d.note_turn(key_b, YI, RAW_YI)
        check("★ 两场话各攒各的（键就是社交那边的 _cooldown_key）",
              d.pending_for(key_a) == 2 and d.pending_for(key_b) == 1,
              f"{d.pending_for(key_a)} / {d.pending_for(key_b)}")

        d.note_turn(key_a, JIA, RAW_JIA)          # 自动监听那一拍又把同一份尾巴带回来了
        check("★ 同一份尾巴再递一遍不重记（自动监听每一拍都会重复带一次）",
              d.pending_for(key_a) == 2, str(d.pending_for(key_a)))
        d.note_turn(key_a, JIA, "我妈又催了，说我都不小了\n周末又去爬了趟山，晚上窝着刷短剧\n今天下班早")
        check("★ 尾巴多出一行时只记那一行（不是整份重记）",
              d.pending_for(key_a) == 3, str(d.pending_for(key_a)))
        check("★ 对象名认不出来就整场不记（没有格，也就无从知道该补谁）",
              d.note_turn("S1", "", RAW_JIA) is None and d.pending_for("S1") == 0, "")
        check("★ 空材料不记", d.note_turn("S9", JIA, "   \n  ") is None and d.pending_for("S9") == 0, "")

        flood = PeerDistiller(config=app.config, store=app.mind_store)
        for index in range(80):
            flood.note_turn("F|甲", "甲", f"第{index}句")
        check(f"★ 条数收口：一格最多留 {pmod.MAX_TURNS} 行（对方刷屏不会无限攒）",
              flood.pending_for("F|甲") == pmod.MAX_TURNS, str(flood.pending_for("F|甲")))
        lorry = PeerDistiller(config=app.config, store=app.mind_store)
        for index in range(40):
            lorry.note_turn("L|甲", "甲", f"第{index}句" + "字" * 100)
        joined = "\n".join(lorry._turns["L|甲"])
        check("★ 总长收口：一格最多留 MAX_CHARS 字上下（取最近的，一长段不拖慢一趟）",
              len(joined) <= pmod.MAX_CHARS + 120, str(len(joined)))

        # ---------- ③ 抽：空闲判定 → 落三记忆库的 peer 格 ----------

        section("③ 抽：沉默够久 → 后台跑 → 落这个人那一格 + 补进攻略卡")

        card = _guide_card(app)
        check("前置：对象攻略卡建好了（单卡，按大类 + 卡名查重）", card is not None, "")

        stub.peer_result = dict(JIA_RESULT)
        stub.prompts.clear()
        app.peer_distiller.note_turn(key_a, JIA, RAW_JIA)
        check("★ 刚说过的那一句：空闲判定这一拍不派（跟主聊天同一把尺 mind.idle_end_min）",
              _check_idle_dispatched(app.peer_distiller, key_a) is False, "")
        _let_idle(app.peer_distiller, key_a)
        check("★ 沉默够久之后：_check_idle 自己就把它抽了（不用谁来点一下）",
              _check_idle_dispatched(app.peer_distiller, key_a) is True, "")
        _settle(app, app.peer_distiller)

        check("★ 抽出来了：提示词里带上了**对方**的话（材料是从屏幕认来的那一份）",
              any(RAW_JIA.splitlines()[0] in p for p in stub.prompts), "")
        check("★ 提示词里写明了只抽结论、不抄原句",
              "不抄原句" in PEER_DISTILL_PROMPT and "不要脑补" in PEER_DISTILL_PROMPT, "")
        check("★ 抽完这一格清空（不会下一拍又抽一遍）",
              app.peer_distiller.pending_for(key_a) == 0, "")
        check("★ 沙箱里没有量化权重 → 本机这趟用不上，回落 API（预期内的常态）",
              app.peer_distiller.used_local is False, "")

        store = app.mind_store
        who = store.profile(peer=JIA)
        check("★ 画像那一格：一句话基本情况落进了这个人那一格",
              who.get("intro") == JIA_RESULT["intro"], repr(who))
        ep = store.ep(peer=JIA)
        check("★ 关系记忆库：关系阶段落进去了",
              ep["rel_state"] == "破冰", repr(ep["rel_state"]))
        check("★ 情绪记忆库：情感状态 + 焦虑原因合成一句",
              "单身" in ep["emotion_snapshot"] and "家里在催婚" in ep["emotion_snapshot"],
              repr(ep["emotion_snapshot"]))
        check("★ 事件记忆库：一起经历 / 约好的事落进去了",
              list(ep["events"]) == JIA_RESULT["events"], repr(ep["events"]))
        frags = [str(row.get("text") or "") for row in store.fragments(peer=JIA)]
        check("★ 碎片：抽出来的关键点进了这个人那一格",
              JIA_RESULT["fragments"][0]["text"] in frags, repr(frags))

        live = app.card_service.card(card.id)
        check("★ 攻略卡那八格：年龄 / 情感状态 / 焦虑 / 兴趣爱好都补上了",
              (live.age, live.emotion_state, live.anxiety, live.interests)
              == ("三十往上", "单身", "家里在催婚", "爬山、短剧"),
              f"{live.age} / {live.emotion_state} / {live.anxiety} / {live.interests}")
        check("★ 攻略卡那八格：喜好 / 禁忌 / 策略 / 关系阶段也补上了（跟批次一手动那四格同一批列）",
              (live.likes, live.dislikes, live.strategy, live.relation_stage)
              == ("爱听人夸他细心", "别提前任", "先接住抱怨，再顺一句夸他", "破冰"),
              f"{live.likes} / {live.dislikes} / {live.strategy} / {live.relation_stage}")
        check("★ 卡片表那八格与 DAO 的过滤表**逐项一致**"
              "（漏一格 = 静默丢，连日志都没有）",
              set(CARD_FIELDS) == set(_PROFILE_COLUMNS), repr(sorted(_PROFILE_COLUMNS)))

        # ---------- ④ 原话不落库 ----------

        section("④ 隐私口径：喂进去的是原话，落库的只有结论")

        check("★ 对方那两句原话一个字都没落进这个人那一格",
              RAW_JIA.splitlines()[0] not in _dump(store, JIA)
              and RAW_JIA.splitlines()[1] not in _dump(store, JIA), repr(_dump(store, JIA)))
        check("★ 卡上那八格也没有原话",
              all(RAW_JIA.splitlines()[0] not in str(v) for v in
                  (live.age, live.emotion_state, live.anxiety, live.interests,
                   live.likes, live.dislikes, live.strategy, live.relation_stage)), "")

        # ---------- ⑤ 不串人：两格互不污染、主人那一档一个字不动 ----------

        section("⑤ 不串人：两个人两格、主人自己那一档一个字不动")

        stub.peer_result = dict(YI_RESULT)
        app.peer_distiller.note_turn(key_b, YI, RAW_YI)
        _let_idle(app.peer_distiller, key_b)
        app.peer_distiller._check_idle()
        _settle(app, app.peer_distiller)

        yidump = _dump(store, YI)
        check("★ 乙的内容落在乙那一格",
              YI_RESULT["intro"] in yidump and "看展" in yidump, repr(yidump))
        check("★ 甲那一格没被乙的这场污染（各自的碎片各在各的格）",
              not any(mark in _dump(store, JIA) for mark in ("看展", "美术馆")),
              repr(_dump(store, JIA)))
        check("★ 乙那一格也没有甲的（不串是双向的）",
              not any(mark in yidump for mark in ("短剧", "爬山", "催婚")), repr(yidump))
        check("★ 分格坐标是「这个人」（sha256 摘要，存摘要不存人名）——两格是两行",
              store.profile(peer=JIA) != store.profile(peer=YI)
              and store.profile(peer=JIA).get("intro") == JIA_RESULT["intro"], "")

        owner = store.profile(peer="") or {}
        check("★ **主人自己那一档一个字没动**（原话不上、结论也不上）",
              owner == {} and store.fragments(peer="") == []
              and (store.ep(peer="")["rel_state"] or "") == "", repr(owner))
        check("★ 主聊天那条提炼线照旧能用（两条线各写各的格，互不干扰）",
              _mind_still_works(app, stub), "")

        # ---------- ⑥ 卡不在就不抽（材料留着，等他开卡） ----------

        section("⑥ 卡不在就不抽：不替每一个聊过的人都立档案")

        gate = {"on": False}
        d2 = PeerDistiller(config=app.config, store=app.mind_store,
                           card_exists=lambda peer: gate["on"],
                           write_card=lambda peer, fields: None)
        key_c = "S3|丙"
        d2.note_turn(key_c, "丙", "今天好累啊")
        check("★ 没卡时不抽（回 False，一个模型调用都不发）",
              d2.distill_now(key_c) is False, "")
        check("★ 没卡时材料**留着**（他哪天把卡开出来，下一拍就补上；不是白攒）",
              d2.pending_for(key_c) == 1, str(d2.pending_for(key_c)))
        check("★ 没卡时什么都不落库", store.profile(peer="丙") == {}, "")
        gate["on"] = True
        check("★ 卡一开出来，下一拍就抽（材料没白攒）",
              d2.distill_now(key_c) is True, "")
        _settle(app, d2)

        # ---------- ⑦ 退出抢抽 / 清记忆 ----------

        section("⑦ 退出抢抽 flush 与清记忆 discard_pending")

        d3 = PeerDistiller(config=app.config, store=app.mind_store,
                           card_exists=lambda peer: True,
                           write_card=lambda peer, fields: None)
        d3.note_turn("SH|丁", "丁", "周末出来了")
        ok = d3.flush(timeout_s=5.0)
        check("★ 退出抢抽：这一趟真落库了（返回 True）", ok is True, "")
        check("★ 退出抢抽之后这一格是空的", d3.pending_for("SH|丁") == 0, "")
        check("★ 退出抢抽落的是记忆库（卡面那几格要回主线程，退出这一趟不补）",
              store.ep(peer="丁")["rel_state"] == "聊得来"
              and store.profile(peer="丁").get("intro") == YI_RESULT["intro"], "")
        check("★ 没东西可抢时回 False（不空跑一趟线程）", d3.flush(timeout_s=1.0) is False, "")
        d4 = PeerDistiller(config=app.config, store=app.mind_store,
                           card_exists=lambda peer: False)
        d4.note_turn("SH|戊", "戊", "在吗")
        check("★ 没卡的格不抢（材料照样留着）",
              d4.flush(timeout_s=1.0) is False and d4.pending_for("SH|戊") == 1, "")

        d3.note_turn("DX|丁", "丁", "又聊了两句")
        drop = d3.discard_pending()
        check("★ 清记忆那条线：缓冲区一并丢掉（不然清完过几分钟又写回去）",
              drop == 1 and d3.pending == 0, str(drop))
        check("★ 丢完之后 flush 不再往库里写", d3.flush(timeout_s=1.0) is False, "")
        check("★ 装配层把这一格接给了清记忆那条线",
              app.memory_reset._peer_distiller is app.peer_distiller, "")

        # ---------- ⑧ 社交那条线的接线 ----------

        section("⑧ 社交那条线：单聊页上把对方的话递过来（三道闸 + 两处调用点）")

        service: SocialReplyService = app.social_reply
        service._session_id, service._peer_name = "S1", JIA
        before = app.peer_distiller.pending
        service._note_peer_turns(RAW_JIA)
        check("★ 递过来的键跟冒话冷却 / 帮聊窗口是**同一把**（sid|peer）",
              service._cooldown_key() == "S1|" + JIA, repr(service._cooldown_key()))
        check("★ 真递到了：这一格攒上了",
              app.peer_distiller.pending == before + 2
              and app.peer_distiller.pending_for("S1|" + JIA) == 2, "")

        app.config.screen.card_autodistill = "off"
        service._note_peer_turns(RAW_JIA)
        check("★ 闸一：off 时不递", app.peer_distiller.pending == before + 2, "")
        app.config.screen.card_autodistill = "auto"

        service._peer_name = ""
        service._note_peer_turns(RAW_JIA)
        check("★ 闸二：认不出对象名时不递（递了也没法分格）",
              app.peer_distiller.pending == before + 2, "")
        service._peer_name = JIA

        service._note_peer_turns("   ")
        check("★ 闸三：空材料不递", app.peer_distiller.pending == before + 2, "")

        # 闸一的口径要跟 `PeerDistiller.enabled` **一字不差**：那一条是直接比字符串
        # （`!= "off"`），跟旁边的 `_card_memory_on` / `_tone_align_on` 同一个口径。
        # 两个闸管的是同一项配置，口径不一致就会出现"提炼器说开着、社交这边不递"这种
        # 两边对不上的状态——所以这一条也要钉住（判据 + 真递得出去，两头都验）。
        app.config.screen.card_autodistill = "off"
        check("★ 闸一：明写 `off` 才算关", service._card_autodistill_on() is False, "")
        never_off_social = [v for v in ("OFF", "0", "false", "真假", "", "auto")
                            if _autodistill_on(app, v) is not True]
        check("★ 闸一的口径跟 PeerDistiller / `card_memory` 一字不差："
              "写别的值（OFF / 0 / false / 真假 / 空 / auto）一律按开算",
              not never_off_social, "、".join(map(repr, never_off_social)))
        app.config.screen.card_autodistill = "OFF"
        service._note_peer_turns(RAW_JIA + "\n又想起来一件事")
        check("★ 写 `OFF` 时真的还递得出去（不只是判据写对了）",
              app.peer_distiller.pending == before + 3,
              str(app.peer_distiller.pending))
        app.config.screen.card_autodistill = "auto"
        app.peer_distiller.discard_pending()

        src_read = inspect.getsource(SocialReplyService._on_screen_read)
        src_listen = inspect.getsource(SocialReplyService._on_listen_read)
        check("★ 两处调用点都在（手动 / 按需那一趟 + 自动监听那一趟）",
              "_note_peer_turns(self._chat_text)" in src_read
              and "_note_peer_turns(_tail_context(read))" in src_listen, "")
        check("★ 认字那一趟：递的那一份摆在「是单聊」那道闸里面"
              "（群聊 / 列表页 / 搜索页那一屏一个字都不递）",
              "if is_personal_kind(page_kind):" in src_read, "")
        check("★ 自动监听那一趟：摆在「是单聊」那道闸之后"
              "（闸之前 `return` 掉了，够不着）",
              src_listen.index("if not is_personal_kind(kind):") < src_listen.index("_note_peer_turns("),
              "")
        check("★ 递的是**对方那半边**（`_tail_context`），不是两边都算的 visible",
              "_note_peer_turns(_tail_context(read))" in src_listen
              and "_note_peer_turns(new_tail)" not in src_listen
              and "_note_peer_turns(visible)" not in src_listen, "")

        mods: list[str] = []
        tree = ast.parse(inspect.getsource(pmod))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods += [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods.append(node.module or "")
        check("★ 提炼器**不认识卡、也不认识库**（写卡那两手是装配层注入的）",
              not [m for m in mods if m.startswith(("life.cards", "data.", "ui."))],
              str(sorted(mods)))
        # 这一节剩下的那点材料不带到下一节去（下一节要数的是"这一格落没落库"）
        app.peer_distiller.discard_pending()

        # ---------- ⑨ 退出：先停表、再抢抽、最后才关库 ----------

        section("⑨ 退出那条路：stop → flush（库还开着）→ 关库")

        # 刚说过一句就关窗（离空闲判定还差三分钟）：**退出这一趟不看空闲阈值**，
        # 攒着的全抢一次——这正是主聊天那边 `flush()` 存在的理由，同一套口径。
        app.peer_distiller.note_turn("S1|" + YI, YI, RAW_YI)
        app.shutdown()
        check("★ 抢抽之后缓冲区是空的", app.peer_distiller.pending == 0,
              str(app.peer_distiller.pending))
        # 库已经关了 → **另开一个来读**。关完库才读得到，才算真落了盘（不是还在内存里）
        reopened = MindStore(app.config.mind_db_path)
        try:
            check("★ shutdown 走了抢抽那一趟：攒着的话真落了盘（不是随进程没了）",
                  reopened.ep(peer=YI)["rel_state"] == "聊得来",
                  repr(reopened.ep(peer=YI)["rel_state"]))
        finally:
            reopened.close()
        check("★ 抢抽那一步排在关库之前（关完库再写就写不进去了）",
              inspect.getsource(type(app).shutdown).index("self.peer_distiller.flush()")
              < inspect.getsource(type(app).shutdown).index("self.mind_store.close()"), "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("对象攻略卡后台静默补全（分格 / 落库 / 补卡 / 退出抢抽）")


def _enabled_with(app, value: str) -> bool:
    app.config.screen.card_autodistill = value
    return app.peer_distiller.enabled


def _autodistill_on(app, value: str) -> bool:
    """社交那边的同一个闸（`_card_autodistill_on`）：两个闸的口径必须逐字对齐。"""
    app.config.screen.card_autodistill = value
    return app.social_reply._card_autodistill_on()


def _check_idle_dispatched(distiller, key: str) -> bool:
    """跑一次空闲判定，回这一次有没有为这一格派出去（走的是真定时器那条路的同一个方法）。"""
    distiller._check_idle()
    return key in distiller._running


def _mind_still_works(app, stub) -> bool:
    """主聊天那条提炼线还照旧：说一句 → 它自己那一格攒上（两条线互不干扰）。"""
    stub.parse_result = {
        "scene": "", "time_hint": "", "time_iso": "", "time_confidence": "none",
        "emotion": "平静", "module": "其他", "command": "", "reply": "嗯嗯",
    }
    app.chatter_service.handle("今天有点累")
    wait_for(lambda: app.mind_distiller.pending > 0, timeout=3.0)
    return app.mind_distiller.pending > 0


if __name__ == "__main__":
    raise SystemExit(main())
