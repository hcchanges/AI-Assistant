"""
工作模块的编辑对话框（指令 16 第二步）：管理员改底层连接用。

三类小对话框：
- NodeFormDialog：加节点 / 改节点（名称、类型、层级、归属部门）；
- EdgeFormDialog：加连线 / 改连线（起点终点、类型主线/NG/并行、标签 OK/NG/空）；
- 部门归属用 QListWidget 多选，比一串勾选框省地方。

编辑只在管理员且「显示底层连接」打开时可用——入口在 FlowView 的右键菜单里，
对话框本身不设权限判断，调用方保证只在可编辑时弹。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from ui.theme import PANEL_QSS
from work.flow import NODE_DECISION, NODE_END, NODE_PROCESS, NODE_START, Workflow

NODE_TYPES = (NODE_START, NODE_DECISION, NODE_PROCESS, NODE_END)
NODE_TYPE_LABELS = {"start": "开始", "decision": "判定", "process": "工序", "end": "结束"}

# 连线类型 → (样式, kind)。画图那套和 JSON 里的字段保持一致。
EDGE_TYPES = (
    ("主线", "solid", ""),
    ("NG 回流", "red", "ng"),
    ("并行分支", "solid", "parallel"),
)
EDGE_LABELS = ("", "OK", "NG")


class NodeFormDialog(QDialog):
    """加一个节点，或改一个已有节点的信息。传 node_id 为空 = 新建。"""

    def __init__(self, workflow: Workflow, node_id: str = "", parent: QWidget | None = None):
        super().__init__(parent)
        self._workflow = workflow
        self._node = workflow.node(node_id) if node_id else None
        self.setWindowTitle("改节点信息" if self._node else "加一个节点")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self._name = QLineEdit(self)
        self._name.setText(self._node.name if self._node else "")
        self._name.setPlaceholderText("例如：质检复核")
        form.addRow("节点名称", self._name)

        self._type = QComboBox(self)
        for node_type in NODE_TYPES:
            self._type.addItem(NODE_TYPE_LABELS[node_type], node_type)
        if self._node:
            self._type.setCurrentIndex(NODE_TYPES.index(self._node.type))
        else:
            self._type.setCurrentIndex(NODE_TYPES.index(NODE_PROCESS))
        form.addRow("类型", self._type)

        self._level = QSpinBox(self)
        self._level.setRange(1, 20)
        self._level.setValue(self._node.level if self._node else 1)
        form.addRow("层级（1 最上）", self._level)

        layout.addLayout(form)

        layout.addWidget(QLabel("归属部门（可多选）", self))
        self._depts = QListWidget(self)
        self._depts.setSelectionMode(QListWidget.MultiSelection)
        current = set(self._node.depts) if self._node else set()
        for department in workflow.departments:
            item = QListWidgetItem(department.name)
            item.setData(0x0100, department.id)
            if department.id in current:
                item.setSelected(True)
            self._depts.addItem(item)
        layout.addWidget(self._depts)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self._on_ok)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_ok(self) -> None:
        if not self._name.text().strip():
            return   # 名称空：不动，让用户补
        self.accept()

    def values(self) -> tuple[str, str, int, list[str]]:
        depts = [
            item.data(0x0100) for item in self._depts.selectedItems() if item.data(0x0100)
        ]
        return (
            self._name.text().strip(),
            self._type.currentData(),
            self._level.value(),
            depts,
        )


class EdgeFormDialog(QDialog):
    """加一条连线，或改一条已有连线的标签。传 (source, target) = 改线。"""

    def __init__(
        self,
        workflow: Workflow,
        source: str = "",
        target: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._workflow = workflow
        edge = (
            next(
                (item for item in workflow.edges if item.source == source and item.target == target),
                None,
            )
            if source and target
            else None
        )
        self.setWindowTitle("改连线" if edge else "连一条线")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        form = QFormLayout()
        self._source = QComboBox(self)
        self._target = QComboBox(self)
        for node in workflow.nodes:
            self._source.addItem(node.name, node.id)
            self._target.addItem(node.name, node.id)
        if edge:
            self._source.setCurrentIndex(self._source.findData(source))
            self._target.setCurrentIndex(self._target.findData(target))
            self._source.setEnabled(False)
            self._target.setEnabled(False)
        form.addRow("从", self._source)
        form.addRow("到", self._target)

        self._kind = QComboBox(self)
        for label, _style, _kind in EDGE_TYPES:
            self._kind.addItem(label, (_style, _kind))
        if edge:
            for index, (_label, style, kind) in enumerate(EDGE_TYPES):
                if style == edge.style and kind == edge.kind:
                    self._kind.setCurrentIndex(index)
                    break
        form.addRow("连线类型", self._kind)

        self._label = QComboBox(self)
        self._label.addItems(EDGE_LABELS)
        if edge and edge.label in EDGE_LABELS:
            self._label.setCurrentIndex(EDGE_LABELS.index(edge.label))
        form.addRow("标签", self._label)

        layout.addLayout(form)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> tuple[str, str, str, str, str]:
        style, kind = self._kind.currentData()
        return (
            self._source.currentData(),
            self._target.currentData(),
            self._label.currentText(),
            style,
            kind,
        )


class EdgePickerDialog(QDialog):
    """从已有连线里挑一条（改标签 / 断线前先选哪条）。"""

    def __init__(self, workflow: Workflow, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("选一条连线")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(340)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        layout.addWidget(QLabel("下面这些是现在连着的线：", self))
        self._edges = QListWidget(self)
        for edge in workflow.edges:
            text = f"{edge.source} → {edge.target}"
            if edge.label:
                text += f"（{edge.label}）"
            if edge.is_ng:
                text += "　· NG 回流"
            elif edge.is_parallel:
                text += "　· 并行"
            item = QListWidgetItem(text)
            item.setData(0x0100, (edge.source, edge.target))
            self._edges.addItem(item)
        self._edges.setCurrentRow(0)
        layout.addWidget(self._edges)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def edge(self) -> tuple[str, str] | None:
        item = self._edges.currentItem()
        return item.data(0x0100) if item else None
