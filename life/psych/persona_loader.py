"""
角色卡加载器（对应指令 11 心理安抚角色卡）。

一张卡 = 一套专业框架：人设 + 世界观 + 口吻 + 回应准则 + 禁忌 + few-shot 示范 + 生成参数 + 安全策略。
数据放在 life/psych/personas/*.json：换角色、调腔调都只改 JSON，代码不动。

与性格系统（指令 3）的关系：角色卡负责"专业框架"（怎么共情、什么禁忌），
pet/persona.py 的 persona_style_layer 负责"表层腔调"（软萌 / 高冷…），两者叠加。
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# 角色卡存放目录
PERSONA_DIR = Path(__file__).resolve().parent / "personas"

# 索引文件：列出这个模块有哪些卡
INDEX_FILE = "index.json"

# 聊天默认用哪张卡：宠物本体就是心理医生「林医生」（指令 11 / 指令 14）
DEFAULT_CHAT_PERSONA = "counselor_lin"

# 命理卡「玄机先生」：玄学页上说话的那位，也是唯一能产玄学卡的角色（指令 14 / 指令 24）
METAPHYSICS_PERSONA = "metaphysics_master"

# 命理类角色（可以自己产卡），对应角色卡 JSON 里的 role_type
ROLE_METAPHYSICS = "metaphysics"

# 生活卡的专业角色（指令 27）：出行 / 饮食两张预置卡各配一位，
# 这两类是"信息型"角色，跟命理 / 心理一样只负责怎么把话说专业，不产卡。
ROLE_GUIDE = "guide"           # 出行卡：导游
ROLE_NUTRITION = "nutrition"   # 饮食卡：营养师兼主厨

# 出行卡的专业角色：导游
TOUR_GUIDE_PERSONA = "tour_guide"

# 饮食卡的专业角色：营养师兼五星主厨（一个人格，一段话里两副视角）
NUTRITION_CHEF_PERSONA = "nutrition_chef"

# 卡牌"陪执行角色"下拉里，表示宠物本体那一项
BODY_LABEL = "宠物本体"


class PersonaError(Exception):
    """角色卡缺失或格式不对。"""


@dataclass(frozen=True)
class PersonaCard:
    """一张角色卡。"""

    id: str
    name: str
    module: str                            # 归属模块，如 life.psych
    role_type: str                         # metaphysics（命理）/ counselor（心理）
    system_prompt: str                     # 人设 + 世界观 + 口吻 + 准则 + 禁忌
    few_shot: tuple[dict[str, str], ...]   # 示范对话：[{user, assistant}, ...]
    temperature: float
    top_p: float
    max_tokens: int
    safety: str                            # 该角色的安全策略（如危机 SOP）

    def system_with_style(self, style_layer: str) -> str:
        """专业框架 + 安全策略 + 表层腔调，拼成 system。

        卡牌"陪执行角色"也用这一套（life/cards 会把性格层传进来），
        所以两种场景拼出来的 system 完全一致，不会各写一份。
        """
        return (
            f"【是谁在说】\n{style_layer}\n\n"
            f"【专业框架（优先级高于上面那段脾气，先按它回应，再带脾气）】\n{self.system_prompt}\n\n"
            f"【安全策略】\n{self.safety}"
        )

    def messages(
        self,
        user_text: str,
        style_layer: str,
        memory_text: str,
        comfort_hint: str = "",
        kb_text: str = "",
    ) -> list[dict[str, str]]:
        """拼出这次要交给模型的消息。

        顺序：表层腔调 → 专业框架 → few-shot 示范 → 这次的语气分寸（指令 26-A）
        → 翻出来的旧记录（v2-rag）→ 关于用户的记忆 → 这次说的话。
        专业框架放在腔调之后，是因为模型对越靠后的指令越服从；顺序如果反过来，
        "毒舌损友"这种性格会把"先共情、再正常化"的准则冲掉（真调模型实测过）。
        few-shot 放在框架后面，模型照着示范学腔调；记忆紧挨着用户这句话，回应才"记得你"。
        语气分寸紧跟在 few-shot 后头：它是这一句话的语气标尺，比示范更贴近当下，
        但也不能压过记忆与用户原话。
        kb_text 是"问起自己记过的事时翻出来的原文"（v2-rag），贴在记忆前面——
        两块都是"翻出来的东西"，挨着放；但记忆那条是概括、这条是原话，
        越像原话的越该离用户这句话近，所以 kb 在前、记忆在后。
        """
        messages: list[dict[str, str]] = [
            {"role": "system", "content": self.system_with_style(style_layer)}
        ]
        for shot in self.few_shot:
            messages.append({"role": "user", "content": shot["user"]})
            messages.append({"role": "assistant", "content": shot["assistant"]})
        if comfort_hint:
            messages.append({"role": "system", "content": comfort_hint})
        if kb_text:
            messages.append({"role": "system", "content": kb_text})
        messages.append(
            {"role": "system", "content": f"你记得关于用户的事（可能为空）：\n{memory_text}"}
        )
        messages.append({"role": "user", "content": user_text})
        return messages


def _read_json(path: Path) -> dict:
    """读一张卡的 JSON。"""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise PersonaError(f"读不到角色卡 {path.name}：{exc}") from exc
    except json.JSONDecodeError as exc:
        raise PersonaError(f"角色卡 {path.name} 不是合法 JSON：{exc}") from exc


def _to_card(data: dict) -> PersonaCard:
    """把 JSON 字典转成角色卡。"""
    params = data.get("params") or {}
    return PersonaCard(
        id=str(data.get("id") or "").strip(),
        name=str(data.get("name") or "").strip(),
        module=str(data.get("module") or "").strip(),
        role_type=str(data.get("role_type") or "").strip(),
        system_prompt=str(data.get("system_prompt") or "").strip(),
        few_shot=tuple(
            {
                "user": str(shot.get("user") or ""),
                "assistant": str(shot.get("assistant") or ""),
            }
            for shot in (data.get("few_shot") or [])
        ),
        temperature=float(params.get("temperature", 0.8)),
        top_p=float(params.get("top_p", 0.9)),
        max_tokens=int(params.get("max_tokens", 512)),
        safety=str(data.get("safety") or "").strip(),
    )


def load_persona(persona_id: str = DEFAULT_CHAT_PERSONA) -> PersonaCard:
    """按 id 加载一张角色卡（文件名就是 id）。"""
    card = _to_card(_read_json(PERSONA_DIR / f"{persona_id}.json"))
    if not card.system_prompt:
        raise PersonaError(f"角色卡 {persona_id} 缺 system_prompt")
    if not card.few_shot:
        logger.warning("角色卡 %s 没有 few-shot 示范，腔调会差一些", persona_id)
    return card


def can_spawn_card(persona_id: str) -> bool:
    """这个角色能不能自己产卡（指令 14：只有玄学类角色可以）。"""
    return load_persona(persona_id).role_type == ROLE_METAPHYSICS


def spawn_options() -> list[tuple[str, str]]:
    """卡牌"陪执行角色"下拉的选项：(显示名, 角色卡 id)。

    宠物本体用空串表示，且它自己就是「林医生」，所以那张卡不重复列出来。
    """
    options: list[tuple[str, str]] = [(BODY_LABEL, "")]
    for entry in list_personas_index():
        if entry.get("id") == DEFAULT_CHAT_PERSONA:
            continue
        options.append((str(entry.get("name") or entry.get("id")), str(entry.get("id") or "")))
    return options


def list_personas_index() -> list[dict]:
    """索引里的角色卡条目（id / name / role_type / file / desc）。"""
    data = _read_json(PERSONA_DIR / INDEX_FILE)
    return [entry for entry in (data.get("personas") or []) if isinstance(entry, dict)]
