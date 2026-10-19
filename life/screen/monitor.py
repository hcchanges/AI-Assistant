"""
屏幕感知（S2）：前台窗口/进程轮询 + OCR 字幕关键词粗判。

一拍做三件事：
1. 问一次前台进程名（复用 monitor/foreground.py，只取进程名，不碰窗口标题）；
2. 只有前台"像在放视频"时才截画面底下那条字幕带，交给 OCR；
3. 识别出的字在关键词表里数命中，命中够多、且连着几拍是同一个场景，才算这个场景成立。

关键词表（OCR 字幕这条线认的三态；画面情绪那条腿见 mood_cv）：

    鬼 / 血 / 杀 → 恐怖    怒 / 骂 / 战 → 愤怒
    喜 / 囍 / 婚 → 笑容
    没有命中任何一个 → 这一拍不成立，安静待着

幽默这一档 v2-mood-detect 已删（搞笑判别押后迭代、由笑容替代）；哭泣那一档
v2-mood-tri 整态删除（连"哭 / 泪"这串字幕关键词一并断，不留半条腿）。愤怒**只**在
字幕这条线上认——画面腿不判愤怒，见 mood_cv。

一拍的顺序里还夹着一道闸（S4 扩展）：前台是浏览器时先看一眼标题，
标题说是"网课 / 在线创作"就**整拍跳过**——既不进场景，也不截屏（人上课，
没有理由再去看人家屏幕）。这条是隐私破例的唯一落点，见 life/screen/browser.py。

几个刻意的取舍：
- **一条命中不算数**：中文里"喜"（喜欢）、"战"（挑战）、"血"（血拼）都很常见，
  单条命中误报太多。所以要求同一拍里命中数最高的那一态，再投进**一分钟多数票**
  （见 life/screen/vote.py）——60 秒里出现最多的那一态才算最终判定；不满窗口、
  平票、一票都没有都不输出。**没把握的那一拍不投票**（v2-mood-recall-fix：
  弃权只作废这一拍，不进窗口稀释）；**散场只由硬证据触发**（换了前台 / 不是视频 /
  空白屏 / 自己的窗口），不再靠"猜不出来的拍数过半"。
- **只在视频类前台应用上 OCR**：别的时候（写文档、看网页）一拍就过。
  这既是省算力，也是少碰隐私——没有理由的时候不去看人家的屏幕。
- **OCR 在后台线程跑**：实测一次识别约 4 秒（1920×300 的字幕带），
  摆界面线程上必卡；界面线程只做"截屏 + 转数组"这两步快活，
  上一拍还没认完就直接跳过这一拍，不往线程池里堆任务。
- **结果只存内存**：画面与文字都是这一拍用完就丢，只留一个场景字符串在对象里。

画面情绪（CV 腿，见 mood_cv / mood_fuse）：上面那条 OCR 认的是**字幕字面**，画面里
没字就什么都认不到。所以又加了一条认**画面样子**的腿——本机的中文 CLIP 零-shot 判
三态（恐怖/愤怒/笑容），跟 OCR 的结论融合（冲突时精细优先，听 CV 的）。这条腿是**可选**的：
`screen.mood = "off"` 时它整个不参与，采样链与加它之前逐字相同；模型不在时静默退回
只用 OCR。它判读用的那一帧跟 OCR 那一帧**同源同命**——内存里过一遍就丢。

不做的事：不落盘、不写 pet.db。**OCR 那条线至今不调模型**；CV 那条腿在 mood != "off"
时会加载一个本地 ONNX（离线，画面不出本机，见 ScreenSection 的口径说明）。

窗口标题**只在一处破例**：浏览器前台时读一次、当场判成 focus / watch / 空串，
标签留下、标题丢掉（见 life/screen/browser.py 与 screen.read_browser_title 开关）。

游戏态这道闸（v2-game-vs-movie，v2-game-fix 与 v2-game-activate 逐步补齐）一共**六条
线索**，都不认字、不认内容：**原有的进程名**（`life/screen/game.py` 的 `GAME_PROCS`）之外，
这一轮又加了五路——**用户自己的本地名单**（`game_procs.txt`，本机在数据根目录）、
**游戏引擎注册的窗口类名**（`GAME_WINDOW_CLASSES`）、**exe 的安装路径**
（`GAME_PATH_MARKERS`：Steam / Epic / Riot / XboxGames… 这些平台把游戏装在哪儿是有
约定的）、**画面怎么动**（`life/screen/motion.py` 的连拍运动特征），以及最弱的一条
**无边框铺满窗口**（窗口样式三条位一条不留）。

正向与反向**共用同一份口径**（`game.recognize`）：反向是"算不算在看片"
（`is_watch_foreground` / `_watch_candidate`），正向是"要不要进陪玩、弹攻略"
（`GameGuideService`）。两边各判各的迟早会有一天说法不一样。

五条里只有进程名那一条是"认得出名字"的，其余四条都只给一个 **exe 主干名当粗名字**
（`re9.exe` → `re9`）——用户在本名单里写一行 `re9.exe = 生化危机 9` 立刻就用上本名。

**这五条弱线索现在只服务正向那条线**（v2-social-scene-boundary）：反向那边（"算不算
在看片"）改成了**只认名单**——非播放器且非浏览器一律不算，全屏兜底整条删掉。原来那套
"全屏 + 进程名不认识 + 不是已知非视频 → 就当在看片"的后门，放进来的绝大多数是最大化过的
普通窗口（截图工具、设置页、资源管理器），用户真机上"对着没在放片的窗口演电影"就是它。

正向那边不一样：那里问的是"要不要弹攻略"，**漏一个认不出名字的全屏游戏是真损失**，
所以五条线索一条不省。花钱的那条（连抢 4 帧看画面怎么动）仍然只在"全屏 + 进程名不认识
+ 不是播放器/浏览器 + 非视频硬闸"这一小块里才跑；其余几条都只是进程名、类名、exe 路径
上的一次比较或系统调用，每次前台换人都问一遍也花不了什么。
"""

from __future__ import annotations

import logging
import os
import threading
import time

from PySide6.QtCore import QObject, QRect, QSize, QTimer, Signal
from PySide6.QtGui import QCursor, QGuiApplication, QImage

from core.config import Config
from core.worker import run_async
from life.screen import shot
from life.screen import motion
from life.screen.browser import TITLE_FOCUS, TITLE_WATCH, classify_title, is_browser
from life.screen.emotion_backend import (
    BACKEND_JEV,
    BACKEND_LLM,
    ENV_BACKEND,
    EmotionJudge,
    LocalBackend,
    load_env as load_emotion_env,
)
from life.screen.mood_cv import MoodCvReader, MoodVerdict
from life.screen.mood_fuse import fuse
from life.screen.mood_cloud import MoodCloudReader
from life.screen.vote import SceneVoter
from life.screen.scenes import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_ORDER,
    SCENE_TITLES,
)
from monitor import foreground

logger = logging.getLogger(__name__)

# ---------- 三态 ----------
#
# 三个标识符的**定义**搬到了 life/screen/scenes.py：CV 那条线也要用它们当类别标签，
# 两边互相 import 会成环，所以落到那个谁都不依赖的叶子上。这里 import 进来之后
# 名字照旧绑在 monitor 的命名空间里，原先 `from life.screen.monitor import SCENE_HORROR`
# 的写法一个都不用改。SCENE_KEYWORDS 是 OCR 这条线自己的，留在本地。
#
# v2-mood-tri：哭泣（SCENE_CRY）整态删除，标识符与它的关键词一并没了。

SCENE_KEYWORDS: dict[str, tuple[str, ...]] = {
    # 恐怖这一档补了一批**无歧义的多字词**（v2-mood-fix）：单字"血"要画面里真出现
    # 那个字才认得到，可"尸体""谋杀"这种词整句里一个单字关键词都不带，原来整条漏掉。
    # 只收不会误伤的：像"死"这种（"笑死我了""死机"）一律不进表。
    SCENE_HORROR: ("鬼", "血", "杀", "尸体", "尸首", "血腥", "割喉", "凶杀", "谋杀", "惨死", "残肢"),
    # v2-mood-anger：愤怒这一档原来只有三个**单字**（"怒""骂""战"），一票就判。
    # "战"在"挑战/观战/备战/商战"里到处都是，"怒"在"怒放/怒目"里也有——于是全屏的
    # 办公页/网页/聊天窗里随手撞上一个字，宠物就冒一句愤怒共情（用户真机：聊着
    # 红烧肉/手艺被弹了「这也能气着？来，我陪你骂两句」）。
    #
    # 现在只收**光看词面就知道是在发火**的词（用户任务书 ① 的"明确愤怒 + 置信度达阈值"：
    # 一个整词的愤怒表达就是这一档的置信度闸，单字不算）。恐怖那一档的单字照旧保留——
    # "鬼""血""杀"不出现在日常文本里，误判代价也只是"我怕怕"；愤怒认错了却是
    # 宠物没头没脑地替人发火，两档的风险不对等。
    SCENE_ANGRY: ("愤怒", "生气", "气死", "气人", "气炸", "火大", "恼火", "来气",
                  "光火", "火冒三丈", "骂人", "怒骂", "吵起来"),
    SCENE_FESTIVE: ("喜", "囍", "婚"),
    # v2-mood-screen ⑤ 曾经在这里加过哭泣那一档（靠字幕里的"哭/泪/抽泣"字面）。
    # v2-mood-tri 整态删除：这条线**不是留着不管**，是整串关键词拔掉——留着它，
    # 字幕里一个"哭"字就还会判出那个已经不存在的态，见 `_count_scene` 的兜底闸。
}

# 每态够几票才算数（v2-mood-fix）。**恐怖这一档要保住 1**：用户同时报了"恐怖血腥
# 漏判"，收紧它等于把漏判做得更漏。恐怖词本身也够具体（"鬼""血""杀"基本不会出现在
# 日常对话里）。
#
# v2-mood-detect：幽默这一档连同它"要两票"的特例一起删了——搞笑判别押后迭代、由笑容
# 替代。愤怒这一档**仍留在这里**（字幕认"怒/骂/战"），因为愤怒共情只认文本/字幕，
# 画面腿不判愤怒。
SCENE_MIN_HITS: dict[str, int] = {
    SCENE_HORROR: 1,
    SCENE_ANGRY: 1,
    SCENE_FESTIVE: 1,
}

# 字幕带：屏幕底下这一截。硬字幕一般在底部 1/4 里，取 28% 留点余量。
SUBTITLE_BAND_RATIO = 0.28
SUBTITLE_BAND_MIN_H = 90      # 但再窄也要有这么高，不然 720p 以下裁得太狠

# 这些前台应用才值得 OCR：播放器与浏览器（刷剧多半在这两类里）。
# 播放器那份跟 monitor/category.py 的"娱乐"类对齐；浏览器单独列，因为它们在
# 内置分类表里归"其他"，但确实是刷剧的主战场。
WATCH_PROCS: frozenset[str] = frozenset({
    # 播放器
    "potplayer.exe", "potplayermini.exe", "potplayermini64.exe", "vlc.exe",
    "mpc-hc64.exe", "mpc-hc.exe", "mpv.exe", "mpv.net.exe", "mpc-be64.exe",
    "mpc-be.exe", "kmplayer.exe", "kmplayer64.exe", "gomplayer.exe",
    "quicktimeplayer.exe", "qqplayer.exe", "bilibili.exe", "wsmplayer.exe",
    # v2-mood-detect：本地播放器五花八门，认不出来的时候只能靠"全屏"这条兜底，
    # 而兜底要等窗口真铺满屏——用户真机上"本地播放器识别延迟"多半就是窗口还没
    # 最大化那一段。把常见的一批补齐，认得出进程名的第一拍就开始看画面。
    "mplayer.exe", "smplayer.exe", "mpcstar.exe", "qqplayer64.exe",
    "pptv.exe", "pplive.exe", "iqiyi.exe", "qyclient.exe", "youku.exe",
    "tudou.exe", "mgtv.exe", "migu.exe", "sohuvideo.exe", "lebo.exe",
    "xmp.exe", "xlplayer.exe", "thunderplayer.exe", "avplayer.exe",
    "videolan.exe", "mpcbe.exe",
    # 浏览器
    "chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe",
    "360se.exe", "360chrome.exe", "sogouexplorer.exe", "quarkpc.exe",
})


def is_watchable(proc: str | None) -> bool:
    """这个前台应用值不值得看一眼字幕。"""
    return bool(proc) and proc.lower() in WATCH_PROCS


# ---------- 非视频硬闸（v2-mood-detect 再审·终） ----------
#
# 原来"在看片"有一条**全屏兜底**：进程名认不出来、但窗口铺满一块屏，就按"在看片"算。
# 这条兜底是给"认不全的本地播放器"留的后门，可它也把一堆**最大化/全屏的非视频窗口**
# 放进了门——ima / 桌面 / 办公 / 聊天，随手一开就是满屏，于是宠物对着聊天窗、知识库
# 乱飘「太上老君」「太气人」那套影视状态词。
#
# 这一轮把门关死：**已知不是视频的进程，铺满全屏也不许进**。判据是分类表
# （monitor/category.py）——工作 / 社交 / 摸鱼 / 休息这四类，再全屏也不会是在放片。
# "娱乐"（播放器/游戏/音乐）不拦，游戏另由 `_is_game` 单独否。
#
# **兜底整条删掉了**（v2-social-scene-boundary，用户拍板"照原话删"）：原先"认不出的
# 进程 + 铺满全屏"还能进，理由是"给认不全的本地播放器留后门"。真机上这条后门放进来的
# 全是不该进的——**认不出名字的又铺满屏的窗口，绝大多数是最大化过的普通窗口**
# （真独占全屏的游戏本来就被无边框/路径/类名那几条线索接住了），而 WATCH_PROCS 里
# 已经收了常见播放器与全部主流浏览器，剩下"认不出名字的播放器"是极少数。代价不对等：
# 漏一个冷门播放器只是"该陪看时没陪"，误判一次就是对着截图工具、设置页、资源管理器
# 冒一句「我陪你看电影」。现在**非播放器且非浏览器一律不放行**。
_NON_VIDEO_CATEGORIES = frozenset({"工作", "社交", "摸鱼", "休息"})
# 这两张名单是"分类表之外"的补充：它们要么不在分类表里（explorer 自己就是桌面），
# 要么在分类表里只算"其他"（系统自带的小工具大多如此）。
_NON_VIDEO_PROCS = frozenset({
    # explorer.exe 单独列：它是"桌面 / 资源管理器"，不在分类表里（它自己就是桌面）。
    "explorer.exe",
    # ---- 截图 / 录屏 / 系统工具（v2-social-scene-boundary）----
    # 这些是"随手开就铺满一屏"的重灾区，也是删掉全屏兜底之后**仍然**要单独列出来的
    # 一批：它们不只是"别当成在看片"，还从 `game_hit` 那道闸里被一并挡掉——不然对着
    # 截图工具会问"要不要攻略"。名字只收**只可能指这一个软件**的（老规矩）。
    # 截图：Win10 截图工具 / Win11 截图工具的真实进程名 / 旧版截取工具 / 各家第三方
    "snippingtool.exe", "screensketch.exe", "screenclip.exe",
    "snipaste.exe", "sharex.exe", "greenshot.exe", "pixpin.exe", "fscapture.exe",
    # 录屏 / 直播工具
    "obs.exe", "obs64.exe", "obs32.exe", "bandicam.exe", "xbxgamebar.exe",
    "gamebar.exe", "gamebarftserver.exe", "gamebarpresencewriter.exe",
    # 系统自带的小工具：放大镜 / 画图 / 计算器 / 任务管理器 / 设置 / 搜索
    "magnify.exe", "mspaint.exe", "win32calc.exe", "calculatorapp.exe",
    "taskmgr.exe", "systemsettings.exe", "searchapp.exe", "searchhost.exe",
    # 系统外壳窗口（UWP 宿主 / 开始菜单 / 锁屏）——它们也是无边框铺满的常客
    "applicationframehost.exe", "startmenuexperiencehost.exe",
    "shellexperiencehost.exe", "lockapp.exe",
})


def _known_non_video(proc: str | None) -> bool:
    """这个前台进程是不是**已知不是视频**（桌面 / 办公 / 聊天 / 知识库 / 工具…）。

    认不出（"其他"）返回 False——但那**不再等于放行**：`is_watch_foreground` 最后一档
    是 `is_watchable`（播放器 / 浏览器名单），认不出的进不去。这个函数现在干的是两件事：
    ① 硬闸本身（已知非视频的直接否）；② 给 `game_hit` 当同一道闸，免得对着截图工具
    问"要不要攻略"。分类表读不了时按"认不出"返回 False，把判定让给 `is_watchable`
    那道名单闸，不越权放行也不把采样关死。
    """
    key = (proc or "").lower()
    if key in _NON_VIDEO_PROCS:
        return True
    try:
        from monitor.category import resolve_category
        return resolve_category(key, {}) in _NON_VIDEO_CATEGORIES
    except Exception:      # noqa: BLE001 —— 分类表读不了就按"认不出"放行，别把采样关死
        return False


def _empty_rules():
    """一份空的本地游戏名单（v2-game-activate）。没有这份文件时用它顶上。

    延迟 import 的理由跟 `_is_game` 一样：`game.py` 不 import monitor，反过来引它是
    安全的，但放函数里将来两边都不会在模块加载期成环。
    """
    from life.screen.game import LocalGameRules

    return LocalGameRules()


# ---------- 画面运动特征这一层的记账（v2-game-fix） ----------
#
# 判"这一屏动得像不像游戏"要连抢 4 帧、帧间隔 90 毫秒，而且这段是**同步**跑在界面
# 线程上的（见 `_grab_motion_frames`）。**实测整段约 0.5 秒**（本机 2560×1528：
# 4 次抓屏各约 40 毫秒 + 3 段 90 毫秒间隔 = 0.43 秒，加判读 0.07 秒），不是帧间隔
# 那 270 毫秒——抓屏本身才是大头。所以不能每拍都来一次：按**前台进程名**记账，
# 同一个进程这么多秒内只真跑一次。进程一换 key 就不同，直接重判（换了应用就是换了
# 画面，没有复用旧结论的道理）。
#
# 45 秒的来历：比一拍采样间隔（默认 5 秒）长得多，一次判读能覆盖十来拍的采样；又比
# 一次"看片 / 打游戏"的时长短得多，用户中途切回去重开也会重新判。这个数**只影响
# 跑多少次连拍**，不影响判定口径——判错的那一次会在 45 秒内一直被复用，所以宁可
# 取长一点：这一层是**辅助**（进程名与窗口类名才是主力），少跑几次比多跑几次稳。
MOTION_TTL_SEC = 45.0


def is_fullscreen_foreground() -> bool:
    """前台窗口是不是全屏（本地播放器全屏放视频的形态，v2-mood-screen ①）。

    WATCH_PROCS 那份表只收了几家播放器，本地播放器五花八门收不全；而全屏是看片
    最典型的形态，靠"窗口铺满一块屏"就能认出来，不用把每个播放器都列一遍。
    取不到前台窗口 / 没有屏幕信息时给 False——宁可这一拍不采样，也不乱采。
    """
    rect = shot.foreground_window_rect()
    if rect is None:
        return False
    geometries = [s.geometry() for s in QGuiApplication.screens()]
    return shot.is_fullscreen(rect, geometries)


def classify_text(text: str) -> str | None:
    """一句（一屏）字幕文字 → 场景；没有命中就返回 None。

    数命中条数，取最多的那一态；并列时按 SCENE_ORDER 让靠前的先赢。

    **先过每态的最低命中数**（SCENE_MIN_HITS，v2-mood-fix）：没过线的那一态当没说话，
    不参与比较——理由写在 SCENE_MIN_HITS 的注释里。
    """
    if not text:
        return None
    best: str | None = None
    best_hits = 0
    for scene in SCENE_ORDER:
        hits = sum(text.count(word) for word in SCENE_KEYWORDS[scene])
        if hits < SCENE_MIN_HITS.get(scene, 1):
            continue                      # 没够票：这一态这一拍不开口
        if hits > best_hits:
            best, best_hits = scene, hits
    return best


# ---------- 情绪判定后端接线（v2-social-emotion-wire） ----------
#
# 只有环境里**显式**写了 `EMOTION_BACKEND=jev|llm` 才走这条接线；不设、写 `local`、
# 写错值，一律维持原来的本地逻辑（MoodCvReader / MoodCloudReader）——既有行为与
# 类型断言逐字不变。显式指定是"用户自己开的口子"，不是默认路径。


def _emotion_env() -> dict[str, str]:
    """读情绪后端的开关（`os.environ` 优先，项目根 `.env` 补缺）。测试可替换。"""
    return load_emotion_env()


def _forced_backend(values: dict[str, str]) -> str:
    """显式指定的后端名；没指定 / `local` / 写错值都返回空串。"""
    name = str(values.get(ENV_BACKEND) or "").strip().lower()
    return name if name in (BACKEND_JEV, BACKEND_LLM) else ""


class EmotionMoodReader:
    """把 `EmotionJudge` 适配成 monitor 那条腿的形状（available / classify_array）。

    下游（mood_fuse → 一分钟多数票 → 场景状态机）拿到的仍是 `MoodVerdict`，所以这里
    只做两件事：把 state 递进去、把统一结构收回三态。`valence` / `arousal` /
    `intention_hint` 原样留在 `last_verdict` 上供调试看——现有融合层不用它们，也不用改。
    """

    def __init__(self, judge: EmotionJudge) -> None:
        self.judge = judge
        # jev / llm 把这一拍的字幕一起看，比只喂一帧更准。
        self.wants_text = True
        self.last_verdict = None

    @property
    def name(self) -> str:
        return str(getattr(self.judge, "name", "") or "")

    @property
    def available(self) -> bool:
        try:
            return bool(self.judge.available())
        except Exception:      # noqa: BLE001 —— 问能不能用出岔子就是不能用
            return False

    def download_async(self, *args, **kwargs) -> bool:
        """与 MoodCvReader 同形：这条腿没有本地权重可下。"""
        return False

    def classify_array(self, array, text: str | None = None) -> MoodVerdict | None:
        """一帧（+ 这一拍的字幕）→ `MoodVerdict`。判不了返回 None，由调用方兜底。"""
        state: dict[str, object] = {}
        if array is not None:
            state["image"] = array
        if text:
            state["text"] = str(text)
        try:
            verdict = self.judge.judge(state)
        except Exception as exc:      # noqa: BLE001 —— 后端炸了这一拍当没判
            logger.info("情绪后端 %s 这一拍没成（%s）", self.name or "?", exc)
            return None
        if verdict is None:
            return None
        self.last_verdict = verdict
        scene = (
            verdict.emotion_label
            if verdict.emotion_label in (SCENE_HORROR, SCENE_ANGRY, SCENE_FESTIVE)
            else None
        )
        score = float(verdict.confidence) if scene else 0.0
        scores = {scene: score} if scene else {}
        return MoodVerdict(scene, score, scores)


class OcrReader:
    """RapidOCR 的懒加载包装：没装就降级成"读不出字"，不报错、不拖慢启动。

    引擎第一次真要用的时候才建（建一次要几秒），建不起来就记一笔、以后不再试。
    """

    def __init__(self) -> None:
        self._engine = None
        self._failed = False
        # 建引擎一次要好几秒（RapidOCR 要加载检测 + 识别两个模型）。加锁是为了让
        # "启动时预热"与"第一拍真识别"这两条路不会同时去建第二个引擎（v2-mood-detect）。
        self._lock = threading.Lock()

    @property
    def available(self) -> bool:
        return self._engine is not None or not self._failed

    def warm_up(self) -> bool:
        """把引擎在后台先建起来（v2-mood-detect）。建好了返回 True。

        **为什么值得单独做这一手**：第一次真识别要先把模型读进来，实测十几秒；
        用户的体感就是"放了半天宠物才反应过来"。把这份开销从"第一拍"挪到"启动时"，
        识别那一步就只剩纯推理。建不起来（没装 / 出错）返回 False，那条路照旧降级。
        """
        return self._ensure() is not None

    def read(self, image: QImage) -> str:
        """认出图里的字，拼成一行；认不出（没装 / 出错）返回空串。"""
        array = _to_numpy(image)
        if array is None:
            return ""
        return self.read_array(array)

    def read_array(self, array) -> str:
        """认一个已经转成数组的画面。

        采样链走的是这一条：QImage → 数组在界面线程做完（只是拷内存，很快），
        真正耗时的识别丢到后台线程去（实测一次约 4 秒，摆界面线程上必卡）。
        """
        return _extract_text(self.read_raw(array))

    def read_lines(self, array) -> list:
        """认字，但**连带字框一起**回（v2-social-ocr-isolate）。

        社交那条线要知道"这一行字背后是哪个颜色的气泡"，没有框就无从取色——所以它走
        这一条而不是 `read_array`（那条把框丢掉了）。形状由
        `life.screen.social_window.parse_ocr_lines` 统一成 `[(框, 字), ...]`。
        没装 / 出错都回空表，由调用方当"没认到"处理。
        """
        from life.screen.social_window import parse_ocr_lines
        return parse_ocr_lines(self.read_raw(array))

    def detect_boxes(self, array):
        """**只跑检测**、不跑识别（v2-social-offer-fast-latency）：回字框列表；没装 / 出错给 None。

        跟 `read_raw` / `read_lines` 共用同一个引擎、同一份懒加载，只是把识别那一环关掉
        （RapidOCR 的 `use_rec=False`）——检测是整趟里最便宜的一环，识别才占那几秒的大头。
        社交那条线拿它当"这一屏像不像会话总列表"的**廉价先验**：命中就整趟省掉识别。

        返回的是检测环的原始形状（每个框是一串四点多边形 `[[x, y], ...]`），坐标已换算回
        **原图**（RapidOCR 自己在 `__call__` 里做过这一步）。认成空屏（一个框都没有）回
        `[]`——跟"没接引擎"的 `None` 分得开。
        """
        engine = self._ensure()
        if engine is None or array is None:
            return None
        try:
            result = engine(array, use_det=True, use_cls=False, use_rec=False)
        except Exception as exc:      # noqa: BLE001 —— 检测失败不该把认字那条线带崩
            logger.debug("OCR 只检测失败: %s", exc)
            return None
        if isinstance(result, tuple) and len(result) == 2:
            result = result[0]
        return result if result is not None else []

    def read_raw(self, array):
        """把 OCR 引擎的**原始返回**交出去；没装 / 出错给 None。

        单独留这一层是为了让"要字"（`read_array`）与"要框"（`read_lines`）共用同一次
        识别，不必为了拿框再跑一遍那 4 秒。
        """
        engine = self._ensure()
        if engine is None or array is None:
            return None
        try:
            return engine(array)
        except Exception as exc:      # OCR 出错不该把整条采样链带崩
            logger.debug("OCR 识别失败: %s", exc)
            return None

    def _ensure(self):
        with self._lock:
            return self._ensure_locked()

    def _ensure_locked(self):
        if self._engine is not None or self._failed:
            return self._engine
        try:
            from rapidocr_onnxruntime import RapidOCR   # 新版本可能叫 rapidocr

            self._engine = RapidOCR()
        except Exception:
            try:
                from rapidocr import RapidOCR          # 新版包名

                self._engine = RapidOCR()
            except Exception as exc:
                self._failed = True
                logger.info("没装 RapidOCR，屏幕感知的字幕识别这一环先停着：%s", exc)
        return self._engine


def _to_numpy(image: QImage):
    """QImage → numpy 三通道数组。没装 numpy 就返回 None。"""
    try:
        import numpy as np
    except ImportError:
        return None
    rgb = image.convertToFormat(QImage.Format_RGB888)
    width, height = rgb.width(), rgb.height()
    if width <= 0 or height <= 0:
        return None
    buffer = rgb.constBits()
    # bytesPerLine 会按 4 字节对齐，比 width*3 宽，得自己裁掉行尾的填充
    raw = bytes(buffer)[: rgb.bytesPerLine() * height]
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, rgb.bytesPerLine())
    return rows[:, : width * 3].reshape(height, width, 3)


def _extract_text(result) -> str:
    """把 RapidOCR 的返回搓成一行字。两代包返回的形状不一样，都认。"""
    if result is None:
        return ""
    # 旧版： (list[[box, text, score]], elapsed)
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], (list, type(None))):
        result = result[0]
    if result is None:
        return ""
    # 新版：结果对象上挂着 .txts
    texts = getattr(result, "txts", None)
    if texts is not None:
        return " ".join(str(item) for item in texts)
    parts: list[str] = []
    for line in result:
        if isinstance(line, (list, tuple)) and len(line) >= 2:
            parts.append(str(line[1]))
    return " ".join(parts)


def grab_subtitle_band(pet_rect: QRect | None = None) -> QImage | None:
    """截当前屏幕底下那条字幕带；宠物自己那块涂白，免得对着猫认字。

    取不到画面（非 Windows / 没有屏幕）返回 None。
    """
    # 挑屏看**前台窗口在哪**，不看光标（v2-mood-fix，跟 shot.pick_screen_index 同一条
    # 规则）：电影全屏放在左边那块屏、鼠标搁在主屏时，原来会去抓主屏，抓到的整条字幕带
    # 是另外一屏的画面，字幕一个都认不到。取不到前台窗口时才退回光标那块屏（老行为）。
    rect = shot.foreground_window_rect()
    if rect is None:
        rect = QRect(QCursor.pos(), QSize(1, 1))
    grabbed = shot.grab_screen_for(rect)
    if grabbed is None:
        return None
    image, geometry = grabbed
    band_h = max(SUBTITLE_BAND_MIN_H, int(geometry.height() * SUBTITLE_BAND_RATIO))
    band_h = min(band_h, image.height())
    band = image.copy(0, image.height() - band_h, image.width(), band_h)

    if pet_rect is not None and not pet_rect.isNull() and not pet_rect.isEmpty():
        local = pet_rect.translated(-geometry.left(), -geometry.top())
        local = local.translated(0, -(image.height() - band_h))
        _whiten(band, local)
    return band


def _whiten(image: QImage, rect: QRect) -> None:
    """把 rect 那块涂白（宠物自己别进 OCR）。"""
    clipped = rect.intersected(QRect(0, 0, image.width(), image.height()))
    if clipped.isEmpty():
        return
    from PySide6.QtGui import QColor, QPainter

    painter = QPainter(image)
    painter.fillRect(clipped, QColor(255, 255, 255))
    painter.end()


# 前台快轮询的间隔（毫秒）。前台切换（专注↔桌面/电影/游戏进出）要"立即响应"，不能等
# sample_sec（默认 5 秒）那一路——用户真机：切回桌面了宠物还傻站着几秒。500ms 是几次
# 系统调用（取前台窗口 + 进程名，浏览器再读一次标题），开销可以忽略。
FOREGROUND_POLL_MS = 500


class ScreenMonitor(QObject):
    """前台轮询 + 字幕识别的采样器。

    三个出口信号：
    - foreground_changed(proc)：前台应用换人了（空串表示取不到）；
    - title_changed(kind)：浏览器标题的判类变了（"focus" / "watch" / ""），
      只有浏览器前台才可能发；同一个浏览器里换标签页时靠它通知；
    - scene_changed(scene)：场景成立或散场了（散场发空串）。
    """

    foreground_changed = Signal(str)
    title_changed = Signal(str)
    scene_changed = Signal(str)

    def __init__(
        self,
        config: Config,
        reader: OcrReader | None = None,
        grabber=None,
        mood: MoodCvReader | None = None,
        cv_grabber=None,
        motion_grabber=None,
        game_rules=None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._reader = reader if reader is not None else OcrReader()
        self._grabber = grabber if grabber is not None else grab_subtitle_band
        # 画面情绪那条腿：本机 ONNX 优先，档位是 cloud 时换成走云端的那一个
        # （但它自己会把"没授权"挡掉，见 mood_cloud 的三道闸）。
        self._mood = mood if mood is not None else self._build_mood()
        self._cv_grabber = cv_grabber if cv_grabber is not None else shot.grab_foreground_window
        # 连拍取帧（v2-game-fix）：画面运动特征那一层要的是**一串**帧，不是一帧。
        # 做成可注入的，测试里塞一个"取不到帧"的桩，免得真机抓屏把测试搞成不确定。
        self._motion_grabber = (
            motion_grabber if motion_grabber is not None else self._grab_motion_frames
        )
        # 运动判读的记账：(进程名, 到期时刻, 结论)，只存内存、只留最近一次
        self._motion_cache: tuple[str, float, bool] | None = None
        # 用户本地游戏名单（v2-game-activate）：本机是 `E:\电子宠物\game_procs.txt`，
        # 启动时由 bootstrap 读一次塞进来。**没塞就是一份空名单**，判定行为跟没有这份
        # 文件时逐字相同——它是"兜底"，不是"必需"。
        self._game_rules = game_rules if game_rules is not None else _empty_rules()
        self._pids = foreground.ProcNames()
        self._reading = False        # 上一次识别还没回来就先不发新的，别把线程池堆满
        # 一帧的复用（v2-mood-detect）：一拍里 OCR 那条腿与画面情绪那条腿都要看同一路
        # 画面，各抓各的就把一次截屏花成两次。这里给每次取帧发一个序号，**同一个序号内
        # 只真抓一次**，后来的那次拿现成的。序号每一拍往前走，所以永远不会拿到上一拍的
        # 旧帧——画面变了，判据跟着变。（v2-social-scene-boundary 删掉空白闸之后，序号
        # 只剩 `sample` 一处往前推了。）
        self._frame_seq = 0
        self._frame = None
        self._frame_key = -1

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.sample)
        # 前台快轮询（v2-scene-switch）：前台切换要即时响应，不能等 sample_sec（默认 5 秒）。
        # 只问"前台是谁、浏览器标题判类"，几次系统调用、便宜又快；耗时的 OCR/截图仍在
        # sample_sec 那一路跑。
        self._fg_timer = QTimer(self)
        self._fg_timer.setInterval(FOREGROUND_POLL_MS)
        self._fg_timer.timeout.connect(self._poll_foreground)

        # 以下是"只存内存"的全部状态：场景、多数票窗口、当前前台进程、浏览器标题的判类。
        # 标题本身**不存**——读完当场判成 focus / watch / ""，只留这三个标签之一。
        self._pet_rect = lambda: None
        self._proc = ""
        self._fg_pid = 0             # 这一拍前台窗口的 pid（v2-mood-anger：用来认自己）
        self._title_kind = ""
        self._scene = ""
        # 一分钟多数票（v2-mood-detect 再审·终）：替掉原来那套"连击"。窗口宽度每拍
        # 从配置现读（见 _count_scene），所以测试随手改 scene_vote_window_sec 就生效。
        self._voter = SceneVoter(self._vote_window())
        # 这一拍两条腿各说了什么（调试/测试看，只存内存）
        self._last_fusion = None
        self._apply_interval()

    def _build_mood(self):
        """按 `screen.mood` 档位建那条腿。off 时返回 None（整条腿不参与）。

        `EMOTION_BACKEND=jev|llm` 显式指定时优先走可插拔后端；没配齐钥匙/地址就退回
        下面原本的本地 / 云端逻辑，不报错、也不改变默认行为。
        """
        values = _emotion_env()
        forced = _forced_backend(values)
        if forced:
            judge = EmotionJudge.from_config(self._config, env=values)
            if not isinstance(judge, LocalBackend):
                logger.info("画面情绪：按 EMOTION_BACKEND 启用 %s 后端", judge.name)
                return EmotionMoodReader(judge)
            logger.info("画面情绪：EMOTION_BACKEND=%s 没配齐（钥匙/地址），退回本地档", forced)
        mode = str(getattr(self._config.screen, "mood", "off") or "off").strip().lower()
        if mode == "off":
            return None
        if mode == "cloud":
            return MoodCloudReader(self._config)
        if mode != "local":
            logger.warning("screen.mood 认不出的档位 %r，按 off 处理", mode)
            return None
        return MoodCvReader(
            self._config.local_models_dir,
            min_score=getattr(self._config.screen, "mood_min_score", 0.70),
        )

    # ---------- "这一档算不算在看片"（v2-mood-detect） ----------

    def is_watch_foreground(self, proc: str) -> bool:
        """前台这一档算不算"在看片"。**只认名单。认不出就是不看片。**

        三档，一道比一道松：

        0. **是游戏就直接否**（v2-game-vs-movie）：游戏窗口多半也是全屏，光看"铺没铺满"
           根本分不出"在放片"还是"在打游戏"，真机上"把游戏识别成电影"就是这么来的。
           进程名在 `GAME_PROCS`、或在**用户本地名单**里，就按游戏算，不进这条路；
        1. **已知不是视频的进程直接否**（v2-mood-detect 再审·终）：工作 / 社交 / 摸鱼 /
           休息 / explorer / 截图录屏系统工具——办公全屏、聊天窗、知识库、截图工具
           铺满一屏也**不许**按看片算；
        2. 进程名在播放器/浏览器名单里（`is_watchable`）→ 算。**只有这一档能点头。**

        **原先的第 3、4 档（全屏兜底 + 空白闸）整条删掉了**（v2-social-scene-boundary，
        用户拍板"照原话删兜底"）：那两档合起来是"进程名不认识 + 铺满屏 + 画面不空白
        → 就当在看片"。它的立足点是"给认不全的本地播放器留个后门"，可真机上这条后门
        放进来的几乎全是**最大化过的普通窗口**——用户对着截图工具、设置页全屏时被冒
        一句「我陪你看电影」，就是它。而它想救的那一小撮（认不出名字的播放器）本来
        也极少：WATCH_PROCS 收了常见播放器与全部主流浏览器，认不出的漏判代价只是
        "该陪看时没陪"，误判的代价却是"对着没在放片的窗口演电影"——**不对等，所以门
        关死**。"非播放器且非浏览器则不放行"，没有例外。

        删掉之后 `_is_game_foreground`（窗口类名 / 安装路径 / 画面运动 / 无边框那几条
        反向线索）与 `_foreground_blank` 都没了调用方，一并删掉。**那几条线索本身没有
        消失**：它们仍在正向那条线（`game_hit` → `game.recognize`）上跑，管的是"要不要
        进陪玩、弹攻略"——那件事上"认不出名字的全屏游戏"是真要救的，判错的代价也不
        一样。正反两边本来就不是同一种误判。
        """
        if not proc:
            return False
        if self._is_game(proc):
            return False
        if _known_non_video(proc):
            return False
        return is_watchable(proc)

    def _is_game(self, proc: str) -> bool:
        """这个前台进程是不是**认得出名字**的游戏（v2-game-vs-movie / v2-game-activate）。

        两条来源：内置进程表（`GAME_PROCS`）与**用户本地名单的进程名那一档**。都只
        认名字、不花钱（一次字典查），所以能排在全链最前面。本地名单里另外两档
        （窗口类名 / 标题关键词）要问 Windows，放在 `_game_hit` 里一起问。

        延迟 import：`life/screen/game.py` 那条线自己不 import monitor，这里反过来
        import 它是安全的；但放在函数里更稳——将来 game.py 哪天要引 monitor 的东西，
        也不会在模块加载期成环。
        """
        from life.screen.game import game_of

        if game_of(proc) is not None:
            return True
        return bool(self._game_rules.match_proc(proc))

    def _game_hit(self, proc: str, motion_game: bool = False):
        """把"要问 Windows / 要读 exe"的那几条线索收成一个结论，给 `GameHit` 或 None。

        结论本身由 `game.recognize` 给（窗口类名 / 安装路径 / 无边框铺满，加上调用方
        已经验过的画面运动）——这里只负责把样机上的东西凑齐递给它。

        **v2-social-scene-boundary 之后只剩 `game_hit`（正向）一个调用方**：反向那条线
        不再问线索了。原先"正反两边共用这一份口径、免得迟早说法不一样"的那条理由也跟着
        失效——现在只有一边在用，不存在两边打架。
        """
        from life.screen.game import recognize

        try:
            window_class = foreground.foreground_window_class()
        except Exception as exc:      # noqa: BLE001 —— 拿不到类名就当没这条线索
            logger.debug("窗口类名取不到：%s", exc)
            window_class = ""
        return recognize(
            proc,
            window_class=window_class,
            exe_path=self._exe_path(),
            title=self._game_title(),
            borderless_fullscreen=self._borderless_fullscreen(),
            motion_game=motion_game,
            rules=self._game_rules,
        )

    def game_hit(self, proc: str = ""):
        """**正向那条线**（`GameGuideService`）用的入口：这个前台是哪款游戏、谁认出来的。

        跟反向那条线（`is_watch_foreground` / `_watch_candidate`）共用同一个 `_game_hit`，
        口径永远一致；**差别只在什么时候问**：

        - 反向那边（v2-social-scene-boundary 之后）**一条游戏线索都不问了**——它只看
          进程名，最后落在 `is_watchable` 那份名单上，认不出就不放行；
        - 这边每次前台换人都问一次。**这里是那几条弱线索唯一的入口了**，所以一条都不
          能省：否则在窗口化模式里跑的游戏、以及认不出名字的全屏游戏，都进不了陪玩
          （`game_of` 那一版只认名字、不看窗口）。多花的只是几个字符串比较与两次系统
          调用。

        三条前置跟反向那边**逐条对齐**：启动器不算；`_known_non_video`（办公 / 社交 /
        桌面 / 截图工具…）不算——不然 PowerPoint 放幻灯、截图工具全屏会被问"要不要
        攻略"；认得出的播放器与浏览器也不算，那是刷剧陪伴的地盘。

        返回 `GameHit` 或 None；取不到 pid 时就按当前采样到的那一档算。
        """
        name = (proc or self._proc or "").strip()
        if not name:
            return None
        from life.screen.game import is_launcher, recognize

        if is_launcher(name):
            return None
        named = recognize(name, rules=self._game_rules)
        if named is not None:
            return named              # 认得出名字：全屏不全屏都不影响
        if _known_non_video(name) or is_watchable(name):
            return None
        hit = self._game_hit(name)
        if hit is not None:
            return hit
        if not is_fullscreen_foreground():
            return None               # 画面运动那一条只在全屏时才有意义
        if not self._probably_game(name):
            return None
        return self._game_hit(name, motion_game=True)

    def _exe_path(self) -> str:
        """前台进程的 exe **完整路径**（安装路径那条线索要它）；取不到给空串。

        走 `ProcNames.exe_of`，它自己带缓存、读不到也不抛（高权限进程 AccessDenied
        是常态）。空串 = "没这条线索"，不是"这个进程没有路径"。
        """
        try:
            return self._pids.exe_of(self._fg_pid)
        except Exception as exc:      # noqa: BLE001 —— 查路径失败不该把判定带崩
            logger.debug("exe 路径取不到：%s", exc)
            return ""

    def _borderless_fullscreen(self) -> bool:
        """前台窗口是不是**无边框铺满**（v2-game-activate 的最后一条线索）。

        判据是窗口样式里那三个位（标题栏 / 可缩放边框 / 系统菜单）**一条都不留**。
        全屏游戏（真独占与无边框都算）通常一条不留；浏览器 F11、播放器全屏、以及一切
        普通窗口都至少留着一条。**样式取不到时算"有边框"**（`window_has_frame` 的口径），
        也就是"没这条线索"，宁可漏判游戏也不误判。

        只在已经确认全屏之后才有意义，所以这里不再问一次全屏。

        **样式是从真实前台窗口的句柄上读的**，所以这里得再看一眼**真实前台窗口**的类名：
        桌面（`Progman`）与任务栏那几张也是无边框铺满的，读出来的样式跟全屏游戏一模一样。
        `recognize()` 里那张黑名单认的是**传进去的那个类名**（正常链路上两者是同一个窗口），
        这里按句柄读回来的这一份是它的独立兜底——两边都拦一道，漏一边不会漏判。
        """
        try:
            from life.screen.game import is_shell_window_class   # 延迟 import，同 `_is_game`

            if is_shell_window_class(foreground.foreground_window_class()):
                return False
            return not foreground.window_has_frame(foreground.foreground_window_style())
        except Exception as exc:      # noqa: BLE001 —— 拿不到样式就当有边框
            logger.debug("窗口样式取不到：%s", exc)
            return False

    def _game_title(self) -> str:
        """给本地名单的 `title:` 关键词用一次窗口标题；**不用时一个字都不读**。

        第二个读标题的地方（第一个是 `_read_title` 的浏览器分类），所以三道闸一样
        不能少：**必须**本地名单里真有 `title:` 词、**必须** `screen.read_browser_title`
        开着，才去读这一次。刚装好的程序里那份名单是空的 → 这个函数永远返回空串，
        标题一个字都不读——加这一档**不会**让标题破例的口子变大。
        """
        rules = self._game_rules
        if not getattr(rules, "titles", ()):
            return ""
        if not self._config.screen.read_browser_title:
            return ""
        try:
            title = foreground.foreground_title()
        except Exception as exc:      # noqa: BLE001 —— 读标题失败就当没这条线索
            logger.debug("读窗口标题失败：%s", exc)
            return ""
        return title

    def _probably_game(self, proc: str) -> bool:
        """连拍几帧看画面怎么动，判"是不是游戏"（v2-game-fix）。判不准一律 False。

        结论按 `MOTION_TTL_SEC` 记账（键是进程名）：一次连拍约 270 毫秒、还同步跑在
        界面线程上，不能每拍都来。**只存内存**，进程一换就重判。
        """
        if not bool(getattr(self._config.screen, "game_motion", True)):
            return False        # 总闸关掉：这一层整个不参与，行为退回加它之前
        key = (proc or "").lower()
        now = time.monotonic()
        cached = self._motion_cache
        if cached is not None and cached[0] == key and now < cached[1]:
            return cached[2]
        verdict = self._burst_says_game()
        self._motion_cache = (key, now + MOTION_TTL_SEC, verdict)
        return verdict

    def _burst_says_game(self) -> bool:
        """真跑一次连拍判读。取不到帧 / numpy 不在 / 判不准 → False（没这条线索）。"""
        try:
            frames = self._motion_grabber()
        except Exception as exc:      # noqa: BLE001 —— 取帧失败不该拖垮这一档判定
            logger.debug("运动特征：连拍取帧失败（%s）", exc)
            return False
        reading = motion.read_images(frames or [])
        if reading is None:
            return False
        logger.debug("运动特征：%s（前台 %s）", reading.line(), self._proc or "?")
        return reading.verdict == motion.MOTION_GAME

    def _grab_motion_frames(self) -> list:
        """连拍 `motion.BURST_FRAMES` 帧前台画面，帧间隔 `motion.BURST_GAP_MS`。

        **同步 sleep**，**实测整段约 0.5 秒**压在界面线程上（本机 2560×1528：4 次
        抓屏各约 40 毫秒 + 3 段 90 毫秒间隔 = 0.43 秒，判读再花 0.07 秒）——这是这一层
        唯一的开销，靠 `_probably_game` 那份 45 秒记账把它压到"一个进程 45 秒最多一次"。
        v2-social-scene-boundary 之后这条路**只由正向那条线（`game_hit`）走**，而且要
        先过"全屏 + 进程名不认识 + 不是播放器/浏览器 + 非视频硬闸"才轮到它。
        取不到帧就提前收工（返回已经拿到的那些，不够 3 帧时判读那边会自己给 None）。

        宠物自己那块照旧涂白（跟 OCR 那条腿一个做法）——它一直在动，不涂掉的话
        这一层等于给每一屏都叠了一层"局部乱动"，把判据往"游戏"那边推。
        """
        frames = []
        for i in range(motion.BURST_FRAMES):
            if i:
                time.sleep(motion.BURST_GAP_MS / 1000.0)
            image = shot.grab_foreground_window(self._pet_rect())
            if image is None:
                break
            frames.append(image)
        return frames

    def _watch_candidate(self, proc: str) -> bool:
        """这一档值不值得按"在看片"处理（去截屏、去认字、去攒那一票多数票）。

        跟 `is_watch_foreground` 的区别**只有一个**：这里在"要不要花一次截屏"这个位置
        上问，历史上它不过空白闸（空白闸本身要截一帧，拿它当门是本末倒置）。v2-social-
        scene-boundary 之后 `is_watch_foreground` 也不再截帧了（空白闸随全屏兜底一起删），
        两个函数现在的判据**逐字相同**——保留两份是因为调用位置不同：这个是"采不采样"
        的门，那个是"开不开会话"的门，将来若有一边要单独的宽严，改一处不影响另一处。

        游戏线索在这儿**一条都不问**了，只看名字（`_is_game`）与非视频硬闸。这不是省
        算力，是"全屏兜底"整个没了之后**没有"认不出的候选"这一类**——够不着那几条线索。
        代价是"认不出名字的全屏游戏"不再被当成看片，这对这一档是**好事**：宁可漏采，
        也不能对着游戏画面 OCR 出一堆字去冒"主人，看电影啦"。
        """
        if not proc or self._is_game(proc):
            return False
        if _known_non_video(proc):
            return False
        return is_watchable(proc)

    def _is_self_foreground(self) -> bool:
        """前台是不是**我们自己的窗口**（宠物 / 聊天卡 / 设置页）。

        v2-mood-anger：用户真机跟宠物聊红烧肉/手艺时被弹了一句愤怒共情。除了关键词
        太松（见 SCENE_KEYWORDS[SCENE_ANGRY]），还有这一条入口——宠物自己的窗口里
        写着它以前说过的话，全屏兜底那条腿把自家人也当成了播放器，它就 OCR 起自己的字，
        判出场景再冒出来，越冒越像。**宠物不该认自己的字。**

        pid 取不到（0）时一律当"不是自己"：宁可少一道闸，也别在取不到 pid 的机器上
        把采样整个关掉。
        """
        return bool(self._fg_pid) and self._fg_pid == os.getpid()

    def _grab_frame(self):
        """取一帧前台画面（字幕带），同一个序号内只真抓一次（v2-mood-detect）。

        一帧截屏在真机上不是白给的，能省一次就省一次。更要紧的是"这一次判定用的是
        哪一帧"必须说得清，所以只复用**刚抓的那一帧**，不跨序号缓存。
        """
        if self._frame is not None and self._frame_key == self._frame_seq:
            return self._frame
        image = self._grabber(self._pet_rect())
        self._frame = image
        self._frame_key = self._frame_seq
        return image

    # ---------- 启停 ----------

    def _apply_interval(self) -> None:
        self._timer.setInterval(max(1, int(self._config.screen.sample_sec)) * 1000)

    def _vote_window(self) -> float:
        """多数票的窗口宽度（秒）。配置里 scene_vote_window_sec，默认 60。"""
        return float(getattr(self._config.screen, "scene_vote_window_sec", 60.0) or 0.0)

    def start(self) -> None:
        if not self._config.screen.enabled:
            logger.info("屏幕感知关着，不采样")
            return
        self._apply_interval()
        self._timer.start()
        self._fg_timer.start()
        self._warm_up_ocr()
        self._ensure_mood_model()

    def _warm_up_ocr(self) -> None:
        """启动时先把 OCR 引擎在后台建好（v2-mood-detect）。

        建成一次要十几秒，摆着不管的话这份开销就落在**第一拍**上——用户看到的就是
        "放了半天宠物才有反应"。挪到启动后台跑，第一拍就只剩纯推理。

        假 reader（测试里注入的）没有 warm_up，直接跳过；建不起来也不影响任何已有功能
        （照旧每拍现建、失败即降级）。
        """
        warm = getattr(self._reader, "warm_up", None)
        if warm is None:
            return
        run_async(warm, on_failed=lambda exc: logger.debug("OCR 预热失败：%s", exc))

    def _ensure_mood_model(self) -> None:
        """本地档且权重不在时，后台去补（131MB，摆界面线程上就是十几秒卡死）。

        补齐之前 `available` 一直是 False，采样链会静默只用 OCR——所以下载这件事
        慢一点不影响任何已有功能，下完了下一拍自然就用上了。
        """
        if self._mood is None or not isinstance(self._mood, MoodCvReader):
            return
        if self._mood.available:
            return
        if self._mood.download_async():
            logger.info("画面情绪：本机没有中文 CLIP 权重，后台开始补（约 132MB）")

    def stop(self) -> None:
        self._timer.stop()
        self._fg_timer.stop()
        self._forget_scene()
        if self._title_kind:
            self._title_kind = ""
            self.title_changed.emit("")
        if self._proc:
            self._proc = ""
            self.foreground_changed.emit("")

    def apply_config(self) -> None:
        """设置页保存后热更新：总开关、采样间隔、画面情绪档位立刻生效。"""
        self._apply_interval()
        self._refresh_mood()
        if self._config.screen.enabled:
            if not self._timer.isActive():
                self._timer.start()
            if not self._fg_timer.isActive():
                self._fg_timer.start()
            self._ensure_mood_model()
        else:
            self.stop()

    def _refresh_mood(self) -> None:
        """档位变了就把那条腿换掉（off / local / cloud 之间切）。

        同档位就不重建：改个判据阈值不该把 132MB 的会话再建一遍，就地改那一档的数。

        显式指定 `EMOTION_BACKEND=jev|llm` 时以环境为准：已经挂着那条后端就不动，
        没有被 `screen.mood` 档位换掉一说；其余情况与从前逐字相同。
        """
        forced = _forced_backend(_emotion_env())
        if forced:
            if isinstance(self._mood, EmotionMoodReader) and self._mood.name == forced:
                return
            self._mood = self._build_mood()
            return
        if isinstance(self._mood, EmotionMoodReader):
            # 环境里不再显式指定（或改成 local / 写错值）：退回档位逻辑
            self._mood = self._build_mood()
            return
        mode = str(getattr(self._config.screen, "mood", "off") or "off").strip().lower()
        if mode not in ("off", "local", "cloud"):
            mode = "off"
        if self._mood is None:
            current = "off"
        elif isinstance(self._mood, MoodCloudReader):
            current = "cloud"
        else:
            current = "local"
        if mode == current:
            if isinstance(self._mood, MoodCvReader):
                self._mood.min_score = getattr(self._config.screen, "mood_min_score", 0.70)
            return
        self._mood = self._build_mood()
        logger.info("画面情绪：档位切到 %s", mode)

    @property
    def recording(self) -> bool:
        return self._timer.isActive()

    @property
    def scene(self) -> str:
        """当前成立的场景（空串表示没有）。"""
        return self._scene

    @property
    def foreground_proc(self) -> str:
        """**上一拍记住的**前台进程名（空串表示取不到）。

        注意它是"记账"那一份，不是"现算"那一份：`_poll_foreground` 只在前台**换人**
        时才更新它，而且前台是我们自己时那一拍整个 return、连记账都不记。要问
        "此刻到底是谁在前台"用 `foreground_now()`。
        """
        return self._proc

    def foreground_now(self) -> str:
        """此刻前台的进程名——**现算，不看变化**（v2-social-foreground-fix）。

        跟 `foreground_changed` 那条信号差在一件事上：信号只在**换人**时发
        （`_poll_foreground` 里那句 `if proc != self._proc`），而冷启动那一刻前台
        往往就是宠物自己（`Bootstrap.start()` 头一句 `pet_window.show()` 就把焦点
        拿在手上），`_poll_foreground` 在 `_is_self_foreground()` 处当场 return——
        **一个信号都不发**。于是"现在到底是谁在前台"这件事，在下一次真的换人之前
        没有任何人问得出来，下游只能抱着各自的初值干等（真机上就是"刚启动不认
        社交窗，再启动一次才对"——认的不是状态，是"有没有人碰巧换过一次前台"）。

        这一位就是补上"现状"那一问。**只看、不发信号、不动 `_proc`**：它不改变
        `foreground_changed` 的语义（前台是自己时照旧不发），只是把当前值读出来。

        前台是**我们自己**时回空串——对外部应用而言"没人在前台"，调用方按
        `is_social_proc("")` 走正好是"不是社交软件"，语义自洽。
        """
        pid = foreground.foreground_pid()
        if pid and pid == os.getpid():
            return ""
        return self._pids.name_of(pid) or ""

    def visible_procs(self) -> list[str]:
        """桌面上这会儿**可见、没最小化**的顶层窗口分属哪些进程（去重，进程名）。

        跟 `foreground_now()` 是**两问**：那一问是"焦点在谁身上"，只能有一个答案；
        这一问是"谁在桌面上摆着"，可以有很多个。补这一问是为了冷启动那一刻
        （v2-social-await-prime-fix 改动二）：宠物自己的窗口一 show 就抢了前台，
        `foreground_now()` 答不出"微信开着但不在焦点"，而"微信开着"这件事对社交
        那条线是**算数**的——用户明明就在跟人聊着，不该等他手动切一次窗才认。

        **只看进程号**：底层 `foreground.visible_pids()` 那一趟一个标题都没读
        （见那边的口径），这里也只是把进程号翻成进程名。翻不出来的（进程刚退出、
        或是读不到名字的系统进程）跳过。

        取不到（不是 Windows / 系统调用失败）返回空表，调用方按"一个都没看见"处理。
        """
        seen: list[str] = []
        for pid in foreground.visible_pids():
            name = self._pids.name_of(pid)
            if name and name not in seen:
                seen.append(name)
        return seen

    @property
    def title_kind(self) -> str:
        """浏览器标题的判类：focus / watch / 空串（不是浏览器，或标题认不出来）。

        注意这里出的是**标签**不是标题——标题读完当场判掉，一个字都不留在对象上。
        """
        return self._title_kind

    @property
    def mood_reader(self):
        """画面情绪那条腿（off 档位时是 None）。设置页拿它问"模型装好没"。"""
        return self._mood

    @property
    def last_fusion(self):
        """上一拍两条腿融合的结论（`mood_fuse.Fusion`；还没采过样时是 None）。

        只存内存、只留最近一拍——给调试与测试看"这一拍到底谁说了算"。
        """
        return self._last_fusion

    # ---------- 采样 ----------

    def _poll_foreground(self) -> None:
        """前台快轮询：只问前台是谁、浏览器标题判类，即时发 foreground / title 变化。

        从 sample() 里拆出来，是因为前台切换要"立即响应"——用户真机：专注↔桌面要等
        sample_sec（默认 5 秒）整整几秒才有反应。前台进程名 / 标题是几次系统调用，便宜
        又快，值得单独一个快计时器跑；耗时的 OCR / 截图仍留在 sample_sec 那一路。

        **前台是我们自己的窗口时一个信号都不发**（v2-scene-switch）：宠物 / 聊天卡 /
        设置页不是"用户换了应用"，不发 foreground / title 变化。否则拖一下宠物、前台
        变成本进程，自动番茄钟 focus_timer 会把它当成"切走了专注应用"收场、场景态跟着
        解锁——真机"专注态拖几下就退出专注"的根因。

        **但那一支里记账的 `_proc` 要跟到现状**（v2-social-fg-residual）：只认"变"不认
        "现状"的话，"微信 → 点一下宠物 → 切回微信"这条路上 `proc` 跟没动过的 `_proc`
        正好相等，一个信号都不发，下游（社交那条线）永远等不到"微信回到前台"。
        改的是**记账**，不是**发信号**：`emit` 仍旧只在真换人那一次。
        """
        self._fg_pid = foreground.foreground_pid()
        proc = self._pids.name_of(self._fg_pid) or ""
        if self._is_self_foreground():
            # 前台是我们自己：**一个信号都不发**（v2-scene-switch 那条规矩一个字没改），
            # 但**记账那一格要跟到现状**（v2-social-fg-residual）。
            #
            # 只认"变"不认"现状"会漏掉一条最平常的路：用户正在微信里聊着，顺手点一下
            # 宠物看一眼（前台变成我们自己），再切回微信——`proc` 跟还停在微信那一格的
            # `_proc` **正好相等**，于是 `foreground_changed` 一个都不发。可下游
            # （社交那条线）知道"微信在不在前台"的唯一入口就是这个信号：它停在上一轮的
            # 状态上，人明明回到微信了，它却当成没回来。真机那条"邀请冒出来之后就再也
            # 不动了"里，有一条支路就是这儿。
            #
            # 把这一格跟到现状，再切回微信就是一次**真的变化**，信号照旧只发那一次。
            # 一句话划清：**记的是现状，发的是变化**——`emit` 与 `_forget_scene()` 都还
            # 只在"真换人"那一支里（这里 return 得比它们早，一个字都不碰）。
            #
            # 场景态不会因此多散一次场：`sample()` 里"前台是自己"那一支本来就
            # `_forget_scene()`（v2-mood-anger 那条），轮不到这里。
            self._proc = proc
            return
        if proc != self._proc:
            if self._proc:
                # 换前台 = 换了这一场戏：旧票立刻作废、场景立刻散场（v2-mood-recall-fix）。
                # 散场从"窗口里空票占多数"改成**硬证据**（前台换了 / 不是视频 / 空白屏 /
                # 自己的窗口）——"猜不出来"不再等于"没在放"。原来的空票法还有个副作用：
                # 关掉播放器之后要等一分钟空票攒够才散场，状态词一直挂着。
                self._forget_scene()
            self._proc = proc
            if proc:
                # 真机排查用（v2-mood-horror ②）：换前台只在此刻记一条。**标题与画面都不记**，
                # 只记进程名和"这一类应用会不会去采样"——日志里一行采样都没有时，翻到这几行
                # 就知道是压根没轮到它，而不是判读判错了。
                logger.info(
                    "屏幕采样：前台换成 %s（%s）",
                    proc,
                    "会去看" if self._watch_candidate(proc)
                    else "不在播放器/浏览器名单里、或不是全屏窗口、或是游戏，不采样",
                )
            self.foreground_changed.emit(proc)

        # 浏览器前台才读标题（隐私破例，见模块开头与 life/screen/browser.py）。
        # 判类变了就报一声：同一个浏览器里换标签页要靠它通知下游。
        kind = self._read_title(proc)
        if kind != self._title_kind:
            self._title_kind = kind
            if kind:
                # 只记**判出来的标签**，标题本身一个字都不进日志（同 browser.py 那条纪律）。
                logger.info(
                    "屏幕采样：浏览器标题判成 %s（%s）",
                    kind,
                    "整拍跳过、不去看画面" if kind == TITLE_FOCUS else "算在看片子，接着看画面",
                )
            self.title_changed.emit(kind)

    def sample(self) -> None:
        """一拍：看前台是谁 → 读不读标题 → 值得看就 OCR → 数关键词 → 投进一分钟多数票。"""
        self._frame_seq += 1        # 新的一拍：上一拍留的那帧作废（见 _grab_frame）
        self._poll_foreground()
        proc = self._proc
        kind = self._title_kind

        if not self._config.screen.ocr_enabled:
            return
        if kind == TITLE_FOCUS:
            # 标题说是网课 / 在线创作：这一拍整个跳过，不截屏也不认字
            self._forget_scene()
            return
        if is_browser(proc) and kind != TITLE_WATCH:
            # 浏览器前台、但标题不是"在看片"（刷网页 / 查东西）——不算电影态，这一拍
            # 不 OCR、不认字（v2-scene-switch）。修的是真机"浏览网页乱飘『好开心』"：
            # 情绪识别只在电影态读情绪并飘词。
            self._forget_scene()
            return
        if self._is_self_foreground():
            # v2-mood-anger：前台是**我们自己的窗口**（宠物 / 聊天卡 / 设置页）——
            # 用户正在跟宠物说话，本来就没什么片子在放。这一拍整个跳过。
            #
            # 这一条是用户真机"聊着红烧肉突然被弹愤怒共情"的另一个入口：宠物自己的
            # 窗口里写着它以前说过的话，鼠标一进气泡、窗口又被认成全屏（兜底那条腿），
            # 它就 OCR 起自己的字来——泛化单字一票就判，还能自己喂自己。
            # **宠物不该认自己的字**，先把这条堵死。
            self._forget_scene()
            logger.info("屏幕采样：前台是我们自己的窗口，这一拍不采样、不认字")
            return
        if not self._watch_candidate(proc):
            # 不是在放视频（进程名不在表里、不是全屏窗口，或者是游戏）：这一拍的场景
            # 多数票作废。**游戏也走这一支**（v2-game-vs-movie）——全屏游戏既不认字、
            # 也不让它影响多数票，游戏那条线由 GameGuideService 单独管。
            self._forget_scene()
            return

        if self._reading:
            return          # 上一次还没认完，这一拍跳过（一次 OCR 要好几秒）
        image = self._grab_frame()
        if image is None:
            return
        if shot.is_blank(image):
            # 空白 / 纯色屏（一个空文件夹、一张白底的空白页）：这一屏什么都没在放，
            # 这一拍不成立——这一票不投，也不去认字（v2-mood-detect）。判据与实测值
            # 见 shot.is_blank 上面那一段。
            self._forget_scene()
            logger.info(
                "屏幕采样：%s 这一屏是空白/纯色（主色占比 >= %.2f），不判场景",
                self._proc or "?", shot.BLANK_RATIO,
            )
            return
        array = _to_numpy(image)      # 转数组留在界面线程：只是拷内存，微秒级
        if array is None:
            return
        cv_array = self._grab_cv()
        self._reading = True
        run_async(
            self._analyze_sample,
            array,
            cv_array,
            on_done=self._on_analysis,
            on_failed=self._on_read_failed,
        )

    def _grab_cv(self):
        """给画面情绪那条腿取一帧。没开这条腿 / 模型不在 / 取不到，都给 None。

        **先问能不能判，再决定抓不抓**：模型不在的时候连截这一下都不做——
        少抓一帧屏幕，就少一次"没理由地看人家屏幕"。

        取的是**前台窗口那一块**（shot.grab_foreground_window），不是 OCR 那条
        字幕带：判"这场戏在演什么"要的是画面，字幕带里只有字。
        """
        if self._mood is None or not self._mood.available:
            return None
        try:
            image = self._cv_grabber(self._pet_rect())
        except Exception as exc:      # noqa: BLE001 —— 取不到这一帧就当没有，不拖垮采样
            logger.debug("画面情绪：取帧失败（%s）", exc)
            return None
        if image is None:
            return None
        return _to_numpy(image)

    def read_screen_array(self, array) -> str:
        """把一张已经转成数组的画面 OCR 成一行字（v2-social-fewshot 用）。

        社交那条线抓了聊天窗口那一屏，借这一手认字——**跟认字幕共用同一个 RapidOCR**，
        不另起引擎、不新开采样循环。**只读、不落盘**；没装 OCR / 出错都回空串，由调用方
        当"没认到"处理。识别耗时（约 4 秒）由调用方丢后台线程，这儿只做纯识别。
        """
        return self._reader.read_array(array)

    def read_screen_lines(self, array) -> list:
        """认字**连带字框**（v2-social-ocr-isolate）。

        跟 `read_screen_array` 同一台引擎、同一次识别，只是把框也留下来——社交那条线
        靠框去图上取气泡底色，才分得出"这行是自己发的还是对方发的"。没装 / 出错回空表。
        """
        return self._reader.read_lines(array)

    def detect_screen_boxes(self, array) -> list:
        """**只跑检测**、不跑识别（v2-social-offer-fast-latency）：回字框列表；没接 / 出错回空表。

        社交那条线在**认字之前**先跑这一手，拿字框几何判"这一屏像不像会话总列表"，
        命中就整趟省掉识别（见 `social.looks_like_list_boxes`）。跟 `read_screen_lines`
        共用同一台引擎。空表 = 没接引擎 / 认成空屏 / 出错，三种都由调用方当"判不出来"
        处理（那就照旧认字，一步都不少）。
        """
        return self._reader.detect_boxes(array) or []

    def _analyze_sample(self, array, cv_array):
        """**后台线程**里把两条腿跑完，回主线程一个结论。

        两条腿塞进同一个任务里，是为了让"这一拍"这件事只有一个结果、一个完成点——
        拆成两个 future 就得自己对齐"谁的结论配谁的帧"，而它们本来就同源同一拍。

        **画面腿先跑，它定案就不用再跑字幕那 4 秒了**（v2-mood-detect 的延迟修复）。
        两件事让它成立：CV 一次只要 0.1~0.2 秒，而 OCR 要 4 秒上下；且融合层本来就是
        "CV 开了口就以它为准"（`mood_fuse.fuse`）。所以 CV 已经定案时跳过 OCR，结论
        **逐字不变**，只是这一拍从 ~4 秒缩到 ~0.2 秒——真机上"情绪要过好一会儿才认出来"
        主要就是被这 4 秒拖着。

        返回 (字幕文字, 画面判读, OCR 跳没跳过)。第三条只用来写日志。

        `EMOTION_BACKEND=jev|llm` 显式启用时，字幕也是判定输入，所以先把这一拍 OCR
        出来再连同画面一起送进去；默认本地档仍走"画面腿先跑、定案就省 OCR"的原路。
        """
        verdict = None
        mood = self._mood
        if mood is not None and getattr(mood, "wants_text", False):
            text = self._reader.read_array(array)
            verdict = mood.classify_array(cv_array, text=text)
            return text, verdict, False
        if mood is not None and cv_array is not None:
            verdict = mood.classify_array(cv_array)
            if verdict is not None and verdict.scene:
                return "", verdict, True
        text = self._reader.read_array(array)
        return text, verdict, False

    def _on_analysis(self, result) -> None:
        """两条腿都回来了（回主线程）：融合成一个场景，再投进一分钟多数票。"""
        self._reading = False
        text, verdict, ocr_skipped = result
        ocr_scene = classify_text(text)
        merged = fuse(ocr_scene, verdict)
        self._last_fusion = merged
        self._count_scene(merged.scene)
        self._log_sample(ocr_scene, verdict, merged, ocr_skipped=ocr_skipped)

    def _log_sample(self, ocr_scene: str | None, verdict, merged, ocr_skipped: bool = False) -> None:
        """每一拍记一条 INFO（v2-mood-horror ②）：真机"宠物没反应"时，一眼看出断在哪一环。

        为什么值得每拍一条：这条链上"没反应"有三种完全不同的成因——前台不是播放器
        （压根不来这儿）、OCR 没认出关键词、CV 分数没过阈值——而它们最终的表现**一模
        一样**（宠物安静）。只记"场景成立了"是不够的，那只覆盖了成功的那些拍。

        为什么不会刷屏：`_on_analysis` 只在**真截了屏、真跑完识别**之后才被叫到，
        而截屏前头还卡着"前台是不是播放器/浏览器"这道闸。不看片的日子这条一行都不出；
        在看片时约每 sample_sec 一条。

        **只记判读结论，不记判读内容**：字幕文字与画面一律不进日志（同 browser.py 的纪律），
        这里落下去的是场景标签和几个分数。多数票那一栏记"窗口里几票**有定论的** / 窗口多宽"
        ——弃权的拍不投票（v2-mood-recall-fix），所以这个数小于"看过的拍数"是正常的，
        差值就是这 60 秒里没把握的那几拍。
        """
        if verdict is None:
            cv = "CV 没参与（没开这条腿 / 权重不在）"
        else:
            ranked = sorted(verdict.scores.items(), key=lambda kv: -kv[1])
            cv = "CV %s（%s）" % (
                verdict.scene or "没把握",
                " ".join(f"{key} {value:.4f}" for key, value in ranked),
            )
        # OCR 那一栏要能分辨"认了没命中"与"压根没跑"（v2-mood-detect 之后 CV 定案就跳）
        ocr_text = "跳过（画面腿已定案）" if ocr_skipped else (ocr_scene or "无命中")
        logger.info(
            "屏幕采样：%s 场景=%s（来源=%s）多数票 %d/%.0fs 已弹=%s ｜ OCR %s ｜ %s",
            self._proc or "?",
            merged.scene or "无",
            merged.source or "无",
            len(self._voter),
            self._voter.window,
            self._voter.held or "无",     # 首弹门槛/去抖看的就是它（v2-mood-window）
            ocr_text,
            cv,
        )

    def _read_title(self, proc: str) -> str:
        """浏览器前台读一次标题并当场判类；别的一律返回空串。

        破例的三道闸都在这个函数里：只有浏览器（`is_browser`）、只在开关打开时、
        只调一次 `foreground_title()`。判完标签，标题就丢了。
        """
        if not self._config.screen.read_browser_title:
            return ""
        if not is_browser(proc):
            return ""
        try:
            title = foreground.foreground_title()
        except Exception as exc:      # 取标题失败不该把整条采样链带崩
            logger.debug("读窗口标题失败: %s", exc)
            return ""
        # 用户自己标的工作站点词一起带上（v2-focus-web ①）：内置表总有漏的，
        # 漏的那个不该只能等下一版。
        return classify_title(title, self._config.screen.focus_words)

    def _on_read_failed(self, exc: Exception) -> None:
        self._reading = False
        logger.debug("这一拍 OCR 没成：%s", exc)

    def set_pet_rect_provider(self, provider) -> None:
        """装一个"宠物现在在哪"的回调，OCR 前把那一块涂白。

        做成回调：宠物一直在被拖着挪，取的时候现问一下比一直记着准。
        """
        self._pet_rect = provider

    def _count_scene(self, scene: str | None) -> None:
        """把这一拍的结果投进一分钟多数票；定案了才报场景。

        判定是"一分钟多数票"（见 life/screen/vote.py）：每拍看一眼，**没把握的那一拍
        不投票**（只是这一拍不算数），窗口里出现最多的那一态才算最终判定。

        **没定案就什么都不动**：既不改场景，也不把已经在显示的影视状态词撤掉——这正是
        用户要的"不秒退、不闪烁"。**散场也不在这里判**：那由 `_forget_scene()` 的硬证据
        （换前台 / 不是视频 / 空白屏 / 自己的窗口）触发（v2-mood-recall-fix）。
        窗口宽度每拍从配置现读——测试把 scene_vote_window_sec 设成 0 就等于"一拍即定"。
        """
        if scene is not None and scene not in SCENE_ORDER:
            # v2-mood-tri 融合层**双保险**：哭泣整态删除之后，任何一条腿若还吐出那个
            # 已经不存在的态（旧配置、漏改的表、将来新加的腿），都在**进投票窗之前**
            # 丢掉，绝不空转占窗——一个恒为零票的态混进窗口，只会把 60 秒观察窗
            # 白占掉、把三态挤出去，用户看到的就是"认了半天没反应"。
            # 四条腿（CLIP 原型集 / 文本提示词 / 字幕关键词 / 情绪云）都已各自拔干净，
            # 这里是最后一道：**不认识的一律当没把握**，与"弃权只作废这一拍"同一口径。
            logger.debug("这一拍认出的场景 %r 不在这三态里，丢掉不投票", scene)
            scene = None
        self._voter.window = self._vote_window()
        decided = self._voter.note(scene)
        if decided is None:
            return
        self._report(decided)

    def _report(self, scene: str | None) -> None:
        """场景变了就广播（scene 为 None 表示散场 / 这会儿不成立）。

        **先把 None 归成空串再比**：散场本来就是空串，不归一的话"已经是散场了、
        再报一次散场"会因为 `None != ""` 每次都广播一遍——不在看片的每一拍照样
        往 scene_changed 上砸一个空串。
        """
        scene = scene or ""
        if scene == self._scene:
            return
        self._scene = scene
        if self._scene:
            logger.debug("屏幕场景成立：%s", SCENE_TITLES.get(self._scene, self._scene))
        self.scene_changed.emit(self._scene)

    def _forget_scene(self) -> None:
        """散场 / 作废这一窗票。**散场只有这一条路**（v2-mood-recall-fix）。

        调用它的都是**硬证据**：换了前台、前台不是视频（含非视频硬闸与游戏）、
        标题是网课、前台是我们自己的窗口、空白纯色屏、采样停掉。这些比"我猜不出来"
        可靠——而"猜不出来"（弃权）现在只作废那一拍，不再攒成散场。
        """
        self._voter.reset()
        self._report(None)
