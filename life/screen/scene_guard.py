"""场景互斥守卫（v2-mood-detect）：专注 / 游戏 / 观影 / 休闲，同一时刻只认一个。

为什么要单独有个守卫：看出"主人这会儿在干嘛"的线是**四条各听各的**——

- 自动番茄钟（`focus_timer.py`）听前台进程与浏览器标题判类；
- 游戏攻略（`game.py`）听前台进程名；
- 刷剧陪伴（`companion.py`）听前台进程、浏览器标题判类、以及采样器判出的场景；
- 打卡片/主动搭话那些线盯着专注态（`Bootstrap._is_focusing`）。

它们之间原先**没有一个地方知道"现在到底算哪一场"**。真机上那个 bug 就是这么来的：
番茄钟正跑着（专注），companion 那条线同时判定"在看片"，于是屏幕上一边是倒计时、
一边冒出「主人，看电影啦」。同一件事还有另一副面孔：全屏游戏窗口既是"游戏"也是
"全屏 = 像在看片"，两条线一起开口。

这里把"谁说了算"收成一个**只读的仲裁点**：优先级 专注 > 社交 > 游戏 > 观影 > 工作 >
休闲，靠前的赢，同一时刻只返回一个。社交那一档是 v2-social-scene-boundary 补的，
判据与理由写在 `PRIORITY` 上面那段。

三条纪律：

- **不新增采样**。守卫自己不截屏、不读标题、不听信号——它只是被上面那几条线喂
  状态、被它们回问"我这会儿该不该开口"。零开销、零新依赖。
- **专注与工作两档只读回调**。这两档不自己判，外部传 `is_focusing` / `is_working`
  回调进来（跟 `ScreenNotesService` 同款）：专注那一档读的是**手动番茄钟 + 自动番茄钟**
  合并后的"正在专注"，工作那一档读的是场景状态机那份"前台算不算工作/网课/创作页"，
  都不重算一套。
- **为什么这两档是回调、别的档是推过来的布尔值**（v2-scene-state 实测踩出来的）：
  别的档（游戏 / 观影）由各自的线在**同一拍里自己推**，推的时刻就是结论成立的时刻；
  而这两档的结论依赖"当前前台是哪一页"，而前台信号是**一条信号派发给所有线**的——
  工作页那条线排在中间某一位，排在它前面的线（看片、番茄钟）在派发半途就会问一次
  仲裁点，那时它手里的工作页标记还是**上一拍**的。真机上看到的样子：正在工作页上专注，
  一切走（番茄钟退出 + 前台换页），宠物先冒一句「我陪你一起工作」再回神——
  就是这条陈旧标记让"工作"在那一瞬间成立。回调是**问的时候现算**，怎么排都不会陈旧。
- **没接线就放行**。守卫默认 None（单测直接 new 一个 companion / GameGuideService
  时），拿不到就按"没有守卫"处理——宁可少一道闸，也别在没接线时把功能整个关掉。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QObject, Signal

# 六档的标识符。空档单独叫 idle——它不是"第零号场景"，是"现在什么都没在演"。
KIND_FOCUS = "focus"
KIND_SOCIAL = "social"
KIND_GAME = "game"
KIND_WATCH = "watch"
KIND_WORK = "work"
KIND_IDLE = "idle"

# 优先级：靠前的赢。专注最贵（用户正在做正事，别打扰），**社交紧随其后**
# （v2-social-scene-boundary，用户拍板的硬规则："只要置顶的是聊天窗口，就必须优先
# 处理社交回复——无论看电影 / 工作 / 游戏界面"），游戏次之（同一块全屏画面，是游戏
# 就该按游戏算，不是"像在看片"），观影再次，工作最弱（"前台是个工作页"只是弱证据，
# 它没占满屏、也没在播东西），其余休闲。
#
# 为什么社交排在专注**之后**而不是最前：这条链上"优先级"管的是**场景飘词**（开场白
# 与拦截），跟社交帮回本身无关——社交那条线（`social.py` 的邀请与自动监听）压根不问
# 仲裁点，置顶聊天窗口时它照常冒话、照常拟候选，专注在前面拦不到它。而「专注要安静」
# （v2-focus-quiet）是另一条钉死的红线，不该被这次改动掀掉。
PRIORITY: tuple[str, ...] = (KIND_FOCUS, KIND_SOCIAL, KIND_GAME, KIND_WATCH, KIND_WORK)


class SceneArbiter(QObject):
    """当前这一场到底是哪一场。喂状态 → 问结论。

    原先刻意不发信号（只是个查询点，结论由提问方自己拿去用）。v2-scene-state 之后
    多了**一个消费者**——场景状态机要按"这会儿是哪一场"改宠物的样子（坐起 / 开场白 /
    问一句），光靠问是问不出来的：没人问就没人知道该变。所以加了 `changed`。

    专注那一档仍然没有信号源（`is_focusing` 是个回调不是事件），所以它不进 setter，
    由装配层在"专注开合"的那两处显式喊一声 `notify_focus_changed()`——一处收口，
    不散在各处。
    """

    changed = Signal(str)      # 这会儿说了算的是哪一档（KIND_*），只在**变了**时发

    def __init__(self, is_focusing: Callable[[], bool] | None = None,
                 is_working: Callable[[], bool] | None = None,
                 is_social: Callable[[], bool] | None = None,
                 parent: QObject | None = None):
        super().__init__(parent)
        self._is_focusing = is_focusing
        self._is_working = is_working
        # 社交那一档也是**回调**（v2-social-scene-boundary），理由跟工作页那条一模一样：
        # 前台是一条信号派发给所有线的，把"是不是聊天窗口"推成布尔值的话，排在前面的线
        # 在派发半途问到的还是上一拍的值（真机那个"从工作页切走先冒了句话"就是这么来的）。
        self._is_social = is_social
        self._game = ""
        self._watch = False
        self._active = KIND_IDLE      # 上次发出去的那一档（只在变了时才发）

    # ---------- 喂状态 ----------

    def set_game(self, game: str) -> None:
        """游戏那条线报一声"前台现在是哪个游戏"（空串 = 不在游戏里）。"""
        self._game = game or ""
        self._publish()

    def set_watch(self, open_: bool) -> None:
        """观影那条线报一声"看片会话开着没有"。"""
        self._watch = bool(open_)
        self._publish()

    def notify_work_changed(self) -> None:
        """工作那条线报一声"前台换页了，重新看一眼"（v2-scene-state）。

        工作页那一档是**回调**（见类开头那段）：这里不发状态，只喊一句"该重算了"。
        场景状态机在前台换人 / 浏览器换标签页两处各喊一次。

        社交那一档（v2-social-scene-boundary）跟着这一声一起重算——它也是回调，判据
        同样是"前台现在是谁"，所以前台换人这一拍本来就该把两档一起重问一遍，不必再
        单开一个通知入口。
        """
        self._publish()

    def notify_focus_changed(self) -> None:
        """专注态变了（装配层在专注开合时喊一声）。"""
        self._publish()

    # ---------- 问结论 ----------

    @property
    def focusing(self) -> bool:
        if self._is_focusing is None:
            return False
        try:
            return bool(self._is_focusing())
        except Exception:      # noqa: BLE001 —— 专注态问不出来就当没在专注，别拖垮冒话
            return False

    @property
    def game(self) -> str:
        return self._game

    @property
    def watching(self) -> bool:
        return self._watch

    @property
    def social(self) -> bool:
        """置顶的是不是聊天窗口（或社交那条线正端着邀请 / 候选 / 草稿）。"""
        if self._is_social is None:
            return False
        try:
            return bool(self._is_social())
        except Exception:      # noqa: BLE001 —— 问不出来就当不是，别拖垮冒话
            return False

    @property
    def working(self) -> bool:
        if self._is_working is None:
            return False
        try:
            return bool(self._is_working())
        except Exception:      # noqa: BLE001 —— 同上，问不出来就当不在工作页
            return False

    @property
    def active(self) -> str:
        """这会儿说了算的是哪一档。同一时刻只会有一个。"""
        if self.focusing:
            return KIND_FOCUS
        if self.social:
            return KIND_SOCIAL
        if self._game:
            return KIND_GAME
        if self._watch:
            return KIND_WATCH
        if self.working:
            return KIND_WORK
        return KIND_IDLE

    def _publish(self) -> None:
        """算一遍当前这一档，跟上次发出去的不一样才发 `changed`。"""
        now = self.active
        if now == self._active:
            return
        self._active = now
        self.changed.emit(now)

    def allow(self, kind: str) -> bool:
        """`kind` 这一档这会儿能不能开。已经在演的那些里没有更靠前的，就准。

        `allow(KIND_WATCH)` 就是"专注中 / 游戏中不许冒「看电影啦」"那一条；
        `allow(KIND_GAME)` 是"专注中不弹攻略"（同一件事的另一面）。
        """
        if kind not in PRIORITY:
            return True
        current = self.active
        if current == KIND_IDLE or current == kind:
            return True
        return PRIORITY.index(current) > PRIORITY.index(kind)


__all__ = [
    "KIND_FOCUS", "KIND_GAME", "KIND_IDLE", "KIND_SOCIAL", "KIND_WATCH", "KIND_WORK",
    "PRIORITY", "SceneArbiter",
]
