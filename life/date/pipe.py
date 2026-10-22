"""约会推理链（v2-date-jev-pipe）：姿态 + 语音 → 统一状态 → Jev 判定 → 教练提示。

三段固定顺序（D3 → D4 → D5）：

- **D3 状态聚合**：把③批 `PoseFeatures.as_state()` 与④批 `AsrFeatures.as_state()`
  合成一个干净 state。只收结构化特征与文字；原始帧 / 原始音频不进来，也不往外出。
- **D4 Jev 判定**：复用 `JevBackend`（`life/screen/emotion_backend.py`）。没有判定后端
  就跳过，不阻塞教练提示；判定失败也只当这一拍没判。
- **D5 教练提示**：复用①批 `date_rules.get_prompt(stage, signals)`。输出是**给用户的
  提示候选**，永远不替用户说话（`coach_only=True` / `speak_for_user=False`）。

**红线**：这里没有“生成一句替我回复她”的路径；也没有把画面 / 音频放进 state 的路径。
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Mapping

from life.cards import date_rules as dr

logger = logging.getLogger(__name__)

MAX_SAFE_DEPTH = 4


def _json_safe(value: Any, depth: int = 0) -> Any:
    """只留下能 JSON 序列化的标量 / 容器；bytes、ndarray、对象一律丢掉。

    原始帧和原始音频就是被这一层挡在 state 外面的。
    """
    if depth > MAX_SAFE_DEPTH:
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            for cleaned in (_json_safe(item, depth + 1),)
            if cleaned is not None
        }
    if isinstance(value, (list, tuple)):
        cleaned = [_json_safe(item, depth + 1) for item in value]
        return [item for item in cleaned if item is not None]
    return None


def _signal_block(value: Any) -> dict[str, Any]:
    """从 pose/asr 的 state 里抠出 signals 那一格。"""
    if not isinstance(value, Mapping):
        return {}
    signals = value.get("signals")
    return dict(signals) if isinstance(signals, Mapping) else {}


def aggregate_state(payload: Any) -> dict[str, Any]:
    """D3：输入（可含原始帧/音频）→ 干净 state。纯函数，测试直接喂字典。"""
    source = payload if isinstance(payload, Mapping) else {}
    pose = _json_safe(source.get("pose"))
    asr = _json_safe(source.get("asr"))
    pose = pose if isinstance(pose, Mapping) else {}
    asr = asr if isinstance(asr, Mapping) else {}

    signals: dict[str, Any] = {}
    signals.update(_signal_block(pose))
    signals.update(_signal_block(asr))
    explicit = source.get("signals")
    if isinstance(explicit, Mapping):
        signals.update(explicit)

    # 只留规则层认得的格子（elapsed_minutes 是阶段推进用的额外一格）
    allowed = set(dr.SIGNAL_KEYS) | {"elapsed_minutes"}
    signals = {key: _json_safe(value) for key, value in signals.items() if key in allowed}
    signals = {key: value for key, value in signals.items() if value is not None}

    text = source.get("text")
    if not text:
        text = asr.get("text", "")
    pose_features = pose.get("pose")
    if not isinstance(pose_features, Mapping):
        pose_features = pose if "seen" in pose else {}
    asr_features = asr.get("asr")
    if not isinstance(asr_features, Mapping):
        asr_features = {key: value for key, value in asr.items()
                        if key not in ("text", "signals")}
    state: dict[str, Any] = {
        "text": str(text or "").strip(),
        "signals": signals,
        "pose": pose_features,
        "asr": asr_features,
    }
    if "elapsed_minutes" in source:
        state["elapsed_minutes"] = _json_safe(source.get("elapsed_minutes"))
        if state["elapsed_minutes"] is not None:
            state["signals"].setdefault("elapsed_minutes", state["elapsed_minutes"])
    card = _json_safe(source.get("card"))
    if isinstance(card, Mapping):
        state["card"] = card
    return state


def _dropped_counts(payload: Any) -> tuple[int, bool]:
    """只数“收到过”，不读内容：原始帧 / 原始音频永远不进入 state。"""
    source = payload if isinstance(payload, Mapping) else {}
    frames = source.get("frames")
    if isinstance(frames, (list, tuple)):
        frame_count = len(frames)
    else:
        frame_count = 1 if frames is not None else 0
    return frame_count, source.get("audio") is not None


class DatePipe:
    """D3 → D4 → D5 的串联。按 session 记阶段，其余全部无状态。"""

    def __init__(self, judge: Any = None,
                 prompt_fn: Callable[[str, Mapping[str, Any] | None], Any] | None = None):
        self._judge = judge
        self._prompt = prompt_fn or dr.get_prompt
        self._stages: dict[str, str] = {}

    def stage(self, session_id: Any) -> str:
        return self._stages.get(str(session_id or "default"), dr.STAGE_ICEBREAK)

    def set_stage(self, session_id: Any, stage: Any) -> str:
        key = dr.normalize_stage(stage)
        self._stages[str(session_id or "default")] = key
        return key

    def reset(self, session_id: Any) -> None:
        self._stages.pop(str(session_id or "default"), None)

    def step(self, payload: Any) -> dict[str, Any]:
        """跑一拍：返回教练提示 + 判定结果 + 干净的 state / trace。"""
        source = payload if isinstance(payload, Mapping) else {}
        session_id = str(source.get("session_id") or "default")
        stage = dr.normalize_stage(source.get("stage") or self.stage(session_id))
        state = aggregate_state(source)

        advanced = False
        if source.get("advance", True):
            nxt = dr.next_stage(stage, state["signals"])
            if nxt:
                stage = nxt
                advanced = True

        verdict = None
        judged = False
        if self._judge is not None:
            try:
                available = getattr(self._judge, "available", None)
                if not callable(available) or available():
                    verdict = self._judge.judge(state)
                    judged = verdict is not None
            except Exception as exc:      # noqa: BLE001 —— 判定失败不挡教练提示
                logger.info("约会判定这一拍没成：%s", exc)
                verdict = None
                judged = False

        prompt = self._prompt(stage, state["signals"])
        out = prompt.as_dict()
        frames, audio = _dropped_counts(source)
        out.update({
            "coach_only": True,
            "speak_for_user": False,
            "emotion": verdict.as_dict() if verdict is not None else None,
            "state": {
                "text": state["text"],
                "signals": state["signals"],
                "pose": state["pose"],
                "asr": state["asr"],
            },
            "trace": {
                "judged": judged,
                "advanced": advanced,
                "frames_dropped": frames,
                "audio_dropped": audio,
            },
        })
        self._stages[session_id] = stage
        return out


__all__ = ["DatePipe", "aggregate_state"]
