"""
指令 25-B 的自动测试：品类判定（防串卡）+ 邀请制出卡 + 限频硬规则 + 自动归档。

离屏、桩模型、不联网：
- 品类判定第一层是本地词表（config.card.categories），测它不需要模型；
- 第二层（词表没命中时请模型判类）用桩模型控制"回哪个品类、置信度多少"；
- 自动归档用"把库里的时间改到 20 天前"来模拟，不去等 14 天。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from sqlalchemy import select

from harness import check, drain_threads, ensure_app, make_env, pump, section, summary, wait_for

from brain.llm.factory import LLMFactory
from data.models import Card, CardCheckin, CardInvite
from life.cards.spawnable import is_safe_text
from life.cards.templates import ALL_FEATURES, METAPHYSICS_CATEGORY, category_icon, family_of
from life.psych.persona_loader import (
    DEFAULT_CHAT_PERSONA,
    METAPHYSICS_PERSONA,
    NUTRITION_CHEF_PERSONA,
    ROLE_GUIDE,
    ROLE_NUTRITION,
    TOUR_GUIDE_PERSONA,
    can_spawn_card,
    load_persona,
)
from life.profile.advice import diet_advice, travel_advice, work_advice
from life.profile.extract import extract_diet_profile, extract_travel_profile, extract_work_profile
from life.profile.store import DIET, TRAVEL, WORK, ProfileStore
from life.profile.tool_registry import CATEGORY_DIET, CATEGORY_TRAVEL, match_tools, tool_suggest
from pet.persona import strip_action_marks
from PySide6.QtWidgets import QLineEdit, QListWidget, QTextEdit, QWidget

from ui.bubble import SpeechBubble
from ui.card_dialogs import CardOfferDialog
from ui.panels.card_panel import ADVICE_WAITING


class _BrokenProvider:
    """一个怎么调都报错的模型：用来测"模型这条挂了"的退路。"""

    def chat(self, *args, **kwargs):
        raise RuntimeError("断网了")

    chat_json = chat
    chat_messages = chat
    chat_with_image = chat


def _say(app, text: str, emotion: float = 0.4) -> None:
    """模拟一句吐嘈走到卡牌服务：跟 utterance_analyzed 信号传的那几样一致。"""
    app.card_service.observe(text, "其他", text[:6], emotion, DEFAULT_CHAT_PERSONA)


def _metaphysics_words(app) -> tuple[str, ...]:
    """配置里「命理」那一类的关键词（可卡牌化过滤对它放宽一档时会用到）。"""
    return tuple(app.config.card.categories.get(METAPHYSICS_CATEGORY, ()))


def _window() -> datetime:
    return datetime.now() - timedelta(days=7)


def _test_classify() -> None:
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("品类判定：本地词表，绝不串卡")
    for text in ("地铁挤爆了", "公交等半天不来", "打车排队排好久"):
        _say(app, text)
    check(
        "三条出行吐槽都落到出行类（手上已有出行卡，不出邀请）",
        len(app.card_event_dao.since("出行", _window())) == 3 and proposed == [],
        f"出行 {len(app.card_event_dao.since('出行', _window()))} 条 / 邀请 {proposed}",
    )
    check(
        "一条都没串到穿搭 / 餐饮",
        not app.card_event_dao.since("穿搭", _window())
        and not app.card_event_dao.since("餐饮", _window()),
        str([(row.theme, row.text) for row in app.card_event_dao.since("出行", _window())]),
    )

    section("品类平手：落在「缺什么」的那一类（场合不算主诉求）")
    match = app.card_service._classifier.local_match("明天见客户没衣服穿愁死")
    check(
        "出行（见客户）与穿搭（衣服）各命中一次 → 取穿搭：这句的诉求是没衣服穿",
        match is not None and match.category == "穿搭" and match.missing,
        str(match),
    )
    check(
        "同一句话换个说法（词表连不上「见客户」）→ 还是穿搭",
        app.card_service._classifier.local_match(
            "明天要去见一个很重要的客户，但我没衣服穿，愁死了"
        ).category
        == "穿搭",
        "穿搭",
    )
    check(
        "平手、也都不在「缺什么」里 → 照旧取靠前的那个（先说的是主诉求）",
        app.card_service._classifier.local_match("加班到11点只能吃泡面").category == "作息",
        str(app.card_service._classifier.local_match("加班到11点只能吃泡面")),
    )

    section("品类判定：一句话只计一个主要类别")
    app.card_event_dao.prune(datetime.now())      # 清空前面的记录，单独看这一句
    _say(app, "加班到11点只能吃泡面", emotion=0.3)
    check(
        "「加班到11点只能吃泡面」只记到作息 1 次，餐饮 0 次",
        len(app.card_event_dao.since("作息", _window())) == 1
        and app.card_event_dao.since("餐饮", _window()) == [],
        f"作息 {len(app.card_event_dao.since('作息', _window()))} 条 / "
        f"餐饮 {len(app.card_event_dao.since('餐饮', _window()))} 条",
    )

    section("品类判定：词表外生词走模型兜底，低置信度不出邀请")
    classifier = app.card_service._classifier
    check("生词本地词表认不出来", classifier.local_match("方案又被leader打回来了") is None, "None")
    check("情绪强 → 值得请模型判一次", classifier.should_ask_llm(0.8, 1), "0.8")
    check("既不强也不高频 → 不打扰模型", not classifier.should_ask_llm(0.2, 1), "0.2 / 1 条")
    check("攒够 3 条没归上类的 → 也请模型判", classifier.should_ask_llm(0.2, 3), "3 条")

    stub.classify_result = {"category": "作息", "confidence": 0.4}
    check(
        "判类链原样带回品类与置信度",
        app.card_service._chain.classify_category("随便说说") == ("作息", 0.4),
        "作息 / 0.4",
    )
    stub.classify_result = {"category": "别的东西", "confidence": 0.9}
    check(
        "品类不在 8 大类里 → 当成认不出",
        app.card_service._chain.classify_category("随便说说") == ("", 0.0),
        "空串",
    )

    app.card_event_dao.prune(datetime.now())
    proposed.clear()
    stub.classify_result = {"category": "作息", "confidence": 0.4}
    _say(app, "方案又被leader打回来了", emotion=0.8)
    pump(0.15)                                   # 等后台判类回来
    check(
        "低置信度 → 不改归类、不出邀请",
        not proposed and len(app.card_event_dao.since("未分类", _window())) == 1,
        f"邀请 {proposed} / 未分类 {len(app.card_event_dao.since('未分类', _window()))} 条",
    )

    stub.classify_result = {"category": "娱乐", "confidence": 0.9}
    _say(app, "那个破剧更新了但我没空看", emotion=0.8)
    check(
        "高置信度 → 改归到判出来的品类（不是另记一条）",
        wait_for(lambda: len(app.card_event_dao.since("娱乐", _window())) == 1)
        and len(app.card_event_dao.since("未分类", _window())) == 1,
        f"娱乐 {len(app.card_event_dao.since('娱乐', _window()))} 条 / "
        f"未分类 {len(app.card_event_dao.since('未分类', _window()))} 条",
    )

    drain_threads()
    box.cleanup()


def _test_accept() -> None:
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("邀请制出卡：强情绪单条直触，[好呀] 改卡名成卡")
    _say(app, "今天又懒得跑步", emotion=0.85)
    check("强情绪单条直接出邀请", proposed == ["健身"], str(proposed))

    dialog = CardOfferDialog("健身")
    check(
        "确认框默认卡名就是品类名、图标取该类的",
        dialog.values() == ("健身", category_icon("健身")),
        str(dialog.values()),
    )
    dialog._title.setText("夜跑")
    check("卡名当场改得动（改成「夜跑」）", dialog.values()[0] == "夜跑", dialog.values()[0])

    made = app.card_service.accept_invite("健身", "夜跑", "🏃")
    check("受理后建了卡，卡名用的是改过的那个", made == "夜跑", made)
    mine = [card for card, _count in app.card_service.plaza() if card.title == "夜跑"]
    check(
        "自建卡带着品类与图标，并进了轮播",
        bool(mine) and mine[0].theme == "健身" and mine[0].icon == "🏃",
        str([(card.title, card.theme, card.icon) for card in mine]),
    )
    check(
        "邀请记录标成「受理」",
        app.card_invite_dao.latest(app.user.id, "健身").accepted is True,
        "True",
    )

    section("限频只落在自己那个品类上：刚邀过健身，挡的是健身，不是消费")
    proposed.clear()
    _say(app, "又超支了", emotion=0.9)              # 消费：情绪很强、手上也没卡，本该触发
    check("换个品类照常邀（不分品类的那条全局限频已经取消）", proposed == ["消费"], str(proposed))
    proposed.clear()
    _say(app, "又超支了", emotion=0.9)
    check("消费自己 3 天内不再邀", proposed == [], str(proposed))

    drain_threads()
    box.cleanup()


def _test_has_card_no_invite() -> None:
    """手上已经有这个主题的卡 → 不再邀「自建一张」（指令 25-B 修订）。

    判据复用 26-B 的 active_card_of_theme（只看轮播上摆着的），不另写一套。
    """
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("手上已经有这个主题的卡 → 跳过邀请，观察照记")
    for theme, text in (("出行", "今天地铁又停运了"), ("餐饮", "外卖又油又咸")):
        check(
            f"{theme} 的卡摆在轮播上",
            app.card_service.active_card_of_theme(theme) is not None,
            theme,
        )
        _say(app, text, emotion=0.9)      # 强情绪单条本来够格触发邀请
    check("这两个主题都不再出邀请", proposed == [], str(proposed))
    check(
        "观察照记（品类的历史不能少一笔）",
        len(app.card_event_dao.since("出行", _window())) == 1
        and len(app.card_event_dao.since("餐饮", _window())) == 1,
        f"出行 {len(app.card_event_dao.since('出行', _window()))} 条 / "
        f"餐饮 {len(app.card_event_dao.since('餐饮', _window()))} 条",
    )

    section("手上没有卡的主题照常邀请；这一场主动服务动过手时不出邀请")
    proposed.clear()
    app.card_service.observe("今天又懒得跑步", "其他", "跑步", 0.9, DEFAULT_CHAT_PERSONA, False)
    check("带 may_invite=False：不邀请", proposed == [], str(proposed))
    check(
        "也照记了一条观察",
        len(app.card_event_dao.since("健身", _window())) == 1,
        f"健身 {len(app.card_event_dao.since('健身', _window()))} 条",
    )
    proposed.clear()
    app.card_service.observe("又是跑步跑到腿软", "其他", "跑步", 0.9, DEFAULT_CHAT_PERSONA)
    check(
        "健身没有卡、也不带那个参数 → 照常邀请（挡住上一次的就是它）",
        proposed == ["健身"],
        str(proposed),
    )

    drain_threads()
    box.cleanup()


def _test_decline() -> None:
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("限频：拒绝过的品类 7 天内不再提")
    _say(app, "今天又懒得跑步", emotion=0.9)
    check("健身攒到强情绪 → 邀请", proposed == ["健身"], str(proposed))

    app.card_service.resolve_invite("健身", False)
    proposed.clear()
    _say(app, "跑步机上跑了十分钟就累得不行", emotion=0.9)
    check("拒绝后 7 天内不再提", proposed == [], str(proposed))

    # 把这条邀请挪到 8 天前，模拟"静默期过了"
    old = datetime.now() - timedelta(days=8)
    with app.db.session_factory() as session:
        for row in session.scalars(select(CardInvite)):
            row.created_at = old
        session.commit()
    proposed.clear()
    _say(app, "又是跑步跑到腿软", emotion=0.9)
    check("过了静默期可以再提一次", proposed == ["健身"], str(proposed))

    drain_threads()
    box.cleanup()


def _test_full_then_archive() -> None:
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("限频：活跃个人卡满 4 张时不邀请")
    for index in range(4):
        app.card_service.create_manual("作息", f"卡{index}", "🌙", "")
    check(
        "先摆满 4 张",
        app.card_service.active_personal_count() == 4,
        str(app.card_service.active_personal_count()),
    )
    _say(app, "又超支了", emotion=0.9)              # 消费类还没有卡，拦住它的只能是"满 4 张"
    check("满 4 张 → 不出邀请", proposed == [], str(proposed))

    drain_threads()
    box.cleanup()


def _test_auto_archive() -> None:
    box, stub, app = make_env()

    section("自动归档：14 天没打卡/交互 → 收进卡册（数据保留，可拉回）")
    app.card_service.create_manual("健身", "夜跑", "🏃", "隔天跑三公里")
    card = next(card for card, _count in app.card_service.plaza() if card.title == "夜跑")

    # 把这张卡的时间整体挪到 20 天前：建卡、最近活跃、那次打卡都在那时候
    old = datetime.now() - timedelta(days=20)
    with app.db.session_factory() as session:
        session.add(CardCheckin(card_id=card.id, created_at=old))
        row = session.get(Card, card.id)
        row.created_at = old
        row.last_active_at = old
        session.commit()

    spoken: list[str] = []
    # 自动归档那一句是宠物自己开的口（清洁工式主动服务）→ 主动线（v2-speech-polish）
    app.bus.pet_speak_proactive.connect(spoken.append)
    moved = app.card_service.auto_archive_stale()

    check("20 天没动的个人卡被收进卡册", moved == ["夜跑"], str(moved))
    check(
        "轮播上不再有它",
        all(item.title != "夜跑" for item, _count in app.card_service.plaza()),
        str([item.title for item, _count in app.card_service.plaza()]),
    )
    book = {item.title: count for item, count in app.card_service.card_book()}
    check("卡册里查得到，历史打卡也还在", book.get("夜跑") == 1, str(book))
    check("宠物提了一句", any("夜跑" in text for text in spoken), str(spoken[-1:] if spoken else []))

    check("可以随时拉回轮播", app.card_service.restore_card(card.id) is True, "拉回")
    app.card_service.archive_card(card.id)          # 再收起来，测重启

    drain_threads()
    from harness import build_app

    app2 = build_app()          # 走 build_app 才会登记到沙箱上，cleanup() 才关得掉
    book2 = {item.title for item, _count in app2.card_service.card_book()}
    check("重启后卡册里的卡还在", "夜跑" in book2, str(book2))

    box.cleanup()


def _test_metaphysics() -> None:
    """命理那一类（第 9 个生活大类）：本人开口问命理，够强就邀卡；卡上绑玄机先生。"""
    box, stub, app = make_env()
    proposed: list[str] = []
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))

    section("命理句归到「命理」这一类（本地词表，不靠模型）")
    match = app.card_service._classifier.local_match("最近诸事不顺，算算我是不是犯太岁克我")
    check(
        "本地词表认得出来",
        match is not None and match.category == "命理",
        str(match),
    )
    check(
        "命理是生活卡那一族（本体有权提；玄学族已并进来，不再另起一条通道）",
        family_of("命理") == "life",
        family_of("命理"),
    )

    section("强情绪单条直触发 → 邀命理卡（不是当心理危机拦下）")
    _say(app, "最近诸事不顺，工作黄了，算算我是不是犯太岁克我", emotion=0.85)
    check("出了命理邀请", proposed == ["命理"], str(proposed))

    section("不强就只观察：弱情绪的命理句首条不触发")
    app.card_event_dao.prune(datetime.now())
    proposed.clear()
    _say(app, "算算我明天运势咋样", emotion=0.2)
    check(
        "首条、又不强 → 仅观察，不邀请",
        proposed == [] and len(app.card_event_dao.since("命理", _window())) == 1,
        f"邀请 {proposed} / 观察 {len(app.card_event_dao.since('命理', _window()))} 条",
    )
    check(
        "但这类句子的心理词不误伤它（「诸事不顺」这种状态下也照样能攒卡）",
        is_safe_text(("最近很绝望，诸事不顺，帮我算算运势",), _metaphysics_words(app)),
        "算生活事",
    )
    check(
        "不带命理词的心理句照样拦（红线没松）",
        not is_safe_text(("最近很绝望，什么都提不起劲",), _metaphysics_words(app)),
        "拦下",
    )
    check(
        "命理句里出现真危机词 → 照样拦（真危机最高优先）",
        not is_safe_text(("不想活了，帮我算算运势",), _metaphysics_words(app)),
        "拦下",
    )

    section("点[好呀] → 命理卡进轮播，并绑上玄机先生")
    made = app.card_service.accept_invite("命理", "命理", "🔮")
    check("建出来了", made == "命理", made)
    mine = [card for card, _count in app.card_service.plaza() if card.title == "命理"]
    check(
        "卡带着命理主题进了轮播",
        bool(mine) and mine[0].theme == "命理",
        str([(card.title, card.theme) for card in mine]),
    )
    check(
        "陪执行的角色是玄机先生（不是宠物本体）",
        bool(mine) and mine[0].persona_id == METAPHYSICS_PERSONA,
        mine[0].persona_id if mine else "无",
    )
    check(
        "卡上说话的是玄机先生",
        bool(mine) and app.card_service.speaker_name(mine[0]) == "玄机先生",
        app.card_service.speaker_name(mine[0]) if mine else "无",
    )

    drain_threads()
    box.cleanup()


def _test_profile_and_tools() -> None:
    """指令 26-C C1/C2：会话画像 + 建议 + 工具池（纯内存，不落库）。"""
    box, stub, app = make_env()

    section("会话画像存储：只进内存、空值不覆盖、get 返回副本")
    store = ProfileStore()
    store.update(WORK, {"dept": "研发部", "task": "bom", "deadline": "月底"})
    store.update(TRAVEL, {"destination": "上海", "frequency": "经常"})
    store.update(DIET, {"last_meal": "红烧肉米饭", "goal": "减脂"})
    check(
        "工作画像取得到",
        store.get(WORK) == {"dept": "研发部", "task": "bom", "deadline": "月底"},
        str(store.get(WORK)),
    )
    check("出行画像取得到", store.get(TRAVEL)["destination"] == "上海", str(store.get(TRAVEL)))
    check(
        "饮食画像取得到",
        store.get(DIET) == {"last_meal": "红烧肉米饭", "goal": "减脂"},
        str(store.get(DIET)),
    )
    store.update(WORK, {"dept": ""})
    check("空值不覆盖已有字段", store.get(WORK)["dept"] == "研发部", str(store.get(WORK)))
    copy = store.get(WORK)
    copy["dept"] = "市场部"
    check("get 返回副本，改副本不伤内部", store.get(WORK)["dept"] == "研发部", str(store.get(WORK)))

    section("画像抽取：只认原文、不编造")
    check(
        "工作句抽出部门/活儿/截止日",
        extract_work_profile("我们研发部在赶BOM，月底要交")
        == {"dept": "研发部", "task": "bom", "deadline": "月底"},
        str(extract_work_profile("我们研发部在赶BOM，月底要交")),
    )
    check(
        "出行句抽出目的地与频次",
        extract_travel_profile("我下周要去上海出差，经常跑那边")
        == {"destination": "上海", "frequency": "经常"},
        str(extract_travel_profile("我下周要去上海出差，经常跑那边")),
    )
    check("「又去广州」抽出目的地", extract_travel_profile("又去广州") == {"destination": "广州"}, str(extract_travel_profile("又去广州")))
    check("认不出就留空（不猜）", extract_travel_profile("今天天气不错") == {}, str(extract_travel_profile("今天天气不错")))
    check(
        "饮食句抽出上一顿",
        extract_diet_profile("中午吃了红烧肉米饭") == {"last_meal": "红烧肉米饭"},
        str(extract_diet_profile("中午吃了红烧肉米饭")),
    )
    check(
        "饮食句抽出健康目标",
        extract_diet_profile("我在减脂，冰箱有鸡胸和西兰花") == {"goal": "减脂"},
        str(extract_diet_profile("我在减脂，冰箱有鸡胸和西兰花")),
    )

    section("建议文案：只基于画像推理")
    check("出行建议带目的地", "上海" in travel_advice({"destination": "上海"}), travel_advice({"destination": "上海"}))
    check("工作建议带死线", "月底" in work_advice({"task": "bom", "deadline": "月底"}), work_advice({"task": "bom", "deadline": "月底"}))
    check(
        "饮食建议带护栏话术",
        diet_advice({"last_meal": "红烧肉米饭"}).endswith("专业医生。）"),
        diet_advice({"last_meal": "红烧肉米饭"}),
    )
    check(
        "饮食建议体现减脂目标",
        "减脂期" in diet_advice({"goal": "减脂"}),
        diet_advice({"goal": "减脂"}),
    )

    section("工具池：按品类匹配、结果只建议不行动")
    check(
        "出行工具 3 件（天气防晒/打包/排行程）",
        [t.id for t in match_tools(CATEGORY_TRAVEL)] == ["weather_sun", "packing", "itinerary"],
        str([t.id for t in match_tools(CATEGORY_TRAVEL)]),
    )
    check(
        "饮食工具 3 件（吃什么/热量/怎么做）",
        [t.id for t in match_tools(CATEGORY_DIET)] == ["what_to_eat", "calorie", "recipe"],
        str([t.id for t in match_tools(CATEGORY_DIET)]),
    )
    check(
        "工作工具 3 件（待办/纪要/工时）",
        [t.id for t in match_tools("工作")] == ["todo", "minutes", "timesheet"],
        str([t.id for t in match_tools("工作")]),
    )
    check("打包清单提到目的地", "上海" in tool_suggest("packing", {"destination": "上海"}), tool_suggest("packing", {"destination": "上海"}))
    check("热量只是估算（不诊断）", "估算" in tool_suggest("calorie", {}), tool_suggest("calorie", {}))
    check(
        "排行程按目的地排（导游那一侧）",
        "上海" in tool_suggest("itinerary", {"destination": "上海"})
        and "一天最多塞两处" in tool_suggest("itinerary", {"destination": "上海"}),
        tool_suggest("itinerary", {"destination": "上海"}),
    )
    check(
        "怎么做给的是火候做法（主厨那一侧）",
        "冒烟" in tool_suggest("recipe", {}) and "一分半" in tool_suggest("recipe", {}),
        tool_suggest("recipe", {}),
    )
    check(
        "做法也带专业护栏（饮食类工具一视同仁）",
        tool_suggest("recipe", {}).endswith("专业医生。）"),
        tool_suggest("recipe", {})[-20:],
    )

    section("装配接线：解析完一句 → 画像进 app.profile_store（会话内存）")
    app.bus.utterance_analyzed.emit("我们研发部在赶BOM，月底要交", "工作", "BOM", 0.5, "lin", True)
    check(
        "工作画像进了装配层",
        app.profile_store.get(WORK) == {"dept": "研发部", "task": "bom", "deadline": "月底"},
        str(app.profile_store.get(WORK)),
    )
    app.bus.utterance_analyzed.emit("我下周要去上海出差", "出行", "出差", 0.4, "lin", True)
    check(
        "出行画像进了装配层",
        app.profile_store.get(TRAVEL)["destination"] == "上海",
        str(app.profile_store.get(TRAVEL)),
    )
    app.bus.utterance_analyzed.emit("中午吃了红烧肉米饭", "餐饮", "吃饭", 0.4, "lin", True)
    check(
        "饮食画像进了装配层",
        app.profile_store.get(DIET) == {"last_meal": "红烧肉米饭"},
        str(app.profile_store.get(DIET)),
    )

    drain_threads()
    box.cleanup()


def _test_card_roles() -> None:
    """指令 27：出行卡请导游说、饮食卡请营养主厨说，建议进上半框。"""
    box, stub, app = make_env()
    panel = app.card_panel
    service = app.card_service
    tour = load_persona(TOUR_GUIDE_PERSONA)
    chef = load_persona(NUTRITION_CHEF_PERSONA)

    section("角色提示词模板：两个专业角色都装得齐")
    check("导游卡装得上", tour.name and tour.role_type == ROLE_GUIDE, f"{tour.name} / {tour.role_type}")
    check("营养主厨卡装得上", chef.name and chef.role_type == ROLE_NUTRITION, f"{chef.name} / {chef.role_type}")
    check(
        "导游卡写的是导游那套（行程/季节/不确定要说不确定）",
        "导游" in tour.system_prompt and "不编造" in tour.system_prompt,
        tour.system_prompt[:30],
    )
    check(
        "主厨卡一个人格带两副视角（营养 + 厨艺）",
        "营养师" in chef.system_prompt and "主厨" in chef.system_prompt,
        chef.system_prompt[:30],
    )
    check("两个角色都有 few-shot 示范", bool(tour.few_shot) and bool(chef.few_shot), f"{len(tour.few_shot)} / {len(chef.few_shot)}")
    check(
        "两个角色都不产卡（只有命理类能产）",
        not can_spawn_card(TOUR_GUIDE_PERSONA) and not can_spawn_card(NUTRITION_CHEF_PERSONA),
    )
    check(
        "都有医疗/安全边界那一段",
        bool(tour.safety) and bool(chef.safety) and "医生" in chef.safety,
        chef.safety[:30],
    )

    section("腔调隔离：专家说专家的，不串玄机/命理那一套")
    trip_system = service._system_for(service._card_dao.by_theme("出行"))
    check("system 里是「表层腔调 + 专业框架 + 安全策略」三段", "【是谁在说】" in trip_system and "【专业框架" in trip_system, trip_system[:30])
    check("专业框架压过脾气（顺序写死）", "优先级高于上面那段脾气" in trip_system, "")
    check(
        "专家的 system 里没有命理/玄机腔",
        not any(word in trip_system for word in ("玄机", "命理", "卦", "八字", "风水")),
        "",
    )

    section("主题 → 专业角色：默认谁在说、以及用户选的压过默认")
    trip = service._card_dao.by_theme("出行")
    diet = service._card_dao.by_theme("餐饮")
    check("出行卡默认请导游", service.persona_of(trip) == TOUR_GUIDE_PERSONA, service.persona_of(trip))
    check("饮食卡默认请营养主厨", service.persona_of(diet) == NUTRITION_CHEF_PERSONA, service.persona_of(diet))
    check(
        "说话人的名字也跟着变（出行卡不填 persona_id 也报导游）",
        service.speaker_name(trip) == tour.name,
        service.speaker_name(trip),
    )
    check("只有出行/餐饮这两类走专业角色", service.has_pro_advice(trip) and service.has_pro_advice(diet))
    other = service._card_dao.add(
        user_id=app.user.id, title="换季", theme="穿搭", summary="", source_memory_ids=[],
        features=list(ALL_FEATURES), goal="", persona_id=None, icon="👕",
    )
    check("别的生活卡没有专业角色", not service.has_pro_advice(other), other.theme)
    check("别的生活卡照旧是宠物本体说", service.persona_of(other) == "", service.persona_of(other))

    service.set_persona(other.id, TOUR_GUIDE_PERSONA)
    check(
        "用户显式选的压过主题默认",
        service.persona_of(service.card(other.id)) == TOUR_GUIDE_PERSONA,
        service.persona_of(service.card(other.id)),
    )
    service.set_persona(trip.id, "")
    check(
        "在出行卡上选回「宠物本体」就真的是本体（不被主题默认顶掉）",
        service.persona_of(service.card(trip.id)) == "",
        repr(service.persona_of(service.card(trip.id))),
    )
    check(
        "退回本体后名字也变回宠物名",
        service.speaker_name(service.card(trip.id)) == app.config.pet.name,
        service.speaker_name(service.card(trip.id)),
    )
    service.set_persona(trip.id, TOUR_GUIDE_PERSONA)

    section("上半框：专业角色现说一段（后台），一次开卡只请一次")
    stub.text = "（导游腔）这趟先落脚，再按天气定第二天的线。"
    requests: list[int] = []
    real_advise = service.advise_card

    def _spy(card_id: int, profile: dict[str, str]) -> None:
        requests.append(card_id)
        real_advise(card_id, profile)

    service.advise_card = _spy          # type: ignore[method-assign]
    panel.show_card(trip.id)
    pump(0.4)
    # 上半框是渲染出口，进框前统一剥动作描写（（导游腔）这种句首括号也算），
    # 所以比的是剥过之后的文本，不是模型原样。
    check(
        "出行卡上半框填的是角色说的那段（已剥动作描写）",
        panel._advice_pane.toPlainText() == strip_action_marks(stub.text),
        panel._advice_pane.toPlainText(),
    )
    check("提示词认得出是出行那一类", "主题：出行" in stub.prompts[-1], stub.prompts[-1][:40])
    before = len(requests)
    app.bus.card_changed.emit()         # 打卡/改目标也会走 refresh
    pump(0.3)
    check("card_changed 刷新不会重复请模型", len(requests) == before, f"{before} → {len(requests)}")

    section("模型这条挂了：退回规则版，框里不留「正在想…」")
    healthy = LLMFactory.create
    LLMFactory.create = staticmethod(lambda config, provider_name=None: _BrokenProvider())  # type: ignore[assignment]
    panel.show_card(trip.id)
    pump(0.4)
    check(
        "挂了就用规则版那段顶上",
        panel._advice_pane.toPlainText()
        == strip_action_marks(travel_advice(app.profile_store.get(TRAVEL))),
        panel._advice_pane.toPlainText()[:40],
    )
    check("不会一直停在「正在想…」", panel._advice_pane.toPlainText() != ADVICE_WAITING, panel._advice_pane.toPlainText()[:20])
    LLMFactory.create = healthy           # type: ignore[assignment]
    service.advise_card = real_advise     # type: ignore[method-assign]

    section("饮食卡：近期有出行时，主厨那段要多带一句外食怎么选")
    service._remember_trip("成都", datetime.now() + timedelta(days=10))
    pump(0.5)
    check("近期出行读得出来（走的是链 a 回写的那条带时间的记忆）", bool(service._recent_trip()), service._recent_trip()[:24])
    stub.text = "（主厨腔）今天这顿先抓主食的量和一份蛋白。"
    panel.show_card(diet.id)
    pump(0.4)
    check("出行那句提示确实进了提示词", "外食" in stub.prompts[-1], stub.prompts[-1][-60:])
    check("走的是餐饮那一支任务", "照着吃的建议" in stub.prompts[-1], stub.prompts[-1][:60])

    section("动作描写净化：进框的建议里不留 （…）动作 / *…* 星号")
    stub.text = (
        "（尾巴扫过手背）今天这顿先抓主食的量和一份蛋白（顺手理了理毛）。"
        "*歪了歪头* 晚上别再加宵夜。"
    )
    panel.show_card(diet.id)
    pump(0.4)
    shown = panel._advice_pane.toPlainText()
    check("框里没有圆括号动作了", "（" not in shown and "(" not in shown, shown)
    check("框里没有星号动作了", "*" not in shown, shown)
    check("剥完跟预期一字不差", shown == strip_action_marks(stub.text), shown)
    check("正经话一个字没少", "先抓主食的量和一份蛋白" in shown, shown)
    check("框没被剥空", bool(shown.strip()), shown)

    section("剥的只是舞台提示，说明性括号原样留着")
    for raw, want in (
        ("（尾巴扫过手背）今天先落脚", "今天先落脚"),
        ("先抓主食的量（尾巴扫过手背）。", "先抓主食的量。"),
        ("*歪了歪头*我看看", "我看看"),
        ("热线打 12356（以你当地为准）。", "热线打 12356（以你当地为准）。"),
        ("（身体不舒服就先停）", "（身体不舒服就先停）"),
        ("（研发这边，跨部门要盯紧接口人。）", "（研发这边，跨部门要盯紧接口人。）"),
    ):
        got = strip_action_marks(raw)
        check(f"{raw[:14]} → {want[:14]}", got == want, got)

    section("危机拦截：素材里有危险信号，就不交给模型")
    from life.psych import crisis
    app.profile_store.update(TRAVEL, {"destination": "成都", "preference": "最近总说活着没意思"})
    stub.text = "（不该出现）模型不该被叫到"
    before = len(stub.prompts)
    panel.show_card(trip.id)
    pump(0.4)
    # 危机话术开头那句「（放缓）」是舞台提示，进框时同样被剥掉，正文一个字不动。
    check(
        "上半框给的是危机话术",
        panel._advice_pane.toPlainText() == strip_action_marks(crisis.CRISIS_REPLY),
        panel._advice_pane.toPlainText()[:30],
    )
    check("这一趟没调模型", len(stub.prompts) == before, f"{before} → {len(stub.prompts)}")
    check("危机话术里有求助热线", "12356" in panel._advice_pane.toPlainText(), "")
    app.profile_store.update(TRAVEL, {"destination": "成都"})

    section("工作卡不受影响：仍是规则版 + 原来的三件工具")
    with service._card_dao._session_factory() as session:
        card = session.get(Card, diet.id)
        card.theme = "工作"
        session.commit()
    app.profile_store.update(WORK, {"task": "BOM", "dept": "研发"})
    panel.show_card(diet.id)
    pump(0.4)
    check(
        "工作主题的卡不请专业角色，用规则版",
        panel._advice_pane.toPlainText()
        == strip_action_marks(work_advice(app.profile_store.get(WORK))),
        panel._advice_pane.toPlainText()[:40],
    )
    check(
        "工具换成工作那三件",
        [panel._tool_layout.itemAt(i).widget().text() for i in range(panel._tool_layout.count())]
        == ["✅ 待办生成", "📝 会议纪要", "⏱ 工时统计"],
        str([panel._tool_layout.itemAt(i).widget().text() for i in range(panel._tool_layout.count())]),
    )

    drain_threads()
    box.cleanup()


def _test_card_upper_clean() -> None:
    """修复项 1：卡里没有对话通道——上半只有一块只读建议框，没有气泡、没有输入框。

    卡内对话（用户气泡 + 宠物回复 + 卡底输入框）在 A 方案里整个删掉了，
    这条守着它别再从哪条渲染路径长回来。
    """
    box, stub, app = make_env()
    panel = app.card_panel
    service = app.card_service

    spoken: list[str] = []
    # 两条都听：这一段的断言是"一个字都没说"，漏听一条就等于放它过去
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)

    section("卡内零对话通道：上半只有只读建议框")
    for theme in ("出行", "餐饮"):
        card = service._card_dao.by_theme(theme)
        service.set_persona(card.id, "")      # 交回宠物本体，别让专业角色掺进来
        spoken.clear()
        panel.show_card(card.id)
        pump(0.5)
        upper = panel._upper.findChildren(QWidget)
        check(f"{theme}卡上半没有对话气泡", not [w for w in upper if isinstance(w, SpeechBubble)],
              str([w for w in upper if isinstance(w, SpeechBubble)]))
        check(f"{theme}卡上半没有输入框", not [w for w in upper if isinstance(w, QLineEdit)],
              str([w.placeholderText() for w in upper if isinstance(w, QLineEdit)]))
        check(f"{theme}卡上半没有对话列表", not [w for w in upper if isinstance(w, QListWidget)], "")
        panes = [w for w in upper if isinstance(w, QTextEdit)]
        check(f"{theme}卡上半就一块只读建议框", len(panes) == 1 and panes[0].isReadOnly(),
              f"{len(panes)} 块")
        check(f"{theme}卡打开时宠物不弹气泡说话", spoken == [], str(spoken))
        # 卡顶那行「这张卡想做成什么？」以前是个输入框，里面还留着用户刚说的话，
        # 看着就像一条聊天气泡；A 方案里卡面不该有常驻输入框，这行已经整个去掉。
        stray = [w for w in panel.findChildren(QLineEdit) if w.parent() is panel]
        check(f"{theme}卡面没有常驻输入框", not stray, str([w.placeholderText() for w in stray]))

    drain_threads()
    box.cleanup()


def main() -> int:
    ensure_app()
    _test_classify()
    _test_accept()
    _test_has_card_no_invite()
    _test_decline()
    _test_full_then_archive()
    _test_auto_archive()
    _test_metaphysics()
    _test_profile_and_tools()
    _test_card_roles()
    _test_card_upper_clean()
    return summary("邀请制出卡与品类判定")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
