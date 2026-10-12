"""切应用上网再切回来，不再重问「要不要帮你回」（v2-social-switch-reask-fix）。

**真机证据链**（`data/logs/pet.log` 2026-10-12 那一段，只读跟踪得来，不是推测）：

```
23:36:24  前台换成 explorer.exe      ← 走出聊天窗口
23:36:26  前台换成 msedge.exe        ← 上网去了
23:37:03  前台换成 Weixin.exe        ← 切回微信
23:37:05,657  社交窗标题带认出这一场是谁：0 字（没认出来，退回会话标识）
23:37:05,658  社交软件在前台（Weixin.exe），冒一句要不要帮忙措辞   ← **又问你一遍**
```

旁证：23:32:33~23:34:54 那几行「帮聊那一则上屏」期间**没有**「冒一句」日志（当时
`_is_helping()` 早退 = 正在帮聊），所以 23:37:05 那次重冒确实是"帮聊那一格按新键查不到"。

根因（跟 v2-social-c1-offer-key-jitter 同源、但走的是另一条腿）：

```
切回那一拍  _grab_social_chat 判「带子变了」→ 清 `_social_peer_band_hint`（真机 2~15 秒才认回来）
紧接着    _read_social_screen 里的 _sync_session() → `_peer_name` 成了空串
_cooldown_key()  = `_session_id`（裸「微信」）    ← 冷却 / 帮聊两本账**当场换了一把键**
_is_helping()    按新键查 → False（戳盖在「微信|茗风」上）
_ask_offer       第一道闸失效 ⇒ 又冒一遍"要不要帮你回"
```

**修法**（`life/screen/social.py`，五处）：给"这一场是谁"那把键**在刚切回社交前台的
那一阵**留一个兜底的名字副本 `_peer_key_name`（`PEER_HOLD_MAX_S = 600.0` 秒上界）：

- **只在 `on_foreground` 的"切回"那一拍布防**（`not was_social and self._social_foreground`），
  走出聊天窗口那一拍**不布防**——兜的是"切回来"，不是"在场"。
- 取键照旧**先看 `_peer_name`**（老口径一个字没动）；只有它空着、且兜的那一阵还没到期，
  才吃 `_peer_key_name`。
- 读出真名字 / 真换场（会话标识变了）→ 兜的那一格**当场作废**；到期也作废。
- **只喂 `_cooldown_key()`**：`_peer_profile_block()` / `_note_peer_turns()` 那些地方照旧
  只认"这一拍读到的名字"——宁可少喂一格，也不把上一个人的档案端给下一个人（不串格）。

这一支钉六件事（①~⑦）：切出那一拍不布防 / 切回布防且那几拍不重问 / 认出来就换 /
真换场照样作废 / 兜有上界 / **没离开过前台时照旧退回裸标识**（jitter 那一批留下的边界
一个字没动）。

红线：零新依赖；不碰 config.yaml / .vscode；`pet.db` / `mind.db` 零写入；
`_is_helping()` / `_due()` / `is_personal_kind("")` / `_cooldown_key()` 的取键主口径
全是既有口径（既有社交测试零改动）。
"""

from __future__ import annotations

import inspect
import re
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

import monitor.foreground as fgmod  # noqa: E402
from core.bootstrap import DesktopPetApp  # noqa: E402
from life.screen import shot  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_LIST,
    PAGE_PERSONAL,
    PEER_HOLD_MAX_S,
    SocialReplyService,
)

MAIN_WINDOW = "微信"           # 主窗那个恒定的标题（合并窗里换会话它一个字都不变）
PEER_A = "茗风"                # 点过 [好呀] 的那一场（真机 23:29~23:34 那一段）
PEER_B = "阿澄"                # 另一个人（真换场用）
FRAME_W, FRAME_H = 742, 1342   # 真机那扇主窗的帧尺寸
BAND_ROW = ((60, 8, 110, 30), PEER_A)
BAND_ROW_B = ((60, 8, 110, 30), PEER_B)
OUT_PROC = "explorer.exe"      # 走出聊天窗口（切到桌面）那一拍
WEB_PROC = "msedge.exe"        # 上网那一拍（真机 23:36:26）
BACK_PROC = "Weixin.exe"       # 切回微信那一拍（真机 23:37:03）


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _code_of(func) -> str:
    """函数的**代码体**（掐掉 docstring）——源码级断言别被注释里的词带偏。"""
    src = inspect.getsource(func)
    parts = src.split('"""')
    return parts[-1] if len(parts) >= 3 else src


class _FakeMonitor:
    """假 monitor：**小图**（那条标题带）认成 `band_rows`，大图（聊天区）认成 `chat_rows`。"""

    def __init__(self) -> None:
        self.band_rows: list = []
        self.chat_rows: list = [((40, 60, 120, 76), "在的，你说")]

    def detect_screen_boxes(self, array):
        return []

    def read_screen_lines(self, array):
        shape = tuple(getattr(array, "shape", ()))
        if len(shape) == 3 and shape[0] <= 100:      # 那条带子（几十像素高）
            return list(self.band_rows)
        return list(self.chat_rows)


def main() -> int:
    import numpy as np
    from PySide6.QtGui import QColor, QImage

    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    app.config.screen.social_cooldown_min = 30

    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    clock = {"t": 1000.0}
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_proc = BACK_PROC
    app._social_session_hint = MAIN_WINDOW
    app._social_peer_hint = ""

    monitor = _FakeMonitor()
    real_monitor = app.screen_monitor
    app.screen_monitor = monitor

    real_title = fgmod.foreground_title
    real_pid = fgmod.foreground_pid
    real_proc = app._proc_name_of
    real_grab = shot.grab_foreground_window
    real_masks = app._social_pet_masks
    frame_shade = {"v": 200}

    def _filled(shade: int) -> QImage:
        img = QImage(FRAME_W, FRAME_H, QImage.Format_RGB32)
        img.fill(QColor(shade, shade, shade))
        return img

    fgmod.foreground_title = lambda: MAIN_WINDOW
    fgmod.foreground_pid = lambda: 4242
    app._proc_name_of = lambda pid: BACK_PROC
    app._social_pet_masks = lambda: []
    shot.grab_foreground_window = lambda rect, extra_masks=None: _filled(frame_shade["v"])

    def _grab_then_sync(shade: int):
        """真机那一拍：`_grab_social_chat` 抓完，`_poll_listen` **紧接着**就刷一次会话。"""
        frame_shade["v"] = shade
        arr = app._grab_social_chat()
        service._sync_session()
        return arr

    def _reader() -> None:
        """后台认字那一拍：走**真的** `_read_social_chat`（带子进门先取走、认名字）。"""
        app._read_social_chat(np.full((300, 400, 3), 250, dtype=np.uint8))
        service._sync_session()

    def _knock() -> bool:
        """从会话总列表点进单聊那一拍——走真实的 `note_page_kind` 跃迁闸（真机重冒那一拍）。"""
        service._awaiting = False
        service._running = False
        service._page_kind = PAGE_LIST
        app.chat_bubble.hide_offer()
        before = len(offers)
        service.note_page_kind(PAGE_PERSONAL)
        return len(offers) - before == 1

    def _key() -> str:
        return service._cooldown_key()

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 起手：认出「茗风」，点 [好呀] ----------

        section("① 起手：带子认出「茗风」、点 [好呀]——戳盖在 `微信|茗风` 上")

        _grab_then_sync(200)
        monitor.band_rows = [BAND_ROW]
        _reader()
        check("前置：键是 `微信|茗风`", _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        service._mark_helping()
        check("前置：这一场在帮聊里（戳盖在 `微信|茗风` 上）",
              service._is_helping() is True
              and set(service._helping) == {f"{MAIN_WINDOW}|{PEER_A}"},
              repr(sorted(service._helping)))
        check("★ 还没切过前台 → 兜的那一格**没布防**（上界是 0.0）",
              service._peer_hold_until == 0.0, repr(service._peer_hold_until))

        # ---------- ② 切出去（走出聊天窗口） ----------

        section("② 切出去那一拍：**不布防**、也不动帮聊那本账（兜的是「切回来」）")

        service.on_foreground(OUT_PROC)
        check("前置：这一拍真走出了聊天窗口",
              service._social_foreground is False, repr(service._social_foreground))
        check("★ 切出去那一拍**没布防**（布防只挂在「切回来」那一拍上）",
              service._peer_hold_until == 0.0, repr(service._peer_hold_until))
        check("★ 切出去那一拍不动帮聊那本账",
              set(service._helping) == {f"{MAIN_WINDOW}|{PEER_A}"},
              repr(sorted(service._helping)))
        check("★ 切出去那一拍键也没被改过",
              _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        check("★ 上网那一拍（别的应用在前台）同样不布防",
              (service.on_foreground(WEB_PROC) or True) and service._peer_hold_until == 0.0,
              repr(service._peer_hold_until))

        # ---------- ③ 切回来：名字还没认出来那几拍，不重问 ----------

        section("③ 切回微信：布防 + 那几拍认不出名字 ⟶ 键不退化、**一句不再重问**")

        clock["t"] += 4.0
        frame_shade["v"] = 120        # 切回那一帧跟离开前不一样（真机上是气泡 / 滚动）
        monitor.band_rows = []        # 认字那条腿这一拍回「0 字」（真机 23:37:05）
        offers_before = len(offers)
        service.on_foreground(BACK_PROC)
        check("前置：这一拍真回到了聊天窗口",
              service._social_foreground is True, repr(service._social_foreground))
        check("★ 切回那一拍**布了防**，上界就是 `PEER_HOLD_MAX_S`",
              service._peer_hold_until == clock["t"] + PEER_HOLD_MAX_S,
              repr(service._peer_hold_until))
        check("★ 切回那一拍没当场多冒一句",
              len(offers) == offers_before, f"{offers_before} → {len(offers)}")

        _grab_then_sync(121)          # 切回之后那几拍：还在「认不出」的窗口里
        _reader()
        check("★★ 这一拍名字**真读不到**（真机那句「0 字」，退回会话标识那一档）",
              service._peer_name == "", repr(service._peer_name))
        check("★★ 档案例外不串格：材料 / 提炼吃的还是「这一拍读到的名字」（空）",
              service._peer_name == "" and app._social_peer_band_hint == "",
              f"{service._peer_name!r} / {app._social_peer_band_hint!r}")
        check("★★ 键**没退化**：还是 `微信|茗风`（修前这里是裸「微信」）",
              _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        check("★★ 这一场判成还在帮聊里（修前 False）",
              service._is_helping() is True, repr(_key()))

        # 冷却这一道必须**照旧放行**，能挡下它的才只可能是帮聊那一格。这一场还没冒过话
        # （`_last_asked` 的 `None` 哨兵，v2-social-due-sentinel）——**不推钟**：兜那一格
        # 的上界只有 10 分钟，推过 30 分钟冷却等于顺手把兜也推没了，那样这一支就白验了。
        check("★ 冷却这一道是**放行**的（这一场还没冒过话，所以能挡下它的只可能是帮聊那一格）",
              service._last_asked is None and service._due() is True,
              repr(service._last_asked))
        check("★★ 「列表 → 单聊」跃迁那一拍**不再重复冒**（真机 23:37:05,658 那一句）",
              _knock() is False, f"{len(offers)} 次")
        service._awaiting = False
        service._running = False
        check("★★ 直接招呼 `_ask_offer` 那一拍同样不冒",
              service._ask_offer(BACK_PROC) is False, f"{len(offers)} 次")
        check("★★ 这一场从头到尾只有一格戳（没按裸标识另开新格）",
              set(service._helping) == {f"{MAIN_WINDOW}|{PEER_A}"},
              repr(sorted(service._helping)))
        check("★★ 落戳那一格也没被换掉（挂问期间换键那种事没发生）",
              f"{MAIN_WINDOW}|{PEER_A}" not in service._last_asked_by_window
              or service._asked_key != MAIN_WINDOW,
              repr(service._asked_key))

        # ---------- ④ 认出来就换 / 真换场照样作废 ----------

        section("④ 名字一认回来：兜的那一格当场作废；真换了人就照旧退回裸标识")

        monitor.band_rows = [BAND_ROW]
        _grab_then_sync(200)
        _reader()
        check("★ 认回来还是茗风 → 键仍是 `微信|茗风`",
              _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        check("★ 认出名字那一刻**兜的那一格当场作废**（`_peer_hold_until` 归 0）",
              service._peer_hold_until == 0.0, repr(service._peer_hold_until))
        check("★ 这一场一直判在帮聊里（认字前后不断档）",
              service._is_helping() is True, repr(_key()))

        monitor.band_rows = [BAND_ROW_B]      # 真换了人（带子变了、标识没变）
        _grab_then_sync(130)
        check("★ 换场那一拍名字当场作废、键退回裸标识（老口径）",
              service._peer_name == "" and _key() == MAIN_WINDOW, repr(_key()))
        _reader()
        check("★ 阿澄认出来 → 键成了 `微信|阿澄`",
              _key() == f"{MAIN_WINDOW}|{PEER_B}", repr(_key()))
        check("★ 阿澄没点过 [好呀] → 不替他拿主意（茗风那一格原样留着）",
              service._is_helping() is False
              and f"{MAIN_WINDOW}|{PEER_A}" in service._helping,
              repr(sorted(service._helping)))

        # ---------- ⑤ 兜有上界 ----------

        section("⑤ 兜是有上界的：过了 `PEER_HOLD_MAX_S` 就照老口径退回裸标识")

        clock["t"] += 10.0
        service.on_foreground(OUT_PROC)
        frame_shade["v"] = 140
        monitor.band_rows = []
        clock["t"] += 5.0
        service.on_foreground(BACK_PROC)
        check("前置：又布了一次防（上界跟着这一拍算）",
              service._peer_hold_until == clock["t"] + PEER_HOLD_MAX_S,
              repr(service._peer_hold_until))
        _grab_then_sync(140)          # 切回那一拍抓屏：带子变了 → 名字那一格当场清掉
        _reader()                     # 认字这条腿这一拍回的又是「0 字」
        check("前置：这一阵键还兜在 `微信|阿澄` 上",
              service._peer_name == "" and _key() == f"{MAIN_WINDOW}|{PEER_B}",
              repr(_key()))
        clock["t"] += PEER_HOLD_MAX_S + 1.0          # 上界过了
        _grab_then_sync(141)
        check("★ 上界一过「认不出」就照老口径办：键退回裸标识（不无限期挂着上一个人的名字）",
              service._peer_name == "" and _key() == MAIN_WINDOW, repr(_key()))
        check("★ 那一格也一并清掉（不留在内存里过夜）",
              service._peer_key_name == "", repr(service._peer_key_name))

        # ---------- ⑥ 没离开过前台时，老口径一个字没动 ----------

        section("⑥ 边界（jitter 那一批留下的）：没离开过前台，读不到名字照旧退回裸标识")

        monitor.band_rows = [BAND_ROW]
        _grab_then_sync(200)
        _reader()
        check("前置：这一场又认出茗风",
              _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        check("前置：没切过前台 → 兜的那一格没布防",
              service._peer_hold_until == 0.0, repr(service._peer_hold_until))
        monitor.band_rows = []
        _grab_then_sync(201)               # 还在这一场里，只是带子那几行像素动了
        service._sync_session()
        check("★ 没离开过前台、名字又读不到 → 键照旧退回裸标识（老行为，`宁可清也不串名`）",
              service._peer_name == "" and _key() == MAIN_WINDOW, repr(_key()))
        check("★ 兜的那一格也没被喂上（布防只认「切回来」那一拍，不是「读不到」）",
              service._peer_key_name == "" and service._peer_hold_until == 0.0,
              repr(service._peer_key_name))

        # ---------- ⑦ 源码级 + 红线 ----------

        section("⑦ 源码级：布防挂在哪、只喂一把键、既有口径一个字没动")

        fg_src = _code_of(SocialReplyService.on_foreground)
        lines = textwrap.dedent(fg_src).splitlines()

        def _next_code(idx: int) -> str:
            for line in lines[idx + 1:]:
                if line.strip() and not line.strip().startswith("#"):
                    return line.strip()
            return ""

        gate = "if not was_social and self._social_foreground:"
        check("★ 源码级：布防挂在「走出去了、又切回社交前台」那一拍上",
              gate in lines, "")
        check("★ 源码级：布防那一句就在这道闸下（不是无条件、也不是挂在别的闸下）",
              _next_code(lines.index(gate)) == "self._arm_peer_hold()",
              _next_code(lines.index(gate)))
        check("★ 源码级：布防在 `on_foreground` 里只有这一处调用",
              fg_src.count("self._arm_peer_hold()") == 1,
              str(fg_src.count("self._arm_peer_hold()")))
        # 既有口径（v2-social-grab-whitelist）：触发那一层**不碰钟**——`monotonic` / `time.` /
        # `clock(` 连注释里都不许出现。所以读钟那一句摆在 `_arm_peer_hold()` 里。
        for tok in ("monotonic", "time.", "clock("):
            check(f"★ 源码级：`on_foreground` 不碰 `{tok}`（那一层的既有口径没被这一批撑开）",
                  tok not in fg_src, "")
        arm_src = inspect.getsource(SocialReplyService._arm_peer_hold)
        check("★ 源码级：读钟那一句在新开的 `_arm_peer_hold()` 里，上界就是常量",
              "self._peer_hold_until = self._clock() + PEER_HOLD_MAX_S" in arm_src, "")
        check("★ 源码级：走出聊天窗口那一段（`was_social and not ...`）里不碰这两格",
              "self._peer_hold_until" not in fg_src.split(gate)[0].split(
                  "if was_social and not self._social_foreground:")[-1], "")

        key_src = inspect.getsource(SocialReplyService._cooldown_key)
        check("★ 源码级：取键还是**先看 `_peer_name`**（老口径），兜的那一格只是兜底",
              "self._peer_name" in key_src and "_peer_key_name" in key_src
              and "self._peer_hold_until" in key_src, "")

        sync_src = inspect.getsource(SocialReplyService._sync_session)
        check("★ 源码级：对象名跟标识同一拍刷那一行原样（没被拆）",
              "self._peer_name = self._normalize_listen(" in sync_src, "")
        check("★ 源码级：换人清记忆那条判据照旧只看标识（返回值一个字没改）",
              "previous == ident" in sync_src
              and "self._forget_session_memory()" in sync_src, "")

        # **只喂一把键**：全类扫一遍，认得 `_peer_key_name` 的只许是这四处。
        blocks: dict[str, list[str]] = {}
        cur = "__head__"
        for line in inspect.getsource(SocialReplyService).splitlines():
            m = re.match(r"    def (\w+)", line)
            if m:
                cur = m.group(1)
            blocks.setdefault(cur, []).append(line)
        users = {name for name, body in blocks.items()
                 if any("_peer_key_name" in ln for ln in body)}
        check("★★ 红线：兜住那一格**只喂一把键**——认得它的只有 __init__ / `_sync_session` / "
              "`_cooldown_key` / `on_foreground`（档案注入 / 提炼器一个字都不许提它）",
              users == {"__init__", "_sync_session", "_cooldown_key", "on_foreground"},
              repr(sorted(users)))
        check("★★ 红线：档案注入那条腿照旧只认 `_peer_name`（不端上一个人的档案）",
              "self._peer_name" in inspect.getsource(SocialReplyService._peer_profile_block)
              and "_peer_key_name" not in inspect.getsource(SocialReplyService._peer_profile_block),
              "")
        check("★★ 红线：提炼器那条腿照旧只认 `_peer_name`",
              "self._peer_name" in inspect.getsource(SocialReplyService._note_peer_turns)
              and "_peer_key_name" not in inspect.getsource(SocialReplyService._note_peer_turns),
              "")

        check("★ 常量：兜的上界是 600 秒（10 分钟）", PEER_HOLD_MAX_S == 600.0, repr(PEER_HOLD_MAX_S))
        check("★ 红线：`is_personal_kind(\"\")` 还是按「单聊」算",
              __import__("life.screen.social", fromlist=["is_personal_kind"]).is_personal_kind("")
              is True, "")
        check("★ 红线：`_is_helping()` 还是「眼下这一场的键在不在那本账里」",
              _code_of(type(service)._is_helping).strip()
              == "return self._cooldown_key() in self._helping",
              repr(_code_of(type(service)._is_helping).strip()))
        check("★ 红线：会话标识 / 对象名那两条腿还是装配层那两只手（这一批没碰 bootstrap）",
              "core/bootstrap.py" not in inspect.getsource(SocialReplyService._sync_session), "")
        check("★ 红线：装配层那两侧一个字没动（`_sync_session` 里没有新接的手）",
              "_social_peer_band" not in key_src and "_social_peer_band" not in sync_src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
    finally:
        fgmod.foreground_title = real_title
        fgmod.foreground_pid = real_pid
        app._proc_name_of = real_proc
        app._social_pet_masks = real_masks
        shot.grab_foreground_window = real_grab
        app.screen_monitor = real_monitor
        drain_threads()
        box.cleanup()

    return summary("切应用上网再切回来，不再重问「要不要帮你回」"
                   "（v2-social-switch-reask-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
