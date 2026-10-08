"""
卡内工具生成（v2-card-tools-B）的自动测试。

对着指令的 S4 三条，加上几条必须一起钉住的边界：

1. **卡窗里说一句"加个喝水提醒"，卡上真长出一个提醒部件** —— 部件绑的是这张卡的卡号、
   真摆在卡面那一列上、卡窗里回一句交代，而且这**整条路都没碰 pet.db**；
2. **重启还在** —— 同一个沙箱再装配一只（新建服务、重新读盘），部件和它的勾选状态都在；
3. **非法 / 代码类请求被拒并提示** —— 带 python / os.system 的那种**根本不问模型**，
   直接说做不到；模型真给了一个白名单外的类型（`shell`），也**摆不上卡**。

另外三件不能省的：

- **沙箱是白名单不是提示词**：`normalize_components` 单测那些"模型不听话"的形状
  （未知类型 / 嵌套对象 / 带代码的值 / 多余的条数）；
- **点一下真的有用**：按钮把话送进这张卡的小窗、快捷指令只填不发、提醒勾选落盘、
  × 把部件拿掉——四个动作各验一遍**真实取值**；
- **A 阶段的隔离没被这条路撬开**：生成部件的提示词里没有 `memories`，也进不了 pet.db。
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from harness import build_app, check, drain_threads, make_env, pump, section, summary, wait_for


CARD_LINE = "加个喝水提醒"
CODE_LINE = "帮我加个 python 脚本调用 os.system 的按钮"
SAY_LINE = "好，给你在卡上摆了个喝水提醒"
REMINDER = {"type": "reminder", "props": {"label": "喝水", "at": "15:00"}}
BUTTON = {"type": "button", "props": {"label": "问它今晚吃什么"}}
SHORTCUT = {"type": "shortcut", "props": {"label": "记一笔", "text": "今天喝水八杯"}}
LIST = {"type": "list", "props": {"label": "今天要做的", "items": ["买菜", "取快递", "浇花"]}}


# ---------- 小工具 ----------


def _mem_counts(box) -> dict[str, int]:
    """pet.db 里那两张"记忆"的表的行数（卡上长东西也不许往这儿写）。"""
    conn = sqlite3.connect(box.dir / "pet.db")
    try:
        return {
            table: int(conn.execute(f"select count(*) from {table}").fetchone()[0])
            for table in ("utterance", "memory")
        }
    finally:
        conn.close()


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else ""


def _card(service, title: str):
    for card, _pos in service.plaza():
        if card.title == title:
            return card
    return None


def _texts(panel) -> list[str]:
    return [panel._history.item(i).text() for i in range(panel._history.count())]


def _send(panel, text: str) -> bool:
    """在卡窗里真打一句话、真点一下「发送」（不绕过按钮那条路）。"""
    panel._input.setText(text)
    if not panel._send_button.isEnabled():
        return False
    QTest.mouseClick(panel._send_button, Qt.LeftButton)
    return True


def _settle(app, ns: str) -> None:
    """等这张卡把话说完（后台线程 + 主线程回调都跑完）。"""
    wait_for(lambda: not app.card_chat_service.busy_with(ns), 5)
    drain_threads()
    pump(0.2)


def _row(strip, index: int):
    return strip.layout().itemAt(index).widget()


def _in_row(row, index: int):
    return row.layout().itemAt(index).widget()


def _remove_button(row):
    """那一行右上角那个 ×。

    按名字找、**不按序号**：行里依次是 控件 / 弹簧 / ×（那个弹簧是 `addStretch(1)`），
    按下标数很容易数到弹簧上——数到弹簧就是 `QTest.mouseClick(None)`，整个进程直接崩
    （探针里真崩过：rc=127，连缓冲的输出都没留下）。
    """
    for child in row.findChildren(QPushButton):
        if child.objectName() == "ToolRemove":
            return child
    return None


def _tool_prompts(stub) -> list[str]:
    """走"生成部件"那条路的提示词（模板里独有的那句"小部件"）。"""
    return [p for p in stub.prompts if "小部件" in p]


# ---------- S4-① 卡上真长出一个部件 ----------


def test_tool_grows_on_life_card() -> None:
    section("S4-① 卡窗里说「加个喝水提醒」，卡上真长出一个提醒部件")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        strip = app.card_panel.card_tools
        ns = f"life:{trip.id}"

        check("部件区和聊天窗绑的是同一张卡的卡号",
              strip.namespace == ns == panel.namespace,
              f"部件区={strip.namespace!r} 聊天窗={panel.namespace!r}")
        check("还没有部件时整块收起来（老卡的卡面一个字不变）",
              strip.isHidden() and strip.components() == [],
              f"收起了吗={strip.isHidden()} 部件数={len(strip.components())}")

        stub.tools_result = {"components": [REMINDER], "say": SAY_LINE}
        mem_before = _mem_counts(box)
        mind_before = _digest(box.dir / "mind.db")
        stub.prompts.clear()
        clicked = _send(panel, CARD_LINE)
        _settle(app, ns)

        comps = strip.components()
        check("说一句之后卡上真长出了一个部件", len(comps) == 1, f"部件={comps!r}")
        check("长出来的正是「提醒」这个预置类型",
              comps and comps[0]["type"] == "reminder" and comps[0]["label"] == "喝水",
              f"type={comps[0]['type'] if comps else None!r}")
        check("时间是模型给的那个（15:00），没被改写成别的",
              comps and comps[0]["props"].get("at") == "15:00",
              f"props={comps[0]['props'] if comps else None!r}")
        check("它真摆在卡面那一列上（在卡面那块板上，见 v2-card-tools-C）",
              app.card_panel.board.row("widgets") is not None
              and app.card_panel.board.row("widgets").block is strip,
              f"板上的行={app.card_panel.board.row('widgets')}")
        check("卡面上真摆了那一行控件（不是只在内存里）",
              strip.layout().count() == 1 and not strip.isHidden(),
              f"行数={strip.layout().count()} 收起了吗={strip.isHidden()}")

        check("卡窗里回了一句交代",
              clicked and _texts(panel) == [CARD_LINE, SAY_LINE],
              f"{_texts(panel)!r}")

        prompts = _tool_prompts(stub)
        check("走的是「回 JSON 部件」那条路（不是普通聊天那条）",
              len(prompts) == 1, f"带「小部件」的提示词={len(prompts)} 条")
        check("提示词里带着这张卡的名字与组件表",
              prompts and "成都行" in prompts[0] and "reminder" in prompts[0])
        check("提示词里没有 memories 那一格（A 阶段的隔离没被这条路撬开）",
              prompts and "memories" not in prompts[0])
        check("也没有把全局记忆里的东西带进来",
              prompts and "阿黄" not in prompts[0])
        check("pet.db 的 utterance / memory 一行没涨",
              _mem_counts(box) == mem_before,
              f"{mem_before} -> {_mem_counts(box)}")
        check("mind.db 一个字节没动", _digest(box.dir / "mind.db") == mind_before)

        record = Path(app.card_chat_service._tools.path(ns))
        check("部件落在沙箱目录下（没碰用户真实的 data/card_tools/）",
              record.is_relative_to(box.dir) and record.name == f"life_{trip.id}.json",
              f"path={record}")
        check("文件里写的就是卡上那个部件（含它点一下算哪种动作，v2-card-tools-D）",
              record.exists() and json.loads(record.read_text(encoding="utf-8"))["components"]
              == [{"id": "reminder:喝水", "type": "reminder", "label": "喝水",
                   "props": {"label": "喝水", "at": "15:00"}, "action": "record"}],
              f"文件={record.read_text(encoding='utf-8') if record.exists() else '（没有）'}")
        check("用户真实的 data/card_tools 没被创建出来",
              not (Path(__file__).resolve().parents[1] / "data" / "card_tools").exists())
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4-② 重启还在 ----------


def test_tool_survives_restart() -> None:
    section("S4-② 关掉桌宠再打开，卡上那个部件还在（勾选状态也在）")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        card = _card(app.card_service, "少喝奶茶")
        app.card_panel.show_card(card.id)
        panel = app.card_panel.card_chat
        strip = app.card_panel.card_tools
        ns = f"life:{card.id}"

        stub.tools_result = {"components": [REMINDER, LIST], "say": SAY_LINE}
        _send(panel, CARD_LINE)
        _settle(app, ns)
        check("先确认这一轮真长出了两个部件", len(strip.components()) == 2,
              f"部件={[c['id'] for c in strip.components()]!r}")

        # 勾一个提醒、再勾清单里的第一条——这些状态得跟着卡一起活过重启
        strip._checks["reminder:喝水"].setChecked(True)
        pump(0.1)
        strip._lists["list:今天要做的"][1].setChecked(True)
        pump(0.1)
        record = Path(app.card_chat_service._tools.path(ns))
        saved = json.loads(record.read_text(encoding="utf-8"))
        check("勾选状态当场就写进那张卡的文件里了",
              saved["state"] == {"reminder:喝水": {"done": True},
                                 "list:今天要做的": {"checked": [1]}},
              f"state={saved['state']!r}")

        # 关掉这一只，另开一只（新建服务、重新读盘）= 重启
        app.shutdown()
        drain_threads()
        app2 = build_app()
        try:
            app2.card_panel.show_card(card.id)
            strip2 = app2.card_panel.card_tools
            check("重开之后卡上那两个部件还在（重新读盘读回来的）",
                  [c["id"] for c in strip2.components()]
                  == ["reminder:喝水", "list:今天要做的"],
                  f"重开后={[c['id'] for c in strip2.components()]!r}")
            check("重开之后卡面上也真摆出来了（不是只有内存里）",
                  strip2.layout().count() == 2 and not strip2.isHidden(),
                  f"行数={strip2.layout().count()} 收起了吗={strip2.isHidden()}")
            state2 = app2.card_chat_service.tools_state(ns)
            check("重开之后提醒上那个勾还是勾着的",
                  state2.get("reminder:喝水") == {"done": True}, f"state={state2!r}")
            check("重开之后清单里勾的那一条也还在",
                  state2.get("list:今天要做的") == {"checked": [1]}, f"state={state2!r}")
            check("重开之后卡面上的勾也是勾着的（真铺回去了）",
                  strip2._checks["reminder:喝水"].isChecked()
                  and strip2._lists["list:今天要做的"][1].isChecked())
        finally:
            app2.shutdown()
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4-③ 非法 / 代码类请求被拒 ----------


def test_code_request_refused() -> None:
    section("S4-③ 代码 / 命令类的请求：不问模型，直接说做不到")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        strip = app.card_panel.card_tools
        ns = f"life:{trip.id}"

        stub.tools_result = {"components": [REMINDER], "say": SAY_LINE}
        stub.prompts.clear()
        clicked = _send(panel, CODE_LINE)
        _settle(app, ns)

        check("这句话被收下了（没漏到全局那条路去）", clicked)
        # 卡面自己那段"建议"是异步的，可能在 clear() 之后才落地，所以不能数"总共几条"，
        # 要数的是**这条路上独有的那种提示词**（模板里那句"小部件"）——它一条都不该有
        check("它压根没去问模型要部件（那条路的提示词一条没发）",
              _tool_prompts(stub) == [], f"提示词={len(_tool_prompts(stub))} 条")
        check("这句话本身也没被哪条链发给模型",
              all("os.system" not in p for p in stub.prompts),
              f"提示词={[p[:40] for p in stub.prompts]!r}")
        check("卡上因此也没有多出任何部件",
              strip.components() == [], f"部件={strip.components()!r}")
        check("卡窗里如实说了做不到（不是装死、也不是编一个）",
              len(_texts(panel)) == 2 and "做不到" in _texts(panel)[-1],
              f"{_texts(panel)!r}")
        check("沙箱里没为这句话生成文件",
              not Path(app.card_chat_service._tools.path(ns)).exists())

        # 模型不听话：给了一个白名单外的类型，字段还填得齐齐的（`prompt` 是自动化那一格）
        # ——要是没有白名单，它就会被当成一个"自动化"部件摆上卡
        stub.tools_result = {
            "components": [{"type": "shell", "props": {"label": "清盘", "prompt": "把这周的排期理一遍"}}],
            "say": "这个我做不了",
        }
        _send(panel, "加个提醒按钮")
        _settle(app, ns)
        check("白名单外的部件类型被丢掉了（卡上还是空的）",
              strip.components() == [], f"部件={strip.components()!r}")
        check("模型那句「做不了」照常回到卡窗里",
              _texts(panel)[-1] == "这个我做不了", f"{_texts(panel)!r}")
    finally:
        box.cleanup()
        drain_threads()


def test_whitelist_is_not_the_prompt() -> None:
    section("S4-③ 沙箱的落点是白名单函数，不是提示词里那句「别生成代码」")
    from life.cards.tools import normalize_components

    cases = [
        # 这条得**字段齐全**才有用：光给个 {"label": ...} 的话，不认识的类型会因为
        # "没填必填字段"被丢掉，看着也像 0——那样就验不出白名单这一步在不在
        # （变异验证里第一次就是这么漏过去的）。给足字段，还能剩下 0 个，才是白名单拦的。
        ("不认识的类型（字段给全了也不认）",
         [{"type": "shell", "props": {"label": "清盘", "prompt": "帮我把这周的排期理一遍"}}], 0),
        ("不认识的类型，字段里还带着命令",
         [{"type": "shell", "props": {"label": "清盘", "prompt": "del /f /q C:"}}], 0),
        ("缺必填字段（提醒没有时间）",
         [{"type": "reminder", "props": {"label": "喝水"}}], 0),
        ("时间写成一句人话",
         [{"type": "reminder", "props": {"label": "喝水", "at": "下午三点"}}], 0),
        ("时间越界（25:99）",
         [{"type": "reminder", "props": {"label": "喝水", "at": "25:99"}}], 0),
        ("倒计时给的是负数",
         [{"type": "timer", "props": {"label": "喝水", "minutes": -5}}], 0),
        ("props 里塞了个嵌套对象",
         [{"type": "button", "props": {"label": "点我", "run": {"cmd": "x"}}}], 1),
        ("值里带代码味",
         [{"type": "button", "props": {"label": "运行 import os"}}], 0),
        ("值里带网址",
         [{"type": "button", "props": {"label": "看 https://example.com"}}], 0),
        ("值里带换行（想撑破界面）",
         [{"type": "button", "props": {"label": "第一行\n第二行"}}], 1),
        ("一次给五个（上限三个）",
         [{"type": "button", "props": {"label": f"第{i}个"}} for i in range(5)], 3),
        ("同一句给两遍（不重复摆）", [BUTTON, dict(BUTTON)], 1),
        ("整个 components 是段字符串",
         "components: [button]", 0),
        ("根本就是个 None", None, 0),
    ]
    for title, raw, expected in cases:
        got = normalize_components(raw)
        check(f"{title} → 收下 {expected} 个", len(got) == expected,
              f"实收 {len(got)} 个：{[c.id for c in got]!r}")
    check("收下的东西只有卡上认识的类型",
          all(c.type in ("button", "shortcut", "reminder", "timer", "list", "automation")
              for c in normalize_components([BUTTON, SHORTCUT, REMINDER, LIST])),
          f"{[c.type for c in normalize_components([BUTTON, SHORTCUT, REMINDER, LIST])]!r}")
    # 嵌套对象不是"整个部件丢掉"，而是**那个字段根本进不了 props**——它最后只变成
    # 卡上的一个按钮，多出来的 run 一个字都留不下（这才叫"只收白名单里的字段"）
    nested = normalize_components([{"type": "button", "props": {"label": "点我", "run": {"cmd": "x"}}}])
    check("嵌套对象进不了 props（部件本身还在，那个字段被丢干净）",
          len(nested) == 1 and nested[0].props == {"label": "点我"},
          f"props={nested[0].props if nested else None!r}")
    # 两个上限不是一回事（这条是探针里逮到的真 bug：第 4 个部件一存一读就没了）
    many = [{"type": "button", "props": {"label": f"第{i}个"}} for i in range(5)]
    check("读回卡上已有的部件时按「一张卡最多 6 个」收，不是按「一次 3 个」",
          len(normalize_components(many, 6)) == 5,
          f"收下 {len(normalize_components(many, 6))} 个")


# ---------- 点一下真有用 ----------


def test_clicks_do_what_they_say() -> None:
    section("卡上那四个动作各点一遍：说得出、做得到")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        app.card_panel.show_card(trip.id)
        panel = app.card_panel.card_chat
        strip = app.card_panel.card_tools
        ns = f"life:{trip.id}"
        # 一次最多加三个（`MAX_PER_REPLY`），所以分两轮要齐这四个部件——
        # 顺带也验了"往已有的卡上接着添"这条路
        stub.tools_result = {"components": [BUTTON, SHORTCUT, REMINDER], "say": SAY_LINE}
        _send(panel, CARD_LINE)
        _settle(app, ns)
        stub.tools_result = {"components": [LIST], "say": "清单也摆上了"}
        _send(panel, "再加个清单")
        _settle(app, ns)
        check("四个部件都摆上了（两轮要来的）", strip.layout().count() == 4,
              f"行数={strip.layout().count()} 部件={[c['id'] for c in strip.components()]!r}")
        # 再开一次这张卡（走的是"从文件读回来"那条路）：四个一个都不能少
        # ——探针在这里逮到过"第 4 个一存一读就被吃掉"的 bug
        app.card_panel.show_card(trip.id)
        pump(0.1)
        check("再开一次这张卡，四个还都在（读回来时没被截掉）",
              [c["id"] for c in strip.components()] == ["button:问它今晚吃什么",
                                                        "shortcut:记一笔",
                                                        "reminder:喝水",
                                                        "list:今天要做的"],
              f"部件={[c['id'] for c in strip.components()]!r}")

        # ① 按钮：点一下 = 这句话当成你说的，送进这张卡的小窗（真调模型、真有回话）
        before = len(_texts(panel))
        QTest.mouseClick(_in_row(_row(strip, 0), 0), Qt.LeftButton)
        _settle(app, ns)
        check("按钮点一下，那句话真进了这张卡的小窗",
              _texts(panel)[before:] == ["问它今晚吃什么", stub.text],
              f"新添的={_texts(panel)[before:]!r}")

        # ② 快捷指令：只填进输入框，不发送
        before = len(_texts(panel))
        QTest.mouseClick(_in_row(_row(strip, 1), 0), Qt.LeftButton)
        pump(0.2)
        check("快捷指令点一下，那句话填进了输入框",
              panel._input.text() == "今天喝水八杯", f"输入框={panel._input.text()!r}")
        check("但它不替你按发送（记录区一条没多）",
              len(_texts(panel)) == before, f"{_texts(panel)[before:]!r}")
        panel._input.clear()

        # ③ 提醒勾一下：落进这张卡的文件，且不去麻烦模型
        prompts_before = len(stub.prompts)
        strip._checks["reminder:喝水"].setChecked(True)
        pump(0.2)
        record = Path(app.card_chat_service._tools.path(ns))
        check("提醒勾一下，状态就落进这张卡的文件里了",
              json.loads(record.read_text(encoding="utf-8"))["state"].get("reminder:喝水")
              == {"done": True},
              f"文件 state={json.loads(record.read_text(encoding='utf-8'))['state']!r}")
        check("勾一下没去调模型", len(stub.prompts) == prompts_before,
              f"{prompts_before} -> {len(stub.prompts)}")

        # ④ 列表勾一条：整条清单的勾选一起记（不是只剩最后一下）
        boxes = strip._lists["list:今天要做的"]
        boxes[0].setChecked(True)
        boxes[2].setChecked(True)
        pump(0.2)
        saved = json.loads(record.read_text(encoding="utf-8"))["state"]["list:今天要做的"]
        check("清单勾两条，两条都记下来了",
              saved == {"checked": [0, 2]}, f"state={saved!r}")
        check("清单也没去调模型", len(stub.prompts) == prompts_before)

        # ⑤ × 拿掉一个：卡上真少一个，文件里也少一个
        QTest.mouseClick(_remove_button(_row(strip, 2)), Qt.LeftButton)
        pump(0.3)
        check("点 × 之后卡上真少了一个部件",
              [c["id"] for c in strip.components()] == ["button:问它今晚吃什么",
                                                        "shortcut:记一笔",
                                                        "list:今天要做的"],
              f"部件={[c['id'] for c in strip.components()]!r}")
        saved = json.loads(record.read_text(encoding="utf-8"))
        check("文件里也跟着少了，连它的状态一起",
              "reminder:喝水" not in [c["id"] for c in saved["components"]]
              and "reminder:喝水" not in saved["state"],
              f"文件={saved!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- 主入口 ----------


def main() -> int:
    test_tool_grows_on_life_card()
    test_tool_survives_restart()
    test_code_request_refused()
    test_whitelist_is_not_the_prompt()
    test_clicks_do_what_they_say()
    return summary()


if __name__ == "__main__":
    sys.exit(main())
