"""
工作模块的流程数据（指令 16 第一步）。

数据来自用户给的流程图导入 JSON（图二：制造业订单履行流程），原样放在 work/flow_data.json：
Workflow / Department / Node / Edge / Annotation / rules，这里只负责"读进来 + 提供查询"，
渲染在 ui/panels/flow_view.py，业务在 work/service.py。

为什么流程要单独一份数据文件：它是"底层连接架构"，默认对员工隐藏（指令 16 第 2、6 节），
本质是数据不是代码——以后改流程（加节点、改流向）只动 JSON，不动程序。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from core.paths import bundle_dir

DATA_FILE = bundle_dir() / "work" / "flow_data.json"

# 连线的三种画法：主线（蓝实线）、NG 回流（红实线）、并行分支（橙虚线）
STYLE_MAIN = "main"
STYLE_NG = "ng"
STYLE_PARALLEL = "parallel"

# 节点类型 → 画法（开始/结束用圆角胶囊，判定用菱形边框，工序用普通圆角矩形）
NODE_START = "start"
NODE_END = "end"
NODE_DECISION = "decision"
NODE_PROCESS = "process"


class FlowDataError(Exception):
    """流程数据缺失或格式不对。"""


@dataclass(frozen=True)
class Department:
    id: str
    name: str


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    type: str                    # start / decision / process / end
    level: int                   # 层级：1 在最上面（客户订单），13 在最下面（出货）
    depts: tuple[str, ...]       # 参与这个节点的部门
    card_keys: tuple[str, ...]   # 挂在这个节点上的卡片
    duties: tuple[str, ...]      # 节点职责说明（来自 annotations，可能为空）


@dataclass(frozen=True)
class Edge:
    source: str
    target: str
    label: str                   # "OK" / "NG" / 空
    style: str
    kind: str                    # "" / ng / parallel

    @property
    def is_ng(self) -> bool:
        return self.kind == STYLE_NG

    @property
    def is_parallel(self) -> bool:
        return self.kind == STYLE_PARALLEL


@dataclass(frozen=True)
class CardDef:
    """JSON 里声明的卡片（demo_attachment.cards）：谁挂在哪个部门、哪个节点上。"""

    key: str
    title: str
    dept: str
    node: str


@dataclass(frozen=True)
class Workflow:
    id: str
    name: str
    version: str
    source: str
    hidden: bool
    note: str
    departments: tuple[Department, ...]
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]
    cards: tuple[CardDef, ...]
    rules: dict

    # ---------- 查询 ----------

    def node(self, node_id: str) -> Node | None:
        return next((item for item in self.nodes if item.id == node_id), None)

    def department(self, dept_id: str) -> Department | None:
        return next((item for item in self.departments if item.id == dept_id), None)

    def dept_name(self, dept_id: str) -> str:
        department = self.department(dept_id)
        return department.name if department else dept_id

    def cards_of(self, dept_id: str) -> tuple[CardDef, ...]:
        """这个部门挂着的卡片（点节点/部门进来看到的卡片墙就是它）。"""
        return tuple(card for card in self.cards if card.dept == dept_id)

    def card(self, key: str) -> CardDef | None:
        return next((card for card in self.cards if card.key == key), None)

    def rows(self) -> list[tuple[int, tuple[Node, ...]]]:
        """按层级分组（1 在最上），画流程图时上层级排在上面。"""
        levels: dict[int, list[Node]] = {}
        for node in self.nodes:
            levels.setdefault(node.level, []).append(node)
        return [(level, tuple(levels[level])) for level in sorted(levels)]

    # ---------- 统计（自测与界面上"图例"用） ----------

    @property
    def ng_count(self) -> int:
        return sum(1 for edge in self.edges if edge.is_ng)

    @property
    def parallel_count(self) -> int:
        return sum(1 for edge in self.edges if edge.is_parallel)

    @property
    def main_count(self) -> int:
        """蓝色主线：既不回流（NG）也不是并行分支的那些。"""
        return sum(1 for edge in self.edges if not edge.is_ng and not edge.is_parallel)


def _to_node(data: dict, duties: dict[str, list[str]]) -> Node:
    node_id = str(data.get("id") or "").strip()
    return Node(
        id=node_id,
        name=str(data.get("name") or "").strip(),
        type=str(data.get("type") or NODE_PROCESS).strip(),
        level=int(data.get("level") or 1),
        depts=tuple(str(item) for item in (data.get("dept") or [])),
        card_keys=tuple(str(item) for item in (data.get("cards") or [])),
        duties=tuple(duties.get(node_id, [])),
    )


def _empty_workflow() -> Workflow:
    """公开版不带内部流程数据时的空流程。

    工作模块仍然能打开：部门卡为空，管理员可以从零画流程；
    保存后自动生成 work/flow_data.json。
    """
    return Workflow(
        id="",
        name="",
        version="",
        source="",
        hidden=True,
        note="",
        departments=(),
        nodes=(),
        edges=(),
        cards=(),
        rules={},
    )


def load_workflow(path: Path | None = None) -> Workflow:
    """读流程数据；文件缺失或结构不对就把原因说清楚。

    公开版没有随仓带内部流程数据（flow_data.json 属于技术仓内部资料）：
    文件不存在时给一张空流程，让界面能正常打开。
    """
    data_file = path or DATA_FILE
    if not data_file.exists():
        return _empty_workflow()
    try:
        raw = json.loads(data_file.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FlowDataError(f"读不到流程数据 {data_file}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise FlowDataError(f"流程数据不是合法 JSON：{exc}") from exc

    meta = raw.get("workflow") or {}
    duties: dict[str, list[str]] = {}
    for item in raw.get("annotations") or []:
        node_id = str(item.get("node") or "").strip()
        title = str(item.get("title") or "").strip()
        text = str(item.get("text") or "").strip()
        if node_id and text:
            duties.setdefault(node_id, []).append(f"{title}：{text}" if title else text)

    nodes = tuple(_to_node(item, duties) for item in (raw.get("nodes") or []))
    edges = tuple(
        Edge(
            source=str(item.get("from") or "").strip(),
            target=str(item.get("to") or "").strip(),
            label=str(item.get("label") or "").strip(),
            style=str(item.get("style") or "solid").strip(),
            kind=str(item.get("kind") or "").strip(),
        )
        for item in (raw.get("edges") or [])
    )
    cards = tuple(
        CardDef(
            key=str(item.get("key") or "").strip(),
            title=str(item.get("title") or "").strip(),
            dept=str(item.get("dept") or "").strip(),
            node=str(item.get("node") or "").strip(),
        )
        for item in ((raw.get("demo_attachment") or {}).get("cards") or [])
    )

    workflow = Workflow(
        id=str(meta.get("id") or "").strip(),
        name=str(meta.get("name") or "").strip(),
        version=str(meta.get("version") or "").strip(),
        source=str(meta.get("source") or "").strip(),
        hidden=bool(meta.get("hidden", True)),
        note=str(meta.get("note") or "").strip(),
        departments=tuple(
            Department(id=str(item.get("id") or "").strip(), name=str(item.get("name") or "").strip())
            for item in (raw.get("departments") or [])
        ),
        nodes=nodes,
        edges=edges,
        cards=cards,
        rules=dict(raw.get("rules") or {}),
    )
    _validate(workflow)
    return workflow


def _validate(workflow: Workflow) -> None:
    """流程数据自检：连线的两端必须存在、部门 id 不许拼错、卡片必须挂在真实节点上。

    这些错误在界面上会表现为"线画到一半没了""点了没反应"，很难查，所以在加载时就报出来。
    """
    node_ids = {node.id for node in workflow.nodes}
    dept_ids = {dept.id for dept in workflow.departments}
    for edge in workflow.edges:
        if edge.source not in node_ids or edge.target not in node_ids:
            raise FlowDataError(f"连线两端有未知节点：{edge.source} → {edge.target}")
    for node in workflow.nodes:
        for dept_id in node.depts:
            if dept_id not in dept_ids:
                raise FlowDataError(f"节点 {node.id} 用了未声明的部门：{dept_id}")
    for card in workflow.cards:
        if card.dept not in dept_ids:
            raise FlowDataError(f"卡片 {card.key} 挂在未声明的部门：{card.dept}")
        if card.node not in node_ids:
            raise FlowDataError(f"卡片 {card.key} 挂在未声明的节点：{card.node}")


# ---------- 编辑（指令 16 第二步）：管理员改底层连接，写回 flow_data.json ----------
# 编辑是"真改数据"，不是内存里摆摆：每次改动先改原始 JSON、再落盘、再重载。
# 这样重启后流程还是改过的样子，管理员改的连接才算真正生效。
# 这些函数都返回改完之后的 Workflow，调用方拿去更新内存里的那份。


def _read_raw(path: Path) -> dict:
    if not path.exists():
        # 管理员第一次编辑：从一份空流程开始。
        return {
            "workflow": {"hidden": True},
            "departments": [],
            "nodes": [],
            "edges": [],
            "annotations": [],
            "demo_attachment": {"cards": []},
            "rules": {},
        }
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise FlowDataError(f"读不到流程数据 {path}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise FlowDataError(f"流程数据不是合法 JSON：{exc}") from exc


def _write_raw(path: Path, raw: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        raise FlowDataError(f"流程数据写不回去 {path}：{exc}") from exc


def _find_node(raw: dict, node_id: str) -> dict | None:
    return next((item for item in (raw.get("nodes") or []) if item.get("id") == node_id), None)


def next_node_id(path: Path) -> str:
    """生成一个不冲突的节点 id（n01…n18 已被用，往下顺延）。"""
    raw = _read_raw(path)
    used = {str(item.get("id") or "") for item in (raw.get("nodes") or [])}
    number = 1
    while f"n{number:02d}" in used:
        number += 1
    return f"n{number:02d}"


def add_node(
    path: Path,
    node_id: str,
    name: str,
    node_type: str,
    level: int,
    depts: list[str],
) -> Workflow:
    """加一个节点（默认不挂卡片；职责标注留空）。"""
    raw = _read_raw(path)
    if _find_node(raw, node_id) is not None:
        raise FlowDataError(f"节点 {node_id} 已经存在")
    raw.setdefault("nodes", []).append(
        {"id": node_id, "name": name, "type": node_type, "level": int(level), "dept": list(depts)}
    )
    _write_raw(path, raw)
    return load_workflow(path)


def update_node(
    path: Path,
    node_id: str,
    *,
    name: str | None = None,
    node_type: str | None = None,
    level: int | None = None,
    depts: list[str] | None = None,
) -> Workflow:
    """改一个节点的信息；没传的字段不动。"""
    raw = _read_raw(path)
    node = _find_node(raw, node_id)
    if node is None:
        raise FlowDataError(f"节点 {node_id} 不存在")
    if name is not None:
        node["name"] = name
    if node_type is not None:
        node["type"] = node_type
    if level is not None:
        node["level"] = int(level)
    if depts is not None:
        node["dept"] = list(depts)
    _write_raw(path, raw)
    return load_workflow(path)


def remove_node(path: Path, node_id: str) -> Workflow:
    """删一个节点：连带删掉所有接到它的连线、挂在它上面的卡片与职责标注。"""
    raw = _read_raw(path)
    if _find_node(raw, node_id) is None:
        raise FlowDataError(f"节点 {node_id} 不存在")
    raw["nodes"] = [item for item in (raw.get("nodes") or []) if item.get("id") != node_id]
    raw["edges"] = [
        item
        for item in (raw.get("edges") or [])
        if item.get("from") != node_id and item.get("to") != node_id
    ]
    raw["annotations"] = [
        item for item in (raw.get("annotations") or []) if item.get("node") != node_id
    ]
    demo = raw.get("demo_attachment") or {}
    demo["cards"] = [
        item for item in (demo.get("cards") or []) if item.get("node") != node_id
    ]
    raw["demo_attachment"] = demo
    _write_raw(path, raw)
    return load_workflow(path)


def add_edge(
    path: Path,
    source: str,
    target: str,
    label: str = "",
    style: str = "solid",
    kind: str = "",
) -> Workflow:
    """连一条线；同起点同终点只许有一条。"""
    raw = _read_raw(path)
    edges = raw.get("edges") or []
    if any(item.get("from") == source and item.get("to") == target for item in edges):
        raise FlowDataError(f"「{source} → {target}」这条连线已经有了")
    edges.append(
        {"from": source, "to": target, "label": label, "style": style, "kind": kind}
    )
    raw["edges"] = edges
    _write_raw(path, raw)
    return load_workflow(path)


def update_edge(
    path: Path,
    source: str,
    target: str,
    *,
    label: str | None = None,
    style: str | None = None,
    kind: str | None = None,
) -> Workflow:
    """改一条连线的标签/样式；用（起点，终点）定位，没传的字段不动。"""
    raw = _read_raw(path)
    for item in raw.get("edges") or []:
        if item.get("from") == source and item.get("to") == target:
            if label is not None:
                item["label"] = label
            if style is not None:
                item["style"] = style
            if kind is not None:
                item["kind"] = kind
            _write_raw(path, raw)
            return load_workflow(path)
    raise FlowDataError(f"没找到「{source} → {target}」这条连线")


def remove_edge(path: Path, source: str, target: str) -> Workflow:
    """断一条线。"""
    raw = _read_raw(path)
    before = len(raw.get("edges") or [])
    raw["edges"] = [
        item
        for item in (raw.get("edges") or [])
        if not (item.get("from") == source and item.get("to") == target)
    ]
    if len(raw.get("edges") or []) == before:
        raise FlowDataError(f"没找到「{source} → {target}」这条连线")
    _write_raw(path, raw)
    return load_workflow(path)
