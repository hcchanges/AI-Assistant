"""从一句话里抽画像（指令 26-C C1 S2）——规则版，不编造。

工作抽：部门 / 在赶的事 / 截止日；出行抽：目的地 / 频次 / 偏好；饮食抽：上一顿 / 目标 / 忌口。
只认原文里实际出现的字眼，认不出就留空，绝不替用户补事实。

为什么用规则版而不是走 counselor_lin 本体 prompt：试点先保证确定性、离线可测、
零编造；规则版能稳定通过离线回归，模型抽取留到后续换口子（换 extract 的调用点即可）。
"""

from __future__ import annotations

import re

# 部门名 + "部"：研发部 / 市场部 / 销售部 / 财务部 …
_DEPT = re.compile(r"(研发|市场|销售|财务|人力|人事|生产|采购|设计|运营|品控|仓储)部")

# 截止日说法：月底 / 月中 / 月初 / 周末 / 下周三 / 几号 / 几月几日
_DEADLINE = re.compile(
    r"(月底|月中|月初|周末|周[一二三四五六日天]|下周[一二三四五六日天]"
    r"|\d{1,2}月\d{1,2}[日号]|\d{1,2}号)"
)

# 手上在赶的活儿（工作实体词；小写 bom 也算）
_WORK_ITEMS = (
    "bom", "方案", "报告", "项目", "需求", "工单", "排产", "打样", "卖点", "汇报", "交付",
)

# 出行偏好：怕冷 / 怕热 / 怕晒 / 晕车 / 忌口 / 吃辣…
_TRAVEL_PREF = re.compile(r"(怕冷|怕热|怕晒|晕车|晕机|忌口|不吃辣|吃辣|早睡|晚睡)")

# 常见目的地（城市名）。只认这份名单里的，认不出就留空——不猜、不编。
_CITIES = (
    "上海", "北京", "广州", "深圳", "成都", "杭州", "南京", "武汉", "西安", "重庆",
    "长沙", "苏州", "青岛", "厦门", "天津", "郑州", "昆明", "三亚", "大理", "桂林",
    "丽江", "哈尔滨", "沈阳", "大连", "济南", "合肥", "福州", "南昌", "南宁", "海口",
    "贵阳", "兰州", "银川", "西宁", "呼和浩特", "石家庄", "太原", "长春",
)


def extract_work_profile(text: str) -> dict[str, str]:
    """工作画像：部门 / 在赶的事 / 截止日。"""
    fields: dict[str, str] = {}
    dept = _DEPT.search(text)
    if dept:
        fields["dept"] = dept.group(1) + "部"
    lowered = text.lower()
    for item in _WORK_ITEMS:
        if item in lowered:
            fields["task"] = item
            break
    deadline = _DEADLINE.search(text)
    if deadline:
        fields["deadline"] = deadline.group(1)
    return fields


def extract_travel_profile(text: str) -> dict[str, str]:
    """出行画像：目的地 / 频次 / 偏好。"""
    fields: dict[str, str] = {}
    for city in _CITIES:
        if city in text:
            fields["destination"] = city
            break
    if re.search(r"(经常|老跑|总跑|常常|每[周月]|熟门熟路)", text):
        fields["frequency"] = "经常"
    pref = _TRAVEL_PREF.search(text)
    if pref:
        fields["preference"] = pref.group(1)
    return fields


# 出行动词：后面紧接城市名，才算"报了个目的地"（去上海 / 跑上海 / 回上海 / 飞上海…）
_TRAVEL_VERBS = ("去", "跑", "回", "飞", "旅行", "旅游", "出差")


def has_travel_destination(text: str) -> bool:
    """这句话里有没有"出行动词 + 目的地城市"（供意图判定复用）。

    只认 _CITIES 名单里的城市、且前面挨着出行动词，认不出就 False——不猜、不编。
    「出差」本身在出行词表里，但"去上海 / 跑上海"这种没带摩擦词（堵车 / 迟到 / 机场）
    的说法，靠词表抓不到，这里补一道。
    """
    for city in _CITIES:
        for verb in _TRAVEL_VERBS:
            if f"{verb}{city}" in text:
                return True
    return False


# 方位说法：挨着城市名的那一两个字（去了上海 / 到上海了 / 人在上海 / 回广州 / 飞上海）。
# 比 _TRAVEL_VERBS 宽一档，也换个用法：_TRAVEL_VERBS 判的是"报了个目的地"（出行意图），
# 这份判的是"这句把用户放在哪儿"（地点追踪，见 life/dialog/location.py）。
_LOCATION_MARKERS: tuple[str, ...] = (
    "去", "到", "回", "来", "飞", "在", "跑", "抵达", "前往", "出差",
)


def mentioned_city(text: str) -> str:
    """这句话把用户放在哪座城市。认不出回空串——不猜、不编。

    只认 _CITIES 名单里的城市，而且要**挨着一个方位词**才算：「去上海 / 回广州 /
    到上海了 / 人在上海」都算；光提一句「上海真热」不算。认不出来宁可当没提过，
    也不猜他现在人在哪儿。

    一句话里提到不止一座城时取**位置最靠前**的那个（先说的那件事是主语）。
    """
    best = ""
    best_at = -1
    for city in _CITIES:
        at = text.find(city)
        if at < 0:
            continue
        before = text[max(0, at - 2) : at]
        if not any(mark in before for mark in _LOCATION_MARKERS):
            continue
        if best_at < 0 or at < best_at:
            best, best_at = city, at
    return best


# 上一顿吃了啥：「吃 / 喝」后面的食物描述，截到标点或空白为止（放前面的是更长的说法，先匹配）
_EAT = re.compile(r"(?:吃过|吃了|喝过|喝了|吃|喝)([一-龥A-Za-z0-9（）()]{1,20})")

# 健康目标（减脂 / 控糖 / 增肌 / 带饭；减肥 / 减重都归到减脂）
_GOAL = re.compile(r"(减脂|减肥|减重|控糖|增肌|带饭)")
_GOAL_ALIAS = {"减肥": "减脂", "减重": "减脂"}

# 忌口：对 X 过敏 / 不吃 X / 忌口
_ALLERGY = re.compile(r"(对[一-龥]{1,6}过敏|[一-龥]{1,6}过敏|不吃[一-龥]{1,6}|忌口)")


def extract_diet_profile(text: str) -> dict[str, str]:
    """饮食画像：上一顿吃了啥 / 健康目标 / 忌口。"""
    fields: dict[str, str] = {}
    eat = _EAT.search(text)
    if eat:
        fields["last_meal"] = eat.group(1).strip()
    goal = _GOAL.search(text)
    if goal:
        fields["goal"] = _GOAL_ALIAS.get(goal.group(1), goal.group(1))
    allergy = _ALLERGY.search(text)
    if allergy:
        fields["allergy"] = allergy.group(1).strip()
    return fields
