"""按住说话这条线：按下开始收、松手去认、认出来交给聊天入口（C 多模态）。

它是采集（capture.Recorder）和识别（asr.recognize）之间的那层胶水，也只做胶水：

    按下  begin()  → 开始收音
    松手  finish() → 停采集 → 后台线程识别 → heard(文字)
    超时          → 录满 asr_max_sec 自动当松手（防着忘了松手一直开着麦克风）

认出来之后**不做任何加工**，原样 emit 出去交给 `chatter.handle`——那句话该怎么
路由、什么情绪、谁来回、要不要语音播报，全是既有那条线的事，这儿一个字都不插手。
所以这条线接上之后，说话和打字走的是**同一条路**，不会出现"语音问的它答得不一样"。

三条规矩：

- **不上模型**：这条线自己一个 token 都不生成，只把话原样递过去；
- **不落库**：音频只在系统临时目录过一下手（识别后端要文件），用完立即删；
  认出来的文字走既有那条线，该记的不该记的由那边决定；
- **不常驻**：麦克风开着的时间 = 按下到松手，其余时刻一律关着（见 capture.py）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer, Signal

from core.config import Config
from core.worker import run_async
from life.voice import asr
from life.voice.capture import Recorder

logger = logging.getLogger(__name__)

NO_SPEECH = "没听清，再说一遍？"
NO_ROUTE = "语音识别没走通（本机和 API 都没成），先打字吧"


class VoiceListener(QObject):
    """按住说话。界面上那个按钮 / 快捷键都调它这两个方法。"""

    heard = Signal(str)              # 认出来的话（交给聊天入口，别的什么都不做）
    failed = Signal(str)             # 没认出来 / 收不了音：给界面一句人能看懂的话
    listening_changed = Signal(bool)  # 正在收音？（界面拿它画状态）

    def __init__(
        self,
        config: Config,
        recorder: Recorder | None = None,
        recognizer: Callable[[Any, Path], asr.Result] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._recorder = recorder or Recorder(self)
        # 识别那一步单独一个口子：测试里换成假的（真模型几秒钟，还得有网/有权重）
        self._recognize = recognizer or asr.recognize
        self._busy = False
        # 录太久自动收尾：忘了松手不该让麦克风一直开着
        self._cap = QTimer(self)
        self._cap.setSingleShot(True)
        self._cap.timeout.connect(self._on_cap)

    # ---------- 对外 ----------

    def available(self) -> bool:
        """现在能不能说话。**不打开麦克风**，只问配置和设备在不在。"""
        if not self._config.voice.enabled or not self._config.voice.asr_enabled:
            return False
        return self._recorder.available()

    @property
    def listening(self) -> bool:
        return self._recorder.listening

    def begin(self) -> bool:
        """按下：开始收。收不了返回 False（调用方只管别切 UI 状态就行）。"""
        if self._busy:
            return False
        if not self.available():
            self.failed.emit("这台机器现在收不了音（没麦克风，或语音输入关着）")
            return False
        try:
            started = self._recorder.start()
        except Exception as exc:  # noqa: BLE001 —— 采集那条线的异常不该冒到界面上
            logger.warning("开始收音失败（%s）", exc)
            self.failed.emit("麦克风打不开，先打字吧")
            return False
        if not started:
            self.failed.emit("麦克风打不开，先打字吧")
            return False
        self._busy = True
        self._cap.start(max(int(self._config.voice.asr_max_sec), 1) * 1000)
        self.listening_changed.emit(True)
        return True

    def finish(self) -> None:
        """松手：停采集，把这一段丢给识别（后台线程，不卡界面）。"""
        self._cap.stop()
        if not self._busy:
            return
        self._busy = False
        self.listening_changed.emit(False)
        try:
            wav = self._recorder.stop()
        except Exception as exc:  # noqa: BLE001 —— 见上
            logger.warning("停止收音失败（%s）", exc)
            wav = b""
        if not wav:
            self.failed.emit(NO_SPEECH)
            return
        run_async(
            self._transcribe, wav,
            on_done=self._on_result,
            on_failed=lambda exc: self._on_error(exc),
        )

    def cancel(self) -> None:
        """不要这一段了（用户按了 Esc / 关掉面板）：停采集，也不去认。"""
        self._cap.stop()
        if not self._busy:
            return
        self._busy = False
        self.listening_changed.emit(False)
        try:
            self._recorder.stop()
        except Exception as exc:  # noqa: BLE001
            logger.warning("取消收音时关麦克风失败（%s）", exc)

    # ---------- 内部 ----------

    def _on_cap(self) -> None:
        """录满了：当用户松手处理（说一句"太长了"更好，但先别把麦克风开着）。"""
        if self._busy:
            logger.info("按住说话：录满 %d 秒自动收尾", self._config.voice.asr_max_sec)
            self.finish()

    def _transcribe(self, wav: bytes) -> asr.Result:
        """后台线程里做的事：落一个临时文件 → 认 → 立刻删掉那个文件。"""
        from life.voice.capture import write_temp

        path = write_temp(wav)
        try:
            return self._recognize(self._config, path)
        finally:
            try:
                path.unlink()
            except OSError as exc:  # noqa: BLE001 —— 删不掉只是留个临时文件，不该算失败
                logger.warning("临时音频删不掉（%s）", exc)

    def _on_result(self, result: asr.Result) -> None:
        if not result.ok:
            self.failed.emit(NO_SPEECH if result.source else NO_ROUTE)
            return
        logger.info("按住说话：认出来了（走的是%s）", result.source)
        self.heard.emit(result.text)

    def _on_error(self, exc: Exception) -> None:
        logger.error("语音识别这一趟挂了（%s）", exc)
        self.failed.emit(NO_ROUTE)
