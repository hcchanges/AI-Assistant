"""
社交帮回·收尾规则 + 撩妹基调 + 解析引用策略（v2-social-closing）的自动测试。

这一轮治的真机：微信茗风只回"还好啊，感觉还挺愉快的"（仅回应情绪、没抛新话题），
宠物却反问"你那天玩得怎么样"、质疑"你问这个是有事吗"——离题。三刀：

- ① 收尾规则（只一则样板）：对方没抛新话题 → 回一句贴合情绪的收尾，禁反问/质疑/开话题；
- ② 社交帮回基调独立：允许轻 pua / 轻粗口 / 张扬个性，主聊天（AI助理）仍友好；
- ③ 「解析」= 策略解释：候选下方解析引用策略分析风格（废测/框架/可得性/情绪价值），
    范例里的括号分析原样保留当格式锚。

红线：零新依赖；pet.db / mind.db 零写入；config.yaml / .vscode 不提交。
模型是桩，所以这里验的是"该教的规则有没有进提示词、范例有没有按行保留括号分析"，
不是真模型的行为。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, section, summary

from brain.prompts.templates import SOCIAL_DRAFT_PROMPT, SOCIAL_DRAFT_SYSTEM
from life.screen.social_examples import SocialExamples, _read_chunks
from pet.persona import CHAT_STYLE_RULES, resolve_voice


def main() -> int:
    box, stub, app = make_env()
    try:
        section("① 收尾规则：对方没抛新话题 → 回收尾，不反问不质疑")

        check("钉了「没抛新话题 → 回一句收尾」这条硬规则",
              "没抛新话题" in SOCIAL_DRAFT_PROMPT and "就只回一句贴合他情绪的收尾" in SOCIAL_DRAFT_PROMPT, "")
        check("收尾样板在场（只一则）",
              "嗯，我也是感觉挺开心的。那下次还邀请你咯" in SOCIAL_DRAFT_PROMPT, "")
        check("禁反问 / 禁质疑 / 禁开新话题",
              "不许反问" in SOCIAL_DRAFT_PROMPT and "不许质疑" in SOCIAL_DRAFT_PROMPT
              and "不许另开一个话题" in SOCIAL_DRAFT_PROMPT, "")
        check("对方抛了新话题 → 正常接（不误伤，别一律收尾）",
              "就正常接" in SOCIAL_DRAFT_PROMPT and "别一律收尾" in SOCIAL_DRAFT_PROMPT, "")
        check("SYSTEM 也钉了收尾（别反问、别质疑、别开新话题）",
              "没抛新话题" in SOCIAL_DRAFT_SYSTEM and "别反问" in SOCIAL_DRAFT_SYSTEM, "")

        section("② 解析 = 策略解释（引用废测/框架/可得性/情绪价值）")

        check("解析从「语气/策略」升级成「策略解释」", "策略解释" in SOCIAL_DRAFT_PROMPT, "")
        check("策略术语在场（废测 / 可得性 / 情绪价值）",
              "废测" in SOCIAL_DRAFT_PROMPT and "可得性" in SOCIAL_DRAFT_PROMPT
              and "情绪价值" in SOCIAL_DRAFT_PROMPT, "")
        check("SYSTEM 也钉了解析写策略解释",
              "策略解释" in SOCIAL_DRAFT_SYSTEM and "废测" in SOCIAL_DRAFT_SYSTEM, "")
        check("范例那段明说：括号里是策略分析、解析照这风格写",
              "策略分析" in SOCIAL_DRAFT_PROMPT and "回复 + 分析" in SOCIAL_DRAFT_PROMPT, "")

        section("③ 撩妹基调独立：社交帮回放得开，主聊天仍友好")

        check("社交帮回允许轻 pua / 轻粗口 / 张扬个性",
              "轻微 pua" in SOCIAL_DRAFT_PROMPT and "轻微粗口" in SOCIAL_DRAFT_PROMPT
              and "张扬个性" in SOCIAL_DRAFT_PROMPT, "")
        check("底线：禁露骨色情 / 违法 / 严重骚扰",
              "露骨色情" in SOCIAL_DRAFT_PROMPT and "严重骚扰" in SOCIAL_DRAFT_PROMPT, "")
        main_system = resolve_voice(app.config.pet).system_prompt(app.config.pet.name)
        check("主聊天仍友好：先接情绪、不评判不指责",
              "不评判" in main_system and "不指责" in main_system,
              main_system.replace("\n", " / "))
        check("主聊天的关系分寸（不撩不暧昧）一条没动",
              any("不撩" in r and "不暧昧" in r for r in CHAT_STYLE_RULES), "")
        check("撩妹口径没漏进主聊天（pua / 粗口一个都没有）",
              "pua" not in main_system and "粗口" not in main_system
              and not any("pua" in r for r in CHAT_STYLE_RULES), "")

        section("④ 范例按行切：括号里的策略分析原样保留（格式锚）")

        ex_dir = box.dir / "chat_examples"
        ex_dir.mkdir(parents=True, exist_ok=True)
        transcript = ex_dir / "撩妹案例.md"
        transcript.write_text(
            "# 案例\n\n"
            "交个朋友吧（表情包）\n"
            "你好（正式开场白，不好不坏）\n"
            "你好这个词距离感太强了，来个备注防走丢吧（略微调侃，拉近距离）\n"
            "哈哈哈，叫我菲菲就好了\n",
            encoding="utf-8",
        )
        chunks = _read_chunks(transcript)
        check("标题行不算，按行切：4 条对话都留住", len(chunks) == 4, f"{len(chunks)} 条")
        check("括号里的策略分析原样保留（格式锚）",
              any("（略微调侃" in c for c in chunks), repr(chunks))

        old = ex_dir / "旧格式.md"
        old.write_text(
            "同事喊我周末加班，我说：这周排不开。\n\n朋友问我有空没，我说：等我忙完。\n",
            encoding="utf-8",
        )
        check("旧的「段间空行、一段一句」写法照样两条",
              _read_chunks(old) == ["同事喊我周末加班，我说：这周排不开。", "朋友问我有空没，我说：等我忙完。"],
              repr(_read_chunks(old)))

        examples = SocialExamples(app.config)
        all_chunks = examples.global_chunks()
        check("全局目录按行切、括号分析真的读进来了",
              any("（略微调侃" in c for c in all_chunks), repr(all_chunks))

        section("⑤ 真链路：规则进到发给模型的提示词")

        service = app.social_reply
        service._examples_provider = lambda query: ""
        stub.prompts.clear()
        service._write("帮我回一下，茗风刚说周末挺开心的", "茗风：还好啊，感觉还挺愉快的")
        prompt = stub.prompts[-1]
        check("收尾样板真的进了提示词",
              "嗯，我也是感觉挺开心的。那下次还邀请你咯" in prompt, "")
        check("策略解释真的进了提示词", "策略解释" in prompt and "废测" in prompt, "")
        check("撩妹基调真的进了提示词", "轻微 pua" in prompt and "轻微粗口" in prompt, "")
        check("底线真的进了提示词", "露骨色情" in prompt and "严重骚扰" in prompt, "")
    finally:
        box.cleanup()

    return summary("社交帮回收尾 + 撩妹基调 + 解析策略（v2-social-closing）")


if __name__ == "__main__":
    raise SystemExit(main())
