# -*- coding: utf-8 -*-
"""情绪判定后端接线（v2-social-emotion-wire）的自动测试。

**这一批验什么**：`monitor` 那条腿在**显式**写了 `EMOTION_BACKEND=jev|llm` 时才换成
可插拔后端；其余情况（不设 / `local` / 写错值 / 钥匙没配齐）行为与从前逐字相同。

1. 未设 → 仍是原来的 `MoodCvReader`（既有类型断言不受影响）；
2. `llm` + 有钥匙 → LLM 适配器，字幕进 state，返回统一结构并收成场景三态；
3. `jev` + 有钥匙/地址 → Jev 适配器，请求形状与响应解析沿用上一批；
4. `local` 强制 → 有钥匙也走原本地；
5. jev/llm 没配齐 → 退回原本地（不报错）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, section, summary

from life.screen import emotion_backend as eb
from life.screen import monitor as monitor_mod
from life.screen import mood_fuse
from life.screen.mood_cv import MoodCvReader
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE


class FakeProvider:
    """假 LLM：记下被问过什么，回一份结构化 JSON。"""

    def __init__(self, data=None):
        self.data = data or {
            "valence": 0.7,
            "arousal": 0.55,
            "emotion_label": SCENE_FESTIVE,
            "confidence": 0.9,
            "intention_hint": "跟着轻快一点",
        }
        self.calls: list[str] = []

    def chat_json(self, prompt, system=None):
        self.calls.append(prompt)
        return dict(self.data)


class StubReader:
    """假 OCR：只回一句固定字幕，记被叫了几次。"""

    def __init__(self, text: str):
        self.text = text
        self.calls = 0

    def read_array(self, array):
        self.calls += 1
        return self.text


def main() -> int:
    import numpy as np

    box, stub, app = make_env()
    mon = app.screen_monitor
    original_env = monitor_mod._emotion_env
    original_has_llm = eb._has_llm_key

    def use_env(values: dict) -> None:
        monitor_mod._emotion_env = lambda: dict(values)

    try:
        section("① 默认不触发：EMOTION_BACKEND 未设 → 原原本地逻辑")

        app.config.screen.mood = "local"
        use_env({})
        mon._refresh_mood()
        reader = mon.mood_reader
        check("★ 未设 → 仍是原 MoodCvReader", isinstance(reader, MoodCvReader),
              type(reader).__name__)
        check("★ 没有偷换成适配器", not isinstance(reader, monitor_mod.EmotionMoodReader), "")
        first = reader
        mon._refresh_mood()
        check("同档位再刷不重建（既有行为不变）", mon.mood_reader is first, "")

        app.config.screen.mood = "off"
        mon._refresh_mood()
        check("off 档位照旧整条腿不在", mon.mood_reader is None, str(mon.mood_reader))

        section("② EMOTION_BACKEND=llm + 有钥匙 → LLM 后端")

        eb._has_llm_key = lambda cfg: True
        use_env({eb.ENV_BACKEND: "llm"})
        app.config.screen.mood = "local"
        mon._refresh_mood()
        reader = mon.mood_reader
        check("★ 构建为 LLM 适配器",
              isinstance(reader, monitor_mod.EmotionMoodReader)
              and reader.name == eb.BACKEND_LLM,
              f"{type(reader).__name__} / {getattr(reader, 'name', '')}")

        provider = FakeProvider()
        reader.judge._provider = provider
        verdict = reader.classify_array(object(), text="婚礼现场好喜庆")
        check("★ 返回结构化判定并收成场景三态",
              verdict is not None and verdict.scene == SCENE_FESTIVE
              and abs(verdict.score - 0.9) < 1e-9,
              repr(verdict))
        check("★ 统一结构留在 last_verdict（含 hint）",
              reader.last_verdict is not None
              and reader.last_verdict.emotion_label == SCENE_FESTIVE
              and reader.last_verdict.intention_hint == "跟着轻快一点",
              reader.last_verdict.as_dict() if reader.last_verdict else "None")
        check("★ 字幕进了 state（prompt 里能看到原话）",
              bool(provider.calls) and "婚礼现场好喜庆" in provider.calls[-1],
              provider.calls[-1][:80] if provider.calls else "")

        fused = mood_fuse.fuse(None, verdict)
        check("★ 下游融合不变：MoodVerdict 直接可用",
              fused.scene == SCENE_FESTIVE and fused.source == mood_fuse.SOURCE_CV,
              f"{fused.scene} / {fused.source}")

        mon._reader = StubReader("婚礼现场好喜庆")
        provider.calls.clear()
        text, got, skipped = mon._analyze_sample(
            np.zeros((4, 4, 3), np.uint8), np.zeros((4, 4, 3), np.uint8)
        )
        check("★ 采样链先 OCR 再连同画面送判定",
              text == "婚礼现场好喜庆" and got is not None and got.scene == SCENE_FESTIVE,
              f"{text!r} / {got}")
        check("OCR 这一拍只叫了一次", mon._reader.calls == 1, f"{mon._reader.calls} 次")
        check("这条路径不谎报跳过 OCR", skipped is False, str(skipped))

        section("③ EMOTION_BACKEND=jev + 有 JEV 钥匙 → Jev 后端")

        use_env({
            eb.ENV_BACKEND: "jev",
            eb.ENV_JEV_KEY: "jev-key",
            eb.ENV_JEV_ENDPOINT: "https://jev.example/judge",
        })
        mon._refresh_mood()
        reader = mon.mood_reader
        check("★ 构建为 Jev 适配器",
              isinstance(reader, monitor_mod.EmotionMoodReader)
              and reader.name == eb.BACKEND_JEV,
              f"{type(reader).__name__} / {getattr(reader, 'name', '')}")

        seen: dict = {}

        def fake_post(url, body, headers, timeout):
            seen.update(url=url, body=body, headers=headers)
            return {
                "answers": {
                    "emotion_label": {"choice": SCENE_ANGRY},
                    "valence": {"score": -0.62},
                    "arousal": {"score": 0.81},
                    "confidence": {"score": 0.77},
                    "intention_hint": {"text": "先别争，顺着他说"},
                }
            }

        reader.judge._post = fake_post
        got = reader.classify_array(None, text="吵起来了")
        check("★ 响应解析成场景三态 + 统一结构",
              got is not None and got.scene == SCENE_ANGRY
              and reader.last_verdict is not None
              and abs(reader.last_verdict.valence + 0.62) < 1e-9,
              repr(got))
        check("★ 请求形状 / 鉴权 / 字幕都没变",
              seen.get("url") == "https://jev.example/judge"
              and seen.get("body", {}).get("state", {}).get("text") == "吵起来了"
              and seen.get("headers", {}).get("Authorization") == "Bearer jev-key",
              str({k: seen.get(k) for k in ("url", "headers")}))

        section("④ EMOTION_BACKEND=local 强制 → 有钥匙也走本地")

        eb._has_llm_key = lambda cfg: True
        use_env({
            eb.ENV_BACKEND: "local",
            eb.ENV_JEV_KEY: "k",
            eb.ENV_JEV_ENDPOINT: "https://x",
        })
        mon._refresh_mood()
        check("★ 强制 local → 原 MoodCvReader",
              isinstance(mon.mood_reader, MoodCvReader)
              and not isinstance(mon.mood_reader, monitor_mod.EmotionMoodReader),
              type(mon.mood_reader).__name__)

        section("⑤ 显式 jev/llm 但没配齐 → 退回原本地（不报错）")

        eb._has_llm_key = lambda cfg: False
        use_env({eb.ENV_BACKEND: "llm"})
        mon._refresh_mood()
        check("llm 没钥匙 → MoodCvReader", isinstance(mon.mood_reader, MoodCvReader),
              type(mon.mood_reader).__name__)

        use_env({eb.ENV_BACKEND: "jev", eb.ENV_JEV_KEY: "only-key"})
        mon._refresh_mood()
        check("jev 只有 key 没地址 → MoodCvReader",
              isinstance(mon.mood_reader, MoodCvReader), type(mon.mood_reader).__name__)

        section("⑥ 红线")

        env_text = (eb.dotenv_path().parent / ".env.example").read_text(encoding="utf-8")
        check("★ .env.example 写明设 jev / llm 即启用",
              "设为 jev / llm 即启用" in env_text, "")

        source = Path(monitor_mod.__file__).read_text(encoding="utf-8")
        heavy = [name for name in ("import requests", "import httpx", "import aiohttp")
                 if name in source]
        check("★ 零新依赖：接线只用既有判读层", not heavy, str(heavy))
    finally:
        monitor_mod._emotion_env = original_env
        eb._has_llm_key = original_has_llm

    return summary("情绪判定后端接线（v2-social-emotion-wire）")


if __name__ == "__main__":
    raise SystemExit(main())
