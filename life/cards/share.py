"""
卡牌分享（对应指令 13 第 7 节）：生成分享码 / 从分享码还原。

这是纯本地软件，没有服务器，做不到"发个链接别人就能打开"，所以分享 = 一段可复制的码：
对方在自己的宠物里粘贴这段码，就能一键把这张卡加进他的生活广场。

隐私红线（指令 13 第 7 节：分享前剔除个人隐私信息）：
公开快照里**只有**卡牌模板本身——标题 / 主题 / 目标 / 能力开关。
记忆、对话原文、来源记忆 id 一律不出去，所以 snapshot_public 由 public_snapshot() 单独构造，
不是把整张卡序列化出去。
"""

from __future__ import annotations

import base64
import binascii
import json
import uuid

# 分享码的抬头：方便用户看出这是什么东西，也方便导入时定位
SHARE_PREFIX = "【电子宠物·卡牌分享】"
SHARE_VERSION = 1


def public_snapshot(card) -> dict:
    """卡牌可以对外的那部分。白名单式构造，避免以后加字段时不小心漏出去。"""
    return {
        "title": card.title,
        "theme": card.theme,
        "goal": card.goal,
        "features": card.feature_list(),
    }


def new_share_id() -> str:
    return uuid.uuid4().hex[:12]


def snapshot_json(snapshot: dict) -> str:
    """存进 card_share.snapshot_public 的形式。"""
    return json.dumps(snapshot, ensure_ascii=False)


def build_code(snapshot: dict, share_id: str) -> str:
    """拼成一段可以直接发给别人的分享码。"""
    payload = {"v": SHARE_VERSION, "id": share_id, "card": snapshot}
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    return f"{SHARE_PREFIX}\n{base64.urlsafe_b64encode(raw).decode('ascii')}"


def parse_code(text: str) -> dict | None:
    """从粘贴进来的文字里还原分享内容；看不懂就返回 None。"""
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith(SHARE_PREFIX):
            continue
        try:
            data = json.loads(base64.urlsafe_b64decode(line.encode("ascii")).decode("utf-8"))
        except (ValueError, UnicodeDecodeError, binascii.Error):
            continue
        if isinstance(data, dict) and isinstance(data.get("card"), dict):
            return data
    return None
