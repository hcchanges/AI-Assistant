"""语音输入（C 多模态 · 听说闭环）的自动测试。

覆盖：
- S1 探测：认得出 whisper 的两种权重布局（whisper.cpp 的 ggml / faster-whisper 的
  CTranslate2 目录），且**不把 LLM 的权重当成语音模型**（同一个 data/models/ 里躺着）；
- S1 本机优先：有本地模型就走本地、一次 API 都不调；没有就回落 API；
  两条腿都不成就说"没认出来"（不是崩溃）；
- S2 采集：按住才收音、松手才收尾、太短/静音不算话、录满自动收尾；**不按不收音**；
- S2 快捷键：纯函数解析（不碰系统）；
- S3 接入（**v2-chat-core #1 已摘**）：引擎照旧跑得通，但 bootstrap 那根
  `heard → _on_chat_send` 的线摘了——认出来的话不再自己溜进对话链；
  用户看得见的入口（菜单项、聊天框按钮、全局快捷键）也一并没了。
- S4 红线：危机词最高优先（连模型都不调；入口换成打字那条，判据一个字没改）、
  语音这条线自己不落库、无音频残留、无新依赖、无语音权重进仓库。

真的麦克风、真的 whisper 权重、真的 API 都不可能出现在测试机上，所以这些腿用假替身
（跟 test_local_llm.py 一个套路）；**但走的都是真链路**——真 Recorder、真 VoiceListener、
真 recognize 的分支判断，只把"设备/后端"这一层换掉。
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from array import array
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import Recorder, check, drain_threads, make_env, pump, section, summary, wait_for

from life.dialog import router
from life.voice import asr
from life.voice.capture import SAMPLE_RATE, Recorder as MicRecorder, to_wav, _usable
from life.voice.hotkey import GlobalHotkey, parse
from life.voice.listen import NO_ROUTE, NO_SPEECH, VoiceListener

_FAKE_PCM_PEAK = 8000


def tone_pcm(seconds: float = 1.0, peak: int = _FAKE_PCM_PEAK) -> bytes:
    """一段"有声音"的假 PCM：16k 单声道 16 位方波。够响，不会被判成静音。"""
    count = int(SAMPLE_RATE * seconds)
    return array("h", [peak if index % 2 else -peak for index in range(count)]).tobytes()


class FakeStream:
    """假的采集会话：不做任何采集，只把事先准备好的一段 PCM 交出来。"""

    def __init__(self, pcm: bytes):
        self._pcm = pcm
        self.stopped = False

    def read_all(self) -> bytes:
        return self._pcm

    def stop(self) -> None:
        self.stopped = True


def fake_mic(pcm: bytes) -> tuple[MicRecorder, list[FakeStream]]:
    """造一个"接着假麦克风"的采集器：start() 真的走 Recorder 的逻辑，
    只是拿到的不是真设备。"""
    made: list[FakeStream] = []
    recorder = MicRecorder()
    # 无头环境里 Qt 未必枚举得到输入设备；这个替身代表"一台有麦克风的机器"，
    # 所以设备这一问也一并换掉。start/stop 那些逻辑仍是真的 Recorder 在跑。
    recorder.available = lambda: True

    def _factory() -> FakeStream:
        stream = FakeStream(pcm)
        made.append(stream)
        return stream

    recorder._stream_factory = _factory
    return recorder, made


def fake_recognizer(text: str, source: str = "local"):
    """假的识别：不加载模型、不联网，只记下"被叫到几次、拿到的什么路径"。"""
    calls: list[Path] = []

    def _recognize(_config, wav_path: Path) -> asr.Result:
        calls.append(Path(wav_path))
        return asr.Result(text=text, source=source)

    return _recognize, calls


class _FakeApi:
    """假的 OpenAI 兼容客户端：只记下"被调用时带了什么"，然后回一句固定的话。"""

    def __init__(self, text: str = "今天好累"):
        self.calls: list[dict] = []
        fake = self

        class _Transcriptions:
            def create(self, **kwargs):
                fake.calls.append(kwargs)
                return SimpleNamespace(text=text)

        self.audio = SimpleNamespace(transcriptions=_Transcriptions())


def _memory_rows(pet_path: Path) -> int:
    conn = sqlite3.connect(str(pet_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    finally:
        conn.close()


def _snapshot(path: Path) -> tuple[int, float]:
    stat = path.stat()
    return stat.st_size, stat.st_mtime


def main() -> int:
    section("S1 探测：whisper 的两种权重布局都认")
    with tempfile.TemporaryDirectory() as tmp:
        models = Path(tmp)
        check("空目录 = 没有模型", asr.find_model(models) is None, "")

        # whisper.cpp：ggml-*.bin
        (models / "ggml-base.bin").write_bytes(b"\x00" * 16)
        found = asr.find_model(models)
        check("认 ggml-*.bin", found is not None and found.name == "ggml-base.bin",
              str(found))

        # LLM 的量化权重躺在同一个目录里——不能被当成语音模型
        (models / "qwen2.5-0.5b-instruct-q4_k_m.gguf").write_bytes(b"\x00" * 16)
        found = asr.find_model(models)
        check("LLM 的 gguf 不会被当成语音模型（靠名字认）",
              found is not None and "qwen" not in found.name, str(found))

    # faster-whisper：CTranslate2 目录（model.bin + config.json 缺一不可）。
    # 单独开一个目录：上面那个已经躺了 ggml 权重，而 ggml 是优先的那一路
    with tempfile.TemporaryDirectory() as tmp2:
        models = Path(tmp2)
        empty = models / "some-ct2"
        empty.mkdir()
        (empty / "config.json").write_text("{}", encoding="utf-8")
        check("只有 config.json 不算（那可能是 HF 格式，faster-whisper 读不了）",
              asr.find_model(models) is None, str(asr.find_model(models)))
        (empty / "model.bin").write_bytes(b"\x00" * 16)
        check("model.bin + config.json → 认这个目录",
              asr.find_model(models) == empty, str(asr.find_model(models)))

        # HuggingFace 格式的目录（model.safetensors）不该被捞进来
        hf = models / "whisper-hf"
        hf.mkdir()
        (hf / "config.json").write_text("{}", encoding="utf-8")
        (hf / "model.safetensors").write_bytes(b"\x00" * 16)
        check("HF 格式的目录（model.safetensors）不算 CTranslate2",
              asr.find_model(models) != hf, "")
        # 两种布局都在（同一个目录里各放一份）时，ggml 优先——whisper.cpp 那条路
        # 最省事，优先挑它
        (models / "ggml-small.bin").write_bytes(b"\x00" * 16)
        check("ggml 与 CT2 目录都在时，优先 ggml",
              (asr.find_model(models) or Path()).name == "ggml-small.bin",
              str(asr.find_model(models)))

    section("S1 探测：后端与可用性")
    with tempfile.TemporaryDirectory() as tmp:
        models = Path(tmp)
        check("没权重 → 没有可执行文件也不算可用", not asr.available(models), "")
        # 探测结果按目录缓存：None 也是一次有效结论
        check("同一目录第二次探测直接命中缓存", asr.load(models) is None, "")
        asr.reset()

        (models / "whisper-cli.exe").write_bytes(b"MZ")
        check("找得到模型目录里的 whisper.cpp 可执行文件",
              (asr.find_exe(models) or Path("")).name == "whisper-cli.exe",
              str(asr.find_exe(models)))
        asr.reset()

    section("S1 探测：LLM 那条线不会被语音权重带偏（同一个 data/models/）")
    from life.mind import local_llm

    with tempfile.TemporaryDirectory() as tmp:
        models = Path(tmp)
        (models / "ggml-medium.bin").write_bytes(b"\x00" * 16)
        check("只有 whisper 权重时，LLM 那条线找不到模型（不硬加载一个语音模型）",
              local_llm.find_model(models) is None, str(local_llm.find_model(models)))
        (models / "qwen2.5-1.5b-q4.gguf").write_bytes(b"\x00" * 16)
        picked = local_llm.find_model(models)
        check("两样都在时，LLM 只挑 qwen 那个",
              picked is not None and "qwen" in picked.name, str(picked))

    section("S2 采集：按住才收音，松手才收尾")
    recorder, made = fake_mic(tone_pcm(1.0))
    check("刚建好：没在收（不常驻）", not recorder.listening, "")
    check("没 start 就 stop：什么也没收到", recorder.stop() == b"", "")
    check("没 start 时一次采集都没开", made == [], "")

    check("start() 成功了", recorder.start() is True, "")
    check("start 之后才算在收", recorder.listening, "")
    check("重复 start 不会开第二路", recorder.start() is False and len(made) == 1, "")
    wav = recorder.stop()
    check("stop 之后就不在收了", not recorder.listening, "")
    check("松手后采集真的被关掉（不常驻监听）", made[0].stopped, "")
    check("吐出来的是一段 WAV（RIFF 头 + 非空）",
          wav[:4] == b"RIFF" and len(wav) > 44, f"{len(wav)} 字节")
    check("时长对得上（1 秒 16k 单声道 16 位 ≈ 32KB）",
          31000 < len(wav) < 33000, f"{len(wav)} 字节")
    check("再 stop 一次不会重复吐", recorder.stop() == b"", "")

    section("S2 采集：不算话的那几种")
    check("太短（0.1 秒）→ 当没听清", not _usable(tone_pcm(0.1)), "")
    check("全是静音（1 秒 0 振幅）→ 当没听清",
          not _usable(array("h", [0] * SAMPLE_RATE).tobytes()), "")
    check("有点底噪但不说话（峰值 50）→ 当没听清",
          not _usable(tone_pcm(1.0, peak=50)), "")
    check("正常说话（峰值 8000）→ 算数", _usable(tone_pcm(1.0)), "")
    r2, _ = fake_mic(tone_pcm(0.1))
    r2.start()
    check("太短的一段，stop 出来是空（调用方据此说没听清）", r2.stop() == b"", "")

    section("S2 采集：WAV 头写对了")
    head = to_wav(tone_pcm(0.5))
    check("WAV 头：16kHz / 单声道 / 16 位",
          head[24:28] == (SAMPLE_RATE).to_bytes(4, "little")
          and head[22:24] == (1).to_bytes(2, "little")
          and head[34:36] == (16).to_bytes(2, "little"),   # 16 位 = 每个采样 2 字节
          f"rate={int.from_bytes(head[24:28], 'little')} "
          f"ch={int.from_bytes(head[22:24], 'little')} "
          f"bits={int.from_bytes(head[34:36], 'little')}")

    section("S2 快捷键：解析（纯函数，不碰系统）")
    check("Ctrl+Alt+Space 解析得出来", parse("Ctrl+Alt+Space") is not None, "")
    check("小写、空格混着写也认", parse(" ctrl + alt + space ") is not None, "")
    check("修饰键一个都不给 → 不认（不能抢占普通按键）", parse("Space") is None, "")
    check("只给修饰键 → 不认", parse("Ctrl+Alt") is None, "")
    check("认不出的键 → 不认", parse("Ctrl+Alt+Foo") is None, "")
    check("空串 → 不认", parse("") is None, "")
    parsed = parse("Ctrl+Shift+F9")
    check("F9 认得出来（0x78）", parsed is not None and parsed[1] == 0x78,
          str(parsed))
    hotkey = GlobalHotkey("Ctrl+Alt+Space")
    check("没 start 就不会去注册", hotkey.stop() is None, "")
    check("空配置的快捷键 start 返回 False（不抛）",
          GlobalHotkey("").start() is False, "")

    section("S3 端到端：不用真麦克风，走完按住说话这条路")
    box, stub, app = make_env()
    try:
        check("VoiceListener 有 heard/failed/listening_changed 三个信号",
              all(hasattr(VoiceListener, name)
                  for name in ("heard", "failed", "listening_changed")), "")

        recorder, made = fake_mic(tone_pcm(1.0))
        recognizer, calls = fake_recognizer("今天好累")
        listener = VoiceListener(app.config, recorder=recorder, recognizer=recognizer)
        heard, failed, states = [], [], []
        listener.heard.connect(heard.append)
        listener.failed.connect(failed.append)
        listener.listening_changed.connect(states.append)

        # 沙箱把整条语音线关着（不真出声、不真合成）；这条线要用就整个打开
        app.config.voice.enabled = True
        app.config.voice.asr_enabled = True
        check("按住前：没在听", not listener.listening, "")
        check("begin() 开始收", listener.begin() is True and listener.listening, "")
        check("界面收到「开始听」的状态", states == [True], str(states))
        check("收着的这段时间里，一次识别都还没发生", calls == [], "")

        listener.finish()
        check("松手后立刻不在听了（不等识别结果）",
              not listener.listening and states == [True, False], str(states))
        wait_for(lambda: bool(heard) or bool(failed), timeout=5.0)
        drain_threads()
        pump(0.1)
        check("识别结果原样送出来，一个字没加工", heard == ["今天好累"], str(heard))
        check("识别时拿到的是一个真 WAV 文件路径",
              len(calls) == 1 and calls[0].suffix == ".wav", str(calls))
        check("那个临时音频用完就删了（不残留）",
              calls and not calls[0].exists(), str(calls[0]) if calls else "（没调用）")

        # v2-chat-core #1：用户看得见的语音输入入口全摘了，bootstrap 那根
        # `voice_listener.heard → _on_chat_send` 的线也跟着摘。这一节原来验的是
        # "认出来的话走打字那条同一个入口"，现在反过来锁：**引擎照旧跑得通，
        # 但认出来的话不会自己溜进对话链**。
        section("S3 接入（v2-chat-core #1 已摘）：引擎还在跑，接线没了")

        boot_src = (Path(__file__).resolve().parents[1] / "core" / "bootstrap.py").read_text(
            encoding="utf-8")
        check("装配层不再把 heard 接到对话入口",
              "voice_listener.heard.connect" not in boot_src, "")
        check("引擎实例还挂在应用上（摘的是入口，不是引擎）",
              getattr(app, "voice_listener", None) is not None, "")

        spy = Recorder(app.chatter_service, "handle")
        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        try:
            app.voice_listener._recorder = fake_mic(tone_pcm(1.0))[0]
            app.voice_listener._recognize = fake_recognizer("今天好累")[0]
            app.voice_listener.begin()
            app.voice_listener.finish()
            drain_threads()
            pump(0.3)
            check("认出来的话没有溜进 chatter.handle（线是真摘了，不是藏起来）",
                  not spy.called, str(spy.calls))
            check("也就不会有回话（pet_speak 上一个字都没有）", spoken == [], str(spoken))
        finally:
            spy.restore()
            app.bus.pet_speak.disconnect(spoken.append)

        # 那句话本身的判读没变——识别出来的文本交给对话链时，意图/情绪照样是现成的。
        # （入口摘了，不等于判据坏了。）
        route = router.classify("今天好累")
        check("「今天好累」→ 倾诉 + 低落（纯规则，不花模型）",
              route.intent == router.INTENT_VENT and route.emotion == "低落",
              f"{route.intent} / {route.emotion}")
    finally:
        box.cleanup()
        drain_threads()

    section("S4 红线：危机词仍然最高优先（入口换成打字那条，判据一个字没改）")
    box, stub, app = make_env()
    try:
        # v2-chat-core #1 之后语音那条腿不再自动接进对话链，所以这条红线改从
        # **打字那条入口**验——它是同一句文本进去、同一个 chatter.handle 收口，
        # 判据（crisis 在 respond 开头拦截）本来就跟"字从哪来"无关。
        stub.messages.clear()
        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        try:
            app.chatter_service.handle("我不想活了")
            drain_threads()
            pump(0.3)
            check("危机句 → 模型一次都没调", stub.messages == [],
                  f"{len(stub.messages)} 次")
            check("走的是危机话术（含热线）",
                  bool(spoken) and "12356" in spoken[-1],
                  spoken[-1][:40] if spoken else "（没说话）")
        finally:
            app.bus.pet_speak.disconnect(spoken.append)
    finally:
        box.cleanup()
        drain_threads()

    section("S4 红线：语音这条线自己不落库、不留音频")
    box, stub, app = make_env()
    try:
        pet_path = app.config.db_path
        mind_path = app.config.abs_path(app.config.mind.db_path)
        temp_before = set(Path(tempfile.gettempdir()).glob("desktoppet_asr_*"))
        before_rows = _memory_rows(pet_path)
        before_pet = _snapshot(pet_path)
        before_mind = _snapshot(mind_path) if mind_path.exists() else None

        # 沙箱把整条语音线关着（不真出声、不真合成）；这条线要用就整个打开
        app.config.voice.enabled = True
        app.config.voice.asr_enabled = True
        recorder, _ = fake_mic(tone_pcm(1.0))
        recognizer, calls = fake_recognizer("记一下明天买牛奶")
        listener = VoiceListener(app.config, recorder=recorder, recognizer=recognizer)
        heard: list[str] = []
        listener.heard.connect(heard.append)
        listener.begin()
        listener.finish()
        wait_for(lambda: bool(heard), timeout=5.0)
        drain_threads()
        pump(0.2)

        check("pet.db 的 memory 表一行没多", _memory_rows(pet_path) == before_rows,
              f"{before_rows} → {_memory_rows(pet_path)}")
        check("pet.db 文件没被动过", _snapshot(pet_path) == before_pet,
              f"{before_pet} → {_snapshot(pet_path)}")
        if before_mind is not None:
            check("mind.db 文件没被动过（语音这条线自己不写库）",
                  _snapshot(mind_path) == before_mind,
                  f"{before_mind} → {_snapshot(mind_path)}")
        temp_after = set(Path(tempfile.gettempdir()).glob("desktoppet_asr_*"))
        check("临时目录里没多出音频文件（用完就删）",
              temp_after <= temp_before, f"{len(temp_before)} → {len(temp_after)}")
        check("识别时那个临时文件确实已经删了",
              calls and not calls[0].exists(), str(calls))
    finally:
        box.cleanup()
        drain_threads()

    section("S4 红线：无新依赖 / 仓库里没有语音权重")
    root = Path(__file__).resolve().parents[1]
    req = (root / "requirements.txt").read_text(encoding="utf-8").lower()
    check("requirements 没有新增录音/识别依赖",
          not any(pkg in req for pkg in ("sounddevice", "pyaudio", "soundfile",
                                         "whisper", "faster-whisper", "ctranslate2")), "")
    check("data/models/ 在 .gitignore 里（权重不进仓库）",
          "data/models/" in (root / ".gitignore").read_text(encoding="utf-8"), "")
    check("语音这条线没往 pet.db 那套 memory 上写东西",
          not any("memory_dao" in (root / name).read_text(encoding="utf-8")
                  for name in ("life/voice/asr.py", "life/voice/capture.py",
                               "life/voice/listen.py")), "")
    check("语音这条线一个模型都不调（不生成任何 token）",
          not any("LLMFactory" in (root / name).read_text(encoding="utf-8")
                  for name in ("life/voice/capture.py", "life/voice/listen.py")), "")

    section("S1 识别：本机优先 / 回落 API / 都不成就说没认出来")
    box, stub, app = make_env()
    try:
        config = app.config
        # 沙箱把整条语音线关着（不真出声、不真合成）；这条线要用就整个打开
        config.voice.enabled = True
        config.voice.asr_enabled = True
        config.voice.asr_local = True
        models_dir = config.asr_models_dir
        models_dir.mkdir(parents=True, exist_ok=True)
        asr.reset()

        # ① 本机有模型：走本地，API 一次都不调
        class _LocalBackend:
            def transcribe(self, wav_path: Path) -> str:
                return "本机认出来的话"

        asr._cache[str(models_dir)] = _LocalBackend()
        fake_api = _FakeApi("API 认出来的话")
        original_client = asr._api_client
        asr._api_client = lambda provider, key: fake_api
        try:
            with tempfile.TemporaryDirectory() as tmp:
                wav = Path(tmp) / "a.wav"
                wav.write_bytes(to_wav(tone_pcm(0.5)))
                result = asr.recognize(config, wav)
                check("有本地模型 → 走本地", result.source == "local" and result.text == "本机认出来的话",
                      f"{result.source} / {result.text}")
                check("有本地模型 → API 一次都没调（对话原文不出本机）",
                      fake_api.calls == [], f"{len(fake_api.calls)} 次")

                # ② 本机没模型：回落 API
                asr._cache[str(models_dir)] = None
                fake_api.calls.clear()
                result = asr.recognize(config, wav)
                check("没本地模型 → 回落 API",
                      result.source == "api" and result.text == "API 认出来的话",
                      f"{result.source} / {result.text}")
                check("回落时真的调了 API 一次", len(fake_api.calls) == 1,
                      str(fake_api.calls))
                check("带上配置里的识别模型名",
                      fake_api.calls and fake_api.calls[0].get("model") == config.voice.asr_model,
                      str(fake_api.calls[0].get("model") if fake_api.calls else "（没调）"))
                check("钉的是中文", fake_api.calls and fake_api.calls[0].get("language") == "zh", "")

                # ③ 显式关掉本地：就算有模型也走 API
                asr._cache[str(models_dir)] = _LocalBackend()
                config.voice.asr_local = False
                fake_api.calls.clear()
                result = asr.recognize(config, wav)
                check("asr_local=False → 强制 API（有模型也不在本地跑）",
                      result.source == "api" and len(fake_api.calls) == 1,
                      f"{result.source} / {len(fake_api.calls)} 次")
                config.voice.asr_local = True

                # ④ 两条腿都不成：返回空结果，不是抛
                asr._cache[str(models_dir)] = None

                def _boom(_provider, _key):
                    raise asr.AsrUnavailable("测试用：客户端建不起来")

                asr._api_client = _boom
                result = asr.recognize(config, wav)
                check("两条腿都不成 → 空结果、source 为空（不抛）",
                      result.text == "" and result.source == "" and not result.ok,
                      f"{result.source!r} / {result.text!r}")
        finally:
            asr._api_client = original_client
            asr.reset()

        section("S3 认不出来时给的是人话（不是异常栈）")
        recorder, _ = fake_mic(tone_pcm(1.0))
        listener = VoiceListener(config, recorder=recorder,
                                 recognizer=lambda c, p: asr.Result("", "local"))
        failed: list[str] = []
        listener.failed.connect(failed.append)
        # 沙箱把整条语音线关着（不真出声、不真合成）；这条线要用就整个打开
        config.voice.enabled = True
        config.voice.asr_enabled = True
        listener.begin()
        listener.finish()
        wait_for(lambda: bool(failed), timeout=5.0)
        drain_threads()
        check("认出来是空的 → 说「没听清」", failed == [NO_SPEECH], str(failed))

        recorder2, _ = fake_mic(tone_pcm(1.0))
        listener2 = VoiceListener(config, recorder=recorder2,
                                  recognizer=lambda c, p: asr.Result("", ""))
        failed2: list[str] = []
        listener2.failed.connect(failed2.append)
        listener2.begin()
        listener2.finish()
        wait_for(lambda: bool(failed2), timeout=5.0)
        drain_threads()
        check("两条腿都不成 → 说「先打字吧」", failed2 == [NO_ROUTE], str(failed2))

        recorder3, _ = fake_mic(tone_pcm(1.0))
        listener3 = VoiceListener(config, recorder=recorder3,
                                  recognizer=fake_recognizer("x")[0])
        failed3: list[str] = []
        listener3.failed.connect(failed3.append)
        config.voice.asr_enabled = False
        check("语音输入关着时 begin() 直接拒绝（不偷偷开麦克风）",
              listener3.begin() is False and not listener3.listening, "")
        check("并且给了一句人话", bool(failed3) and "收不了音" in failed3[0], str(failed3))
        # 沙箱把整条语音线关着（不真出声、不真合成）；这条线要用就整个打开
        config.voice.enabled = True
        config.voice.asr_enabled = True

        section("S2 录满自动收尾：忘了松手也不会一直开着麦克风")
        recorder4, made4 = fake_mic(tone_pcm(1.0))
        listener4 = VoiceListener(config, recorder=recorder4,
                                  recognizer=fake_recognizer("太长了吧")[0])
        heard4: list[str] = []
        listener4.heard.connect(heard4.append)
        config.voice.asr_max_sec = 1        # 1 秒就到点
        check("开始收", listener4.begin() is True, "")
        wait_for(lambda: bool(heard4), timeout=8.0)
        drain_threads()
        check("到点自动收尾并识别（没等松手）", heard4 == ["太长了吧"], str(heard4))
        check("到点之后不再收音", not listener4.listening, "")
        check("麦克风真的被关了", made4 and made4[0].stopped, "")
    finally:
        box.cleanup()
        drain_threads()

    return summary("语音输入（听说闭环）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
