"""
改流程的那三个对话框（公开版占位）。

内部版：改节点信息 / 连一条线 / 从已有连线里挑一条，交给 `work/service.py` 的
`edit_*` 去写流程文件。公开仓没有流程可改，三个对话框只把签名留下——真弹出来也是
一句"需要内部授权"，不是一堆点了没反应的输入框。

（公开版没有页面会实例化它们——工作模块那两页都换成了锁着的提示，见
`ui/panels/work_panel.py`。）
"""

from __future__ import annotations

from PySide6.QtWidgets import QDialog, QLabel, QVBoxLayout, QWidget

from work.flow import Workflow

LOCK_TEXT = "🔒 这个操作需要内部授权（公开版没有流程可改）。"


class _LockedDialog(QDialog):
    """锁着的对话框：一句话，一个「关闭」。"""

    def __init__(self, title: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(320)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.addWidget(QLabel(LOCK_TEXT, self))


class NodeFormDialog(_LockedDialog):
    """加一个节点 / 改一个节点的信息（公开版：锁着）。"""

    def __init__(self, workflow: Workflow | None = None, node_id: str = "", parent: QWidget | None = None):
        super().__init__("改节点信息" if node_id else "加一个节点", parent)
        self._workflow = workflow
        self._node_id = node_id


class EdgeFormDialog(_LockedDialog):
    """加一条连线 / 改一条连线的标签（公开版：锁着）。"""

    def __init__(
        self,
        workflow: Workflow | None = None,
        source: str = "",
        target: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__("改连线" if source else "连一条线", parent)
        self._workflow = workflow
        self._source = source
        self._target = target


class EdgePickerDialog(_LockedDialog):
    """从已有连线里挑一条（公开版：锁着）。"""

    def __init__(self, workflow: Workflow | None = None, parent: QWidget | None = None):
        super().__init__("选一条连线", parent)
        self._workflow = workflow
