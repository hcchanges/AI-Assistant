"""语音识别（C 多模态 · 听说闭环里的"听"）。

**本机优先、API 兜底**，跟情感记忆那条提炼线（life/mind/local_llm.py）同一套取舍：
本机 `data/models/` 里躺着 whisper 权重、而且装得起后端，就在本地认——**一个字节
都不出本机**；没有就回落 API 识别，行为跟"这功能本来就在云端"一样。

两种本地后端都认，谁先探到用谁：

    whisper.cpp      一个可执行文件（whisper-cli.exe / main.exe）+ ggml-*.bin 权重
                     —— 量化版，CPU 上跑得动，装了就能用，最省事
    faster-whisper   pip 装的 faster_whisper + CTranslate2 格式的模型目录
                     —— 同样是量化推理，好处是全在 Python 里

两个都不装、或者没有权重，都不是错误：那是**预期内**的情况（Gitee 用户拉下来
就没有模型），回落 API 就完了。所以这条线抛的是 `AsrUnavailable`，语义是
"这一趟没走成本地"，不是"出事了"。

**这条线一个字都不落库。** 认出来的文字交给调用方（听的那条线），音频本身只是
一个临时 WAV，用完就删——不进 pet.db、不进 mind.db、也不留在 data/ 里。
"""

from __future__ import annotations

import logging
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

logger = logging.getLogger(__name__)

# 认的时候钉死中文：这个宠物就说中文，让模型自己猜语言只会多一次判断、多一点错
LANGUAGE = "zh"
# 一次识别的超时（秒）。whisper.cpp 是子进程，卡住了得有人管
LOCAL_TIMEOUT_SEC = 120

# whisper.cpp 的权重命名（ggml-base.bin / whisper-large-v3-turbo.gguf 之类）
_WHISPER_HINTS = ("ggml", "whisper")
# whisper.cpp 的可执行文件（不同版本叫法不一样）
_EXE_NAMES = ("whisper-cli.exe", "main.exe", "whisper.exe", "whisper-cli", "main")
# CTranslate2 格式（faster-whisper 吃的）认这两个文件，缺一不可
_CT2_FILES = ("model.bin", "config.json")


class AsrUnavailable(RuntimeError):
    """这一趟没走成本地（没模型 / 没后端 / 跑挂了）——调用方该回落 API。

    这是**预期内**的情况，不是错误。
    """


class Backend(Protocol):
    """本地识别器的样子：给一个 WAV 文件，吐一段文字。"""

    def transcribe(self, wav_path: Path) -> str: ...


# 探测 + 加载一次要好几秒、还要占几百 MB，不能每轮重来。结果按模型目录缓存：
# 缓存值可能是 None（=这个目录下没模型），那也是一次有效结论，省得每轮重扫盘。
_lock = threading.Lock()
_cache: dict[str, Backend | None] = {}


@dataclass(frozen=True)
class Result:
    """认出来的结果 + 这一趟走的哪条腿。"""

    text: str
    source: str          # "local" / "api" / ""（两边都没成）

    @property
    def ok(self) -> bool:
        return bool(self.text)


# ---------- 探测 ----------


def find_model(models_dir: Path) -> Path | None:
    """在模型目录里找一个 whisper 权重；没有返回 None。

    两种布局都认：单个 ggml/whisper 权重文件，或者一个 CTranslate2 模型目录。
    **靠名字认 whisper**：同一个 data/models/ 底下还躺着 LLM 的权重（量化的小
    qwen），不能把那个当成语音模型捞进来。
    """
    models_dir = Path(models_dir)
    if not models_dir.is_dir():
        return None

    # whisper.cpp：ggml-*.bin / *.gguf，文件名里带 ggml 或 whisper
    files = sorted(
        p for p in models_dir.rglob("*")
        if p.is_file() and p.suffix.lower() in (".bin", ".gguf")
        and any(hint in p.name.lower() for hint in _WHISPER_HINTS)
    )
    if files:
        return files[0]

    # faster-whisper：CTranslate2 目录，model.bin + config.json 缺一不可。
    # 只认 config.json 会把 HuggingFace 格式的目录（model.safetensors）也捞进来，
    # 那个 faster-whisper 读不了。
    for child in sorted(p for p in models_dir.iterdir() if p.is_dir()):
        if all((child / name).exists() for name in _CT2_FILES):
            return child
    return None


def find_exe(models_dir: Path) -> Path | None:
    """找 whisper.cpp 的可执行文件：先看模型目录（用户可能整个解压在这儿），
    再看 PATH。都没有返回 None。"""
    models_dir = Path(models_dir)
    if models_dir.is_dir():
        for name in _EXE_NAMES:
            for found in sorted(models_dir.rglob(name)):
                if found.is_file():
                    return found
    for name in _EXE_NAMES:
        located = shutil.which(name)
        if located:
            return Path(located)
    return None


def available(models_dir: Path) -> bool:
    """本机现在能不能在本地认。"""
    return load(models_dir) is not None


# ---------- 加载 ----------


def load(models_dir: Path) -> Backend | None:
    """探测 + 把本地识别器建起来；没有可用模型返回 None。结果按目录缓存。"""
    key = str(Path(models_dir))
    with _lock:
        if key in _cache:
            return _cache[key]
        backend: Backend | None = None
        found = find_model(Path(models_dir))
        if found is not None:
            backend = _build_backend(Path(models_dir), found)
        _cache[key] = backend
        return backend


def reset() -> None:
    """清掉缓存（测试用；以后加"换模型"的界面时也走它）。"""
    with _lock:
        _cache.clear()


def _build_backend(models_dir: Path, found: Path) -> Backend | None:
    """按权重长什么样挑后端。挑不出来（后端没装）记一笔，返回 None。"""
    if found.is_dir():
        backend = _build_faster_whisper(found)
        if backend is not None:
            return backend
        logger.info("语音识别：有模型目录但没有 faster-whisper 后端，改试 whisper.cpp")
    exe = find_exe(models_dir)
    if exe is not None:
        return _build_whisper_cpp(exe, found)
    logger.info("语音识别：本机没装 whisper 后端（faster-whisper 或 whisper.cpp），回落 API")
    return None


def _build_faster_whisper(model_dir: Path) -> Backend | None:
    try:
        from faster_whisper import WhisperModel
    except Exception:  # noqa: BLE001 —— 没装就是没装，不是错误
        return None
    try:
        # int8 量化：CPU 上跑得动，这也是这条线唯一现实的配置
        model = WhisperModel(str(model_dir), device="cpu", compute_type="int8")
    except Exception as exc:  # noqa: BLE001 —— 权重坏了/版本不对，都算"本地这趟没成"
        logger.warning("语音识别：faster-whisper 加载失败（%s）", exc)
        return None

    class _FasterWhisper:
        def transcribe(self, wav_path: Path) -> str:
            segments, _info = model.transcribe(str(wav_path), language=LANGUAGE)
            return "".join(segment.text for segment in segments).strip()

    return _FasterWhisper()


def _build_whisper_cpp(exe: Path, model: Path) -> Backend | None:
    class _WhisperCpp:
        def transcribe(self, wav_path: Path) -> str:
            # -nt 不要时间戳、-np 不要进度条：stdout 上只剩正文
            command = [
                str(exe), "-m", str(model), "-f", str(wav_path),
                "-l", LANGUAGE, "-nt", "-np",
            ]
            try:
                done = subprocess.run(
                    command, capture_output=True, timeout=LOCAL_TIMEOUT_SEC, check=False
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise AsrUnavailable(f"whisper.cpp 没跑起来：{exc}") from exc
            if done.returncode != 0:
                detail = done.stderr.decode("utf-8", "ignore").strip()[-200:]
                raise AsrUnavailable(f"whisper.cpp 退出码 {done.returncode}：{detail}")
            return done.stdout.decode("utf-8", "ignore").strip()

    return _WhisperCpp()


# ---------- 识别 ----------


def transcribe_local(models_dir: Path, wav_path: Path) -> str:
    """本地认一遍。没有可用后端就抛 `AsrUnavailable`（调用方回落 API）。"""
    backend = load(models_dir)
    if backend is None:
        raise AsrUnavailable("本机没有可用的 whisper 模型或后端")
    return backend.transcribe(Path(wav_path))


def _api_client(provider: Any, api_key: str) -> Any:
    """建一个 OpenAI 兼容客户端。**单独抽出来是为了测试能换掉它。**"""
    from openai import OpenAI

    return OpenAI(api_key=api_key, base_url=provider.base_url)


def transcribe_api(config: Any, wav_path: Path) -> str:
    """走 API 认一遍。没 Key / 没装 SDK / 请求失败都抛 `AsrUnavailable`。

    接口跟看图那条线（LLMFactory.create_vision）一个套路：**复用某个供应商的
    地址与 Key，只换模型名**——不新增一套凭据，也不新增依赖。
    """
    voice = config.voice
    provider = config.llm.provider(voice.asr_provider or None)
    api_key = config.get_api_key(provider)
    if not api_key:
        raise AsrUnavailable(f"还没填 {provider.name} 的 API Key（语音识别要用）")
    try:
        client = _api_client(provider, api_key)
    except Exception as exc:  # noqa: BLE001 —— 没装 openai 就当作这条路走不通
        raise AsrUnavailable(f"没有可用的 HTTP 客户端：{exc}") from exc
    try:
        with open(wav_path, "rb") as audio:
            answer = client.audio.transcriptions.create(
                model=voice.asr_model, file=audio, language=LANGUAGE
            )
    except Exception as exc:  # noqa: BLE001 —— 网络/额度/模型名不对，一律回落成"没认出来"
        raise AsrUnavailable(f"API 识别失败：{exc}") from exc
    return str(getattr(answer, "text", "") or "").strip()


def recognize(config: Any, wav_path: Path) -> Result:
    """认一段 WAV：本机有模型就在本地认，没有回落 API。两边都不成就返回空结果。

    **先本地后 API 的顺序是硬的**：`asr_local` 关掉才是显式要求走 API（用户自己
    决定的），否则只要有本地模型就绝不出网——对话原文不出本机这件事，是这条线
    最该守住的东西。
    """
    if config.voice.asr_local:
        try:
            text = transcribe_local(config.asr_models_dir, wav_path)
        except AsrUnavailable as exc:
            logger.info("语音识别：本机这次没用上（%s），回落 API 识别", exc)
        else:
            return Result(text=text, source="local")
    try:
        return Result(text=transcribe_api(config, wav_path), source="api")
    except AsrUnavailable as exc:
        logger.warning("语音识别：两条腿都没走成（%s）", exc)
        return Result(text="", source="")
