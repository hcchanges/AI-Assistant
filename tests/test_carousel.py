"""
卡片轮播与"放大定格"（指令 17）的自动测试。

两部分：
- 组件级：CardCarousel（一屏 3 张、中间略大、左右键切卡、点哪张算哪张）与
  CardOverlay（放大到铺满窗口、关闭缩回、Esc 关闭），直接构造，不依赖整个应用；
- 接线：生活广场用的就是 CardCarousel；点卡 → 定格层打开、宠物让位，关上 → 宠物恢复。

（工作模块那一侧的接线用例——部门卡轮播、选择页、滚轮循环——没随公开版发布，
公开树里那块是锁着的占位页，用例一并摘了。）
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    build_app,
    check,
    drain_threads,
    ensure_app,
    install_stub_llm,
    make_env,
    pump,
    section,
    summary,
    wait_for,
)

from PySide6.QtCore import QAbstractAnimation, QEvent, QPoint, QPointF, QRect, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QWheelEvent
from PySide6.QtWidgets import QApplication, QLabel, QPushButton, QWidget

from ui.panels.carousel import (
    CARD_PAD,
    CHILD_GAP,
    CHILD_PER_VIEW,
    CardCarousel,
    CardOverlay,
    CarouselCard,
    CarouselChild,
)
from life.cards.service import is_diet_card, is_personal_card, is_preset_card
from life.cards.templates import ALL_FEATURES, PRESET_THEMES
from ui.panels import life_panel as life_panel_module
from ui.panels.life_panel import LIFE_CARD_RATIO, LifePanel

WINDOW = QRect(0, 0, 500, 400)


def click(widget: QWidget, point) -> None:
    """在控件上模拟一次"按下 → 松开"（位移为 0，所以算点击）。"""
    press = QMouseEvent(
        QEvent.MouseButtonPress, point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
    )
    release = QMouseEvent(
        QEvent.MouseButtonRelease, point, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
    )
    widget.mousePressEvent(press)
    widget.mouseReleaseEvent(release)


def press_key(widget: QWidget, key) -> None:
    widget.keyPressEvent(QKeyEvent(QEvent.KeyPress, key, Qt.NoModifier))


def drag(widget: QWidget, start: QPointF, end: QPointF) -> None:
    """按住从 start 拖到 end（方向由位移决定）。"""
    widget.mousePressEvent(
        QMouseEvent(QEvent.MouseButtonPress, start, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    )
    widget.mouseMoveEvent(
        QMouseEvent(QEvent.MouseMove, end, Qt.NoButton, Qt.LeftButton, Qt.NoModifier)
    )
    widget.mouseReleaseEvent(
        QMouseEvent(QEvent.MouseButtonRelease, end, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier)
    )


def _wheel_event(delta: int, pos: QPointF | None = None, pixels: bool = False) -> QWheelEvent:
    point = pos if pos is not None else QPointF(60, 60)
    return QWheelEvent(
        point,
        point,
        QPoint(0, delta) if pixels else QPoint(0, 0),
        QPoint(0, 0) if pixels else QPoint(0, delta),
        Qt.NoButton,
        Qt.NoModifier,
        Qt.NoScrollPhase,
        False,
    )


def wheel(widget: QWidget, delta: int, pos: QPointF | None = None, pixels: bool = False) -> None:
    """滚一格：正数=往上滚，负数=往下滚（直接叫控件的处理函数）。

    pixels=True 时只报像素位移（触控板那种滚轮），用来验证两种滚轮都认。
    """
    widget.wheelEvent(_wheel_event(delta, pos, pixels))


def main() -> int:
    ensure_app()

    section("轮播：一屏 3 张，中间那张略大")
    host = QWidget()
    host.resize(WINDOW.width(), WINDOW.height())
    host.show()

    carousel = CardCarousel(parent=host)
    carousel.setGeometry(0, 0, WINDOW.width(), 200)
    cards = [
        CarouselCard(key=index, title=f"第 {index} 张", theme="餐饮", status="进行中 · 打卡 1 次")
        for index in range(1, 5)
    ]
    carousel.set_cards(cards)
    carousel.slide = 1.0        # 把第 2 张放到中间，左右各一张
    pump(0.02)

    widths = [carousel._rect_of(index).width() for index in range(3)]
    check("中间的卡最大", widths[1] > widths[0] and widths[1] > widths[2], str(widths))
    check("左右两张一样大", abs(widths[0] - widths[2]) < 0.5, f"{widths[0]:.1f} / {widths[2]:.1f}")
    inside = [
        index
        for index in range(3)
        if carousel._rect_of(index).left() >= 0
        and carousel._rect_of(index).right() <= WINDOW.width()
    ]
    check("一屏正好摆下 3 张", inside == [0, 1, 2], str(inside))
    check("居中那张就是 current", carousel.current_index() == 1, str(carousel.current_index()))

    section("轮播：左右键切卡，滑到整卡停住")
    carousel.slide = 0.0
    press_key(carousel, Qt.Key_Right)
    check("→ 目标位置是下一张", carousel._anim.endValue() == 1.0, str(carousel._anim.endValue()))
    moved = wait_for(lambda: carousel.current_index() == 1, timeout=1.0)
    check("动画走完真的到了下一张", moved and carousel.current_index() == 1, str(carousel.current_index()))
    press_key(carousel, Qt.Key_Left)
    moved = wait_for(lambda: carousel.current_index() == 0, timeout=1.0)
    check("← 回到上一张", moved and carousel.current_index() == 0, str(carousel.current_index()))
    check("已在第一张再往左，仍停在这张（不越界）", carousel._anim.endValue() == 0.0, str(carousel._anim.endValue()))

    section("轮播：滚轮左右轮动（指令 18 第 3 节）")
    carousel.slide = 0.0
    wheel(carousel, -120)
    check("往下滚 → 下一张", carousel._anim.endValue() == 1.0, str(carousel._anim.endValue()))
    moved = wait_for(lambda: carousel.current_index() == 1, timeout=1.0)
    check("滚完真的翻过去了", moved and carousel.current_index() == 1, str(carousel.current_index()))
    wheel(carousel, 120)
    check("往上滚 → 上一张", carousel._anim.endValue() == 0.0, str(carousel._anim.endValue()))
    wait_for(lambda: carousel.current_index() == 0, timeout=1.0)

    # 触控板那种一次只滚一点点：攒够一格才翻页
    carousel.slide = 0.0
    wheel(carousel, -30)
    check("只滚一点点 → 先不翻", carousel.current_index() == 0, str(carousel.current_index()))
    wheel(carousel, -30)
    wheel(carousel, -30)
    wheel(carousel, -30)
    check("攒够一格 → 翻到下一张", carousel._anim.endValue() == 1.0, str(carousel._anim.endValue()))

    section("轮播：点哪张算哪张")
    activated: list[tuple] = []
    carousel.card_activated.connect(lambda key, rect: activated.append((key, rect)))
    carousel.slide = 0.0
    click(carousel, carousel._rect_of(0).center())
    check("点中间那张 → 发激活信号", [key for key, _ in activated] == [1], str(activated))
    check(
        "信号带上了这张卡在窗口里的矩形",
        bool(activated) and activated[0][1].width() > 100,
        str(activated[0][1] if activated else ""),
    )
    check(
        "矩形确实在窗口坐标系里",
        bool(activated) and host.rect().contains(activated[0][1].center()),
        str(activated[0][1] if activated else ""),
    )

    activated.clear()
    carousel.slide = 0.0
    click(carousel, carousel._rect_of(1).center())
    check("点侧边那张 → 只滑过去，不直接打开", not activated, str(activated))
    check("滑的目标正是被点的那张", carousel._anim.endValue() == 1.0, str(carousel._anim.endValue()))

    section("容器卡：内嵌卡位（指令 19 ⑤⑥）")
    dept = CarouselCard(
        key="group_a",
        title="一号组",
        status="5 张子卡",
        accent="#2f6fb0",
        container=True,
        children=[
            CarouselChild(key="feature_a", title="功能甲", subtitle="一句话说明"),
            CarouselChild(key="extra", title="另一个功能", subtitle="说明"),
            CarouselChild(key="extra2", title="再一个功能", subtitle="说明"),
            CarouselChild(key="extra3", title="第四个功能", subtitle="说明"),
            CarouselChild(key="extra4", title="第五个功能", subtitle="说明"),
        ],
    )
    empty = CarouselCard(
        key="spare",
        title="备用",
        status="还没有子卡",
        container=True,
        children=[CarouselChild(key=None, title="", empty=True) for _ in range(3)],
    )
    carousel.set_cards([dept, empty])
    carousel.slide = 0.0
    pump(0.02)

    rect = carousel._rect_of(0)
    area = carousel._child_area(rect)
    check(
        "整张卡都是卡位区（卡内不再写名字与计数；卡内边距固定 12–14px）",
        abs(area.top() - rect.top() - CARD_PAD) < 1
        and abs(rect.height() - area.height() - CARD_PAD * 2) < 1
        and CARD_PAD <= 14,
        f"上边距 {area.top() - rect.top():.0f} / 区高 {area.height():.0f}",
    )
    first = carousel._child_rect(0, 0)
    check("子卡与容器卡同宽（只留内缩）", first.width() < rect.width() and first.width() > rect.width() * 0.8, f"{first.width():.0f} / {rect.width():.0f}")
    check(
        "卡位高度按“一屏刚好摆 CHILD_PER_VIEW 个”算",
        abs(first.height() - (area.height() - (CHILD_PER_VIEW - 1) * CHILD_GAP) / CHILD_PER_VIEW) < 1,
        f"{first.height():.0f} / 区高 {area.height():.0f}",
    )
    check("卡位数就是卡里给的卡位数", len(dept.children) == 5, f"{len(dept.children)} 个")

    opened: list[tuple] = []
    carousel.card_activated.connect(lambda *args: opened.append(args))
    click(carousel, first.center())
    check(
        "点子卡 → 打开这张容器卡（发 card_activated，不再是 child_activated）",
        [item[0] for item in opened] == ["group_a"],
        str(opened),
    )
    check("矩形落在容器卡那一块", bool(opened) and opened[0][1].width() > 50, str(opened[0][1] if opened else ""))

    opened.clear()
    click(carousel, QPointF(rect.center().x(), rect.top() + rect.height() * 0.1))
    check(
        "点容器卡卡身 → 也发 card_activated（卡身点得进去）",
        [item[0] for item in opened] == ["group_a"],
        str(opened),
    )

    # 竖直往下拖 → 看下面的职能卡
    before = carousel._scroll.get(0, 0.0)
    drag(carousel, QPointF(first.center()), QPointF(first.center().x(), first.center().y() - 40))
    check("卡内能上下滑动", carousel._scroll.get(0, 0.0) > before, f"{before:.0f} → {carousel._scroll.get(0, 0.0):.0f}")
    span = carousel._child_span(0)
    carousel._scroll[0] = 0.0
    drag(carousel, QPointF(first.center()), QPointF(first.center().x(), first.center().y() - span))
    phase = carousel._scroll.get(0, 0.0)
    check(
        "拖满一圈回到原位（首尾相接，一直滚得下去）",
        min(abs(phase), abs(phase - span)) < 0.5,
        f"{phase:.1f} / 一圈 {span:.1f}",
    )

    # 竖直拖不会顺手切卡
    carousel.slide = 0.0
    drag(carousel, QPointF(first.center()), QPointF(first.center().x(), first.center().y() - 60))
    check("卡内上下滑不会切到别的卡", carousel.current_index() == 0, str(carousel.current_index()))

    # 松手带滑行，并吸附到整卡位（和左右切卡一个手感）
    carousel.slide = 0.0
    carousel._inner_anim.stop()
    step = carousel._child_height(carousel._rect_of(0)) + CHILD_GAP
    carousel._scroll[0] = step * 0.4        # 停在两档之间
    carousel._velocity_y = 0.0
    carousel._settle_inner()
    check(
        "松手后滑行动画在跑",
        carousel._inner_anim.state() == QAbstractAnimation.Running,
        str(carousel._inner_anim.state()),
    )
    wait_for(lambda: carousel._inner_anim.state() != QAbstractAnimation.Running, timeout=1.0)
    check("滑完吸附回整卡位", abs(carousel._scroll.get(0, 0.0)) < 0.5, f"{carousel._scroll.get(0, 0.0):.1f} / 一档 {step:.1f}")

    carousel._scroll[0] = 0.0
    carousel._velocity_y = -step * 10       # 快速往上甩
    carousel._settle_inner()
    wait_for(lambda: carousel._inner_anim.state() != QAbstractAnimation.Running, timeout=1.0)
    check(
        "往上甩 → 滑到下一档并停住",
        abs(carousel._scroll.get(0, 0.0) - step) < 0.5,
        f"{carousel._scroll.get(0, 0.0):.1f} / 一档 {step:.1f}",
    )

    # 滚轮：打在卡位区里 = 滚卡内卡位；打在卡外 = 切容器卡
    carousel._inner_anim.stop()
    carousel._scroll[0] = 0.0
    carousel.slide = 0.0
    wheel(carousel, -120, QPointF(250, 120))
    check(
        "滚轮打在卡位区里 → 滚卡内卡位",
        abs(carousel._inner_anim.endValue() - carousel._child_pitch(0)) < 0.5,
        f"{carousel._inner_anim.endValue():.1f}",
    )

    span = carousel._child_span(0)
    carousel._scroll[0] = carousel._child_pitch(0) * (len(dept.children) - 1)   # 最后一张摆最上面
    wheel(carousel, -120, QPointF(250, 120))
    check(
        "最后一张之后再往下滚 → 第 1 张从底下接上来",
        carousel._inner_anim.endValue() % span < 0.5,
        f"目标相位 {carousel._inner_anim.endValue() % span:.1f} / 一圈 {span:.1f}",
    )

    carousel.slide = 1.0
    wheel(carousel, -120, QPointF(20, 100))
    wait_for(lambda: carousel.current_index() == 0, timeout=1.0)
    check("滚轮打在卡外 → 切容器卡", carousel.current_index() == 0, str(carousel.current_index()))

    carousel.slide = 0.0
    wheel(carousel, -120, QPointF(20, 100), pixels=True)
    check("只报像素的滚轮（触控板）也认", carousel._anim.endValue() == 1.0, str(carousel._anim.endValue()))
    carousel._anim.stop()          # 别把吸附动画留给下一节，免得位置在两者之间

    section("空卡位：虚线卡 + 「＋」（指令 19 ⑥）")
    carousel.slide = 1.0
    pump(0.02)
    check("占位卡的卡位都是空卡位", all(child.empty for child in empty.children), f"{len(empty.children)} 个")
    carousel._inner_anim.stop()
    carousel._scroll[1] = 0.0
    wheel(carousel, -120, QPointF(250, 120))
    wait_for(lambda: carousel._inner_anim.state() != QAbstractAnimation.Running, timeout=1.0)
    check("空卡位也排成一圈，能接着往下滚", carousel._scroll.get(1, 0.0) > 0, f"{carousel._scroll.get(1, 0.0):.1f}")
    opened.clear()
    carousel._scroll[1] = 0.0        # 回到没滚过的位置，第一张「＋」就在卡里看得见
    pump(0.02)
    click(carousel, carousel._child_rect(1, 0).center())
    check("点「＋」空卡位 → 打开这张容器卡（不再弹待建）", [item[0] for item in opened] == ["spare"], str(opened))

    section("轮播：没有卡片时不崩")
    activated.clear()
    carousel.set_cards([])
    pump(0.02)
    check("空态没有当前卡", carousel.current_card() is None)
    click(carousel, carousel.rect().center())
    check("空态点击无反应", not activated, str(activated))

    section("定格层：从原位放大到铺满，关闭缩回")
    overlay = CardOverlay(host)
    page = QWidget()
    overlay.add_page("card", page)
    opened: list[int] = []
    closed: list[int] = []
    overlay.opened.connect(lambda: opened.append(1))
    overlay.closed.connect(lambda: closed.append(1))

    start = QRect(120, 120, 150, 170)
    overlay.show_page("card", start)
    check("打开后可见", overlay.is_open() and overlay.isVisible())
    check("发了 opened 信号", len(opened) == 1, str(len(opened)))
    check("起手位置就在卡片那一带", overlay.geometry().width() <= start.width() + 5, str(overlay.geometry()))
    check("装上的是这张卡的详情页", overlay._current is page)
    reached = wait_for(lambda: overlay.geometry() == host.rect(), timeout=2.0)
    check("放大到铺满整个窗口", reached and overlay.geometry() == host.rect(), str(overlay.geometry()))
    check("详情页跟着铺满", page.geometry() == overlay.rect(), str(page.geometry()))

    overlay.close_overlay()
    gone = wait_for(lambda: not overlay.is_open(), timeout=2.0)
    check("关闭后藏起来", gone and not overlay.isVisible())
    check("缩回了原来的位置", overlay.geometry() == start, str(overlay.geometry()))
    check("发了 closed 信号", len(closed) == 1, str(len(closed)))

    section("定格层：Esc 关闭")
    overlay.show_page("card", start)
    wait_for(lambda: overlay.geometry() == host.rect(), timeout=2.0)
    press_key(overlay, Qt.Key_Escape)
    gone = wait_for(lambda: not overlay.is_open(), timeout=2.0)
    check("Esc 能关掉定格层", gone and not overlay.isVisible())
    check("关闭信号又发了一次", len(closed) == 2, str(len(closed)))

    host.hide()

    section("接线：两处都用同一个轮播；点卡 → 定格 + 宠物让位")
    box, stub, app = make_env()
    app.panel.show()
    app.pet_window.show()
    pump(0.05)

    card_title = app.card_service.create_manual("作息", "早睡", "🌙", "十一点前上床")
    app.life_panel.refresh_cards()
    pump(0.02)
    check("生活广场用的是轮播组件", isinstance(app.life_panel._carousel, CardCarousel))
    rows = app.card_service.plaza()
    preset_titles = [card.title for card, _count in rows if card.theme in PRESET_THEMES]
    check(
        "生活广场预置了 2 张预设卡（饮食 / 出行）",
        preset_titles == ["饮食", "出行"],
        str(preset_titles),
    )
    check(
        "预置的饮食卡仍按减脂卡对待（打开它照样给饮食建议）",
        any(is_diet_card(card) for card, _count in rows),
        str([card.title for card, _count in rows if is_diet_card(card)]),
    )
    check(
        "删掉的预设不再出现（穿搭 / 健身 / 情感陪护）",
        not {"穿得顺手", "动起来", "惦记的人"} & {card.title for card, _count in rows},
        str([card.title for card, _count in rows]),
    )
    check("手动建的卡进了轮播", app.life_panel._carousel.current_card() is not None, card_title)
    check(
        "自建卡带着分类与图标",
        any(card.title == "早睡" and card.icon == "🌙" and card.theme == "作息" for card, _c in rows),
        str([(card.title, card.icon, card.theme) for card, _c in rows]),
    )
    check(
        "生活卡是简洁的单层卡（不嵌套职能卡）",
        all(not card.container and not card.children for card in app.life_panel._carousel._cards),
        "指令 19 ⑦",
    )

    card_id = app.life_panel._carousel.current_card().key
    check("创建卡之后宠物在", app.pet_window.isVisible())
    app.open_card(card_id, QRect(60, 220, 150, 170))
    pump(0.02)
    check("点卡 → 定格层打开", app.overlay.is_open())
    check("定格层里装的是生活卡详情", app.overlay._current is app.card_panel)
    check("全屏卡期间宠物本体让位", not app.pet_window.isVisible())

    app.overlay.close_overlay()
    wait_for(lambda: not app.overlay.is_open(), timeout=2.0)
    check("关掉后宠物回来", app.pet_window.isVisible())

    # 原本就隐身的话，关上不该把它擅自叫出来
    app.pet_window.hide_pet()
    pump(0.02)
    app.open_card(card_id, QRect(60, 220, 150, 170))
    app.overlay.close_overlay()
    wait_for(lambda: not app.overlay.is_open(), timeout=2.0)
    check("本来就隐身的宠物，关上后仍保持隐身", not app.pet_window.isVisible())

    section("生活页：大卡轮播是主体（指令 22）")
    life = app.life_panel
    life_carousel = life._carousel
    keys = [card.key for card in life_carousel._cards]
    check(
        "轮播上全是生活卡：没有 entry: 开头的功能入口卡（三张已删）",
        all(isinstance(key, int) for key in keys)
        and not any(str(key).startswith("entry:") for key in keys),
        str(keys),
    )
    check(
        "入口卡相关的 key 常量已从 life_panel 移除",
        not hasattr(life_panel_module, "ENTRY_POMODORO")
        and not hasattr(life_panel_module, "ENTRY_PLAYMATE"),
        str([name for name in ("ENTRY_POMODORO", "ENTRY_PLAYMATE") if hasattr(life_panel_module, name)]),
    )
    check(
        "LifePanel 构造函数不再收 on_pomodoro / on_playmate",
        "on_pomodoro" not in inspect.signature(LifePanel.__init__).parameters
        and "on_playmate" not in inspect.signature(LifePanel.__init__).parameters,
        str(list(inspect.signature(LifePanel.__init__).parameters)),
    )
    check(
        "tool_cards_enabled 开关也从配置里摘了（没人读了）",
        not hasattr(app.config.life_cards, "tool_cards_enabled"),
        str(hasattr(app.config.life_cards, "tool_cards_enabled")),
    )
    check(
        "大卡模式：按比例定宽 + 卡片高跟着轮播区走",
        life_carousel._card_ratio == LIFE_CARD_RATIO and life_carousel._fill,
        f"ratio={life_carousel._card_ratio} fill={life_carousel._fill}",
    )
    life_neighbor = life_carousel._rect_of((life_carousel.current_index() + 1) % len(keys))
    check(
        "搬大之后两侧邻卡各露 ≥24px（指令 23 第 4 项）",
        life_carousel.width() - life_neighbor.left() >= 24,
        f"露边 {life_carousel.width() - life_neighbor.left():.0f}px",
    )
    rect = life_carousel._rect_of(life_carousel.current_index())
    ratio = rect.height() / app.panel.height()
    check(
        "卡片占窗口高 70–80%",
        0.65 <= ratio <= 0.85,
        f"{rect.height():.0f} / 窗口 {app.panel.height()} = {ratio * 100:.0f}%",
    )
    check(
        "副标题改成了「认领你的生活卡牌…」",
        life._intro.text() == f"认领你的生活卡牌，{app.config.pet.name}到点会主动喊你。",
        life._intro.text(),
    )
    check(
        "生活页没有聊天了（消息区 / 输入框 / 说给它听全删）",
        not hasattr(life, "_conversation")
        and not hasattr(life, "_input")
        and not hasattr(life, "_send_button"),
        "",
    )
    button_texts = [button.text() for button in life.findChildren(QPushButton)]
    check(
        "生活页底部三个按钮（指令 25-A：加了卡册）",
        button_texts[:3] == ["+ 手动建一张", "卡册（0）", "导入分享码"],
        str(button_texts),
    )
    check("底部保留「已收集 N 张」", life._wall_hint.text().startswith("已收集"), life._wall_hint.text())

    section("功能页本身还在（卡下架了，功能一行没删）")
    check(
        "番茄钟页与陪玩页都还挂在面板上",
        "pomodoro" in app.panel._index and "playmate" in app.panel._index,
        str(sorted(app.panel._index)),
    )
    app.panel.show_page("pomodoro")
    pump(0.05)
    check("番茄钟页仍切得过去", app.panel._current == "pomodoro", app.panel._current)
    app.panel.show_page("playmate")
    pump(0.05)
    check("陪玩页仍切得过去", app.panel._current == "playmate", app.panel._current)
    app.panel.show_page("life")
    pump(0.02)

    section("指令 25-A：预设精简 / 个人卡上限 / 卡册归档")
    box2, stub2, app2 = make_env()

    def seed(title: str, theme: str):
        return app2.card_dao.add(
            app2.user.id, title, theme, "", [], list(ALL_FEATURES), goal="", persona_id=None
        )

    # 造一份"老库"：三张被删掉的预设、两张老名字的预设、六张个人卡。
    # 预设那两张装配时已按新模板建好，这里把名字改回老库里的旧名——真在老库里它们本来
    # 就叫这两个名字，验证启动时会按新模板归位（只改名，打卡与内容都留着）。
    clothes = seed("穿得顺手", "穿搭")
    seed("动起来", "健身")
    seed("惦记的人", "情感陪护")
    old_food = app2.card_dao.by_theme("餐饮")
    app2.card_dao.set_title(old_food.id, "吃得舒服点")
    app2.card_dao.set_title(app2.card_dao.by_theme("出行").id, "走得顺一点")
    app2.card_checkin_dao.add(clothes.id)          # 老预设打过一次卡
    for index in range(6):
        seed(f"自己的卡{index}", "作息")

    app2.card_service.ensure_preset_cards()
    moved = app2.card_service.migrate_cards()
    active = [card.title for card, _count in app2.card_service.plaza()]
    book = app2.card_service.card_book()
    book_counts = {card.id: count for card, count in book}
    check(
        "删掉的预设不再出现在轮播上",
        not {"穿得顺手", "动起来", "惦记的人"} & set(active),
        str(active),
    )
    check(
        "删掉的预设进了卡册",
        {"穿得顺手", "动起来", "惦记的人"} <= {card.title for card, _c in book},
        str([card.title for card, _c in book]),
    )
    check("卡册里查得到历史打卡", book_counts.get(clothes.id) == 1, str(book_counts.get(clothes.id)))
    check(
        "老预设卡名归位成新的（只改名）",
        app2.card_dao.get(old_food.id).title == "饮食",
        app2.card_dao.get(old_food.id).title,
    )
    check(
        "活跃个人卡被压到 4 张",
        app2.card_service.active_personal_count() == 4,
        str(app2.card_service.active_personal_count()),
    )
    check("这次一共收了 5 张（3 张删掉的预设 + 2 张超限）", moved == 5, str(moved))

    check(
        "满 4 张时再建被拦住",
        app2.card_service.create_manual("作息", "再建一张", "🌙", "") == "",
        "返回空串",
    )
    share_code = app2.card_service.share(app2.card_dao.by_theme("餐饮").id)
    check("满 4 张时导入也被拦住", app2.card_service.import_share(share_code) == "", "返回空串")

    victim = next(card for card, _c in app2.card_service.plaza() if is_personal_card(card))
    back = book[0][0]
    check("位子满了直接拉回会被拒", app2.card_service.restore_card(back.id) is False, "先腾位子")
    check("收一张进卡册腾位子", app2.card_service.archive_card(victim.id), victim.title)
    check("一换一：拉回来了", app2.card_service.restore_card(back.id), back.title)
    check(
        "拉回后仍是 4 张",
        app2.card_service.active_personal_count() == 4,
        str(app2.card_service.active_personal_count()),
    )
    check("迁移可以重复跑（幂等，不会越收越多）", app2.card_service.migrate_cards() == 0, "没再收")

    # 重启：重新装配一次，轮播与卡册照旧
    install_stub_llm()
    app3 = build_app()          # 同理：直接 DesktopPetApp() 的话 cleanup() 关不掉它
    pump(0.05)
    check(
        "重启后预设还是 2 张",
        len([c for c, _n in app3.card_service.plaza() if is_preset_card(c)]) == 2,
        str([c.title for c, _n in app3.card_service.plaza()]),
    )
    check(
        "重启后活跃个人卡仍是 4 张",
        app3.card_service.active_personal_count() == 4,
        str(app3.card_service.active_personal_count()),
    )
    check(
        "重启后卡册里的卡还在",
        len(app3.card_service.card_book()) == len(book),
        f"{len(app3.card_service.card_book())} 张",
    )

    drain_threads()
    box2.cleanup()
    box.cleanup()
    return summary("卡片轮播与放大定格")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
