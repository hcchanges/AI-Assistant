"""
卡面拖动编排（v2-card-tools-C）：卡上那几块东西左边有个小把手，按住能把它挪个位置。

**这一层只管"把块排成什么样、把手怎么拖"**，不管记在哪儿（那是
`life/cards/layout.py` 的 `CardLayoutStore`），也不管卡上每块自己是什么（那是两个
面板的事）。卡面把块交给 `CardBoard` 时说三件事：块号、控件、要不要吃掉剩余高度。

**为什么拖的是次序，不是坐标。** 卡面的块是**竖着一列**的，宽度永远跟着窗口走——
真按坐标摆，窗口一窄就互相盖住。所以把手拖完是"换了个位置"，块自己仍然占满整宽、
高度自己说了算（建议区那一块还照旧吃掉多余的高度）。`positions()` 量出来的坐标是
**照实量**的（`mapTo(这块板)`），不是编的：竖列里 x 就是把手那一栏的宽度、y 才是拖动
真正改的东西。坐标跟着次序一起存下来，存的是"拖的那一刻它长什么样"。

**把手为什么不跟"点块本身"合在一起。** 块身体上的按键、输入框、勾选框都是用户
要点的东西——在块身上起拖，等于跟这些控件抢点击（还会抢走文本框里的划选）。所以
拖动**只在把手那一小条上**发生，别处一个像素都不抢。这也是为什么把手是一直显着的：
它占的宽度固定（14px），不会因为"悬停才出现"而让整列内容跳来跳去。
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QPoint, Qt, Signal
from PySide6.QtGui import QMouseEvent
from PySide6.QtWidgets import QHBoxLayout, QLabel, QVBoxLayout, QWidget

from life.cards.layout import apply_order

#: 把手上悬停时说什么（不然没人知道那排小点是干什么的）
GRIP_HINT = "按住这里拖动，把这一块挪到别处"
#: 把手的字与宽度（固定宽度，显示 / 隐藏都不会让内容跳）
GRIP_TEXT = "⠿"
GRIP_WIDTH = 14

#: 「重置布局」按钮的写法与悬停说明（生活卡与工作卡用同一套字，别一边一个说法）
RESET_TEXT = "重置布局"
RESET_HINT = "把这张卡上几块东西的上下位置恢复成默认的样子"

_BOARD_QSS = (
    "QLabel#CardGrip { color: #cbbca4; font-size: 12px; }"
    "QLabel#CardGrip[dragging=\"true\"] { color: #a8763c; }"
    "QWidget#CardRow[dragging=\"true\"] {"
    "  background-color: rgba(214, 178, 120, 60);"
    "  border-radius: 8px;"
    "}"
)


class CardBoard(QWidget):
    """卡面那块板：把几个块竖着排，块左边一个把手，按住能换位置。

    `order_changed` 只在**松开鼠标之后**发一次（拖着的时候每一步都发的话，外面会
    跟着存一串中间状态）。发出来的是**换完之后**的完整次序，外面照原样存就是了。
    """

    order_changed = Signal(list)     # 新的次序（块号列表）

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(_BOARD_QSS)
        self._rows: dict[str, _Row] = {}
        self._stretch: dict[str, int] = {}
        self._order: list[str] = []
        self._draggable = True
        self._drag_key = ""

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(0, 0, 0, 0)
        self._layout.setSpacing(8)

    # ---------- 摆块 ----------

    def add_block(
        self,
        key: str,
        block: QWidget,
        stretch: int = 0,
        margins: tuple[int, int, int, int] = (0, 0, 0, 0),
    ) -> _Row:
        """把一块交给这块板。`stretch` 给 1 的块会吃掉多余的高度（比如建议区）。

        `margins` 是这一行（左、上、右、下）额外留的空，比在板子上塞一个空的占位控件、
        或者再套一层"只为留边距"的外壳干净。**交给板子的必须是那块真的会自己藏起来的
        控件**：套一层外壳的话，里面那块收起来了、外壳还立着，卡上就留下一截空档和
        一个孤零零的把手（工作卡那边踩过）。
        """
        row = _Row(self, key, block, margins)
        self._rows[key] = row
        self._stretch[key] = stretch
        self._order.append(key)
        self._layout.addWidget(row, stretch)
        return row

    def keys(self) -> list[str]:
        """现在有哪些块（次序是默认次序，没被拖动过的那种）。"""
        return list(self._rows)

    def order(self) -> list[str]:
        """现在板上的次序。"""
        return list(self._order)

    def apply_order(self, order: list[str]) -> list[str]:
        """按记下来的次序摆。认不出来的块号丢掉、少了的块补到最后（见 apply_order）。

        返回**真正摆上的**次序——调用方要存的是这个，不是它递进来的那一串。
        """
        self._order = apply_order(order, self.keys())
        self._relayout()
        return self.order()

    def positions(self) -> dict[str, dict[str, int]]:
        """每一块**现在**在这块板上的坐标（照实量，不是推算的）。

        量的是**块本人**（不含它左边那栏把手），所以 x 就是把手那一栏占掉的宽度
        （14 + 4 的间距 = 18）。竖列里 x 是恒定的——**拖动真正改的是 y 与次序**——
        但它是照实量出来的数，不是编的。（真到了"自由画布"那天，同一个字段就能装 x。）

        **没显示出来的块不记位置**：它这会儿在卡上压根没有位置，记一个 0 反而像是
        "它被摆到了左上角"（藏起来的控件本来就没有被摆过，量出来就是 0）。次序照记——
        它还是这一列里的一员，只是暂时不露面。

        量之前先 `_ensure_laid_out()`：不排一遍的话，在"这块板还没显示出来"的时候
        （卡面绑在一张还没打开的页上），量到的还是控件的默认尺寸（100×30），
        存进记录的坐标就全是假的——而这份坐标是要给用户看的"真实值"。
        """
        self._ensure_laid_out()
        out: dict[str, dict[str, int]] = {}
        for key in self._order:
            row = self._rows[key]
            if row.block is None or row.block.isHidden():
                continue
            point = row.block.mapTo(self, QPoint(0, 0))
            out[key] = {"x": point.x(), "y": point.y()}
        return out

    def _ensure_laid_out(self) -> None:
        """从最外面一层往里，把沿途每一层布局都排一遍（见 `positions` 的说明）。

        从窗口往下走是因为：上面那几层的布局没排，这块板自己就没有真实尺寸，
        板里的行排出来照样是假的。
        """
        chain: list[QWidget] = []
        widget: QWidget | None = self
        while widget is not None:
            chain.append(widget)
            widget = widget.parentWidget()
        for widget in reversed(chain):
            layout = widget.layout()
            if layout is not None:
                layout.activate()
        # 行不在上面这条链上（行是板的下级，不是上级），所以还得单独把行里面排一遍：
        # 板只把行摆到位，行里那个把手与块由行自己的布局排。不排的话，凡是要看"块本人"
        # 几何的地方（截图、预览）拿到的还是控件默认的 100×30。
        for row in self._rows.values():
            layout = row.layout()
            if layout is not None:
                layout.activate()

    def sync(self) -> None:
        """把每一行的显示与否，跟它那一块对上（块收起来时，行连把手一起收）。

        **为什么要外面叫一声**：块**重新露出来**的时候，Qt 不一定给它发 Show 事件——
        如果它的上级（这一行）当时正藏着，Qt 认为它压根没真的显示出来，事件就省了。
        于是就成了死结：行藏着 → 块显不出来 → 块不发 Show → 行永远不放回去，卡上那
        一块再也回不来（只剩一截空档和一个孤零零的把手）。
        所以分工是："块**收起来**"靠事件过滤当场跟上（这条路不会死结：行是显示着的，
        块收起来时 Hide 事件一定到）；"块**露出来**"由改可见性的那一方（两个卡面板）
        改完叫一声 `sync()`。`showEvent` 里也补一次，管"卡面整个重新打开"这种情况。
        """
        for row in self._rows.values():
            row.sync_visibility()

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Show:
            self.sync()
        return super().event(event)

    def set_draggable(self, enabled: bool) -> None:
        self._draggable = bool(enabled)

    def row(self, key: str) -> QWidget | None:
        """某个块现在待的那一行（外面看"它在不在板上"用）。"""
        return self._rows.get(key)

    def grip(self, key: str) -> QWidget | None:
        """某个块的把手（**拖动真正从这儿起**，测试与界面上都是一个入口）。"""
        row = self._rows.get(key)
        return row.grip if row is not None else None

    # ---------- 拖动 ----------

    def _begin_drag(self, key: str) -> None:
        self._drag_key = key
        self._rows[key].set_dragging(True)

    def _drag_to(self, global_pos: QPoint) -> None:
        """指针挪到哪儿了：算它压在哪个位置上，不一样就当场换（拖的时候看得见）。"""
        if not self._drag_key or self._drag_key not in self._rows:
            return
        # 判落在第几个位置靠的是各行当下的中线，所以先把布局排到最新（理由同 `positions`）
        self._ensure_laid_out()
        target = self._index_at(self.mapFromGlobal(global_pos).y())
        current = self._order.index(self._drag_key)
        if target == current:
            return
        self._order.insert(target, self._order.pop(current))
        self._relayout()

    def _end_drag(self) -> None:
        key, self._drag_key = self._drag_key, ""
        if not key or key not in self._rows:
            return
        self._rows[key].set_dragging(False)
        self.order_changed.emit(self.order())

    def _index_at(self, y: int) -> int:
        """板子上 y 这个高度，算落在第几个位置。

        判据是**每一行现在的中线**：指针过了第 n 行的中线，就算到第 n+1 个位置上。
        这个算法不用额外画一条插入线，也不挑"往上拖还是往下拖"。
        """
        rows = [self._rows[key] for key in self._order]
        for index, row in enumerate(rows):
            if y < row.y() + row.height() // 2:
                return index
        return max(0, len(rows) - 1)

    def _relayout(self) -> None:
        """把行按 `self._order` 重新塞回布局（先全拿出来，再按次序放）。

        拿出来再放回去是 QVBoxLayout 里换次序最稳的写法：Qt 没有"把这一项挪到第 n 位"
        的接口，`insertWidget` 又得自己算下标（算错了就是原地打转）。放完让 Qt 当场
        排一遍，这样拖动过程中接下来量到的 `y()` 就已经是新的了。
        """
        for row in self._rows.values():
            self._layout.removeWidget(row)
        for key in self._order:
            self._layout.addWidget(self._rows[key], self._stretch[key])
        self._layout.activate()


class _Row(QWidget):
    """一块 + 它左边那个把手。**把手是唯一能拖起来的地方。**"""

    def __init__(self, board: CardBoard, key: str, block: QWidget, margins: tuple):
        super().__init__(board)
        self.setObjectName("CardRow")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.key = key
        self.block = block

        box = QHBoxLayout(self)
        box.setContentsMargins(*margins)
        box.setSpacing(4)
        self.grip = _Grip(board, key, self)
        box.addWidget(self.grip)
        block.setParent(self)
        box.addWidget(block, 1)
        block.installEventFilter(self)
        self.sync_visibility()

    def sync_visibility(self) -> None:
        """这一行的显示与否跟着块走（块收起来，行连把手一起收）。"""
        self.setVisible(not self.block.isHidden())

    def eventFilter(self, obj: object, event: object) -> bool:
        """块自己收起来时，这一行跟着收（立刻，不用等外面叫 sync）。

        卡上有几块是**按情况才露面**的：部件区没有部件时整块收起来、建议区不是那三类卡
        时收起来。只藏块不藏行的话，卡上会留下一截空档和一个孤零零的把手——"没有部件的
        卡跟以前一模一样"这条就破了。（"重新露出来"那一半为什么不能靠事件，见 `sync`。）
        """
        if obj is self.block and event.type() in (QEvent.Show, QEvent.Hide):
            self.sync_visibility()
        return super().eventFilter(obj, event)

    def set_dragging(self, dragging: bool) -> None:
        """拖动中把这一行标出来（把手变色 + 整行淡底），松手再撤掉。"""
        for widget in (self, self.grip):
            widget.setProperty("dragging", bool(dragging))
            widget.style().unpolish(widget)
            widget.style().polish(widget)


class _Grip(QLabel):
    """把手本身：按下去就开始拖，松开就定下来。"""

    def __init__(self, board: CardBoard, key: str, parent: QWidget | None = None):
        super().__init__(GRIP_TEXT, parent)
        self.setObjectName("CardGrip")
        self.setFixedWidth(GRIP_WIDTH)
        self.setAlignment(Qt.AlignTop | Qt.AlignHCenter)
        self.setToolTip(GRIP_HINT)
        self.setCursor(Qt.OpenHandCursor)
        self._board = board
        self._key = key
        self._dragging = False

    # 下面三个是**真的拖动**走的那条路（真实鼠标与测试里造的鼠标事件都打这儿过）

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.LeftButton or not self._board._draggable:
            super().mousePressEvent(event)
            return
        self._dragging = True
        self.setCursor(Qt.ClosedHandCursor)
        # 这里**不用**自己 grabMouse：Qt 在按下时本来就会把鼠标隐式抓给收到按下的控件，
        # 所以拖到别的块上面、拖出卡面，后续的移动与松开照样回到这个把手上。
        # 自己再抓一次是多余的（窗口没显示时还会失败并打一行警告）。
        self._board._begin_drag(self._key)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            super().mouseMoveEvent(event)
            return
        self._board._drag_to(event.globalPosition().toPoint())
        event.accept()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if not self._dragging:
            super().mouseReleaseEvent(event)
            return
        self._dragging = False
        self.setCursor(Qt.OpenHandCursor)
        self._board._end_drag()
        event.accept()


__all__ = [
    "GRIP_HINT", "GRIP_TEXT", "GRIP_WIDTH", "RESET_HINT", "RESET_TEXT", "CardBoard",
]
