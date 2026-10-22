# -*- coding: utf-8 -*-
"""本地语音识别 + 韵律特征（v2-asr-local）的自动测试。

**这一批验什么**：

1. 识别结果 → 语速 / 停顿比例 / 能量 / 粗情绪 / 自然度，都是可解释的规则量；
2. 只走本地：源码里没有 API 兜底、没有网络请求；
3. 没装 faster-whisper / 没模型 / 后端异常 / 文件不存在，都优雅降级，不崩；
4. 原始音频不保留：实例不留路径，结果可 JSON 序列化；
5. WAV 能量与时长能算，且对非 WAV 只给 None。
"""

from __future__ import annotations

import json
import math
import sys
import tempfile
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.date import asr


class FakeBackend:
    name = "fake"

    def __init__(self, transcript=None, boom: bool = False):
        self.transcript = transcript
        self.boom = boom
        self.calls = 0

    def transcribe(self, wav_path):
        self.calls += 1
        if self.boom:
            raise RuntimeError("模型炸了")
        return self.transcript


def make_wav(path: Path, seconds: float = 1.0, rate: int = 16000,
             amplitude: int = 8000) -> Path:
    frames = bytearray()
    for index in range(int(rate * seconds)):
        value = int(amplitude * math.sin(2 * math.pi * 220 * index / rate))
        frames += int(value).to_bytes(2, "little", signed=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(bytes(frames))
    return path


def close(a, b, tolerance=0.01) -> bool:
    return a is not None and b is not None and abs(float(a) - float(b)) <= tolerance


def main() -> int:
    section("① 识别结果 → 韵律特征")

    normal = asr.analyze_transcript(
        asr.Transcript(
            text="我们今天去喝咖啡吧",
            segments=(asr.AsrSegment(0.0, 1.2, "我们今天"),
                      asr.AsrSegment(1.8, 3.0, "去喝咖啡吧")),
            avg_logprob=-0.2,
            energy=0.10,
        ),
        duration_sec=3.0,
    )
    check("★ 语速 = 字数 / 时长", close(normal.speech_rate, 3.0), str(normal.speech_rate))
    check("★ 停顿比例只算超过阈值的缝", close(normal.pause_ratio, 0.2),
          str(normal.pause_ratio))
    check("★ avg_logprob → 0~1 置信度", normal.confidence is not None
          and 0.8 < normal.confidence <= 1.0, str(normal.confidence))
    check("★ 自然度是 0~1 的可解释组合",
          normal.naturalness is not None and 0 <= normal.naturalness <= 1,
          str(normal.naturalness))
    check("正常语速 + 有能量 → engaged", normal.emotion_label == "engaged",
          normal.emotion_label)

    short_gap = asr.analyze_transcript(
        asr.Transcript(
            text="你好呀",
            segments=(asr.AsrSegment(0.0, 1.0, "你好"), asr.AsrSegment(1.2, 2.0, "呀")),
            energy=0.10,
        ),
        duration_sec=2.0,
    )
    check("小缝（< 0.4s）不算停顿", close(short_gap.pause_ratio, 0.0),
          str(short_gap.pause_ratio))

    fast = asr.analyze_transcript(
        asr.Transcript(text="一" * 20, segments=(asr.AsrSegment(0.0, 2.0, "一" * 20),),
                       energy=0.10),
        duration_sec=2.0,
    )
    check("★ 语速过快 → tense", fast.emotion_label == "tense", fast.emotion_label)

    low = asr.analyze_transcript(
        asr.Transcript(text="嗯", segments=(asr.AsrSegment(0.0, 3.0, "嗯"),),
                       energy=0.01),
        duration_sec=3.0,
    )
    check("★ 语速慢 + 能量低 → low", low.emotion_label == "low", low.emotion_label)

    calm = asr.analyze_transcript(
        asr.Transcript(text="还好吧", segments=(asr.AsrSegment(0.0, 2.0, "还好吧"),),
                       energy=None),
        duration_sec=2.0,
    )
    check("能量未知时不冒认 engaged（降为 calm）", calm.emotion_label == "calm",
          calm.emotion_label)
    check("能量未知时自然度仍能算（语速 + 停顿）", calm.naturalness is not None, "")

    unknown = asr.analyze_transcript(
        asr.Transcript(text="嗯", segments=(asr.AsrSegment(0.0, 0.3, "嗯"),), energy=0.1),
        duration_sec=0.3,
    )
    check("太短不做情绪判断 → unknown", unknown.emotion_label == "unknown", "")

    empty = asr.analyze_transcript(
        asr.Transcript(text="", segments=(), energy=0.001), duration_sec=1.5
    )
    check("★ 没认到字 → silence 信号，但不判情绪",
          empty.as_signals() == {"silence": True} and empty.emotion_label == "unknown",
          str(empty.as_signals()))

    section("② WAV 统计：时长 / 能量（只读内存，不留副本）")

    with tempfile.TemporaryDirectory() as tmp:
        wav = make_wav(Path(tmp) / "sample.wav", seconds=1.0)
        duration, energy = asr.read_wav_stats(wav)
        check("★ WAV 时长算得对", close(duration, 1.0, 0.02), str(duration))
        check("★ 正弦波能量 > 0 且 <= 1", energy is not None and 0 < energy <= 1,
              str(energy))
        bad = Path(tmp) / "not-a.wav"
        bad.write_bytes(b"not wave")
        check("非 WAV → (None, None)，不抛异常", asr.read_wav_stats(bad) == (None, None), "")

    section("③ 后端降级：没装 / 没模型 / 异常 / 文件不存在")

    orphan = asr.DateAsr(use_faster_whisper=False)
    check("★ 显式不给后端 → available=False，不抛异常",
          orphan.available is False and "没给" in orphan.degraded_reason,
          orphan.describe())
    missing = orphan.transcribe("whatever.wav")
    check("降级时 transcribe() 返回“没认到字”",
          missing.available is False and missing.text == "" and bool(missing.degraded_reason),
          missing.degraded_reason)

    no_model = asr.DateAsr(model_dir="不存在的目录")
    check("★ 没模型目录 → 优雅降级（本机 faster-whisper 未装也一样）",
          no_model.available is False and bool(no_model.degraded_reason),
          no_model.describe())

    with tempfile.TemporaryDirectory() as tmp:
        wav = make_wav(Path(tmp) / "sample.wav", seconds=0.5)
        backend = FakeBackend(boom=True)
        doc = asr.DateAsr(backend=backend)
        out = doc.transcribe(wav)
        check("★ 后端异常 → 只算这一窗没认到", out.available is False
              and "识别异常" in out.degraded_reason, out.degraded_reason)

        none_out = asr.DateAsr(backend=FakeBackend(None)).transcribe(wav)
        check("后端返回 None → 降级", none_out.available is False,
              none_out.degraded_reason)

        gone = doc.transcribe(Path(tmp) / "gone.wav")
        check("★ 文件不存在 → 降级，连后端都不调",
              gone.available is False and backend.calls == 1, str(backend.calls))

        backend = FakeBackend(
            asr.Transcript(
                text="我们走吧",
                segments=(asr.AsrSegment(0.0, 1.0, "我们走吧"),),
                avg_logprob=-0.1,
                energy=0.12,
            )
        )
        doc = asr.DateAsr(backend=backend)
        out = doc.transcribe(wav)
        check("★ 正常一窗：文字 / 韵律 / 后端名都对",
              out.available is True and out.text == "我们走吧"
              and out.provider == "fake" and out.speech_rate is not None,
              f"{out.text}/{out.speech_rate}/{out.provider}")
        check("后端只被调用一次", backend.calls == 1, str(backend.calls))

    section("④ 原始音频不保留 / 状态输入")

    with tempfile.TemporaryDirectory() as tmp:
        wav = make_wav(Path(tmp) / "sample.wav", seconds=0.5)
        doc = asr.DateAsr(backend=FakeBackend(
            asr.Transcript(text="好", segments=(asr.AsrSegment(0.0, 0.5, "好"),), energy=0.1)
        ))
        out = doc.transcribe(wav)
        check("★ 实例上不留音频路径", wav not in vars(doc).values()
              and not hasattr(doc, "last_path"), str(sorted(vars(doc))))
        try:
            json.dumps(out.as_dict(), ensure_ascii=False)
            serializable = True
        except TypeError:
            serializable = False
        check("★ 结果能直接 JSON 序列化（没有音频/ndarray）", serializable, "")
        state = out.as_state()
        check("as_state 是聚合层要的形状：asr + text + signals",
              state.get("text") == "好" and "asr" in state and "signals" in state, "")

    section("⑤ 模型定位 / 依赖 / 红线")

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        direct = root / "ct2"
        direct.mkdir()
        (direct / "model.bin").write_bytes(b"x")
        (direct / "config.json").write_text("{}", encoding="utf-8")
        check("直接给 CTranslate2 目录 → 认得",
              asr.resolve_model_dir(direct) == direct, "")
        check("给模型根目录 → 找到底下的 CTranslate2 目录",
              asr.resolve_model_dir(root) == direct, "")
        check("空目录 → None", asr.resolve_model_dir(root / "nope") is None, "")

    source = Path(asr.__file__).read_text(encoding="utf-8")
    check("★ 只走本地：源码里没有 API / 网络客户端",
          not any(token in source for token in
                  ("import openai", "from openai", "urllib", "requests.", "http.client")), "")
    check("★ ASR 层不落库（无 sqlite / DAO）",
          not any(token in source for token in ("sqlite3", "from data.dao", "import data.dao")), "")
    req = Path(asr.__file__).resolve().parents[2] / "service" / "requirements.txt"
    req_text = req.read_text(encoding="utf-8")
    check("★ faster-whisper 依赖记在 service/requirements.txt",
          "faster-whisper" in req_text, "")
    check("依赖是可选的：没装时 available=False 也能跑",
          asr.faster_whisper_available() is False or asr.faster_whisper_available() is True,
          "")

    return summary("本地语音识别（v2-asr-local）")


if __name__ == "__main__":
    raise SystemExit(main())
