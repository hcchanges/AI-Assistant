"""
卡内技能（v2-life-skill）的自动测试。

对着指令 S1 的几条：

1. **记技能**：卡窗里说「加个技能」，模型听出名称 / 触发语 / 工作流，写进这张卡的
   skills.md（跟卡专属知识库同一目录），卡窗回一句交代；
2. **一句话唤起**：说一句触发语，模型认出击中哪个技能，把它的工作流注入这一轮提示词
   （不只是记一笔）；
3. **没命中就走普通聊天**：没记技能 / 没命中时，不额外跑匹配那条路，照常聊天；
4. **隔离**：skills.md 只写这一张卡的目录，不碰 pet.db / mind.db；登记与匹配的提示词里
   不带 memories / confidence 这些别链的关键词。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.cards import skills as skill_mod


def _card(service, title: str):
    for card, _pos in service.plaza():
        if card.title == title:
            return card
    return None


def _settle(app, ns: str) -> None:
    wait_for(lambda: not app.card_chat_service.busy_with(ns), 5)
    drain_threads()
    pump(0.2)


# ---------- ① 记技能 ----------


def test_add_skill_writes_registry() -> None:
    section("① 说「加个技能」→ 听出三样，写进这张卡的 skills.md")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        diet = _card(app.card_service, "少喝奶茶")
        ns = f"life:{diet.id}"
        ctx = app._card_chat_context(ns)
        skills_file = skill_mod.skills_path(ctx["kb"])

        stub.skill_result = {
            "skill_name": "喝水提醒",
            "skill_triggers": "喝水 / 口渴 / 提醒喝水",
            "skill_prompt": "每隔两小时温柔地提醒喝一次水",
        }
        stub.prompts.clear()
        replies: list[tuple[str, str]] = []
        app.bus.card_chat_reply.connect(lambda n, t: replies.append((n, t)))
        accepted = app.card_chat_service.ask(ns, ctx, "加个技能：提醒我喝水")
        _settle(app, ns)

        check("这句话被收下了（没漏到全局那条路去）", accepted, "")
        check("skills.md 落在这张卡的专属知识库目录里（沙箱里）",
              skills_file.is_file() and skills_file.name == "skills.md"
              and skills_file.is_relative_to(box.dir), str(skills_file))
        got = skill_mod.load_skills(ctx["kb"])
        check("注册表里记下了这个技能（三样都对得上）",
              got == [skill_mod.Skill(name="喝水提醒", triggers="喝水 / 口渴 / 提醒喝水",
                                      prompt="每隔两小时温柔地提醒喝一次水")],
              f"{got!r}")
        check("卡窗里回了一句交代（带技能名）",
              replies and replies[-1][0] == ns and "记下这个技能" in replies[-1][1]
              and "喝水提醒" in replies[-1][1], f"{replies!r}")
        add_prompts = [p for p in stub.prompts if '"skill_name"' in p]
        check("走的是「登记」那条路（提示词里带 skill_name）", len(add_prompts) == 1,
              f"登记提示词 {len(add_prompts)} 条")
        check("登记那条提示词里没带 memories（别链的关键词）",
              add_prompts and "memories" not in add_prompts[0], "")
    finally:
        box.cleanup()
        drain_threads()


# ---------- ② 一句话唤起 ----------


def test_trigger_injects_skill() -> None:
    section("② 说一句触发语 → 认出击中哪个技能，把工作流注入这一轮提示词")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        diet = _card(app.card_service, "少喝奶茶")
        ns = f"life:{diet.id}"
        ctx = app._card_chat_context(ns)
        skill_mod.append_skill(ctx["kb"], skill_mod.Skill(
            name="喝水提醒", triggers="喝水 / 口渴", prompt="每隔两小时温柔地提醒喝一次水"))
        check("先确认记上了一个技能", len(skill_mod.load_skills(ctx["kb"])) == 1, "")

        # 技能注册表不算知识：卡库检索不该把 skills.md 的原文捞出来
        kb_ctx = app.kb.card_context("喝水", ctx["kb"])
        check("skills.md 不参与卡库检索（技能元数据不算知识）",
              "触发语" not in kb_ctx and "每隔两小时" not in kb_ctx,
              kb_ctx.replace("\n", " ") if kb_ctx else "（空）")

        stub.skill_match_result = {"matched": "喝水提醒"}
        stub.text = "该喝水了"
        stub.prompts.clear()
        app.card_chat_service._reply(ctx, "提醒我喝水", [])

        match_prompts = [p for p in stub.prompts if '"matched"' in p]
        check("跑了一次技能匹配（那条提示词里带 matched）", len(match_prompts) == 1, "")
        check("回话那条提示词里注入了这个技能的工作流",
              stub.prompts and "被这句话唤起" in stub.prompts[-1]
              and "每隔两小时温柔地提醒喝一次水" in stub.prompts[-1],
              stub.prompts[-1][:80] if stub.prompts else "")
    finally:
        box.cleanup()
        drain_threads()


# ---------- ③ 没命中走普通聊天 ----------


def test_no_skill_goes_plain() -> None:
    section("③ 没记技能 / 没命中 → 不额外跑匹配，照常聊天")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        diet = _card(app.card_service, "少喝奶茶")
        ns = f"life:{diet.id}"
        ctx = app._card_chat_context(ns)

        stub.text = "好呀"
        stub.prompts.clear()
        app.card_chat_service._reply(ctx, "今天吃什么", [])
        check("没记技能 → 一次匹配都没跑",
              not any('"matched"' in p for p in stub.prompts), "")
        check("照常回话（这句话进了提示词）",
              stub.prompts and "今天吃什么" in stub.prompts[-1], "")

        # 记了技能但没命中：匹配跑一次、不注入工作流、照常回话
        skill_mod.append_skill(ctx["kb"], skill_mod.Skill(
            name="喝水提醒", triggers="喝水", prompt="每隔两小时温柔地提醒喝一次水"))
        stub.skill_match_result = {"matched": None}
        stub.prompts.clear()
        app.card_chat_service._reply(ctx, "今天吃什么", [])
        check("有技能但没命中 → 匹配跑了一次",
              len([p for p in stub.prompts if '"matched"' in p]) == 1, "")
        check("没注入工作流（回话里没有技能的工作流原文）",
              stub.prompts and "每隔两小时温柔地提醒喝一次水" not in stub.prompts[-1], "")
    finally:
        box.cleanup()
        drain_threads()


# ---------- ④ 没听出不写文件 ----------


def test_add_skill_refused_when_empty() -> None:
    section("④ 模型没听出技能（三项空的）→ 如实说没记下，不写文件")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        diet = _card(app.card_service, "少喝奶茶")
        ns = f"life:{diet.id}"
        ctx = app._card_chat_context(ns)
        skills_file = skill_mod.skills_path(ctx["kb"])

        stub.skill_result = {"skill_name": "", "skill_triggers": "", "skill_prompt": ""}
        replies: list[tuple[str, str]] = []
        app.bus.card_chat_reply.connect(lambda n, t: replies.append((n, t)))
        app.card_chat_service.ask(ns, ctx, "加个技能")
        _settle(app, ns)

        check("没写文件", not skills_file.exists(), str(skills_file))
        check("如实说没听出（没装成记下了）",
              replies and "没听出" in replies[-1][1], f"{replies!r}")
    finally:
        box.cleanup()
        drain_threads()


# ---------- ⑤ skills.py 纯函数 ----------


def test_skills_module_unit() -> None:
    section("⑤ skills.py 纯函数：解析 / 回写 / 识别")
    s = [skill_mod.Skill(name="喝水提醒", triggers="喝水 / 口渴", prompt="提醒喝水")]
    text = skill_mod.render_skills_md(s)
    check("回写再解析能还原", skill_mod.parse_skills_md(text) == s, text)
    check("找不到文件当没记", skill_mod.load_skills("/nonexistent/x/y") == [], "")
    check("is_skill_add 认得 skill / 技能",
          skill_mod.is_skill_add("加个skill") and skill_mod.is_skill_add("给我加个技能"), "")
    check("is_skill_add 不拦普通话", not skill_mod.is_skill_add("加个喝水提醒"), "")


def main() -> int:
    test_add_skill_writes_registry()
    test_trigger_injects_skill()
    test_no_skill_goes_plain()
    test_add_skill_refused_when_empty()
    test_skills_module_unit()
    return summary("卡内技能（v2-life-skill）：")


if __name__ == "__main__":
    raise SystemExit(main())
