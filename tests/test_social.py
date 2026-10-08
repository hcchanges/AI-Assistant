"""
社交帮忙回复扩展（v2-social-reply-ext）的自动测试。

在原 v2-social-reply（识别 + 前台触发 + 起草稿）与 v2-social-flow（[好呀]/[不用了] 轻按钮）
之上，这一轮补三件事：

- ① 社交邀请那排按钮**真弹出来**：修前 `_on_social_offer` 只 `show_offer("social")`，
  按钮排在关着的聊天框里等于没排（与 scene-state 按钮复用失败同源）。现在走 `_pop_offer`，
  把问句 + 按钮一起打开。
- ② 候选回复：点 [好呀] 讲完处境后，给 1 条候选回话，附一句解析（语气/策略）；
  候选框里这条可复制，**没有发送**。（v2-social-single-ui：候选从 3~5 条砍回 1 条。）
- ③ 候选列完后问一句"要不要建对象攻略卡"：[好呀] → 从这次对话抽对方信息（兴趣/爱好/约会地点）
  → 弹可改框 → 建「情感」卡 + 写专属知识库；[不用了] → 只回候选，不建卡。

第四轮 v2-social-fewshot 加了"对方最新消息 + 双路范例"，治真机"答非所问"：

- ④ 截屏 OCR 社交窗口那一屏，把"对方最近说了什么"当生成前提（读完即弃、只存内存）；
- ⑤ 全局 `<聊天范例>/` + 对象攻略卡《聊天范例.md》两路 few-shot（按相关度挑几条）。

红线（v2-social-fewshot 改过口径）：**不模拟按键 / 不碰剪贴板**；截屏 OCR 那两只手的
**实现只在装配层**，服务自己不 import 截屏 / OCR 的库；认到的字**只存内存、看完即弃**，
不落盘、不进 pet.db / mind.db / 卡库。草稿候选框**只有复制和关闭**，起草路径不发新
网络请求。
"""

from __future__ import annotations

import logging
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtWidgets import QApplication, QFrame, QLabel, QPushButton

from life.psych.crisis import CRISIS_REPLY, is_crisis
from life.screen import social as smod
from life.screen.social import DEFAULT_DRAFT_WORDS, SocialReplyService, is_social_proc
from life.screen.social_window import (
    SENDER_OTHER,
    SENDER_USER,
    ChatMessage,
    ChatRead,
)
from ui.chat_bubble import ROLE_SOCIAL_DRAFT

# 认得出的社交软件（都在内置分类表的「社交」一档里）
SOCIAL_HIT = "WeChat.exe"
SOCIAL_HIT2 = "QQ.exe"
# 认得出但不是社交软件
NOT_SOCIAL = ("chrome.exe", "Code.exe", "notepad.exe", "explorer.exe", "steam.exe", "", "   ")

# 明确在求文的说法（该被接走）
WANT_DRAFT = (
    "帮我回一下，同事问我周末加不加班",
    "帮我回复一下领导那条消息",
    "这个人一直追着问，我不知道怎么回他",
    "帮我措辞一下，室友让我帮他带饭我不想带",
    "刚收到条奇怪的私信，帮我拟一段回过去",
)
# **不是**求文的说法（半截话，一个都不许被劫走）
NOT_DRAFT = (
    "我不想回家",
    "这个点该回去了",
    "怎么回去快一点",
    "今天天气不错",
    "帮我看看这个报错",
    "回复率太低了怎么办",
)


def _fake_foreground(monitor, holder) -> None:
    """把前台进程换成可控的假值；窗口标题也堵成空串，免得测试里去摸真 Win32。"""
    import monitor.foreground as fg

    fg.foreground_pid = lambda: 1
    fg.foreground_title = lambda: ""
    monitor._pids.name_of = lambda pid: holder["proc"]


def _sample(monitor, timeout: float = 5.0) -> None:
    monitor.sample()
    wait_for(lambda: not monitor._reading, timeout=timeout)
    pump(0.02)


class Probe:
    """接住"宠物说了什么""候选给了什么"，好断言。"""

    def __init__(self, app) -> None:
        self.spoken: list[str] = []
        self.offers: list[str] = []
        self.candidates: list[list[dict]] = []
        # 两条都要听：社交**邀请**是宠物自己开的口（主动线，v2-speech-polish），
        # 起草稿过程里的"还在写"/"想不好"是**回用户的话**（老口子）。听漏一条，
        # 下面那些"冒了几句"的断言就会无声地变成假绿。
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.pet_speak_proactive.connect(self.spoken.append)
        # "社交这一问冒没冒"**只认这条信号**（v2-social-chat-ui-fix2 提交D）：老口径服务
        # 会先 `pet_speak_proactive` 冒一遍台词，现在那一条删了——问句与按钮都由
        # `social_offer` 这一条信号交出去（装配层 `_pop_offer` 把问句摆进气泡）。
        # 这里补上它，是为了让"冷却里冒了几次""该冒的时候冒没冒"这些断言**还有东西可听**，
        # 不是为了绕过"头顶那句没了"这件事——那件事另有专门的断言盯着（见 S1）。
        app.bus.social_offer.connect(self.offers.append)
        app.bus.social_candidates.connect(self.candidates.append)

    def reset(self) -> None:
        self.spoken.clear()
        self.offers.clear()
        self.candidates.clear()


class _Logs(logging.Handler):
    """把 `life.screen.social` 这一路的日志收进来（v2-social-stale-guard 要看"留没留痕"）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)

    def messages(self, min_level: int = logging.WARNING) -> list[str]:
        return [r.getMessage() for r in self.records if r.levelno >= min_level]


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _file_has(root: Path, text: str) -> list[str]:
    """沙箱里哪些文件里写着 `text`（v2-social-fewshot：验对方原话不落盘）。"""
    needle = text.encode("utf-8")
    hits: list[str] = []
    for path in Path(root).rglob("*"):
        if not path.is_file():
            continue
        try:
            if needle in path.read_bytes():
                hits.append(path.name)
        except OSError:
            continue
    return hits


def _bubble(app) -> list[str]:
    """气泡里现在显示着哪几句（按顺序）。"""
    history = app.chat_bubble._history
    return [history.item(i).text() for i in range(history.count())]


def _code_refs(module) -> set[str]:
    """模块里**代码**引用到的名字（含 import 进来的模块/名字），不含注释与文档字符串。

    红线要查的是"代码里有没有伸这只手"，不是"注释里提没提"——所以走 AST，
    不能用字符串搜索：模块开头正大光明地写着"不读聊天内容、不模拟按键"，
    一搜就自己把自己判违规了（test_game.py 里踩过这个坑）。
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    refs: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
        elif isinstance(node, ast.Import):
            refs.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.add(node.module or "")
            refs.update(alias.name for alias in node.names)
    return refs


def _factory_calls(module) -> int:
    """模块里 `LLMFactory.xxx(...)` 这样的调用点有几个。"""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(module))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id == "LLMFactory"
    )


def _repo_input_apis(root: Path) -> list[str]:
    """全仓库（不含 .venv 与测试目录）里有没有"模拟按键/读剪贴板"这类 API。

    这是"草稿不自动发送"最硬的那条证据：**不是我们克制着不按，是这块代码里没有那只手**。
    扫源码文本而不是 AST——要的就是"连字符串里都不许出现"这个更严的口径。
    """
    banned = ("SendInput", "keybd_event", "SendMessage", "uiautomation", "pyautogui",
              "pynput", "win32clipboard", "SetForegroundWindow")
    hits: list[str] = []
    for path in root.rglob("*.py"):
        parts = set(path.parts)
        if ".venv" in parts or "tests" in parts or "__pycache__" in parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for word in banned:
            if word in text:
                hits.append(f"{path.name}:{word}")
    return hits


class _DialogWatch:
    """现抓"这条路上有没有人建模态框 / 有没有人真去 `exec()`"（v2-social-copy-clean）。

    候选那个模态框已经整档删掉了：候选走宠物自己的非模态气泡 + 一次静默写剪贴板。
    这里临时把 `QDialog.__init__` / `QDialog.exec` 换成记录员——离屏环境里真 `exec()`
    会永远卡住，所以既要断言"没建"，也要能说出"建了谁"。
    """

    def __init__(self) -> None:
        self.built: list[str] = []
        self.executed: list[str] = []

    def __enter__(self) -> "_DialogWatch":
        from PySide6.QtWidgets import QDialog

        self._real_init = QDialog.__init__
        self._real_exec = QDialog.exec

        def _watch_init(dialog, *args, **kwargs):
            self.built.append(type(dialog).__name__)
            return self._real_init(dialog, *args, **kwargs)

        def _watch_exec(dialog):
            self.executed.append(type(dialog).__name__)
            return self._real_exec(dialog)

        QDialog.__init__ = _watch_init        # type: ignore[method-assign]
        QDialog.exec = _watch_exec            # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        from PySide6.QtWidgets import QDialog

        QDialog.__init__ = self._real_init    # type: ignore[method-assign]
        QDialog.exec = self._real_exec        # type: ignore[method-assign]


class _GuideCardRecorder:
    """把对象攻略卡的可改框换成记录员：模拟"保存并建卡 / 取消 / 改过正文"三种结局。"""

    shown: list[tuple[str, object]] = []
    accept = True                 # exec() 返回 Accepted（保存）还是 Rejected（取消）
    edited: str | None = None     # values() 返回改过的正文；None = 原样

    def __init__(self, profile: str, parent=None):
        self.profile = profile or ""
        self.parent = parent
        _GuideCardRecorder.shown.append((self.profile, parent))

    def exec(self) -> int:
        return 1 if _GuideCardRecorder.accept else 0

    def values(self) -> str:
        if _GuideCardRecorder.edited is not None:
            return _GuideCardRecorder.edited
        return self.profile.strip()

    def deleteLater(self) -> None:      # noqa: N802
        pass


def main() -> int:
    box, stub, app = make_env()
    import ast
    import inspect as _inspect

    import core.bootstrap as bootstrap_mod
    from ui.social_draft import GuideCardDialog as RealGuideDialog

    # 先钉住"bootstrap 接的就是那个真建卡框"，再把模态换成记录员（顺序不能反）。
    # 候选那个框整个没了（v2-social-copy-clean），这里少换一个。
    guide_wired = bootstrap_mod.GuideCardDialog is RealGuideDialog
    _GuideCardRecorder.shown.clear()
    _GuideCardRecorder.accept = True
    _GuideCardRecorder.edited = None
    bootstrap_mod.GuideCardDialog = _GuideCardRecorder
    try:
        monitor = app.screen_monitor
        service = app.social_reply
        probe = Probe(app)
        app.config.screen.social_enabled = True
        app.config.screen.social_cooldown_min = 30
        # 本文件验的是老的「冒话邀请 → 讲处境」手动流程；自动监听是另一条新线，
        # 单独在 test_social_auto.py 里关起门来验。这里关掉它，别让那个轻量轮询
        # 计时器插进来干扰老流程的断言。
        app.config.screen.social_auto_listen = False
        check("bootstrap 里已经没有候选框那个类了（v2-social-copy-clean 整档删掉）",
              "SocialCandidatesDialog" not in _inspect.getsource(bootstrap_mod), "")
        check("bootstrap 接的就是 ui/social_draft 那个建卡框", guide_wired, "")

        # ---------- S1 识别层 ----------

        section("S1 识别层：社交软件认得出（复用分类表里现成的「社交」一档）")
        check("微信认得出", is_social_proc(SOCIAL_HIT) is True, "")
        check("全小写也认得出", is_social_proc("wechat.exe") is True, "")
        check("全大写也认得出", is_social_proc("WECHAT.EXE") is True, "")
        check("QQ 认得出", is_social_proc(SOCIAL_HIT2) is True, "")
        check("Telegram 认得出", is_social_proc("Telegram.exe") is True, "")
        check("Discord 认得出", is_social_proc("discord.exe") is True, "")

        section("S1 识别层：非社交软件一律不认（宁可漏判）")
        for proc in NOT_SOCIAL:
            check(f"「{proc or '（空串）'}」不算社交软件",
                  is_social_proc(proc) is False, str(is_social_proc(proc)))
        check("管理员在分类表里改过归类，这里跟着改",
              is_social_proc("someapp.exe", {"someapp.exe": "社交"}) is True, "")

        # ---------- S1 前台触发 ----------

        section("S1 前台触发：社交软件在前台才冒话（走真实的 sample → 信号 → 服务）")
        holder = {"proc": "chrome.exe"}
        _fake_foreground(monitor, holder)
        _sample(monitor)
        probe.reset()
        bubble_before = _bubble(app)
        holder["proc"] = SOCIAL_HIT
        _sample(monitor)
        landed = _bubble(app)[len(bubble_before):]
        check("前台换成社交软件 → 冒了一句邀请", len(probe.offers) == 1,
              f"{len(probe.offers)} 次：{' | '.join(probe.offers) or '（没冒）'}")
        check("冒的那句就是社交邀请台词",
              smod.ASK_LINE in landed, " | ".join(landed) or "（空）")
        # ★ v2-social-chat-ui-fix2 提交D：老口径服务先 `pet_speak_proactive.emit(ASK_LINE)`
        # 让头顶浮动气泡冒一遍，装配层收到 `social_offer` 后再走 `_pop_offer` 把**同一句**
        # 摆进对话框——真机上同一问句冒两遍。现在头顶那条不发，问句只在气泡里这一份。
        check("★ 头顶那条主动冒话信号一个字都没发（同一问句不再冒两遍）",
              smod.ASK_LINE not in probe.spoken, " | ".join(probe.spoken) or "（没说）")
        check("★ 而且气泡里就这一份（不是头顶一份、气泡里又一份）",
              landed.count(smod.ASK_LINE) == 1, " | ".join(landed) or "（空）")
        # 批次11：邀请改短成"要不要我帮你回"这一问，不再在冒话里教一遍怎么用
        # （老口径教的是"把情况讲给我听"，那对应的是"点 [好呀] 还要再讲一遍处境"的流程，
        # 现在点一下当场就拟，没有那一步了）。
        check("邀请问的就是「要不要我帮你回」（不再先教一遍怎么用）",
              "要不要我帮你回" in smod.ASK_LINE, smod.ASK_LINE)

        section("S1 前台触发：非社交软件一个字都不冒")
        service._last_asked = 0.0
        for proc in ("chrome.exe", "Code.exe", "notepad.exe"):
            probe.reset()
            holder["proc"] = proc
            _sample(monitor)
            check(f"「{proc}」不冒社交邀请", not probe.offers and smod.ASK_LINE not in probe.spoken,
                  f"offer {len(probe.offers)} 次 / 说 {len(probe.spoken)} 句")

        section("S1 冷却：同一档里这么多分钟不重复冒话")
        # 冒了几次现在按 `social_offer` 数（提交D：头顶那句台词删了，`probe.spoken` 上
        # 再也看不到这一问）。冷却那两道闸（`_awaiting` / `_last_asked`）一个字没动，
        # 换的只是"从哪条信号上看它冒了没"。
        service._last_asked = 0.0
        service._awaiting = False
        probe.reset()
        holder["proc"] = SOCIAL_HIT
        _sample(monitor)
        first = len(probe.offers)
        check("第一次冒出口", first == 1, f"{first} 次")
        holder["proc"] = "chrome.exe"
        _sample(monitor)
        holder["proc"] = SOCIAL_HIT
        _sample(monitor)
        check("冷却内切走再切回来也不重复冒", len(probe.offers) == 1, f"{len(probe.offers)} 次")

        section("S1 冷却过期后可以再冒（注入时钟，不改真实时间）")
        fake_now = {"t": 5000.0}
        clocked = SocialReplyService(config=app.config, bus=app.bus, clock=lambda: fake_now["t"])
        seen: list[str] = []
        app.bus.social_offer.connect(seen.append)
        clocked.on_foreground(SOCIAL_HIT)
        check("从没冒过就问", len(seen) == 1, f"{len(seen)} 次")
        fake_now["t"] += 60
        clocked.on_foreground(SOCIAL_HIT)
        check("冷却没到不再冒", len(seen) == 1, f"{len(seen)} 次")
        fake_now["t"] += 30 * 60
        clocked.on_foreground(SOCIAL_HIT)
        check("按钮还挂着 → 冷却到了也不重复冒（v2-social-flow：不给他叠第二排）",
              len(seen) == 1, f"{len(seen)} 次")
        clocked._awaiting = False
        clocked.on_foreground(SOCIAL_HIT)
        check("冷却到了、上一问也有结果了 → 可以再冒", len(seen) == 2, f"{len(seen)} 次")
        app.bus.social_offer.disconnect(seen.append)

        section("S3 总闸：social_enabled 关掉之后一句都不冒，也不接活儿")
        app.config.screen.social_enabled = False
        service._last_asked = 0.0
        probe.reset()
        holder["proc"] = SOCIAL_HIT
        _sample(monitor)
        check("关掉后不冒话", not probe.offers and smod.ASK_LINE not in probe.spoken,
              f"offer {len(probe.offers)} 次 / 说 {len(probe.spoken)} 句")
        check("关掉后也不接起草稿的活儿",
              service.is_request("帮我回一下，同事问我周末加不加班") is False, "")
        app.config.screen.social_enabled = True

        # ---------- S2 触发词 ----------

        section("S2 触发词：明确在求文的说法接得住")
        for line in WANT_DRAFT:
            check(f"「{line[:12]}…」认得出是求草稿", service.is_request(line) is True, "")
        check("内置词表非空", len(DEFAULT_DRAFT_WORDS) > 0, f"{len(DEFAULT_DRAFT_WORDS)} 条")

        section("S2 触发词：半截话**一个都不许劫走**（宁可漏判）")
        for line in NOT_DRAFT:
            check(f"「{line}」不当成求草稿", service.is_request(line) is False, "")
        check("「不想回…」没进词表（它会误伤「我不想回家」）",
              not any("不想回" in w for w in DEFAULT_DRAFT_WORDS), "")
        check("「怎么回…」没进词表（它会误伤「怎么回去」）",
              not any(w.startswith("怎么回") for w in DEFAULT_DRAFT_WORDS), "")

        # ---------- S2 候选回复（起草一条 → 1 条候选 + 解析，多给也砍到 1） ----------

        section("S2 候选：聊天里讲处境 → 出 1 条候选，每条附解析")
        place = "帮我回一下，同事问我周末加不加班"
        stub.candidates_result = {"candidates": [
            {"draft": "这周末我可能得把手上的事收个尾，去不了的话我提前跟你说。", "why": "委婉、留余地"},
            {"draft": "这周实在排不开，下次再约。", "why": "直接明确"},
            {"draft": "我先看看，回头跟你确认。", "why": "可进可退"},
        ]}
        stub.prompts.clear()
        probe.reset()
        QApplication.clipboard().setText("动手之前剪贴板里本来放着的东西")
        app._on_chat_send(place)
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        check("候选只出 1 条（模型回了 3 条也砍到 1）",
              len(probe.candidates) == 1 and len(probe.candidates[0]) == 1,
              f"{len(probe.candidates)} 组 / 每组 {len(probe.candidates[0]) if probe.candidates else 0} 条")
        check("每条候选都带解析（why）",
              bool(probe.candidates) and all(c.get("why") for c in probe.candidates[0]),
              str(probe.candidates[0] if probe.candidates else ""))
        # 批次11：`GIVING_LINE`（"我琢磨一下怎么说合适…"）整条删了。用户讲完处境，
        # 手上这个发送按钮已经按成灰的"在想…"（`set_busy(True)`），再补一句口头预报
        # 是多余的——起草这条路上不该再冒这句话。
        check("起草前不再先应一句预报（批次11 删了「我琢磨一下」）",
              not any("琢磨" in s for s in probe.spoken), " | ".join(probe.spoken))
        check("把用户自己说的处境原样交给了模型",
              any(place in p for p in stub.prompts), f"{len(stub.prompts)} 次")
        check("提示词钉了「用他自己的口吻、不是宠物的」",
              any("他自己的口吻" in p for p in stub.prompts), "")
        check("提示词钉了「他没提到的细节一律不许编」",
              any("不许编" in p for p in stub.prompts), "")
        check("提示词钉了「候选只给 1 段」（不再 3~5）",
              any("只给 1 段" in p for p in stub.prompts)
              and not any("3~5" in p for p in stub.prompts), "")
        check("★ 候选真被亮出来了（bootstrap 那条线接上了，走的是宠物自己的气泡）",
              any(probe.candidates[0][0]["draft"] in b for b in _bubble(app)),
              " | ".join(_bubble(app)[-2:]))
        shown_text = "\n".join(_bubble(app))
        check("★ 亮出来的就是这几条候选本身 + 它的解析",
              bool(probe.candidates) and all(
                  probe.candidates[0][0][k] in shown_text for k in ("draft", "why")),
              str(probe.candidates[0][:1]))
        # 批次11：生成那一刻不再替用户静默写剪贴板了。写不写归「复制回复」那一下。
        check("★ 生成那一刻没动剪贴板（原来放着的还在）",
              QApplication.clipboard().text() == "动手之前剪贴板里本来放着的东西",
              repr(QApplication.clipboard().text()))
        check("★ 正文旁边挂着「复制回复」按钮，且手上拿的就是这一版",
              app.chat_bubble._copy_row.isVisible()
              and app.chat_bubble._copy_text == probe.candidates[0][0]["draft"],
              repr(app.chat_bubble._copy_text))
        check("起草完了忙态清干净了（下一段还能接）", service.busy is False, "")

        section("S2 候选：模型回了乱七八糟 / 空数组 → 如实说想不好，不摆空框")
        stub.candidates_result = {"candidates": [
            {"draft": "", "why": "没正文"}, {"draft": "   ", "why": "空白"}, "垃圾",
            {"why": "只有解析没正文"},
        ]}
        probe.reset()
        app._on_chat_send("帮我回一下，房东说要涨租")
        wait_for(lambda: any("想不好" in s for s in probe.spoken))
        drain_threads()
        pump(0.1)
        check("没有一条能用的候选 → 不给空框", not probe.candidates, f"{len(probe.candidates)} 组")
        check("如实说了想不好（不编一段）",
              any("想不好" in s for s in probe.spoken), " | ".join(probe.spoken))

        section("S2 候选：模型整个不可用 → 同样如实说想不好")
        from brain.llm.base import LLMError as _LLMError
        from brain.llm.factory import LLMFactory as _Factory

        original = _Factory.create
        service._running = False
        probe.reset()
        _Factory.create = staticmethod(
            lambda config: (_ for _ in ()).throw(_LLMError("还没填 API Key"))
        )
        try:
            app._on_chat_send("帮我回一下，朋友约我聚会")
            wait_for(lambda: not service.busy)
            drain_threads()
            pump(0.1)
            check("起不来草稿时一条候选都不给", not probe.candidates, f"{len(probe.candidates)} 组")
            check("如实说了想不好（不编一段）",
                  any("想不好" in s for s in probe.spoken), " | ".join(probe.spoken))
            check("失败后忙态清干净了（下次还能接）", service.busy is False, "")
        finally:
            _Factory.create = original

        section("S2 上一段还在写：不叠第二段")
        service._running = True
        probe.reset()
        stub.prompts.clear()
        app._on_chat_send("帮我回一下，同事问我周末加不加班")
        check("正在写时不叠第二段", not probe.candidates, f"{len(probe.candidates)} 组")
        check("正在写时也没再发一次模型请求", not stub.prompts, f"{len(stub.prompts)} 次")
        check("正在写时明说了要等",
              any("还在写" in s for s in probe.spoken), " | ".join(probe.spoken))
        service._running = False

        section("S2 不是求文的话：一个字都不变，照旧走普通回话那条链")
        stub.text = "嗯，聊点别的"
        stub.prompts.clear()
        probe.reset()
        spoken_before = len(probe.spoken)
        app._on_chat_send("今天天气不错")
        wait_for(lambda: len(probe.spoken) > spoken_before)
        drain_threads()
        pump(0.1)
        check("普通话不产生候选", not probe.candidates, f"{len(probe.candidates)} 组")
        check("普通话照旧走聊天那条线",
              any("聊点别的" in s for s in probe.spoken), " | ".join(probe.spoken[-2:]))

        # ---------- v2-social-flow / v2-social-reply-ext：按钮与建卡流程 ----------

        def _social_offer() -> None:
            """把前台切到社交软件，让冒话那条链路真跑一遍（monitor → 信号 → 服务 → 界面）。"""
            service._running = False
            service._awaiting = False
            service._card_awaiting = False
            service._last_asked = 0.0
            probe.reset()
            app.chat_bubble.hide_offer()
            app.chat_bubble.set_busy(False)
            holder["proc"] = "chrome.exe"
            _sample(monitor)
            holder["proc"] = SOCIAL_HIT
            _sample(monitor)

        def _click_offer(label: str) -> None:
            """点气泡上那排轻按钮（走真实的 clicked → _reply_offer → offer_reply 信号）。"""
            from PySide6.QtWidgets import QPushButton

            row = app.chat_bubble._offer_row
            texts = [b.text() for b in row.findChildren(QPushButton)]
            matched = [b for b in row.findChildren(QPushButton) if b.text() == label]
            assert matched, f"气泡上没有「{label}」这个按钮：{texts}"
            app.chat_bubble.set_busy(False)
            matched[0].click()
            pump(0.05)

        section("S4-① 社交软件在前台 → 冒话之外，气泡真弹出来、摆出 [好呀] [不用了]")
        _social_offer()
        # 提交D：这一问现在只走 `social_offer` 一条信号，问句落在气泡里。
        check("那句邀请照旧冒了（气泡里那一份）",
              smod.ASK_LINE in _bubble(app), " | ".join(_bubble(app)[-2:]) or "（没冒）")
        check("★ 头顶那条没跟着再冒一遍", smod.ASK_LINE not in probe.spoken,
              " | ".join(probe.spoken) or "（没说）")
        check("气泡上摆了社交这排按钮（修前只冒话，一个可点的东西都没有）",
              app.chat_bubble._offer_theme == "social",
              app.chat_bubble._offer_theme or "（没摆）")
        check("按钮是真露出来的，不是隐着的（点开聊天就能看到）",
              not app.chat_bubble._offer_row.isHidden(),
              f"隐藏着={app.chat_bubble._offer_row.isHidden()}")
        from PySide6.QtWidgets import QPushButton

        offer_labels = [b.text() for b in app.chat_bubble._offer_row.findChildren(QPushButton)]
        check("两个按钮就是 [好呀] [不用了]", offer_labels == ["好呀", "不用了"], str(offer_labels))
        check("服务这边也记着「正等答复」", service.awaiting_confirm is True, "")
        # v2-social-reply-ext ①：修的是"按钮藏在关着的框里"。这里顺带钉一下——邀请弹出后，
        # 聊天框本身是打开的（`_pop_offer` 打开它），而不是只在隐藏框里排了一排按钮。
        check("聊天框被打开了（不是藏在关着的框里）", app.chat_bubble.isVisible(), "")

        section("S4-② 点 [好呀] → 当场照对方末句拟一段（批次11：不再先问处境）")
        stub.candidates_result = {"candidates": [
            {"draft": "行，那我周末先过去一趟，具体几点我回头跟你确认。", "why": "留余地，可进可退"},
            {"draft": "这周我实在腾不出手，下次再约。", "why": "直接拒绝，但客气"},
        ]}
        stub.prompts.clear()
        probe.reset()
        # 批次17（甲）：点 [好呀] 时手上得有"对方原话"才拟。这一段验的是"不再先问处境、
        # 当场照对方末句拟"，所以先照真机的样子把认回来的那一屏摆上（人在微信里看过对方
        # 末句、再点回宠物这扇窗看见按钮）。**手上一字没有那一档**由批次17 那个测试文件
        # （`test_social_await_snap.py` ①）钉：那时宠物如实说一句，不拟。
        service._chat_text = "那你周六到底来不来呀"
        service._chat_text_at = service._clock()
        _click_offer("好呀")
        # 批次11 改的就是这一步：老口径点完 [好呀] 会再问一句"那你说说是什么情况"
        # （`NEED_SITUATION_LINE`），把用户接着打的那句当处境。现在点一下**当场**就拟，
        # 处境那格直接留空——所以下面这一条的判据从"问了没"翻成了"没问"。
        check("点头之后没再问一句「是什么情况」",
              not any("什么情况" in s for s in probe.spoken), " | ".join(probe.spoken))
        check("社交那一问收掉了（不再等用户点 [好呀]）", service.awaiting_confirm is False, "")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        check("当场就出候选（不用再讲一遍处境）",
              len(probe.candidates) == 1, f"{len(probe.candidates)} 组")
        check("候选交给界面的是模型回的正文",
              bool(probe.candidates) and any("回头跟你确认" in c["draft"] for c in probe.candidates[0]),
              str(probe.candidates[0][:1] if probe.candidates else "（空）"))
        check("★ 处境那格真按空给（提示词里那条「空的怎么办」跟着落地了）",
              any("也可能是空的" in p for p in stub.prompts), f"{len(stub.prompts)} 次")
        check("**没有自动发出去**：仓里没有发送那只手（见下面 S3 那一段）",
              not any("发出去了" in s or "已发送" in s for s in probe.spoken),
              " | ".join(probe.spoken))
        check("这一轮完了，忙态清干净", service.busy is False, "")
        check("这一轮走完也没留下「等处境」那档状态（批次11 起没人再装那个窗口）",
              service._waiting_for_situation() is False, "")

        # **这里刻意不再补一发"讲处境"**：批次11 把"正等处境"那个窗口拆了之后，点完
        # [好呀] 再打字走的是通用聊天（`is_request` 那条腿照旧，只是没人再替他布防）。
        # 要再拟一版得明说"帮我回"——那条手动路 S2 已经验过，再打字那条由下面 S4-⑫ 验。

        section("S4-③ 候选列完 → 自动问「要不要建对象攻略卡」")
        check("候选框弹过之后，正等建卡那一问的答复", service.awaiting_card is True, "")
        check("气泡上摆了「social_card」这排按钮",
              app.chat_bubble._offer_theme == "social_card",
              app.chat_bubble._offer_theme or "（没摆）")
        check("按钮还是 [好呀] [不用了]",
              [b.text() for b in app.chat_bubble._offer_row.findChildren(QPushButton)]
              == ["好呀", "不用了"],
              str([b.text() for b in app.chat_bubble._offer_row.findChildren(QPushButton)]))

        section("S4-⑭ 建卡询问独立：游戏态来了也不把「要不要建卡」顶掉（v2-social-reply-fix）")
        # 真机回归：候选列完、正等「要不要建对象攻略卡」的答复时，前台若被误判成游戏
        # （宠物自己挂在 pythonw.exe 上跑），游戏攻略那句会把 social_card 这排按钮盖掉。
        # 修法：社交建卡询问是最高优先级，游戏攻略问按钮前先过 `_social_offer_pending`。
        app.config.screen.guide_ask_delay_sec = 0
        game_offers: list[str] = []
        app.bus.game_offer.connect(game_offers.append)
        probe.reset()
        holder["proc"] = "League of Legends.exe"
        _sample(monitor)
        drain_threads()
        pump(0.05)
        check("游戏攻略没冒话（社交建卡询问最高优先）", not game_offers,
              f"offers={game_offers}")
        check("游戏攻略没留下等待态", app.game_guide.awaiting_confirm is False, "")
        check("气泡上还是「social_card」这排按钮（没被顶掉）",
              app.chat_bubble._offer_theme == "social_card",
              app.chat_bubble._offer_theme or "（被顶掉了）")
        check("社交那边还端着「等建卡答复」", service.awaiting_card is True, "")
        app.bus.game_offer.disconnect(game_offers.append)
        app.config.screen.guide_ask_delay_sec = 30

        section("S4-④ 点 [不用了] → 只回候选、不建卡、不多说")
        _GuideCardRecorder.shown.clear()
        _click_offer("不用了")
        drain_threads()
        pump(0.05)
        check("拒绝后不再等着建卡", service.awaiting_card is False, "")
        check("没弹建卡那个可改框", not _GuideCardRecorder.shown, f"{len(_GuideCardRecorder.shown)} 次")
        check("没有新建卡", app.card_service.active_card_of_theme(smod.CARD_CATEGORY) is None,
              str(app.card_service.active_card_of_theme(smod.CARD_CATEGORY)))

        section("S4-⑤ 点 [好呀] → 抽对方信息 → 可改框 → 建卡 + 写专属知识库")
        # 重跑一遍候选 → 建卡这一问，然后点 [好呀]
        stub.candidates_result = {"candidates": [
            {"draft": "这周我实在腾不出手，下次再约。", "why": "直接拒绝，但客气"},
        ]}
        stub.profile_result = {"profile": "对方是谁：室友\n兴趣/爱好：打游戏、跑步\n约会地点：楼下健身房"}
        _social_offer()
        # 批次11：这一席的原料得靠**明说"帮我回"**递进去（点 [好呀] 那条路给的是空处境，
        # `extract_profile` 抽的就是这份 `last_request`，空的话抽不出东西）。
        app._on_chat_send("帮我回一下，室友约我周末一起跑步，可我这周没空")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        check("前置：又到「要不要建卡」这一问", service.awaiting_card is True, "")

        _GuideCardRecorder.shown.clear()
        _GuideCardRecorder.accept = True
        _GuideCardRecorder.edited = None
        _click_offer("好呀")
        wait_for(lambda: bool(_GuideCardRecorder.shown))
        drain_threads()
        pump(0.1)
        check("抽出来的对方信息弹进了可改框（C 混合：自动抽 + 用户可改）",
              bool(_GuideCardRecorder.shown) and "室友" in _GuideCardRecorder.shown[0][0]
              and "跑步" in _GuideCardRecorder.shown[0][0],
              _GuideCardRecorder.shown[0][0] if _GuideCardRecorder.shown else "（没弹）")
        check("抽信息时原料是用户自己讲的那段处境（不读聊天正文）",
              any("室友约我周末一起跑步" in p and '"profile"' in p for p in stub.prompts),
              f"{len(stub.prompts)} 次提示词")
        card = app.card_service.active_card_of_theme(smod.CARD_CATEGORY)
        check("建了一张「情感」大类的对象攻略卡",
              card is not None and card.title == smod.GUIDE_CARD_TITLE,
              f"{card.title if card else '（没建）'}")
        kb_file = Path(card.kb_path) / "对方信息.md"
        check("对方信息写进了这张卡的专属知识库",
              kb_file.exists() and "跑步" in kb_file.read_text(encoding="utf-8"),
              str(kb_file))
        check("宠物交代了一句「建好了」",
              any("对象攻略卡" in s and "建好" in s for s in probe.spoken),
              " | ".join(probe.spoken))

        section("S4-⑥ 用户把抽出来的信息改了 → 落库的是改过的版本（用户可改）")
        def _emotion_cards():
            return [c for c, _ in app.card_service.plaza() if c.theme == smod.CARD_CATEGORY]

        before = len(_emotion_cards())
        service._last_request = "她喜欢喝手冲咖啡，周末常去美术馆"
        stub.profile_result = {"profile": "对方是谁：相亲对象\n兴趣/爱好：咖啡、看展"}
        _GuideCardRecorder.shown.clear()
        _GuideCardRecorder.accept = True
        _GuideCardRecorder.edited = "对方是谁：相亲对象\n兴趣/爱好：手冲咖啡、逛美术馆\n备注：别约她吃辣"
        service.extract_profile(
            on_done=lambda profile: app._on_profile_extracted(profile),
            on_failed=lambda exc: None,
        )
        # 这一段原来断言的是"又建了第二张情感卡"——那是 **v2-social-card-dedup 之前的
        # 行为**：同一问再来一轮就再建一张同名的卡，真机上就是"对象攻略卡攒了好几张"。
        # 现在同类同名不再重复建（`create_manual_card` 交回既有那张），所以这里改验
        # **宁可验的那件事**：用户改过的正文真的落库了——落点是**既有那张**的专属库。
        # 走的是"那一份写进去了没有"这条判据，不是"卡多了没有"。
        guide = app.card_service.find_card(smod.CARD_CATEGORY, smod.GUIDE_CARD_TITLE)
        kb_file = Path(guide.kb_path) / "对方信息.md" if guide is not None else None
        wait_for(lambda: bool(kb_file) and kb_file.exists()
                 and "别约她吃辣" in kb_file.read_text(encoding="utf-8"))
        drain_threads()
        pump(0.1)
        check("没有重复建卡（同类同名不再建第二张，v2-social-card-dedup）",
              len(_emotion_cards()) == before, f"{before} → {len(_emotion_cards())}")
        check("可改框拿到的就是用户改过的那份（写进的是既有那张的库）",
              bool(kb_file) and kb_file.exists()
              and "别约她吃辣" in kb_file.read_text(encoding="utf-8"),
              str(kb_file))

        section("S4-⑦ 用户在可改框里取消 → 不建卡")
        before = len(_emotion_cards())
        _GuideCardRecorder.shown.clear()
        _GuideCardRecorder.accept = False
        service._last_request = "同事约我周末爬山"
        stub.profile_result = {"profile": "对方是谁：同事\n兴趣/爱好：爬山"}
        service.extract_profile(
            on_done=lambda profile: app._on_profile_extracted(profile),
            on_failed=lambda exc: None,
        )
        wait_for(lambda: bool(_GuideCardRecorder.shown))
        drain_threads()
        pump(0.1)
        check("取消后没有新建卡", len(_emotion_cards()) == before, f"{before} → {len(_emotion_cards())}")
        check("宠物说了句「先不建」",
              any("先不建" in s for s in probe.spoken), " | ".join(probe.spoken))

        section("S4-⑧ 点 [不用了] → 静默陪伴：一句都不说、不追问、不留候选（初次邀请）")
        _social_offer()
        probe.reset()
        _click_offer("不用了")
        drain_threads()
        pump(0.05)
        check("拒绝之后一个字都没有", not probe.spoken, " | ".join(probe.spoken) or "（安静）")
        check("也没有候选", not probe.candidates, f"{len(probe.candidates)} 组")
        check("按钮收掉了", app.chat_bubble._offer_theme == "",
              app.chat_bubble._offer_theme or "（已收）")
        check("服务不再等着答复", service.awaiting_confirm is False, "")
        check("也不留「等处境」这一档（他什么都没要）",
              service._waiting_for_situation() is False, "")

        section("S4-⑨ 用户在框里自己说了话 → 挂着的那一问跟着作废（不留死结）")
        _social_offer()
        check("前置：这会儿正挂着等答复", service.awaiting_confirm is True, "")
        stub.text = "嗯，聊点别的"
        app._on_chat_send("随便说点什么")
        drain_threads()
        pump(0.05)
        check("按钮收了，服务那边也不再端着（修前会一直卡在「等答复」，再也不冒话）",
              service.awaiting_confirm is False, "")

        section("S4-⑩ 对方原话：社交窗那一屏认出来的字进了起草提示词（v2-social-fewshot）")
        # 截屏 / OCR 那两只手的**实现**在装配层；测试里换成可控的假帧 + 假字。
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: "茗风：今天玩得开心吗 你周末有什么安排"
        service._examples_provider = lambda query: ""
        service._chat_text = ""
        service._chat_reading = False
        stub.candidates_result = {"candidates": [
            {"draft": "挺开心的！就出去转了转，你呢？", "why": "日常寒暄，同分量回"},
        ]}
        stub.prompts.clear()
        probe.reset()
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        pump(0.05)
        check("社交窗那一屏被认成了字，只存在内存里",
              "今天玩得开心吗" in service._chat_text, service._chat_text or "（空）")
        app._on_chat_send("帮我回一下")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        draft_seen = [p for p in stub.prompts if "他自己的口吻" in p]
        latest = draft_seen[-1] if draft_seen else ""
        check("对方最新那条原样进了起草提示词", "今天玩得开心吗" in latest,
              latest[:80].replace("\n", " / "))
        check("提示词钉着主药：先回应对方那条、不许升级话题",
              "直接回应它" in latest and "不许自己把话题升级" in latest, "")
        check("对方的话用掉即弃：起草完内存里清空了", service._chat_text == "",
              service._chat_text or "（已清空）")

        section("S4-⑩b 多行消息整条进链（v2-social-multiline）：不丢行、不切半句")
        # 真机症状：对方一条消息两行（「这么快就放弃啦？\n我比较欣赏有毅力的男孩哟」），
        # 宠物只答末半句。这一节从**装配层回 ChatRead** 那一头开始，一路验到提示词。
        #
        # `groups` 这一格是**非有不可**的（v2-social-ui-batch 批次8-A 起）：那两行是
        # **同一条**消息（`classify_lines` 按气泡框拼回来的那条），材料只取视觉最底部那一条
        # 对方气泡——不给 `groups` 的话，`messages` 就退化成"一行算一条"，那两行成了两条，
        # 这一节验的"两行都得留着"当场变成"只留末行"。摆对了，才是真机那条折行的气泡。
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: ChatRead(
            lines=[
                (SENDER_OTHER, "这么快就放弃啦？"),
                (SENDER_OTHER, "我比较欣赏有毅力的男孩哟"),
            ],
            groups=[ChatMessage(sender=SENDER_OTHER,
                                lines=["这么快就放弃啦？", "我比较欣赏有毅力的男孩哟"])],
        )
        service._examples_provider = lambda query: ""
        service._chat_text = ""
        service._chat_reading = False
        stub.candidates_result = {"candidates": [
            {"draft": "没放弃呀，刚在忙。那就当你在夸我。", "why": "先接住试探，再收评价"},
        ]}
        stub.prompts.clear()
        probe.reset()
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        pump(0.05)
        check("★ 两行**都**进了服务手里的对方原话（修前只留末行那一句）",
              "这么快就放弃啦" in service._chat_text
              and "我比较欣赏有毅力的男孩哟" in service._chat_text,
              service._chat_text or "（空）")
        check("★ 两行之间还是**换行**，没被拍成一串空格",
              "\n" in service._chat_text
              and service._chat_text.index("这么快就放弃")
              < service._chat_text.index("我比较欣赏"),
              repr(service._chat_text))
        app._on_chat_send("帮我回一下")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        draft_seen = [p for p in stub.prompts if "他自己的口吻" in p]
        latest = draft_seen[-1] if draft_seen else ""
        prompt_rows = latest.split("\n")
        top = next((k for k, ln in enumerate(prompt_rows) if "这么快就放弃" in ln), None)
        bottom = next((k for k, ln in enumerate(prompt_rows) if "我比较欣赏" in ln), None)
        check("★ 提示词里那两行是**分开的行**、上下顺序不乱（模型这才看得出同一条说了两件事）",
              top is not None and bottom is not None and top < bottom,
              f"行下标 {top} / {bottom}")
        check("提示词里同时教了'多行是同一条、每行都要接'",
              "每一行都要接住" in latest and "只答末行" in latest, "")
        check("多行这一档用完照样清空", service._chat_text == "", service._chat_text or "（已清空）")

        section("S4-⑩c 最新那条**一长**时也按整行留尾，不切在半句上")
        # 老口径 `body[-600:]`：切点落在最新那条句子中间——真机丢的就是开头那句试探。
        prologue = "\n".join(f"旧消息第{i}行" for i in range(1, 30))
        long_tail = "「" + "很长的最新一条" * 120 + "」"          # 本身长过 600 字上限
        service._read_chat = lambda array: ChatRead(lines=[
            (SENDER_OTHER, prologue),
            (SENDER_OTHER, long_tail),
        ])
        service._chat_text = ""
        service._chat_reading = False
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        pump(0.05)
        captured = service._chat_text
        check("★ 最新的长那条**整条都在**（开头没被削掉）",
              captured.startswith("「") and captured.endswith("」"),
              f"{len(captured)} 字 / 开头 {captured[:8]!r} / 结尾 {captured[-8:]!r}")
        check("更旧的那几行被按**整行**留在外面（留尾那个初衷没丢）",
              "旧消息第1行" not in captured, captured[:40].replace("\n", " / "))
        src_rows = (prologue + "\n" + long_tail).split("\n")
        check("留下的每一行都在原文里**逐行**出现（不会出现半截行）",
              all(ln in src_rows for ln in captured.split("\n")), "")

        section("S4-⑮ 对方寒暄 → 回寒暄，没脑补成别的（图证场景反向锁）")
        # 图证：对方只问了句日常寒暄，宠物却生成"关于咱们俩的事，我想认真聊一下"。
        # 现在对方那句原话进了提示词，模型手里就有"这是寒暄"这个前提。
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: "茗风：今天玩得开心吗"
        service._chat_text = ""
        service._chat_reading = False
        stub.candidates_result = {"candidates": [
            {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄，不升级"},
        ]}
        stub.prompts.clear()
        probe.reset()
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        app._on_chat_send("帮我回一下茗风")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("对方那句寒暄在提示词里（是生成的前提，不是宠物自己脑补的）",
              "今天玩得开心吗" in latest, "")
        check("寒暄这条要求明确在场（他说寒暄就回寒暄）",
              "就回一句同样分量的日常寒暄" in latest, "")

        section("S4-⑯ 认不到对方那条 → 提示词如实说没认到，不猜")
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: ""
        service._chat_text = ""
        service._chat_reading = False
        stub.prompts.clear()
        probe.reset()
        _social_offer()
        wait_for(lambda: service._chat_reading is False)
        drain_threads()
        app._on_chat_send("帮我回一下领导")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("那格填的是「没认到」，说明白没认到、别猜",
              smod.NO_CHAT_CONTEXT in latest, "")

        section("S4-⑰ 全局范例：按相关度挑最相关的几条（v2-social-fewshot）")
        from life.screen.social_examples import SocialExamples

        ex_dir = box.dir / "chat_examples"
        ex_dir.mkdir(parents=True, exist_ok=True)
        (ex_dir / "我的说法.md").write_text(
            "同事喊我周末加班，我说：这周真排不开，下周我来顶。\n\n"
            "朋友问我啥时候有空，我说：等我忙完这阵子约你啊。\n",
            encoding="utf-8",
        )
        examples = SocialExamples(app.config)
        picked = examples.for_query("同事问我周末加不加班")
        check("全局范例被读到了", "这周真排不开" in picked, picked.replace("\n", " / "))
        check("跟这次话题最沾的那条排第一",
              picked.splitlines()[0].startswith("- 同事喊我周末加班"), picked.replace("\n", " / "))
        check("一条都没命中时回空串（不现编）",
              SocialExamples(app.config).for_query("") == "", "")
        # 范例经 provider 进提示词（服务那一格接的正是 for_query）
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: "同事：周末来加个班呗"
        service._examples_provider = examples.for_query
        service._chat_text = ""
        service._chat_reading = False
        stub.prompts.clear()
        probe.reset()
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        app._on_chat_send("帮我回一下同事")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("范例真的进了提示词那一格", "这周真排不开" in latest, "")
        service._examples_provider = lambda query: ""

        section("S4-⑱ 卡级范例：按名字匹配《对方信息.md》，认准哪张攻略卡")
        kb7 = box.dir / "cards_kb" / "7"
        kb7.mkdir(parents=True, exist_ok=True)
        (kb7 / "对方信息.md").write_text("对方是谁：茗风\n兴趣：爬山、露营\n", encoding="utf-8")
        (kb7 / "聊天范例.md").write_text(
            "茗风：今天玩得开心吗\n我：挺开心的，你呢？\n", encoding="utf-8")

        class _FakeCard:
            kb_path = str(kb7)

        ex2 = SocialExamples(app.config, cards_provider=lambda: [_FakeCard()])
        check("按名字认出了茗风那张攻略卡",
              ex2.guide_card_for("茗风问我今天玩得开心吗") is not None, "")
        got = ex2.for_query("茗风问我今天玩得开心吗")
        check("那张卡的《聊天范例.md》被读进来", "挺开心的" in got, got.replace("\n", " / "))
        check("不相干的人不认这张卡（宁可不摆，也不乱配）",
              ex2.guide_card_for("老王问我要不要一起吃饭") is None, "")

        section("S4-⑲ 主动求助也抓屏：不过邀请、直接喊「帮回」也有对方原话（v2-social-ocr-path）")
        # 真机图证：微信茗风回"还好啊，感觉还挺愉快的"，用户没过那扇邀请（冷却没到 /
        # 上一问还挂着），直接跑回"AI助理"喊帮回——旧代码只在冒邀请那一拍抓屏，
        # 这一拍没冒话就没抓，提示词只能填"没认到"，于是自己脑补、跑题。
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: "茗风：还好啊，感觉还挺愉快的"
        service._examples_provider = lambda query: ""
        service._chat_text = ""
        service._chat_reading = False
        # 前置：这一拍**不冒邀请**（上一问还挂着按钮），但抓屏照旧该发生。
        service._awaiting = True
        probe.reset()
        service.on_foreground(SOCIAL_HIT)
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        pump(0.05)
        check("不冒邀请这一拍也抓了屏（抓屏与冒话解耦）",
              bool(service._chat_text), service._chat_text or "（空）")
        check("抓到的是茗风那条原话（不是空、也不是用户自己的提问）",
              "还好啊，感觉还挺愉快的" in service._chat_text, service._chat_text or "（空）")
        service._awaiting = False
        # 主动求助：用户切到宠物这儿喊帮回（前台已经不是社交软件），手上那屏字还在，
        # 起草照用，不跑题。
        service._social_foreground = False
        stub.candidates_result = {"candidates": [
            {"draft": "嗯，还行，就是正常聊聊天。", "why": "寒暄回寒暄，不升级"},
        ]}
        stub.prompts.clear()
        probe.reset()
        app._on_chat_send("帮我回一下茗风")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("主动求助的草稿也带着茗风那条（没跑题）",
              "还好啊，感觉还挺愉快的" in latest, latest[:80].replace("\n", " / "))
        check("对方的话用掉即弃：起草完内存里又清空了", service._chat_text == "",
              service._chat_text or "（已清空）")

        section("S4-⑳ 社交软件还在前台时喊帮回 → 当场再抓一屏（覆盖旧值）")
        # 另一种入口：用户可能语音喊的、没切走前台，社交软件还挂在前台。这一拍
        # handle_request 要再抓一屏刷新对方原话，而不是端着上一场（甚至上一场是空的）。
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: "茗风：那我们下周见"
        service._examples_provider = lambda query: ""
        service._chat_text = "上一场的旧值（该被覆盖）"
        service._chat_reading = False
        # 让 _social_foreground = True：前台还是社交软件。
        service._social_foreground = True
        stub.candidates_result = {"candidates": [
            {"draft": "好呀，下周见。", "why": "爽快答应"},
        ]}
        stub.prompts.clear()
        probe.reset()
        app._on_chat_send("帮我回一下茗风")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("前台还在社交软件 → 当场再抓的那句进了提示词（旧值被覆盖）",
              "那我们下周见" in latest and "上一场的旧值" not in latest,
              latest[:80].replace("\n", " / "))
        service._social_foreground = False

        section("S4-㉑ OCR 关掉（取不到帧）→ 降级明说没认到，不瞎编")
        service._grab_chat = lambda: None
        service._read_chat = lambda array: "不该被读到"
        service._chat_text = ""
        service._chat_reading = False
        stub.candidates_result = {"candidates": [
            {"draft": "我回头看看再回你。", "why": "留余地"},
        ]}
        stub.prompts.clear()
        probe.reset()
        app._on_chat_send("帮我回一下领导")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        latest = [p for p in stub.prompts if "他自己的口吻" in p][-1]
        check("取不到帧 → 那格如实填「没认到」", smod.NO_CHAT_CONTEXT in latest, "")
        check("没把抓不到的东西编进提示词", "不该被读到" not in latest, "")

        section("S4-㉒ 取末尾不取开头：最新那句（对方回复）压在下面，旧字别带跑")
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: ("帮我回一下领导这条（用户自己发的提问）" + "旧消息" * 200
                                            + " 茗风：那我们下周见")
        service._chat_text = ""
        service._chat_reading = False
        service._social_foreground = True
        service._pending_draft = None
        _ok = service._read_social_screen()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        pump(0.05)
        check("长屏里留的是末尾那段（对方最新那句在里面）",
              "茗风：那我们下周见" in service._chat_text, "")
        check("开头那段（用户自己的提问）被末尾截断挡掉了",
              "帮我回一下领导这条（用户自己发的提问）" not in service._chat_text, "")
        service._social_foreground = False
        service._grab_chat = lambda: None
        service._read_chat = None
        service._chat_text = ""

        section("S4-㉓ 抓不成时不用上一屏旧字静默起草（v2-social-stale-guard）")
        # 真机上报的形状：上一场认到的字还挂在内存里（几分钟前、另一场戏），这一拍
        # 前台已经是宠物自己（用户点回宠物的聊天窗说"帮我回"）→ 装配层这一拍不采、
        # `_grab_chat()` 回 None。老口径会**静默**把那屏旧字当"对方刚发来的那条"
        # 交出去，用户看到的就是答非所问，而且一行日志都没有。
        stale_line = "上一场认到的旧话（不该拿来充数）"
        fresh_line = "茗风：刚认回来的那句（还在新鲜期内）"
        fake = {"t": 50_000.0}
        real_clock2 = service._clock
        logs = _Logs()
        social_logger = logging.getLogger("life.screen.social")
        # 测试起的这套日志根logger 拧在 ERROR 上（跑起来才干净），warning 会被挡在
        # logger 那一层、连 handler 都进不来——**先把这个 logger 自己放开到 DEBUG**，
        # 收完再还原。不放开的话这条"有没有留痕"的断言会永远红/永远绿，跟代码无关。
        real_level = social_logger.level
        social_logger.setLevel(logging.DEBUG)
        social_logger.addHandler(logs)
        try:
            service._clock = lambda: fake["t"]
            service._grab_chat = lambda: None          # 前台是宠物自己：这一拍采不到
            service._read_chat = lambda array: "不该被读到"
            service._examples_provider = lambda query: ""
            service._social_foreground = True          # 社交那条线还标着"在前台"
            service._chat_reading = False
            stub.candidates_result = {"candidates": [
                {"draft": "好呀，回头聊。", "why": "寒暄"},
            ]}

            def _ask() -> str:
                """喊一句"帮我回"，回来的是这一次发出去的起草提示词。"""
                stub.prompts.clear()
                probe.reset()
                app._on_chat_send("帮我回一下茗风")
                wait_for(lambda: bool(probe.candidates))
                drain_threads()
                pump(0.1)
                seen = [p for p in stub.prompts if "他自己的口吻" in p]
                return seen[-1] if seen else ""

            # ① 过期（认回来已经超过新鲜期）→ 当没有
            logs.records.clear()
            service._chat_text = stale_line
            service._chat_text_at = fake["t"] - (smod.CHAT_TEXT_FRESH_S + 5)
            prompt = _ask()
            check("★ 过期的旧字一个字都没进提示词（不当成「对方刚发来的那条」）",
                  stale_line not in prompt, prompt[:0] or f"有没有：{stale_line in prompt}")
            check("★ 那格如实填「没认到」，让模型别猜",
                  smod.NO_CHAT_CONTEXT in prompt, "")
            warns = logs.messages(logging.WARNING)
            check("★ 这一拍留了一条 warning（不再静默回退旧字）",
                  any("不可采" in w for w in warns), " / ".join(warns) or "（一条都没有）")
            check("旧字也照样取走即弃（内存里不残留）", service._chat_text == "",
                  service._chat_text or "（已清空）")

            # ② 还在新鲜期内 → 照用（"在微信看完消息、点回宠物喊帮回"那条主路不许掐死）
            logs.records.clear()
            service._chat_text = fresh_line
            service._chat_text_at = fake["t"] - 5.0
            prompt = _ask()
            check("★ 刚认回来的那屏字照用（切回宠物喊帮回这条主路没被一起掐死）",
                  fresh_line in prompt, prompt[:0] or f"有没有：{fresh_line in prompt}")
            check("这一档不该说「没认到」（有原话就说有）",
                  smod.NO_CHAT_CONTEXT not in prompt, "")
            check("这一档也不留 warning（它是正常路径，不是降级）",
                  not logs.messages(logging.WARNING),
                  " / ".join(logs.messages(logging.WARNING)) or "（干净）")

            # ③ 抓不成、手上也没有字 → 照样留痕，别无声无息
            logs.records.clear()
            service._chat_text = ""
            service._chat_text_at = 0.0
            prompt = _ask()
            check("③ 手上没字 → 那格填「没认到」", smod.NO_CHAT_CONTEXT in prompt, "")
            check("③ 也留了一条 warning（社交窗不可采这件事值得留痕）",
                  any("不可采" in w for w in logs.messages(logging.WARNING)),
                  " / ".join(logs.messages(logging.WARNING)) or "（一条都没有）")
        finally:
            service._clock = real_clock2
            social_logger.removeHandler(logs)
            social_logger.setLevel(real_level)
            service._social_foreground = False
            service._grab_chat = lambda: None
            service._read_chat = None
            service._chat_text = ""
            service._chat_text_at = 0.0

        section("S4-⑪ 上下文正确：按他这次给的处境写，不串场、不跑题")
        stub.candidates_result = {"candidates": [
            {"draft": "我下周手上这个得先交出去，出差的事我回头跟你确认。", "why": "留余地"},
        ]}
        stub.prompts.clear()
        probe.reset()
        second = "领导问我下周能不能出差，我不想去"
        _social_offer()
        # 批次11：处境得**明着喊"帮我回"**才递得进去（点 [好呀] 那条路给的是空处境，
        # 点完再打字走通用聊天）。
        app._on_chat_send(f"帮我回一下，{second}")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        draft_seen = [p for p in stub.prompts if "他自己的口吻" in p]
        latest = draft_seen[-1] if draft_seen else ""
        check("这一次的处境原样带进去了", second in latest, latest[:60].replace("\n", " / "))
        check("上一次那件事没有跟着串进来（不串场）",
              "周末帮他搬东西" not in latest and "什么时候结婚" not in latest, "")
        check("宠物别的事也没混进来（不跑题）",
              not any(w in latest for w in ("番茄钟", "知识库", "抓屏", "这一屏")), "")

        section("S4-⑫ 点 [好呀] 之后那扇「等处境」的窗根本没装：再打字走通用聊天，不被劫")
        # 批次11 拆的就是这一段。老口径点完 [好呀] 会布一个 180 秒的窗口（`SITUATION_WINDOW_S`），
        # 把用户接着打的那句当处境收走；这个测试原来验的是"窗口过了就不收"。
        # 现在窗口**一次都不装**（`confirm()` 直接照对方末句拟）——所以就没有"过没过"这回事，
        # 之后什么时候打字都是通用聊天。下面四条把这件事两头都钉住：当场拟了 + 窗没装 +
        # 闲话不被劫 + 闲话照旧走聊天。
        stub.candidates_result = {"candidates": [
            {"draft": "行，那我回头跟你说。", "why": "留余地"},
        ]}
        stub.text = "嗯，聊点别的"
        probe.reset()
        service._awaiting = True
        # 批次17（甲）：同上——手上摆着刚认回来的对方末句（没有那一档不拟，见批次17 测试）
        service._chat_text = "那你周六到底来不来呀"
        service._chat_text_at = service._clock()
        service.confirm("好呀")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        check("点完 [好呀] 当场就拟了一版（不等人再讲一遍处境）",
              len(probe.candidates) == 1, f"{len(probe.candidates)} 组")
        check("★ 那扇「等处境」的窗一次都没装起来",
              service._waiting_for_situation() is False
              and service._want_situation_until == 0.0,
              f"until={service._want_situation_until}")
        stub.prompts.clear()
        probe.reset()
        app._on_chat_send("今天天气不错")
        wait_for(lambda: len(probe.spoken) > 0)
        drain_threads()
        pump(0.1)
        check("之后一句闲话不被当成处境写草稿", not probe.candidates, f"{len(probe.candidates)} 组")
        check("闲话照旧走普通聊天那条线（一个字都没变）",
              any("聊点别的" in s for s in probe.spoken), " | ".join(probe.spoken))
        service._awaiting = False
        service._want_situation_until = 0.0

        section("S4-⑬ 红线复核：社交那条线还挂着的时候来一句危机话，照样归危机话术")
        # 批次11 之前这一节说的是"等处境的时候"——那扇窗没了，但**这根红线一个字没动**：
        # `handle_request` 的第一道闸仍然是危机词（在"算不算求文"之前）。这里照样先
        # 把社交那条线摆成"正等答复"的样子，再说一句危机话，看它归谁。
        probe.reset()
        service._awaiting = True
        stub.prompts.clear()
        crisis_while_waiting = "我不想活了"
        app._on_chat_send(crisis_while_waiting)
        wait_for(lambda: any(CRISIS_REPLY[:12] in s for s in probe.spoken)
                 or any(CRISIS_REPLY[:12] in b for b in _bubble(app)))
        drain_threads()
        pump(0.1)
        check("危机话术照旧出", any(CRISIS_REPLY[:12] in s for s in probe.spoken)
              or any(CRISIS_REPLY[:12] in b for b in _bubble(app)),
              " | ".join(probe.spoken[-2:]))
        check("这一句没被当成处境去写草稿", not probe.candidates, f"{len(probe.candidates)} 组")
        check("那扇「等处境」的窗本来就不存在，没被谁意外装上",
              service._waiting_for_situation() is False, "")
        service._awaiting = False

        # ---------- S3 红线 ----------

        section("S3 红线：危机词最高优先——绝不把危机话劫成起草稿")
        crisis_line = "帮我回一下，可是我真的不想活了"
        check("这句本身确实是危机词", is_crisis(crisis_line) is True, "")
        check("危机词不算求草稿", service.is_request(crisis_line) is False, "")
        check("危机话里带求草稿的说法也不接管",
              service.handle_request(crisis_line) is False, "")
        stub.prompts.clear()
        probe.reset()
        app._on_chat_send(crisis_line)
        wait_for(lambda: any(CRISIS_REPLY[:12] in s for s in probe.spoken)
                 or any(CRISIS_REPLY[:12] in b for b in _bubble(app)))
        drain_threads()
        pump(0.1)
        check("危机话术照旧出（没被起草稿的活儿盖掉）",
              any(CRISIS_REPLY[:12] in s for s in probe.spoken)
              or any(CRISIS_REPLY[:12] in b for b in _bubble(app)),
              " | ".join(probe.spoken[-2:]) or " | ".join(_bubble(app)[-2:]))
        check("危机话一条候选都不起草", not probe.candidates, f"{len(probe.candidates)} 组")
        check("危机话也没拿出去问模型",
              not any("你自己的口吻" in p for p in stub.prompts), f"{len(stub.prompts)} 次")

        section("S3 红线：不模拟按键 / 服务不碰剪贴板；截屏只认一屏、读完即弃（v2-social-fewshot 新口径）")
        refs = _code_refs(smod)
        # 剪贴板这条口径 v2-social-copy-clean 改过：**服务自己仍然一个字都不碰**，
        # 那一次静默写入在装配层（`bootstrap._copy_social_draft`），而且是只写不读。
        banned = {"SendInput", "keybd_event", "uiautomation", "pyautogui", "pynput",
                  "win32clipboard", "read_browser_title", "foreground_title", "clipboard"}
        check("服务代码里没有模拟按键 / 碰剪贴板那几条腿",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("服务自己不 import 截屏 / OCR 的库（那两只手由装配层注入）",
              not ({"OcrReader", "grab_foreground_window", "shot", "save_temp"} & refs),
              "、".join(sorted({"OcrReader", "grab_foreground_window", "shot", "save_temp"} & refs))
              or "（一处都没有）")
        import inspect

        source = inspect.getsource(smod)
        check("只挂前台换人这一条信号（attach 没变）",
              "monitor.foreground_changed.connect" in source, "")
        check("自动监听是轻量轮询：不新开采样循环、不直接认屏（v2-social-auto）",
              "QTimer" in source and "_poll_listen" in source
              and "read_screen_array" not in refs and "grab_foreground_window" not in refs, "")
        check("前台只拿进程名（输入只有 proc 一个字符串）",
              "def on_foreground(self, proc: str)" in source, "")
        check("红线新口径写进注释了（看一眼、用完就丢 / 起草完立刻清空）",
              "看一眼、用完就丢" in source and "起草完立刻清空" in source, "")
        check("认到的字只存内存、生成时取走即清（读完即弃）",
              "_take_chat_text" in source and "self._chat_text, \"\"" in source, "")
        check("不自动发送这条没动（草稿只落只读框）",
              "不自动发送" in source, "")

        section("S3 红线：截屏 / OCR 那两只手只在装配层，且只在这一拍抓")
        import inspect as _inspect

        boot_source = _inspect.getsource(bootstrap_mod)
        # v2-social-ocr-isolate 起装配层走的是 `read_screen_lines`（要字**连框一起**，
        # 好按气泡色分谁说的）；`read_screen_array`（只要字）仍在 monitor 上，给别处用。
        check("装配层才 import 截屏 / OCR 的实现",
              "grab_foreground_window" in boot_source
              and "read_screen_lines" in boot_source, "")
        check("OCR 总闸关掉就一个像素都不采",
              "screen.ocr_enabled" in boot_source, "")

        section("S3 红线：全仓库里根本没有「发送/模拟按键」那只手")
        hits = _repo_input_apis(Path(__file__).resolve().parent.parent)
        check("仓库代码（不含 .venv 与测试）里没有输入模拟类 API",
              not hits, "、".join(hits) or "（一处都没有）")

        section("S5 候选终点：非模态气泡亮正文+解析，写剪贴板等「复制回复」那一下")
        # 这一段把"候选给出去"那一拍整条链重跑一遍：模型桩 → `_auto_generate` →
        # `social_candidates` 信号 → 装配层 `_on_social_candidates` → 气泡（+ 复制按钮）。
        # 判据全是**新口径**的：一个模态框都不许建、`exec()` 一次都不许跑；
        # 而且（v2-social-chat-ui-copy 批次11）**生成那一刻不碰剪贴板**——
        # 写不写由用户按下面那个「复制回复」决定。
        service._running = False
        stub.candidates_result = {"candidates": [
            {"draft": "这周末我可能得加个班，回头再约。", "why": "委婉、留余地"},
        ]}
        probe.reset()
        with _DialogWatch() as watch:
            app._on_chat_send("帮我回一下，同事问我周末加不加班")
            wait_for(lambda: bool(probe.candidates))
            drain_threads()
            pump(0.1)

            check("★ 全程**没有**建任何模态框（弹窗那个入口彻底没了）",
                  watch.built == [], "、".join(watch.built) or "（一个都没建）")
            check("★ 更没有人去 `exec()` 跑嵌套事件循环（真机卡死、弹窗摞弹窗的根）",
                  watch.executed == [], "、".join(watch.executed) or "（一次都没跑）")
            check("★ 生成那一刻**没有**碰剪贴板（批次11 起不偷写了）",
                  QApplication.clipboard().text() != "这周末我可能得加个班，回头再约。",
                  repr(QApplication.clipboard().text()))
            # 不能取最后一条：候选摆进气泡之后，装配层紧接着又排了"要不要建对象攻略卡"
            # 那一句（`offer_card`），末尾那条是它。整段合起来搜才对。
            shown = "\n".join(_bubble(app))
            check("★ 宠物那个非模态气泡里写着「候选原文」",
                  "这周末我可能得加个班，回头再约。" in shown, repr(shown))
            check("★ 同一个气泡里也写着「解析」",
                  "解析：委婉、留余地" in shown, repr(shown))
            check("★ 气泡里不再有那两句替剪贴板打包票的收尾（TAIL 整段删了）",
                  "已经放进你剪贴板了" not in shown and "没能放进剪贴板" not in shown,
                  repr(shown))
            check("★ 正文下面挂着「复制回复」按钮（写剪贴板就等这一下）",
                  app.chat_bubble._copy_row.isVisible()
                  and app.chat_bubble._copy_button.text() == "复制回复",
                  f"露着={app.chat_bubble._copy_row.isVisible()}")
            # 真按一下那个按钮：这一下才轮到手去写，写的就是这一版正文
            app.chat_bubble._copy_button.click()
            pump(0.05)
            check("★ 按下「复制回复」→ 剪贴板里就是这一版正文（全仓唯一的写入时机）",
                  QApplication.clipboard().text() == "这周末我可能得加个班，回头再约。",
                  repr(QApplication.clipboard().text()))
            check("气泡没进入忙态（不阻塞，用户随时能接着说话）",
                  app.chat_bubble.busy is False, str(app.chat_bubble.busy))
            check("输入框与发送键都可用（UI 还能继续交互）",
                  app.chat_bubble._input.isEnabled() and app.chat_bubble._send_button.isEnabled()
                  and app.chat_bubble._send_button.text() == "发送",
                  f"输入框={app.chat_bubble._input.isEnabled()} / "
                  f"发送键='{app.chat_bubble._send_button.text()}'")
            # 真的再走一遍事件循环 + 敲一句话：卡死的流程在这一步会僵住
            app.chat_bubble._input.setText("那我先忙了")
            pump(0.05)
            check("★ 流程走完界面照常转：输入框收了字、事件循环还在跑",
                  app.chat_bubble._input.text() == "那我先忙了", app.chat_bubble._input.text())
            app.chat_bubble._input.clear()

        section("S5 那则文案只剩两段：正文 + 解析（批次11 删了开头交代与收尾打包票）")
        text = bootstrap_mod.social_draft_text("候选", "解析")
        check("`social_draft_text` 就是「正文」+ 空行 + `解析：…`（再无第三段）",
              text == "「候选」\n\n解析：解析", repr(text))
        check("开头那句交代（HEAD）整条没了",
              "你看看要不要改" not in text and not text.startswith("这条"), repr(text))
        check("收尾那两句「放进去了 / 没能放进去」也整条没了（TAIL 与 fail 分支都删了）",
              "已经放进你剪贴板了" not in text and "没能放进剪贴板" not in text, repr(text))
        check("空正文 → `_copy_social_draft` 回 False（没得写，不假报成功）",
              app._copy_social_draft("") is False, "")
        check("正常写 → `_copy_social_draft` 回 True（真写进去了）",
              app._copy_social_draft("候选正文") is True, "")
        # 走一遍真流程、但把写入那只手钉成失败：这会儿谁都不该四处嚷嚷"已经放进去了"
        # ——文案里已经根本没有那句话，所以判据是"哪儿都没说"+"剪贴板确实没变"。
        _orig_copy = app._copy_social_draft
        app._copy_social_draft = lambda draft: False
        try:
            QApplication.clipboard().setText("按下按钮之前剪贴板里本来放着的东西")
            stub.candidates_result = {"candidates": [{"draft": "这周加班去不了。", "why": "直接说"}]}
            probe.reset()
            app._on_chat_send("帮我回一下，同事问我周末加不加班")
            wait_for(lambda: bool(probe.candidates))
            drain_threads()
            pump(0.1)
            check("★ 写失败也不假报成功：气泡里哪一条都没说已经放进去了",
                  "已经放进你剪贴板了" not in "\n".join(_bubble(app)), "")
            app.chat_bubble._copy_button.click()
            pump(0.05)
            check("★ 按钮按了、手却失败了 → 剪贴板原样没动（写没写成两边对齐）",
                  QApplication.clipboard().text() == "按下按钮之前剪贴板里本来放着的东西",
                  repr(QApplication.clipboard().text()))
        finally:
            app._copy_social_draft = _orig_copy

        section("S5 多轮社交：换掉气泡里那一则 + 不摞第二个窗（批次11：只留最新一则）")
        # 气泡最多留 40 条（`ui.chat_bubble.MAX_ITEMS`），所以"多轮不摞窗"不能靠条数涨——
        # 靠两件事：这一轮新内容确实进了**那个**气泡，而且气泡窗口的个数没多出来。
        # 批次11 再加一条：上一轮那一则**被摘掉**，记录区里只留最新一则（需求5）。
        def _bubble_windows() -> int:
            return sum(1 for w in QApplication.allWidgets() if type(w) is type(app.chat_bubble))

        windows_before = _bubble_windows()

        def _plain_count() -> int:
            """没打帮聊标记的条目数——就是用户与宠物的普通聊天记录。"""
            h = app.chat_bubble._history
            return sum(1 for i in range(h.count()) if not h.item(i).data(ROLE_SOCIAL_DRAFT))

        plain_before = _plain_count()
        QApplication.clipboard().setText("上一轮按下复制之后留在剪贴板里的东西")
        stub.candidates_result = {"candidates": [
            {"draft": "那改天吧。", "why": "不硬约"},
        ]}
        probe.reset()
        with _DialogWatch() as watch2:
            app._on_chat_send("帮我回一下，同事问我周末加不加班")
            wait_for(lambda: bool(probe.candidates))
            drain_threads()
            pump(0.1)
            check("★ 第二轮：照样没有模态框、没有 exec()",
                  watch2.built == [] and watch2.executed == [],
                  f"{watch2.built} / {watch2.executed}")
            shown2 = "\n".join(_bubble(app))
            check("★ 第二轮：新内容进的是同一个气泡（不是又开一个窗）",
                  "那改天吧。" in shown2 and "不硬约" in shown2
                  and _bubble_windows() == windows_before,
                  f"气泡窗 {windows_before} → {_bubble_windows()}，"
                  f"条数上限 40（现 {len(_bubble(app))}）")
            check("★ 上一轮那一则被摘掉了（批次11 需求5：只留最新一则）",
                  "这周末我可能得加个班，回头再约。" not in shown2
                  and "解析：委婉、留余地" not in shown2, repr(shown2))
            check("★ 摘的是打了标记的那几条，用户与宠物的普通聊天一条没动",
                  _plain_count() == plain_before,
                  f"普通条目 {plain_before} → {_plain_count()}")
            check("★ 第二轮照样不偷写剪贴板（上一轮留下的东西原样在位）",
                  QApplication.clipboard().text() == "上一轮按下复制之后留在剪贴板里的东西",
                  repr(QApplication.clipboard().text()))
            # 但按钮手上那版已经换成第二轮的了：再按一下拿到的是最新的，不是上一轮那版。
            app.chat_bubble._copy_button.click()
            pump(0.05)
            check("第二轮：按钮认的是最新那一版（不是上一轮的旧正文）",
                  QApplication.clipboard().text() == "那改天吧。",
                  repr(QApplication.clipboard().text()))

        section("S5 套框：正文 + 解析 + 复制按钮同属一则（v2-social-chat-ui-fix2 提交F）")
        # 真机诉求：一眼分清"哪句能直接粘走、哪句是给我看的门道"，而且按钮不能跟那一则
        # 两样命。所以验的是**控件树**（外粗框 / 内细框 / 解析 / 右下角按钮各在哪一层），
        # 不是截图——离屏截出来的观感不作数，那一条得真机上看。
        import ui.chat_bubble as bubble_mod

        history = app.chat_bubble._history
        marked = [i for i in range(history.count()) if history.item(i).data(ROLE_SOCIAL_DRAFT)]
        check("★ 那一则是**一条**（正文与解析不再各占一条）",
              len(marked) == 1, f"{len(marked)} 条打了标记")
        card = history.itemWidget(history.item(marked[0])) if marked else None
        check("★ 那一则挂着一个真控件（不是 delegate 画的纯文本）",
              isinstance(card, QFrame), type(card).__name__)
        inner = card.findChild(QFrame, "ChatDraftBody") if card is not None else None
        check("外层是粗边框（3px）——这一整块就是「一则」",
              card is not None and "border: 3px solid" in card.styleSheet(),
              card.styleSheet() if card is not None else "（没控件）")
        check("内层是细边框（1px）、而且只装正文那一句",
              inner is not None and "border: 1px solid" in inner.styleSheet()
              and inner.findChild(QLabel, "ChatDraftText") is not None
              and inner.findChild(QLabel, "ChatDraftNote") is None,
              inner.styleSheet() if inner is not None else "（没内层框）")
        note_label = card.findChild(QLabel, "ChatDraftNote") if card is not None else None
        check("解析在外层框里、正文框**外**，而且是灰字（跟系统提示同一档色）",
              note_label is not None and note_label.parent() is card
              and f"color: {bubble_mod.TEXT_MUTED}" in note_label.styleSheet(),
              note_label.styleSheet() if note_label is not None else "（没解析）")
        button = card.findChild(QPushButton, "ChatOffer") if card is not None else None
        check("★ 「复制回复」长在这一则**里面**（不再是气泡底部的固定控件）",
              button is not None and card.isAncestorOf(button)
              and button.text() == app.chat_bubble.COPY_LABEL,
              f"{button.text() if button is not None else '（没按钮）'}")
        row = app.chat_bubble._copy_row
        cells = ([row.layout().itemAt(i) for i in range(row.layout().count())]
                 if row is not None else [])
        check("按钮压在外层框的**右下角**（左边先撑一个弹簧、按钮在它后面）",
              row is not None and row.parent() is card
              and len(cells) == 2 and cells[0].spacerItem() is not None
              and cells[1].widget() is button,
              f"按钮那一行 {len(cells)} 格："
              f"{['弹簧' if c.spacerItem() is not None else type(c.widget()).__name__ for c in cells]}")
        # 整框找一遍"复制回复"：就长在那一则里的这一只。别的 `ChatOffer` 是 [好呀] [不用了]
        # 那排轻按钮（同一款样式，别把它们算进来）。
        copies = [b for b in app.chat_bubble.findChildren(QPushButton, "ChatOffer")
                  if b.text() == app.chat_bubble.COPY_LABEL]
        check("★ 对照：整框里「复制回复」就这么一只（不另有一个杵在气泡底部）",
              copies == [button], f"{[b.text() for b in copies]}")

        section("S6 卡片高度：按这个宽度真实折行来量，长解析不被裁（v2-social-card-size 批次11b-补G）")
        # 现象（真机图）：解析恢复长版（批次11b 提交E）之后，那一则的高度还是按**短桩文本**
        # 量的，多行解析被裁出框外（"比较……"之后截断）。根因：老口径 `card.adjustSize()`
        # 用的是 `sizeHint()`，而带 `setWordWrap(True)` 的 `QLabel` 的 sizeHint 是按
        # **不折行**给的自然宽高（离屏量过：卡片宽 262，sizeHint 却是 582×133）→ 卡片只按
        # 一行长高。这里另起一个气泡、拿真·长解析建一张卡，把"折行测量"这条腿钉住——
        # 只在 app 那一个气泡上验会把后面的断言搅乱（那一则本就挂在里面）。
        long_note = (
            "对方把选择权直接甩回来，表面上是「你定」，其实是让你替两个人做决定——"
            "他不用承担被拒的尴尬。回这句的用意是把球轻轻推回去：框架上不被他牵着走，"
            "同时释放可得性，让他知道你乐意，但时间得由他先落一个具体的准话。")
        long_body = (
            "行，那我先按你说的排一排。周末我这边不一定腾得开，你要是哪天方便就提前说一声，"
            "我把别的事往两边挪挪，尽量把整块时间留出来给你。")
        probe_bubble = bubble_mod.ChatBubble("天渌")
        try:
            probe_bubble.resize(300, 400)
            probe_bubble.show()
            pump(0.05)
            probe_bubble.append_social_card(long_body, long_note, long_body)
            pump(0.05)
            ph = probe_bubble._history
            pitem = ph.item(ph.count() - 1)
            pcard = ph.itemWidget(pitem) if pitem is not None else None
            pnote = pcard.findChild(QLabel, "ChatDraftNote") if pcard is not None else None
            pbody = pcard.findChild(QFrame, "ChatDraftBody") if pcard is not None else None
            prow = probe_bubble._copy_row
            pbutton = pcard.findChild(QPushButton, "ChatOffer") if pcard is not None else None
            ready = None not in (pcard, pnote, pbody, prow, pbutton)

            check("★ 卡片高**不是**按不折行量出来那个值（老口径会矮一截，解析末行被裁）",
                  ready and pcard.height() > pcard.sizeHint().height(),
                  f"卡片 {pcard.height() if pcard is not None else '（没卡）'} / "
                  f"不折行的 sizeHint {pcard.sizeHint().height() if pcard is not None else '—'}")
            check("★ 卡片高 = 这个宽度下折完行真实要占的高",
                  ready and pcard.height() == pcard.heightForWidth(pcard.width()),
                  f"卡片 {pcard.height() if pcard is not None else '—'} / "
                  f"heightForWidth {pcard.heightForWidth(pcard.width()) if pcard is not None else '—'}")
            check("★ 卡片高 = 正文框 + 解析(全行) + 按钮行 + 间距 + 两层留白 + 两层边框 之和",
                  ready and pcard.height() == (
                      pbody.height() + pnote.height() + prow.height()
                      + 2 * bubble_mod.CARD_GAP + 2 * bubble_mod.CARD_PAD
                      + 2 * bubble_mod.CARD_BORDER_W),
                  f"卡片 {pcard.height() if pcard is not None else '—'} / 各段之和 "
                  f"{pbody.height() + pnote.height() + prow.height() + 2 * bubble_mod.CARD_GAP + 2 * bubble_mod.CARD_PAD + 2 * bubble_mod.CARD_BORDER_W if ready else '—'}")
            check("★ 解析那一格拿到的高正好是它折行需要的高（末行完整、不截断）",
                  ready and pnote.height() == pnote.heightForWidth(pnote.width())
                  and pnote.heightForWidth(pnote.width())
                  > 2 * pnote.fontMetrics().lineSpacing(),
                  f"拿到 {pnote.height() if pnote is not None else '—'} / "
                  f"需要 {pnote.heightForWidth(pnote.width()) if pnote is not None else '—'}"
                  f"（一行 {pnote.fontMetrics().lineSpacing() if pnote is not None else '—'}）")
            check("★ 条目高度 = 控件实际高度（delegate 与 `_content_height` 读的就是这一格）",
                  ready and pitem.sizeHint().height() == pcard.height(),
                  f"条目 {pitem.sizeHint().height() if pitem is not None else '—'} / "
                  f"控件 {pcard.height() if pcard is not None else '—'}")
            check("★ 列表给这一条的高度也是它（滚到底时整条都看得见）",
                  ready and ph.visualItemRect(pitem).height() == pcard.height(),
                  f"列表 {ph.visualItemRect(pitem).height() if ready else '—'}")
            check("★ 撑高之后按钮仍压在卡片右下角（没被挤出框外）",
                  ready and pbutton.pos().y() + pbutton.height() == prow.height()
                  and pbutton.pos().x() + pbutton.width() == prow.width()
                  and pbutton.pos().y() + pbutton.height() <= pcard.height(),
                  f"按钮底 {pbutton.pos().y() + pbutton.height() if ready else '—'} / "
                  f"按钮行高 {prow.height() if ready else '—'} / "
                  f"卡片高 {pcard.height() if pcard is not None else '—'}")
            # 对照：短内容上两种量法给的数**相同**——这一版没把简单情况一起撑高
            # （老口径就是 `sizeHint().height()`，所以"相等"就是"跟修前一样"）。
            probe_bubble.append_social_card("那改天吧。", "留余地", "那改天吧。")
            pump(0.05)
            sitem = ph.item(ph.count() - 1)
            scard = ph.itemWidget(sitem) if sitem is not None else None
            check("★ 对照：短正文 + 短解析时，卡片高跟老的 sizeHint 量法**一模一样**"
                  "（简单情况没被撑高）",
                  scard is not None and scard.height() == scard.sizeHint().height()
                  and scard.height() == scard.heightForWidth(scard.width()),
                  f"卡片 {scard.height() if scard is not None else '—'} / "
                  f"sizeHint {scard.sizeHint().height() if scard is not None else '—'} / "
                  f"heightForWidth {scard.heightForWidth(scard.width()) if scard is not None else '—'}")
        finally:
            probe_bubble.hide()
            probe_bubble.deleteLater()
            pump(0.02)

        section("S5 红线：候选终点只**写**剪贴板，从不读")
        # 按 AST 数"伸了几只手"，不按源码文本数：`_copy_social_draft` 的 docstring 里
        # 就写着"不碰原生剪贴板 API"，一搜文本会把自己判违规（同 `_code_refs` 的坑）。
        boot_ops = [
            node.attr
            for node in ast.walk(ast.parse(_inspect.getsource(bootstrap_mod)))
            if isinstance(node, ast.Attribute)
        ]
        check("★ 装配层对剪贴板**只有一处引用**（那个引用就是那次 setText）",
              boot_ops.count("clipboard") == 1 and boot_ops.count("setText") == 1,
              f"clipboard × {boot_ops.count('clipboard')}、setText × {boot_ops.count('setText')}")
        check("★ 没有任何读剪贴板 / 剪贴板监听的腿",
              "setMimeData" not in boot_ops and "textChanged" not in boot_ops
              and "dataChanged" not in boot_ops, "")
        others = [
            p.name
            for p in Path(__file__).resolve().parent.parent.rglob("*.py")
            if ".venv" not in p.parts and "tests" not in p.parts and "__pycache__" not in p.parts
            and "clipboard" in p.read_text(encoding="utf-8", errors="replace")
            and p.name != "bootstrap.py"
        ]
        check("★ 全仓（不含测试）只有装配层这一处碰剪贴板，服务那条线一个字没碰",
              not others, "、".join(others) or "（就 bootstrap.py 一处）")

        section("S3 红线：建卡框**只有保存和取消**，正文可改")
        from ui.social_draft import GuideCardDialog

        gbox = GuideCardDialog("对方是谁：同事", None)
        try:
            from PySide6.QtWidgets import QPushButton

            labels = [b.text() for b in gbox.findChildren(QPushButton)]
            check("建卡框有「保存并建卡」和「取消」",
                  sorted(labels) == sorted(["保存并建卡", "取消"]), "、".join(labels))
            check("对方信息正文**可改**（不是只读）", gbox._editor.isReadOnly() is False, "")
            check("正文预填了抽出来的那份", gbox.profile() == "对方是谁：同事", gbox.profile())
            gbox._editor.setPlainText("对方是谁：同事\n兴趣：跑步")
            check("改过之后 values() 返回改过的正文",
                  gbox.values() == "对方是谁：同事\n兴趣：跑步", gbox.values())
        finally:
            gbox.deleteLater()
            pump(0.02)

        section("S3 红线：起草路径两库零写入、不落盘")
        service._running = False
        stub.candidates_result = {"candidates": [{"draft": "好，回头说。", "why": "留余地"}]}
        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        for line in ("帮我回一下，同事问我周末加不加班", "帮我回复一下领导"):
            service.handle_request(line)
            drain_threads()
            pump(0.05)
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")

        section("S3 红线：对方原话不落盘（沙箱里搜不到那句话）")
        marker = "汤圆爱吃榴莲千层"
        service._grab_chat = lambda: "FAKE_FRAME"
        service._read_chat = lambda array: f"茗风：{marker}"
        service._examples_provider = lambda query: ""
        service._chat_text = ""
        service._chat_reading = False
        stub.candidates_result = {"candidates": [{"draft": "好呀，回头聊。", "why": "寒暄"}]}
        pet_before2 = _rows(app.config.db_path, "utterance")
        mind_before2 = app.mind_store.fragment_count()
        _social_offer()
        wait_for(lambda: bool(service._chat_text))
        drain_threads()
        check("认出来的字只在内存里", marker in service._chat_text, service._chat_text or "（空）")
        app._on_chat_send("帮我回一下")
        wait_for(lambda: bool(probe.candidates))
        drain_threads()
        pump(0.1)
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before2,
              f"{pet_before2} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before2,
              f"{mind_before2} → {app.mind_store.fragment_count()}")
        hits = _file_has(box.dir, marker)
        check("沙箱里没有任何文件写着那句原话", not hits, "、".join(hits) or "（一处都没有）")
        check("起草完内存里也清干净了（读完即弃）", service._chat_text == "",
              service._chat_text or "（已清空）")
        service._grab_chat = None
        service._read_chat = None

        section("S3 红线：不发新的网络请求、没有新依赖")
        check("social.py 不直接 import 联网库",
              not any(w in source for w in ("requests", "httpx", "urllib", "aiohttp")), "")
        check("起草与抽信息走的是既有的普通对话通道（LLMFactory.create 只有一个引用点）",
              "LLMFactory.create(" in source, "")
        check("除了 create，没有别的模型调用口子",
              _factory_calls(smod) == 1, f"{_factory_calls(smod)} 处")
        check("没动联网检索那条通道（起草不需要联网）",
              "create_search" not in source, "")
    finally:
        # 把模态换成记录员是这一轮测试动过的东西，收工前还原（别影响别的模块/后续用例）。
        # 候选那个框已经不存在了（v2-social-copy-clean），要还原的只剩建卡框。
        bootstrap_mod.GuideCardDialog = RealGuideDialog
        drain_threads()
        box.cleanup()
    return summary("社交帮忙回复扩展")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
