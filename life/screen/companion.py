"""
刷剧陪伴（S3）：认出场景后，让宠物在浮窗里主动说一句。

四个场景各有一小池话，都是宠物本体的腔（短句、口语、带点黏人），
**不经过角色卡系统**——所以玄机先生那套命理腔不会串进来（腔调隔离）。
每池第一句是用户指定的那句，后面几句是同一腔调补的：池子只有一句时
"同态 N 分钟内不重复"就没有意义了，补几句才轮得开。

三条纪律：
- **同态不重复**：同一个场景在 scene_cooldown_min 分钟内只冒一次话；
  同一池里也尽量不连着挑同一句（挑过的那句压到队尾）。
- **不弹卡**：只发 pet_speak，气泡说完就散，不建卡、不弹面板。
- **冒话前过危机拦截**：话虽然是本地固定池，也照样过一遍 is_crisis——
  这条红线不因为是"自己写死的句子"就跳过，将来往池子里加话时也不会漏。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import QObject, QTimer

from core.config import Config
from core.events import EventBus
from life.psych.crisis import is_crisis
from life.screen.browser import TITLE_WATCH, is_browser
# `life.screen.monitor` 这个模块本身不再 import 了（v2-social-scene-boundary）：原来只在
# `_is_watch_foreground` 的"没接线"那一支用它取 `is_fullscreen_foreground()`，那条兜底
# 删掉之后就没有模块级引用了（类里的 `monitor` 都是 `attach` 传进来的那个采样器实例）。
from life.screen.monitor import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    SCENE_TITLES,
    is_watchable,
)
from life.screen.scene_guard import KIND_WATCH

logger = logging.getLogger(__name__)

# 认出在看片子之后先说这一句（用户给的原文，别改）。只显示不出声，停 6 秒。
OPENING_LINE = "主人，看电影啦，我陪你看吧！"

# 开场字幕"再等等"的时长（v2-mood-horror ①）。宽限期到点时，若这一拍已经判出了场景、
# 只是多数票窗口还没满（`scene_vote_window_sec`，见 life/screen/vote.py），就再等这么久。
# 取 8 秒是因为**实测**一拍就是 sample_sec(5s) 加一次识别(约 4s)，等这么久正好把已经
# 露头的那一拍等完。窗口满不满是另一回事——这里只保证"露头的那一拍"不被开场字幕抢掉。
#
# 为什么非要有这一手：宽限期(12s)和"场景成立"本来是在抛硬币。真机实测两轮，同一份
# 代码、同一张丧尸画面——全屏那轮第 12.2 秒判出 horror，开场字幕被正确撤掉；窗口化
# 那轮第 17.8 秒才判出，开场字幕已经先说出口了。差的只是采样落点，于是用户要的
# "开场即恐怖 → 直接怕怕"就变成一半概率。
#
# **只延一次**：延完还没成立，就该老老实实说那句开场白。
OPENING_DEFER_MS = 8000

# 三态语句池。第一句是用户定稿的原文，别改；后面的都是同一腔调补的。
#
# v2-mood-tri：**哭泣那一池已整池删除**（原来是「呜呜呜，我也好想哭……」起头那四句）。
# 那四句是用户定稿的原文，删掉不是"觉得不好"，是这一态整态不认了——留着池子等于留着
# 一个永远进不来的出口。原文全部抄进收尾报告第九十六节存档，要恢复照抄回来即可。
SCENE_LINES: dict[str, tuple[str, ...]] = {
    SCENE_HORROR: (
        "我怕怕…",
        # v2-mood-detect 再审·终：删掉「太上老君急急如律令，剑来——主人别怕」——
        # 那是玄幻咒语腔，不是恐怖腔；恐怖/打斗的台词不该往"念咒"上跑。
        "这段我不敢看了，捂眼睛的那种(>_<)",
        "主人你要是怕，就抓着我尾巴看",
    ),
    SCENE_ANGRY: (
        # v2-mood-anger：第一句原来是「这也能气着？来，我陪你骂两句」——用户定稿换成
        # 下面这句（"陪你骂"听着像撺掇，改成替他不平、替他出气）。
        "太气人，真想捶他几下",
        "什么人啊这是，主人别憋着，跟我说",
        "气死我了，尾巴都竖起来了",
        "消消气，骂完了咱接着看",
    ),
    SCENE_FESTIVE: (
        "好开心",
        "看到笑容我也跟着开心起来了",
        "笑得这么开心，发生什么好事啦",
        "这个笑真好看，心情都变好了",
    ),
}


class ScreenCompanion(QObject):
    """盯着场景变化，该开口就开口。"""

    def __init__(self, config: Config, bus: EventBus, guard=None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        # 场景互斥守卫（v2-mood-detect）：专注 / 游戏在演的时候不许开看片会话。
        # 默认 None = 没接线（单测直接 new 一个）：那就按"没有守卫"放行，宁可少一道闸，
        # 也别在没接线时把功能整个关掉。
        self._guard = guard
        # 只存内存：每个场景上次冒话是什么时候、每一池挑到第几句了
        self._last_spoken: dict[str, float] = {}
        self._cursor: dict[str, int] = {}
        # ---- 开场陪伴字幕（v2-speech-polish ④）----
        # 这一次"在放片子"的会话里，开场字幕说过没有 / 被剧情抢掉没有。
        # 会话由前台换成播放器/浏览器（或浏览器标题判成 watch）开启，换走就重置。
        self._watch_open = False
        self._opening_done = False
        self._opening_skipped = False
        # 「宽限期到点再看一眼苗头」用的两样（v2-mood-horror ①）：这次宽限延过没有，
        # 以及被判读的那个 monitor（`attach` 时挂上，取它最近一拍的判读结论）。
        self._opening_deferred = False
        self._monitor = None
        # 浏览器标题的判类（focus / watch / 空串）。前台在浏览器上时，靠它决定
        # "开着这个窗口算不算在看片"——见 on_foreground。
        self._title_kind = ""
        # 这一档宽限期用长的还是短的（v2-mood-detect 的本地播放器提速）：认得出名字的
        # 本地播放器用 opening_grace_fast_sec，浏览器与"全屏兜底"用 opening_grace_sec。
        self._fast_open = False
        # 开场字幕晚一点再冒：先看这一拍认不认得出剧情。认得出就直接出表情反应，
        # 开场那句"我陪你看吧"就不发了（用户要的就是"开场即恐怖 → 直接怕怕"）。
        self._opening_timer = QTimer(self)
        self._opening_timer.setSingleShot(True)
        self._opening_timer.timeout.connect(self._show_opening)

    # ---------- 接线 ----------

    def attach(self, monitor) -> None:
        """挂到 ScreenMonitor 上。"""
        self._monitor = monitor         # 开场字幕那条要用它最近一拍的判读结论，见 _scene_pending
        monitor.scene_changed.connect(self.on_scene)
        # 开场那条要两个信号一起判：前台换成了播放器（foreground_changed），
        # 或者浏览器标题判成"在放片子"（title_changed=watch）。两个都算"要看片了"。
        monitor.foreground_changed.connect(self.on_foreground)
        monitor.title_changed.connect(self.on_title)

    # ---------- 看片会话与开场字幕（v2-speech-polish ④） ----------

    def on_foreground(self, proc: str) -> None:
        """前台换人了：换成播放器就开一场看片会话，换走就收。

        **浏览器不一样**：光开着 Chrome 不算在看片（那多半是在干活、在查东西），
        只有标题判成 watch（剧集/电影/直播）才算——那条由 `on_title` 说话。
        不这么分的话，随手打开一个浏览器，12 秒后就会冒出"主人，看电影啦"。
        """
        # 这一档该用长宽限期还是短宽限期（v2-mood-detect）：认得出名字的本地播放器
        # 是硬证据，用短的；浏览器证据弱（光开着 Chrome 不算在看片），仍用长的。
        self._fast_open = is_watchable(proc) and not is_browser(proc)
        if is_browser(proc):
            if self._title_kind != TITLE_WATCH:
                self._end_watch()
            return
        if self._is_watch_foreground(proc):
            self._begin_watch()
        else:
            # 不是在看片：**游戏**也落在这一支（v2-game-vs-movie，判据在
            # ScreenMonitor.is_watch_foreground 里，认得出的游戏直接否）。
            self._end_watch()

    def _is_watch_foreground(self, proc: str) -> bool:
        """这一档算不算在看片。**交给采样器判**（v2-mood-detect）。

        原先这里自己拿 `is_watchable(proc) or is_fullscreen_foreground()` 拼——那条
        判据只看"窗口铺没铺满屏"，于是打开一个空文件夹（最大化窗口也是满屏）也会开一场
        看片会话，12 秒后就冒"主人，看电影啦"。v2-social-scene-boundary 把采样器那边
        剩下的全屏兜底也删干净了，所以两边现在**逐字相同**：只认名单。

        没有采样器时（单元测试直接 new 一个 ScreenCompanion）走同一个判据——认得出的
        播放器/浏览器才开，认不出的不开。**这里不留"没接线就放松"的后路**：放松的那
        一支恰好就是这次要删掉的那条兜底，留着等于换个地方留后门。
        """
        fn = getattr(self._monitor, "is_watch_foreground", None)
        if fn is not None:
            return bool(fn(proc))
        return is_watchable(proc)

    def on_title(self, kind: str) -> None:
        """浏览器标题判类变了：判成 watch（剧集/电影/直播）就算在看片子，其余算收场。"""
        self._title_kind = kind
        if kind == TITLE_WATCH:
            self._begin_watch()
        else:
            self._end_watch()

    def _begin_watch(self) -> None:
        """开一场看片会话：把开场字幕**排上队**（不是立刻发，见 _opening_timer）。

        **先过互斥守卫**（v2-mood-detect）：专注番茄钟在跑、或者正在玩游戏，这一场就
        不开——真机上那个"番茄钟跑着还冒「主人，看电影啦」"就是缺这一道闸。守卫不在
        （没接线）时照开。
        """
        if not self._config.screen.enabled or not self._config.screen.companion_enabled:
            return
        if not self._allowed():
            logger.debug("看片会话不开：这会儿是 %s 说了算（场景互斥）",
                         getattr(self._guard, "active", "?"))
            self._end_watch()
            return
        self._watch_open = True
        self._opening_done = False
        self._opening_skipped = False
        self._opening_deferred = False
        if self._guard is not None:
            self._guard.set_watch(True)
        # 先给剧情一个认出来的机会：这段窗口内真认出了场景，开场那句就撤。
        # 宽限期分两档：本地播放器（认得出名字）用短的，别的用长的（v2-mood-detect）。
        self._opening_timer.start(self._grace_ms())

    def _grace_ms(self) -> int:
        """这一场开场字幕等多久才冒。"""
        sec = getattr(self._config.screen, "opening_grace_sec", 12)
        if self._fast_open:
            sec = getattr(self._config.screen, "opening_grace_fast_sec", sec)
        return max(0, int(sec)) * 1000

    def _allowed(self) -> bool:
        """看片这一档这会儿开不开得了（没接线 = 放行）。"""
        if self._guard is None:
            return True
        return bool(self._guard.allow(KIND_WATCH))

    def stop_watch(self) -> None:
        """从外面收掉看片会话（v2-mood-detect）：进专注时装配层喊一声。

        跟 `_end_watch` 是同一件事，公开这一版是因为"进专注"这个动作发生在
        bootstrap 里（番茄钟状态变了），它得有个正经口子通知这边，而不是去戳私有方法。
        """
        self._end_watch()

    def _end_watch(self) -> None:
        """看片会话结束（切到别的窗口）：计时停掉，下次重开算新的一场。"""
        self._watch_open = False
        self._opening_timer.stop()
        if self._guard is not None:
            self._guard.set_watch(False)

    def _show_opening(self) -> None:
        """宽限期过了还没认出剧情：发开场陪伴字幕。

        发出之前先看一眼**苗头**（v2-mood-horror ①）：这一拍要是已经判出了场景、
        只是多数票窗口还没满，就把这一句往后挪一次——不然"开场即恐怖"就成了抛硬币，
        见 OPENING_DEFER_MS 上面那段实测。挪过一次就不再挪。

        **只显示、不出声**：走的是 `bus.screen_caption`，那条通道结构上就没有
        接 voice.say（见 core/events.py 那一段注释），所以不需要靠静音闸兜底。
        停几秒由界面那头定（Bootstrap 传 show_ms）。
        """
        if not self._watch_open or self._opening_done or self._opening_skipped:
            return
        if not self._opening_deferred:
            pending = self._scene_pending()
            if pending:
                self._opening_deferred = True
                logger.info(
                    "看片开场：剧情已经露头（%s），开场字幕再等 %.0f 秒看它成不成立",
                    SCENE_TITLES.get(pending, pending),
                    OPENING_DEFER_MS / 1000,
                )
                self._opening_timer.start(OPENING_DEFER_MS)
                return
        self._opening_done = True
        logger.debug("看片开场：发陪伴字幕（%s）", OPENING_LINE)
        self._bus.screen_caption.emit(OPENING_LINE)

    def _scene_pending(self) -> str:
        """这一拍是不是已经判出了场景、只差窗口没满（`on_scene` 还没响）？返回那个场景。

        只看最近一拍：`monitor.last_fusion` 只存内存、只留一拍。**已经报过的场景不算
        苗头**——那种情况 `on_scene` 早把开场字幕撤了，这里再拦就成了"永远不发"。
        """
        fusion = getattr(self._monitor, "last_fusion", None)
        scene = getattr(fusion, "scene", None) or ""
        if not scene:
            return ""
        if scene == getattr(self._monitor, "scene", ""):
            return ""
        return scene

    # ---------- 场景 ----------

    def on_scene(self, scene: str) -> None:
        """场景成立（或散场）了。散场是空串，什么都不做。"""
        if not scene or not self._config.screen.enabled:
            return
        if not self._allowed():
            # 专注/游戏在演：这一拍认出的剧情反应也不冒（v2-mood-detect 的场景互斥）。
            # 只丢这一拍，不记 `_last_spoken`——专注结束、下次再认出来时照常能开口。
            logger.debug("场景反应不发：这会儿是 %s 说了算（场景互斥）",
                         getattr(self._guard, "active", "?"))
            return
        if self._watch_open and not self._opening_done:
            # 开场字幕还没发出来（还在宽限期里）剧情就先认出来了：这一场**不发开场字幕**，
            # 直接走下面的表情反应。用户要的正是这个——开场即恐怖，上来就该是"怕怕"，
            # 不该先说一句"我陪你看吧"再怕。
            self._opening_skipped = True
            self._opening_timer.stop()
            logger.debug("开场即剧情（%s）：跳过开场陪伴字幕", SCENE_TITLES.get(scene, scene))
        if not self._due(scene):
            return
        line = self._pick(scene)
        if not line:
            return
        self._last_spoken[scene] = time.monotonic()
        logger.debug("刷剧陪伴开口（%s）：%s", SCENE_TITLES.get(scene, scene), line)
        self._bus.pet_speak_proactive.emit(line)

    def _due(self, scene: str) -> bool:
        """这个场景这会儿该不该冒话（同态冷却）。"""
        last = self._last_spoken.get(scene)
        if last is None:
            return True
        cooldown = max(0, int(self._config.screen.scene_cooldown_min)) * 60
        return time.monotonic() - last >= cooldown

    def _pick(self, scene: str) -> str:
        """从这一池里挑一句，尽量跟上次挑的不同（轮着来）。"""
        pool = SCENE_LINES.get(scene)
        if not pool:
            return ""
        index = self._cursor.get(scene, 0) % len(pool)
        self._cursor[scene] = index + 1
        line = pool[index]
        if is_crisis(line):
            # 池子里的话不可能触发危机，但红线不跳步：真中了就换下一句
            logger.warning("陪伴台词 %r 命中危机词，跳过", line)
            self._cursor[scene] = index + 2
            return ""
        return line
