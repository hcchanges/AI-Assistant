"""情绪原型（few-shot 情绪识别，v2-mood-proto / v2-mood-fear）的自动测试。

覆盖：
1. proto_path 落点；
2. load_prototypes 的降级（缺文件 / 坏文件 → None）与读回（三态齐全、归一化）；
3. proto_verdict 的判据——够像且领先才开口，贴边 / 不够像不开口，开口时分数五档归一；
4. build_prototypes 没有模型就抛 CvUnavailable（离线），本机有权重时真建 + 往返（含愤怒+愤怒2 合并）；
5. 红线：样例目录没图不落盘。

全部用合成向量 / 临时目录，不碰用户真实的样例目录或数据根。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests.harness import check, section, skip, summary  # noqa: E402

from life.screen import mood_cv                                    # noqa: E402
from life.screen.mood_cv import (                                  # noqa: E402
    MoodCvReader,
    MoodVerdict,
    PROTO_EMOTIONS,
    build_prototypes,
    load_prototypes,
    proto_path,
    proto_verdict,
)
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
REAL_MODEL_DIR = ROOT / "data" / "models"
HAS_MODEL = mood_cv.model_ready(REAL_MODEL_DIR)


def _unit(x: float, y: float):
    import numpy as np

    v = np.array([x, y], dtype=np.float32)
    return v / np.linalg.norm(v)


def main() -> int:
    import numpy as np

    with tempfile.TemporaryDirectory(prefix="pet_mood_proto_") as tmp:
        tmp = Path(tmp)

        section("proto_path 落点")
        check(
            "proto_path -> <models>/mood_prototypes.npz",
            proto_path(tmp / "models") == tmp / "models" / "mood_prototypes.npz",
            str(proto_path(tmp / "models")),
        )

        section("load_prototypes 降级与读回")
        check("缺文件 → None", load_prototypes(tmp) is None, "")
        (tmp / "mood_prototypes.npz").write_bytes(b"not-a-zip")
        check("坏文件 → None", load_prototypes(tmp) is None, "")
        good = {scene: np.random.default_rng(i).random(8) for i, scene in enumerate(PROTO_EMOTIONS)}
        np.savez(tmp / "mood_prototypes.npz", **good)
        loaded = load_prototypes(tmp)
        check("三态齐全（v2-mood-tri 之后 PROTO_EMOTIONS 就三格）",
              loaded is not None and set(loaded) == set(PROTO_EMOTIONS), str(set(loaded or {})))
        check(
            "读回是归一化的（范数≈1）",
            loaded is not None
            and all(abs(float(np.linalg.norm(loaded[s])) - 1.0) < 1e-6 for s in PROTO_EMOTIONS),
            "",
        )

        section("proto_verdict 判据")
        # v2-mood-tri：冠军这一角原来用的是哭泣，哭泣整态删掉之后换成笑容当主角。
        # **注意 margin 是按冠军态取的**——笑容那一档的门是 0.015（不是默认的 0.03），
        # 所以下面"贴边"那条用的是 9°（余弦差 0.0123），不是原来那个 5°。

        def protos_with(festive_deg: float):
            return {
                SCENE_FESTIVE: _unit(1, 0),
                SCENE_ANGRY: _unit(np.cos(np.deg2rad(festive_deg)), np.sin(np.deg2rad(festive_deg))),
                SCENE_HORROR: _unit(0, 1),
            }

        # 干脆利落：这一帧跟笑容原型 1.0、跟愤怒（60°）只有 0.5
        far = protos_with(60)
        got = proto_verdict(_unit(1, 0), far)
        check("够像且领先 → 开口 festive", got is not None and got.scene == SCENE_FESTIVE, str(got))
        check(
            "开口时分数四档（三态 + 中立）且归一",
            got is not None and len(got.scores) == 4 and abs(sum(got.scores.values()) - 1.0) < 1e-6,
            f"{len(got.scores) if got else 0} 档，和={sum(got.scores.values()):.4f}" if got else "None",
        )
        check(
            "festive 分数最高",
            got is not None and got.scores[SCENE_FESTIVE] == max(got.scores.values()),
            str(got.scores if got else None),
        )
        check(
            "三态都在分数表里，只有中立置 0（幽默/哭泣都已删）",
            got is not None and got.scores.get(mood_cv.NEUTRAL_KEY, -1.0) == 0.0
            and SCENE_HORROR in got.scores and SCENE_ANGRY in got.scores
            and "funny" not in got.scores and "cry" not in got.scores,
            str(got.scores if got else None),
        )

        # 跟谁都远：三态都到不了 0.76
        got_low = proto_verdict(_unit(-1, 0), far)
        check("跟谁都不够像 → 不开口", got_low is None, str(got_low))

        # 贴边：跟冠军 1.0 但亚军（9°，余弦 0.9877）差 0.0123 < 笑容那档的 0.015
        close = protos_with(9)
        got_close = proto_verdict(_unit(1, 0), close)
        check("贴边（领先不足该态自己的 margin）→ 不开口", got_close is None, str(got_close))
        check("这个贴边例子确实落在笑容那一档的门（0.015）里，不是默认 0.03",
              mood_cv.margin_for(SCENE_FESTIVE) == 0.015
              and 1 - np.cos(np.deg2rad(9)) < mood_cv.margin_for(SCENE_FESTIVE),
              f"{1 - np.cos(np.deg2rad(9)):.4f}")

        section("build_prototypes 没有模型就抛 CvUnavailable")
        try:
            build_prototypes(tmp, tmp / "models", tmp / "out.npz")
            check("缺模型 → 抛 CvUnavailable", False, "居然没抛")
        except mood_cv.CvUnavailable:
            check("缺模型 → 抛 CvUnavailable", True, "")

        section("build_prototypes 真建 + 往返（本机有权重时）")
        if not HAS_MODEL:
            skip("真建原型", f"{REAL_MODEL_DIR} 下没有中文 CLIP 权重（gitignore，不进仓库）")
        else:
            from PIL import Image

            ref = tmp / "ref"
            # 悲伤那个目录照旧摆在这儿（盘上本来就有），用来证"摆着也不参与编码"。
            for dirname, color in [("悲伤", (30, 30, 120)), ("愤怒", (120, 30, 30)),
                                   ("愤怒2", (140, 20, 20)), ("笑容", (120, 120, 30)),
                                   ("恐惧", (30, 120, 120))]:
                d = ref / dirname
                d.mkdir(parents=True)
                Image.new("RGB", (32, 32), color).save(d / "a.png")
            out = tmp / "out" / "mood_prototypes.npz"
            protos = build_prototypes(ref, REAL_MODEL_DIR, out)
            check("三态都建出来（愤怒+愤怒2 合并成一档）", set(protos) == set(PROTO_EMOTIONS), str(set(protos)))
            check("悲伤目录就在那儿摆着，也没被编进去（PROTO_DIRS 里没有它）",
                  "cry" not in protos and "悲伤" not in str(mood_cv.PROTO_DIRS),
                  str(mood_cv.PROTO_DIRS))
            check("npz 落盘", out.is_file(), str(out))
            back = load_prototypes(out.parent)
            check(
                "读回三态且归一",
                back is not None
                and set(back) == set(PROTO_EMOTIONS)
                and all(abs(float(np.linalg.norm(back[s])) - 1.0) < 1e-6 for s in PROTO_EMOTIONS),
                "",
            )

            # 红线：样例目录里没图 → 返回空、不落盘
            empty_out = tmp / "empty.npz"
            got_empty = build_prototypes(tmp / "no-such-dir", REAL_MODEL_DIR, empty_out)
            check("样例目录没图 → 返回空、不落盘", got_empty == {} and not empty_out.exists(), str(got_empty))

    return summary("情绪原型（v2-mood-proto）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
