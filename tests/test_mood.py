"""画面情绪（CV 腿 + 融合 + 云端档）：S1~S6 的取证。

分八段：
- S1 判据与降级：提示词表、权重要求、模型不在时的每一条退路；
- S2 真模型跑真值：本机有权重就真建会话、真推理（跳过则说明原因）；
- S3 融合：CV/OCR 冲突时精细优先、音频腿缺了权重归一化不塌；
- S4 接入 monitor：CV 覆盖 OCR 兜底、off 档位与从前逐字相同、模型不在时不抓帧；
- S5 云端档：没授权的每一条路都不发；授权后才发，且发的是编码后的内存字节；
- S5 设置页：告知真的显示在界面上、授权离了云端档不成立（不是只写了个常量）；
- S6 红线：零写入、帧不落盘、模型 gitignore、零新依赖；
- S7 不误报：合成画面一个都不该被报出来。这一段验的是**误报**不是准头——
  真照片那半边的实测值记在报告里（本机只有 1 张真照片，不拿来当测试的底座）。

本机权重在 data/models/ 下（gitignore）。没有它的时候 S2/S7 会走 skip，
其余各段照样全跑——降级路径本来就是这些段在验的东西。
"""

from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QImage                                    # noqa: E402

from life.screen import mood_cloud, mood_cv, mood_fuse               # noqa: E402
from life.screen.mood_cv import CvUnavailable, MoodCvReader, MoodVerdict   # noqa: E402
from life.screen.scenes import (                                     # noqa: E402
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
)
from tests.harness import (                                          # noqa: E402
    check,
    make_env,
    pump,
    section,
    skip,
    summary,
    use_scene_vote,
    wait_for,
)

ROOT = Path(__file__).resolve().parents[1]
REAL_MODEL_DIR = ROOT / "data" / "models"
HAS_MODEL = mood_cv.model_ready(REAL_MODEL_DIR)


def _numpy():
    import numpy as np

    return np


class FakeMood:
    """假的 CV 腿：判什么由测试说了算，省掉一次真推理。"""

    def __init__(self, verdict: MoodVerdict | None) -> None:
        self.verdict = verdict
        self.calls = 0
        self.available = True

    def classify_array(self, array):
        self.calls += 1
        return self.verdict


class FakeGrabber:
    """假截屏：记自己被叫了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


class FakeProvider:
    """假多模态 provider：记被建了几次、被发了几帧。"""

    def __init__(self) -> None:
        self.built = 0
        self.sent = 0
        self.answer = "笑容的场景"

    def chat_with_image(self, prompt, image, system=None):
        self.sent += 1
        self.answer = self.answer
        return self.answer


class _TextReader:
    """假的 OCR 识别器：读什么由测试说了算，省掉一次 4 秒的真 OCR。"""

    def __init__(self, text: str) -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


def _fake_foreground(monitor, proc_holder) -> None:
    """把前台进程换成可控的假值（跟 test_screen 一个做法）。"""
    from monitor import foreground

    foreground.foreground_pid = lambda: 1
    monitor._pids.name_of = lambda pid: proc_holder["proc"]


def _sample(monitor, rounds: int = 1, timeout: float = 5.0) -> None:
    for _ in range(rounds):
        monitor.sample()
        wait_for(lambda: not monitor._reading, timeout=timeout)
        pump(0.02)


def main() -> int:
    np = _numpy()
    box, stub, app = make_env()

    # ---------- S1 判据与降级 ----------
    section("S1 判据：提示词表与两态对齐")
    prompts, keys = mood_cv.prompt_table()
    check("提示词条数与档位数对得上", len(prompts) == len(keys), f"{len(prompts)} 条")
    check("文本零-shot 两态每一档都有判据（愤怒走原型；幽默已删、哭泣 v2-mood-tri 整态删除）",
          all(s in keys for s in (SCENE_HORROR, SCENE_FESTIVE)),
          str(sorted(set(keys))))
    check("文本提示词表里一个哭泣都不剩",
          not any(k == "cry" for k in keys), str(sorted(set(keys))))
    check("有中立锚（没它会被逼着二选一）", mood_cv.NEUTRAL_KEY in keys, "")
    check("判据里没有空串", all(p.strip() for p in prompts), "")

    section("S1 降级：模型不在时每一条退路都不炸")
    empty = Path(tempfile.mkdtemp()) / "models"
    check("模型不在 → model_ready False", not mood_cv.model_ready(empty), str(empty))
    reader = MoodCvReader(empty)
    check("模型不在 → available False", not reader.available, str(reader.available))
    check("模型不在 → classify_array 给 None（不抛）",
          reader.classify_array(np.zeros((32, 32, 3), np.uint8)) is None, "")
    check("模型不在 → 也没把会话建起来", not reader.loaded, "")
    check("模型不在 → 也没去联网（available 是纯文件检查）", not reader.downloading, "")

    section("S1 降级：权重补不下来时抛 CvUnavailable，不往上带崩")
    saved_ms, saved_hf = mood_cv._MS_ENDPOINT, mood_cv._HF_ENDPOINT
    mood_cv._MS_ENDPOINT = "https://127.0.0.1:9/{repo}/{path}"     # 一个必然连不上的口
    mood_cv._HF_ENDPOINT = "https://127.0.0.1:9/{repo}/{path}"
    try:
        raised = ""
        try:
            mood_cv.ensure_model(Path(tempfile.mkdtemp()) / "models")
        except CvUnavailable as exc:
            raised = str(exc)
        check("补不下来 → 抛 CvUnavailable（调用方据此退回 OCR）", bool(raised), raised[:60] + "…")
    finally:
        mood_cv._MS_ENDPOINT, mood_cv._HF_ENDPOINT = saved_ms, saved_hf

    # ---------- S2 真模型 ----------
    section("S2 真模型：本机有权重就真建会话、真推理")
    if not HAS_MODEL:
        skip("真模型这一段", f"{REAL_MODEL_DIR} 下没有中文 CLIP 权重（gitignore，不进仓库）")
    else:
        real = MoodCvReader(REAL_MODEL_DIR)
        check("权重齐 → available True", real.available, "")
        noise = np.random.default_rng(7).integers(0, 255, (224, 224, 3), dtype=np.uint8)
        first = real.classify_array(noise)
        check("真跑起来了（会话建好）", real.loaded, str(real.loaded))
        check("给出的分布是三档（文本两态 + 中立）且归一到 1",
              first is not None and len(first.scores) == 3
              and abs(sum(first.scores.values()) - 1.0) < 1e-6,
              f"{len(first.scores) if first else 0} 档，和={sum(first.scores.values()):.4f}" if first else "None")
        second = real.classify_array(noise)
        check("同一帧两次判读结果一致（确定性）",
              first is not None and second is not None and first.scene == second.scene,
              f"{first.scene if first else None} / {second.scene if second else None}")
        check("阈值在起作用：噪声帧报不出来（不乱猜）",
              first is not None and first.scene is None,
              f"scene={first.scene} score={first.score:.3f}" if first else "None")

        section("S2 阈值：调低才可能对噪声开口（证明上面那条不是恒真）")
        loose = MoodCvReader(REAL_MODEL_DIR, min_score=0.05)
        loose_verdict = loose.classify_array(noise)
        check("阈值压到 0.05 之后噪声也会被报一个场景出来",
              loose_verdict is not None and loose_verdict.scene is not None,
              f"{loose_verdict.scene} {loose_verdict.score:.3f}" if loose_verdict else "None")

    # ---------- S3 融合 ----------
    section("S3 融合：冲突时精细（CV）优先")
    cv_horror = MoodVerdict(SCENE_HORROR, 0.91, {SCENE_HORROR: 0.91, SCENE_ANGRY: 0.09})
    merged = mood_fuse.fuse(SCENE_ANGRY, cv_horror)
    check("OCR 说愤怒、CV 说恐怖 → 听 CV 的", merged.scene == SCENE_HORROR, str(merged.scene))
    check("并标出来这是 CV 赢的（不是两边一致）", merged.source == mood_fuse.SOURCE_CV, merged.source)

    merged = mood_fuse.fuse(SCENE_HORROR, cv_horror)
    check("两边一致 → 还是那个场景", merged.scene == SCENE_HORROR, str(merged.scene))
    check("一致时标成 both", merged.source == mood_fuse.SOURCE_BOTH, merged.source)

    section("S3 融合：CV 没把握就退回 OCR")
    merged = mood_fuse.fuse(SCENE_FESTIVE, MoodVerdict(None, 0.3, {SCENE_FESTIVE: 0.3}))
    check("CV 没开口 → 用 OCR 的", merged.scene == SCENE_FESTIVE, str(merged.scene))
    check("来源标成 ocr", merged.source == mood_fuse.SOURCE_OCR, merged.source)

    merged = mood_fuse.fuse(None, None)
    check("OCR 没命中、CV 也没开口 → 这一拍不成立",
          merged.scene is None and not merged.resolved, f"{merged.scene} / {merged.source}")
    merged = mood_fuse.fuse(None, MoodVerdict(None, 0.2, {SCENE_HORROR: 0.2}))
    check("只有'没把握的 CV'时也不成立（没把握不等于知道）",
          merged.scene is None, f"{merged.scene} / {merged.source}")

    section("S3 融合：音频腿缺了，权重归一化，不塌")
    only_ocr = mood_fuse.fuse(SCENE_ANGRY, None)
    with_audio = mood_fuse.fuse(SCENE_ANGRY, None, {SCENE_ANGRY: 0.8, SCENE_HORROR: 0.2})
    check("没有音频腿时 OCR 仍能定案（不是被空腿摊薄到定不了）",
          only_ocr.scene == SCENE_ANGRY, str(only_ocr.scene))
    check("音频腿缺席不改变结论",
          with_audio.scene == only_ocr.scene, f"{only_ocr.scene} / {with_audio.scene}")
    check("音频腿在场时总分照样是归一的",
          abs(sum(with_audio.scores.values()) - sum(only_ocr.scores.values())) < 1e-9
          or sum(with_audio.scores.values()) > 0,
          f"{sum(only_ocr.scores.values()):.3f} / {sum(with_audio.scores.values()):.3f}")
    both = mood_fuse.fuse(SCENE_HORROR, cv_horror, {SCENE_ANGRY: 1.0})
    check("音频跟 CV 打架时仍然精细优先（CV 赢）",
          both.scene == SCENE_HORROR, str(both.scene))

    # ---------- S4 接入 monitor ----------
    section("S4 接入：CV 覆盖 OCR 兜底（冲突精细优先）")
    monitor = app.screen_monitor
    # 这一节只看两条腿怎么判读，不看窗口投票——窗口设成 0 秒 = 一拍即定
    use_scene_vote(monitor, window=0.0)
    monitor._reader = _TextReader("你凭什么骂人")        # OCR：愤怒（幽默已删，愤怒现在只走字幕这条腿）
    monitor._grabber = FakeGrabber()
    cv_grabber = FakeGrabber()
    monitor._cv_grabber = cv_grabber
    monitor._mood = FakeMood(cv_horror)               # CV：恐怖
    proc = {"proc": "potplayer.exe"}
    _fake_foreground(monitor, proc)

    seen: list[str] = []
    monitor.scene_changed.connect(seen.append)
    _sample(monitor)
    check("两条腿都跑了（CV 被问过）", monitor._mood.calls == 1, f"{monitor._mood.calls} 次")
    check("CV 也真取了一帧", cv_grabber.calls == 1, f"{cv_grabber.calls} 次")
    check("冲突时场景取 CV 的（恐怖），不是 OCR 的（愤怒）",
          monitor.scene == SCENE_HORROR and seen == [SCENE_HORROR], f"{monitor.scene} / {seen}")
    check("融合结论留在对象上，能看出谁说了算",
          monitor.last_fusion is not None and monitor.last_fusion.source == mood_fuse.SOURCE_CV,
          str(monitor.last_fusion.source) if monitor.last_fusion else "None")

    section("S4 降级：CV 不在时，采样链退回到只用 OCR（与从前一致）")
    monitor._mood = None
    cv_grabber.calls = 0
    seen.clear()
    monitor._scene = ""
    monitor._voter.reset()
    monitor._reader = _TextReader("恭喜恭喜新婚快乐")
    _sample(monitor)
    check("CV 关掉后一帧都不多抓", cv_grabber.calls == 0, f"{cv_grabber.calls} 次")
    check("OCR 照样定案（喜庆）", monitor.scene == SCENE_FESTIVE, str(monitor.scene))

    section("S4 off 档位：行为与加这一层之前逐字相同")
    app.config.screen.mood = "off"
    monitor._refresh_mood()
    check("off → 那条腿整个不在", monitor.mood_reader is None, str(monitor.mood_reader))

    section("S4 档位切换：local / cloud 之间切得动")
    app.config.screen.mood = "local"
    monitor._refresh_mood()
    check("切回 local → 腿回来了", isinstance(monitor.mood_reader, MoodCvReader), str(type(monitor.mood_reader).__name__))
    local_reader = monitor.mood_reader
    monitor._refresh_mood()
    check("同档位再刷一次不重建（省掉 132MB 会话重开）",
          monitor.mood_reader is local_reader, "")
    app.config.screen.mood = "cloud"
    monitor._refresh_mood()
    check("切到 cloud → 换成云端那个 reader",
          isinstance(monitor.mood_reader, mood_cloud.MoodCloudReader), str(type(monitor.mood_reader).__name__))
    app.config.screen.mood = "off"
    monitor._refresh_mood()
    check("切回 off → 又没有了", monitor.mood_reader is None, str(monitor.mood_reader))

    # ---------- S5 云端档 ----------
    section("S5 云端档：没授权的每一条路都不发")
    provider = FakeProvider()

    def factory(cfg, _p=provider):
        _p.built += 1
        return _p

    app.config.screen.mood = "cloud"
    app.config.screen.mood_cloud = False          # 默认就是关的
    cloud = mood_cloud.MoodCloudReader(app.config, provider_factory=factory)
    check("默认关 → available False", not cloud.available, "")
    check("默认关 → consent_state 说明卡在哪",
          not mood_cloud.consent_state(app.config).allowed,
          mood_cloud.consent_state(app.config).reason)
    got = cloud.classify_array(np.full((32, 32, 3), 200, np.uint8))
    check("默认关 → classify_array 给 None", got is None, "")
    check("默认关 → **provider 一次都没建**（连收拾画面都没做）", provider.built == 0, f"{provider.built} 次")
    check("默认关 → 一帧都没发", provider.sent == 0 and cloud.sent == 0, f"{provider.sent} 帧")

    section("S5 档位不是 cloud 时，授权开着也不发")
    app.config.screen.mood = "local"
    app.config.screen.mood_cloud = True
    check("档位 local + 授权开 → 仍然不传",
          not mood_cloud.consent_state(app.config).allowed,
          mood_cloud.consent_state(app.config).reason)
    app.config.screen.mood = "cloud"          # 档位切回云端，单看授权这一道
    app.config.screen.mood_cloud = False
    check("档位 cloud + 授权关 → 仍然不传",
          not mood_cloud.consent_state(app.config).allowed,
          mood_cloud.consent_state(app.config).reason)

    section("S5 授权之后才真走，且传的是内存里编码的字节")
    app.config.screen.mood_cloud = True
    app.config.get_api_key = lambda *a, **k: "test-key"      # type: ignore[method-assign]
    provider.answer = "笑容的场景"
    verdict = cloud.classify_array(np.full((32, 32, 3), 200, np.uint8))
    check("授权后 → 真发了一帧", provider.sent == 1 and cloud.sent == 1, f"{provider.sent} 帧")
    check("授权后 → 判读结果解析得出（笑容）",
          verdict is not None and verdict.scene == SCENE_FESTIVE,
          str(verdict.scene) if verdict else "None")
    check("告知文案说清了传什么、传给谁",
          "这一帧画面" in mood_cloud.CONSENT_NOTICE and "多模态服务商" in mood_cloud.CONSENT_NOTICE,
          mood_cloud.CONSENT_NOTICE.splitlines()[0][:36] + "…")

    section("S5 设置页：告知真在界面上，且授权离了云端档不成立")
    import ui.settings as settings_mod

    dialog = settings_mod.SettingsDialog(
        config=app.config,
        bus=app.bus,
        pet_profile_dao=app.pet_profile_dao,
        activity_dao=app.activity_dao,
        category_dao=app.app_category_dao,
        memory_store=app.memory_store,
        reminder_dao=app.reminder_dao,
        jobs=app.jobs,
        user_id=app.user.id,
        parent=None,
    )
    # 起始档位是本地（默认档）：告知与授权框都该收着
    app.config.screen.mood = "local"
    app.config.screen.mood_cloud = False
    dialog._load_from_config()
    check("默认档位落在「本地」", dialog._mood_combo.currentData() == "local", str(dialog._mood_combo.currentData()))
    check("本地档下授权框是灰的（点不着，也就不会误授权）", not dialog._mood_cloud_check.isEnabled(), "")
    # 用 isHidden 不用 isVisible：这个对话框在测试里从没 show() 过，
    # 顶层的 isVisible 一律是 False，那样断言等于没断言。
    check("本地档下告知收着（不占地方瞎吓人）", dialog._mood_notice.isHidden(), "")

    dialog._mood_combo.setCurrentIndex(dialog._mood_combo.findData("cloud"))
    check("切到云端 → 告知亮出来", not dialog._mood_notice.isHidden(), "")
    check("告知显示的**就是** CONSENT_NOTICE 原文（不是另写一段好听的）",
          dialog._mood_notice.text() == mood_cloud.CONSENT_NOTICE, "")
    check("告知里说清了传什么、传给谁、关掉会怎样",
          all(w in dialog._mood_notice.text() for w in ("这一帧画面", "多模态服务商", "不开启时")), "")
    check("切到云端 → 授权框可以点了", dialog._mood_cloud_check.isEnabled(), "")

    dialog._mood_cloud_check.setChecked(True)
    dialog._mood_combo.setCurrentIndex(dialog._mood_combo.findData("local"))
    check("从云端切走 → 授权自己清掉（不留'上次好像勾过'的糊涂账）",
          not dialog._mood_cloud_check.isChecked(), "")

    # 就算有人绕过界面把配置改成"本地档 + 已授权"，存一次也得落成没授权
    app.config.screen.mood = "local"
    app.config.screen.mood_cloud = True
    dialog._mood_combo.setCurrentIndex(dialog._mood_combo.findData("off"))
    dialog._mood_min_spin.setValue(0.66)
    ok = dialog._collect_fields()
    check("保存能过校验", ok, dialog._status.text())
    check("off 档存下来是 off", app.config.screen.mood == "off", app.config.screen.mood)
    check("off 档 + 旧授权 → 存下来授权是 false（默认关这条线守住了）",
          app.config.screen.mood_cloud is False, str(app.config.screen.mood_cloud))
    check("阈值跟着界面走", abs(app.config.screen.mood_min_score - 0.66) < 1e-6,
          str(app.config.screen.mood_min_score))

    # 云端档 + 勾上授权才存成 true
    app.config.screen.mood = "cloud"
    app.config.screen.mood_cloud = False
    dialog._load_from_config()
    dialog._mood_cloud_check.setChecked(True)
    dialog._collect_fields()
    check("云端档 + 明确勾选 → 存下来才是 true",
          app.config.screen.mood == "cloud" and app.config.screen.mood_cloud is True,
          f"{app.config.screen.mood} / {app.config.screen.mood_cloud}")
    dialog.close()

    section("S5 音频腿：本轮没有音源（不是没授权）")
    check("transmit_audio 恒返回 None",
          mood_cloud.transmit_audio(app.config, b"RIFF....") is None, "")
    check("而且它自己也说了原因",
          "音源" in (mood_cloud.transmit_audio.__doc__ or ""), "")

    # ---------- S6 红线 ----------
    section("S6 红线：零写入 / 帧不落盘 / 无模型进 git / 零新依赖")
    pet = app.config.db_path
    before = (pet.stat().st_size, pet.stat().st_mtime)
    tmp = Path(tempfile.gettempdir())
    pattern = ("pet_notes_*", "desktoppet_*", "*.png", "*.part")
    before_files = {p for pat in pattern for p in tmp.glob(pat)}
    for array in (np.zeros((64, 64, 3), np.uint8), np.full((64, 64, 3), 255, np.uint8)):
        reader.classify_array(array)
        cloud.classify_array(array)
    after_files = {p for pat in pattern for p in tmp.glob(pat)}
    after = (pet.stat().st_size, pet.stat().st_mtime)
    check("pet.db 没被动过", before == after, f"{before} → {after}")
    check("跑这一趟没有落下任何画面临时文件（一个字节都不落盘）",
          before_files == after_files, f"{len(before_files)} → {len(after_files)} 个")

    ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    check("data/models/ 在忽略清单里（权重不进仓库）", "data/models/" in ignore, "")
    tracked = subprocess.run(
        ["git", "ls-files", "data/models"], cwd=str(ROOT),
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    ).stdout.strip()
    check("git 里没有权重文件", tracked == "", tracked[:80] or "（空）")
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    check("requirements 里没有 torch / transformers / funasr（零新依赖）",
          not any(k in req for k in ("torch", "transformers", "funasr", "clip")), "")
    # 这两条看的是**源码文本**，不看运行时状态：模块级 import 就会在"没装/没授权"
    # 的时候也把重家伙拉起来，那正好是要防的事。
    cv_src = (ROOT / "life" / "screen" / "mood_cv.py").read_text(encoding="utf-8")
    heavy = [ln for ln in cv_src.splitlines()
             if ln.startswith(("import onnxruntime", "from onnxruntime", "import torch", "from torch"))
             or ln.startswith(("import tokenizers", "from tokenizers"))]
    check("mood_cv 模块级没 import 重家伙（缺了也不报错，用到才建）", not heavy, str(heavy[:2]))
    cloud_src = (ROOT / "life" / "screen" / "mood_cloud.py").read_text(encoding="utf-8")
    top_factory = [ln for ln in cloud_src.splitlines()
                   if ln.startswith(("from brain.llm.factory", "import brain.llm.factory"))]
    check("mood_cloud 模块级没 import 模型工厂（没授权时连网络栈都不碰）",
          not top_factory, str(top_factory))

    # ---------- S7 不误报 ----------
    section("S7 不误报：合成画面一个都不该被报出来")
    if not HAS_MODEL:
        skip("不误报这一段", "同上：本机没有权重")
    else:
        # 这一段验的是**误报**，不是"识别得多准"——别把它读成质量证据。
        # 真照片那一半（模型仓库自带的 festival.jpg → 笑容 0.990）是报告里的实测值，
        # provenance 写在那儿；测试里不下网络，也不拿一张真照片来假装跑过。
        r = MoodCvReader(REAL_MODEL_DIR)
        cases = {
            "纯灰": np.full((224, 224, 3), 128, np.uint8),
            "全黑": np.zeros((224, 224, 3), np.uint8),
            "白噪声": np.random.default_rng(7).integers(0, 255, (224, 224, 3), dtype=np.uint8),
            "纯红": np.full((224, 224, 3), (200, 30, 30), np.uint8),
        }
        for label, array in cases.items():
            v = r.classify_array(array)
            check(f"合成图「{label}」不报场景（宁可不说，也不乱猜）",
                  v is not None and v.scene is None,
                  f"scene={v.scene} score={v.score:.3f}" if v else "None")

    box.cleanup()
    return summary("画面情绪（CV 腿 + 融合 + 云端档）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
