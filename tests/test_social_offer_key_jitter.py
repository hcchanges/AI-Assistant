"""进帮聊后每回合重复冒 offer：同场名字不再每帧抖动（v2-social-c1-offer-key-jitter）。

**只读跟踪定下的根因**（`data/logs/pet.log` 2026-10-11 18:13~18:15 那一段）：

```
_grab_social_chat  每一拍抓屏**无条件**清 `_social_peer_band_hint = ""`（修前那一行）
_read_social_chat  后台线程认出名字才填回去（真机一趟 2.1~4.8 秒）
_poll_listen       抓完屏**紧接着**就 `_sync_session()`——那一刻名字这一格正是空的
_cooldown_key()    = `_session_id`（+ `|对象名`）⇒ 同一场里那把键在「微信|茗风」与
                   「微信」之间每拍抖一次
_is_helping()      按前一把的键盖的戳、按后一把的键来问 ⇒ 判 False
_ask_offer         「非单聊 → 单聊」跃迁那一拍放行 ⇒ **又冒一遍"要不要帮你回"**
```

真机佐证：18:13:30 点了 [好呀]（戳盖在 `微信|茗风` 上），18:14:32,340 又冒了一句
「社交软件在前台（Weixin.exe），冒一句要不要帮忙措辞」——那一拍 `_due()` 读的是
「微信」这一格（从没冒过）才放的行，正是键被那一次无条件清空打散了。

**主修**（`core/bootstrap.py`）：拿掉那句无条件清空，改成**只在换场那一拍清**，判据是
标题带**自身的指纹**（`_peer_band_changed`）：指纹一样 = 同一场 ⇒ 不清（本场沿用上次认到
的名字），指纹变了 = 换了场 ⇒ 清（宁可清也不串名）。合并窗里 `_session_id` 恒是"微信"、
不随对象走，所以**只能**看带子——拿标识变更判换场等于永远判不出换场。

这一支钉两件事：

- **T1 换场不串名 + 新场首问**（① ②）：同一扇主窗里从茗风换到阿澄（标识一个字没变、
  只有带子变了）⇒ 名字当场作废、键退回裸标识；阿澄自己那一场该冒就冒，不沿用茗风那份
  帮聊态；两人的键各占一格、互不覆盖。
- **T2 同场不重冒**（③）：同一场里连着几拍（带子一模一样）⇒ 名字一直是茗风、键一直是
  `微信|茗风`、`_is_helping()` 一直 True ⇒ 每一次「列表 → 单聊」跃迁都被挡下，**一句都
  不再重复冒**（修前每一拍都冒）。其中两拍专门摆成"**这一拍重新认字且认不出来**"
  （清掉认字缓存、`band_rows` 给空——真机那句「0 字（没认出来）」），要的还是那个结论。

**边界（如实钉在 ④）**：这条口径是"指纹变 = 换场"，**宁可清**。真机上同一场里带子本身
会变（宠物气泡一挡，`_chat_left` 在 92 / 254 之间跳，见 18:13:47~18:14:20 那几十行），
那一段名字照旧会被清空——这一段的收口留给下一批次（`_is_helping()` 那条闸按"这一场"判，
而不是按"这一拍的名字"判）。本批次按指令只做主修。

红线：零新依赖（`hashlib` / QImage / numpy 全现成）；不碰 config.yaml / .vscode；
`pet.db` / `mind.db` 零写入；`life/screen/social.py` **一个字没动**——`_is_helping()` /
`_due()` / `is_personal_kind("")` / `NON_PERSONAL_PAGES` / `_cooldown_key()` 全是既有口径。
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

import monitor.foreground as fgmod  # noqa: E402
from core.bootstrap import DesktopPetApp  # noqa: E402
from life.screen import shot  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_LIST,
    PAGE_PERSONAL,
    SocialReplyService,
)

MAIN_WINDOW = "微信"           # 主窗那个恒定的标题（合并窗里换会话它一个字都不变）
PEER_A = "茗风"                # 第一场：点过 [好呀] 的那一场
PEER_B = "阿澄"                # 第二场：换过去的那一场（带子变了、标识没变）
FRAME_W, FRAME_H = 742, 1342   # 真机那扇主窗的帧尺寸（见 pet.log 那句「帧 742x1342」）
BAND_ROW = ((60, 8, 110, 30), PEER_A)      # 带子上最左那一行就是名字（真机那一行的位置）


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
    """假 monitor：**小图**（那条标题带）认成 `band_rows`，大图（聊天区）认成 `chat_rows`。

    带子只有几十像素高（真机 674x34），聊天区是几百像素高，按第一维分是谁足够稳。
    """

    def __init__(self) -> None:
        self.band_rows: list = []
        self.chat_rows: list = [((40, 60, 120, 76), "在的，你说")]
        self.band_calls = 0

    def detect_screen_boxes(self, array):
        return []                    # 判不出列表 → 照旧认字（漏判零回归那一档）

    def read_screen_lines(self, array):
        shape = tuple(getattr(array, "shape", ()))
        if len(shape) == 3 and shape[0] <= 100:      # 那条带子（几十像素高）
            self.band_calls += 1
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
    service._last_proc = "Weixin.exe"
    # 会话标识 / 对象名那两条腿接的是装配层**真**方法（`_social_session_id` /
    # `_social_peer_id`），测试不去替换它们——替换了就等于自己把要验的那条链剪断。
    app._social_session_hint = MAIN_WINDOW
    app._social_peer_hint = ""

    monitor = _FakeMonitor()
    real_monitor = app.screen_monitor
    app.screen_monitor = monitor

    # ---- 前台猴：社交软件在前台（不是宠物自己），抓屏那一套才走得通 ----
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
    app._proc_name_of = lambda pid: "Weixin.exe"
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
        service._sync_session()      # 认字回调 `_on_screen_read` 里补的那一次

    def _knock() -> bool:
        """从会话总列表点进单聊那一拍——走真实的 `note_page_kind` 跃迁闸。"""
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
        # ---------- ① 起手：主窗里认出「茗风」，戳盖在那一格上 ----------

        section("① 起手：带子认出「茗风」→ 键 `微信|茗风`，点 [好呀] 的戳盖在这一格上")

        arr = _grab_then_sync(200)
        check("★ 抓屏那一拍真跑到底了（回来一个数组）",
              arr is not None, type(arr).__name__ if arr is not None else "None")
        check("★ 第一拍就裁下了那条标题带（主窗才裁：标题腿是空的）",
              app._social_peer_band is not None,
              type(app._social_peer_band).__name__)
        check("★ 抓屏那一拍没有无条件把名字清掉（这一格本来就还没认过人）",
              app._social_peer_band_hint == "", repr(app._social_peer_band_hint))
        check("名字还没认出来 → 键就是裸标识（老口径那一条降级照旧）",
              _key() == MAIN_WINDOW, repr(_key()))

        monitor.band_rows = [BAND_ROW]
        _reader()
        check("★ 那条带子真送进了认字那条腿", monitor.band_calls >= 1, str(monitor.band_calls))
        check("★ 后台认字那一拍把名字落进了装配层那一格",
              app._social_peer_band_hint == PEER_A, repr(app._social_peer_band_hint))
        check("★ 键成了 `微信|茗风`", _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        check("★ 标识还是主窗那个恒定的应用名（真机那句「这一场标识 2 字」）",
              service._session_id == MAIN_WINDOW, repr(service._session_id))

        service._mark_helping()                                 # 用户点了 [好呀]
        check("★ 帮聊那一戳盖在 `微信|茗风` 上",
              set(service._helping) == {f"{MAIN_WINDOW}|{PEER_A}"},
              repr(sorted(service._helping)))

        # ---------- ② T1 换场不串名 + 新场首问 ----------

        section("② T1 换场：标识一个字没变、只有带子变了 → 名字当场作废、阿澄首问重冒")

        _grab_then_sync(120)                       # 同一扇主窗，标题带上那一行换了人
        check("★ 换场那一拍的名字**当场作废**（不是留着上一场那一行）",
              app._social_peer_band_hint == "", repr(app._social_peer_band_hint))
        check("★★ 服务那一格不再端着上一个名字（本批次最怕的那条**串名回归**）",
              service._peer_name != PEER_A, repr(service._peer_name))
        check("★ 键退回裸标识（这一场还没认出来）", _key() == MAIN_WINDOW, repr(_key()))
        check("★ 换场判据**不是**标识变更：`_session_id` 一个字都没变",
              service._session_id == MAIN_WINDOW, repr(service._session_id))
        check("★ 茗风那一格的帮聊戳原样留着（换场只清名字，不抹别人的账）",
              f"{MAIN_WINDOW}|{PEER_A}" in service._helping,
              repr(sorted(service._helping)))
        check("★ 划到新一场，茗风那份帮聊态**不跟过来**（键都换了，戳自然不命中）",
              service._is_helping() is False, repr(_key()))

        check("★ 阿澄这一场第一问**重新冒一次**（不沿用茗风那份帮聊态）",
              _knock() is True, f"{len(offers)} 次")
        check("★ 落戳落在裸标识那一格上（阿澄还没认出来）",
              service._asked_key == MAIN_WINDOW, repr(service._asked_key))

        monitor.band_rows = [((60, 8, 110, 30), PEER_B)]
        _grab_then_sync(120)                       # 换场之后那几拍：带子还是阿澄这条
        _reader()
        check("★ 阿澄的名字认出来了 → 键成了 `微信|阿澄`",
              _key() == f"{MAIN_WINDOW}|{PEER_B}", repr(_key()))
        check("★ 两个人的键各占一格、互不覆盖（`微信|茗风` 仍在）",
              f"{MAIN_WINDOW}|{PEER_A}" in service._helping,
              repr(sorted(service._helping)))
        check("★ 阿澄这一场没点过 [好呀] → 不自动替他拿主意",
              service._is_helping() is False, repr(_key()))
        # 阿澄认出来之后账就落在他**自己**那一格上。这一拍放行是他那一格从没冒过
        # （`_due()` 的既有一格一账，v2-social-cooldown-per-window），跟本批次要修的
        # 「同一场里键来回抖」是两回事——要钉的是**账没落到茗风那一格**上。
        clock["t"] += 60.0
        check("★ 阿澄认出来之后再跃迁 → 冒的是他**自己**那一格的新账",
              _knock() is True and service._asked_key == f"{MAIN_WINDOW}|{PEER_B}",
              f"{len(offers)} 次 / {service._asked_key!r}")
        check("★★ 账**没落到茗风那一格**上（换场之后谁也不替谁记账）",
              f"{MAIN_WINDOW}|{PEER_A}" not in service._last_asked_by_window,
              repr(sorted(service._last_asked_by_window)))

        # ---------- ③ T2 同场不重冒 ----------

        section("③ T2 同场：带子一模一样连着几拍 ⟶ 名字不抖、键不抖、一句不再重复冒")

        monitor.band_rows = [BAND_ROW]
        _grab_then_sync(200)                       # 回到茗风那一场（带子又是这一条）
        _reader()
        check("前置：回到茗风那一场，键是 `微信|茗风`",
              _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
        service._mark_helping()                    # 用户点过 [好呀] 这件事重新摆好
        clock["t"] += 31 * 60                      # 冷却早过了：挡下它的必须是帮聊那一格
        knock_before = len(offers)
        check("★ 这一拍冷却**是放行的**（所以能挡下它的只可能是帮聊那一格）",
              service._due() is True, f"{len(offers)} 次")

        rounds = [
            (False, True, "同一条带子、认字缓存在（这一拍直接命中）"),
            (True, False, "同一条带子、**这一拍重新认字且认不出来**（真机那句「0 字」）"),
            (True, False, "同一条带子、又一次没认出来"),
            (False, True, "同一条带子、又认得出来了"),
        ]
        for i, (blow_cache, recognized, label) in enumerate(rounds, start=1):
            monitor.band_rows = ([BAND_ROW] if recognized else [])
            _grab_then_sync(200)                   # 同一帧 → 带子一模一样（指纹不变）
            check(f"★ 第 {i} 拍（{label}）：抓屏那一拍之后名字这一格**没被清掉**",
                  app._social_peer_band_hint == PEER_A,
                  repr(app._social_peer_band_hint))
            check(f"★ 第 {i} 拍：键仍旧是 `微信|茗风`（修前这里退回裸「微信」）",
                  _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
            if blow_cache:
                app._social_peer_band_md5 = ""      # 认字缓存清掉 → 这一拍不得不重认
            _reader()
            check(f"第 {i} 拍：认字之后名字这一格还在（认不出来时那一格**一个字不动**）",
                  app._social_peer_band_hint == PEER_A, repr(app._social_peer_band_hint))
            check(f"第 {i} 拍：认字之后键还是 `微信|茗风`",
                  _key() == f"{MAIN_WINDOW}|{PEER_A}", repr(_key()))
            check(f"第 {i} 拍：这一场判成在帮聊里", service._is_helping() is True, "")
            check(f"★ 第 {i} 拍：「列表 → 单聊」跃迁那一拍**不再重复冒**",
                  _knock() is False, f"{len(offers)} 次")

        check("★★ 连着四拍跃迁，一句都没多冒（修前每拍一句）",
              len(offers) == knock_before, f"{knock_before} → {len(offers)}")
        check("★ 这一场从头到尾只有一格戳（没有按每拍的名字另开新格）",
              set(service._helping) == {f"{MAIN_WINDOW}|{PEER_A}"},
              repr(sorted(service._helping)))

        # ---------- ④ 边界（如实钉）：带子本身一变就清，跟"是不是真换了人"无关 ----------

        section("④ 边界（如实钉）：同一场里带子**本身**一变就清——这一段留给下一批次")

        check("前置：名字在格子里",
              app._social_peer_band_hint == PEER_A, repr(app._social_peer_band_hint))
        _grab_then_sync(201)                       # 还是同一个人，只是那一带像素动了
        check("★ 带子一变就清（本条口径是「指纹变 = 换场」，**宁可清也不串名**）",
              app._social_peer_band_hint == "", repr(app._social_peer_band_hint))
        check("★ 键跟着退回裸标识（真机上宠物气泡一挡就是这么一段）",
              _key() == MAIN_WINDOW, repr(_key()))
        check("★ 但戳没丢：茗风那一格的帮聊态还在，名字一认回来就命中",
              service._is_helping() is False and f"{MAIN_WINDOW}|{PEER_A}" in service._helping,
              repr(sorted(service._helping)))
        monitor.band_rows = [BAND_ROW]
        _reader()
        check("★ 名字一认回来，键就回到 `微信|茗风`、帮聊那一格当场生效",
              _key() == f"{MAIN_WINDOW}|{PEER_A}" and service._is_helping() is True,
              f"{_key()!r} / helping={service._is_helping()}")

        # ---------- ⑤ 源码级 + 红线 ----------

        section("⑤ 源码级：那道无条件清空没了 / 两个指纹槽各存各的 / 既有口径一个字没动")

        grab_code = _code_of(DesktopPetApp._grab_social_chat)
        check("★ 源码级：抓屏那一拍认得那把换场闸（只看带子指纹）",
              "_peer_band_changed(self._social_peer_band)" in grab_code, "")
        check("★ 源码级：清名字只剩两处，**各自挂在一道闸下**（认不出带子 / 换了场）",
              grab_code.count('self._social_peer_band_hint = ""') == 2,
              str(grab_code.count('self._social_peer_band_hint = ""')))

        lines = textwrap.dedent(grab_code).splitlines()

        def _next_code(idx: int) -> str:
            for line in lines[idx + 1:]:
                if line.strip() and not line.strip().startswith("#"):
                    return line.strip()
            return ""

        none_at = lines.index("if self._social_peer_band is None:")
        check("★ 源码级：第一处清名字挂在「这一拍没有可读的带子」那道闸下",
              _next_code(none_at) == 'self._social_peer_band_hint = ""', _next_code(none_at))
        changed_at = lines.index("elif self._peer_band_changed(self._social_peer_band):")
        check("★ 源码级：第二处清名字挂在「带子变了 = 换场」那道闸下",
              _next_code(changed_at) == 'self._social_peer_band_hint = ""',
              _next_code(changed_at))

        change_src = _code_of(DesktopPetApp._peer_band_changed)
        check("★ 源码级：换场那把闸认的是**带子自身的 md5**",
              "hashlib.md5(bytes(band.constBits())).hexdigest()" in change_src, "")
        check("★★ 源码级：判换场那一格跟**后台认字**那格各存各的（绝不写认字缓存）",
              "_social_peer_band_md5" not in change_src
              and "_social_peer_band_seen_md5" in change_src, "")
        check("★ 源码级：指纹只当「一样 / 不一样」用——不落盘、不进日志、不记名字",
              "logger" not in change_src and "open(" not in change_src, "")

        name_src = _code_of(DesktopPetApp._social_peer_name_from_band)
        check("★ 后台认字那条腿原样：还是它自己那格缓存 + 仍旧只记长度",
              "_social_peer_band_md5" in name_src
              and "len(name)" in inspect.getsource(DesktopPetApp._social_peer_name_from_band),
              "")
        check("★ 认字那一拍仍旧「认出来才填」（认不出来一个字不动）",
              "if name:" in _code_of(DesktopPetApp._read_social_chat), "")

        check("★ `_social_peer_band_seen_md5` 是新开的一格（跟认字缓存不是一个属性）",
              hasattr(app, "_social_peer_band_seen_md5")
              and app._social_peer_band_seen_md5 != app._social_peer_band_md5, "")

        iso = __import__("life.screen.social", fromlist=["is_personal_kind"]).is_personal_kind
        check("★ 红线：`is_personal_kind(\"\")` 还是按「单聊」算（这条口径没动）",
              iso("") is True, repr(iso("")))
        due_src = _code_of(type(service)._due)
        check("★ 红线：`_due()` 的 `None → True` 哨兵一个字没动",
              "if last is None:" in due_src and "return True" in due_src, "")
        check("★ 红线：`_is_helping()` 还是「眼下这一场的键在不在那本账里」",
              _code_of(type(service)._is_helping).strip()
              == "return self._cooldown_key() in self._helping",
              repr(_code_of(type(service)._is_helping).strip()))
        key_src = inspect.getsource(type(service)._cooldown_key)
        check("★ 红线：那把键**不认识**标题带（喂给它的还是会话标识 + 对象名）",
              "_social_peer_band" not in key_src, "")

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

    return summary("进帮聊后每回合重复冒 offer：同场名字不再每帧抖动"
                   "（v2-social-c1-offer-key-jitter）")


if __name__ == "__main__":
    raise SystemExit(main())
