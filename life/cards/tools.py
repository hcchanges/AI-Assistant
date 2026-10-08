"""
卡内工具生成（v2-card-tools-B）：用户说一句"加个喝水提醒"，卡上多出一个小部件。

**这条路是"把话变成卡上的控件"，不是"让模型跑代码"。** 两件事得分开说：

1. 提示词里先把边界写死（只能从预置的六种里挑、不许出现代码 / 命令 / 网址），
   但那只是**第一道**——模型不一定听话；
2. 真正的闸在 `normalize_components()`：**类型**与**动作**两道白名单外的一律丢掉，
   props 只收字符串 / 数字 / 布尔 / 字符串数组（嵌套对象直接丢），带代码味的字串也丢。
   **没有一个字段会被 `eval`、拼接成路径、或者喂给任何执行入口**——它们最后只变成
   卡上的文字与本地状态。

**动作与分角色的红线（v2-card-tools-D）。** 部件不只是"长什么样"，还有"点一下算干了
什么"——后者才是要紧的：

- **`workFlowEdit`（改流程 / 改连接 / 增删卡）任何卡、任何身份都不许**：`ALL_ACTIONS`
  里没有它，提示词里点名说不接受，后端认出来一律丢（藏在 props 深处、换个下划线写法
  也一样认）。卡上的小部件碰不到工作流——那是程序里那个编辑入口的事；
- **生活卡不受身份影响**：六种都能摆（回归时这条必须还是绿的）；
- **工作卡分身份**：管理员六种都能摆；员工只剩 `record` 动作那三种（提醒 / 计时器 /
  清单），工作卡上不长"点一下就以他的名义替他说句话"的按钮。身份来自
  `work.role`（`WorkService.is_admin`），由装配处接进卡窗服务。

两道校验（提示词那道、代码这道）**取的是同一份数据**（`ToolPolicy`），
所以不存在"提示词里说不许、代码里放过去"这种缝。

**六个预置部件（`CATALOG`）与它们的点击行为**，全部只作用于这张卡：

| type | 卡上长什么样 | 点一下会发生什么 |
| --- | --- | --- |
| `button` 按钮 | 一个小按钮 | 把按钮上的字当成你说的一句话，送进这个卡窗 |
| `shortcut` 快捷指令 | 一个小按钮 | 把那句常说的话**填进输入框**（不自动发送，你还能改） |
| `reminder` 提醒 | 一行「时间 + 事」 | 点一下在这张卡上勾掉 / 取消勾掉（**不碰系统的提醒**） |
| `timer` 计时器 | 一个倒计时按钮 | 开始倒数，到点在卡窗里留一句交代（**不调模型**）；再点一下停 |
| `list` 列表 | 一个勾选清单 | 勾一条记一条（勾选状态跟着卡存在卡里） |
| `automation` 自动化 | 一个小按钮 | 把那串准备好的话**送进这个卡窗**（一次点击 = 一次已经写好的请求） |

**为什么这些部件不写 pet.db、不设系统提醒**：这一阶段要的是"卡上有东西、能用、重启还在"，
而"替用户真的设一个系统提醒 / 打卡 / 改卡"是**另一个量级的授权**（那是 D 阶段的权限与红线
收口要谈的事）。所以部件的记录落在 `data/card_tools/<卡号>.json` 这一份本机文件里，
跟 pet.db / mind.db 都没有关系——探针里那一句 `utterance 0 → 0` 就是这么来的。

**跨重启**：部件与勾选状态都在那份文件里，`CardChatService.specs()` 每次按卡号读，
所以关掉桌宠再打开，卡上那几个部件还在（测试里用"同一份配置重开一只"验的就是这条）。
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from core.config import Config

logger = logging.getLogger(__name__)

# 部件的落点（一张卡一个文件）。**模块级常量**，理由跟 life/cards/chat.py 的 CHAT_DIR 一样：
# 它默认落在项目里的 data/card_tools/，测试得能在构造应用之前换到临时目录去
# （见 tests/harness.py），否则跑一次测试就往用户真实的卡上添一个部件。
TOOLS_DIR = "cache/card_tools"

TYPE_BUTTON = "button"
TYPE_SHORTCUT = "shortcut"
TYPE_REMINDER = "reminder"
TYPE_TIMER = "timer"
TYPE_LIST = "list"
TYPE_AUTOMATION = "automation"

# ---------- 动作（v2-card-tools-D）----------
# 部件不只"长什么样"，还有"点一下算干了什么"。**红线的关键在后一半**：
# 卡上的小部件**永远不许**去改工作流 / 改底层连接 / 增删卡（动作名 `workFlowEdit`）——
# 那是程序里那个编辑入口干的事，不是卡上摆一个按钮就能碰到的。所以动作分两档：
ACTION_CHAT = "chat"        # 点一下 = 把一句话送进这张卡的卡窗（本质还是"说话"）
ACTION_RECORD = "record"    # 点一下 = 只在这张卡上记状态（勾选 / 倒计时）
#: 红线动作：任何卡、任何身份，白名单里都没有它（`ALL_ACTIONS` 里也没有）
ACTION_WORKFLOW_EDIT = "workFlowEdit"
#: 红线关键字（去分隔符、小写之后比）：`workFlowEdit` / `work_flow_edit` / `WORK-FLOW-EDIT`
#: 都算同一个东西，模型换个写法也翻不出去
WORKFLOW_EDIT_KEY = "workflowedit"

# 六种预置部件（顺序就是设置页/提示词里给模型看的顺序）。`action` 是**类型定死的动作**：
# 模型说了不算，写别的要么被丢掉、要么跟类型对不上被丢掉（见 `_build`）。
CATALOG: dict[str, dict[str, str]] = {
    TYPE_BUTTON: {"title": "按钮", "fields": "label（按钮文字）", "action": ACTION_CHAT},
    TYPE_SHORTCUT: {
        "title": "快捷指令",
        "fields": "label（按钮文字）, text（填进输入框那句话）",
        "action": ACTION_CHAT,
    },
    TYPE_REMINDER: {
        "title": "提醒", "fields": "label（提醒的事）, at（24 小时制 HH:MM）",
        "action": ACTION_RECORD,
    },
    TYPE_TIMER: {
        "title": "计时器", "fields": "label（倒计时的事）, minutes（1~1440 的整数）",
        "action": ACTION_RECORD,
    },
    TYPE_LIST: {
        "title": "列表", "fields": "label（清单名字）, items（一串条目）",
        "action": ACTION_RECORD,
    },
    TYPE_AUTOMATION: {
        "title": "自动化",
        "fields": "label（按钮文字）, prompt（点一下送进卡窗的那句话）",
        "action": ACTION_CHAT,
    },
}

#: 全部**允许存在**的动作。`ACTION_WORKFLOW_EDIT` 不在里面——这就是红线本身
ALL_ACTIONS: frozenset[str] = frozenset({ACTION_CHAT, ACTION_RECORD})
#: 员工身份在工作卡上只许用的动作（记事，不许让卡上长出"点一下替他发话"的东西）
WORK_STAFF_ACTIONS: frozenset[str] = frozenset({ACTION_RECORD})

MAX_COMPONENTS = 6          # 一张卡上最多留这么多部件（再多卡面就没地方了）
MAX_PER_REPLY = 3           # 一次最多加这么多个（模型一次说三个已经是上限）
LABEL_MAX = 20
TEXT_MAX = 200
ITEMS_MAX = 12
MINUTES_MAX = 24 * 60

# 带这些字样的值一律丢掉：这条路**从不执行**任何东西，所以这只是"别把看着像代码的
# 东西摆到卡上"的第二道，跟提示词里那句"不许生成代码"配成一对。
CODE_MARKERS: tuple[str, ...] = (
    "import ", "eval(", "exec(", "os.system", "subprocess", "__", "<script", "</",
    "powershell", "cmd /c", "cmd.exe", "rm -rf", "del /f", "curl ", "wget ",
    "http://", "https://", "\\\\", "..\\", "../",
)

# 判"这句是要在卡上加个部件"。**宁可漏判**（跟知识库词表、社交求文词表一个态度）：
# 认不出来就当普通聊天，走原来那条路，一个字都不变。
# 所以要求"动词 + 名词"两样都在——只出现"提醒"两个字（"你别忘了提醒我"）不算。
TOOL_VERBS: tuple[str, ...] = (
    "加个", "加一个", "加一", "来个", "来一个", "做一个", "做个", "生成", "整个",
    "给我", "帮我", "弄个", "搞个", "添个", "新增",
)
TOOL_NOUNS: tuple[str, ...] = (
    "提醒", "计时器", "倒计时", "按钮", "快捷", "清单", "列表", "自动化", "小工具",
    "小部件", "组件", "卡片工具", "卡上工具",
)

# 拒绝了什么、为什么，得说清楚（不然用户以为坏了）
CODE_REFUSAL = "这个我做不到——卡上的小部件只能从现成的那几种里挑，我不会在卡上跑代码或者执行命令。你说的是件正经事的话，我陪你聊，或者换个说法的部件我试试。"
FAILED_LINE = "这个部件我没做出来，换个说法再跟我说一遍？"
# 工作卡上的两条拒绝（v2-card-tools-D）：红线一条、身份受限一条，别混着说
WORK_EDIT_REFUSAL = "工作卡上我不摆会改流程、改连接、动卡片本身的东西——那种事得去编辑入口做，卡上的小部件碰不到它。想加个记事的小工具，我这就给你摆。"
WORK_STAFF_REFUSAL = "工作卡上（现在这个身份）只能摆记事的部件：提醒、计时器、清单。要那种点一下就替你发话的，换管理员身份再来。"

# 卡号前缀（"life:12" / "work:feature_a"）。**放这儿当唯一一份**：
# `life/cards/chat.py` 的 `LIFE_KIND` / `WORK_KIND` 直接取这里的值，
# 免得两处各写一遍字符串、哪天改了一处另一处还留着。
KIND_LIFE = "life"
KIND_WORK = "work"


def catalog_text(types: frozenset[str] | None = None) -> str:
    """给提示词看的那张组件表（跟 `CATALOG` 同一份数据，不另抄一遍）。

    `types` 给了就只列这几种——**员工的工作卡上那张表里不许出现 chat 动作的部件**，
    提示词里就不该把它们摆出来给模型挑（"看得见但不给"最容易招模型去试）。
    """
    return "\n".join(
        f"- {name}（{item['title']}，动作 {item['action']}）：{item['fields']}"
        for name, item in CATALOG.items()
        if types is None or name in types
    )


def default_action(type_name: str) -> str:
    """这个类型的部件点一下算什么动作（模型说了不算，类型定死）。"""
    item = CATALOG.get(type_name)
    return str(item["action"]) if item else ""


def _norm_action(value: Any) -> str:
    """动作值比大小之前先归一：去分隔符、转小写。

    `workFlowEdit` / `work_flow_edit` / `WORK-FLOW-EDIT` / `work flow edit` 都是同一个
    东西——模型换个写法不该就绕过去了。归一之后认不出来的动作会被上层丢掉（不纠正、
    不猜它想干什么）。
    """
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\s_\-/.]", "", value).lower()


@dataclass(frozen=True)
class ToolPolicy:
    """**这张卡上许摆什么**：类型白名单 + 动作白名单（v2-card-tools-D）。

    两份白名单是同一份数据，提示词（前端那道）与 `normalize_components`（后端那道）
    都从这儿取——两道校验必须一模一样，不然就是"提示词里说不行、代码里放过去了"。

    规矩：

    - **`workFlowEdit` 谁都没有**（红线）。`actions` 永远从 `ALL_ACTIONS` 里取，
      而 `ALL_ACTIONS` 里没有它；
    - **生活卡不受身份影响**：六种都能摆、两种动作都行（"回归生活卡可自由生成"）；
    - **工作卡**：管理员六种都能摆；员工只剩 `record` 动作那三种（提醒 / 计时器 / 清单）
      ——员工的工作卡上不长"点一下就以他的名义替他说句话"的按钮。
    """

    kind: str = KIND_LIFE
    is_admin: bool = True

    @property
    def types(self) -> frozenset[str]:
        if self.kind != KIND_WORK or self.is_admin:
            return frozenset(CATALOG)
        return frozenset(name for name, item in CATALOG.items() if item["action"] in self.actions)

    @property
    def actions(self) -> frozenset[str]:
        if self.kind != KIND_WORK:
            return ALL_ACTIONS
        return ALL_ACTIONS if self.is_admin else WORK_STAFF_ACTIONS

    @property
    def label(self) -> str:
        """给日志与提示词看的说法。"""
        if self.kind != KIND_WORK:
            return "生活卡"
        return "工作卡·管理员" if self.is_admin else "工作卡·员工"

    def allows_type(self, type_name: str) -> bool:
        return type_name in self.types

    def allows_action(self, action: str) -> bool:
        return action in self.actions

    def allows(self, type_name: str, action: str) -> bool:
        """类型与动作都过，才许摆。"""
        return self.allows_type(type_name) and self.allows_action(action)


def policy_for(kind: str, is_admin: bool = True) -> ToolPolicy:
    """按卡号前缀与身份取这份卡的政策。**认不出的前缀当生活卡**（宽松那档）。

    认不出时往宽松走是有意的：这条路上的"卡"只有生活卡与工作卡两种，前缀认不出
    说明号本身不对——那种时候拦的不是生成，而是别处（卡号绑不上卡，界面压根到不了这儿）；
    在这里收紧只会把一张正常的卡误伤成"什么都不能加"。
    """
    return ToolPolicy(KIND_WORK if kind == KIND_WORK else KIND_LIFE, bool(is_admin))


def policy_text(policy: ToolPolicy) -> str:
    """写进提示词里的那一小段"这张卡上许摆什么"（前端那道校验）。"""
    lines = [f"这张卡是{policy.label}。"]
    if policy.kind == KIND_WORK:
        lines.append(
            "工作卡上**绝对不许**出现「改流程 / 改连接 / 增删卡片本身」这类部件——"
            "它们的动作是 " + ACTION_WORKFLOW_EDIT + "，我这边一例都不接受；"
            "那是程序里的编辑入口该做的事，卡上的小部件碰不到它。"
        )
        if not policy.is_admin:
            lines.append(
                "而且**现在这个身份是员工**：只能挑动作是 "
                + ACTION_RECORD + " 的那几种（"
                + "、".join(item["title"] for name, item in CATALOG.items()
                            if name in policy.types)
                + "）；动作是 " + ACTION_CHAT + " 的（能替你发话的那些）这个身份放不上卡。"
            )
    else:
        lines.append("生活卡上不受身份限制，表里的六种都能挑，动作只有 "
                     + ACTION_CHAT + " 与 " + ACTION_RECORD + " 两种，也都可以用。")
    lines.append(
        "**动作由类型定死**：别自己写动作字段；真写了、又跟类型对不上，那一个部件会被直接丢掉。"
    )
    return "\n".join(lines)


def refusal_line(policy: ToolPolicy) -> str:
    """按政策挑那句"为什么没给你摆上"（用户以为坏了，得说清楚）。"""
    if policy.kind == KIND_WORK:
        return WORK_STAFF_REFUSAL if not policy.is_admin else WORK_EDIT_REFUSAL
    return FAILED_LINE


def is_tool_request(text: str) -> bool:
    """这句话是不是"在卡上加个部件"。**危机词面前一律不算**（调用方先过危机词）。"""
    body = (text or "").strip()
    if not body:
        return False
    has_verb = any(word in body for word in TOOL_VERBS)
    has_noun = any(word in body for word in TOOL_NOUNS)
    return has_verb and has_noun


def looks_like_code(text: str) -> bool:
    """这句里带着像代码 / 命令的东西——这条路不接（也不去问模型）。"""
    low = (text or "").lower()
    return any(marker in low for marker in CODE_MARKERS)


@dataclass(frozen=True)
class Component:
    """卡上的一个部件。`id` 由类型 + 标签定下来，同一张卡上不会有两个一模一样的。

    `action` 是**类型定死的动作**（`chat` / `record`），不是模型给的——它落进记录文件里，
    是为了"卡上这个东西点一下算干什么"能事后查得到（`workFlowEdit` 一例都不会出现在这儿）。
    """

    id: str
    type: str
    label: str
    props: dict[str, Any] = field(default_factory=dict)
    action: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "label": self.label,
            "props": dict(self.props),
            "action": self.action or default_action(self.type),
        }


def component_id(type_name: str, label: str) -> str:
    """部件号：类型 + 标签。同一个"喝水提醒"再加一次会被认成同一个（不重复摆）。"""
    return f"{type_name}:{label}"


def normalize_components(
    raw: Any,
    limit: int = MAX_PER_REPLY,
    policy: ToolPolicy | None = None,
) -> list[Component]:
    """把模型给的那一串收拾成能摆上卡的部件。**类型与动作两道白名单之外的一律丢掉。**

    这是沙箱真正的落点：类型不在 `CATALOG` 里 → 丢；动作（模型写的那个，或者从类型
    推出来的那个）不在政策里 → 丢；`workFlowEdit` 这类改工作流的动作 → **一律丢**
    （v2-card-tools-D 的红线，连管理员也不行）；props 里有嵌套对象 / 数组（除了
    `items`）/ 别的怪东西 → 丢；值里带代码味 → 丢；缺必填字段 → 丢。
    丢干净了也不报错——上层会退成"回他一句话"，不编一个部件出来。

    `policy` 是**这张卡上许摆什么**（`policy_for`）。给 `None` 就是"不按卡类型收紧"，
    只剩类型白名单与红线——**读回卡上已有的部件时走的就是这一条**：政策是"生成时"
    的闸，不是"事后清算"，用户以前摆上的东西不因为换了身份就消失。

    `limit` 有两个口径，**不能混**：模型一次最多给 3 个（`MAX_PER_REPLY`，默认值），
    而**读回卡上已有的**部件时上限是这张卡能摆的总数（`MAX_COMPONENTS`，6）。
    这里曾经写死成一次 3 个——结果第 4 个部件一存一读就被吃掉（探针里
    "卡上摆了 4 个、重开变 3 个"就是这么来的），所以这条参数留出来了。
    """
    if not isinstance(raw, list):
        return []
    out: list[Component] = []
    seen: set[str] = set()
    for item in raw[:limit]:
        built = _build(item, policy)
        if built is None or built.id in seen:
            continue
        seen.add(built.id)
        out.append(built)
    return out


#: 被红线挡下时卡窗里说的那句话（跟"身份受限"分开说，不然用户以为换个身份就行）
EDIT_REFUSAL = "这个我不做——卡上的小部件改不了流程、改不了连接，也动不了卡片本身。那种事得去程序里的编辑入口做。"


def blocked_reason(raw: Any, policy: ToolPolicy) -> str:
    """这一串里有没有被闸挡下的、挡在哪儿：`""`（没有）/ `"edit"`（红线）/ `"policy"`（这张卡 / 这个身份）。

    **红线优先**：既踩红线又不合身份的，说是红线（那是更硬的那条）。

    只回答"是不是闸挡的"，不重新做一遍校验：类型都不认识的（模型瞎编的 `shell`）不算
    ——那种该说"没做出来"，不该说"你这个身份不行"（说错了比不说更让人糊涂）。
    """
    if not isinstance(raw, list):
        return ""
    reason = ""
    for item in raw:
        if not isinstance(item, dict):
            continue
        if _given_action(item) == WORKFLOW_EDIT_KEY:
            return "edit"
        type_name = str(item.get("type") or "").strip().lower()
        if type_name in CATALOG and not policy.allows(type_name, default_action(type_name)):
            reason = "policy"
    return reason


def _given_action(item: dict[str, Any]) -> str:
    """这个部件里**模型自己写的**动作（顶层或 props 里的 `action`），归一之后。

    没有动作字段时还会把整条 JSON 归一后扫一遍红线关键字：模型把 `workFlowEdit` 藏进
    别的字段名、或者塞在 props 深处，只要那串字还在，这个部件就按"想改工作流"处理
    （返回红线关键字本身，`_build` 那里自然丢掉它）。扫的是归一后的字串，所以
    `work_flow_edit` / `WORK-FLOW-EDIT` 换个写法一样跑不掉。
    """
    for holder in (item, item.get("props")):
        if not isinstance(holder, dict):
            continue
        value = holder.get("action")
        if isinstance(value, str) and value.strip():
            return _norm_action(value)
    try:
        blob = _norm_action(json.dumps(item, ensure_ascii=False))
    except (TypeError, ValueError):
        return ""
    return WORKFLOW_EDIT_KEY if WORKFLOW_EDIT_KEY in blob else ""


def _build(item: Any, policy: ToolPolicy | None = None) -> Component | None:
    if not isinstance(item, dict):
        return None
    type_name = str(item.get("type") or "").strip().lower()
    if type_name not in CATALOG:
        logger.info("卡内工具：丢掉一个不认识的部件类型（%s）", type_name or "空")
        return None
    props = item.get("props")
    # 动作：类型定死。模型要是自己写了一个（顶层或 props 里都算），只有跟类型对得上才收——
    # 对不上就丢，**不替它纠正**（纠正等于猜它想干什么，而这里最不该猜的就是动作）。
    wanted = default_action(type_name)
    given = _given_action(item)
    if given and given != _norm_action(wanted):
        logger.info("卡内工具：丢掉一个动作跟类型对不上的部件（%s 想要 %s）", type_name, given)
        return None
    if policy is not None and not policy.allows(type_name, wanted):
        logger.info(
            "卡内工具：%s 上不摆 %s（动作 %s）——类型或动作不在白名单里",
            policy.label, type_name, wanted,
        )
        return None
    if not isinstance(props, dict):
        return None
    label = _text(props.get("label"), LABEL_MAX)
    if not label:
        return None
    kept: dict[str, Any] = {}
    if type_name == TYPE_BUTTON:
        kept = {"label": label}
    elif type_name == TYPE_SHORTCUT:
        text = _text(props.get("text"), TEXT_MAX)
        if not text:
            return None
        kept = {"label": label, "text": text}
    elif type_name == TYPE_REMINDER:
        at = _clock(props.get("at"))
        if not at:
            return None
        kept = {"label": label, "at": at}
    elif type_name == TYPE_TIMER:
        minutes = _minutes(props.get("minutes"))
        if not minutes:
            return None
        kept = {"label": label, "minutes": minutes}
    elif type_name == TYPE_LIST:
        items = _items(props.get("items"))
        if not items:
            return None
        kept = {"label": label, "items": items}
    else:                                   # TYPE_AUTOMATION
        prompt = _text(props.get("prompt"), TEXT_MAX)
        if not prompt:
            return None
        kept = {"label": label, "prompt": prompt}
    return Component(component_id(type_name, label), type_name, label, kept, wanted)


def _text(value: Any, limit: int) -> str:
    """收一个字符串字段：不是字符串 / 空 / 太长 / 带代码味 / 带换行，都当没有。"""
    if not isinstance(value, str):
        return ""
    clean = value.strip().replace("\r", " ").replace("\n", " ")
    if not clean or len(clean) > limit or looks_like_code(clean):
        return ""
    return clean


def _clock(value: Any) -> str:
    """`HH:MM`：写法不规范就当没有（不替用户猜一个时间）。"""
    raw = _text(value, 5)
    if not re.fullmatch(r"\d{1,2}:\d{2}", raw):
        return ""
    hour, minute = (int(part) for part in raw.split(":"))
    if hour > 23 or minute > 59:
        return ""
    return f"{hour:02d}:{minute:02d}"


def _minutes(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    minutes = int(value)
    return minutes if 1 <= minutes <= MINUTES_MAX else 0


def _items(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out = [t for t in (_text(entry, TEXT_MAX) for entry in value[:ITEMS_MAX]) if t]
    return out


class CardToolStore:
    """一张卡一个 JSON：`{"components": [...], "state": {...}}`。跟 pet.db 无关。"""

    def __init__(self, config: Config):
        self._config = config

    def path(self, namespace: str) -> Path:
        return self._config.data_path(f"{TOOLS_DIR}/{_safe_stem(namespace)}.json")

    def load(self, namespace: str) -> dict[str, Any]:
        """读这张卡上的部件与它们的状态。文件不在 / 坏了都当"还没有部件"。"""
        try:
            raw = json.loads(self.path(namespace).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("卡内工具：%s 的部件读不出来（%s），当没有", namespace, exc)
            return {"components": [], "state": {}}
        if not isinstance(raw, dict):
            return {"components": [], "state": {}}
        # 读回来时按"一张卡最多摆几个"收，不是"模型一次给几个"——见 normalize_components
        components = [
            c.to_json()
            for c in normalize_components(raw.get("components"), MAX_COMPONENTS)
        ]
        state = raw.get("state")
        return {"components": components, "state": state if isinstance(state, dict) else {}}

    def save(self, namespace: str, data: dict[str, Any]) -> None:
        path = self.path(namespace)
        tmp = path.with_name(path.name + ".tmp")
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)
        except OSError as exc:
            logger.error("卡内工具：%s 的部件写不进去（%s），这一次只留在界面上", namespace, exc)

    def add(self, namespace: str, specs: list[Component]) -> list[Component]:
        """往这张卡上添几个部件（已有的同名不重复添）。返回真添上去的那几个。"""
        data = self.load(namespace)
        have = {str(item.get("id")) for item in data["components"]}
        added: list[Component] = []
        for spec in specs:
            if spec.id in have or len(data["components"]) >= MAX_COMPONENTS:
                continue
            have.add(spec.id)
            data["components"].append(spec.to_json())
            added.append(spec)
        if added:
            self.save(namespace, data)
        return added

    def set_state(self, namespace: str, comp_id: str, value: Any) -> None:
        """记一个部件的状态（勾选 / 倒数到没到点）。只收 JSON 能装下的东西。"""
        data = self.load(namespace)
        try:
            json.dumps(value)
        except (TypeError, ValueError):
            return
        data["state"][comp_id] = value
        self.save(namespace, data)

    def clear(self, namespace: str) -> None:
        self.save(namespace, {"components": [], "state": {}})


def _safe_stem(namespace: str) -> str:
    """卡号 → 文件名（跟 life/cards/chat.py 同一套写法）。"""
    return re.sub(r"[^0-9A-Za-z_-]", "_", namespace) or "card"


__all__ = [
    "ACTION_CHAT", "ACTION_RECORD", "ACTION_WORKFLOW_EDIT", "ALL_ACTIONS", "CATALOG",
    "CODE_REFUSAL", "Component", "CardToolStore", "FAILED_LINE", "KIND_LIFE", "KIND_WORK",
    "TYPE_AUTOMATION", "TYPE_BUTTON", "TYPE_LIST", "TYPE_REMINDER", "TYPE_SHORTCUT",
    "TYPE_TIMER", "TOOLS_DIR", "ToolPolicy", "WORK_EDIT_REFUSAL", "WORK_STAFF_ACTIONS",
    "EDIT_REFUSAL", "WORK_STAFF_REFUSAL", "blocked_reason", "catalog_text",
    "component_id", "default_action",
    "is_tool_request", "looks_like_code", "normalize_components", "policy_for",
    "policy_text", "refusal_line",
]
