"""本地语音合成（C 多模态 · 语音先行版）。

**后端：Windows 自带 SAPI5（System.Speech），零第三方依赖、零模型权重、全程本机。**

为什么先行版用 SAPI 而不是 VITS/CosyVoice，得说清楚——这不是偷懒，是红线逼的：

- VITS / CosyVoice 都要 torch，本机没装（装一次 2GB+），装上也下不到权重离线验；
- 常驻红线是「真跑行为验证」，SAPI 是本机唯一能端到端真出声、还能落成 WAV 拿去断言的东西；
- 零依赖 + 零权重，正好也踩在「模型文件不进 git」这条上——没有权重可漏。

**权重落点 `data/models/` 这一条为它留着**：将来换 VITS/CosyVoice，`VoiceEngine.say(text, tone)`
这个口子不动，只把 `_synthesize` 底下换成「从 data/models/ 加载模型」的实现，调用方一行不用改。
`data/models/` 已经在 .gitignore 里（本项目模型目录本就叫这个，见 core/config.py 的 model_dir）。

**隐私红线：文本只在本地合成，一个字都不出本机。** SAPI 是系统本地语音，不联网、
不上传；这条线的输入就是「要念的那句话」，跟 LLM 那条线完全分开——LLM 只发对话文字，
TTS 只念已经生成好的文字，两者都不碰屏幕内容、不碰记忆库。

**降级口径：** 没中文语音、PowerShell 调不起来、合成报错——一律「只出文字不报错」，
`say()` 静默返回 False，日志留一句，绝不往外抛、绝不拖累任何一条说话线。

**语气 → 音色/语速（S3）：** SAPI 里可调的杠杆是 rate（语速，-10~10）与 volume（0~100）。
本机只装了「Microsoft Huihui Desktop」一把中文嗓子，「音色」没法在两把嗓子之间切，
所以先行版把语气落在**语速 + 音量**上（越亲近越慢、越轻）；pitch 在 TONE_PROFILES 里
留了口子，等模型后端再接。映射规则是死的、可测的，不交给模型。
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
import winsound
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 语气 → 语速 / 音量。rate 负数是慢，volume 0~100。pitch 留给未来模型后端（SAPI 这条腿不接）。
TONE_PROFILES: dict[str, dict[str, int]] = {
    "default": {"rate": 0, "volume": 100, "pitch": 0},
    "closer": {"rate": -1, "volume": 96, "pitch": 0},
    "confide": {"rate": -2, "volume": 90, "pitch": 0},
}

# SAPI5 里挑中文嗓子的依据：Culture 以 zh 开头
_ZH_CULTURE_PREFIX = "zh"

# 合成脚本：把 UTF-8 文本文件念成 WAV。全 ASCII（中文在文件里），没有编码坑。
# rate / volume / voice 由调用方以字符串塞进来（voice 带空格，得用单引号包住）。
_PS_SCRIPT = (
    "Add-Type -AssemblyName System.Speech;"
    "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
    "try { $s.SelectVoice(%s) } catch {};"
    "$s.Rate = %s;"
    "$s.Volume = %s;"
    "$text = [System.IO.File]::ReadAllText(%s, [System.Text.Encoding]::UTF8);"
    "$s.SetOutputToWaveFile(%s);"
    "$s.Speak($text);"
    "$s.Dispose();"
)

# 朗读用的临时文件：固定路径，新的念出来顶掉旧的（winsound 再放一次会自动停掉上一次）
_TMP_WAV = Path(tempfile.gettempdir()) / "desktoppet_voice.wav"
_TMP_TEXT = Path(tempfile.gettempdir()) / "desktoppet_voice.txt"

# 一个 WAV 至少要有 RIFF(4) + size(4) + WAVE(4) + fmt 块，44 字节是极小合法值
_MIN_WAV_BYTES = 44


def _ps_quote(value: Any) -> str:
    """把一串值变成 PowerShell 的单引号字面量。路径/嗓子名可能带空格。"""
    return "'" + str(value).replace("'", "''") + "'"


def tone_for_unlocked(unlocked: list[str] | tuple[str, ...]) -> str:
    """把人设语气层的解锁状态映射成 TTS 语气（B 养成给的 closer_tone / confide）。

    映射是死的、单向的：confide 压 closer 压 default。没解锁任何语气就是 default。
    这个函数不 import life.mind，避免 voice 层反向依赖情感记忆那套。
    """
    if "confide" in (unlocked or ()):
        return "confide"
    if "closer_tone" in (unlocked or ()):
        return "closer"
    return "default"


def _probe_zh_voice() -> str | None:
    """找出系统里第一把中文嗓子。没有就返回 None（语音整条线降级成纯文字）。"""
    script = (
        "Add-Type -AssemblyName System.Speech;"
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
        "$s.GetInstalledVoices() | ForEach-Object {"
        " $_.VoiceInfo.Name + '|' + $_.VoiceInfo.Culture.Name }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
            capture_output=True,
            timeout=15,
        )
    except Exception as exc:  # PowerShell 起不来 / 超时：就当没有语音
        logger.error("语音：枚举系统嗓子失败（%s），这轮只出文字", exc)
        return None
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        if "|" not in line:
            continue
        name, culture = line.split("|", 1)
        if culture.strip().startswith(_ZH_CULTURE_PREFIX):
            return name.strip()
    return None


class VoiceEngine:
    """宠物开口的语音叠加层。文字照常出，语音能出就出、不能出就静默跳过。

    设计上「永远不抛、永远不阻塞主线程」：`say` 只负责起一个后台线程，
    合成 + 播放都在那个线程里；主线程（Qt）该画气泡画气泡，跟语音无关。
    """

    def __init__(self, enabled: bool = True):
        self._enabled = enabled
        self._voice_name: str | None = None
        self._probed = False
        self._lock = threading.Lock()

    # ---------- 对外 ----------

    @property
    def available(self) -> bool:
        """现在有没有本事念出来（启用 + 找到中文嗓子）。只探一次，结果缓存。"""
        if not self._enabled:
            return False
        if not self._probed:
            self._probed = True
            self._voice_name = _probe_zh_voice()
            if not self._voice_name:
                logger.info("语音：系统里没有中文嗓子，整条线降级成纯文字")
        return self._voice_name is not None

    def say(self, text: str, tone: str = "default") -> bool:
        """念一句话（后台线程，异步）。文字不在这儿管，这里只负责声音。

        返回 True 只表示「排上队了」，不保证一定放出声——真放不出来就静默降级。
        """
        if not self._enabled:
            return False
        body = self._speakable(text)
        if not body:
            return False
        threading.Thread(target=self._say_worker, args=(body, tone), daemon=True).start()
        return True

    def synthesize(self, text: str, tone: str = "default", out_path: str | None = None) -> bool:
        """同步合成一句成 WAV（不播放）。主要给测试用，也是 `say` 背后的那一步。

        返回是否真产出了一个像样的 WAV——测试拿它当「真的能说」的行为证据，
        而不是打桩糊弄。
        """
        if not self._enabled:
            return False
        body = self._speakable(text)
        if not body:
            return False
        if self._voice_name is None:
            self._voice_name = _probe_zh_voice()
        if not self._voice_name:
            return False
        out = Path(out_path) if out_path else _TMP_WAV
        return self._synthesize(body, tone, out)

    def shutdown(self) -> None:
        """预留：模型后端将来要释放资源。SAPI 这条腿没有要收拾的东西。"""

    # ---------- 内部 ----------

    @staticmethod
    def _speakable(text: str) -> str:
        """送进合成之前的那一道：剥掉颜文字，只留能读出来的话（v2-speech-polish）。

        收口在这儿而不是在每个说话点：`say` 是全仓唯一的出声口，改这一处就等于
        **所有**说话线（回话 / 主动搭话 / 提醒 / 陪伴）一起生效，将来加新话术也不会漏。
        剥完是空串（这一句本来就只有一只猫的脸）就返回空串，`say` 自己当"没得念"处理——
        UI 那一边照旧把这句显示出来，文字一个字不少。
        """
        from pet.persona import strip_kaomoji

        return strip_kaomoji(str(text or "")).strip()

    def _say_worker(self, text: str, tone: str) -> None:
        try:
            with self._lock:  # 同一时刻只合成一条，新的顶掉旧的（winsound 再放会停掉上一次）
                if self._voice_name is None:
                    self._voice_name = _probe_zh_voice()
                if not self._voice_name:
                    logger.info("语音：没有中文嗓子，这句只出文字")
                    return
                if not self._synthesize(text, tone, _TMP_WAV):
                    return
                winsound.PlaySound(str(_TMP_WAV), winsound.SND_FILENAME | winsound.SND_ASYNC)
        except Exception as exc:  # 播放那一步的任何岔子都不该冒泡到主线程
            logger.error("语音播放没成（%s），这次只出文字", exc)

    def _synthesize(self, text: str, tone: str, out: Path) -> bool:
        profile = TONE_PROFILES.get(tone, TONE_PROFILES["default"])
        _TMP_TEXT.write_text(text, encoding="utf-8")
        script = _PS_SCRIPT % (
            _ps_quote(self._voice_name),
            profile["rate"],
            profile["volume"],
            _ps_quote(str(_TMP_TEXT)),
            _ps_quote(str(out)),
        )
        try:
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", script],
                capture_output=True,
                timeout=60,
            )
        except Exception as exc:
            logger.error("语音合成没成（%s），这句只出文字", exc)
            return False
        if proc.returncode != 0:
            err = proc.stderr.decode("utf-8", "replace").strip()
            logger.warning("语音合成返回非零（%s），这句只出文字", err[:200] or "无 stderr")
            return False
        return out.exists() and out.stat().st_size >= _MIN_WAV_BYTES
