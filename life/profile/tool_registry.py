"""工具池（指令 26-C C2 / C1 S4）：预设工具 + 按品类匹配。

三类分治（借鉴 WorkBuddy）：信息整理型（打包 / 待办 / 会议纪要 / 工时统计）、
实时决策型（天气 / 吃什么 / 热量）、高风险娱乐型（本 MVP 不涉及）。

四检查的落点：
- 实时性：天气 / 吃什么 / 热量标成「实时决策」，生成建议时提醒看当天实况；
- 隐私：工具只读会话内存画像，不存敏感信息、不落库；
- 动作权限：只给建议 + 人确认，不自动下单 / 支付；
- 专业边界：热量只做估算，不诊断。
"""

from __future__ import annotations

from dataclasses import dataclass

KIND_INFO = "信息整理"
KIND_REALTIME = "实时决策"

CATEGORY_WORK = "工作"
CATEGORY_TRAVEL = "出行"
CATEGORY_DIET = "餐饮"

# 饮食类工具的专业护栏（指令 26-C 收尾）：只追加到饮食工具返回末尾，
# 明确「估算与建议不能替代专业意见」。出行 / 工作工具不挂这句。
DIET_GUARDRAIL = "（以上为 AI 估算与建议，不能替代专业营养师或医生意见；如有糖尿病、肾病、过敏、孕期等特殊情况，重要饮食调整请咨询专业医生。）"


@dataclass(frozen=True)
class Tool:
    id: str
    title: str
    icon: str
    category: str
    kind: str
    blurb: str


TOOLS: tuple[Tool, ...] = (
    Tool("todo", "待办生成", "✅", CATEGORY_WORK, KIND_INFO, "把这件事拆成能勾掉的待办"),
    Tool("minutes", "会议纪要", "📝", CATEGORY_WORK, KIND_INFO, "把一段会补成一条条结论"),
    Tool("timesheet", "工时统计", "⏱", CATEGORY_WORK, KIND_INFO, "估一估这件事要占多少时间"),
    Tool("weather_sun", "天气防晒", "🌤️", CATEGORY_TRAVEL, KIND_REALTIME, "出门前看天气与防晒"),
    Tool("packing", "打包清单", "🎒", CATEGORY_TRAVEL, KIND_INFO, "按目的地列一份行李清单"),
    # 导游那一侧的行程工具（指令 27）：前两个管"出门前带什么"，这个管"到了怎么排"
    Tool("itinerary", "排行程", "🗺", CATEGORY_TRAVEL, KIND_INFO, "把想去的地方排成一份按天的行程"),
    Tool("what_to_eat", "今日吃什么", "🍚", CATEGORY_DIET, KIND_REALTIME, "给你挑一餐，避开忌口"),
    Tool("calorie", "热量估算", "🔥", CATEGORY_DIET, KIND_REALTIME, "估一估这顿大概多少热量"),
    # 主厨那一侧的做法工具（指令 27）：前两个管"吃什么"，这个管"怎么做"
    Tool("recipe", "怎么做", "👨‍🍳", CATEGORY_DIET, KIND_INFO, "按手上的食材给个家常做法"),
)


def match_tools(category: str) -> list[Tool]:
    """按品类取工具：工作 / 出行 / 餐饮。"""
    return [tool for tool in TOOLS if tool.category == category]


def tool_suggest(tool_id: str, profile: dict[str, str]) -> str:
    """点工具后给一句建议（MVP：规则生成，不联网、不下单、不诊断）。"""
    dest = profile.get("destination", "")
    task = profile.get("task", "")
    deadline = profile.get("deadline", "")
    if tool_id == "packing":
        tail = f"；去{dest}再看天气补雨具、防晒。" if dest else "；看天气补雨具、防晒。"
        return "打包清单：证件、充电器、换洗衣物、常备药" + tail
    if tool_id == "weather_sun":
        if dest:
            return f"去{dest}前查一下当天天气，晴天备防晒、雨天备伞。"
        return "出门前查一下当天天气，晴天备防晒、雨天备伞。"
    if tool_id == "itinerary":
        head = f"{dest}这趟排行程" if dest else "排行程"
        return (
            head + "：一天最多塞两处大的，上午给远的、下午给近的；"
            "路上按单程一小时估，中间留吃饭的空档，最后一天别排满。"
        )
    if tool_id == "what_to_eat":
        return "今日吃什么：一荤一素一主食，别凑合一顿泡面。" + DIET_GUARDRAIL
    if tool_id == "calorie":
        return "热量估算：按普通分量估，主食减半、多添蔬菜更稳。" + DIET_GUARDRAIL
    if tool_id == "recipe":
        return (
            "怎么做：锅先烧到冒烟再下料，肉两面各煎一分半就够；"
            "青菜大火快炒、起锅前再放盐，出水少还脆。"
            + DIET_GUARDRAIL
        )
    if tool_id == "todo":
        base = f"把「{task}」拆成待办" if task else "把手上这件事拆成待办"
        return base + "：先列清单、标优先级" + (f"（{deadline} 是死线）。" if deadline else "。")
    if tool_id == "minutes":
        return "会议纪要：结论、谁负责、什么时候交，三样记全。"
    if tool_id == "timesheet":
        return "工时统计：先估每个子任务耗时，做完再回填实际。"
    return ""
