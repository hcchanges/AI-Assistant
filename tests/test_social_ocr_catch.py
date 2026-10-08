"""正向抓取补盲 + 主语归属（v2-social-ocr-catch）的自动测试。

上一轮（v2-social-ocr-isolate）验的全是"**别**抓错"——宠物自己的窗别采、自己发的话别
当成对方说的、别开两个候选框。这一轮补的是它的反面：**该抓到的，一个都不许漏**，
外加一条"别把主客说反"。

真机上量的三条硬事实（2026-10-06，DPR=1.5，微信 4.x；见 `社会窗口` 那一节的注释）：

1. **自己那半边是 (157,242,159)**，不是 3.x 的 #95EC69=(149,236,105)。老口径拿固定色号
   比，蓝通道差 54 → 判成"认不出"，于是"末尾是自己发的"这条腿整个失灵。
2. **老口径左切 32%** 在一扇 759px 宽的窗上是 242px，而对方的气泡从 x≈174 就开始了
   → **每条消息的开头都被削掉**（真机版面上是"今天玩得开心吗"变成"元得开心吗"这种，
   窗再窄一点就整条消失）。
3. **老口径下切 26%** 落在窗口 74% 处，而真机上输入区从 93.8% 才开始 → 白丢两成消息区，
   越是新的消息越在下头，正好把最要紧的那条扔掉。

所以这一轮：气泡改判**色相**（绿压过红蓝就算自己的），版面改按**真机量的固定像素**切，
并且**先按像素找聊天区的左边界**（连着 40 列空列才开始），找不到才退回按版面硬切。

红线：零新依赖（隔离层仍只用标准库）、pet.db / mind.db 零写入、不模拟按键 / 不碰剪贴板。
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
    pump,
    section,
    skip,
    summary,
    wait_for,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter  # noqa: E402

from brain.prompts.templates import SOCIAL_DRAFT_PROMPT  # noqa: E402
from life.screen import monitor as mon  # noqa: E402
from life.screen.social import CHAT_CONTEXT_CLIP, _tail_context, _tail_lines  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    CHAT_LIST_PX,
    INPUT_BOTTOM_MIN_FRAC,
    INPUT_BOX_PX,
    INPUT_TOP_FRAC,
    SENDER_OTHER,
    SENDER_UNKNOWN,
    SENDER_USER,
    SIDEBAR_PX,
    TITLE_BAR_PX,
    ChatRead,
    as_chat_read,
    bottom_cut_px,
    box_bounds,
    bubble_rgb,
    chat_area_box,
    chat_client_rect,
    classify_lines,
    cut_edges_box,
    detect_chat_left,
    group_rows,
    input_cut_px,
    is_self_bubble,
    sender_from_bubble,
    tidy_lines,
)

from life.screen.social_window import (  # noqa: E402
    _joinable,
    _repair_senders,
)

ROOT = Path(__file__).resolve().parents[1]

FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/NotoSerifSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
)

# ---------- 真机量到的版面与颜色（不是随手编的数） ----------

DPR = 1.5
WIN_W, WIN_H = 1140, 1350          # 物理像素 ≈ 760×900 逻辑（微信主窗：带会话列表）
SOLO_W, SOLO_H = 760, 1352         # 真机上抓到的那扇**独立聊天窗**（759×1352，没有会话列表）
GUTTER_PX = 66                     # 聊天区左缘 → 对方气泡左缘（真机 108 → 174），物理像素

SELF_GREEN = QColor(157, 242, 159)     # 4.x 实测的绿
SELF_GREEN_3X = QColor(149, 236, 105)  # 3.x 的绿（两种都得认）
PEER_BG = QColor(250, 250, 250)        # 实测的对方气泡
CHAT_BG = QColor(255, 255, 255)        # 聊天区底
SIDEBAR_BG = QColor(237, 237, 237)     # 图标栏
LIST_BG = QColor(243, 243, 243)        # 会话列表

# 这一屏里各说各的话（断言只认这些句子，别的不许进 context）
PEER_LINES = ("今天玩得开心吗", "那周末一起爬山吧", "我最近有点忙")
SELF_LINE = "挺开心的你呢"
LIST_NAME = "盛华小区物业群"        # 会话列表里的名字（进了 context 就是把别人家的事当对方原话）
DRAFT_LINE = "我想说的是这个"        # 输入框里敲了一半的草稿

_ROW_H = 132

NEWEST_LINE = "在吗，我刚到家"    # 贴着输入框上方的那条「最新消息」（最容易被底部那一刀啃掉）


def _input_top(height: int) -> int:
    """合成窗里输入区的上沿（真机量到的比例：83.8% 起）。

    **故意不引 `INPUT_BOX_PX`**：拿被测常量去画被测对象是自证——常量改小，输入区跟着
    变小，"草稿没漏进来"就永远绿（v2-social-input-guard 修的就是这处假绿）。
    """
    return int(height * INPUT_TOP_FRAC)


def _install_real_font() -> str:
    ensure_app()
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return ""


def _bubble(painter: QPainter, rect: QRect, color: QColor, text: str) -> None:
    painter.fillRect(rect, color)
    painter.setPen(QColor(20, 20, 20))
    painter.drawText(rect.adjusted(18, 0, -18, 0), 0x0001 | 0x0080, text)


def _wechat_window(family: str, peer_lines=PEER_LINES, self_line=SELF_LINE,
                   list_name=LIST_NAME, draft=DRAFT_LINE,
                   width: int = WIN_W, height: int = WIN_H,
                   bottom_peer: str = "") -> QImage:
    """画一扇**照真机版面**的微信窗：图标栏 +（可选）会话列表 + 聊天区 + 输入框。

    尺寸、切边、颜色全按真机上量到的来（带 dpr），所以这帧能直接喂生产那条链。
    `list_name` 传 None 就是**独立聊天窗**那条版面（没有会话列表，聊天区紧挨着图标栏）
    ——真机上抓到的那一扇正是这个版面（759×1352，聊天区从 x≈108 就开始了）。

    `bottom_peer` 是**紧挨输入框上方**再加的那一条（对方最新那条），用来验"底部那一刀
    只切输入区、不啃消息"（v2-social-input-guard）。
    """
    image = QImage(width, height, QImage.Format_RGB32)
    image.fill(CHAT_BG)
    painter = QPainter(image)
    painter.setFont(QFont(family or "Sans", 22))

    sidebar_w = int(SIDEBAR_PX * DPR)
    list_w = int(CHAT_LIST_PX * DPR) if list_name else 0
    title_h = int(TITLE_BAR_PX * DPR)
    # 输入区上沿按**真机量到的比例**（83.8%）定，**不再按 `INPUT_BOX_PX` 算**（v2-social-
    # input-guard）。原来 `input_h = INPUT_BOX_PX * DPR` 是被测对象跟着被测常量走：
    # 常量改小，输入区一起变小，"草稿没漏进来"永远绿——那是一条自证等式。
    input_top = _input_top(height)
    chat_x0 = sidebar_w + list_w

    # 左边那条图标栏（没文字）
    painter.fillRect(QRect(0, 0, sidebar_w, height), SIDEBAR_BG)
    painter.setPen(QColor(120, 120, 120))
    for i in range(5):
        painter.drawText(QRect(0, 60 + i * 90, sidebar_w, 60), 0x0004, "◻")

    if list_w:
        # 会话列表：每一行都有头像和名字，所以**没有一列是干净的**（像素法正是靠这个跳过它）
        painter.fillRect(QRect(sidebar_w, title_h, list_w, input_top - title_h), LIST_BG)
        for i in range(6):
            y = title_h + 16 + i * _ROW_H
            painter.fillRect(QRect(sidebar_w + 16, y + 8, 88, 88), QColor(176, 198, 222))
            painter.setPen(QColor(50, 50, 50))
            painter.drawText(QRect(sidebar_w + 118, y + 8, list_w - 130, 46), 0x0000, list_name)
            painter.setPen(QColor(150, 150, 150))
            painter.drawText(QRect(sidebar_w + 118, y + 56, list_w - 130, 40), 0x0000,
                             "好的没问题收到啦")

    # 顶部标题栏
    painter.fillRect(QRect(0, 0, width, title_h), QColor(240, 240, 240))
    painter.setPen(QColor(40, 40, 40))
    painter.drawText(QRect(chat_x0 + 24, 10, 600, title_h - 20), 0x0000, "老王的聊天")

    # 聊天区里的气泡：对方的**紧贴聊天区左边**（正是老口径按比例切掉的那一块），自己靠右
    y = title_h + 40
    for index, text in enumerate(peer_lines):
        # `text` 里带 `\n` 就是**同一个气泡里的两行**（v2-social-multiline）——真机上
        # 对方一条消息写长了就是这样。气泡跟着长高，宽度按最长那行算。
        rows = str(text).splitlines() or [""]
        rect = QRect(chat_x0 + GUTTER_PX, y, 60 + 26 * max(len(r) for r in rows),
                     74 + (len(rows) - 1) * 56)
        _bubble(painter, rect, PEER_BG, text)
        y += rect.height() + 76
        if index == 0:
            rect = QRect(width - 60 - 26 * len(self_line) - 24, y,
                         60 + 26 * len(self_line), 74)
            _bubble(painter, rect, SELF_GREEN, self_line)
            y += 150

    # 紧挨输入框上方的**最新那条**：底部那一刀切多了就先啃掉它（v2-social-input-guard）。
    # 离输入区上沿 160 物理像素——真机上新消息就落在输入框正上方那一段。
    if bottom_peer:
        rect = QRect(chat_x0 + GUTTER_PX, input_top - 160 - 74,
                     60 + 26 * len(bottom_peer), 74)
        _bubble(painter, rect, PEER_BG, bottom_peer)

    # 底部输入框（用户敲了一半的草稿也在这儿——不切掉就会被当成对方的消息）
    painter.fillRect(QRect(0, input_top, width, height - input_top), QColor(250, 250, 250))
    painter.setPen(QColor(30, 30, 30))
    painter.drawText(QRect(chat_x0 + 24, input_top + max(0, (height - input_top) // 2 - 30),
                           700, 60), 0x0000, draft)
    painter.end()
    return image


def _noise_frame(width: int = WIN_W, height: int = WIN_H) -> QImage:
    """一帧"花花绿绿"的画面：认不出聊天区底色，像素法该老实回 None。

    真机上这种帧不罕见——聊天区被别的窗口挡了一半、或者在放视频。这时**不能**硬编一个
    边界，得退回按版面切。这里用小方块拼花，保证没有哪一列有 85% 是同一种颜色。
    """
    palette = (QColor(200, 60, 60), QColor(60, 200, 60), QColor(60, 60, 200),
               QColor(220, 220, 40), QColor(40, 220, 220), QColor(220, 40, 220),
               QColor(30, 30, 30), QColor(240, 240, 240))
    image = QImage(width, height, QImage.Format_RGB32)
    painter = QPainter(image)
    seed = 12345
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            painter.fillRect(QRect(x, y, 4, 4), palette[(seed >> 16) % len(palette)])
    painter.end()
    return image


def main() -> int:
    box, stub, app = make_env()
    try:
        family = _install_real_font()

        # ---------- ① 气泡色：绿就是自己的，认色相不认色号 ----------

        section("① 气泡色：4.x 实测的绿也算「自己发的」（老口径认不出）")

        check("3.x 的绿 → user", sender_from_bubble(
            (SELF_GREEN_3X.red(), SELF_GREEN_3X.green(), SELF_GREEN_3X.blue())) == SENDER_USER, "")
        check("4.x 实测的绿 (157,242,159) → user（老口径这里给 unknown）",
              sender_from_bubble((157, 242, 159)) == SENDER_USER, "")
        check("对方的白 → other", sender_from_bubble((250, 250, 250)) == SENDER_OTHER, "")
        check("聊天区底色（纯白）→ other（近白那档）",
              sender_from_bubble((255, 255, 255)) == SENDER_OTHER, "")
        check("浅灰（列表底）→ other", sender_from_bubble((243, 243, 243)) == SENDER_OTHER, "")
        check("深灰（工具栏 / 深色主题）→ 认不出，不硬编",
              sender_from_bubble((60, 60, 60)) == SENDER_UNKNOWN, "")
        check("蓝色（别的客户端）→ 认不出", sender_from_bubble((80, 140, 230)) == SENDER_UNKNOWN, "")
        check("暗绿（照片上的叶子）→ 认不出", sender_from_bubble((30, 90, 30)) == SENDER_UNKNOWN, "")
        check("取不到颜色 → 认不出", sender_from_bubble(None) == SENDER_UNKNOWN, "")
        check("绿/白两条判据互斥",
              is_self_bubble((255, 255, 255)) is False
              and is_self_bubble((157, 242, 159)) is True, "")
        check("老口径的容差参数还在（老调用不炸）", is_self_bubble((149, 236, 105), 26) is True, "")

        # ---------- ② 裁剪：只切窗口，不切消息 ----------

        section("② 裁剪范围：图标栏 / 会话列表 / 标题栏 / 输入区，一个不多一个不少")

        window = QRect(0, 0, 1000, 800)
        client = chat_client_rect(window)
        check("左切 = 图标栏 + 会话列表（固定像素，不随窗宽走）",
              client.left() == SIDEBAR_PX + CHAT_LIST_PX, f"{client.left()}")
        check("上切 = 标题栏", client.top() == TITLE_BAR_PX, f"{client.top()}")
        check("下切 = 输入区（不再一下切掉四分之一）",
              window.bottom() - client.bottom() == INPUT_BOX_PX,
              f"{window.bottom() - client.bottom()}")
        check("高分屏按倍数放大", chat_client_rect(QRect(0, 0, 1500, 1200), 1.5).left()
              == int((SIDEBAR_PX + CHAT_LIST_PX) * 1.5),
              f"{chat_client_rect(QRect(0, 0, 1500, 1200), 1.5).left()}")

        if not family:
            skip("合成聊天窗的像素法裁剪", "没装上能画中文的真字体")
        else:
            image = _wechat_window(family)
            left = detect_chat_left(image, DPR)
            check("像素法找到了聊天区的左边界（不是 0，也不是按比例拍脑袋）",
                  left is not None and left >= int(SIDEBAR_PX * DPR),
                  "None" if left is None else str(left))
            check("找出来的边界越过整段会话列表（列表右缘就是聊天区左缘）",
                  left is not None and left >= int((SIDEBAR_PX + CHAT_LIST_PX) * DPR),
                  "None" if left is None else f"{left} vs 列表右缘 "
                  f"{int((SIDEBAR_PX + CHAT_LIST_PX) * DPR)}")

            area = chat_area_box(image, DPR)
            check("量出来的那块在窗口里面", area is not None
                  and 0 <= area[0] and area[0] + area[2] <= WIN_W
                  and area[1] + area[3] <= WIN_H, str(area))
            check("下边界在输入框上面（新消息那一头不再白丢两成）",
                  area is not None and area[1] + area[3] <= _input_top(WIN_H),
                  f"{area} vs 输入区上沿 {_input_top(WIN_H)}")

            # 真机上抓到的那一扇是**独立聊天窗**：没有会话列表，聊天区紧挨图标栏。
            # 老口径在这个版面上按 32% 切（760*0.32=243），而对方气泡从 x≈156 就开始了。
            solo = _wechat_window(family, list_name=None, width=SOLO_W, height=SOLO_H)
            solo_left = detect_chat_left(solo, DPR)
            check("没有会话列表的独立聊天窗也认得出左边界（真机抓到的就是这种）",
                  solo_left is not None, "None" if solo_left is None else str(solo_left))
            check("独立窗量出来的左边界**比老口径（按 32% 切）更靠左**，靠边的对方气泡不再被削",
                  solo_left is not None and solo_left < int(SOLO_W * 0.32),
                  "None" if solo_left is None else
                  f"{solo_left} vs 老口径 {int(SOLO_W * 0.32)}")
            check("独立窗的下边界也贴在输入框上沿（新消息不再白丢）",
                  chat_area_box(solo, DPR) is not None
                  and chat_area_box(solo, DPR)[1] + chat_area_box(solo, DPR)[3]
                  <= _input_top(SOLO_H), str(chat_area_box(solo, DPR)))

        # ---------- ②-2 底部那一刀：固定 96 与窗口高度的 8% 取大，最新那条不许被啃掉 ----------
        #
        # v2-social-input-guard。原来底部只按固定的 96 逻辑像素切，而这个数**只在一扇
        # 1352 高的窗上量过**（真机输入区从 93.8% 才开始 ≈84 物理像素）。窗口拉到 2000+
        # 像素高时那一刀相对就薄了，所以压一条按比例的下限（8%），两条取大。这一节把公式
        # 和"切完之后最新那条还在不在"两头都摆出来。

        section("②-2 底部那一刀：固定 96 与窗口高度 8% 取大，且不许啃掉最新那条")

        check("矮窗（800，DPR 1）：固定那 96 更宽，听固定的",
              bottom_cut_px(800, 1.0) == INPUT_BOX_PX, str(bottom_cut_px(800, 1.0)))
        check("真机那扇窗（1352，DPR 1.5）：版面那条还是 144，可实测输入区（83.8%）有 220——"
              "版面那条**盖不住**，底部那一刀归 `input_cut_px` 管",
              bottom_cut_px(SOLO_H, DPR) == int(INPUT_BOX_PX * DPR)
              and SOLO_H - _input_top(SOLO_H) > bottom_cut_px(SOLO_H, DPR),
              f"版面那条 {bottom_cut_px(SOLO_H, DPR)} / 实测输入区 {SOLO_H - _input_top(SOLO_H)}")
        check("高窗（2400，DPR 1.5）：8% = 192 反超固定的 144，这时听比例的",
              bottom_cut_px(2400, DPR) == int(2400 * INPUT_BOTTOM_MIN_FRAC)
              and int(2400 * INPUT_BOTTOM_MIN_FRAC) > int(INPUT_BOX_PX * DPR),
              str(bottom_cut_px(2400, DPR)))
        check("高度取不到（0 / 负数）只出固定那一刀，不乘出个负数把整块画面切没",
              bottom_cut_px(0, DPR) == int(INPUT_BOX_PX * DPR)
              and bottom_cut_px(-5, DPR) == int(INPUT_BOX_PX * DPR),
              f"{bottom_cut_px(0, DPR)} / {bottom_cut_px(-5, DPR)}")

        if not family:
            skip("最新那条贴着输入框上方那两帧", "没装上能画中文的真字体")
        else:
            for label, w, h in (("真机那扇窗", SOLO_W, SOLO_H), ("高窗", WIN_W, 2400)):
                frame = _wechat_window(family, list_name=None, width=w, height=h,
                                       bottom_peer=NEWEST_LINE)
                area2 = chat_area_box(frame, DPR)
                if area2 is None:
                    skip(f"{label}：最新那条", "这一帧没量出聊天区")
                    continue
                out2 = mon._to_numpy(frame.copy(QRect(*area2)))
                read2 = classify_lines(out2, mon.OcrReader().read_lines(out2))
                check(f"{label}：贴着输入框上方的**最新那条**仍认得到（底部那一刀没啃掉它）",
                      "刚刚到家" in read2.context or NEWEST_LINE in read2.context,
                      repr(read2.context))
                check(f"{label}：它落在 context **末尾**（取到的是最新那条，不是上面更旧的）",
                      "到家" in read2.context.split("\n")[-1],
                      repr(read2.context.split("\n")[-1]))
                check(f"{label}：它归对方（可数的那半边），不是被当成自己的话丢掉",
                      any(s == SENDER_OTHER and "到家" in t for s, t in read2.lines),
                      str(read2.lines))
                check(f"{label}：输入框里那半句草稿仍不在 context 里",
                      DRAFT_LINE not in read2.context,
                      "（混进来了）" if DRAFT_LINE in read2.context else "（干净）")

        # ---------- ③ 正向用例：对方的话一条都不许漏 ----------

        section("③ 正向抓取：一屏里对方说的全进 context，自己 / 列表 / 草稿一个都不进")

        if not family:
            skip("整条链跑一帧真机版面的聊天窗", "没装上能画中文的真字体")
        else:
            image = _wechat_window(family)
            area = chat_area_box(image, DPR)
            check("先量出要裁哪一块", area is not None, str(area))

            # 走生产那条链：量框 → 裁 → 认字 → 分说话人
            crop = image.copy(QRect(*area))
            crop_arr = mon._to_numpy(crop)
            read = classify_lines(crop_arr, mon.OcrReader().read_lines(crop_arr))
            context = read.context

            missing = [line for line in PEER_LINES if line not in context]
            check("对方说的每一句都进了 context（**正向**：一条不漏）",
                  not missing, f"漏了 {missing}" if missing else f"{len(PEER_LINES)} 句都在")
            check("自己发的那句不在 context 里",
                  SELF_LINE not in context,
                  "（混进来了）" if SELF_LINE in context else "（干净）")
            check("会话列表里的名字不在 context 里",
                  LIST_NAME not in context,
                  "（混进来了）" if LIST_NAME in context else "（干净）")
            check("输入框里敲了一半的草稿不在 context 里",
                  DRAFT_LINE not in context,
                  "（混进来了）" if DRAFT_LINE in context else "（干净）")
            check("末尾那条分得出是对方发的", read.tail_sender == SENDER_OTHER,
                  read.tail_sender or "unknown")
            check("自己那句归 outgoing（没被当成对方）",
                  SELF_LINE in read.outgoing and SELF_LINE not in read.incoming,
                  read.outgoing or "（没认出来）")

            senders = {s for s, _t in read.lines}
            check("分出来的归属只有 user / other 两种（没有整帧降级）",
                  senders <= {SENDER_USER, SENDER_OTHER} and SENDER_USER in senders,
                  str(senders))
            check("3.x 皮肤的绿同样算自己发的（判据是色相，不是色号）",
                  is_self_bubble((SELF_GREEN_3X.red(), SELF_GREEN_3X.green(),
                                  SELF_GREEN_3X.blue())) is True, "")

            # —— 真机那扇独立聊天窗（最窄的版面，对方气泡贴着聊天区左边）——
            solo = _wechat_window(family, list_name=None, width=SOLO_W, height=SOLO_H)
            solo_arr = mon._to_numpy(solo)
            solo_area = chat_area_box(solo, DPR)
            check("独立窗也量得出一块", solo_area is not None, str(solo_area))

            # 先在**整帧**上认一遍，拿到每条气泡的真实坐标——用来证明老口径确实削到了它们
            full_lines = mon.OcrReader().read_lines(solo_arr)
            peer_boxes = [box_bounds(b) for b, t in full_lines
                          if any(line in t for line in PEER_LINES)]
            check("整帧上认得到对方那几条（前置条件成立）", len(peer_boxes) >= 2,
                  f"{len(peer_boxes)} 条")
            old_cut = int(SOLO_W * 0.32)
            check("老口径的左切确实会削到靠边的对方气泡（这就是漏抓的由来）",
                  any(bb is not None and bb[0] < old_cut for bb in peer_boxes),
                  f"最靠左的气泡 x={min((bb[0] for bb in peer_boxes if bb), default=None)}"
                  f" vs 老切 {old_cut}")

            if solo_area is not None:
                solo_crop = solo.copy(QRect(*solo_area))
                solo_out = mon._to_numpy(solo_crop)
                solo_read = classify_lines(solo_out,
                                           mon.OcrReader().read_lines(solo_out))
                solo_missing = [line for line in PEER_LINES if line not in solo_read.context]
                check("独立窗上对方每一句也都进了 context（老口径在这一扇上句句都被削掉开头）",
                      not solo_missing,
                      f"漏了 {solo_missing}" if solo_missing else f"{len(PEER_LINES)} 句都在")
                check("独立窗上末尾那条也归对方", solo_read.tail_sender == SENDER_OTHER,
                      solo_read.tail_sender or "unknown")
                check("独立窗上自己那句不进 context",
                      SELF_LINE not in solo_read.context,
                      "（混进来了）" if SELF_LINE in solo_read.context else "（干净）")
                check("独立窗上草稿也不进 context",
                      DRAFT_LINE not in solo_read.context,
                      "（混进来了）" if DRAFT_LINE in solo_read.context else "（干净）")

        # ---------- ③-2 多行消息：一条占几行，一行都不许丢 ----------
        #
        # v2-social-multiline。真机上漏掉的是这一种：对方**一条消息写了两行**——
        # 「这么快就放弃啦？\n我比较欣赏有毅力的男孩哟」，宠物只认了末行、回复也只回末行，
        # 开头那句试探整句没了。根因是两头一起犯的：拼接链路把换行拍成空格（`ChatRead`
        # 那几个 join + `_on_screen_read` 的 `" ".join(...split())`），提示词又写着
        # "只认对方最近发的那一条"。这一节把两种多行形状都摆出来验。

        section("③-2 多行消息：一条占几行也照样整条进 context，行序不丢")

        MULTI_TOPIC = "这么快就放弃啦？\n我比较欣赏有毅力的男孩哟"   # ① 同一个气泡两行两话题
        TEASE_LINE, JUDGE_LINE = "这么快就放弃啦？", "我比较欣赏有毅力的男孩哟"

        # OCR 会把全角「？」认成半角「?」（真机上也是这样）。断言里比的是"字在不在、
        # 顺序对不对"，不是标点长什么样，所以先归一化标点再比。
        def _same(text: str, needle: str) -> bool:
            return needle.replace("？", "?") in text.replace("？", "?")

        if not family:
            skip("多行气泡走真图那条链", "没装上能画中文的真字体")
        else:
            # ① 同一个气泡里两行：真机上对方一条消息写长了就是这样
            one = _wechat_window(family, peer_lines=(MULTI_TOPIC, "下次再约"))
            one_area = chat_area_box(one, DPR)
            if one_area is None:
                skip("同一个气泡两行", "这一帧没量出聊天区")
            else:
                one_arr = mon._to_numpy(one.copy(QRect(*one_area)))
                one_read = classify_lines(one_arr, mon.OcrReader().read_lines(one_arr))
                one_ctx = one_read.context
                lost = [s for s in (TEASE_LINE, JUDGE_LINE) if not _same(one_ctx, s)]
                check("① 同一气泡两行不同话题：两行**都在** context 里",
                      not lost, f"漏了 {lost}" if lost else repr(one_ctx))
                # "分开的行"要这么验：两句话落在**不同的行下标**上，且前面那句在下标小的那行
                rows = one_ctx.split("\n")
                top = next((k for k, ln in enumerate(rows) if "这么快就放弃" in ln), None)
                bottom = next((k for k, ln in enumerate(rows) if "我比较欣赏" in ln), None)
                check("① 两行是**分开的行**（换行语义保住了，没被拍成一串空格）",
                      top is not None and bottom is not None and top < bottom,
                      f"行下标 {top} / {bottom}，context={one_ctx!r}")
                check("① 两行都归对方（同一个气泡里的第二行不许掉成 UNKNOWN）",
                      all(s == SENDER_OTHER for s, t in one_read.lines
                          if TEASE_LINE in t or JUDGE_LINE in t),
                      str(one_read.lines))

            # ② 对方连发两条（各自一个气泡）：先调侃、后评价——顺序就是说话的先后
            two = _wechat_window(family, peer_lines=(TEASE_LINE, JUDGE_LINE))
            two_arr = mon._to_numpy(two.copy(QRect(*chat_area_box(two, DPR))))
            two_read = classify_lines(two_arr, mon.OcrReader().read_lines(two_arr))
            two_ctx = two_read.context
            two_rows = two_ctx.split("\n")
            two_top = next((k for k, ln in enumerate(two_rows) if "这么快就放弃" in ln), None)
            two_bottom = next((k for k, ln in enumerate(two_rows) if "我比较欣赏" in ln), None)
            check("② 对方连发两行（调侃 + 评价）：两条都在 context 里，顺序不乱",
                  _same(two_ctx, TEASE_LINE) and _same(two_ctx, JUDGE_LINE)
                  and two_top is not None and two_bottom is not None and two_top < two_bottom,
                  repr(two_ctx))
            check("② 连发的两条也各占一行",
                  two_top is not None and two_bottom is not None and two_top != two_bottom,
                  repr(two_ctx))
            check("② 末尾那条分得出是对方发的（连发时最后一句仍是对方）",
                  two_read.tail_sender == SENDER_OTHER, two_read.tail_sender or "unknown")

            # ③ 茗风式折行：一条消息写到一半、被气泡宽度折到下一行（「事情都比较」/「忙」）。
            # 真机这一轮报的正是它——宠物只拿到换行前段、漏掉"忙"，回复对后半句无响应。
            ming = _wechat_window(family, peer_lines=("可能要等几个月。这段时间，事情都比较\n忙",))
            ming_area = chat_area_box(ming, DPR)
            if ming_area is None:
                skip("茗风式折行", "这一帧没量出聊天区")
            else:
                ming_arr = mon._to_numpy(ming.copy(QRect(*ming_area)))
                ming_read = classify_lines(ming_arr, mon.OcrReader().read_lines(ming_arr))
                # OCR 会把「较」这类字偶尔认丢（真机也这样），断言只认"前段 + 折下来的后半段
                # 都在、且是两行拼着"，不苛求每个字都对。
                check("③ 茗风式折行：折下来的后半段「忙」也进了 context（不再只拿前段）",
                      "忙" in ming_read.context and "事情" in ming_read.context
                      and "可能要等几个月" in ming_read.context,
                      repr(ming_read.context))
                latest = ming_read.latest_other
                check("③ 折行那一条按『条』看是完整的一条（前半句 + 折下来的「忙」都在同一条）",
                      latest is not None and "事情" in latest.text and "忙" in latest.text
                      and "\n" in latest.text,
                      latest.text if latest else "（没取到）")

        # 纯逻辑那半边（不依赖字体）：拼接保换行、留尾按整行、提示词写明"多行是同一条"
        section("③-2b 多行那一层的纯逻辑：`tidy_lines` / `_tail_lines` / 提示词")

        check("行内空白压平、行与行的换行留住",
              tidy_lines("  这么快  就放弃啦？\n\n  我比较欣赏有毅力的男孩哟 ")
              == ["这么快 就放弃啦？", "我比较欣赏有毅力的男孩哟"], "")
        check("单行照旧（老行为一个字没变）",
              tidy_lines("今天玩得开心吗") == ["今天玩得开心吗"], "")
        check("空串 / 只有空白 → 没有行",
              tidy_lines("") == [] and tidy_lines("   \n  ") == [], "")

        check("ChatRead.incoming 一行一句（\\n 分隔，顺序自上而下）",
              ChatRead(lines=[(SENDER_OTHER, "这么快就放弃啦？"),
                              (SENDER_USER, "我没有"),
                              (SENDER_OTHER, "我比较欣赏有毅力的男孩哟")]).incoming
              == "这么快就放弃啦？\n我比较欣赏有毅力的男孩哟", "")
        check("ChatRead.visible 同样保换行（两边都算）",
              ChatRead(lines=[(SENDER_OTHER, "上"), (SENDER_USER, "下")]).visible == "上\n下", "")
        check("as_chat_read 收多行字符串也不拍平",
              as_chat_read("A\nB").context == "A\nB", repr(as_chat_read("A\nB").context))

        # 留尾：按**整行**往前捡，绝不切半句
        long_ctx = "\n".join(f"第{i}行" + "字" * 60 for i in range(1, 9))   # 每行 ≈65 字
        tail = _tail_lines(long_ctx)
        check("留尾留下的是**完整的行**（每一行都在原文里逐行出现）",
              all(ln in long_ctx.split("\n") for ln in tail.split("\n")), repr(tail[:60]))
        check("留尾留的是**末尾**那几行（最近的压在下面）",
              tail.split("\n")[-1] == long_ctx.split("\n")[-1], repr(tail.split("\n")[-1]))
        check("留的量不超过上限多少（约 600 字，一行之内的浮动）",
              len(tail) <= CHAT_CONTEXT_CLIP + 70, str(len(tail)))
        # 这一条正是真机的形状：最新那条本身就长过上限，老口径按字符切就会削掉它的开头
        long_last = _tail_lines("先说一句\n" + "很长的最后一条" * 120 + "收尾")
        check("★ 最新那条**一长**时，它的开头不许被削（老口径 `body[-600:]` 就削在这儿）",
              long_last.endswith("收尾") and long_last.startswith("很长的最后一条")
              and len(long_last) > CHAT_CONTEXT_CLIP,
              f"{len(long_last)} 字 / 开头 {long_last[:6]!r}")
        check("给的就是空 / 全空白 → 回空串（不是 None，也不是一句占位）",
              _tail_lines("") == "" and _tail_lines("  \n ") == "", "")

        # 只有一行的输入不是"多行消息"，是整屏被拍平的那种（OCR 降级 / 老桩回整段字符串）：
        # 那里没有行边界可守，退回老口径按字符取尾，"别被整屏旧字带跑"这条不能丢。
        flat = _tail_lines("旧" * 900 + "最新的那句")
        check("只有一行的输入按老口径切尾（没有行边界时保住'取末尾'那个初衷）",
              len(flat) == CHAT_CONTEXT_CLIP and flat.endswith("最新的那句"),
              f"{len(flat)} 字 / 结尾 {flat[-6:]!r}")
        check("单行但没超上限 → 一个字都不动",
              _tail_lines("就这一句") == "就这一句", "")

        check("提示词把'一条消息可能好几行'写进去了（多行是同一条，每行都要接）",
              "不止一行" in SOCIAL_DRAFT_PROMPT and "每一行都要接住" in SOCIAL_DRAFT_PROMPT
              and "只答末行" in SOCIAL_DRAFT_PROMPT, "")
        check("老那条'只认对方最近发的那一条'已经不再教它只看一行",
              "只认对方最近\n   发的那一条" not in SOCIAL_DRAFT_PROMPT
              and "只认对方最近发的那一条" not in SOCIAL_DRAFT_PROMPT, "")
        check("原来的主药一个字没动（先回应对方、不许升级话题）",
              "直接回应它" in SOCIAL_DRAFT_PROMPT
              and "不许自己把话题升级" in SOCIAL_DRAFT_PROMPT, "")
        check("提示词里那格明说'一行一句、换行分开'",
              "一行一句" in SOCIAL_DRAFT_PROMPT, "")

        # ---------- ③-3 折行续行认亲 + 按条取尾（v2-social-grab-fix） ----------
        #
        # 真机上这一轮报的两件事：①对方一条消息因气泡宽度折行（「可能要等几个月。这段时间，
        # 事情都比较」/「忙」），宠物只拿到换行前段；②等几分钟才反应、且回的是顶部旧消息。
        # 前者是"折行第二行没认上说话人就被 `incoming` 丢掉"，后者是"整屏旧字当上下文、
        # 没取最新那条"。这一节把两个新帮手按纯逻辑验一遍。

        section("③-3 折行续行认亲 + 按条取尾（v2-social-grab-fix）")

        # 折行的形状：两行贴得近、左缘对齐 → 认作同一个气泡里的上下行（续行继承说话人）
        near = ((100, 100, 500, 150), "可能要等几个月。这段时间，事情都比较", SENDER_OTHER)
        cont = ((100, 155, 300, 205), "忙", SENDER_UNKNOWN)          # 第二行没认上底色
        read = classify_lines(None, [])
        check("折行两行左缘对齐、行距小 → `_joinable` 认得出来（底下的数在阈值内）",
              _joinable(near[0], cont[0]) is True, "")
        gap = ((100, 100, 500, 150), "上一条", SENDER_OTHER)
        far = ((100, 260, 300, 310), "下一条", SENDER_UNKNOWN)        # 另一个气泡（空档大）
        check("两个气泡之间有空档 → `_joinable` 不认（不把两条并成一条）",
              _joinable(gap[0], far[0]) is False, "")
        # 框抖动那一两个像素（v2-social-material-empty-fix 15-X 的连带）：底部那一刀从
        # 老口径 144 改到实测上沿（当时量到 84）之后，裁出来的图高了 60 像素，RapidOCR 内部
        # 缩放比跟着变，同一行的框差 1 像素。真机上量到的实例就是这个形状：空档 11、行高 31
        # （阈值 31×0.35 = 10.85 —— 只超 0.15），老口径下"同一条消息当场被拆成两条"、
        # `latest_other` 只剩「忙」。加了抖动余量之后这 1 像素不算数。
        jit_prev = ((83, 70, 577, 101), "可能要等几个月。这段时间，事情都比较", SENDER_OTHER)
        jit_next = ((83, 112, 117, 143), "忙", SENDER_UNKNOWN)
        check("★ 框抖 1 像素（空档 11 / 行高 31，老口径只差 0.15 就拆条）→ 仍旧是同一条续行",
              _joinable(jit_prev[0], jit_next[0]) is True, "")
        check("抖动余量没把真气泡之间的空档也吃掉（同尺寸下 76 像素照旧隔开）",
              _joinable((83, 100, 500, 150), (83, 226, 300, 276)) is False, "")
        check("没框可比 → `_joinable` 不认亲（没有依据就不硬猜）",
              _joinable(None, cont[0]) is False and _joinable(near[0], None) is False, "")

        # 认亲 + 归条：第二行继承了上一行的说话人，且并进同一条
        rows = [near, cont]
        repaired = _repair_senders(rows)
        check("折行第二行认不出说话人 → 继承上一行（不再被 `incoming` 丢掉）",
              repaired[1][2] == SENDER_OTHER, str(repaired))
        grouped = group_rows(repaired)
        check("两行并成**一条**消息，正文用换行拼全",
              len(grouped) == 1 and grouped[0].text == "可能要等几个月。这段时间，事情都比较\n忙",
              repr([g.text for g in grouped]))
        check("那一整条归对方（`latest_other` 拿到的是拼全的那一条）",
              grouped[0].sender == SENDER_OTHER, "")

        # 对方连发两条（各自一个气泡、中间有空档）→ 归成两条，取最新那条 = 末一条整条
        two = [((100, 100, 500, 150), "这么快就放弃啦？", SENDER_OTHER),
               ((100, 300, 500, 350), "我比较欣赏有毅力的男孩哟", SENDER_OTHER)]
        two_read = ChatRead(
            lines=[(SENDER_OTHER, "这么快就放弃啦？"),
                   (SENDER_OTHER, "我比较欣赏有毅力的男孩哟")],
            groups=group_rows(_repair_senders(two)),
        )
        check("对方连发两条（中间空档）→ 归成两条，不并一条",
              len(two_read.messages) == 2, repr([g.text for g in two_read.messages]))
        check("★ 取最新那条 = 末一条，不是顶部那条（v2-social-grab-fix 治的就是这个）",
              two_read.latest_other.text == "我比较欣赏有毅力的男孩哟", two_read.latest_other.text)

        # 按条取尾：最新那条整条不切，更旧的一条一条往外丢
        old_tail = "「" + "很长的最新一条" * 120 + "」"
        tail_read = ChatRead(
            lines=[(SENDER_OTHER, "旧消息第1行"), (SENDER_OTHER, "旧消息第2行"),
                   (SENDER_OTHER, old_tail)],
        )
        kept = _tail_context(tail_read)
        check("★ 按条取尾：最新的长那条**整条都在**（开头没被削）",
              kept == old_tail, f"{len(kept)} 字 / 开头 {kept[:8]!r}")
        check("更旧的两条被按**条**留在外面（留尾的初衷没丢）",
              "旧消息第1行" not in kept and "旧消息第2行" not in kept, repr(kept[:30]))
        check("一行都没分出说话人 → 退回按行取尾（降级，不硬编'条'）",
              _tail_context(ChatRead(lines=[(SENDER_UNKNOWN, "A\nB")]))
              == _tail_lines("A\nB"), "")

        # ---------- ④ 量不出来时老实退化 ----------

        section("④ 量不出来就退回按版面切，绝不硬编一个边界")

        noise = _noise_frame()
        check("画面花了（认不出哪块是聊天区底色）→ 回 None，不瞎给一个边界",
              detect_chat_left(noise, DPR) is None,
              "给了 " + str(detect_chat_left(noise, DPR)))
        check("这时退回按版面硬切，仍然给得出一块",
              chat_area_box(noise, DPR) is not None, str(chat_area_box(noise, DPR)))
        flat = QImage(WIN_W, WIN_H, QImage.Format_RGB32)
        flat.fill(QColor(18, 18, 18))          # 一整块深色：每列都"纯"，怎么算都行
        flat_left = detect_chat_left(flat, DPR)
        check("纯色帧就算给边界，也绝不越过图标栏（那条栏永远切掉）",
              flat_left is None or flat_left >= int(SIDEBAR_PX * DPR),
              "None" if flat_left is None else str(flat_left))
        check("空画面不炸", chat_area_box(QImage(), DPR) is None, "")
        check("形状对不上也不炸", detect_chat_left(None, DPR) is None, "")

        # ---------- ④-2 最后一刀：至少切掉标题栏与会话列表（v2-social-leak-mask） ----------
        #
        # `chat_area_box` 量不出、`chat_client_rect` 又原样交回（窗太小）时，原来只剩
        # "切掉底部输入区"一刀——标题栏（独立聊天窗上写的就是联系人 / 群名）与会话列表
        # （群名 + 消息预览）整块留在画面里，浅底深字判出来正是"对方"（真机图2 的
        # "妙妙屋 / 群名"）。这一节把 `cut_edges_box` 那条纯几何腿逐条摆开。

        section("④-2 最后一刀：切不动时也收掉标题栏与会话列表（不整块丢给 OCR）")

        big = QImage(WIN_W, WIN_H, QImage.Format_RGB32)
        big.fill(CHAT_BG)
        cut = cut_edges_box(big, DPR)
        check("整帧都是聊天区底色 → 左边界照旧走像素法，上切标题栏、下切输入区",
              cut is not None and cut[1] == int(TITLE_BAR_PX * DPR)
              and cut[1] + cut[3] == WIN_H - input_cut_px(big, DPR), str(cut))

        noise = _noise_frame()
        cut_n = cut_edges_box(noise, DPR)
        check("画面花了 → 左边界退回硬切（图标栏 + 会话列表），不瞎给一个",
              cut_n is not None and cut_n[0] == int((SIDEBAR_PX + CHAT_LIST_PX) * DPR),
              str(cut_n))
        check("画面花了，上/下两刀照样收口（标题栏与输入区不进 OCR）",
              cut_n is not None and cut_n[1] == int(TITLE_BAR_PX * DPR)
              and cut_n[1] + cut_n[3] == WIN_H - input_cut_px(noise, DPR), str(cut_n))

        narrow_noise = _noise_frame(width=int(300 * DPR), height=WIN_H)
        cut_nn = cut_edges_box(narrow_noise, DPR)
        check("窄窗且画面花了（切左一刀就是负的）→ 放弃左边那一刀、保留完整宽度，上下照切",
              cut_nn is not None and cut_nn[0] == 0 and cut_nn[2] == int(300 * DPR),
              str(cut_nn))
        check("窄窗上标题栏那一刀还在",
              cut_nn is not None and cut_nn[1] == int(TITLE_BAR_PX * DPR), str(cut_nn))

        check("空画面给 None（调用方退回'只切底部'那条老路，绝不返回更坏的东西）",
              cut_edges_box(QImage(), DPR) is None, "")
        check("小到一刀都切不动也给 None（不硬造一个坏边界）",
              cut_edges_box(QImage(int(40 * DPR), int(20 * DPR), QImage.Format_RGB32),
                            DPR) is None, "")

        # ---------- ⑤ 主语归属：别把「谁的我」说反 ----------

        section("⑤ 主语归属：对方说的「我」是对方，回话里的「我」才是用户")

        check("提示词里钉了「对方说的『我』指对方」这条",
              "对方说的" in SOCIAL_DRAFT_PROMPT and "指**对方**" in SOCIAL_DRAFT_PROMPT,
              "")
        check("提示词里点破了主客颠倒那种错法",
              "主客颠倒" in SOCIAL_DRAFT_PROMPT and "我最近也挺忙" in SOCIAL_DRAFT_PROMPT, "")
        check("回话是用户要发出去的（那个「我」指用户）",
              "指**用户**自己" in SOCIAL_DRAFT_PROMPT, "")

        service = app.social_reply
        app.config.screen.social_enabled = True
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "那你先忙，等你不忙再约", "why": "接住对方的状态"}]}
        stub.prompts.clear()
        stuck = PEER_LINES[-1]           # 对方最后那句：「我最近有点忙」
        got = service._write("对方刚回了消息，帮我拟一段回过去的话", stuck)
        prompt = stub.prompts[-1] if stub.prompts else ""
        check("对方那句原话原样落在「对方最近发来的那条」那一格",
              f'"""{stuck}"""' in prompt, prompt[:0] or f"有没有：{stuck in prompt}")
        check("人称归属那条规则也跟着提示词一起发下去",
              "主客颠倒" in prompt, "")
        check("拿回来的候选还是那一条（这几条规则没把解析弄崩）",
              isinstance(got, dict) and got.get("candidates"), str(got)[:60])

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交正向抓取 + 主语归属（v2-social-ocr-catch）")


if __name__ == "__main__":
    raise SystemExit(main())
