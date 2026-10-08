"""
卡片描述文字被裁（v2-work-card-font）的自动测试。

用户真机反馈：卡片上那行描述文字整行底部被水平裁切，只看得见上半截，标题正常。

S0 量出来的成因：卡面是 `QPainter.drawText(QRectF, ...)` 手绘的，行高按「字号 × 系数」算——
把**磅当像素**用了。9pt 的 Noto Serif SC 实高 17px（ascent 14 + descent 3），算式只给
9 × 1.45 = 13.05px，而 drawText 是裁到矩形里画的，底部那 4px 直接被剪掉；那句里的
`Top` 的 p 下伸部最先没。缺口跟着字号与 DPI 一起长（144 DPI 下 9pt 实高 26px，缺口 12.95px）。

这一轮验三件事：

1. **画出来的字是完整的**：把生产代码渲染成图，按行切出"墨迹段"，每段的高度必须等于
   同一句话在足够高的框里的墨迹高度——被裁会矮一截。判据只用生产渲染出来的像素，
   不采信测试自己复刻的排版（复刻的只有"去哪儿找"这一段字）。
2. **画出来的生活卡也一并验**（`_paint_plain_card`，同一套坏算法）。原先还量了工作
   模块卡位里那两张职能卡——工作模块没随公开版发布，那一段一并摘了。
3. **回不去**：源码里不许再出现「字号 × 系数」当行高，必须出现 `lineSpacing()`。

**必须先装真字体**：offscreen 平台下 `QFontDatabase.families()` 是空的，不装的话 Qt 落到
一个退化字体（descent=0、行高 = 字号），老算法在那套度量下**刚好不裁**——测试会假绿。
装不上真字体时这一节明确跳过并打出来，不假装通过。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, ensure_app, make_env, note, pump, section, skip, summary

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter

from ui.panels.carousel import (
    ACCENT,
    BG,
    CHILD_SUB_SIZE,
    CHILD_TITLE_SIZE,
    TEXT,
    TEXT_MUTED,
    CarouselCard,
    CardCarousel,
)
from ui.theme import (
    CARD_SUB_SPACING,
    CARD_SUB_WEIGHT,
    CARD_TITLE_SPACING,
    CARD_TITLE_WEIGHT,
    card_font,
)

ROOT = Path(__file__).resolve().parents[1]

# 真字体候选：先思源宋体（生产 card_font 用的那个族），退而求其次微软雅黑。
# 有了它，"Noto Serif SC" 才真的解析到带 descent 的字体，裁切才复现得出来。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/NotoSerifSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
)

# 墨迹判定：底、卡面、卡位底都是接近白的暖色（BG #fdf7ee = 253），字色一律比它深。
# 阈值贴着底色往下取 8 档——不是取"深色"，是因为生活卡底行用的是强调色 #e79a4d（231），
# 离底色只差 22，阈值定狠了这个字就"隐身"了（量出来 0 行，测试还以为没这行字）。
INK_VALUE = QColor(BG).value() - 8
PROBE_W = 760
PROBE_H = 460


def _install_real_font() -> str:
    """装一个能画中文的真字体，返回装上的族名；一个都没有返回空串。"""
    ensure_app()
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return ""


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _ink_bands(img: QImage, x0: int, x1: int, y0: int, y1: int) -> list[tuple[int, int]]:
    """把一块区域里的墨迹按行切成连续段：[(首行, 末行), ...]。

    一行字 = 一段。被裁掉一截时它**还是**一段，只是变矮——所以判据是段高、不是段数。
    这样跟排版位置无关，测试不用复刻生产的位置公式，只需知道去哪儿找这段字。
    """
    x0, x1 = max(0, int(x0)), min(img.width(), int(x1))
    y0, y1 = max(0, int(y0)), min(img.height(), int(y1))
    rows = [
        y
        for y in range(y0, y1)
        if any(img.pixelColor(x, y).value() < INK_VALUE for x in range(x0, x1))
    ]
    bands: list[tuple[int, int]] = []
    for y in rows:
        if bands and y == bands[-1][1] + 1:
            bands[-1] = (bands[-1][0], y)
        else:
            bands.append((y, y))
    return bands


def _text_ink_height(font: QFont, text: str, width: int, pen: str) -> int:
    """同一句话、同一支笔，画进一个足够高的框，量它**本来**要占多少像素（被裁的也算）。

    笔色必须跟生产那一行一模一样：字越淡，抗锯齿的边越靠近底色，阈值一卡就少一行。
    生产用淡笔、这里用深笔，量出来的"本来"就会比"画出来"大，白白报成裁切。
    """
    img = QImage(int(width) + 8, 220, QImage.Format_RGB32)
    img.fill(QColor(BG))
    painter = QPainter(img)
    painter.setPen(QColor(pen))
    painter.setFont(font)
    painter.drawText(QRectF(0, 0, width, 219), Qt.AlignLeft, text)
    painter.end()
    bands = _ink_bands(img, 0, width, 0, 220)
    return bands[-1][1] - bands[0][0] + 1 if bands else 0


def _render(widget, resize_to=None) -> QImage:
    """把控件（生产绘制）渲成图；resize 放在这儿，中间绝不转事件循环，
    免得父布局把刚设的尺寸又拉回去（那样量到的框和画出来的字就对不上了）。"""
    if resize_to is not None:
        widget.resize(*resize_to)
    img = QImage(int(widget.width()), int(widget.height()), QImage.Format_RGB32)
    img.fill(QColor(BG))
    widget.render(img)
    return img


def main() -> int:
    family = _install_real_font()
    box, _stub, app = make_env()

    section("行高来源：不许再拿「字号 × 系数」顶")
    carousel_src = _src("ui/panels/carousel.py")
    # 只查代码行：注释里正好写着老写法长什么样（就是上面这条注释），
    # 那是在解释"以前错在哪"，不该被当成现在的错。
    code = "\n".join(
        line for line in carousel_src.splitlines() if not line.strip().startswith("#")
    )
    # 老写法：title_line = title_size * 1.45 / font.pointSize() * 1.7。
    # 这两条一旦回来下面那些像素断言会红，但把话说明白比让后人猜强。
    check(
        "画字的地方不再出现 字号×1.45",
        "title_size * 1.45" not in code and "sub_size * 1.45" not in code,
        "",
    )
    check("画字的地方不再出现 pointSize()×1.7", "pointSize() * 1.7" not in code, "")
    check(
        "行高改成问字体要（lineSpacing）",
        code.count("lineSpacing()") >= 3,
        f"出现 {code.count('lineSpacing()')} 次",
    )

    if not family:
        skip(
            "真字体下的墨迹完整性",
            "这台机器上找不到 " + " / ".join(FONT_CANDIDATES)
            + " —— 装不上真字体量不出裁切，这一节空过",
        )
        note("提醒：本节跳过时上面 3 条结构锁仍然算数，但「描述完整没被裁」并没有被验证")
        drain_threads()
        box.cleanup()
        return summary("卡片描述文字裁切（v2-work-card-font）")

    note(f"已装入真字体：{family}")

    # 原先这一段量的是工作模块部门卡里那两张职能卡（销售部 / 研发部）——工作模块没随
    # 公开版发布，公开树里那一块是锁着的占位页，取不到卡位，那一段一并摘了。
    # 下面这一节量的是生活卡：同一套画字代码（`_paint_plain_card`），同一个毛病。
    section("生活卡：两行字都得是完整的（走的是同一套画字代码）")
    # 生活卡走 _paint_plain_card，行高原来也是 pointSize() * 1.7，同一个毛病。
    # 沙箱里的生活库是空的，所以这里自己造一张卡——测的是绘制，不是数据。
    plain = CardCarousel(fill=True, per_screen=2)
    plain.set_cards([CarouselCard(key=1, title="夜跑", theme="健康", status="进行中")])
    plain._anim.stop()
    pimg = _render(plain, (300, 420))
    rect = plain._rect_of(0)
    family_plain = plain.font().family()
    ratio = rect.width() / 150.0
    title_font = QFont(family_plain, max(11, round(15 * ratio)), QFont.Bold)
    detail_font = QFont(family_plain, max(8, round(10 * ratio)))
    pad = max(10.0, rect.width() * 0.09)
    text_width = int(rect.width() - pad * 2)
    bands = _ink_bands(
        pimg,
        rect.left() + rect.width() * 0.05,
        rect.right() - rect.width() * 0.05,
        rect.top() + rect.height() * 0.02,
        rect.bottom() - rect.height() * 0.02,
    )
    heights = [b[1] - b[0] + 1 for b in bands]
    want_title = _text_ink_height(title_font, "夜跑", text_width, TEXT)
    want_detail = _text_ink_height(detail_font, "健康 · 进行中", text_width, ACCENT)
    note(
        f"生活卡 卡宽 {rect.width():.0f}px 字号 {title_font.pointSize()}/{detail_font.pointSize()}pt "
        f"墨迹段 {heights}  本来 卡名 {want_title}px / 底行 {want_detail}px"
    )
    check("生活卡切出两段字（卡名 + 底行）", len(bands) >= 2, f"切出 {len(bands)} 段：{heights}")
    check(
        "生活卡卡名完整",
        bool(bands) and heights[0] == want_title,
        f"画出来 {heights[0] if heights else 0}px，本来 {want_title}px",
    )
    check(
        "生活卡底行完整（贴卡底那行原来也被裁）",
        len(bands) >= 2 and heights[-1] == want_detail,
        f"画出来 {heights[-1] if heights else 0}px，本来 {want_detail}px",
    )

    section("红线：只动画字那几下，别的一步没挪")
    check(
        "位置与间距那套常量还在用（没顺手改动版式）",
        "CHILD_TITLE_LIFT" in carousel_src and "CHILD_SUB_GAP" in carousel_src,
        "",
    )
    gui_import = next(
        (line for line in carousel_src.splitlines() if line.startswith("from PySide6.QtGui import")),
        "",
    )
    check(
        "零新依赖：QtGui 那行只多拿了同模块的 QFontMetricsF",
        gui_import == "from PySide6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, "
                      "QPainter, QPainterPath, QPen",
        gui_import,
    )

    drain_threads()
    box.cleanup()
    return summary("卡片描述文字裁切（v2-work-card-font）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
