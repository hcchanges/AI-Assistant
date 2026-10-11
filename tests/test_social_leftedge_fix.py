"""带子的裁剪**跟着名字走**：左沿漂、指纹不漂（v2-social-leftedge-fix）。

**这一批解的那个坑**（§187 末尾如实留下的那一条）：`title_band_box` 的左沿原来直接取
`_chat_left`——"聊天区左沿量在哪儿"。可那条边**会漂**：真机那批帧上它在 92 / 164 / 263 / 465
之间来回跳，因为聊天区里一条大气泡（真机填充色 (238,238,240)）把 `detect_chat_left` 那条
"连着几十列都是空列"的腿顶到气泡右缘去了（那几帧真机日志上就是 `最终 left 254` ↔
`最终 left 92`）。带子按那条边裁，同一场的两拍就裁出**两块不同的像素**：指纹变 →
`_peer_band_changed()` 当成"换了场" → 名字被清掉（下一拍又认回来，冷却那把键在「微信|茗风」
与「微信」之间来回抖）；左沿跳到名字右边那几拍更糟——带子里压根没有名字，OCR 只能回 0 字。

**这一批改的**：左沿改成**锚在名字身上**（`title_band_box` → `_title_name_left`：在带子那
几行里找第一笔墨，从那儿起裁）。同一场里名字没动，锚点就不动。

**红线**（本模块逐条验）：
  1. 左沿漂的时候，带子的**指纹一个字都不变**（`_peer_band_changed` 看的就是那个 md5）；
  2. **会话列表那一列照旧一格像素都进不了带子**（主窗上量出来的左沿被大气泡顶到右边也一样）；
  3. 材料那一刀（`chat_area_box` / `cut_edges_box`）**一个字没动**——这一批只动那条只读取证带。

零新依赖（只用 Qt 的 QImage/QColor 与标准库 hashlib）；不碰 config.yaml / .vscode；不写
pet.db / mind.db。
"""

from __future__ import annotations

import ast
import hashlib
import inspect
import sys
import textwrap
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary  # noqa: E402

from life.screen import social_window as sw  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    TITLE_BAND_PX,
    TITLE_BAR_PX,
    TITLE_INK_TOL,
    TITLE_NAME_PAD_PX,
    chat_area_box,
    title_band_box,
)

# ---------- 合成帧：按真机那扇 742×1342 单页窗的读数摆 ----------
#
# 真机读数（§183 / §187）：名字那一行在 y≈70-72、x≈121-170；大气泡的填充色是 (238,238,240)，
# 聊天区底色 (250,250,250)；`_chat_left` 那几帧在 92 ↔ 254 之间跳。带子的行区间是
# [TITLE_BAR 56 × 1.5 − TITLE_BAND 34 × 1.5, TITLE_BAR 56 × 1.5) = [33, 84)。
W, H, DPR = 742, 1342, 1.5
MAIN_W = 1512
CHAT_BG = (250, 250, 250)
LIST_BG = (240, 240, 240)         # 会话列表那一列的底色（跟聊天区底色差得开，量左沿时不算"空列"）
ICON_BG = (243, 243, 243)
BUBBLE = (238, 238, 240)          # 真机 2026-10-08 那条大气泡的填充色
INK = (30, 30, 30)
MARK = (255, 0, 255)              # 会话列表那一列的内容：带子里出现一个像素都不行
LIST_MARK_BOX = (100, 36, 440, 80)   # 主窗里落在**带子行区间**里的列表内容（搜索框 + 首行）
NAME = (121, 60, 49, 22)          # 名字「茗风」
NAME_OTHER = (121, 60, 61, 22)    # 换了个名字（同一个左沿）
MORE = (700, 62, 26, 8)           # 「更多」那几个点（真机在名字右边）
MAIN_NAME = (470, 60, 80, 22)     # 主窗里聊天区那个名字（紧挨会话列表右边）
ICON_BAR_PX = int(60 * DPR)       # 图标栏右缘 = SIDEBAR_PX × dpr = 90
LAYOUT_LEFT = int((60 + 250) * DPR)   # 版面常数：有会话列表列时聊天区的左沿 = 465
#
# **摆帧为什么用 32 位格式**（`Format_RGB32`）：这一批验的就是带子的 **md5**，而
# `_peer_band_changed` 量的是**整段缓冲**（含 Qt 给每行补齐的那些字节）。24 位的
# `Format_RGB888` 在 621 像素宽上每行补 1 个字节，而那几个补齐字节 Qt 不保证初始化——
# 同一块像素量两回能得出两个 md5（测试自己先成了随机的）。真机抓屏给的就是 32 位格式
# （4 字节一像素、没有补齐字节），所以摆帧也得跟着用 32 位，量出来的才是像素本身。


def _fill(img, x0: int, y0: int, x1: int, y1: int, rgb) -> None:
    from PySide6.QtGui import QColor

    color = QColor(*rgb)
    for x in range(x0, x1):
        for y in range(y0, y1):
            img.setPixelColor(x, y, color)


def _page(bubble_w: int = 0, name=NAME):
    """真机那扇**单页窗**：图标栏 + 聊天区 + 名字那一行（+ 可选一条宽大气泡）。

    `bubble_w` 是聊天区里那条大气泡有多宽（从图标栏右缘起）：0 = 没有气泡，164 = 真机上
    把"连续空列"那条腿顶到 254 的那一帧。
    """
    from PySide6.QtGui import QColor, QImage

    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(QColor(*CHAT_BG))
    _fill(img, 0, 0, ICON_BAR_PX, H, ICON_BG)          # 图标栏（x < 90）
    if bubble_w:
        _fill(img, ICON_BAR_PX, 400, ICON_BAR_PX + bubble_w, 700, BUBBLE)
    for (bx, by, bw, bh) in (name, MORE):
        _fill(img, bx, by, bx + bw, by + bh, INK)
    return img


def _main(bubble_x=None, name=MAIN_NAME):
    """**主窗**：图标栏 + 会话列表那一列（375 逻辑像素）+ 聊天区。

    列表那一列在带子行区间里也摆了东西（真机上就是搜索框与列表首行的名字），一律抹成
    `MARK`：带子里只要出现一个 `MARK` 像素，就是"别人的名字/预览漏进来了"。
    """
    from PySide6.QtGui import QColor, QImage

    img = QImage(MAIN_W, H, QImage.Format_RGB32)
    img.fill(QColor(*CHAT_BG))
    _fill(img, 0, 0, ICON_BAR_PX, H, ICON_BG)          # 图标栏
    _fill(img, ICON_BAR_PX, 0, LAYOUT_LEFT, H, LIST_BG)   # 会话列表那一列
    bx0, by0, bx1, by1 = LIST_MARK_BOX
    _fill(img, bx0, by0, bx1, by1, MARK)               # 带子行区间里的列表内容（搜索框 / 首行名）
    for i in range(11):                                # 列表各行：头像 + 名字 + 一行预览
        top = 100 + i * 96
        _fill(img, 100, top, 154, top + 54, MARK)
        _fill(img, 170, top + 6, 300, top + 26, MARK)
        _fill(img, 170, top + 34, 440, top + 48, MARK)
    if bubble_x:
        _fill(img, bubble_x, 400, bubble_x + 200, 700, BUBBLE)
    for (x, y, w, h) in (name, MORE):
        _fill(img, x, y, x + w, y + h, INK)
    return img


def _md5(band) -> str:
    """带子指纹：跟 `_peer_band_changed` 里那一个**同一把尺**（原始字节的 md5）。"""
    return hashlib.md5(bytes(band.constBits())).hexdigest()


def _crop(img, box):
    from PySide6.QtCore import QRect

    return img.copy(QRect(*box))


def _has_rgb(img, box, rgb) -> bool:
    from PySide6.QtGui import QColor

    want = QColor(*rgb)
    x, y, w, h = box
    for px in range(x, x + w):
        for py in range(y, y + h):
            if img.pixelColor(px, py) == want:
                return True
    return False


def _ink_columns(img, box, rgb=INK):
    """带子里**有墨的那几列**（x 坐标），供"锚在名字上"那几条断言用。"""
    from PySide6.QtGui import QColor

    want = QColor(*rgb)
    x, y, w, h = box
    out = []
    for px in range(x, x + w):
        for py in range(y, y + h):
            if img.pixelColor(px, py) == want:
                out.append(px)
                break
    return out


def _calls(func, name: str) -> list[str]:
    """函数里真叫过的 `名字(...)`（AST：只看真调用，不看注释/字符串里提到的）。"""
    src = textwrap.dedent(inspect.getsource(func))
    out = []
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Call):
            fn = node.func
            if isinstance(fn, ast.Name) and fn.id == name:
                out.append(name)
            elif isinstance(fn, ast.Attribute) and fn.attr == name:
                out.append(name)
    return out


def _rows(db_path: str, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 根因复现：同一场的两拍，左沿漂了、带子一个字没变 ----------

        section("① 根因复现：大气泡把左沿顶到 254，带子却必须跟没气泡那拍**逐字节相同**")

        plain = _page(0)                  # 没有大气泡那一拍
        bubbled = _page(164)              # 聊天区里一条 164 像素宽的大气泡那一拍
        left_plain = sw._chat_left(plain, DPR)
        left_bubbled = sw._chat_left(bubbled, DPR)
        check("★ 复现得出那个坑：同一场的两拍，量出来的左沿真的不一样（90 ↔ 254）",
              left_plain == ICON_BAR_PX and left_bubbled == 254,
              f"{left_plain} / {left_bubbled}")
        check("★ 量出来的左沿确实在跳（不是我们摆错了帧）",
              left_plain != left_bubbled, f"{left_plain} / {left_bubbled}")

        box_plain = title_band_box(plain, DPR, left_plain)
        box_bubbled = title_band_box(bubbled, DPR, left_bubbled)
        check("★ 两拍的带子**同一个框**（左沿不再跟着那条会漂的边）",
              box_plain == box_bubbled and box_plain is not None,
              f"{box_plain} vs {box_bubbled}")
        check("★ 指纹（`_peer_band_changed` 看的那一个 md5）逐字节相同",
              _md5(_crop(plain, box_plain)) == _md5(_crop(bubbled, box_bubbled)), "")
        check("★ 锚点落在名字上（名字左沿 − 让出的那点边距）",
              box_plain[0] == NAME[0] - int(TITLE_NAME_PAD_PX * DPR),
              f"{box_plain[0]} vs {NAME[0]}")
        cols = _ink_columns(plain, box_plain)
        name_cols = [x for x in cols if x < MORE[0]]
        more_cols = [x for x in cols if x >= MORE[0]]
        check("★ 名字那一行的墨**在带子里**（左沿 254 那拍照样认得到，不再回 0 字）",
              name_cols == list(range(NAME[0], NAME[0] + NAME[2])),
              f"{name_cols[:1]}..{name_cols[-1:]}")
        check("★ 带子里那一段墨就是名字那一块（x 范围与真机读数一致 121..169）",
              (min(name_cols), max(name_cols)) == (NAME[0], NAME[0] + NAME[2] - 1),
              f"{min(name_cols)}..{max(name_cols)}")
        check("★ 「更多」那几个点也在带子里（它在名字右边，不参与「第一笔墨」）",
              more_cols == list(range(MORE[0], MORE[0] + MORE[2])), f"{more_cols[:1]}..{more_cols[-1:]}")

        # ---------- ② 左沿漂到哪儿，带子都不动 ----------

        section("② 左沿取遍真机那几个读数（含「放弃左切」那一档），带子一动不动")

        drift = (12, 88, 90, 92, 164, 254, 263, 300, 445)   # 真机日志上出现过的 / 退化的
        same = {left: title_band_box(bubbled, DPR, left) for left in drift}
        check("★ 单页那一档：九个左沿量出来的带子**同一个框**",
              len(set(same.values())) == 1, repr(same))
        check("★ 框跟「没气泡那一拍按量出来的左沿裁的」也相同",
              list(same.values())[0] == box_plain, f"{list(same.values())[0]} vs {box_plain}")
        check("★ 左沿取 12（真机「单页放弃左切」那一档）也锚在名字上，没把图标栏吃进来",
              same[12] is not None and same[12][0] >= ICON_BAR_PX, repr(same[12]))

        # ---------- ③ 会话列表那一列一格像素都进不了带子 ----------

        section("③ 主窗：量出来的左沿被大气泡顶到右边，列表那一列的内容照样进不了带子")

        main_plain = _main()
        main_shoved = _main(bubble_x=480)      # 大气泡把左沿顶到右边那一拍
        left_main = sw._chat_left(main_plain, DPR)
        left_shoved = sw._chat_left(main_shoved, DPR)
        check("★ 主窗上量出来的左沿就落在版面常数那儿（465）",
              left_main == LAYOUT_LEFT, f"{left_main}")
        check("★ 复现主窗那一版：大气泡把左沿顶得更右边去了",
              left_shoved > left_main, f"{left_main} → {left_shoved}")

        bm_plain = title_band_box(main_plain, DPR, left_main)
        bm_shoved = title_band_box(main_shoved, DPR, left_shoved)
        check("★ 两拍的带子同一个框（主窗那一档也稳）",
              bm_plain == bm_shoved and bm_plain is not None, f"{bm_plain} vs {bm_shoved}")
        check("★ 带子的左沿**不越过版面常数**（会话列表那一列被常数挡在外面，不靠测量）",
              bm_plain is not None and bm_plain[0] >= LAYOUT_LEFT, repr(bm_plain))
        check("★ 带子里一个 MARK 像素都没有（列表里的名字/头像/搜索框一格没漏进来）",
              not _has_rgb(main_plain, bm_plain, MARK)
              and not _has_rgb(main_shoved, bm_shoved, MARK),
              f"{bm_plain} / {bm_shoved}")
        check("★ 带子里那笔墨是**聊天区**那个名字（主窗上也认得出人）",
              _ink_columns(main_plain, bm_plain)[:1] == [MAIN_NAME[0]]
              and _ink_columns(main_shoved, bm_shoved)[:1] == [MAIN_NAME[0]],
              f"{_ink_columns(main_plain, bm_plain)[:1]} / {_ink_columns(main_shoved, bm_shoved)[:1]}")
        check("★ 主窗两拍的指纹也逐字节相同",
              _md5(_crop(main_plain, bm_plain)) == _md5(_crop(main_shoved, bm_shoved)), "")
        check("★ 反面对照：这两条带子跟单页那扇的**不是**同一条（没把两扇窗混成一格）",
              _md5(_crop(main_plain, bm_plain)) != _md5(_crop(plain, box_plain)), "")

        # ---------- ④ 指纹该变还得变：名字真换了、窗口真换了 ----------

        section("④ 指纹不是「什么都判不出来」：名字真换了 / 画面真变了，照样变")

        other = _page(164, name=NAME_OTHER)
        box_other = title_band_box(other, DPR, sw._chat_left(other, DPR))
        check("★ 名字换了但左沿没动 → 框还是同一个（锚点只认「第一笔墨在哪」）",
              box_other == box_plain, f"{box_other} vs {box_plain}")
        check("★ 换了名字 → 指纹变了（该清的场照样清）",
              _md5(_crop(other, box_other)) != _md5(_crop(bubbled, box_bubbled)), "")
        check("★ 名字右边那个「更多」不参与锚点（它一直在名字右边）",
              min(_ink_columns(bubbled, box_bubbled)) == NAME[0], "")

        # ---------- ⑤ 老口径没动：没墨就照旧、几何没动、材料那一刀没动 ----------

        section("⑤ 老口径与几何：没墨 → 左沿就是传进来那个；上沿/下沿/高/右沿一个字没动")

        from PySide6.QtGui import QColor, QImage

        blank = QImage(W, H, QImage.Format_RGB32)
        blank.fill(QColor(*CHAT_BG))
        calls = {"n": 0}
        real_left = sw._chat_left

        def counting_left(img, dpr=1.0):
            calls["n"] += 1
            return real_left(img, dpr)

        sw._chat_left = counting_left
        try:
            b99 = title_band_box(blank, 1.0, left=99)
            check("★ 白画面（一个字都没有）→ 左沿就是传进来的 99（这一改之前的老口径）",
                  b99 is not None and b99[0] == 99, repr(b99))
            check("★ 传了 left → 一次 `_chat_left` 都不多跑",
                  calls["n"] == 0, f"{calls['n']} 次")
            b_inner = title_band_box(blank, 1.0)
            check("★ 不传 left → 才在内部量一次（回退腿没断）",
                  calls["n"] == 1 and b_inner is not None, f"calls={calls['n']}")
        finally:
            sw._chat_left = real_left
        check("★ 锚点那两步**从不调 `_chat_left`**（源码级：一体一位都不许量第二趟）",
              _calls(sw._title_name_left, "_chat_left") == []
              and _calls(sw._column_has_ink, "_chat_left") == [], "")

        dark = QImage(W, H, QImage.Format_RGB32)
        dark.fill(QColor(40, 40, 40))
        _fill(dark, 0, 0, ICON_BAR_PX, H, (30, 30, 30))
        _fill(dark, NAME[0], NAME[1], NAME[0] + NAME[2], NAME[1] + NAME[3], (235, 235, 235))
        box_dark = title_band_box(dark, DPR, left_plain)
        check("★ 深色主题也认得出墨（只看「差得开」、不看谁深谁浅）",
              box_dark is not None and box_dark[0] == NAME[0] - int(TITLE_NAME_PAD_PX * DPR)
              and abs(235 - 40) >= TITLE_INK_TOL, repr(box_dark))
        check("★ 深色主题下锚点也落在名字上，没锚到更浅的底上",
              box_dark[0] + box_dark[2] - 1 >= NAME[0] + NAME[2] - 1, repr(box_dark))

        cut = int(TITLE_BAR_PX * DPR)
        band_h = int(TITLE_BAND_PX * DPR)
        chat_box = chat_area_box(bubbled, DPR)
        check("★ 下沿 == 聊天区上沿（两块不重叠：名字进不了材料）",
              box_bubbled[1] + box_bubbled[3] == cut == chat_box[1], f"{box_bubbled} {chat_box}")
        check("★ 上沿 == 下沿 − 带子高（TITLE_BAND_PX × dpr）",
              box_bubbled[1] == cut - band_h and box_bubbled[3] == band_h, repr(box_bubbled))
        check("★ 带子跟 `chat_area_box` 在几何上**不相交**",
              box_bubbled[1] + box_bubbled[3] <= chat_box[1], f"{box_bubbled} vs {chat_box}")
        check("★ 右沿照旧是 `width − RIGHT_PAD_PX × dpr`（一个字没动）",
              box_bubbled[0] + box_bubbled[2] == chat_box[0] + chat_box[2],
              f"{box_bubbled} vs {chat_box}")
        check("★ 缩放口径没动：dpr=2 时带子跟着长",
              title_band_box(blank, 2.0)[3] == int(TITLE_BAND_PX * 2), "")
        check("★ 空画面 / 1×1 → None（调用方当这一帧没有名字）",
              title_band_box(QImage(), DPR) is None
              and title_band_box(QImage(1, 1, QImage.Format_RGB32), DPR) is None, "")

        section("⑤-b 材料那一刀一个字没动：`chat_area_box` 的左沿还是量出来的那条")

        check("★ 聊天区的左沿**没被锚点带跑**（单页那一帧：还是量出来的 254）",
              chat_area_box(bubbled, DPR, left_bubbled)[0] == 254
              and chat_area_box(bubbled, DPR)[0] == left_bubbled, "")
        check("★ `chat_area_box` 源码里一个 `title_band_box` / 锚点都没有",
              "title_band_box" not in inspect.getsource(chat_area_box)
              and "TITLE_BAND_PX" not in inspect.getsource(chat_area_box)
              and "_title_name_left" not in inspect.getsource(chat_area_box), "")
        cut_src = inspect.getsource(sw.cut_edges_box)
        check("★ `cut_edges_box` 也没动（左沿还是 `_chat_left`）",
              "_chat_left(image, dpr)" in cut_src and "_title_name_left" not in cut_src, "")

        # ---------- ⑥ 真路：界面线程那一拍判"换场"的结论 ----------

        section("⑥ 真路（`_peer_band_changed`）：左沿漂前漂后都判「没换场」，名字真换了才判「换了」")

        band_plain = _crop(plain, box_plain)
        band_bubbled = _crop(bubbled, box_bubbled)
        app._social_peer_band_seen_md5 = ""
        check("★ 第一拍：没见过这条带子 → True（换场，该清）",
              app._peer_band_changed(band_plain) is True, "")
        app._social_peer_band_seen_md5 = _md5(band_bubbled)
        check("★ 第二拍：左沿从 90 漂到 254，带子逐字节相同 → **False**（不许清名字）",
              app._peer_band_changed(band_plain) is False, "")
        check("★ 那一拍走完，装配层记下的还是同一条带子（名字没被清）",
              app._social_peer_band_seen_md5 == _md5(band_plain), "")
        band_other = _crop(other, box_other)
        check("★ 反面对照：名字真换了（带子也真变了）→ True（换场必须清）",
              app._peer_band_changed(band_other) is True, "")
        check("★ 再拿漂前那一条回来 → 又是 True（不是「记住了一条就永远 False」）",
              app._peer_band_changed(band_plain) is True, "")

        # ---------- ⑦ 红线 ----------

        section("⑦ 红线：零新依赖 + 两库零写入")

        banned = {"requests", "pyautogui", "win32api", "pyperclip", "pynput", "keyboard",
                  "mouse", "PIL", "cv2", "psutil"}
        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(sw))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        check("★ `social_window` 这一条线没多一个第三方 import（含全局热键那类库）",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
        check("★ 这一批没碰社交服务那一层（`social.py` 里没有锚点那一套）",
              "_title_name_left" not in inspect.getsource(
                  __import__("life.screen.social", fromlist=["x"])), "")
        check("★ 这一批没碰装配层（`core/bootstrap.py` 里没有锚点那一套）",
              "_title_name_left" not in inspect.getsource(
                  __import__("core.bootstrap", fromlist=["x"])), "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("带子裁剪跟着名字走：左沿漂、指纹不漂（v2-social-leftedge-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
