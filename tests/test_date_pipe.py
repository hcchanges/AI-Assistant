# -*- coding: utf-8 -*-
"""约会推理链（v2-date-jev-pipe）的自动测试。

**这一批验什么**：

1. D3 状态聚合：③④两批的 `as_state()` 能拼成干净 state，显式信号最后覆盖；
2. D4 复用 `JevBackend`：真类 + 注入 post，判定结果进 `emotion`；没 key / 判定炸了都不挡提示；
3. D5 只出教练提示：`coach_only=True`、`speak_for_user=False`，没有“替我回复”的字段；
4. 原始帧 / 音频不进入 state、不回声，只在 trace 里计数；
5. 阶段按 session 隔离，满足结束标志才推进。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.cards import date_rules as dr
from life.date import pipe
from life.date.asr import AsrFeatures
from life.date.pose import PoseFeatures
from life.screen.emotion_backend import JevBackend


def pose_state(**signals) -> dict:
    features = PoseFeatures(seen=True, persons=1, distance=signals.pop("distance", 0.8),
                            knees_toward=True, gaze_away_no_retreat=True,
                            feeding_seen=False, confidence=0.9)
    state = features.as_state()
    state["signals"].update(signals)
    return state


def asr_state(text: str = "她主动讲了自己的事") -> dict:
    features = AsrFeatures(available=True, text=text, duration_sec=3.0,
                           speech_rate=3.5, energy=0.1, naturalness=0.8,
                           emotion_label="engaged")
    return features.as_state()


class FakeJudge:
    def __init__(self, verdict=None, boom: bool = False):
        self.verdict = verdict
        self.boom = boom
        self.calls = 0
        self.seen_state = None

    def available(self) -> bool:
        return True

    def judge(self, state):
        self.calls += 1
        self.seen_state = state
        if self.boom:
            raise RuntimeError("判定炸了")
        return self.verdict


class Verdict:
    def __init__(self, label: str = "festive"):
        self.label = label

    def as_dict(self):
        return {"valence": 0.6, "arousal": 0.5, "emotion_label": self.label,
                "confidence": 0.8, "intention_hint": "她放松了"}


def main() -> int:
    section("① D3 状态聚合")

    state = pipe.aggregate_state({
        "pose": pose_state(sharing="high"),
        "asr": asr_state("今天聊得挺开心"),
        "signals": {"distance": 0.9, "不认识的格子": 1},
        "text": "显式文字优先",
        "elapsed_minutes": 20,
    })
    check("★ 姿态 + 语音信号合并", state["signals"].get("knees_toward") is True
          and state["signals"].get("sharing") == "high", str(state["signals"]))
    check("★ 显式信号最后覆盖", state["signals"].get("distance") == 0.9,
          str(state["signals"].get("distance")))
    check("★ 规则不认识的信号被丢掉", "不认识的格子" not in state["signals"],
          str(state["signals"]))
    check("★ 文字以显式为准", state["text"] == "显式文字优先", state["text"])
    check("姿态 / 语音特征都进 state", state["pose"].get("seen") is True
          and state["asr"].get("emotion_label") == "engaged", "")

    raw = pipe.aggregate_state({
        "frames": [b"\x00\x01", b"\x02\x03"],
        "audio": b"raw-audio",
        "pose": pose_state(),
    })
    check("★ 原始帧 / 音频不进入 state",
          "frames" not in raw and "audio" not in raw
          and "frames" not in raw["pose"] and "audio" not in raw["asr"],
          str(sorted(raw)))
    try:
        json.dumps(raw, ensure_ascii=False)
        serializable = True
    except TypeError:
        serializable = False
    check("★ 聚合后的 state 可 JSON 序列化（bytes 被丢掉）", serializable, "")

    section("② D5 输出是教练提示，不是替我说话")

    out = pipe.DatePipe().step({
        "session_id": "s1",
        "pose": pose_state(distance=0.2),
        "asr": asr_state("随便聊聊"),
    })
    check("★ 返回教练提示", bool(out.get("suggestion")) and out.get("stage") == dr.STAGE_ICEBREAK,
          str(out.get("suggestion"))[:40])
    check("★ 明确只给提示：coach_only / speak_for_user",
          out.get("coach_only") is True and out.get("speak_for_user") is False,
          f"{out.get('coach_only')}/{out.get('speak_for_user')}")
    check("★ 没有“替我回复”的字段",
          not any(key in out for key in ("reply", "response", "message", "answer")),
          str(sorted(out)))
    check("破冰阶段不给升级测试", out.get("can_test") is False and out.get("test") is None,
          "")
    check("提示带禁忌清单", isinstance(out.get("taboos"), list) and bool(out["taboos"]), "")

    section("③ 阶段推进 / 按 session 隔离")

    p = pipe.DatePipe()
    first = p.step({"session_id": "a", "signals": {"sharing": "high"}})
    check("★ 破冰满足结束标志 → 推到升温", first["stage"] == dr.STAGE_WARMUP
          and first["trace"]["advanced"] is True, first["stage"])
    again = p.step({"session_id": "a"})
    check("同一 session 下一拍留在升温", again["stage"] == dr.STAGE_WARMUP, again["stage"])
    other = p.step({"session_id": "b"})
    check("★ 另一个 session 仍从破冰开始", other["stage"] == dr.STAGE_ICEBREAK,
          other["stage"])
    check("不满足结束标志不推进", other["trace"]["advanced"] is False, "")

    p.set_stage("c", dr.STAGE_INTIMATE)
    # advance=False：这一拍先看亲密阶段自己的提示（否则结束标志会直接推到私密）
    intimate = p.step({"session_id": "c", "pose": pose_state(distance=0.8),
                       "advance": False})
    check("★ 亲密阶段 + 信号足 → 可做索吻测试",
          intimate["stage"] == dr.STAGE_INTIMATE and intimate["can_test"] is True
          and intimate["test"] == "kiss", str(intimate.get("test")))
    moved = p.step({"session_id": "c", "pose": pose_state(distance=0.8)})
    check("亲密结束标志满足 → 下一拍推到私密", moved["stage"] == dr.STAGE_PRIVATE
          and moved["trace"]["advanced"] is True, moved["stage"])
    p.reset("c")
    check("reset 后阶段回到破冰", p.stage("c") == dr.STAGE_ICEBREAK, p.stage("c"))

    section("④ D4 复用 JevBackend：真类 + 注入 post")

    calls: list[dict] = []

    def fake_post(url, payload, headers, timeout):
        calls.append(payload)
        return {"answers": [
            {"name": "emotion_label", "choice": "festive"},
            {"name": "valence", "score": 0.6},
            {"name": "arousal", "score": 0.5},
            {"name": "confidence", "score": 0.8},
            {"name": "intention_hint", "text": "她放松了"},
        ]}

    judge = JevBackend(api_key="test-key", endpoint="http://example.invalid",
                       post=fake_post)
    out = pipe.DatePipe(judge=judge).step({
        "session_id": "jev", "pose": pose_state(), "asr": asr_state(),
        "frames": [b"f1", b"f2"], "audio": b"raw",
    })
    check("★ JevBackend 真被调用一次", len(calls) == 1, str(len(calls)))
    check("★ 判定结果进 emotion", out["emotion"] is not None
          and out["emotion"].get("emotion_label") == "festive", str(out["emotion"]))
    check("trace 记下判过了", out["trace"]["judged"] is True, str(out["trace"]))
    sent = calls[0]
    check("★ 发给判定层的是干净 state（没有原始帧/音频）",
          "state" in sent and "frames" not in sent and "audio" not in sent
          and "frames" not in sent["state"], str(sorted(sent)))
    check("判定请求带回问题清单", isinstance(sent.get("questions"), list)
          and len(sent["questions"]) >= 3, str(len(sent.get("questions", []))))
    check("★ 整条输出可 JSON 序列化", json.dumps(out, ensure_ascii=False) != "", "")

    no_key = JevBackend(api_key="", endpoint="")
    plain = pipe.DatePipe(judge=no_key).step({"session_id": "no-key"})
    check("★ 判定后端没配齐 → 跳过，教练提示照出",
          plain["trace"]["judged"] is False and plain["emotion"] is None
          and bool(plain["suggestion"]), plain["trace"])
    check("没配齐时不会真的发请求", no_key.available() is False, "")

    boom = FakeJudge(boom=True)
    broken = pipe.DatePipe(judge=boom).step({"session_id": "boom"})
    check("★ 判定抛异常 → 不挡提示", broken["trace"]["judged"] is False
          and broken["emotion"] is None and bool(broken["suggestion"]), "")

    fake = FakeJudge(verdict=Verdict("festive"))
    p2 = pipe.DatePipe(judge=fake).step({"session_id": "fake", "text": "她笑了"})
    check("注入判定器：结果进 emotion，state 也给它看",
          p2["emotion"]["emotion_label"] == "festive"
          and fake.seen_state is not None and fake.seen_state.get("text") == "她笑了", "")

    section("⑤ 原始帧 / 音频不回声")

    raw_out = pipe.DatePipe().step({
        "session_id": "raw",
        "frames": [b"f1", b"f2", b"f3"],
        "audio": b"raw-audio",
        "pose": pose_state(),
    })
    check("★ trace 只计数，不回传内容",
          raw_out["trace"]["frames_dropped"] == 3
          and raw_out["trace"]["audio_dropped"] is True
          and "frames" not in raw_out and "audio" not in raw_out,
          str(raw_out["trace"]))
    check("★ state 里也没有原始数据",
          not any(key in raw_out["state"] for key in ("frames", "audio")),
          str(sorted(raw_out["state"])))

    source = Path(pipe.__file__).read_text(encoding="utf-8")
    check("★ 推理链自己不联网（判定交给注入的后端）",
          not any(token in source for token in
                  ("import openai", "urllib", "requests.", "http.client")), "")
    check("推理链不落库（无 sqlite / DAO）",
          not any(token in source for token in
                  ("sqlite3", "from data.dao", "import data.dao")), "")

    empty = pipe.DatePipe().step({})
    check("空输入也不崩", bool(empty.get("suggestion")), "")

    return summary("约会推理链（v2-date-jev-pipe）")


if __name__ == "__main__":
    raise SystemExit(main())
