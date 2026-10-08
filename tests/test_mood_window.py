"""
影视情绪窗口的两道闸：**首弹门槛 + 切换去抖**（v2-mood-window）的自动测试。

真机症状（用户报的）：看一段**愤怒**的戏，开局先飘「我怕怕…」，过一会儿又改成
「太气人」。根子不是窗口太长（45~60 秒那份耐心照旧，`scene_vote_window_sec` 与
`mood_cv` 的 PROTO_* 三个阈值**一个数没动**），是这条一分钟窗口规则缺两道闸：

1. **首弹太轻**：窗口一满、只要有一态是"唯一最多票"就立刻开口——没有票数下限、
   没有占比下限。真机一拍是"5 秒采样 + 一次识别约 4 秒"，60 秒窗口里装得下十来拍、
   有定论的更少（实测弃权率 31%，真视频更高），于是**3 票就能定案**，开局那几帧
   偏恐怖，第一句就飘错了。
2. **翻案太容易**：定案之后，下一拍窗口里的唯一最多票一变就立刻改口，没有"新态得
   持续多久"的门槛。

两道闸（都在 `life/screen/vote.py`，取值依据见收尾报告第九十九节的仿真表）：

- **首弹门槛**：窗口满之外，还要**有定论的票 ≥8** 且**最多那一态占比 ≥60%**；
- **切换去抖**：已弹的那一态要被换掉，新态得**连续**当 ≥10 秒的唯一最多票（"窗口占
  多数"不必单独判——三态下唯一最多票必然过半，推理见 `vote.py` 里那段注释）。

这个模块**一律走生产默认参数**（`SceneVoter(...)` 一个门槛参数都不传），跟
`test_mood_vote.py` 分开：那边验"多数票本身"（满窗 / 平票 / 弃权 / 剪票），把闸摘掉
是为了两件事各验各的；这边验闸，一条都不放水。

红线：pet.db / mind.db 零写入、零新依赖（两道闸只用标准库）。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, use_scene_vote

from life.screen import mood_cv
from life.screen import vote as vote_mod
from life.screen.companion import SCENE_LINES
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR
from life.screen.vote import (
    FIRST_MIN_SHARE,
    FIRST_MIN_VOTES,
    FIRST_MIN_VOTES_BY_STATE,
    SWITCH_HOLD_SEC,
    SceneVoter,
    first_min_votes_for,
)

ROOT = Path(__file__).resolve().parents[1]

# 真机一拍 = 采样 5 秒 + 一次识别；测试里用假钟按 5 秒走。
TICK = 5.0
WINDOW = 60.0


def _voter(start: float = 0.0):
    """生产默认参数的投票器 + 一个假钟（**不传任何门槛参数** = 闸全开）。"""
    box = {"t": start}
    return SceneVoter(WINDOW, now=lambda: box["t"]), box


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _imports(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names += [alias.name.split(".")[0] for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.append(node.module.split(".")[0])
    return names


def main() -> int:
    # ---------- ① 契约：窗口与阈值一个数没动 ----------

    section("① 契约：窗口 45~60 秒与 PROTO_* 三个阈值**一个数没动**")

    check("首弹门槛基础值 = 8 票 / 60%（占比这道一个数没动）",
          (FIRST_MIN_VOTES, FIRST_MIN_SHARE) == (8, 0.60),
          f"{FIRST_MIN_VOTES} 票 / {FIRST_MIN_SHARE}")
    check("按态分票（v2-mood-window-tune + v2-mood-fear-tune）：愤怒降到 6、恐惧抬回 8、笑容回落 8",
          first_min_votes_for(SCENE_ANGRY) == 6 and first_min_votes_for(SCENE_HORROR) == 8
          and first_min_votes_for(SCENE_FESTIVE) == 8,
          f"怒 {first_min_votes_for(SCENE_ANGRY)} / 恐 {first_min_votes_for(SCENE_HORROR)}"
          f" / 笑 {first_min_votes_for(SCENE_FESTIVE)}")
    check("按态表没有别的洞（没单列的态回落 8）",
          first_min_votes_for("没这一态") == 8, str(first_min_votes_for("没这一态")))
    check("切换去抖 = 10 秒（没动）", SWITCH_HOLD_SEC == 10.0, f"{SWITCH_HOLD_SEC} 秒")

    plain = SceneVoter(WINDOW)
    check("构造函数默认就是这两道闸（不传参数 = 生产口径）",
          (plain.first_min_votes, plain.first_min_share, plain.switch_hold_sec)
          == (FIRST_MIN_VOTES, FIRST_MIN_SHARE, SWITCH_HOLD_SEC), "")
    check("生产默认带着按态分票表（怒 6 / 恐 8，其余回落 8）",
          plain.first_min_votes_by_state == FIRST_MIN_VOTES_BY_STATE == {SCENE_ANGRY: 6,
                                                                         SCENE_HORROR: 8},
          str(plain.first_min_votes_by_state))
    check("「窗口占多数」没有单独判一次——三态下唯一最多票必然过半（数学见 vote.py）",
          not hasattr(plain, "switch_min_share"), "")
    check("窗口宽度默认仍是 60 秒（没动）", plain.window == 60.0, str(plain.window))
    check("PROTO_* 三个阈值一个数没动（0.76 / 0.03 / 20.0）",
          (mood_cv.PROTO_CONF, mood_cv.PROTO_MARGIN, mood_cv.PROTO_TEMP) == (0.76, 0.03, 20.0),
          f"{mood_cv.PROTO_CONF} / {mood_cv.PROTO_MARGIN} / {mood_cv.PROTO_TEMP}")

    from dataclasses import fields as _fields

    from core.config import ScreenSection
    _defaults = {f.name: f.default for f in _fields(ScreenSection)}
    check("配置那一格的默认值还是 60 秒（没动）",
          _defaults["scene_vote_window_sec"] == 60.0,
          str(_defaults["scene_vote_window_sec"]))

    # ---------- ② 首弹门槛 ----------

    section("② 首弹门槛：票太少 / 占比不够 → 先不开口；够了才开口")

    v, clk = _voter()
    for i in range(13):          # 0/5/…/60 秒：正好一个满窗口，13 张票全恐怖
        clk["t"] = i * TICK
        got = v.note(SCENE_HORROR)
    check("满窗口 + 13 票全是恐怖（票数与占比都够）→ 首弹恐怖",
          got == SCENE_HORROR and v.held == SCENE_HORROR, repr(got))

    v, clk = _voter()
    got = None
    for i in range(4):           # 只走到 15 秒，4 张票
        clk["t"] = i * TICK
        got = v.note(SCENE_HORROR)
    check("窗口还没满 → 什么都不弹（老规矩，没动）", got is None and v.held is None, repr(got))

    # 真机上弃权率三成往上（真视频更高）：一个满窗口常只有 5~7 张有定论的票。
    # v2-mood-window-tune 把愤怒/恐惧降到 6、笑容维持 8；v2-mood-fear-tune 又把恐惧
    # 抬回 8（只认真恐、别让剧情紧张漏成恐惧）——所以 5 张恐怖票仍不弹、8 张恐怖票
    # 才弹；笑容照旧 8 张才弹。两个门槛各验一条。
    v, clk = _voter()
    got = None
    for i in range(13):
        clk["t"] = i * TICK
        got = v.note(SCENE_HORROR if i < 5 else None)
    check("★ 满窗口 + 只有 5 张有定论的恐怖票（< 8）→ 不弹，接着攒（真机的常态）",
          got is None and v.held is None and len(v) == 5, f"{got!r} / {len(v)} 票")

    decided = None
    for i in range(13, 30):
        clk["t"] = i * TICK
        got = v.note(SCENE_HORROR)
        if got is not None:
            decided = (got, len(v))
            break
    check("★ 恐怖攒到 **8 张**有定论的票那一刻才开口（v2-mood-fear-tune 抬回 8）",
          decided == (SCENE_HORROR, 8), str(decided))

    v, clk = _voter()
    got = None
    for i in range(13):
        clk["t"] = i * TICK
        got = v.note(SCENE_FESTIVE if i < 7 else None)
    check("满窗口 + 只有 7 张笑容票（< 8）→ 不弹（笑容弃权低，没放宽）",
          got is None and v.held is None and len(v) == 7, f"{got!r} / {len(v)} 票")
    decided = None
    for i in range(13, 30):
        clk["t"] = i * TICK
        got = v.note(SCENE_FESTIVE)
        if got is not None:
            decided = (got, len(v))
            break
    check("★ 笑容攒到 **8 张**才开口（维持原门槛，没跟着放宽）",
          decided == (SCENE_FESTIVE, 8), str(decided))

    v, clk = _voter()
    plan = ([SCENE_HORROR] * 6 + [SCENE_ANGRY] * 7)[:13]      # 13 票：恐怖 6 / 愤怒 7
    got = None
    for i, scene in enumerate(plan):
        clk["t"] = i * TICK
        got = v.note(scene)
    check("票数够了（13 票）但最多的那一态只有 7/13 = 54% < 60% → 不弹",
          got is None and v.held is None, f"{got!r}（7/13 = {7 / 13:.0%}）")

    # ---------- ③ 开局混惊恐：首弹不该弹错 ----------

    section("③ 真机那个开局：愤怒戏 + 开局几拍惊恐 → 首弹必须是愤怒")

    def _feed(v, clk, plan, start_index=0):
        """喂一串逐拍结论（`None` = 这一拍弃权），返回第一次开口的那一态与时刻。"""
        for i, scene in enumerate(plan):
            clk["t"] = (start_index + i) * TICK
            got = v.note(scene)
            if got is not None:
                return got, clk["t"]
        return None, None

    # 用户报的那一段戏长这样：**第一个满窗口里惊恐只是"略多于"愤怒**（5 对 4，
    # 外加 4 拍弃权）——这 5/9 = 56% 的领先正是"三两帧噪点就能造出来"的那种领先。
    # 之后画面回到愤怒（真机上那一段本来就是愤怒戏，开局几个镜头偏阴森）。
    OPENING = [SCENE_HORROR] * 5 + [SCENE_ANGRY] * 4 + [None] * 4
    PLAN = OPENING + [SCENE_ANGRY] * 20

    v, clk = _voter()
    got, at = _feed(v, clk, PLAN)
    check("★ 开局混惊恐的愤怒戏 → 首弹是**愤怒**，不是先飘「我怕怕…」",
          got == SCENE_ANGRY and v.held == SCENE_ANGRY, f"{got!r} @ {at} 秒")
    check("而且只在窗口满之后才开口（没抢跑）", at is not None and at >= 60.0, f"{at} 秒")

    # 反向对照：同一串帧，把两道闸摘掉（旧口径）——头一句就是错的。
    clk_old = {"t": 0.0}
    v_old = SceneVoter(WINDOW, now=lambda: clk_old["t"],
                       first_min_votes=0, first_min_share=0.0, switch_hold_sec=0.0)
    got_old, at_old = _feed(v_old, clk_old, PLAN)
    check("反向对照：同一串帧走**旧口径**首弹就是惊恐——这道门槛不是恒真的",
          got_old == SCENE_HORROR, f"{got_old!r} @ {at_old} 秒")
    check("旧口径下它甚至**第一个满窗口就开口**了（56% 的领先就够）",
          at_old == 60.0, f"{at_old} 秒")

    # ---------- ④ 切换去抖 ----------

    section("④ 切换去抖：已弹的那一态要被换掉，新态得连续当 ≥10 秒的唯一最多票")

    # 这一节用**短窗口 + 密拍**（10 秒窗口 / 2 秒一拍）把去抖的节奏走完：窗口短，老票
    # 什么时候退场一眼看得清；拍密，10 秒那条线 5 拍就走到了。**第一道闸（首弹）在这一节
    # 放宽到 1 票**：10 秒的窗口里最多只有 6 张票，生产门槛（≥8 票）在这么短的窗口里
    # 永远达不到，而这一节要验的是**第二道闸**——首弹门槛本身在 ①（默认值）②③（真窗口
    # 60 秒）⑦（端到端）里按生产口径验，这里放宽不改变第二道闸的取值（`switch_hold_sec`
    # 仍是生产默认 10 秒，一条没动）。
    def _settled_voter(window: float = 10.0, step: float = 2.0):
        box = {"t": 0.0}
        v = SceneVoter(window, now=lambda: box["t"], first_min_votes=1, first_min_share=0.0)
        v.note(SCENE_HORROR)               # t=0 这一拍（窗口没满，还不弹）
        box["t"] = window                  # 走到窗口满
        got = v.note(SCENE_HORROR)
        assert got == SCENE_HORROR, got    # 窗口满 + 占比 100% → 首弹恐怖
        return v, box, step

    def _feed(v, box, step, plan):
        """按 plan 喂 [(拍号, 结论)]，返回每一次的返回值。"""
        outs = []
        for i, scene in plan:
            box["t"] = i * step
            outs.append(v.note(scene))
        return outs

    v, box, step = _settled_voter()
    check("前置：这一场已经弹过恐怖（生产门槛下弹出来的）",
          v.held == SCENE_HORROR and len(v) == 2, f"{v.held!r} / {len(v)} 票")

    # 6 拍之后画面全变愤怒（真机上一段戏切到另一种情绪）。票数与窗口的推移：
    #   t=12 恐怖(10) / 愤怒(12)                → 平票，不定案，挑战者计时作废
    #   t=14 恐怖(10) / 愤怒(12,14)             → 愤怒成唯一最多票，计时从 14 秒起
    #   t=16 … t=22                             → 一路是愤怒，到 t=24 正好满 10 秒
    outs = _feed(v, box, step, [(6, SCENE_ANGRY), (7, SCENE_ANGRY), (8, SCENE_ANGRY)])
    check("新态冒头前先是平票 → 不定案（平票那一拍挑战者计时作废）",
          outs[0] is None and v.held == SCENE_HORROR, str(outs))
    check("★ 新态成为唯一最多票才 2 秒 → 不翻案（已经飘在头上的「我怕怕…」照旧留着）",
          outs[1] is None and v.held == SCENE_HORROR, str(outs))
    check("★ 连续到 6 秒 → 还是不翻",
          outs[2] is None and v.held == SCENE_HORROR, str(outs))

    outs = _feed(v, box, step, [(9, SCENE_ANGRY), (10, SCENE_ANGRY), (11, SCENE_ANGRY),
                                (12, SCENE_ANGRY)])
    check("8 秒 → 不翻", outs[0] is None, str(outs))
    check("★ 满 10 秒（t=14 → 24）→ 这时才准翻案，翻成愤怒",
          outs[3] == SCENE_ANGRY and v.held == SCENE_ANGRY, str(outs))
    check("翻案之后再拍还是愤怒 → 不重复报（monitor 那边值没变）",
          _feed(v, box, step, [(13, SCENE_ANGRY)]) == [SCENE_ANGRY], "")

    # 挑战者中途被打断：计时必须**从头再来**（恐怖票回来把票数打成平票）。
    v, box, step = _settled_voter()
    _feed(v, box, step, [(6, SCENE_ANGRY), (7, SCENE_ANGRY), (8, SCENE_ANGRY)])
    got = _feed(v, box, step, [(9, SCENE_HORROR)])          # t=16：恐怖 2 票 / 愤怒 2 票
    check("挑战者被打断（平票，挑战者不再是最多票）→ 什么都不动、计时作废",
          got == [None] and v.held == SCENE_HORROR, str(got))
    outs = _feed(v, box, step, [(10, SCENE_ANGRY), (11, SCENE_ANGRY)])
    check("★ 挑战者重新冒头（t=18）→ 从这一刻重新计时，4 秒时仍不翻",
          outs[1] is None and v.held == SCENE_HORROR, str(outs))
    outs = _feed(v, box, step, [(12, SCENE_ANGRY), (13, SCENE_ANGRY), (14, SCENE_ANGRY)])
    check("★ 被打断过一趟 → 翻案推迟到 t=28（比没打断那趟晚 4 秒，正是被打断的那段）",
          outs[-1] == SCENE_ANGRY and v.held == SCENE_ANGRY, str(outs))

    # 弃权帧不该打断挑战者：它本来就不投票，窗口里的票一张没动。
    v, box, step = _settled_voter()
    _feed(v, box, step, [(6, SCENE_ANGRY), (7, SCENE_ANGRY), (8, SCENE_ANGRY)])
    got = _feed(v, box, step, [(9, None), (10, None)])
    check("中间两拍弃权 → 照旧什么都不动（挑战者还在计时）",
          got == [None, None] and v.held == SCENE_HORROR, str(got))
    outs = _feed(v, box, step, [(11, SCENE_ANGRY), (12, SCENE_ANGRY), (13, SCENE_ANGRY)])
    check("★ 起点仍是 t=14 → t=24 满 10 秒，照旧翻案（弃权不打断）",
          outs[-1] == SCENE_ANGRY and v.held == SCENE_ANGRY, str(outs))

    # ---------- ⑤ 整窗全弃权 / 散场 / reset ----------

    section("⑤ 整窗全弃权维持上次不散场（沿用 A）；reset（换一场）重新走门槛")

    v, box, step = _settled_voter()
    got = None
    for i in range(6, 12):                 # t=12 … 22：整整一窗全是弃权，老票在这一窗里退干净
        box["t"] = i * step
        got = v.note(None)
    check("整窗全弃权 → 什么都不动、**不散场**（散场只由 monitor 的硬闸触发）",
          got is None and v.held == SCENE_HORROR and len(v) == 0,
          f"{got!r} / held={v.held!r} / 票 {len(v)}")

    v.reset()
    check("reset 之后已弹的那一态也清掉（新的一场重新走首弹门槛，不接着上一场的账）",
          v.held is None and v.decide() is None, repr(v.held))
    v.note(SCENE_HORROR)
    check("reset 后窗口从这一拍重新算：一拍不满窗 → 不弹",
          v.held is None and v.decide() is None, repr(v.held))

    # ---------- ⑥ 单拍档照旧 ----------

    section("⑥ 「一拍即定」那一档（窗口 0 秒）照旧：门槛在那种模式下没有意义")

    box0 = {"t": 5.0}
    v0 = SceneVoter(0.0, now=lambda: box0["t"])
    check("★ 窗口 0 秒 + 只有一票 → 照旧立刻给结论（旧 scene_stable_samples=1）",
          v0.note(SCENE_FESTIVE) == SCENE_FESTIVE, "")
    box0["t"] = 10.0
    check("窗口 0 秒下照样不卡切换（下一拍换态就换，票箱每拍只有一张）",
          v0.note(SCENE_ANGRY) == SCENE_ANGRY, "")

    # ---------- ⑦ 端到端：monitor → companion → 飘出来的那句 ----------

    section("⑦ 端到端：一段愤怒戏（开局混惊恐）→ 第一句飘出来的就是「太气人」")

    box, _stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        cfg = app.config.screen
        cfg.enabled = True
        cfg.companion_enabled = True

        db_path = app.config.db_path
        mind_path = app.config.mind.db_path
        before_utt = _rows(db_path, "utterance")
        before_mind = _rows(mind_path, "fragment")

        said: list[str] = []
        app.bus.pet_speak_proactive.connect(said.append)

        # 真机那一段戏：开局第一个满窗口里惊恐只是**略多于**愤怒（5 对 4 + 4 拍弃权，
        # 56% 的领先），之后画面回到愤怒。
        OPENING = [SCENE_HORROR] * 5 + [SCENE_ANGRY] * 4 + [None] * 4
        PLAN = OPENING + [SCENE_ANGRY] * 20

        def run_segment(gates: bool) -> list[str]:
            """喂完这段戏，返回这段时间**飘出来的话**。"""
            said.clear()
            companion._last_spoken.clear()
            companion._cursor.clear()
            monitor._scene = ""
            monitor._forget_scene()
            clock = use_scene_vote(monitor, window=WINDOW, gates=gates)
            for i, scene in enumerate(PLAN):
                if i:
                    clock.advance(TICK)
                monitor._count_scene(scene)
                pump(0.01)
            return list(said)

        try:
            lines_new = run_segment(gates=True)
            check("★ 走生产门槛：第一句是愤怒那句「太气人，真想捶他几下」",
                  lines_new[:1] == [SCENE_LINES[SCENE_ANGRY][0]], str(lines_new))
            check("★ 而且「我怕怕…」这一场**一次都没飘过**（真机那个症状就是这个）",
                  SCENE_LINES[SCENE_HORROR][0] not in lines_new, str(lines_new))
            check("全程只飘一句（同一态有 10 分钟冷却，不刷屏）",
                  len(lines_new) == 1, str(lines_new))

            lines_old = run_segment(gates=False)
            check("反向对照：把闸摘掉（旧口径）同一段戏**第一句就是「我怕怕…」**",
                  lines_old[:1] == [SCENE_LINES[SCENE_HORROR][0]], str(lines_old))
        finally:
            app.bus.pet_speak_proactive.disconnect(said.append)

        section("⑧ 红线：两个库零写入 + 零新依赖")

        check("pet.db 的 utterance 一行没多",
              _rows(db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              _rows(mind_path, "fragment") == before_mind,
              f"{before_mind} → {_rows(mind_path, 'fragment')}")

        imports = set(_imports(ROOT / "life" / "screen" / "vote.py"))
        # 「零新依赖」= 不新增第三方包；`life` 是同库的 scenes 叶子常量模块（SCENE_* 的
        # 唯一定义处），不算新依赖。
        check("vote.py 不 import 第三方包（零新依赖；只多了同库 scenes 叶子）",
              imports <= {"__future__", "time", "collections", "typing", "life"},
              str(sorted(imports)))
        src = (ROOT / "life" / "screen" / "vote.py").read_text(encoding="utf-8")
        check("两道闸纯内存：vote.py 里没有任何落盘入口",
              not any(word in src for word in ("open(", "sqlite3", "write_bytes", "write_text")), "")

        drain_threads()
    finally:
        box.cleanup()

    return summary("影视情绪窗口的两道闸：首弹门槛（≥8 票且 ≥60%）+ 切换去抖（≥10 秒连续）"
                   "（v2-mood-window）")


if __name__ == "__main__":
    raise SystemExit(main())
