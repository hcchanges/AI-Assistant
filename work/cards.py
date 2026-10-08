"""
工作卡片的说明与详情页对照（公开版占位）。

内部版在这里写"哪张卡挂在哪个部门/节点、点进去是哪套界面、卡面上一行字写什么"，
数据本身在 `flow_data.json`。公开仓一张工作卡都没有，所以这里三张表都是空的，
三个查询函数照签名留着（拿到不认识的 key 就抛 `FlowDataError`，跟内部版一致）。
"""

from __future__ import annotations

from work.flow import FlowDataError

# 卡片墙上的说明（详情页顶部用）
CARD_BLURBS: dict[str, str] = {}

# 卡面上一行字的小说明
CARD_BRIEFS: dict[str, str] = {}

# 卡片 → 详情页
CARD_PAGES: dict[str, str] = {}


def blurb_of(key: str) -> str:
    try:
        return CARD_BLURBS[key]
    except KeyError as exc:
        raise FlowDataError(f"公开版没有工作卡 {key}。") from exc


def brief_of(key: str) -> str:
    try:
        return CARD_BRIEFS[key]
    except KeyError as exc:
        raise FlowDataError(f"公开版没有工作卡 {key}。") from exc


def page_of(key: str) -> str:
    try:
        return CARD_PAGES[key]
    except KeyError as exc:
        raise FlowDataError(f"公开版没有工作卡 {key}。") from exc
