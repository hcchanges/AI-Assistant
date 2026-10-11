"""合并窗下从**标题带**取对象名，喂进冷却那把键的 peer 通道（v2-social-peer-by-titlebar）。

**上一节点（#7 / v2-social-cooldown-by-peer）留下的那半场**：`_cooldown_key()` 已经是
`f"{会话标识}|{对象名}"`，可主窗合并模式下窗口标题恒是应用名（真机 `pet.log.1` 10:26:33
到 10:40:56 那几十行「这一场标识 2 字」），那条腿认不出人 → 所有单聊仍共用一格。对象名其实
在屏上：它长在标题栏那一行（真机 742×1342 那批帧上量得 y≈70-72、x≈121-170），而那一行**正好
落在 `chat_area_box` 上沿（`TITLE_BAR_PX`(56) × dpr(1.5) = 84）的外面**。

这一节点**不动裁剪几何**（B 案），另开一条只读的**取证带**：

```
_grab_social_chat   裁下那条带子（只在"标题腿认不出人"时裁）   core/bootstrap._social_peer_band
_read_social_chat   后台线程认那一行字、过滤成名字             _social_peer_band_hint
_social_peer_id     两条腿：带子名字优先，退回窗口标题腿        core/bootstrap
_sync_session       认字那一拍再刷一次（带子要等 OCR 才有结果）  life/screen/social
_cooldown_key       f"{sid}|{peer}"                            （#7 原样不动）
```

**名字绝不进材料**是结构性的：带子跟喂给 `classify_lines` 的那张聊天区图是两块——带子的下沿
跟 `chat_area_box` 的上沿**逐像素对齐**（`title_band_box` 里那两条边），带子里的像素一个都
不在聊天区里，所以名字到不了 `ChatRead.lines` / `raw` / `groups` / `_tail_context` / `_chat_text`
/ `_auto_generate`。测试里⑤两路都钉（行为级 + 源码级）。

红线：零新依赖（`hashlib` / numpy / QImage 现成）；不碰 config.yaml / .vscode；不写 pet.db /
mind.db；`chat_area_box` 等裁剪几何一行不改；b9b6caf 的 `looks_like_list_boxes` 输入数组不动；
584c707 / 2afc76d / e64700f / b9b6caf / afebdcb / #6 / #7 既有语义全不动。

**真机缺口（如实记在这里，报告 §183 也写了）**：手上没有搜索态的帧，「含搜索即丢」那一条
没在真机搜索态上复验过；另外真机那批帧上 `_chat_left` 逐帧在 92 / 164 / 263 / 465 之间跳，
`_chat_left` 落到 164 那几帧上名字那一行（x≈121）在带子**左边**、被左切挡在带子外，那几帧
认不出名字、退回裸会话标识。
"""

from __future__ import annotations

import inspect
import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary  # noqa: E402

from core.bootstrap import (  # noqa: E402
    _band_leftmost_text,
    _peer_name_from_band_text,
)
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_GROUP,
    PAGE_LIST,
    PAGE_PERSONAL,
    SocialReplyService,
)
from life.screen.social_window import (  # noqa: E402
    TITLE_BAND_PX,
    TITLE_BAR_PX,
    chat_area_box,
    title_band_box,
)

MAIN_WINDOW = "微信"          # 主窗那个恒定的标题（真机日志里报的「2 字」）
BAND_W, BAND_H = 300, 40      # 合成那条取证带的像素尺寸（只是给假 monitor 认形状用）


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class _FakeMonitor:
    """假 monitor：两件事——把**带子**认成几行字、把**聊天帧**认成几行字。

    按数组形状分是谁（带子 40×300、聊天帧 300×400），顺带把每次调用的形状记下来，
    好验"b9b6caf 的 `looks_like_list_boxes` 输入数组没动"（它只看 `detect_screen_boxes`
    收到的那一份）与"带子像素没并进聊天帧"。
    """

    def __init__(self) -> None:
        self.band_rows: list = []
        self.chat_rows: list = []
        self.band_shape = (BAND_H, BAND_W, 3)
        self.band_calls = 0
        self.detect_shapes: list = []
        self.read_shapes: list = []

    def detect_screen_boxes(self, array):
        self.detect_shapes.append(tuple(array.shape))
        return []                    # 判不出来 → 照旧认字（漏判零回归那一档）

    def read_screen_lines(self, array):
        shape = tuple(array.shape)
        self.read_shapes.append(shape)
        if shape == self.band_shape:
            self.band_calls += 1
            return list(self.band_rows)
        return list(self.chat_rows)


class _Catch(logging.Handler):
    """抓 `core.bootstrap` 那几行 DEBUG，验"日志只出长度、不出名字"。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.lines.append(record.getMessage())
        except Exception:      # noqa: BLE001 —— 这是取证，不该把被测那条线带崩
            pass


def main() -> int:
    from PySide6.QtGui import QColor, QImage
    import numpy as np

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
    # 前台标题就是主窗那个恒定的应用名——这一节点要解的那一半。两条 provider 都是
    # 装配层在 `__init__` 里接的**真**方法（`_social_session_id` / `_social_peer_id`），
    # 测试不去替换它们，替换了就等于自己把要验的那条链剪断了。
    app._social_session_hint = MAIN_WINDOW
    app._social_peer_hint = ""

    monitor = _FakeMonitor()
    real_monitor = app.screen_monitor
    app.screen_monitor = monitor

    def _band_image(shade: int = 200) -> QImage:
        img = QImage(BAND_W, BAND_H, QImage.Format_RGB888)
        img.fill(QColor(shade, shade, shade))
        return img

    def _frame():
        frame = np.full((300, 400, 3), 250, dtype=np.uint8)
        return frame

    def _read(chat_rows: list) -> tuple[object, object]:
        """走**真的** `_read_social_chat`（带子进门就取走、页型判完才认名字）。"""
        monitor.chat_rows = chat_rows
        return app._read_social_chat(_frame())

    def _knock() -> bool:
        """从会话总列表点进单聊那一拍——走真实的 `note_page_kind` 跃迁闸。"""
        service._awaiting = False
        service._running = False
        service._page_kind = PAGE_LIST
        app.chat_bubble.hide_offer()
        before = len(offers)
        service.note_page_kind(PAGE_PERSONAL)
        return len(offers) - before == 1

    def _read_and_sync(chat_rows: list) -> object:
        """认字那一拍：`_read_social_chat` 认名字，回调里补的那次 `_sync_session()` 刷进服务。

        把页型先摆成"已经在单聊里"：回调末尾那一报就不构成跃迁，不会顺手冒一句、把冷却
        账本按上——冒话本身在 `_knock()` 里单独验，两件事分开才看得清。
        """
        read = _read(chat_rows)
        service._page_kind = PAGE_PERSONAL
        service._on_screen_read(read)
        return read

    # 单聊那一屏认出来的一行（页型判 `PAGE_PERSONAL`、`looks_like_list_page` 不命中）
    PERSONAL_ROWS = [((40, 60, 120, 76), "在的，你说")]
    GROUP_ROWS = [((40, 60, 200, 76), "某某群 (25)")]

    catcher = _Catch()
    blog = logging.getLogger("core.bootstrap")
    blog_level_before = blog.level
    blog.setLevel(logging.DEBUG)
    blog.addHandler(catcher)

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 主窗换会话 → 键从「微信」变成「微信|茗风」 ----------

        section("① 主窗（标题腿认不出人）→ 带子认出名 → 键变 `微信|茗风`，换一场各占一格")

        monitor.band_rows = [((60, 8, 110, 30), "茗风")]      # 最左那一行就是名字
        app._social_peer_band = _band_image()
        read = _read(PERSONAL_ROWS)
        check("★ 带子那一趟真跑了（不是没走到认名字那一步）", monitor.band_calls == 1,
              f"{monitor.band_calls} 次")
        check("★ 名字落进了装配层那一格（`_social_peer_band_hint`）",
              app._social_peer_band_hint == "茗风", repr(app._social_peer_band_hint))
        check("★ `_social_peer_id()` 先看带子那条腿",
              app._social_peer_id() == "茗风", repr(app._social_peer_id()))

        # 认字那一拍的回调：它里面补的那一次 `_sync_session()` 才是把名字刷进服务的地方
        app._social_peer_band = _band_image()
        read = _read(PERSONAL_ROWS)
        service._page_kind = PAGE_PERSONAL
        service._on_screen_read(read)
        check("★ 服务那一格刷成了带子认出来的名字（E 刷新那一拍补上了）",
              service._peer_name == "茗风", repr(service._peer_name))
        check("★ 标识一个字没变——还是主窗那个恒定的标题（真机那句「这一场标识 2 字」）",
              service._session_id == MAIN_WINDOW, repr(service._session_id))
        check("★ 键成了 `微信|茗风`（#7 那半场在主窗里终于生效）",
              service._cooldown_key() == f"{MAIN_WINDOW}|茗风",
              repr(service._cooldown_key()))

        check("★ 茗风这一场冒了", _knock() is True, f"{len(offers)} 次")
        check("★ 落戳记在**茗风**那一格上",
              set(service._last_asked_by_window) == {f"{MAIN_WINDOW}|茗风"},
              repr(sorted(service._last_asked_by_window)))

        clock["t"] += 60.0
        check("★ 一分钟后再跃迁（还是茗风）→ 被冷却挡下", _knock() is False, f"{len(offers)} 次")

        # 换一场：同一扇主窗、标识仍不变，只有标题带上那一行变了
        monitor.band_rows = [((60, 8, 110, 30), "阿澄")]
        app._social_peer_band = _band_image(120)          # 像素也变了 → 指纹变了 → 重认
        _read_and_sync(PERSONAL_ROWS)
        check("★ 换了会话 → 键换了一格（`微信|阿澄`）",
              service._cooldown_key() == f"{MAIN_WINDOW}|阿澄",
              repr(service._cooldown_key()))
        check("★ 阿澄这一场没冒过 → 当场放行（复刻真机 10:37:50 那一拍）",
              _knock() is True, f"{len(offers)} 次")
        check("★ 两格各占各的、互不覆盖",
              set(service._last_asked_by_window) == {f"{MAIN_WINDOW}|茗风", f"{MAIN_WINDOW}|阿澄"},
              repr(sorted(service._last_asked_by_window)))

        clock["t"] = 1000.0 + 30 * 60 + 1
        monitor.band_rows = [((60, 8, 110, 30), "茗风")]   # 又切回茗风那一场
        app._social_peer_band = _band_image()
        app._social_peer_band_hint = ""
        service._peer_name = ""
        service._session_id = ""
        _read_and_sync(PERSONAL_ROWS)
        check("★ 切回茗风 → 键回到 `微信|茗风`（标识始终一个字没变）",
              service._cooldown_key() == f"{MAIN_WINDOW}|茗风",
              repr(service._cooldown_key()))
        check("★ 那一格已经占满 30 分钟 → 放行（冷却时长一个字没动）", _knock() is True,
              f"{len(offers)} 次")

        # ---------- ② 认不出 / 非单聊 / 取不到 → 退回裸标识 ----------

        section("② 带子认不出 / 这一屏不是单聊 / 压根没带子 → 逐字退回裸 `_session_id`")

        monitor.band_rows = []                    # 那一行一个字都没认出来
        app._social_peer_band = _band_image(90)
        app._social_peer_band_hint = ""
        _read_and_sync(PERSONAL_ROWS)
        check("★ 带子认不出 → 键 == 裸标识（一个字符都没多出来）",
              service._cooldown_key() == MAIN_WINDOW, repr(service._cooldown_key()))

        monitor.band_rows = [((60, 8, 110, 30), "茗风")]
        app._social_peer_band = _band_image(70)
        app._social_peer_band_hint = ""
        before_calls = monitor.band_calls
        read = _read(GROUP_ROWS)
        check("★ 这一屏判成群聊（页型真判出来了）", read.page_kind == PAGE_GROUP,
              repr(read.page_kind))
        check("★ 非单聊那一趟**不烧**带子的 OCR（省那几秒）",
              monitor.band_calls == before_calls, f"{monitor.band_calls} 次")
        check("★ 群聊那一场名字不落（群名不是「这一场是谁」）",
              app._social_peer_band_hint == "", repr(app._social_peer_band_hint))
        _read_and_sync(GROUP_ROWS)
        check("★ 非单聊 → 键仍是裸标识", service._cooldown_key() == MAIN_WINDOW,
              repr(service._cooldown_key()))

        app._social_peer_band = None              # 抓屏那一拍压根没裁（独立聊天窗走这支）
        app._social_peer_band_hint = ""
        monitor.band_rows = [((60, 8, 110, 30), "茗风")]
        before_calls = monitor.band_calls
        read = _read(PERSONAL_ROWS)
        check("★ 没带子那一趟也不烧 OCR", monitor.band_calls == before_calls,
              f"{monitor.band_calls} 次")

        # 独立聊天窗：标题腿自己有名字，带子腿空着 → 退回 #7 那条腿（老行为）
        app._social_peer_hint = "独立窗里的名字"
        app._social_peer_band_hint = ""
        service._page_kind = PAGE_PERSONAL
        service._on_screen_read(read)
        check("★ 带子腿空着时退回窗口标题那条腿（#7 原逻辑没动）",
              service._cooldown_key() == f"{MAIN_WINDOW}|独立窗里的名字",
              repr(service._cooldown_key()))
        app._social_peer_hint = ""

        app._social_peer_band_hint = ""
        app._social_peer_hint = ""
        service._session_id = ""
        service._peer_name = ""
        check("★ 两条腿都空 → 键是空串那一格（老口径：全场共用一格，不是「永不冷却」）",
              service._cooldown_key() == "", repr(service._cooldown_key()))
        app._social_session_hint = MAIN_WINDOW

        # ---------- ③ 标题带的几何 ----------

        section("③ `title_band_box` 几何：左沿同聊天区、下沿逐像素贴着聊天区上沿")

        canvas = QImage(742, 1342, QImage.Format_RGB888)
        canvas.fill(QColor(250, 250, 250))
        band_box = title_band_box(canvas, 1.5)
        chat_box = chat_area_box(canvas, 1.5)
        cut = int(TITLE_BAR_PX * 1.5)
        band_h = int(TITLE_BAND_PX * 1.5)
        check("★ 量得出来", band_box is not None, repr(band_box))
        check("★ 左沿就是聊天区的左沿（会话列表那一列挡在带子外）",
              band_box[0] == chat_box[0] and band_box[0] >= 0, f"{band_box} vs {chat_box}")
        check(f"★ 下沿 == 聊天区上沿（cut={cut}）——两块**不重叠**，所以名字进不了材料",
              band_box[1] + band_box[3] == cut and band_box[1] + band_box[3] == chat_box[1],
              f"{band_box} cut={cut}")
        check(f"★ 高度 == TITLE_BAND_PX({TITLE_BAND_PX}) × dpr = {band_h}",
              band_box[3] == band_h, repr(band_box[3]))
        check(f"★ 上沿 == cut - 带子高 = {cut - band_h}", band_box[1] == cut - band_h,
              repr(band_box[1]))

        check("★ 空画面量不出来 → None（调用方当这一帧没有名字）",
              title_band_box(QImage(), 1.5) is None, "")
        check("★ 1×1 那种切完没有面积的也回 None",
              title_band_box(QImage(1, 1, QImage.Format_RGB888), 1.5) is None, "")
        check("★ 缩放：dpr=2 时带子也跟着长（跟 `TITLE_BAR_PX` 同一条口径）",
              title_band_box(canvas, 2.0)[3] == int(TITLE_BAND_PX * 2), "")

        # ---------- ④ 名字过滤 ----------

        section("④ 那一行字 → 名字：应用名 / 搜索框 / 宠物名 / 纯数字 / 太长一律丢")

        check("★ 应用名（主窗那一行常常就是它）→ 丢",
              _peer_name_from_band_text("微信") == ""
              and _peer_name_from_band_text("WeChat") == ""
              and _peer_name_from_band_text("weixin") == "", "")
        check("★ 带未读数的应用名 → 抹掉未读数之后还是应用名 → 丢",
              _peer_name_from_band_text("微信(3)") == "", "")
        check("★ 真名字带着未读数 → 抹掉未读数留住",
              _peer_name_from_band_text("茗风(3)") == "茗风", "")
        check("★ 名字里本来就带括号的不误伤",
              _peer_name_from_band_text("项目（二期）") == "项目（二期）", "")
        check("★ 含「搜索」→ 丢（那是搜索框，不是名字）",
              _peer_name_from_band_text("搜索") == ""
              and _peer_name_from_band_text("搜索茗风") == "", "")
        check("★ 就是宠物自己 → 丢",
              _peer_name_from_band_text("团子", "团子") == ""
              and _peer_name_from_band_text("团子", "别的名") == "团子", "")
        check("★ 纯数字（未读角标 / 时间戳）→ 丢",
              _peer_name_from_band_text("12") == ""
              and _peer_name_from_band_text("3") == "", "")
        check("★ 一个字不成名字 → 丢", _peer_name_from_band_text("茗") == "", "")
        check("★ 太长（> 24 字）→ 丢",
              _peer_name_from_band_text("名字" * 13) == "", "")
        check("★ 行内空白压平（OCR 会把一个字拆成两框）",
              _peer_name_from_band_text("  茗  风 ") == "茗 风", "")
        check("★ 空 / None → 空串，不炸",
              _peer_name_from_band_text("") == ""
              and _peer_name_from_band_text(None) == "", "")

        check("★ 带子里取**最靠左**那一行（窗控 / 「更多」都在名字右边）",
              _band_leftmost_text([((500, 0, 520, 10), "更多"),
                                   ((60, 8, 110, 30), "茗风")]) == "茗风", "")
        check("★ 老式四点框也认（`box_bounds` 两代形状都收）",
              _band_leftmost_text([([[9, 9], [99, 9], [99, 29], [9, 29]], "茗风")]) == "茗风", "")
        check("★ 一行都没有 / 只有空白行 → 空串",
              _band_leftmost_text([]) == ""
              and _band_leftmost_text([((0, 0, 9, 9), "   ")]) == "", "")
        check("★ 形状不对的那几行跳过、不炸",
              _band_leftmost_text([("不对劲", "x"), ((0, 0, 9, 9), "茗风")]) == "茗风", "")

        # ---------- ⑤ 名字绝不进材料 ----------

        section("⑤ 名字绝不进材料：带子像素从没进过 `classify_lines` 那个数组")

        monitor.band_rows = [((60, 8, 110, 30), "茗风")]
        app._social_peer_band = _band_image(160)
        app._social_peer_band_hint = ""
        monitor.read_shapes.clear()
        monitor.detect_shapes.clear()
        read = _read(PERSONAL_ROWS)

        check("★ 行为级：`read` 各行 / 原文里没有那个名字",
              all("茗风" not in text for _s, text in read.lines)
              and "茗风" not in read.raw and "茗风" not in read.visible
              and "茗风" not in read.incoming, repr(read.raw))
        check("★ 行为级：`_tail_context(read)`（材料那一份）里也没有",
              "茗风" not in smod._tail_context(read), "")
        check("★ 行为级：`read.messages` / `groups` 里也没有",
              all("茗风" not in m.text for m in read.messages), "")

        service._chat_text = ""
        service._page_kind = PAGE_PERSONAL
        service._on_screen_read(read)
        check("★ 行为级：`_chat_text`（喂模型的那一份）里更没有",
              "茗风" not in service._chat_text, repr(service._chat_text))

        check("★ `detect_screen_boxes`（b9b6caf 免 OCR 那条腿）只看聊天帧，没看过带子",
              monitor.detect_shapes and set(monitor.detect_shapes) == {(300, 400, 3)},
              repr(monitor.detect_shapes))
        check("★ 带子那一份数组**只**喂给了认名字那一手（没混进别的调用）",
              monitor.read_shapes.count((BAND_H, BAND_W, 3)) == 1
              and monitor.read_shapes.count((300, 400, 3)) == 1,
              repr(monitor.read_shapes))

        src_read = inspect.getsource(app.__class__._read_social_chat)
        src_band = inspect.getsource(app.__class__._social_peer_name_from_band)
        src_grab = inspect.getsource(app.__class__._grab_social_chat)
        check("★ 源码级：`classify_lines` 那一行的入参还是 `array`，没被带子换过",
              "classify_lines(array, monitor.read_screen_lines(array))" in src_read, "")
        check("★ 源码级：带子只在 `_social_peer_name_from_band` 里认（一处口子）",
              src_band.count("read_screen_lines") == 1, "")
        check("★ 源码级：抓屏那一拍只 `copy` 那条带子，**不 OCR**（界线程纪律）",
              "title_band_box(" in src_grab and "band_rect" in src_grab
              and "read_screen_lines" not in src_grab, "")
        check("★ 源码级：名字那一格只在喂键那条链上（没进 `_tail_context` / `_auto_generate`）",
              "_social_peer_band" not in inspect.getsource(smod)
              and "_peer_name" not in inspect.getsource(smod._tail_context), "")

        # ---------- ⑥ 带子没变就不重认 ----------

        section("⑥ 同一条带子不重认（真机一趟 2.1~4.8 秒，同一场里别每 1.5 秒烧一遍）")

        monitor.band_rows = [((60, 8, 110, 30), "记着")]
        app._social_peer_band = _band_image(210)
        app._social_peer_band_hint = ""
        before_calls = monitor.band_calls
        _read(PERSONAL_ROWS)
        check("★ 换了一条带子 → 认一次", monitor.band_calls == before_calls + 1,
              f"{monitor.band_calls} 次")

        app._social_peer_band = _band_image(210)          # 一模一样的那条（下一帧重裁）
        app._social_peer_band_hint = ""
        _read(PERSONAL_ROWS)
        check("★ 像素没变 → **不再烧 OCR**", monitor.band_calls == before_calls + 1,
              f"{monitor.band_calls} 次")
        check("★ 但名字复用上次那一份（键不受影响）",
              app._social_peer_band_hint == "记着", repr(app._social_peer_band_hint))

        app._social_peer_band = _band_image(211)          # 像素变了
        app._social_peer_band_hint = ""
        _read(PERSONAL_ROWS)
        check("★ 像素一变 → 立刻重认", monitor.band_calls == before_calls + 2,
              f"{monitor.band_calls} 次")

        app._social_peer_band = None
        app._social_peer_band_hint = ""
        _read(PERSONAL_ROWS)
        check("★ 进门就取走并置空（带子绝不留到下一帧）",
              app._social_peer_band is None, repr(app._social_peer_band))

        # 那行 DEBUG 只出长度
        catcher.lines.clear()
        monitor.band_rows = [((60, 8, 110, 30), "茗风")]
        app._social_peer_band = _band_image(222)
        app._social_peer_band_hint = ""
        _read(PERSONAL_ROWS)
        said = [ln for ln in catcher.lines if "标题带" in ln]
        check("★ 那一行日志真冒出来了（不是没走到）", bool(said), f"{catcher.lines}")
        check("★ 只出长度、不出名字", any("2 字" in ln for ln in said)
              and not any("茗风" in ln for ln in said), f"{said}")

        # ---------- ⑦ 红线 ----------

        section("⑦ 红线：既有语义一处没动 + 两库零写入")

        key_src = inspect.getsource(SocialReplyService._cooldown_key)
        sync_src = inspect.getsource(SocialReplyService._sync_session)
        check("★ `_cooldown_key()` 一个字没改（只读自家那两格）",
              "_session_provider" not in key_src and "_social_peer_band" not in key_src
              and "self._session_id" in key_src and "self._peer_name" in key_src, "")
        check("★ 换人清记忆那条判据照旧只看标识（返回值一个字没改）",
              "previous == ident" in sync_src
              and "self._forget_session_memory()" in sync_src, "")
        check("★ 认字那一拍补的那一次 `_sync_session()` 在材料赋值**之前**",
              inspect.getsource(SocialReplyService._on_screen_read).index("self._sync_session()")
              < inspect.getsource(SocialReplyService._on_screen_read).index("self._chat_text =")
              and inspect.getsource(SocialReplyService._on_listen_read).index(
                  "self._sync_session()")
              < inspect.getsource(SocialReplyService._on_listen_read).index(
                  "self._chat_text ="), "")
        check("★ 裁剪几何一行没改：`chat_area_box` 上沿还是 `TITLE_BAR_PX`，带子没并进去",
              "TITLE_BAR_PX" in inspect.getsource(chat_area_box)
              and "title_band_box" not in inspect.getsource(chat_area_box)
              and "TITLE_BAND_PX" not in inspect.getsource(chat_area_box), "")
        check("★ `looks_like_list_boxes` 的判据没动（那是 b9b6caf 的腿）",
              "def looks_like_list_boxes" in inspect.getsource(smod), "")
        check("★ 状态机没动：`_awaiting` / `_running` 没被这一节点碰",
              "_awaiting =" not in key_src and "_running =" not in key_src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
    finally:
        app.screen_monitor = real_monitor
        blog.removeHandler(catcher)
        blog.setLevel(blog_level_before)
        drain_threads()
        box.cleanup()

    return summary("合并窗下从标题带取对象名（v2-social-peer-by-titlebar）")


if __name__ == "__main__":
    raise SystemExit(main())
