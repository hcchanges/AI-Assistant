"""本地语音识别 + 粗粒度韵律特征（v2-asr-local）。

**边界**：只走本地。这里没有 API 兜底、不发任何网络请求——约会现场的对话原文
不出本机。识别后端用 faster-whisper（CTranslate2 量化，CPU int8）；没装 / 没模型
就优雅降级成"没认到字"，绝不抛异常。

输出三样东西：

1. `text`：认出来的文字；
2. `emotion_label` / `naturalness`：**按韵律算的粗标签，不是情绪模型**——
   语速、能量、停顿比例三个可解释的量，够规则层做参考，不冒称"读心"；
3. `as_state()`：`{asr, text, signals}`，给⑤批聚合。

原始音频只以 WAV 路径进来，读完即走：结果里没有音频、没有路径，实例上也不留。
"""

from __future__ import annotations

import logging
import math
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

LANGUAGE = "zh"
PAUSE_SEC = 0.40          # 两段之间超过它算一次停顿
MIN_ANALYSIS_SEC = 0.8    # 太短的一段不做韵律判断


@dataclass(frozen=True)
class AsrSegment:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Transcript:
    """后端交回来的原始识别结果（只有文字与时间，没有音频）。"""

    text: str
    segments: tuple[AsrSegment, ...] = ()
    avg_logprob: float | None = None
    energy: float | None = None       # 0~1 的均方根能量；后端不给就由 WAV 现算


@dataclass(frozen=True)
class AsrFeatures:
    """一窗语音的结构化结果。不知道的格子是 None，不是 0。"""

    available: bool = False
    text: str = ""
    duration_sec: float | None = None
    speech_rate: float | None = None        # 字/秒
    pause_ratio: float | None = None        # 停顿时长 / 总时长
    energy: float | None = None             # 0~1
    confidence: float | None = None         # avg_logprob → 0~1
    emotion_label: str = "unknown"          # calm / engaged / tense / low / unknown
    naturalness: float | None = None        # 0~1，粗粒度
    provider: str = ""
    degraded_reason: str = ""

    def as_signals(self) -> dict[str, Any]:
        """只交规则层认得的格子；没有语音就报 silence，其余不硬猜。"""
        if self.available and not self.text and (self.duration_sec or 0) > 0:
            return {"silence": True}
        return {}

    def as_dict(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "text": self.text,
            "duration_sec": self.duration_sec,
            "speech_rate": self.speech_rate,
            "pause_ratio": self.pause_ratio,
            "energy": self.energy,
            "confidence": self.confidence,
            "emotion_label": self.emotion_label,
            "naturalness": self.naturalness,
            "provider": self.provider,
            "degraded_reason": self.degraded_reason,
        }

    def as_state(self) -> dict[str, Any]:
        return {"asr": self.as_dict(), "text": self.text, "signals": self.as_signals()}

    @classmethod
    def missing(cls, reason: str, provider: str = "") -> "AsrFeatures":
        return cls(available=False, provider=provider, degraded_reason=reason)


class AsrBackend(Protocol):
    """本地识别后端：给 WAV 路径，吐文字 + 段时间。"""

    name: str

    def transcribe(self, wav_path: Path) -> Transcript | None: ...


def resolve_model_dir(model_dir: Any) -> Path | None:
    """找到能直接喂 faster-whisper 的 CTranslate2 目录。

    两种给法都认：直接给模型目录（里面有 model.bin + config.json），
    或者给模型根目录（底下某个子目录是 CTranslate2 格式）。
    """
    if model_dir is None or str(model_dir).strip() == "":
        return None
    root = Path(model_dir)
    if (root / "model.bin").is_file() and (root / "config.json").is_file():
        return root
    if not root.is_dir():
        return None
    for child in sorted(item for item in root.iterdir() if item.is_dir()):
        if (child / "model.bin").is_file() and (child / "config.json").is_file():
            return child
    return None


class FasterWhisperBackend:
    """faster-whisper 的薄适配（懒加载；没装时构造抛异常，由上层降级）。"""

    name = "faster-whisper"

    def __init__(self, model_dir: Any):
        from faster_whisper import WhisperModel  # 延迟到要用时才 import

        resolved = resolve_model_dir(model_dir)
        if resolved is None:
            raise FileNotFoundError("模型目录里没有 CTranslate2 格式的 whisper 权重")
        self._model = WhisperModel(str(resolved), device="cpu", compute_type="int8")

    def transcribe(self, wav_path: Path) -> Transcript | None:
        segments, _info = self._model.transcribe(
            str(wav_path), language=LANGUAGE, vad_filter=True
        )
        parts: list[AsrSegment] = []
        logprobs: list[float] = []
        for segment in segments:
            text = str(getattr(segment, "text", "") or "").strip()
            if not text:
                continue
            parts.append(AsrSegment(
                start=float(getattr(segment, "start", 0.0) or 0.0),
                end=float(getattr(segment, "end", 0.0) or 0.0),
                text=text,
            ))
            logprob = getattr(segment, "avg_logprob", None)
            if logprob is not None:
                logprobs.append(float(logprob))
        text = "".join(part.text for part in parts).strip()
        avg = sum(logprobs) / len(logprobs) if logprobs else None
        return Transcript(text=text, segments=tuple(parts), avg_logprob=avg)


def faster_whisper_available() -> bool:
    try:
        from importlib.util import find_spec

        return find_spec("faster_whisper") is not None
    except Exception:      # noqa: BLE001
        return False


def read_wav_stats(wav_path: Any) -> tuple[float | None, float | None]:
    """WAV → (时长秒, 0~1 均方根能量)。只读内存，不落任何副本；算不出就给 None。"""
    try:
        with wave.open(str(wav_path), "rb") as handle:
            channels = int(handle.getnchannels())
            width = int(handle.getsampwidth())
            rate = int(handle.getframerate())
            frames = int(handle.getnframes())
            raw = handle.readframes(frames)
    except Exception:      # noqa: BLE001 —— 不是 WAV / 文件坏了，都不算错误
        return None, None
    duration = (frames / float(rate)) if rate > 0 else None
    if width != 2 or channels <= 0 or not raw:
        return duration, None
    samples = array("h")
    samples.frombytes(raw[: len(raw) - (len(raw) % 2)])
    if not samples:
        return duration, None
    total = 0.0
    count = 0
    for index in range(0, len(samples), channels):
        chunk = samples[index:index + channels]
        total += sum(float(value) for value in chunk) / len(chunk)
        count += 1
    if count == 0:
        return duration, None
    mean = total / count
    # 第二遍算均方根（第一遍的均值用来去掉直流偏置）
    square = 0.0
    for index in range(0, len(samples), channels):
        chunk = samples[index:index + channels]
        value = sum(float(item) for item in chunk) / len(chunk) - mean
        square += value * value
    rms = math.sqrt(square / count) / 32768.0
    return duration, max(0.0, min(1.0, rms))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, float(value)))


def analyze_transcript(
    transcript: Transcript,
    duration_sec: float | None = None,
    energy: float | None = None,
) -> AsrFeatures:
    """纯函数：识别结果 → 语速 / 停顿 / 能量 / 粗情绪 / 自然度。测试直接喂假结果。"""
    text = str(transcript.text or "").strip()
    segments = tuple(transcript.segments or ())
    segment_end = max((seg.end for seg in segments), default=0.0)
    duration = float(duration_sec) if duration_sec else (segment_end or None)
    if duration is not None and duration <= 0:
        duration = None

    speech_rate = None
    pause_ratio = None
    if text and duration:
        speech_rate = round(len(text) / duration, 2)
    if duration and segments:
        gap = 0.0
        for previous, current in zip(segments, segments[1:]):
            silence = max(0.0, float(current.start) - float(previous.end))
            if silence >= PAUSE_SEC:          # 换气级别的小缝不算停顿
                gap += silence
        pause_ratio = round(_clamp(gap / duration), 3)

    energy_value = transcript.energy if transcript.energy is not None else energy
    if energy_value is not None:
        energy_value = round(_clamp(float(energy_value)), 4)
    confidence = None
    if transcript.avg_logprob is not None:
        confidence = round(_clamp(math.exp(float(transcript.avg_logprob))), 3)

    emotion_label = "unknown"
    naturalness = None
    if text and duration and duration >= MIN_ANALYSIS_SEC and speech_rate is not None:
        rate_score = 1.0 - min(1.0, abs(speech_rate - 4.0) / 4.0)
        pause_score = 1.0 - (pause_ratio if pause_ratio is not None else 0.0)
        energy_score = _clamp((energy_value or 0.0) / 0.20)
        weights = [(rate_score, 0.6), (pause_score, 0.4)]
        if energy_value is not None:
            weights = [(rate_score, 0.5), (pause_score, 0.2), (energy_score, 0.3)]
        naturalness = round(
            _clamp(sum(score * weight for score, weight in weights)
                   / sum(weight for _, weight in weights)), 3
        )
        if speech_rate >= 6.5 or (energy_value is not None and energy_value >= 0.35
                                  and speech_rate >= 5.0):
            emotion_label = "tense"
        elif speech_rate < 1.5 and (energy_value is None or energy_value < 0.06):
            emotion_label = "low"
        elif (speech_rate >= 3.0 and energy_value is not None
              and energy_value >= 0.05):
            emotion_label = "engaged"
        else:
            emotion_label = "calm"

    return AsrFeatures(
        available=True,
        text=text,
        duration_sec=round(duration, 3) if duration is not None else None,
        speech_rate=speech_rate,
        pause_ratio=pause_ratio,
        energy=energy_value,
        confidence=confidence,
        emotion_label=emotion_label,
        naturalness=naturalness,
        provider=str(getattr(transcript, "provider", "") or ""),
    )


class DateAsr:
    """对外的本地 ASR 入口：只认 WAV 路径，自动降级，绝不接异常。"""

    def __init__(self, model_dir: Any = None, backend: AsrBackend | None = None,
                 use_faster_whisper: bool = True):
        self._backend = backend
        self._degraded = ""
        if self._backend is None and use_faster_whisper:
            try:
                self._backend = FasterWhisperBackend(model_dir)
            except Exception as exc:      # noqa: BLE001 —— 没装/没权重都算降级
                self._degraded = f"本地 ASR 不可用：{exc}"
                logger.info("语音：%s，本批按“没认到字”降级", self._degraded)
        elif self._backend is None:
            self._degraded = "没给本地 ASR 后端"

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "name", "") or "")

    @property
    def degraded_reason(self) -> str:
        return "" if self.available else (self._degraded or "没有可用的本地 ASR 后端")

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend_name,
            "reason": self.degraded_reason,
        }

    def transcribe(self, wav_path: Any) -> AsrFeatures:
        """WAV → 特征。音频读完即走，实例上不留路径/音频。"""
        if self._backend is None:
            return AsrFeatures.missing(self.degraded_reason)
        path = Path(str(wav_path))
        if not path.is_file():
            return AsrFeatures.missing(f"音频文件不存在：{path.name}", self.backend_name)
        duration, energy = read_wav_stats(path)
        try:
            transcript = self._backend.transcribe(path)
        except Exception as exc:      # noqa: BLE001 —— 这一窗失败只算没认到
            logger.debug("语音识别这一窗失败：%s", exc)
            return AsrFeatures.missing(f"识别异常：{exc}", self.backend_name)
        if transcript is None:
            return AsrFeatures.missing("后端没返回结果", self.backend_name)
        features = analyze_transcript(transcript, duration_sec=duration, energy=energy)
        return AsrFeatures(**{**features.as_dict(), "provider": self.backend_name})

    def close(self) -> None:
        self._backend = None


__all__ = [
    "AsrSegment",
    "Transcript",
    "AsrFeatures",
    "AsrBackend",
    "FasterWhisperBackend",
    "DateAsr",
    "analyze_transcript",
    "read_wav_stats",
    "resolve_model_dir",
    "faster_whisper_available",
    "PAUSE_SEC",
]
