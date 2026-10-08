"""本地小模型（A++ 本地优先）：本机放着量化模型就离线跑提炼，没有就回落 API。

为什么要它：情感记忆的提炼（``distill``）要把**用户的对话原文**发给模型。发给云端
本身是一次外发——那是 S7 明确允许的（屏幕内容才是不许出本机的），但对"私房话不想
上云"的用户，本机只要有个小模型就该优先在本地跑。所以这个模块只干一件事：
把 distill 那一次 ``chat_json`` 换成"本机有模型就本地算，没有就照旧走 API"。

红线（跟其它几轮一致，这里再钉一遍）：

- **不新增依赖**。本地推理要用 ``llama-cpp-python`` 或 ``transformers``，但这俩都
  **不在** requirements.txt 里——用户自己装了就用，没装就走 API。这里 import 它们
  一律包在 try 里，缺了不算错、只算"本机没有本地能力"。
- **不往仓库塞模型**。权重放 ``data/models/``（已在 .gitignore），本文件里只有代码。
- **不碰 pet.db / mind.db**。这里只负责"算出 JSON"，写库还是 distill 的事。
- **危机词拦截在 distill 之前**（chatter 那条线），本地 / 云端都绕不过去。

探测规则（``find_model``）：只看 ``data/models/`` 下
1. 先找 ``*.gguf``（量化模型的正统格式；文件名带 qwen 的优先）；
2. 没有再找 HuggingFace 格式的目录（有 config.json + safetensors/pytorch 权重）。
都找不到 → ``None`` → 调用方回落 API。注意 bge 那个向量模型目录**不会被认成 LLM**
（它是 ``model.onnx``，两个条件都不满足）。
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 一次最多吐多少 token。提炼要的是结构化短 JSON，512 足够；
# 本地小模型生成慢，给多了每轮白等好几秒
MAX_NEW_TOKENS = 512
# 文件名里带它就优先挑（本机可能同时躺着好几个模型）
PREFER_HINT = "qwen"
# 文件名里带它的**不是** LLM 权重，别捞：语音那套（ggml-*.bin / whisper-*.gguf）
# 跟 LLM 权重住同一个 data/models/，是两码事（见 life/voice/asr.py）
_NOT_LLM_HINTS = ("whisper", "ggml")


class LocalUnavailable(RuntimeError):
    """本机没有可用的本地模型，或本地这一趟没跑成——调用方该回落 API。

    这是**预期内**的情况，不是错误：Gitee 用户拉下来就没有模型，全靠它回落。
    """


class Runtime(Protocol):
    """本地推理器的样子：给一段 prompt，吐一段文本。"""

    def complete(self, prompt: str) -> str: ...


# 探测 + 加载一次要好几秒、还要占几百 MB，不能每轮重来。结果按模型目录缓存：
# 缓存值可能是 None（=这个目录下没模型），那也是一次有效结论，省得每轮重扫盘。
_lock = threading.Lock()
_cache: dict[str, Runtime | None] = {}


# ---------- 探测 ----------


def find_model(models_dir: Path) -> Path | None:
    """在模型目录里找一个能用的 LLM 权重；没有返回 None。"""
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        return None

    ggufs = sorted(
        p for p in models_dir.rglob("*.gguf")
        if p.is_file() and not any(h in p.name.lower() for h in _NOT_LLM_HINTS)
    )
    if ggufs:
        preferred = [p for p in ggufs if PREFER_HINT in p.name.lower()]
        return (preferred or ggufs)[0]

    for child in sorted(p for p in models_dir.iterdir() if p.is_dir()):
        # HuggingFace 格式：有 config.json 才算，且必须有真正的权重文件。
        # 只认 config.json 会把 bge 那种只有 .onnx 的目录也捞进来。
        if not (child / "config.json").exists():
            continue
        has_weights = bool(
            list(child.glob("*.safetensors")) or list(child.glob("*.bin"))
        )
        if has_weights:
            return child
    return None


def available(models_dir: Path) -> bool:
    """本机现在有没有能用的本地模型。"""
    return load(models_dir) is not None


# ---------- 加载 ----------


def load(models_dir: Path) -> Runtime | None:
    """探测 + 把本地推理器建起来；没有可用模型返回 None。结果按目录缓存。"""
    key = str(Path(models_dir))
    with _lock:
        if key in _cache:
            return _cache[key]
        runtime: Runtime | None = None
        found = find_model(Path(models_dir))
        if found is not None:
            runtime = _build_runtime(found)
        _cache[key] = runtime
        return runtime


def reset() -> None:
    """清掉缓存（测试用；以后加"换模型"的界面时也走它）。"""
    with _lock:
        _cache.clear()


def _build_runtime(path: Path) -> Runtime | None:
    """按权重格式挑一个后端建起来。装不出后端就返回 None（=回落 API）。"""
    if path.suffix.lower() == ".gguf":
        return _build_llama_cpp(path)
    return _build_transformers(path)


def _build_llama_cpp(path: Path) -> Runtime | None:
    try:
        from llama_cpp import Llama
    except Exception:  # noqa: BLE001 —— 没装是常态，不该把提炼这条线炸掉
        logger.info(
            "本地模型：找到 %s，但没装 llama-cpp-python（pip install llama-cpp-python），"
            "这次仍走 API", path.name,
        )
        return None
    try:
        llm = Llama(model_path=str(path), n_ctx=2048, verbose=False)
    except Exception as exc:  # noqa: BLE001 —— 权重坏了/显存不够，同样回落 API
        logger.warning("本地模型：%s 加载失败（%s），回落 API", path.name, exc)
        return None

    class _LlamaCpp:
        def complete(self, prompt: str) -> str:
            out = llm(prompt, max_tokens=MAX_NEW_TOKENS, temperature=0.7)
            choices = out.get("choices") or []
            return str(choices[0].get("text", "")) if choices else ""

    return _LlamaCpp()


def _build_transformers(path: Path) -> Runtime | None:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except Exception:  # noqa: BLE001 —— 同上：没装是常态
        logger.info(
            "本地模型：找到 %s，但没装 transformers（pip install torch transformers），"
            "这次仍走 API", path.name,
        )
        return None
    try:
        tokenizer = AutoTokenizer.from_pretrained(str(path))
        model = AutoModelForCausalLM.from_pretrained(str(path))
        model.eval()
    except Exception as exc:  # noqa: BLE001 —— 权重不全/版本不匹配，回落 API
        logger.warning("本地模型：%s 加载失败（%s），回落 API", path.name, exc)
        return None

    class _Transformers:
        def complete(self, prompt: str) -> str:
            inputs = tokenizer(prompt, return_tensors="pt")
            with torch.no_grad():
                out = model.generate(
                    **inputs, max_new_tokens=MAX_NEW_TOKENS, do_sample=True, temperature=0.7
                )
            new_tokens = out[0][inputs["input_ids"].shape[1]:]
            return str(tokenizer.decode(new_tokens, skip_special_tokens=True))

    return _Transformers()


# ---------- 推理 ----------


def infer(models_dir: Path, prompt: str) -> dict[str, Any]:
    """本地跑一次，回解析好的 JSON。本机没能力 / 这趟没跑成 → 抛 LocalUnavailable。

    抛而不是回空 dict：调用方要能分清"模型说了个空结果"和"本机压根没这能力"，
    后者得回落 API，前者是模型自己的判断。本地这趟跑砸了也一律算后者——
    宁可多花一趟 API 的钱，也不能把这批对话丢了。
    """
    runtime = load(models_dir)
    if runtime is None:
        raise LocalUnavailable("本机没有可用的本地模型")

    try:
        text = runtime.complete(prompt)
    except Exception as exc:  # noqa: BLE001 —— 推理中途炸了（爆内存等），回落 API
        logger.warning("本地模型：这一趟推理失败（%s），回落 API", exc)
        raise LocalUnavailable(f"本地推理失败：{exc}") from exc

    try:
        return parse_json(text)
    except ValueError as exc:
        logger.warning("本地模型：吐出来的不是 JSON（%s），回落 API", exc)
        raise LocalUnavailable(f"本地输出不是 JSON：{exc}") from exc


# 小模型爱把 JSON 包在 ```json 围栏里，或者前面先客套一句
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def parse_json(text: str) -> dict[str, Any]:
    """从模型吐的文本里把 JSON 抠出来。抠不出抛 ValueError。

    容错要厚：本地 0.5b 那种小模型不像云端那么听话，围栏、前后废话、"好的，"
    这类都见得多。先撕围栏，再退一步取第一个 { 到最后一个 }。
    """
    text = (text or "").strip()
    match = _FENCE_RE.search(text)
    if match:
        text = match.group(1).strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError(f"没找到 JSON：{text[:60]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise ValueError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ValueError(f"不是 JSON 对象：{type(data).__name__}")
    return data
