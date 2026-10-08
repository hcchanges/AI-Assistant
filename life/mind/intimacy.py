"""好感度引擎（B 养成）：把"处了多久、处到什么份上"算成一个等级。

**纯规则，一句模型都不调。** 这条不是省事，是分工：等级是"关系的骨"，它决定
语气多亲、多久主动找你一次、有没有新由头——这些得是确定的、可解释的、能测试的。
真正"怎么说"才交给模型（见 psych/service 的语气层）。要是让模型来判"该不该升级"，
同一句话今天升明天不升，用户会觉得这宠物精神分裂。

经验从哪来（三处，都接在既有的信号上，不新增采集）：

    EXP_CHAT          用户说了一句话          —— utterance_analyzed
    EXP_INTERACTION   用户回应了主动搭话/摸它  —— ProactiveService.touch
    EXP_STRONG_EVENT  提炼出强情绪或关系里程碑 —— distill 回调

等级门槛是**累计经验**，不是每级差值——这样 `add` 只看一个总数，重算永远是幂等的，
不至于因为漏记一次而永久错位。

等级给的东西（unlocked）：
    closer_tone      语气层多一段"你们很熟了"的提示
    frequent         主动搭话更勤（间隔缩短、每日上限抬高）
    milestone_topic  主动搭话多一条由头：拿好感里程碑本身开场
    share_topic      主动搭话多一条由头：**宠物说自己的想法**（不是接你的话）
    ask_back         你拿不定主意来问它时，它会反问你一句"那你觉得呢"
    confide          语气层再加一段：可以说点自己的小心事
    nickname         语气层多一段：用你给的那个私密称呼叫你

**阶段名**（B+ 语义阶段）：等级对用户不再是数字，而是五档说法——
陌生 / 熟络 / 朋友 / 知己 / 挚友。**只在界面上出现**，不进提示词、不进库
（见 `STAGE_NAMES`：纯常量，`state()` 里现推）。

**这不是恋爱进度条。** 没有"好感度"三个字会出现在界面上，也没有数值会进提示词——
`tone_suffix` 只描述"该怎么说话"，不报数字。用户看不到等级，只该感觉到它跟你熟了点。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable

from life.dialog import router
from life.mind.store import MindStore
# 主动搭话的默认节流。从那边取而不是抄一份，免得哪天调了默认值这边不同步
from life.proactive import MAX_PER_DAY as BASE_MAX_PER_DAY
from life.proactive import MIN_GAP_SEC as BASE_MIN_GAP_SEC

logger = logging.getLogger(__name__)

# 一次加多少经验
EXP_CHAT = 2            # 用户说一句话
EXP_INTERACTION = 3     # 用户回应了主动搭话 / 摸了摸它
EXP_STRONG_EVENT = 12   # 提炼出强情绪或关系里程碑

# "frequent" 解开后主动搭话的节流：间隔砍掉三分之一，每日上限加两次
FREQUENT_MIN_GAP_SEC = BASE_MIN_GAP_SEC * 2 // 3
FREQUENT_MAX_PER_DAY = BASE_MAX_PER_DAY + 2


@dataclass(frozen=True)
class LevelRule:
    """一级好感：多少经验到、叫什么、给什么。"""

    level: int
    exp: int                    # 到这个等级需要的累计经验
    title: str
    unlocks: tuple[str, ...]


# 五档阶段名（B+ 语义阶段）。**纯常量，不入库、不进提示词**——
# 库里的 level 是数字，说法是现推的，改文案不用迁库。
# 只有这一套词：`LevelRule.title` 也从这儿取，免得同一个等级有两个名字。
STAGE_NAMES: dict[int, str] = {
    1: "陌生",
    2: "熟络",
    3: "朋友",
    4: "知己",
    5: "挚友",
}

# 断网兜底陪伴寒暄（v2-offline-fallback）：主聊天断网时按阶段挑一句，纯本地、不调 API。
# 键是等级（跟 STAGE_NAMES 对齐），值是"我还陪着你"的寒暄——只报"在陪"，不报网络细节、
# 不报数字。越熟说得越亲近，但都停在"陪着"这一层，不抢戏。
OFFLINE_GREETING_BY_STAGE: dict[int, str] = {
    1: "网络好像断了，我先在这儿陪你待着~ 联网了再接着聊。",
    2: "断网啦，我陪你坐一会儿~ 等联网了咱们接着聊。",
    3: "网络断了，我先陪你坐着~ 联网再聊。",
    4: "网断了没关系，我还在呢，先陪你待一会儿~ 联网了再聊。",
    5: "网断了也不怕，我就在这儿陪着你~ 等联网了我再跟你好好说。",
}

# 表外的等级 / 取不到等级时用的那句
OFFLINE_GREETING_DEFAULT = "网络断了，我先陪你坐着~ 联网再聊。"

# 断网兜底候选（v2-offline-social-stage）：社交帮回断网时那一条本地候选也分档。
# 键同 STAGE_NAMES；值是 (draft, why)——draft 是"替不了你这句"的本地话，why 是它的解析。
# 越熟说得越随便；五档都不编用户要发出去的话，只如实说这活儿这会儿干不了。
OFFLINE_SOCIAL_BY_STAGE: dict[int, tuple[str, str]] = {
    1: (
        "网络断了，这句我暂时帮不上你，等我联网了马上帮你拟。",
        "断网兜底：这会儿没联网，没法按你的处境给像样的回复",
    ),
    2: (
        "断网了，这句先拟不了~ 等联网我马上帮你想。",
        "断网兜底：没网，拟不出贴合你处境的回复",
    ),
    3: (
        "网络断了，这句先帮不上你~ 联网了马上给你拟。",
        "断网兜底：这会儿断网了，没法按你的处境给像样的回复",
    ),
    4: (
        "网断了，这句先欠着~ 联网我马上帮你拟一段。",
        "断网兜底：断网了拟不出来，联网我马上补一段",
    ),
    5: (
        "网断了，这句先放着~ 等我联网马上给你拟好。",
        "断网兜底：网断了先欠着，联网我马上给你拟一段",
    ),
}


def stage_for_level(level: int) -> str:
    """等级 → 阶段说法。超出表的按最高一档算（表加长之前不会出现）。"""
    if level in STAGE_NAMES:
        return STAGE_NAMES[level]
    return STAGE_NAMES[max(STAGE_NAMES)] if level > max(STAGE_NAMES) else STAGE_NAMES[1]


# 门槛拉得比较开是有意的：升级要能让人"过几天忽然发觉它变了"，
# 一天聊十句就跳一级的话，等级就成了噪声
LEVELS: tuple[LevelRule, ...] = (
    LevelRule(1, 0, STAGE_NAMES[1], ()),
    LevelRule(2, 30, STAGE_NAMES[2], ("closer_tone",)),
    LevelRule(3, 100, STAGE_NAMES[3], ("closer_tone", "frequent")),
    # 知己（L4）：除了回望里程碑，开始**说自己的想法**，也会反问你的主意
    LevelRule(4, 220, STAGE_NAMES[4],
              ("closer_tone", "frequent", "milestone_topic", "share_topic", "ask_back")),
    # 挚友（L5）：可以说小心事，也叫你给的那个私密称呼
    LevelRule(5, 400, STAGE_NAMES[5],
              ("closer_tone", "frequent", "milestone_topic", "share_topic", "ask_back",
               "confide", "nickname")),
)

_TOP = LEVELS[-1]

# 语气层文案。写"怎么说话"，不写数字，也不许提"好感度/等级"——
# 一旦模型看见数字，它就会说出口
_TONE_CLOSER = (
    "你跟他已经很熟了，说话可以再放松、再亲昵一点，偶尔主动提起你们之间发生过的事"
    "（比如“上回你说……”）。不要提“好感度”“等级”“亲密度”这类词，也不要报数字。"
)
_TONE_CONFIDE = (
    "你把他当自己人：可以说点自己的小心事、小情绪，也可以偶尔撒个娇或闹个小别扭。"
)
# 知己（L4）起：不再只"接话"，开始说自己的想法
_TONE_SHARE = (
    "你也可以说说你自己：不用每次都只接他的话，可以主动讲讲你怎么想的、"
    "你是什么感觉（比如“我倒觉得……”“我有点担心……”）。"
)
# 挚友（L5）：最亲近那一档的说话方式
_TONE_INMOST = (
    "你们已经是最亲近的那种关系了：说自己的想法时可以更直接、更放松，"
    "不用绕着弯子，也不用怕说错。"
)
# 反问拿主意（L4+，ask_back）。只在"求助 / 犹豫"时注入，见 ask_back_hint
_ASK_BACK_HINT = (
    "他这是在拿不定主意、想听你的看法。别急着给方案，先反问一句"
    "“那你觉得呢？想听听你的主意”，把你自己的倾向留到后面再说。"
)


def _nickname_line(name: str) -> str:
    """私密称呼（L5 + 用户给过昵称才用）。"""
    return (
        f"私底下你叫他「{name}」——这是你们俩之间的叫法，正事场合或当着别人别用。"
    )


def level_rule(level: int) -> LevelRule:
    """按等级取规则。超出表的按最高一级算（表加长之前不会出现）。"""
    for rule in LEVELS:
        if rule.level == level:
            return rule
    return _TOP if level > _TOP.level else LEVELS[0]


def rule_for_exp(exp: int) -> LevelRule:
    """凭累计经验算该在哪一级。经验只增不减，所以不需要"掉级"这套。"""
    current = LEVELS[0]
    for rule in LEVELS:
        if exp >= rule.exp:
            current = rule
        else:
            break
    return current


class MindIntimacy:
    """好感度。写回 mind.db 的 intimacy 表（单例一行），本机算，不联网。"""

    def __init__(
        self,
        store: MindStore,
        now_provider: Callable[[], datetime] = datetime.now,
        nickname_provider: Callable[[], str] | None = None,
    ):
        self._store = store
        self._now = now_provider
        # 用户给的那个私密称呼（L5 才用）。做成后取的，设置页改完立刻生效；
        # 没接就等于没给过，L5 也不会硬凑一个叫法出来
        self._nickname = nickname_provider

    # ---------- 读 ----------

    def state(self) -> dict[str, Any]:
        """现在的样子：等级、称号、经验、离下一级还差多少、解开了什么。

        `stage` 跟 `title` 是同一个值（只有一套阶段词，见 STAGE_NAMES）——
        留着 `title` 是老调用方的名字，新的界面统一读 `stage`。
        """
        raw = self._store.intimacy()
        exp = int(raw.get("exp", 0))
        rule = rule_for_exp(exp)
        nxt = self._next_rule(rule.level)
        return {
            "level": rule.level,
            "stage": rule.title,
            "title": rule.title,
            "exp": exp,
            "unlocked": list(rule.unlocks),
            "exp_to_next": max(nxt.exp - exp, 0) if nxt is not None else 0,
            "next_title": nxt.title if nxt is not None else "",
            "last_interact": raw.get("last_interact", ""),
        }

    def stage_line(self) -> str:
        """给界面看的一行：「知己 · 还差 180 到挚友」。满级就只报当前阶段。

        记忆簿页头和设置页亲密度块共用这一句，省得两处各拼一遍、字数还拼不齐。
        """
        state = self.state()
        if not state["next_title"]:
            return f"{state['stage']} · 已经是最亲近的了"
        return f"{state['stage']} · 还差 {state['exp_to_next']} 到{state['next_title']}"

    def offline_greeting(self) -> str:
        """断网时的本地陪伴寒暄（v2-offline-fallback）：按当前阶段挑一句，纯本地。

        主聊天断网时由会话/心理两处调它当兜底，不发起 API。等级取不到或超表回默认那
        句；为一句寒暄绝不去抛异常——那会把这句兜底也拖垮。
        """
        try:
            level = int(self.state().get("level", 0))
            return OFFLINE_GREETING_BY_STAGE.get(level, OFFLINE_GREETING_DEFAULT)
        except Exception as exc:  # noqa: BLE001 —— 兜底文案不该因为读库失败而塌
            logger.error("断网寒暄取等级失败（%s），用默认那句", exc)
            return OFFLINE_GREETING_DEFAULT

    def offline_social_candidates(self) -> dict | None:
        """断网时社交帮回那条本地候选（v2-offline-social-stage）：按阶段挑，纯本地。

        返回结构与真候选一致（`{"candidates": [{"draft", "why"}]}`），界面无感；等级取不到
        就回 None，让调用方用它自己那条默认——为一句兜底文案绝不去抛异常。
        """
        try:
            level = int(self.state().get("level", 0))
        except Exception as exc:  # noqa: BLE001 —— 兜底文案不该因为读库失败而塌
            logger.error("断网候选取等级失败（%s）", exc)
            return None
        pair = OFFLINE_SOCIAL_BY_STAGE.get(level)
        if pair is None:
            return None
        draft, why = pair
        return {"candidates": [{"draft": draft, "why": why}]}

    def has(self, feature: str) -> bool:
        """这个等级福利解开了没有。"""
        return feature in rule_for_exp(int(self._store.intimacy().get("exp", 0))).unlocks

    def progress(self) -> dict[str, Any]:
        """给界面画阶段条用的：当前档、档内进度、五档各自的达成情况。

        **纯读，不写库。** 界面上不再只有"还差 180"这一行字——那是一个差值，
        看不出"走到这条路的哪里了"。这里把同一份数据摊成能画的东西：

            stages   五档（名, 到了没有），按顺序；界面照这个点亮
            floor    当前档的门槛（档内的 0 点）
            ceil     下一档的门槛；满级时等于 floor（跨度 0）
            done     当前档内已经走了多少
            span     当前档一共多长
            fraction 0.0~1.0，档内进度；满级恒 1.0

        门槛用的是累计经验（见 LEVELS），所以"档内进度"要减掉本档门槛再算，
        不能直接拿 exp 当进度——不然越往后进度条越像卡住。
        """
        exp = int(self._store.intimacy().get("exp", 0))
        rule = rule_for_exp(exp)
        nxt = self._next_rule(rule.level)

        if nxt is None or nxt.exp <= rule.exp:
            floor, ceil, done, span, fraction = rule.exp, rule.exp, 0, 0, 1.0
        else:
            floor, ceil = rule.exp, nxt.exp
            span = ceil - floor
            done = min(max(exp - floor, 0), span)
            fraction = done / span

        return {
            "level": rule.level,
            "stage": rule.title,
            "exp": exp,
            "floor": floor,
            "ceil": ceil,
            "done": done,
            "span": span,
            "fraction": fraction,
            "stages": [(r.title, exp >= r.exp) for r in LEVELS],
        }

    # ---------- 写 ----------

    def add(self, exp: int, reason: str = "") -> dict[str, Any]:
        """加经验，顺手判升级。返回这次发生了什么（升级时 level_up=True）。

        经验值以**算出来的当前等级**为准往上加，而不是拿库里存的 level 往上加——
        等级本来就是经验的函数，两个都存只是为了少算一次，不该有两套真相。
        """
        amount = int(exp)
        if amount <= 0:
            current = self.state()
            return {"gained": 0, "level": current["level"], "level_up": False,
                    "new_unlocks": [], "title": current["title"]}
        raw = self._store.intimacy()
        before = rule_for_exp(int(raw.get("exp", 0)))
        total = int(raw.get("exp", 0)) + amount
        after = rule_for_exp(total)

        unlocked = list(after.unlocks)
        self._store.save_intimacy(after.level, total, unlocked, self._stamp())
        gained_unlocks = [f for f in after.unlocks if f not in before.unlocks]
        result = {
            "gained": amount,
            "level": after.level,
            "level_up": after.level > before.level,
            "new_unlocks": gained_unlocks,
            "title": after.title,
        }
        if result["level_up"]:
            logger.info("好感度升级：%s（L%d，累计 %d 经验）%s",
                        after.title, after.level, total, reason or "")
        return result

    # ---------- 等级福利 ----------

    def tone_suffix(self) -> str:
        """叠到人设层尾巴上的一段语气提示。还没到"熟络"就是空串，一个字都不加。"""
        state = self.state()
        if "closer_tone" not in state["unlocked"]:
            return ""
        lines = [_TONE_CLOSER]
        if "share_topic" in state["unlocked"]:
            lines.append(_TONE_SHARE)
        if "confide" in state["unlocked"]:
            lines.append(_TONE_CONFIDE)
        if "nickname" in state["unlocked"]:
            # 到挚友这一档才提私密称呼；用户没给过昵称就不提，改说最亲近那档的语气
            name = (self._nickname() if self._nickname is not None else "").strip()
            lines.append(_nickname_line(name) if name else _TONE_INMOST)
        return "\n".join(lines)

    def ask_back_hint(self, route: Any, text: str) -> str:
        """他拿不定主意来问你时，注入一句"反问要他的主意"（L4+ 的 ask_back）。

        触发点是两处一起判：意图走路由（求助），犹豫走文本词表——路由没有
        "犹豫"这一类，也不打算为它加一类（那会动到五类意图的优先级）。
        没解锁、或者这句话不是在拿主意，都返回空串，一个字都不注入。
        """
        if not self.has("ask_back"):
            return ""
        intent = getattr(route, "intent", "")
        if intent != router.INTENT_HELP and not router.is_hesitating(text):
            return ""
        return _ASK_BACK_HINT

    def nickname(self) -> str:
        """用户给的那个私密称呼（没给过就是空串）。"""
        return (self._nickname() if self._nickname is not None else "").strip()

    def pace(self) -> tuple[int, int] | None:
        """主动搭话的节流 (最短间隔秒, 每日上限)。没解开 frequent 就返回 None，
        让调用方原样用 life/proactive 里的默认值。"""
        if not self.has("frequent"):
            return None
        return FREQUENT_MIN_GAP_SEC, FREQUENT_MAX_PER_DAY

    # ---------- 内部 ----------

    def _next_rule(self, level: int) -> LevelRule | None:
        for rule in LEVELS:
            if rule.level > level:
                return rule
        return None

    def _stamp(self) -> str:
        return self._now().isoformat(timespec="seconds")
