"""画像 → 建议文案（指令 26-C C1 S3）。

规则版：只基于画像里已有的字段推理，不编造没提过的事实。
认不出的字段就跳过，别假装知道。
"""

from __future__ import annotations

from life.profile.tool_registry import DIET_GUARDRAIL

# 这两个字面量跟 life/cards/service.py 的 TRIP_THEME / DIET_THEME 是同一个取值，
# 那边是卡的主题、这边只用来挑建议文案；放这儿是为了不反向依赖 life.cards（会成环）。
TRAVEL_THEME = "出行"
DIET_THEME = "餐饮"


def card_advice(theme: str, profile: dict[str, str]) -> str:
    """按主题挑一段规则版卡面建议；这两类以外的主题返回空串。

    指令 27 之后，出行 / 饮食卡上半框平时由专业角色（导游 / 营养主厨）来说，
    这一段是模型那条挂了时的兜底，也是"手上没有模型"时还能显示的内容。
    """
    if theme == TRAVEL_THEME:
        return travel_advice(profile)
    if theme == DIET_THEME:
        return diet_advice(profile)
    return ""


def travel_advice(profile: dict[str, str]) -> str:
    """出行卡上半：行前建议。只提画像里有的（目的地 / 频次 / 偏好）。"""
    dest = profile.get("destination", "")
    freq = profile.get("frequency", "")
    pref = profile.get("preference", "")
    parts: list[str] = []
    if dest:
        parts.append(f"去{dest}：出门前看下天气，防晒和雨具都带上更稳妥。")
    else:
        parts.append("出门前看下天气，防晒和雨具都带上更稳妥。")
    if freq:
        parts.append("这条路你常走，票务和证件提前核一遍就省心。")
    if pref:
        parts.append(f"按你的习惯（{pref}）来准备。")
    return " ".join(parts)


def work_advice(profile: dict[str, str]) -> str:
    """工作卡上半：进度提醒。只提画像里有的（在赶的事 / 截止日 / 部门）。"""
    task = profile.get("task", "")
    deadline = profile.get("deadline", "")
    dept = profile.get("dept", "")
    parts: list[str] = []
    if task:
        if deadline:
            parts.append(f"手上在赶「{task}」，截止在{deadline}，先拆成几小步、每步定个死线。")
        else:
            parts.append(f"手上在赶「{task}」，先拆成几小步、每步定个死线。")
    else:
        parts.append("手上这件活儿，先拆成几小步、每步定个死线，别等最后一天。")
    if dept:
        parts.append(f"（{dept}这边，跨部门要盯紧接口人。）")
    return "".join(parts)


def diet_advice(profile: dict[str, str]) -> str:
    """饮食卡上半：饮食建议。只提画像里有的（上一顿 / 目标 / 忌口），末尾带专业护栏。"""
    last_meal = profile.get("last_meal", "")
    goal = profile.get("goal", "")
    allergy = profile.get("allergy", "")
    parts: list[str] = []
    if last_meal:
        parts.append(f"这顿「{last_meal}」荤素搭着吃、主食别过量。")
    else:
        parts.append("一荤一素一主食，别凑合一顿泡面。")
    if goal == "减脂":
        parts.append("减脂期：主食减半、多蛋白多蔬菜，少油少糖。")
    elif goal == "控糖":
        parts.append("控糖期：少精米白面，主食换粗粮，餐后别马上躺。")
    elif goal == "增肌":
        parts.append("增肌期：蛋白质要够，鸡胸、蛋奶、鱼虾轮着来。")
    elif goal == "带饭":
        parts.append("带饭的话：荤素分装，绿叶菜别隔夜。")
    if allergy:
        parts.append(f"有忌口（{allergy}），点菜或买菜时避开。")
    return " ".join(parts) + DIET_GUARDRAIL
