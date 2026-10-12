"""
测试脚手架：离屏跑、临时数据、桩模型、断言与汇总。

三条原则：
1. 绝不碰用户真实数据——配置、数据库、向量库、日志、番茄钟存档全部指到临时目录；
2. 不联网——模型换成桩（真调模型的用例单独放在 test_live_llm.py，需要显式开启）；
3. 只构造应用、不调用 start()——所有定时器都不启动，测试里手动驱动，结果才可重复。
"""

from __future__ import annotations

import contextlib
import gc
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Callable

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

# 测试输出统一走 UTF-8：Windows 控制台默认 GBK，工具 / 分类里的 emoji（🏃☀️🌙）一打印就
# UnicodeEncodeError，直接把整个模块崩掉（run_all 里表现为"失败"，其实是编码崩，不是断言没过）。
# 这里把 stdout/stderr 重配成 UTF-8，打印不再挑代码页，run_all 才稳定可复现。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import yaml
from PySide6.QtCore import QThreadPool
from PySide6.QtWidgets import QApplication

from core import config as config_mod

# ---------- 断言与汇总 ----------

_RESULTS: list[tuple[str, str, bool, str]] = []
_SECTION = ""


def section(title: str) -> None:
    global _SECTION
    _SECTION = title
    print(f"\n【{title}】")


def check(name: str, ok: bool, detail: str = "") -> bool:
    _RESULTS.append((_SECTION, name, bool(ok), detail))
    mark = " OK " if ok else "FAIL"
    line = f"  [{mark}] {name}"
    if detail:
        line += f"   -> {detail}"
    print(line)
    return bool(ok)


def skip(name: str, reason: str) -> None:
    print(f"  [跳过] {name}   -> {reason}")


def note(text: str) -> None:
    """只报告不判定的行：用于"看一眼结果"的采样，不计入通过/失败。"""
    print(f"  [  ··  ] {text}")


def failures() -> list[tuple[str, str, str]]:
    return [(sec, name, detail) for sec, name, ok, detail in _RESULTS if not ok]


def summary(title: str = "测试结果") -> int:
    total = len(_RESULTS)
    bad = failures()
    print("\n" + "=" * 64)
    print(f"{title}：共 {total} 项，通过 {total - len(bad)}，失败 {len(bad)}")
    if bad:
        print("-" * 64)
        for sec, name, detail in bad:
            print(f"  失败 · {sec} · {name}" + (f"   {detail}" if detail else ""))
    print("=" * 64)
    return len(bad)


# ---------- Qt ----------


def ensure_app() -> QApplication:
    """整个进程只用一个 QApplication。"""
    return QApplication.instance() or QApplication([])


def pump(seconds: float = 0.05) -> None:
    """转一会儿事件循环，让排队的主线程回调（后台任务的结果）有机会派发。"""
    app = QApplication.instance()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if app is not None:
            app.processEvents()
        time.sleep(0.005)


def wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> bool:
    """等条件成立，期间一直转事件循环；超时返回最后一次的结果。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        pump(0.02)
    return cond()


def drain_threads() -> None:
    QThreadPool.globalInstance().waitForDone(3000)


def settle_notes(notes, timeout: float = 5.0) -> None:
    """等抓屏笔记这一拍**整个走完**：后台认字 + （够一轮时）那趟提炼都回来。

    v2-focus-mode-freeze-fix 14-A 起 `ScreenNotesService._capture` 的认字在线程池里
    （修前是同步压在界面线程上），所以"抓一拍"不再能在 `tick()` 返回那一刻就断言完成：
    结果要等后台那一趟回主线程派发。`_capturing` 是那一趟的在飞标志，**在飞期间下一拍
    会被闸挡掉**——不等它就往下走，十拍里会有几拍被挡，断言"抓了十次"就成了假红。

    开头那一下 `pump` 是给 14-B 的：进专注即拍改成了 `QTimer.singleShot(0, snap_now)`，
    排在本拍事件循环之后。不先把这拍放出来，这一趟会在"拍还没抓"的时候就返回。
    """
    pump(0.02)                                  # 放行排队中的 0 间隔定时器（进专注即拍）
    wait_for(lambda: not notes._capturing, timeout=timeout)
    drain_threads()
    wait_for(lambda: not notes._running, timeout=timeout)
    pump(0.05)


def varied_image(seed: int):
    """由 seed 决定的一张 8×8 格块图，给抓屏笔记的假取景器用。

    **不能拿"每拍回同一张图"或"只有七八种变化"的假图**：抓屏笔记有均值哈希去重
    （`life/screen/notes._ahash`，汉明距离 <=6 算同一屏），第八拍往后就必然撞上
    `RECENT_HASHES=8` 那个窗口里已有的指纹，被当成重复帧挡掉——测试会莫名其妙地
    "抓了十拍只攒下四份"。这里按 64 位线性同余铺满整张图：任意两个 seed 的指纹
    平均差 32 位，远越过阈值 6，想抓多少拍就有多少"明显不同的一屏"。
    """
    from PySide6.QtGui import QColor, QImage, QPainter

    image = QImage(64, 64, QImage.Format_RGB32)
    image.fill(QColor(0, 0, 0))
    painter = QPainter(image)
    bits = ((seed * 0x9E3779B1) & 0xFFFFFFFF)
    bits |= bits << 32                      # 32 位铺成 64 位，正好一格一位
    for index in range(64):
        if (bits >> index) & 1:
            row, col = divmod(index, 8)
            painter.fillRect(col * 8, row * 8, 8, 8, QColor(255, 255, 255))
    painter.end()
    return image


# ---------- 场景多数票的假时钟 ----------


class SceneClock:
    """给"一分钟多数票"用的可控时钟（v2-mood-detect 再审·终）。

    `SceneVoter` 认一个 `now()` 回调。测试里把它换成这个，就能在一瞬间把"60 秒窗口"
    走完——否则每个用到场景判定的用例都得真等一分钟，整套件就跑不动了。
    """

    def __init__(self, start: float = 0.0) -> None:
        self.t = float(start)

    def __call__(self) -> float:
        return self.t

    def advance(self, sec: float = 5.0) -> float:
        self.t += float(sec)
        return self.t


def use_scene_vote(monitor, window: float = 10.0, start: float = 0.0,
                   gates: bool = False) -> SceneClock:
    """把采样器的场景判定换成"可控时钟的多数票窗口"，返回那个假时钟。

    窗口宽度**同时写进配置**：`ScreenMonitor._count_scene` 每拍从配置现读窗口宽度，
    只换 `_voter` 不改配置的话，(配置 60, 投票 10) 就对不上号了。

    用法：每拍 `monitor.sample()`（或 `_on_analysis(...)`）之前 `clock.advance(5)` 一下，
    就当"这一拍又往前走了 5 秒"。调用方自己决定窗口多宽——默认 10 秒 / 每拍 5 秒，
    两拍填满，正好复现旧用例里"第二拍成立"的节奏。

    `gates`（v2-mood-window）**默认关掉**，这里照实说明为什么：生产链路上那道
    "首弹门槛（≥8 票且 ≥60%）+ 切换去抖（≥10 秒）"是按**真机的 60 秒窗口**定的，
    而这里的用例是拿 5~10 秒的合成窗口复现旧节奏、验的又是**别的功能**（开场字幕、
    场景互斥、硬闸、台词时长……）——那种窗口里根本装不下 8 拍，闸一开这些用例
    全部恒不定案，验的东西就废了。**闸本身**的验证集中在 `test_mood_window.py`，
    那里走的是**生产默认参数**（这个参数一律不传），一条都不放水。
    """
    from life.screen.vote import SceneVoter

    monitor._config.screen.scene_vote_window_sec = float(window)
    clock = SceneClock(start)
    if gates:
        monitor._voter = SceneVoter(window, now=clock)
    else:
        monitor._voter = SceneVoter(window, now=clock, first_min_votes=0,
                                    first_min_share=0.0, switch_hold_sec=0.0)
    return clock


# ---------- 临时环境 ----------

# 最近建的那个沙箱：build_app() 造出来的应用都挂到它身上，cleanup() 才知道该关谁。
# 只记"最近一个"就够——建沙箱和建应用总是成对的（make_env 里就是一前一后）。
_LAST_SANDBOX: "Sandbox | None" = None


class Sandbox:
    """一次测试用的隔离环境：配置、数据库、向量库、日志全在临时目录里。"""

    def __init__(self, name: str = "pet_test_"):
        global _LAST_SANDBOX
        self.dir = Path(tempfile.mkdtemp(prefix=name))
        self.apps: list[Any] = []       # build_app() 的产物都挂上来，cleanup() 逐个关
        _LAST_SANDBOX = self
        raw = yaml.safe_load((ROOT / "config.yaml").read_text(encoding="utf-8")) or {}
        raw.setdefault("app", {})
        raw["app"]["log_dir"] = str(self.dir / "logs")
        raw["app"]["log_level"] = "ERROR"
        # v2-data-root：测试期间数据根退回项目内 data/（fallback）。不钉的话，本机
        # config.yaml 里的 data_dir=E:\电子宠物 会渗进来——启动迁移会去碰用户真实的
        # E 盘，连带着 model_path（下面对它故意不覆盖）也解析到 E 盘那个还没迁过去的目录。
        raw["app"]["data_dir"] = ""
        raw.setdefault("memory", {})
        raw["memory"]["db_path"] = str(self.dir / "pet.db")
        raw["memory"]["vector_path"] = str(self.dir / "chroma")
        # memory.model_path 不动：相对路径会解析到项目里的真实向量模型目录
        # 情感记忆库（S1）同理：库文件进临时目录，密钥走环境变量。
        # 密钥**必须**在这儿钉死——不然 resolve_key 会去写用户真实的 Windows 凭据管理器，
        # 跑一次测试就在人家机器上留一把钥匙，还可能把用户自己配的那把覆盖掉。
        raw.setdefault("mind", {})
        raw["mind"]["db_path"] = str(self.dir / "mind.db")
        # 本地小模型（A++）：探测目录也必须进沙箱——不然用户往真实 data/models/
        # 丢了个 qwen gguf 之后，再跑测试就会真去加载那个模型（几秒 + 几百 MB），
        # 还会把"回落 API"这条线全带歪。默认空目录 = 本机没模型 = 走桩模型。
        raw["mind"]["local_model_dir"] = str(self.dir / "models")
        # 语音（C 多模态）：测试里整条关掉——不然每个说话点都会真去 spawn PowerShell
        # 合成、真出声音，既拖慢全套件，又把临时 WAV 写得到处都是。语音那条线自己
        # 有专门的 test_voice.py（直接调 VoiceEngine，不经过这个沙箱）单独验。
        raw.setdefault("voice", {})
        raw["voice"]["enabled"] = False
        # 语音输入（C 多模态 · 听说闭环）：ASR 权重目录进沙箱（同 local_model_dir 的
        # 道理——用户往真实 data/models/ 丢了 whisper 权重之后，测试不该去加载它），
        # 全局快捷键在测试里留空：不然每跑一次全套件就往用户的桌面上抢一次
        # Ctrl+Alt+Space。听这条线自己由 test_asr.py 关起门来验。
        raw["voice"]["asr_enabled"] = False
        raw["voice"]["asr_local_model_dir"] = str(self.dir / "models")
        raw["voice"]["asr_hotkey"] = ""
        # 对象视角提炼的总闸（v2-social-card-autodistill）：跟下面 notes.enabled 是同一条
        # 道理——本文件拿用户真实 config.yaml 当底稿，用户哪天在设置里把它关成 off，凡是
        # 走社交那条线的测试就都不再走提炼那一段（或者反过来：他开着、测试以为关着），
        # 红的会是本机偏好而不是代码。这里钉成**代码默认值**（`ScreenSection.card_autodistill`
        # 的初值 auto），要测 off 那条路的模块自己往 `app.config.screen` 上改——改的是
        # 内存对象，不碰文件（`test_social_card_inject.py` 里改 `card_memory` 就是这么做的）。
        raw.setdefault("screen", {})
        raw["screen"]["card_autodistill"] = "auto"
        # 抓屏笔记（C 多模态）：落点必须进临时沙箱——不然测试会往用户真实的
        # data/notes/ 里写东西（红线）。默认关着（NotesSection 默认），要测的模块自己开。
        #
        # **总闸在这里钉死成代码默认值（关）**，不跟着本机 config.yaml 走：本文件是以
        # 用户真实 config.yaml 为底稿的，用户哪天在设置里把抓屏笔记打开（v2-focus-notes2
        # 就是这么做的），一开就会渗进**所有**用到专注态的测试——它们跑着跑着就去调
        # 桩模型提炼笔记，把"这一轮不该喊模型"这类断言弄红。测试要跟本机配置无关。
        raw.setdefault("notes", {})
        raw["notes"]["enabled"] = False
        raw["notes"]["dir"] = str(self.dir / "notes")
        # 知识库目录（v2-kb-init）：落点必须进临时沙箱——不然测试会去扫用户真实的
        # ../知识库 目录（红线）。跟 notes 一样，要测的模块自己往沙箱目录里塞文档。
        raw.setdefault("knowledge", {})
        raw["knowledge"]["dir"] = str(self.dir / "knowledge")
        # **默认落点也要钉**（v2-kb-init 的坏路径兜底）：`knowledge_dir` 在配置的目录
        # 建不出来时会退到 `KNOWLEDGE_DIR_DEFAULT`，而那个常量指向**用户真实的
        # ../知识库**。不钉的话，凡是走到兜底那一步的测试都会去扫/建用户的真文件夹
        # （红线）。它是个模块级常量、属性每次都重新读，所以换掉模块属性就够。
        self._prev_kb_default = config_mod.KNOWLEDGE_DIR_DEFAULT
        config_mod.KNOWLEDGE_DIR_DEFAULT = str(self.dir / "knowledge_default")
        # 聊天范例目录（v2-social-fewshot）：跟知识库同一个道理——默认落点指向用户真实的
        # ../聊天范例，那儿是用户自己放的聊天片段。不钉的话，凡是走社交起草那条线的测试
        # 都会去扫/读它（红线），提示词里还会混进用户真片段、把断言带歪。属性每次都重新
        # 读这个模块级常量，所以换掉常量就够；要测带范例的用例，自己往这个沙箱目录里塞。
        self._prev_examples_default = config_mod.SOCIAL_EXAMPLES_DIR_DEFAULT
        config_mod.SOCIAL_EXAMPLES_DIR_DEFAULT = str(self.dir / "chat_examples")
        # 工作模块的两个纯本地偏好（v2-sandbox-fix）：身份 role 与点评开关 pet_comment。
        # 跟 notes.enabled 是同一条道理——它们是**用户自己在本机切过/开过的偏好**，
        # 本文件拿用户真实 config.yaml 当底稿，用户把 role 切成"员工"（调试用那个切换）
        # 或把点评打开，config 里就是 user / true，于是别处"默认是管理员视角""点评默认关"
        # 那几条断言当场变红——红的是本机偏好，
        # 不是代码。这里钉成**代码默认值**（core/config.py 里 WorkConfig 的初值），
        # 让这两个键跟本机配置彻底脱钩；要测非默认值的模块自己往 app.config.work 上改
        # （要测非默认值的，改完调 _on_settings_saved() 生效）——改的是内存对象，不碰文件。
        raw.setdefault("work", {})
        raw["work"]["role"] = config_mod.WORK_ROLE_ADMIN
        raw["work"]["pet_comment"] = False
        self._prev_mind_key = os.environ.get("PET_MIND_KEY")
        os.environ["PET_MIND_KEY"] = "test-mind-key-not-a-real-secret"
        # 番茄钟状态存档（focus/pomodoro.STATE_FILE）默认落在项目里的
        # data/pomodoro_state.json——那是用户正在养的那只宠物的真实状态：测试里
        # start_focus()/reset()/shutdown() 随手动一下就把人家"进行到一半的番茄"覆盖了。
        # 它是模块级常量、构造 PomodoroTimer 时读，所以得在构造应用之前换掉。
        from focus import pomodoro as pomodoro_mod

        self._prev_state_file = pomodoro_mod.STATE_FILE
        pomodoro_mod.STATE_FILE = str(self.dir / "pomodoro_state.json")
        # 抓屏笔记的问询存档（life/screen/notes.ASK_STATE_FILE，v2-notes-cadence）同理：
        # 默认落在项目里的 data/notes_state.json，那是用户真机上"这一周问过了没有"的
        # 真实记录——测试里问一次就把人家的一周节奏改掉了。同样是模块级常量，构造前换掉。
        from life.screen import notes as notes_mod

        self._prev_ask_file = notes_mod.ASK_STATE_FILE
        notes_mod.ASK_STATE_FILE = str(self.dir / "notes_state.json")
        # 卡窗对话（life.cards.chat.CHAT_DIR，v2-card-tools-A）同理：默认落在项目里的
        # data/card_chat/，那是用户真机上每张卡聊过的话。测试里只要打开一张卡、说一句，
        # 就会往人家真实的卡窗记录里写（红线）——同样是模块级常量，构造应用之前换掉。
        from life.cards import chat as card_chat_mod

        self._prev_card_chat_dir = card_chat_mod.CHAT_DIR
        card_chat_mod.CHAT_DIR = str(self.dir / "card_chat")
        # 卡上小部件（life.cards.tools.TOOLS_DIR，v2-card-tools-B）同理：默认落在项目里的
        # data/card_tools/，那是用户真机上各张卡摆着的部件。测试里生成一次就会往人家真实
        # 的卡上添一个（红线）——同样在构造应用之前换成临时目录。
        from life.cards import tools as card_tools_mod

        self._prev_card_tools_dir = card_tools_mod.TOOLS_DIR
        card_tools_mod.TOOLS_DIR = str(self.dir / "card_tools")
        # 卡面摆成什么样（life.cards.layout.LAYOUT_DIR，v2-card-tools-C）同理：默认落在
        # 项目里的 data/card_layout/，那是用户真机上各张卡拖成的样子。测试里只要拖一下
        # 就会改到人家真实的卡面（红线）——同样在构造应用之前换掉。
        from life.cards import layout as card_layout_mod

        self._prev_card_layout_dir = card_layout_mod.LAYOUT_DIR
        card_layout_mod.LAYOUT_DIR = str(self.dir / "card_layout")
        self.config_path = self.dir / "config.yaml"
        self.config_path.write_text(
            yaml.safe_dump(raw, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        self._patch_config_load()

    def _patch_config_load(self) -> None:
        """Config.load 的默认参数在函数定义时就绑定了，改模块常量没用，得换掉整个方法。"""
        real = config_mod.Config.load.__func__
        path = self.config_path

        def fake_load(cls, p: Any = None):
            return real(cls, Path(p) if p else path)

        config_mod.Config.load = classmethod(fake_load)

    @property
    def state_file(self) -> Path:
        return self.dir / "pomodoro_state.json"

    def cleanup(self) -> None:
        if self._prev_mind_key is None:
            os.environ.pop("PET_MIND_KEY", None)
        else:
            os.environ["PET_MIND_KEY"] = self._prev_mind_key
        from focus import pomodoro as pomodoro_mod
        from life.cards import chat as card_chat_mod
        from life.cards import layout as card_layout_mod
        from life.cards import tools as card_tools_mod
        from life.screen import notes as notes_mod

        pomodoro_mod.STATE_FILE = self._prev_state_file
        notes_mod.ASK_STATE_FILE = self._prev_ask_file
        card_chat_mod.CHAT_DIR = self._prev_card_chat_dir
        card_tools_mod.TOOLS_DIR = self._prev_card_tools_dir
        card_layout_mod.LAYOUT_DIR = self._prev_card_layout_dir
        config_mod.KNOWLEDGE_DIR_DEFAULT = self._prev_kb_default
        config_mod.SOCIAL_EXAMPLES_DIR_DEFAULT = self._prev_examples_default
        self._release_handles()
        for _ in range(5):
            if not self.dir.exists():
                break
            shutil.rmtree(self.dir, ignore_errors=True)
            if self.dir.exists():
                time.sleep(0.2)

    def _release_handles(self) -> None:
        """把还攥着沙箱文件不放的句柄松开，不然 Windows 上 rmtree 一个都删不掉。

        实测（这次清点：%TEMP% 里躺着 11751 个 pet_test_*、合计 4.7 GB）：光靠
        ``rmtree(ignore_errors=True)``，沙箱目录会**整个**留下来——
        ``pet.db`` 被 SQLAlchemy 引擎的连接池攥着、``chroma.sqlite3`` 被 chroma 的
        system 攥着、``pet.log`` 被 logging 的文件句柄攥着；``ignore_errors=True``
        把这些全吞了，于是每跑一次就多留一个目录，谁也没发现。

        所以删之前按"谁开得晚谁先放手"的顺序松：应用自己的 ``shutdown()``（它关
        mind.db、停各服务）→ 两个 chroma 客户端的 ``close()``（``chroma.sqlite3``
        的锁在这儿，只清 system 缓存不管用）→ SQLAlchemy 的 ``engine.dispose()``
        → logging 的文件句柄。每一步都包在 ``contextlib.suppress`` 里：清理是收尾
        动作，哪一步失败都不该把测试判成失败。用 suppress 而不是 ``except: pass``，
        是为了让"这儿是故意不报错"一眼看得出来，不至于跟真吞异常混在一起。
        """
        # 后台线程可能还攥着一条从池里借出去的连接（engine.dispose() 只回收"已归还"的），
        # 不等它跑完就关，pet.db 还是删不掉——test_card_chat 里就有两处这样漏下来的。
        with contextlib.suppress(Exception):
            drain_threads()
        # 一个沙箱可能有不止一个应用：模拟"重启"的用例会再装一个（test_intent 两处、
        # test_carousel、test_card_offer 各一处），**每个都得关**，漏一个目录就删不掉。
        for app in self.apps:
            with contextlib.suppress(Exception):
                app.shutdown()
            # vector_store / mind_index 各自攥着一个 chroma client，共用一个库目录
            for attr in ("vector_store", "mind_index"):
                client = getattr(getattr(app, attr, None), "_client", None)
                if client is None:
                    continue
                with contextlib.suppress(Exception):
                    client.close()
            engine = getattr(getattr(app, "db", None), "engine", None)
            if engine is not None:
                with contextlib.suppress(Exception):
                    engine.dispose()
        with contextlib.suppress(Exception):
            from chromadb.api.client import SharedSystemClient

            SharedSystemClient.clear_system_cache()
        for handler in logging.root.handlers[:]:
            if not isinstance(handler, logging.FileHandler):
                continue                      # 屏幕那一路留着，只关写文件的
            with contextlib.suppress(Exception):
                handler.close()
            logging.root.removeHandler(handler)
        gc.collect()
        self.apps.clear()


# ---------- 桩模型 ----------


class StubLLM:
    """可控的假模型：测试里改这几个属性，就等于决定"模型怎么回"。"""

    def __init__(self) -> None:
        self.parse_result: dict[str, Any] = {}          # 吐嘈解析回什么 JSON
        self.classify_result: dict[str, Any] = {"category": "", "confidence": 0.0}   # 品类判定回什么
        self.intent_result: dict[str, Any] = {"kind": "none", "theme": "", "confidence": 0.0, "urgent": False}   # 意图兜底回什么
        self.followup: dict[str, str] = {"intent": "unclear", "time_iso": ""}
        self.memories: list[dict[str, Any]] = []
        self.mind_result: dict[str, Any] = {}            # 情感记忆提炼回什么 JSON
        # 对象视角提炼（v2-social-card-autodistill）：对方档案回什么 JSON
        self.peer_result: dict[str, Any] = {}
        self.notes_result: dict[str, Any] = {}           # 抓屏笔记提炼回什么 JSON
        self.tools_result: dict[str, Any] = {}           # 卡内工具生成（v2-card-tools-B）回什么 JSON
        self.candidates_result: dict[str, Any] = {}      # 社交候选回复（v2-social-reply-ext）回什么 JSON
        self.profile_result: dict[str, Any] = {}         # 抽对方信息（v2-social-reply-ext）回什么 JSON
        self.skill_result: dict[str, Any] = {}          # 卡内技能登记（v2-life-skill）回什么 JSON
        self.skill_match_result: dict[str, Any] = {}    # 卡内技能匹配（v2-life-skill）回什么 JSON
        self.text = "好呀"                               # chat() / chat_messages() 的回话
        self.image_text = "（看图回复）"                  # chat_with_image() 的回话
        self.prompts: list[str] = []                    # 收到的提示词，供断言
        self.messages: list[list[dict[str, str]]] = []   # chat_messages() 收到的多轮消息
        # chat_messages() 每次收到的生成参数（v2-chat-mirror 要验"字数预算跟着对方走"）
        self.params: list[dict[str, Any]] = []
        self.online_text = "{}"                          # 联网检索回来的正文（工作卡片用）
        self.online_sources = 0                          # 联网检索回来几条来源
        self.search_queries: list[str] = []             # ask() 收到的消歧搜索词（v2-search-tune）

    def route_json(self, prompt: str) -> dict[str, Any]:
        self.prompts.append(prompt)
        # 对象视角提炼（v2-social-card-autodistill）：判据是那段提示词里**独有**的字面
        # `"anxiety"`（对方档案那几格之一）。**摆在最前面**：它跟下面每一条分流标记都不
        # 撞（不带 `"profile"` / `"rel_state"` / `"intent"` / `"key_points"`…），摆头一条
        # 谁也别想误伤它——这张表条数越来越多，新标记挤进来时最先命中的那个说了算。
        if '"anxiety"' in prompt:
            return dict(self.peer_result)
        if "time_confidence" in prompt:          # 吐嘈解析
            return dict(self.parse_result)
        if '"rel_state"' in prompt:              # 情感记忆提炼（S3）
            return dict(self.mind_result)
        if '"key_points"' in prompt:             # 抓屏笔记提炼（C 多模态）
            return dict(self.notes_result)
        if '"components"' in prompt:            # 卡内工具生成（v2-card-tools-B）
            return dict(self.tools_result)
        if '"candidates"' in prompt:            # 社交候选回复（v2-social-reply-ext）
            return dict(self.candidates_result)
        if '"profile"' in prompt:               # 抽对方信息（v2-social-reply-ext）
            return dict(self.profile_result)
        if '"skill_name"' in prompt:            # 卡内技能登记（v2-life-skill）
            return dict(self.skill_result)
        if '"matched"' in prompt:               # 卡内技能匹配（v2-life-skill）
            return dict(self.skill_match_result)
        # 主动服务的意图兜底（指令 26-B）：它和记忆抽取都带 "kind"，
        # 靠只有它才有的 "theme" 区分开，别把记忆抽取那条劫走了
        if '"kind"' in prompt and '"theme"' in prompt:
            return dict(self.intent_result)
        if '"confidence"' in prompt:             # 品类判定（词表没命中时）
            return dict(self.classify_result)
        if '"intent"' in prompt:                 # 等待态意图分类
            return dict(self.followup)
        if "memories" in prompt:                 # 长期记忆抽取
            return {"memories": [dict(item) for item in self.memories]}
        return {}


class StubProvider:
    """把桩模型包装成 LLMProvider 接口。"""

    name = "stub"
    model_name = "stub"

    def __init__(self, stub: StubLLM):
        self._stub = stub

    def chat(self, prompt: str, system: str | None = None) -> str:
        self._stub.prompts.append(prompt)
        return self._stub.text

    def chat_json(self, prompt: str, system: str | None = None) -> dict[str, Any]:
        return self._stub.route_json(prompt)

    def chat_with_image(self, prompt: str, image: bytes, system: str | None = None) -> str:
        self._stub.prompts.append(prompt)
        return self._stub.image_text

    def chat_messages(
        self,
        messages: list[dict[str, str]],
        temperature: float | None = None,
        top_p: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        self._stub.messages.append(messages)
        self._stub.params.append(
            {"temperature": temperature, "top_p": top_p, "max_tokens": max_tokens}
        )
        return self._stub.text


def install_stub_llm() -> StubLLM:
    """把模型工厂换成桩（含联网检索那条通道），之后所有链都不会联网。"""
    from brain.llm.factory import LLMFactory

    stub = StubLLM()
    provider = StubProvider(stub)
    LLMFactory.create = staticmethod(lambda config, provider_name=None: provider)  # type: ignore[assignment]
    LLMFactory.create_vision = staticmethod(lambda config: provider)  # type: ignore[assignment]
    LLMFactory.create_search = staticmethod(lambda config: StubSearch(stub))  # type: ignore[assignment]
    return stub


class StubSearch:
    """可控的假联网检索：正文来自 stub.online_text，来源按 stub.online_sources 生成。

    真来源是厂商返回的 search_info（title/url/site_name），这里用 example.com 顶上，
    断言时只看"界面拿到的链接是不是这一份"——绝不看模型自己写的链接。
    """

    def __init__(self, stub: StubLLM):
        self._stub = stub
        self.name = "stub-search"
        self.model_name = "stub-search"

    def ask(self, prompt: str, system: str = "", search_query: str = ""):
        from brain.llm.search import SearchAnswer, SearchSource

        self._stub.prompts.append(prompt)
        self._stub.search_queries.append(search_query)
        sources = tuple(
            SearchSource(
                index=index,
                title=f"示例来源 {index}",
                url=f"https://example.com/source-{index}",
                site="示例站",
            )
            for index in range(1, self._stub.online_sources + 1)
        )
        return SearchAnswer(text=self._stub.online_text, sources=sources)

    def ask_multi(self, prompt: str, system: str = "", queries=()):
        from brain.llm.search import multi_ask

        return multi_ask(self, prompt, system, queries)


# ---------- 应用 ----------


def build_app():
    """装配一整套应用（只构造不启动：没有定时器在跑，测试自己驱动）。

    **模拟"重启"的那些用例也得走这里**，别直接 ``DesktopPetApp()``：直接构造的话
    cleanup() 不知道它的存在，它攥着的 pet.db / mind.db / chroma.sqlite3 就没人松，
    沙箱目录会整个留在 %TEMP% 里（实测攒到过 11751 个、4.7 GB）。
    """
    from core.bootstrap import DesktopPetApp

    app = DesktopPetApp()
    # 社交截屏认字（v2-social-fewshot）：真那条会在社交软件前台时截一次真屏、再跑
    # RapidOCR（几秒、还取决于测试机当时前台是什么窗口）。跟 `_motion_grabber` 一个
    # 道理——测试里统一换成"取不到帧"的桩：拿不到画面，整条降级成"没认到对方原话"，
    # 跟加它之前逐字相同。要验这条线的用例自己把 `_grab_chat` / `_read_chat` 换掉。
    social = getattr(app, "social_reply", None)
    if social is not None:
        social._grab_chat = lambda: None
    if _LAST_SANDBOX is not None:
        _LAST_SANDBOX.apps.append(app)
    return app


def make_env(with_stub: bool = True, prepare=None):
    """一次测试的标准准备：沙箱 + （可选的）构造前调整 + 桩模型 + 应用。

    prepare 会在沙箱建好、应用构造之前调用，用来改那些"构造时就定下来"的东西
    （比如番茄钟存档路径），改完才构造应用，才真的生效。
    """
    ensure_app()
    box = Sandbox()
    if prepare is not None:
        prepare(box)
    stub = install_stub_llm() if with_stub else None
    app = build_app()
    # v2-game-fix：画面运动特征那一层默认会**真去连抢 4 帧**（帧间隔 90 毫秒，整段约
    # 270 毫秒，还同步压在界面线程上）。测试机上当前前台是什么窗口完全不可控，真抓屏
    # 会让所有走"全屏兜底"那一档的用例变成看运气。这里统一换成一个"取不到帧"的桩：
    # 那一层拿不到画面就给 None，判定**退回加它之前**，跟原来逐字相同。
    # 想验运动特征本身的用例自己换掉这个桩（见 tests/test_game_fix.py）。
    monitor = getattr(app, "screen_monitor", None)
    if monitor is not None:
        monitor._motion_grabber = lambda: []
    return box, stub, app


def add_no_key_provider(config, name: str = "nokey") -> str:
    """往配置里塞一个"没有 Key"的供应商，用来测没钥匙时的分支。"""
    from core.config import LLMProviderConfig

    config.llm.providers[name] = LLMProviderConfig(
        name=name,
        protocol="openai_compatible",
        base_url="https://example.invalid/v1",
        model="nokey-model",
        api_key_ref=f"llm_{name}_not_set",
    )
    return name


class Recorder:
    """把某个方法换成记录员：既不改行为，又能断言"它被调用了、带的什么参数"。"""

    def __init__(self, target: Any, attr: str):
        self.calls: list[tuple] = []
        self._target = target
        self._attr = attr
        self._original = getattr(target, attr)

        def spy(*args, **kwargs):
            self.calls.append(args)
            return None

        setattr(target, attr, spy)

    @property
    def called(self) -> bool:
        return bool(self.calls)

    @property
    def last(self) -> tuple:
        return self.calls[-1] if self.calls else ()

    def restore(self) -> None:
        setattr(self._target, self._attr, self._original)
