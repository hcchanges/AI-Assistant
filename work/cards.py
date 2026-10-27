"""
工作卡片（指令 16 第一步；指令 19 ④ 补了卡面上的短说明）。

"哪张卡挂在哪个部门/节点" 由 JSON 声明（work/flow_data.json 的 demo_attachment.cards），
这里只补两样东西：卡片墙/详情页那句引导说明，和"点进这张卡是哪套界面"。

之所以不在这里再抄一遍部门与节点：两处各写一份迟早走样（改了 JSON 忘了改代码，
界面上就会出现点了没反应的卡）。测试里有一条专门盯着"JSON 里每张卡这里都有交代"。

没挂功能卡的部门不再造占位卡（指令 19 ⑥ 改成在部门卡里画「+」），所以这里只管真卡。
"""

from __future__ import annotations

from work.flow import FlowDataError

# 卡片墙上的说明：帮用户判断这张卡是干嘛的（详情页顶部用，可以写全）
CARD_BLURBS: dict[str, str] = {
    "bom_optimize": "上传 BOM（Excel/CSV），逐行联网找替代料，算清能省多少钱",
    "sales_trend": "填品类与平台，联网检索当下的潮流词与热度，给出 Top N 卖点",
}

# 卡面上一行字的小说明（指令 19 ④：卡里放不下长句，另备一份短的）
CARD_BRIEFS: dict[str, str] = {
    "bom_optimize": "上传 BOM，逐行联网找替代料并算省钱",
    "sales_trend": "填品类与平台，联网给出 Top N 卖点",
}

# 卡片 → 详情页（ui/panels/work_card_panel.py 里的两套界面）
CARD_PAGES: dict[str, str] = {
    "bom_optimize": "bom",
    "sales_trend": "trend",
}


def blurb_of(key: str) -> str:
    try:
        return CARD_BLURBS[key]
    except KeyError as exc:
        raise FlowDataError(f"卡片 {key} 还没写说明（work/cards.py 的 CARD_BLURBS）") from exc


def brief_of(key: str) -> str:
    """卡面上那一行短说明（指令 19 ④）。"""
    try:
        return CARD_BRIEFS[key]
    except KeyError as exc:
        raise FlowDataError(f"卡片 {key} 还没写短说明（work/cards.py 的 CARD_BRIEFS）") from exc


def page_of(key: str) -> str:
    try:
        return CARD_PAGES[key]
    except KeyError as exc:
        raise FlowDataError(f"卡片 {key} 还没接界面（work/cards.py 的 CARD_PAGES）") from exc
