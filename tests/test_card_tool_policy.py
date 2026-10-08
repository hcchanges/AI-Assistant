"""
卡内工具生成的红线与分角色（v2-card-tools-D）的自动测试。

对着指令的四条：

1. **工作卡上不许长出会改工作流的东西** —— 动作 `workFlowEdit` 一例都不收：提示词里
   点名说不接受，后端认出来就丢（藏在 props 深处、换个 `work_flow_edit` 写法也一样认）；
2. **前后端双重校验** —— 类型与动作两份白名单，提示词那道与代码这道**取的是同一份
   数据**（`ToolPolicy`），所以不存在"提示词说不许、代码放过去"这种缝；
3. **回归：生活卡可自由生成** —— 六种都能摆，管理员 / 员工身份都不影响。

（原先还有两条端到端用例，验的是员工身份在**工作卡**上长不出什么——工作卡没随
公开版发布，公开树里长不出工作卡，那两条一并摘了。）

外加三条自己钉住的边界：

- **红线连管理员都拦**（不是"身份高就能改流程"）；
- **政策只管生成，不清算**：以前摆上的部件不因为换了身份就消失（读回不看政策）；
- **真取值得对得上**：卡窗里回的那句话是政策那句，不是模型那句"加好了"。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from core.config import WORK_ROLE_ADMIN, WORK_ROLE_USER
from life.cards import tools as tool_mod

CARD_LINE = "加个喝水提醒"
SAY_LINE = "好，给你在卡上摆了个喝水提醒"
REMINDER = {"type": "reminder", "props": {"label": "喝水", "at": "15:00"}}
BUTTON = {"type": "button", "props": {"label": "问它今晚吃什么"}}
#: 模型硬要一个"改流程"的部件——两种写法都试（规范写法 + 藏进 props 的换写法）
EDIT_PLAIN = {"type": "button", "props": {"label": "改流程"}}
EDIT_HIDDEN = {"type": "button", "props": {"label": "推一下", "action": "work_flow_edit"}}
#: 模型说要改工作流的那种"直接动作"部件（后端压根不认识这个类型）
EDIT_ACTIONED = {"type": "button", "props": {"label": "推一下"}, "action": "workFlowEdit"}


# ---------- 小工具 ----------


def _send(panel, text: str) -> None:
    panel._input.setText(text)
    QTest.mouseClick(panel._send_button, Qt.LeftButton)


def _settle(app, ns: str) -> None:
    wait_for(lambda: not app.card_chat_service.busy_with(ns), 5)
    drain_threads()
    pump(0.2)


def _texts(panel) -> list[str]:
    return [panel._history.item(i).text() for i in range(panel._history.count())]


def _tool_prompts(stub) -> list[str]:
    """走"生成部件"那条路的提示词（模板里独有的那句"小部件"）。"""
    return [p for p in stub.prompts if "小部件" in p]


def _life_card(app, title: str = "少喝奶茶"):
    app.card_service.create_manual("餐饮", title, "🥤", "一周两杯")
    for card, _pos in app.card_service.plaza():
        if card.title == title:
            return card
    raise AssertionError(f"没找到生活卡 {title}")


# ---------- ① 政策：类型 + 动作两份白名单 ----------


def test_policy_shape() -> None:
    section("D-① 政策本身：生活卡不受限、工作卡分身份、workFlowEdit 谁都没有")
    life = tool_mod.policy_for(tool_mod.KIND_LIFE)
    work_admin = tool_mod.policy_for(tool_mod.KIND_WORK, True)
    work_staff = tool_mod.policy_for(tool_mod.KIND_WORK, False)

    check("生活卡六种都能摆", len(life.types) == 6, f"{sorted(life.types)}")
    check("生活卡两种动作都行", sorted(life.actions) == ["chat", "record"],
          f"{sorted(life.actions)}")
    check("管理员在工作卡上不受限（跟生活卡一样宽）",
          work_admin.types == life.types and work_admin.actions == life.actions,
          f"{sorted(work_admin.types)} / {sorted(work_admin.actions)}")
    check("员工在工作卡上只剩记事的三种",
          sorted(work_staff.types) == ["list", "reminder", "timer"],
          f"{sorted(work_staff.types)}")
    check("员工的动作白名单里只有 record",
          sorted(work_staff.actions) == ["record"], f"{sorted(work_staff.actions)}")
    check("员工那三种全是 record 动作（表与动作对得上）",
          all(tool_mod.default_action(name) == tool_mod.ACTION_RECORD
              for name in work_staff.types))
    check("**红线**：workFlowEdit 不在任何一档的允许动作里",
          not any(tool_mod.ACTION_WORKFLOW_EDIT in policy.actions
                  for policy in (life, work_admin, work_staff)))
    check("**红线**：ALL_ACTIONS（允许存在的动作全集）里也没有它",
          tool_mod.ACTION_WORKFLOW_EDIT not in tool_mod.ALL_ACTIONS,
          f"{sorted(tool_mod.ALL_ACTIONS)}")
    check("员工身份的工作卡：六种类型里那三种 chat 的一个都不许",
          all(not work_staff.allows_type(name)
              for name, item in tool_mod.CATALOG.items()
              if item["action"] == tool_mod.ACTION_CHAT),
          f"{sorted(work_staff.types)}")


def test_prompt_uses_the_same_policy() -> None:
    section("D-② 前端那道：提示词里的白名单跟代码那道是同一份数据")
    work_staff = tool_mod.policy_for(tool_mod.KIND_WORK, False)
    catalog = tool_mod.catalog_text(work_staff.types)
    policy = tool_mod.policy_text(work_staff)

    check("员工的组件表里真的没有 button / shortcut / automation",
          all(name not in catalog for name in ("button", "shortcut", "automation")),
          f"表={catalog!r}")
    check("员工的组件表里三种记事的都在",
          all(name in catalog for name in ("reminder", "timer", "list")))
    check("表里列出来的类型与政策允许的类型一一对上",
          {line.split("（")[0].removeprefix("- ") for line in catalog.splitlines()}
          == set(work_staff.types))
    check("工作卡的提示词里点名了 workFlowEdit 不接受",
          tool_mod.ACTION_WORKFLOW_EDIT in policy
          and ("不许" in policy or "不接受" in policy),
          f"{policy[:40]}…")
    check("工作卡那一句是「绝对不许」，不是「尽量别」",
          "绝对不许" in policy, f"{policy}")
    check("员工的提示词里写明了身份受限与只剩哪几种",
          "员工" in policy and "提醒" in policy and "record" in policy, f"{policy}")
    check("提示词里那张表是照政策收窄的（不是把六种都摆出来再说不许）",
          catalog.count("- ") == len(work_staff.types), f"{catalog.count('- ')} 行")
    check("生活卡那一档说的是不受身份限制",
          "不受身份限制" in tool_mod.policy_text(tool_mod.policy_for(tool_mod.KIND_LIFE)))


# ---------- ② 后端那道闸：类型 + 动作 ----------


def test_backend_gate_on_actions() -> None:
    section("D-③ 后端那道：动作与类型对不上就丢，workFlowEdit 一律丢")
    life = tool_mod.policy_for(tool_mod.KIND_LIFE)
    work_admin = tool_mod.policy_for(tool_mod.KIND_WORK, True)
    work_staff = tool_mod.policy_for(tool_mod.KIND_WORK, False)

    check("红线：直接写 workFlowEdit 动作的部件，一个都不收（管理员也不收）",
          tool_mod.normalize_components([EDIT_ACTIONED], policy=work_admin) == [],
          f"{tool_mod.normalize_components([EDIT_ACTIONED], policy=work_admin)}")
    check("红线：连生活卡也不收（红线不分卡）",
          tool_mod.normalize_components([EDIT_ACTIONED], policy=life) == [])
    check("红线：藏在 props 里、换个下划线写法一样认出来",
          tool_mod.normalize_components([EDIT_HIDDEN], policy=work_admin) == [])
    check("红线：连「不按卡收紧」那条读回路径也拦得住（政策不给也拦）",
          tool_mod.normalize_components([EDIT_HIDDEN]) == [])
    check("动作跟类型对不上就丢（reminder 硬说自己是 chat）",
          tool_mod.normalize_components(
              [{"type": "reminder", "props": {"label": "x", "at": "09:00"}, "action": "chat"}]
          ) == [])
    check("动作跟类型对得上就照收（button 说自己 chat）",
          [c.id for c in tool_mod.normalize_components(
              [{"type": "button", "props": {"label": "问一句"}, "action": "chat"}])]
          == ["button:问一句"])
    check("同一串里：能收的收、红线那个丢（不是一刀切全丢）",
          [c.id for c in tool_mod.normalize_components(
              [REMINDER, EDIT_ACTIONED, BUTTON], policy=work_admin)]
          == ["reminder:喝水", "button:问它今晚吃什么"],
          f"{[c.id for c in tool_mod.normalize_components([REMINDER, EDIT_ACTIONED, BUTTON], policy=work_admin)]}")
    check("员工的工作卡上：按钮丢、提醒收",
          [c.id for c in tool_mod.normalize_components([BUTTON, REMINDER], policy=work_staff)]
          == ["reminder:喝水"])
    check("管理员的工作卡上：两个都收",
          [c.id for c in tool_mod.normalize_components([BUTTON, REMINDER], policy=work_admin)]
          == ["button:问它今晚吃什么", "reminder:喝水"])
    check("认出来的是「身份挡的」（不是「模型瞎编的」）",
          tool_mod.blocked_reason([BUTTON, REMINDER], work_staff) == "policy",
          f"{tool_mod.blocked_reason([BUTTON, REMINDER], work_staff)!r}")
    check("红线挡的那一种单独认出来（说的那句话不一样）",
          tool_mod.blocked_reason([EDIT_ACTIONED], work_admin) == "edit",
          f"{tool_mod.blocked_reason([EDIT_ACTIONED], work_admin)!r}")
    check("既踩红线又不合身份的，按红线说（红线更硬）",
          tool_mod.blocked_reason([EDIT_ACTIONED], work_staff) == "edit")
    check("瞎编的类型不算「闸挡的」（该说「没做出来」，不是「身份不行」）",
          tool_mod.blocked_reason([{"type": "shell", "props": {"label": "x"}}], work_staff) == "")
    check("存下来的部件带着动作（事后查得到它算什么）",
          tool_mod.normalize_components([REMINDER])[0].to_json()["action"] == "record")
    check("**政策只管生成，不清算**：读回盘上的旧部件不看身份",
          len(tool_mod.normalize_components([BUTTON, REMINDER], policy=None)) == 2)


def test_life_card_stays_free() -> None:
    section("D-⑤ 回归：生活卡可自由生成（身份不影响）")
    box, stub, app = make_env()
    try:
        app.work_service.set_role(WORK_ROLE_USER)      # 故意用员工身份
        card = _life_card(app)
        app.card_panel.show_card(card.id)
        panel = app.card_panel.card_chat
        strip = app.card_panel.card_tools
        ns = f"life:{card.id}"

        stub.tools_result = {"components": [BUTTON, REMINDER], "say": SAY_LINE}
        _send(panel, CARD_LINE)
        _settle(app, ns)
        check("员工身份下生活卡上照样能长出按钮（不受身份影响）",
              [c["type"] for c in strip.components()] == ["button", "reminder"],
              f"部件={strip.components()!r}")
        check("生活卡的政策里两种动作都在",
              sorted(app.card_chat_service.policy_for(ns).actions) == ["chat", "record"])
        check("生活卡那张组件表还是六种都在",
              app.card_chat_service.policy_for(ns).types == frozenset(tool_mod.CATALOG),
              f"{sorted(app.card_chat_service.policy_for(ns).types)}")
        prompts = _tool_prompts(stub)
        check("生活卡的提示词里没有「不许」那一档工作卡的话",
              prompts and "工作卡" not in prompts[0],
              "提示词里出现了工作卡" if prompts and "工作卡" in prompts[0] else "")

        # 线：红线在生活卡上也一样（模型硬塞一个改流程的）
        stub.tools_result = {"components": [EDIT_ACTIONED], "say": "加好了"}
        _send(panel, CARD_LINE)
        _settle(app, ns)
        check("生活卡上塞一个改流程的部件：也摆不上",
              not any(c["label"] == "推一下" for c in strip.components()),
              f"部件={strip.components()!r}")
        check("卡窗里说的是红线那句，不是模型那句「加好了」",
              _texts(panel)[-1] == tool_mod.EDIT_REFUSAL, f"{_texts(panel)[-1]!r}")
        check("文件里也没有它",
              "推一下" not in Path(app.card_chat_service._tools.path(ns)).read_text(encoding="utf-8"))
    finally:
        box.cleanup()
        drain_threads()


if __name__ == "__main__":
    for case in (
        test_policy_shape,
        test_prompt_uses_the_same_policy,
        test_backend_gate_on_actions,
        test_life_card_stays_free,
    ):
        case()
    # 同 test_reset_state：失败数带成退出码，别让 run_all 把有红的模块记成"通过"。
    raise SystemExit(summary())
