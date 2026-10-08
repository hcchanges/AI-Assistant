"""
流程图视图（公开版占位）。

内部版把 `work/flow.py` 读进来的节点与连线画成一张可点、可在管理员手里改的图。
公开仓没有流程数据，也就没有图：这块画布是空的，编辑相关的接口照签名留着（都不做），
免得别处按老样子调用时 AttributeError。

（公开版没有页面会实例化它——工作模块那两页都换成了锁着的提示，见
`ui/panels/work_panel.py`。）
"""

from __future__ import annotations

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QGraphicsScene, QGraphicsView, QWidget

from work.flow import Workflow


class FlowView(QGraphicsView):
    """流程图（公开版：空画布）。"""

    # 信号照旧：装配层与 work_panel 按老样子接线，公开版不会有人发它们
    node_clicked = Signal(str)
    node_edit_requested = Signal(str)
    node_remove_requested = Signal(str)
    node_add_requested = Signal()
    edge_add_requested = Signal()
    edge_edit_requested = Signal()
    edge_remove_requested = Signal()

    def __init__(self, workflow: Workflow | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self._workflow = workflow or Workflow()
        self._show_edges = False
        self._editable = False
        self._edge_lines: list = []
        self._edge_labels: list = []
        self._edge_arrows: list = []
        self._edge_items: list = []
        self.setScene(QGraphicsScene(self))

    def set_editable(self, editable: bool) -> None:
        self._editable = bool(editable)

    def editable(self) -> bool:
        return self._editable

    def set_show_edges(self, show: bool) -> None:
        self._show_edges = bool(show)

    def show_edges(self) -> bool:
        return self._show_edges

    def rebuild(self, workflow: Workflow | None = None) -> None:
        """内部版重画整张图；公开版没有流程可画。"""
        if workflow is not None:
            self._workflow = workflow

    def fit_width(self) -> None:
        return None
