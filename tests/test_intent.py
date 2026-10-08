"""
指令 26-B 的自动测试：主动服务的统一决策树 + 限频。

离屏、桩模型、不联网：
- 本地判定（工作词 + 任务动作 / 八大类命中）直接调 IntentJudge，不需要模型；
  带时间词的句子归谁：品类词压过时间词，显式提醒词压过品类词；
- 决策树走真的 ChatterService：桩模型只决定"这句话解析成哪个 module/topic、情绪几分"，
  再断言"该弹的弹了、该问的问了、闲聊什么都没发生"；
- 限频与跨重启：库里那笔记录说话，重新构造应用之后还拦得住。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtWidgets import QDialog

from life.cards.templates import category_icon
from life.chatter import POP_ASK_TIMES, POP_DECLINE_SILENCE, SERVICE_WINDOW
from life.intent import LIFE, NONE, WORK


def _parse(module: str = "其他", topic: str = "", emotion: float = 0.3) -> dict:
    """桩模型对这句话的解析（只填跟主动服务有关的那几样）。"""
    return {
        "scene": module,
        "time_hint": "",
        "time_iso": "",
        "time_confidence": "none",
        "emotion": "平静",
        "emotion_score": emotion,
        "topic": topic,
        "module": module,
        "command": "",
    }


class Flow:
    """驱动一轮对话，并把"弹了哪张卡 / 问了什么 / 说了什么"都收下来。"""

    def __init__(self, app, stub):
        self.app, self.stub = app, stub
        self.spoken: list[str] = []
        self.offers: list[str] = []
        self.cards: list[tuple[int, str]] = []
        self.works: list[str] = []
        self.proposed: list[str] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.service_offer.connect(self.offers.append)
        app.bus.service_show_card.connect(
            lambda card_id, text: self.cards.append((card_id, text))
        )
        app.bus.service_open_work.connect(self.works.append)
        app.bus.card_proposed.connect(lambda theme, _title: self.proposed.append(theme))

    def say(
        self,
        text: str,
        *,
        module: str = "其他",
        topic: str = "",
        emotion: float = 0.3,
    ) -> None:
        """用户说了一句：解析 → 回话 → 主动服务，一条线走完。"""
        self.stub.parse_result = _parse(module, topic, emotion)
        self.app.chatter_service.handle(text)
        pump(0.3)

    def settle(self) -> None:
        """再转一圈，让"晚一步"的那条（场景建议）也落地。"""
        pump(0.3)


def _test_local() -> None:
    box, stub, app = make_env()
    judge = app.chatter_service._serve.intent

    section("本地判定：工作要两层都对上；生活要品类词 +（求助信号 或 报了时间）")
    check(
        "工作词 + 任务动作 → 工作意图",
        judge.judge("BOM 方案今天要做完").kind == WORK,
        judge.judge("BOM 方案今天要做完").kind,
    )
    check(
        "只有工作词、没有任务动作 → 不动",
        judge.judge("方案又要改了").kind == NONE,
        judge.judge("方案又要改了").kind,
    )
    check(
        "大小写不敏感：小写 bom 照样判工作（BOM + 做完 双命中）",
        judge.judge("bom表还没做完，要快点").kind == WORK,
        judge.judge("bom表还没做完，要快点").kind,
    )
    life = judge.judge("怎么办？见客户快要迟到了！")
    check(
        "出行词 + 求助信号 → 生活意图·出行",
        life.kind == LIFE and life.theme == "出行",
        f"{life.kind} / {life.theme}",
    )
    check(
        "八大类命中、但没求助信号也没报时间 → 不动（吐槽不是求助）",
        judge.judge("今天地铁挤爆了").kind == NONE,
        judge.judge("今天地铁挤爆了").kind,
    )
    timed = judge.judge("明天见客户没衣服穿愁死", timed=True)
    check(
        "报了时间的句子：时间词压不过品类词（这一句落在穿搭）",
        timed.kind == LIFE and timed.theme == "穿搭",
        f"{timed.kind} / {timed.theme}",
    )
    check(
        "口子只开给报了时间的：同一件事没报时间照旧不动",
        judge.judge("最近没衣服穿愁死").kind == NONE,
        judge.judge("最近没衣服穿愁死").kind,
    )
    check(
        "显式提醒词是例外：明说要提醒的还给提醒那条线（报了时间也压得住）",
        judge.judge("提醒我明天见客户", timed=True).kind == NONE
        and judge.judge("叫我一声，我今天想跑步", timed=True).kind == NONE,
        judge.judge("提醒我明天见客户", timed=True).kind,
    )
    check("吃饭闲聊零触发", judge.judge("中午吃啥好呢").kind == NONE, "none")
    check("天气闲聊零触发", judge.judge("今天天气真好").kind == NONE, "none")
    check("纯情绪零触发", judge.judge("加班好累").kind == NONE, "none")

    urgent = judge.judge("来不及了，见客户快迟到，怎么办")
    check(
        "急词命中 → 生活意图带上 urgent",
        urgent.kind == LIFE and urgent.theme == "出行" and urgent.urgent,
        f"{urgent.kind} / {urgent.theme} / urgent={urgent.urgent}",
    )
    calm = judge.judge("怎么办，地铁又停了")
    check(
        "没急词 → urgent 是 False（照旧走渐进授权）",
        calm.kind == LIFE and not calm.urgent,
        f"{calm.kind} / {calm.theme} / urgent={calm.urgent}",
    )
    meta = judge.judge("怎么办，最近是不是犯太岁了")
    check(
        "命理词 + 求助信号 → 生活意图·命理（跟别的大类用同一套词表，不另起一份）",
        meta.kind == LIFE and meta.theme == "命理",
        f"{meta.kind} / {meta.theme}",
    )
    dest = judge.judge("大后天跑上海", timed=True)
    check(
        "报目的地（跑上海）+ 报了时间 → 出行意图（词表里没有摩擦词也认得出）",
        dest.kind == LIFE and dest.theme == "出行",
        f"{dest.kind} / {dest.theme}",
    )
    check(
        "报目的地、但没报时间也没求助 → 照旧不动（只是陈述，不是求助）",
        judge.judge("我想去上海").kind == NONE,
        judge.judge("我想去上海").kind,
    )

    section("模型兜底只开在窄口子上（把握 < 0.7 不动）")
    check("纯情绪不请模型", judge.needs_fallback("加班好累", 0.8) is False, "False")
    check(
        "有工作词但没动作、也没求助 → 不请模型",
        judge.needs_fallback("方案又被打回来了", 0.8) is False,
        "False",
    )
    check(
        "生词 + 求助信号 + 带情绪 → 值得请模型看一眼",
        judge.needs_fallback("那个东西我快撑不住了", 0.6) is True,
        "True",
    )
    check("本地已经判出来了就不再请模型", judge.needs_fallback("BOM 今天要做完", 0.9) is False, "False")

    check("兜底：把握不够 → 不动作", not judge.from_model(LIFE, "出行", 0.5).actionable, "0.5")
    check("兜底：把握够、品类合法 → 生活意图", judge.from_model(LIFE, "出行", 0.9).theme == "出行", "出行")
    check("兜底：品类不在八大类里 → 不动作", not judge.from_model(LIFE, "天气", 0.9).actionable, "天气")
    check("兜底：判成闲聊 → 不动作", not judge.from_model(NONE, "", 0.9).actionable, "none")
    check(
        "兜底：模型说「特别急」→ 意图带上 urgent",
        judge.from_model(LIFE, "出行", 0.9, True).urgent is True,
        str(judge.from_model(LIFE, "出行", 0.9, True)),
    )

    drain_threads()
    box.cleanup()


def _seed_consent(app, theme: str) -> None:
    """把某个主题的点头次数直接攒满：跳过"先问三次"，直接看"以后直接弹"那一步。"""
    for _ in range(POP_ASK_TIMES):
        app.service_consent_dao.accept(app.user.id, theme)


def _test_life_card() -> None:
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("生活意图 + 手上有这张卡：第一次先问，点头才弹")
    trip = app.card_service.active_card_of_theme("出行")
    check("预置的出行卡就摆在轮播上", trip is not None, trip.title if trip else "无")
    # 联网正文带角标（厂商接口强制加的）：场景建议里要摘干净、也不附来源列表
    stub.online_text = "最快是打车转地铁，大概三十五分钟[6]。出门前扫一眼导航[9]，别站错口。"
    # 这句不带急词（"迟到/来不及"那类），所以走的是"先问一句"的渐进授权
    flow.say("怎么办，地铁又停了", module="出行", topic="通勤", emotion=0.6)

    check(
        "先问一句，没直接弹",
        wait_for(lambda: flow.offers == [LIFE] and flow.cards == [], timeout=10),
        f"offers={flow.offers} cards={flow.cards}",
    )
    check(
        "问的是「要不要翻出来看看」",
        any("翻出来看看" in text for text in flow.spoken),
        str(flow.spoken),
    )

    app.chatter_service.handle("好呀")
    check(
        "点头 → 出行卡弹出来，并把刚说的那句话一起带过去",
        wait_for(lambda: bool(flow.cards)) and flow.cards[0] == (trip.id, "怎么办，地铁又停了"),
        str(flow.cards[:1]),
    )
    check("先安抚一句，不是先讲道理", any("别急" in text for text in flow.spoken), str(flow.spoken[:2]))
    check(
        "点头次数记上了一笔",
        app.service_consent_dao.state(app.user.id, "出行")[0] == 1,
        str(app.service_consent_dao.state(app.user.id, "出行")),
    )
    check(
        "场景建议（联网那条链）跟着说出来",
        wait_for(lambda: any("打车转地铁" in text for text in flow.spoken)),
        str(flow.spoken[-1:]),
    )
    advice = [text for text in flow.spoken if "打车转地铁" in text]
    check(
        "只留建议本身：角标摘掉了、也不附来源列表",
        bool(advice) and "[6]" not in advice[0] and "参考来源" not in advice[0],
        advice[0] if advice else "没等到",
    )
    # 出行卡详情页那条完整规划照旧带来源：摘角标只管聊天里这一条即时建议
    stub.online_sources = 2
    check(
        "详情页的完整规划仍然带来源（没被一起改掉）",
        "参考来源" in app.card_service._compose_trip("北京", datetime.now()),
        "带来源",
    )
    stub.online_sources = 0

    drain_threads()

    section("同一句里只出现一件事：弹了卡就不再问「要不要建张卡」")
    flow2 = Flow(app, stub)
    diet = app.card_service.active_card_of_theme("餐饮")
    check("饮食卡也在轮播上", diet is not None and diet.title == "饮食", diet.title if diet else "无")
    _seed_consent(app, "餐饮")        # 问够三次了：以后直接弹
    # 情绪很强（0.9）：卡牌那条线本来够格出"要不要建张卡"的邀请
    flow2.say("怎么办，外卖太油了吃不下", module="餐饮", topic="外卖", emotion=0.9)
    check(
        "点头次数攒够 → 直接弹，不再问",
        flow2.offers == []
        and wait_for(lambda: bool(flow2.cards))
        and flow2.cards[0][0] == diet.id,
        f"offers={flow2.offers} cards={flow2.cards[:1]}",
    )
    check(
        "没再冒第二句「要不要建张卡」（手上已有卡 + 这一场也动过手）",
        flow2.proposed == [],
        str(flow2.proposed),
    )

    drain_threads()
    box.cleanup()


def _test_progressive_consent() -> None:
    """渐进授权（指令 26-B 修订）：问三次都点头 → 以后直接弹；拒绝 → 静默 12 小时。"""
    box, stub, app = make_env()
    flow = Flow(app, stub)
    target = "怎么办，地铁又停了"      # 不带急词：走的是"先问三次"的渐进授权

    section("问三次都点头 → 第四次起直接弹")
    for round_no in range(1, POP_ASK_TIMES + 1):
        flow.offers.clear()
        flow.cards.clear()
        flow.say(target, module="出行", topic=f"第{round_no}次", emotion=0.6)
        check(
            f"第 {round_no} 次：先问，没直接弹",
            wait_for(lambda: flow.offers == [LIFE] and flow.cards == [], timeout=10),
            f"offers={flow.offers} cards={flow.cards}",
        )
        app.chatter_service.handle("好呀")
        check(
            f"第 {round_no} 次点头 → 卡弹出来，点头次数={round_no}",
            wait_for(lambda: len(flow.cards) == 1)
            and app.service_consent_dao.state(app.user.id, "出行")[0] == round_no,
            str(app.service_consent_dao.state(app.user.id, "出行")),
        )

    flow.offers.clear()
    flow.cards.clear()
    flow.say(target, module="出行", topic="第四次", emotion=0.6)
    check(
        "攒够之后：不再问，直接弹",
        flow.offers == [] and wait_for(lambda: bool(flow.cards)),
        f"offers={flow.offers} cards={flow.cards[:1]}",
    )

    section("拒绝 → 静默 12 小时（不弹也不问）；过期后从头再问")
    flow.offers.clear()
    flow.cards.clear()
    flow.say("怎么办，外卖太油了吃不下", module="餐饮", topic="外卖", emotion=0.6)
    check("饮食卡第一次也是先问", wait_for(lambda: flow.offers == [LIFE], timeout=10), str(flow.offers))
    app.chatter_service.handle("不用了")
    check(
        "拒绝后点头次数归零、记下拒绝时刻",
        app.service_consent_dao.state(app.user.id, "餐饮")[0] == 0
        and app.service_consent_dao.state(app.user.id, "餐饮")[1] is not None,
        str(app.service_consent_dao.state(app.user.id, "餐饮")),
    )
    flow.offers.clear()
    flow.cards.clear()
    flow.say("怎么办，又点了油腻的外卖", module="餐饮", topic="外卖第二回", emotion=0.6)
    check(
        "静默期内：不弹也不问",
        flow.offers == [] and flow.cards == [],
        f"offers={flow.offers} cards={flow.cards}",
    )

    # 把拒绝时刻挪到 13 小时前，模拟"静默期过了"
    from sqlalchemy import select as sa_select

    from data.models import ServiceConsent

    with app.db.session_factory() as session:
        for row in session.scalars(sa_select(ServiceConsent)):
            row.declined_at = datetime.now() - POP_DECLINE_SILENCE - timedelta(hours=1)
        session.commit()
    flow.offers.clear()
    flow.say("怎么办，外卖太油了吃不下", module="餐饮", topic="外卖第三回", emotion=0.6)
    check("静默期过了 → 从「问」重新开始", wait_for(lambda: flow.offers == [LIFE], timeout=10), str(flow.offers))

    drain_threads()

    section("重启保持：点头次数与拒绝时刻都在库里")
    from harness import build_app

    app2 = build_app()          # 走 build_app 才会登记到沙箱上，cleanup() 才关得掉
    check(
        "重启后出行还是「直接弹」那一档",
        app2.service_consent_dao.state(app2.user.id, "出行")[0] >= POP_ASK_TIMES,
        str(app2.service_consent_dao.state(app2.user.id, "出行")),
    )
    check(
        "重启后饮食的拒绝记录还在（静默期照旧）",
        app2.service_consent_dao.state(app2.user.id, "餐饮")[1] is not None,
        str(app2.service_consent_dao.state(app2.user.id, "餐饮")),
    )
    box.cleanup()


def _test_life_invite() -> None:
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("生活意图 + 手上没这张卡 → 按邀请制问一句")
    check("作息类还没有卡", app.card_service.active_card_of_theme("作息") is None, "无")
    flow.say("怎么办，明天要早起可我老是失眠", module="作息", topic="失眠", emotion=0.6)

    check("出了建卡邀请", flow.proposed == ["作息"], str(flow.proposed))
    check("没有直接弹卡（手上本来就没有这张）", flow.cards == [], str(flow.cards))
    check(
        "问的是「要不要备一张」，不是工作那条线的问法",
        any("做一张卡" in text for text in flow.spoken),
        str(flow.spoken),
    )

    drain_threads()
    box.cleanup()


class _AutoAcceptOffer:
    """把邀请确认框换成"直接点确定"，用来走 chatter → 确认框 → 建卡 这条全程。

    真对话框是模态的（dialog.exec() 会阻塞），测试里必须换掉，否则整条线卡在那儿。
    """

    def __init__(self, theme: str, parent=None):
        self._theme = theme

    def exec(self) -> int:
        return QDialog.Accepted

    def values(self) -> tuple[str, str]:
        return self._theme, category_icon(self._theme)

    def deleteLater(self) -> None:
        pass


def _test_card_made_focus() -> None:
    """点头建完卡 → 当场把这张卡翻到眼前（复用主动服务那条聚焦路），不停在聊天框。"""
    import core.bootstrap as bootstrap_mod

    box, stub, app = make_env()
    flow = Flow(app, stub)
    app.pet_window.show()
    pump(0.05)

    section("点头建卡：建完当场翻开，不让人自己去生活广场找")
    check("作息类还没有卡", app.card_service.active_card_of_theme("作息") is None, "无")
    flow.say("怎么办，明天要早起可我老是失眠", module="作息", topic="失眠", emotion=0.6)
    check("先出建卡邀请", flow.proposed == ["作息"], str(flow.proposed))

    real_dialog = bootstrap_mod.CardOfferDialog
    bootstrap_mod.CardOfferDialog = _AutoAcceptOffer
    try:
        app.chatter_service.handle("好呀")
        pump(0.3)
    finally:
        bootstrap_mod.CardOfferDialog = real_dialog

    made = app.card_service.active_card_of_theme("作息")
    check("卡建出来了", made is not None, made.title if made else "无")
    check(
        "建完当场把它翻到眼前（带的是空场景文本）",
        made is not None and flow.cards == [(made.id, "")],
        str(flow.cards),
    )
    check("放大定格层开着", app.overlay.is_open(), str(app.overlay.is_open()))
    check(
        "这条不让位：卡翻开了，宠物还站在旁边（没被「啪」地抽走）",
        app.pet_window.isVisible() and not app._pet_yielded,
        f"宠物可见={app.pet_window.isVisible()} _pet_yielded={app._pet_yielded}",
    )
    app.overlay.close_overlay()
    check(
        "关上之后宠物也还在（本来就一直没走）",
        wait_for(lambda: not app.overlay.is_open(), timeout=2.0) and app.pet_window.isVisible(),
        f"overlay.is_open()={app.overlay.is_open()} 宠物可见={app.pet_window.isVisible()}",
    )
    check(
        "回话是「卡建好了」，且不再让人去生活广场",
        any("卡建好了" in text for text in flow.spoken)
        and not any("生活广场" in text for text in flow.spoken),
        str(flow.spoken[-2:]),
    )

    drain_threads()
    box.cleanup()


def _test_work() -> None:
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("工作意图 → 先回应 → 询问 → [好呀] 开工作模块")
    flow.say("BOM 方案今天要做完", module="其他", topic="BOM", emotion=0.4)

    check("先出了提议（露轻按钮）", wait_for(lambda: flow.offers == [WORK], timeout=10), str(flow.offers))
    check(
        "对话框里把 [好呀] [不用了] 露出来了",
        app.chat_bubble._offer_theme == WORK,
        app.chat_bubble._offer_theme,
    )
    check(
        "问的是「要不要打开工作模块」",
        any("工作模块" in text for text in flow.spoken),
        str(flow.spoken),
    )
    from life.chatter import ConversationState

    check(
        "进入等待答复的状态",
        app.chatter_service.state == ConversationState.AWAITING_SERVICE,
        app.chatter_service.state.value,
    )
    check("还没开工作模块（工作模块是大窗口，先问再开）", flow.works == [], str(flow.works))

    app.chatter_service.handle("好呀")
    pump(0.1)
    check("点头 → 开工作模块，并带上原话", flow.works == ["BOM 方案今天要做完"], str(flow.works))
    check(
        "开完回到空闲",
        app.chatter_service.state == ConversationState.IDLE,
        app.chatter_service.state.value,
    )
    # 原先这里还有四条「猜部门」断言（BOM 落研发部、卖点落销售部、成本落成本部…）。
    # 那是工作模块内部那张部门流转图上的数据，没随公开版发布（公开树里 work/ 是空壳，
    # 猜部门一律返回 None），一并摘了。工作那条线本身照旧：出提议 → [好呀] →
    # 开工作模块（公开版开出来的是块锁着的占位页）。

    drain_threads()
    box.cleanup()


def _test_work_fuzzy_time() -> None:
    """工作句带模糊时间（"月底要交"）不该被"几点提醒你"顶掉——工作卡得弹得出来。

    原先带时间词的句子整句被提醒那条线接走，工作句也不例外：状态机进了 AWAITING_TIME、
    反问一句"几点"，_maybe_serve 撞上"不是 IDLE 就别插话"的守卫，工作模块一条都轮不上。
    只有生活那条线开过这道口子（_is_life_matter），工作漏了，所以"工作卡还是没弹出来"。
    """
    box, stub, app = make_env()
    flow = Flow(app, stub)

    from life.chatter import ConversationState

    section("工作句 + 模糊时间词 → 走工作那条线，不反问「几点」")
    stub.parse_result = dict(
        _parse("其他", "BOM", 0.4), time_hint="月底", time_confidence="fuzzy"
    )
    app.chatter_service.handle("BOM 方案月底要交")
    check(
        "出了工作提议（不是被反问几点）",
        wait_for(lambda: flow.offers == [WORK], timeout=10)
        and app.chatter_service.state == ConversationState.AWAITING_SERVICE,
        f"offers={flow.offers} state={app.chatter_service.state.value}",
    )
    check(
        "没问「几点」",
        not any("几点" in text for text in flow.spoken),
        str(flow.spoken),
    )
    app.chatter_service.handle("好呀")
    pump(0.1)
    check(
        "点头 → 开工作模块，带上原话",
        flow.works == ["BOM 方案月底要交"],
        str(flow.works),
    )

    drain_threads()
    box.cleanup()


def _test_emotion_and_chitchat() -> None:
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("纯情绪 / 闲聊 → 什么都不动，只安抚")
    flow.say("加班好累", module="作息", topic="加班", emotion=0.6)
    check("回了话（安抚）", bool(flow.spoken), str(flow.spoken))
    check("没问、没弹、没邀", not flow.offers and not flow.cards and not flow.proposed, "都没有")

    flow.say("今天天气真好", module="其他", topic="", emotion=0.1)
    flow.settle()
    check(
        "闲聊零触发（T1 对照组的零误报守住）",
        not flow.offers and not flow.cards and not flow.proposed,
        f"offers={flow.offers} cards={flow.cards} proposed={flow.proposed}",
    )

    drain_threads()
    box.cleanup()


def _test_limits() -> None:
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("同场只动一次；生活卡不再有 24 小时那条限制")
    _seed_consent(app, "出行")        # 点头次数攒够：走"直接弹"那一档
    flow.say("怎么办？见客户快要迟到了！", module="出行", topic="通勤", emotion=0.6)
    check("第一次照常弹卡", wait_for(lambda: len(flow.cards) == 1), str(len(flow.cards)))

    flow.say("怎么办，路上堵车要迟到了", module="出行", topic="通勤", emotion=0.6)
    flow.settle()
    check("同一个话题里只动一次手", len(flow.cards) == 1, f"弹了 {len(flow.cards)} 次")

    flow.say("怎么办，地铁又停运了", module="出行", topic="地铁", emotion=0.6)
    flow.settle()
    check(
        "换了话题（新的一场）可以再弹：24 小时那条限制已经取消",
        len(flow.cards) == 2,
        f"弹了 {len(flow.cards)} 次",
    )
    check(
        "跨场次也不会冒出「自建出行卡」邀请（手上已有出行卡）",
        flow.proposed == [],
        str(flow.proposed),
    )

    section("工作那条线：拒绝后 24 小时不再问；连着点头 3 次后直接开")
    flow2 = Flow(app, stub)
    flow2.say("BOM 方案明天要做完", module="其他", topic="方案", emotion=0.4)
    check("工作那条线出了提议", wait_for(lambda: flow2.offers == [WORK], timeout=10), str(flow2.offers))
    app.chatter_service.handle("不用了")
    pump(0.1)
    check("拒绝后不开工作模块", flow2.works == [], str(flow2.works))
    flow2.say("工单也要交出去", module="其他", topic="工单", emotion=0.4)
    check("拒绝后 24 小时内不再问", flow2.offers == [WORK], str(flow2.offers))

    # 把「工作」的拒绝时刻挪出 24 小时窗口，模拟静默期过了，再连着点头 3 次
    from sqlalchemy import select as sa_select

    from data.models import ServiceConsent

    with app.db.session_factory() as session:
        for row in session.scalars(
            sa_select(ServiceConsent).where(ServiceConsent.theme == "工作")
        ):
            row.declined_at = datetime.now() - SERVICE_WINDOW - timedelta(hours=1)
        session.commit()
    for round_no in range(1, POP_ASK_TIMES + 1):
        flow2.offers.clear()
        flow2.works.clear()
        flow2.say(f"方案{round_no}要做完", module="其他", topic=f"方案{round_no}", emotion=0.4)
        check(
            f"第 {round_no} 次点头前仍先问",
            wait_for(lambda: flow2.offers == [WORK], timeout=10),
            str(flow2.offers),
        )
        app.chatter_service.handle("好呀")
        pump(0.1)
        check(
            f"第 {round_no} 次点头 → 开模块，点头次数记上了",
            flow2.works == [f"方案{round_no}要做完"]
            and app.service_consent_dao.state(app.user.id, "工作")[0] == round_no,
            f"works={flow2.works} 点头={app.service_consent_dao.state(app.user.id, '工作')}",
        )

    flow2.offers.clear()
    flow2.works.clear()
    flow2.say("报告要交了", module="其他", topic="报告", emotion=0.4)
    check(
        "连着点头 3 次之后 → 直接开，不再问",
        wait_for(lambda: bool(flow2.works), timeout=10) and flow2.offers == [],
        f"offers={flow2.offers} works={flow2.works}",
    )

    drain_threads()

    section("重启保持：点头次数与限频记录都在库里")
    from harness import build_app

    app2 = build_app()          # 走 build_app 才会登记到沙箱上，cleanup() 才关得掉
    flow3 = Flow(app2, stub)
    flow3.say("怎么办，地铁又停了", module="出行", topic="地铁", emotion=0.6)
    check("重启后出行还是「直接弹」那一档", wait_for(lambda: bool(flow3.cards)), str(flow3.cards[:1]))
    flow3.say("BOM 方案今天要做完", module="其他", topic="BOM", emotion=0.4)
    check(
        "重启后工作仍记得连着点头 3 次 → 直接开",
        wait_for(lambda: bool(flow3.works), timeout=10) and flow3.offers == [],
        f"offers={flow3.offers} works={flow3.works}",
    )

    box.cleanup()


def _test_work_beats_travel() -> None:
    """工作的事和赶时间同现时，判工作；[好呀] 只开工作模块，绝不弹出行卡（指令 26-B 回归）。"""
    box, stub, app = make_env()
    judge = app.chatter_service._serve.intent

    section("工作实体词 + 赶时间的说法 → 判工作，不是出行")
    for text in (
        "赶时间做BOM",
        "BOM 方案赶时间，怎么办，快要迟到了",
        "项目需求要赶工，deadline 快到了",
    ):
        intent = judge.judge(text)
        check(f"「{text}」→ 工作", intent.kind == WORK, f"{intent.kind} / {intent.theme}")
    life = judge.judge("怎么办？见客户快要迟到了！")
    check(
        "出行那句照旧判出行（没被回退）",
        life.kind == LIFE and life.theme == "出行",
        f"{life.kind} / {life.theme}",
    )
    check("「见客户快要迟到了」是急事 → urgent", life.urgent, f"urgent={life.urgent}")
    life2 = judge.judge("赶时间见客户")
    check(
        "「赶时间见客户」→ 出行（没提活儿，赶的是路）",
        life2.kind == LIFE and life2.theme == "出行",
        f"{life2.kind} / {life2.theme}",
    )
    for text in ("见客户快迟到了，怎么办？", "赶着去机场赶飞机"):
        intent = judge.judge(text)
        check(
            f"真人口吻「{text}」→ 出行",
            intent.kind == LIFE and intent.theme == "出行",
            f"{intent.kind} / {intent.theme}",
        )
    work_like = judge.judge("BOM方案今天必须要做完，太赶了，真烦")
    check(
        "真人口吻「BOM方案今天必须要做完，太赶了，真烦」→ 工作",
        work_like.kind == WORK,
        f"{work_like.kind} / {work_like.theme}",
    )

    section("点 [好呀] 走的是开工作模块，不碰出行卡")
    flow = Flow(app, stub)
    text = "赶时间做BOM，方案今天要做完"
    flow.say(text, module="其他", topic="BOM", emotion=0.5)
    check(
        "先出的是工作提议",
        wait_for(lambda: flow.offers == [WORK] and flow.cards == [], timeout=10),
        f"{flow.offers} / {flow.cards}",
    )
    app.chatter_service.handle("好呀")
    pump(0.2)
    check("开的是工作模块，带上原话", flow.works == [text], str(flow.works))
    check("一张卡都没弹（尤其不是出行卡）", flow.cards == [], str(flow.cards))
    # 同上：这条原先还钉了一句「落到研发部那张卡上」——部门流转不在公开树里。

    drain_threads()
    box.cleanup()


def _test_stale_overlay() -> None:
    """出行卡弹过之后，再点头开工作模块，眼前得是工作模块（不是那张旧卡）。"""
    box, stub, app = make_env()
    flow = Flow(app, stub)

    section("出行卡弹过之后，点头开工作模块不该还停在出行卡上")
    _seed_consent(app, "出行")
    flow.say("怎么办？见客户快要迟到了！", module="出行", topic="通勤", emotion=0.6)
    check("先弹出行卡", wait_for(lambda: bool(flow.cards)), str(flow.cards[:1]))
    check("放大定格层开着", app.overlay.is_open(), str(app.overlay.is_open()))

    flow.say("BOM 方案今天要做完", module="其他", topic="方案", emotion=0.5)
    check("出的是工作提议", wait_for(lambda: flow.offers == [WORK], timeout=10), str(flow.offers))
    app.chatter_service.handle("好呀")
    pump(0.3)
    check("点头后开工作模块", flow.works == ["BOM 方案今天要做完"], str(flow.works))
    check(
        "定格层已经收起（眼前是工作模块，不是旧的那张出行卡）",
        wait_for(lambda: not app.overlay.is_open(), timeout=2.0),
        f"overlay.is_open()={app.overlay.is_open()}",
    )

    drain_threads()
    box.cleanup()


def _test_back_to_back_cards() -> None:
    """连着弹两张卡：第二张弹出时会把第一张的关闭动画打断，不能因此把自己收掉。

    这条走的是"卡还开着的时候又来一张卡"：装配层会先收起旧卡再打开新卡，
    两次动画挨在一起（指令 26-B 回归）。
    """
    box, stub, app = make_env()
    flow = Flow(app, stub)
    app.pet_window.show()
    pump(0.05)

    section("连着弹两张卡：第二张得好好留在眼前")
    _seed_consent(app, "出行")
    _seed_consent(app, "餐饮")
    flow.say("怎么办？见客户快要迟到了！", module="出行", topic="通勤", emotion=0.6)
    check("第一张（出行）弹出来，定格层开着", wait_for(lambda: bool(flow.cards)) and app.overlay.is_open(), str(flow.cards[:1]))
    flow.say("怎么办，外卖太油了吃不下", module="餐饮", topic="外卖", emotion=0.6)
    check("第二张（饮食）也弹出来", wait_for(lambda: len(flow.cards) == 2), str(flow.cards))
    pump(0.6)      # 等放大动画走完
    check(
        "动画落定后，第二张卡仍在眼前（没被上一次的关闭顺手收掉）",
        app.overlay.is_open(),
        f"overlay.is_open()={app.overlay.is_open()} closing={app.overlay._closing}",
    )
    check(
        "两张卡期间宠物让位了（第一次弹卡时藏的）",
        not app.pet_window.isVisible() and app._pet_yielded,
        f"宠物可见={app.pet_window.isVisible()} _pet_yielded={app._pet_yielded}",
    )
    app.overlay.close_overlay()
    check(
        "关得掉（没有被吊在「正在关闭」上）",
        wait_for(lambda: not app.overlay.is_open(), timeout=2.0),
        f"overlay.is_open()={app.overlay.is_open()}",
    )
    check(
        "关掉后宠物回来（重开定格层不覆盖「原来在不在」，指令 17）",
        app.pet_window.isVisible() and not app._overlay_was_open,
        f"宠物可见={app.pet_window.isVisible()} _overlay_was_open={app._overlay_was_open}",
    )

    section("标题栏 ✕ 关面板：让位的宠物照样得回来")
    card = app.card_service.active_card_of_theme("出行")
    app.open_panel("life")
    app.open_card(card.id, app.panel.rect())
    pump(0.4)
    check(
        "又翻开一张卡，宠物再次让位",
        app.overlay.is_open() and app._pet_yielded and not app.pet_window.isVisible(),
        f"定格层={app.overlay.is_open()} _pet_yielded={app._pet_yielded}",
    )
    # 这回关的不是卡片自己的 ✕，是窗口标题栏那个 ✕——以前走这条路定格层没人收，
    # 「宠物让位」就没解除，桌宠从此消失
    app.panel.close()
    pump(0.4)
    check("面板关掉后定格层收干净了", not app.overlay.is_open(), f"定格层={app.overlay.is_open()}")
    check(
        "宠物回来了（不会一直藏着）",
        app.pet_window.isVisible() and not app._pet_yielded and not app._overlay_was_open,
        f"宠物可见={app.pet_window.isVisible()} _pet_yielded={app._pet_yielded}",
    )

    drain_threads()
    box.cleanup()


def _test_urgent_bypass() -> None:
    """特别紧急（指令 26-B 修订）：不管 12 小时静默、也不问一句，直接把卡翻到眼前。"""
    box, stub, app = make_env()
    flow = Flow(app, stub)
    trip = app.card_service.active_card_of_theme("出行")

    section("本地急词命中 → 直接弹，不问一句")
    flow.say("来不及了，见客户快迟到，怎么办", module="出行", topic="通勤", emotion=0.7)
    check(
        "没问一句、直接把出行卡弹出来",
        flow.offers == []
        and wait_for(lambda: bool(flow.cards))
        and flow.cards[0][0] == trip.id,
        f"offers={flow.offers} cards={flow.cards[:1]}",
    )

    section("拒绝过也不管：静默期里照样直接弹")
    diet = app.card_service.active_card_of_theme("餐饮")
    flow2 = Flow(app, stub)
    flow2.say("怎么办，外卖太油了吃不下", module="餐饮", topic="外卖", emotion=0.6)
    check(
        "不急的饮食话题照旧先问一句",
        flow2.offers == [LIFE] and flow2.cards == [],
        f"offers={flow2.offers} cards={flow2.cards}",
    )
    app.chatter_service.handle("不用了")
    check(
        "拒绝后记下静默",
        app.service_consent_dao.state(app.user.id, "餐饮")[1] is not None,
        str(app.service_consent_dao.state(app.user.id, "餐饮")),
    )
    flow2.offers.clear()
    flow2.cards.clear()
    flow2.say("来不及了，点的外卖太油吃不下，怎么办", module="餐饮", topic="外卖第二回", emotion=0.8)
    check(
        "静默期内、又是急事 → 照样直接弹，也不问",
        flow2.offers == []
        and wait_for(lambda: bool(flow2.cards))
        and flow2.cards[0][0] == diet.id,
        f"offers={flow2.offers} cards={flow2.cards[:1]}",
    )

    drain_threads()

    section("模型兜底说「特别急」→ 也直接弹")
    stub.intent_result = {"kind": "life", "theme": "出行", "confidence": 0.9, "urgent": True}
    flow3 = Flow(app, stub)
    flow3.say("怎么办，我快撑不住了", module="其他", topic="说不清的事", emotion=0.6)
    check(
        "本地认不出、模型说急 → 直接弹出行卡（不问）",
        flow3.offers == []
        and wait_for(lambda: bool(flow3.cards))
        and flow3.cards[0][0] == trip.id,
        f"offers={flow3.offers} cards={flow3.cards[:1]}",
    )

    section("对照：模型说「不急」→ 照旧先问一句")
    stub.intent_result = {"kind": "life", "theme": "出行", "confidence": 0.9, "urgent": False}
    flow4 = Flow(app, stub)
    flow4.say("怎么办，我快撑不住了", module="其他", topic="又是说不清的事", emotion=0.6)
    check(
        "先问一句，没直接弹",
        flow4.offers == [LIFE] and flow4.cards == [],
        f"offers={flow4.offers} cards={flow4.cards}",
    )

    drain_threads()
    box.cleanup()


def _test_timeword_vs_life() -> None:
    """带时间词的生活句不该被"明天大概几点"吞掉（时间词 vs 品类词）。

    原先解析出模糊时间词就整句进等待态、只反问一句"大概几点"，等回话说完要动手时
    又被"不是 IDLE 就别插话"那道守卫挡住——生活里的事一句都轮不上。
    """
    box, stub, app = make_env()
    app.scheduler_engine.start()      # 建提醒要往调度器里登记
    # 先给穿搭摆一张卡：这句落在穿搭上，手上有卡才走 26-B "先问一句"那条；
    # 手上没卡的话走的是邀请制，点头会弹确认框（模态，离屏下一等就等死）
    app.card_service.create_manual("穿搭", "穿得顺手", "👔", "")
    flow = Flow(app, stub)

    from life.chatter import ConversationState

    section("模糊时间词 + 生活品类词 → 走生活那条线，不反问「几点」")
    stub.parse_result = dict(
        _parse("穿搭", "穿搭", 0.6), time_hint="明天", time_confidence="fuzzy"
    )
    app.chatter_service.handle("明天见客户没衣服穿愁死")
    # 回话与主动服务都在后台线程上，跑满整轮时要等它落地，不能只 pump 固定时长
    check(
        "落在穿搭（不是句首那个「见客户」）→ 手上有这张卡，先问一句「要不要翻出来看看」",
        wait_for(lambda: bool(flow.offers), timeout=10)
        and any("穿得顺手" in text for text in flow.spoken),
        f"offers={flow.offers} 说过={flow.spoken}",
    )
    check(
        "进的是「等答复」的状态，不是「等用户补答时间」",
        app.chatter_service.state == ConversationState.AWAITING_SERVICE,
        app.chatter_service.state.value,
    )
    check("没问「几点」", not any("几点" in text for text in flow.spoken), str(flow.spoken))
    check(
        "也没顺手建一条提醒",
        app.reminder_dao.list_all(limit=100) == [],
        str([row.content for row in app.reminder_dao.list_all(limit=100)]),
    )

    section("显式提醒词照旧走提醒：还是问「几点」")
    app.chatter_service.handle("好呀")        # 把上一句的提议结掉，回到空闲
    wait_for(lambda: app.chatter_service.state == ConversationState.IDLE, timeout=10)
    flow.spoken.clear()
    stub.parse_result = dict(_parse("其他", "", 0.2), time_hint="明天", time_confidence="fuzzy")
    app.chatter_service.handle("提醒我明天见客户")
    check(
        "进了「等用户补答时间」的状态",
        wait_for(
            lambda: app.chatter_service.state == ConversationState.AWAITING_TIME, timeout=10
        ),
        app.chatter_service.state.value,
    )
    check("反问的是「几点」", any("几点" in text for text in flow.spoken), str(flow.spoken))

    section("工作那条线不参与这条比较：该建的提醒照建")
    app.chatter_service.handle("算了")        # 退出等待态
    wait_for(lambda: app.chatter_service.state == ConversationState.IDLE, timeout=10)
    before = len(app.reminder_dao.list_all(limit=100))
    stub.parse_result = dict(
        _parse("其他", "", 0.2),
        time_hint="明天",
        time_iso=(datetime.now() + timedelta(hours=20)).strftime("%Y-%m-%d %H:%M"),
        time_confidence="explicit",
    )
    app.chatter_service.handle("明天9点要交方案")
    check(
        "工作句照旧建了提醒（没被生活那条线抢走）",
        wait_for(
            lambda: len(app.reminder_dao.list_all(limit=100)) == before + 1, timeout=10
        ),
        str([row.content for row in app.reminder_dao.list_all(limit=100)]),
    )

    drain_threads()
    box.cleanup()


def main() -> int:
    _test_local()
    _test_life_card()
    _test_progressive_consent()
    _test_urgent_bypass()
    _test_life_invite()
    _test_card_made_focus()
    _test_work()
    _test_work_fuzzy_time()
    _test_work_beats_travel()
    _test_stale_overlay()
    _test_back_to_back_cards()
    _test_emotion_and_chitchat()
    _test_limits()
    _test_timeword_vs_life()
    return summary("主动服务的意图与限频")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
