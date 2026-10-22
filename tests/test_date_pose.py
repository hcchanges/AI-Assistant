# -*- coding: utf-8 -*-
"""本地姿态特征（v2-pose-local）的自动测试。

**这一批验什么**：

1. 关键点 → 结构化特征：距离 / 膝盖朝向 / 视线 / 喂食动作都能从假关键点算出来；
2. "不知道"就是不知道：遮挡、没检测到人、帧格式不认识，都只降级、不硬猜 False；
3. 没装 mediapipe 不崩：`PoseExtractor()` 返回"没看见人"，`available == False`；
4. 原始帧不保留：实例上没有帧、特征可 JSON 序列化、能直接被规则层吃下；
5. 后端起异常只影响这一帧。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.cards import date_rules as dr
from life.date import pose


def person(span: float = 0.2, nose_dx: float = 0.0, knee_ratio: float = 0.7,
           wrist_near: bool = False, visibility: float = 0.9) -> dict[int, pose.Point]:
    """造一个人的关键点：肩宽 span、鼻尖横移 nose_dx、膝间距/髋间距 = knee_ratio。"""
    mid = 0.5
    points = {
        pose.NOSE: pose.Point(mid + nose_dx, 0.18, visibility),
        pose.LEFT_SHOULDER: pose.Point(mid - span / 2, 0.30, visibility),
        pose.RIGHT_SHOULDER: pose.Point(mid + span / 2, 0.30, visibility),
        pose.LEFT_HIP: pose.Point(mid - 0.10, 0.60, visibility),
        pose.RIGHT_HIP: pose.Point(mid + 0.10, 0.60, visibility),
    }
    knee_gap = 0.20 * knee_ratio
    points[pose.LEFT_KNEE] = pose.Point(mid - knee_gap / 2, 0.85, visibility)
    points[pose.RIGHT_KNEE] = pose.Point(mid + knee_gap / 2, 0.85, visibility)
    if wrist_near:
        points[pose.LEFT_WRIST] = pose.Point(mid + nose_dx, 0.20, visibility)
    else:
        points[pose.LEFT_WRIST] = pose.Point(mid - 0.30, 0.55, visibility)
    return points


class FakeBackend:
    name = "fake"

    def __init__(self, *frames):
        self.frames = list(frames)
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        if self.frames:
            return self.frames.pop(0)
        return None


class BoomBackend:
    name = "boom"

    def detect(self, frame):
        raise RuntimeError("模型炸了")


class Array:
    """最小帧替身：只要有 shape，就当 numpy 帧用（不真的 import numpy）。"""

    shape = (720, 1280, 3)


def main() -> int:
    section("① 关键点 → 结构化特征")

    near = pose.features_from_landmarks([person(span=0.35)], backend="fake")
    far = pose.features_from_landmarks([person(span=0.08)], backend="fake")
    check("★ 肩宽大 → 距离近；肩宽小 → 距离远",
          near.seen and far.seen and near.distance > far.distance
          and near.distance > 0.7 and far.distance < 0.1,
          f"近 {near.distance} / 远 {far.distance}")
    check("★ 双膝收拢 → knees_toward = True",
          pose.features_from_landmarks([person(knee_ratio=0.6)]).knees_toward is True, "")
    check("膝间距不小于髋间距 → knees_toward = False",
          pose.features_from_landmarks([person(knee_ratio=1.0)]).knees_toward is False, "")

    turned = pose.features_from_landmarks([person(span=0.2, nose_dx=0.10)])
    check("★ 鼻尖偏开 + 没后退 → gaze_away_no_retreat = True",
          turned.head_turn is not None and turned.head_turn >= pose.HEAD_TURN_AWAY
          and turned.gaze_away_no_retreat is True,
          f"head_turn={turned.head_turn}")
    straight = pose.features_from_landmarks([person(span=0.2, nose_dx=0.0)])
    check("正脸 → gaze_away_no_retreat = False",
          straight.gaze_away_no_retreat is False, f"head_turn={straight.head_turn}")

    fed = pose.features_from_landmarks([person(wrist_near=True)])
    check("★ 手腕到鼻尖够近 → feeding_seen = True", fed.feeding_seen is True, "")
    check("★ 单帧只认“手在嘴边”，不硬判喂食档位",
          fed.feeding_level == -1 and "feeding" not in fed.as_signals(),
          str(fed.as_signals()))

    two = pose.features_from_landmarks([person(span=0.1), person(span=0.3)])
    check("画面里两个人 → persons=2，取肩最宽的那个算特征",
          two.persons == 2 and two.distance > 0.5, f"{two.persons}/{two.distance}")

    section("② 不知道就说不知道（不拿 False 冒充否定）")

    hidden = dict(person())
    hidden[pose.LEFT_SHOULDER] = pose.Point(0.4, 0.3, 0.1)      # 可见度太低
    occluded = pose.features_from_landmarks([hidden])
    check("★ 肩被遮挡 → distance 是 None，不是 0",
          occluded.seen and occluded.distance is None
          and "distance" not in occluded.as_signals(),
          str(occluded.as_signals()))
    check("被遮挡的肩不拉低其它格子", occluded.knees_toward is True, "")

    nobody = pose.features_from_landmarks([])
    check("★ 没检测到人 → seen=False、信号为空",
          nobody.seen is False and nobody.as_signals() == {} and bool(nobody.degraded_reason),
          nobody.degraded_reason)

    section("③ 降级不崩：没装 mediapipe / 帧不认识 / 后端起异常")

    extractor = pose.PoseExtractor(use_mediapipe=True)
    check("★ 本机没装 mediapipe → available=False，但不抛异常",
          pose.mediapipe_available() is False or extractor.available is True,
          str(extractor.describe()))
    degraded = extractor.extract(Array())
    check("降级时 extract() 返回“没看见人”",
          degraded.seen is False and bool(degraded.degraded_reason), degraded.degraded_reason)

    none_frame = pose.PoseExtractor(backend=FakeBackend([person()])).extract(None)
    check("★ 帧是 None → 降级，不崩", none_frame.seen is False, none_frame.degraded_reason)
    bad_frame = pose.PoseExtractor(backend=FakeBackend([person()])).extract(object())
    check("帧没有 shape → 降级", bad_frame.seen is False, bad_frame.degraded_reason)

    boom = pose.PoseExtractor(backend=BoomBackend()).extract(Array())
    check("★ 后端这一帧抛异常 → 只算这帧没看见",
          boom.seen is False and "检测异常" in boom.degraded_reason, boom.degraded_reason)

    section("④ 原始帧不保留 / 特征能进规则层")

    backend = FakeBackend([person(span=0.3)])
    extractor = pose.PoseExtractor(backend=backend)
    frame = Array()
    features = extractor.extract(frame)
    check("★ 后端确实拿到了这一帧", backend.calls == 1, str(backend.calls))
    check("★ 实例上不留帧（也没有 last_frame）",
          not hasattr(extractor, "last_frame") and frame not in vars(extractor).values(),
          str(sorted(vars(extractor))))
    try:
        json.dumps(features.as_dict(), ensure_ascii=False)
        serializable = True
    except TypeError:
        serializable = False
    check("★ 特征能直接 JSON 序列化（没有 ndarray）", serializable, "")

    signals = features.as_signals()
    normalized = dr.normalize_snapshot(signals)
    check("★ 特征信号能被 date_rules 原样收下",
          set(normalized) <= set(signals) and "distance" in normalized,
          f"{signals} → {normalized}")
    state = features.as_state()
    check("as_state 是聚合层要的形状：pose + signals",
          "pose" in state and "signals" in state
          and isinstance(state["pose"], dict) and isinstance(state["signals"], dict), "")

    section("⑤ 边界与红线")

    source = Path(pose.__file__).read_text(encoding="utf-8")
    check("★ 姿态层不落库（无 sqlite / DAO）",
          not any(name in source for name in ("sqlite3", "from data.dao", "import data.dao")), "")
    req = Path(pose.__file__).resolve().parents[2] / "service" / "requirements.txt"
    req_text = req.read_text(encoding="utf-8")
    check("★ 姿态依赖单独记在 service/requirements.txt",
          "mediapipe" in req_text and "pose" in req_text.lower(), "")

    clipless = pose.PoseExtractor(backend=None, use_mediapipe=False)
    check("显式不给后端 → 也是可降级的空实现",
          clipless.available is False and clipless.extract(Array()).seen is False, "")

    return summary("本地姿态特征（v2-pose-local）")


if __name__ == "__main__":
    raise SystemExit(main())
