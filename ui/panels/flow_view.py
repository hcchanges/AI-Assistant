"""
工作流程视图（指令 16 第 1、2 节）：把 flow_data.json 画成一张可点的流程图。

画法：按 level 从上往下排（1 = 客户订单在最上面，13 = 出货在最下面），
同一层的节点横向并排居中；连线三种画法：
- 主线：蓝色实线，从上层节点底部下来；
- NG 回流：红色实线，从左侧绕回去（本来是从下层回到上层的，直接画会盖住节点）；
- 并行分支：橙色虚线，表示这两条支路同时开工。

底层连接默认隐藏（指令 16 第 2、6 节）：员工视角只看节点；不画连线时，
节点上的"OK/NG"这些判定也就不出现，看不出流转向哪儿走。开关在 WorkPanel 上。

只做展示与点击：点了节点发 node_clicked，由面板决定去哪个部门的卡片墙。
"""

from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QTransform,
)
from PySide6.QtWidgets import (
    QFrame,
    QGraphicsItem,
    QGraphicsPathItem,
    QGraphicsRectItem,
    QGraphicsScene,
    QGraphicsSimpleTextItem,
    QGraphicsView,
    QMenu,
)

from ui.theme import (
    CARD,
    FLOW_DECISION,
    FLOW_DEPT_TEXT,
    FLOW_MAIN,
    FLOW_NG,
    FLOW_NODE_BORDER,
    FLOW_NODE_TEXT,
    FLOW_PARALLEL,
    FLOW_START,
    TEXT_MUTED,
)
from work.flow import NODE_DECISION, NODE_END, NODE_START, Node, Workflow

NODE_W = 176
NODE_H = 62
GAP_X = 26              # 同层节点之间的横向间距
GAP_Y = 40              # 层与层之间的纵向间距
MARGIN = 18
LEFT_CHANNEL = 12       # NG 回流走的左侧通道起点
CHANNEL_STEP = 12       # 多条 NG 回流各走各的通道，免得叠在一起

NODE_TYPE_TAGS = {
    NODE_START: "开始",
    NODE_DECISION: "判定",
    NODE_END: "结束",
}

NAME_FONT_SIZE = 10
TAG_FONT_SIZE = 7
DEPT_FONT_SIZE = 7

MAX_DEPT_CHARS = 16     # 部门那行最多显示这么多字，超出写成"等 N 个部门"


class FlowView(QGraphicsView):
    """流程图（可点；管理员且显示底层时还可编辑）。"""

    node_clicked = Signal(str)   # 节点 id（点一下进卡片墙）
    # 编辑信号（指令 16 第二步）：由 WorkPanel 弹对话框、调 service 改数据
    node_edit_requested = Signal(str)    # 改节点信息
    node_remove_requested = Signal(str)  # 删节点
    node_add_requested = Signal()        # 加节点
    edge_add_requested = Signal()        # 连一条线
    edge_edit_requested = Signal()       # 改连线标签
    edge_remove_requested = Signal()     # 断一条线

    def __init__(self, workflow: Workflow, parent=None):
        super().__init__(parent)
        self._workflow = workflow
        self._show_edges = False
        self._editable = False
        self._edge_lines: list[QGraphicsItem] = []
        self._edge_labels: list[QGraphicsItem] = []
        self._edge_arrows: list[QGraphicsItem] = []
        self._edge_items: list[QGraphicsItem] = []

        self.setScene(QGraphicsScene(self))
        self.setBackgroundBrush(QBrush(QColor(CARD)))
        self.setFrameShape(QFrame.NoFrame)
        self.setRenderHints(QPainter.Antialiasing | QPainter.TextAntialiasing)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setMinimumHeight(220)

        self._build_scene()
        self.set_show_edges(False)

    # ---------- 对外 ----------

    def set_editable(self, editable: bool) -> None:
        """编辑模式：右键节点/空白弹编辑菜单（仅在管理员 + 显示底层时开启）。"""
        self._editable = editable

    @property
    def editable(self) -> bool:
        return self._editable

    def set_show_edges(self, show: bool) -> None:
        """显示/隐藏底层连接（含 OK/NG 标签与箭头）。"""
        self._show_edges = show
        for item in self._edge_items:
            item.setVisible(show)

    @property
    def show_edges(self) -> bool:
        return self._show_edges

    def rebuild(self, workflow: Workflow) -> None:
        """流程被编辑后重画：清空场景、按新数据重建，并保住显示/编辑状态。"""
        self._workflow = workflow
        self._edge_lines = []
        self._edge_labels = []
        self._edge_arrows = []
        self._edge_items = []
        self.scene().clear()
        self._build_scene()
        self.set_show_edges(self._show_edges)

    def fit_width(self) -> None:
        """把整张图缩放到刚好放下宽度（只缩小不放大，免得字发虚）。"""
        scene = self.scene()
        if scene is None or scene.width() <= 0:
            return
        self.resetTransform()
        viewport_width = self.viewport().width()
        if viewport_width > 0 and scene.width() > viewport_width:
            factor = viewport_width / scene.width()
            self.scale(factor, factor)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self.fit_width()

    # ---------- 画 ----------

    def _build_scene(self) -> None:
        scene = self.scene()
        rows = self._workflow.rows()
        widths = [
            len(nodes) * NODE_W + (len(nodes) - 1) * GAP_X for _level, nodes in rows
        ]
        content_width = max(widths) if widths else NODE_W
        scene_width = content_width + MARGIN * 2

        # 位置：先按层算好每个节点的矩形，画连线时要用来算起止点
        rects: dict[str, QRectF] = {}
        for row_index, (_level, nodes) in enumerate(rows):
            row_width = len(nodes) * NODE_W + (len(nodes) - 1) * GAP_X
            start_x = MARGIN + (content_width - row_width) / 2
            top = MARGIN + row_index * (NODE_H + GAP_Y)
            for column, node in enumerate(nodes):
                left = start_x + column * (NODE_W + GAP_X)
                rects[node.id] = QRectF(left, top, NODE_W, NODE_H)

        # 连线先画、节点后画：后加的在上面，节点就不会被线盖住
        self._draw_edges(rects)
        for node in self._workflow.nodes:
            rect = rects.get(node.id)
            if rect is not None:
                scene.addItem(_NodeItem(node, rect, self, self._dept_text(node)))

        scene.setSceneRect(QRectF(0, 0, scene_width, MARGIN * 2 + len(rows) * NODE_H + max(0, len(rows) - 1) * GAP_Y))
        self._draw_edges_visibility()

    def _draw_edges_visibility(self) -> None:
        for item in self._edge_items:
            item.setVisible(self._show_edges)

    # ---------- 编辑菜单（指令 16 第二步） ----------

    def _node_at(self, pos) -> "_NodeItem | None":
        item = self.itemAt(pos)
        while item is not None:
            if isinstance(item, _NodeItem):
                return item
            item = item.parentItem()
        return None

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键菜单：只有可编辑时才弹；点在节点上给节点项，点在空白给连线项。"""
        if not self._editable:
            return
        menu = self._build_edit_menu(self._node_at(event.pos()))
        menu.exec(event.globalPos())
        menu.deleteLater()

    def _build_edit_menu(self, node_item: "_NodeItem | None") -> QMenu:
        """拼编辑菜单（抽出来方便测试菜单项，不真的弹）。"""
        menu = QMenu(self)
        if node_item is not None:
            node_id = node_item.node.id
            edit = menu.addAction("改这个节点的信息")
            remove = menu.addAction("删除这个节点")
            edit.triggered.connect(lambda _c=False, nid=node_id: self.node_edit_requested.emit(nid))
            remove.triggered.connect(lambda _c=False, nid=node_id: self.node_remove_requested.emit(nid))
        else:
            menu.addAction("加一个节点").triggered.connect(self.node_add_requested.emit)
            menu.addSeparator()
            menu.addAction("连一条线").triggered.connect(self.edge_add_requested.emit)
            menu.addAction("改连线标签").triggered.connect(self.edge_edit_requested.emit)
            menu.addAction("断一条线").triggered.connect(self.edge_remove_requested.emit)
        return menu

    def _dept_text(self, node: Node) -> str:
        names = [self._workflow.dept_name(dept_id) for dept_id in node.depts]
        text = "/".join(names)
        if len(text) > MAX_DEPT_CHARS and len(names) > 1:
            text = f"{'/'.join(names[:2])} 等 {len(names)} 个部门"
        return text

    def _draw_edges(self, rects: dict[str, QRectF]) -> None:
        scene = self.scene()
        ng_index = 0
        for edge in self._workflow.edges:
            source = rects.get(edge.source)
            target = rects.get(edge.target)
            if source is None or target is None:
                continue
            if edge.is_ng:
                # 回流线：从左边出去，绕到上层节点的左侧再进去，不穿过中间的节点
                channel_x = LEFT_CHANNEL + ng_index * CHANNEL_STEP
                ng_index += 1
                path = _path(
                    [
                        QPointF(source.left(), source.center().y()),
                        QPointF(channel_x, source.center().y()),
                        QPointF(channel_x, target.center().y()),
                        QPointF(target.left(), target.center().y()),
                    ]
                )
                color = FLOW_NG
                dashed = False
            else:
                mid_y = (source.bottom() + target.top()) / 2
                path = _path(
                    [
                        QPointF(source.center().x(), source.bottom()),
                        QPointF(source.center().x(), mid_y),
                        QPointF(target.center().x(), mid_y),
                        QPointF(target.center().x(), target.top()),
                    ]
                )
                color = FLOW_PARALLEL if edge.is_parallel else FLOW_MAIN
                dashed = edge.is_parallel

            pen = QPen(QColor(color), 1.6)
            if dashed:
                pen.setStyle(Qt.DashLine)
            item = QGraphicsPathItem(path)
            item.setPen(pen)
            scene.addItem(item)
            self._edge_lines.append(item)

            if edge.label:
                label = QGraphicsSimpleTextItem(edge.label)
                label.setBrush(QBrush(QColor(color)))
                font = QFont()
                font.setPointSize(TAG_FONT_SIZE)
                label.setFont(font)
                label.setPos(
                    QPointF(target.center().x() + 4, target.top() - 12)
                    if not edge.is_ng
                    else QPointF(target.left() + 4, target.center().y() - 10)
                )
                scene.addItem(label)
                self._edge_labels.append(label)

            arrow = _arrow_head(path, color)
            scene.addItem(arrow)
            self._edge_arrows.append(arrow)

        self._edge_items = [*self._edge_lines, *self._edge_labels, *self._edge_arrows]
        self._draw_edges_visibility()


def _path(points: list[QPointF]) -> QPainterPath:
    path = QPainterPath(points[0])
    for point in points[1:]:
        path.lineTo(point)
    return path


def _arrow_head(path: QPainterPath, color: str) -> QGraphicsPathItem:
    """在折线终点画一个箭头（顺着最后一段的方向）。"""
    end = path.pointAtPercent(1.0)
    before = path.pointAtPercent(max(0.0, 1.0 - 0.06 / max(path.length(), 0.001)))
    angle = 0.0
    dx, dy = end.x() - before.x(), end.y() - before.y()
    if abs(dx) > 1e-6 or abs(dy) > 1e-6:
        angle = math.degrees(math.atan2(dy, dx))

    size = 7.0
    base = QPolygonF(
        [
            QPointF(0, 0),
            QPointF(-size, -size * 0.45),
            QPointF(-size, size * 0.45),
        ]
    )
    polygon = QTransform().translate(end.x(), end.y()).rotate(angle).map(base)
    item = QGraphicsPathItem()
    arrow_path = QPainterPath(polygon.at(0))
    for index in range(1, polygon.count()):
        arrow_path.lineTo(polygon.at(index))
    arrow_path.closeSubpath()
    item.setPath(arrow_path)
    item.setBrush(QBrush(QColor(color)))
    item.setPen(QPen(QColor(color), 0.5))
    return item


class _NodeItem(QGraphicsRectItem):
    """一个节点：名称 + 部门 + 类型标签；点一下进该部门的卡片墙。"""

    def __init__(
        self,
        node: Node,
        rect: QRectF,
        view: FlowView,
        dept_text: str,
    ):
        super().__init__(rect)
        self.node = node
        self._view = view

        border = FLOW_NODE_BORDER
        if node.type == NODE_START or node.type == NODE_END:
            border = FLOW_START
        elif node.type == NODE_DECISION:
            border = FLOW_DECISION
        self.setBrush(QBrush(QColor(CARD)))
        self.setPen(QPen(QColor(border), 1.4))
        self.setAcceptHoverEvents(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip(self._tooltip(dept_text))

        tag = QGraphicsSimpleTextItem(NODE_TYPE_TAGS.get(node.type, "工序"), self)
        tag.setBrush(QBrush(QColor(TEXT_MUTED)))
        tag.setFont(_font(TAG_FONT_SIZE))
        tag.setPos(rect.left() + 7, rect.top() + 5)

        name = QGraphicsSimpleTextItem(node.name, self)
        name.setBrush(QBrush(QColor(FLOW_NODE_TEXT)))
        name.setFont(_font(NAME_FONT_SIZE, bold=True))
        name_rect = name.boundingRect()
        name.setPos(
            rect.center().x() - name_rect.width() / 2,
            rect.top() + 18,
        )

        line = QGraphicsSimpleTextItem(dept_text, self)
        line.setBrush(QBrush(QColor(FLOW_DEPT_TEXT)))
        line.setFont(_font(DEPT_FONT_SIZE))
        line_rect = line.boundingRect()
        line.setPos(rect.center().x() - line_rect.width() / 2, rect.top() + 42)

    def _tooltip(self, dept_text: str) -> str:
        parts = [f"{self.node.name}（{dept_text}）"]
        if self.node.duties:
            parts.extend(self.node.duties)
        parts.append("点一下看这个部门的卡片")
        return "\n".join(parts)

    def hoverEnterEvent(self, event) -> None:  # noqa: N802
        pen = self.pen()
        pen.setWidthF(2.4)
        self.setPen(pen)
        super().hoverEnterEvent(event)

    def hoverLeaveEvent(self, event) -> None:  # noqa: N802
        pen = self.pen()
        pen.setWidthF(1.4)
        self.setPen(pen)
        super().hoverLeaveEvent(event)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._view.node_clicked.emit(self.node.id)
        super().mousePressEvent(event)


def _font(size: int, bold: bool = False) -> QFont:
    font = QFont()
    font.setPointSize(size)
    font.setBold(bold)
    return font
