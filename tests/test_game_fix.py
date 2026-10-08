"""
游戏态彻底识别（v2-game-fix / v2-game-activate）：认不出名字的游戏也认得出。

真机上"打游戏仍被弹「主人，看电影啦」"这一条，根子在判"在不在看片"那条链的最后两档：
**进程名不认识 → 窗口又铺满了屏 → 就当在看片**。

**那一节的口径在 v2-social-scene-boundary 之后变了**，这个文件的落点跟着搬：

- **反向那条线（"算不算在看片"）不再问游戏线索了**，改成只认名单（非播放器且非浏览器
  一律不算）——全屏兜底被用户拍板删掉。所以下面 ④ 段原来是"两条新线索否掉在看片"，
  现在是"**这两条线索在正向那条线上照样认得出游戏**"。
- 线索本身一条没少，只是只剩**正向**（`GameGuideService`："要不要进陪玩、弹攻略"）
  这一个入口。那件事上漏掉认不出名字的全屏游戏是**真损失**，所以五条线索一条不省。

这个文件现在验三件事：

1. **游戏窗口类名**（`life/screen/game.py` 的 `GAME_WINDOW_CLASSES`）——引擎给自家窗口
   注册的固定常量（`UnityWndClass` / `UnrealWindow` / `Valve001`…）。UE/Unity 游戏
   没有统一的进程名（各随项目命名，补不完），但有统一的类名。
2. **画面运动特征**（`life/screen/motion.py`）——连抢 4 帧看画面"怎么动"：电影镜头
   整幅平滑运镜，一个整体位移就解释得干干净净；游戏里 HUD 钉死不动、角色乱跑，
   没有任何整体位移解释得了。
3. 这两条**接进判定链**之后，行为跟没接之前逐条对齐（认得出的照旧认、硬闸照旧拦、
   取不到证据一律退回"没这条线索"）。

刻意的**只否不给**：这两条只会说"像游戏"，说不出"像电影"；拿不到证据（帧取不到、
numpy 不在、判不准）一律返回"没这条线索"，**行为退回加它们之前**。

每组都给**对照**（认得出的播放器/浏览器照旧认、非视频硬闸照旧拦、取不到证据时照旧
退回），免得断言变成恒真的。红线：pet.db / mind.db 一行不多；零新依赖。
"""

from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
)

from life.screen import monitor as mon
from life.screen import motion
from life.screen import game as game_mod
from monitor import foreground as fg_mod
from life.screen.game import (
    GAME_PROCS,
    GAME_WINDOW_CLASSES,
    game_of,
    is_game_window_class,
    is_launcher,
)

# 认不出的"游戏"进程名：故意是个表里没有的名字，用来验第二、三条线索
UNKNOWN_PROC = "some-ue-title-win64-shipping.exe"
PLAYER_PROC = "potplayer.exe"
BROWSER_PROC = "chrome.exe"

W, H = 960, 540


# ---------- 合成连拍素材 ----------
#
# **刻意用合成画面，不读用户机器上的照片**：测试要的是"判据这条链通不通"，不要
# 依赖某台机器上恰好有什么图。合成出来的两类在判据上分得干净（下面各自有断言）。

def _texture(seed: int = 5):
    """一张有细节的底图（画满随机色块）。有细节才量得出运动。"""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor, QImage, QPainter

    rnd = random.Random(seed)
    image = QImage(W, H, QImage.Format_RGB32)
    image.fill(QColor(30, 30, 40))
    painter = QPainter(image)
    painter.setPen(Qt.NoPen)
    for _ in range(1400):
        painter.setBrush(
            QColor(rnd.randint(0, 255), rnd.randint(0, 255), rnd.randint(0, 255))
        )
        painter.drawRect(
            rnd.randint(0, W), rnd.randint(0, H), rnd.randint(6, 70), rnd.randint(6, 70)
        )
    painter.end()
    return image


def _movie_frames(base, step: int = 24, n: int = 4):
    """电影式：整幅**平滑平移**，一个整体位移就解释得干干净净。"""
    from PySide6.QtCore import QRect
    from PySide6.QtGui import QImage, QPainter

    wide = QImage(W + step * n, H, QImage.Format_RGB32)
    painter = QPainter(wide)
    painter.drawImage(0, 0, base)
    painter.drawImage(W, 0, base)      # 右边接一圈，免得平移露白边
    painter.end()
    return [wide.copy(QRect(i * step, 0, W, H)) for i in range(n)]


def _game_frames(base, amp: int = 12, n: int = 4, tile: int = 48, seed: int = 1):
    """游戏式：画面打成小块**各动各的**，外加一条钉死不动的 HUD。"""
    from PySide6.QtGui import QColor, QImage, QPainter

    rnd = random.Random(seed)
    out, current = [], base
    for _ in range(n):
        image = QImage(W, H, QImage.Format_RGB32)
        painter = QPainter(image)
        for y in range(0, H, tile):
            for x in range(0, W, tile):
                painter.drawImage(
                    x, y, current,
                    x + rnd.randint(-amp, amp), y + rnd.randint(-amp, amp), tile, tile,
                )
        painter.setBrush(QColor(20, 20, 20))
        painter.drawRect(0, 0, W, 26)              # HUD：每帧都在同一个位置
        painter.setBrush(QColor(230, 200, 60))
        painter.drawRect(30, 6, 260, 14)
        painter.end()
        out.append(image)
        current = image
    return out


def _code_refs(module) -> set[str]:
    """模块里**代码**引用到的名字（不含注释与文档字符串）。"""
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
            refs.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.update(a.name for a in node.names)
    return refs


def _rows(db_path, table: str) -> int:
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    # ---------- ① 进程表补齐 ----------

    section("① 进程识别强化：表补了，但挑人的原则没松")

    check("新补的国产/热门大作认得出",
          game_of("b1-Win64-Shipping.exe") == "黑神话：悟空"
          and game_of("narakabladepoint.exe") == "永劫无间", "")
    check("补进来的写实大作认得出（最容易被当成电影的那一批）",
          game_of("hogwartslegacy.exe") == "霍格沃茨之遗"
          and game_of("starfield.exe") == "星空"
          and game_of("armoredcore6.exe") == "装甲核心 6", "")
    check("网游 / 竞技认得出",
          game_of("wow.exe") == "魔兽世界"
          and game_of("ffxiv_dx11.exe") == "最终幻想 14"
          and game_of("crossfire.exe") == "穿越火线", "")
    check("大小写不敏感（前台拿到的名字大小写不保证）",
          game_of("B1-WIN64-SHIPPING.EXE") == "黑神话：悟空", "")

    check("启动器仍然不算游戏（挂着 Steam 逛商店不该问攻略）",
          all(game_of(p) is None for p in
              ("steam.exe", "epicgameslauncher.exe", "battle.net.exe", "wegame.exe")), "")
    check("启动器自查函数也认得出", is_launcher("Steam.exe") is True, "")
    check("通用宿主仍然不收（收了会误伤一大片工具软件）",
          all(game_of(p) is None for p in
              ("javaw.exe", "unityplayer.exe", "python.exe", "chrome.exe")), "")

    # 表是**按类别分组**写的，不按字母序——所以这里只查"全小写、无重复键"。
    # （重复键在字面量里不会报错，只会后者盖前者，静悄悄少认一个游戏，必须查。）
    keys = list(GAME_PROCS)
    dups = sorted({k for k in keys if keys.count(k) > 1})
    bad_case = sorted(k for k in keys if k != k.lower())
    check(f"GAME_PROCS 全是小写、无重复（{len(keys)} 条）",
          not dups and not bad_case,
          f"重复={dups} 非小写={bad_case}")
    check("值都不为空（空名字会让问句变成「看你开了一把《》」）",
          all(v.strip() for v in GAME_PROCS.values()), "")

    # ---------- ② 窗口类名这条线索 ----------

    section("② 窗口类名：认引擎，不认内容")

    check("Unity 的窗口类名算游戏", is_game_window_class("UnityWndClass") is True, "")
    check("Unreal 的窗口类名算游戏（UE 游戏没有统一进程名，靠这条兜住）",
          is_game_window_class("UnrealWindow") is True, "")
    check("Valve / Riot 的窗口类名算游戏",
          is_game_window_class("Valve001") is True
          and is_game_window_class("RiotWindowClass") is True, "")

    check("**UI 框架**的类名不算游戏（Chrome_WidgetWin_1 = 一切 Electron 应用）",
          is_game_window_class("Chrome_WidgetWin_1") is False, "")
    check("Java Swing 的类名不算游戏（SunAwtFrame = 一切 Swing 程序）",
          is_game_window_class("SunAwtFrame") is False, "")
    check("认不出的类名不算游戏（当没这条线索）",
          is_game_window_class("") is False
          and is_game_window_class("WindowsForms10.Window.8.app") is False, "")
    check("大小写敏感（类名是注册时写死的常量，不存在变体拼法）",
          is_game_window_class("unitywndclass") is False, "")

    # ---------- ③ 画面运动特征本身 ----------

    section("③ 画面运动特征：分得开，且**只否不给**")

    base = _texture()
    movie = _movie_frames(base, step=24)
    game = _game_frames(base, amp=12)

    movie_reading = motion.read_images(movie)
    game_reading = motion.read_images(game)

    check("电影式（整幅平滑平移）→ 量得出运动，但**不判游戏**",
          movie_reading is not None and movie_reading.verdict is None,
          movie_reading.line() if movie_reading else "None")
    check("游戏式（分块乱动 + 钉死的 HUD）→ 判游戏",
          game_reading is not None and game_reading.verdict == motion.MOTION_GAME,
          game_reading.line() if game_reading else "None")
    check("两类的可解释度隔着量级（判据的分离度是实的，不是拍的）",
          movie_reading.coherence > 0.3 > game_reading.coherence,
          f"电影 {movie_reading.coherence:.3f} vs 游戏 {game_reading.coherence:.3f}")

    check("帧不够（少于 3 帧）→ 没结论，不硬判",
          motion.read_images([base, base]) is None, "")
    check("一帧都没取到 → 没结论",
          motion.read_images([]) is None and motion.read_images(None) is None, "")
    check("取不到画面（签名给 None）→ 没结论",
          motion.read([None, None, None, None]) is None, "")
    check("太小的帧当没看见（测试占位图那种，量不出运动）",
          motion.signature(_texture().scaled(8, 8)) is None, "")

    real_numpy = motion._numpy
    try:
        motion._numpy = lambda: None      # 模拟"这台机器上没有 numpy"
        check("numpy 不在 → 整层静默失效，给 None（不抛、不半判）",
              motion.read_images(movie) is None
              and motion.signature(movie[0]) is None, "")
    finally:
        motion._numpy = real_numpy
    check("numpy 回来之后照常工作（失效是可逆的，不是把自己焊死）",
          motion.read_images(game) is not None, "")

    # ---------- ④ 接进判定链（正向那条线） ----------

    section("④ 接入：线索现在走**正向**那条线（要不要进陪玩、弹攻略）")

    box, stub, app = make_env()
    try:
        monitor = app.screen_monitor
        companion = app.screen_companion
        pet = app.pet_window
        pet.show()
        pump(0.05)
        pet._settled = True

        # **这一段的落点在 v2-social-scene-boundary 换过**：原来验的是反向那条
        # （`is_watch_foreground`：认不出的全屏窗口会不会被当成在看片），全屏兜底
        # 删掉之后反向那条线一眼就能看完（`_known_non_video` + `is_watchable`），
        # 三条线索全归正向。这里改成问 `game_hit`——那才是它们真正的归宿。
        #
        # 要摆的真机条件只剩"全屏"一条（正向那条线里，画面运动这一档的前提是全屏）。
        # 不摆的话离屏环境里恒为 False，游戏分支根本走不到，断言就成了恒真的。
        real_fullscreen = mon.is_fullscreen_foreground
        mon.is_fullscreen_foreground = lambda: True
        # v2-game-activate 加的最后一条线索（**无边框铺满**）读的是**真机当前前台窗口**
        # 的样式，而跑测试的时候前台很可能是桌面——Windows 桌面（`Progman`，样式
        # `0x96000000`）本身就是一个无边框铺满的窗口，这条就会凭空命中，断言变成
        # "看跑测试时鼠标在哪儿"。把窗口类名与样式**显式钉成普通窗口**（有标题栏、
        # 可缩放边框），让这一节只验它自己要验的那两件事。
        real_style = mon.foreground.foreground_window_style
        real_fg_class = mon.foreground.foreground_window_class
        mon.foreground.foreground_window_style = lambda: 0x17CF0000
        mon.foreground.foreground_window_class = lambda: "Chrome_WidgetWin_1"

        section("④-1 窗口类名这条线索")

        real_class = mon.foreground.foreground_window_class
        try:
            mon.foreground.foreground_window_class = lambda: ""
            check("前置：全屏 + 认不出名字 + 非游戏类名 + 路径也不像 → 认不出，不问攻略",
                  monitor.game_hit(UNKNOWN_PROC) is None, "")

            mon.foreground.foreground_window_class = lambda: "UnrealWindow"
            hit = monitor.game_hit(UNKNOWN_PROC)
            check("全屏 + 认不出名字 + **Unreal 窗口类名** → 认出来了，问攻略",
                  hit is not None and hit.source == "window-class",
                  str(hit))

            mon.foreground.foreground_window_class = lambda: "Chrome_WidgetWin_1"
            check("反向对照：类名是 UI 框架的（不是游戏引擎）→ 照旧认不出",
                  monitor.game_hit(UNKNOWN_PROC) is None, "")
        finally:
            mon.foreground.foreground_window_class = real_class

        section("④-2 画面运动这条线索")

        monitor._motion_cache = None
        monitor._motion_grabber = lambda: _game_frames(base, amp=12)
        hit = monitor.game_hit(UNKNOWN_PROC)
        check("全屏 + 认不出名字 + **画面动得像游戏** → 认出来了（source=motion）",
              hit is not None and hit.source == "motion", str(hit))

        monitor._motion_cache = None
        monitor._motion_grabber = lambda: _movie_frames(base, step=24)
        check("反向对照：同一个认不出的进程，画面是平滑运镜 → 照旧认不出（那是电影）",
              monitor.game_hit(UNKNOWN_PROC) is None, "")

        section("④-3 取不到证据时，行为退回加这层之前")

        monitor._motion_cache = None
        monitor._motion_grabber = lambda: []
        check("连拍取不到帧 → 没这条线索，照旧认不出",
              monitor.game_hit(UNKNOWN_PROC) is None, "")

        monitor._motion_cache = None
        monitor._motion_grabber = lambda: (_ for _ in ()).throw(RuntimeError("抓屏炸了"))
        check("取帧抛异常 → 吞掉，当没这条线索（不把整拍判定带崩）",
              monitor.game_hit(UNKNOWN_PROC) is None, "")

        section("④-4 总闸：game_motion 关掉，这一层一次都不跑")

        calls: list[int] = []
        monitor._motion_cache = None
        monitor._motion_grabber = lambda: (calls.append(1) or _game_frames(base, amp=12))
        app.config.screen.game_motion = False
        try:
            check("关掉之后：画面再像游戏也认不出",
                  monitor.game_hit(UNKNOWN_PROC) is None, "")
            check("关掉之后：连拍**一次都没跑**（不是跑了不采纳）",
                  calls == [], f"跑了 {len(calls)} 次")
        finally:
            app.config.screen.game_motion = True

        section("④-5 记账：同一个进程只真跑一次连拍")

        monitor._motion_cache = None
        calls.clear()
        monitor._motion_grabber = lambda: (calls.append(1) or _movie_frames(base, step=24))
        for _ in range(4):
            monitor.game_hit(UNKNOWN_PROC)
        check("同一进程连着问 4 次，连拍只跑 1 次（实测约 0.5 秒压在界面线程上，省不得）",
              len(calls) == 1, f"跑了 {len(calls)} 次")

        monitor.game_hit("another-unknown.exe")
        check("换了进程 → 旧结论作废，重新判",
              len(calls) == 2, f"跑了 {len(calls)} 次")

        # ---------- ⑤ 不破坏原来的判定 ----------

        section("⑤ 不破坏：反向那条线只认名单，别的照旧")

        monitor._motion_cache = None
        monitor._motion_grabber = lambda: _game_frames(base, amp=12)   # 画面**像游戏**
        check("本地播放器照旧算在看片（那段画面跟它没关系——反向线不采画面了）",
              monitor.is_watch_foreground(PLAYER_PROC) is True, "")
        check("浏览器照旧算在看片（网页里看剧不能被当游戏）",
              monitor.is_watch_foreground(BROWSER_PROC) is True, "")
        check("**认不出名字的一律不算在看片**（全屏兜底删掉之后的正面断言）",
              monitor.is_watch_foreground(UNKNOWN_PROC) is False
              and monitor._watch_candidate(UNKNOWN_PROC) is False, "")
        for proc, why in (("explorer.exe", "桌面/资源管理器"),
                          ("code.exe", "编辑器"),
                          ("wechat.exe", "聊天")):
            check(f"非视频硬闸照旧：{why} 铺满全屏也不算看片",
                  monitor.is_watch_foreground(proc) is False, "")
        check("认得出的游戏照旧直接否（上一轮那条腿没被绕过）",
              monitor.is_watch_foreground("league of legends.exe") is False, "")

        section("⑤-2 端到端：认不出名字的全屏窗口，一个字都不冒")

        said: list[str] = []
        app.bus.screen_caption.connect(lambda line: said.append(line))
        monitor._motion_cache = None
        monitor._motion_grabber = lambda: _game_frames(base, amp=12)
        real_class = mon.foreground.foreground_window_class
        mon.foreground.foreground_window_class = lambda: "UnityWndClass"
        try:
            companion.on_foreground(UNKNOWN_PROC)
            pump(0.05)
            check("认不出名字的全屏窗口 → 不开看片会话",
                  companion._watch_open is False, str(companion._watch_open))
            companion._show_opening()      # 就算宽限期到了也不该冒出东西来
            pump(0.02)
            check("认不出名字的全屏窗口 → 一个字都不冒（修前这里会冒「主人，看电影啦」）",
                  said == [], str(said))
        finally:
            mon.foreground.foreground_window_class = real_class

        section("⑤-3 反向对照：名单里的播放器，照旧开口")

        said.clear()
        companion._watch_open = False
        companion._opening_done = False
        companion._opening_skipped = False
        monitor._motion_cache = None
        monitor._motion_grabber = lambda: _movie_frames(base, step=24)
        companion.on_foreground(PLAYER_PROC)
        pump(0.02)
        check("名单里的播放器 → 看片会话照开（删兜底没有误伤认得出的播放器）",
              companion._watch_open is True, str(companion._watch_open))

        mon.is_fullscreen_foreground = real_fullscreen
        mon.foreground.foreground_window_style = real_style
        mon.foreground.foreground_window_class = real_fg_class

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：零新依赖、不落盘、两库零写入")

        refs = _code_refs(motion)
        banned = {"grab_foreground_window", "OcrReader", "save_temp", "open", "write_bytes",
                  "write_text", "mkdir", "Path", "tempfile", "remove", "unlink",
                  "read_browser_title", "getWindowText", "ReadProcessMemory"}
        check("motion.py 代码里没有落盘 / 截屏 / 读标题那几只的手",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")

        src_motion = (Path(__file__).resolve().parents[1]
                      / "life" / "screen" / "motion.py").read_text(encoding="utf-8")
        src_monitor = (Path(__file__).resolve().parents[1]
                       / "life" / "screen" / "monitor.py").read_text(encoding="utf-8")
        src_game = (Path(__file__).resolve().parents[1]
                    / "life" / "screen" / "game.py").read_text(encoding="utf-8")
        check("没有新引重型视觉库（opencv / 光流 / torch 一律没有）",
              not any(w in src_motion for w in
                      ("import cv2", "import torch", "optical_flow", "calcOpticalFlow")), "")
        check("运动特征只用 PySide6 + 既有的 numpy（numpy 还是惰性拿的）",
              "from PySide6" in src_motion and "import numpy as np" in src_motion
              and "def _numpy()" in src_motion, "")
        # monitor 侧**本来就在用 numpy**（`_to_numpy` 那条腿，比这一轮早），所以这里
        # 查的不是"有没有 numpy"，而是"有没有多出重型视觉库"。走 AST 查代码引用，
        # 不查字符串——注释里提一句"没有 opencv"不该被判成引了 opencv。
        mon_refs = _code_refs(mon)
        check("monitor 侧没有新引重型视觉库 / 网络库",
              not ({"cv2", "torch", "scipy", "requests", "httpx"} & mon_refs),
              "、".join(sorted({"cv2", "torch", "scipy", "requests", "httpx"} & mon_refs))
              or "（一处都没有）")
        check("这一轮给 monitor 加的东西只有自家 motion 模块",
              "motion" in mon_refs and "MOTION_TTL_SEC" in mon_refs, "")

        # game.py 这条同理：文档字符串里明明白白写着"取的不是 GetWindowTextW 那句
        # 标题"，字符串搜索会自己把自己判违规——所以走 AST 查代码里到底有没有伸这只手。
        game_refs = _code_refs(game_mod)
        check("game.py 代码里没有伸手去读窗口标题",
              not ({"GetWindowText", "GetWindowTextW", "GetWindowTextLengthW",
                    "foreground_title", "read_browser_title"} & game_refs),
              "、".join(sorted({"GetWindowText", "GetWindowTextW", "foreground_title"}
                               & game_refs)) or "（一处都没有）")
        check("读类名那一步走的是 monitor/foreground.py 的 foreground_window_class",
              "foreground_window_class" in mon_refs
              and callable(getattr(fg_mod, "foreground_window_class", None)), "")

        section("⑥-2 两库零写入")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        for proc in (UNKNOWN_PROC, PLAYER_PROC, "league of legends.exe"):
            monitor.is_watch_foreground(proc)
            monitor._watch_candidate(proc)
            pump(0.05)
        drain_threads()
        check("pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")

        section("⑥-3 界面上不留画面（只留判读出来的那几个数字）")

        reading = motion.read_images(_game_frames(base, amp=12))
        check("判读结果对象上只有数字与结论，没有画面本身",
              set(vars(reading)) ==
              {"frames", "pairs", "energy", "coherence", "busy_share", "verdict"},
              str(sorted(vars(reading))))
        check("一行日志也不带画面（只有数字）",
              "帧" in reading.line() and "能量" in reading.line(), reading.line())

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("游戏态彻底识别（v2-game-fix）：进程名 + 窗口类名 + 画面运动")


if __name__ == "__main__":
    raise SystemExit(main())
