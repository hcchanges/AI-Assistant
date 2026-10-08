"""
聊天与对话核心（v2-chat-core）的自动测试。

用户真机反馈里点名要治的七件事，一节一条：

| # | 用户的原话 | 治在哪儿 |
|---|---|---|
| ① | 聊天框里那个「按住说话」和菜单里的语音输入不要了 | `ui/chat_bubble.py`、`ui/pet_window.py`、`ui/tray.py`、`core/bootstrap.py` |
| ② | 文字回复别再自动念出来 | `core/bootstrap.py::_on_pet_speak`（连询问按钮一起去掉） |
| ③ | 「早上好」回得太热情，降下来 | `life/dialog/router.py::is_greeting` + `life/psych/casual.py::GREETING_RULE` |
| ④ | 「今天天气真热，早知道不出门了」被回成"外面还有更累更烦的事" | `life/psych/grounding.py` |
| ⑤ | 说丢钱包，它自造一句"我是不是太没用了" | 同上 |
| ⑥ | 先说回广州、后说到上海，接不上；也不翻出行卡 | `life/psych/service.py::_with_history` + `life/dialog/location.py` |
| ⑦ | 「过两天请你吃饭」被机械追问「大概几点」 | `life/dialog/invite.py` + `life/chatter.py::_apply` |

④ 与 ⑤ 是同一件事的两面（都要求"只根据用户说过的话"），共用 `grounding.hint()`，
两条回话路（闲聊轻路 / 林医生专业框架）注入的是**同一份**提示词——这里专门有一条
断言盯着"一个字不差"，免得将来只补了一条路。

⑥ 的后半（出行卡）与 ⑦ 都要走 `ChatterService` 那条真链：驱动的是真 `handle()`、
真解析链、真卡片服务，只把模型换成桩。所以这几条量的是"真的会发生什么"，不是
"源码里写着什么"。

⑦ 的修复前对照在 `tests/test_...` 之外没法复现（要改源码），所以那条用**状态机**
量：修复前会进 `AWAITING_TIME` 并吐 `ask_time_plain` 那句反问，修复后既不进那个态、
也不吐那句话。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtWidgets import QHBoxLayout

from life.dialog import invite, location, router
from life.dialog.router import INTENT_CHAT, INTENT_VENT, DialogRoute
from life.profile.extract import mentioned_city
from life.psych import grounding
from life.psych.service import _with_history

ROOT = Path(__file__).resolve().parents[1]

# 用户真机反馈里点名的四句
WEATHER_LINE = "今天天气真热，早知道不出门了"     # 图1：别往"还有别的事更烦"上引
INVITE_LINE = "过两天我请你吃饭"                 # 图2：别机械追问「大概几点」
LOSS_LINE = "我钱包丢了"                         # 图3：别自造"我是不是太没用了"
CITY_A, CITY_B = "我先回广州", "我到上海了"       # 回广州 → 到上海


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _sys_text(messages: list[dict[str, str]]) -> str:
    """一次模型调用里所有 system 段拼起来。"""
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


def _last_request(stub) -> list[dict[str, str]]:
    return stub.messages[-1]


def main() -> int:
    box, stub, app = make_env()
    try:
        bubble = app.pet_window._bubble
        chat = app.chat_bubble
        svc = app.psych_service
        # 沙箱默认把语音总闸关着。这一轮要验"开着也不念"，所以故意打开。
        app.config.voice.enabled = True
        say = Recorder(app.voice, "say")

        # ---------- ① 语音输入：用户看得见的入口全摘 ----------

        section("① 语音输入 UI 移除：聊天输入区只剩文字框 + 发送")

        row_layout = next(
            lay for lay in chat.findChildren(QHBoxLayout) if lay.indexOf(chat._send_button) >= 0
        )
        row_widgets = [row_layout.itemAt(i).widget() for i in range(row_layout.count())]
        check("聊天框底部只有「文字框 + 发送」两个控件",
              [type(w).__name__ for w in row_widgets] == ["QLineEdit", "QPushButton"]
              and row_widgets[0] is chat._input and row_widgets[1] is chat._send_button,
              f"{[type(w).__name__ for w in row_widgets]}")

        def _menu_texts(menu):
            return [a.text() for a in menu.actions() if not a.isSeparator()]

        pet_menu = _menu_texts(app.pet_window._build_menu())
        tray_menu = _menu_texts(app.tray.contextMenu())
        check("桌宠右键菜单里没有「语音输入」",
              not any("语音输入" in t for t in pet_menu), str(pet_menu))
        check("托盘菜单里也没有", not any("语音输入" in t for t in tray_menu), str(tray_menu))

        boot_src = _src("core/bootstrap.py")
        check("装配层不再接语音输入那几根线（heard / talk_started / 全局热键）",
              "voice_listener.heard.connect" not in boot_src
              and "talk_started.connect" not in boot_src
              and "voice_hotkey" not in boot_src, "")
        check("引擎本体留着（摘的是入口，不是把 life/voice/ 删了）",
              app.voice_listener is not None and app.voice is not None
              and (ROOT / "life" / "voice" / "tts.py").exists(), "")

        # ---------- ② 回复不出声 ----------

        section("② 回复不回语音：文字照出，一个字都不念")

        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        try:
            say.calls.clear()
            app.chat_bubble.send_requested.emit("在干嘛呀")
            wait_for(lambda: bool(spoken), timeout=8.0)
            drain_threads()
            pump(0.2)
            check("回话照旧落在 pet_speak 上", bool(spoken), str(spoken))
            check("但一次都没念（voice.say 0 次）", say.calls == [], str(say.calls))
        finally:
            app.bus.pet_speak.disconnect(spoken.append)

        say.calls.clear()
        bubble.hide_bubble()
        app.pet_window._speech_until = 0.0
        app.bus.pet_speak_proactive.emit("我自己想说的那句话")
        pump(0.05)
        check("它自己开口那条线也一个字不念", say.calls == [], str(say.calls))
        check("文字照旧冒出来了", bubble.isVisible()
              and bubble._label.text() == "我自己想说的那句话",
              repr(bubble._label.text()))

        check("界面上没有可点的出声入口（询问行 / 轻按钮整块不在）",
              not hasattr(bubble, "_ask_row") and not hasattr(bubble, "answer_ask")
              and not hasattr(chat, "_voice_row") and not hasattr(chat, "_voice_yes"), "")
        check("装配层里 self.voice.say( 一处不剩",
              "self.voice.say(" not in boot_src,
              f"{boot_src.count('self.voice.say(')} 处")

        # ---------- ③ 寒暄降热情 ----------

        section("③ 寒暄降热情：招呼就回招呼，别升温")

        for text, want in (("早上好", True), ("你好呀", True), ("早安～", True),
                           ("你好烦", False), ("晚上好，我今天好累", False),
                           ("在吗", True), ("今天早上好困啊", False)):
            got = router.is_greeting(text)
            check(f"「{text}」是不是纯寒暄 → {want}", got is want, f"got={got}")

        stub.messages.clear()
        svc._compose("早上好", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        greet_sys = _sys_text(_last_request(stub))
        check("真跑一句「早上好」：system 里挂上了「平实地应一声」那条",
              "只是**寒暄**" in greet_sys, "")
        check("并且走的是闲聊轻路（没套四步咨询框架）",
              "【回应准则" not in greet_sys, "")

        stub.messages.clear()
        svc._compose("晚上好，我今天好累", 0.6, DialogRoute(INTENT_VENT, "低落"))
        tired_sys = _sys_text(_last_request(stub))
        check("「晚上好，我今天好累」不算寒暄（不挂那条，也没被招呼接掉）",
              "只是**寒暄**" not in tired_sys, "")
        check("这一句照旧走林医生那张卡",
              "【回应准则" in tired_sys, "")

        # ---------- ④ 锚定用户原话 ----------

        section("④ 意图锚定原话：他说的就是那件事，别自己加戏")

        route = router.classify(WEATHER_LINE)
        check("图1 那句判成「闲聊 / 平静」（不是倾诉，没人替他加一层难受）",
              route.intent == INTENT_CHAT and route.emotion == "平静",
              f"{route.intent} / {route.emotion}")

        stub.messages.clear()
        svc._compose(WEATHER_LINE, 0.0, route)
        weather_sys = _sys_text(_last_request(stub))
        check("system 里点名了「顺着这件事接，别往别的事更烦上引」",
              "顺着这件事接" in weather_sys and "别往" in weather_sys, "")
        check("system 里点名了「不许替他补」",
              "不许替他补" in weather_sys, "")

        # 两条回话路注入的是同一份——一个字不差。只补一条路是这一轮最容易犯的错。
        stub.messages.clear()
        svc._compose("我好累，撑不住了", 0.6, DialogRoute(INTENT_VENT, "低落"))
        vent_sys = _sys_text(_last_request(stub))
        check("两条回话路注入的是同一份「只说他说过的事」（一个字不差）",
              all(rule in weather_sys for rule in grounding.RULES)
              and all(rule in vent_sys for rule in grounding.RULES),
              f"轻路={[r in weather_sys for r in grounding.RULES]} "
              f"专业={[r in vent_sys for r in grounding.RULES]}")

        # ---------- ⑤ 禁幻觉 ----------

        section("⑤ 禁幻觉：不知道的就是不知道，只许问、不许编")

        check("硬约束里有一条专治「编造你们之间没发生过的对话」",
              any("没发生过的对话" in rule for rule in grounding.RULES), "")
        check("还有一条：拿不准的地方写成问句，而且一次只问一件事",
              any("只能写成问句" in rule for rule in grounding.RULES)
              and any("一次只问一件事" in rule for rule in grounding.RULES), "")
        check("最硬的那条点名了反例（编出来的处境明写着不许说）",
              any("不许说" in rule for rule in grounding.RULES)
              and any("不许替他补" in rule for rule in grounding.RULES), "")

        stub.messages.clear()
        svc._compose(LOSS_LINE, 0.0, DialogRoute(INTENT_CHAT, "平静"))
        loss_sys = _sys_text(_last_request(stub))
        check("真跑「我钱包丢了」：整套硬约束都在 system 里",
              all(rule in loss_sys for rule in grounding.RULES), "")
        check("提示词里没有把用户定性成「很累 / 很烦 / 很难受」的现成句子",
              all(preset not in loss_sys for preset in ("他很累", "他很烦", "他很难受")), "")

        # ---------- ⑥ 记忆连续 + 矛盾检测 ----------

        section("⑥ 记忆连续：这一轮带上前几轮的原话")

        messages = [{"role": "system", "content": "s"}, {"role": "user", "content": "这一句"}]
        check("前文为空时一个字节都不动（刚开聊 / 上一轮没生成成功）",
              _with_history(messages, []) == messages, "")

        svc._history.clear()
        stub.messages.clear()
        svc._compose("我上周去了趟苏州", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        first_reply = stub.text
        svc._compose("那边怎么样呀", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        second = _last_request(stub)
        check("第二轮真的把第一轮的原话带上了",
              {"role": "user", "content": "我上周去了趟苏州"} in second,
              f"{len(second)} 条消息")
        check("也把它上一轮的回复带上了（接得住「你刚才说的那个」）",
              {"role": "assistant", "content": first_reply} in second, "")
        check("原话排在最后那句之前（顺序读得通顺）",
              second[-1] == {"role": "user", "content": "那边怎么样呀"}, str(second[-1]))
        # few-shot 范本是固定 12 条（v2-chat-style），前文才受 HISTORY_TURNS 上限约束；
        # 这一轮只该带上第一轮的一问一答，别把整场聊天塞进每一次请求。
        history_count = sum(
            1 for m in second if m.get("content") in ("我上周去了趟苏州", first_reply)
        )
        check("带的前文有条数上限（不会把整场聊天塞进每一次请求）",
              history_count == 2, f"前文 {history_count} 条 / 总 {len(second)} 条")

        section("⑥ 矛盾检测：先说回广州、后说到上海 → 先问清楚 + 翻出行卡")

        tracker = location.LocationTracker()
        check("第一次提地点：只记下，不报冲突",
              tracker.note(CITY_A) is None and tracker.last == "广州", tracker.last)
        conflict = tracker.note(CITY_B)
        check("换了地方 → 报出前后不一致（广州 → 上海）",
              conflict == location.TravelConflict(previous="广州", current="上海"),
              str(conflict))
        check("同一座城连着说两遍不算冲突",
              location.LocationTracker().note(CITY_A) is None
              and location.LocationTracker().note("我还在广州呢") is None, "")

        check("「上海真热」不算把人放在上海（没方位词）",
              mentioned_city("上海真热") == "", mentioned_city("上海真热"))
        check("「我到上海了」算（方位词挨着城市名）",
              mentioned_city(CITY_B) == "上海", mentioned_city(CITY_B))

        stub.messages.clear()
        svc._compose(CITY_B, 0.0, DialogRoute(INTENT_CHAT, "平静"), conflict)
        conflict_sys = _sys_text(_last_request(stub))
        check("提示词里把这对不一致摆出来了，并要求用他自己的话问一句",
              "前后对不上" in conflict_sys and "广州" in conflict_sys
              and "上海" in conflict_sys, "")
        check("没带冲突时就不注入这一段（不无中生有）",
              "前后对不上" not in grounding.hint(), "")

        # 走真链：chatter.handle 两轮，看出行卡有没有被递到眼前。
        chatter = app.chatter_service
        chatter._locator.reset()
        pop = Recorder(chatter, "_pop_card")
        said: list[str] = []
        app.bus.pet_speak.connect(said.append)
        try:
            said.clear()
            chatter.handle(CITY_A)
            wait_for(lambda: bool(said), timeout=8.0)
            drain_threads()
            pump(0.2)
            check("第一句只是记下地点，没急着翻卡", not pop.called, str(pop.calls))

            said.clear()
            chatter.handle(CITY_B)
            wait_for(lambda: pop.called, timeout=10.0)
            drain_threads()
            pump(0.2)
            check("第二句换了地方 → 出行卡真被递到眼前了",
                  pop.called and pop.calls[0][1] == "出行", str(pop.calls))
            check("卡上带着他刚说的那句话",
                  pop.called and pin_text(pop.calls[0]) == CITY_B, str(pop.calls))
        finally:
            pop.restore()
            app.bus.pet_speak.disconnect(said.append)

        pop2 = Recorder(chatter, "_pop_card")
        try:
            chatter._locator.reset()
            chatter.handle(CITY_A)
            wait_for(lambda: True, timeout=0.1)
            drain_threads()
            pump(0.2)
            chatter.handle("我还在广州呢")
            pump(0.5)
            drain_threads()
            check("对照：同城再说一遍不会翻卡（不是「一提城市就翻」）",
                  not pop2.called, str(pop2.calls))
        finally:
            pop2.restore()

        # ---------- ⑦ 去机械触发 ----------

        section("⑦ 去机械触发：邀约不被当成日程登记，不再机械追问「大概几点」")

        check("「过两天我请你吃饭」判成邀约", invite.is_invitation(INVITE_LINE) is True, "")
        check("普通吐槽不会被当成邀约", invite.is_invitation(WEATHER_LINE) is False, "")
        check("「一起去」「请你喝」这类也认",
              invite.is_invitation("周末一起去爬山吧") is True
              and invite.is_invitation("改天请你喝奶茶") is True, "")

        stub.messages.clear()
        svc._compose(INVITE_LINE, 0.0, DialogRoute(INTENT_CHAT, "平静"))
        invite_sys = _sys_text(_last_request(stub))
        check("system 里点名了「这是邀约，不要追问他大概几点」",
              "邀约" in invite_sys and "大概几点" in invite_sys, "")

        # 真跑一轮。关键是把解析结果钉成"模糊时间"——这正是修复前会掉进去的那条路
        # （"过两天"是个模糊时间说法，原先没人拦它，于是反问「大概几点」）。
        chatter2 = app.chatter_service
        said2: list[str] = []
        app.bus.pet_speak.connect(said2.append)
        try:
            stub.parse_result = {
                "scene": INVITE_LINE[:12],
                "time_hint": "过两天",
                "time_iso": "",
                "time_confidence": "fuzzy",
                "emotion": "平静",
                "module": "其他",
                "command": "",
                "reply": "好呀",
            }
            said2.clear()
            chatter2.handle(INVITE_LINE)
            wait_for(lambda: bool(said2), timeout=8.0)
            drain_threads()
            pump(0.3)
            check("真跑一轮（模糊时间）：没进「等时间」那个态",
                  chatter2.state.value != "awaiting_time", chatter2.state.value)
            check("也没吐那句「几点」的反问",
                  all("几点" not in line for line in said2), str(said2))
            check("这一句就只是聊天：没建提醒",
                  not any("提醒" in line for line in said2), str(said2))
        finally:
            app.bus.pet_speak.disconnect(said2.append)

        chatter_src = _src("life/chatter.py")
        check("源码那条分支上确实挂了邀约判据（不是碰巧没走到）",
              "or invite.is_invitation(text)" in chatter_src, "")

        # ---------- 回归：四条既有红线不许松 ----------

        section("回归：红线照旧")

        # 1) 危机词最高优先：同一句从打字那条入口进去，模型一次都不调。
        svc._history.clear()
        stub.messages.clear()
        crisis_seen: list[str] = []
        app.bus.pet_speak.connect(crisis_seen.append)
        try:
            app.chatter_service.handle("我不想活了")
            drain_threads()
            pump(0.3)
            check("危机句 → 模型一次都没调", stub.messages == [], f"{len(stub.messages)} 次")
            check("走的是危机话术（含热线 12356）",
                  any("12356" in line for line in crisis_seen), str(crisis_seen))
        finally:
            app.bus.pet_speak.disconnect(crisis_seen.append)

        # 2) 零新依赖：这一轮新增的三个模块一个第三方包都没引。
        owner_pkgs = {"brain", "core", "life", "memory", "monitor", "pet", "ui"}
        for name in ("life/psych/grounding.py", "life/dialog/invite.py",
                     "life/dialog/location.py"):
            tree = ast.parse(_src(name), filename=name)
            imported: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imported.update(alias.name.split(".")[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    imported.add(node.module.split(".")[0])
            third_party = sorted(
                mod for mod in imported
                if mod not in sys.stdlib_module_names and mod not in owner_pkgs
                and mod != "__future__"
            )
            check(f"{name} 一个第三方包都没引（零新依赖）",
                  third_party == [], str(third_party) or "（只用标准库 + 仓内模块）")

        # 3) 模型名一个字没动。
        check("这一轮没碰任何模型名（deepseek 在新增模块里一次都没出现）",
              all("deepseek" not in _src(name).lower()
                  for name in ("life/psych/grounding.py", "life/dialog/invite.py",
                               "life/dialog/location.py")), "")

        # 4) pet.db 零写入：只是聊天（不说话、不点卡片）不该新增任何一行。
        before = table_rows(app.config.db_path, "utterance")
        stub.messages.clear()
        svc._compose("今天就这样吧", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        drain_threads()
        check("直接调 _compose 不落库（回话这一步自己不写 pet.db）",
              table_rows(app.config.db_path, "utterance") == before,
              f"{before} → {table_rows(app.config.db_path, 'utterance')}")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("聊天与对话核心（v2-chat-core）")


def pin_text(call: tuple) -> str:
    """`_pop_card(card_id, title, text)` 的第三个参数。"""
    return call[2] if len(call) > 2 else ""


def table_rows(db_path, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
