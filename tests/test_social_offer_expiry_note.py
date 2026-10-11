"""邀请过期那一拍：交代无条件补 + 只摘刚过期那一句（v2-social-offer-expiry-note）。

只读跟踪已证的两个 bug，这一节点各修一处：

**(a) 无交代。** `_on_social_offer_expired` 补那句灰字原来是挂在"人还在单聊里"
（`social_reply.in_personal_chat`）后头的。可**过期那一拍它几乎必为假**：超时判据是
`_awaiting` 挂了 `OFFER_HANG_S`（90 秒）没人理（`social._drop_stale_offer`），90 秒里人早就
切去别处了——真机后果是"问句凭空消失、一个字不交代"。判据本身还自相矛盾：
`is_personal_kind("")` 把空串当单聊（回 True），而 `_page_kind` 一过列表 / 登录页就不是空串、
同一个判据立刻回 False。**改成无条件补**：这一句本来就是说给"还看着这一问的人"听的。

**(b) 误摘上下文那一句。** `clear_offer_ask` 原来是**全表扫**：倒着走一遍，把记录区里
**所有**打了 `ROLE_OFFER_ASK` 的条目一扫而空。于是"点 [好呀] 之后留作这一则草稿上下文的那句
问句"也被一并摘掉——那不是这一拍要收的那一则。**改成只摘最近那一则**（倒着走摘第一个命中的
就停）：这一拍过期的只可能是**最后**冒出来的那一问（`_awaiting` 同一时刻只有一个）。

红线：零新依赖；不碰 config.yaml / .vscode；`pet.db` / `mind.db` 零写入；
`f10e5f2`（#1）的 `ROLE_OFFER_ASK` / `append_offer_ask` 标记机制一个字没动（只改
`clear_offer_ask` 的"摘哪些"与过期补交代的条件）；`in_personal_chat` 属性保留
（`bootstrap.py:2097` / `social.py:1276` 那两处调用方不动）。

**如实记一处必要改动**：本节点把 §前身 `tests/test_social_offer_lifetime.py` 里三处
**钉住旧行为**的断言改掉了（那段"停在会话总列表上就不补"的反面对照、源码级那条
`"in_personal_chat" in expired_src`、以及文档里那句"判据用 `in_personal_chat`"）。那不是
放宽既有测试，而是**旧断言钉的正是本节点要修的 bug**；新口径的完整覆盖在本文件里。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    summary,
)

from core.bootstrap import OFFER_EXPIRED_NOTE  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_LIST,
    SocialReplyService,
)
from ui.chat_bubble import (  # noqa: E402
    ROLE_OFFER_ASK,
    ROLE_SPEAKER,
    SPEAKER_NOTE,
)

CONTEXT_ASK = "聊天么？要不要我帮你回呀？"      # 点 [好呀] 之后留作上下文的那一句
NEW_ASK = "又聊上了？要不要我再帮你回一段？"     # 后面新冒出来的那一问


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _ask_items(app) -> list[str]:
    """记录区里打了 `ROLE_OFFER_ASK` 那几格（问句）。"""
    history = app.chat_bubble._history
    return [history.item(i).text()
            for i in range(history.count())
            if history.item(i).data(ROLE_OFFER_ASK)]


def _note_items(app) -> list[str]:
    """记录区里"系统灰字"那几条（按说话人那一格认）。"""
    history = app.chat_bubble._history
    return [history.item(i).text()
            for i in range(history.count())
            if history.item(i).data(ROLE_SPEAKER) == SPEAKER_NOTE]


def _bubble_text(app) -> str:
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def _clear_stage(app) -> None:
    """清空台面（连 `ROLE_OFFER_ASK` 那几格一起——每节都从零起步，计数才准）。"""
    history = app.chat_bubble._history
    history.clear()
    app.chat_bubble.hide_offer()
    app.chat_bubble.set_busy(False)


def _code_of(func) -> str:
    """函数的**代码体**（掐掉 docstring）——源码级断言别被注释里的词带偏。"""
    src = inspect.getsource(func)
    parts = src.split('"""')
    return parts[-1] if len(parts) >= 3 else src


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.ocr_enabled = True

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 过期那一拍：无论停在哪一页，交代都补 ----------

        section("① 过期那声：login / list / 空页型 一律补「这条邀请过期了」")

        handler_src = inspect.getsource(type(app)._on_social_offer_expired)
        handler_code = _code_of(type(app)._on_social_offer_expired)
        check("★ 源码级：交代**不再挂在任何页型闸上**（`in_personal_chat` 已从这一处拿掉）",
              "in_personal_chat" not in handler_code, "")
        note_line = next(line for line in handler_code.splitlines()
                         if "append_note(OFFER_EXPIRED_NOTE)" in line)
        check("★ 源码级：`append_note(OFFER_EXPIRED_NOTE)` 是**无条件**的（在函数体那一层、"
              "没缩进进任何 `if`）",
              len(note_line) - len(note_line.lstrip()) == 8, f"缩进 {len(note_line) - len(note_line.lstrip())} 格")

        for kind, label in ((PAGE_LIST, "会话总列表"), ("login", "登录页"), ("", "空页型")):
            _clear_stage(app)
            service._page_kind = kind
            checks_before = app.chat_bubble._history.count()
            app._on_social_offer_expired()
            notes = _note_items(app)
            check(f"★ 停在{label}（`_page_kind={kind!r}`）→ 照样补上那句交代",
                  notes == [OFFER_EXPIRED_NOTE],
                  f"notes={notes} / {_bubble_text(app).replace(chr(10), ' / ')}")
            check(f"{label}：交代走的是**灰字系统条**（`SPEAKER_NOTE`，不是宠物气泡）",
                  app.chat_bubble._history.item(
                      app.chat_bubble._history.count() - 1).data(ROLE_SPEAKER)
                  == SPEAKER_NOTE, "")
            check(f"{label}：按钮那一排也收了（跟从前一样）",
                  app.chat_bubble.offer_theme == "",
                  repr(app.chat_bubble.offer_theme))
            check(f"{label}：交代就多这一条（没顺手多冒别的）",
                  app.chat_bubble._history.count() == checks_before + 1,
                  f"{checks_before} → {app.chat_bubble._history.count()}")

        # ---------- ② 只摘刚过期那一句：点过 [好呀] 留作上下文的那一问不许动 ----------

        section("② 上下文那一问不被摘：只收刚过期的那一则")

        _clear_stage(app)
        app.chat_bubble.append_offer_ask(CONTEXT_ASK)   # 点 [好呀] 之后留作上下文的一问
        check("前置：上下文那一问摆上了",
              _ask_items(app) == [CONTEXT_ASK], repr(_ask_items(app)))

        app.chat_bubble.append_offer_ask(NEW_ASK)       # 后面又冒了新的那一问
        check("前置：新的一问压在最上面",
              _ask_items(app) == [CONTEXT_ASK, NEW_ASK], repr(_ask_items(app)))

        app._on_social_offer_expired()                  # 过期的是新那一问
        check("★ **刚过期那一问被摘掉**了", NEW_ASK not in _ask_items(app),
              repr(_ask_items(app)))
        check("★ 点 [好呀] 留作上下文的那一问**原样留着**（修前它被全表扫连坐摘走）",
              CONTEXT_ASK in _ask_items(app), repr(_ask_items(app)))
        check("★ 摘完只掉了那一条（不是把标记过的全扫了）",
              _ask_items(app) == [CONTEXT_ASK], repr(_ask_items(app)))
        check("★ 交代照补（这一拍两件事都做）",
              _note_items(app) == [OFFER_EXPIRED_NOTE], repr(_note_items(app)))

        # ---------- ③ `clear_offer_ask` 只摘一则，不是全表扫 ----------

        section("③ `clear_offer_ask` 只摘目标那一则（源码级 + 计数断言）")

        ask_src = inspect.getsource(type(app.chat_bubble).clear_offer_ask)
        ask_code = _code_of(type(app.chat_bubble).clear_offer_ask)
        check("★ 认的还是 item data，不按文本挑（#1 那套口径一个字没动）",
              "data(ROLE_OFFER_ASK)" in ask_code, "")
        check("★ 还是倒着走（`takeItem` 会挪后面的行号）",
              "range(self._history.count() - 1, -1, -1)" in ask_code, "")

        lines = textwrap.dedent(ask_code).splitlines()
        take_at = next(i for i, line in enumerate(lines)
                       if "self._history.takeItem(row)" in line)
        after = [line.strip() for line in lines[take_at + 1:] if line.strip()]
        check("★ 源码级：`takeItem` 之后**紧跟着 `return`**（摘一个就收，不是扫全表）",
              after and after[0] == "self._fit_height()" and after[1] == "return",
              f"takeItem 之后是 {after[:3]}")
        check("★ 源码级：修前那个「一个一个攒着一起摘」的累加旗子（`removed`）已经没了",
              "removed" not in ask_code, "")

        _clear_stage(app)
        for text in ("第一问", "第二问", "第三问"):
            app.chat_bubble.append_offer_ask(text)
        before = _ask_items(app)
        check("前置：三则问句都在台面上（计数断言才有意义）",
              before == ["第一问", "第二问", "第三问"], repr(before))

        app.chat_bubble.clear_offer_ask()
        after_clear = _ask_items(app)
        check("★ 只摘掉**一则**（修前是 0 则——三则一锅端）",
              len(after_clear) == len(before) - 1, f"{before} → {after_clear}")
        check("★ 摘的正是**最近**那一则（倒着走第一个命中的）",
              after_clear == ["第一问", "第二问"], repr(after_clear))

        app.chat_bubble.clear_offer_ask()
        app.chat_bubble.clear_offer_ask()
        check("★ 再来两次，一次摘一则（幂等口径保住：没有就什么都不做）",
              _ask_items(app) == [], repr(_ask_items(app)))
        app.chat_bubble.clear_offer_ask()
        check("★ 台面空了再调也不炸（幂等）", _ask_items(app) == [], repr(_ask_items(app)))

        # 用户自己打的那句话，文本跟问句一模一样：摘的时候绝不能连坐
        _clear_stage(app)
        app.chat_bubble.append_user(CONTEXT_ASK)
        app.chat_bubble.append_offer_ask(CONTEXT_ASK)
        app.chat_bubble.clear_offer_ask()
        check("★ 文本与问句一模一样的**用户发言**不被摘（认标记、不认文本）",
              _ask_items(app) == []
              and _bubble_text(app).count(CONTEXT_ASK) == 1,
              _bubble_text(app).replace("\n", " / "))

        # ---------- ④ 红线 ----------

        section("④ 红线：#1 的标记机制没动 / 别条线不连坐 / 两库零写入")

        append_src = inspect.getsource(type(app.chat_bubble).append_offer_ask)
        check("★ #1 的 `append_offer_ask` 一个字没动（还是 `SPEAKER_PET` + `ROLE_OFFER_ASK`）",
              f"role=ROLE_OFFER_ASK" in append_src
              and "SPEAKER_PET" in append_src, "")
        check("★ 角色常量也没动（还是 `Qt.UserRole + 3`）",
              __import__("ui.chat_bubble", fromlist=["ROLE_OFFER_ASK"]).ROLE_OFFER_ASK
              == __import__("PySide6.QtCore", fromlist=["Qt"]).Qt.UserRole + 3, "")

        pop_src = inspect.getsource(type(app)._pop_offer)
        check("★ `_pop_offer` 仍旧只给 `social` 那条线打标记（别条线不连坐）",
              'theme == "social"' in pop_src, "")

        check("★ `clear_offer_ask` 全仓**只有一处**调用点（过期那一声）",
              inspect.getsource(type(app)).count("clear_offer_ask") == 1,
              str(inspect.getsource(type(app)).count("clear_offer_ask")))
        for name in ("_on_chat_send", "_on_card_clear_due"):
            check(f"★ `{name}` 不摘问句（用户打字那拍 / 3 分钟绝对收窗那拍绝不碰它）",
                  "clear_offer_ask" not in inspect.getsource(getattr(type(app), name)), "")

        check("★ `in_personal_chat` 属性**保留着**（别的调用方一个没动）",
              isinstance(type(service).in_personal_chat, property), "")
        check("★ 声明它给的那些调用方还在（`bootstrap` 里那一处 + 服务自己）",
              "service.in_personal_chat" in inspect.getsource(type(app)), "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
    finally:
        drain_threads()
        box.cleanup()

    return summary("邀请过期那一拍：无条件补交代 + 只摘刚过期那一句（v2-social-offer-expiry-note）")


if __name__ == "__main__":
    raise SystemExit(main())
