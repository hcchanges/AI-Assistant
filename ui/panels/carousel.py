"""
卡片轮播与"放大定格"层（指令 17、18、19；指令 20 第 3、4 项改外观）。

生活广场（生活卡）与工作模块（部门卡）共用这一套：
- CardCarousel：横向摆卡、一屏若干张、中间那张略大；鼠标拖拽 / 滚轮 / 左右方向键切换，
  松手带惯性并吸附回弹；点中居中的那张 → 发 card_activated（附该卡在窗口里的矩形）；
  居中那张换了 → 发 card_centered（工作模块拿它把当前部门名写到窗口标题栏）。
  fill=True 是大卡模式（指令 22；指令 25-C 起工作模块也用它）：卡片高跟着轮播区走，
  一屏摆一张大卡、不留空白；card_ratio 给了就按轮播宽度的比例定卡宽
  （生活卡 0.74、部门卡 0.88），两种模式都保证两侧邻卡各露 ≥24px 的边，不会把屏幕挡死。
- 部门卡（container=True，指令 19 ⑤⑥、指令 20 ⑥）：卡里纵向排列卡位（与部门卡同宽、
  一屏摆 CHILD_PER_VIEW 个），超出部分可上下拖动查看；职能卡只是预览，点部门卡任意位置
  都只打开这张卡（card_activated），进概览后再点某一张功能。还没做出来的空卡位
  （CarouselChild.empty）只画一张「＋」的虚线卡，不写名字。卡位由工作模块按部门给
  （work/service.py 的 dept_slots）。
- 卡内不写文字（指令 20 第 3 项）：部门卡整张都是卡位区，部门名与管理员标识交给窗口的
  灰色标题栏（ui/panels/container.py 的 set_window_note）；部门卡也不再画"N 个功能卡"这类计数。
- CardOverlay：全窗口覆盖层。按 carousel 给的矩形起手，把卡片详情面板动画放大到铺满窗口
  （定格），关闭时缩回原矩形；打开/关闭会发 opened / closed，由装配层负责宠物让位与恢复。

手势怎么分（指令 18 说滚轮=左右切卡，指令 19 说卡内职能卡上下滑）：
滚轮一律左右切卡（保持指令 18 的手感），卡内职能卡用"按住上下拖"查看——按下后按
先动起来的那个方向锁定：横向=切卡，纵向=滚卡内。这样两边不会互相误触。
两边松手都一样：按甩动速度多滑一点，再吸附到整卡 / 整卡位。

外观参数集中在下面的常量，改外观改这里。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

from PySide6.QtCore import (
    QEasingCurve,
    QPointF,
    Property,
    QPropertyAnimation,
    QRect,
    QRectF,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QFontMetrics, QFontMetricsF, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QPushButton, QWidget

from ui.theme import (
    ACCENT,
    BG,
    BORDER,
    CARD,
    CARD_SUB_SPACING,
    CARD_SUB_WEIGHT,
    CARD_TITLE_SPACING,
    CARD_TITLE_WEIGHT,
    TEXT,
    TEXT_MUTED,
    card_font,
)

# ---------- 轮播外观与手感（指令 17 第 5 节：参数可配） ----------

PER_SCREEN = 3          # 一屏摆几张（中间完整 + 两侧各一张）
SPACING = 12            # 卡片之间的间距（指令 23：12–14px）
SIDE_SCALE = 0.88       # 两侧卡片的缩放；中间那张是 1.0，所以看着"略大"
SIDE_FADE = 0.55        # 越往两侧越淡的比例
SLIDE_MS = 260          # 吸附动画时长
WHEEL_STEP = 120        # 滚轮滚多少角度算翻一张（鼠标一格就是 120）
WHEEL_PIXELS = 120      # 触控板那种按像素报位移的滚轮：滚这么多像素算翻一张
DRAG_SLOP = 4           # 拖动超过这么多像素才算"在滑"，否则当成点击
FLING_SEC = 0.12        # 甩动惯性：松手速度折算成额外滑动的时间（秒）
# 卡片高 / 宽：4:3 胶片比例（指令 23 第 1 项：350 宽 → 260 高）
CARD_ASPECT = 0.743
CARD_RADIUS = 14.0      # 卡片圆角
CARD_PAD = 13.0         # 卡内边距（指令 23：12–14px）
CAROUSEL_HEIGHT = 190   # 轮播区最小高度
FILL_MARGIN = 28        # 大卡模式（fill=True）卡片四周留的空：卡片高 = 轮播区高 - 它

# ---------- 部门卡里的职能卡（指令 19 ⑤⑥） ----------

CHILD_PER_VIEW = 3          # 卡位区一屏摆几个（多出来的卡片在后面，靠上下拖才看得到）
CHILD_GAP = 26              # 卡位之间的间距（指令 25-C：18 → 26，部门卡里的职能卡之间再透气一些）
ADD_LABEL = "＋"            # 空卡位上的符号（只有它，不写名字）
CHILD_RADIUS = 12.0         # 职能卡圆角（与详情页里的卡一致）
CHILD_SUB_GAP = 0.07        # 卡名底到描述顶的空档，按卡位高度折算（指令 25-D：原来紧挨着，太挤）

CHILD_TITLE_SIZE = 12           # 卡名字号上限（卡位矮时按比例往下缩；字体与字重见 ui/theme.py）
CHILD_SUB_SIZE = 9              # 描述字号上限（比卡名小一号）
CHILD_TITLE_LIFT = 0.035        # 卡名再往上抬一点（按卡位高度折算；描述不动，指令 25-G）

# ---------- 卡右上角的 X 删除键（v2-card-del） ----------

CLOSE_ARM_MS = 3000     # 轻确认：第一下点 X 变成「确定删?」，这么久没再点就自己收回去
CLOSE_SIZE = 0.14       # X 方块的边长，按卡宽折算
CLOSE_PAD = 0.06        # X 离卡右边 / 上边的距离，按卡宽折算
CLOSE_ARM_W = 0.46      # 「确定删?」那个小药丸的宽度，按卡宽折算（比 X 宽，字才塞得下）

# ---------- 放大定格 ----------

OPEN_MS = 220           # 放大到铺满窗口的时长
CLOSE_MS = 180          # 缩回原位的时长


@dataclass
class CarouselChild:
    """部门卡里内嵌的卡位（指令 19 ⑤）：卡里的职能卡预览。

    empty=True 表示这个卡位还没做出功能卡：卡面上只画一个「＋」（不写名字）。
    职能卡只是预览，点部门卡任意位置都进概览（指令 20 ⑥），不再直接点开某一张。
    """

    key: object             # 子卡 key（如 "feature_a"）；空卡位是 None
    title: str
    subtitle: str = ""
    empty: bool = False


@dataclass
class CarouselCard:
    """轮播里一张卡要显示的东西（不含业务数据，业务数据挂在 key 上）。"""

    key: object             # 生活卡是 card.id（int），部门卡是部门卡 key（str）
    title: str
    theme: str = ""         # 主题 / 标签（部门卡是部门名）
    status: str = ""        # 状态 / 说明
    accent: str = ACCENT    # 色块标识色
    hint: str = ""          # 悬停提示
    children: list[CarouselChild] = field(default_factory=list)   # 内嵌职能卡
    container: bool = False  # True = 部门卡（卡内有职能卡区，没职能卡时显示「+」）
    closable: bool = False   # True = 卡片右上角画一个 X（生活卡的删除键，v2-card-del）


class CardCarousel(QWidget):
    """横向轮播：一屏若干张、中间略大、拖拽/滚轮/方向键切换、松手吸附回弹。"""

    # (卡片 key, 该卡此刻在窗口里的矩形) —— 矩形是"放大定格"动画的起手位置
    card_activated = Signal(object, object)
    # 居中那张换了（卡片 key）——工作模块拿它把当前部门名写到窗口标题栏（指令 20 第 4 项）
    card_centered = Signal(object)
    # 某张卡的 X 被"确认删除"了（连点两下的第二下 / 位置见 _close_rect）。只有 closable 的卡会发
    card_close_requested = Signal(object)

    def __init__(
        self,
        empty_hint: str = "还没有卡片",
        parent: QWidget | None = None,
        per_screen: int = PER_SCREEN,
        fill: bool = False,
        card_ratio: float | None = None,
    ):
        super().__init__(parent)
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumHeight(CAROUSEL_HEIGHT)
        self.setMouseTracking(False)

        self._cards: list[CarouselCard] = []
        self._pos = 0.0                 # 当前"居中位置"，浮点索引：1.5 = 在 1 与 2 之间
        self._empty_hint = empty_hint
        self._per_screen = max(1, per_screen)   # 一屏摆几张（工作模块用 2：卡片更大、更像卡片流）
        self._fill = fill               # True = 大卡模式：卡片高跟着轮播区走（生活模块用）
        # 给了比例就按比例定卡宽（指令 23：部门卡 = 轮播宽的 ~74%，落下正好是窗口宽的 ~70%），
        # 没给就按"一屏几张"平分；两种都保证两侧邻卡露得出来。
        self._card_ratio = card_ratio
        self._scroll: dict[int, float] = {}     # 每张部门卡内卡位的滚动偏移
        self._inner_index: int | None = None    # 正在滚卡内的是哪张卡（松手滑行动画认它）

        self._press_at: QPointF | None = None
        self._press_x = 0.0
        self._last_x = 0.0
        self._last_y = 0.0
        self._last_t = 0.0
        self._velocity = 0.0            # 松手瞬间的横向速度（像素/秒），用来做切卡惯性
        self._velocity_y = 0.0          # 松手瞬间的纵向速度（像素/秒），用来做卡内滑行
        self._dragged = False
        self._axis = ""                 # 手势方向："x"=切卡，"y"=滚卡内卡位
        self._wheel_acc = 0.0           # 滚轮累积角度，够一格才翻页

        # X 删除键的轻确认状态（v2-card-del）：第一下点 X 变成「确定删?」，
        # 第二下才真的发 card_close_requested；期间点别处 / 滚走 / 拖走 / 超时都作废。
        # 只记被问到的那张卡的 key，不记索引——重排（set_cards）之后索引会变，key 不会。
        self._close_armed: object | None = None
        self._close_timer = QTimer(self)
        self._close_timer.setSingleShot(True)
        self._close_timer.timeout.connect(self._disarm_close)

        self._anim = QPropertyAnimation(self, b"slide", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

        self._inner_anim = QPropertyAnimation(self, b"inner", self)
        self._inner_anim.setDuration(SLIDE_MS)
        self._inner_anim.setEasingCurve(QEasingCurve.OutCubic)

    # ---------- 数据 ----------

    def set_cards(self, cards: list[CarouselCard]) -> None:
        self._cards = list(cards)
        self._disarm_close()      # 卡都换了一批，"待确认"那个 key 未必还在
        self._pos = self._pos % len(self._cards) if self._cards else 0.0
        self._scroll.clear()
        self._inner_index = None
        self._inner_anim.stop()
        self.update()
        if self._cards:
            self.card_centered.emit(self._cards[self.current_index()].key)

    def current_index(self) -> int:
        if not self._cards:
            return 0
        return int(round(self._pos)) % len(self._cards)

    def current_card(self) -> CarouselCard | None:
        if not self._cards:
            return None
        return self._cards[self.current_index()]

    def focus_key(self, key: object) -> bool:
        """把某张卡滑到中间（指令 26-B：聊天里提到某件事，开工作模块时落到那个部门）。

        走最近的一边（首尾相接，不绕一整圈），找不到这张卡就返回 False。
        """
        for index, card in enumerate(self._cards):
            if card.key == key:
                self._slide_to(self._pos + self._wrap_distance(index))
                return True
        return False

    # ---------- 位置（Qt 属性：吸附动画要靠它插值） ----------
    # 名字不叫 pos：QWidget 自己有个 pos（窗口位置），同名会被 Qt 的属性系统抢走

    def _get_slide(self) -> float:
        return self._pos

    def _set_slide(self, value: float) -> None:
        count = len(self._cards)
        before = self.current_index() if count else None
        self._pos = float(value) % count if count else 0.0
        self.update()
        if count:
            now = self.current_index()
            if now != before:
                # 居中那张换人了：告诉外面（工作模块把部门名写到窗口标题栏）
                self.card_centered.emit(self._cards[now].key)

    slide = Property(float, _get_slide, _set_slide)

    def _get_inner(self) -> float:
        if self._inner_index is None:
            return 0.0
        return self._scroll.get(self._inner_index, 0.0)

    def _set_inner(self, value: float) -> None:
        if self._inner_index is None:
            return
        span = self._child_span(self._inner_index)
        if span <= 0:
            return
        # 卡位排成一圈：滚过一圈就从头接着来，动画给的是不折回的值，这里只取相位
        self._scroll[self._inner_index] = float(value) % span
        self.update()

    # 卡内卡位的滚动位置（松开手后的滑行动画靠它插值）
    inner = Property(float, _get_inner, _set_inner)

    # ---------- 几何 ----------

    def _card_w(self) -> float:
        if self._card_ratio is not None:
            return max(60.0, self.width() * self._card_ratio)
        return max(60.0, (self.width() - SPACING * (self._per_screen - 1)) / self._per_screen)

    def _card_h(self) -> float:
        if self._fill:
            # 大卡模式（指令 22）：卡片高跟着轮播区走，一屏一张、占满这块，不留一大片空白
            return max(60.0, self.height() - FILL_MARGIN)
        return self._card_w() * CARD_ASPECT

    def _step(self) -> float:
        """相邻两张卡片中心的距离。"""
        return self._card_w() + SPACING

    def _rect_of(self, index: int) -> QRectF:
        """第 index 张卡此刻的矩形（含缩放），坐标相对本控件。"""
        delta = self._wrap_distance(index)
        scale = 1.0 - (1.0 - SIDE_SCALE) * min(abs(delta), 1.0)
        width = self._card_w() * scale
        height = self._card_h() * scale
        center_x = self.width() / 2 + delta * self._step()
        center_y = self.height() / 2
        return QRectF(center_x - width / 2, center_y - height / 2, width, height)

    def _wrap_distance(self, index: int) -> float:
        """第 index 张卡相对"居中位置"的偏移，取最短的那一边。

        卡片首尾相接：最后一张的右边就是第一张，所以偏移要按一圈折回来算，
        这样滚过最后一张时第一张是接着滑上来的，而不是"啪"地跳回去。
        """
        count = len(self._cards)
        if count <= 1:
            return float(index) - self._pos
        half = count / 2
        return (index - self._pos + half) % count - half

    def _child_area(self, rect: QRectF) -> QRectF:
        """部门卡里放职能卡的那块：整张卡都是（指令 20 第 3 项：卡内不再写部门名）。

        内边距用固定 12–14px（指令 23 第 1 项），卡片放大后不会跟着水涨船高。
        """
        return QRectF(
            rect.left() + CARD_PAD,
            rect.top() + CARD_PAD,
            rect.width() - CARD_PAD * 2,
            rect.height() - CARD_PAD * 2,
        )

    def _child_height(self, rect: QRectF) -> float:
        """卡位高度：卡位区里一屏刚好摆下 CHILD_PER_VIEW 个。"""
        area = self._child_area(rect)
        return max(1.0, (area.height() - (CHILD_PER_VIEW - 1) * CHILD_GAP) / CHILD_PER_VIEW)

    def _child_pitch(self, index: int) -> float:
        """相邻两张卡位的间距（一张的高度 + 卡位之间的空隙）。"""
        return self._child_height(self._rect_of(index)) + CHILD_GAP

    def _child_span(self, index: int) -> float:
        """卡位排一圈的长度：排满一圈接着排第一张（首尾相接，能一直滚下去）。"""
        count = len(self._cards[index].children)
        return count * self._child_pitch(index) if count else 0.0

    def _child_rect(self, index: int, child_index: int, shift: float = 0.0) -> QRectF:
        """第若干张卡位的位置；shift 用来画"绕回来的下一圈"。"""
        rect = self._rect_of(index)
        area = self._child_area(rect)
        height = self._child_height(rect)
        offset = self._scroll.get(index, 0.0)
        top = area.top() + child_index * (height + CHILD_GAP) - offset + shift
        return QRectF(area.left(), top, area.width(), height)

    def window_rect(self, index: int) -> QRect:
        """第 index 张卡在窗口里的矩形（放大动画从这儿起手）。"""
        if not self._cards:
            return QRect()
        return self._to_window(self._rect_of(index))

    def _to_window(self, rect: QRectF) -> QRect:
        box = rect.toRect()
        return QRect(self.mapTo(self.window(), box.topLeft()), box.size())

    # ---------- 绘制 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.fillRect(self.rect(), QColor(BG))

        if not self._cards:
            painter.setPen(QColor(TEXT_MUTED))
            painter.drawText(self.rect(), Qt.AlignCenter, self._empty_hint)
            return

        # 离中间越远的越先画，保证中间那张压在最上面
        order = sorted(range(len(self._cards)), key=lambda index: -abs(self._wrap_distance(index)))
        for index in order:
            self._paint_card(painter, index)

    def _paint_card(self, painter: QPainter, index: int) -> None:
        card = self._cards[index]
        rect = self._rect_of(index)
        if rect.right() < 0 or rect.left() > self.width():
            return

        delta = abs(self._wrap_distance(index))
        focused = delta < 0.5
        painter.save()
        painter.setOpacity(1.0 if focused else max(0.4, 1.0 - (delta - 0.5) * SIDE_FADE))

        path = QPainterPath()
        path.addRoundedRect(rect, CARD_RADIUS, CARD_RADIUS)
        painter.fillPath(path, QColor(CARD))
        pen = QPen(QColor(card.accent if focused else BORDER))
        pen.setWidthF(2.0 if focused else 1.0)
        painter.setPen(pen)
        painter.drawPath(path)

        if card.container:
            self._paint_dept_card(painter, card, rect, index, focused)
        else:
            self._paint_plain_card(painter, card, rect, focused)
        # X 删除键只画在正中那张上：画在侧卡上却点不动，比不画更让人迷惑
        if focused and card.closable:
            self._paint_close(painter, card, rect)
        painter.restore()

    # ---------- 卡右上角的 X 删除键（v2-card-del） ----------

    def _close_rect(self, index: int) -> QRectF:
        """X（或它变成的「确定删?」小药丸）在控件坐标里的位置。

        没确认时是右上角一个小方块；确认待定期间放宽成药丸，把"确定删?"四个字装下——
        这两块都能点，命中测试与绘制共用这一个函数，不会画一处点另一处。
        """
        rect = self._rect_of(index)
        size = max(16.0, rect.width() * CLOSE_SIZE)
        pad = max(7.0, rect.width() * CLOSE_PAD)
        armed = self._close_armed is not None and self._close_armed == self._cards[index].key
        width = max(size, rect.width() * CLOSE_ARM_W) if armed else size
        return QRectF(rect.right() - pad - width, rect.top() + pad, width, size)

    def _paint_close(self, painter: QPainter, card: CarouselCard, rect: QRectF) -> None:
        index = self._cards.index(card)
        box = self._close_rect(index)
        armed = self._close_armed == card.key
        painter.save()
        if armed:
            painter.setPen(QPen(QColor(card.accent), 1.2))
            painter.setBrush(QColor(CARD))
            painter.drawRoundedRect(box, box.height() / 2, box.height() / 2)
            font = QFont(painter.font().family(), max(8, round(11 * rect.width() / 150.0)))
            painter.setFont(font)
            painter.setPen(QColor(card.accent))
            painter.drawText(box, Qt.AlignCenter, "确定删?")
        else:
            # 平时是安静的：一个透明底的小叉，鼠标不扫过来也不抢卡面内容的注意力
            painter.setPen(QPen(QColor(TEXT_MUTED), max(1.2, rect.width() / 110.0)))
            inset = box.width() * 0.28
            painter.drawLine(box.left() + inset, box.top() + inset, box.right() - inset, box.bottom() - inset)
            painter.drawLine(box.right() - inset, box.top() + inset, box.left() + inset, box.bottom() - inset)
        painter.restore()

    def _click_close(self, index: int, point: QPointF) -> bool:
        """点在 X 那块上没有？是就处理掉这次点击，返回 True（调用方直接收手）。

        第一下：进入"待确认"，起 3 秒表；第二下：发 card_close_requested。
        """
        card = self._cards[index]
        if not card.closable or not self._close_rect(index).contains(point):
            return False
        if self._close_armed == card.key:
            self._disarm_close()
            self.card_close_requested.emit(card.key)
        else:
            self._close_armed = card.key
            self._close_timer.start(CLOSE_ARM_MS)
            self.update()
        return True

    def _disarm_close(self) -> None:
        """把"待确认"收回去（点别处、滚走、拖走、超时都走这儿）。"""
        self._close_timer.stop()
        if self._close_armed is None:
            return
        self._close_armed = None
        self.update()

    def _paint_plain_card(
        self, painter: QPainter, card: CarouselCard, rect: QRectF, focused: bool
    ) -> None:
        """生活卡（指令 19 ⑦）：只有标题 + 主题 + 状态，简洁不嵌套。

        顶部那条主题色块已按指令 20 第 3 项裁掉；主题色还在卡片描边与底部那行字上用着。
        """
        family = painter.font().family()
        pad = max(10.0, rect.width() * 0.09)
        left = rect.left() + pad
        width = rect.width() - pad * 2
        ratio = rect.width() / 150.0

        top = rect.top() + pad
        font = QFont(family, max(11, round(15 * ratio)), QFont.Bold)
        painter.setFont(font)
        painter.setPen(QColor(TEXT if focused else TEXT_MUTED))
        lines = self._wrap(font, card.title, width, 2)
        # 行高问字体要，理由同 _paint_child（v2-work-card-font）：`pointSize() * 1.7` 是按
        # 磅当像素算出来的，比字体实高矮，drawText 会把每行底部裁掉一截。
        line = QFontMetricsF(font).lineSpacing()
        painter.drawText(QRectF(left, top, width, len(lines) * line), Qt.AlignLeft, "\n".join(lines))

        font = QFont(family, max(8, round(10 * ratio)))
        painter.setFont(font)
        painter.setPen(QColor(card.accent))
        detail = " · ".join(part for part in (card.theme, card.status) if part)
        lines = self._wrap(font, detail, width, 2)
        line = QFontMetricsF(font).lineSpacing()
        # AlignBottom：块底贴着卡底，框加高只会往上长，这一行的位置一点不动
        painter.drawText(
            QRectF(left, rect.bottom() - pad - len(lines) * line, width, len(lines) * line),
            Qt.AlignLeft | Qt.AlignBottom,
            "\n".join(lines),
        )

    def _paint_dept_card(
        self,
        painter: QPainter,
        card: CarouselCard,
        rect: QRectF,
        index: int,
        focused: bool,
    ) -> None:
        """部门卡（指令 19 ⑤⑥；指令 20 第 3 项裁掉卡内文字）：整张卡都是可上下滑的卡位区。

        部门名不在这里画了——它在窗口标题栏上（跟着居中那张卡走）。
        """
        # 卡内职能卡区：中间那张才画（两侧的小卡只画个底，省得挤成一团）
        if not focused:
            return
        area = self._child_area(rect)
        painter.save()
        painter.setClipRect(area)
        count = len(card.children)
        span = self._child_span(index)
        if count and span > 0:
            rounds = int(area.height() // span) + 2   # 一屏比一圈还高时得多画几趟
            for turn in range(rounds):
                for child_index in range(count):
                    self._paint_child(painter, card, index, child_index, turn * span)
        painter.restore()

    def _paint_child(
        self,
        painter: QPainter,
        card: CarouselCard,
        index: int,
        child_index: int,
        shift: float = 0.0,
    ) -> None:
        child = card.children[child_index]
        box = self._child_rect(index, child_index, shift)
        area = self._child_area(self._rect_of(index))
        if box.bottom() < area.top() or box.top() > area.bottom():
            return
        if child.empty:
            self._paint_empty_slot(painter, box)
            return
        path = QPainterPath()
        path.addRoundedRect(box, CHILD_RADIUS, CHILD_RADIUS)
        painter.fillPath(path, QColor(BG))
        painter.setPen(QPen(QColor(card.accent), 1.0))
        painter.drawPath(path)

        # 两行字：卡名在下、描述跟在后头（指令 25-D）。字号跟卡位高度长，但都封顶——
        # 卡位宽松时字不跟着无限放大，留白才是"精致"的那部分
        pad = max(7.0, box.height() * 0.13)
        left = box.left() + pad
        width = box.width() - pad * 2
        inner_h = box.height() - pad * 2

        title_size = max(9, min(CHILD_TITLE_SIZE, round(box.height() * 0.24)))
        sub_size = max(8, min(CHILD_SUB_SIZE, round(box.height() * 0.16)))    # 比卡名小一号
        font = card_font(title_size, CARD_TITLE_WEIGHT, CARD_TITLE_SPACING)
        sub_font = card_font(sub_size, CARD_SUB_WEIGHT, CARD_SUB_SPACING)
        # 行高**问字体要**，不再拿「字号 × 系数」顶（v2-work-card-font）。
        # 老算法把"磅"当"像素"使：9pt 的 Noto Serif SC 实高 17px（ascent 14 + descent 3），
        # 算式只给 9 × 1.45 = 13.05px，而 drawText 是**裁到矩形里**的——每行底部那 4px
        # 直接被剪掉，真机上就是"描述只看得见上半截"（那句里 Top 的 p 下伸部最先没）。
        # 这缺口还跟着字号和 DPI 一起长：144 DPI 下 9pt 实高 26px，缺口 12.95px ≈ 裁一半。
        # lineSpacing() = ascent + descent + leading，是字体自己说的"一行要占多高"，
        # 换字体、换字号、换 DPI、换缩放都跟着走，不会有第二次。
        title_line = QFontMetricsF(font).lineSpacing()
        sub_line = QFontMetricsF(sub_font).lineSpacing()
        # 卡名与描述之间留一段空档（没写描述就不留，免得整块字往上飘）
        gap = max(6.0, box.height() * CHILD_SUB_GAP) if child.subtitle else 0.0
        sub_rows = 1 if child.subtitle else 0
        # 卡名与描述各最多两行（指令 23：不再截断），塞不下才退回一行，绝不硬切
        for title_lines_max, sub_lines_max in ((2, 2), (2, 1), (1, 2), (1, 1)):
            title_lines = self._wrap(font, child.title, width, title_lines_max)
            sub_lines = self._wrap(sub_font, child.subtitle, width, sub_lines_max)
            need = len(title_lines) * title_line + sub_rows * sub_line + gap
            if need <= inner_h:
                break

        # 两行字贴在方框上部（指令 25-E）：下半部整片留白，左下角不占，
        # 留出来给以后往那儿放别的内容
        top = box.top() + pad
        # 卡名再往上抬一点、描述原地不动（指令 25-G）：抬出来的空档落在两行之间
        title_top = top - box.height() * CHILD_TITLE_LIFT
        painter.setPen(QColor(TEXT))
        painter.setFont(font)
        painter.drawText(
            QRectF(left, title_top, width, len(title_lines) * title_line),
            Qt.AlignLeft,
            "\n".join(title_lines),
        )
        if not child.subtitle:
            return

        painter.setPen(QColor(TEXT_MUTED))
        painter.setFont(sub_font)
        painter.drawText(
            QRectF(left, top + len(title_lines) * title_line + gap, width, len(sub_lines) * sub_line),
            Qt.AlignLeft,
            "\n".join(sub_lines),
        )

    def _paint_empty_slot(self, painter: QPainter, box: QRectF) -> None:
        """还没做出来的空卡位：一张虚线卡，中间只有一个「＋」（不写名字）。"""
        path = QPainterPath()
        path.addRoundedRect(box.adjusted(0, 0, -1, -1), CHILD_RADIUS, CHILD_RADIUS)
        painter.setPen(QPen(QColor(BORDER), 1.4, Qt.DashLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)

        painter.setPen(QColor(TEXT_MUTED))
        painter.setFont(card_font(max(12, round(box.height() * 0.45)), 400, 0.0))
        painter.drawText(box, Qt.AlignCenter, ADD_LABEL)

    @staticmethod
    def _wrap(font: QFont, text: str, width: float, max_lines: int) -> list[str]:
        """按宽度把中文逐字折行；真的放不下才在末尾省一个省略号。"""
        if not text:
            return [""]
        metrics = QFontMetrics(font)
        lines: list[str] = []
        current = ""
        for char in text:
            if current and metrics.horizontalAdvance(current + char) > width:
                lines.append(current)
                current = char
                if len(lines) == max_lines:
                    break
            else:
                current += char
        else:
            lines.append(current)
            return lines
        # 到了行数上限还有剩字：把最后一行改成省略号收尾
        rest = text[len("".join(lines)) :]
        if rest:
            last = lines[-1]
            while last and metrics.horizontalAdvance(last + "…") > width:
                last = last[:-1]
            lines[-1] = last + "…"
        else:
            lines.append(current)
        return lines

    # ---------- 滑动 ----------

    def _slide_to(self, index: float) -> None:
        self._disarm_close()      # 换了一张卡在中间，"待确认"的那个 X 就作废（滚轮/方向键都走这儿）
        self._anim.stop()
        self._anim.setDuration(SLIDE_MS)
        self._anim.setStartValue(self._pos)
        self._anim.setEndValue(float(index))
        self._anim.start()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton or not self._cards:
            return
        self._anim.stop()
        self._inner_anim.stop()
        self.setFocus(Qt.MouseFocusReason)
        self._press_at = event.position()
        self._press_x = event.position().x()
        self._last_x = event.position().x()
        self._last_y = event.position().y()
        self._last_t = time.monotonic()
        self._velocity = 0.0
        self._velocity_y = 0.0
        self._dragged = False
        self._axis = ""

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._press_at is None:
            return
        now = time.monotonic()
        dt = max(now - self._last_t, 1e-3)
        dx = event.position().x() - self._last_x
        dy = event.position().y() - self._last_y
        self._last_x = event.position().x()
        self._last_y = event.position().y()
        self._last_t = now

        moved_x = abs(event.position().x() - self._press_x)
        moved_y = abs(event.position().y() - self._press_at.y())
        if not self._axis:
            if max(moved_x, moved_y) <= DRAG_SLOP:
                return
            # 先动起来的方向决定这次手势干什么（指令 19 ⑤ 与指令 18 滚轮不打架）
            self._axis = "x" if moved_x >= moved_y else "y"
            self._dragged = True

        if self._axis == "x":
            self._velocity = dx / dt
            self.slide = self._pos - dx / self._step()      # 往左拖 → 后面的卡跟上来
        else:
            self._velocity_y = dy / dt
            self._scroll_inner(dy)

    def _scroll_inner(self, dy: float) -> None:
        """上下拖：滚中间那张部门卡里的卡位（排成一圈，能一直滚下去）。"""
        index = self.current_index()
        if not self._cards or not self._cards[index].container:
            return
        span = self._child_span(index)
        if span <= 0:
            return
        self._inner_index = index
        self._scroll[index] = (self._scroll.get(index, 0.0) - dy) % span
        self.update()

    def _settle_inner(self) -> None:
        """松手：按甩动速度多滑一点，再吸附到整卡位（和左右切卡一个手感）。"""
        index = self.current_index()
        if not self._cards or not self._cards[index].container:
            return
        span = self._child_span(index)
        if span <= 0:
            return
        step = self._child_pitch(index)
        start = self._scroll.get(index, 0.0)
        fling = -self._velocity_y * FLING_SEC           # 往上甩 → 接着看后面的卡位
        target = round((start + fling) / step) * step
        self._inner_index = index
        self._inner_anim.stop()
        self._inner_anim.setStartValue(start)
        self._inner_anim.setEndValue(target)
        self._inner_anim.start()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._press_at is None:
            return
        self._press_at = None
        if not self._dragged:
            self._on_click(event.position())
            return
        # 拖动（不是点击）：这一下不是回答删除键，把"待确认"收回去
        self._disarm_close()
        if self._axis == "x":
            # 松手：按甩动速度多滑一点，再吸附到整卡
            fling = (self._velocity / self._step()) * FLING_SEC
            self._slide_to(round(self._pos - fling))
        else:
            self._settle_inner()
        self._axis = ""
        self._dragged = False

    def _on_click(self, point: QPointF) -> None:
        """点中间那张卡 → 打开它（生活卡进详情、部门卡进功能卡概览，指令 20 ⑥）。

        部门卡里的职能卡只是预览：点它同样只是打开这张部门卡，不再直接进某一张功能。
        """
        index = self._hit_index(point)
        if index is None:
            self._disarm_close()
            return
        centered = abs(self._wrap_distance(index)) < 0.5
        if centered and self._click_close(index, point):
            return   # 点的是 X：这一下归删除键，不再打开卡
        self._disarm_close()
        if centered:
            card = self._cards[index]
            self.card_activated.emit(card.key, self.window_rect(index))
        else:
            # 点侧边那张：走最近的一边滑过去（首尾相接，别绕一整圈）
            self._slide_to(self._pos + self._wrap_distance(index))

    def _hit_index(self, point: QPointF) -> int | None:
        best: int | None = None
        best_distance = 1e9
        for index in range(len(self._cards)):
            rect = self._rect_of(index)
            if rect.contains(point):
                distance = abs(self._wrap_distance(index))
                if distance < best_distance:
                    best, best_distance = index, distance
        return best

    def wheelEvent(self, event) -> None:  # noqa: N802
        """滚轮：鼠标在中间那张卡的卡位区里 → 滚卡内的卡位；在别处 → 切部门卡。

        两边都是"一格一张、到头绕回另一头"（首尾相接，一直循环下去）。
        角度/像素两种位移都认：鼠标滚轮报角度，触控板常报像素。
        累积到位移够一张才动，触控板那种一次只滚一点点的小步也能生效。
        """
        if not self._cards:
            event.ignore()
            return
        self._wheel_acc += self._wheel_delta(event)
        steps = int(self._wheel_acc)          # 朝零取整：攒够一张才动
        if steps:
            self._wheel_acc -= steps
            if self._wheel_over_slots(event):
                self._wheel_inner(steps)
            else:
                self._wheel_cards(steps)
        event.accept()

    @staticmethod
    def _wheel_delta(event) -> float:
        """这次滚轮相当于滚了几张（带小数）：鼠标按角度折算，触控板按像素折算。"""
        angle = event.angleDelta().y()
        if angle:
            return angle / WHEEL_STEP
        return event.pixelDelta().y() / WHEEL_PIXELS

    def _wheel_over_slots(self, event) -> bool:
        """滚轮是不是落在中间那张卡的卡位区里（在的话滚卡内，不在就切卡）。"""
        index = self.current_index()
        if not self._cards[index].container:
            return False
        return self._child_area(self._rect_of(index)).contains(event.position())

    def _wheel_cards(self, steps: int) -> None:
        """滚轮切卡：一格一张，首尾相接（最后一张再滚，第一张紧接着滑上来）。"""
        if len(self._cards) <= 1:
            return
        self._slide_to(round(self._pos) - steps)

    def _wheel_inner(self, steps: int) -> None:
        """滚轮滚卡内卡位：一格一张，排成一圈一直滚（第 4 张后面接第 1 张，从底下冒出来）。"""
        index = self.current_index()
        span = self._child_span(index)
        if span <= 0:
            return
        pitch = self._child_pitch(index)
        start = self._scroll.get(index, 0.0)
        # 目标不折回：动画一路往一个方向走，取相位时自然接上第一张
        target = (round(start / pitch) - steps) * pitch
        self._inner_index = index
        self._inner_anim.stop()
        self._inner_anim.setStartValue(start)
        self._inner_anim.setEndValue(target)
        self._inner_anim.start()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if not self._cards:
            super().keyPressEvent(event)
            return
        if event.key() == Qt.Key_Left:
            self._slide_to(round(self._pos) - 1)
        elif event.key() == Qt.Key_Right:
            self._slide_to(round(self._pos) + 1)
        elif event.key() in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Space):
            card = self.current_card()
            if card is not None:
                self.card_activated.emit(card.key, self.window_rect(self.current_index()))
        else:
            super().keyPressEvent(event)


class CardOverlay(QWidget):
    """全窗口放大的卡片层：从卡片原位放大到铺满窗口，关闭时缩回原位。

    里面装的是现成的详情面板（生活卡详情 / 工作卡详情），它们的功能原样可用。
    """

    opened = Signal()
    closed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setObjectName("CardOverlay")
        self.setVisible(False)

        self._pages: dict[object, QWidget] = {}
        self._current: QWidget | None = None
        self._start = QRect()
        self._closing = False
        self._open = False

        self._close_button = QPushButton("×", self)
        self._close_button.setObjectName("OverlayClose")
        self._close_button.setToolTip("关闭（Esc）")
        self._close_button.setCursor(Qt.PointingHandCursor)
        self._close_button.setFixedSize(30, 30)
        self._close_button.clicked.connect(self.close_overlay)

        self._anim = QPropertyAnimation(self, b"geometry", self)
        self._anim.finished.connect(self._on_anim_finished)

    # ---------- 装配 ----------

    def add_page(self, key: object, page: QWidget) -> None:
        page.setParent(self)
        page.hide()
        self._pages[key] = page

    def is_open(self) -> bool:
        """定格层是不是开着（别用 isVisible：主面板没显示时子控件也不可见）。"""
        return self._open

    # ---------- 打开 / 关闭 ----------

    def show_page(self, key: object, start: QRect) -> None:
        page = self._pages.get(key)
        if page is None:
            return
        if self._current is not None and self._current is not page:
            self._current.hide()
        self._current = page
        self._start = QRect(start)
        self._open = True
        # 上一次的关闭动画还没走完就又被叫开了：那次关闭已经被下面的动画打断，
        # 复位标记，否则"正在关闭"会一直挂着，之后按 Esc / 点×都关不掉了
        self._closing = False

        self.setGeometry(start)
        page.setGeometry(self.rect())
        page.show()
        page.raise_()
        self._close_button.raise_()

        self.show()
        self.raise_()
        self.setFocus(Qt.OtherFocusReason)
        self._animate(self.parentWidget().rect(), OPEN_MS)
        self.opened.emit()

    def swap_page(self, key: object) -> None:
        """定格层已经开着时换一张页（不再播放大动画，关闭仍缩回原来那张卡）。

        部门功能卡列表里点一张卡就是走这里：列表已经铺满窗口了，
        再从头播一次"从小放大"只会闪一下，所以只换内容。
        """
        page = self._pages.get(key)
        if not self._open or page is None or page is self._current:
            return
        if self._current is not None:
            self._current.hide()
        self._current = page
        page.setGeometry(self.rect())
        page.show()
        page.raise_()
        self._close_button.raise_()

    def close_overlay(self, immediate: bool = False) -> None:
        """收掉定格层。immediate=True 时不播缩回动画，当场收干净并发出 closed。

        面板窗口被标题栏 ✕ 关掉时走的就是 immediate 那条：窗口都要没了，
        再等 260ms 动画没有意义，而"宠物让位"得靠 closed 这一发才能解除。
        """
        if not self._open or self._closing:
            return
        self._closing = True
        if immediate:
            self._anim.stop()
            self._on_anim_finished()
            return
        self._animate(self._start, CLOSE_MS)

    def _animate(self, target: QRect, duration: int) -> None:
        self._anim.stop()
        self._anim.setDuration(duration)
        self._anim.setStartValue(self.geometry())
        self._anim.setEndValue(target)
        self._anim.start()

    def _on_anim_finished(self) -> None:
        if not self._closing:
            return
        self._closing = False
        self._open = False
        if self._current is not None:
            self._current.hide()
        self.hide()
        self.closed.emit()

    # ---------- 尺寸 ----------

    def paintEvent(self, event) -> None:  # noqa: N802
        """铺一层面板底色，别让底下的东西透出来。"""
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(BG))

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        size = self.size()
        self._close_button.move(size.width() - self._close_button.width() - 10, 10)
        if self._current is not None:
            self._current.setGeometry(0, 0, size.width(), size.height())

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if event.key() == Qt.Key_Escape:
            self.close_overlay()
            return
        super().keyPressEvent(event)
