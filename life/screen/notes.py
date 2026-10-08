"""抓屏笔记（C 多模态 · 笔记先行版）：网课/工作时隔一阵把窗口认成字、提炼成笔记。

三段：S1 取景（life/screen/shot.py）→ S2 提炼（复用 distill 那条 chat_json 通道）
→ S3 落 <笔记文件夹>/<日期>.md / S4 问一句再发（不自动发）。

触发条件：**在番茄钟专注态里**（`is_focusing`，装配层传 `Bootstrap._is_focusing`：
手动点「开始专注」**或**前台落在网课 / 创作 / 工作类软件）。人在干别的（刷剧、写日记、
随便逛逛）一拍都不抓——这是"只在专注被动场景触发"的红线落点。

v2-scene-state 多了一条**并集**（`work_ok`）：工作态里宠物问了「我可以帮你整理笔记，
需要吗？」，用户点「好呀」之后这一场随时整理。这一条不是把红线拆了——它由场景状态机
给，而那边只在**前台是工作/网课/创作页**时才成立（`scene_state.notes_allowed`），所以
用户在看剧、闲逛时点头也不会抓；松的只是"必须同时挂着番茄钟"这一条。

v2-notes-focus 补上的是"手动番茄钟专注"这一条：原来装配层只看自动那套
`screen_focus_timer.running`，用户手动点「开始专注」后自动那套根本没被前台触发，
于是整个专注期间抓屏笔记一个字都不记。

v2-notes-cadence 把"抓得多、整理得碎、问得勤"这三件事一起收住：

- **静止就停拍**：最近 `STILL_STOP_S` 秒没有键鼠操作 = 人在盯一屏静态内容，周期拍整个
  停下；换屏（`snap_now`）与滑屏（`_on_cadence`，10s 节流）那两条事件路照旧；
- **攒满一轮才整理**：抓够 `BATCH_SIZE` 份才把认到的字拼起来提炼一次。原来每抓一帧就
  提炼一次、每提炼一次就可能问一句——用户不滑屏也被连着问"发不发笔记"；
- **一周只问一次**：问过就把时间戳落到 `ASK_STATE_FILE`，`ASK_INTERVAL_DAYS` 天之内
  不再问第二遍。整理可以有很多轮，问只发生在一周里的第一轮。

几件刻意不做的事：
- **不主动抓、不定时无脑抓**：`tick` 先问 `is_focusing`，不是专注态直接返回；
- **危机帧跳过**：识别出的字里踩到危机词，这一拍整张丢掉，不记、不提炼（跟聊天那条
  线的危机优先一致：那种内容不该被攒成一条"笔记"）；
- **原图不长期落盘**：截图只在内存 + 一个临时文件里过一趟，提炼完立刻删；
- **不进画像库、不写 pet.db / mind.db**：笔记只落那个「笔记」文件夹（默认在项目上一级，
  设置页可改），`MindStore` 一个字不碰——屏幕内容永远不进关系/情绪/画像那条加密库。

没装 OCR（RapidOCR）或没配模型时，这一环安静降级：认不出字就这一拍不产出，不报错、
不打断任何别的线。

v2-focus-mode-freeze-fix 把"不打断任何别的线"那句承诺兑现了：这一环起，**取帧留界面
线程、认字丢线程池**（`_capture` → `_read_screen`，跟 `life/screen/monitor.py` 的
`read_array` / `life/screen/social.py` 的 `_read_social_screen` 同一个写法）。修前
`self._reader.read(image)` 是**同步**压在界面线程上的，RapidOCR 一次约 4 秒，而专注态
里"进专注 / 换屏 / 每 10 秒一拍"三条路都往 `snap_now` 上撞——真机上就是"专注时弹出
对话框之后卡死"。认字在飞期间（`_capturing`）这一拍直接不抓，不叠队。
"""

from __future__ import annotations

import json
import logging
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Qt
from PySide6.QtGui import QImage, qGray

from brain.llm.base import LLMOfflineError
from brain.llm.factory import LLMFactory
from brain.prompts.templates import SCREEN_NOTES_PROMPT
from core.config import Config
from core.events import EventBus
from core.worker import run_async
from life.psych import crisis
from life.screen import shot
from life.screen.monitor import OcrReader
from monitor import foreground

logger = logging.getLogger(__name__)

# 用户点了"好呀"那一类：跟聊天气泡轻按钮的口径一致
YES_WORDS = ("好呀", "好的", "好啊", "好", "要", "嗯", "嗯嗯", "可以", "发")

TITLE_EMPTY = "（这一屏没看出在讲什么）"

# 抓屏节奏（v2-notes-cadence）：专注态下，滚动/活动时 10 秒拍一帧，停活动 3 秒回到
# 5 分钟周期兜底。滚动检测用 GetLastInputInfo（"最近有键鼠操作"）近似——零新依赖，
# 不引入滚轮 hook。打字也计入"活动"，在专注态下那是同样值得记的内容。
CADENCE_MS = 10_000
IDLE_EXIT_S = 3

# 静止停拍（v2-notes-cadence ①）：最近这么久没有键鼠操作 = 人在看一屏静态内容
# （没滑、没打字），**周期拍整个停掉**。不滑屏就不该一次一次抓同一屏，更不该顺着
# 抓取连着问"发不发笔记"。两个事件路不受这道闸管：换屏即拍（snap_now）、进专注即拍。
STILL_STOP_S = 60

# 批量整理（v2-notes-cadence ②）：抓满这么多份图片才整理一轮笔记。
# **不能是 1**：一帧一整理 = 每抓必打扰模型、每抓都可能问一句，真机上不滑屏也被连着
# 问好几条"发笔记"。一轮十屏也正好是"一节课里的一段"这个粒度。
BATCH_SIZE = 10

# 发笔记询问的最小间隔（v2-notes-cadence ③）：一周（7 天）之内只问一次。
# 整理可以有很多轮，问只发生在第一轮——用户嫌烦的是"问"，不是"记"。
ASK_INTERVAL_DAYS = 7
# 上次询问时间戳落在这儿。跟番茄钟存档（focus/pomodoro.STATE_FILE）同款做法：
# 一个项目内的小 JSON，不碰 pet.db / mind.db（那两条是红线）。
ASK_STATE_FILE = "cache/notes_state.json"

# 笔记去重（v2-notes-cadence）：均值哈希（aHash）64-bit，汉明距离 <=6 算雷同（约 90%
# 相似），滑屏时同一屏不反复记。8x8 灰度，直接用 QImage 算，不引图像库。
DUP_MAX_DIST = 6
RECENT_HASHES = 8


def _lines(items: Any) -> list[str]:
    """把模型给的数组搓成去空的字符串列表。"""
    if not isinstance(items, (list, tuple)):
        return []
    return [str(item).strip() for item in items if str(item).strip()]


def _parse_points(raw: Any) -> list[dict]:
    """把模型给的要点搓成 [(point, [detail, ...])] 的金字塔列表（v2-scene-state-fix ⑤）。

    兼容两种形态：旧的平铺字符串列表（当"要点、无细分论据"），和新的金字塔
    `{{"point": ..., "details": [...]}}` 对象列表（顶层结论下挂分层论据）。模型偶尔
    把某一条吐成字符串、另一条吐成对象也各按各的收，不整批丢掉。
    """
    result: list[dict] = []
    if not isinstance(raw, (list, tuple)):
        return result
    for item in raw:
        if isinstance(item, dict):
            point = str(item.get("point") or "").strip()
            details = _lines(item.get("details"))
        else:
            point = str(item).strip()
            details = []
        if point:
            result.append({"point": point, "details": details})
    return result


def _parse_stamp(raw: Any) -> datetime | None:
    """把存档里的时间戳读回 datetime。读不出来（没有 / 手改坏了）就当"没问过"。"""
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def _ahash(image) -> int:
    """均值哈希（aHash）：缩到 8x8 灰度，逐像素跟均值比大小，得 64-bit 指纹。

    零新依赖：直接用 QImage 的 scaled + pixel 读灰度，8x8 只有 64 个像素，快。
    """
    small = image.scaled(8, 8, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    gray = small.convertToFormat(QImage.Format_Grayscale8)
    pixels = [qGray(gray.pixel(x, y)) for y in range(8) for x in range(8)]
    avg = sum(pixels) / len(pixels)
    digest = 0
    for index, value in enumerate(pixels):
        if value >= avg:
            digest |= 1 << (63 - index)
    return digest


def _hamming(a: int, b: int) -> int:
    """两个 64-bit 指纹的汉明距离（不同位的个数）。"""
    return (a ^ b).bit_count()


class ScreenNotesService(QObject):
    """专注态下定时抓前台窗口 → 认字 → 提炼成笔记（只内存 + 笔记文件）。"""

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        is_focusing: Callable[[], bool],
        work_ok: Callable[[], bool] | None = None,
        grabber: Callable[..., Any] | None = None,
        reader: OcrReader | None = None,
        pet_rect: Callable[[], Any] | None = None,
        now_provider: Callable[[], datetime] = datetime.now,
        idle_provider: Callable[[], float] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._is_focusing = is_focusing
        # "这一场用户点过头了没有"（v2-scene-state）：工作态里宠物问了「我可以帮你整理
        # 笔记，需要吗？」，点头之后这一场就随时整理。**不是把专注那道闸拆了**——它由
        # 场景状态机给，而那边只在**前台是工作/网课/创作页**时才认（见 scene_state 的
        # `notes_allowed`），所以刷剧、闲逛时点头也不会抓。"只在网课/工作时抓"这条纪律
        # 一个字没松，松的只是"必须同时挂着番茄钟"这一条。
        self._work_ok = work_ok if work_ok is not None else (lambda: False)
        self._grabber = grabber if grabber is not None else shot.grab_foreground_window
        self._reader = reader if reader is not None else OcrReader()
        self._pet_rect = pet_rect if pet_rect is not None else (lambda: None)
        self._now = now_provider
        # "最近多久没键鼠操作"。装配层不传就用真的 GetLastInputInfo（见 `_idle_seconds`）；
        # 测试里可以注进来，不然"静止停拍"那条判断会跟着跑测试的人手有没有动而时红时绿。
        self._idle = idle_provider

        self._timer = QTimer(self)
        self._timer.timeout.connect(self.tick)
        self._running = False        # 上一次提炼还没回来就先不发新的，别把线程池堆满
        # 这一拍的认字还在后台线程里（v2-focus-mode-freeze-fix 14-A）：在飞期间不抓新
        # 的一拍。"进专注 / 换屏 / 10 秒节拍"三处入口都可能叠在同一拍上，不挡就是排队
        # 占着线程池、拖着一串过期帧。
        self._capturing = False
        self._notes: list[str] = []          # 攒着还没发出去的笔记（成段的文字）
        self._awaiting = False               # 已经问过用户"要不要发"，等答复
        # 批量整理（v2-notes-cadence ②）：认出字的那些屏先攒在这儿，够 BATCH_SIZE 才提炼
        self._buffer: list[str] = []
        self._inflight: list[str] | None = None      # 正在提炼的那一批（失败了好塞回去）
        self._last_ask_at: datetime | None = None    # 上一次问"要不要发"的时刻
        # 滚动节奏 + 去重（v2-notes-cadence）
        self._cadence_timer = QTimer(self)
        self._cadence_timer.setInterval(CADENCE_MS)
        self._cadence_timer.timeout.connect(self._on_cadence)
        self._recent_hashes: deque[int] = deque(maxlen=RECENT_HASHES)
        self._apply_interval()

    # ---------- 启停 ----------

    def _interval_ms(self) -> int:
        return max(1, int(self._config.notes.interval_min)) * 60 * 1000

    def _apply_interval(self) -> None:
        self._timer.setInterval(self._interval_ms())

    def start(self) -> None:
        if not self._config.notes.enabled:
            logger.info("抓屏笔记关着，不抓")
            return
        self._apply_interval()
        self._timer.start()
        self._cadence_timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._cadence_timer.stop()

    def apply_config(self) -> None:
        """设置页保存后热更新：总开关与间隔立刻生效。"""
        self._apply_interval()
        if self._config.notes.enabled:
            if not self._timer.isActive():
                self._timer.start()
            if not self._cadence_timer.isActive():
                self._cadence_timer.start()
        else:
            self.stop()

    @property
    def recording(self) -> bool:
        return self._timer.isActive()

    @property
    def pending(self) -> int:
        """攒着还没发出去几条笔记（**已整理成段的**，不是抓了几帧）。"""
        return len(self._notes)

    @property
    def buffered(self) -> int:
        """这一轮还差几份才够整理（v2-notes-cadence ②）。"""
        return len(self._buffer)

    @property
    def awaiting_confirm(self) -> bool:
        """已经问出口"要不要发"，正等用户答复。"""
        return self._awaiting

    # ---------- 一拍 ----------

    def _idle_seconds(self) -> float:
        """距离最后一次键鼠操作多少秒。注了 provider 就用它，否则现问系统一次。

        **不把 `foreground.idle_seconds` 在构造时抓成 bound method**：那样测试里
        换掉模块属性就不生效了，只能去改实例私有属性——多一条只有内部人才知道的规矩。
        """
        if self._idle is not None:
            return self._idle()
        try:
            return foreground.idle_seconds()
        except Exception:      # noqa: BLE001 —— 问不到就当"刚操作过"，别把周期拍卡死
            return 0.0

    def tick(self) -> None:
        """周期拍（5 分钟 timer）：**静止就停拍**（v2-notes-cadence ①）。

        不滑屏、不打字，说明人在盯一屏静态内容——这一拍不抓。事件驱动那两条路
        （换屏 `snap_now`、滑屏 `_on_cadence`）不走这道闸，见各自的说明。
        """
        if self._idle_seconds() > STILL_STOP_S:
            logger.info("抓屏笔记：%d 秒没有键鼠操作（静态屏），这一拍不抓", STILL_STOP_S)
            return
        self._capture()

    def _capture(self) -> None:
        """真抓一拍：只在专注态抓 → 取帧 → **认字丢后台** → 危机帧丢掉 → 攒进这一轮。

        取帧 / 指纹 / 落临时文件留在界面线程（抓屏本来就该在界面线程，而且只是拷内存），
        `self._reader.read` 那一趟（RapidOCR 一次约 4 秒，`monitor.OcrReader.read_array`
        的注释里记着这个数）交给线程池——压在界面线程上，专注态三处入口连着来就是"卡死"。

        在飞闸（`_capturing`）摆在这一处：`tick` / `snap_now` / `_on_cadence` 三个入口都
        从这儿过，挡一次等于三处都挡住，不必各写一份重复判断。
        """
        if not self._config.notes.enabled:
            return
        if self._running:
            return                    # 上一次还没提炼完
        if self._capturing:
            return                    # 上一拍的认字还没回来，别叠着抓
        if not self._can_capture():
            return                    # 不是网课/工作/创作：这一拍不抓
        image = self._grabber(self._pet_rect())
        if image is None:
            return
        # 去重（v2-notes-cadence）：这一帧跟最近帧雷同就不写，滑屏时同一屏不反复记
        digest = _ahash(image)
        if any(_hamming(digest, recent) <= DUP_MAX_DIST for recent in self._recent_hashes):
            logger.info("抓屏笔记：这一帧跟刚拍的雷同，跳过")
            return
        self._recent_hashes.append(digest)
        # 原图这一趟走完就删：落一次临时文件（主线程），认完字立刻丢弃（后台那趟里删）
        temp = shot.save_temp(image)
        self._capturing = True
        run_async(
            self._read_screen,
            image,
            temp,
            on_done=self._on_text,
            on_failed=self._on_read_failed,
        )

    def _read_screen(self, image, temp) -> str:
        """后台线程里跑：认字（一次约 4 秒），认完把这一趟的临时原图删掉。

        `QImage` 跨线程只读是安全的（它是值类型，不是 QPixmap；`OcrReader.read` 里那步
        `convertToFormat` / `constBits` 都只是拷内存），所以这里直接收图、不先转数组——
        `monitor` 那条采样链是先转数组再丢后台，两条都行，这条少一次搬运。临时文件的
        删除也放在这一趟里，省得那点磁盘 I/O 又回到界面线程上。
        """
        try:
            return (self._reader.read(image) or "").strip()
        finally:
            shot.discard(temp)

    def _on_text(self, text: str) -> None:
        """认字回来了（主线程回调）：危机帧丢掉 → 攒进这一轮，够 BATCH_SIZE 才提炼。"""
        self._capturing = False
        if not text:
            return                    # 认不出字（没装 OCR / 纯图）：这一拍不产出
        if crisis.is_crisis(text):
            logger.info("抓屏笔记：这一屏认到危机内容，跳过不记")
            return
        # 攒着，不立刻提炼（v2-notes-cadence ②）：一帧一整理就是"每抓必打扰"的那种吵。
        self._buffer.append(text)
        logger.info("抓屏笔记：这一屏认到字，这轮攒到 %d/%d 份", len(self._buffer), BATCH_SIZE)
        if len(self._buffer) < BATCH_SIZE:
            return
        batch = list(self._buffer)
        self._buffer.clear()
        self._running = True
        self._inflight = batch
        run_async(
            self._distill,
            "\n\n".join(batch),
            on_done=self._on_notes,
            on_failed=self._on_failed,
        )

    def _on_read_failed(self, exc: Exception) -> None:
        """这一拍认字没成（引擎炸了）：这一拍不产出，不报错、不打断别的线。"""
        self._capturing = False
        logger.warning("抓屏笔记：这一拍认字没成（%s），这一拍不产出", exc)

    def _can_capture(self) -> bool:
        """这会儿该不该抓（v2-scene-state 起是两条并集，判据本身没松）。

        专注态照旧；多出来的是"工作态里用户点过头"这一条——由场景状态机给，
        而它只在**前台是工作/网课/创作页**时才成立。两条都不成立就一拍不抓。
        """
        if self._is_focusing():
            return True
        try:
            return bool(self._work_ok())
        except Exception:      # noqa: BLE001 —— 问不出来就当没点头，别把抓屏这条线带崩
            return False

    def snap_now(self) -> None:
        """换屏 / 进入专注的瞬间立即抓一拍（不等 timer，v2-focus-notes）。

        **事件驱动，不过"静止停拍"那道闸**：换窗口本身就是键鼠操作，这一拍必须抓，
        否则"换屏即拍"就成了空话。其余判断（总开关 / 专注态 / 上一次没提炼完 / 上一拍
        的认字在飞）跟周期拍同一套——都在 `_capture` 里，所以它不会在不该抓的时候抓。
        """
        self._capture()

    def _on_cadence(self) -> None:
        """滚动/活动节奏（v2-notes-cadence）：专注态下最近有键鼠操作（滚动）就抓一帧。

        用 GetLastInputInfo 近似"在滚动/阅读"：最近 `IDLE_EXIT_S` 秒内有操作，就按
        `CADENCE_MS` 节流抓；停了 `IDLE_EXIT_S` 秒以上就自然不抓，回到 5 分钟 timer
        兜底（兜底那一路自己也过"静止停拍"的闸，见 `tick`）。零新依赖——不引入滚轮
        hook，打字也算"活动"（专注态下同样值得记）。
        """
        if (not self._config.notes.enabled or self._running
                or self._capturing or not self._can_capture()):
            return
        try:
            if self._idle_seconds() > IDLE_EXIT_S:
                return
        except Exception:      # noqa: BLE001 —— 拿不到空闲时长就当"没在滚动"，不抓
            return
        self._capture()

    # ---------- S2 提炼 ----------

    def _distill(self, text: str) -> dict[str, Any]:
        """后台线程里跑：复用 distill 那条 chat_json 通道，把认出的字整理成笔记。"""
        prompt = SCREEN_NOTES_PROMPT.format(text=text)
        provider = LLMFactory.create(self._config)
        data = provider.chat_json(prompt)
        return data if isinstance(data, dict) else {}

    def _on_notes(self, data: dict[str, Any]) -> None:
        self._running = False
        self._inflight = None
        title = str(data.get("title") or "").strip()
        conclusion = str(data.get("conclusion") or "").strip()
        points = _parse_points(data.get("key_points"))
        todos = _lines(data.get("todos"))
        questions = _lines(data.get("questions"))
        if not title and not conclusion and not points and not todos and not questions:
            return                    # 全是噪声：这一拍不产出
        block = self._render(title, conclusion, points, todos, questions)
        self._append_file(block)
        self._notes.append(block)
        logger.info("抓屏笔记：一轮整理完，攒到第 %d 条", len(self._notes))
        # 问"要不要发"（v2-notes-cadence ③）：一轮整理完才可能问，且一周只问一次。
        # 到这儿一定是"刚整理完一轮"——一次抓取就整理的老路子已经没有了。
        if self._awaiting:
            return                       # 上一问还没答复，不叠着问
        if not self._ask_cooldown_ok():
            logger.info("抓屏笔记：离上次问还不到 %d 天，这一轮不问", ASK_INTERVAL_DAYS)
            return
        self._awaiting = True
        self._remember_ask()
        self._bus.notes_offer.emit(len(self._notes))

    def _on_failed(self, exc: Exception) -> None:
        self._running = False
        logger.error("抓屏笔记：这一轮提炼失败: %s", exc)
        # 断网单独报一句（v2-offline-fallback），联网恢复后下一轮自动再试——下面那行
        # 塞回队首就是"再试"的机关，两件事并排，互不抢。
        if isinstance(exc, LLMOfflineError):
            self._bus.toast.emit("断网暂无法整理，联网后重试。")
        # 这一批塞回队首，下一轮凑够再试一次——十屏认出来的字不该因为一次模型抖动
        # 就白丢（原来一拍一整理，丢掉的最多是这一屏）。留满一轮的量，不无限回堆。
        if self._inflight:
            self._buffer[:0] = self._inflight
            self._inflight = None
            del self._buffer[BATCH_SIZE:]

    # ---------- 询问节奏：一周只问一次（v2-notes-cadence ③） ----------

    def _state_path(self) -> Path:
        return self._config.data_path(ASK_STATE_FILE)

    def _load_last_ask(self) -> datetime | None:
        """读上次询问的时间戳。没有 / 坏了都当"没问过"——宁可能问，不可永远不问。"""
        try:
            raw = json.loads(self._state_path().read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            logger.debug("抓屏笔记：询问存档读不出来（%s），当没问过", exc)
            return None
        return _parse_stamp(raw.get("last_ask_at")) if isinstance(raw, dict) else None

    def _ask_cooldown_ok(self) -> bool:
        """距离上次问够久了吗。没问过 → 可以问；问过不到 `ASK_INTERVAL_DAYS` 天 → 不问。"""
        if self._last_ask_at is None:
            self._last_ask_at = self._load_last_ask()
        if self._last_ask_at is None:
            return True
        return (self._now() - self._last_ask_at).total_seconds() >= ASK_INTERVAL_DAYS * 86400

    def _remember_ask(self) -> None:
        """记下"问过了"这一刻。写不进去也不拦着这轮问——退化成只在本次运行里记住。"""
        self._last_ask_at = self._now()
        try:
            path = self._state_path()
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(
                    {"last_ask_at": self._last_ask_at.isoformat(timespec="seconds")},
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            logger.error("抓屏笔记：询问时间戳写不进去（%s），这次只记在内存里", exc)

    # ---------- S3 落盘 ----------

    def _render(
        self,
        title: str,
        conclusion: str,
        points: list[dict],
        todos: list[str],
        questions: list[str],
    ) -> str:
        """按金字塔排（v2-scene-state-fix ⑤）：标题 → 顶层结论 → 分层要点/论据 → 待办/疑问。"""
        head = self._now().strftime("%H:%M")
        lines = [f"## {head}　{title or TITLE_EMPTY}"]
        if conclusion:
            lines.append(f"**结论**：{conclusion}")
        if points:
            lines.append("**要点**")
            for point in points:
                lines.append(f"- {point['point']}")
                for detail in point["details"]:
                    lines.append(f"  - {detail}")
        for label, items in (("待办", todos), ("疑问", questions)):
            if not items:
                continue
            lines.append(f"**{label}**")
            lines.extend(f"- {item}" for item in items)
        return "\n".join(lines)

    def _append_file(self, block: str) -> None:
        """追加进 <笔记文件夹>/<日期>.md。

        配置的那个文件夹写不进去（盘没挂 / 权限 / 路径是个文件……），**退到默认那个
        「笔记」文件夹再试一次**——笔记是用户主动抓屏挣来的东西，不该因为路径填错就
        静默丢掉（v2-notes-path）。两处都写不进去才只记日志，不抛：抓屏是被动线，
        磁盘写不进去不该打断别的事。
        """
        primary = Path(self._config.notes_dir)
        fallback = Path(self._config.notes_dir_default)
        targets = [primary] if primary == fallback else [primary, fallback]
        for root in targets:
            try:
                self._write_note(root, block)
                if root != primary:
                    logger.warning("抓屏笔记：%s 用不了，这条落到默认文件夹 %s", primary, root)
                return
            except OSError as exc:
                logger.error("抓屏笔记落盘失败（%s）: %s", root, exc)
        logger.error("抓屏笔记：这条没落盘（默认文件夹也写不进去）")

    def _write_note(self, root: Path, block: str) -> None:
        """写一个 md（当天的还没有就先写标题行）。写不进去由调用方兜底。"""
        path = root / f"{self._now():%Y-%m-%d}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        new_file = not path.exists()
        with path.open("a", encoding="utf-8") as f:
            if new_file:
                f.write(f"# {self._now():%Y-%m-%d} 抓屏笔记\n\n")
            f.write(block + "\n\n")

    # ---------- S4 问一句再发 ----------

    def confirm(self, answer: str) -> bool:
        """用户答复了"要不要发"。点头才发，别的都当不发。返回这次发没发。"""
        self._awaiting = False
        if not self._notes:
            return False
        if not self._is_yes(answer):
            # 不发：清掉这一批（笔记已在文件里，不代表要丢了）
            self._notes.clear()
            return False
        text = "\n\n".join(self._notes)
        self._notes.clear()
        self._bus.notes_delivered.emit(text)
        return True

    @staticmethod
    def _is_yes(answer: str) -> bool:
        a = (answer or "").strip()
        return any(word in a for word in YES_WORDS)
