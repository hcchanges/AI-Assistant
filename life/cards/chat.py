"""
卡内小助手（v2-card-tools-A）：每张卡底下嵌一间自己的小房间，隔壁听不见。

**这张卡归这张卡。** 生活卡（出行 / 减脂）与工作卡底部各有一个嵌进卡面的
聊天窗，说话只在**这一张卡**里发生：

- 卡与卡之间不串——出行卡里说的"我下周去成都"，减脂卡不会看见；
- 卡与全局不串——卡窗里说的话**不进** pet.db 的 `utterance` / `memory`，**不进**
  mind.db 的片段、记忆册与好感度，也**不进**画像内存（`ProfileStore`）；
- 反方向也一样：全局「AI助理」的记忆**不进**卡窗的提示词。卡窗的提示词里没有
  `{memories}` 这一格——那是有意留的空，不是漏掉的。

隔离靠三件事一起成立，缺一件都漏：

1. **另一条链**：卡窗不走 `chatter_service.handle()`（那条链会写 pet.db、路由、解析、
   攒片段），只走 `LLMFactory.create(config).chat()` 一条裸通道；
2. **另一份存储**：一张卡一个 `data/card_chat/<卡号>.json`（`CardChatStore`），
   原子写，跟 pet.db / mind.db 都不是一个文件；
3. **另一个卡号**：`card_namespace()` 给每张卡一个 `life:12` / `work:feature_a`
   这样的号，存储与信号都按号分，界面上两个卡窗各自只认自己的号。

**危机词仍然最高优先（红线，不跳步）。** 卡窗里出现危机表达时：不调模型、不落这张卡的
记录、也不当成"要生成个工具"的需求，直接把全局那条 `CRISIS_REPLY` 交给宠物本体说，
卡窗里只留一句交代（`CRISIS_NOTE`）。红线的口径跟 `ChatterService._handle_new_utterance`
的短路一致——同一条话术，同一个优先次序，卡窗不是法外之地。

**全局窗口一个字不改。** 卡窗是**多出来的一间房**，不是把「AI助理」搬进卡里：
那条链、那份记忆、那个气泡全都原样留着。

**卡上还能长东西（v2-card-tools-B）。** 同样是这个窗口，说一句"加个喝水提醒"，模型回一串
结构化部件（`life/cards/tools.py` 的白名单里那六种），摆在卡面上、记在
`data/card_tools/<卡号>.json` 里，重启还在。**这条路依然是"卡内"的**：部件只作用于这张卡
（说句话 / 填个输入框 / 本地勾个选），既不写 pet.db，也不动系统提醒，更不执行任何代码。
"""

from __future__ import annotations

import json
import logging
import os
import re
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject

from brain.llm.factory import LLMFactory
from brain.prompts.templates import (
    CARD_CHAT_PROMPT,
    CARD_CHAT_SYSTEM,
    CARD_SKILL_ADD_PROMPT,
    CARD_SKILL_ADD_SYSTEM,
    CARD_SKILL_MATCH_PROMPT,
    CARD_SKILL_MATCH_SYSTEM,
    CARD_TOOL_PROMPT,
    CARD_TOOL_SYSTEM,
    GUIDE_ADVICE_HINT,
)
from core.config import Config
from core.events import EventBus
from core.worker import run_async
from life.cards import skills as skill_mod
from life.cards import tools as tool_mod
from life.psych import crisis

logger = logging.getLogger(__name__)

# 卡窗对话的落点（一张卡一个文件）。**模块级常量，不写死在 config 里**：
# 跟番茄钟存档（`focus.pomodoro.STATE_FILE`）、抓屏笔记询问存档
# （`life.screen.notes.ASK_STATE_FILE`）一个道理——它是本机运行期数据，
# 测试得能在构造应用之前把它换到临时目录（见 tests/harness.py），
# 否则跑一次测试就往用户真实的 data/ 里写一遍卡窗记录。
CHAT_DIR = "cache/card_chat"

HISTORY_LIMIT = 40      # 一张卡最多留这么多条，跟气泡那个 MAX_ITEMS 一个意思
PROMPT_HISTORY = 8      # 送进提示词的最近几条（多了把卡本身的信息挤没了）

# 卡号前缀。**唯一一份在 `life/cards/tools.py`**（`KIND_LIFE` / `KIND_WORK`）——
# 卡的类型要跟"这张卡上许摆什么"对得上，两处各写一遍字符串迟早会错开。
LIFE_KIND = tool_mod.KIND_LIFE
WORK_KIND = tool_mod.KIND_WORK

WAITING_LINE = "我想想…"
# 生成不出来时如实说，不给一段编的（跟社交草稿、游戏攻略、知识库一个态度）
FAILED_LINE = "这句我一时没接上，你再说一遍？"
# 卡窗里遇到危机表达时的交代：话由宠物本体说，这儿只说清楚"我没把它当工具需求收下"
CRISIS_NOTE = "这句我没当成这张卡里的事——那是件要紧的事，我说给你听了，也没写进这张卡的记录。"
# 要的部件这张卡上已经有了 / 这张卡摆满了——两种都如实说，别让用户以为模型没做出来
ALREADY_LINE = "这个卡上已经有它了，你往下看一眼就是。"
FULL_LINE = "这张卡上的小部件摆满了，先去一个再添。"
# 记技能（v2-life-skill）的两句交代：成了报名字，没听出给一句怎么说的提示
SKILL_OK_LINE = "记下这个技能了：{name}。往后你说一句它的触发语，我就照它做。"
SKILL_REFUSE_LINE = "这句我没听出要记个什么技能——说一句「加个技能：名称，触发语，怎么做」我就能记下来。"


def card_namespace(kind: str, ident: object) -> str:
    """给一张卡一个号：`life:12`（生活卡）/ `work:feature_a`（工作卡）。

    号是卡窗隔离的钥匙——存储文件名、信号里的第一个参数、界面认自己那一间，都用它。
    生活卡用主键 id（`data/models.py` 的 `Card.id`），工作卡用稳定的字符串 key
    （工作模块自带的那份数据里那几条，换台机器也不变）。
    """
    return f"{kind}:{ident}"


def _safe_stem(namespace: str) -> str:
    """号变文件名：`work:feature_a` → `work_feature_a`。

    只留字母数字下划线连字符，别的一律换成下划线——号里的冒号在 Windows 文件名里
    非法，卡名里再出现个 `/` 或者 emoji 也不会把路径带跑偏。
    """
    return re.sub(r"[^0-9A-Za-z_-]", "_", namespace) or "card"


def card_kb_root(config: Config, ident: object) -> Path:
    """这张卡的专属知识库目录：`<全局知识库>/cards/<卡号>`（v2-card-kb）。

    卡号用 `str(ident)` 再走一遍 `_safe_stem`：生活卡是 int id、工作卡是稳定字符串 key，
    两样都不重名；再过一遍文件名净化，就算哪天 key 里带个 `/` 也不会把路径带跑偏。
    """
    return Path(config.knowledge_dir) / "cards" / _safe_stem(str(ident))


def ensure_card_kb_root(config: Config, ident: object) -> Path:
    """把这张卡的专属知识库目录建出来（不存在就建），返回这个路径。

    建不出来只记日志、不拦着说话——卡库查不到时，知识库那边会走全局兜底
    （v2-card-kb 护栏②：冷启动不强制建库、目录为空也不报错）。
    """
    root = card_kb_root(config, ident)
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning("卡知识库：%s 建不出来（%s），这次走全局库兜底", root, exc)
    return root


# 对象攻略卡的专属知识库里那份文件（v2-card-suite 增量②）。卡窗回话前看这张卡的库目录里
# 有没有它：有 → 这张卡是"对象攻略卡"，提示词里多喂一段攻略口径（见 GUIDE_ADVICE_HINT）。
GUIDE_KB_FILE = "对方信息.md"


def guide_hint(context: dict) -> str:
    """这张卡是不是"对象攻略卡"：它的专属知识库目录里有《对方信息.md》就算。

    有 → 返回那段攻略口径（约会 / 送礼 / 找话题照着对方信息给建议）；没有 → 回空串，
    卡窗提示词里这一格就不占地方。只读一个文件的在不在，不碰 pet.db / mind.db。
    """
    kb_root = context.get("kb") if isinstance(context, dict) else None
    if not kb_root:
        return ""
    try:
        if (Path(kb_root) / GUIDE_KB_FILE).is_file():
            return GUIDE_ADVICE_HINT
    except (OSError, TypeError, ValueError):
        return ""
    return ""


# 对象攻略卡那八格「对方档案」的键 → 人话名字（v2-social-card-uiread）。键就是 `Card` 上的
# 列名（跟 `data/dao.py::_PROFILE_COLUMNS` / `life/mind/peer_distill.py::CARD_FIELDS` 同一批）。
# 顺序按"接话时最要用上的排前面"：关系阶段（调速用）→ 喜好 / 兴趣（挑话题）→ 禁忌（绕开）
# → 年龄 / 情感状态 / 烦心事 / 攻略。**空的一律不出现**（见 `guide_profile_lines`）。
GUIDE_PROFILE_LABELS: tuple[tuple[str, str], ...] = (
    ("relation_stage", "关系阶段"),
    ("likes", "喜好"),
    ("interests", "兴趣"),
    ("dislikes", "禁忌"),
    ("age", "年龄"),
    ("emotion_state", "情感状态"),
    ("anxiety", "烦心事"),
    ("strategy", "攻略"),
)


def guide_profile_lines(card: object) -> list[str]:
    """对象攻略卡那八格里**非空**的几项，收成「名字：内容」的一列（v2-social-card-uiread）。

    这八格是对象档案：前四个手动填（`v2-social-card-base`，建卡框里那四行），后四个由后台
    提炼静默补（`v2-social-card-autodistill`，见 `life/mind/peer_distill.py`）。**写是写了，
    读端此前一个都没有**——卡窗那格 `data` 原先只拼目标 / 背景 / 状态 / 打卡，卡详情页也不
    显示，于是后台抽出来的东西在界面上一个字都看不到（用户看到的就是"这张卡全空"）。真机
    实测：`card` 表 #3「对象攻略卡」八格里 `relation_stage='破冰'` / `interests='短剧'` /
    `likes='被叫哥、打赌较劲'` 都填着，而卡窗 `data` 与卡面一个都没露。

    这个函数就是补上的那一个读端：卡窗 `data`（`core/bootstrap.py::_card_chat_context`）与卡
    详情页（`ui/panels/card_panel.py`）**共用它**，两处露的是同一批字，不各拼一套。

    **纯读**：不碰盘、不碰库、不写、不改抽取那条线。八格全空（其余八类卡、或还没提炼过的
    攻略卡）就回空列表——调用方据此整块收着，不留空框。
    """
    return [
        f"{label}：{getattr(card, key, None)}"
        for key, label in GUIDE_PROFILE_LABELS
        if getattr(card, key, None)
    ]


class CardChatStore:
    """一张卡一个 JSON 文件，只装这张卡的对话。**跟 pet.db / mind.db 没有任何关系。**"""

    def __init__(self, config: Config):
        self._config = config

    def path(self, namespace: str) -> Path:
        return self._config.data_path(f"{CHAT_DIR}/{_safe_stem(namespace)}.json")

    def load(self, namespace: str) -> list[dict[str, str]]:
        """读这张卡说过的话。文件不在 / 坏了都当"还没聊过"——不拦着人说话。"""
        try:
            raw = json.loads(self.path(namespace).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("卡窗：%s 的记录读不出来（%s），当没聊过", namespace, exc)
            return []
        items = raw.get("items") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        out: list[dict[str, str]] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            who, text = item.get("who"), item.get("text")
            if who in ("user", "pet") and isinstance(text, str) and text:
                out.append({"who": who, "text": text})
        return out[-HISTORY_LIMIT:]

    def append(self, namespace: str, who: str, text: str) -> None:
        """往这张卡里添一句。写不进去只记一笔日志——说话这件事不该被落盘拦住。"""
        items = self.load(namespace)
        items.append({"who": who, "text": text})
        self._write(namespace, items[-HISTORY_LIMIT:])

    def clear(self, namespace: str) -> None:
        """把这张卡的对话清掉（清空卡窗记录 / 重来一遍用）。"""
        self._write(namespace, [])

    def _write(self, namespace: str, items: list[dict[str, str]]) -> None:
        """整份重写，先写临时文件再 `os.replace`——跟番茄钟存档同款，掉电也不会写半截。"""
        path = self.path(namespace)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps({"items": items}, ensure_ascii=False, indent=2)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("卡窗：%s 的记录写不进去（%s），这一次只留在界面上", namespace, exc)


class CardChatService(QObject):
    """卡窗的嘴：按卡号记会话、按卡号回话，两头都不跟别处搭线。

    每张卡各自有一个"正在写"的标记——一张卡在等回话时，别的卡照常说话，
    因为两张卡本来就没有共享的上下文，没有互相等的理由。

    **说话与"生成个部件"是同一个入口（v2-card-tools-B）。** 卡窗里那一句话，先过危机词，
    再看是不是"给卡上加个东西"（`tools.is_tool_request`）：是的话走 JSON 那条路，回来的
    部件摆到卡上、写进 `data/card_tools/<卡号>.json`；不是的话还是原来那条聊天路。
    两条路**都不进 pet.db / mind.db**，卡窗的隔离一分没松。
    """

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        store: CardChatStore | None = None,
        tool_store: tool_mod.CardToolStore | None = None,
        is_admin: Callable[[], bool] | None = None,
        kb_provider: Callable[[str, str | None], str] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._store = store if store is not None else CardChatStore(config)
        self._tools = (
            tool_store if tool_store is not None else tool_mod.CardToolStore(config)
        )
        # 工作卡上"现在这个身份"从哪儿来（v2-card-tools-D）：装配处接的是
        # `WorkService.is_admin`（它的值跟着 `work.role` 走，设置页一切就变）。
        # **没接线时按"员工"算**——最小权限：宁可不给摆，也不要哪天忘了接线就悄悄放开。
        self._is_admin = is_admin if is_admin is not None else (lambda: False)
        # 卡级知识库上下文从哪儿来（v2-card-kb）：装配处接的是 `KnowledgeBase.card_context`，
        # 收一句卡窗原话 + 这张卡的 kb 目录，回一段卡库优先、全局兜底的原文。
        # **没接线时回空串**——不接也能照常聊天，只是卡窗里不注入知识库那段。
        self._kb_provider = (
            kb_provider if kb_provider is not None else (lambda text, root: "")
        )
        self._running: set[str] = set()

    # ---------- 对外 ----------

    @property
    def busy(self) -> bool:
        """有没有哪张卡正在写（界面上就是"发送"变"在想…"）。"""
        return bool(self._running)

    def busy_with(self, namespace: str) -> bool:
        """这一张卡是不是正在写。"""
        return namespace in self._running

    def history(self, namespace: str) -> list[dict[str, str]]:
        """这张卡聊过的话（打开卡窗时铺回界面上）。"""
        return self._store.load(namespace)

    def clear(self, namespace: str) -> None:
        self._store.clear(namespace)

    def cards_tools(self, namespace: str) -> list[dict]:
        """这张卡上摆着的部件（`[{"id","type","label","props"}]`）。开卡时铺一遍。"""
        return list(self._tools.load(namespace)["components"])

    def tools_state(self, namespace: str) -> dict:
        """部件自己的状态（勾选了哪几条 / 倒计时走到哪）。按部件号分。"""
        return dict(self._tools.load(namespace)["state"])

    def set_tool_state(self, namespace: str, comp_id: str, value: object) -> None:
        """把某个部件的状态记回卡上（勾选、倒计时）。**只落这一份文件。**"""
        self._tools.set_state(namespace, comp_id, value)

    def remove_tool(self, namespace: str, comp_id: str) -> None:
        """从卡上拿掉一个部件（连它的状态一起）。"""
        data = self._tools.load(namespace)
        kept = [c for c in data["components"] if c.get("id") != comp_id]
        data["components"] = kept
        data["state"].pop(comp_id, None)
        self._tools.save(namespace, data)
        self._bus.card_tools_changed.emit(namespace)

    def ask(self, namespace: str, context: dict, text: str) -> bool:
        """卡窗里说了一句。返回 True = 这句我收下了（调用方别再往全局那条路送）。

        `context` 是这张卡当下的样子（`type` / `title` / `brief` / `data` 四格），
        由装配处从服务层取好递进来——界面不碰数据，服务不碰控件。
        """
        body = (text or "").strip()
        if not body or not namespace:
            return False
        # 红线：危机词最高优先。不走模型、不落这张卡的记录，也不当成"要生成个工具"的
        # 需求——话交给宠物本体说（跟全局那条短路同一句话术、同一个次序）。
        if crisis.is_crisis(body):
            logger.warning("卡窗（%s）里出现危机表达，转危机话术", namespace)
            self._bus.pet_speak.emit(crisis.CRISIS_REPLY)
            self._bus.card_chat_note.emit(namespace, CRISIS_NOTE)
            return True
        if namespace in self._running:
            return False        # 这张卡上一句还没回来，不吃第二句
        self._running.add(namespace)
        # 用户那句先落这张卡的记录：回话还没来也不能丢，重开卡窗它还在
        self._store.append(namespace, "user", body)
        if skill_mod.is_skill_add(body):
            self._start_skill_add(namespace, context or {}, body)
        elif tool_mod.is_tool_request(body):
            self._start_tool(namespace, context or {}, body)
        else:
            run_async(
                self._reply,
                context or {},
                body,
                self._store.load(namespace),
                on_done=lambda reply: self._on_reply(namespace, reply),
                on_failed=lambda exc: self._on_failed(namespace, exc),
            )
        return True

    # ---------- 卡上添个部件（v2-card-tools-B） ----------

    def _start_tool(self, namespace: str, context: dict, body: str) -> None:
        """这句是"给卡上加个东西"。像代码 / 命令的一律不去问模型，直接说做不到。"""
        if tool_mod.looks_like_code(body):
            logger.info("卡窗（%s）里要的是代码 / 命令，按沙箱拒绝", namespace)
            self._finish_line(namespace, tool_mod.CODE_REFUSAL)
            return
        # 这张卡上许摆什么（类型 + 动作），**这一趟前后两道都用它**：
        # 提示词里照它写、模型回来的东西照它验（v2-card-tools-D）。
        policy = self.policy_for(namespace)
        run_async(
            self._make_tool,
            context,
            body,
            self.cards_tools(namespace),
            policy,
            on_done=lambda data: self._on_tools(namespace, data, policy),
            on_failed=lambda exc: self._on_failed(namespace, exc),
        )

    def policy_for(self, namespace: str) -> tool_mod.ToolPolicy:
        """这张卡（按卡号前缀）现在这个身份下许摆什么。

        身份是**每次问的时候现取**的（`WorkService.is_admin` 会随设置页切换变），
        所以切完身份下一句话就按新身份走，不用重启。
        """
        kind = namespace.partition(":")[0]
        return tool_mod.policy_for(kind, self._is_admin())

    def _make_tool(
        self,
        context: dict,
        text: str,
        existing: list[dict],
        policy: tool_mod.ToolPolicy,
    ) -> dict:
        """后台线程里要一个部件 JSON。**要回来的东西还没有资格上卡**——得先过
        `normalize_components` 那道白名单（在 `_on_tools` 里）。

        提示词那道（前端）也用同一份政策：员工的工作卡上，**表里就不列 chat 动作的
        部件**——看得见但不给，最容易招模型去试。
        """
        provider = LLMFactory.create(self._config)
        prompt = CARD_TOOL_PROMPT.format(
            card_type=context.get("type") or "生活卡",
            title=context.get("title") or "（还没起名）",
            brief=context.get("brief") or "（这张卡没有别的说明）",
            data=context.get("data") or "（这张卡还没有填下什么内容）",
            existing=_render_existing(existing),
            text=text,
            catalog=tool_mod.catalog_text(policy.types),
            policy=tool_mod.policy_text(policy),
        )
        return provider.chat_json(prompt, system=CARD_TOOL_SYSTEM)

    def _on_tools(
        self,
        namespace: str,
        data: object,
        policy: tool_mod.ToolPolicy,
    ) -> None:
        """模型给的部件到了：过白名单 → 上卡 → 落盘 → 通知界面。

        白名单带**政策**（这张卡 + 这个身份）跑，所以模型不管说了什么、绕到哪一步，
        摆上卡的东西一定在这一档允许的范围里——`workFlowEdit` 那一类一个都进不来。
        """
        self._running.discard(namespace)
        payload = data if isinstance(data, dict) else {}
        wanted = payload.get("components")
        specs = tool_mod.normalize_components(wanted, policy=policy)
        say = payload.get("say") if isinstance(payload.get("say"), str) else ""
        if not specs:
            # 模型没给出能用的部件：如实说没做出来，**绝不自己编一个摆上去**。
            # 被闸挡下来的另外说一句，而且**红线与身份分开说**（红线那条换个身份也不行，
            # 说成"身份受限"等于骗用户去折腾设置页）；不然用户以为是自己说得不好。
            # 注意这时候**不采信模型那句 say**——它可能正说着"加好了"，
            # 而实际上一个都没摆上去。
            logger.info("卡窗（%s）：这一次没得到能用的部件", namespace)
            reason = tool_mod.blocked_reason(wanted, policy)
            if reason == "edit":
                line = tool_mod.EDIT_REFUSAL
            elif reason == "policy":
                line = tool_mod.refusal_line(policy)
            else:
                line = say.strip() or tool_mod.FAILED_LINE
            self._finish_line(namespace, line)
            return
        added = self._tools.add(namespace, specs)
        if added:
            self._bus.card_tools_changed.emit(namespace)
            line = say.strip() or "好，摆在这张卡上了。"
        else:
            have = {str(c.get("id")) for c in self.cards_tools(namespace)}
            line = say.strip() or (ALREADY_LINE if all(s.id in have for s in specs) else FULL_LINE)
        self._finish_line(namespace, line)

    def _finish_line(self, namespace: str, line: str) -> None:
        """把一句结果当成宠物在卡窗里说的话收尾（落盘 + 发给界面）。"""
        self._running.discard(namespace)
        text = (line or "").strip() or FAILED_LINE
        self._store.append(namespace, "pet", text)
        self._bus.card_chat_reply.emit(namespace, text)

    # ---------- 记技能（v2-life-skill） ----------

    def _start_skill_add(self, namespace: str, context: dict, body: str) -> None:
        """这句是「给我加个技能」：听出名称 / 触发语 / 工作流，写进这张卡的 skills.md。"""
        kb_root = context.get("kb")
        run_async(
            self._make_skill,
            context,
            body,
            on_done=lambda skill: self._on_skill_add(namespace, kb_root, skill),
            on_failed=lambda exc: self._on_failed(namespace, exc),
        )

    def _make_skill(self, context: dict, text: str) -> skill_mod.Skill | None:
        """后台线程里要一个技能的三样东西。要回来的还不算数：三项缺一项就不要。"""
        provider = LLMFactory.create(self._config)
        prompt = CARD_SKILL_ADD_PROMPT.format(
            card_type=context.get("type") or "生活卡",
            title=context.get("title") or "（还没起名）",
            brief=context.get("brief") or "（这张卡没有别的说明）",
            existing=skill_mod.render_skills_for_match(skill_mod.load_skills(context.get("kb"))),
            text=text,
        )
        data = provider.chat_json(prompt, system=CARD_SKILL_ADD_SYSTEM)
        if not isinstance(data, dict):
            return None
        name = skill_mod.sanitize(data.get("skill_name"))
        triggers = skill_mod.sanitize(data.get("skill_triggers"))
        prompt_text = skill_mod.sanitize(data.get("skill_prompt"))
        if not name or not triggers or not prompt_text:
            return None
        return skill_mod.Skill(name=name, triggers=triggers, prompt=prompt_text)

    def _on_skill_add(self, namespace: str, kb_root, skill: skill_mod.Skill | None) -> None:
        """模型听出的技能到了：写进这张卡的 skills.md，成了就报一声，没成就如实说。"""
        self._running.discard(namespace)
        if skill is None or not skill_mod.append_skill(kb_root, skill):
            self._finish_line(namespace, SKILL_REFUSE_LINE)
            return
        self._finish_line(namespace, SKILL_OK_LINE.format(name=skill.name))

    # ---------- 回话 ----------

    def _reply(self, context: dict, text: str, history: list[dict[str, str]]) -> str:
        """后台线程里回一句：先看这句唤不唤起卡上记的技能，再走裸模型通道回话。"""
        skill = self._match_skill(context, text)
        provider = LLMFactory.create(self._config)
        # 卡级知识库上下文（v2-card-kb）：只扫这张卡的 .md/.txt（+ 全局兜底），
        # 一个字节都不碰 pet.db / mind.db——隔离跟上面三条纪律一个口径。
        kb = self._kb_provider(text, context.get("kb")) or "（这张卡没查到相关的知识库内容）"
        prompt = CARD_CHAT_PROMPT.format(
            card_type=context.get("type") or "生活卡",
            title=context.get("title") or "（还没起名）",
            brief=context.get("brief") or "（这张卡没有别的说明）",
            data=context.get("data") or "（这张卡还没有填下什么内容）",
            guide_hint=guide_hint(context),
            kb=kb,
            skill=_render_skill(skill),
            history=_render_history(history),
            text=text,
        )
        return (provider.chat(prompt, system=CARD_CHAT_SYSTEM) or "").strip()

    def _match_skill(self, context: dict, text: str) -> skill_mod.Skill | None:
        """这句想不想唤起这张卡上记的某个技能：没记技能就跳过，有就让模型挑一个。"""
        skills = skill_mod.load_skills(context.get("kb"))
        if not skills:
            return None
        provider = LLMFactory.create(self._config)
        prompt = CARD_SKILL_MATCH_PROMPT.format(
            skills=skill_mod.render_skills_for_match(skills),
            text=text,
        )
        data = provider.chat_json(prompt, system=CARD_SKILL_MATCH_SYSTEM)
        name = data.get("matched") if isinstance(data, dict) else None
        return skill_mod.find_skill(skills, name)

    def _on_reply(self, namespace: str, reply: str) -> None:
        self._running.discard(namespace)
        clean = (reply or "").strip()
        if not clean:
            self._bus.card_chat_reply.emit(namespace, FAILED_LINE)
            return
        self._store.append(namespace, "pet", clean)
        self._bus.card_chat_reply.emit(namespace, clean)

    def _on_failed(self, namespace: str, exc: Exception) -> None:
        """模型不可用：如实说没接上，**不落盘**（这是一次失败，不是它说过的话）。"""
        self._running.discard(namespace)
        logger.error("卡窗（%s）回话失败：%s", namespace, exc)
        self._bus.card_chat_reply.emit(namespace, FAILED_LINE)


def _render_existing(existing: list[dict]) -> str:
    """这张卡上已经摆着哪些部件（免得同一个再添一遍）。"""
    labels = [str(item.get("label")) for item in existing if item.get("label")]
    return "、".join(labels) if labels else "（这张卡上还没有小部件）"


def _render_skill(skill) -> str:
    """把被唤起的那个技能铺成提示词里的一段；没唤起就回空串（这一格不占地方）。"""
    if skill is None:
        return ""
    return (
        f"这张卡上记着的一个技能被这句话唤起了，叫「{skill.name}」。"
        f"就按它记下的工作流来做，别自作主张加戏、也别扯到别的卡。工作流：{skill.prompt}"
    )


def _render_history(history: list[dict[str, str]]) -> str:
    """最近几句铺成提示词里那一段。**只有这张卡自己的话**，别处的一律不在。"""
    rows = history[-PROMPT_HISTORY:]
    if not rows:
        return "（这张卡还没聊过什么）"
    lines = [
        f"{'用户' if row['who'] == 'user' else '你'}：{row['text']}"
        for row in rows
        if row.get("text")
    ]
    return "\n".join(lines) if lines else "（这张卡还没聊过什么）"


__all__ = [
    "ALREADY_LINE", "CHAT_DIR", "CRISIS_NOTE", "CardChatService", "CardChatStore",
    "FAILED_LINE", "FULL_LINE", "GUIDE_KB_FILE", "HISTORY_LIMIT", "LIFE_KIND",
    "PROMPT_HISTORY", "SKILL_OK_LINE", "SKILL_REFUSE_LINE", "WAITING_LINE", "WORK_KIND",
    "card_kb_root", "card_namespace",
    "ensure_card_kb_root", "guide_hint",
]
