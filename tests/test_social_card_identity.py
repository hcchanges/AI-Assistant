# -*- coding: utf-8 -*-
"""卡身份：挂名 + 标签 + 重名复用（v2-social-card-identity）的自动测试。

**病根**（真机图 image_425567049.png）：攻略卡标题是「❤️ 对象攻略卡」，没对象名、没标签。
多对象攻略时一堆卡全叫同一个名字，分不清谁是谁。这一批补「③增强」：

- **挂名**：建卡时把社交那条线认出来的会话名（如「茗风」）挂上去，`card.name = 对象名·攻略卡`；
- **查重**：按 `card.peer_name`（原始对象名）查重，同一个对象不再攒好几张；
- **标签**：新增 `card.tags`（字符串数组），用户在卡面自打分类（暧昧对象 / 同事 / 闺蜜…）。

三格的落点（实现决策已定死，这一批只落地、不推演）：
- `Card.name` 是**显示名**（可改），`Card.peer_name` 是**查重归属**（改名不动它），
  `Card.tags` 逗号分隔存（跟 `features` 一个口径）。
- 卡面顶部由「❤️ 对象攻略卡」改成「❤️ {card.name}」+ 一行「卡名 / 标签」可改框；
  轮播 / 卡册 / 存图共用 `Card.display_name()`（挂名就用 name，没挂退回 title）。
- 查重两侧统一走 `find_card_by_peer`（手动建卡 + c1 自动抽取触发）；认不出对象名就
  退回老口径（标题「对象攻略卡」按同类同名查重），老行为一个字节不改。

红线（本模块逐条钉住）：
- **零新依赖**（只加 `EMOTION_CATEGORY` 一个既有模板常量）；
- **两库零写入**（挂名 / 标签 / 查重不写回话，pet.db utterance 一行不多）；
- **既有社交 / 卡测试零改动**（`find_card` 老口径、`_PROFILE_COLUMNS` 那八格一个字没动）。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary  # noqa: E402

import core.bootstrap as bootstrap_mod  # noqa: E402
import data.dao as dao_mod  # noqa: E402
import data.models as models_mod  # noqa: E402
import life.cards.service as service_mod  # noqa: E402
import ui.panels.card_panel as panel_mod  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)

PEER = "茗风"
SUFFIX = "·攻略卡"
PEER_DISPLAY = f"{PEER}{SUFFIX}"


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


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _emotion_cards(service) -> list:
    """轮播上的「情感」大类卡（对象攻略卡就在这一类里）。"""
    return [card for card, _pos in service.plaza() if card.theme == CARD_CATEGORY]


def _banned_refs(module) -> set:
    """这个模块里引了哪些第三方库（零新依赖的判据）。"""
    banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
              "pyautogui", "win32api", "fuzzywuzzy", "regex"}
    refs: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(module))):
        if isinstance(node, ast.Import):
            refs.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.update(alias.name for alias in node.names)
    return banned & refs


def main() -> int:
    box, stub, app = make_env()
    try:
        service = app.card_service
        pet_before = _rows(app.config.db_path, "utterance")

        # ---------- ① 建卡挂名：name / peer_name / tags 三格落库 ----------

        section("① 建卡挂名：name = 「对象名·攻略卡」，peer_name / tags 落库")

        check("前置：轮播上还没有对象攻略卡、也没有这个对象名",
              service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE) is None
              and service.find_card_by_peer(PEER) is None, "")
        guide = service.create_manual_card(
            CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
            name=PEER_DISPLAY, peer_name=PEER, tags=["暧昧对象", "同事"],
        )
        check("★ 建卡成功", guide is not None, repr(guide))
        check("★ card.name == 「茗风·攻略卡」", guide.name == PEER_DISPLAY, repr(guide.name))
        check("★ card.peer_name == 「茗风」（查重归属）", guide.peer_name == PEER, repr(guide.peer_name))
        check("★ title 还是「对象攻略卡」（挂名只加 name，不动 title）",
              guide.title == GUIDE_CARD_TITLE, repr(guide.title))
        check("★ tags 逗号分隔落库", guide.tags == "暧昧对象,同事", repr(guide.tags))
        check("★ display_name() 挂名就显示 name", guide.display_name() == PEER_DISPLAY,
              guide.display_name())
        check("★ tag_list() 还原成列表", guide.tag_list() == ["暧昧对象", "同事"],
              str(guide.tag_list()))
        reloaded = service.card(guide.id)
        check("★ 新会话再读，三格都在（不是只写在内存对象上）",
              reloaded.name == PEER_DISPLAY and reloaded.peer_name == PEER
              and reloaded.tags == "暧昧对象,同事", "")

        diet = service.create_manual_card("餐饮", "少喝奶茶", "🥤", "一周两杯")
        check("★ 其余卡不挂名：name / peer_name 是 NULL、tags 是空串",
              diet.name is None and diet.peer_name is None and diet.tags == "", "")
        check("★ 其余卡 display_name() 退回 title", diet.display_name() == "少喝奶茶",
              diet.display_name())

        # ---------- ② 重名复用：同 peer_name 再建 → 不新建 ----------

        section("② 同对象再建 → 复用不新建（find_card_by_peer 命中）")

        n_emotion = len(_emotion_cards(service))
        check("★ find_card_by_peer 认得出来", service.find_card_by_peer(PEER).id == guide.id, "")
        again = service.create_manual_card(
            CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
            name=PEER_DISPLAY, peer_name=PEER,
        )
        check("★ 同 peer_name 再建 → 交回既有那张（不新建）",
              again.id == guide.id, f"#{guide.id} → #{again.id}")
        check("★ 情感类卡还是那一张（没攒第二张）",
              len(_emotion_cards(service)) == n_emotion,
              f"{n_emotion} → {len(_emotion_cards(service))}")

        # ---------- ③ 卡面顶部：挂名 + 标签行（只攻略卡有） ----------

        section("③ 卡面顶部显示挂名 + 标签行（只攻略卡有）")

        panel = app.card_panel
        panel.show_card(guide.id)
        pump(0.2)
        check("★ 顶部标题是「💗 茗风·攻略卡」（不再是「对象攻略卡」）",
              panel._title.text() == f"{guide.icon} {guide.display_name()}",
              panel._title.text())
        check("★ 身份块亮着（只有攻略卡有这一行）", panel._identity.isVisibleTo(panel), "")
        check("★ 卡名框回显挂名", panel._name_edit.text() == PEER_DISPLAY,
              panel._name_edit.text())
        check("★ 标签框回显标签（顿号分隔）", panel._tags_edit.text() == "暧昧对象、同事",
              panel._tags_edit.text())

        panel.show_card(diet.id)
        pump(0.2)
        check("★ 换成非攻略卡 → 身份块整块收着", not panel._identity.isVisibleTo(panel), "")
        check("★ 非攻略卡顶部标题还是 title（display_name 退回）",
              panel._title.text() == f"{diet.icon} {diet.title}", panel._title.text())

        # ---------- ④ 手动改名 / 打标签：改名不动查重归属 ----------

        section("④ 手动改名 / 打标签：改名只动 name，查重归属 peer_name 不动")

        panel.show_card(guide.id)
        pump(0.2)
        panel._name_edit.setText("宝贝·攻略卡")
        panel._save_name()
        pump(0.1)
        renamed = service.card(guide.id)
        check("★ 改名只动 name", renamed.name == "宝贝·攻略卡", repr(renamed.name))
        check("★ peer_name 一字不动（查重归属没被带跑）", renamed.peer_name == PEER,
              repr(renamed.peer_name))
        check("★ 改了名仍按 peer_name 命中", service.find_card_by_peer(PEER).id == guide.id, "")
        check("★ 改名后再按同 peer 建卡仍复用",
              service.create_manual_card(
                  CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
                  name=PEER_DISPLAY, peer_name=PEER,
              ).id == guide.id, "")
        check("★ 顶部标题跟着改名刷新",
              panel._title.text() == f"{renamed.icon} {renamed.display_name()}",
              panel._title.text())

        panel._tags_edit.setText("暧昧对象 / 同事 / 闺蜜")
        panel._save_tags()
        pump(0.1)
        retagged = service.card(guide.id)
        check("★ 打标签：斜杠 / 空格切开成三枚",
              retagged.tag_list() == ["暧昧对象", "同事", "闺蜜"], str(retagged.tag_list()))
        check("★ 标签框回显（顿号分隔）", panel._tags_edit.text() == "暧昧对象、同事、闺蜜",
              panel._tags_edit.text())

        # ---------- ⑤ 社交那条建卡真路：认得出对象名就挂名 ----------

        section("⑤ 装配层 _on_profile_extracted：会话名挂到卡名上")

        bootstrap_mod.GuideCardDialog = _GuideCardRecorder
        app.social_reply._peer_name = "明锋"
        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        n_before = len(_emotion_cards(service))
        app._on_profile_extracted("对方是谁：室友\n兴趣/爱好：爬山")
        pump(0.1)
        mingfeng = service.find_card_by_peer("明锋")
        check("★ 认得出对象名 → 建出 card.name == 「明锋·攻略卡」",
              mingfeng is not None and mingfeng.name == "明锋·攻略卡",
              repr(getattr(mingfeng, "name", None)))
        check("★ peer_name == 「明锋」", mingfeng is not None and mingfeng.peer_name == "明锋", "")
        check("★ 情感类卡多了一张（不是复用茗风那张）",
              len(_emotion_cards(service)) == n_before + 1,
              f"{n_before} → {len(_emotion_cards(service))}")
        check("★ 说的话带挂名", any("明锋·攻略卡" in s for s in spoken), " | ".join(spoken))

        spoken.clear()
        app._on_profile_extracted("对方是谁：同事\n兴趣/爱好：跑步")
        pump(0.1)
        check("★ 同一个对象再抽 → 复用不新建（说「已经有一张」）",
              len(_emotion_cards(service)) == n_before + 1
              and any("已经有一张" in s and "明锋·攻略卡" in s for s in spoken),
              " | ".join(spoken))

        # 认不出对象名 → 退回老口径（标题「对象攻略卡」按同类同名查重仍认得出来）
        check("★ 认不出对象名 → 退回老口径（find_card 按同类同名仍查得到）",
              service.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE) is not None, "")

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：pet.db 零写入、零新依赖、老口径没被带跑")

        drain_threads()
        check("pet.db utterance 一行没多（挂名 / 标签 / 查重不写回话）",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        for name, mod in (("data.models", models_mod), ("data.dao", dao_mod),
                          ("life.cards.service", service_mod), ("ui.panels.card_panel", panel_mod)):
            check(f"★ 零新依赖：{name} 没多一个第三方 import",
                  not _banned_refs(mod), "、".join(sorted(_banned_refs(mod))) or "（一处都没有）")
        check("★ 查重归属字段表没动（dao 那八格一个没改）",
              dao_mod._PROFILE_COLUMNS == (
                  "likes", "dislikes", "strategy", "relation_stage",
                  "age", "emotion_state", "anxiety", "interests",
              ), str(dao_mod._PROFILE_COLUMNS))
        check("★ 老口径 find_card 照旧按同类同名找（没被挂名带跑）",
              "list_by_archived" in inspect.getsource(service.find_card), "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("卡身份：挂名 + 标签 + 重名复用（v2-social-card-identity）")


if __name__ == "__main__":
    raise SystemExit(main())
