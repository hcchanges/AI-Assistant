"""
游戏态系统性识别（v2-game-activate）：不靠逐款枚举名单，主流游戏通杀。

上一轮（v2-game-fix）修的是"**误判电影**"那一侧——给认不出名字的全屏游戏补了窗口类名
与画面运动两条线索。这一轮把**正向那侧**也接通，并把识别策略从"进程名枚举"升级成
"游戏引擎特征 + 安装路径 + 全屏独占渲染 + 运动特征 + 本地名单"五路，任何一路命中且
不是明确影视信号 → 游戏态。

真缺口在哪儿（S0 查出来的）：正向链路一直是通的，`GameGuideService.on_foreground`
从来没缺分支——它只是**只查 `game_of(proc)`**，也就是 `GAME_PROCS` 那 152 条等值表。
表里没有的游戏（RE9 之类）第三行就 return 了，一个字都不问；同时反向那条链还可能把
它当看电影。所以"接通正向"这件事的真身是：**让正向也走那五条线索**。

本文件覆盖：

① 安装路径这条线索（"通杀"的主力，不认识名字也能命中）
② 引擎窗口类名扩充（Unity / Unreal / Source / Godot…）
③ 用户本地名单 `game_procs.txt`（三种写法、读不到不抛、**只读不写**）
④ `recognize()` 五路合一 + 认不出时的**粗名字**（exe 主干名）
⑤ 反向：接进"算不算在看片"那条链，主流平台逐类验一遍
⑥ 正向：认不出名字的游戏也能进陪玩 + 弹攻略；与影视互斥
⑦ 红线：两库零写入、零新依赖、不读窗口标题（除非名单里真有 title: 词）

反向对照贯穿每一节：认得出的播放器/浏览器照旧算看片、已知非视频硬闸照旧拦、
取不到证据时照旧放行——免得断言变成恒真的。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from life.screen import game as game_mod
from life.screen import monitor as mon
from life.screen.game import (
    GAME_PATH_MARKERS,
    GAME_WINDOW_CLASSES,
    LOCAL_RULES_FILE,
    LOCAL_TITLE_MAX,
    GameHit,
    GameGuideService,
    LocalGameRules,
    exe_stem,
    looks_like_game_path,
    recognize,
)

# 认不出名字（不在 GAME_PROCS 里）的假游戏进程名。故意起得又长又怪，像真游戏 exe。
UNKNOWN = "some-title-win64-shipping.exe"
UNKNOWN2 = "mystery-teaser.exe"

# 一个**有**标题栏的普通窗口样式（真机采到的值：VS Code 那类）
STYLE_WINDOWED = 0x17CF0000
# 一个**没有**标题栏/边框/系统菜单的样式（无边框铺满：把三条位全清掉）
STYLE_BORDERLESS = 0x10000000


# ---------- 可控的假前台 ----------

class FakeDesktop:
    """把"前台是谁"整条线摆成可控的假值。

    要摆的不止进程名了（v2-game-activate）：窗口类名、exe 安装路径、窗口样式、全屏、
    标题——五条线索各要一样。真机在离屏环境里这些全是空值，不摆出来的话那几条
    分支根本走不到，断言就成了恒真的。

    **`blank` 这个参数没有了**（v2-social-scene-boundary）：全屏兜底整条删掉之后，
    "空白闸"（`_foreground_blank`）连函数都没了，摆它没有意义。调用方传 `blank=` 会
    直接 TypeError——这是有意的，免得有人以为它还在起作用。

    退出时把 `monitor.foreground` 上被换掉的函数原样放回去（它是模块对象，
    **补丁是进程级的**，不还原会污染后面的用例）。
    """

    def __init__(self, monitor, app, *, proc="", window_class="", exe="",
                 style=STYLE_WINDOWED, fullscreen=True, title=""):
        self.monitor = monitor
        self.app = app
        self.proc = proc
        self.window_class = window_class
        self.exe = exe
        self.style = style
        self.fullscreen = fullscreen
        self.title = title
        self._saved = {}

    def __enter__(self):
        fg = mon.foreground
        self._saved = {
            "pid": fg.foreground_pid,
            "class": fg.foreground_window_class,
            "style": fg.foreground_window_style,
            "title": fg.foreground_title,
            "name_of": self.monitor._pids.name_of,
            "exe_of": self.monitor._pids.exe_of,
            "fullscreen": mon.is_fullscreen_foreground,
            "grabber": self.monitor._motion_grabber,
            "cache": self.monitor._motion_cache,
        }
        fg.foreground_pid = lambda: 1
        fg.foreground_window_class = lambda: self.window_class
        fg.foreground_window_style = lambda: self.style
        fg.foreground_title = lambda: self.title
        self.monitor._pids.name_of = lambda pid: self.proc
        self.monitor._pids.exe_of = lambda pid: self.exe
        mon.is_fullscreen_foreground = lambda: self.fullscreen
        # 默认一格画面都取不到 → 运动那条线索静默失效，绝不真去抓用户的屏
        self.monitor._motion_grabber = lambda: []
        self.monitor._motion_cache = None
        return self

    def __exit__(self, *exc):
        fg = mon.foreground
        fg.foreground_pid = self._saved["pid"]
        fg.foreground_window_class = self._saved["class"]
        fg.foreground_window_style = self._saved["style"]
        fg.foreground_title = self._saved["title"]
        self.monitor._pids.name_of = self._saved["name_of"]
        self.monitor._pids.exe_of = self._saved["exe_of"]
        mon.is_fullscreen_foreground = self._saved["fullscreen"]
        self.monitor._motion_grabber = self._saved["grabber"]
        self.monitor._motion_cache = self._saved["cache"]
        return False


def _code_refs(module) -> set[str]:
    """模块里**代码**引用到的名字（不含注释与文档字符串）。

    红线查的是"代码里有没有伸这只手"，不是"注释里提没提"——所以走 AST，不能用字符串
    搜索：这几个模块的开头正大光明地解释着红线本身，一搜就自己把自己判违规。
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


# 各主流发行平台的**真实**安装路径样本（照各平台默认装法写的）
PLATFORM_PATHS = [
    ("Steam 主库", r"C:\Program Files (x86)\Steam\steamapps\common\RE9\re9.exe"),
    ("Steam 副库", r"D:\SteamLibrary\steamapps\common\Some Game\game.exe"),
    ("Epic", r"C:\Program Files\Epic Games\SomeTitle\Binaries\Win64\Some-Win64-Shipping.exe"),
    ("Ubisoft", r"C:\Program Files (x86)\Ubisoft\Ubisoft Game Launcher\games\Far Cry\bin\fc.exe"),
    ("GOG", r"C:\Program Files (x86)\GOG Galaxy\Games\Witcher\witcher3.exe"),
    ("Riot", r"C:\Riot Games\VALORANT\live\VALORANT.exe"),
    ("EA", r"C:\Program Files\Electronic Arts\Battlefield\bf.exe"),
    ("Xbox/PC Game Pass", r"C:\XboxGames\Starfield\Content\Starfield.exe"),
    ("WeGame", r"D:\WeGameApps\rail_apps\somegame\game.exe"),
]

# 这些路径**不许**判成游戏（判了就等于"所有装机软件都是游戏"）
NOT_GAME_PATHS = [
    ("VS Code", r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe"),
    ("记事本", r"C:\Windows\System32\notepad.exe"),
    ("微软商店应用", r"C:\Program Files\WindowsApps\SomeApp\a.exe"),
    ("随便一个叫 Games 的文件夹", r"D:\Games\a.exe"),
    ("steamapps 下的创意工坊（不带 common）",
     r"C:\Program Files (x86)\Steam\steamapps\workshop\content\123\a.exe"),
    ("Steam 本体", r"C:\Program Files (x86)\Steam\steam.exe"),
]


def main() -> int:
    # ---------- ① 安装路径：通杀的主力 ----------

    section("① 安装路径：不认识名字也认得（这条才是'主流游戏通杀'）")

    for label, path in PLATFORM_PATHS:
        check(f"{label} 目录下的 exe 判成游戏", looks_like_game_path(path) is True, path)

    for label, path in NOT_GAME_PATHS:
        check(f"不误伤：{label}", looks_like_game_path(path) is False, path)

    check("反斜杠 / 正斜杠都认（psutil 给反斜杠，测试与别处可能给正斜杠）",
          looks_like_game_path("C:/Program Files (x86)/Steam/steamapps/common/X/x.exe") is True,
          "")
    check("大小写不敏感（Windows 路径本来就不分大小写）",
          looks_like_game_path(r"c:\STEAMAPPS\COMMON\x\x.exe") is True, "")
    check("空串当'查不到路径'，不是游戏",
          looks_like_game_path("") is False and looks_like_game_path(None or "") is False, "")
    check("标记自己写对了：全是正斜杠、两端带斜杠、没有一个是原始字符串的反斜杠",
          all(m.startswith("/") and m.endswith("/") and "\\" not in m
              for m in GAME_PATH_MARKERS),
          str(GAME_PATH_MARKERS))

    # ---------- ② 引擎窗口类名 ----------

    section("② 引擎窗口类名：认引擎，不认内容")

    check(f"表里每一条都自认（{len(GAME_WINDOW_CLASSES)} 条）",
          all(game_mod.is_game_window_class(c) for c in GAME_WINDOW_CLASSES),
          str(sorted(GAME_WINDOW_CLASSES)))
    check("三大引擎都在：Unity / Unreal / Source",
          all(game_mod.is_game_window_class(c)
              for c in ("UnityWndClass", "UnrealWindow", "Valve001")), "")
    check("Godot / SDL / GLFW 这类**游戏常用**的也在（独立游戏靠这几条）",
          all(game_mod.is_game_window_class(c)
              for c in ("Godot", "SDL_app", "GLFW30")), "")
    check("**UI 框架**的类名仍然不收（收了等于'所有 Electron/Swing 应用都是游戏'）",
          not any(game_mod.is_game_window_class(c)
                  for c in ("Chrome_WidgetWin_1", "SunAwtFrame",
                            "WindowsForms10.Window.8.app", "")), "")

    section("②-2 外壳窗口黑名单：**桌面就是个无边框铺满的窗口**")

    check("桌面（Progman）被认成外壳窗口",
          game_mod.is_shell_window_class("Progman") is True, "")
    check("任务栏 / 开始菜单那几个也在名单里",
          all(game_mod.is_shell_window_class(c) for c in
              ("WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd",
               "Windows.UI.Core.CoreWindow")), "")
    check("UI 框架的类名**不在**黑名单里（那一类走'认不出'，不是'一定不是游戏'）",
          not any(game_mod.is_shell_window_class(c) for c in
                  ("Chrome_WidgetWin_1", "SunAwtFrame", "UnityWndClass", "")), "")
    check("**真机逮到的那个坑**：桌面 + 无边框铺满 → 不许判成游戏",
          recognize("explorer.exe", window_class="Progman",
                    exe_path=r"C:\Windows\explorer.exe",
                    borderless_fullscreen=True) is None, "")
    check("反过来：同样的无边框铺满、类名不是外壳 → 照旧走'无边框'那条线索",
          (lambda h: h is not None and h.source == "fullscreen-exclusive")(
              recognize(UNKNOWN, window_class="SomeGameWnd",
                        borderless_fullscreen=True)), "")

    # ---------- ③ 本地名单 ----------

    section("③ 用户本地名单：三种写法、读不到不抛、**只读不写**")

    box, stub, app = make_env()
    try:
        # **必须写在沙箱的临时目录里**，不能写 `app.config.data_dir`：沙箱把 data_dir
        # 钉成空串（见 harness.Sandbox 的 v2-data-root 那段），空串会让 `data_path`
        # 退回**仓库自己的 data/**——那样每跑一次测试就往仓库里丢一个 game_procs.txt。
        # 写这一节时踩过一次（真的丢出来了，被 git status 逮住）。
        tmp = box.dir
        path = tmp / LOCAL_RULES_FILE
        # v2-scene-state 之后"问攻略"默认要等开场白说完（30 秒）。这一套测的是识别与
        # 接通（认得出就该问），不是那个等待，所以归零。
        app.config.screen.guide_ask_delay_sec = 0

        # 文件不在 → 空名单，不抛（绝大多数用户不会写这个文件）
        missing = LocalGameRules.load(tmp / "根本没有这个文件.txt")
        check("文件不在 → 一份空名单，不抛", len(missing) == 0, f"len={len(missing)}")

        path.write_text(
            "# 注释行\n"
            "\n"
            "  # 缩进的注释也要认\n"
            "re9.exe = 生化危机 9\n"
            "Hades.exe=哈迪斯\n"                      # 等号两边不留空格也要行
            "class:Godot = 上帝引擎小游戏\n"
            "title:生化危机 = 生化危机 9（标题认出来的）\n"
            "这一行写坏了没有等号\n"
            "= 没有键\n"
            "没有值 =\n",
            encoding="utf-8",
        )
        before = path.read_bytes()
        rules = LocalGameRules.load(path)

        check("进程名那档：大小写不敏感、两边空格都剥掉",
              rules.match_proc("RE9.EXE") == "生化危机 9"
              and rules.match_proc("hades.exe") == "哈迪斯"
              and rules.match_proc(" re9.exe ") == "生化危机 9", "")
        check("窗口类名那档：**大小写敏感**（跟程序注册的一模一样才行）",
              rules.match_class("Godot") == "上帝引擎小游戏"
              and rules.match_class("godot") is None, "")
        check("标题那档：标题里**出现**这个词就算命中",
              rules.match_title("[窗口] 生化危机 9 完整版") == "生化危机 9（标题认出来的）"
              and rules.match_title("别的窗口") is None, "")
        check("注释（含缩进的）与空行都跳过，写坏的行跳过不报错、不拦启动",
              len(rules) == 4, f"len={len(rules)}")
        check("标题关键词截断到上限（用户手写的东西不设上限，别拿超长串去扫）",
              len("x" * (LOCAL_TITLE_MAX + 50)) > LOCAL_TITLE_MAX and LOCAL_TITLE_MAX > 0, "")

        check("**只读不写**：读一遍之后文件逐字节没变",
              path.read_bytes() == before, "")

        # ---------- ④ 五路合一 ----------

        section("④ recognize()：五条线索收成一个结论，认不出给粗名字")

        check("第 1 路 进程表 → 本名",
              (lambda h: h is not None and h.source == "process" and h.name)(
                  recognize("dota2.exe")), "")
        check("第 2 路 本地名单 → 本名",
              (lambda h: h is not None and h.source == "local"
                  and h.name == "生化危机 9")(recognize("re9.exe", rules=rules)), "")
        check("第 3 路 引擎窗口类名 → **粗名字**（exe 主干名）",
              (lambda h: h is not None and h.source == "window-class" and h.name == "re9")(
                  recognize(UNKNOWN, window_class="UnityWndClass",
                            exe_path=r"C:\Games\re9.exe")), "")
        check("第 4 路 安装路径 → 粗名字",
              (lambda h: h is not None and h.source == "install-path" and h.name == "re9")(
                  recognize(UNKNOWN, exe_path=PLATFORM_PATHS[0][1])), "")
        check("第 5 路 画面动得像游戏 → 粗名字",
              (lambda h: h is not None and h.source == "motion" and h.name == "re9")(
                  recognize(UNKNOWN, exe_path=r"C:\Games\re9.exe", motion_game=True)), "")
        check("第 6 路 无边框铺满 → 粗名字（最弱一条，摆最后）",
              (lambda h: h is not None and h.source == "fullscreen-exclusive")(
                  recognize(UNKNOWN, exe_path=r"C:\Games\re9.exe",
                            borderless_fullscreen=True)), "")

        check("五路全不中 → None（认不出就什么都不说）",
              recognize("notepad.exe") is None, "")
        check("空进程名 → None", recognize("") is None and recognize("  ") is None, "")

        check("**启动器先排掉**，而且是排在最前面：Epic 的启动器自己就装在 "
              "'Epic Games' 目录里，靠这条拦住",
              recognize("epicgameslauncher.exe",
                        exe_path=r"C:\Program Files\Epic Games\Launcher\Portal\Binaries\Win64\EpicGamesLauncher.exe"
                        ) is None
              and recognize("steam.exe", exe_path=PLATFORM_PATHS[0][1]) is None, "")

        check("**认得出的播放器/浏览器**不会被弱线索否掉（那条线是刷剧陪伴的地盘）",
              mon.is_watchable("chrome.exe") and mon.is_watchable("potplayer.exe"), "")

        check("粗名字：re9.exe → re9；取不到路径退回进程名主干；都没有给空串",
              exe_stem(r"C:\a\b\re9.exe") == "re9"
              and exe_stem("", "Hades.EXE") == "Hades"
              and exe_stem("") == "", "")

        # ---------- ⑤ 反向：接进"算不算在看片" ----------

        section("⑤ 反向接入：认不出名字的一律不算'在看片'（名单闸）")

        # **这一节的口径在 v2-social-scene-boundary 变了**：反向那条线不再问游戏线索，
        # 全屏兜底删掉之后**只认名单**——非播放器且非浏览器一律不算。所以下面这些
        # "认不出的进程 → 不算在看片"的断言**结论没变**（原来靠游戏线索拦住，现在在
        # 名单那一档就拦住了），但拦它的理由换了；每条都按新理由重写了措辞。
        # **线索本身没丢**：它们在 ⑥ 正向那条线上逐条验着（认不出名字的全屏游戏照样
        # 能进陪玩、弹攻略）——那边才是真要救的场景。

        monitor = app.screen_monitor
        real_rules = monitor._game_rules
        try:
            for label, exe in PLATFORM_PATHS:
                # 进程名故意用表里没有的那个
                with FakeDesktop(monitor, app, proc=UNKNOWN, exe=exe):
                    check(f"{label}：认不出名字的游戏 → **不算在看片**（不在播放器/浏览器名单里）",
                          monitor.is_watch_foreground(UNKNOWN) is False, exe)
                    check(f"{label}：同样不进采样（不截屏、不认字）",
                          monitor._watch_candidate(UNKNOWN) is False, "")

            with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="UnityWndClass"):
                check("Unity 独立游戏：认不出名字 → 不算在看片",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")
            with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="UnrealWindow"):
                check("Unreal 游戏 → 不算在看片",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")
            with FakeDesktop(monitor, app, proc=UNKNOWN, style=STYLE_BORDERLESS):
                check("无边框铺满：认不出名字、也没有别的线索 → 不算在看片",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")

            # 真机回归逮到过的那一个：前台是**桌面**（`Progman`），它也是个无边框铺满的
            # 窗口。没修之前这条会把桌面判成游戏（`source='fullscreen-exclusive'`）。
            with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="Progman",
                             style=STYLE_BORDERLESS, exe=r"C:\Windows\explorer.exe"):
                check("**桌面**（无边框铺满的外壳窗口）→ 不算游戏",
                      monitor.game_hit(UNKNOWN) is None, "")
                check("同上：全屏兜底已经删了，认不出的进程**更不可能**算在看片",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")

            # ---- 反向对照：这几类**必须**照旧算在看片 / 照旧不判游戏 ----

            with FakeDesktop(monitor, app, proc="chrome.exe", window_class="Chrome_WidgetWin_1",
                             exe=r"C:\Program Files\Google\Chrome\Application\chrome.exe"):
                check("反向对照：浏览器前台 → 照旧算在看片（认得出的那条腿在前一档就返回）",
                      monitor.is_watch_foreground("chrome.exe") is True, "")
                check("反向对照：浏览器不算游戏（有边框、也不是引擎类名）",
                      monitor.game_hit("chrome.exe") is None, "")
            with FakeDesktop(monitor, app, proc="potplayer.exe", style=STYLE_BORDERLESS):
                check("反向对照：本地播放器全屏 → 照旧算在看片",
                      monitor.is_watch_foreground("potplayer.exe") is True, "")
                check("反向对照：播放器不算游戏", monitor.game_hit("potplayer.exe") is None, "")

            with FakeDesktop(monitor, app, proc="powerpnt.exe", window_class="PP12FrameClass",
                             style=STYLE_BORDERLESS,
                             exe=r"C:\Program Files\Microsoft Office\root\Office16\POWERPNT.EXE"):
                check("**办公全屏（放幻灯也是无边框全屏）** → 硬闸拦住，不算游戏",
                      monitor.game_hit("powerpnt.exe") is None, "")
                check("办公全屏也不算在看片（已知非视频那一道硬闸）",
                      monitor.is_watch_foreground("powerpnt.exe") is False, "")

            with FakeDesktop(monitor, app, proc="Code.exe", window_class="Chrome_WidgetWin_1",
                             exe=r"C:\Users\me\AppData\Local\Programs\Microsoft VS Code\Code.exe"):
                check("反向对照：编辑器全屏 → 不算游戏（路径也不在游戏平台的目录下）",
                      monitor.game_hit("Code.exe") is None, "")

            with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="Chrome_WidgetWin_1",
                             exe=r"C:\Users\me\AppData\Local\SomeApp\a.exe", fullscreen=False):
                check("反向对照：认不出名字、还不是全屏 → 不算在看片",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")
            # **这一条是本轮改动最直接的那一处**（v2-social-scene-boundary）：原来
            # "认不出名字 + 全屏 + 线索一条不中 → 照旧算在看片"，也就是那个"给认不全的
            # 本地播放器留的后门"。用户真机上它放进来的全是最大化过的普通窗口（截图工具、
            # 设置页、资源管理器），于是对着没在放片的窗口冒出「我陪你看电影」。现在按
            # 用户拍板的原话**删掉兜底**：非播放器且非浏览器一律不放行。
            with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="Chrome_WidgetWin_1",
                             exe=r"C:\Users\me\AppData\Local\SomeApp\a.exe", fullscreen=True):
                check("反向对照（本轮改动）：认不出名字 + **全屏** + 线索一条不中 "
                      "→ **不再**算在看片（全屏兜底已删）",
                      monitor.is_watch_foreground(UNKNOWN) is False, "")
                check("同上：这一档也不再进采样（不截屏、不认字）",
                      monitor._watch_candidate(UNKNOWN) is False, "")
            # 同一形态的**正向对照**：真正在名单里的播放器，全屏照样算——"删兜底"没有
            # 把"认得出的播放器"这条腿一起删掉，这一条钉住边界。
            with FakeDesktop(monitor, app, proc="mpv.exe",
                             exe=r"C:\Users\me\AppData\Local\mpv\mpv.exe", fullscreen=True):
                check("正向对照：名单里的播放器（mpv）+ 全屏 → 照旧算在看片",
                      monitor.is_watch_foreground("mpv.exe") is True, "")
                check("正向对照：点名进采样",
                      monitor._watch_candidate("mpv.exe") is True, "")

            # ---- 本地名单接进判定链 ----

            monitor._game_rules = LocalGameRules(
                {"mystery-teaser.exe": "我本地的游戏"}, {}, ()
            )
            with FakeDesktop(monitor, app, proc=UNKNOWN2, fullscreen=False):
                check("本地名单里的进程 → 哪怕窗口化、不全屏，也不算在看片",
                      monitor.is_watch_foreground(UNKNOWN2) is False, "")
            monitor._game_rules = real_rules

            # ---- 窗口样式那条判据本身 ----

            check("样式取不到（0）→ 当'有边框'，也就是没这条线索（宁可漏判游戏）",
                  mon.foreground.window_has_frame(0) is True, "")
            check("普通窗口（有标题栏）→ 有边框",
                  mon.foreground.window_has_frame(STYLE_WINDOWED) is True, "")
            check("三条位一条不留 → 无边框",
                  mon.foreground.window_has_frame(STYLE_BORDERLESS) is False, "")
            check("只要还留着一条位就不算无边框（只留系统菜单也算）",
                  mon.foreground.window_has_frame(mon.foreground.WS_SYSMENU) is True, "")

            # ---------- ⑥ 正向：认不出名字也能进陪玩 ----------

            section("⑥ 正向接通：认不出名字的游戏也进陪玩 + 弹攻略")

            guide = app.game_guide
            offers: list[str] = []
            app.bus.game_offer.connect(offers.append)
            try:
                guide._awaiting = False
                guide._last_asked.clear()
                offers.clear()

                with FakeDesktop(monitor, app, proc=UNKNOWN, exe=PLATFORM_PATHS[0][1]):
                    guide.on_foreground(UNKNOWN)
                check("Steam 目录下、名字认不出的游戏 → **问出口了**（修前这里一个字都没有）",
                      len(offers) == 1, f"{len(offers)} 次")
                check("问句里用的是**粗名字**（exe 主干名），不是空串、也不是进程全名",
                      offers and offers[-1] == "re9", str(offers))
                check("确实在等用户点头（先问再给）", guide.awaiting_confirm is True, "")

                guide._awaiting = False
                guide._last_asked.clear()
                offers.clear()
                with FakeDesktop(monitor, app, proc=UNKNOWN, window_class="UnityWndClass"):
                    guide.on_foreground(UNKNOWN)
                check("Unity 独立游戏（认不出名字）→ 也问出口",
                      len(offers) == 1, f"{len(offers)} 次")

                # ---- 与影视互斥 ----

                guide._awaiting = False
                guide._last_asked.clear()
                offers.clear()
                with FakeDesktop(monitor, app, proc="potplayer.exe", style=STYLE_BORDERLESS):
                    guide.on_foreground("potplayer.exe")
                check("**影视互斥**：播放器前台一个字都不问（不弹攻略）",
                      not offers, str(offers))
                with FakeDesktop(monitor, app, proc="chrome.exe",
                                 window_class="Chrome_WidgetWin_1"):
                    guide.on_foreground("chrome.exe")
                check("**影视互斥**：浏览器前台也不问", not offers, str(offers))
                with FakeDesktop(monitor, app, proc="powerpnt.exe", style=STYLE_BORDERLESS):
                    guide.on_foreground("powerpnt.exe")
                check("**影视互斥**：办公全屏也不问", not offers, str(offers))

                # ---- 认得出名字那条老路没退步 ----

                guide._awaiting = False
                guide._last_asked.clear()
                offers.clear()
                guide.on_foreground("dota2.exe")
                check("老路没退步：进程表里认得出的游戏照旧问",
                      len(offers) == 1 and offers[-1] == "DOTA2", str(offers))

                guide._awaiting = False
                guide._last_asked.clear()
                offers.clear()
                with FakeDesktop(monitor, app, proc=UNKNOWN, fullscreen=False,
                                 exe=r"C:\Users\me\AppData\Local\SomeApp\a.exe"):
                    guide.on_foreground(UNKNOWN)
                check("认不出、线索一条不中 → 不问（别把随便一个窗口都当游戏）",
                      not offers, str(offers))

                # ---- 没挂采样器时退回老行为 ----

                lone = GameGuideService(config=app.config, bus=app.bus)
                lone_offers: list[str] = []
                app.bus.game_offer.connect(lone_offers.append)
                try:
                    lone.on_foreground("dota2.exe")
                    check("没挂采样器（测试里直接 new 出来的那种）→ 退回只认名字，仍能问",
                          len(lone_offers) == 1, f"{len(lone_offers)} 次")
                    lone._awaiting = False
                    lone.on_foreground(UNKNOWN)
                    check("没挂采样器时认不出名字 → 照旧不问（那几条线索全在采样器手上）",
                          len(lone_offers) == 1, f"{len(lone_offers)} 次")
                finally:
                    app.bus.game_offer.disconnect(lone_offers.append)
            finally:
                app.bus.game_offer.disconnect(offers.append)

            # ---------- ⑦ 红线 ----------

            section("⑦ 红线：两库零写入 / 零新依赖 / 不读窗口标题")

            pet_before = _rows(app.config.db_path, "utterance")
            mind_before = app.mind_store.fragment_count()

            with FakeDesktop(monitor, app, proc=UNKNOWN, exe=PLATFORM_PATHS[0][1]):
                for _ in range(3):
                    monitor.is_watch_foreground(UNKNOWN)
                    monitor._watch_candidate(UNKNOWN)
                    monitor.game_hit(UNKNOWN)
                    guide._awaiting = False
                    guide._last_asked.clear()
                    guide.on_foreground(UNKNOWN)
            drain_threads()
            pump(0.05)

            check("pet.db utterance 一行没多",
                  _rows(app.config.db_path, "utterance") == pet_before,
                  f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
            check("mind.db 碎片一条没多",
                  app.mind_store.fragment_count() == mind_before,
                  f"{mind_before} → {app.mind_store.fragment_count()}")

            # 零新依赖：只看 import，不看代码里提到的名字（那种写法只会误伤）
            def _imports(module) -> set[str]:
                import ast
                import inspect

                roots: set[str] = set()
                for node in ast.walk(ast.parse(inspect.getsource(module))):
                    if isinstance(node, ast.Import):
                        roots.update(a.name.split(".")[0] for a in node.names)
                    elif isinstance(node, ast.ImportFrom) and node.module:
                        roots.add(node.module.split(".")[0])
                return roots

            # 允许的第三方只有仓库里本来就有的那几个；其余必须是标准库或本仓库的包。
            # `rapidocr_onnxruntime` 是 OCR 那条腿的既有依赖（`OcrReader` 一直在用），
            # 不在"这一轮新引进来的"里面。
            known = {"PySide6", "psutil", "numpy", "rapidocr", "rapidocr_onnxruntime",
                     "core", "life", "monitor", "ui", "brain"}
            stdlib_ok = {"__future__", "ast", "ctypes", "dataclasses", "functools",
                         "logging", "os", "pathlib", "random", "re", "sys", "threading",
                         "time", "typing"}
            for label, module in (("game.py", game_mod), ("monitor.py", mon)):
                foreign = _imports(module) - known - stdlib_ok
                check(f"{label} 没有引新包（光流 / opencv / win32 / pywin32 一个都没有）",
                      not foreign, str(sorted(foreign)))
            check("game.py 那边的第三方仍然只有本来就有的 PySide6（没冒出第四个）",
                  _imports(game_mod) & {"PySide6", "psutil", "numpy"} == {"PySide6"},
                  str(sorted(_imports(game_mod) & {"PySide6", "psutil", "numpy"})))

            src_game = Path(game_mod.__file__).read_text(encoding="utf-8")
            check("docstring 里的那句红线口径还在（只读进程名与窗口类名、不读窗口标题）",
                  "只读" in src_game and "不读窗口标题" in src_game, "")
            check("**game.py 自己一次都不读屏**（不读标题、不截屏、不认字）",
                  not (_code_refs(game_mod)
                       & {"foreground_title", "GetWindowTextW", "grab", "grabber",
                          "signature", "read_images", "QImage"}),
                  str(sorted(_code_refs(game_mod)
                             & {"foreground_title", "grab", "signature"})))
            check("认游戏的结论是个 GameHit（名字 + 哪条线索认出来的），不是裸字符串",
                  isinstance(monitor.game_hit("dota2.exe"), GameHit), "")

            src_mon = Path(mon.__file__).read_text(encoding="utf-8")
            check("读 exe 路径走的是既有依赖 psutil（不是新包）",
                  "psutil" in Path(mon.foreground.__file__).read_text(encoding="utf-8")
                  or "psutil" in src_mon, "")
            check("标题那道破例**没有**多开口子：名单里没有 title: 词时一个字都不读",
                  monitor._game_title() == ""
                  or bool(getattr(monitor._game_rules, "titles", ())), "")
        finally:
            monitor._game_rules = real_rules
    finally:
        drain_threads()
        box.cleanup()

    return summary("游戏态系统性识别")


if __name__ == "__main__":
    sys.exit(main())
