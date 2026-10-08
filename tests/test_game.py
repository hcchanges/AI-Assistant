"""
游戏攻略（v2-game-guide）的自动测试。

覆盖：
- S1 识别层：认得出游戏（大小写不敏感）、**启动器不算游戏**、认不出的一律不认；
- S1 前台触发：前台切到游戏才冒话、切到非游戏/启动器不冒话、同游戏冷却、等答复时不重复问；
- S2 先问再给：不点头一个字都不外发；点头才去找；本地起手优先、没有才联网；
  两条都不行时**如实说没找着**（不编、也不回空串）；
- S3 红线：只读进程名（不读标题、不截屏、不 OCR）、危机词不跳步、两库零写入、无新依赖。

前台进程用假的（离屏跑不动真 psutil），但**信号接线走真实链路**：
monitor.sample() → foreground_changed → GameGuideService → bus.game_offer → bootstrap
→ 气泡轻按钮 → offer_reply → 服务 → bus.game_guide，端到端验到。
联网检索那条腿由 harness 的桩顶上（StubSearch），所以整轮不联网、不花钱。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen import game as gmod
from life.screen.game import (
    GAME_PROCS,
    GENERIC_HOST_PROCS,
    LOCAL_TIPS,
    UNSURE_LINE,
    GameGuideService,
    game_of,
    is_launcher,
    recognize,
)

# 认得出、且**本地有起手话**的游戏（走本地那条腿）
KNOWN_HIT = "League of Legends.exe"
KNOWN_HIT_NAME = "英雄联盟"
# 认得出、但**本地没写起手话**的游戏（只能走联网那条腿）
KNOWN_MISS = "tslgame.exe"
KNOWN_MISS_NAME = "绝地求生"
# 认得出但不是游戏的东西
NOT_A_GAME = ("chrome.exe", "Code.exe", "notepad.exe", "explorer.exe", "", "   ")
# 平台/启动器：前台是它们**不代表在玩**
LAUNCHERS = ("steam.exe", "steamwebhelper.exe", "wegame.exe",
             "epicgameslauncher.exe", "battle.net.exe", "ubisoftconnect.exe")


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
    """接住"宠物说了什么""攻略给了什么"，好断言。"""

    def __init__(self, app) -> None:
        self.spoken: list[str] = []
        self.offers: list[str] = []
        self.guides: list[str] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.game_offer.connect(self.offers.append)
        app.bus.game_guide.connect(self.guides.append)

    def reset(self) -> None:
        self.spoken.clear()
        self.offers.clear()
        self.guides.clear()


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _bubble(app) -> list[str]:
    """气泡里现在显示着哪几句（按顺序）。"""
    history = app.chat_bubble._history
    return [history.item(i).text() for i in range(history.count())]


def _code_refs(module) -> set[str]:
    """模块里**代码**引用到的名字（含 import 进来的模块/名字），不含注释与文档字符串。

    红线要查的是"代码里有没有伸这只手"，不是"注释里提没提"——所以走 AST，
    不能用字符串搜索：模块的开头正大光明地写着"不借 monitor.py 的 read_browser_title"，
    一搜就自己把自己判违规了。
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


def main() -> int:
    box, stub, app = make_env()
    try:
        monitor = app.screen_monitor
        guide = app.game_guide
        probe = Probe(app)
        app.config.screen.guide_enabled = True
        app.config.screen.guide_cooldown_min = 30
        # v2-scene-state 之后"问攻略"默认要等开场白说完（`guide_ask_delay_sec`，30 秒）。
        # 这一套测的是**问句本身**（认得出就问、冷却、点头才给），不是那个等待，
        # 所以把等待归零，回到"一切到游戏就问"——等待另有一条专门的测试。
        app.config.screen.guide_ask_delay_sec = 0

        # ---------- S1 识别层（纯函数） ----------

        section("S1 识别层：认得出游戏，大小写不敏感")
        check("英雄联盟认得出", game_of(KNOWN_HIT) == KNOWN_HIT_NAME, str(game_of(KNOWN_HIT)))
        check("全小写也认得出", game_of("league of legends.exe") == KNOWN_HIT_NAME, "")
        check("全大写也认得出", game_of("LEAGUE OF LEGENDS.EXE") == KNOWN_HIT_NAME, "")
        check("前后带空格也认得出", game_of("  dota2.exe  ") == "DOTA2", "")
        check("另一个游戏也认得出", game_of(KNOWN_MISS) == KNOWN_MISS_NAME, "")
        check("同一个游戏的多个 exe 都认得出",
              game_of("cs2.exe") == game_of("CS2.EXE") == "反恐精英 2", "")
        check("进程名表非空", len(GAME_PROCS) > 0, f"{len(GAME_PROCS)} 条")

        section("S1 识别层：启动器/平台不算在玩（这条前提任务书里是错的）")
        for proc in LAUNCHERS:
            check(f"「{proc}」不算游戏", game_of(proc) is None, str(game_of(proc)))
        check("steam 被认成启动器", is_launcher("steam.exe") is True, "")
        check("英雄联盟不是启动器", is_launcher(KNOWN_HIT) is False, "")

        section("S1 识别层：认不出来的一律不认（宁可漏判）")
        for proc in NOT_A_GAME:
            check(f"「{proc or '（空串）'}」不算游戏", game_of(proc) is None, str(game_of(proc)))
        check("通用宿主 javaw.exe 不收（任何 Java 程序都叫这个名）",
              game_of("javaw.exe") is None, "")
        check("通用宿主 UnityPlayer.exe 不收", game_of("UnityPlayer.exe") is None, "")

        section("S1 识别层：通用运行时宿主不算游戏（pythonw 等，连最弱的线索也堵）")
        # v2-social-reply-fix 真机回归：前台 pythonw.exe（宠物自己就是它跑起来的）被
        # 「无边框全屏」那条最弱的线索认成《pythonw》，游戏态抢了前台，把社交候选后的
        # 建卡询问盖没了。修法是把 Python / Java / Node 这类**通用宿主**从所有线索里
        # 一起摘掉——进程名认不出只是第一步，这里补上后四条线索（窗口类名 / 安装路径 /
        # 画面运动 / 无边框全屏）也不能把它们认成游戏。
        for proc in GENERIC_HOST_PROCS:
            check(f"「{proc}」进程名不算游戏", game_of(proc) is None, str(game_of(proc)))
            check(f"「{proc}」光认进程名也不算游戏", recognize(proc) is None, proc)
            # 关键：就算窗口是无边框全屏、画面在动、路径躺进 Steam 目录，也**不算游戏**
            check(f"「{proc}」无边框全屏也不当游戏",
                  recognize(proc, borderless_fullscreen=True) is None,
                  str(recognize(proc, borderless_fullscreen=True)))
            check(f"「{proc}」画面在动也不当游戏",
                  recognize(proc, motion_game=True) is None,
                  str(recognize(proc, motion_game=True)))
        # 正向对照：别的进程照样能被最弱的线索认出来（没把后四条线索整个关掉）
        check("非通用宿主无边框全屏仍是游戏",
              recognize("weirdgame.exe", borderless_fullscreen=True) is not None, "")
        check("非通用宿主画面在动仍是游戏",
              recognize("weirdgame.exe", motion_game=True) is not None, "")
        check("真游戏照旧认得出",
              recognize("League of Legends.exe") is not None, "")

        # ---------- S1 前台触发 ----------

        section("S1 前台触发：切到游戏才冒话（走真实的 sample → 信号 → 服务）")
        holder = {"proc": "chrome.exe"}
        _fake_foreground(monitor, holder)
        _sample(monitor)
        probe.reset()
        holder["proc"] = KNOWN_HIT
        _sample(monitor)
        check("前台换成游戏 → 冒出问句", len(probe.offers) == 1, f"{len(probe.offers)} 次")
        check("问句带上了游戏名", bool(probe.offers) and probe.offers[0] == KNOWN_HIT_NAME,
              probe.offers[0] if probe.offers else "（空）")
        check("宠物嘴里说了那句问话（带书名号）",
              any(f"《{KNOWN_HIT_NAME}》" in line for line in probe.spoken),
              " | ".join(probe.spoken))
        check("服务记着自己正等着答复", guide.awaiting_confirm is True, "")
        check("服务记着正问的是哪个游戏", guide.game == KNOWN_HIT_NAME, guide.game)

        section("S1 前台触发：非游戏 / 启动器一个字都不冒")
        # 注意：只断言"没出攻略问句"。别的服务（自动番茄钟）对 notepad 这类工作软件
        # 本来就该冒话，那不是这条线的账，别把别人的话算到游戏攻略头上
        for proc in ("chrome.exe", "steam.exe", "notepad.exe"):
            guide._awaiting = False          # 把上一问的等待态清掉，单看"认不认得出"
            probe.reset()
            holder["proc"] = proc
            _sample(monitor)
            check(f"「{proc}」不冒攻略问句", not probe.offers,
                  f"offers={probe.offers}")
            check(f"「{proc}」没留下等待态", guide.awaiting_confirm is False, "")

        section("S1 前台触发：通用宿主 pythonw 在前台也不冒（v2-social-reply-fix）")
        # 真机回归：宠物自己就挂在 pythonw.exe 上跑，用户点一下宠物、前台就是 pythonw，
        # 旧版把它认成《pythonw》冒攻略问句。这里走真实 sample → 信号 → 服务链路，
        # 断言前台是 pythonw 时一句攻略都不冒、也不留等待态。
        for proc in ("pythonw.exe", "python.exe", "javaw.exe"):
            guide._awaiting = False
            guide._last_asked.clear()
            probe.reset()
            holder["proc"] = proc
            _sample(monitor)
            check(f"「{proc}」不冒攻略问句（走真实链路）", not probe.offers,
                  f"offers={probe.offers}")
            check(f"「{proc}」没留下等待态", guide.awaiting_confirm is False, "")

        section("S1 冷却是**按游戏各自算**的，同一个游戏冷却内不重复问")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        # 得**来回切**才能再触发一次：前台没换人时 monitor 不发 foreground_changed
        holder["proc"] = KNOWN_HIT
        _sample(monitor)
        check("第一次问出口", len(probe.offers) == 1, f"{len(probe.offers)} 次")
        guide._awaiting = False              # 用户没答就切走了
        holder["proc"] = "chrome.exe"
        _sample(monitor)
        holder["proc"] = KNOWN_HIT           # 冷却没过又切回来
        _sample(monitor)
        check("同一个游戏冷却内不再问（切走再切回来也不问）",
              len(probe.offers) == 1, f"{len(probe.offers)} 次")
        # 换一个游戏：各自算冷却，所以照样该问
        guide._awaiting = False
        holder["proc"] = "dota2.exe"
        probe.reset()
        _sample(monitor)
        check("换了个游戏照样问（冷却按游戏分）", len(probe.offers) == 1,
              f"{len(probe.offers)} 次")

        section("S2 冷却过期后可以再问（注入时钟，不改真实时间）")
        fake_now = {"t": 1000.0}
        clocked = GameGuideService(
            config=app.config, bus=app.bus, clock=lambda: fake_now["t"]
        )
        seen: list[str] = []
        app.bus.game_offer.connect(seen.append)
        clocked.on_foreground("dota2.exe")
        check("冷却过了（从没问过）就问", len(seen) == 1, f"{len(seen)} 次")
        clocked._awaiting = False
        fake_now["t"] += 60                  # 才过 1 分钟
        clocked.on_foreground("dota2.exe")
        check("冷却 30 分钟没到，不再问", len(seen) == 1, f"{len(seen)} 次")
        clocked._awaiting = False
        fake_now["t"] += 30 * 60             # 过了 30 分钟
        clocked.on_foreground("dota2.exe")
        check("冷却到了可以再问", len(seen) == 2, f"{len(seen)} 次")
        app.bus.game_offer.disconnect(seen.append)

        section("S1 已经问出口、等答复时不再问第二遍")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        guide.on_foreground(KNOWN_HIT)
        check("第一遍问了", len(probe.offers) == 1, f"{len(probe.offers)} 次")
        guide.on_foreground(KNOWN_HIT)
        check("等答复期间再切回来也不重复问", len(probe.offers) == 1, f"{len(probe.offers)} 次")

        section("S1 等答复期间切到另一个游戏：上一问作废，重新识别这个（v2-game-react）")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        guide.on_foreground(KNOWN_HIT)          # 玩 A → 问 A，等答复
        check("A 问了", len(probe.offers) == 1 and guide.awaiting_confirm, f"{len(probe.offers)} 次")
        guide.on_foreground(KNOWN_MISS)         # 没点按钮，直接切到 B（绝地求生）
        check("切到 B → 上一问作废，重新识别问 B",
              len(probe.offers) == 2 and probe.offers[-1] == KNOWN_MISS_NAME,
              str(probe.offers))
        check("现在等的是 B 的答复", guide.awaiting_confirm and guide.game == KNOWN_MISS_NAME,
              f"game={guide.game}")
        guide.on_foreground(KNOWN_MISS)         # 还在等 B，再切回 B：不重复问
        check("等 B 的答复时再切回 B 也不重复问", len(probe.offers) == 2, f"{len(probe.offers)} 次")

        section("S3 别在别人等答复时插嘴（气泡上那排按钮只有一份）")
        app.config.screen.guide_enabled = True
        blocked = GameGuideService(
            config=app.config, bus=app.bus, can_offer=lambda: False
        )
        seen2: list[str] = []
        app.bus.game_offer.connect(seen2.append)
        blocked.on_foreground(KNOWN_HIT)
        check("别人正等答复时不插嘴", not seen2, f"{len(seen2)} 次")
        app.bus.game_offer.disconnect(seen2.append)

        section("S3 总闸：guide_enabled 关掉之后一句都不问")
        app.config.screen.guide_enabled = False
        probe.reset()
        guide._awaiting = False
        guide._last_asked.clear()
        guide.on_foreground(KNOWN_HIT)
        check("关掉后不问", not probe.offers and not probe.spoken, "")
        app.config.screen.guide_enabled = True

        # ---------- S2 先问再给 ----------

        section("S2 不点头：一个字都不外发，也不去联网")
        guide._awaiting = False
        guide._game = ""
        probe.reset()
        stub.prompts.clear()
        guide.on_foreground(KNOWN_MISS)
        check("问了（绝地求生本地没起手话，本该走联网）",
              len(probe.offers) == 1, f"{len(probe.offers)} 次")
        # 走真实的轻按钮出口：点「不用了」
        app._on_offer_reply("不用了")
        drain_threads()
        pump(0.1)
        check("不点头 → 没给攻略", not probe.guides, f"{len(probe.guides)} 条")
        check("不点头 → 一次都没联网检索", not stub.prompts, f"{len(stub.prompts)} 次")
        # 「好，那先不找了」走的是气泡 append_pet（不念出来），所以查气泡不查 pet_speak
        check("不点头 → 气泡里应了一声",
              any("那先不找" in line for line in _bubble(app)),
              " | ".join(_bubble(app)[-2:]))
        check("不点头 → 等待态清掉了", guide.awaiting_confirm is False, "")

        section("S2 点头 + 本地有起手话：秒回本地那条，**不联网**")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        stub.prompts.clear()
        guide.on_foreground(KNOWN_HIT)
        app._on_offer_reply("好呀")
        wait_for(lambda: bool(probe.guides))
        drain_threads()
        pump(0.1)
        check("给了攻略", len(probe.guides) == 1, f"{len(probe.guides)} 条")
        check("给的就是本地那条起手话",
              bool(probe.guides) and probe.guides[0] == LOCAL_TIPS[KNOWN_HIT_NAME],
              (probe.guides[0][:30] + "…") if probe.guides else "（空）")
        check("本地命中时一次都没联网（用户数据不外发那条口径）",
              not stub.prompts, f"{len(stub.prompts)} 次")
        check("点头时先说了一句去翻", any("翻翻" in s for s in probe.spoken),
              " | ".join(probe.spoken))
        check("本地起手表非空", len(LOCAL_TIPS) > 0, f"{len(LOCAL_TIPS)} 条")

        section("S2 点头 + 本地没有：走联网检索那条腿")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        stub.prompts.clear()
        stub.online_text = "先跳伞去人少的野区，别一落地就冲房区。\n毒圈刷新前先看车，跑毒比打架要紧。"
        guide.on_foreground(KNOWN_MISS)
        app._on_offer_reply("好呀")
        wait_for(lambda: bool(probe.guides))
        drain_threads()
        pump(0.1)
        check("把游戏名交给了检索", any(KNOWN_MISS_NAME in p for p in stub.prompts),
              f"{len(stub.prompts)} 次")
        check("检索提示词要求「真实信息、别编」",
              any("不要凭印象编" in p for p in stub.prompts), "")
        check("检索提示词带「无来源兜底声明」原话（拿不准就直说，不编）",
              any(UNSURE_LINE in p for p in stub.prompts),
              f"兜底句={UNSURE_LINE!r}")
        check("把检索回来的正文给了气泡",
              bool(probe.guides) and "跳伞" in probe.guides[0],
              probe.guides[0][:30] if probe.guides else "（空）")

        section("S2 两条都不行：如实说没找着（不编、也不回空串）")
        from brain.llm.base import LLMError as _LLMError
        from brain.llm.factory import LLMFactory as _Factory

        original = _Factory.create_search
        _Factory.create_search = staticmethod(
            lambda config: (_ for _ in ()).throw(_LLMError("还没填 API Key"))
        )
        try:
            guide._awaiting = False
            guide._last_asked.clear()
            probe.reset()
            guide.on_foreground(KNOWN_MISS)
            app._on_offer_reply("好呀")
            wait_for(lambda: bool(probe.guides))
            drain_threads()
            pump(0.1)
            check("没查着也给了话", len(probe.guides) == 1, f"{len(probe.guides)} 条")
            check("没查着的话里带游戏名",
                  bool(probe.guides) and KNOWN_MISS_NAME in probe.guides[0],
                  probe.guides[0][:40] if probe.guides else "（空）")
            check("没查着说的是「没翻到」，不是编一段",
                  bool(probe.guides) and "没翻到" in probe.guides[0],
                  probe.guides[0][:40] if probe.guides else "（空）")
            check("没查着也不是空串", bool(probe.guides) and bool(probe.guides[0].strip()), "")
        finally:
            _Factory.create_search = original

        section("S2 联网回来是空串：同样落到「没找着」")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        stub.online_text = "   "
        guide.on_foreground(KNOWN_MISS)
        app._on_offer_reply("好")
        wait_for(lambda: bool(probe.guides))
        drain_threads()
        pump(0.1)
        check("空正文换成「没找着」的交代",
              bool(probe.guides) and "没翻到" in probe.guides[0],
              probe.guides[0][:40] if probe.guides else "（空）")

        section("S2 「不用了」以外的含糊答复也不给（只有明确的 yes 才给）")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        stub.prompts.clear()
        guide.on_foreground(KNOWN_MISS)
        app._on_offer_reply("算了吧")
        drain_threads()
        pump(0.1)
        check("含糊答复当成不要", not probe.guides, f"{len(probe.guides)} 条")
        check("也没联网", not stub.prompts, f"{len(stub.prompts)} 次")

        section("S0 边界：没人等答复时，轻按钮答复照样走聊天那条老路")
        app.game_guide._awaiting = False
        app.screen_notes._awaiting = False
        spoken_before = len(probe.spoken)
        # 对话链走的是 chat_messages，回话取自 stub.text（不是 parse_result）
        stub.text = "嗯，聊点别的"
        app._on_offer_reply("今天天气不错")
        wait_for(lambda: len(probe.spoken) > spoken_before)
        check("没人等答复时，答复走的是聊天那条线",
              any("聊点别的" in s for s in probe.spoken), " | ".join(probe.spoken[-2:]))

        # ---------- S3 红线 ----------

        section("S3 红线：危机词不跳步（连自己写死的模板也过一遍）")
        original_ask = gmod.ASK_LINE
        gmod.ASK_LINE = "我想死"
        try:
            guide._awaiting = False
            guide._last_asked.clear()
            probe.reset()
            guide.on_foreground(KNOWN_HIT)
            check("问句踩到危机词就不冒这句、也不露按钮",
                  not probe.offers and not probe.spoken,
                  f"offers={probe.offers} spoken={probe.spoken}")
        finally:
            gmod.ASK_LINE = original_ask

        section("S3 红线：只看进程名——不读标题、不截屏、不 OCR")
        import inspect

        source = inspect.getsource(gmod)
        refs = _code_refs(gmod)
        banned = {"OcrReader", "grab_foreground_window", "save_temp", "read_browser_title",
                  "title_changed", "foreground_title"}
        check("代码里不碰截屏 / OCR / 窗口标题那几条腿",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("只挂前台换人这一条信号",
              "monitor.foreground_changed.connect" in source, "")
        # v2-scene-state 之后模块里多了个 QTimer：那是"等开场白说完再问"的**一次性**延时
        # （`setSingleShot(True)`），由前台信号触发、自己不会转——没人切前台它永远不响。
        # 所以这条红线的断言从"一个字都不许提 QTimer"改成"不许有会自己转的计时器、不许
        # 自己开线程"，那才是"没新开采样循环"的本意（采样循环=不问也要按点跑）。
        check("接的是 ScreenMonitor，没新开采样循环",
              guide._ask_timer.isSingleShot()
              and "setInterval" not in source
              and "Thread(" not in source,
              f"singleShot={guide._ask_timer.isSingleShot()}")
        check("红线口径写进注释了", "只读" in source and "不读窗口标题" in source, "")

        section("S3 红线：两库零写入、不落盘")
        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        guide._awaiting = False
        guide._last_asked.clear()
        for proc in (KNOWN_HIT, KNOWN_MISS, "steam.exe"):
            guide.on_foreground(proc)
            guide.confirm("好呀")
            drain_threads()
            pump(0.05)
        check("pet.db utterance 一行没多", _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")

        section("S3 红线：没有新依赖、没有自己的联网客户端")
        check("game.py 不直接 import 联网库",
              not any(w in source for w in ("requests", "httpx", "urllib", "aiohttp")), "")
        check("联网走的是既有的 create_search（工作卡片同一条通道）",
              "LLMFactory.create_search" in source, "")
        check("除了 create_search，没有别的模型调用口子",
              _factory_calls(gmod) == 1, f"{_factory_calls(gmod)} 处")

        section("S0 边界：认不出的前台游戏，一个字都不冒（宁可漏判）")
        guide._awaiting = False
        guide._last_asked.clear()
        probe.reset()
        guide.on_foreground("SomeUnknownGame.exe")
        check("认不出的游戏不冒话", not probe.offers and not probe.spoken, "")
        check("认不出也不会留下等待态", guide.awaiting_confirm is False, "")
    finally:
        drain_threads()
        box.cleanup()
    return summary("游戏攻略")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
