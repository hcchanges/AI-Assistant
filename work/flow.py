"""
工作模块的流程数据模型（公开版占位）。

内部版在这里读 `work/flow_data.json`（那张流程图导入出来的数据），对外提供部门 / 节点 /
连线 / 卡片四类查询，界面由 `ui/panels/flow_view.py` 画出来，改动由 `work_edit` 那几个
对话框驱动。

公开仓**不带那份数据，也没有读写它的实现**：下面的类型只把形状留下（字段名与内部版一致），
`load_workflow()` 一律返回一个空流程，改流程的几个函数一律抛 `FlowDataError`。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

DATA_FILE = Path(__file__).resolve().parent / "flow_data.json"

# 连线的三种画法（内部版：主线蓝实线 / NG 回流红实线 / 并行分支橙虚线）
STYLE_MAIN = "main"
STYLE_NG = "ng"
STYLE_PARALLEL = "parallel"

# 节点类型 → 画法（开始/结束用圆角胶囊，判定用菱形边框，工序用普通圆角矩形）
NODE_START = "start"
NODE_END = "end"
NODE_DECISION = "decision"
NODE_PROCESS = "process"


class FlowDataError(Exception):
    """公开版没有流程数据可读，也没有流程可改。"""


@dataclass(frozen=True)
class Department:
    id: str
    name: str


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    type: str = NODE_PROCESS
    level: int = 1
    depts: tuple[str, ...] = ()
    card_keys: tuple[str, ...] = ()
    duties: tuple[str, ...] = ()


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    label: str = ""
    style: str = STYLE_MAIN
    kind: str = ""

    @property
    def is_ng(self) -> bool:
        return self.kind == "ng"

    @property
    def is_parallel(self) -> bool:
        return self.kind == "parallel"


@dataclass(frozen=True)
class CardDef:
    key: str
    title: str
    brief: str = ""
    blurb: str = ""
    dept: str = ""
    node: str = ""
    page: str = ""


@dataclass(frozen=True)
class Workflow:
    id: str = ""
    name: str = ""
    version: str = ""
    source: str = ""
    hidden: bool = True
    note: str = ""
    departments: tuple[Department, ...] = ()
    nodes: tuple[Node, ...] = ()
    edges: tuple[Edge, ...] = ()
    cards: tuple[CardDef, ...] = ()
    rules: dict = field(default_factory=dict)

    # ---------- 查询（空流程上都有定义，外面照常问，只是答案全是"没有"） ----------

    def node(self, node_id: str) -> Node | None:
        return next((item for item in self.nodes if item.id == node_id), None)

    def department(self, dept_id: str) -> Department | None:
        return next((item for item in self.departments if item.id == dept_id), None)

    def dept_name(self, dept_id: str) -> str:
        department = self.department(dept_id)
        return department.name if department else dept_id

    def cards_of(self, dept_id: str) -> tuple[CardDef, ...]:
        return tuple(card for card in self.cards if card.dept == dept_id)

    def card(self, key: str) -> CardDef | None:
        return next((card for card in self.cards if card.key == key), None)

    def rows(self) -> list[tuple[int, tuple[Node, ...]]]:
        levels: dict[int, list[Node]] = {}
        for node in self.nodes:
            levels.setdefault(node.level, []).append(node)
        return [(level, tuple(levels[level])) for level in sorted(levels)]

    @property
    def ng_count(self) -> int:
        return sum(1 for edge in self.edges if edge.is_ng)

    @property
    def parallel_count(self) -> int:
        return sum(1 for edge in self.edges if edge.is_parallel)

    @property
    def main_count(self) -> int:
        return sum(1 for edge in self.edges if not edge.is_ng and not edge.is_parallel)


def load_workflow(path: Path | None = None) -> Workflow:
    """公开版：不读盘，一律给一个空流程（没有部门、没有节点、没有卡）。"""
    return Workflow()


# ---------- 改动流程：公开版一律不做 ----------


def _locked() -> None:
    raise FlowDataError("公开版没有流程数据，改流程需要内部授权版。")


def next_node_id(path: Path | None = None) -> str:
    _locked()


def add_node(*args, **kwargs):
    _locked()


def update_node(*args, **kwargs):
    _locked()


def remove_node(*args, **kwargs):
    _locked()


def add_edge(*args, **kwargs):
    _locked()


def update_edge(*args, **kwargs):
    _locked()


def remove_edge(*args, **kwargs):
    _locked()
