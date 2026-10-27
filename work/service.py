"""
工作模块的服务层（指令 16 第一步；指令 21 加了员工自建卡）。

管四件事：
1. 流程与卡片：读 work/flow_data.json，给出部门、节点、各部门挂着的卡片（纯查询）；
2. 员工自建卡（指令 21）：部门卡里员工自己加的卡，存在 custom_cards 表里，可增可改可删；
3. BOM 卡：上传解析（本地做）、逐行找替代（联网，跑后台线程）、接受/忽略、算节省、导出；
4. 卖点卡：一次联网预测，结果留在内存里给界面看。

单用户占位（指令 16 第 6 节）：不做完整账号体系；设置页用轻量「管理者登录」
（固定常量比对）把 role 写成 admin / user，再由配置项 work.role 带进来。
管理员多一个"编辑底层连接"的入口（指令 21）。

BOM 与结果都不入库（指令 16 第 4 节：不预置、上传时现算），
所以这些都是"一次会话内的状态"，重启就清空——演示够用，也不给用户电脑留文件；
自建卡不一样：它是用户自己攒的东西，落库、重启还在（指令 21 第 4 节）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from core.config import WORK_ROLE_ADMIN, Config
from core.events import EventBus
from core.worker import run_async
from data.dao import CustomCardDAO
from data.models import CustomCard
from work.bom import PRICE, QTY, BomError, BomRow, BomTable, export_csv, parse_bom, parse_number
from work.cards import brief_of, blurb_of, page_of
from work.chain import WorkChain
from work import flow as flow_module
from work.flow import (
    DATA_FILE,
    CardDef,
    Department,
    Node,
    Workflow,
    load_workflow,
)
from work.schemas import Alternative, AlternativeResult, TrendReport

logger = logging.getLogger(__name__)

# 管理者登录（阶段一，轻量）：直接比对源码里的固定账号 / 密码。
# 暂不接 User.password_hash，也不做会话 / 令牌；正式账号体系仍待后续阶段。
DEFAULT_ADMIN_USER = "abc"
DEFAULT_ADMIN_PASS = "abc123456"

# 卖点卡的平台与时间范围选项（界面下拉直接用）
PLATFORMS = ("小红书", "抖音", "微博", "淘宝", "B站")
PERIODS = ("最近 7 天", "最近 30 天", "最近 90 天")

# 每个部门卡里最少摆几格（指令 21 + 指令 25-C）：初始就 3 格——没功能卡的部门是
# 空位 + 空位 + 「＋」，有功能卡的是功能卡 + 空位 + 「＋」，一屏（CHILD_PER_VIEW=3）
# 正好看完，不用上下拖；员工建了自建卡，格子跟着长，都摆得下
DEPT_SLOT_MIN = 3

# ---------- 员工自建卡（指令 21） ----------

CUSTOM_CARD_MAX = 3          # 每个部门最多 3 张自建卡
CUSTOM_TITLE_MAX = 10        # 标题最长 10 个字
CUSTOM_CARD_ICONS = ("📋", "🧾", "📦", "🔧", "📊", "⏰", "💡", "✏️")   # 新建卡时可选的图标


class WorkCardError(Exception):
    """自建卡的信息不合适（标题空 / 太长 / 这个部门的 3 个位置已经满了）。"""


@dataclass(frozen=True)
class SlotView:
    """部门卡里的一格（指令 21）：真功能卡，或员工自建卡；空卡位在 dept_slots 里是 None。"""

    key: str | None                  # 功能卡的 key；自建卡还没有功能页，所以是 None
    title: str
    brief: str                       # 卡面上那一行短说明（功能卡取 brief，自建卡取描述）
    icon: str = ""                   # 自建卡的图标；功能卡没有
    custom_id: int | None = None     # 自建卡的 id（右键编辑 / 删除要用）；功能卡是 None

    def label(self) -> str:
        """卡面标题：自建卡带上图标，功能卡就是名字。"""
        return f"{self.icon} {self.title}".strip()


def _check_custom_title(title: str) -> str:
    """自建卡的标题：必须写、最长 CUSTOM_TITLE_MAX 个字，返回去掉两端空白的结果。"""
    text = title.strip()
    if not text:
        raise WorkCardError("给这张卡起个名字吧")
    if len(text) > CUSTOM_TITLE_MAX:
        raise WorkCardError(f"标题最多 {CUSTOM_TITLE_MAX} 个字")
    return text


class WorkService:
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        chain: WorkChain,
        role: str,
        card_dao: CustomCardDAO,
        flow_path: Path | None = None,
    ):
        self._config = config
        self._bus = bus
        self._chain = chain
        self._role = role
        self._card_dao = card_dao
        # 流程数据文件（测试传临时路径，别碰真实的 work/flow_data.json）
        self._flow_path = flow_path or DATA_FILE
        self.workflow: Workflow = load_workflow(self._flow_path)

        self._bom: BomTable | None = None
        self._results: dict[int, AlternativeResult] = {}      # 行号 → 找替代的结果
        self._accepted: dict[int, Alternative] = {}           # 行号 → 已接受的替代
        self._report: TrendReport | None = None
        self._comment_enabled = config.work.pet_comment        # 结果末尾是否让宠物点评一句

    # ---------- 结果末尾的宠物点评（可关） ----------

    def set_comment_enabled(self, enabled: bool) -> None:
        self._comment_enabled = enabled

    @property
    def comment_enabled(self) -> bool:
        return self._comment_enabled

    # ---------- 流程与卡片 ----------

    def is_admin(self) -> bool:
        """能不能看/改底层连接：管理员可以，员工不行（身份来自配置里的调试切换）。"""
        return self._role == WORK_ROLE_ADMIN

    def set_role(self, role: str) -> None:
        """设置当前身份：设置页「管理者登录 / 退出」时调用。"""
        self._role = role

    def departments(self) -> tuple[Department, ...]:
        return self.workflow.departments

    def dept_name(self, dept_id: str) -> str:
        return self.workflow.dept_name(dept_id)

    def nodes_of_dept(self, dept_id: str) -> tuple[Node, ...]:
        """这个部门参与哪些节点（卡片墙顶部会写出来，让用户知道这张卡在流程里处于哪一步）。"""
        return tuple(node for node in self.workflow.nodes if dept_id in node.depts)

    def cards_of(self, dept_id: str) -> tuple[CardDef, ...]:
        """挂在这个部门的功能卡（可能为空——空的时候界面画「+」占位）。"""
        return self.workflow.cards_of(dept_id)

    def guess_dept(self, text: str) -> str | None:
        """这句话大概在说哪个部门（指令 26-B：开工作模块时尽量落到对应那张部门卡）。

        两样东西对字面：直接把部门叫出来的（"研发部那边…"），和配置里那些工作词落在谁的范围里
        （"BOM" 对上研发部那张《研发部·BOM 物料优化》，"成本核算" 对上成本部）。
        命中最多的算；一个都对不上就返回 None，界面停在原来那张卡上——猜不准比乱跳好。

        大小写不敏感：用户打小写「bom」，词表存的是大写「BOM」，得照样落到研发部。
        """
        lowered = text.lower()
        words = [word for word in self._config.intent.work_words if word.lower() in lowered]
        best: str | None = None
        best_hits = 0
        for department in self.workflow.departments:
            blob = self._dept_blob(department.id).lower()
            hits = sum(1 for word in words if word.lower() in blob)
            if department.name in text:
                hits += 1
            if hits > best_hits:
                best, best_hits = department.id, hits
        return best

    def _dept_blob(self, dept_id: str) -> str:
        """这个部门的"话头"：部门名 + 它参与的节点名 + 挂在它这儿的卡名与说明。"""
        parts = [self.dept_name(dept_id)]
        parts += [node.name for node in self.nodes_of_dept(dept_id)]
        for card in self.cards_of(dept_id):
            parts += [card.title, brief_of(card.key), blurb_of(card.key)]
        return " ".join(parts)

    def dept_slots(self, dept_id: str) -> tuple[SlotView | None, ...]:
        """这个部门卡里的卡位：功能卡 → 员工自建卡 → 空位（None，界面画「＋」）。

        格子数按实际用到的算：功能卡 + 已有自建卡 + 一个「＋」（点它新建自建卡），
        再兜一个 DEPT_SLOT_MIN 的下限——新部门一上来就有 3 格，一屏正好看完；
        员工建了自建卡，格子跟着长（建满 3 张时是 功能卡 + 3 张自建 + 「＋」）。
        没满的时候点「＋」是新建，3 张已满时点它就是"已满"提示（指令 21）。
        """
        cards = self.cards_of(dept_id)
        slots: list[SlotView | None] = [
            SlotView(key=card.key, title=card.title, brief=brief_of(card.key)) for card in cards
        ]
        slots += [
            SlotView(
                key=None,
                title=card.title,
                brief=card.description,
                icon=card.icon,
                custom_id=card.id,
            )
            for card in self.custom_cards(dept_id)
        ]
        total = max(DEPT_SLOT_MIN, len(slots) + 1)
        return tuple(slots + [None] * max(total - len(slots), 0))

    # ---------- 员工自建卡（指令 21） ----------
    # 系统功能卡不在 custom_cards 表里，这几个方法都按自建卡的 id 操作，所以系统卡改不到也删不掉。

    def custom_cards(self, dept_id: str) -> tuple[CustomCard, ...]:
        """这个部门里员工自己建的卡（按摆放顺序）。"""
        return tuple(self._card_dao.list_by_dept(dept_id))

    def custom_card(self, card_id: int) -> CustomCard | None:
        return self._card_dao.get(card_id)

    def custom_count(self, dept_id: str) -> int:
        return self._card_dao.count_by_dept(dept_id)

    def custom_full(self, dept_id: str) -> bool:
        """这个部门的 3 个自建卡位是不是已经占满了。"""
        return self.custom_count(dept_id) >= CUSTOM_CARD_MAX

    def add_custom_card(
        self, dept_id: str, title: str, description: str, icon: str
    ) -> CustomCard:
        """新建一张自建卡；标题不合法或这个部门满了就抛 WorkCardError。"""
        text = _check_custom_title(title)
        if self.custom_full(dept_id):
            raise WorkCardError(f"已满 {CUSTOM_CARD_MAX} 张，先删除一张")
        card = self._card_dao.add(dept_id, text, description.strip(), icon)
        self._bus.work_cards_changed.emit()
        return card

    def update_custom_card(
        self, card_id: int, title: str, description: str, icon: str
    ) -> CustomCard | None:
        """改一张自建卡的标题 / 描述 / 图标。"""
        text = _check_custom_title(title)
        self._card_dao.update(card_id, text, description.strip(), icon)
        self._bus.work_cards_changed.emit()
        return self._card_dao.get(card_id)

    def delete_custom_card(self, card_id: int) -> None:
        """删掉一张自建卡（删了就没了）。"""
        self._card_dao.delete(card_id)
        self._bus.work_cards_changed.emit()

    def card_place(self, card: CardDef) -> str:
        """这张卡在流程里的位置：部门 · 环节。"""
        node = self.workflow.node(card.node)
        if node is None:
            return self.dept_name(card.dept)
        return f"{self.dept_name(card.dept)} · {node.name}"

    def card_blurb(self, key: str) -> str:
        return blurb_of(key)

    def card_brief(self, key: str) -> str:
        """卡面上那一行短说明（指令 19 ④）。"""
        return brief_of(key)

    def card_page(self, key: str) -> str:
        return page_of(key)

    def flow_summary(self) -> str:
        flow = self.workflow
        return (
            f"{flow.name} · {len(flow.departments)} 个部门 · {len(flow.nodes)} 个节点 · "
            f"{len(flow.edges)} 条连线"
            f"（主线 {flow.main_count} · NG 回流 {flow.ng_count} · 并行 {flow.parallel_count}）"
        )

    # ---------- 编辑底层连接（指令 16 第二步，仅管理员） ----------

    def _apply_edit(self, workflow: Workflow) -> None:
        """编辑完把新流程存进内存、通知界面重建。"""
        self.workflow = workflow
        self._bus.work_flow_changed.emit()

    def edit_add_node(self, name: str, node_type: str, level: int, depts: list[str]) -> str:
        node_id = flow_module.next_node_id(self._flow_path)
        self._apply_edit(
            flow_module.add_node(self._flow_path, node_id, name, node_type, level, depts)
        )
        return node_id

    def edit_update_node(
        self,
        node_id: str,
        *,
        name: str | None = None,
        node_type: str | None = None,
        level: int | None = None,
        depts: list[str] | None = None,
    ) -> None:
        self._apply_edit(
            flow_module.update_node(
                self._flow_path, node_id, name=name, node_type=node_type, level=level, depts=depts
            )
        )

    def edit_remove_node(self, node_id: str) -> None:
        self._apply_edit(flow_module.remove_node(self._flow_path, node_id))

    def edit_add_edge(self, source: str, target: str, label: str, style: str, kind: str) -> None:
        self._apply_edit(
            flow_module.add_edge(self._flow_path, source, target, label, style, kind)
        )

    def edit_update_edge(
        self,
        source: str,
        target: str,
        *,
        label: str | None = None,
        style: str | None = None,
        kind: str | None = None,
    ) -> None:
        self._apply_edit(
            flow_module.update_edge(
                self._flow_path, source, target, label=label, style=style, kind=kind
            )
        )

    def edit_remove_edge(self, source: str, target: str) -> None:
        self._apply_edit(flow_module.remove_edge(self._flow_path, source, target))

    # ---------- BOM 卡 ----------

    @property
    def bom(self) -> BomTable | None:
        return self._bom

    def open_bom(self, path: str | Path) -> BomTable:
        """读一份 BOM：解析在本地做（不联网），读完清掉上一份的结果。"""
        table = parse_bom(path)
        self._bom = table
        self._results.clear()
        self._accepted.clear()
        logger.info("BOM 已载入：%s（%d 行）", table.filename, len(table.rows))
        return table

    def row(self, row_index: int) -> BomRow | None:
        if self._bom is None:
            return None
        return next((item for item in self._bom.rows if item.index == row_index), None)

    def find_alternatives(self, row_index: int) -> None:
        """给一行找替代料（联网，后台跑）。结果通过 work_alternatives 信号回来。"""
        row = self.row(row_index)
        if row is None:
            self._bus.toast.emit("这一行不在当前的 BOM 里，可能换过文件了")
            return
        run_async(
            self._chain.alternatives,
            row,
            on_done=self._on_alternatives,
            on_failed=self._failed,
        )

    def _on_alternatives(self, result: AlternativeResult) -> None:
        self._results[result.row_index] = result
        self._bus.work_alternatives.emit(result.row_index, result)
        # 点评一句（可关）：结果是给用户看的，宠物在末尾带一句自己的看法
        if result.alternatives:
            self._maybe_comment(
                "bom_optimize",
                f"「{result.item_label}」找到 {len(result.alternatives)} 条替代，"
                f"首推 {result.alternatives[0].model}（{result.alternatives[0].price or '未标价'}）",
            )

    def result_of(self, row_index: int) -> AlternativeResult | None:
        return self._results.get(row_index)

    def accept(self, row_index: int, alternative: Alternative) -> None:
        self._accepted[row_index] = alternative
        self._bus.work_alternatives.emit(row_index, self._results.get(row_index))

    def ignore(self, row_index: int) -> None:
        self._accepted.pop(row_index, None)
        self._results.pop(row_index, None)
        self._bus.work_alternatives.emit(row_index, None)

    def accepted_of(self, row_index: int) -> Alternative | None:
        return self._accepted.get(row_index)

    def saving_of(self, row_index: int) -> float | None:
        """这一行接受替代后能省多少钱（单价差 × 用量）。缺单价或用量算不出来就返回 None。"""
        row = self.row(row_index)
        alternative = self._accepted.get(row_index)
        if row is None or alternative is None:
            return None
        old_price = row.number(PRICE)
        new_price = parse_number(alternative.price)
        if old_price is None or new_price is None:
            return None
        return (old_price - new_price) * (row.number(QTY) or 1.0)

    def savings(self) -> tuple[float, float | None]:
        """整批的预计节省：(省下的钱, 占整张 BOM 金额的比例)。算不出比例时比例是 None。"""
        saved = 0.0
        for row_index in self._accepted:
            amount = self.saving_of(row_index)
            if amount is not None:
                saved += amount
        total = self._bom.amount if self._bom is not None else 0.0
        ratio = saved / total if total > 0 else None
        return saved, ratio

    def export(self, path: str | Path) -> int:
        """导出优化后的 BOM（CSV，Excel 能直接打开）：只写接受过替代的行。"""
        rows: list[tuple[BomRow, dict[str, str]]] = []
        for row_index, alternative in self._accepted.items():
            row = self.row(row_index)
            if row is None:
                continue
            saving = self.saving_of(row_index)
            rows.append(
                (
                    row,
                    {
                        "model": alternative.model,
                        "price": alternative.price,
                        "saving": f"{saving:.2f}" if saving is not None else "",
                    },
                )
            )
        if not rows:
            raise BomError("还没接受任何替代料，导出会和原文件一样")
        export_csv(rows, path)
        logger.info("已导出优化后的 BOM：%s（%d 行）", Path(path).name, len(rows))
        return len(rows)

    # ---------- 卖点卡 ----------

    @property
    def report(self) -> TrendReport | None:
        return self._report

    def predict_trends(self, category: str, platform: str, period: str) -> None:
        """联网预测卖点（后台跑），结果通过 work_trends 信号回来。"""
        text = category.strip()
        if not text:
            self._bus.toast.emit("先写上要预测的品类或关键词")
            return
        run_async(
            self._chain.trends,
            text,
            platform,
            period,
            on_done=self._on_trends,
            on_failed=self._failed,
        )

    def _on_trends(self, report: TrendReport) -> None:
        self._report = report
        self._bus.work_trends.emit(report)
        # 点评一句（可关）：报告主体保持专业，这一句是宠物在末尾带的话
        self._maybe_comment(
            "sales_trend",
            f"「{report.category}」在{report.platform}给出 {len(report.points)} 条卖点：{report.summary}",
        )

    # ---------- 宠物点评（可关） ----------

    def _maybe_comment(self, key: str, summary: str) -> None:
        if not self._comment_enabled:
            return
        run_async(
            self._chain.comment,
            key,
            summary,
            on_done=lambda text: self._bus.work_comment.emit(key, text),
            on_failed=lambda exc: logger.info("宠物点评没生成，跳过：%s", exc),
        )

    # ---------- 出错 ----------

    def _failed(self, exc: Exception) -> None:
        logger.error("工作卡片联网检索失败: %s", exc)
        self._bus.toast.emit(f"联网检索没成功：{exc}")
