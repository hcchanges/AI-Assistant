"""
真调模型测"意图主动服务"（要联网、要花几分钱，加 --live 才跑）。

指令 26-B 的验收：
- 一句不急的出行话题（"地铁今天特别挤"）要先问一句"要不要翻出来看看"，点头才弹出出行卡、
  附一段能用的走法（交通方式 + 时间预估）；
- 一句真急的（"来不及了…"）不问、也不受 12 小时静默限制，直接把卡翻到眼前；
- "加班好累"这种纯情绪和闲聊一句话都不该多问；
- 带时间词的生活句（"明天见客户没衣服穿愁死"）该走到那张卡上，不该只换来一句"明天大概几点"。
桩模型只能证明"分流走对了"（那部分在 test_intent.py 里），"真模型认得出、说得出来"只有真调才知道。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, note, pump, section, skip, summary, wait_for

TRIP_TEXT = "怎么办，地铁今天特别挤"                 # 不带急词：该先问一句
URGENT_TEXT = "来不及了，饿得胃疼，点的外卖还没到"     # 带急词：该直接弹
TRIP_WORDS = ("打车", "地铁", "公交", "骑车", "走路", "开车", "网约车", "出租车", "步行")
TIME_WORDS = ("分钟", "小时", "刻")
CALM_TEXTS = ("加班好累", "今天天气真好")   # 没求助信号也没报时间：零触发
# 带时间词的生活句：时间词不该把生活那条线顶掉（原先只换来一句"明天大概几点"）
TIMED_TEXT = "明天见客户没衣服穿愁死"


def _collect(app):
    """把"弹了哪张卡 / 问了什么 / 说了什么"都收下来。"""
    spoken: list[str] = []
    cards: list[tuple[int, str]] = []
    replies: list[str] = []
    offers: list[str] = []
    proposed: list[str] = []
    works: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.service_show_card.connect(lambda card_id, text: cards.append((card_id, text)))
    app.bus.card_reply.connect(lambda _card_id, reply: replies.append(reply))
    app.bus.service_offer.connect(offers.append)
    app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))
    app.bus.service_open_work.connect(works.append)
    return spoken, cards, replies, offers, proposed, works


def main() -> int:
    box, _stub, app = make_env(with_stub=False)
    try:
        if not app.config.get_api_key():
            skip("真模型意图主动服务验收", "这台机器没配 API Key（设置页填一个再来）")
            return summary("意图主动服务（真模型）")

        spoken, cards, replies, offers, proposed, works = _collect(app)

        section("端到端：不急的出行话题 → 问一句 → 点头 → 弹出行卡")
        trip = app.card_service.active_card_of_theme("出行")
        app.chatter_service.handle(TRIP_TEXT)
        check(
            "真解析 + 真回话走完之后，先问一句「要不要翻出来看看」",
            wait_for(lambda: bool(offers), timeout=90) and offers == ["life"],
            f"offers={offers}",
        )
        check("要点头才弹（没直接弹出来）", cards == [], str(cards))
        app.chatter_service.handle("好呀")
        check(
            "点头 → 出行卡弹出来，并带上原话",
            wait_for(lambda: bool(cards), timeout=90) and cards[0] == (trip.id, TRIP_TEXT),
            f"{cards[:1]}",
        )
        check(
            "先安抚一句（林医生腔收尾）",
            any("别急" in text or "莫慌" in text for text in spoken),
            str(spoken[:3]),
        )
        check("同一场只动一次手：没再冒「要不要建张卡」", proposed == [], str(proposed))

        section("场景建议：能照着走（联网生成，不是模板）")
        if app.config.get_api_key(app.config.llm.provider(app.config.work.provider)):
            check("建议回到了卡上", wait_for(lambda: bool(replies), timeout=90), f"{len(replies)} 条")
            advice = replies[-1] if replies else ""
            note(f"建议：{advice}")
            check("给了一段建议，不是空话", len(advice) > 12, f"{len(advice)} 字")
            check("提到具体的交通方式", any(word in advice for word in TRIP_WORDS), advice[:80])
            check("给了一个大致的时间预估", any(word in advice for word in TIME_WORDS), advice[:80])
            check(
                "没有推脱（不是「我查不到」那种）",
                not any(word in advice for word in ("查不到", "无法", "抱歉", "不知道")),
                advice[:80],
            )
        else:
            skip("场景建议内容", "没配联网检索的 Key（work.provider 那一把）")

        section("真的急了：不问一句、也不管静默，直接弹")
        cards.clear()
        offers.clear()
        spoken.clear()
        diet = app.card_service.active_card_of_theme("餐饮")
        app.chatter_service.handle(URGENT_TEXT)
        check(
            "没问一句、直接把饮食卡弹出来",
            wait_for(lambda: bool(cards), timeout=90) and cards[0] == (diet.id, URGENT_TEXT),
            f"cards={cards[:1]} offers={offers}",
        )
        check("确实没走「要不要翻出来看看」那一步", offers == [], str(offers))

        section("带时间词的生活句：问的是那张卡，不是「明天大概几点」")
        # 这句话落在穿搭上（"没衣服穿"才是主诉求，"见客户"只是场合）：
        # 手上先摆一张，走的就是"先问一句"那条
        app.card_service.create_manual("穿搭", "穿得顺手", "👔", "")
        spoken.clear()
        cards.clear()
        offers.clear()
        proposed.clear()
        app.chatter_service.handle(TIMED_TEXT)
        # 主动服务排在回话之后：得等它真落地，"是不是没动手"才作数
        check(
            "走的是生活那条线：问一句「穿得顺手」要不要翻出来",
            wait_for(lambda: bool(offers), timeout=90)
            and any("穿得顺手" in t for t in spoken),
            f"offers={offers} cards={cards} proposed={proposed}",
        )
        pump(0.5)
        check(
            "没反问时间（「大概是几点」和「什么时候提醒你」都没出现）",
            not any("大概是几点" in t or "什么时候提醒你" in t for t in spoken),
            str(spoken[:3]),
        )

        section("纯情绪 / 闲聊：一句话都不多问")
        spoken.clear()
        cards.clear()
        offers.clear()
        works.clear()
        proposed.clear()
        for text in CALM_TEXTS:
            app.chatter_service.handle(text)
            pump(0.5)
        wait_for(lambda: bool(spoken), timeout=60)
        check(
            "没弹卡、没问工作",
            not cards and not offers and not works,
            f"cards={cards} offers={offers} works={works}",
        )
        note("它说的话：" + " | ".join(spoken[:2]))
        if proposed:
            note(f"（卡牌那条线自己出的建卡邀请：{proposed}——25-B 的阈值，不归 26-B 管）")

        section("意图兜底链（本地词表没认出来时才走的那条）")
        chain = app.chatter_service._serve.chain
        # 一句话里的事明显归某个大类，但用的都不是词表里的词——本地认不出，该模型出手
        kind, theme, confidence, urgent = chain.classify("怎么办，我早上老是起不来，闹钟响十遍还是睡过去了")
        note(f"生词急事 → {kind} / {theme} / {confidence} / urgent={urgent}")
        check("把握值在 0~1 之间", 0.0 <= confidence <= 1.0, str(confidence))
        check(
            "判成了生活意图，并给了一个合法大类",
            kind == "life" and theme in app.config.card.categories,
            f"{kind} / {theme}",
        )

        kind2, _theme2, confidence2, urgent2 = chain.classify("加班好累")
        note(f"纯情绪 → {kind2} / {confidence2} / urgent={urgent2}")
        check("纯情绪判成 none（陪着就好，不动手）", kind2 == "none", kind2)

        return summary("意图主动服务（真模型）")
    finally:
        box.cleanup()


if __name__ == "__main__":
    sys.exit(main())
