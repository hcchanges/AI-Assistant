"""本地姿态特征（v2-pose-local）。

**这一层只做一件事**：一帧画面 → 几个结构化特征（距离 / 膝盖朝向 / 视线 / 喂食动作）。
画面本身不保存、不出本进程；上层拿到的永远只有 `PoseFeatures`。

选型说明（按本机实时性 + 零权重文件）：

- 选 **MediaPipe Pose**：pip 装完就有 33 个关键点，不需要额外下模型权重，
  CPU 上单帧够快；本机 `mediapipe` 未安装，因此这一层做成**可选后端 + 自动降级**，
  没装时 `available == False`，`extract()` 返回"没看见人"，绝不抛异常。
- 没选 RTMPose：它需要单独的 ONNX 权重文件，仓库里不放权重、也不在运行时联网下载
  （红线：本地、不联网）。以后要换后端，实现 `PoseBackend` 即可，上层不动。

**降级口径**：没装 / 加载失败 / 这帧没检测到人 / 关键点被遮挡——都只是"不知道"，
不是"没有这个信号"。所以 `as_signals()` 只交出有把握的格子，不拿 False 冒充否定。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping, Protocol, Sequence

logger = logging.getLogger(__name__)

# MediaPipe Pose 33 个关键点里用得到的几个（公开文档的固定下标）
NOSE = 0
LEFT_SHOULDER = 11
RIGHT_SHOULDER = 12
LEFT_WRIST = 15
RIGHT_WRIST = 16
LEFT_HIP = 23
RIGHT_HIP = 24
LEFT_KNEE = 25
RIGHT_KNEE = 26

VISIBILITY_MIN = 0.5      # 低于这个可见度当没看见
HEAD_TURN_AWAY = 0.35     # 鼻尖偏离肩中点的比例超过它，算"头转开了"
FEEDING_NEAR = 0.12       # 手腕到鼻尖的归一化距离小于它，算"手在嘴边"
RETREAT_RATIO = 0.85      # 肩宽缩到基线的这个比例以下，算"往后靠/退开"


@dataclass(frozen=True)
class Point:
    """归一化关键点（MediaPipe 口径：x/y 是 0~1 的画面比例，visibility 0~1）。"""

    x: float
    y: float
    visibility: float = 1.0


@dataclass(frozen=True)
class PoseFeatures:
    """一帧的结构化结果。所有"不知道"的格子都是 None / -1，不是硬猜的值。"""

    seen: bool = False
    persons: int = 0
    shoulder_span: float | None = None
    distance: float | None = None          # 0~1，越大越近（肩宽占画面的比例换算）
    knees_toward: bool | None = None       # 双膝比双髋更收拢
    head_turn: float | None = None         # 0~1，越大越偏
    gaze_away_no_retreat: bool | None = None
    feeding_seen: bool = False             # 手在嘴边（分不清自己的餐具/喂食/直接，所以不给档位）
    feeding_level: int = -1                # -1 = 不知道；档位由会话侧按 date_rules 补
    retreat: bool = False                  # 肩宽相对基线缩小 → 往后靠
    confidence: float = 0.0
    backend: str = ""
    degraded_reason: str = ""

    def as_signals(self) -> dict[str, Any]:
        """只交有把握的格子给规则层（`date_rules.normalize_snapshot` 认的形状）。"""
        signals: dict[str, Any] = {}
        if self.seen:
            if self.distance is not None:
                signals["distance"] = round(float(self.distance), 3)
            if self.knees_toward is not None:
                signals["knees_toward"] = bool(self.knees_toward)
            if self.gaze_away_no_retreat is not None:
                signals["gaze_away_no_retreat"] = bool(self.gaze_away_no_retreat)
            if self.feeding_level >= 0:
                signals["feeding"] = int(self.feeding_level)
        return signals

    def as_dict(self) -> dict[str, Any]:
        return {
            "seen": self.seen,
            "persons": self.persons,
            "shoulder_span": self.shoulder_span,
            "distance": self.distance,
            "knees_toward": self.knees_toward,
            "head_turn": self.head_turn,
            "gaze_away_no_retreat": self.gaze_away_no_retreat,
            "feeding_seen": self.feeding_seen,
            "feeding_level": self.feeding_level,
            "retreat": self.retreat,
            "confidence": self.confidence,
            "backend": self.backend,
            "degraded_reason": self.degraded_reason,
        }

    def as_state(self) -> dict[str, Any]:
        """交给聚合层（⑤批）的形状：特征 + 能直接用的信号。"""
        return {"pose": self.as_dict(), "signals": self.as_signals()}

    @classmethod
    def missing(cls, reason: str, backend: str = "") -> "PoseFeatures":
        return cls(seen=False, backend=backend, degraded_reason=reason)


class PoseBackend(Protocol):
    """后端只要能把一帧变成"每个人的关键点字典"就够了。"""

    name: str

    def detect(self, frame: Any) -> Sequence[Mapping[int, Point]] | None: ...


class MediaPipeBackend:
    """MediaPipe Pose 的薄适配（懒加载；没装时构造直接抛 ImportError，由上层降级）。"""

    name = "mediapipe"

    def __init__(self, model_complexity: int = 1, static_image_mode: bool = False):
        import mediapipe as mp  # 延迟到真正要用时才 import

        self._pose = mp.solutions.pose.Pose(
            static_image_mode=bool(static_image_mode),
            model_complexity=int(model_complexity),
            min_detection_confidence=0.5,
            min_tracking_confidence=0.5,
        )

    def detect(self, frame: Any) -> list[dict[int, Point]] | None:
        result = self._pose.process(frame)
        landmarks = getattr(result, "pose_landmarks", None)
        if not landmarks:
            return None
        points: dict[int, Point] = {}
        for index, item in enumerate(landmarks.landmark):
            points[index] = Point(
                x=float(getattr(item, "x", 0.0)),
                y=float(getattr(item, "y", 0.0)),
                visibility=float(getattr(item, "visibility", 1.0)),
            )
        return [points]

    def close(self) -> None:
        close = getattr(self._pose, "close", None)
        if callable(close):
            close()


def mediapipe_available() -> bool:
    """只查包在不在，不 import 整个 mediapipe（那一步很慢）。"""
    try:
        from importlib.util import find_spec

        return find_spec("mediapipe") is not None
    except Exception:      # noqa: BLE001 —— 查不到就当没有
        return False


def _visible(points: Mapping[int, Point], index: int,
             minimum: float = VISIBILITY_MIN) -> Point | None:
    point = points.get(index)
    if point is None or float(point.visibility) < minimum:
        return None
    return point


def _mid(a: Point, b: Point) -> Point:
    return Point((a.x + b.x) / 2.0, (a.y + b.y) / 2.0, min(a.visibility, b.visibility))


def _span(points: Mapping[int, Point]) -> float | None:
    left = _visible(points, LEFT_SHOULDER)
    right = _visible(points, RIGHT_SHOULDER)
    if left is None or right is None:
        return None
    return abs(float(right.x) - float(left.x))


def _distance_from_span(span: float | None) -> float | None:
    """肩宽占比 → 0~1 的"近"。经验区间 5%~45%，只当相对趋势用，不当厘米数。"""
    if span is None:
        return None
    return max(0.0, min(1.0, (float(span) - 0.05) / 0.40))


def features_from_landmarks(
    detections: Sequence[Mapping[int, Point]] | None,
    span_baseline: float | None = None,
    backend: str = "",
) -> PoseFeatures:
    """纯函数：关键点 → 特征。测试可以直接喂假关键点，不需要装模型。"""
    people = [item for item in (detections or []) if item]
    if not people:
        return PoseFeatures.missing("这一帧没检测到人", backend=backend)

    # 取肩最宽的那个人（画面里最靠前/最完整的那个），其余人只计数
    best = max(people, key=lambda item: _span(item) or 0.0)
    span = _span(best)
    shoulders_ok = span is not None and span > 0.0

    retreat = bool(
        span is not None and span_baseline is not None
        and span < float(span_baseline) * RETREAT_RATIO
    )

    # 膝盖朝向：双膝的横向间距比双髋更窄 → 收拢（朝向你）
    knees_toward: bool | None = None
    lh = _visible(best, LEFT_HIP)
    rh = _visible(best, RIGHT_HIP)
    lk = _visible(best, LEFT_KNEE)
    rk = _visible(best, RIGHT_KNEE)
    if lh and rh and lk and rk:
        hip_gap = abs(rh.x - lh.x)
        knee_gap = abs(rk.x - lk.x)
        if hip_gap > 0.01:
            knees_toward = knee_gap < hip_gap * 0.85

    # 头转向：鼻尖相对肩中点的横向偏移，按肩宽归一
    head_turn: float | None = None
    nose = _visible(best, NOSE)
    ls = _visible(best, LEFT_SHOULDER)
    rs = _visible(best, RIGHT_SHOULDER)
    if nose and ls and rs and shoulders_ok:
        mid = _mid(ls, rs)
        head_turn = max(0.0, min(1.0, abs(nose.x - mid.x) / float(span)))

    gaze_away_no_retreat: bool | None = None
    if head_turn is not None:
        gaze_away_no_retreat = bool(head_turn >= HEAD_TURN_AWAY and not retreat)

    # 手到嘴边的距离（喂食动作的弱信号；不判"谁喂谁"）
    feeding_seen = False
    if nose is not None:
        for wrist_index in (LEFT_WRIST, RIGHT_WRIST):
            wrist = _visible(best, wrist_index)
            if wrist is None:
                continue
            if ((wrist.x - nose.x) ** 2 + (wrist.y - nose.y) ** 2) ** 0.5 < FEEDING_NEAR:
                feeding_seen = True
                break

    used = [p for p in (nose, ls, rs, lh, rh, lk, rk) if p is not None]
    confidence = round(sum(float(p.visibility) for p in used) / len(used), 3) if used else 0.0

    return PoseFeatures(
        seen=True,
        persons=len(people),
        shoulder_span=round(span, 4) if span is not None else None,
        distance=_distance_from_span(span),
        knees_toward=knees_toward,
        head_turn=round(head_turn, 3) if head_turn is not None else None,
        gaze_away_no_retreat=gaze_away_no_retreat,
        feeding_seen=feeding_seen,
        feeding_level=-1,
        retreat=retreat,
        confidence=confidence,
        backend=backend,
    )


class PoseExtractor:
    """对外的姿态入口：构造时可选后端，运行时自动降级，绝不让上层接异常。

    只保留"上一帧的特征"和肩宽基线，**不保留任何一帧画面**。
    """

    def __init__(self, backend: PoseBackend | None = None,
                 use_mediapipe: bool = True,
                 model_complexity: int = 1):
        self._backend = backend
        self._degraded = ""
        if self._backend is None and use_mediapipe:
            if mediapipe_available():
                try:
                    self._backend = MediaPipeBackend(model_complexity=model_complexity)
                except Exception as exc:      # noqa: BLE001 —— 加载失败就降级
                    self._degraded = f"mediapipe 加载失败：{exc}"
                    logger.info("姿态：%s，本批按“没看见人”降级", self._degraded)
            else:
                self._degraded = "本机没装 mediapipe"
        elif self._backend is None:
            self._degraded = "没给姿态后端"
        self._span_baseline: float | None = None

    @property
    def available(self) -> bool:
        return self._backend is not None

    @property
    def backend_name(self) -> str:
        return str(getattr(self._backend, "name", "") or "")

    @property
    def degraded_reason(self) -> str:
        return "" if self.available else (self._degraded or "没有可用的姿态后端")

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "backend": self.backend_name,
            "reason": self.degraded_reason,
        }

    def extract(self, frame: Any) -> PoseFeatures:
        """一帧 → 特征。画面用完即丢，不挂在实例上。"""
        if self._backend is None:
            return PoseFeatures.missing(self.degraded_reason)
        if frame is None or not hasattr(frame, "shape"):
            return PoseFeatures.missing("帧格式不认识（要 numpy HxWxC）", self.backend_name)
        try:
            detections = self._backend.detect(frame)
        except Exception as exc:      # noqa: BLE001 —— 单帧失败只算这一帧没看见
            logger.debug("姿态检测这一帧失败：%s", exc)
            return PoseFeatures.missing(f"检测异常：{exc}", self.backend_name)

        features = features_from_landmarks(
            detections, span_baseline=self._span_baseline, backend=self.backend_name
        )
        if features.shoulder_span is not None:
            span = float(features.shoulder_span)
            if self._span_baseline is None:
                self._span_baseline = span
            else:
                self._span_baseline = self._span_baseline * 0.85 + span * 0.15
        return features

    def close(self) -> None:
        close = getattr(self._backend, "close", None)
        if callable(close):
            close()
        self._backend = None


__all__ = [
    "Point",
    "PoseFeatures",
    "PoseBackend",
    "MediaPipeBackend",
    "PoseExtractor",
    "features_from_landmarks",
    "mediapipe_available",
    "HEAD_TURN_AWAY",
    "FEEDING_NEAR",
    "RETREAT_RATIO",
]
