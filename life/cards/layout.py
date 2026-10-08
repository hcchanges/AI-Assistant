"""
卡面布局（v2-card-tools-C）：卡上那几块东西能拖着换位置，换完记下来，重开还在。

**为什么不摆成自由画布。** 卡面是一条**竖列**，宽度永远跟着窗口走，建议区还要吃掉
剩下的高度（`stretch 1`）——这是它原来就有的"响应式"脾气。要是把它改成"每块给一个
绝对坐标、爱摆哪摆哪"，窗口一窄就互相盖住，卡也就没法看了。所以这一版的做法是：

- **拖动改变的是次序**（拖到哪一行，就插到第几个）；
- **记下来的除了次序，还有落下那一刻每块的真实坐标**（`pos` 里的 x / y）——
  坐标是**照实量的**（`mapTo(板子)` 量出来再存），不是编的；重开时按次序摆回去，
  摆完再量一遍：跟存的对得上就用不着管，对不上（换过窗口大小）就**按新的写回**。

一句话：**次序是用户的意思，坐标是当下的事实**；事实变了就以事实为准，
用户的意思（次序）永远留着。这也就是跟"卡面本来是响应式"这件事的边界——
不去跟它抢位置，只记住人家长什么样。

**存过的块号对不上现在的卡**（老记录、块没了、以后加了新块）：认识的照记的次序排，
不认识的丢掉，缺的补到最后（见 `apply_order`）。**不报错、不吞掉、不把卡弄成空白。**

落点跟卡窗记录、卡上部件一个规矩：一张卡一个文件 `data/card_layout/<卡号>.json`，
本机运行期数据，不入库。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Any, Iterable

from core.config import Config

logger = logging.getLogger(__name__)

#: 卡面布局的落点（一张卡一个文件）。模块级常量，理由跟 CHAT_DIR / TOOLS_DIR 一样：
#: 测试得能在构造应用之前换到临时目录（见 tests/harness.py）。
LAYOUT_DIR = "cache/card_layout"

#: 卡面上那几块东西的块号。界面（ui/card_layout.py 的 CardBoard）按这些号认块，
#: 记录文件里存的也是这些号——两边用同一套常量，免得改一边忘一边。
BLOCK_HEAD = "head"        # 卡头：标题 + 状态
BLOCK_ADVICE = "advice"    # 上半·建议区（生活卡）
BLOCK_BODY = "body"        # 工作卡中间那块数据页
BLOCK_TOOLS = "tools"      # 下半·辅助工具区（生活卡）
BLOCK_WIDGETS = "widgets"  # 卡上长出来的小部件（v2-card-tools-B）
BLOCK_CHAT = "chat"        # 卡底·卡内小助手（v2-card-tools-A）

#: 生活卡没存过布局（或者按了「重置布局」）时的次序：跟这一版之前一模一样，从上到下。
#: 工作卡是另一套块（有数据页、没有"上下两半"），它的默认次序就是它自己登记的那几块
#: ——所以"是不是默认"这件事由调用方把自己那份递进来（`is_default(order, default)`），
#: 不在这里替它猜。猜错的后果是：工作卡每开一次都白写一份"默认次序"的记录。
DEFAULT_ORDER: tuple[str, ...] = (
    BLOCK_HEAD, BLOCK_ADVICE, BLOCK_TOOLS, BLOCK_WIDGETS, BLOCK_CHAT,
)

#: 工作卡的默认次序：四块（没有"上下两半"那两块，中间那块是数据页）。
WORK_DEFAULT_ORDER: tuple[str, ...] = (BLOCK_HEAD, BLOCK_BODY, BLOCK_WIDGETS, BLOCK_CHAT)


def apply_order(saved: Iterable[Any], available: Iterable[str]) -> list[str]:
    """把记下来的次序对到"这张卡上现在真有的块"上。

    - 认识的（在 `available` 里）照记的次序排，重复的只算一次；
    - 记录里不认识的块号（块被删了 / 老记录）**丢掉**；
    - 卡上有、记录里没有的块（以后新加的）补到最后，**不能让它消失**。

    这条是"拖动编排"最容易出事的地方：直接把存下来的次序当布局用，卡上就会少一块，
    而且看不出来少在哪。
    """
    have = list(dict.fromkeys(str(item) for item in available))
    known = [str(key) for key in saved if str(key) in have]
    ordered = list(dict.fromkeys(known))
    return ordered + [key for key in have if key not in ordered]


def is_default(order: Iterable[str], default: Iterable[str]) -> bool:
    """这份次序是不是就是"这张卡本来的样子"（是的话没必要存、也没有可重置的）。"""
    return list(order) == list(default)


class CardLayoutStore:
    """一张卡一个 JSON：`{"order": [...], "pos": {块号: {"x": .., "y": ..}}}`。"""

    def __init__(self, config: Config):
        self._config = config

    def path(self, namespace: str) -> Path:
        return self._config.data_path(f"{LAYOUT_DIR}/{_safe_stem(namespace)}.json")

    def load(self, namespace: str) -> dict[str, Any]:
        """读这张卡摆成什么样。文件不在 / 坏了都当"没摆过"——回到默认次序。"""
        try:
            raw = json.loads(self.path(namespace).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("卡面布局：%s 的记录读不出来（%s），按默认次序", namespace, exc)
            return {"order": [], "pos": {}}
        if not isinstance(raw, dict):
            return {"order": [], "pos": {}}
        order = [str(key) for key in raw.get("order") or [] if isinstance(key, str)]
        pos = raw.get("pos")
        return {"order": order, "pos": pos if isinstance(pos, dict) else {}}

    def order(self, namespace: str) -> list[str]:
        """记下来的次序（没存过/按了重置就是空的——由调用方补默认次序）。"""
        return self.load(namespace)["order"]

    def save(
        self,
        namespace: str,
        order: list[str],
        pos: dict[str, Any],
        default: Iterable[str] | None = None,
    ) -> bool:
        """把次序与那一刻量到的坐标写下来。**返回写完还有没有记录。**

        `default` 是"这张卡本来的样子"（调用方把它的默认次序递进来）：摆来摆去又摆回
        默认，就把文件删掉——留一份"跟默认一样"的记录没有意义，还会让「重置布局」
        这个按钮一直亮着。传 None 就是不比较，照写。
        """
        path = self.path(namespace)
        if default is not None and is_default(order, default):
            self.clear(namespace)
            return False
        payload = json.dumps(
            {"order": list(order), "pos": pos}, ensure_ascii=False, indent=2
        )
        tmp = path.with_name(path.name + ".tmp")
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("卡面布局：%s 写不进去（%s），这一次只留在界面上", namespace, exc)
            return False
        return True

    def clear(self, namespace: str) -> None:
        """回到默认次序：把记录删掉（不是写一份默认次序进去）。"""
        try:
            self.path(namespace).unlink(missing_ok=True)
        except OSError as exc:
            logger.error("卡面布局：%s 的记录删不掉（%s）", namespace, exc)


def _safe_stem(namespace: str) -> str:
    """卡号 → 文件名（跟 life/cards/chat.py、tools.py 同一套写法）。"""
    return re.sub(r"[^0-9A-Za-z_-]", "_", namespace) or "card"


__all__ = [
    "BLOCK_ADVICE", "BLOCK_BODY", "BLOCK_CHAT", "BLOCK_HEAD", "BLOCK_TOOLS",
    "BLOCK_WIDGETS", "DEFAULT_ORDER", "LAYOUT_DIR", "WORK_DEFAULT_ORDER",
    "CardLayoutStore", "apply_order", "is_default",
]
