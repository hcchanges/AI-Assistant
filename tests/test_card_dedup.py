"""
建卡去重（v2-social-card-dedup）的自动测试。

真机那条：候选列完问一句"要不要建对象攻略卡"，点 [好呀] 就建一张；下一轮社交还是
同样的一问、同样的大类、同样的卡名，于是**同名的「对象攻略卡」攒了好几张**（一直攒到
个人卡位满）。两头各堵一道：

- **做卡那一层**（`CardService.find_card` / `create_manual_card`）：同类同名已经有一张了
  → 不再重复建，把**既有那张**交回去（调用方接着往它的专属库写东西）；
- **社交那一层**（`SocialReplyService.offer_card`）：已经有这张卡了，那一问**根本不排**
  ——判据由装配层接做卡那边的 `find_card`，社交这条线自己不存"建过没有"这份记忆。

五段：① 同类同名不再重复建（含"查重蒙上就又建一张"的反证）；② 判重是**两条都对上**、
且按落库口径截齐（不误伤"不同大类同名"）；③ 归档的那张不算、卡位满了也认得出现有那张；
④ 社交那一层不再排那一问（含"判据蒙上就又排出来"的反证）；⑤ 点 [好呀] 那条真路写进
既有卡、话也说得对。

红线：pet.db 零写入；零新依赖（只用既有 DAO 的查法）；不模拟按键；社交那条线一个
剪贴板接口都没碰（写剪贴板那只手在装配层，这一节连那条真路都不跑）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

import core.bootstrap as bootstrap_mod
from life.cards.service import is_personal_card
from life.cards.templates import CARD_TITLE_MAX
from life.screen import social as smod
from life.screen.social import (
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)


class _GuideCardRecorder:
    """把对象攻略卡的可改框换成记录员（离屏没人关 `exec()`，不换必卡死）。"""

    def __init__(self, profile: str, parent=None):
        self.profile = profile or ""
        self.parent = parent

    def exec(self) -> int:
        return 1

    def values(self) -> str:
        return self.profile.strip()

    def deleteLater(self) -> None:      # noqa: N802
        pass


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _source(obj) -> str:
    import inspect

    return inspect.getsource(obj)


def _emotion_cards(service) -> list:
    """轮播上的「情感」大类卡（对象攻略卡就在这一类里）。"""
    return [card for card, _pos in service.plaza() if card.theme == CARD_CATEGORY]


def _make_guide(service):
    return service.create_manual_card(
        CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL
    )


def main() -> int:
    box, stub, app = make_env()
    try:
        service = app.card_service
        social = app.social_reply
        pet_before = _rows(app.config.db_path, "utterance")
        offers: list[str] = []
        spoken: list[str] = []
        app.bus.social_card_offer.connect(lambda: offers.append("social_card"))
        app.bus.pet_speak.connect(spoken.append)

        # ---------- ① 同类同名不再重复建 ----------

        section("① 同类同名已经有一张 → 不再重复建（做卡那一层）")

        check("前置：轮播上还没有「对象攻略卡」",
              service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE) is None, "")
        first = _make_guide(service)
        check("第一张照常建出来",
              first is not None and first.title == GUIDE_CARD_TITLE,
              f"#{getattr(first, 'id', None)} {getattr(first, 'title', '')}")
        check("查重那只眼认得出来（find_card 找的就是它）",
              service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE) is not None
              and service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE).id == first.id, "")

        count_before = len(_emotion_cards(service))
        drain_threads()
        pump(0.1)
        mind_n = app.mind_store.fragment_count()
        second = _make_guide(service)
        drain_threads()
        pump(0.1)
        check("★ 同一对再建一次 → 不再新建，交回**既有那张**",
              second is not None and second.id == first.id,
              f"#{first.id} → #{getattr(second, 'id', None)}")
        check("★ 卡没多（真机那条'攒好几张'没了）",
              len(_emotion_cards(service)) == count_before,
              f"{count_before} → {len(_emotion_cards(service))}")
        check("交回来的是整张卡（kb_path 现成，调用方接着往它写）",
              bool(getattr(second, "kb_path", "")), str(getattr(second, "kb_path", "")))
        check("这一拍也没多写一条卡牌记忆（不建就不记「认领了一张卡」）",
              app.mind_store.fragment_count() == mind_n,
              f"{mind_n} → {app.mind_store.fragment_count()}")

        # 反证：把查重那只眼蒙上（= 修复前的样子）→ 同一对再建一次，真就又建一张
        real_find = service.find_card
        service.find_card = lambda *a, **k: None
        try:
            dup = _make_guide(service)
        finally:
            service.find_card = real_find
        count_dup = len(_emotion_cards(service))
        check("★ 反证：查重蒙上（修复前的样子）→ 同名同类真就又建了一张",
              dup is not None and dup.id != first.id and count_dup == count_before + 1,
              f"又建了 #{getattr(dup, 'id', None)}，这一类现在 {count_dup} 张")

        check("把那多出来的一张删掉，查重照旧认得出现有那张",
              service.delete_card(dup.id) is True
              and len(_emotion_cards(service)) == count_before
              and service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE).id == first.id,
              f"现在 {len(_emotion_cards(service))} 张")

        # ---------- ② 判重是"两条都对上"，且按落库口径截齐 ----------

        section("② 判重比的是**大类 + 卡名两条**（不误伤别的类）")

        check("不同大类下的同名卡不算「已经有了」",
              service.find_card("穿搭", GUIDE_CARD_TITLE) is None, "")
        other = service.create_manual_card("穿搭", GUIDE_CARD_TITLE, "👕", "")
        check("★ 别的大类下同名 → 照旧建得出来（不是「同名就一律挡」）",
              other is not None and other.id != first.id,
              f"#{getattr(other, 'id', None)}（对象攻略卡是 #{first.id}）")

        long_title = "十个字以上的很长卡名啊哈"
        long_card = service.create_manual_card("作息", long_title, "🌙", "")
        check("卡名比落库上限长时先截齐再查重（截断后就是同一张）",
              long_card is not None
              and service.find_card("作息", long_title) is not None
              and service.find_card("作息", long_title).id == long_card.id,
              f"落库卡名 {getattr(long_card, 'title', '')!r}（上限 {CARD_TITLE_MAX} 字）")

        # ---------- ③ 归档的那张不算；卡位满了也认得出 ----------

        section("③ 归档的不算「已经有了」；卡位满了也认得出现有那张")

        check("前置：先把「穿搭」那张收进卡册",
              service.archive_card(other.id) is True, f"#{other.id}")
        check("★ 归档的那张不算「已经有了」（收起来 = 轮播上的位子空着）",
              service.find_card("穿搭", GUIDE_CARD_TITLE) is None, "")
        again_other = service.create_manual_card("穿搭", GUIDE_CARD_TITLE, "👕", "")
        check("★ 于是再建一张新的顶上（不是把归档那张交回来）",
              again_other is not None and again_other.id != other.id,
              f"归档 #{other.id} → 新建 #{getattr(again_other, 'id', None)}")

        # 卡位填满：预设那两张（餐饮/出行）不占名额，拿别的大类填
        filled = 0
        for index, category in enumerate(("娱乐", "消费", "健身")):
            if service.personal_full():
                break
            if service.create_manual_card(category, f"占位{index}", "🎮", "") is not None:
                filled += 1
        check("前置：个人卡位已经满了", service.personal_full() is True,
              f"这一轮填了 {filled} 张")
        check("卡位满了 → 新的一张建不出来（返回 None）",
              service.create_manual_card("娱乐", "还想再建一张", "🎮", "") is None, "")
        still = _make_guide(service)
        check("★ 卡位满了也不谎报：同一对再要一次，交回的仍是**既有那张**",
              still is not None and still.id == first.id,
              f"交回 #{getattr(still, 'id', None)}（既有那张是 #{first.id}）")

        # ---------- ④ 社交那一层：已经有卡了就不再问 ----------

        section("④ 已经有「对象攻略卡」→ 那一问根本不排（社交那一层）")

        check("装配层真接上了判据（问的是做卡那边的 find_card）",
              app._guide_card_exists() is True, "")

        offers.clear()
        social.dismiss_offer()
        social.offer_card()
        check("★ 已经有卡 → 一条 social_card_offer 都不发", not offers,
              " | ".join(offers) or "（一条都没发）")
        check("★ 也不端着「正等建卡答复」（pending_offer 落到 False）",
              social.awaiting_card is False and social.pending_offer is False,
              f"awaiting_card={social.awaiting_card} pending_offer={social.pending_offer}")

        # 反证：把判据换成"一直说没有"（= 修复前的样子）→ 那一问又排出来了
        social.set_card_exists_provider(lambda: False)
        try:
            offers.clear()
            social.offer_card()
            check("★ 反证：判据蒙上（修复前的样子）→ 那一问照旧排出来",
                  len(offers) == 1 and social.awaiting_card is True,
                  f"{len(offers)} 条 / awaiting_card={social.awaiting_card}")
        finally:
            social.set_card_exists_provider(app._guide_card_exists)

        # 收进卡册之后，那一问该回来（判据是现问的，不是开场算一次的）
        for card in _emotion_cards(service):
            service.archive_card(card.id)
        check("★ 把那张收进卡册 → 判据立刻回到 False（现问，不是开场算一次）",
              app._guide_card_exists() is False, "")
        offers.clear()
        social.dismiss_offer()
        # 这一趟社交前台里"要不要建卡"已经问过一遍了（v2-social-offer-dedup 只在切出聊天窗
        # 再回来才重置）。这里只单验"卡收了 → 判据现问、又回到没有卡"这条，先把上一问的
        # 会话级记忆清掉，别让"同一趟只问一次"把这条挡住。
        social._last_offered = 0.0
        social.offer_card()
        check("★ 于是那一问照旧排出来（收起来 ≠ 永远不再问）",
              len(offers) == 1 and social.awaiting_card is True, f"{len(offers)} 条")
        social.dismiss_offer()

        # ---------- ④-2 建卡那一问：同一趟社交前台里只问一次（v2-social-offer-dedup） ----------
        #
        # 真机那条：候选一轮接一轮地来，装配层每完成一轮都喊一次 `offer_card`，连拟几条就
        # 连问几遍"要不要建卡"（"连回三条"问三遍）。这一节验：同一趟社交前台里只问一次，
        # 答了「不用了」也不重问；切出聊天窗再回来才重新问。

        section("④-2 建卡那一问：同一趟社交前台里只问一次（v2-social-offer-dedup）")

        # 上一节收尾已经把卡收干净（判据回到「没有卡」）、也把"问过"翻篇过一遍，这里从头摆。
        social._last_offered = 0.0
        social._card_awaiting = False
        offers.clear()
        social.offer_card()
        check("★ 第一问照常排出来", len(offers) == 1 and social.awaiting_card is True,
              f"{len(offers)} 条")

        # 装配层下一轮候选还是这套动作：dismiss（清邀请态）→ offer。第二问不许再排。
        offers.clear()
        social.dismiss_offer()
        social.offer_card()
        check("★ 同一趟里第二问不再排（连拟几条 ≠ 问几遍）",
              not offers and social.awaiting_card is False, f"{len(offers)} 条")

        # 用户点 [不用了] 之后，同一趟里也照样不再问（别翻来覆去劝）
        social.confirm_card("不用了")
        offers.clear()
        social.offer_card()
        check("★ 答了「不用了」→ 这一趟里也照旧不再问", not offers, f"{len(offers)} 条")

        # 切出聊天窗 = 翻篇：下一趟进来那一问重新问一次
        social._social_foreground = True
        social.on_foreground("chrome.exe")
        check("切出聊天窗 → 「这一趟已问过」清零",
              social._last_offered == 0.0, str(social._last_offered))
        offers.clear()
        social.offer_card()
        check("★ 下一趟进来 → 那一问照旧排出来（不是永久关掉）",
              len(offers) == 1 and social.awaiting_card is True, f"{len(offers)} 条")
        social.dismiss_offer()
        social._card_awaiting = False

        # ---------- ⑤ 建卡那条真路：写进既有卡 + 说真话 ----------

        section("⑤ 点 [好呀] 那条真路：第一张照旧建，第二张写进它")

        # 先腾位子、并把现有的攻略卡收掉：这一节要从"还没有这张卡"起步
        room = 0
        for card, _pos in service.plaza():
            if card.theme != CARD_CATEGORY and is_personal_card(card):
                room += service.archive_card(card.id)
        for card in _emotion_cards(service):
            room += service.archive_card(card.id)
        check("前置：卡位腾出空来、轮播上也没有攻略卡了",
              not service.personal_full() and app._guide_card_exists() is False,
              f"收掉 {room} 张")

        bootstrap_mod.GuideCardDialog = _GuideCardRecorder
        n_before = len(_emotion_cards(service))
        spoken.clear()
        app._on_profile_extracted("对方是谁：室友\n兴趣/爱好：跑步")
        pump(0.1)
        built = service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)
        check("第一张：照旧建出来、照旧说「建好了」",
              built is not None and len(_emotion_cards(service)) == n_before + 1
              and any(GUIDE_CARD_TITLE in s and "建好" in s for s in spoken),
              " | ".join(spoken))
        kb_file = Path(built.kb_path) / "对方信息.md"
        check("对方信息写进了这张卡的专属库（前置条件）",
              kb_file.exists() and "跑步" in kb_file.read_text(encoding="utf-8"),
              str(kb_file))

        spoken.clear()
        app._on_profile_extracted("对方是谁：同事\n兴趣/爱好：爬山")
        pump(0.1)
        check("★ 已经有一张了 → 不再建第二张",
              len(_emotion_cards(service)) == n_before + 1,
              f"{n_before + 1} → {len(_emotion_cards(service))}")
        check("★ 这一份写进的是**既有那张**的专属库",
              "爬山" in kb_file.read_text(encoding="utf-8"),
              kb_file.read_text(encoding="utf-8").strip().replace("\n", " / "))
        check("★ 话也说得对：说「已经有一张了…写进那张里了」，不谎报「建好了」",
              any(GUIDE_CARD_TITLE in s and "已经有一张" in s for s in spoken)
              and not any("建好" in s for s in spoken),
              " | ".join(spoken))

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：pet.db 零写入、零新依赖、不碰剪贴板")

        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("查重只用既有 DAO 的查法（零新依赖）",
              "list_by_archived" in _source(service.find_card), "")
        check("社交那条线自己一个剪贴板接口都没碰（写的那只手动在装配层）",
              "clipboard" not in _source(smod).lower(), "")

        drain_threads()
    finally:
        box.cleanup()
    return summary("建卡去重（v2-social-card-dedup）")


if __name__ == "__main__":
    sys.exit(main())
