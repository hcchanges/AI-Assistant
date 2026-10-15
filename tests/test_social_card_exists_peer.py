# -*- coding: utf-8 -*-
"""按对象名问卡（v2-social-card-exists-peer）的自动测试。

**病根**：`_guide_card_exists` 原来是**无参**的、判据只按大类 + 卡名「对象攻略卡」查
（peer 盲）。autodistill 那条线问「**这个人**有没有卡」时，这一层拿到的对象名被 lambda
丢掉（`card_exists=lambda peer: self._guide_card_exists()`），于是两面都错：

- **有别人卡时误放**：库里有一张「对象攻略卡」（挂给别人的），这个没卡的人也被当成有卡，
  材料被抽进别人的卡；
- **没卡时全挡死**：这个人自己明明有卡（挂了名、标题还是通用的「对象攻略卡」），只要老
  口径没对上就漏判、这场就挡死。

本批把 `_guide_card_exists(peer)` 改成**按对象名优先**：`find_card_by_peer(peer)` 先查，
查不到才退回「老卡 / 没挂名的卡」（无 `peer_name`、只有标题那张通用卡）。红线逐条钉住。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen.social import (
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)

JIA, YI = "茗风", "苓风"

RAW_JIA = "周末又去爬了趟山，晚上窝着刷短剧"
JIA_RESULT = {
    "age": "三十往上",
    "emotion_state": "单身",
    "anxiety": "家里在催婚",
    "interests": "爬山、短剧",
    "likes": "爱听人夸他细心",
    "dislikes": "别提前任",
    "strategy": "先接住抱怨，再顺一句夸他",
    "relation_stage": "破冰",
    "intro": "单身，周末爱爬山刷短剧",
    "events": ["约好周末一起去爬山"],
    "fragments": [{"text": "周末常爬山、晚上刷短剧", "tags": ["兴趣", "爬山"]}],
}


def _archive_guides(app) -> None:
    """把轮播上所有「对象攻略卡」收进卡册（腾干净，好从头摆）。"""
    for card, _pos in app.card_service.plaza():
        if card.theme == CARD_CATEGORY:
            app.card_service.archive_card(card.id)


def _guide_card(app, peer_name=None):
    return app.card_service.create_manual_card(
        CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
        peer_name=peer_name,
    )


def _settle(app, distiller) -> None:
    """等后台那一趟跑完、回调回主线程（写卡那一声也在这儿发）。"""
    wait_for(lambda: not distiller._running, timeout=5.0)
    drain_threads()
    pump(0.05)


def main() -> int:
    box, stub, app = make_env()
    try:
        section("① 按对象名问：这个人自己的卡才算有")

        check("前置：一张攻略卡都没有",
              app._guide_card_exists(JIA) is False and app._guide_card_exists(YI) is False, "")

        _guide_card(app, peer_name=JIA)
        check("★ 这个人自己的卡 → 算有", app._guide_card_exists(JIA) is True, "")
        check("★ 别人有卡、这个人没卡 → 不算有（不误放）",
              app._guide_card_exists(YI) is False, "")

        _archive_guides(app)
        _guide_card(app)  # 老卡 / 没挂名（无 peer_name，仅标题）
        check("★ 老卡 / 没挂名的卡 → 兜底命中仍算有",
              app._guide_card_exists(YI) is True and app._guide_card_exists(JIA) is True, "")

        check("★ 没给对象名（建卡查重那条 offer_card）→ 退回老口径：有这张卡就算有",
              app._guide_card_exists() is True, "")

        section("② 装配接线：autodistill 问的是「按对象名」，不是老的那层 peer 盲")

        _archive_guides(app)
        _guide_card(app, peer_name=JIA)
        check("★ 接线真把对象名递进去了：JIA 有卡、YI 没卡，两个问出**不同**答案",
              app.peer_distiller._card_exists(JIA) is True
              and app.peer_distiller._card_exists(YI) is False,
              f"JIA={app.peer_distiller._card_exists(JIA)} YI={app.peer_distiller._card_exists(YI)}")

        section("③ 别人有卡、这个人没卡 → 不误抽、不误写")

        jia_card = _guide_card(app, peer_name=JIA)  # 复用上面那张（同类同名，交回既有卡）
        app.peer_distiller.discard_pending()
        key_yi = "S1|" + YI
        app.peer_distiller.note_turn(key_yi, YI, "今天好累啊")
        check("★ 没卡时不抽（回 False，一个模型调用都不发）",
              app.peer_distiller.distill_now(key_yi) is False, "")
        check("★ 材料留着（不是白攒，他哪天开卡下一拍就补上）",
              app.peer_distiller.pending_for(key_yi) == 1,
              str(app.peer_distiller.pending_for(key_yi)))
        live_jia = app.card_service.card(jia_card.id)
        check("★ 别人的卡一个字没被写（不误写）",
              (live_jia.interests or "") == "" and (live_jia.strategy or "") == "",
              f"interests={live_jia.interests!r} strategy={live_jia.strategy!r}")

        section("④ 这个人自己有卡 → 抽进这张卡（不是被挡、也不是写进别人的）")

        app.peer_distiller.discard_pending()
        stub.peer_result = dict(JIA_RESULT)
        key_jia = "S2|" + JIA
        app.peer_distiller.note_turn(key_jia, JIA, RAW_JIA)
        check("★ 有卡 → 这一抽真派出去", app.peer_distiller.distill_now(key_jia) is True, "")
        _settle(app, app.peer_distiller)
        live_jia = app.card_service.card(jia_card.id)
        check("★ 抽进的是**这一张**卡（interests 含短剧类）",
              (live_jia.interests or "") == "爬山、短剧", repr(live_jia.interests))

        return summary("按对象名问卡（v2-social-card-exists-peer）")
    finally:
        drain_threads()
        box.cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
