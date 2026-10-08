"""
游戏陪玩（生活助手 · 攻关提示）。

陪玩模式开着时：每 N 秒（默认 5 秒）在本地截一帧 → 只在本地比"画面变没变" →
真变了、且距上次建议够久，才把这一帧发给能看图的模型，让它说 1~2 句提示。

几条约定：
- 截图只在内存里过一下，不落盘；发给模型的是压缩后的小图；
- 截屏前把宠物自己那块涂掉，免得模型对着猫点评；
- 只截鼠标所在的那块屏幕；
- 模型觉得没什么可说的（菜单、加载中）就回 [SILENT]，那就保持安静；
- 陪玩模式只在本次运行有效（重启就关），想玩时说一句"开始陪玩"就行。

开关走聊天（聊天是主入口）：bootstrap 把 user_command 里的 start/stop_playmate 接到这里。
"""

from __future__ import annotations

import logging
import time
from typing import Callable

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, QObject, QRect, QTimer, Qt
from PySide6.QtGui import QColor, QCursor, QGuiApplication, QImage, QPainter

from brain.chains.playmate import PlaymateChain
from brain.chains.proactive import is_silent
from core.config import Config
from core.events import EventBus
from core.worker import run_async

logger = logging.getLogger(__name__)

SIGN_WIDTH = 64        # 本地比对用的缩略图尺寸
SIGN_HEIGHT = 36
JPEG_QUALITY = 72      # 发给模型前的压缩质量
MAX_FAILURES = 3       # 连续失败这么多次就自己关掉，别一直对着坏接口喊


def grab_screen(pet_rect: QRect | None = None) -> QImage | None:
    """截当前屏幕（鼠标所在那块）；pet_rect 那块涂白。取不到画面返回 None。"""
    screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
    if screen is None:
        return None
    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        return None
    image = pixmap.toImage().convertToFormat(QImage.Format_RGB32)
    if pet_rect is not None and not pet_rect.isNull() and not pet_rect.isEmpty():
        painter = QPainter(image)
        painter.fillRect(
            pet_rect.translated(-screen.geometry().topLeft()), QColor(255, 255, 255)
        )
        painter.end()
    return image


def encode_jpeg(config: Config, image: QImage) -> bytes:
    """按配置的宽度上限缩放，再压成 JPEG（省流量省钱）。"""
    max_width = max(320, int(config.vision.max_width))
    scaled = (
        image
        if image.width() <= max_width
        else image.scaledToWidth(max_width, Qt.SmoothTransformation)
    )
    buffer = QBuffer()
    buffer.open(QIODevice.WriteOnly)
    scaled.save(buffer, "JPG", JPEG_QUALITY)
    return bytes(buffer.data())


def screen_signature(image: QImage) -> np.ndarray:
    """缩成很小的灰度网格，用来判断"画面变了没"。"""
    small = image.scaled(
        SIGN_WIDTH, SIGN_HEIGHT, Qt.IgnoreAspectRatio, Qt.FastTransformation
    ).convertToFormat(QImage.Format_Grayscale8)
    raw = np.frombuffer(bytes(small.constBits()), dtype=np.uint8)
    rows = raw.reshape(SIGN_HEIGHT, small.bytesPerLine())[:, :SIGN_WIDTH]
    return rows.astype(np.int16)


def grab_screen_jpeg(config: Config, pet_rect: QRect | None = None) -> bytes:
    """截一帧当前画面并编码成发给模型的 JPEG（设置页「测试看图」用）。"""
    image = grab_screen(pet_rect)
    return b"" if image is None else encode_jpeg(config, image)


class PlaymateService(QObject):
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        chain: PlaymateChain,
        app_name: Callable[[], str],
        pet_rect: Callable[[], QRect],
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._chain = chain
        self._app_name = app_name
        self._pet_rect = pet_rect

        self._active = False
        self._busy = False            # 正在等模型回话，这期间不再截屏
        self._last_sign: np.ndarray | None = None
        self._last_advice = 0.0
        self._failures = 0

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._tick)

    # ---------- 开关 ----------

    @property
    def is_active(self) -> bool:
        return self._active

    def start(self) -> None:
        self._timer.setInterval(max(1, int(self._config.vision.sample_sec)) * 1000)
        self._last_sign = None
        self._last_advice = 0.0
        self._failures = 0
        self._active = True
        self._timer.start()
        self._bus.playmate_changed.emit(True)
        logger.info("陪玩模式已打开：每 %d 秒看一眼画面", self._config.vision.sample_sec)

    def stop(self) -> None:
        if not self._active:
            return
        self._active = False
        self._timer.stop()
        self._bus.playmate_changed.emit(False)
        logger.info("陪玩模式已关闭")

    # ---------- 每一拍 ----------

    def _tick(self) -> None:
        if not self._active or self._busy:
            return
        frame = self._capture()
        if frame is None:
            return
        image_bytes, sign = frame
        diff = self._diff(sign)
        self._last_sign = sign
        if diff < self._config.vision.change_threshold:
            return
        gap = time.monotonic() - self._last_advice
        if gap < self._config.vision.min_gap_sec:
            logger.debug("画面变了（%.1f）但距上次建议才 %.0f 秒，先不说", diff, gap)
            return
        logger.info("画面变化 %.1f，交给模型看一眼", diff)
        self._last_advice = time.monotonic()
        self._busy = True
        run_async(
            self._chain.advise,
            self._app_name(),
            image_bytes,
            on_done=self._on_advice,
            on_failed=self._on_failed,
        )

    # ---------- 截屏 ----------

    def _capture(self) -> tuple[bytes, np.ndarray] | None:
        """截当前屏幕（鼠标所在那块），返回（发给模型的 JPEG, 本地比对用的缩略图）。"""
        image = grab_screen(self._pet_rect())
        if image is None:
            return None
        return encode_jpeg(self._config, image), screen_signature(image)

    def _diff(self, sign: np.ndarray) -> float:
        if self._last_sign is None:
            return 999.0      # 刚开陪玩：先看一眼当前画面
        return float(np.abs(sign - self._last_sign).mean())

    # ---------- 模型回话 ----------

    def _on_advice(self, text: str) -> None:
        self._busy = False
        self._failures = 0
        if is_silent(text):
            logger.info("陪玩：这张画面没什么可说的")
            return
        logger.info("陪玩提示：%s", text)
        self._bus.pet_speak_proactive.emit(text)

    def _on_failed(self, exc: Exception) -> None:
        self._busy = False
        self._failures += 1
        logger.error("陪玩看图失败（第 %d 次）: %s", self._failures, exc)
        if self._failures < MAX_FAILURES:
            return
        # 一直失败就别对着坏接口喊了，先关掉并告诉用户
        self.stop()
        self._bus.pet_speak_proactive.emit(f"陪玩先关掉了，看图那边一直出错：{exc}")
