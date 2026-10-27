"""
工作模块面板（指令 16 第一步；指令 18 改版；指令 20 第 2–5 项精简；指令 21 接回底层编辑入口）。

打开工作模块就是部门卡轮播（探探式），页面上没有任何说明文字：
- 卡片轮播：一屏 2 张、中间的略大；滚轮 / 左右方向键切卡，点中间的卡放大定格（指令 20 ⑥）；
- 卡内不写文字：部门卡里只有卡位（功能卡 / 自建卡 / 空卡位「＋」）；
- 部门名与管理员标识写在窗口的灰色标题栏上（跟着居中那张卡走，指令 20 第 4 项）；
- 页面上只剩一个开关：管理员可见的「编辑底层连接」。「结果让宠物点评」已挪到设置页
  （指令 23 第 3 项）；大标题、操作提示、当前身份行、部门筛选都按指令 20 第 2 项去掉了。

卡片来自 work/service.py：有功能卡的部门用它的功能卡，没挂卡的部门补空位卡（界面画「＋」），
员工自建卡（指令 21）排在功能卡后面——所以轮播永远是满的（指令 18 第 5 节）。

流程数据（节点 / 连线）存在 work/flow_data.json，默认不呈现（指令 18 隐藏了流程图）；
指令 21 把编辑入口按"仅管理员可见"重新挂上：FlowView 与 work_edit 的对话框接在下面的
WorkFlowPanel 里，员工视角进不去，也就看不到流向与节点编辑。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt, Signal
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.panels.carousel import CardCarousel, CarouselCard, CarouselChild
from ui.panels.flow_view import FlowView
from ui.panels.work_edit import EdgeFormDialog, EdgePickerDialog, NodeFormDialog
from ui.theme import FLOW_MAIN, WORK_TEMPLATE
from work.flow import FlowDataError
from work.service import WorkService

# 部门卡宽 / 轮播区宽（指令 23 第 1 项；指令 25-C 起卡高吃满轮播区）：560 的窗口里轮播区是 532，
# 卡片落下 407 宽（占窗口宽 ~73%），两侧还各留 26px 让邻卡露边（要求 ≥24px）。
# 宽度到这儿就是上限了：再宽一点，两侧邻卡就被挤出屏幕（它们按 SIDE_SCALE 缩小、
# 又隔着一整个卡宽 + 间距，露得出来的一共就只有 (轮播宽 - 卡宽) / 2 这一条）。
# 所以"放大部门卡"主要靠高度：卡高 = 轮播区高 - FILL_MARGIN，跟生活页一样吃满
DEPT_CARD_RATIO = 0.765


class WorkPanel(QWidget):
    # 窗口标题栏该显示什么（部门名 + 管理员标识）：装配层把它接到 PanelWindow 上
    status_note = Signal(str)

    def __init__(
        self,
        service: WorkService,
        bus,
        on_dept: Callable[[str, QRect], None],
        on_flow: Callable[[QRect], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._service = service
        self._bus = bus
        self._on_dept = on_dept
        self._on_flow = on_flow
        self._admin_button: QPushButton | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 14)
        layout.setSpacing(8)

        # 只留一个（管理员才有的）底层编辑入口：点评开关已按指令 23 第 3 项挪进设置页，
        # 员工这一行没有内容，会自己塌成 0 高，不占地方。
        self._tools_row = QHBoxLayout()
        self._tools_row.addStretch(1)
        layout.addLayout(self._tools_row)

        # 卡片轮播：一屏就一张大卡（468 宽，高吃满轮播区），两侧各露 ≥24px 邻卡边缘
        self._carousel = CardCarousel(
            "这里还没有部门卡。", self, card_ratio=DEPT_CARD_RATIO, fill=True
        )
        self._carousel.card_activated.connect(self._open_dept_card)
        self._carousel.card_centered.connect(self._on_card_centered)
        layout.addWidget(self._carousel, 1)

        # 流程数据被管理员改过：部门卡跟着重建
        bus.work_flow_changed.connect(self._on_flow_changed)
        # 员工自建卡有增删改：部门卡里的卡位跟着重排
        bus.work_cards_changed.connect(self._refresh_cards)

        self._refresh_cards()
        self.apply_role()      # 卡片排好之后再按身份摆按钮（标题栏要说的话也得有部门名才拼得出来）

    # ---------- 卡片轮播 ----------

    def _refresh_cards(self) -> None:
        """每张部门卡是一个容器，里面装这个部门的卡位（指令 19 ⑤⑥）。

        卡内不写文字（指令 20 第 3、5 项）：不放部门名，也不放"N 个功能卡"这类计数，
        部门名由窗口标题栏显示（跟着居中那张卡，见 _on_card_centered）。
        """
        cards: list[CarouselCard] = []
        for department in self._service.departments():
            functions = self._service.cards_of(department.id)     # 真功能卡；空卡位界面画「＋」
            slots: list[CarouselChild] = []
            for slot in self._service.dept_slots(department.id):
                if slot is None:
                    slots.append(CarouselChild(key=None, title="", empty=True))
                else:
                    slots.append(
                        CarouselChild(
                            key=slot.key,
                            title=slot.label(),           # 自建卡带图标，功能卡就是名字
                            subtitle=slot.brief,
                        )
                    )
            cards.append(
                CarouselCard(
                    key=department.id,
                    title=department.name,
                    accent=FLOW_MAIN if functions else WORK_TEMPLATE,
                    container=True,
                    children=slots,
                )
            )
        self._carousel.set_cards(cards)

    def _open_dept_card(self, dept_key: object, rect: QRect) -> None:
        """点部门卡（指令 20 ⑥）：所有部门都能进，进的是这个部门的功能卡概览。"""
        self._on_dept(str(dept_key), rect)

    def focus_dept(self, dept_id: str) -> None:
        """把某张部门卡滑到中间（指令 26-B：聊天里提到某件事，开工作模块时落到那个部门）。

        认不出这个部门就什么都不做，停在原来那张卡上。
        """
        self._carousel.focus_key(dept_id)

    # ---------- 窗口标题栏上的部门名与管理员标识（指令 20 第 4 项） ----------

    def current_note(self) -> str:
        """此刻该写在窗口标题栏上的那段：居中那个部门的名字（+ 管理员标识）。"""
        card = self._carousel.current_card()
        parts = [str(card.title)] if card is not None else []
        if self._service.is_admin():
            parts.append("管理员")
        return " · ".join(part for part in parts if part)

    def _on_card_centered(self, _dept_key: object) -> None:
        self.status_note.emit(self.current_note())

    def apply_role(self) -> None:
        """按当前身份摆右上角那两个开关（身份切换是调试用，切完重新打开工作页生效）。

        管理员才建「编辑底层连接」；切回员工时把这个按钮摘掉，员工界面上就没有入口。
        """
        if self._service.is_admin():
            if self._admin_button is None:
                self._admin_button = QPushButton("编辑底层连接", self)
                self._admin_button.setToolTip("只有管理员看得到：改节点与连线（底层流程）")
                self._admin_button.setCursor(Qt.PointingHandCursor)
                self._admin_button.clicked.connect(self._open_flow)
                self._tools_row.addWidget(self._admin_button)
        elif self._admin_button is not None:
            self._admin_button.setParent(None)
            self._admin_button.deleteLater()
            self._admin_button = None
        self.status_note.emit(self.current_note())     # 标题栏上的"管理员"跟着变

    def _open_flow(self) -> None:
        """点「编辑底层连接」（只有管理员有这个按钮，指令 21）：从按钮原位放大到底层页。"""
        if self._admin_button is None:
            return
        rect = QRect(self._admin_button.mapTo(self.window(), QPoint(0, 0)), self._admin_button.size())
        self._on_flow(rect)

    def _on_flow_changed(self) -> None:
        self._refresh_cards()


class WorkFlowPanel(QWidget):
    """底层连接页（指令 21，仅管理员进得来）：画出流程图，右键改节点 / 连线。

    界面只做展示与转发：改数据的活儿在 work/service.py 的 edit_*，流程文件的读写与校验在 work/flow.py；
    这里负责弹 work_edit 里的那几个对话框，以及改完之后把图重画一遍。
    """

    def __init__(self, service: WorkService, bus, parent: QWidget | None = None):
        super().__init__(parent)
        self._service = service
        self._bus = bus

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        title = QLabel("底层连接（管理员）", self)
        title.setObjectName("TitleLabel")
        layout.addWidget(title)

        hint = QLabel(
            "蓝线 = 主线，红线 = NG 回流，橙虚线 = 并行分支。"
            "右键节点改信息 / 删节点，右键空白加节点、连线；员工看不到这一页。",
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._view = FlowView(service.workflow, self)
        self._view.set_show_edges(True)      # 管理员才进得来，进门就把连线亮出来
        self._view.set_editable(True)
        self._view.node_edit_requested.connect(self._edit_node)
        self._view.node_remove_requested.connect(self._remove_node)
        self._view.node_add_requested.connect(self._add_node)
        self._view.edge_add_requested.connect(self._add_edge)
        self._view.edge_edit_requested.connect(self._edit_edge)
        self._view.edge_remove_requested.connect(self._remove_edge)
        layout.addWidget(self._view, 1)

        bus.work_flow_changed.connect(self._on_flow_changed)

    def refresh(self) -> None:
        """每次打开都按当前流程重画（刚改过的、或从别处改过的都跟上）。"""
        self._view.rebuild(self._service.workflow)
        self._view.fit_width()

    def _on_flow_changed(self) -> None:
        self.refresh()

    # ---------- 节点 ----------

    def _add_node(self) -> None:
        dialog = NodeFormDialog(self._service.workflow, parent=self)
        accepted = dialog.exec() == QDialog.Accepted
        name, node_type, level, depts = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            node_id = self._service.edit_add_node(name, node_type, level, depts)
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"加好了节点「{name}」（{node_id}）")

    def _edit_node(self, node_id: str) -> None:
        dialog = NodeFormDialog(self._service.workflow, node_id, parent=self)
        accepted = dialog.exec() == QDialog.Accepted
        name, node_type, level, depts = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            self._service.edit_update_node(
                node_id, name=name, node_type=node_type, level=level, depts=depts
            )
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"改好了节点「{name}」")

    def _remove_node(self, node_id: str) -> None:
        node = self._service.workflow.node(node_id)
        name = node.name if node is not None else node_id
        answer = QMessageBox.question(
            self, "删除节点", f"删掉「{name}」会连带删掉接到它的连线，确定？"
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self._service.edit_remove_node(node_id)
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"删掉了节点「{name}」")

    # ---------- 连线 ----------

    def _add_edge(self) -> None:
        dialog = EdgeFormDialog(self._service.workflow, parent=self)
        accepted = dialog.exec() == QDialog.Accepted
        source, target, label, style, kind = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            self._service.edit_add_edge(source, target, label, style, kind)
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit("连好了这条线")

    def _pick_edge(self) -> tuple[str, str] | None:
        """从已有连线里挑一条（改标签 / 断线都要先挑）。"""
        picker = EdgePickerDialog(self._service.workflow, parent=self)
        accepted = picker.exec() == QDialog.Accepted
        picked = picker.edge()
        picker.deleteLater()
        return picked if accepted else None

    def _edit_edge(self) -> None:
        picked = self._pick_edge()
        if picked is None:
            return
        source, target = picked
        dialog = EdgeFormDialog(self._service.workflow, source, target, parent=self)
        accepted = dialog.exec() == QDialog.Accepted
        _source, _target, label, style, kind = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            self._service.edit_update_edge(source, target, label=label, style=style, kind=kind)
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit("这条连线改好了")

    def _remove_edge(self) -> None:
        picked = self._pick_edge()
        if picked is None:
            return
        source, target = picked
        answer = QMessageBox.question(
            self, "断一条线", f"确定断开「{source} → {target}」这条连线？"
        )
        if answer != QMessageBox.Yes:
            return
        try:
            self._service.edit_remove_edge(source, target)
        except FlowDataError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit("这条连线断开了")
