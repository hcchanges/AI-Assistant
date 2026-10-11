"""
缩短"打开单聊后 offer 才冒"的两处延迟（v2-social-offer-fast-latency）的自动测试。

**背景**（只读报告 v2-social-offer-latency-trace 的结论）：从会话总列表点进某个人的
单聊，offer 要约 10 秒才冒；账里最贵、又最该省的一块是——**列表那一屏白做了一趟全文
OCR**（实测中位 2.7 秒），而它既不该冒 offer、也不进材料，还卡着下一拍（识别期间
`_chat_reading` 立着，用户点进单聊的"画面变了"要等它回来 + 下一拍轮询 + 第二次识别）。
本批只做两处，都不碰 OCR 固有耗时、不动 `_running` / `_awaiting` 状态残留：

**① 列表页免全文 OCR**（`life/screen/monitor.OcrReader.detect_boxes` +
`social.looks_like_list_boxes` + `core.bootstrap._read_social_chat`）——在**认字之前**
先只跑检测（RapidOCR 的 `use_rec=False`），拿**字框几何**判"这一屏像不像会话总列表"：
会话总列表每行右边有一个**右对齐的时间列**（一批"右边缘贴窗右边框、本身又是短框"的
框）；单聊里自己的绿气泡右边永远跟着**头像**、时间戳又是**居中**的，两条都进不了这条
带子。命中就整趟省掉识别，走的仍是 16-β 那条"清成空 read"、页型同样记 `PAGE_LIST`
——6aab8da 那道"仅单聊"闸的语义一个字没动，只是把"判列表"从认字之后挪到了认字之前。

**判错的代价不对称**：漏判只是照旧认一遍字（跟加这一手之前逐字相同、零回归）；误判会
把一场真单聊当列表、offer 与自动监听都不冒。所以阈值往**漏判**那边压，并且**判不出来
就照旧认字**（没接引擎 / 形状对不上 / 空屏 / 检测抛异常，四种一律落到老路）。

**② 监听轮询 1.5 秒 → 0.5 秒**（`social.LISTEN_POLL_MS`）：指纹只是毫秒级的 8×8 哈希、
抓帧也只是拷内存，"点进单聊那一瞬画面变了"最迟要等一拍才发现——把这一格压细。

红线：零新依赖；不动 `_running` / `_awaiting` 状态机；不写 pet.db / mind.db；不碰
`config.yaml` / `.vscode/`；不改时钟口径（判据那一层不读任何钟）；不改 6aab8da 的闸门语义。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    summary,
)

from PySide6.QtGui import QColor, QImage  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    LIST_TIME_BAND_PX,
    LIST_TIME_MAX_W_PX,
    LIST_TIME_ROWS_MIN,
    LISTEN_POLL_MS,
    PAGE_LIST,
    _tail_context,
    looks_like_list_boxes,
)
from life.screen.social_window import ChatRead  # noqa: E402

# ---------- 真机读数（照抄，不是造出来的） ----------
#
# 下面每一行是 2026-10-09 那批真机帧中的一张，走**生产裁剪路径**（`social_window.chat_area_box`
# 按 DPR 1.5 裁完）后在裁剪帧上只跑检测（`%TEMP%/p20/gen_data.py` 导出）：
#   (帧名末六位, 裁剪帧宽, 人工标注的页型, [每个检测框的 (左, 右), ...])
# 页型是肉眼逐张核过的：list = 会话总列表，chat = 1:1 单聊，mixed = 一张上下两扇窗的
# 调试帧（非生产帧，归"不该判列表"那一档）。
REAL_FRAMES = [
    ("214347", 638, "list", [
        (67, 94), (83, 621), (85, 153), (85, 215), (85, 268), (85, 624), (86, 153), (86, 173),
        (86, 246), (86, 439), (86, 440), (87, 200), (87, 216), (87, 416), (87, 614), (87, 614),
        (87, 628), (88, 280), (88, 298), (88, 334), (88, 394), (249, 264), (261, 357),
        (549, 626), (549, 628), (550, 627), (550, 627), (550, 627), (584, 626), (584, 626),
        (584, 627), (584, 628), (603, 625), (603, 626),
    ]),
    ("214400", 637, "chat", [
        (119, 146), (119, 146), (121, 259), (121, 332), (122, 291), (122, 500), (130, 172),
        (136, 495), (137, 517), (273, 366), (274, 365), (274, 368), (345, 501), (388, 515),
        (468, 519),
    ]),
    ("214420", 347, "list", [
        (0, 21), (0, 50), (0, 66), (0, 78), (1, 49), (2, 12), (2, 96), (2, 213), (2, 324),
        (2, 332), (258, 333), (258, 334), (258, 334), (259, 332), (259, 332), (259, 334),
        (259, 334), (259, 334), (259, 335), (259, 335), (259, 335), (312, 333), (314, 332),
        (314, 332), (314, 332), (315, 330),
    ]),
    ("214432", 347, "list", [
        (0, 21), (0, 50), (0, 77), (1, 49), (1, 65), (2, 12), (2, 121), (2, 214), (2, 326),
        (150, 236), (258, 333), (258, 334), (259, 332), (259, 334), (259, 334), (259, 334),
        (259, 334), (259, 335), (259, 335), (259, 335), (259, 335), (314, 331), (314, 332),
        (314, 332), (314, 332), (315, 331),
    ]),
    ("215428", 445, "list", [
        (0, 9), (0, 15), (0, 71), (0, 100), (0, 107), (0, 107), (0, 312), (1, 53), (1, 76),
        (1, 111), (1, 147), (2, 119), (2, 224), (2, 426), (3, 145), (5, 175), (71, 163),
        (248, 337), (356, 431), (356, 433), (356, 433), (356, 433), (356, 433), (356, 433),
        (356, 434), (356, 434), (356, 434), (356, 434), (357, 434), (410, 434), (411, 432),
        (411, 432), (411, 432), (411, 433),
    ]),
    ("224526", 447, "list", [
        (0, 16), (0, 55), (0, 73), (0, 78), (1, 102), (1, 108), (1, 109), (1, 112), (1, 120),
        (1, 149), (1, 428), (2, 146), (2, 312), (2, 432), (3, 177), (5, 169), (73, 165),
        (358, 434), (358, 434), (358, 435), (358, 435), (358, 435), (358, 435), (358, 435),
        (358, 435), (358, 435), (358, 435), (412, 434), (412, 435), (413, 434), (413, 435),
        (413, 435),
    ]),
    ("232318", 638, "list", [
        (20, 71), (21, 71), (22, 56), (61, 98), (83, 128), (83, 133), (83, 303), (84, 158),
        (84, 173), (84, 270), (84, 505), (84, 620), (85, 204), (85, 263), (85, 312), (85, 623),
        (86, 356), (86, 395), (86, 628), (87, 200), (87, 298), (87, 342), (87, 390), (87, 614),
        (88, 298), (146, 220), (332, 366), (381, 399), (549, 627), (549, 627), (549, 628),
        (549, 628), (549, 628), (550, 626), (550, 626), (550, 627), (550, 627), (584, 625),
        (603, 626), (603, 626), (603, 626), (605, 624),
    ]),
    ("232333", 638, "chat", [
        (119, 147), (119, 147), (120, 292), (120, 335), (121, 259), (122, 501), (134, 171),
        (137, 496), (140, 517), (275, 366), (275, 367), (275, 368), (347, 504), (388, 516),
        (470, 520),
    ]),
    ("232343", 638, "chat", [
        (121, 292), (122, 260), (123, 428), (123, 502), (134, 171), (137, 496), (137, 503),
        (137, 517), (137, 518), (139, 482), (140, 517), (266, 376), (268, 376), (275, 367),
        (275, 368), (469, 520),
    ]),
    ("232408", 638, "chat", [
        (80, 121), (119, 147), (119, 147), (120, 293), (122, 259), (123, 501), (134, 170),
        (136, 504), (137, 496), (140, 517), (274, 370), (275, 367), (275, 367), (345, 503),
        (388, 517), (469, 520),
    ]),
    ("234105", 265, "mixed", [
        (0, 47), (1, 56), (1, 264), (2, 142), (2, 232), (2, 262), (2, 264), (3, 235), (3, 264),
        (3, 264), (9, 62), (10, 264), (15, 212), (18, 121), (43, 68), (55, 264), (207, 264),
    ]),
]


def _boxes(pairs) -> list:
    """把 (左, 右) 对子还原成检测环那种四点多边形 `[[x, y], ...]`。"""
    return [[[left, 0], [right, 0], [right, 1], [left, 1]] for left, right in pairs]


def _blank(width: int, height: int = 400) -> QImage:
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(QColor(255, 255, 255))
    return img


class _FakeMonitor:
    """替掉真 monitor 的**两只手**：只检测那一条与认字那一条，各自记调用次数。"""

    def __init__(self, boxes: list, lines: list) -> None:
        self.boxes = boxes
        self.lines = lines
        self.box_calls = 0
        self.line_calls = 0

    def detect_screen_boxes(self, array):        # noqa: ARG002 —— 跟真接口对齐
        self.box_calls += 1
        return self.boxes

    def read_screen_lines(self, array):          # noqa: ARG002
        self.line_calls += 1
        return self.lines


class _NoDetectMonitor:
    """老桩：没有 `detect_screen_boxes` 那一手（照着"没接检测"那条降级路走）。"""

    def __init__(self, lines: list) -> None:
        self.lines = lines
        self.line_calls = 0

    def read_screen_lines(self, array):          # noqa: ARG002
        self.line_calls += 1
        return self.lines


class _RaiseMonitor:
    """检测那一手当场抛：照旧要落到认字那条老路上去，不许把线带崩。"""

    def __init__(self, lines: list) -> None:
        self.lines = lines
        self.line_calls = 0

    def detect_screen_boxes(self, array):        # noqa: ARG002
        raise RuntimeError("检测这一手出岔子了")

    def read_screen_lines(self, array):          # noqa: ARG002
        self.line_calls += 1
        return self.lines


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, _stub, app = make_env()
    try:
        # ---------- ① 判据的三个常量 ----------

        section("① 判据常量：右带 40px / 短框上限 200px / 至少 6 个")

        check("右带（右边缘距窗右边框多少 px 以内算贴边）就是 40",
              LIST_TIME_BAND_PX == 40, str(LIST_TIME_BAND_PX))
        check("短框上限（比这宽的是整行文字，不算）就是 200",
              LIST_TIME_MAX_W_PX == 200, str(LIST_TIME_MAX_W_PX))
        check("门槛（凑够几个这样的框才算列表）就是 6",
              LIST_TIME_ROWS_MIN == 6, str(LIST_TIME_ROWS_MIN))

        # ---------- ② 判据算术 ----------

        section("② 判据算术：贴边短框凑够 6 个才算列表")

        right = 628          # 638 宽的帧，右边缘贴边 10px
        hit12 = _boxes([(540, right)] * 12)
        hit6 = _boxes([(540, right)] * 6)
        hit5 = _boxes([(540, right)] * 5)
        check("★ 12 个贴边短框 → 判列表", looks_like_list_boxes(hit12, 638) is True, "")
        check("边界：正好 6 个 → 判列表", looks_like_list_boxes(hit6, 638) is True, "")
        check("★ 边界：只有 5 个 → 不判列表（宁可漏判，不可误判）",
              looks_like_list_boxes(hit5, 638) is False, "")

        long_boxes = _boxes([(100, right)] * 12)      # 右边缘贴边，但一条 528px 的整行长框
        check("★ 右边缘贴边但**很长**的框（终端满行那种）一个都不算",
              looks_like_list_boxes(long_boxes, 638) is False, "")

        far_boxes = _boxes([(200, 500)] * 12)         # 短框，但右边缘离边框 138px
        check("★ 短框但**不贴边**（单聊里左对齐气泡那种）一个都不算",
              looks_like_list_boxes(far_boxes, 638) is False, "")

        mixed = _boxes([(100, right), (540, right + 0), (100, right), (540, 626),
                        (100, right), (540, 625), (100, right), (540, 624),
                        (100, right), (540, 623), (100, right), (540, 622)])
        check("长框与短框混着来：只数短的那 6 个，照样判列表",
              looks_like_list_boxes(mixed, 638) is True, "")

        check("空表 / 宽度为 0 / 框形状对不上 → 一律不判列表（判不出来就照旧认字）",
              looks_like_list_boxes([], 638) is False
              and looks_like_list_boxes(None, 638) is False
              and looks_like_list_boxes(hit12, 0) is False
              and looks_like_list_boxes(["不是框", object()], 638) is False, "")

        # ---------- ③ 真机几何：11 张真帧的检测框照抄，逐张判 ----------

        section("③ 真机几何：6 张列表帧命中、4 张单聊帧与 1 张混合帧不命中")

        verdicts = []
        for key, width, label, pairs in REAL_FRAMES:
            got = looks_like_list_boxes(_boxes(pairs), width)
            verdicts.append((key, label, got, width, len(pairs)))
        list_hits = [v for v in verdicts if v[1] == "list"]
        chat_hits = [v for v in verdicts if v[1] == "chat"]
        check("前提：真机那批一共 6 张列表帧 + 4 张单聊帧（人工逐张核过）",
              len(list_hits) == 6 and len(chat_hits) == 4, f"{len(list_hits)} / {len(chat_hits)}")
        check("★ 6 张会话总列表帧，一张不漏，全判列表",
              all(g for _k, _l, g, _w, _n in list_hits),
              "、".join(f"{k}={g}" for k, _l, g, _w, _n in list_hits))
        check("★ 4 张 1:1 单聊帧，一张不误判",
              all(not g for _k, _l, g, _w, _n in chat_hits),
              "、".join(f"{k}={g}" for k, _l, g, _w, _n in chat_hits))
        mixed_row = [v for v in verdicts if v[1] == "mixed"]
        check("★ 那张上下两扇窗的调试混合帧也不判列表（终端满行靠「长框不算」挡掉）",
              mixed_row and mixed_row[0][2] is False,
              "、".join(f"{k}={g}" for k, _l, g, _w, _n in mixed_row))
        # 两侧余量：列表帧的命中数与单聊帧的命中数之间要留得开
        n_list_min = min(sum(1 for l, r in pairs
                             if (w - r) <= LIST_TIME_BAND_PX and (r - l) <= LIST_TIME_MAX_W_PX)
                         for _k, w, lab, pairs in REAL_FRAMES if lab == "list")
        n_chat_max = max(sum(1 for l, r in pairs
                             if (w - r) <= LIST_TIME_BAND_PX and (r - l) <= LIST_TIME_MAX_W_PX)
                         for _k, w, lab, pairs in REAL_FRAMES if lab != "list")
        check("★ 真机读数上阈值 6 落在两侧正中（列表最少 11、非列表最多 1）",
              n_list_min == 11 and n_chat_max == 1,
              f"列表最少 {n_list_min} / 非列表最多 {n_chat_max}")

        # ---------- ④ 落点：装配层这一拍走"免识别"快路 ----------

        section("④ 落点：判成列表就整趟省掉识别，回的还是那份空 read（16-β 语义）")

        real_monitor = getattr(app, "screen_monitor", None)
        try:
            list_lines = [([100, 100, 260, 126], "茗风"), ([600, 100, 636, 124], "06:54")]
            arr = mon._to_numpy(_blank(638))
            fm = _FakeMonitor(_boxes([(540, 628)] * 12), list_lines)
            app.screen_monitor = fm
            got = app._read_social_chat(arr)
            check("★ 命中列表：**全文识别一次都没跑**（省下的就是那 2.7 秒）",
                  fm.line_calls == 0, f"{fm.line_calls} 次")
            check("前置：只检测那一手真被叫过一次", fm.box_calls == 1, f"{fm.box_calls} 次")
            check("★ 回的是空 read（一行都没有）", not got.lines and not got.visible,
                  f"lines={got.lines}")
            check("★ 页型记的是 PAGE_LIST（跟认字之后那条老路记的同一档）",
                  got.page_kind == PAGE_LIST, repr(got.page_kind))
            check("★ `_tail_context` 一个字都取不出来（列表昵称进不了材料）",
                  _tail_context(got) == "", repr(_tail_context(got)))

            # 非列表：照旧走认字那条老路，一步都不少
            fm2 = _FakeMonitor(_boxes([(200, 500)] * 12), list_lines)
            app.screen_monitor = fm2
            got2 = app._read_social_chat(arr)
            check("★ 非列表（短框不贴边）：照旧认字，一次不少",
                  fm2.line_calls == 1 and fm2.box_calls == 1,
                  f"认字 {fm2.line_calls} 次 / 检测 {fm2.box_calls} 次")
            check("非列表那一拍的字照旧进了 read", bool(got2.lines), f"{len(got2.lines)} 行")

            # 只有 5 个贴边短框：够不着门槛 → 照旧认字
            fm3 = _FakeMonitor(_boxes([(540, 628)] * 5), list_lines)
            app.screen_monitor = fm3
            app._read_social_chat(arr)
            check("边界：只有 5 个贴边短框 → 不省识别，照旧认字",
                  fm3.line_calls == 1, f"{fm3.line_calls} 次")

            # 老桩：没有 detect 那一手 → 降级成照旧认字（不崩）
            fm4 = _NoDetectMonitor(list_lines)
            app.screen_monitor = fm4
            app._read_social_chat(arr)
            check("★ 没接检测那一手（老桩）→ 降级成照旧认字，不崩",
                  fm4.line_calls == 1, f"{fm4.line_calls} 次")

            # 检测当场抛 → 也要落到老路上去
            fm5 = _RaiseMonitor(list_lines)
            app.screen_monitor = fm5
            app._read_social_chat(arr)
            check("★ 检测那一手抛异常 → 照旧认字，不把线带崩",
                  fm5.line_calls == 1, f"{fm5.line_calls} 次")
        finally:
            if real_monitor is not None:
                app.screen_monitor = real_monitor

        # ---------- ⑤ 监听轮询 1.5 秒 → 0.5 秒 ----------

        section("⑤ 监听轮询：LISTEN_POLL_MS 1500 → 500，计时器跟它对齐")

        check("★ 常量就是 500", LISTEN_POLL_MS == 500, str(LISTEN_POLL_MS))
        service = app.social_reply
        check("★ 轻量轮询那支计时器的间隔跟着成了 500ms",
              service._listen_timer.interval() == LISTEN_POLL_MS == 500,
              f"{service._listen_timer.interval()} / {LISTEN_POLL_MS}")
        check("按需抓拍那支（PENDING_SNAP_MS）也是 500 —— 两格现在同级",
              smod.PENDING_SNAP_MS == 500, str(smod.PENDING_SNAP_MS))

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：零新依赖 / 不动状态机 / 不改 6aab8da / 两库零写入")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        refs: set[str] = set()
        for mod in (smod, mon):
            for node in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(node, ast.Import):
                    refs.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    refs.update(a.name for a in node.names)
        banned = {"requests", "PIL", "psutil", "pyperclip", "cv2"}
        check("这一批没有新引入第三方库", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        judge_src = inspect.getsource(smod.looks_like_list_boxes)
        check("★ 判据那一层不读任何钟（不看时间，只看几何）",
              not any(tok in judge_src for tok in
                      ("monotonic", "time.", "now(", "QElapsedTimer", "elapsed", "datetime")),
              "")
        check("★ 判据那一层不碰颜色 / 不认字（只算字框的 x 跨度）",
              "pixel" not in judge_src and "color" not in judge_src.lower(), "")

        det_src = inspect.getsource(mon.OcrReader.detect_boxes)
        check("★ 只检测那一手确实把识别关掉了（use_rec=False）",
              "use_rec=False" in det_src and "use_det=True" in det_src, "")

        social_src = inspect.getsource(smod)
        check("★ 不动状态机：`_ask_offer` 那两道老闸（`_running` / `_awaiting`）一字未动",
              "if self._running or self._awaiting:" in social_src, "")
        check("★ 不动状态机：`_poll_listen` 门口那行 early-return 也没动",
              "if self._running or self._awaiting or self._chat_reading or self._listen_reading:"
              in social_src, "")
        check("★ 6aab8da 的闸门语义没改：非单聊那一格照旧「只记事实、不冒话」、"
              "空串照旧按单聊算",
              "if not is_personal_kind(kind):" in social_src
              and 'return (kind or "") not in NON_PERSONAL_PAGES' in social_src, "")

        boot_src = inspect.getsource(app.__class__)
        check("★ 快路走的仍是 16-β 那条「清成空 read」，没另起一条路",
              "read = ChatRead()" in boot_src and "read.page_kind = PAGE_LIST" in boot_src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("打开单聊后 offer 冒得慢：列表页免全文 OCR + 监听轮询 1.5→0.5 秒"
                   "（v2-social-offer-fast-latency）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
