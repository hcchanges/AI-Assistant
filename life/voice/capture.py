"""麦克风采集（C 多模态 · 听说闭环的入口）：按住才收音，松手吐一段 WAV。

**不常驻监听，这是刻意的。** 麦克风只在 `start()` 到 `stop()` 之间开着——没按键
就没有任何采集，也没有任何音频落盘。桌宠是个一直开着的程序，"一直听着"和
"按一下才听"是两种产品，这条线只做后者：

    按下说话（按钮 / 快捷键）→ start() → 松手 → stop() → WAV 字节 → 识别

采集用 Qt 自带的 `QAudioSource`（PySide6 已经在依赖里），**不为这一件事新增
任何依赖**：sounddevice / pyaudio 都不引入。采样率钉 16kHz 单声道 16 位，
这是 whisper 那一系模型的原生输入，多一步重采样都是白费。

麦克风不在了（没设备、被独占、驱动抽风）不是错误，是**预期内**的情况：
抛 `CaptureUnavailable` 或者返回一段空音频，调用方退回打字就完了。
"""

from __future__ import annotations

import logging
import wave
from array import array
from io import BytesIO
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QBuffer, QIODevice, QObject
from PySide6.QtMultimedia import QAudioFormat, QAudioSource, QMediaDevices

logger = logging.getLogger(__name__)

SAMPLE_RATE = 16000      # whisper 系的原生采样率
CHANNELS = 1
SAMPLE_WIDTH = 2         # 16 位
# 比这还短的一段（不到 0.3 秒）当"没听清"：手上滑了一下、点了两下都会走到这儿
MIN_SEC = 0.3
# 判定"这一段是静音"的门槛：峰值低于满量程的千分之三，就当没说话。
# 用峰值不用均值：屋里有点底噪时均值会被抬起来，峰值不会。
SILENCE_PEAK = 100


class CaptureUnavailable(RuntimeError):
    """这台机器现在收不了音（没麦克风 / 被占用 / 格式不支持）——退回打字。"""


class _Stream(Protocol):
    """一次采集会话的样子。**抽出来是为了测试能换掉它**（无头环境没有麦克风）。"""

    def read_all(self) -> bytes: ...

    def stop(self) -> None: ...


class _QtStream:
    """真家伙：QAudioSource 往内存缓冲里写，不落任何文件。"""

    def __init__(self, container: Any, fmt: QAudioFormat):
        self._container = container      # 被采集的音频"写进哪里"由它决定
        self._source = QAudioSource(container.device, fmt)
        self._source.start(container.buffer)

    def read_all(self) -> bytes:
        return bytes(self._container.buffer.data())

    def stop(self) -> None:
        self._source.stop()


class _Container:
    """一次采集的内存落点：一个设备、一个 QBuffer。"""

    def __init__(self, device: Any, buffer: Any):
        self.device = device
        self.buffer = buffer


class Recorder(QObject):
    """按住说话用的录音器。**不 start 就不收音**，就这么简单。"""

    def __init__(self, parent: QObject | None = None, stream_factory: Any = None):
        super().__init__(parent)
        self._stream: _Stream | None = None
        # 采集会话怎么开，单独一个口子：测试里换成假的（真麦克风在无头环境没有）
        self._stream_factory = stream_factory or self._open_stream

    # ---------- 探 ----------

    @property
    def listening(self) -> bool:
        """现在是不是正在收。界面靠它画"正在听"那个状态。"""
        return self._stream is not None

    def available(self) -> bool:
        """这台机器现在能不能录。只看有没有输入设备，**不打开麦克风**。"""
        try:
            return not QMediaDevices.defaultAudioInput().isNull()
        except Exception as exc:  # noqa: BLE001 —— 枚举设备出岔子就是"录不了"
            logger.warning("查询麦克风失败（%s）", exc)
            return False

    # ---------- 收 ----------

    def start(self) -> bool:
        """开始收。收不了返回 False（没设备 / 已经开着），调用方退回打字。"""
        if self._stream is not None:
            return False
        try:
            self._stream = self._stream_factory()
        except CaptureUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001 —— 设备被独占、驱动抽风，都是"录不了"
            logger.warning("麦克风打不开（%s）", exc)
            return False
        logger.info("按住说话：开始收音")
        return True

    def stop(self) -> bytes:
        """松手：返回这一段 WAV 的字节；太短或全是静音就给空串。

        一定经过 `stop()` 关掉采集——不管是正常松手还是异常退出，
        "不常驻监听"这条就是靠这里守住的。
        """
        stream, self._stream = self._stream, None
        if stream is None:
            return b""
        try:
            pcm = stream.read_all()
        except Exception as exc:  # noqa: BLE001 —— 读不出来就当没收到
            logger.warning("读采集缓冲失败（%s）", exc)
            pcm = b""
        finally:
            try:
                stream.stop()
            except Exception as exc:  # noqa: BLE001 —— 关不掉也得继续往下走
                logger.warning("关麦克风失败（%s）", exc)
        logger.info("按住说话：收完了，%d 字节", len(pcm))
        if not _usable(pcm):
            return b""
        return to_wav(pcm)

    # ---------- 内部 ----------

    def _open_stream(self) -> _Stream:
        """开一路真的采集。没设备就抛 `CaptureUnavailable`。"""
        device = QMediaDevices.defaultAudioInput()
        if device.isNull():
            raise CaptureUnavailable("这台机器没有麦克风")
        fmt = _pick_format(device)
        buffer = QBuffer()
        buffer.open(QIODevice.ReadWrite)
        return _QtStream(_Container(device, buffer), fmt)


def _pick_format(device: Any) -> QAudioFormat:
    """优先要 16k 单声道 16 位；设备不认就退回它自己的首选格式。"""
    wanted = QAudioFormat()
    wanted.setSampleRate(SAMPLE_RATE)
    wanted.setChannelCount(CHANNELS)
    wanted.setSampleFormat(QAudioFormat.Int16)
    if device.isFormatSupported(wanted):
        return wanted
    logger.info("麦克风不支持 16k 单声道，改用它自己的首选格式")
    return device.preferredFormat()


def _usable(pcm: bytes) -> bool:
    """这一段值不值得送去识别：够不够长、是不是全是静音。"""
    if len(pcm) < SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS * MIN_SEC:
        return False
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) - (len(pcm) % 2)])
    if not samples:
        return False
    peak = max(max(samples), -min(samples))
    return peak >= SILENCE_PEAK


def to_wav(pcm: bytes, sample_rate: int = SAMPLE_RATE) -> bytes:
    """裸 PCM → 一段 WAV 字节。识别后端要的是完整 WAV，不是裸流。"""
    out = BytesIO()
    with wave.open(out, "wb") as handle:
        handle.setnchannels(CHANNELS)
        handle.setsampwidth(SAMPLE_WIDTH)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)
    return out.getvalue()


def write_temp(wav: bytes, directory: Path | None = None) -> Path:
    """把这段 WAV 落到一个临时文件里，返回路径。

    落在系统临时目录、**不落在 data/ 里**——识别后端（whisper.cpp / openai SDK）
    要的是文件，不是字节流，所以只能过一下盘；调用方用完立即删掉。
    """
    import tempfile

    handle = tempfile.NamedTemporaryFile(
        prefix="desktoppet_asr_", suffix=".wav", dir=str(directory) if directory else None,
        delete=False,
    )
    try:
        handle.write(wav)
    finally:
        handle.close()
    return Path(handle.name)


def pcm_seconds(pcm: bytes) -> float:
    """这段 PCM 有多少秒（日志和"录太久了"那句提示用）。"""
    return len(pcm) / float(SAMPLE_RATE * SAMPLE_WIDTH * CHANNELS)


__all__ = [
    "CaptureUnavailable",
    "Recorder",
    "SAMPLE_RATE",
    "pcm_seconds",
    "to_wav",
    "write_temp",
    "_usable",
]
