"""游戏态识别（v2-game-guide，v2-game-fix 加第二条线索）。

**只读进程名与窗口类名，不读窗口标题、不截屏、不认字。**

两条线索，都不是"读屏"：

1. **前台进程名**（`GAME_PROCS`）——主力。等值对照，大小写不敏感。
2. **前台窗口类名**（`GAME_WINDOW_CLASSES`，v2-game-fix）——只用来补表认不全的那批。
   引擎给自家长出来的窗口注册一个固定类名（`UnityWndClass`、`UnrealWindow`、
   `Valve001`…），**它是程序写在代码里的常量，不是标题、不认内容**，所以
   `monitor.py` 里"读浏览器标题"那道破例的口径在这里不成立——那条破例是为了看
   标题里的**用户内容**（网址、文档名）才必须单开总闸，这里读的是一个跟内容无关的
   枚举值。**注意"不读窗口标题"这条仍然成立**：`GetClassNameW` 取的是类名，
   不是 `GetWindowTextW` 那句标题，模块里没有引用后者。

**这条块唯一不碰的还是读屏**：OCR 字幕带那条腿、CV 画面那条腿，一脚都不踩
（画面运动特征在 `motion.py` 里，是另一条独立的线索、也只看"动得匀不匀"，不认内容）。

**启动器不算游戏。** Steam / WeGame / Epic 挂着只说明"平台开着"，不代表在玩——
往那儿冒一句"在玩什么呀"会很蠢。它们单独一张表 `LAUNCHER_PROCS`，只用来**排除**。
真正在玩的时候，前台进程是游戏自己的 exe。

**这两张表注定认不全。** 它们不是"游戏数据库"，是**起手表**：认不出来就当没在玩，
一个字都不说（宁可漏判，跟 kb 那张词表一个态度）。要补就补 `GAME_PROCS`——
就是一张"进程名 → 游戏名"的等值对照表，比对时两边都降成小写，加一行就多认一个游戏，
没有别的机关。（刻意不做子串匹配：`repo.exe` 这种短名用子串匹配会误伤一大片。也**刻意
不做** `*-win64-shipping.exe` 这种后缀匹配——Unreal 游戏的名字各随各的项目，确实没有
统一的进程名，但那批由窗口类名 `UnrealWindow` 兜住了，不需要再开一个后缀机关。）

**窗口类名这条的代价，写在这儿。** 它比进程名糙：`UnityWndClass` 是一个**引擎**，
不是一款游戏——某些 Unity 做的非游戏应用（工具、演示）也用它。所以这一条**只在
"全屏 + 进程名认不出 + 不是播放器/浏览器"那一档里当辅证**（见 `monitor.is_watch_foreground`），
不会单独把谁判成游戏。代价方向也是可接受的那一边：把非游戏的 Unity 应用当成游戏，
只是少陪看一次；反过来认不出游戏才是这一轮要修的 bug。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from brain.llm.factory import LLMFactory
from brain.llm.search import build_query
from core.config import Config
from core.events import EventBus
from core.worker import run_async
from life.psych import crisis
from life.screen.notes import YES_WORDS
from life.screen.scene_guard import KIND_GAME

logger = logging.getLogger(__name__)

# 进程名 → 展示用的游戏名。键统一小写（比对时也把前台进程名降成小写）。
#
# 挑人的原则：**只收"前台进程名足够独特、不会误伤"的**。所以：
#   · 不收 javaw.exe / unityplayer.exe 这类通用宿主（它们是任何 Java/Unity 程序，
#     认成游戏会把一堆工具软件误判）；
#   · 不收浏览器小游戏（进程还是浏览器，认不出来，也承认认不出来）。
GAME_PROCS: dict[str, str] = {
    # 竞技 / MOBA
    "league of legends.exe": "英雄联盟",
    "leagueclient.exe": "英雄联盟",
    "valorant.exe": "无畏契约",
    "valorant-win64-shipping.exe": "无畏契约",
    "dota2.exe": "DOTA2",
    "cs2.exe": "反恐精英 2",
    "csgo.exe": "CS:GO",
    "overwatch.exe": "守望先锋",
    "pubg.exe": "绝地求生",
    "tslgame.exe": "绝地求生",
    "apex_legends.exe": "Apex 英雄",
    "r5apex.exe": "Apex 英雄",
    "rainbowsix.exe": "彩虹六号",
    "genshinimpact.exe": "原神",
    "yuanshen.exe": "原神",
    "starrail.exe": "崩坏：星穹铁道",
    "bh3.exe": "崩坏 3",
    "zzz.exe": "绝区零",
    "hk4e.exe": "原神",
    # 单机 / 沙盒 / 建造
    "minecraft.exe": "我的世界",
    # 注意：Java 版我的世界跑在 javaw.exe 里，进程名认不出来（见模块开头的挑人原则），
    # 只有基岩版（minecraft.exe）认得出。这是这张表认不全的一个实例，不是疏漏
    "terraria.exe": "泰拉瑞亚",
    "stardew valley.exe": "星露谷物语",
    "dontstarve_steam.exe": "饥荒",
    "cuphead.exe": "茶杯头",
    "hollow_knight.exe": "空洞骑士",
    "eldenring.exe": "艾尔登法环",
    "sekiro.exe": "只狼",
    "darksoulsiii.exe": "黑暗之魂 3",
    "baldurs gate 3.exe": "博德之门 3",
    "bg3.exe": "博德之门 3",
    "cyberpunk2077.exe": "赛博朋克 2077",
    "witcher3.exe": "巫师 3",
    "rdr2.exe": "荒野大镖客 2",
    "gta5.exe": "侠盗猎车手 5",
    "gtav.exe": "侠盗猎车手 5",
    "civilizationvi.exe": "文明 6",
    "cities.exe": "城市：天际线",
    "factorio.exe": "异星工厂",
    "satisfactory.exe": "幸福工厂",
    "sims4.exe": "模拟人生 4",
    "starbound.exe": "星界边境",
    "noita.exe": "女巫",
    "deadcells.exe": "死亡细胞",
    "slaythespire.exe": "杀戮尖塔",
    "isaac-ng.exe": "以撒的结合",
    "papers please.exe": "请出示证件",
    "human fall flat.exe": "人类一败涂地",
    "overcooked2.exe": "胡闹厨房 2",
    "it takes two.exe": "双人成行",
    "ittakestwo.exe": "双人成行",
    "palworld.exe": "幻兽帕鲁",
    "lethal company.exe": "致命公司",
    "repo.exe": "REPO",
    # 写实 / 电影感剧情（v2-mood-detect）：这类游戏画面写实，最容易跟电影搞混，而且
    # 压抑/紧张/打斗的氛围最容易被画面腿误判成用户"愤怒"。补进库，前台命中即按游戏走，
    # 不弹观影、也不弹愤怒。
    "gow.exe": "战神",
    "ds.exe": "死亡搁浅",
    "detroitbecomehuman.exe": "底特律：变人",
    "re2.exe": "生化危机 2",
    "re3.exe": "生化危机 3",
    "re4.exe": "生化危机 4",
    "re7.exe": "生化危机 7",
    "re8.exe": "生化危机 8",
    "sottr.exe": "古墓丽影：暗影",
    "rotr.exe": "古墓丽影：崛起",
    "metroexodus.exe": "地铁：离去",
    "hellblade.exe": "地狱之刃",
    "quantumbreak.exe": "量子破碎",
    "acorigins.exe": "刺客信条：起源",
    "acodyssey.exe": "刺客信条：奥德赛",
    "acvalhalla.exe": "刺客信条：英灵殿",
    "watchdogs.exe": "看门狗",
    "watchdogs2.exe": "看门狗 2",
    "watchdogslegion.exe": "看门狗：军团",
    "bf1.exe": "战地 1",
    "bfv.exe": "战地 5",
    "bf2042.exe": "战地 2042",
    "cod.exe": "使命召唤",
    "modernwarfare.exe": "使命召唤",
    "lifeisstrange.exe": "奇异人生",
    "horizonzerodawn.exe": "地平线：零之曙光",
    "horizonforbiddenwest.exe": "地平线：西之绝境",
    "aplaguetaleinnocence.exe": "瘟疫传说：无罪",
    "aplaguetalerequiem.exe": "瘟疫传说：安魂曲",
    "alanwake.exe": "心灵杀手",
    "alanwake2.exe": "心灵杀手 2",
    "ghostoftsushima.exe": "对马岛之魂",
    "tlou-i.exe": "最后生还者",
    # 音游 / 独立
    "osu!.exe": "osu!",
    "rhythmdector.exe": "Rhythm Doctor",
    # ---- v2-game-fix 补表 ----
    #
    # 上一轮只收了"写实/电影感"那一小撮，真机上玩别的照样被当成看电影。这一批按
    # **Steam 上真有人玩、且进程名足够独特**来收，重心放在两类最容易触发误判的：
    # 画面写实的第一/第三人称大作（最像电影），以及画面上有大量 UI 的网游/竞技。
    # 收的标准跟上面一样：**进程名是一个游戏专有的**，通用宿主（javaw / unityplayer /
    # 各种 launcher）一律不收。
    #
    # 国产与 Steam 热门（用户的机器上最可能出现的）
    "blackmythwukong.exe": "黑神话：悟空",
    "b1-win64-shipping.exe": "黑神话：悟空",
    "narakabladepoint.exe": "永劫无间",
    "crossfire.exe": "穿越火线",
    "dnf.exe": "地下城与勇士",
    "kartrider.exe": "跑跑卡丁车",
    "wow.exe": "魔兽世界",
    "wowclassic.exe": "魔兽世界（经典）",
    "ffxiv.exe": "最终幻想 14",
    "ffxiv_dx11.exe": "最终幻想 14",
    "mhw.exe": "怪物猎人：世界",
    "monsterhunterworld.exe": "怪物猎人：世界",
    "monsterhunterrise.exe": "怪物猎人：崛起",
    "hogwartslegacy.exe": "霍格沃茨之遗",
    "starfield.exe": "星空",
    "armoredcore6.exe": "装甲核心 6",
    "liesofp.exe": "匹诺曹的谎言",
    "sifu.exe": "师父",
    "dyinglight2.exe": "消逝的光芒 2",
    "hitman3.exe": "杀手 3",
    "spiderman.exe": "漫威蜘蛛侠",
    "deathstranding.exe": "死亡搁浅",
    "diablo iv.exe": "暗黑破坏神 4",
    "diabloiv.exe": "暗黑破坏神 4",
    "destiny2.exe": "命运 2",
    "warframe.exe": "星际战甲",
    "warframe.x64.exe": "星际战甲",
    "pathofexile.exe": "流放之路",
    "pathofexile_x64.exe": "流放之路",
    "rocketleague.exe": "火箭联盟",
    "brawlhalla.exe": "英灵乱战",
    "team fortress 2.exe": "军团要塞 2",
    "hl2.exe": "Source 引擎游戏",
    "rust.exe": "Rust",
    "dayz.exe": "DayZ",
    "arma3_x64.exe": "武装突袭 3",
    "7daystodie.exe": "七日杀",
    "valheim.exe": "英灵神殿",
    "vrising.exe": "夜族崛起",
    "raft.exe": "木筏求生",
    "subnautica.exe": "深海迷航",
    "phasmophobia.exe": "恐鬼症",
    "amongus.exe": "我们之中",
    "fallguys_client_game.exe": "糖豆人",
    "oxygennotincluded.exe": "缺氧",
    "rimworldwin64.exe": "边缘世界",
    "stellaris.exe": "群星",
    "ck3.exe": "十字军之王 3",
    "eu4.exe": "欧陆风云 4",
    "eurotrucks2.exe": "欧洲卡车模拟 2",
    "assettocorsa.exe": "神力科莎",
    "beamng.drive.exe": "BeamNG.drive",
    "forzahorizon5.exe": "极限竞速：地平线 5",
    "hades.exe": "哈迪斯",
    "hades2.exe": "哈迪斯 2",
    "celeste.exe": "蔚蓝",
    "undertale.exe": "传说之下",
    "deltarune.exe": "三角符文",
    "baba is you.exe": "Baba Is You",
    "ori.exe": "奥日",
    "fc24.exe": "EA SPORTS FC 24",
    "fc25.exe": "EA SPORTS FC 25",
    "fifa23.exe": "FIFA 23",
    "pes2021.exe": "实况足球 2021",
    # 手游模拟器里跑的游戏还是模拟器的进程名，认不出来——不收
}

# 平台 / 启动器：前台是它们**不代表在玩**，用来排除。也别删它们——
# 不排除的话，"挂着 Steam 逛商店"会被当成在玩游戏。
LAUNCHER_PROCS: frozenset[str] = frozenset({
    "steam.exe", "steamwebhelper.exe", "steamservice.exe",
    "wegame.exe", "wegamehelper.exe", "tencentdl.exe",
    "epicgameslauncher.exe", "epicwebhelper.exe",
    "battle.net.exe", "battle.net helper.exe", "agent.exe",
    "ubisoftconnect.exe", "upc.exe", "uplay.exe",
    "eadesktop.exe", "origin.exe", "gog galaxy.exe", "galaxyclient.exe",
    "riotclientservices.exe", "riotclientux.exe", "riotclientcrashhandler.exe",
    "rockstar games launcher.exe", "socialclubhelper.exe",
    "playnite.desktopapp.exe", "itch.exe",
})

# 通用运行时宿主：进程名是**宿主**，不是游戏本体，认成游戏会误伤一大片。
# 跟启动器一个道理——只用来**排除**，而且要比启动器更狠：它们不该进任何一条线索
# （进程表 / 窗口类名 / 安装路径 / 画面运动 / 无边框全屏），不然一个全屏的 Python 工具
# 就会被「无边框全屏」那条最弱的线索认成《pythonw》。真机就栽在这上面（v2-social-reply-fix）。
GENERIC_HOST_PROCS: frozenset[str] = frozenset({
    # Python（无控制台宿主 pythonw / 带控制台 python，含常见的 3 后缀变体）
    "python.exe", "pythonw.exe", "python3.exe", "python3w.exe",
    # Java（java.exe / javaw.exe——Java 版我的世界也认不出，见 GAME_PROCS 挑人原则）
    "java.exe", "javaw.exe",
    # Node 运行时（Electron 窗口类名本就不收，进程名这边再堵一道）
    "node.exe",
})


# ---------- 第二条线索：游戏窗口的**窗口类名**（v2-game-fix） ----------
#
# 为什么需要它。`GAME_PROCS` 是一张等值表，注定认不全——尤其 Unreal / Unity 那批，
# 进程名各随各的项目（`MyProj-Win64-Shipping.exe`、`Whatever.exe`），没有任何规律
# 可循，一个个往表里堆是补不完的。但**引擎会给自己的窗口注册一个固定的类名**，
# 跟项目叫什么都不相干：只要那款游戏跑在这个引擎上，类名就对得上。
#
# 键是**窗口类名原样**（`GetClassNameW` 拿到的字符串，**大小写敏感**——类名是程序
# 注册时写的常量，同一个类名不会有大小写变体，所以这里不做降级比对）。
#
# **只收引擎自己注册的类名。** 认得出是哪款游戏最好，认不出是哪款也没关系——这里的
# 用途只有"确认这是游戏"，名字由 `GAME_PROCS` 那份表负责（类名命中时给不出游戏名，
# 攻略那条线自然就不问，见 `monitor` 那边的接法）。
#
# **刻意不收的**（收了就是灾难）：
#   · `Chrome_WidgetWin_1` —— 那是 Chrome / Edge / 一切 Electron 应用共用的类名
#     （VS Code、Discord、微信…）。收进来等于"所有 Electron 应用都是游戏"。
#     这是本表最要紧的一条禁忌：**窗口类名里凡是"某个 UI 框架的"都要放过**，
#     只收"某个游戏引擎的"；
#   · `SunAwtFrame` —— 一切 Java Swing 桌面程序（IDE、工具、Java 版 MC 之外的一大片）
#     都是它，理由同上。
#
# SDL / GLFW / LWJGL 这一档是踩在界上的：它们既是游戏引擎、也是通用框架，收它们
# **是有意的**——用这几套写出来的窗口，绝大多数就是游戏，而且这一档判错的代价
# （少陪看一次）比漏判（对着游戏冒"看电影啦"）轻得多。真出问题，删这几行即可。
GAME_WINDOW_CLASSES: frozenset[str] = frozenset({
    # Unity 独立版：`UnityWndClass` 是 Unity Player 的固定类名
    "UnityWndClass",
    # Unreal Engine 4/5：`UnrealWindow` —— 本表最有价值的一条，UE 游戏没有统一的
    # 进程名（各随项目命名），但有统一的类名
    "UnrealWindow",
    # Valve Source / Source 2（CS2、DOTA2、军团要塞…共用）
    "Valve001",
    # Riot 自家引擎（英雄联盟、无畏契约）
    "RiotWindowClass",
    # CryEngine
    "CryENGINE",
    # 通用框架那一档（见上面的取舍说明）
    "SDL_app",
    "GLFW30",
    "LWJGL",
    # Godot（v2-game-activate 加）。**这一条没在真机验过**：Godot 在 Windows 上到底
    # 注册成固定类名还是随项目名，我没有把握。留着它是因为**验不到也无害**——它只是
    # 五条线索里的一条，错了顶多少认一个游戏，不会把非游戏认成游戏；真机验出不对
    # 就删掉这一行，Godot 那批由安装路径（Steam 目录）与本地名单两条兜住。
    "Godot",
})


def game_of(proc: str) -> str | None:
    """前台进程是不是某个游戏；是就返回游戏名，不是（含认不出来）就 None。

    大小写不敏感：前台拿到的进程名大小写不保证（`League of Legends.exe` 与
    `LeagueClient.exe` 在同一次会话里都可能出现），统一降成小写再比。
    """
    name = (proc or "").strip().lower()
    if not name or name in LAUNCHER_PROCS or name in GENERIC_HOST_PROCS:
        return None
    return GAME_PROCS.get(name) or None


def is_game_window_class(class_name: str) -> bool:
    """这个**窗口类名**是不是游戏引擎自己注册的那个（v2-game-fix）。

    大小写**敏感**，理由见表上的注释（类名是注册时写死的常量，不存在大小写变体；
    这里跟着 `GetClassNameW` 原样比，免得把 `sdl_app` 这种拼法也放进来）。

    **它不返回游戏名**——类名只说得清"哪个引擎"，说不清"哪款游戏"。想知道名字
    得靠 `game_of` 那份进程表。所以这条线索的实际用法只有一种：**确认这是游戏**，
    用来把认不出名字的全屏游戏从"影视兜底"里捞出来（见 `monitor.is_watch_foreground`）。
    """
    return (class_name or "").strip() in GAME_WINDOW_CLASSES


def is_launcher(proc: str) -> bool:
    """前台是不是游戏平台/启动器。给测试与排查用，判定游戏态不靠它。"""
    return (proc or "").strip().lower() in LAUNCHER_PROCS


# ---------- 第三条线索：exe 的**安装路径**（v2-game-activate） ----------
#
# 为什么这条路值得走。前面两张表都是"认名字"——认进程名、认窗口类名，都要事先知道
# 那个游戏叫什么。而**主流发行平台把游戏装在哪儿是有约定的**：Steam 一律落在
# `steamapps\common\` 下，Epic 落在 `Epic Games\` 下，等等。于是不用知道游戏叫什么，
# 只看 exe 躺在哪个目录，就能判出"这是个游戏"——**这一条才是"通杀"的主力**，
# 因为它不认识名字也能命中（RE9、以及将来任何一款新游戏，装在 Steam 库里就认得）。
#
# 只收**游戏专有**的目录。刻意不收的（收了误伤面太大）：
#   · `\WindowsApps\` —— 微软商店装的东西全在这儿，绝大多数不是游戏；
#   · 光一个 `\Games\` —— 太泛，谁都能建一个叫 Games 的文件夹；
#   · `\Tencent\` / `\Program Files\` 这类大目录 —— 什么都有。
# 也不收 `\steamapps\`（不带 common）：那个目录下还有 `workshop`（创意工坊内容、
# 常由别的程序打开）与 `shadercache`，游戏本体在 common 里。
#
# 写法说明：路径一律**先归一成正斜杠再比**，所以这里的标记也用正斜杠。别写成
# `r"\steamapps\common\\"` —— 那是**原始字符串**，末尾的 `\\` 就是两个反斜杠，
# 真实路径里永远不会有，这条标记会一次都命中不了（写这一节时踩过一次）。
GAME_PATH_MARKERS: tuple[str, ...] = (
    "/steamapps/common/",        # Steam（含副库 /SteamLibrary/steamapps/common/）
    "/epic games/",              # Epic 默认装到 C:/Program Files/Epic Games/<Title>
    "/ubisoft/ubisoft game launcher/games/",   # Ubisoft Connect
    "/ubisoft/",
    "/gog galaxy/games/",        # GOG Galaxy
    "/gog games/",
    "/riot games/",              # 英雄联盟 / 无畏契约装在 C:/Riot Games/
    "/electronic arts/",         # EA
    "/ea games/",
    "/origin games/",
    "/xboxgames/",               # PC Game Pass 默认装到 C:/XboxGames/<Title>
    "/battle.net/",              # 暴雪（多数暴雪游戏装在自家目录，这条只覆盖装这儿的）
    "/wegameapps/",              # 腾讯 WeGame
)


# ---------- 第五条线索的反面：**外壳窗口**一定不是游戏（v2-game-activate） ----------
#
# 这一张是**黑名单**，跟 `GAME_WINDOW_CLASSES` 那张白名单方向相反——白名单收"某个游戏
# 引擎的类名"，这一张只收"Windows 外壳自己的"类名。两边的中间地带（某个 UI 框架的
# 类名，比如 `Chrome_WidgetWin_1`）照旧按"认不出"处理。
#
# 为什么非要这一张：**桌面本身就是个无边框铺满的窗口**（`Progman`，样式 `0x96000000`，
# 标题栏 / 可缩放边框 / 系统菜单三个位一个不留），跟全屏游戏的窗口样式长得一模一样。
# 真机回归时逮到过一次：前台是桌面时"无边框铺满"那条线索把桌面判成了游戏。
SHELL_WINDOW_CLASSES: frozenset[str] = frozenset({
    "Progman",                    # 桌面（Program Manager）—— 就是逮到的那一个
    "WorkerW",                    # 桌面壁纸宿主（Win10/11 上不止一个）
    "Shell_TrayWnd",              # 主任务栏
    "Shell_SecondaryTrayWnd",     # 副屏任务栏
    "NotifyIconOverflowWindow",   # 托盘溢出面板
    "TaskListThumbnailWnd",       # 任务栏预览缩略图
    "Windows.UI.Core.CoreWindow", # 开始菜单 / 系统面板那一圈 UWP 壳
})


def is_shell_window_class(class_name: str) -> bool:
    """这个窗口类名是不是**外壳自己的**（桌面 / 任务栏 / 开始菜单那一圈）。

    v2-game-activate 真机回归时逮到的：**Windows 桌面就是一个"无边框铺满"的窗口**
    ——类名 `Progman`，窗口样式 `0x96000000`，标题栏 / 可缩放边框 / 系统菜单三个位
    一个都不留，跟全屏游戏的窗口样式长得**一模一样**。用户点一下桌面，最后那条
    "无边框铺满"的线索就会把桌面判成游戏（真机实测：`GameHit(name='explorer',
    source='fullscreen-exclusive')`）。

    生产链路上桌面其实被前面的 `_known_non_video`（`explorer.exe` 在名单里）拦住了，
    **但那道闸认的是进程名**——只要有任何一个外壳窗口挂在别的进程名底下，这条就漏了。
    所以在**窗口这一层**再设一道，跟名字那道互不依赖。

    这一条是"**一定不是游戏**"的**黑名单**，跟 `GAME_WINDOW_CLASSES` 那条白名单
    方向相反：白名单只收"某个游戏引擎的"，黑名单只收"Windows 外壳自己的"，
    两边都不碰"某个 UI 框架的"（那类仍然走"认不出"）。
    """
    return (class_name or "").strip() in SHELL_WINDOW_CLASSES


def looks_like_game_path(exe_path: str) -> bool:
    """这个 exe 的**安装路径**是不是主流游戏平台的目录（v2-game-activate）。

    大小写不敏感；反斜杠先归一成正斜杠再比（psutil 在 Windows 上给的是反斜杠，
    测试与别的调用方可能给正斜杠）。空串 → False（"查不到路径"当没这条线索）。
    """
    if not exe_path:
        return False
    text = exe_path.replace("\\", "/").lower()
    return any(marker in text for marker in GAME_PATH_MARKERS)


def exe_stem(exe_path: str, proc: str = "") -> str:
    """给游戏取一个**粗名字**：exe 的主干名（`re9.exe` → `re9`）。

    认不出是哪款游戏时用它当名字（v2-game-activate）——问句要有东西可填，攻略检索
    也要有关键词。名字糙是糙，但**用户在本名单里写一行 `re9.exe = 生化危机 9`，
    这里立刻就用上本名了**，不用等仓库更新。取不到路径时退回进程名主干，都没有就空串。
    """
    raw = (exe_path or "").replace("/", "\\")
    if raw:
        raw = raw.rsplit("\\", 1)[-1]
    raw = raw or (proc or "")
    if raw.lower().endswith(".exe"):
        raw = raw[:-4]
    return raw.strip()


# ---------- 第四条线索：用户自己的本地名单 ----------
#
# 表能补，但补的速度永远赶不上新游戏与冷门游戏。这一份是**给用户自己写的**：
# 放在数据根目录（本机是 `E:\电子宠物\game_procs.txt`），启动时读一次，跟内置两张表
# 一起生效。格式（每行一条，`#` 开头与空行忽略）：
#
#     re9.exe = 生化危机 9            ← 进程名（大小写不敏感）
#     class:Godot = 某游戏            ← 窗口类名（大小写敏感，跟程序注册的一样）
#     title:生化危机 = 生化危机 9      ← 窗口标题关键词（**要 screen.read_browser_title 开着**）
#
# 三处刻意的设计：
#   · **等号两边都留着空格**也能解析（`x = y` / `x=y` 都行），写错格式的行跳过、
#     不报错、不拦启动——一份用户手写的名单不该把程序弄崩；
#   · `title:` 那条要读窗口标题，所以**跟着既有的那道破例总闸**
#     （`screen.read_browser_title`）走：关掉总闸，标题关键词这一档整个不参与，
#     其余两档照常。不在游戏这条线上另开一个新的破例口子；
#   · **不落盘、不回写**。这份名单只读，程序一个字都不往里写。
LOCAL_RULES_FILE = "game_procs.txt"
# 标题关键词最多这么长就截断——用户手写的东西不设上限，匹配时别拿一个超长串去扫。
LOCAL_TITLE_MAX = 64


@dataclass(frozen=True)
class GameHit:
    """一次游戏识别的结论：是什么游戏 + **哪条线索认出来的**。

    `name` 可能是本名（表里查到的）、也可能是 exe 主干名那种粗名字（认不出名字时）。
    `source` 只给日志与测试看，不进任何判定。
    """

    name: str
    source: str


class LocalGameRules:
    """用户本地名单（`game_procs.txt`）的解析与匹配。

    **读不到文件不是错误**：没有这份文件（绝大多数用户不会写）就是一份空名单，
    所有匹配都给 None。解析只在 `load()` 那一下做一次，之后每次查询都是查字典/遍历
    那几条标题词——判定跑在每拍采样上，这里不能有文件 IO。
    """

    def __init__(
        self,
        procs: dict[str, str] | None = None,
        classes: dict[str, str] | None = None,
        titles: tuple[tuple[str, str], ...] = (),
    ):
        self.procs = dict(procs or {})
        self.classes = dict(classes or {})
        self.titles = tuple(titles)

    def __len__(self) -> int:
        return len(self.procs) + len(self.classes) + len(self.titles)

    @classmethod
    def load(cls, path) -> "LocalGameRules":
        """读一份名单。文件不在 / 读不动 / 里面写乱了，都退回空名单，**不抛**。"""
        procs: dict[str, str] = {}
        classes: dict[str, str] = {}
        titles: list[tuple[str, str]] = []
        try:
            text = Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.debug("本地游戏名单读不到（%s）：%s", path, exc)
            return cls()
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if not key or not value:
                continue
            if key.lower().startswith("class:"):
                # 类名大小写敏感：程序注册时写的是什么就是什么
                classes[key[len("class:"):].strip()] = value
            elif key.lower().startswith("title:"):
                word = key[len("title:"):].strip()[:LOCAL_TITLE_MAX]
                if word:
                    titles.append((word.lower(), value))
            else:
                procs[key.lower()] = value
        logger.info(
            "本地游戏名单已并入：%d 条进程名 / %d 条窗口类 / %d 条标题词（%s）",
            len(procs), len(classes), len(titles), path,
        )
        return cls(procs, classes, titles)

    def match_proc(self, proc: str) -> str | None:
        return self.procs.get((proc or "").strip().lower())

    def match_class(self, window_class: str) -> str | None:
        return self.classes.get((window_class or "").strip())

    def match_title(self, title: str) -> str | None:
        """标题里出现某个关键词就算命中。**只在总闸开着时调用方才会传标题进来。**"""
        text = (title or "").lower()
        if not text:
            return None
        for word, name in self.titles:
            if word in text:
                return name
        return None


def recognize(
    proc: str,
    *,
    window_class: str = "",
    exe_path: str = "",
    title: str = "",
    borderless_fullscreen: bool = False,
    motion_game: bool = False,
    rules: "LocalGameRules | None" = None,
) -> GameHit | None:
    """把五条线索收成一个结论（v2-game-activate）。**认不出就返回 None。**

    顺序是有意的，从"最确凿"到"最弱"，**先命中先返回**：

    1. **启动器先排掉**（`LAUNCHER_PROCS`）：挂着 Steam 逛商店不是"在玩"——
       这一条排在最前面是有意的：Epic 的启动器自己就装在 `Epic Games` 那个目录里，
       安装路径那条线索会把**它自己**认成游戏，只有靠这条先把它拦住；
    2. **内置进程表**（`GAME_PROCS`）→ 有本名；
    3. **用户本地名单**（进程名 / 窗口类名 / 标题关键词）→ 有本名；
    —— 到这儿为止都是"认得出名字"的，下面四条只给粗名字 ——
    4. **引擎窗口类名**（`GAME_WINDOW_CLASSES`）→ 只有粗名字；
    5. **安装路径**（`GAME_PATH_MARKERS`）→ 只有粗名字。**"通杀"靠的就是这条**：
       它不认识游戏名字也能命中；
    6. **画面动得像游戏**（`motion_game`，调用方已经保证是全屏）→ 只有粗名字。
       沿用 v2-game-fix 那一层，堵"进程名、窗口类名、安装路径三样都不认识"的游戏；
    7. **外壳窗口排掉**（`SHELL_WINDOW_CLASSES`，黑名单）：桌面 / 任务栏 / 开始菜单。
       它卡在最后一条前面，是**专为最后一条设的**——见下面那条；
    8. **无边框全屏**（`borderless_fullscreen`）→ 只有粗名字。**这几条里最弱的一条，
       弱到它根本不看画面、不看名字**，只看"这个窗口连标题栏都没有、铺满整屏"。
       留着它是因为现代游戏大量用无边框全屏，而播放器/浏览器/办公在前几档就被拦掉了。
       它是**唯一一条可能把非游戏认成游戏的**，第 7 步那张外壳黑名单就是它捅出来的
       窟窿堵上的（桌面 `Progman` 的样式是 `0x96000000`，跟全屏游戏一模一样）。

    **调用方负责先过 `_known_non_video` 硬闸**（工作 / 社交 / 摸鱼 / 休息 / explorer）：
    那才是"办公全屏绝不当游戏"的兜底（PowerPoint 放幻灯也是无边框全屏），这里不重复
    判一次——两处都判会导致两处口径各说各话，将来改一处忘一处。
    """
    name = (proc or "").strip().lower()
    if not name or name in LAUNCHER_PROCS or name in GENERIC_HOST_PROCS:
        return None
    known = GAME_PROCS.get(name)
    if known:
        return GameHit(known, "process")
    if rules is not None:
        local = rules.match_proc(name) or rules.match_class(window_class) \
            or rules.match_title(title)
        if local:
            return GameHit(local, "local")
    if is_game_window_class(window_class):
        return GameHit(exe_stem(exe_path, name), "window-class")
    if looks_like_game_path(exe_path):
        return GameHit(exe_stem(exe_path, name), "install-path")
    if motion_game:
        return GameHit(exe_stem(exe_path, name), "motion")
    # 最后那条线索**只看窗口样式**，所以只堵它一个：外壳自己的窗口（桌面 `Progman`、
    # 任务栏那几张）天生就是无边框铺满的，样式跟全屏游戏一模一样，不摘出去就是误判。
    # 摆在这么靠后的位置是有意的——前面那几条看的是**进程**（名字 / 安装路径）与
    # **画面**（运动），外壳类名管不着它们，别顺手动它们。
    if is_shell_window_class(window_class):
        return None
    if borderless_fullscreen:
        return GameHit(exe_stem(exe_path, name), "fullscreen-exclusive")
    return None


# ---------- 问那一句 ----------

ASK_LINE = "看你开了一把《{game}》，要我帮你翻份攻略吗？"
GIVING_LINE = "我去翻翻《{game}》…稍等我一下。"

# "过一会儿再问"（v2-scene-state）：前台一切到游戏就先冒开场白（「我陪你一起玩游戏」），
# 隔 `config.screen.guide_ask_delay_sec`（默认 30 秒）再问攻略。取这个数是**实测一拍
# 约 5 秒采样 + 4 秒识别**那个尺度上的"过一会"：开场白那条气泡（9 秒）早散了，用户
# 也刚坐定，两句话不挤在一起。0 = 老口径（一切到游戏就问，直接 new 服务时是这个）。
GUIDE_ASK_DELAY_MS = 30_000
# 本地与联网都没东西时，**如实说没找着**——不编一份出来（跟知识库那条一个态度）
NOT_FOUND_LINE = (
    "《{game}》我这边没翻到现成的攻略，你自己搜搜看？"
    "或者你告诉我卡在哪一步，我陪你想想。"
)

# ---------- S2 本地起手表 ----------
#
# 键是游戏名（跟 GAME_PROCS 的值一致）。**刻意留空档**：认得出游戏名、这里却没
# 条目的，走联网那条；两条都不行才说"没找着"。所以别往里塞兜底条目，塞了就再也
# 走不到联网那一支了。
#
# 这里只写**不随版本变**的起手话（怎么开局、先做哪件事），**不写**出装/数值/阵容——
# 那种东西写死在仓库里，过两个月就是错的了，而"宠物背了一段过期出装"比"宠物说它
# 得联网查一下"糟得多。想给某个游戏补一条，直接加一行。
LOCAL_TIPS: dict[str, str] = {
    "英雄联盟": "先走一遍训练营把补兵和技能连招摸熟，别急着排位。\n"
              "对线期眼位比人头值钱，被抓两次就学乖了。",
    "DOTA2": "新手先打人机，把「反补」「拉野」这两个词搞明白再进匹配。\n"
             "死了不掉等级只掉钱，前期别硬拼。",
    "无畏契约": "先把准星压到爆头线、练几个常用英雄的技能，再谈上分。\n"
              "开局经济差的时候该省就省，别每回合都买满。",
    "原神": "跟着主线走到能开传送锚点，路上顺手清每日委托和体力，别攒着溢出。\n"
            "元素反应是核心，配队先看能不能打出反应，再看伤害数字。",
    "我的世界": "第一晚之前一定要挖到石头、做石镐和火把，天黑前把洞口堵上。\n"
              "木头永远不嫌多，见到就砍。",
    "泰拉瑞亚": "先砍树做工作台，然后往下挖，第一件事是找生命水晶。\n"
              "别在没装备的时候招惹史莱姆王。",
    "星露谷物语": "第一年别急着赚钱，先把洒水器和背包格子铺开。\n"
                "每天浇水太耗时，早点把浇水的问题解决掉。",
    "空洞骑士": "地图碎片和钉子升级优先，别省那点钱。\n"
              "打不过 Boss 先去别的地图转一圈，回来会轻松很多。",
    "艾尔登法环": "打不过就绕路，这游戏的正确解法经常是「先别打」。\n"
                "骨灰和战灰该用就用，不是逃课。",
    "只狼": "这游戏靠弹反不靠翻滚，先在被虐里把「叮」的节奏听出来。\n"
            "架势条比血条重要。",
    "星穹铁道": "先把主线推到开启日常，体力和模拟宇宙的资源别浪费。\n"
               "配队先看能不能凑齐一套命途，再看强度。",
    "文明 6": "开局先出侦察兵探路，别闷头造建筑。\n"
             "城邦使者前期比什么都划算。",
    "异星工厂": "别追求一步到位，先自动化最基础的一条传送带。\n"
               "手搓是过渡，不是玩法。",
    "模拟人生 4": "先保证工作和睡眠，娱乐条跌到底会很难受。\n"
                 "房子不用一次建好，钱是慢慢攒的。",
    "胡闹厨房 2": "先把一个岗位做熟（切菜/煮/装盘），别所有人都乱跑。\n"
                 "喊出来比闷头做有用得多。",
    "双人成行": "两个人各管一半，谁先急着抢会一起掉下去。\n"
               "卡住的时候看看另一边有没有能踩的机关。",
    "幻兽帕鲁": "早做帕鲁球，抓到干活的帕鲁就把采集自动化起来。\n"
               "晚上冷，先给自己弄件衣服。",
    "杀戮尖塔": "删牌比加牌重要，卡组越薄越稳。\n"
               "前两个精英别硬碰，先看看手里有没有能扛住的牌。",
    "死亡细胞": "先固定一把顺手的武器，别每局换玩法。\n"
               "卷轴的分配认准一条路线加，别平摊。",
}

# 无可靠来源时，模型只回这一句原话（v2-game-guide-check）：不编造、把球踢回给用户，
# 让他补充"卡在哪个场景"。跟 NOT_FOUND_LINE 是两码事——那个是代码自己的兜底（检索失败
# 或空串时 `_on_guide` 塞的），这个是检索"看似成功"但拿不准时、模型该给的自述。
UNSURE_LINE = "我还不太确定，你告诉我场景我帮你查。"

GAME_GUIDE_PROMPT = """用户在电脑上开了一把《{game}》，想找一份能马上用上的攻略。

请联网检索，给 3~5 条**当前还成立**的上手/进阶要点：怎么开局、常见的坑、卡住了先做什么。
每条一句话，短、能直接照做；不要 markdown 标题、不要序号符号、不要客套、不要复述题目。

只写检索得到的真实信息。检索不到可靠出处、或具体关卡/数值/阵容/版本强度拿不准时，
**只回下面这一句原话，一个字都别多**：
"{unsure}"
——**不要凭印象编**具体数值、阵容或版本强度，编出来的攻略比没有更坑人。"""

GAME_GUIDE_SYSTEM = (
    "只输出可以直接照做的短建议，3~5 句简体中文；不要解释、不要写来源编号、不要问问题。"
    "拿不准、没有可靠出处时，只回指定的那一句声明，别硬凑建议。"
)


class GameGuideService(QObject):
    """前台是认得出的游戏时，问一句"要不要攻略"，点头了再去找（**先问再给**）。

    跟刷剧陪伴（`companion.py`）的区别就一条，也是最要紧的一条：那个是**直接冒话**，
    这个**必须先问、等用户点一下**。理由是攻略这件事要往外走一趟（见 `_build`），
    不点头就不该往外发任何东西——这也是为什么它复用抓屏笔记那套轻按钮
    （`bus.game_offer` → 气泡 [好呀] [不用了]），而不是学陪伴那样说完就散。

    三条纪律：

    - **不读屏。** 这条线自己既不截屏也不认字——它认游戏的**五条线索全在采样器那边**
      （进程名 / 本地名单 / 窗口类名 / 安装路径 / 画面运动），需要问 Windows 或要连拍
      的那几条由 `ScreenMonitor.game_hit` 代劳，这里只拿结论。窗口标题只在用户自己
      写了 `title:` 关键词时才被读一次，且**不经过这里**（见 `monitor._game_title`）。
    - **先问再给。** 没点头之前，一个字都不往外发（`_build` 那个联网调用是
      `confirm` 之后才发生的，不是前台一变就发）。
    - **零写入。** 不碰 pet.db / mind.db，不落盘；冷却、当前游戏这些全在内存里。
    """

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        can_offer: Callable[[], bool] | None = None,
        clock: Callable[[], float] | None = None,
        guard=None,
        rules: "LocalGameRules | None" = None,
        ask_delay_ms: int | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        # "过一会儿再问"（v2-scene-state）：默认跟着配置走（`screen.guide_ask_delay_sec`，
        # 30 秒）——游戏态的开场白（「我陪你一起玩游戏」）先说出口，攻略问句等它说完再问，
        # 两句不挤在同一个气泡里。传一个具体毫秒数就写死（0 = 老口径：一切到游戏就问）。
        # 跟 `guide_cooldown_min` 一样**每次要用时现读**，不是构造时抄一份：装配完再改
        # 配置（测试就是这么干的）当场生效。
        self._ask_delay_override = None if ask_delay_ms is None else max(0, int(ask_delay_ms))
        self._ask_timer = QTimer(self)
        self._ask_timer.setSingleShot(True)
        self._ask_timer.timeout.connect(self._on_ask_due)
        self._pending = ""          # 认清、正等着过一会儿再问的那个游戏
        self._seen = ""             # 最近一次前台认出来的游戏（等延迟时用来复核还在不在）
        # 别在别人正等着用户点按钮的时候插一脚（气泡上那排按钮只有一份）
        self._can_offer = can_offer if can_offer is not None else (lambda: True)
        # 场景互斥守卫（v2-mood-detect）：游戏这条线既**喂**它（当前是哪个游戏），
        # 也**问**它（专注在跑就别弹攻略）。默认 None = 没接线，照旧放行。
        self._guard = guard
        self._clock = clock if clock is not None else time.monotonic
        # 用户本地游戏名单（v2-game-activate）：跟采样器共用同一份，由 bootstrap 读一次
        # 两边各塞一个。**没接线时**（测试直接 new 出来用）退回一份空名单，认名字只靠
        # 内置表——行为跟加这一档之前逐字相同。
        self._rules = rules if rules is not None else LocalGameRules()
        # 挂上采样器之后才有"五条线索"里的后四条；没挂也能跑（只有进程名与本地进程名）。
        self._monitor = None
        # 只存内存：每个游戏上次问过是什么时候、当前正问哪个、等没等答复
        self._last_asked: dict[str, float] = {}
        self._game = ""
        self._awaiting = False

    # ---------- 接线 ----------

    def attach(self, monitor) -> None:
        """挂到采样器上：信号给前台换人，引用用来回问"这是哪款游戏"。

        v2-game-activate 改了口径（原来这里**不留** monitor 引用）：现在还要问窗口类名、
        exe 安装路径、无边框全屏与画面运动，而 pid、连拍那套东西都在采样器身上——
        不握着它就等于把这四条线索全丢了。留着引用不构成环：`game.py` 不 import
        monitor 的任何东西，采样器反过来只用延迟 import 引本模块。
        """
        self._monitor = monitor
        monitor.foreground_changed.connect(self.on_foreground)

    # ---------- 冒话 ----------

    @property
    def enabled(self) -> bool:
        return bool(self._config.screen.enabled and self._config.screen.guide_enabled)

    @property
    def ask_delay_ms(self) -> int:
        """攻略问句晚多少毫秒问（0 = 一切到游戏就问）。"""
        if self._ask_delay_override is not None:
            return self._ask_delay_override
        sec = getattr(self._config.screen, "guide_ask_delay_sec", None)
        if sec is None:
            return GUIDE_ASK_DELAY_MS
        return max(0, int(sec)) * 1000

    @property
    def awaiting_confirm(self) -> bool:
        """已经问出口"要不要攻略"，正等用户答复。"""
        return self._awaiting

    @property
    def game(self) -> str:
        """正问着的是哪个游戏（空串表示没在问）。"""
        return self._game

    def on_foreground(self, proc: str) -> None:
        """前台换人了：是认得出的游戏、又过了冷却，就问一句。

        认游戏这一下走 `ScreenMonitor.game_hit`（挂上了的话），五条线索一起判；
        没挂采样器时退回"只有进程名与本地进程名"那两档——测试直接 new 出来的就是这么跑的。

        等答复期间的处理（v2-game-react）：**还是同一个游戏**就不重复问；**切到了
        另一个游戏**，上一个游戏的问句作废，重新识别这个——不然"玩着 A 弹了问句、
        没点按钮就切到 B"时，B 会被上一问的等待态挡掉，看着像"切换游戏没反应"。
        """
        game = self._name_of(proc)
        self._seen = game
        if game != self._pending:
            # 换了个游戏 / 不在游戏里了：还等着的那一问作废（v2-scene-state）。
            # 放在守卫那段之外：没接线（测试直接 new）时这条作废同样得生效。
            self._ask_timer.stop()
            self._pending = ""
        if self._guard is not None:
            # 先如实登记"前台现在是哪个游戏"（空串 = 不在游戏里），再问该不该开口。
            # 登记要在问之前：守卫那边 FOCUS 压着 GAME，所以专注时 active 仍是 focus，
            # `allow(KIND_GAME)` 自然给否——不用在这儿自己判一次专注态。
            self._guard.set_game(game)
        if not self.enabled:
            return
        if not game:
            return
        if self._awaiting:
            if game == self._game:
                return          # 还在等同一个游戏的答复，不重复问
            self._awaiting = False
            self._game = ""     # 切到别的游戏了：上一个问句作废
        delay_ms = self.ask_delay_ms
        if delay_ms > 0:
            # 等开场白说完再问（v2-scene-state）。还等着同一个游戏就什么都不做，
            # 别把计时一直往后推——那样切来切去就永远问不出口。
            if self._pending == game and self._ask_timer.isActive():
                return
            self._pending = game
            logger.debug("游戏态认出来了（%s），过 %.0f 秒再问要不要攻略",
                         game, delay_ms / 1000)
            self._ask_timer.start(delay_ms)
            return
        self._ask(game)

    def _on_ask_due(self) -> None:
        """"过一会儿"到点了：还在同一个游戏上就真问，切走了这一问作废。"""
        game = self._pending
        self._pending = ""
        if not game or game != self._seen:
            logger.debug("攻略问句作废：这期间前台已经不是《%s》了", game or "?")
            return
        self._ask(game)

    def _ask(self, game: str) -> None:
        """问出口"要不要攻略"（排轻按钮）。该不该问的几道闸都在这儿再判一次。

        等待期间可能已经变了（专注起来了 / 别的线正等着用户点按钮 / 这个游戏刚问过），
        所以要在这里复核，而不是在排计时器那一刻判完就算。
        """
        if not self.enabled or not game:
            return
        if self._guard is not None and not self._guard.allow(KIND_GAME):
            # 专注番茄钟正跑着：这条攻略问句不冒（v2-mood-detect 的场景互斥）。
            logger.debug("攻略问句不发：这会儿是 %s 说了算（场景互斥）",
                         self._guard.active)
            return
        if self._awaiting or self._pending:
            return
        if not self._can_offer():
            return
        if not self._due(game):
            return
        # 台词是写死的模板，不可能踩到危机词——但红线不跳步（跟 companion.py 同款）：
        # 真踩到了就不冒这句话，也不露按钮。将来往模板里加东西时不会漏。
        if crisis.is_crisis(ASK_LINE.format(game=game)):
            logger.warning("攻略问句命中危机词，跳过这一句")
            return
        self._game = game
        self._awaiting = True
        self._last_asked[game] = self._clock()
        logger.debug("游戏态认出来了（%s），问一句要不要攻略", game)
        self._bus.game_offer.emit(game)

    def _name_of(self, proc: str) -> str:
        """这个前台算哪款游戏（空串 = 不算）。**五条线索都收口在这儿。**

        挂上采样器时整个交给 `ScreenMonitor.game_hit`——剩下四条线索（窗口类名 /
        安装路径 / 无边框全屏 / 画面运动）全要 pid、要问 Windows、要连拍，只有它手上
        有。认不出名字的游戏拿到的是一**粗名字**（exe 主干名，`re9.exe` → `re9`），
        问句照这个填——用户在本名单里写一行 `re9.exe = 生化危机 9` 就换成本名了。
        """
        monitor = self._monitor
        if monitor is not None:
            try:
                hit = monitor.game_hit(proc)
            except Exception as exc:      # noqa: BLE001 —— 认游戏出错不该把整条线带崩
                logger.warning("认游戏出错，这一档按“不是游戏”算：%s", exc)
                return ""
            return hit.name if hit is not None else ""
        named = recognize(proc or "", rules=self._rules)
        return named.name if named is not None else ""

    def _due(self, game: str) -> bool:
        """这个游戏这会儿该不该再问一遍（按游戏各自冷却）。"""
        last = self._last_asked.get(game)
        if last is None:
            return True
        cooldown = max(0, int(self._config.screen.guide_cooldown_min)) * 60
        return self._clock() - last >= cooldown

    # ---------- 点头才给 ----------

    def confirm(self, answer: str) -> bool:
        """用户答复了"要不要攻略"。点头才去翻，别的都当不要。返回这次给没给。

        只有点头才会走到 `_build`——**"往外发东西"这件事完全发生在这一步之后**。
        """
        game = self._game
        self._awaiting = False
        self._game = ""
        if not game or not self._is_yes(answer):
            return False
        self._bus.pet_speak.emit(GIVING_LINE.format(game=game))
        run_async(
            self._build,
            game,
            on_done=partial(self._on_guide, game),
            on_failed=partial(self._on_failed, game),
        )
        return True

    def _build(self, game: str) -> str:
        """后台线程里找攻略：**先本地，本地没有才联网**。

        本地那条是仓库里写死的起手话（`LOCAL_TIPS`），不联网、不外发、秒回；
        它没有这个游戏时才走联网检索那条（`LLMFactory.create_search`，用的是用户
        在设置页里配好的那条检索通道，跟工作卡片用的是同一条）。

        两条都不行，返回空串——由 `_on_guide` 换成"没找着"的交代。**绝不编一份出来**。
        """
        tip = LOCAL_TIPS.get(game)
        if tip:
            return tip
        answer = LLMFactory.create_search(self._config).ask_multi(
            GAME_GUIDE_PROMPT.format(game=game, unsure=UNSURE_LINE),
            GAME_GUIDE_SYSTEM,
            queries=(
                build_query(game, "PC", "攻略"),
                build_query(game, "新手", "避坑", "技巧"),
            ),
        )
        return (getattr(answer, "text", "") or "").strip()

    def _on_guide(self, game: str, text: str) -> None:
        self._bus.game_guide.emit((text or "").strip() or NOT_FOUND_LINE.format(game=game))

    def _on_failed(self, game: str, exc: Exception) -> None:
        """找攻略失败了（多半是没配检索通道的 Key）。**如实说没找着**，别编。"""
        logger.error("找攻略失败（%s）：%s", game, exc)
        self._bus.game_guide.emit(NOT_FOUND_LINE.format(game=game))

    @staticmethod
    def _is_yes(answer: str) -> bool:
        a = (answer or "").strip()
        return any(word in a for word in YES_WORDS)


__all__ = [
    "ASK_LINE", "GAME_GUIDE_PROMPT", "GAME_GUIDE_SYSTEM", "GAME_PATH_MARKERS",
    "GAME_PROCS", "GAME_WINDOW_CLASSES", "GENERIC_HOST_PROCS", "GIVING_LINE",
    "GUIDE_ASK_DELAY_MS", "GameGuideService",
    "GameHit", "LAUNCHER_PROCS", "LOCAL_RULES_FILE", "LOCAL_TIPS",
    "LocalGameRules", "NOT_FOUND_LINE", "SHELL_WINDOW_CLASSES", "UNSURE_LINE",
    "exe_stem", "game_of", "is_game_window_class", "is_launcher", "is_shell_window_class",
    "looks_like_game_path", "recognize",
]
