"""
空材料：切点太高、对方末句被切走 + 会话列表草稿预览被当对方话（v2-social-material-empty-fix）。

真机（2026-10-08 09:44 / 09:58 的 DEBUG 日志）那两件事：

**15-X · 空材料。** 帧 744×1342，`detect_input_top` 一次给 1327、一次给 None，老口径
`input_cut_px = max(bottom_cut_px=144, …)` 两种都落回 **144** —— 切点 1198。可那扇窗上
输入区是从 **1269**（距底 73）才开始的：**紧贴输入框上方那条对方末句（底边 1252）被一起
切走**，材料于是是空的（"对方那条没认到"）。那个 `max()` 的立意是"绝不会比原来切得少"，
代价就是"输入区比 144 薄"这件事**结构性地**算不出来。现在改成：实测上沿优先（但要"不薄于
按比例那条"才采信，防 1327 那种错锚），按真机比例那条（≈83）兜底，底下压一道 `SAFE_FLOOR`。

**15-Y · 污染。** 会话列表里"这条会话压着没发出去的草稿"时预览长成 `[草稿] 你敲的那半句`，
真机那两拍把 `[草稿 行，那我就等着看。别到时候光打雷不下雨，我可记着呢。` 当成了"对方原话"
（`CHAT_LIST_DRAFT_PREFIX`）。**它长在列表那一列，本就不该进裁剪框**；治不了几何之前，
先在**文本这一层**把整行丢掉（`is_chat_list_draft`），并且把"这一屏漏了列表"记成
`ChatRead.list_draft_rows`，让装配层落一张临时图给真机复验。

红线：零新依赖（只用既有的 Qt / 标准库）；pet.db / mind.db 零写入；不模拟按键 / 不碰剪贴板。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from life.screen.social import _tail_context  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    CHAT_LIST_DRAFT_PREFIX,
    INPUT_TOP_FRAC,
    SAFE_FLOOR,
    SENDER_OTHER,
    SENDER_UNKNOWN,
    bottom_cut_px,
    chat_area_box,
    classify_lines,
    detect_input_top,
    input_cut_px,
    is_chat_list_draft,
)

DPR = 1.5

# 真机那一帧（2026-10-08，本机 DPR=1.5、微信 4.x 主窗）量到的数——**照抄测量值，不照抄常量**：
# 拿 `INPUT_TOP_FRAC` / `INPUT_BOX_PX` 去画被测对象就是自证（v2-social-input-guard 修过那处假绿）。
REAL_W, REAL_H = 744, 1342
REAL_INPUT_TOP = 1124        # 输入区上沿：真机 153717 那一帧的读数（距底 218 = 83.8%）
REAL_INPUT_TOP_TALL = 966    # 草稿把输入框顶高时的样子：真机 175630 那一帧的读数（距底 376）
REAL_INPUT_TOP_MID = 1086    # 真机 175643 那一帧的读数（距底 256）——草稿就在框里那半句
REAL_INPUT_TOP_MID2 = 996    # 真机 175706 那一帧的读数（距底 346）
REAL_PEER_TOP = 1032         # 对方末句那条：1032..1084，紧贴在输入区之上
REAL_PEER_BOTTOM = 1084      # 底边距底 258，正好落在输入区上沿（1124）上头

CHAT_BG = QColor(255, 255, 255)     # 聊天区底
INPUT_BG = QColor(250, 250, 250)    # 真机实测的输入区底（跟聊天区只差 5）
INK = QColor(25, 25, 25)            # 字（深色条，不用字体，离屏上省掉字体依赖）
GRAY = QColor(128, 128, 128)        # 认不出说话人的那一块（灰的：既不是白气泡，也不是自己那半边绿）

# 灰块的位置与它上面那行字（离底那条带子很远，不参与量尺）
GRAY_BOX = (500, 100, 100, 40)
UNKNOWN_BOX = [510, 105, 590, 135]


def _frame(input_top: int, input_bg: QColor = INPUT_BG, peer: bool = True) -> QImage:
    """真机那一帧的形状：聊天区（白）+ 对方末句（白底深字）+ 输入区（250）。

    `input_top` / `input_bg` 留给几个变体：输入区上沿的位置（草稿顶高 / 真机那个薄输入区）、
    输入区与聊天区**同色**（`detect_input_top` 老实回 None）。
    """
    img = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
    img.fill(CHAT_BG)
    p = QPainter(img)
    if peer:
        p.fillRect(QRect(60, REAL_PEER_TOP, 420, REAL_PEER_BOTTOM - REAL_PEER_TOP), INPUT_BG)
        p.fillRect(QRect(78, REAL_PEER_TOP + 8, 384, 34), INK)
    p.fillRect(QRect(0, input_top, REAL_W, REAL_H - input_top), input_bg)
    p.fillRect(QRect(90, input_top + 24, 380, 30), INK)          # 草稿那行
    # 一块认不出说话人的底色（灰）：既不是白气泡（对方）、也不是自己那半边绿
    p.fillRect(QRect(*GRAY_BOX), GRAY)
    p.end()
    return img


def _noise_frame(width: int = 744, height: int = 1342) -> QImage:
    """花花绿绿：底那一条里没有哪个色占得到一半，量尺该老实回 None。"""
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


def _rows(db_path: str, table: str) -> int:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    frac_cut = int(REAL_H * (1 - INPUT_TOP_FRAC))       # 真机 1342 高上就是 217（83.8% 那条）
    old_cut = bottom_cut_px(REAL_H, DPR)                # 144：老版面那条（切点 1198）
    try:
        # ---------- ① 真机那一帧：切点该落在输入区上沿 1124，不是 1198 ----------

        section("① 真机 744×1342 那一帧：实切 218（上沿 1124），对方末句留住、输入框切掉")

        img = _frame(REAL_INPUT_TOP)
        top = detect_input_top(img, DPR)
        cut = input_cut_px(img, DPR)
        area = chat_area_box(img, DPR)
        crop_bottom = None if area is None else area[1] + area[3]

        check("前提：这一帧上量得出输入区上沿（就是真机那个 1124）",
              top is not None and abs(top - REAL_INPUT_TOP) <= 2, f"给了 {top}")
        check("★ 实切跟到实测上沿（218），**不是**版面那条 144 / 老比例那条 1269",
              cut == REAL_H - top and cut != old_cut and cut > frac_cut,
              f"实切 {cut} / 比例那条 {frac_cut} / 版面那条 {old_cut}")
        check(f"★ 裁剪底边 {REAL_INPUT_TOP}（老口径是 {REAL_H - old_cut}）",
              crop_bottom == REAL_H - cut, f"底边 {crop_bottom}")
        check("★ 紧贴输入框上方那条对方末句（底边 1084）留在裁剪框里",
              crop_bottom is not None and REAL_PEER_BOTTOM <= crop_bottom,
              f"末句底边 {REAL_PEER_BOTTOM} / 裁剪底边 {crop_bottom}")
        check("★ 输入区（1124 往下）一个像素都没伸进来：草稿照旧切掉",
              crop_bottom is not None and crop_bottom <= REAL_INPUT_TOP,
              f"裁剪底边 {crop_bottom} / 输入区上沿 {REAL_INPUT_TOP}")
        check("★ 老口径确实会把输入框顶上那截草稿留在框里（这就是第⑦条误判的来路）",
              REAL_H - old_cut > REAL_INPUT_TOP + 24,
              f"老切点 {REAL_H - old_cut} > 草稿顶 {REAL_INPUT_TOP + 24}")

        # ---------- ② 量尺认得出时按实测走（输入区比版面那条高：草稿把它顶高） ----------

        section("② 量尺认得出就按实测，不按比例（批次12 那条腿没松）")

        for input_top, label in ((REAL_INPUT_TOP_TALL, "真机 175630 那一帧（草稿一整段在框里）"),
                                 (REAL_INPUT_TOP_MID2, "真机 175706 那一帧"),
                                 (REAL_INPUT_TOP_MID, "真机 175643 那一帧")):
            tall = _frame(input_top)
            tall_top = detect_input_top(tall, DPR)
            tall_cut = input_cut_px(tall, DPR)
            check(f"{label}：量到上沿 {input_top}，实切 {REAL_H - input_top}（跟着上沿走）",
                  tall_top is not None and abs(tall_top - input_top) <= 2
                  and tall_cut == REAL_H - tall_top,
                  f"量到 {tall_top} / 实切 {tall_cut}")
            check(f"{label}：实切比按比例那条（{frac_cut}）厚——该切多少切多少",
                  tall_cut > frac_cut, f"实切 {tall_cut} / 比例那条 {frac_cut}")

        # ---------- ③ 错锚：锚到比底带主色还深的工具栏 → 丢掉，回按比例那条 ----------

        section("③ 错锚：锚不到输入框底（底带只剩更深的工具栏）就丢掉，回按比例那条")

        # 真机 1327 那一下：输入框只剩贴底十几像素，底带里"最浅且不是聊天区底"的色锚到了
        # 输入框**底下**那条工具栏（比底带主色还深）。`_band_lightest` 那条新闸（锚比主色深
        # → None）当场把它挡在门外，量尺回 None；退一步真有薄读数溜过来，`input_cut_px` 那条
        # `INPUT_ANCHOR_TOL` 也会丢——两道都不许让错锚把消息区切走。
        for input_top, measured in ((1327, 15), (1300, 42)):
            thin = _frame(input_top)
            thin_top = detect_input_top(thin, DPR)
            check(f"★ 真机那种薄输入区（{measured}px）：量尺锚不到输入框底 → 老实回 None",
                  thin_top is None, f"量到 {thin_top}")
            check(f"★ 回按比例那条 {frac_cut}（不让错锚把消息区切走）",
                  input_cut_px(thin, DPR) == frac_cut,
                  f"实切 {input_cut_px(thin, DPR)}")

        # 退一步：真有薄读数溜到 `input_cut_px` 手上（底带最浅色确实更浅，但只浅出一条），
        # `INPUT_ANCHOR_TOL` 也得把它丢掉。
        thin2 = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
        thin2.fill(QColor(250, 250, 250))
        pt = QPainter(thin2)
        pt.fillRect(QRect(0, REAL_H - 42, REAL_W, 42), QColor(255, 255, 255))
        pt.end()
        check("★ 溜过来的薄读数（42）被 `INPUT_ANCHOR_TOL` 丢掉，仍回按比例那条",
              input_cut_px(thin2, DPR) == frac_cut,
              f"实切 {input_cut_px(thin2, DPR)}")

        # ---------- ④ 退化与绝对下限 ----------

        section("④ 退化与绝对下限")

        same = _frame(REAL_INPUT_TOP, input_bg=CHAT_BG)
        check("输入区与聊天区同色 → 量尺回 None、按比例那条兜底（不是 144）",
              detect_input_top(same, DPR) is None and input_cut_px(same, DPR) == frac_cut,
              f"实切 {input_cut_px(same, DPR)}")
        noise = _noise_frame()
        check("画面花了 → 量尺回 None、按比例那条兜底（不是 144）",
              detect_input_top(noise, DPR) is None and input_cut_px(noise, DPR) == frac_cut,
              f"实切 {input_cut_px(noise, DPR)}")
        check("尺寸都取不到 → 退回版面那条老口径（不拿坏尺寸乘出负数）",
              input_cut_px(QImage(), DPR) == bottom_cut_px(0, DPR) == 144, "")
        check("小到排不出底那一条 → 退回绝对下限 `SAFE_FLOOR`（不切出 0 / 负数）",
              input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR) == SAFE_FLOOR == 40,
              str(input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR)))

        # ---------- ⑤ 会话列表的草稿预览不进"谁说的话" ----------

        section("⑤ 15-Y 会话列表草稿预览（`[草稿] …`）不进材料")

        check(f"常量就是客户端那个标记 {CHAT_LIST_DRAFT_PREFIX!r}",
              CHAT_LIST_DRAFT_PREFIX == "[草稿", CHAT_LIST_DRAFT_PREFIX)
        check("真机那一行认得出（末尾带不带 `]` 都认）",
              is_chat_list_draft("[草稿 行，那我就等着看。别到时候光打雷不下雨，我可记着呢。")
              and is_chat_list_draft("[草稿] 你敲的那半句"), "")
        check("OCR 吐出来的空格前后没准，也认",
              is_chat_list_draft("  [草稿   行，那我就等着看"), "")
        check("只在**行首**认：句子中间出现的 `[草稿` 是人话，不许吞",
              not is_chat_list_draft("他说 [草稿 两个字是人话")
              and not is_chat_list_draft("行，那我就等着看。"), "")

        draft_text = "[草稿 行，那我就等着看。别到时候光打雷不下雨，我可记着呢。"
        read = classify_lines(img, [([0, 0, 200, 20], draft_text),
                                    ([60, REAL_PEER_TOP + 8, 462, REAL_PEER_TOP + 42],
                                     "那我就等着看")])
        check("★ 草稿预览整行丢掉，剩下的照常按气泡分说话人（对方那条还是 other）",
              read.list_draft_rows == 1
              and [t for _s, t in read.lines] == ["那我就等着看"]
              and read.lines[0][0] == SENDER_OTHER,
              f"list_draft_rows={read.list_draft_rows} lines={read.lines}")
        check("★ 它不在 `incoming` 里（不当对方原话）",
              "草稿" not in read.incoming and "那我就等着看" in read.incoming,
              repr(read.incoming))
        check("★ 也不在 `visible` / `context` 里（降级那条路也不带它）",
              "[草稿" not in read.visible and "[草稿" not in read.context, repr(read.visible))
        check("★ 端到端：`_tail_context` 里只有对方那条，没有那半句草稿",
              "[草稿" not in _tail_context(read) and "那我就等着看" in _tail_context(read),
              repr(_tail_context(read)))

        # 这一刀只砍草稿预览：认不出说话人的行照旧留着（记 UNKNOWN），不静默丢
        mixed = classify_lines(img, [(UNKNOWN_BOX, "这条认不出是谁说的")])
        check("别的不静默丢：认不出说话人的行照旧留着（记 UNKNOWN），不算草稿行",
              len(mixed.lines) == 1 and mixed.lines[0][0] == SENDER_UNKNOWN
              and mixed.lines[0][1] == "这条认不出是谁说的"
              and mixed.list_draft_rows == 0,
              f"lines={mixed.lines} list_draft_rows={mixed.list_draft_rows}")

        # ---------- ⑥ 复验：认出草稿预览就落一张临时图 ----------

        section("⑥ 15-Y 复验：认出草稿预览就落一张临时图（同一分钟只落一张）")

        import tempfile

        from life.screen import monitor as mon

        arr = mon._to_numpy(img)
        app._social_leak_dump_at = 0.0
        app._dump_social_leak(arr)
        pattern = "pet_social_leak_*.png"
        dumped = sorted(Path(tempfile.gettempdir()).glob(pattern),
                        key=lambda p: p.stat().st_mtime)
        check("★ 落了一张 `pet_social_leak_*.png`（真机复验那一张）",
              bool(dumped), "一张都没有")
        first = dumped[-1] if dumped else None
        if first is None:
            check("落下来的图能打开、尺寸就是那一帧（744×1342）", False, "前提没成立")
            check("同一分钟内不再落第二张", False, "前提没成立")
        else:
            shot = QImage(str(first))
            check("落下来的图能打开、尺寸就是那一帧（744×1342）",
                  shot.width() == REAL_W and shot.height() == REAL_H,
                  f"{shot.width()}x{shot.height()}")
            before = first.stat().st_mtime
            app._dump_social_leak(arr)          # 紧接着再来一次：该被间隔闸挡掉
            pump(0.01)
            again = sorted(Path(tempfile.gettempdir()).glob(pattern),
                           key=lambda p: p.stat().st_mtime)
            check("同一分钟内不再落第二张（漏是持续的，不然一秒一张）",
                  bool(again) and again[-1].stat().st_mtime == before,
                  f"{len(again)} 张")
            for p in again:
                if p.stat().st_mtime >= before:
                    p.unlink(missing_ok=True)

        # ---------- ⑦ 红线 ----------

        section("⑦ 红线：零新依赖 / 两库零写入")

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
        check("量尺那一层没有新引入第三方库",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("空材料：切点太高、对方末句被切 + 会话列表草稿预览当对方话"
                   "（v2-social-material-empty-fix）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
