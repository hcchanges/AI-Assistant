"""线下约会教练规则（v2-date-knowledge）：把约会素材内化成纯规则，供后续批次检索。

这一层**只放规则，不落库、不碰线上帮聊、不碰 monitor**：

- `STAGES`：五段阶段机（破冰 → 升温 → 亲密 → 私密 → 带走过夜），每段带目标与结束标志；
- `SIGNALS`：可观察的信号（距离、膝盖朝向、对视移开不后撤、喂食三阶、主动肢体、
  主动分享、情绪投入度、是否后撤）；
- `TESTS`：可做的升级测试（指甲、过马路牵手、咖啡厅擦泡沫、甜品店对视索吻、影院手放腿上）；
- `LINES`：破冰 / 转场与冷场假性终结 / 玩笑梗 / 邀约四类话术；
- `REDLINES`：禁忌与刹车规则。

调用口子是 `get_prompt(stage, signal_snapshot)`：给定当前阶段与这一拍的信号快照，
回一条**给用户的教练提示**（说什么、现在能不能测试升级、需要注意哪些禁忌）。
它不替用户说话，也不替用户做动作——只提示候选。

**边界**：本模块不保存任何会话、不发网络、不读写数据库。素材里提到的"写回同一张卡"
放到 v2-date-card-writeback；Jev 判定放到 v2-date-jev-pipe。这里的信号全部由调用方
从结构化 state 里交进来，不收原始帧、不收原始音频。

**刹车优先于推进**：任何一步只要对方后撤、明确拒绝或含糊犹豫，直接停——这条写在
`REDLINE_CONSENT` 里，并且在 `eligible_tests` / `get_prompt` 里是硬闸，不是建议。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ---------- 阶段机 ----------

STAGE_ICEBREAK = "icebreak"
STAGE_WARMUP = "warmup"
STAGE_INTIMATE = "intimate"
STAGE_PRIVATE = "private"
STAGE_OVERNIGHT = "overnight"

STAGE_ORDER: tuple[str, ...] = (
    STAGE_ICEBREAK,
    STAGE_WARMUP,
    STAGE_INTIMATE,
    STAGE_PRIVATE,
    STAGE_OVERNIGHT,
)

STAGE_TITLES: dict[str, str] = {
    STAGE_ICEBREAK: "破冰",
    STAGE_WARMUP: "升温",
    STAGE_INTIMATE: "亲密",
    STAGE_PRIVATE: "私密",
    STAGE_OVERNIGHT: "带走过夜",
}


@dataclass(frozen=True)
class Stage:
    """一个约会阶段：在哪里、要什么、什么时候算走完、下一步去哪。"""

    key: str
    name: str
    venue: str
    goal: str
    end_markers: tuple[str, ...]
    typical_minutes: tuple[int, int] | None
    next_key: str | None


STAGES: dict[str, Stage] = {
    STAGE_ICEBREAK: Stage(
        key=STAGE_ICEBREAK,
        name="破冰",
        venue="见面地点 / 咖啡厅",
        goal="先让自己放松，像见老朋友一样；聊约会地点、她的朋友圈、真实情绪，"
             "把安全感与舒适度给够。破冰阶段不升级。",
        end_markers=(
            "她主动延续话题",
            "她主动分享自己和朋友的故事",
            "表达欲明显变强",
        ),
        typical_minutes=(15, 20),
        next_key=STAGE_WARMUP,
    ),
    STAGE_WARMUP: Stage(
        key=STAGE_WARMUP,
        name="升温",
        venue="餐馆",
        goal="把话题从大众话题收到两个人身上：聊经历、听她说，让约会越来越 personal。",
        end_markers=(
            "晚餐话题已围绕两个人",
            "她愿意讲更深的经历",
        ),
        typical_minutes=(45, 90),
        next_key=STAGE_INTIMATE,
    ),
    STAGE_INTIMATE: Stage(
        key=STAGE_INTIMATE,
        name="亲密",
        venue="甜品店",
        goal="坐近一点、说话看眼睛；分享人生感悟与情绪，等气氛到了再吻。",
        end_markers=(
            "对视几秒后移开视线，但身体没有后撤",
            "她主动靠近或主动制造肢体接触",
        ),
        typical_minutes=(30, 60),
        next_key=STAGE_PRIVATE,
    ),
    STAGE_PRIVATE: Stage(
        key=STAGE_PRIVATE,
        name="私密",
        venue="私人影院",
        goal="安静环境里自然进挪：每一步先看反馈，不反抗才继续；"
             "她挣脱就退回来，别硬来。",
        end_markers=(
            "双方对下一步的意愿明确且自愿",
            "她明确表示愿意继续",
        ),
        typical_minutes=(60, 120),
        next_key=STAGE_OVERNIGHT,
    ),
    STAGE_OVERNIGHT: Stage(
        key=STAGE_OVERNIGHT,
        name="带走过夜",
        venue="共同决定",
        goal="确认双方都愿意、边界清楚、安全有保障；任何犹豫、拒绝或含糊都停。",
        end_markers=(),
        typical_minutes=None,
        next_key=None,
    ),
}


def normalize_stage(stage: Any) -> str:
    """阶段名归一：认不出就退回破冰（宁可少推进，也不凭空跳段）。"""
    key = str(stage or "").strip().lower()
    if key in STAGES:
        return key
    for name, item in STAGES.items():
        if str(stage or "").strip() == item.name:
            return name
    return STAGE_ICEBREAK


def stage_index(stage: Any) -> int:
    return STAGE_ORDER.index(normalize_stage(stage))


def stage_ready_to_advance(stage: Any, signal_snapshot: Mapping[str, Any] | None = None) -> bool:
    """这一阶段算不算走完了。只看调用方交进来的结构化信号，不猜。"""
    key = normalize_stage(stage)
    snap = normalize_snapshot(signal_snapshot)
    if key == STAGE_ICEBREAK:
        return bool(snap.get("she_extends_topic")) or _level(snap.get("sharing")) >= 0.6
    if key == STAGE_WARMUP:
        return bool(snap.get("meal_done")) or _number(snap.get("elapsed_minutes")) >= 45
    if key == STAGE_INTIMATE:
        return (
            bool(snap.get("gaze_away_no_retreat"))
            and _number(snap.get("distance")) >= 0.6
        ) or bool(snap.get("kiss_done"))
    if key == STAGE_PRIVATE:
        return bool(snap.get("mutual_yes")) or bool(snap.get("overnight_yes"))
    return False


def next_stage(stage: Any, signal_snapshot: Mapping[str, Any] | None = None) -> str | None:
    """满足结束标志 → 下一阶段 key；没满足 → None（留在本阶段）。"""
    key = normalize_stage(stage)
    if not stage_ready_to_advance(key, signal_snapshot):
        return None
    return STAGES[key].next_key


# ---------- 信号 ----------

FEEDING_LEVELS: dict[str, int] = {
    "own_utensil": 0,      # 用自己的餐具接住你给的东西：普通朋友
    "your_utensil": 1,     # 接过你手上的餐具：比一般朋友好一点
    "direct": 2,           # 直接吃你给的东西：好感已经很强
}
_FEEDING_ALIASES = {
    "own": "own_utensil",
    "self": "own_utensil",
    "普通": "own_utensil",
    "yours": "your_utensil",
    "hand": "your_utensil",
    "稍好": "your_utensil",
    "直接": "direct",
    "eat": "direct",
}
_LEVEL_WORDS = {"low": 0.2, "medium": 0.5, "mid": 0.5, "high": 0.85,
                "低": 0.2, "中": 0.5, "高": 0.85}
_DISTANCE_WORDS = {"far": 0.1, "normal": 0.5, "close": 0.8, "touch": 1.0,
                   "远": 0.1, "一般": 0.5, "近": 0.8, "贴近": 1.0}


@dataclass(frozen=True)
class Signal:
    """一个可观察信号：叫什么、大概什么含义、有哪几档。"""

    key: str
    name: str
    meaning: str
    levels: tuple[str, ...] = ()


SIGNALS: dict[str, Signal] = {
    "distance": Signal(
        "distance", "并肩距离", "走路/坐着时她会不会靠近；突然坐远看她会不会挪回来",
        ("far", "normal", "close", "touch"),
    ),
    "knees_toward": Signal(
        "knees_toward", "膝盖朝向", "肩并肩坐着时，膝盖会不会自觉朝向你",
    ),
    "gaze_away_no_retreat": Signal(
        "gaze_away_no_retreat", "对视移开不后撤",
        "对视几秒后她移开视线，但身体没有后撤——这是可以吻的信号",
    ),
    "feeding": Signal(
        "feeding", "喂食三阶",
        "自己餐具=普通；接你餐具=稍好；直接吃=好感强、可肢体接触",
        tuple(FEEDING_LEVELS),
    ),
    "active_touch": Signal(
        "active_touch", "主动肢体", "主动挠痒、捏胳膊、轻轻打你——趁热打铁",
    ),
    "sharing": Signal(
        "sharing", "主动分享度", "她主动讲自己的事、朋友的事，表达欲变强",
        ("low", "medium", "high"),
    ),
    "sex_talk_comfort": Signal(
        "sex_talk_comfort", "情绪投入度",
        "聊情感/性相关话题时的自然度与投入度（只看舒适度，不代替同意）",
        ("low", "medium", "high"),
    ),
    "resisted": Signal(
        "resisted", "后撤/拒绝",
        "对方明确后撤、抽手、转头、说不要——这是硬刹车",
    ),
}

SIGNAL_KEYS: tuple[str, ...] = tuple(SIGNALS)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    if number != number:          # NaN
        return float(default)
    return number


def _level(value: Any) -> float:
    """0~1 的档位值：数字照收，词表里的 low/medium/high 换成数。"""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _LEVEL_WORDS:
            return _LEVEL_WORDS[key]
    return max(0.0, min(1.0, _number(value)))


def _distance(value: Any) -> float:
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _DISTANCE_WORDS:
            return _DISTANCE_WORDS[key]
    return max(0.0, min(1.0, _number(value)))


def _as_bool(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "y", "是", "有")
    return bool(value)


def feeding_level(value: Any) -> int:
    """喂食三阶归一成 0 / 1 / 2；认不出给 -1（当没看见）。"""
    if isinstance(value, bool):
        return 2 if value else -1
    if isinstance(value, (int, float)):
        return max(0, min(2, int(value)))
    key = str(value or "").strip().lower()
    key = _FEEDING_ALIASES.get(key, key)
    return FEEDING_LEVELS.get(key, -1)


def normalize_snapshot(signal_snapshot: Mapping[str, Any] | None) -> dict[str, Any]:
    """把调用方交进来的信号收成固定几格。缺的格子不补值（保持"不知道"）。"""
    source = signal_snapshot if isinstance(signal_snapshot, Mapping) else {}
    snap: dict[str, Any] = {}
    if "distance" in source:
        snap["distance"] = _distance(source.get("distance"))
    for key in ("sharing", "sex_talk_comfort"):
        if key in source:
            snap[key] = _level(source.get(key))
    if "feeding" in source:
        snap["feeding"] = feeding_level(source.get("feeding"))
    for key in ("knees_toward", "gaze_away_no_retreat", "active_touch",
                "she_extends_topic", "meal_done", "kiss_done", "mutual_yes",
                "overnight_yes", "resisted", "silence"):
        if key in source:
            snap[key] = _as_bool(source.get(key))
    if "elapsed_minutes" in source:
        snap["elapsed_minutes"] = _number(source.get("elapsed_minutes"))
    return snap


def actions_for(signal_snapshot: Mapping[str, Any] | None) -> tuple[str, ...]:
    """信号 → 可做的动作候选（只给候选，是否执行由调用方结合阶段判断）。"""
    snap = normalize_snapshot(signal_snapshot)
    actions: list[str] = []
    if snap.get("resisted"):
        return ("对方后撤/拒绝：立即停，退回上一段节奏",)
    if snap.get("gaze_away_no_retreat") and _number(snap.get("distance")) >= 0.6:
        actions.append("可以尝试索吻")
    if snap.get("knees_toward") or _number(snap.get("distance")) >= 0.5:
        actions.append("可以尝试牵手（过马路等有正当理由的场景）")
    if feeding_level(snap.get("feeding")) >= 2 or snap.get("active_touch"):
        actions.append("身体接触的窗口已经很强，可以自然靠近")
    if _level(snap.get("sharing")) >= 0.6 or snap.get("she_extends_topic"):
        actions.append("破冰够了，可以转场")
    return tuple(actions)


# ---------- 升级测试 ----------

@dataclass(frozen=True)
class EscalationTest:
    """一个可做的升级测试：在哪个阶段、需要什么信号、怎么试、怎么收。"""

    key: str
    name: str
    stage: str
    requires: tuple[str, ...]
    action: str
    pass_signal: str
    fallback: str


TESTS: dict[str, EscalationTest] = {
    "nail": EscalationTest(
        "nail", "指甲测试", STAGE_WARMUP, (),
        "聊到指甲白色部分是健康值，伸手让她也伸出来，自然抓住她的手。",
        "没有下意识往回缩。",
        "回缩就松开，接着聊，不解释、不追问。",
    ),
    "hand_hold": EscalationTest(
        "hand_hold", "过马路牵手", STAGE_WARMUP, ("distance",),
        "走路摆臂时先碰一下她的手，再在有正当理由（过马路）时牵住。",
        "不抽回，或反过来握住。",
        "抽回就松手，用保护策略化解：'那我不牵了'，别硬拽。",
    ),
    "coffee_foam": EscalationTest(
        "coffee_foam", "咖啡厅擦泡沫 / 尝饮", STAGE_WARMUP, (),
        "点卡布奇诺或拿铁，帮她擦泡沫；第二次让她自己擦，第三次让她帮你擦。",
        "接受并配合，不躲。",
        "躲开就停在第一次，换成聊咖啡或朋友圈。",
    ),
    "kiss": EscalationTest(
        "kiss", "对视索吻", STAGE_INTIMATE,
        ("gaze_away_no_retreat", "distance"),
        "坐近、看着她；对视几秒她移开视线但不后撤时，头慢慢靠近吻过去。",
        "迎合、闭眼、不后撤。",
        "后撤、转头、推开：立刻退回来，聊回话题；不要再试第二次。",
    ),
    "hand_on_leg": EscalationTest(
        "hand_on_leg", "影院手放腿上", STAGE_PRIVATE, (),
        "影院前十分钟先看电影，聊两句剧情，再自然地把手放到她腿上。",
        "不反抗，或主动靠过来。",
        "挣脱就收回来，说'帮你暖和一下'；过一会儿最多再试一次，仍拒绝就停。",
    ),
}

TEST_ORDER: tuple[str, ...] = ("nail", "hand_hold", "coffee_foam", "kiss", "hand_on_leg")


def eligible_tests(stage: Any, signal_snapshot: Mapping[str, Any] | None = None) -> tuple[EscalationTest, ...]:
    """当前阶段 + 信号下**允许尝试**的测试。

    硬闸：
    - 破冰阶段一律不升级（`REDLINE_NO_EARLY_ESCALATION`）；
    - 对方后撤/拒绝时一律不升级（`REDLINE_CONSENT`）；
    - 索吻要求"对视移开不后撤"且距离够近；
    - 每个测试还有自己的最低阶段。
    """
    key = normalize_stage(stage)
    snap = normalize_snapshot(signal_snapshot)
    if key == STAGE_ICEBREAK or snap.get("resisted"):
        return ()
    current = stage_index(key)
    result: list[EscalationTest] = []
    for test_key in TEST_ORDER:
        test = TESTS[test_key]
        if current < stage_index(test.stage):
            continue
        if test_key == "kiss":
            if not snap.get("gaze_away_no_retreat"):
                continue
            if _number(snap.get("distance")) < 0.6:
                continue
        if test_key == "hand_hold" and "distance" in snap:
            if _number(snap.get("distance")) < 0.4 and not snap.get("knees_toward"):
                continue
        result.append(test)
    return tuple(result)


# ---------- 话术 ----------

LINE_ICEBREAK = "icebreak"
LINE_AWKWARD = "awkward"
LINE_JOKE = "joke"
LINE_INVITE = "invite"


@dataclass(frozen=True)
class Line:
    """一句候选话术。`variant` 只给邀约分型用。"""

    key: str
    kind: str
    text: str
    note: str = ""
    variant: str = ""


LINES: tuple[Line, ...] = (
    Line("icebreak_venue", LINE_ICEBREAK,
         "我对这一带还挺熟的，等会儿带你走走。你平时喜欢逛什么？",
         "聊约会地点：她熟就有共同点，她不熟就由你提供价值。"),
    Line("icebreak_moments", LINE_ICEBREAK,
         "刚翻到你朋友圈那张照片，拍得挺有意思的——怎么拍的？",
         "聊她最近的朋友圈：既表明关注，也让她有话可接。"),
    Line("icebreak_real", LINE_ICEBREAK,
         "说实话，我今天还挺期待的，也有点紧张。",
         "表达真实情绪，但不把对方当情绪垃圾桶。"),
    Line("awkward_fake_end", LINE_AWKWARD,
         "行，那今天先到这儿？我送你回去。",
         "冷场时假性提出结束；她挽留就顺势转场，不挽留就体面收场。"),
    Line("awkward_transition", LINE_AWKWARD,
         "再坐下去我怕把这家店聊穷了——走，换个地方。",
         "转场要有理由；没转场成功不要升级。"),
    Line("joke_shy", LINE_JOKE,
         "你看，我这么帅，你什么时候请我吃个饭？",
         "卖萌/自恋的玩笑，用来松氛围，不用于逼邀约。"),
    Line("joke_tease", LINE_JOKE,
         "说好一起到白头，你却悄悄焗了油。",
         "耍贱玩梗，接得住就继续，接不住就换回正常聊天。"),
    Line("invite_vague", LINE_INVITE,
         "有空一起去逛逛？",
         "模糊邀约：先探窗口，被拒成本最低。", "vague"),
    Line("invite_seed", LINE_INVITE,
         "那家店二楼靠窗的位置特别舒服，改天带你去坐坐。",
         "播种邀约：先铺画面与预期，不当场要答复。", "seed"),
    Line("invite_formal", LINE_INVITE,
         "周六下午三点，我们在××咖啡见。",
         "正式邀约要正经：时间、地点、人物说清楚。", "formal"),
    Line("invite_stop", LINE_INVITE,
         "OK，那下次见。",
         "止损：被拒就爽快收，不纠缠、不连环邀。", "stop"),
)


def lines_for(kind: str | None = None) -> tuple[Line, ...]:
    """按类别取话术；不传就全给。"""
    if not kind:
        return LINES
    return tuple(line for line in LINES if line.kind == kind)


# ---------- 禁忌 ----------

REDLINE_NO_EARLY_ESCALATION = "no_early_escalation"
REDLINE_INVITE_TIMING = "invite_timing"
REDLINE_NO_CLINGING = "no_clinging"
REDLINE_NO_LOOKS_COMMENTARY = "no_looks_commentary"
REDLINE_NO_ILLOGICAL_MOVES = "no_illogical_moves"
REDLINE_SHY_START_LIGHT = "shy_start_light"
REDLINE_NOT_EMOTIONAL_DUMP = "not_emotional_dump"
REDLINE_CONSENT = "consent_stop"


@dataclass(frozen=True)
class Redline:
    key: str
    rule: str


REDLINES: tuple[Redline, ...] = (
    Redline(REDLINE_NO_EARLY_ESCALATION,
            "没有成功转场，不要过度升级关系；破冰阶段只建立舒适度。"),
    Redline(REDLINE_INVITE_TIMING,
            "邀约不要过早、不要过频；被拒三次就停下，不形成追问阶梯。"),
    Redline(REDLINE_NO_CLINGING,
            "约会结束不纠缠：问一句'你待会儿还有事吗'，有事就让她走。"),
    Redline(REDLINE_NO_LOOKS_COMMENTARY,
            "不要对妆容、穿搭评头论足；想夸就具体地夸，不想夸就别说。"),
    Redline(REDLINE_NO_ILLOGICAL_MOVES,
            "不要做没有逻辑、突兀的动作；每个接触都要有当下的理由。"),
    Redline(REDLINE_SHY_START_LIGHT,
            "害羞内敛的女生从最轻的一档开始：手背、手腕、肩膀，别一上来就牵手拥抱。"),
    Redline(REDLINE_NOT_EMOTIONAL_DUMP,
            "表达情绪要适度，不要把对方当情绪垃圾桶；真实不等于倾倒负能量。"),
    Redline(REDLINE_CONSENT,
            "对方后撤、抽手、转头、说不要，立即停；犹豫和含糊也按拒绝处理。"),
)

REDLINE_BY_KEY: dict[str, Redline] = {item.key: item for item in REDLINES}


# ---------- 检索口 ----------


@dataclass(frozen=True)
class CoachPrompt:
    """一条给用户的教练提示：说什么、能不能测试、要注意什么。"""

    stage: str
    stage_name: str
    suggestion: str
    can_test: bool
    test: EscalationTest | None
    taboos: tuple[str, ...]
    next_hint: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "stage_name": self.stage_name,
            "suggestion": self.suggestion,
            "can_test": self.can_test,
            "test": self.test.key if self.test is not None else None,
            "taboos": list(self.taboos),
            "next_hint": self.next_hint,
        }


def _pick_line(stage: str, snap: Mapping[str, Any]) -> Line:
    if snap.get("silence"):
        return _line(LINE_AWKWARD, "awkward_fake_end")
    if stage == STAGE_ICEBREAK:
        if _level(snap.get("sharing")) < 0.4:
            return _line(LINE_ICEBREAK, "icebreak_venue")
        if snap.get("she_extends_topic") or _level(snap.get("sharing")) >= 0.6:
            return _line(LINE_ICEBREAK, "icebreak_real")
        return _line(LINE_ICEBREAK, "icebreak_moments")
    if stage == STAGE_WARMUP:
        return _line(LINE_INVITE, "invite_seed")
    if stage == STAGE_INTIMATE:
        return _line(LINE_ICEBREAK, "icebreak_real")
    if stage == STAGE_PRIVATE:
        return _line(LINE_AWKWARD, "awkward_transition")
    return _line(LINE_INVITE, "invite_stop")


def _line(kind: str, key: str) -> Line:
    for line in LINES:
        if line.key == key:
            return line
    return lines_for(kind)[0]


def _best_test(tests: tuple[EscalationTest, ...],
               snap: Mapping[str, Any]) -> EscalationTest | None:
    """从允许的测试里挑**这一拍最贴合**的一个：有索吻信号就优先索吻，
    私密阶段优先影院测试，其余按亲近程度往下排。"""
    if not tests:
        return None
    by_key = {test.key: test for test in tests}
    if "kiss" in by_key:
        return by_key["kiss"]
    if "hand_on_leg" in by_key:
        return by_key["hand_on_leg"]
    if "hand_hold" in by_key and (_number(snap.get("distance")) >= 0.4
                                  or snap.get("knees_toward")):
        return by_key["hand_hold"]
    for key in TEST_ORDER:
        if key in by_key:
            return by_key[key]
    return tests[0]


def _taboo_keys(stage: str, snap: Mapping[str, Any], can_test: bool) -> tuple[str, ...]:
    keys: list[str] = []
    if stage == STAGE_ICEBREAK:
        keys.append(REDLINE_NO_EARLY_ESCALATION)
    if snap.get("resisted"):
        keys.append(REDLINE_CONSENT)
    elif can_test:
        keys.append(REDLINE_NO_ILLOGICAL_MOVES)
        keys.append(REDLINE_CONSENT)
    if snap.get("silence"):
        keys.append(REDLINE_NO_CLINGING)
    if _level(snap.get("sharing")) >= 0.6:
        keys.append(REDLINE_NOT_EMOTIONAL_DUMP)
    keys.append(REDLINE_NO_LOOKS_COMMENTARY)
    # 去重、保持稳定顺序
    seen: list[str] = []
    for key in keys:
        if key not in seen:
            seen.append(key)
    return tuple(seen)


def get_prompt(stage: Any, signal_snapshot: Mapping[str, Any] | None = None) -> CoachPrompt:
    """线下约会的主检索口：阶段 + 信号快照 → 教练提示候选。

    - `suggestion`：这一拍适合说什么（一句候选，不是逐字稿）；
    - `can_test` / `test`：现在能不能做升级测试、做哪一个；
    - `taboos`：相关禁忌提醒（含"未转场不升级"与"后撤就停"两道硬闸）；
    - `next_hint`：还差什么才能转场。
    """
    key = normalize_stage(stage)
    snap = normalize_snapshot(signal_snapshot)
    stage_def = STAGES[key]
    line = _pick_line(key, snap)
    tests = eligible_tests(key, snap)
    test = _best_test(tests, snap)
    taboos = tuple(REDLINE_BY_KEY[item].rule for item in _taboo_keys(key, snap, test is not None))
    if stage_ready_to_advance(key, snap) and stage_def.next_key:
        next_hint = f"这一段的结束标志已经出现，可以自然转场到「{STAGES[stage_def.next_key].name}」。"
    else:
        next_hint = "先看结束标志：" + "；".join(stage_def.end_markers) if stage_def.end_markers else "由双方共同确认下一步。"
    return CoachPrompt(
        stage=key,
        stage_name=stage_def.name,
        suggestion=line.text,
        can_test=test is not None,
        test=test,
        taboos=taboos,
        next_hint=next_hint,
    )


__all__ = [
    "CoachPrompt",
    "EscalationTest",
    "FEEDING_LEVELS",
    "LINES",
    "Line",
    "REDLINES",
    "REDLINE_BY_KEY",
    "REDLINE_CONSENT",
    "REDLINE_NO_EARLY_ESCALATION",
    "SIGNALS",
    "SIGNAL_KEYS",
    "STAGES",
    "STAGE_ICEBREAK",
    "STAGE_INTIMATE",
    "STAGE_ORDER",
    "STAGE_OVERNIGHT",
    "STAGE_PRIVATE",
    "STAGE_TITLES",
    "STAGE_WARMUP",
    "Signal",
    "Stage",
    "TESTS",
    "TEST_ORDER",
    "actions_for",
    "eligible_tests",
    "feeding_level",
    "get_prompt",
    "lines_for",
    "next_stage",
    "normalize_snapshot",
    "normalize_stage",
    "stage_index",
    "stage_ready_to_advance",
]
