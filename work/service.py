"""
工作模块的服务层（公开版占位）。

内部版在这里管四件事：流程与卡片的查询、员工自建卡（落 `custom_cards` 表）、BOM 卡的
上传与找替代、卖点卡的一次预测。

公开仓这几件一件都不做：同名方法都在（装配层照常接线），给出来的都是"没有"这个答案——
部门列表空、找不到卡、认不出部门、不读文件。**不返回编造的假数据**，那样调用方会以为
真跑通了。界面上这一页是锁着的（见 `ui/panels/work_panel.py`）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from work.chain import WorkLocked
from work.flow import CardDef, Workflow

logger = logging.getLogger(__name__)

# 卖点卡的平台与时间范围选项（内部版界面下拉直接用；公开版只留常量）
PLATFORMS = ("小红书", "抖音", "微博", "淘宝", "B站")
PERIODS = ("最近 7 天", "最近 30 天", "最近 90 天")

# 每个部门卡里最少摆几格
DEPT_SLOT_MIN = 3

# 员工自建卡：每个部门最多几张、标题最长几个字
CUSTOM_CARD_MAX = 3
CUSTOM_TITLE_MAX = 10
CUSTOM_CARD_ICONS = ()


class WorkCardError(Exception):
    """自建卡的信息不合适（公开版：这个功能是锁着的）。"""


@dataclass(frozen=True)
class SlotView:
    """部门卡里的一格（内部版：真功能卡 / 员工自建卡 / 空位）。"""

    key: str | None
    title: str
    brief: str
    icon: str = ""
    custom_id: int | None = None

    def label(self) -> str:
        return f"{self.icon} {self.title}".strip()


class WorkService:
    """只保留构造签名与最外层要问的那几个问题的答案。"""

    def __init__(
        self,
        config: Any = None,
        bus: Any = None,
        chain: Any = None,
        role: str = "",
        card_dao: Any = None,
        flow_path: Path | None = None,
    ):
        self._config = config
        self._bus = bus
        self._chain = chain
        self._role = role
        self._card_dao = card_dao
        self._flow_path = flow_path
        # 公开版没有一个节点、一张卡：给出去的是空流程
        self.workflow: Workflow = Workflow()

    # ---------- 身份（内部版：管理员 / 员工两档） ----------

    def set_role(self, role: str) -> None:
        self._role = role

    def is_admin(self) -> bool:
        """公开版一律不是管理员：解锁版才谈得上身份。"""
        return False

    def set_comment_enabled(self, enabled: bool) -> None:
        self._comment_enabled = bool(enabled)

    @property
    def comment_enabled(self) -> bool:
        return bool(getattr(self, "_comment_enabled", False))

    # ---------- 流程与卡片（全是"没有"） ----------

    def departments(self) -> tuple:
        return ()

    def dept_name(self, dept_id: str) -> str:
        return ""

    def nodes_of_dept(self, dept_id: str) -> tuple:
        return ()

    def cards_of(self, dept_id: str) -> tuple:
        return ()

    def guess_dept(self, text: str) -> str | None:
        """从一句话里猜部门（内部版按部门名与职责词匹配）；公开版没有部门可猜。"""
        return None

    def dept_slots(self, dept_id: str) -> tuple:
        return ()

    def custom_cards(self, dept_id: str) -> tuple:
        return ()

    def custom_count(self, dept_id: str) -> int:
        return 0

    def custom_full(self, dept_id: str) -> bool:
        return True

    def add_custom_card(self, *args, **kwargs):
        raise WorkLocked("工作卡的增删改需要内部授权版。")

    def update_custom_card(self, *args, **kwargs):
        raise WorkLocked("工作卡的增删改需要内部授权版。")

    def delete_custom_card(self, *args, **kwargs):
        raise WorkLocked("工作卡的增删改需要内部授权版。")

    def card_place(self, card: CardDef | None) -> str:
        return ""

    def card_blurb(self, key: str) -> str:
        return ""

    def card_brief(self, key: str) -> str:
        return ""

    def card_page(self, key: str) -> str:
        return ""

    def flow_summary(self) -> str:
        return ""

    # ---------- 流程编辑（内部版：管理员专用） ----------

    def edit_add_node(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    def edit_update_node(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    def edit_remove_node(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    def edit_add_edge(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    def edit_update_edge(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    def edit_remove_edge(self, *args, **kwargs):
        raise WorkLocked("改流程需要内部授权版。")

    # ---------- 两张功能卡（BOM / 卖点） ----------

    def bom(self):
        return None

    def open_bom(self, path):
        raise WorkLocked("上传 BOM 需要内部授权版。")

    def row(self, row_index: int):
        return None

    def find_alternatives(self, row_index: int) -> None:
        raise WorkLocked("BOM 找替代料需要内部授权版。")

    def result_of(self, row_index: int):
        return None

    def accept(self, row_index: int, alternative) -> None:
        raise WorkLocked("BOM 卡片需要内部授权版。")

    def ignore(self, row_index: int) -> None:
        raise WorkLocked("BOM 卡片需要内部授权版。")

    def accepted_of(self, row_index: int):
        return None

    def saving_of(self, row_index: int):
        return None

    def savings(self) -> tuple[float, float | None]:
        return (0.0, None)

    def export(self, path) -> int:
        raise WorkLocked("导出 BOM 需要内部授权版。")

    def report(self):
        return None

    def predict_trends(self, category: str, platform: str, period: str) -> None:
        raise WorkLocked("爆品卖点预测需要内部授权版。")
