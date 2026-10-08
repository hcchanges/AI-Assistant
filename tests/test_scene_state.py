"""
场景状态机（v2-scene-state）的自动测试。

用户这一轮要的是"宠物看得出我这时候在干嘛，然后换一副样子陪着"，外加一个真机 bug：

- **三态互斥**：工作页 / 电影 / 游戏，同一时刻只算一场，宠物的样子跟着它走；
- **强制坐起**：进这三场就演"坐姿开心"那张图（不再溜达、趴着睡），打断/静音/暂停都
  不解锁，用户切走或进专注才解除（v2-scene-state-fix ②）；
- **开场白**：工作场「我陪你一起工作」、电影场「我陪你看电影」、游戏场「我陪你一起
  玩游戏啦」，进态首拍即飘（v2-scene-state-fix ①：电影不再复用看片线那句）；
- **过一会儿问一句**：工作场问「我可以帮你整理笔记，需要吗？」，游戏场问攻略，都摆出
  [好呀] [不用了] 轻按钮（v2-scene-state-fix ③：按钮真弹出来，不再藏在关着的框里）；
- **点「不用了」= 这一场把宠物收起来**（换个场次照旧出来）；
- **电影情绪词常驻头顶**：直到情绪变（换词）或散场（收掉）才消失，不再 60 秒秒退
  （v2-scene-state-fix ④）；
- **工作态整理笔记走金字塔结构**：结论先行 + 分层论据（v2-scene-state-fix ⑤）；
- **切回番茄专注，头顶的情绪词要立刻收掉**：用户真机反馈"切回专注了，头上还挂着
  「我怕怕…」"——原先只做到"不再冒新的"，已经飘在屏上的那句得等它自己的计时器。

本文件分十节：① 只验状态机本身（不走界面）；②~⑥ 走**真链路**（假前台 → 采样器信号 →
各条线 → 装配层）；⑦ 全程锁坐姿（v2-scene-state-fix）；⑧ 电影情绪词常驻；⑨ 金字塔笔记；
⑩ 红线。②~⑥ 里的"前台"是靠替换采样器的取进程名/标题两个口子造出来的（跟
test_focus_web / test_scene_guard 同款），走的是 `monitor.sample()` 那条真路，
不是直接去戳各个服务的私有方法。

**一个必须写明的优先级后果**：`focus_enabled` 开着时，前台落在工作页会先让**自动番茄钟**
开跑，仲裁点那边专注压着工作，于是这时"哪一场"是**专注**（要的是安静，宠物连姿态都放掉），
工作场只在番茄钟没在跑时才成立。这一条②节里有断言，节尾还有一条专门的对照。
红线：pet.db / mind.db 零写入；零新依赖。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtCore import QObject, Signal

from core.events import EventBus
from life.screen import monitor as mon
from life.screen.companion import OPENING_LINE, SCENE_LINES
from life.screen.scene_guard import (
    KIND_FOCUS,
    KIND_GAME,
    KIND_IDLE,
    KIND_WATCH,
    KIND_WORK,
    SceneArbiter,
)
from life.screen.scene_state import (
    ASK_NOTES,
    OPENING_GAME,
    OPENING_WATCH,
    OPENING_WORK,
    SceneStateService,
    is_work_page,
)
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR
from pet.behavior import Behavior
from ui.pet_skin import POSE_IDLE, POSE_SLEEP
from ui.pet_window import ZZZ_TEXT

# 三场的代表：工作页（网课/创作类里挑一个）、游戏（进程名表里认得出）、播放器
WORK_PROC = "pycharm64.exe"
GAME_PROC = "league of legends.exe"
PLAYER_PROC = "potplayer.exe"
OTHER_PROC = "explorer.exe"        # 既不是工作页、也不是游戏/播放器


class FakeMonitor(QObject):
    """只带场景状态机要的那两条信号的假采样器（真采样器要 Windows，①节起不来）。"""

    foreground_changed = Signal(str)
    title_changed = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.foreground_proc = ""
        self.title_kind = ""


class FakeReader:
    """假识别器：省掉一次真 OCR。"""

    def __init__(self) -> None:
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return ""


class FakeGrabber:
    """假取景器：**绝不真去抓屏**（离屏环境里真抓会拿到用户桌面上的东西）。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None):
        from PySide6.QtGui import QImage

        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _fake_foreground(monitor) -> dict:
    """把"前台是谁 / 标题是什么"两个口子换成可控的（同 test_focus_web）。"""
    holder = {"proc": "", "title": ""}
    mon.foreground.foreground_pid = lambda: 1
    monitor._pids.name_of = lambda pid: holder["proc"]
    mon.foreground.foreground_title = lambda: holder["title"]
    return holder


def _goto(monitor, holder, proc: str, title: str = "") -> None:
    """换前台（走真路）：改口子 → `sample()` → 信号发给所有挂了它的线。"""
    holder["proc"], holder["title"] = proc, title
    monitor.sample()
    wait_for(lambda: not monitor._reading)
    pump(0.05)


def main() -> int:
    box, _stub, app = make_env()
    try:
        monitor = app.screen_monitor
        guard = app.screen_scene_guard
        scene = app.scene_state
        pet = app.pet_window
        bubble = app.chat_bubble
        scheduler = app.behavior_scheduler
        companion = app.screen_companion
        focus = app.screen_focus_timer

        app.config.screen.enabled = True
        app.config.screen.companion_enabled = True
        app.config.screen.guide_enabled = True
        # 本节起先把**自动番茄钟关掉**：前台落在工作页时它会先开跑，仲裁点那边专注压着
        # 工作，"工作场"就永远轮不到（这后果⑥节最后专门验一次）。①②节要的是工作场本身。
        app.config.screen.focus_enabled = False
        app.config.screen.ocr_enabled = True
        # 屏幕上真在放什么与本文件无关：两条腿都换成假的，绝不真抓屏。
        monitor._reader = FakeReader()
        monitor._grabber = FakeGrabber()
        holder = _fake_foreground(monitor)

        pet.show()
        pump(0.05)

        # ---------- ① 状态机本体：三态互斥，同一时刻只有一个 ----------

        section("① 状态机本体：工作 / 游戏 / 观影互斥，同时只算一场")

        # 这一节不走界面、也不碰装配层那只宠物：只验"喂状态 → 问结论"这一件事。
        # 用自己的总线，免得这里冒的话打到装配层那只宠物头上。
        bus = EventBus()
        spoken: list[str] = []
        asked: list[str] = []
        offers: list[str] = []
        bus.pet_speak_proactive.connect(spoken.append)
        bus.pet_speak.connect(asked.append)
        bus.scene_offer.connect(offers.append)
        fake = FakeMonitor()
        focusing = {"on": False}
        # 工作那一档跟生产一样递**回调**（`is_work_page` 现算），不推一个布尔值——
        # 推值会在信号派发半途陈旧，这正是这一轮修掉的那个 bug。
        unit_guard = SceneArbiter(is_focusing=lambda: focusing["on"],
                                  is_working=lambda: is_work_page(fake))
        unit = SceneStateService(config=app.config, bus=bus, guard=unit_guard,
                                 ask_delay_ms=0)
        unit.attach(fake)

        check("前置：什么都没喂 → 空档 idle",
              unit_guard.active == KIND_IDLE and unit.state == KIND_IDLE,
              f"{unit_guard.active} / {unit.state}")

        # ---- 工作 ----
        fake.foreground_proc = WORK_PROC
        fake.foreground_changed.emit(WORK_PROC)
        check("工作页在前台 → 这一场是工作",
              unit_guard.active == KIND_WORK and unit.state == KIND_WORK,
              f"{unit_guard.active} / {unit.state}")
        check("工作场开场飘一句「我陪你一起工作」（只显示不出声那条线）",
              spoken == [OPENING_WORK], repr(spoken))
        check("问句紧跟着出口（这一节把等待设成 0）", asked == [ASK_NOTES], repr(asked))
        check("轻按钮跟着露出来（工作那一档）", offers == [KIND_WORK], repr(offers))
        check("正向对照：只有工作时也算工作（不是被别的压着）",
              unit_guard.active == KIND_WORK, unit_guard.active)

        # ---- 电影 ----
        unit_guard.set_watch(True)
        check("播放器开着看片会话 → 这一场是观影（工作让位）",
              unit_guard.active == KIND_WATCH and unit.state == KIND_WATCH,
              f"{unit_guard.active} / {unit.state}")
        check("电影场开场飘一句「我陪你看电影」（v2-scene-state-fix：不再复用看片线）",
              spoken == [OPENING_WORK, OPENING_WATCH], repr(spoken))

        # ---- 游戏 ----
        unit_guard.set_game("英雄联盟")
        check("游戏在前台 → 这一场是游戏（压过观影）",
              unit_guard.active == KIND_GAME and unit.state == KIND_GAME,
              f"{unit_guard.active} / {unit.state}")
        check("游戏场开场飘一句「我陪你一起玩游戏啦」",
              spoken == [OPENING_WORK, OPENING_WATCH, OPENING_GAME], repr(spoken))

        # ---- 三样一起喂：唯一的那把椅子归优先级最高的 ----
        flags = (unit_guard.focusing, bool(unit_guard.game), unit_guard.watching,
                 unit_guard.working)
        check("三场同时成立时，四档标志里至少三项为真（确实在抢）",
              sum(flags) >= 3, str(flags))
        check("★ 抢的时候只有一个赢家：游戏 > 观影 > 工作",
              unit_guard.active == KIND_GAME, unit_guard.active)

        focusing["on"] = True
        # 专注那一档没有信号源（`is_focusing` 是回调），所以要装配层喊一声才知道变了——
        # 生产里喊的就是这一句（见 `_sync_scene_guard`）。
        unit_guard.notify_focus_changed()
        check("★ 专注一开，其余全靠边（专注 > 游戏）",
              unit_guard.active == KIND_FOCUS and unit.state == KIND_FOCUS,
              f"{unit_guard.active} / {unit.state}")
        focusing["on"] = False
        unit_guard.notify_focus_changed()
        check("专注一走 → 立刻回到次高的那场（不是弹回 idle）",
              unit_guard.active == KIND_GAME and unit.state == KIND_GAME,
              f"{unit_guard.active} / {unit.state}")

        # ---- 全清：回空档 ----
        unit_guard.set_game("")
        unit_guard.set_watch(False)
        fake.foreground_proc = OTHER_PROC
        fake.foreground_changed.emit(OTHER_PROC)
        check("三场全散 → 回空档 idle", unit_guard.active == KIND_IDLE and unit.state == KIND_IDLE,
              f"{unit_guard.active} / {unit.state}")

        # ---- 工作场的点头 / 摇头（状态机这一层只记"这一场点过头没有"）----
        fake.foreground_proc = WORK_PROC
        fake.foreground_changed.emit(WORK_PROC)
        check("点头之前：这一场不整理笔记", unit.notes_allowed() is False, "")
        check("答复被工作场接下了", unit.answer("好呀") is True, "")
        check("★ 点头之后：这一场随时整理笔记", unit.notes_allowed() is True, "")
        fake.foreground_proc = OTHER_PROC
        fake.foreground_changed.emit(OTHER_PROC)
        check("换一场：上一场那次点头作废（按场算，不接着用）",
              unit.notes_allowed() is False, "")

        fake.foreground_proc = WORK_PROC
        fake.foreground_changed.emit(WORK_PROC)
        declined: list[str] = []
        unit.declined.connect(declined.append)
        check("摇头也被接下了", unit.answer("不用了") is True, "")
        check("★ 摇头 → 报一声「这一场先别陪着」（装配层据此收起来）",
              declined == [KIND_WORK], repr(declined))
        check("摇头不当成点头", unit.notes_allowed() is False, "")

        # ---------- ② 工作场：坐起 + 开场白 + 过一会儿问一句 ----------

        section("② 工作场：坐起来陪着、开场先说一句、过一会儿才问")

        words: list[str] = []
        app.bus.pet_speak_proactive.connect(words.append)
        said: list[str] = []
        app.bus.pet_speak.connect(said.append)

        _goto(monitor, holder, WORK_PROC)
        check("前置：这一场判成了工作", scene.state == KIND_WORK, scene.state)
        check("★ 宠物坐起来了（坐姿开心那张图 = Behavior.SIT）",
              scheduler.forced is Behavior.SIT and pet._behavior is Behavior.SIT,
              f"forced={scheduler.forced} pose={pet._behavior}")
        check("工作场开场飘了「我陪你一起工作」", OPENING_WORK in words, repr(words))
        check("开场这一句只显示不出声（走 pet_speak_proactive，不走出声那条）",
              OPENING_WORK not in said, repr(said))
        check("宠物没被藏起来（只是在陪着你）", pet.isVisible() is True, "")
        check("★ 问句不是当场就问的：还在等（开场白那条气泡没散）",
              scene._ask_timer.isActive() and bubble.offer_theme == "",
              f"timer={scene._ask_timer.isActive()} offer={bubble.offer_theme!r}")

        scene._ask_notes()          # = 等待到点（计时器 timeout 打的就是这个坑）
        pump(0.02)
        check("过一会儿 → 问出口「我可以帮你整理笔记，需要吗？」",
              ASK_NOTES in said, repr(said))
        check("★ 轻按钮跟着露出来，记的是工作这一档",
              bubble.offer_theme == "scene_work", repr(bubble.offer_theme))
        check("★ 按钮是真弹出来的：聊天框打开了、[好呀][不用了] 不再藏在关着的框里",
              bubble.isVisible() and not bubble._offer_row.isHidden(),
              f"visible={bubble.isVisible()} hidden={bubble._offer_row.isHidden()}")

        # ---- 点「不用了」：这一场把宠物收起来 ----
        # 走**真按钮**那一路（收起按钮 → 记下用户这句话 → 派发），不是直接叫派发口。
        bubble._reply_offer("不用了")
        pump(0.05)
        check("★ 点「不用了」→ 宠物收起来", pet.isVisible() is False, "")
        check("记的是『这一场先别陪着』，不是『用户自己藏了』",
              app._muted_kind == KIND_WORK and app._pet_user_hidden is False,
              f"muted={app._muted_kind!r} user_hidden={app._pet_user_hidden}")
        check("问句那份等待态也清了", scene.awaiting is False, "")

        # ---- 换个场次：上一场那句「不用了」到此为止 ----
        _goto(monitor, holder, GAME_PROC)
        pump(0.05)
        check("★ 换一场 → 上一场的「不用了」解除、宠物回来了",
              app._muted_kind == "" and pet.isVisible() is True,
              f"muted={app._muted_kind!r} visible={pet.isVisible()}")

        # ---- 点头那条路 ----
        _goto(monitor, holder, WORK_PROC)
        pump(0.05)
        scene._ask_notes()
        pump(0.02)
        bubble._reply_offer("好呀")
        pump(0.05)
        check("★ 点「好呀」→ 这一场随时整理笔记",
              scene.accepted is True and scene.notes_allowed() is True,
              f"accepted={scene.accepted} allowed={scene.notes_allowed()}")
        check("点头之后轻按钮收起来（问句已经答完）",
              bubble.offer_theme == "", repr(bubble.offer_theme))

        # ---------- ③ 游戏场：开场白 + 过一会儿问攻略 + 点否隐身 ----------

        section("③ 游戏场：坐起来、开场说一句、过一会儿再问攻略")

        _goto(monitor, holder, OTHER_PROC)      # 先离开工作页，让这一场干净
        pump(0.05)
        guide = app.game_guide
        words.clear()
        offers.clear()
        app.bus.game_offer.connect(offers.append)

        _goto(monitor, holder, GAME_PROC)
        pump(0.05)
        check("前置：这一场判成了游戏", scene.state == KIND_GAME, scene.state)
        check("★ 宠物坐起来了", scheduler.forced is Behavior.SIT, str(scheduler.forced))
        check("游戏场开场飘了「我陪你一起玩游戏」", OPENING_GAME in words, repr(words))
        check("★ 攻略问句**不当场**问：「过一会儿」这一刻它还在等",
              offers == [] and guide.awaiting_confirm is False, repr(offers))
        check("等待排上了（记着是哪个游戏，等着到点再问）",
              guide._pending != "" and guide._ask_timer.isActive(),
              f"pending={guide._pending!r} timer={guide._ask_timer.isActive()}")

        guide._on_ask_due()                     # = 等一下到点了
        pump(0.05)
        check("★ 到点 → 攻略问出口了", len(offers) == 1, f"{len(offers)} 次")
        check("问句里带的是游戏名", offers == ["英雄联盟"], repr(offers))
        check("轻按钮跟着露出来（游戏那一档）",
              bubble.offer_theme == "game", repr(bubble.offer_theme))
        check("★ 游戏场按钮也是真弹出来的（聊天框开着）",
              bubble.isVisible() and not bubble._offer_row.isHidden(),
              f"visible={bubble.isVisible()} hidden={bubble._offer_row.isHidden()}")

        bubble._reply_offer("不用了")
        pump(0.05)
        check("★ 游戏场点「不用了」→ 宠物收起来", pet.isVisible() is False, "")
        check("记的是游戏这一场", app._muted_kind == KIND_GAME, repr(app._muted_kind))

        # ---- 等待期间切走了：这一问作废（不然切回来会莫名其妙冒出一句）----
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        offers.clear()
        _goto(monitor, holder, GAME_PROC)
        pump(0.05)
        check("前置：又重新排上了等待", guide._ask_timer.isActive(), "")
        _goto(monitor, holder, OTHER_PROC)      # 还没到点就切走了
        pump(0.05)
        check("★ 还没到点就切走 → 这一问当场作废（不挂着等它到点）",
              guide._pending == "" and not guide._ask_timer.isActive(),
              f"pending={guide._pending!r} timer={guide._ask_timer.isActive()}")
        guide._on_ask_due()
        pump(0.05)
        check("到点那一下再复核一次 → 也不冒（两条路都不漏）", offers == [], repr(offers))

        # ---------- ④ 电影场：坐起来陪着，开场飘「我陪你看电影」 ----------

        section("④ 电影场：坐起来陪着，开场飘「我陪你看电影」（不再复用看片线）")

        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        words.clear()
        captions: list[str] = []
        app.bus.screen_caption.connect(captions.append)
        _goto(monitor, holder, PLAYER_PROC)
        pump(0.05)
        check("前置：看片会话开着（这一场是观影）",
              companion._watch_open is True and scene.state == KIND_WATCH,
              f"watch={companion._watch_open} scene={scene.state}")
        check("★ 电影场也坐起来陪着", scheduler.forced is Behavior.SIT, str(scheduler.forced))
        check("★ 电影场开场飘了「我陪你看电影」（v2-scene-state-fix 原话）",
              OPENING_WATCH in words, repr(words))
        # 正向对照：看片那条线自己的开场陪伴字幕照旧（那句「主人，看电影啦」是另一条
        # 线的活，跟这句不是同一句——正是这次返工要分开的两件事）。
        # 把"剧情已经露头就把它往后挪"那一手临时关掉——那一手是 v2-mood-horror 的事，
        # 有它自己的测试；这一节只验"这句还在不在"。
        real_pending = companion._scene_pending
        companion._scene_pending = lambda: ""
        try:
            companion._show_opening()
        finally:
            companion._scene_pending = real_pending
        pump(0.02)
        check("正向对照：看片那条线的开场字幕照旧冒（不是把开场白整个砍了）",
              captions == [OPENING_LINE], repr(captions))

        # 随情绪的飘词沿用 mood 那条线（v2-mood-window 之后的口径不变）
        words.clear()
        companion.on_scene(SCENE_HORROR)
        pump(0.05)
        check("★ 剧情认出来了 → 情绪词照旧飘（沿用 mood 那条线）",
              words == [SCENE_LINES[SCENE_HORROR][0]], repr(words))

        # ---------- ⑤ 悠闲场：三场都不成立 → 自由 ----------

        section("⑤ 悠闲场：三场都不在 → 交回悠闲调度（不再钉着坐）")

        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        check("前置：这一场回空档", scene.state == KIND_IDLE, scene.state)
        check("★ 强制姿态解除了（坐起是这三场的样子，不是永久状态）",
              scheduler.forced is None, str(scheduler.forced))

        # ---------- ⑥ 切回专注：头顶的情绪词立刻收掉，且不复发 ----------

        section("⑥ 切回番茄专注：头顶的情绪词立刻收掉、且不复发（真机那个 bug）")

        app.config.screen.focus_enabled = True     # 这一节要的正是自动番茄钟
        focus.reset()
        # 先把"头上挂着情绪词"这个现场摆出来：看片 → 认出恐怖 → 怕怕飘上去
        _goto(monitor, holder, PLAYER_PROC)
        pump(0.05)
        companion._last_spoken.clear()      # 清冷却：上一节已经冒过恐怖，别再被同态挡掉
        companion._cursor.clear()           # 轮换指针也归零，好让这一拍还是从第一句「我怕怕…」开始
        companion.on_scene(SCENE_HORROR)
        pump(0.05)
        check("前置：头顶正飘着状态词（气泡可见）",
              pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_HORROR][0],
              repr(pet._bubble._label.text()))

        _goto(monitor, holder, WORK_PROC)          # 切回工作页 = 自动番茄钟起来
        pump(0.05)
        check("前置：专注真跑起来了（这会儿 _is_focusing 为真）",
              focus.running is True and app._is_focusing() is True,
              f"running={focus.running} focusing={app._is_focusing()}")
        check("★ 切回专注 → 头顶那条情绪词**立刻**收掉（不等它自己 60 秒）",
              pet._bubble.isVisible() is False, "")
        check("专注这一场由仲裁点说了算（工作让位给专注）",
              scene.state == KIND_FOCUS, scene.state)
        # 口径反转（v2-pose-sync）：专注**也**锁坐姿。原先这里是"专注放掉姿态"，
        # 真机上就成了"番茄钟跑了五分钟它还趴着睡 + 冒 zzz"——`set_quiet` 只缩小可挑
        # 的池子，而打盹本身就是安静动作，照样挑得到。
        check("专注也锁坐姿（安静陪着 = 坐起，不是趴回去睡）",
              scheduler.forced is Behavior.SIT, str(scheduler.forced))
        check("看片会话也收掉了", companion._watch_open is False, "")

        words.clear()
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("★ 不复发：专注期间剧情反应一个字都不冒",
              words == [] and pet._bubble.isVisible() is False, repr(words))

        # 反向对照：退出专注后同一个场景照常冒 —— 证明刚才是"专注挡住了"，不是线断了
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        focus.reset()
        pump(0.05)
        companion._last_spoken.clear()
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("反向对照：退出专注 → 同一个场景照常冒（静默没把线关死）",
              len(words) == 1 and words[0] == SCENE_LINES[SCENE_ANGRY][0], repr(words))

        # ---- 直接回归：从工作页切走的**那一拍**不许闪出工作场 ----
        # 前台换页与番茄钟退出落在同一拍里（同一条前台信号派发的半途）。那一刻仲裁点
        # 要是拿到"上一拍还在工作页"这个陈旧结论，就会先冒一句「我陪你一起工作」再回神
        # ——修前在真机上看到的就是这个。工作那一档改成**回调现算**就是为这一条。
        app.config.screen.focus_enabled = True
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        _goto(monitor, holder, WORK_PROC)
        pump(0.05)
        check("前置：工作页上番茄钟跑着 → 这一场算专注",
              focus.running is True and scene.state == KIND_FOCUS,
              f"running={focus.running} scene={scene.state}")
        words.clear()
        _goto(monitor, holder, OTHER_PROC)     # 一拍之内：前台换页 + 番茄钟退出
        pump(0.05)
        check("★ 从工作页切走的那一拍**不闪出工作场**（修前这里会冒一句「我陪你一起工作」）",
              not any(OPENING_WORK in line for line in words), repr(words))
        check("切完这一场回到空档（没被那一瞬的错觉钉住）",
              scene.state == KIND_IDLE, scene.state)

        # ---- 优先级后果：工作页 + 番茄钟跑着时，算的是**专注**，不是工作场 ----
        app.config.screen.focus_enabled = True
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        _goto(monitor, holder, WORK_PROC)
        pump(0.05)
        check("★ 工作页上番茄钟也开着时，仲裁点算的是**专注**（专注 > 工作，用户选定的优先级）",
              scene.state == KIND_FOCUS,
              f"{scene.state}（工作场只在番茄钟没在跑时才成立）")
        focus.reset()
        app.config.screen.focus_enabled = False
        pump(0.05)

        # ---------- ⑦ 全程锁坐姿：打断/静音/暂停都不放，切走/进专注才放 ----------

        section("⑦ 全程锁坐姿：打断/静音/暂停都不放，切走/进专注才放")

        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        _goto(monitor, holder, WORK_PROC)
        pump(0.05)
        check("前置：这一场是工作、强制坐起",
              scene.state == KIND_WORK and scheduler.forced is Behavior.SIT,
              f"{scene.state} / {scheduler.forced}")

        scheduler.interrupt()
        pump(0.02)
        check("★ 打断（碰一下）不解除坐起", scheduler.forced is Behavior.SIT, str(scheduler.forced))

        scheduler.set_quiet(True)
        pump(0.02)
        check("★ 静音不解除坐起", scheduler.forced is Behavior.SIT, str(scheduler.forced))
        scheduler.set_quiet(False)
        pump(0.02)

        scheduler.set_paused(True)
        scheduler.set_paused(False)
        pump(0.02)
        check("★ 暂停再恢复不解除坐起", scheduler.forced is Behavior.SIT, str(scheduler.forced))

        # 场景微变：换一个工作页（还是工作那一档），不触发解除
        _goto(monitor, holder, "code.exe")
        pump(0.05)
        check("★ 场景微变（换个工作页）不解除坐起",
              scheduler.forced is Behavior.SIT and scene.state == KIND_WORK,
              f"{scene.state} / {scheduler.forced}")

        # 切走（回空档）→ 放
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        check("★ 切走（回悠闲）→ 解除坐起", scheduler.forced is None, str(scheduler.forced))

        # 进更高优先态（专注）→ 放
        _goto(monitor, holder, WORK_PROC)
        pump(0.05)
        check("前置：又坐起来了", scheduler.forced is Behavior.SIT, str(scheduler.forced))
        focus.start_manual()     # 手动专注：不等前台判定，直接起一轮，仲裁点据此放掉坐起
        pump(0.05)
        check("前置：专注真跑起来了（_is_focusing 为真）",
              focus.running is True and app._is_focusing() is True,
              f"running={focus.running} focusing={app._is_focusing()}")
        # 口径反转（v2-pose-sync）：进专注不再"解除坐起"，而是**换成专注这一档继续坐**。
        check("★ 进更高优先态（专注）→ 照样坐着（不趴回去）",
              scheduler.forced is Behavior.SIT, str(scheduler.forced))
        focus.reset()
        pump(0.05)

        # ---------- ⑧ 电影情绪词：怒/笑常驻，恐惧只当轻反应 ----------

        section("⑧ 电影情绪词：怒/笑常驻头顶，恐惧只当轻反应（v2-mood-fear-tune）")

        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        app.config.screen.focus_enabled = False     # 别让专注这条线插一脚
        focus.reset()
        pump(0.05)
        words.clear()

        _goto(monitor, holder, PLAYER_PROC)
        pump(0.05)
        companion._last_spoken.clear()              # 清冷却，确保这一拍开口
        companion._cursor.clear()                   # 轮换指针归零，从头那句开始
        monitor._scene = SCENE_ANGRY                # 怒 = 强信号：走常驻那条路
        companion.on_scene(SCENE_ANGRY)
        pump(0.05)
        check("前置：电影场飘上了愤怒情绪词（气泡可见）",
              pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_ANGRY][0],
              repr(pet._bubble._label.text()))
        check("★ 怒情绪词常驻：自动淡出计时器没在跑（不再 60 秒秒退）",
              pet._bubble.isVisible() and not pet._bubble._hide_timer.isActive(),
              f"visible={pet._bubble.isVisible()} timer={pet._bubble._hide_timer.isActive()}")
        pump(0.3)
        check("★ 过一小会儿还挂着（不是拉长计时，是压根不排自动淡出）",
              pet._bubble.isVisible() and not pet._bubble._hide_timer.isActive(),
              f"visible={pet._bubble.isVisible()} timer={pet._bubble._hide_timer.isActive()}")

        # 情绪变（愤怒 → 笑容）→ 换词
        companion._last_spoken.clear()
        companion._cursor.clear()
        monitor._scene = SCENE_FESTIVE
        companion.on_scene(SCENE_FESTIVE)
        pump(0.05)
        check("★ 情绪变了 → 换词（不是叠着两句）",
              pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_FESTIVE][0],
              repr(pet._bubble._label.text()))
        check("换词后照样常驻（不秒退）",
              pet._bubble.isVisible() and not pet._bubble._hide_timer.isActive(),
              f"visible={pet._bubble.isVisible()} timer={pet._bubble._hide_timer.isActive()}")

        # 恐惧 → 轻反应：不常驻，随默认时长淡出（v2-mood-fear-tune）
        companion._last_spoken.clear()
        companion._cursor.clear()
        monitor._scene = SCENE_HORROR
        companion.on_scene(SCENE_HORROR)
        pump(0.05)
        check("★ 恐惧只当轻反应：气泡还飘「我怕怕…」",
              pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_HORROR][0],
              repr(pet._bubble._label.text()))
        check("★ 恐惧不常驻：自动淡出计时器跑起来了（跟怒/笑不一样）",
              pet._bubble.isVisible() and pet._bubble._hide_timer.isActive(),
              f"visible={pet._bubble.isVisible()} timer={pet._bubble._hide_timer.isActive()}")

        # 散场（切走）→ 收
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        check("★ 散场（切走）→ 情绪词收掉",
              pet._bubble.isVisible() is False, "")

        # ---------- ⑨ 金字塔笔记：结论先行 + 分层论据 ----------

        section("⑨ 金字塔笔记：结论先行 + 分层论据（v2-scene-state-fix ⑤）")

        from life.screen.notes import _parse_points

        flat = _parse_points(["要点甲", "要点乙"])
        check("旧口径：平铺字符串列表 → 每条成一要点、无细分论据",
              [p["point"] for p in flat] == ["要点甲", "要点乙"]
              and all(p["details"] == [] for p in flat), repr(flat))

        pyr = _parse_points([
            {"point": "顶层结论 A", "details": ["论据 A1", "论据 A2"]},
            {"point": "要点 B", "details": []},
        ])
        check("新口径：对象列表 → 结论带分层论据",
              [p["point"] for p in pyr] == ["顶层结论 A", "要点 B"]
              and pyr[0]["details"] == ["论据 A1", "论据 A2"], repr(pyr))

        mixed = _parse_points(["平铺一条", {"point": "对象一条", "details": ["论据 X"]}])
        check("混着吐：串串和对象各按各的收",
              [p["point"] for p in mixed] == ["平铺一条", "对象一条"]
              and mixed[1]["details"] == ["论据 X"], repr(mixed))

        svc = app.screen_notes
        block = svc._render("这一课", "先记结论", pyr, ["待办1"], ["疑问1"])
        lines = block.splitlines()
        check("★ 顶层结论在要点之前（结论先行）",
              lines.index("**结论**：先记结论") < lines.index("**要点**"),
              "\n".join(lines))
        check("★ 分层论据：要点下面缩进挂论据",
              lines[lines.index("- 顶层结论 A") + 1] == "  - 论据 A1",
              "\n".join(lines))
        check("待办/疑问按自己的段收尾",
              "**待办**" in lines and "- 待办1" in lines and "**疑问**" in lines,
              "\n".join(lines))

        # ---------- ⑪ 别在别人等答复时插嘴 ----------

        section("⑪ 社交建卡询问最高优先：工作态问句让位（v2-social-reply-fix）")
        # 候选列完、正等「要不要建对象攻略卡」的答复时，工作态那句「整理笔记」不能冒，
        # 不然会把 social_card 那排按钮顶掉。这里用 can_offer 那道闸挡它。
        blocked = SceneStateService(
            config=app.config, bus=bus, guard=unit_guard,
            ask_delay_ms=0, can_offer=lambda: False,
        )
        blocked_offers: list[str] = []
        bus.scene_offer.connect(blocked_offers.append)
        blocked._kind = KIND_WORK
        blocked._awaiting = False
        blocked._accepted = False
        blocked._ask_notes()
        check("别人正等答复时，工作态问句不冒", not blocked_offers,
              f"offers={blocked_offers}")
        bus.scene_offer.disconnect(blocked_offers.append)

        # ---------- ⑫ 姿态 + 气泡强制同步（v2-pose-sync） ----------

        section("⑫ 正在打盹时进场景态：立刻坐起 + zzz 跟着收掉（v2-pose-sync）")
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        app.chat_bubble.close_bubble(fade=False)
        pet.hide_speech_bubble()
        pet.show()
        pump(0.02)

        pet.play_behavior(Behavior.SLEEP, 30_000)     # 真机那个现场：趴着睡
        pump(0.02)
        check("前置：趴着睡、头顶挂着 zzz",
              pet._behavior is Behavior.SLEEP
              and pet._current_pose() == POSE_SLEEP
              and pet._bubble._label.text() == ZZZ_TEXT,
              f"pose={pet._current_pose()} bubble={pet._bubble._label.text()!r}")

        focus.start_manual()                          # 进专注（真机那次就是这么来的）
        pump(0.05)
        check("★ 进专注 → 立刻坐起（睡觉那个动作被停掉，不是留着演完）",
              scheduler.forced is Behavior.SIT and pet._behavior is Behavior.SIT,
              f"forced={scheduler.forced} behavior={pet._behavior}")
        check("★ 立绘就是坐姿那一张（姿态单例：不是还趴着）",
              pet._current_pose() == POSE_IDLE, pet._current_pose())
        check("★ zzz 跟着收掉（睡觉都结束了，气泡不该还挂着）",
              pet._bubble.isVisible() is False, f"可见={pet._bubble.isVisible()}")
        focus.reset()
        pump(0.05)

        section("⑬ 姿态 + 气泡单例：切态先清旧再上新，不留腾空残影")
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        pet.play_behavior(Behavior.HOP, 3_000)        # 蹦到一半
        pump(0.02)
        check("前置：蹦起来了（这会儿算腾空）", pet._settled is False, str(pet._settled))

        _goto(monitor, holder, WORK_PROC)             # 蹦的过程中切进工作场
        pump(0.05)
        check("★ 当场落地（腾空那笔账结掉，不再悬着）",
              pet._settled is True, str(pet._settled))
        check("★ 姿态是坐姿那一张（腾空不再压着场景态强制姿态）",
              pet._current_pose() == POSE_IDLE, pet._current_pose())
        check("★ 只有一个姿态：强制姿态说了算",
              scheduler.forced is Behavior.SIT and pet._behavior is Behavior.SIT,
              f"forced={scheduler.forced} behavior={pet._behavior}")

        # 腾空/状态两路都想抢姿态时，强制姿态照样赢（真机"切态偶现两个姿势"的机制）
        pet._settled = False                          # 硬摆出"腾空中"
        check("★ 腾空中也还是坐姿（airborne 抢不走强制姿态）",
              pet._current_pose() == POSE_IDLE, pet._current_pose())
        pet._settled = True

        # 反向对照：切走（回悠闲）之后才放回自由调度
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        check("★ 切走（回悠闲）才解除强制姿态",
              scheduler.forced is None, str(scheduler.forced))

        # ---------- ⑩ 红线 ----------

        section("⑩ 红线：两库零写入、零新依赖")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        _goto(monitor, holder, WORK_PROC)
        scene._ask_notes()
        app._on_offer_reply("好呀")
        pump(0.05)
        _goto(monitor, holder, GAME_PROC)
        pump(0.05)
        _goto(monitor, holder, PLAYER_PROC)
        companion.on_scene(SCENE_HORROR)
        pump(0.2)
        drain_threads()
        check("pet.db 的 utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              app.mind_store.fragment_count() == mind_before, "")

        refs = _imports_of("life/screen/scene_state.py")
        check("scene_state.py 只 import 自家模块 + PySide6 + 标准库",
              not (refs & {"requests", "numpy", "cv2", "PIL", "openai", "torch"}),
              "、".join(sorted(refs & {"requests", "numpy", "cv2", "PIL", "openai", "torch"})) or "（一处都没有）")

        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
    finally:
        drain_threads()
        box.cleanup()

    return summary("场景状态机：三态互斥 + 强制坐起 + 开场白/问句 + 专注清残留（v2-scene-state）")


def _imports_of(rel: str) -> set[str]:
    """某个模块 import 了哪些顶层包（按 AST 取，不靠正则）。"""
    import ast

    path = Path(__file__).resolve().parents[1] / rel
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


if __name__ == "__main__":
    raise SystemExit(main())
