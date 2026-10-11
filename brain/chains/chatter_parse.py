"""
吐嘈解析链：把一句自然语言的吐嘈，解析成 情境 / 时间 / 情绪 / 意图 / 关联子模块 / 让宠物做的事。

只做结构化抽取，不负责回话：回话由 life/psych 的角色卡（心理医生「林医生」）单独生成，
两件事分开，解析要的是准，回话要的是有温度。

时间处理规则（对应方案第 8 节的"模糊时间反问"）：
- 能精确到几点 → time_confidence=explicit，直接建提醒；
- 说法模糊（下班后/晚上/改天）→ fuzzy，由宠物反问确认；
- 解析出的时刻已经过去 → 也按 fuzzy 处理，让用户确认，避免建一条永远不触发的提醒。

问谁是可切的（A+++ 本地优先，跟 A++ 的 distill 一个套路）：本机 ``data/models/`` 里
放着量化模型就**在本地算**，这句对话原文一个字节都不出本机；没有模型（或本地这趟没
跑成）就**自动回落 API**，行为跟以前一模一样。切换点全在 ``_ask`` 一个方法里。

**这一次调用就是"入口那一次"**：每句话只有它跑模型，本地有模型时它离线；
``intent`` 拿回去校正 ``life/dialog/router.py`` 的规则判断（越界回落规则值）。

发给模型的只有用户这一句话 + 当前时间，不掺屏幕内容（S7 红线）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime

from brain.llm.factory import LLMFactory
from brain.prompts.templates import (
    CHATTER_PARSE_PROMPT,
    FOLLOWUP_INTENT_PROMPT,
    SPEAKER_USER,
    now_text,
    speaker_label,
)
from core.config import Config
from life.mind import local_llm
from pet.persona import resolve_voice

logger = logging.getLogger(__name__)

_TIME_FORMATS = ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d %H:%M")

VALID_CONFIDENCE = ("explicit", "fuzzy", "none")

# 等待态下用户意图的合法取值
VALID_INTENTS = ("answer_time", "cancel", "new_topic", "unclear")
INTENT_ANSWER_TIME = "answer_time"
INTENT_CANCEL = "cancel"
INTENT_NEW_TOPIC = "new_topic"
INTENT_UNCLEAR = "unclear"

# 用户在聊天里让宠物做的事（聊天是主入口：报今日活动、隐身、开会面板、退出、陪玩）
COMMAND_ACTIVITY_REPORT = "activity_report"
COMMAND_HIDE_PET = "hide_pet"
COMMAND_OPEN_LIFE = "open_life"
COMMAND_OPEN_WORK = "open_work"
COMMAND_OPEN_SETTINGS = "open_settings"
COMMAND_QUIT = "quit"
COMMAND_START_PLAYMATE = "start_playmate"
COMMAND_STOP_PLAYMATE = "stop_playmate"

VALID_COMMANDS = (
    COMMAND_ACTIVITY_REPORT,
    COMMAND_HIDE_PET,
    COMMAND_OPEN_LIFE,
    COMMAND_OPEN_WORK,
    COMMAND_OPEN_SETTINGS,
    COMMAND_QUIT,
    COMMAND_START_PLAYMATE,
    COMMAND_STOP_PLAYMATE,
)

@dataclass
class ChatterAnalysis:
    # v2-social-card-base：这份解析是"谁说的"。默认 user（主聊天那条线），
    # 帮聊那条线传 peer——下游据此知道 intent / emotion 描述的是**对方**而不是主人。
    speaker: str = SPEAKER_USER
    scene: str = ""
    time_hint: str = ""
    time_iso: datetime | None = None
    time_confidence: str = "none"
    emotion: str = ""
    emotion_score: float = 0.0     # 负向情绪强度 0~1，卡牌阈值模型用（顺带在这次解析里打的分）
    topic: str = ""                # 主题族，如"体重"（同义近义归成一个名字）
    module: str = "其他"
    command: str = ""
    # A+++：模型判的意图（指令/倾诉/求助/闲聊/确认），拿去校正规则路由。
    # 空串 = 模型没给或给了越界值 → 调用方保留规则那一档（见 router.refine）
    intent: str = ""

    @property
    def timed(self) -> bool:
        """这句话里报时间了吗（说不准的也算：明天 / 晚上 / 回头）。

        "时间词 vs 品类词"那条优先级要看它：报了时间的句子原本整句被提醒那条线接走，
        生活大类命中也得给它让路——所以只有这种句子，品类命中才算数。
        """
        return self.time_confidence in ("fuzzy", "explicit") or bool(self.time_hint)


def parse_iso(text: str) -> datetime | None:
    """解析模型给出的时刻字符串。"""
    if not text:
        return None
    cleaned = str(text).strip().replace("T", " ")
    for fmt in _TIME_FORMATS:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue
    logger.warning("无法解析模型返回的时间: %r", text)
    return None


def parse_emotion_score(value) -> float:
    """把模型给的情绪强度夹到 0~1；给的不是数字就按 0（平静）算。"""
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return min(max(score, 0.0), 1.0)


class ChatterParseChain:
    def __init__(self, config: Config):
        self._config = config
        # 最近一趟解析是在本地算的还是回落的 API（设置页提示 / 测试断言用）
        self._last_used_local = False

    @property
    def used_local(self) -> bool:
        """最近一趟解析是不是在本地算的（False = 回落了 API）。"""
        return self._last_used_local

    def _ask(self, prompt: str, system: str) -> dict:
        """这一趟问谁：本机有量化模型就在本地算，没有就照旧走 API。

        A+++ 本地优先（跟 A++ 的 distill 同一套）。**只换"调谁"**——两边回的 JSON
        走的是同一段解析，后面那串字段一个字都不动。

        本地跑砸了（权重换了一半、内存不够、吐出来不是 JSON）不往上抛：在这儿当成
        "本机这趟不行"，回落 API。回落是**预期内**的常态，不是故障。

        本地这条腿只喂 prompt、不带 system：小模型对长提示更敏感，而解析要的是
        结构化抽取，人设那层留白反而更稳（system 里主要是语气，结构不靠它）。
        """
        if self._config.mind.local_model:
            try:
                data = local_llm.infer(self._config.local_models_dir, prompt)
            except local_llm.LocalUnavailable as exc:
                logger.info("吐嘈解析：本机这次没用上（%s），回落 API", exc)
            else:
                self._last_used_local = True
                return data
        self._last_used_local = False
        return LLMFactory.create(self._config).chat_json(prompt, system=system)

    def analyze(self, text: str, speaker: str = SPEAKER_USER) -> ChatterAnalysis:
        """解析一条吐嘈。

        ``speaker``（v2-social-card-base）：这句话是谁说的。默认 ``"user"``——主聊天那条线
        一个字节都不改（提示词里那两处仍拼出"用户"）；帮聊那条线传 ``"peer"``，拼出来的
        提示词把主语换成"对方"，同一套抽取规则用在**对方**那句话上。
        """
        now, weekday = now_text()
        prompt = CHATTER_PARSE_PROMPT.format(
            now=now,
            weekday=weekday,
            text=text,
            speaker=speaker_label(speaker),
        )
        system = resolve_voice(self._config.pet).system_prompt(self._config.pet.name)
        data = self._ask(prompt, system)

        confidence = str(data.get("time_confidence") or "none").strip().lower()
        if confidence not in VALID_CONFIDENCE:
            confidence = "none"

        command = str(data.get("command") or "").strip().lower()
        if command not in VALID_COMMANDS:
            command = ""      # 模型偶尔会自己造名字，认不出来的当没这条命令

        analysis = ChatterAnalysis(
            speaker=str(speaker or SPEAKER_USER).strip().lower() or SPEAKER_USER,
            scene=str(data.get("scene") or "").strip(),
            time_hint=str(data.get("time_hint") or "").strip(),
            time_iso=parse_iso(data.get("time_iso")),
            time_confidence=confidence,
            emotion=str(data.get("emotion") or "").strip(),
            emotion_score=parse_emotion_score(data.get("emotion_score")),
            topic=str(data.get("topic") or "").strip(),
            module=str(data.get("module") or "其他").strip(),
            command=command,
            # 越界值**不在这儿判**：归一与回落都归 router.refine 管，
            # 这儿原样带出去，下游一眼能看出模型到底说了什么
            intent=str(data.get("intent") or "").strip(),
        )

        # 声称精确但解析不出来，或时刻已经过去 → 退回模糊，交给反问确认
        if analysis.time_confidence == "explicit":
            if analysis.time_iso is None or analysis.time_iso <= datetime.now():
                analysis.time_confidence = "fuzzy"
                analysis.time_iso = None

        return analysis

    def classify_followup(self, time_hint: str, text: str) -> tuple[str, datetime | None]:
        """等待态下对用户的话做意图分类，返回 (intent, 时刻)。

        intent ∈ answer_time / cancel / new_topic / unclear。
        仅 answer_time 会带解析出的时刻；模型返回值非法或时刻无效时按 unclear 处理，
        让调用方走"重试"出口，避免误判导致错误建提醒或错误切走话题。
        """
        provider = LLMFactory.create(self._config)
        now, weekday = now_text()
        prompt = FOLLOWUP_INTENT_PROMPT.format(
            now=now, weekday=weekday, time_hint=time_hint or "这件事", text=text
        )
        data = provider.chat_json(
            prompt,
            system=resolve_voice(self._config.pet).system_prompt(self._config.pet.name),
        )

        intent = str(data.get("intent") or INTENT_UNCLEAR).strip().lower()
        if intent not in VALID_INTENTS:
            logger.warning("意图分类返回非法值 %r，按 unclear 处理", intent)
            intent = INTENT_UNCLEAR

        moment = parse_iso(data.get("time_iso"))
        if intent == INTENT_ANSWER_TIME:
            # answer_time 必须给出未来的具体时刻，否则按"没听清"重试
            if moment is None or moment <= datetime.now():
                intent = INTENT_UNCLEAR
                moment = None
        else:
            moment = None
        return intent, moment
