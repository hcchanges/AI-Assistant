"""社交冒话那道冷却从「窗口标题」升级成「窗口标题|对象名」（v2-social-cooldown-by-peer）。

**为什么要它**：`_cooldown_key()` 上一节点取的是 `_session_id or ""`，而 `_session_id`
在**主窗合并模式**下恒是同一个值——真机 `data/logs/pet.log.1` 10:26:33 到 10:40:56 那几
十行「社交邀请被冷却挡下：距上次冒话 N 秒 < 1800 秒（这一场标识 **2 字**）」就是它：用户
在一个窗口里换了好几个会话，冷却一直钉在 2 字那一格，16 分钟里一句都没再冒。反过来
10:37:41 那行「社交窗口换了会话（上一场标识 2 字 → 这一场 6 字）」之后 9 秒，10:37:50
就「冒一句要不要帮忙措辞」——**换一格就当场放行，机制本身没坏，坏的是所有会话共用一格**。

这一节点把对象名合进键：`f"{sid}|{peer}"`（`peer` 由装配层从窗口标题里认出来，独立聊天
窗上就是对方 / 群名；主窗标题是应用名，认不出对象名就回空串）。

三条守住的老语义：
- **认不出对象名就逐字退回裸 `_session_id`**（测试桩 / 主窗 / 列表页 / 账号选择页全走这一
  支）——不是「永不冷却」，是「退回这一改之前的老行为」；
- **一个字的名字都不多进日志**：日志仍只出 `len(self._cooldown_key())`（`微信|甲` 报 4 字）；
- **纯内存**：`_last_asked_by_window` / `_helping` / `_asked_key` 三处都不落盘、不进库。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不碰 `chat_area_box`
裁剪几何（那是 B 案）；不动 584c707 哨兵 / 2afc76d 绝对三分钟 / e64700f 孤儿化白名单 /
b9b6caf 列表免 OCR / afebdcb loading 腿 / #6 账号选择腿 / 三白名单 / 危机红线。
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary  # noqa: E402

from core.bootstrap import _SOCIAL_APP_TITLES, _strip_unread_count  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_LIST,
    PAGE_PERSONAL,
    SocialReplyService,
)
from life.screen.social_window import chat_area_box  # noqa: E402

MAIN_WINDOW = "微信"          # 主窗那个恒定的标题（真机日志里报的「2 字」）


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class _Catch(logging.Handler):
    """把 `life.screen.social` 那几行 DEBUG 抓下来，好验「日志里只有长度、没有名字」。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:      # noqa: BLE001 —— 这是取证，不该把被测那条线带崩
            pass


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    # 冷却写死成 30 分钟（跟 config 默认一致），别跟着本机配置走。
    app.config.screen.social_cooldown_min = 30

    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    sid = {"s": MAIN_WINDOW}          # 「窗口标题」那条腿
    peer = {"p": ""}                  # 「对象名」那条腿（装配层滤过的）
    clock = {"t": 1000.0}
    service._session_provider = lambda: sid["s"]
    service._peer_provider = lambda: peer["p"]
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_proc = "Weixin.exe"

    def _switch(who: str, title: str = MAIN_WINDOW) -> None:
        """换到某个对象那一场：两条腿一起喂，走真实的 `_sync_session`（不直接塞私有格）。"""
        sid["s"] = title
        peer["p"] = who
        service._sync_session()

    def _knock() -> bool:
        """从会话总列表点进单聊那一拍——走真实的 `note_page_kind` 跃迁闸。

        回 True = 这一拍真冒了（`social_offer` 发了出去）。
        """
        service._awaiting = False
        service._running = False
        service._page_kind = PAGE_LIST          # 先退出来，「非单聊 → 单聊」才成立
        app.chat_bubble.hide_offer()
        before = len(offers)
        service.note_page_kind(PAGE_PERSONAL)
        return len(offers) - before == 1

    catcher = _Catch()
    log = logging.getLogger("life.screen.social")
    log_level_before = log.level
    log.setLevel(logging.DEBUG)          # 这行日志是 DEBUG，得让它真发出来
    log.addHandler(catcher)

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 主窗里两个对象 = 两格：谁也不连坐谁 ----------

        section("① 同一扇主窗里换对象 → 两格，各占各的 30 分钟（真机那 16 分钟就栽在这）")

        check("前置：装配层真接上了这条腿（`bootstrap` 里 peer_provider 不是空的）",
              service._peer_provider is not None, "")

        _switch("甲")
        check("前置：标识还是主窗那个恒定的标题（模拟真机「这一场标识 2 字」）",
              service._session_id == MAIN_WINDOW, repr(service._session_id))
        check("★ 键里合进了对象名（`微信|甲`）",
              service._cooldown_key() == f"{MAIN_WINDOW}|甲",
              repr(service._cooldown_key()))
        check("★ 甲这一场冒了", _knock() is True, f"{len(offers)} 次")
        check("★ 落戳记在**甲**那一格上",
              set(service._last_asked_by_window) == {f"{MAIN_WINDOW}|甲"},
              repr(sorted(service._last_asked_by_window)))

        clock["t"] += 60.0
        check("★ 一分钟后再跃迁（还是甲）→ 被冷却挡下，不冒", _knock() is False,
              f"{len(offers)} 次")

        # ---------- ② 换对象名当场放行（复刻真机 10:37:50 那一拍） ----------

        section("② 换到乙 → 标识一个字没变，就靠对象名换了一格 → 当场放行")

        _switch("乙")
        check("★ 窗口标识**没变**（主窗合并模式下它一直不变——这就是要修的那一半）",
              service._session_id == MAIN_WINDOW, repr(service._session_id))
        check("★ 但键变了（`微信|乙`）",
              service._cooldown_key() == f"{MAIN_WINDOW}|乙",
              repr(service._cooldown_key()))
        check("★ 乙这一场没冒过 → 当场冒出来（真机 10:37:50 那一拍）",
              _knock() is True, f"{len(offers)} 次")
        check("★ 两格各占着，互不覆盖",
              set(service._last_asked_by_window) == {f"{MAIN_WINDOW}|甲", f"{MAIN_WINDOW}|乙"},
              repr(sorted(service._last_asked_by_window)))

        clock["t"] += 60.0
        _switch("甲")                          # 切回甲：这一场那一格还在占着
        check("★ 再切回甲 → 甲那一格还在占着，不冒", _knock() is False, f"{len(offers)} 次")
        clock["t"] = 1000.0 + 30 * 60 + 1
        check("★ 甲那一格满 30 分钟 → 放行（冷却时长一个字没动）", _knock() is True,
              f"{len(offers)} 次")

        # ---------- ③ 取不到对象名 → 逐字退回裸标识（老行为） ----------

        section("③ 认不出对象名（主窗 / 列表页 / 测试桩）→ 逐字退回裸 `_session_id`")

        _switch("", title="群聊名")          # 有标识、没对象名
        check("★ 键 == 裸标识（一个字符都没多出来）",
              service._cooldown_key() == "群聊名", repr(service._cooldown_key()))

        _switch("甲", title="微信")          # 接回正轨，再验测试桩那条路
        service._peer_provider = None
        _switch("甲", title="只有标题")
        check("★ 没接 `peer_provider` 时键 == 裸标识（既有那条用例走的就是这里）",
              service._cooldown_key() == "只有标题", repr(service._cooldown_key()))
        check("★ 没接这条腿也不崩（键还是字符串，不会变成「永不冷却」）",
              isinstance(service._cooldown_key(), str)
              and service._last_asked is None, repr(service._cooldown_key()))
        service._peer_provider = lambda: peer["p"]

        _switch("同名", title="同名")
        check("★ 对象名跟标识同一个值 → 不重复拼成 `同名|同名`",
              service._cooldown_key() == "同名", repr(service._cooldown_key()))

        # 两条腿都空（还没抓过标题）：`_sync_session` 在标识为空时**什么都不动**（宁可漏清
        # 一场、不误清一场，老语义一个字没改），所以桩里手工摆这一格。
        service._session_id = ""
        service._peer_name = ""
        check("★ 两条腿都取不到 → 键是空串那一格（老口径：全场共用一格，不是「永不冷却」）",
              service._cooldown_key() == "", repr(service._cooldown_key()))

        # ---------- ④ 键里带了名字，日志里仍然只有长度 ----------

        section("④ 私密口径没松：日志只出长度，名字一个字都不进日志")

        _switch("甲")
        clock["t"] += 1.0
        service._last_asked = clock["t"]        # 手工按一个戳，让下一拍必被冷却挡下
        clock["t"] += 5.0
        service._awaiting = False
        service._running = False
        app.chat_bubble.hide_offer()
        catcher.lines.clear()
        service._page_kind = PAGE_LIST
        service.note_page_kind(PAGE_PERSONAL)
        blocked = [ln for ln in catcher.lines if "被冷却挡下" in ln]
        check("★ 冷却那行真冒出来了（不是没走到这一步）", bool(blocked), f"{catcher.lines}")
        check("★ 只报标识**长度**（`微信|甲` = 4 字）",
              any("这一场标识 4 字" in ln for ln in blocked), f"{blocked}")
        check("★ 名字本身一个字都没进日志",
              not any("甲" in ln for ln in blocked), f"{blocked}")

        # ---------- ⑤ 源码级 + 红线 ----------

        section("⑤ 源码级：换的是键的内容，不是调用点；红线一个没动")

        svc_src = inspect.getsource(smod)
        key_src = inspect.getsource(SocialReplyService._cooldown_key)
        sync_src = inspect.getsource(SocialReplyService._sync_session)
        ask_src = inspect.getsource(SocialReplyService._ask_offer)
        drop_src = inspect.getsource(SocialReplyService._drop_stale_offer)

        check("★ 其余用键处一处没改：它们全问 `_cooldown_key()`",
              "_last_asked_by_window.get(self._cooldown_key())" in svc_src
              and "_last_asked_by_window.get(self._asked_key)" in drop_src
              and "_asked_key = self._cooldown_key()" in ask_src
              and "self._helping[self._cooldown_key()] = self._clock()" in svc_src
              and "self._helping.pop(self._cooldown_key(), None)" in svc_src, "")
        check("★ `_cooldown_key` 只读自家那两格，不另开一只手去读窗口标题",
              "_session_provider" not in key_src and "_grab" not in key_src
              and "self._session_id" in key_src and "self._peer_name" in key_src, "")
        check("★ 对象名跟标识同一拍刷（不会「标识换了、名字还是上一场的」）",
              "self._peer_name = self._normalize_listen(" in sync_src
              and "self._session_id = ident" in sync_src, "")
        check("★ 换人清记忆那条判据照旧只看标识（返回值一个字没改）",
              "previous == ident" in sync_src
              and "self._forget_session_memory()" in sync_src, "")
        check("★ 对象名不进材料（`_chat_text` 那条线一处都没碰它）",
              "_peer_name" not in inspect.getsource(SocialReplyService._on_screen_read)
              and "_peer_name" not in inspect.getsource(smod._tail_context), "")
        check("★ 没碰裁剪几何（B 案不动）：`chat_area_box` 的上沿还是 `TITLE_BAR_PX`",
              "TITLE_BAR_PX" in inspect.getsource(chat_area_box), "")
        check("★ `_awaiting` / `_running` 状态机一个字没动",
              "_awaiting =" not in key_src and "_running =" not in key_src, "")

        # ---------- ⑥ 装配层那条腿（只用真函数，不造假） ----------

        section("⑥ 装配层：先抹未读数，再把「是不是应用名」认出来")

        check("★ 未读数抹掉（不抹的话每来一条未读就换一个键，30 分钟冷却被自己顶掉）",
              _strip_unread_count("微信(3)") == "微信"
              and _strip_unread_count("微信（12）") == "微信"
              and _strip_unread_count("微信[5]") == "微信", "")
        check("★ 名字里本来就带括号的不能误伤",
              _strip_unread_count("项目（二期）") == "项目（二期）"
              and _strip_unread_count("团子(1)") == "团子", "")
        check("★ 应用名那一类在册（主窗标题 → 对象名留空）",
              "微信" in _SOCIAL_APP_TITLES and "wechat" in _SOCIAL_APP_TITLES, "")
        check("空标题 / 取不到 → 空串，不炸",
              _strip_unread_count("") == "" and _strip_unread_count(None) == "", "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        log.removeHandler(catcher)
        log.setLevel(log_level_before)
        drain_threads()
        box.cleanup()

    return summary("社交冒话冷却按「会话标识 + 对象名」分格（v2-social-cooldown-by-peer）")


if __name__ == "__main__":
    raise SystemExit(main())
