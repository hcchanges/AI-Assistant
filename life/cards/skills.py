"""
卡内技能（v2-life-skill）：一张卡上记几个「技能」，说一句触发语就唤起一个。

跟卡内小部件（v2-card-tools-B）是两回事：

- 部件是**卡面上的小控件**（提醒 / 计时器 / 按钮…），落在 `data/card_tools/<卡号>.json`；
- 技能是**一段记在卡上的工作流**（「名称 + 触发语 + 怎么做」），落在这张卡的专属知识库
  `cards/<卡>/skills.md` 里——用户说一句触发语，模型就按这段工作流回话，**不产生任何控件**。

隔离跟卡窗一条口径：技能只作用于这一张卡，写的是这张卡自己的 `skills.md`，一个字节都不碰
pet.db / mind.db。注册表是纯文本 markdown，人来也能看懂、能改；模型只负责「听出要记什么」
和「听出这句唤起哪个」，真正的落盘还是这一小段死代码（不引任何新包）。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

SKILLS_FILE = "skills.md"   # 一张卡一份，摆在它的专属知识库目录里（跟《对方信息.md》同级）


@dataclass
class Skill:
    """一个技能 = 名称 + 触发语 + 工作流（prompt）。"""
    name: str
    triggers: str
    prompt: str


def skills_path(kb_root) -> Path | None:
    """这张卡的技能注册表路径。kb_root 没给（冷启动没建库）回 None。"""
    if not kb_root:
        return None
    return Path(kb_root) / SKILLS_FILE


def sanitize(text: object) -> str:
    """压成一行：换行、多余空白都并掉，避免把注册表拆坏。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def parse_skills_md(text: str) -> list[Skill]:
    """把 skills.md 的原文拆成技能列表。拆不出 / 拆坏了就当没有，绝不拦着说话。"""
    skills: list[Skill] = []
    for block in re.split(r"(?m)^##[ \t]+", text or ""):
        lines = block.splitlines()
        if not lines:
            continue
        name = lines[0].strip()
        triggers = ""
        prompt = ""
        for line in lines[1:]:
            m = re.match(r"^[-*]?[ \t]*触发语[：:][ \t]*(.*)$", line)
            if m:
                triggers = m.group(1).strip()
                continue
            m = re.match(r"^[-*]?[ \t]*工作流[：:][ \t]*(.*)$", line)
            if m:
                prompt = m.group(1).strip()
        if name and (triggers or prompt):
            skills.append(Skill(name=name, triggers=triggers, prompt=prompt))
    return skills


def load_skills(kb_root) -> list[Skill]:
    """读这张卡记着的技能。文件不在 / 坏了都当「还没记」，不拦人。"""
    path = skills_path(kb_root)
    if path is None:
        return []
    try:
        return parse_skills_md(path.read_text(encoding="utf-8"))
    except OSError:
        return []


def render_skills_md(skills: list[Skill]) -> str:
    """把技能列表写成 skills.md 的原文（人来能看懂）。"""
    header = "# 这张卡上记着的技能（Skill）\n\n"
    header += "> 说一句触发语就唤起一个技能；「加个技能：名称，触发语，怎么做」往这里记一条。\n\n"
    blocks = [
        f"## {sanitize(s.name)}\n- 触发语：{sanitize(s.triggers)}\n- 工作流：{sanitize(s.prompt)}"
        for s in skills
    ]
    return header + "\n\n".join(blocks) + ("\n" if blocks else "")


def save_skills(kb_root, skills: list[Skill]) -> bool:
    """整份重写（先写临时文件再 os.replace）。写不进去只回 False，不抛。"""
    path = skills_path(kb_root)
    if path is None:
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(render_skills_md(skills), encoding="utf-8")
        os.replace(tmp, path)
        return True
    except OSError:
        return False


def append_skill(kb_root, skill: Skill) -> bool:
    """往这张卡加一个技能（同名的换掉旧的），落盘成功回 True。"""
    skills = [s for s in load_skills(kb_root) if s.name != skill.name]
    skills.append(skill)
    return save_skills(kb_root, skills)


def find_skill(skills: list[Skill], name: object) -> Skill | None:
    """按名称（一字不差）在列表里找技能。找不着回 None。"""
    if not isinstance(name, str) or not name:
        return None
    want = name.strip()
    for s in skills:
        if s.name == want:
            return s
    return None


def render_skills_for_match(skills: list[Skill]) -> str:
    """给「匹配」提示词铺的技能清单：名称 + 触发语，一条一行。"""
    if not skills:
        return "（这张卡上还没记技能）"
    rows = [f"{i}. 名称：{s.name}｜触发语：{s.triggers}" for i, s in enumerate(skills, 1)]
    return "\n".join(rows)


def is_skill_add(text: str) -> bool:
    """这句是不是「给我加个技能」：带上 skill / 技能 二字就算。

    （「加载 XX」不在这里拦——那走触发语匹配那条路，命中哪个技能唤起哪个。）
    """
    t = (text or "").lower()
    return "skill" in t or "技能" in t
