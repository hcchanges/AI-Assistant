"""
主动服务的意图判定（指令 26-B）：这句话该不该让我做点什么。

三条线，都要"词表对上了"才动手：
- 工作：work_words（手上在做的东西）+ action_words（任务动作：要做完 / 要交 / 赶时间）都命中 → 问一句要不要开工作模块；
- 生活：八个大类的词表（沿用 card.categories）命中，**而且**话里有求助信号（help_words）
  或者报了时间（timed）→ 直接弹那张卡；
- 其余一律不动：只按 26-A 安抚。吃饭、天气这类闲聊过不了第二层，所以零触发。

显式提醒词（remind_words：提醒我 / 定闹钟 / 喊我）例外优先：命中了就不判生活意图，
把这句话还给"建提醒"那条线——用户明说要提醒，别拿卡去顶它。

生活那条线为什么要给"报了时间"的句子单开一道口子：这种句子原先整句被提醒那条线接走
（"明天见客户没衣服穿愁死"只换来一句"明天大概几点"），生活里的事根本轮不上。
"明天"是这件事的时限，不是用户要提醒，它不该压过品类词——但这道口子只开给这种句子：
"加班好累"这种没报时间、也没求助信号的吐槽，仍旧只安抚，不拿卡去烦人。
误报还有几道闸兜着：渐进授权（先问三次）、拒绝后静默、一场只动一次手。

工作优先于出行："赶时间做 BOM" 与 "赶时间见客户" 里都有"赶时间"，区别只在提没提到活儿——
提了就先问工作模块，没提才弹出行卡；只要句子里有工作实体词，就不走生活那条线。

窄口径的模型兜底：本地两层都没命中、但话里有求助信号而且带情绪时，请模型判一次
（工作 / 生活哪一类 / 都不是，外加一个"是不是特别急"），置信度不到 0.7 就什么都不做。

"特别紧急"是生活那条线上的一档：命中 urgent_words（或模型说它急）就跳过渐进授权，
不管拒绝后的静默、也不再问一句，直接把卡翻到眼前——人真急了的时候，
"要不要翻出来看看"这种问句是添乱。
"""

from __future__ import annotations

from dataclasses import dataclass

from core.config import Config
from life.cards.classify import CardClassifier
from life.profile.extract import has_travel_destination

WORK = "work"      # 工作上的任务动作 → 先问再开工作模块
LIFE = "life"      # 生活里的急事 + 有对应的卡 → 直接弹卡
NONE = "none"      # 不动（只安抚）
TRAVEL_THEME = "出行"   # 出行意图的主题名（与 card.categories 的键、CardService.TRIP_THEME 一致）

# 模型兜底时要到的把握（指令 26-B：低于它不动作）
FALLBACK_CONFIDENCE = 0.7


@dataclass(frozen=True)
class Intent:
    """这一句想让我做什么。"""

    kind: str = NONE
    theme: str = ""      # 生活那条线才有：八个大类之一
    urgent: bool = False  # 特别紧急（只对生活那条线有意义）：不管静默、不问，直接弹

    @property
    def actionable(self) -> bool:
        return self.kind in (WORK, LIFE)


class IntentJudge:
    def __init__(self, config: Config, classifier: CardClassifier):
        self._work = tuple(config.intent.work_words)
        self._action = tuple(config.intent.action_words)
        self._help = tuple(config.intent.help_words)
        self._remind = tuple(config.intent.remind_words)
        self._urgent = tuple(config.intent.urgent_words)
        self._classifier = classifier      # 八个大类的词表在它那儿（与卡牌判定同一份）

    # ---------- 本地判定 ----------

    def judge(self, text: str, timed: bool = False) -> Intent:
        """本地能定的就本地定，定不出来返回 NONE（要不要请模型看，见 needs_fallback）。

        timed 是"这句话报了时间"（解析链给的 ChatterAnalysis.timed）：报了时间的句子
        生活品类才算数，因为它本来会被提醒那条线整句接走。

        工作优先：句子里提了工作上的东西（BOM / 方案 / 报告…），
        哪怕后面跟着"快要迟到了""怎么办"这种急话，也按工作算——
        主语是那件活儿，赶路只是它的时限。反过来，没提活儿、只有出行词的，
        才走生活那条线（"见客户快要迟到了"照旧判出行）。

        工作之下分两层：显式提醒词 → 判不出（还给提醒那条线）；品类词命中 + 有求助信号
        或报了时间 → 生活。第二层为什么给"报了时间"开口子："明天见客户没衣服穿愁死"
        这种只有事、没有"怎么办"的句子原先一律判 NONE，然后被提醒那条线用一句
        "明天大概几点"接走。口子不开给普通吐槽（"加班好累"照旧只安抚）。
        """
        work_thing = _has(text, self._work)
        if work_thing and _has(text, self._action):
            return Intent(WORK)
        if _has(text, self._remind):
            return Intent(NONE)
        match = self._classifier.local_match(text)
        # 出行多一道"报目的地"的口子：词表里都是摩擦词（堵车 / 迟到 / 机场），
        # "去上海 / 大后天跑上海"这种直接报目的地的说法抓不到，补在这里。
        # 仍受同一道闸管着：报了时间（timed）或有求助信号才算数，平铺直叙不弹卡。
        travel = match is None and not work_thing and has_travel_destination(text)
        if (match is not None or travel) and not work_thing and (timed or _has(text, self._help)):
            theme = match.category if match is not None else TRAVEL_THEME
            return Intent(LIFE, theme=theme, urgent=_has(text, self._urgent))
        return Intent(NONE)

    def needs_fallback(self, text: str, emotion_score: float) -> bool:
        """本地没判出来，但这句值不值得请模型再看一眼。

        条件写得窄：话里有求助信号、而且带着情绪才问模型——生词加急事（"帮我把那个弄一下"）
        本地认不出，这种才需要兜底；平铺直叙的话请模型也是白花钱。

        提到工作上的东西的一律不问模型：它一看见"赶时间""快迟到"就往出行上归，
        这正是要拦住的那类误判（宁可这一句不动手，也别弹错卡）。

        显式提醒词同样不问：用户明说了要提醒，模型再判成"该弹张卡"也不该顶掉它。
        """
        if self.judge(text).actionable:
            return False
        if _has(text, self._work):
            return False
        if _has(text, self._remind):
            return False
        return emotion_score > 0 and _has(text, self._help)

    def from_model(self, kind: str, theme: str, confidence: float, urgent: bool = False) -> Intent:
        """把模型给的判定收进来（置信度不够就当没说过）。"""
        if confidence < FALLBACK_CONFIDENCE:
            return Intent(NONE)
        if kind == WORK:
            return Intent(WORK)
        if kind == LIFE and theme in self._classifier.categories:
            return Intent(LIFE, theme=theme, urgent=bool(urgent))
        return Intent(NONE)


def _has(text: str, words: tuple[str, ...]) -> bool:
    """text 里有没有这些词里的任何一个。

    大小写不敏感：用户常把小写「bom」直接打出来，词表里存的是大写「BOM」，
    两者都得算命中——只把两边都转小写再比，中文不受影响。
    """
    lowered = text.lower()
    return any(word and word.lower() in lowered for word in words)
