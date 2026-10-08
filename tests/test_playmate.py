"""
游戏陪玩的自动测试。

覆盖：截图编码与缩放 / 本地画面变化比对 / 门控（没变化不发、没到间隔不发） /
静默约定 / 连续失败自动关 / 开关状态广播。
"""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary, wait_for

from PySide6.QtGui import QColor, QImage, QPainter

from brain.chains.proactive import SILENT_TOKEN
import life.playmate as playmate


def frame(color, second=None) -> QImage:
    image = QImage(800, 600, QImage.Format_RGB32)
    image.fill(QColor(*color))
    if second is not None:
        painter = QPainter(image)
        painter.fillRect(0, 0, 400, 600, QColor(*second))
        painter.end()
    return image


class StubChain:
    def __init__(self):
        self.calls = 0
        self.reply = "往左边那个火盆走，能烧掉藤蔓。"
        self.boom = False

    def advise(self, app_name: str, image: bytes) -> str:
        self.calls += 1
        if self.boom:
            raise RuntimeError("网络炸了")
        return self.reply


def main() -> int:
    box, stub, app = make_env()
    config = app.config
    service = app.playmate_service

    section("配置里的默认值")
    from core.config import VisionSection

    # 这一节问的是**仓库出厂默认**，不是"这台机器的 config.yaml 现在写的是什么"。
    # 沙箱是以用户真实 config.yaml 为底稿的（见 harness.Sandbox），所以
    # `Config.load(box.config_path)` 会把本机偏好一起读进来——本机把 vision.sample_sec
    # 改成了 3，那条"默认 5 秒"就一直假红。直接问 dataclass 的字段默认值，跟本机无关。
    defaults = VisionSection()
    check("默认用的是能看图的模型", "vl" in defaults.model or "v" in defaults.model, defaults.model)
    check("默认借通义那家的接口", defaults.provider in ("tongyi", "dashscope"), defaults.provider)
    check("默认把图缩到 1024 宽", defaults.max_width == 1024, str(defaults.max_width))

    section("截图编码与缩放")
    image = QImage(1600, 900, QImage.Format_RGB32)
    image.fill(QColor(30, 60, 120))
    data = playmate.encode_jpeg(config, image)
    decoded = QImage()
    ok = decoded.loadFromData(data, "JPG")
    check("产出可解码的 JPEG", bool(data) and ok, f"{len(data)} 字节")
    check("按 max_width 缩过", decoded.width() <= config.vision.max_width, f"宽 {decoded.width()}")

    sign = playmate.screen_signature(image)
    check(
        "比对用的缩略图尺寸固定",
        sign.shape == (playmate.SIGN_HEIGHT, playmate.SIGN_WIDTH),
        str(sign.shape),
    )
    same = float(abs(playmate.screen_signature(image) - sign).mean())
    changed = float(abs(playmate.screen_signature(frame((250, 250, 250))) - sign).mean())
    check("同一画面差异为 0", same == 0.0, str(same))
    check("明显不同的画面差异超阈值", changed >= config.vision.change_threshold, f"{changed:.1f}")

    real = playmate.grab_screen_jpeg(config)
    check("真实抓屏能出图", bool(real), f"{len(real)} 字节" if real else "空")

    section("服务门控")
    # 把间隔调大：测试是手动"拍"的，别让真实的定时器在旁边插一脚
    config.vision.sample_sec = 600
    chain = StubChain()
    service._chain = chain
    frames: list[QImage] = []
    playmate.grab_screen = lambda pet_rect=None: frames.pop(0) if frames else None

    spoken: list[str] = []
    states: list[bool] = []
    # 陪玩提示是宠物自己开的口 → 主动线（v2-speech-polish）
    app.bus.pet_speak_proactive.connect(spoken.append)
    app.bus.playmate_changed.connect(states.append)

    service.start()
    check("开启后处于陪玩中", service.is_active)
    check("截屏间隔按配置来", service._timer.interval() == config.vision.sample_sec * 1000,
          str(service._timer.interval()))
    check("开启会广播状态", states == [True], str(states))

    frames.append(frame((0, 0, 0), (255, 255, 255)))
    service._tick()
    wait_for(lambda: chain.calls == 1 and not service._busy)
    check("刚开就先看一眼当前画面", chain.calls == 1, str(chain.calls))
    check("提示通过主动线说出来（自己开口，先冒话后问要不要念）",
          spoken == [chain.reply], str(spoken))

    frames.append(frame((0, 0, 0), (255, 255, 255)))
    service._tick()
    check("画面没变就不再分析", chain.calls == 1, str(chain.calls))

    frames.append(frame((200, 10, 10)))
    service._tick()
    check("画面变了但没到间隔，先不说", chain.calls == 1, str(chain.calls))

    service._last_advice = time.monotonic() - 100
    frames.append(frame((10, 200, 10)))
    service._tick()
    wait_for(lambda: chain.calls == 2 and not service._busy)
    check("隔够了才说第二次", chain.calls == 2, str(chain.calls))

    chain.reply = SILENT_TOKEN
    service._last_advice = time.monotonic() - 100
    frames.append(frame((250, 250, 0)))
    before = len(spoken)
    service._tick()
    wait_for(lambda: not service._busy)
    check("模型回 [SILENT] 时保持安静", len(spoken) == before, str(spoken))

    section("截图取不到时不崩")
    calls_before = chain.calls
    frames.clear()          # 假 grab_screen 会返回 None
    service._tick()
    check("取不到画面就跳过这一拍", service.is_active and chain.calls == calls_before, str(chain.calls))

    section("连续失败自动关")
    chain.boom = True
    spoken.clear()
    logging.getLogger().setLevel(logging.CRITICAL)   # 这段是故意让它失败的，别刷堆栈
    for index in range(playmate.MAX_FAILURES):
        service._last_advice = time.monotonic() - 100
        tone = 255 if index % 2 else 0
        frames.append(frame((tone, tone, tone)))
        service._tick()
        wait_for(lambda: not service._busy)
    check(f"连续失败 {playmate.MAX_FAILURES} 次 → 自动关闭", not service.is_active)
    check("关闭时告诉用户出了什么事", any("陪玩先关掉" in text for text in spoken), str(spoken))
    check("关闭会广播状态", states[-1] is False, str(states))

    service.stop()
    check("已经关了再停一次不报错、也不重复广播", states.count(False) == 1, str(states))

    drain_threads()
    box.cleanup()
    return summary("游戏陪玩")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
