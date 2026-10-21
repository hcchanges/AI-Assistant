# -*- coding: utf-8 -*-
"""线下约会教练规则（v2-date-knowledge）的自动测试。

**这一批验什么**：知识是否内化成了可检索的纯规则——

1. 五段阶段机流转（破冰 → 升温 → 亲密 → 私密 → 带走过夜），结束标志说了算；
2. 信号 → 动作映射（喂食三阶、对视移开不后撤 → 可索吻）；
3. 禁忌拦截（破冰未转场不升级、对方后撤立即停）；
4. 话术检索非空，`get_prompt` 输出结构完整。

全程不建环境、不开库、不发网络——这一层只有结构。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.cards import date_rules as dr


def main() -> int:
    section("① 阶段机：五段顺序与结束标志")

    check("★ 五段齐全且顺序固定",
          dr.STAGE_ORDER == (
              dr.STAGE_ICEBREAK, dr.STAGE_WARMUP, dr.STAGE_INTIMATE,
              dr.STAGE_PRIVATE, dr.STAGE_OVERNIGHT,
          ),
          str(dr.STAGE_ORDER))
    check("每段都有目标、场地、下一段",
          all(dr.STAGES[key].goal and dr.STAGES[key].venue for key in dr.STAGE_ORDER)
          and dr.STAGES[dr.STAGE_ICEBREAK].next_key == dr.STAGE_WARMUP
          and dr.STAGES[dr.STAGE_OVERNIGHT].next_key is None,
          "")
    check("破冰时长约 15-20 分钟",
          dr.STAGES[dr.STAGE_ICEBREAK].typical_minutes == (15, 20),
          str(dr.STAGES[dr.STAGE_ICEBREAK].typical_minutes))

    check("★ 破冰未满足结束标志 → 原地不动",
          dr.next_stage(dr.STAGE_ICEBREAK, {"sharing": "low"}) is None, "")
    check("★ 她主动分享度变高 → 破冰结束，可转场升温",
          dr.next_stage(dr.STAGE_ICEBREAK, {"sharing": "high"}) == dr.STAGE_WARMUP, "")
    check("★ 她主动延续话题 → 同样算破冰结束",
          dr.next_stage(dr.STAGE_ICEBREAK, {"she_extends_topic": True}) == dr.STAGE_WARMUP, "")
    check("★ 餐馆吃完 / 时间够 → 升温结束，可转场亲密",
          dr.next_stage(dr.STAGE_WARMUP, {"meal_done": True}) == dr.STAGE_INTIMATE, "")
    check("★ 对视移开不后撤 + 距离够近 → 亲密结束，可转场私密",
          dr.next_stage(dr.STAGE_INTIMATE,
                        {"gaze_away_no_retreat": True, "distance": "close"}) == dr.STAGE_PRIVATE,
          "")
    check("私密阶段要双方明确同意才往下走",
          dr.next_stage(dr.STAGE_PRIVATE, {}) is None
          and dr.next_stage(dr.STAGE_PRIVATE, {"mutual_yes": True}) == dr.STAGE_OVERNIGHT,
          "")

    section("② 信号 → 动作映射")

    check("★ 喂食三阶：自己餐具 / 接你餐具 / 直接吃",
          dr.feeding_level("own_utensil") == 0
          and dr.feeding_level("your_utensil") == 1
          and dr.feeding_level("direct") == 2,
          "")
    check("喂食认不出 → -1（不硬凑）", dr.feeding_level("不知道") == -1, "")

    snap = {"gaze_away_no_retreat": True, "distance": "close"}
    actions = dr.actions_for(snap)
    check("★ 对视移开不后撤 → 可尝试索吻",
          "可以尝试索吻" in actions, str(actions))
    check("★ 膝盖朝向 / 距离近 → 可尝试牵手",
          "可以尝试牵手（过马路等有正当理由的场景）"
          in dr.actions_for({"knees_toward": True, "distance": "normal"}),
          "")

    prompt = dr.get_prompt(dr.STAGE_INTIMATE, snap)
    check("★ 亲密阶段 + 对视信号 → 允许测试升级，且选的是索吻",
          prompt.can_test is True and prompt.test is not None and prompt.test.key == "kiss",
          str(prompt.as_dict()))
    warm_tests = {test.key for test in dr.eligible_tests(dr.STAGE_WARMUP, {"distance": "close"})}
    check("升温阶段可以牵手 / 擦泡沫，但还不能索吻",
          {"hand_hold", "coffee_foam"} <= warm_tests and "kiss" not in warm_tests,
          str(sorted(warm_tests)))

    section("③ 禁忌拦截（硬闸）")

    strong = {
        "distance": "touch",
        "gaze_away_no_retreat": True,
        "feeding": "direct",
        "active_touch": True,
        "sharing": "high",
    }
    blocked = dr.get_prompt(dr.STAGE_ICEBREAK, strong)
    check("★ 破冰未转场，就算信号再强也不升级",
          blocked.can_test is False and blocked.test is None, str(blocked.as_dict()))
    check("★ 提示里明确给出「未转场不升级」",
          any("没有成功转场" in rule for rule in blocked.taboos), str(blocked.taboos))

    resisted = dr.get_prompt(dr.STAGE_PRIVATE, {"resisted": True, "distance": "touch"})
    check("★ 对方后撤/拒绝 → 一切测试停下",
          resisted.can_test is False and dr.eligible_tests(dr.STAGE_PRIVATE, {"resisted": True}) == (),
          str(resisted.as_dict()))
    check("★ 拒绝时给的是刹车提醒，不是推进建议",
          any("立即停" in rule for rule in resisted.taboos), str(resisted.taboos))

    section("④ 话术检索与 get_prompt 结构")

    for kind in (dr.LINE_ICEBREAK, dr.LINE_AWKWARD, dr.LINE_JOKE, dr.LINE_INVITE):
        check(f"话术类别 {kind} 非空", len(dr.lines_for(kind)) > 0,
              str(len(dr.lines_for(kind))))
    variants = {line.variant for line in dr.lines_for(dr.LINE_INVITE)}
    check("★ 邀约四型齐全：模糊 / 播种 / 正式 / 止损",
          variants == {"vague", "seed", "formal", "stop"}, str(sorted(variants)))

    prompt = dr.get_prompt(dr.STAGE_ICEBREAK, {"sharing": "low"})
    data = prompt.as_dict()
    check("★ get_prompt 输出：说什么 + 可否测试 + 禁忌",
          bool(data["suggestion"]) and isinstance(data["can_test"], bool)
          and isinstance(data["taboos"], list) and bool(data["taboos"])
          and data["stage"] == dr.STAGE_ICEBREAK,
          str(data))
    check("认不出的阶段退回破冰（不凭空跳段）",
          dr.get_prompt("不存在", {}).stage == dr.STAGE_ICEBREAK, "")
    ready = dr.get_prompt(dr.STAGE_ICEBREAK, {"sharing": "high"})
    check("结束标志出现时，提示里给出转场方向",
          "转场" in ready.next_hint and "升温" in ready.next_hint, ready.next_hint)

    section("⑤ 红线：纯结构")

    source = Path(dr.__file__).read_text(encoding="utf-8")
    check("★ 不落库、不发网络、无第三方依赖",
          not any(name in source for name in ("sqlite3", "import requests", "import httpx")),
          "")
    check("★ 禁忌清单至少覆盖素材里的七条 + 同意刹车",
          len(dr.REDLINES) >= 8 and dr.REDLINE_CONSENT in dr.REDLINE_BY_KEY,
          str(len(dr.REDLINES)))

    return summary("线下约会教练规则（v2-date-knowledge）")


if __name__ == "__main__":
    raise SystemExit(main())
