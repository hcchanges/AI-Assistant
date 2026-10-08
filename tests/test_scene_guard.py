"""
场景互斥 + 游戏优先 + 情绪提速（v2-mood-detect 扩写 + v2-game-vs-movie）的自动测试。

真机模块⑥（游戏被判成电影）与图2（番茄钟专注中仍弹「看电影啦」）暴露的六件事：

① **场景互斥。** 专注 / 游戏 / 观影 / 休闲 原先**没有**任何仲裁点——自动番茄钟、
   游戏攻略、刷剧陪伴各听各的 `foreground_changed`，可以同时开口。现在收成一个
   `SceneArbiter`：优先级 专注 > 游戏 > 观影 > 休闲，同一时刻只返回一个。
② **游戏优先于电影。** 判"在看片"的闸原先只看"进程名在播放器名单里吗 / 窗口铺满屏
   吗"——游戏窗口多半也是全屏，于是打游戏冒出"主人，看电影啦"。现在进程名认得出的
   游戏直接否。
③ **场景判定改走一分钟多数票。** （v2-mood-detect 再审·终）原先固定要连中
   `scene_stable_samples`(2) 拍；后来短暂有过"画面腿干脆利落就一拍即报"的快路。
   现在两条都撤了，统一走 `life/screen/vote.py` 的 60 秒滑动窗口多数票：窗口没满
   或平票**什么都不动**（维持上一次判定，不秒退），窗口里多数是"没情绪"才算散场。
④ **本地播放器提速。** 认得出名字的本地播放器是硬证据，开场字幕的宽限期从 12 秒
   降到 `opening_grace_fast_sec`(4 秒)。
⑤ **笑容「好开心」。** 笑容的分数本来就贴在阈值线附近，跟着窗口多数票一起走。

每条都给**反向对照**（不被互斥挡的时候照旧开口、窗口没满/平票的时候不动），
免得断言变成恒真的。红线：pet.db / mind.db 一行不多；零新依赖。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, use_scene_vote, wait_for

from life.screen import monitor as mon
from life.screen.mood_cv import MoodVerdict
from life.screen.mood_fuse import SOURCE_CV, fuse
from life.screen.scene_guard import (
    KIND_FOCUS,
    KIND_GAME,
    KIND_IDLE,
    KIND_WATCH,
    SceneArbiter,
)
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR

# 认得出的游戏 / 认得出的本地播放器 / 浏览器，各取一个当代表
GAME_PROC = "league of legends.exe"
PLAYER_PROC = "potplayer.exe"
BROWSER_PROC = "chrome.exe"


def _verdict(scene: str, top: float, runner: float) -> MoodVerdict:
    """造一个画面腿的判读：`scene` 拿 `top` 分，第二名 `runner` 分。"""
    rest = [s for s in (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE)
            if s != scene]
    scores = {scene: top}
    for index, other in enumerate(rest):
        scores[other] = runner if index == 0 else runner / 2
    return MoodVerdict(scene=scene, score=top, scores=scores)


def _feed(monitor, verdict: MoodVerdict, text: str = ""):
    """把一拍的判读结论喂进采样链（等价于这一拍识别完回来了）。"""
    monitor._on_analysis((text, verdict, True))


def main() -> int:
    # ---------- ① 守卫本身 ----------

    section("① 守卫：专注 / 游戏 / 观影 / 休闲，同一时刻只认一个")

    focus_on = {"value": False}
    guard = SceneArbiter(is_focusing=lambda: focus_on["value"])
    check("什么都没演 → 休闲", guard.active == KIND_IDLE, guard.active)
    check("休闲时观影准开", guard.allow(KIND_WATCH) is True, "")
    check("休闲时游戏也准开", guard.allow(KIND_GAME) is True, "")

    guard.set_watch(True)
    check("开了看片会话 → 说了算的是观影", guard.active == KIND_WATCH, guard.active)

    guard.set_game("英雄联盟")
    check("同一时刻游戏压过观影（active 只有一个）",
          guard.active == KIND_GAME, guard.active)
    check("游戏中不许再开观影（游戏优先于电影）",
          guard.allow(KIND_WATCH) is False, "")
    check("游戏自己照开", guard.allow(KIND_GAME) is True, "")

    focus_on["value"] = True
    check("专注一开 → 专注说了算（压住游戏与观影）",
          guard.active == KIND_FOCUS, guard.active)
    check("专注中不许开观影（图2 那条 bug 的判据）",
          guard.allow(KIND_WATCH) is False, "")
    check("专注中也不弹攻略", guard.allow(KIND_GAME) is False, "")

    focus_on["value"] = False
    guard.set_game("")
    guard.set_watch(False)
    check("专注结束、游戏也退 → 回到休闲", guard.active == KIND_IDLE, guard.active)

    check("没接线（is_focusing=None）→ 当没在专注，不误挡",
          SceneArbiter().focusing is False, "")
    check("认不出的档位一律放行（不把新档位误挡在门外）",
          SceneArbiter().allow("something-new") is True, "")

    # ---------- ② 游戏优先于电影 ----------

    box, stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        guide = app.game_guide
        pet = app.pet_window
        # v2-scene-state 之后"问攻略"默认要等开场白说完（30 秒）。这一节测的是场景互斥
        # （游戏压过电影），不是那个等待，所以归零——等待另有一条专门的测试。
        app.config.screen.guide_ask_delay_sec = 0
        pet.show()
        pump(0.05)
        pet._settled = True

        # 让"全屏"这一档恒为真：把"全屏游戏窗口"这个真机场景摆出来。不这么铺的话，
        # 离屏环境里 is_fullscreen_foreground() 恒为 False，正向那条线里"画面运动"
        # 那一档根本走不到。
        # （v2-social-scene-boundary 之后反向那条线不再看全屏也不再看画面——它只认名单；
        # 下面那条前置断言跟着改成了"认不出的照样不算在看片"。）
        real_fullscreen = mon.is_fullscreen_foreground
        mon.is_fullscreen_foreground = lambda: True

        section("② 游戏优先于电影：全屏游戏窗口不许再被判成「在看片」")

        check("前置：认不出名字的窗口**不算**在看片（全屏兜底已删，判据只有名单）",
              monitor.is_watch_foreground("mystery_player.exe") is False, "")
        check("全屏**游戏**窗口 → 不算在看片（修前这里会返回 True）",
              monitor.is_watch_foreground(GAME_PROC) is False, "")
        check("游戏也不进采样那一道门（不截屏、不认字）",
              monitor._watch_candidate(GAME_PROC) is False, "")

        said: list[str] = []
        app.bus.screen_caption.connect(lambda line: said.append(line))
        offers: list[str] = []
        app.bus.game_offer.connect(lambda game: offers.append(game))

        # 走真路：两条线都挂在同一个 foreground_changed 上，这里挨个叫一遍
        companion.on_foreground(GAME_PROC)
        guide.on_foreground(GAME_PROC)
        pump(0.05)

        check("游戏在前台 → 不开看片会话",
              companion._watch_open is False, str(companion._watch_open))
        companion._show_opening()      # 就算宽限期到了也不该有东西冒出来
        pump(0.02)
        check("游戏在前台 → 一个字都不冒（不弹「看电影啦」）",
              said == [], str(said))
        check("游戏在前台 → 攻略那条照问（只弹攻略，不弹电影）",
              offers == ["英雄联盟"], str(offers))

        # 反向对照：同一个全屏判据下，**不是游戏**的进程照旧开看片会话。
        # 两条线都要通知到（真机上它们挂在同一个 foreground_changed 上，一起收到）：
        # 游戏那条线得先知道"前台已经离开游戏了"，守卫里的游戏态才退——
        # 这正是互斥在起作用，不是多余的仪式。
        guide.on_foreground(PLAYER_PROC)
        check("前台离开游戏 → 守卫里的游戏态退掉",
              app.screen_scene_guard.active == KIND_IDLE,
              app.screen_scene_guard.active)
        companion.on_foreground(PLAYER_PROC)
        pump(0.02)
        check("反向对照：同样的全屏判据，非游戏 → 看片会话照开",
              companion._watch_open is True, str(companion._watch_open))

        guide.on_foreground("explorer.exe")     # 退游戏，清掉守卫里的游戏态
        companion.on_foreground("explorer.exe")
        mon.is_fullscreen_foreground = real_fullscreen
        pump(0.02)

        # ---------- ③ 专注中不出观影邀请 ----------

        section("③ 专注中不出观影邀请（图2：番茄钟跑着还冒「主人，看电影啦」）")

        guard = app.screen_scene_guard
        said.clear()
        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        pump(0.05)

        app.screen_focus_timer.enter("chaoxing.exe")   # 进专注（自动那套）
        pump(0.05)
        check("前置：确实进了专注、守卫也认",
              app._is_focusing() and guard.active == KIND_FOCUS,
              f"{app._is_focusing()} / {guard.active}")

        companion.on_foreground(PLAYER_PROC)           # 专注中切进播放器
        pump(0.02)
        check("专注中切进播放器 → 不开看片会话",
              companion._watch_open is False, str(companion._watch_open))
        companion._show_opening()
        pump(0.02)
        check("专注中 → 一个字都没冒（修前这里会发开场陪伴字幕）",
              said == [], str(said))

        section("③ 反向对照：不在专注时，同一个前台照旧开口")

        app.screen_focus_timer.reset()
        pump(0.05)
        check("前置：已经退出专注", app._is_focusing() is False, str(app._is_focusing()))
        companion.on_foreground(PLAYER_PROC)
        pump(0.02)
        check("不在专注 → 看片会话照开", companion._watch_open is True, "")
        companion._show_opening()
        pump(0.05)
        check("不在专注 → 开场陪伴字幕照发",
              said == ["主人，看电影啦，我陪你看吧！"], str(said))

        section("③ 反向对照：先在看片、随后进专注 → 已开着的那一场自己收掉")

        check("前置：看片会话正开着", companion._watch_open is True, "")
        app.pomodoro_service.timer.start_focus()       # 用户手动点「开始专注」
        pump(0.05)
        check("手动进专注 → 看片会话被收掉",
              companion._watch_open is False, str(companion._watch_open))
        check("守卫也换成专注说了算", guard.active == KIND_FOCUS, guard.active)
        app.pomodoro_service.timer.reset()
        pump(0.05)

        # ---------- ④ 场景判定：一分钟多数票 ----------

        section("④ 场景判定：窗口没满不报，窗口满了按多数票定案")

        app.screen_focus_timer.reset()
        pump(0.02)
        monitor._forget_scene()
        monitor._scene = ""
        clock = use_scene_vote(monitor, window=10.0)      # 10 秒窗口，喂三拍（每拍 5 秒）

        seen: list[str] = []
        monitor.scene_changed.connect(lambda scene: seen.append(scene))

        decisive = _verdict(SCENE_HORROR, 0.91, 0.05)
        merged = fuse(None, decisive)
        check("前置：这一拍确实只有画面腿在说话",
              merged.source == SOURCE_CV, merged.source)

        _feed(monitor, decisive)
        check("第一票：窗口没满 → 不报（修前这里靠'连击 1/2'挡着）", seen == [], str(seen))
        clock.advance(5)
        _feed(monitor, decisive)
        check("第二票：还不满 10 秒 → 照旧不报", seen == [], str(seen))
        clock.advance(5)
        _feed(monitor, decisive)
        check("第三票：满 10 秒、三票全是恐怖 → 定案", seen == [SCENE_HORROR], str(seen))

        section("④ 反向对照：多数票是别的情绪，就不跟着某一拍走")

        monitor._forget_scene()
        monitor._scene = ""
        seen.clear()
        _feed(monitor, _verdict(SCENE_ANGRY, 0.80, 0.05))       # 愤怒 1 票
        clock.advance(5)
        _feed(monitor, _verdict(SCENE_FESTIVE, 0.90, 0.04))     # 笑容 1 票（还打平）
        clock.advance(5)
        _feed(monitor, _verdict(SCENE_FESTIVE, 0.88, 0.06))     # 笑容 2 票 → 过半数
        check("窗口里笑容 2/3 → 定案笑容（不是第一票那个愤怒）",
              seen == [SCENE_FESTIVE], str(seen))

        section("④ 反向对照：两票打平 → 不定案，维持上一次判定（不秒退）")

        monitor._voter.reset()
        monitor._scene = SCENE_FESTIVE          # 假装这会儿正在显示笑容
        seen.clear()
        _feed(monitor, _verdict(SCENE_ANGRY, 0.80, 0.05))
        clock.advance(10)
        _feed(monitor, _verdict(SCENE_HORROR, 0.85, 0.05))
        check("愤怒 1 / 恐怖 1 打平 → 一个信号都不发，场景仍是笑容",
              seen == [] and monitor._scene == SCENE_FESTIVE,
              f"{seen} / {monitor._scene!r}")

        # ---------- ⑤ 本地播放器提速 ----------

        section("⑤ 本地播放器提速：认得出名字的播放器，开场字幕不等满 12 秒")

        companion.on_foreground(PLAYER_PROC)
        pump(0.02)
        check("本地播放器（potplayer）→ 走短宽限期 4 秒",
              companion._grace_ms() == 4000, str(companion._grace_ms()))
        companion._end_watch()
        companion.on_foreground(BROWSER_PROC)
        pump(0.02)
        check("浏览器 → 照旧走长宽限期 12 秒（证据弱，得给剧情留时间）",
              companion._grace_ms() == 12000, str(companion._grace_ms()))
        companion._end_watch()
        pump(0.02)

        # ---------- ⑥ 笑容「好开心」 ----------

        section("⑥ 笑容：窗口攒够、多数票是笑容就说「好开心」")

        monitor._voter.reset()
        monitor._scene = ""
        # 上一节（多数票是别的情绪）已经真冒过一次「好开心」，把同态冷却也吃掉了——
        # 不清的话这一节验的就不是"窗口攒够会说"，而是被冷却挡着。
        companion._last_spoken.clear()
        companion._cursor.clear()
        spoken: list[str] = []
        app.bus.pet_speak_proactive.connect(lambda line: spoken.append(line))

        smile = _verdict(SCENE_FESTIVE, 0.88, 0.06)
        _feed(monitor, smile)
        check("第一票：窗口没满，先不冒话",
              monitor._scene == "" and spoken == [], f"{monitor._scene!r} / {spoken}")
        clock.advance(10)
        _feed(monitor, smile)
        pump(0.05)
        check("第二票：窗口满、多数票是笑容 → 场景成立",
              monitor._scene == SCENE_FESTIVE, monitor._scene)
        check("宠物开口就是定稿原文「好开心」",
              spoken[:1] == ["好开心"], str(spoken[:1]))

        # ---------- 红线 ----------

        section("红线：这一轮只动判定与互斥，两个库一行不多")

        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        utterance = _rows(app.config.db_path, "utterance")
        fragments = app.mind_store.fragment_count()

        app.screen_focus_timer.enter("chaoxing.exe")
        pump(0.2)
        companion.on_foreground(PLAYER_PROC)
        guide.on_foreground(GAME_PROC)
        pump(0.2)
        app.screen_focus_timer.reset()
        app.pomodoro_service.timer.reset()
        drain_threads()
        check("pet.db 的 utterance 一行没多",
              _rows(app.config.db_path, "utterance") == utterance,
              f"{utterance} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              app.mind_store.fragment_count() == fragments, "")

        section("红线：零新依赖")

        root = Path(__file__).resolve().parents[1]
        src = (root / "life" / "screen" / "scene_guard.py").read_text(encoding="utf-8")
        check("scene_guard.py 只用 PySide6 + 标准库",
              "from PySide6" in src
              and "import numpy" not in src and "import cv2" not in src, "")
        check("这一轮没引新包（monitor 侧也只多 import 了自家 game 模块）",
              "import onnxruntime" not in (root / "life" / "screen" / "monitor.py").read_text(encoding="utf-8"),
              "")

        wait_for(lambda: False, timeout=0.01)
        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("场景互斥 + 游戏优先 + 情绪提速（v2-mood-detect / v2-game-vs-movie）")


if __name__ == "__main__":
    raise SystemExit(main())
