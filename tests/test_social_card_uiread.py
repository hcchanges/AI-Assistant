# -*- coding: utf-8 -*-
"""对象攻略卡·「对方档案」写/读断链修复（v2-social-card-uiread）的自动测试。

**病根**（只读回填 + 真机实测）：`v2-social-card-autodistill` 把「对面这个人是谁」抽出来，
经 `core/bootstrap.py::_write_guide_card_fields` → `CardService.set_profile` 写进 `card` 表的
八列（`data/dao.py::_PROFILE_COLUMNS`：`relation_stage / likes / interests / dislikes /
age / emotion_state / anxiety / strategy`）。**写是写了，读端一个都没有**：

- 卡窗那格 `data`（`core/bootstrap.py::_card_chat_context`）原先只拼**目标 / 背景 / 状态 /
  打卡**，八格一个都不读；
- 卡详情页（`ui/panels/card_panel.py`）也不显示这八格（`_refresh_profile_panes` 是出行 /
  餐饮 / 工作卡的画像建议，跟这八格不是一回事）。

于是用户看到的是**"这张卡全空"**。真机实测 #3「对象攻略卡」：`relation_stage='破冰'`、
`interests='短剧'`、`likes='被叫哥、打赌较劲'`、`age='三十往上'`、`strategy='顺着短剧和打赌
的梗接话，别急着认哥'` **都填着**，而 `summary` 是空的、`对方信息.md` 只有 8 字节——卡上看
不到任何一条。**这不是没抽到，是写/读断链。**

这一批补的就是**那一个读端**（选"补卡窗 data"，并顺手把卡详情页也接上——两处**共用**同一个
读端函数，免得再断一次）：

- `life/cards/chat.py::guide_profile_lines(card)` —— 新增的**唯一**读端：八格**非空**的几项
  收成「名字：内容」一列，全空回空列表；
- `bootstrap._card_chat_context`（卡窗 `data`）与 `card_panel._refresh_guide_profile`（卡头
  补一块只读小框）**共用它**。

**抽取那条线一个字没动**（`peer_distill.CARD_FIELDS` / `dao._PROFILE_COLUMNS` / `set_profile`
的写入口径全原样）——这一批只加"读"的那一头。

红线（本模块逐条钉住）：
- **零新依赖**、不新增 `{}` 占位符、不新增开关（`config.yaml` 零改动）；
- **两库零写入**（读端只读；测试也只在沙箱与桩上跑）；
- **既有测试零改动**；其余八类卡那八列本来就空 → **卡窗 `data` 与改之前逐字相同**（这一条
  是本批"不回归"的判据）。

**口径如实记（够不着的地方不装作验过）**：这里验的是"读端真的把库里那八格端出来了、两处
端的是同一批字、其余卡不受影响"，**不是**"模型看清了、攻略卡更好用了"——那只能真机看。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
)

from brain.prompts.templates import CARD_CHAT_PROMPT, CARD_CHAT_SYSTEM  # noqa: E402
from life.cards import chat as chat_mod  # noqa: E402
from life.cards.chat import (  # noqa: E402
    GUIDE_PROFILE_LABELS,
    LIFE_KIND,
    card_namespace,
    guide_profile_lines,
)
from life.mind import peer_distill  # noqa: E402
from life.screen.social import (  # noqa: E402
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)

# 真机实测 #3 卡那八格里填着的几项（当样本用；值照抄真机那条日志/库里的）
PEER_FIELDS = {
    "relation_stage": "破冰",
    "likes": "被叫哥、打赌较劲",
    "interests": "短剧",
    "age": "三十往上",
    "strategy": "顺着短剧和打赌的梗接话，别急着认哥",
}

# 八格全空时，这段一个字都不该出现（其余八类卡与"还没提炼过的攻略卡"都是这个状态）
HEADER = "对方档案"


class _FakeCard:
    """给纯函数用的最小替身：只有那八格属性。"""

    def __init__(self, **kw) -> None:
        for key, _ in GUIDE_PROFILE_LABELS:
            setattr(self, key, kw.get(key))


def main() -> int:
    # ---------- ① 纯函数：非空才出、顺序固定、全空回空 ----------

    section("① guide_profile_lines：八格非空的才出、按固定顺序、全空回空")

    lines = guide_profile_lines(_FakeCard(**PEER_FIELDS))
    check("★ 五格有值 → 出五行", len(lines) == 5, str(lines))
    check("★ 顺序按「最要用上的排前面」：关系阶段 → 喜好 → 兴趣 → …（不是字典序）",
          lines[:3] == ["关系阶段：破冰", "喜好：被叫哥、打赌较劲", "兴趣：短剧"], str(lines))
    check("★ 空的那三格（禁忌 / 情感状态 / 烦心事）一行都不出现",
          all(not ln.startswith(("禁忌：", "情感状态：", "烦心事：")) for ln in lines), str(lines))
    check("★ 八格全空 → 空列表（调用方据此整块收着，不留空框）",
          guide_profile_lines(_FakeCard()) == [], str(guide_profile_lines(_FakeCard())))
    check("★ 值是 `名字：内容` 的形态（卡窗与卡面直接照拼）",
          all("：" in ln for ln in lines), str(lines))
    check("★ 标签表就是那八格、键就是 Card 上的列名",
          tuple(k for k, _ in GUIDE_PROFILE_LABELS)
          == ("relation_stage", "likes", "interests", "dislikes",
              "age", "emotion_state", "anxiety", "strategy"),
          str([k for k, _ in GUIDE_PROFILE_LABELS]))

    # ---------- ② 卡窗 data：autodistill 写进去的八格，卡窗这一格读得到了 ----------

    box, stub, app = make_env()
    try:
        section("② 卡窗 data 读得到八格（写/读断链打通）")

        guide = app.card_service.create_manual_card(
            CARD_CATEGORY, GUIDE_CARD_TITLE, GUIDE_CARD_ICON, GUIDE_CARD_GOAL,
            profile=dict(PEER_FIELDS),
        )
        check("前置：攻略卡建出来了", guide is not None, repr(guide))
        ns = card_namespace(LIFE_KIND, guide.id)

        # 走**真**装配层那一手（`bootstrap._card_chat_context`），不绕开它自己拼
        ctx = app._card_chat_context(ns)
        data = ctx.get("data") or ""
        check("★ 卡窗 data 里有「对方档案」那一段", HEADER in data, data.replace("\n", " / "))
        check("★ 真机那几格照原样端出来（关系阶段 / 喜好 / 兴趣 / 年龄 / 攻略 都在）",
              all(f"{label}：{PEER_FIELDS[key]}" in data
                  for key, label in GUIDE_PROFILE_LABELS if key in PEER_FIELDS),
              data.replace("\n", " / "))
        check("★ 空的格一个都没冒出来（没编、没补空行）",
              "禁忌：" not in data and "情感状态：" not in data and "烦心事：" not in data, "")
        check("★ 原有的几行照旧在（目标 / 状态 / 打卡 —— 这一段是**追加**，不是替换）",
              "状态：" in data and "已打卡" in data, data.replace("\n", " / "))
        check("★ 就是那一个读端在供（卡窗这一段与纯函数逐行一致）",
              all(ln in data.split("\n") for ln in guide_profile_lines(guide)), "")

        # ---------- ③ 生成侧真读得到：卡窗提示词里能看到 relation_stage ----------

        section("③ relation_stage 可被生成侧读取（卡窗提示词里看得到）")

        prompt = CARD_CHAT_PROMPT.format(
            card_type=ctx.get("type") or "生活卡",
            title=ctx.get("title") or "",
            brief=ctx.get("brief") or "",
            data=data,
            guide_hint="",
            kb="",
            skill="",
            history="",
            text="他昨天说周末去爬山",
        )
        check("★ 卡窗提示词里带着「关系阶段：破冰」（阶段调速要读的就是它）",
              "关系阶段：破冰" in prompt, "")
        check("★ 思路那两格（喜好 / 兴趣）也在",
              "喜好：被叫哥、打赌较劲" in prompt and "兴趣：短剧" in prompt, "")
        check("卡窗提示词的骨架没被动（还是那一套：卡上现在的内容 + 回话要求）",
              "卡上现在的内容" in prompt and "就着**这张卡**回话" in prompt, "")

        # ---------- ④ 其余八类卡不受影响：data 逐字与改之前相同 ----------

        section("④ 其余八类卡：那八列本来就空 → data 一行都不加")

        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        other = None
        for card, _pos in app.card_service.plaza():
            if card.title == "少喝奶茶":
                other = card
        check("前置：餐饮卡建出来了", other is not None, "")
        other_data = (app._card_chat_context(card_namespace(LIFE_KIND, other.id)) or {}).get("data") or ""
        check("★ 非攻略卡的 data 里没有「对方档案」这一段", HEADER not in other_data,
              other_data.replace("\n", " / "))
        expected = "\n".join(
            r for r in (
                f"目标：{other.goal}" if other.goal else "",
                f"背景：{other.summary}" if other.summary else "",
                f"状态：{other.status}（active 进行中 / done 已完成 / paused 暂停）",
                f"已打卡 {app.card_service.checkin_count(other.id)} 次",
            ) if r
        )
        check("★ 非攻略卡的 data 与「只拼那四行」的算法**逐字相同**（本批没碰它们的卡窗）",
              other_data == expected, repr(other_data))

        # 攻略卡还没提炼过（八格全空）那一路：也一样不加
        blank = app.card_service.create_manual_card(
            CARD_CATEGORY, "对象攻略卡（空）", GUIDE_CARD_ICON, GUIDE_CARD_GOAL, profile={},
        )
        if blank is not None:
            blank_data = (app._card_chat_context(
                card_namespace(LIFE_KIND, blank.id)) or {}).get("data") or ""
            check("★ 八格全空的攻略卡也不加这一段（不摆一个空壳表头）",
                  HEADER not in blank_data, blank_data.replace("\n", " / "))

        # ---------- ⑤ 卡详情页：卡头那一小块真铺出来了 ----------

        section("⑤ 卡详情页卡头补的那一块：读了同一批字、非攻略卡整块收着")

        panel = app.card_panel
        panel.show_card(guide.id)
        pump(0.2)
        shown = panel._guide_pane.text()
        check("★ 卡头那一块亮着，字就是那批（跟卡窗 data 同源）",
              panel._guide_pane.isVisibleTo(panel)
              and all(ln in shown.split("\n") for ln in guide_profile_lines(guide)),
              shown.replace("\n", " / "))
        check("★ 卡面这一块与卡窗 data 端的是**同一批字**（不是各拼一套）",
              set(guide_profile_lines(guide)).issubset(set(shown.split("\n"))), "")

        panel.show_card(other.id)
        pump(0.2)
        check("★ 换成非攻略卡 → 这一块整块收着、字也清干净了",
              not panel._guide_pane.isVisibleTo(panel)
              and panel._guide_pane.text() == "", repr(panel._guide_pane.text()))

        # ---------- ⑥ 红线：抽取那条线没动 / 零新依赖 / 两库零写入 ----------

        section("⑥ 红线：只加读端（写那条线一字未改）、零新依赖、两库零写入")

        check("★ 抽取那条线的字段表一个字没动（`peer_distill.CARD_FIELDS`）",
              peer_distill.CARD_FIELDS == (
                  "age", "emotion_state", "anxiety", "interests",
                  "likes", "dislikes", "strategy", "relation_stage",
              ), str(peer_distill.CARD_FIELDS))
        check("★ 写入口径一个字没动（`dao._PROFILE_COLUMNS` 还是那八格）",
              __import__("data.dao", fromlist=["x"])._PROFILE_COLUMNS == (
                  "likes", "dislikes", "strategy", "relation_stage",
                  "age", "emotion_state", "anxiety", "interests",
              ), "")
        check("★ 这一批没在抽取提示词里加东西（`PEER_DISTILL_PROMPT` 的字面照旧）",
              "要推断，不要照抄" in peer_distill.PEER_DISTILL_PROMPT, "")

        src = inspect.getsource(chat_mod)
        refs: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(alias.name for alias in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("★ 零新依赖：读端那个模块没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        card_before = app._card_chat_context(ns) or {}

        # 读端反复走几趟（卡窗 + 卡面）——**只读**，两库与卡上八格都不该动
        for _ in range(3):
            app._card_chat_context(ns)
            panel.show_card(guide.id)
            pump(0.05)
        drain_threads()

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
        card_after = app.card_service.card(guide.id)
        check("★ 读端不写卡：那八格读前读后逐字相同",
              all(getattr(card_after, k) == v for k, v in PEER_FIELDS.items())
              and (app._card_chat_context(ns) or {}).get("data") == card_before.get("data"),
              "")
        check("★ 卡窗 SYSTEM 一个字没动（本批只动 data 那一格）",
              "不要复述上面的卡片信息" in CARD_CHAT_SYSTEM, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("对象攻略卡·「对方档案」写/读断链修复（v2-social-card-uiread）")


if __name__ == "__main__":
    raise SystemExit(main())
