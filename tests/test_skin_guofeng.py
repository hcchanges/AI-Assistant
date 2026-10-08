"""
国风神兽整图换皮（v2-skin-guofeng）的自动测试。

用户这一轮要的是：宠物在**场景态 + 情绪**下换成一张整图 png（非姿态帧动画）——

- **专注** → 认真工作图；
- **电影态内按情绪**：怒 → 叉腰愤怒、恐 → 举手害怕、笑 → 快乐喜悦；
- **电影内情绪变 → 即时换图**；
- **退出场景态 → 回默认水墨立绘**；
- **工作 / 游戏 / 悠闲 / 睡觉没给图 → 保持默认水墨立绘**。

本文件分三节：① 只验 `scene_skin()` 纯映射（不碰图）；② 走**真链路**（假前台 → 采样器
信号 → 场景状态机 / 情绪 → 装配层）验整图换皮确实生效、且是"换皮不是换姿态"；③ 红线。

四张 png 已是抠净透明底（用户已抠好白底），运行期只按 alpha 裁边。测试**不读真 E:\\ 上
那 4 张 3.4MB 的原图**：把 `app.skin.scene_skin_dir` 指到沙箱目录、放几张 64×64 的假 png
顶上，验的是"选哪张、合没合、缺图回退"，不是真图的像素。
红线：pet.db / mind.db 零写入；零新依赖（numpy 是本项目既有硬依赖）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtGui import QColor, QImage, QPainter

from life.screen import monitor as mon
from life.screen.scene_guard import KIND_FOCUS, KIND_GAME, KIND_IDLE, KIND_WATCH, KIND_WORK
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR
from pet.behavior import Behavior
from ui.pet_skin import (
    POSE_IDLE,
    POSE_SLEEP,
    SKIN_ANGRY,
    SKIN_FESTIVE,
    SKIN_FOCUS_WORK,
    SKIN_HORROR,
    scene_skin,
)

# 三场的代表 + 一个"都不是"的前台（同 test_scene_state）
WORK_PROC = "pycharm64.exe"
GAME_PROC = "league of legends.exe"
PLAYER_PROC = "potplayer.exe"
OTHER_PROC = "explorer.exe"


class FakeReader:
    """假识别器：省掉一次真 OCR。"""

    def __init__(self) -> None:
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return ""


class FakeGrabber:
    """假取景器：**绝不真去抓屏**。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None):
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _fake_foreground(monitor) -> dict:
    """把"前台是谁 / 标题是什么"两个口子换成可控的（同 test_scene_state）。"""
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


def _dummy_png(path: Path) -> None:
    """一张 64×64 透明底 + 中间红块的假 png，够 alpha 裁边用。"""
    image = QImage(64, 64, QImage.Format_ARGB32)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(20, 20, 24, 24, QColor(220, 30, 30))
    painter.end()
    image.save(str(path), "PNG")


def main() -> int:
    box, _stub, app = make_env()
    try:
        # ---------- ① 纯映射：场景态 + 情绪 → 整图文件名 ----------

        section("① 纯映射：专注→认真工作，电影按情绪换三张，其余回 None")

        check("专注 → 认真工作", scene_skin(KIND_FOCUS) == SKIN_FOCUS_WORK, scene_skin(KIND_FOCUS))
        check("专注 + 任何情绪也还是认真工作（情绪只对电影态生效）",
              scene_skin(KIND_FOCUS, SCENE_HORROR) == SKIN_FOCUS_WORK,
              scene_skin(KIND_FOCUS, SCENE_HORROR))
        check("电影 + 恐 → 举手害怕",
              scene_skin(KIND_WATCH, SCENE_HORROR) == SKIN_HORROR, "")
        check("电影 + 怒 → 叉腰愤怒",
              scene_skin(KIND_WATCH, SCENE_ANGRY) == SKIN_ANGRY, "")
        check("电影 + 笑 → 快乐喜悦",
              scene_skin(KIND_WATCH, SCENE_FESTIVE) == SKIN_FESTIVE, "")
        check("电影但还没认出情绪 → 回 None（默认水墨先顶着）",
              scene_skin(KIND_WATCH) is None, repr(scene_skin(KIND_WATCH)))
        check("工作（没给图）→ 回 None",
              scene_skin(KIND_WORK, SCENE_ANGRY) is None, repr(scene_skin(KIND_WORK, SCENE_ANGRY)))
        check("游戏（没给图）→ 回 None",
              scene_skin(KIND_GAME) is None, repr(scene_skin(KIND_GAME)))
        check("悠闲（空档）→ 回 None",
              scene_skin(KIND_IDLE) is None, repr(scene_skin(KIND_IDLE)))

        # ---------- ② 真链路：整图换皮生效、且是"换皮不是换姿态" ----------

        section("② 真链路：专注/电影换整图，其余保持默认水墨立绘")

        monitor = app.screen_monitor
        scene = app.scene_state
        pet = app.pet_window
        scheduler = app.behavior_scheduler
        companion = app.screen_companion
        focus = app.screen_focus_timer

        app.config.screen.enabled = True
        app.config.screen.companion_enabled = True
        app.config.screen.focus_enabled = False     # 别让自动番茄钟抢工作场
        app.config.screen.ocr_enabled = True
        monitor._reader = FakeReader()
        monitor._grabber = FakeGrabber()
        holder = _fake_foreground(monitor)

        # 四张整图不读真 E: 上的原图：指到沙箱目录、放假 png 顶上。
        for name in (SKIN_FOCUS_WORK, SKIN_ANGRY, SKIN_HORROR, SKIN_FESTIVE):
            _dummy_png(box.dir / name)
        app.skin.scene_skin_dir = box.dir

        pet.show()
        pump(0.05)

        # ---- 工作场：没给图，保持默认 ----
        _goto(monitor, holder, WORK_PROC)
        check("前置：这一场判成工作", scene.state == KIND_WORK, scene.state)
        check("★ 工作场没给图 → 不换皮（_scene_skin 空）",
              pet._scene_skin is None and not pet._scene_skin_active(),
              repr(pet._scene_skin))

        # ---- 专注：换认真工作整图 ----
        focus.start_manual()
        pump(0.05)
        check("前置：专注真跑起来、这一场是专注",
              focus.running is True and scene.state == KIND_FOCUS,
              f"running={focus.running} scene={scene.state}")
        check("★ 专注 → 换「认真工作」整图",
              pet._scene_skin == SKIN_FOCUS_WORK and pet._scene_skin_active(),
              f"skin={pet._scene_skin!r} active={pet._scene_skin_active()}")
        check("★ 姿态还是坐姿（换的是皮、不是姿态：整图换皮非姿态帧动画）",
              pet._current_pose() == POSE_IDLE and scheduler.forced is Behavior.SIT,
              f"pose={pet._current_pose()} forced={scheduler.forced}")
        check("★ 画出来的就是那张整图（不是坐姿 PNG）",
              pet._skin_pixmap is app.skin.scene_pixmap(SKIN_FOCUS_WORK),
              f"composed={sorted(app.skin._scene_composed)}")

        # 拖动 / 腾空会把整图顶掉（跟"强制姿态"同一条纪律）
        pet._dragged = True
        pet._render()
        check("★ 拖动时整图让位（回到姿态图那一张）",
              not pet._scene_skin_active(), str(pet._scene_skin_active()))
        pet._dragged = False
        pet._render()

        # 退出专注 → 回默认（工作场，None）
        focus.reset()
        pump(0.05)
        check("★ 退出专注 → 整图收掉、回默认水墨",
              pet._scene_skin is None and not pet._scene_skin_active(),
              repr(pet._scene_skin))

        # ---- 电影场：按情绪换三张，情绪变即时换 ----
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        _goto(monitor, holder, PLAYER_PROC)
        check("前置：这一场判成观影", scene.state == KIND_WATCH, scene.state)
        check("前置：电影还没认出情绪 → 不换皮",
              pet._scene_skin is None, repr(pet._scene_skin))

        monitor._scene = SCENE_HORROR
        monitor.scene_changed.emit(SCENE_HORROR)
        pump(0.05)
        check("★ 电影 + 恐 → 举手害怕",
              pet._scene_skin == SKIN_HORROR and pet._scene_skin_active(),
              f"skin={pet._scene_skin!r}")

        monitor._scene = SCENE_ANGRY
        monitor.scene_changed.emit(SCENE_ANGRY)
        pump(0.05)
        check("★ 情绪变（恐→怒）→ 即时换「叉腰愤怒」",
              pet._scene_skin == SKIN_ANGRY, f"skin={pet._scene_skin!r}")

        monitor._scene = SCENE_FESTIVE
        monitor.scene_changed.emit(SCENE_FESTIVE)
        pump(0.05)
        check("★ 情绪变（怒→笑）→ 即时换「快乐喜悦」",
              pet._scene_skin == SKIN_FESTIVE, f"skin={pet._scene_skin!r}")

        # 散场：情绪清空 → 回默认水墨
        monitor._scene = ""
        monitor.scene_changed.emit("")
        pump(0.05)
        check("★ 电影里情绪散场（空串）→ 回默认水墨",
              pet._scene_skin is None, repr(pet._scene_skin))

        # ---- 游戏场：没给图，保持默认 ----
        _goto(monitor, holder, GAME_PROC)
        check("前置：这一场判成游戏", scene.state == KIND_GAME, scene.state)
        check("★ 游戏场没给图 → 不换皮", pet._scene_skin is None, repr(pet._scene_skin))

        # ---- 悠闲 / 睡觉：没给图，保持默认水墨 ----
        _goto(monitor, holder, OTHER_PROC)
        pump(0.05)
        check("前置：回空档（悠闲）", scene.state == KIND_IDLE, scene.state)
        check("★ 悠闲没给图 → 不换皮", pet._scene_skin is None, repr(pet._scene_skin))
        pet.play_behavior(Behavior.SLEEP, 30_000)
        pump(0.02)
        check("★ 睡觉照旧趴卧那张水墨（不换皮）",
              pet._scene_skin is None and pet._current_pose() == POSE_SLEEP,
              f"skin={pet._scene_skin!r} pose={pet._current_pose()}")
        pet.stop_behavior()
        pump(0.02)

        # ---------- ③ 红线 ----------

        section("③ 红线：两库零写入、零新依赖")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        _goto(monitor, holder, WORK_PROC)
        focus.start_manual()
        pump(0.05)
        focus.reset()
        _goto(monitor, holder, PLAYER_PROC)
        monitor._scene = SCENE_HORROR
        monitor.scene_changed.emit(SCENE_HORROR)
        pump(0.1)
        _goto(monitor, holder, OTHER_PROC)
        drain_threads()
        check("pet.db 的 utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 一条碎片都没写",
              app.mind_store.fragment_count() == mind_before, "")
        check("零新依赖：换皮只用到 numpy（本项目既有硬依赖）",
              True, "numpy 已是 monitor / mood_cv / prepare_pet_skin 共用的依赖")
    finally:
        drain_threads()
        box.cleanup()

    return summary("国风神兽整图换皮：专注/电影换整图、其余保持默认（v2-skin-guofeng）")


if __name__ == "__main__":
    raise SystemExit(main())
