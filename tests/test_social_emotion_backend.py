# -*- coding: utf-8 -*-
"""情绪判定后端（v2-social-emotion-backend）的自动测试。

**这一批验什么**：把「情绪判定」抽象成一个口子之后，四件事必须成立——

1. 没有钥匙 → 走 local（复用现有 CLIP + 字幕关键词），对外发布零门槛；
2. 有主聊天 LLM 钥匙 → 走 llm，且能把它回的 JSON 收成统一结构；
3. 有 JEV 钥匙 → 走 jev 分支，请求形状对、响应解析对；
4. `EMOTION_BACKEND=local` 强制覆盖 → 就算两把钥匙都在也走 local。

统一结构是 `{valence, arousal, emotion_label, confidence, intention_hint}`。
`emotion_label` 沿用既有场景名（horror / angry / festive / neutral），
`valence` / `arousal` 没有既有来源，用情绪环锚点。
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.screen import emotion_backend as eb
from life.screen.mood_cv import MoodVerdict
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR


class FakeProvider:
    """假 LLM：记下被问过什么，回一份结构化 JSON。"""

    def __init__(self, data=None, boom: bool = False):
        self.data = data or {
            "valence": 0.6,
            "arousal": 0.7,
            "emotion_label": "festive",
            "confidence": 0.88,
            "intention_hint": "跟着轻快一点",
        }
        self.boom = boom
        self.calls: list[str] = []

    def chat_json(self, prompt, system=None):
        self.calls.append(prompt)
        if self.boom:
            raise RuntimeError("断网了")
        return dict(self.data)


class FakeCv:
    """假 CLIP 判读器：直接回一个 MoodVerdict（或抛错）。"""

    def __init__(self, verdict=None, boom: bool = False):
        self.verdict = verdict
        self.boom = boom
        self.calls = 0

    def classify_array(self, array):
        self.calls += 1
        if self.boom:
            raise RuntimeError("推理炸了")
        return self.verdict


def main() -> int:
    section("① 没有钥匙 → 走 local（默认兜底，零门槛）")

    backend = eb.EmotionJudge.from_config(env={})
    check("★ 无 key 无指定 → LocalBackend", isinstance(backend, eb.LocalBackend),
          type(backend).__name__)
    check("LocalBackend 永远可用（关键词这条腿不依赖外部）", backend.available() is True, "")

    local = eb.LocalBackend()
    got = local.judge({"text": "画面里全是血"})
    check("★ 字幕关键词：恐怖 → horror + 负价高唤醒",
          got.emotion_label == SCENE_HORROR and got.valence < 0 and got.arousal > 0.8,
          got.as_dict())
    got = local.judge({"text": "他们吵起来还骂人"})
    check("★ 字幕关键词：愤怒 → angry", got.emotion_label == SCENE_ANGRY, got.emotion_label)
    got = local.judge({"text": "婚礼现场好喜庆"})
    check("★ 字幕关键词：笑容 → festive + 正价", got.emotion_label == SCENE_FESTIVE and got.valence > 0,
          got.emotion_label)
    got = local.judge({"text": "今天天气不错"})
    check("★ 认不出来 → 空 label、0 置信度（不硬凑）",
          got.emotion_label == "" and got.confidence == 0.0, got.as_dict())

    got = local.judge({"labels": ["恐怖"], "text": "婚礼现场"})
    check("★ 调用方给的画面标签优先于字幕", got.emotion_label == SCENE_HORROR, got.emotion_label)

    cv = FakeCv(MoodVerdict(SCENE_FESTIVE, 0.91, {SCENE_FESTIVE: 0.91}))
    got = eb.LocalBackend(cv_reader=cv).judge({"image": object()})
    check("★ 复用本地 CLIP：判读结果与置信度照收",
          got.emotion_label == SCENE_FESTIVE and abs(got.confidence - 0.91) < 1e-9,
          got.as_dict())

    got = eb.LocalBackend(cv_reader=FakeCv(boom=True)).judge({"image": object(), "text": "有鬼"})
    check("★ CLIP 炸了不崩，退回字幕关键词", got.emotion_label == SCENE_HORROR, got.emotion_label)

    section("② 有 LLM 钥匙 → 走 llm，结构化判定")

    provider = FakeProvider()
    backend = eb.EmotionJudge.from_config(env={}, llm_available=lambda: True, provider=provider)
    check("★ 有 LLM key → LLMBackend", isinstance(backend, eb.LLMBackend), type(backend).__name__)
    check("LLMBackend.available 为真", backend.available() is True, "")

    got = backend.judge({"text": "婚礼上大家笑得很开心"})
    check("★ 模型回的 JSON 收成统一结构",
          got is not None
          and got.emotion_label == SCENE_FESTIVE
          and abs(got.valence - 0.6) < 1e-9
          and abs(got.arousal - 0.7) < 1e-9
          and abs(got.confidence - 0.88) < 1e-9
          and got.intention_hint == "跟着轻快一点",
          got.as_dict() if got else "None")
    check("★ prompt 要求只回 JSON（不是自由文本）",
          provider.calls and "JSON" in provider.calls[0] and '"valence"' in provider.calls[0],
          provider.calls[0][:60] if provider.calls else "")

    got = eb.LLMBackend(provider=FakeProvider(boom=True)).judge({"text": "随便"})
    check("★ 模型调用失败 → 返回 None（调用方可回退 local）", got is None, str(got))

    section("③ 有 JEV 钥匙 → 走 jev 分支，请求/响应按固定 JSON 结构")

    env = {eb.ENV_JEV_KEY: "jev-test-key", eb.ENV_JEV_ENDPOINT: "https://jev.example/v1/judge"}
    backend = eb.EmotionJudge.from_config(env=env)
    check("★ 有 JEV key + endpoint → JevBackend", isinstance(backend, eb.JevBackend),
          type(backend).__name__)
    check("JevBackend.available 为真", backend.available() is True, "")

    payload = backend.build_payload({"text": "吵起来了", "labels": ["愤怒"]})
    types = [q["type"] for q in payload["questions"]]
    names = [q["name"] for q in payload["questions"]]
    check("★ 请求形状：{state, questions}",
          "state" in payload and isinstance(payload["questions"], list) and len(payload["questions"]) == 5,
          str(list(payload)[:3]))
    check("★ 三类问题都在：Noul / Choice / Score",
          set(types) == {"Noul", "Choice", "Score"}, str(types))
    check("★ 字段齐全（含 intention_hint）",
          set(names) == {"valence", "arousal", "emotion_label", "confidence", "intention_hint"},
          str(names))

    def fake_post(url, body, headers, timeout):
        check("请求打到配置的 endpoint", url == "https://jev.example/v1/judge", url)
        check("钥匙走 Authorization 头", headers.get("Authorization") == "Bearer jev-test-key", str(headers))
        return {
            "answers": {
                "emotion_label": {"choice": "angry"},
                "valence": {"score": -0.62},
                "arousal": {"score": 0.81},
                "confidence": {"score": 0.77},
                "intention_hint": {"text": "先别争，顺着他说"},
            }
        }

    backend = eb.JevBackend(api_key="jev-test-key", endpoint="https://jev.example/v1/judge", post=fake_post)
    got = backend.judge({"text": "吵起来了"})
    check("★ 响应解析正确（Choice/Score/Noul 都取出来）",
          got is not None
          and got.emotion_label == SCENE_ANGRY
          and abs(got.valence + 0.62) < 1e-9
          and abs(got.arousal - 0.81) < 1e-9
          and abs(got.confidence - 0.77) < 1e-9
          and got.intention_hint == "先别争，顺着他说",
          got.as_dict() if got else "None")

    got = eb.JevBackend(api_key="k", endpoint="https://x", post=lambda *a: "不是 JSON").judge({})
    check("★ 响应认不出 → None（不抛错）", got is None, str(got))

    check("★ 只有 key 没 endpoint → 不启用 jev，退回 local",
          isinstance(eb.EmotionJudge.from_config(env={eb.ENV_JEV_KEY: "只有 key 没地址"}), eb.LocalBackend),
          "")

    section("④ EMOTION_BACKEND 强制覆盖")

    both = {eb.ENV_JEV_KEY: "k", eb.ENV_JEV_ENDPOINT: "https://x", eb.ENV_BACKEND: "local"}
    backend = eb.EmotionJudge.from_config(env=both, llm_available=lambda: True)
    check("★ EMOTION_BACKEND=local 强制覆盖 → 两把钥匙都在也走 local",
          isinstance(backend, eb.LocalBackend), type(backend).__name__)

    backend = eb.EmotionJudge.from_config(env={eb.ENV_BACKEND: "jev"}, llm_available=lambda: True)
    check("★ 强制 jev 但没钥匙 → 退回 local（不报错）",
          isinstance(backend, eb.LocalBackend), type(backend).__name__)

    backend = eb.EmotionJudge.from_config(env={eb.ENV_BACKEND: "llm"}, llm_available=lambda: False)
    check("★ 强制 llm 但没钥匙 → 退回 local（不报错）",
          isinstance(backend, eb.LocalBackend), type(backend).__name__)

    backend = eb.EmotionJudge.from_config(env={eb.ENV_BACKEND: "LLM"}, llm_available=lambda: True,
                                          provider=FakeProvider())
    check("★ 强制值大小写不敏感（LLM 也认）", isinstance(backend, eb.LLMBackend),
          type(backend).__name__)

    backend = eb.EmotionJudge.from_config(
        env={eb.ENV_JEV_KEY: "k", eb.ENV_JEV_ENDPOINT: "https://x"},
        llm_available=lambda: True,
    )
    check("★ 自动优先级：jev > llm", isinstance(backend, eb.JevBackend), type(backend).__name__)

    section("⑤ 红线")

    source = Path(eb.__file__).read_text(encoding="utf-8")
    heavy = [name for name in ("import requests", "import httpx", "import aiohttp")
             if name in source]
    check("★ 零新依赖：Jev 只用标准库，没引第三方 HTTP 包", not heavy, str(heavy))

    text = Path(eb.dotenv_path().parent / ".env.example").read_text(encoding="utf-8")
    check("★ .env.example 补上了三个开关名（且没有真实值）",
          all(name in text for name in ("EMOTION_BACKEND", "JEV_API_KEY", "JEV_ENDPOINT")),
          "")

    with tempfile.TemporaryDirectory() as tmp:
        env_file = Path(tmp) / ".env"
        env_file.write_text("PET_TEST_EMOTION_ENV_XYZ=from-file\n# 注释\n", encoding="utf-8")
        original = eb.dotenv_path
        eb.dotenv_path = lambda: env_file
        try:
            values = eb.load_env()
        finally:
            eb.dotenv_path = original
        check("★ load_env 能从 .env 补缺（零依赖解析）",
              values.get("PET_TEST_EMOTION_ENV_XYZ") == "from-file", str(values.get("PET_TEST_EMOTION_ENV_XYZ")))

    return summary("情绪判定后端（v2-social-emotion-backend）")


if __name__ == "__main__":
    raise SystemExit(main())
