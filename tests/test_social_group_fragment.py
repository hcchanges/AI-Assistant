"""群聊材料抓碎片（v2-social-group-fragment-leak）的自动测试。

真机 2026-10-08 16:52/16:54/16:55 那几拍：微信停在一页**群聊单页视图**上（从会话总列表
点进去的那一页，左边没有"会话列表"那一列），而 `detect_chat_left` 的主候选底色被**群公告
气泡的填充色** (238,238,240) 顶掉了真底色 (250,250,250)（差 34，`BG_TOL` 只有 6）——
两条腿一起空转回 None，`chat_area_box` 于是按版面硬切 `(60+250)×1.5 = 465`：**单页视图上
那条版面线在窗口里没有对应物**，一刀削掉左起 370 像素，群公告每一行都被削掉开头，OCR
只认回各行尾巴，材料成了 `平知学堂` 四个字（群公告里 `@知乎知学堂` 那条提及的残片）。
模型拿着四个字当然写不出话，用户看到的就是"稍等"。

这一批两处一起收：

  · **甲**（`social_window.py`）：左沿加一条**副底色候选**的腿（主候选估错时才跑，取
    "紧贴图标栏右缘那一小段的主色"），再加一档"左侧有没有会话列表列结构"的分档——
    没有就判**单页视图**、**放弃左切**（`SINGLE_PAGE_PAD_PX`）；有结构而没量到边界时
    照旧退回 465（主窗行为不变）；
  · **乙**（`social.py`）：材料那一层加一道**碎片闸**——纯 @提及 / 光秃秃一个短名 /
    装潢（"正在输入"）都不算"对方最近发来的那条"，接着往上一句找；一句都没有就回空串
    （空材料那条路 = "稍等、不拟稿"，上游批次17甲已经有了）。

红线老样子：零新依赖 / `pet.db`、`mind.db` 零写入 / 不模拟按键 / 不碰剪贴板。这一批
一个都不沾（连 OCR 都是拿现成的 `monitor.OcrReader` 读一张内存里的合成帧）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    ensure_app,
    make_env,
    note,
    section,
    skip,
    summary,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen.social import (  # noqa: E402
    _is_fragment_material,
    _tail_context,
    looks_like_list_page,
)
from life.screen.social_window import (  # noqa: E402
    BG_BAND,
    BG_RUN_MIN_PX,
    BG_RUN_PX,
    BG_TOL,
    CHAT_LIST_PX,
    SENDER_OTHER,
    SENDER_USER,
    SIDEBAR_PX,
    SINGLE_PAGE_PAD_PX,
    ChatRead,
    _alt_background_rgb,
    _dominant_rgb,
    _first_empty_run,
    _rgb_near,
    _scale,
    chat_area_box,
    classify_lines,
    cut_edges_box,
    detect_chat_left,
    has_chat_list_column,
    is_chat_list_draft,
)

ROOT = Path(__file__).resolve().parents[1]

FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/NotoSerifSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
)

# ---------- 合成帧的版面与颜色（照真机 742×1342 那一批摆） ----------

DPR = 1.5
PANE_W, PANE_H = 742, 1342          # 真机群聊单页窗的物理像素
SIDEBAR_BG = (237, 237, 237)        # 图标栏
PANE_BG = (250, 250, 250)           # 聊天区底色（真机那一帧上量的就是这个）
ANNOUNCE_BG = (238, 238, 240)       # 群公告气泡的填充色——**主候选会被它顶掉**
CARD_BG = (255, 255, 255)           # 文件卡片

PAD = 40                            # 图标栏右缘到公告气泡左缘那一小段（真机约 20~30）

ANNOUNCE_LINE = "群公告：本周直播安排表已送达，精彩提前看，别错过今晚八点的直播间。"
MENTION_LINE = "@知乎知学堂"          # 真机那条提及（残片就是它被切掉开头来的）
CARD_LINES = ("给AI销售打造一套知识库.pdf", "666.0K")

# 材料里总该能认出这几样之一（公告正文 / 文件卡片）——用来验"材料不是那四个字的残片"
DRAWN_HINTS = ("群公告", "直播安排", "直播间", "提前看", "知识库", "pdf", "PDF", "666",
               "AI销售", "知乎知学堂")


def _install_real_font() -> str:
    """装上能画中文的真字体（off screen 上不装就是一堆豆腐块，OCR 什么也认不到）。"""
    ensure_app()
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return ""


def _group_single_page(family: str) -> QImage:
    """一帧**群聊单页视图**：图标栏 + 聊天区（没有会话列表那一列）。

    要复现的形状只有一个：**主候选量到的是群公告气泡的填充色**（公告又长又有字，占满右半边，
    在"右半边主色"这件事上把真底色顶掉），于是主候选那两条腿一条都不中：

      · 长腿要 60 列连着的底色 —— 公告里每一列都有字（13 行正文，行距 60，那一列的底色占比
        过不了 `BG_COL_FRAC` 85%），找不着；
      · 短腿只看**紧贴图标栏右缘**那 18 列 —— 那儿是聊天区真底色 (250,250,250)，跟主候选
        (238,238,240) 差 34，`BG_TOL` 只有 6，也对不上。

    底下按真机那一帧的次序摆：**公告 → 提及行 → 文件卡片**（真机上被当成材料的那四个字，
    就是"文件卡片上面那行提及"被切掉开头来的）。
    """
    image = QImage(PANE_W, PANE_H, QImage.Format_RGB32)
    image.fill(QColor(*PANE_BG))
    painter = QPainter(image)
    try:
        sidebar = int(SIDEBAR_PX * DPR)
        painter.fillRect(0, 0, sidebar, PANE_H, QColor(*SIDEBAR_BG))
        # 群公告：从图标栏右边一小段起，**横到右缘**、竖着占满统计区间（BG_BAND）
        x0 = sidebar + PAD
        painter.fillRect(x0, 200, PANE_W - x0, 800, QColor(*ANNOUNCE_BG))
        painter.setFont(QFont(family or "Sans", 22))
        painter.setPen(QColor(30, 30, 30))
        y = 240
        while y < 990:                              # 密到每一列都有字，长腿找不到那 60 列
            painter.drawText(x0 + 10, y, ANNOUNCE_LINE)
            y += 60
        # 提及行：真机上它就在文件卡片上面那一行
        painter.drawText(x0 + 10, 1070, MENTION_LINE)
        # 文件卡片（视觉最底下那一条对方消息）
        painter.fillRect(x0 + 10, 1100, 560, 110, QColor(*CARD_BG))
        painter.drawText(x0 + 30, 1146, CARD_LINES[0])
        painter.drawText(x0 + 30, 1192, CARD_LINES[1])
    finally:
        painter.end()
    return image


def _photo_single_page(family: str) -> QImage:
    """一帧**单页视图、聊天区被纹理糊住**的（真机 141124 那一帧的形状）。

    要复现的是"左沿量不出来，而左侧又没有会话列表列"那一档：聊天区上铺一层 4×4 的小块
    纹理（每 16 像素一块），于是
      · 每一列都有四分之一的行不是底色 → `_column_is_background` 的 85% 那条闸过不了；
      · 每一行只有八分之一的取样点不是底色 → 又到不了"这一行有东西"那条线（`blocks` 为 0）。
    两样都量不出来、左侧也数不着头像方块 → 判单页视图 → **放弃左切**。
    """
    image = QImage(PANE_W, PANE_H, QImage.Format_RGB32)
    image.fill(QColor(*PANE_BG))
    painter = QPainter(image)
    try:
        sidebar = int(SIDEBAR_PX * DPR)
        painter.fillRect(0, 0, sidebar, PANE_H, QColor(*SIDEBAR_BG))
        spot = QColor(120, 170, 230)
        for y in range(0, PANE_H, 16):
            for x in range(sidebar, PANE_W, 16):
                painter.fillRect(x, y, 4, 4, spot)
        painter.setFont(QFont(family or "Sans", 22))
        painter.setPen(QColor(30, 30, 30))
        painter.drawText(sidebar + 40, 600, "对方发来的一张图")
    finally:
        painter.end()
    return image


def _list_window() -> QImage:
    """**主窗**那一版：图标栏 + 310 逻辑像素宽的会话列表 + 聊天区（真机 1140×1350）。"""
    image = QImage(1140, 1350, QImage.Format_RGB32)
    image.fill(QColor(*PANE_BG))
    painter = QPainter(image)
    try:
        sidebar = int(SIDEBAR_PX * DPR)
        list_w = int(CHAT_LIST_PX * DPR)
        painter.fillRect(0, 0, sidebar, 1350, QColor(*SIDEBAR_BG))
        painter.fillRect(sidebar, 0, list_w, 1350, QColor(243, 243, 243))
        y = 60
        for _ in range(9):
            painter.fillRect(sidebar + 16, y, 88, 88, QColor(120, 120, 120))     # 头像方块
            painter.fillRect(sidebar + 118, y + 8, list_w - 150, 34, QColor(150, 150, 150))
            painter.fillRect(sidebar + 118, y + 52, list_w - 180, 30, QColor(170, 170, 170))
            y += 132
        chat_x0 = sidebar + list_w
        y = 120
        for i in range(7):
            # 气泡色要**离聊天区底色足够远**：白 (255,255,255) 离 (250,250,250) 只有 5，
            # 落在 `BG_TOL`(6) 里头，会被当成底色，量出来的"聊天区左沿"就恰好是列表右缘。
            painter.fillRect(chat_x0 + 20, y, 300, 52,
                             QColor(149, 236, 105) if i % 2 else QColor(238, 238, 238))
            y += 96
    finally:
        painter.end()
    return image


def _noise_frame(width: int = 1140, height: int = 1350) -> QImage:
    """花屏那一帧（跟 `test_social_ocr_catch._noise_frame` 同一个造法，八色拼花）。"""
    palette = (QColor(200, 60, 60), QColor(60, 200, 60), QColor(60, 60, 200),
               QColor(220, 220, 40), QColor(40, 220, 220), QColor(220, 40, 220),
               QColor(30, 30, 30), QColor(240, 240, 240))
    image = QImage(width, height, QImage.Format_RGB32)
    painter = QPainter(image)
    try:
        seed = 12345
        for y in range(0, height, 4):
            for x in range(0, width, 4):
                seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
                painter.fillRect(x, y, 4, 4, palette[(seed >> 16) % len(palette)])
    finally:
        painter.end()
    return image


def _primary_legs(image) -> int | None:
    """**老口径那两条腿**：只拿主候选（右半边主色）跑一遍长闸与短闸。

    测试里自己复刻一遍，是为了把"这一帧本来就是量不出来、旧口径会退回 465"这件事
    **量给断言的断前看**——不拿被测常量画被测对象，也不靠"改之前跑一遍记下来的数"。
    """
    width, height = image.width(), image.height()
    bg = _dominant_rgb(image, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    if bg is None:
        return None
    y0 = int(height * BG_BAND[0])
    y1 = max(int(height * BG_BAND[1]), y0 + 1)
    scale = _scale(DPR)
    run = max(8, min(int(BG_RUN_PX * scale), width // 8))
    floor = max(1, int(SIDEBAR_PX * scale))
    ceiling = max(floor + 1, int(width * 0.60))
    start = min(floor, max(0, width - 1))
    left = _first_empty_run(image, bg, y0, y1, start, ceiling, run)
    if left is not None:
        return left
    near = max(4, int(BG_RUN_MIN_PX * scale))
    return _first_empty_run(image, bg, y0, y1, start, start + near, near)


def main() -> int:
    box, stub, app = make_env()
    hard_cut = int((SIDEBAR_PX + CHAT_LIST_PX) * DPR)      # 465：老口径那条版面线
    try:
        family = _install_real_font()

        # ---------- ① 群聊单页视图：左沿不再被 60+250 硬切 ----------

        section("① 群聊单页视图：公告那几行的开头不再被削掉（甲）")

        group = _group_single_page(family)
        bg = _dominant_rgb(group, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
        check("前置：主候选量到的底色是**公告气泡的填充色**（真机那一帧就是这么被顶掉的）",
              bg is not None and _rgb_near(bg, ANNOUNCE_BG, 4),
              "bg=%s（公告填充 %s / 聊天区底 %s）" % (bg, ANNOUNCE_BG,
                                                  PANE_BG))
        check("前置：老口径那两条腿在这一帧上一条都不中（所以旧代码会硬切 465）",
              _primary_legs(group) is None, "给了 %s" % (_primary_legs(group),))
        alt = _alt_background_rgb(group, DPR)
        check("副候选取到的是**聊天区底色**（紧贴图标栏右缘那一小段）",
              alt is not None and _rgb_near(alt, PANE_BG, 4), "副候选 %s" % (alt,))

        left = detect_chat_left(group, DPR)
        check("★ detect_chat_left 不再回 None（副候选那条腿把它接住了）",
              left is not None, "None")
        check("★ 量出来的左沿在聊天区左缘那一段（真机 92/93，这里约 90）",
              left is not None and int(SIDEBAR_PX * DPR) <= left <= 120,
              "给了 %s，老口径会切 %d" % (left, hard_cut))
        area = chat_area_box(group, DPR)
        check("★ 裁剪框左沿 = 量出来的那条（不再等于 465）",
              area is not None and area[0] == left and area[0] != hard_cut, str(area))
        check("★ 公告正文那一行的开头落在裁剪框里（老口径把它整段削掉）",
              area is not None and area[0] + area[2] > int(SIDEBAR_PX * DPR) + PAD + 26,
              "裁剪框 %s，正文起于 x=%d" % (area, int(SIDEBAR_PX * DPR) + PAD + 26))

        # ---------- ② 主窗不回归：有会话列表列时照旧按版面切 ----------

        section("② 主窗（有会话列表列）不回归：照旧按版面切 310 逻辑像素")

        main_win = _list_window()
        check("左侧数得着会话列表列结构（头像方块）",
              has_chat_list_column(main_win, DPR) is True, "判定为没有结构")
        check("主窗上照样量不出左边界（列表把两条腿都挡着）",
              detect_chat_left(main_win, DPR) is None,
              "给了 %s" % (detect_chat_left(main_win, DPR),))
        mbox = chat_area_box(main_win, DPR)
        check("★ 主窗照样按版面切：左沿 = 465 物理像素 = 310 逻辑像素",
              mbox is not None and mbox[0] == hard_cut, str(mbox))
        check("★ 会话列表那一列整块在裁剪框外（一个名字都不留给 OCR）",
              mbox is not None and mbox[0] >= int(SIDEBAR_PX * DPR) + int(CHAT_LIST_PX * DPR),
              str(mbox))
        mcut = cut_edges_box(main_win, DPR)
        check("最后一刀那条腿同一个口径（小窗那一档也按版面切）",
              mcut is not None and mcut[0] == hard_cut, str(mcut))

        # ---------- ③ 量不出来 + 左侧没有列结构 → 单页视图，放弃左切 ----------

        section("③ 单页视图（左侧没有会话列表列）且量不出来 → 放弃左切，不硬切")

        photo = _photo_single_page(family)
        check("前置：这一帧上左沿量不出来（三条腿都不中）",
              detect_chat_left(photo, DPR) is None,
              "给了 %s" % (detect_chat_left(photo, DPR),))
        check("前置：左侧也数不着会话列表列结构",
              has_chat_list_column(photo, DPR) is False, "判成了有结构")
        pbox = chat_area_box(photo, DPR)
        check("★ 判成单页视图 → 只留极小边距，**不**硬切 465",
              pbox is not None and pbox[0] == int(SINGLE_PAGE_PAD_PX * DPR)
              and pbox[0] < hard_cut, str(pbox))

        # ---------- ④ 花屏照旧硬切：这一档一个字没改 ----------

        section("④ 花屏（有列结构、量不出来）照旧退回硬切——老断言不能红")

        noise = _noise_frame()
        check("花屏：左侧数得着一整条杂色（判成有结构，不走单页那一档）",
              has_chat_list_column(noise, DPR) is True, "判成了单页")
        nbox = chat_area_box(noise, DPR)
        check("花屏：左沿照旧是那条版面线 465（`test_social_ocr_catch` 那条断言一字不动）",
              nbox is not None and nbox[0] == hard_cut, str(nbox))

        # ---------- ⑤ 碎片闸：三种形状不算材料 ----------

        section("⑤ 碎片闸：纯 @提及 / 光秃秃一个短名 / 装潢 都不算材料（乙）")

        screen = ["群公告：本周直播安排表已送达！", "平知学堂", "乎知学堂"]
        check("整条就是一个 @提及（真机那条提及完整时的形状）",
              _is_fragment_material("@知乎知学堂", screen) is True, "没认出来")
        check("整条就是一个 @所有人", _is_fragment_material("@所有人", screen) is True, "没认出来")
        check("同一屏里印了两遍的短名（平知学堂 / 乎知学堂，差一位、不在末位）",
              _is_fragment_material("平知学堂", screen) is True, "没认出来")
        check("装潢漏网的那一条：正在输入",
              _is_fragment_material("对方正在输入…", screen) is True, "没认出来")
        check("★ 人话不许误伤：一句话长短随意，照旧当材料",
              _is_fragment_material("我票都看好了", screen) is False, "被当成碎片了")
        check("★ 只差**末位**的短句（语气词那种）不算同一个名字",
              _is_fragment_material("我知道了", ["我知道了", "我知道啦"]) is False,
              "被当成碎片了")
        check("空材料照旧不算数（老口径：调用方当这条没东西可给）",
              _is_fragment_material("", screen) is True, "")

        # ---------- ⑥ 材料那一条：残片不当末句 ----------

        section("⑥ 材料不取残片：退回上一条有效消息，一条都没有就走空材料那条路")

        mention_last = ChatRead(lines=[
            (SENDER_OTHER, "群公告：本周直播安排表已送达！"),
            (SENDER_OTHER, MENTION_LINE),
        ])
        check("★ 最底下那一条是 @提及 → 材料退回**上一条有效消息**",
              _tail_context(mention_last) == "群公告：本周直播安排表已送达！",
              repr(_tail_context(mention_last)))
        check("那个 @提及一个字都没进材料",
              MENTION_LINE not in _tail_context(mention_last),
              repr(_tail_context(mention_last)))

        name_last = ChatRead(lines=[(SENDER_OTHER, "平知学堂"), (SENDER_OTHER, "乎知学堂")])
        check("★ 群昵称行单独成了末句 → 不当材料（退回上一句，也是残片就回空串）",
              _tail_context(name_last) == "", repr(_tail_context(name_last)))

        typing_last = ChatRead(lines=[(SENDER_OTHER, "在吗"), (SENDER_OTHER, "对方正在输入…")])
        check("装潢那一条也不当材料（退回上一条真话）",
              _tail_context(typing_last) == "在吗", repr(_tail_context(typing_last)))

        mine_last = ChatRead(lines=[(SENDER_OTHER, MENTION_LINE), (SENDER_USER, "在的，刚到家")])
        check("末尾是自己发的、对方最近那条又是残片 → 老实回空串（走空材料那条路）",
              _tail_context(mine_last) == "", repr(_tail_context(mine_last)))

        # ---------- ⑦ 端到端：合成群聊单页帧走完整条链（含真 OCR） ----------

        section("⑦ 端到端：合成群聊单页帧 → 裁剪 → OCR → 材料（不是那四个字）")

        if not family:
            skip("群聊单页帧端到端（材料不是残片）", "没装上能画中文的真字体")
        else:
            area = chat_area_box(group, DPR)
            check("先量得出要裁哪一块", area is not None, str(area))
            crop = group.copy(QRect(*area))
            arr = mon._to_numpy(crop)
            read = classify_lines(arr, mon.OcrReader().read_lines(arr))
            material = _tail_context(read)
            check("★ 材料不是那四个字的残片（群公告/文件卡片那几行里出）",
                  material != "" and material.strip() not in ("平知学堂", "知乎知学堂",
                                                              MENTION_LINE),
                  repr(material[:80]))
            check("★ 材料是从这一帧里画着的东西来的（公告正文 / 文件卡片）",
                  any(hint in material for hint in DRAWN_HINTS), repr(material[:80]))
            check("材料也不是列表草稿预览那一行",
                  is_chat_list_draft(material) is False, repr(material[:40]))
            check("★ 16-β/总列表判定不红：这一帧不是会话总列表",
                  looks_like_list_page(read) is False,
                  "判成了总列表（%d 行）" % len(read.lines))

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("群聊材料抓碎片（v2-social-group-fragment-leak）")


if __name__ == "__main__":
    raise SystemExit(main())
