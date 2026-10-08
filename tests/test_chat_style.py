"""
宠物语气去 AI 味（v2-chat-style）的自动测试。

把语气规则 + few-shot 范本注入对话生成 prompt，让回复更像真人。验四件事：
① 语气规则进了 system（两条回话路共用），闲聊路多了日常陪伴 few-shot 范本；
② 范本与规则不出现油腻 / PUA / 粗口 / 恋爱话术；
③ 意图路由、情绪识别、记忆不受影响（闲聊走轻路、求助走专业框架，记忆段照旧）；
④ 人设口径：身份是 AI 助理，不再是猫、也不带「喵」（v2-pet-persona-rename）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from pet.persona import CHAT_STYLE_RULES, persona_style_layer
from life.dialog.router import DialogRoute, INTENT_CHAT, INTENT_VENT
from life.psych import casual

ROOT = Path(__file__).resolve().parent.parent


def _system_of(messages) -> str:
    return "\n".join(m.get("content", "") for m in messages if m.get("role") == "system")


def main() -> int:
    box, stub, app = make_env()
    try:
        pet = app.config.pet
        svc = app.psych_service

        # ---------- ① 语气规则 + few-shot 范本注入 ----------

        section("① 语气规则进了 system，闲聊路多了 few-shot 范本")

        style = persona_style_layer(pet, pet.name)
        check("persona_style_layer 里多了一段「去 AI 味」语气",
              "去 AI 味" in style, style[style.find("语气"):][:80])
        check("语气规则一条不少地进了 system（含新增「阶段分寸」）",
              all(rule in style for rule in CHAT_STYLE_RULES),
              f"{len(CHAT_STYLE_RULES)} 条 / 命中 {sum(r in style for r in CHAT_STYLE_RULES)}")
        check("新增「阶段分寸」规则：前期别急着升温、不撩、不暧昧",
              any("关系分寸" in r for r in CHAT_STYLE_RULES) and "关系分寸" in style,
              next((r for r in CHAT_STYLE_RULES if "关系分寸" in r), ""))

        check("few-shot 范本 6 段（3 陌生 + 3 熟悉）", len(casual.CASUAL_FEW_SHOT) == 6,
              f"{len(casual.CASUAL_FEW_SHOT)} 段")
        check("CHAT_STAGES 当前只启用前期两档（03/04 待解锁）",
              casual.CHAT_STAGES == ("陌生", "熟悉"), str(casual.CHAT_STAGES))
        check("范本按阶段分：3 陌生 + 3 熟悉",
              [s.stage for s in casual.FEW_SHOTS].count("陌生") == 3
              and [s.stage for s in casual.FEW_SHOTS].count("熟悉") == 3,
              str([s.stage for s in casual.FEW_SHOTS]))
        # 真跑闲聊路，范本应该以 user/assistant 成对出现在喂给模型的消息里
        stub.messages.clear()
        svc._compose("今天好累啊", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        msgs = stub.messages[-1] if stub.messages else []
        check("闲聊路真把范本塞进了消息（范本第一句 user 在）",
              any(m.get("content") == casual.CASUAL_FEW_SHOT[0][0] for m in msgs),
              f"{len(msgs)} 条消息")
        check("范本的回复也在（有来有回那半句）",
              any(m.get("content") == casual.CASUAL_FEW_SHOT[0][1] for m in msgs), "")
        sys_text = _system_of(msgs)
        check("闲聊路 system 仍含「日常闲聊」那条路标", "日常闲聊" in sys_text, "")

        # ---------- ② 不油腻 / 不 PUA / 不粗口 / 不恋爱 ----------

        section("② 前期范本与规则不出现油腻 / PUA / 粗口 / 恋爱话术")

        banned = ("宝贝", "亲亲", "么么哒", "爱你", "喜欢你", "在一起", "PUA", "打压",
                  "蠢", "滚", "废物", "离不开我", "只有我",
                  # v2-chat-stage 补充：样本里实际出现的恋爱/撩妹/粗口/露骨词，前期范本不得带
                  "男朋友", "女朋友", "男友", "想你", "抱走", "麻袋", "带回家",
                  "按摩", "SPA", "来我家", "卧操", "他妈", "放屁", "狗屁")
        all_shot = "\n".join(u + a for u, a in casual.CASUAL_FEW_SHOT)
        check("few-shot 范本一个禁用词都没有",
              all(word not in all_shot for word in banned),
              [w for w in banned if w in all_shot] or "（干净）")
        all_rules = "\n".join(CHAT_STYLE_RULES)
        check("语气规则也不带禁用词",
              all(word not in all_rules for word in banned),
              [w for w in banned if w in all_rules] or "（干净）")
        check("后期阶段（03 好朋友 / 04 恋人）的范本还没注入（待解锁）",
              "彩礼" not in all_shot and "男友视角" not in all_shot, "")

        # ---------- ③ 意图 / 情绪 / 记忆不受影响 ----------

        section("③ 意图路由、情绪识别、记忆不受影响")

        stub.messages.clear()
        svc._compose("今天好累啊", 0.0, DialogRoute(INTENT_CHAT, "平静"))
        casual_sys = _system_of(stub.messages[-1] if stub.messages else [])
        check("闲聊句 → 走轻路（没有专业框架的「回应准则」）",
              "回应准则" not in casual_sys and "日常闲聊" in casual_sys, "")

        stub.messages.clear()
        svc._compose("我最近焦虑得睡不着，不知道该怎么办", 0.7, DialogRoute(INTENT_VENT, "焦虑"))
        pro_sys = _system_of(stub.messages[-1] if stub.messages else [])
        check("求助句 → 仍走专业框架（「回应准则」还在，语气规则没把它冲掉）",
              "回应准则" in pro_sys, "")
        check("专业框架路也带了语气规则（两条路共用）", "去 AI 味" in pro_sys, "")
        check("记忆段两条路都照旧（「你记得关于用户的事」在）",
              "你记得关于用户的事" in casual_sys and "你记得关于用户的事" in pro_sys, "")

        # ---------- ④ 人设口径（v2-pet-persona-rename） ----------

        section("④ 人设口径：身份是 AI 助理，不再是猫、也不带「喵」")

        check("system 里的身份是 AI 助理（不再是「养的一只猫」）",
              "AI 助理" in style and "猫" not in style, "")
        check("语气规则里不再有「喵」这条（去语气词，改成「别油腻、别用力过猛」）",
              "喵" not in "\n".join(CHAT_STYLE_RULES) and "喵" not in style
              and "别油腻" in CHAT_STYLE_RULES[-2], CHAT_STYLE_RULES[-2])

        # ---------- ⑤ 范本原文替换 + 文件加载（v2-chat-style-fix） ----------

        section("⑤ 语气范本：原文替换 + 文件加载（v2-chat-style-fix）")

        check("范本从文件读，不再写死在 .py 里",
              casual.FEW_SHOT_FILE.is_file() and casual.FEW_SHOT_FILE.parent.name == "范本",
              str(casual.FEW_SHOT_FILE))
        check("范本文件随代码走（在仓库里，不挂用户目录）",
              str(casual.FEW_SHOT_FILE).startswith(str(ROOT)),
              str(casual.FEW_SHOT_FILE.relative_to(ROOT)))
        # 加载出来的就是文件里那几段：改文件即改范本（不是"另有一份抄写好的常量"）
        from_file = casual.parse_few_shot(casual.FEW_SHOT_FILE.read_text(encoding="utf-8"))
        check("CASUAL_FEW_SHOT 就是文件解析出来的（改文件即生效，没第二份副本）",
              from_file == casual.FEW_SHOTS,
              f"文件 {len(from_file)} 段 / 内存 {len(casual.FEW_SHOTS)} 段")

        # 每一段的用户句，都得在出处文件里逐字找得到——这才叫"原文"
        mismatch: list[str] = []
        spans: list[str] = []
        for shot in casual.FEW_SHOTS:
            src = casual.FEW_SHOT_FILE.parent / shot.source_file
            if not src.is_file():
                mismatch.append(f"{shot.source_file} 不在")
                continue
            lines = src.read_text(encoding="utf-8").splitlines()
            if not (1 <= shot.source_start <= shot.source_end <= len(lines)):
                spans.append(f"{shot.source_file}:{shot.source_start}-{shot.source_end} 区间不合法")
                continue
            raw = lines[shot.source_start - 1]
            original = raw.split("：", 1)[1] if "：" in raw else raw
            if original.strip() != shot.user:
                mismatch.append(
                    f"{shot.source_file}:{shot.source_start} 原文是 {original!r}，范本写成 {shot.user!r}"
                )
        check("6 段用户句都在原文里逐字对得上（不是自拟）",
              not mismatch, mismatch or "（6/6 对得上）")
        check("每段的出处区间都落在原文里（回原文核得动）", not spans, spans or "（6/6 合法）")

        # 解析只坏一段连坐一段：缺了宠物句的那段丢掉，别的照留
        partly = casual.parse_few_shot(
            "[陌生]\n用户：甲\n宠物：乙\n出处：01_陌生阶段.md:1-2\n\n"
            "[熟悉]\n用户：丙\n出处：01_陌生阶段.md:3-4\n"
        )
        check("一段写坏了只丢那一段，不连坐（解析的容错）",
              len(partly) == 1 and partly[0].user == "甲", f"{len(partly)} 段")

        # 文件丢了：空表 + 日志里有话，不炸
        missing = casual.load_few_shot(casual.FEW_SHOT_FILE.parent / "根本没有这份.md")
        check("范本文件丢了：给空表、不抛错（程序照常起得来）", missing == (), f"{len(missing)} 段")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("宠物语气去 AI 味（v2-chat-style）")


if __name__ == "__main__":
    raise SystemExit(main())
