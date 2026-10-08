"""
输入框上沿那条**边框线**那条腿（v2-social-input-edge-line）的自动测试。

真机 2026-10-08 那批 742×1342 的帧上，「输入框草稿被当对方话」是怎么来的：

* 输入区底 == 聊天区底（群聊单页视图上两个都是 250），`fill` 那条腿把"最浅且不是聊天区底"
  的色锚到了输入框**底下**那条工具栏（247），量出来只剩 15 像素；
* `dark` 那条腿瞄的是草稿那一行（1270 / 1277），也不是输入框上沿；
* 两条腿都给不出上沿，底部那一刀只能按版面比例切，切点落在输入框**里面**，
  输入框顶上那半句草稿（真机 175643 那一帧：「行，那就先这样，下次有机会再约。」）
  就跟着进了裁剪框，被 `is_other_bubble` 认成"对方原话"。

这一轮加的是第三条腿：输入框那个圆角矩形的**上边框**是一条横贯整宽、比聊天区底深一点点
（真机 231~249，底是 250）、又远没到"字"那么深的细线。`_detect_input_top_by_edge` 从窗口
底往上找第一条**够薄**的这种线，它的上沿就是输入框上沿。

这个文件按三件事验：

① **合成帧**——输入框上沿画在真机那个几何上（**字面量 1124 / 742×1342**，不是拿
   `INPUT_TOP_FRAC` 反推出来的：拿被测常量画被测对象就是自证），验那条腿认得出来、
   `fill` / `dark` 两条腿在这帧上确实量不出（证明是第三条腿在干活）；
② **真机帧**——19 张 742×1342 的帧，逐张对"该切多少"（短输入框 → 218、草稿把输入框顶高
   → 406 / 376 / 346 / 256）；帧不在盘上就 skip，不假绿；
③ **退化与"别的窗口压上来"**——没有线 / 线太厚（群公告气泡那种一整块浅色）/ 画面花了 /
   自己那扇窗被涂白之后，都不许硬切（回 None，交给比例那条），更不许把聊天区切走。

红线：零新依赖（只多标准库）；pet.db / mind.db 零写入；不模拟按键 / 不碰剪贴板。
"""

from __future__ import annotations

import pathlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    skip,
    summary,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    INPUT_EDGE_FRAC,
    INPUT_EDGE_MAX_PX,
    SAFE_FLOOR,
    bottom_cut_px,
    chat_area_box,
    classify_lines,
    detect_input_top,
    input_cut_px,
)
from life.screen.social_window import (  # noqa: E402
    _detect_input_top_by_dark,
    _detect_input_top_by_edge,
    _detect_input_top_by_fill,
)

DPR = 1.5

# ---------- 真机几何：照抄测量值，不是从常量反推的 ----------
#
# 2026-10-08 那批帧（本机 DPR=1.5、微信 4.x、742×1342）：输入框上沿在 y=1124（距底 218）。
# 草稿把输入框顶高时量到过 966 / 996 / 1086（真机 175630 / 175706 / 175643 那三帧）。
REAL_W, REAL_H = 742, 1342
REAL_INPUT_TOP = 1124
REAL_TALL_TOP = 966
REAL_TALL363 = 996          # 175706 那一帧
REAL_TALL718 = 1086         # 175643 那一帧
REAL_REPLY_LINE = "行，那就先这样，下次有机会再约。"     # 175643 那一帧灌进材料的草稿

CHAT_BG = QColor(250, 250, 250)      # 真机这一版聊天区底（群聊单页视图上就是这个灰白）
PEER_BG = QColor(255, 255, 255)      # 对方气泡（比底色白一档）
LINE_RGB = QColor(231, 231, 231)     # 输入框上沿那条边框线（真机 231~249 那一档）
TOOLBAR_RGB = QColor(232, 232, 232)  # 输入框底下那条工具栏（同样落在"线"那一档，但很厚）
TOOLBAR_PX = 24

PEER_LINE = "好呀，下次有机会再说"
PEER_TOP, PEER_BOTTOM = 1032, 1084   # 紧贴输入框上方那条对方末句
DRAFT_TOP = REAL_INPUT_TOP + 24      # 输入框顶上那半句草稿（175643 那一帧的样子）

FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/mt.ttf",
    "C:/Windows/Fonts/msyh.ttc",
)

FRAME_GLOB = "pet_social_leak_20261008_*.png"


def _install_real_font() -> str:
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        fid = QFontDatabase.addApplicationFont(path)
        fams = QFontDatabase.applicationFontFamilies(fid)
        if fams:
            return fams[0]
    return ""


def _frame(input_top: int = REAL_INPUT_TOP, *, draft: str = "",
           line: bool = True, toolbar: bool = True,
           block: tuple[int, int] | None = None) -> QImage:
    """真机那一版版面：聊天区底 250 + 对方气泡（255）+ 输入框上沿那条边框线 + 输入区。

    参数都是"可变的那几处"：
      · `input_top`——输入框上沿（草稿把它顶高时传真机那三个读数）；
      · `draft`——输入框里那半句草稿（画在框顶往下 24 像素处）；
      · `line`——画不画那条边框线（不画就是"这条腿该回 None"的现场）；
      · `toolbar`——输入框底下那条工具栏（比线厚得多，用来验"太厚的不算线"）；
      · `block`——在 `(y0, y1)` 之间压一整块浅色（群公告气泡 / 卡片那种），
                   用来验"另一块浅色不许被当成输入框上沿"。
    """
    img = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
    img.fill(CHAT_BG)
    p = QPainter(img)
    p.setFont(QFont("Sans", 20))
    # 对方那条末句（紧贴输入框上方）。字画在 x=110 起——裁剪框左沿由 `_chat_left` 定
    # （合成帧上量到 90），画到 90 以左就有一截落在裁剪框外，OCR 读出来会缺头一个字。
    p.fillRect(QRect(96, PEER_TOP, 420, PEER_BOTTOM - PEER_TOP), PEER_BG)
    p.setPen(QColor(20, 20, 20))
    p.drawText(QRect(110, PEER_TOP + 8, 384, 34), 0, PEER_LINE)
    if block is not None:
        p.fillRect(QRect(0, block[0], REAL_W, block[1] - block[0]), QColor(238, 238, 240))
    if line:
        p.fillRect(QRect(0, input_top, REAL_W, 2), LINE_RGB)
    if toolbar:
        p.fillRect(QRect(0, REAL_H - TOOLBAR_PX, REAL_W, TOOLBAR_PX), TOOLBAR_RGB)
    if draft:
        p.setPen(QColor(25, 25, 25))
        p.drawText(QRect(40, DRAFT_TOP, 620, 60), 0, draft)
    p.end()
    return img


def _noise_frame(width: int = REAL_W, height: int = REAL_H) -> QImage:
    """花花绿绿：底那一带没有哪个色占得到一半，两条腿都该老实回 None。"""
    palette = (QColor(200, 60, 60), QColor(60, 200, 60), QColor(60, 60, 200),
               QColor(220, 220, 40), QColor(40, 220, 220), QColor(220, 40, 220),
               QColor(30, 30, 30), QColor(240, 240, 240))
    img = QImage(width, height, QImage.Format_RGB32)
    p = QPainter(img)
    seed = 12345
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            p.fillRect(QRect(x, y, 4, 4), palette[(seed >> 16) % len(palette)])
    p.end()
    return img


def _real_frame(tag: str) -> QImage | None:
    """盘上那张真机帧；不在就回 None（调用方 skip，不假绿）。"""
    temp = pathlib.Path.home() / "AppData" / "Local" / "Temp"
    path = temp / f"pet_social_leak_20261008_{tag}.png"
    if not path.exists():
        return None
    img = QImage(str(path))
    if img.isNull() or (img.width(), img.height()) != (REAL_W, REAL_H):
        return None
    return img


def _rows(db_path: str, table: str) -> int:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        family = _install_real_font()
        frac_cut = int(REAL_H * (1 - INPUT_EDGE_BASE_FRAC()))     # 真机 1342 高上的比例那条

        # ---------- ① 合成帧：上沿画在真机那个几何（字面量 1124） ----------

        section("① 合成帧：输入框上沿画在 1124（字面量），第三条腿认得出来")

        img = _frame(REAL_INPUT_TOP, draft="我想说的是这个")
        top = detect_input_top(img, DPR)
        check("★ 量到输入框上沿 = 真机那个 1124（不是 1327 / None）",
              top is not None and abs(top - REAL_INPUT_TOP) <= 2, f"量到 {top}")
        check("★ 前两条腿在这帧上给不出真上沿（证明是第三条腿在干活）",
              _detect_input_top_by_fill(img, DPR, REAL_W, REAL_H) is None
              and _detect_input_top_by_dark(img, DPR, REAL_W, REAL_H) != REAL_INPUT_TOP,
              f"fill={_detect_input_top_by_fill(img, DPR, REAL_W, REAL_H)}"
              f" dark={_detect_input_top_by_dark(img, DPR, REAL_W, REAL_H)}"
              f"（dark 顶多咬到草稿那行，咬不到真上沿 1124）")
        check("★ 第三条腿自己就给 1124（不是被别的腿顺出来的）",
              _detect_input_top_by_edge(img, DPR, REAL_W, REAL_H) == REAL_INPUT_TOP,
              str(_detect_input_top_by_edge(img, DPR, REAL_W, REAL_H)))
        check("实切 = 高 − 上沿 = 218（整段输入框切掉）",
              input_cut_px(img, DPR) == REAL_H - REAL_INPUT_TOP,
              f"实切 {input_cut_px(img, DPR)}")
        area = chat_area_box(img, DPR)
        check("裁剪底边 = 输入框上沿（边框线下面那一整块不进 OCR）",
              area is not None and area[1] + area[3] == REAL_INPUT_TOP,
              "None" if area is None else str(area))
        check("紧贴输入框上方那条对方末句（底边 1084）在裁剪框里",
              area is not None and PEER_BOTTOM <= area[1] + area[3],
              "None" if area is None else f"底边 {area[1] + area[3]}")
        check("输入框里那半句草稿（框顶往下 24）一个像素都没进来",
              area is not None and area[1] + area[3] <= DRAFT_TOP,
              "None" if area is None else f"底边 {area[1] + area[3]} / 草稿顶 {DRAFT_TOP}")

        # 端到端：裁出来的那块里，对方的句子在、草稿不在
        if not family:
            skip("合成帧走 OCR 那条链", "没装上能画中文的真字体")
        else:
            img2 = _frame(REAL_INPUT_TOP, draft="我想说的是这个")
            p2 = QPainter(img2)
            p2.setFont(QFont(family, 20))
            p2.setPen(QColor(20, 20, 20))
            p2.drawText(QRect(110, PEER_TOP + 8, 384, 34), 0, PEER_LINE)
            p2.setPen(QColor(25, 25, 25))
            p2.drawText(QRect(40, DRAFT_TOP, 620, 60), 0, "我想说的是这个")
            p2.end()
            area2 = chat_area_box(img2, DPR)
            check("先量得出要裁哪一块", area2 is not None, str(area2))
            if area2 is not None:
                crop = img2.copy(QRect(*area2))
                arr = mon._to_numpy(crop)
                read = classify_lines(arr, mon.OcrReader().read_lines(arr))
                check("★ 对方那句在材料里", PEER_LINE[:6] in read.context, repr(read.context[:60]))
                check("★ 输入框里那半句草稿不在材料里（第⑦条的正主）",
                      "我想说的是这个" not in read.context,
                      "（混进来了）" if "我想说的是这个" in read.context else "（干净）")

        # ---------- ② 真机帧：逐张对"该切多少" ----------

        section("② 真机 742×1342 帧：短输入框切 218、草稿顶高的按实测切")

        # （时间戳, 该切多少, 量到的上沿, 说明）——**期望值全是字面量**，来自 2026-10-08
        # 那批帧的实测，没从 `INPUT_TOP_FRAC` 反推。帧不在盘上就 skip 那一张，
        # 不拿"没跑"当"通过"。
        #
        # 最后三张是"锚不住"的现场：输入框上沿那条线量不出来，量尺咬到的是**输入框自己
        # 的下边框**（y=1333，离窗底只剩 9 像素）。那个读数离比例那条（217）差了 208 像素，
        # 早超出 `INPUT_ANCHOR_TOL`（30），`input_cut_px` 照样回落到 217——宁可少切一像素
        # （多留一行输入框自己的边框线进 OCR，无害），也不许按那个假读数去切。
        REAL_CASES = (
            ("153717", 218, 1124, "输入框上沿 1124"),
            ("153728", 218, 1124, "输入框上沿 1124"),
            ("153745", 218, 1124, "输入框上沿 1124"),
            ("153758", 218, 1124, "输入框上沿 1124"),
            ("153812", 218, 1124, "输入框上沿 1124"),
            ("154233", 218, 1124, "输入框上沿 1124"),
            ("154506", 218, 1124, "输入框上沿 1124"),
            ("154716", 218, 1124, "输入框上沿 1124"),
            ("155507", 218, 1124, "输入框上沿 1124"),
            ("155518", 218, 1124, "输入框上沿 1124"),
            ("141205", 218, 1124, "输入框上沿 1124"),
            ("142453", 218, 1124, "输入框上沿 1124"),
            ("142235", 406, 936, "草稿把输入框顶到 936"),
            ("175630", 376, 966, "草稿把输入框顶到 966（一整段在框里）"),
            ("175706", 346, 996, "草稿把输入框顶到 996"),
            ("175643", 256, 1086, "草稿把输入框顶到 1086（灌进材料的就是它）"),
            ("154728", 217, 1333, "只咬到输入框自己的下边框（距底 9），按锚容差丢掉、回比例那条"),
            ("165246", 217, 1333, "只咬到输入框自己的下边框（距底 9），按锚容差丢掉、回比例那条"),
            ("185728", 217, 1333, "只咬到输入框自己的下边框（距底 9），按锚容差丢掉、回比例那条"),
        )
        seen = 0
        for tag, expect, expect_top, why in REAL_CASES:
            frame = _real_frame(tag)
            if frame is None:
                skip(f"真机帧 {tag}（{why}）", "这一张不在 %TEMP% 里")
                continue
            seen += 1
            cut = input_cut_px(frame, DPR)
            top = detect_input_top(frame, DPR)
            check(f"真机 {tag}：该切 {expect}（{why}）",
                  cut == expect, f"实切 {cut} / 量到上沿 {top}")
            check(f"真机 {tag}：量到的上沿 = 实测那个 {expect_top}",
                  top == expect_top, f"量到 {top}")
        if seen == 0:
            skip("整节真机帧", "这批真机帧一张都不在盘上")

        # 草稿那一帧：切完，灌进材料的那半句一定不在裁剪框里
        frame = _real_frame("175643")
        if frame is None:
            skip("真机 175643 那一帧的草稿", "这一张不在 %TEMP% 里")
        else:
            area = chat_area_box(frame, DPR)
            check("★ 真机 175643：裁剪底边落在输入框上沿（1086）之上，草稿整段切掉",
                  area is not None and area[1] + area[3] <= 1086,
                  "None" if area is None else f"底边 {area[1] + area[3]}")

        # ---------- ③ 退化：宁可不切，也不硬切 ----------

        section("③ 退化：没有线 / 线太厚 / 画面花了 → 都不硬切")

        no_line = _frame(REAL_INPUT_TOP, line=False)
        check("★ 没画那条边框线 → 第三条腿回 None（不猜）",
              _detect_input_top_by_edge(no_line, DPR, REAL_W, REAL_H) is None,
              str(_detect_input_top_by_edge(no_line, DPR, REAL_W, REAL_H)))

        thick = _frame(REAL_INPUT_TOP, line=True, block=(700, 1100))
        check(f"★ 一整块浅色（{1100 - 700} 像素厚，群公告气泡 / 卡片那种）不算线："
              f"厚度超过 {INPUT_EDGE_MAX_PX} 就不认",
              _detect_input_top_by_edge(thick, DPR, REAL_W, REAL_H) == REAL_INPUT_TOP,
              f"给了 {_detect_input_top_by_edge(thick, DPR, REAL_W, REAL_H)}")
        check("★ 那一块也不许把切点拉到 700（不许硬切掉大半个聊天区）",
              input_cut_px(thick, DPR) <= REAL_H - REAL_INPUT_TOP,
              f"实切 {input_cut_px(thick, DPR)}")

        noise = _noise_frame()
        check("画面花了 → 第三条腿回 None",
              _detect_input_top_by_edge(noise, DPR, REAL_W, REAL_H) is None,
              str(_detect_input_top_by_edge(noise, DPR, REAL_W, REAL_H)))
        check("画面花了 → 整条量尺回 None、按比例那条兜底",
              detect_input_top(noise, DPR) is None and input_cut_px(noise, DPR) == frac_cut,
              f"实切 {input_cut_px(noise, DPR)}")

        flat = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
        flat.fill(QColor(255, 255, 255))
        check("整帧一个色（没有比底色深的线）→ 第三条腿回 None",
              _detect_input_top_by_edge(flat, DPR, REAL_W, REAL_H) is None,
              str(_detect_input_top_by_edge(flat, DPR, REAL_W, REAL_H)))

        check("空画面 / 形状对不上都不炸",
              detect_input_top(QImage(), DPR) is None
              and detect_input_top(None, DPR) is None, "")
        check("小到排不出底那一条 → 退回绝对下限 `SAFE_FLOOR`",
              input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR) == SAFE_FLOOR,
              str(input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR)))

        # ---------- ④ 甲-risk：别的窗口压上来 / 自己那扇窗被涂白，都不许硬切 ----------

        section("④ 甲-risk：另一扇窗压住下半屏、或自己那扇窗被涂白 → 不硬切")

        # (a) 宠物自己那扇窗压在输入框左半边（`shot.grab_foreground_window` 涂白那一下
        #     刷的是纯白 255）：白块的上沿本身就是一条横贯不了整宽的浅色边。
        #     量尺要么认那条真边框线（1124）、要么回 None 走比例那条——不许把切点抬到
        #     白块上沿（1080）上去，那等于把输入框上面那几条消息全切了。
        masked = _frame(REAL_INPUT_TOP)
        pm = QPainter(masked)
        pm.fillRect(QRect(20, 1080, 260, 220), QColor(255, 255, 255))
        pm.end()
        edge_m = _detect_input_top_by_edge(masked, DPR, REAL_W, REAL_H)
        top_m = detect_input_top(masked, DPR)
        cut_m = input_cut_px(masked, DPR)
        check("★ 涂白那一下：**新加的这条腿**没把白块上沿（1080）当成输入框上沿",
              edge_m is None or edge_m == REAL_INPUT_TOP,
              f"第三条腿给 {edge_m}")
        check("★ 涂白那一下：切点没被白块上沿（1080 → 那就是 "
              f"{REAL_H - 1080}）带到聊天区上半截去",
              top_m != 1080 and cut_m <= REAL_H // 2,
              f"量到 {top_m} / 实切 {cut_m}")
        check("★ 也不是「整块切走」：消息区至少还剩上半屏往下那一大截",
              REAL_H - cut_m >= REAL_H // 2,
              f"裁剪底边 {REAL_H - cut_m}")

        # 附记（不是断言，是给后来人的指路牌）：这张合成帧上白块盖掉了那条边框线
        # 宽度的 35%（260 / 742），`edge_frac` 掉到 0.65 < `INPUT_EDGE_FRAC`（0.8），
        # 第三条腿认不出线 → 让位给最后那条 `dark` 腿，而 dark 咬到的是**对方那句
        # 自己的字**（1048），实切 294。这是 `dark` 腿早就有的毛病（v2-social-input-region-leak-fix
        # 那轮就记着它"只瞄得见字"），**这一轮没动它**——改它等于动"切多少"的那条链，
        # 是另一批的事。这一轮管的是"新加的那条腿不许自己造出硬切"，上面三条断的就是这个。

        # (b) 另一扇窗横压在下半屏（一条窗口底边 + 底下是别的画面）：
        #     那扇窗的底边同样是一条横贯的线，但不该被当成输入框上沿。
        other = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
        other.fill(QColor(60, 70, 90))                       # 另一扇窗（深色，不是聊天底）
        po = QPainter(other)
        po.fillRect(QRect(0, 0, REAL_W, 700), CHAT_BG)        # 上半截才是聊天窗
        po.fillRect(QRect(0, 700, REAL_W, 2), LINE_RGB)       # 那扇窗的底边
        po.end()
        top_o = detect_input_top(other, DPR)
        cut_o = input_cut_px(other, DPR)
        check("★ 压上来的另一扇窗：切点不许被它那条底边（700）带走，仍回落到比例那条",
              cut_o <= frac_cut,
              f"量到 {top_o} / 实切 {cut_o}（被带走的话是 {REAL_H - 700}）")

        # ---------- ⑤ 量尺常量自洽 ----------

        section("⑤ 量尺常量自洽：边框线判据的那几条摆在明处")

        check("`INPUT_EDGE_FRAC` 是「够宽才算横贯一条线」的比例，落在 (0.5, 1] 之间",
              0.5 < INPUT_EDGE_FRAC <= 1.0, str(INPUT_EDGE_FRAC))
        check("`INPUT_EDGE_MAX_PX` 是「太厚就不是线」的上限，是个小整数",
              1 <= INPUT_EDGE_MAX_PX <= 32, str(INPUT_EDGE_MAX_PX))

        # ---------- ⑥ 红线：零新依赖 / 两库零写入 ----------

        section("⑥ 红线：零新依赖 / 两库零写入")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        import ast
        import inspect

        from life.screen import social_window as swmod

        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(swmod))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2"}
        check("量尺只用标准库 / 既有的像素取样那只手，没有新 import 的第三方库",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("输入框上沿那条边框线（v2-social-input-edge-line）")


def INPUT_EDGE_BASE_FRAC() -> float:
    """比例那条（`1 − INPUT_TOP_FRAC`）——从生产常量读，因为这一条**不是在测它**。

    这个文件测的是"第三条腿（边框线）认不认得出上沿"，比例那条只是期望值里的一个兜底
    项；合成帧那三条断言用的是**字面量 1124**，没从常量反推。
    """
    from life.screen.social_window import INPUT_TOP_FRAC
    return INPUT_TOP_FRAC


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
