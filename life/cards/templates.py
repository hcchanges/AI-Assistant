"""
卡牌模板库。

原先的「生活吐槽 → 六大子项固定入口」已在指令 12 里删除；那六个子项的内容不浪费，
转到这里成为卡牌模板：主题标签 + 默认标题 + 一句话说明。

按指令 14 原先分两族：生活卡由宠物本体（就是林医生）产，玄学卡由玄机先生产。
**本轮把玄学族并进了生活卡**：姻缘 / 事业 / 财运 / 学业 / 运势 不再单独成族，
一律归到第 9 生活大类「命理」。命理卡跟别的生活卡一样由本体提卡、本体建卡（邀请制），
但建出来绑玄机先生当陪执行角色——提卡权与执行角色就此解耦（见 CardService._owner_persona）。

按指令 25-A，生活卡这边分成两样东西，别再混着看：
- **预设模板**（LIFE_TEMPLATES）：启动时预置成牌、直接上轮播。5 张精简成 2 张
  （饮食 / 出行）——删掉的穿搭 / 健身 / 情感陪护不再预置，老库里那几张由
  CardService 收进卡册（打卡历史照样查得到）。
- **9 大类**（LIFE_CATEGORIES）：只是"建卡时的分类标签"，不再直接当轮播卡。用户自建卡时从中选一类，
  也是品类判定（指令 25-B）的落点：一句话归到哪个大类，就在那个大类下攒卡、出邀请。

心理/精神类议题不靠模板挡了，改由 life/cards/spawnable.py 的危机词与心理词表拦
（指令 14 的红线：不把别人的痛苦当收藏品，只由林医生陪着聊）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 卡牌只剩一族（指令 14 的两族制在本轮合并）：统统是生活卡。
# 名字留着 FAMILY_LIFE 没改，是因为门槛与日志那条线还在按"族"说话（见 _invite_if_due）。
FAMILY_LIFE = "life"

FAMILY_TITLES: dict[str, str] = {
    FAMILY_LIFE: "生活卡",
}

# ---------- AI 能力（指令 12 第 4 节） ----------

FEATURE_GUIDE = "guide"      # AI 引导：把模糊需求变清晰、设目标、拆步骤
FEATURE_REMIND = "remind"    # 提醒：按卡牌定时提醒（复用主动提醒调度）
FEATURE_ADVISE = "advise"    # 建议：结合卡牌与长期记忆给个性化建议
FEATURE_CHECKIN = "checkin"  # 打卡 / 进度
FEATURE_TALK = "talk"        # 对话：就这张卡深入交流（带性格腔调）

ALL_FEATURES: tuple[str, ...] = (
    FEATURE_TALK,
    FEATURE_GUIDE,
    FEATURE_ADVISE,
    FEATURE_CHECKIN,
    FEATURE_REMIND,
)

# 命理卡开的能力：一事一卦，没有"进度"可言，所以不开引导与打卡；
# 其余 8 个生活大类仍开全套（见 CardService.create_manual）
METAPHYSICS_FEATURES: tuple[str, ...] = (FEATURE_TALK, FEATURE_ADVISE, FEATURE_REMIND)

# ---------- 卡牌状态 ----------

STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_PAUSED = "paused"

CARD_STATUS_TITLES: dict[str, str] = {
    STATUS_ACTIVE: "进行中",
    STATUS_DONE: "已完成",
    STATUS_PAUSED: "暂停",
}

# ---------- 9 大类（指令 25-A 第 2 项；命理是后加的第 9 类） ----------
# 只当"建卡时的分类标签"：预置卡不再按它们铺满轮播，用户自建一张卡时从这 9 个里挑一个。
# 每个大类配一个默认图标，新建卡时图标默认取所属大类的那个，用户还能在 CARD_ICONS 里自己换。
# 「命理」这一类由宠物本体提卡、用户点头就建卡，但建出来的卡绑玄机先生当陪执行角色
# （命理那套框架由它说，见 CardService._owner_persona）。
CATEGORY_ICONS: dict[str, str] = {
    "餐饮": "🍲",
    "穿搭": "👕",
    "出行": "🚶",
    "健身": "🏃",
    "情感": "💗",
    "作息": "🌙",
    "消费": "💰",
    "娱乐": "🎮",
    "命理": "🔮",
}

LIFE_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_ICONS)

# v2-emotion-log：情感/关系类的话题（对象/家人/吵架…）不再**自动**建成「情感」卡，
# 改记情感事件（归因 + 分类标签）。手动建卡仍能从 9 大类里选「情感」——这里关掉的
# 只是"识别到情绪关键词就自动提卡"那条路（见 life/cards/service.py 的 observe）。
EMOTION_CATEGORY = "情感"

# 命理那一类的类名。这是命理卡的**唯一标识**，取值必须就是上面 LIFE_CATEGORIES 里那一项
# （同一个字面量 "命理"）：CardService._owner_persona 靠它把卡绑给玄机先生，
# 若哪天这里另起一个名，卡就绑不上玄机先生、卡内腔调会错成林医生。
METAPHYSICS_CATEGORY = "命理"

# 玄学族原先那几个主题。它们不再是能成卡的品类了，这份名单只用来做一次性数据迁移：
# 老库里散在这些主题下的观察记录要归并到「命理」（见 CardService.migrate_divination_themes）。
LEGACY_DIVINATION_THEMES: tuple[str, ...] = ("姻缘", "事业", "财运", "学业", "运势")

# 建卡时能挑的图标：生活向的一小撮，够用就好
CARD_ICONS: tuple[str, ...] = (
    "🍲", "👕", "🚶", "🏃", "💗", "🌙", "💰", "🎮", "🔮",
    "📚", "🎯", "🧘", "☀️", "🛒", "✈️", "🎵", "🐾",
)

# 卡名最长几个字（指令 25-A 第 3 项）
CARD_TITLE_MAX = 10

# 活跃个人卡上限（指令 25-A 第 3 项）：只有预设那 2 张不占这个数
PERSONAL_ACTIVE_MAX = 4


@dataclass(frozen=True)
class CardTemplate:
    theme: str            # 主题标签，也是话题归类的取值
    title: str            # 默认卡牌标题
    blurb: str            # 一句话说明（用户可见）
    features: tuple[str, ...] = ALL_FEATURES


LIFE_TEMPLATES: tuple[CardTemplate, ...] = (
    CardTemplate(
        theme="餐饮",
        title="饮食",
        blurb="吃什么、怎么吃得合适，也管着减脂那点事",
    ),
    CardTemplate(
        theme="出行",
        title="出行",
        blurb="怎么走顺路、省时间，出门前也用得上",
    ),
)

# 会预置成牌、直接上轮播的那两张（指令 25-A 第 1 项）
PRESET_THEMES: tuple[str, ...] = tuple(template.theme for template in LIFE_TEMPLATES)

# 指令 25-A 删掉的三张预设：主题 + 当初的标题。
# 迁移时按这一对认出"就是那张预设卡"（用户自己用同名大类建的卡标题是他们自己起的，不动），
# 把预设那张收进卡册——不再出现在轮播上，但打卡历史留着。
REMOVED_PRESETS: tuple[tuple[str, str], ...] = (
    ("穿搭", "穿得顺手"),
    ("健身", "动起来"),
    ("情感陪护", "惦记的人"),
)

# 玄学族已并进「命理」：姻缘 / 事业 / 财运 / 学业 / 运势 不再单独成族，也没有自己的模板。
# 命理卡跟别的生活卡一样由 create_manual 建，卡名与图标用户在确认框里定。
#
# 生活族 = 9 大类（指令 25-A 第 2 项：它们是建卡的分类标签，也是攒卡与出邀请的落点）。
# 认不出的主题（工作、其他、心理安抚）返回空串，不成卡。
_FAMILY_BY_THEME = {category: FAMILY_LIFE for category in LIFE_CATEGORIES}


def family_of(theme: str) -> str:
    """这个主题属于哪一族：如今只剩 life；不认识的主题返回空串。"""
    return _FAMILY_BY_THEME.get((theme or "").strip(), "")


def is_life_category(theme: str) -> bool:
    """是不是 9 大类之一（卡牌按它归类、攒卡与出邀请）。"""
    return (theme or "").strip() in LIFE_CATEGORIES


def category_icon(category: str) -> str:
    """这个大类默认用哪个图标；不认识的大类给个通用图标。"""
    return CATEGORY_ICONS.get((category or "").strip(), "📌")
