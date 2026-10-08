"""
屏幕感知与陪伴（S2/S3/S4）的自动测试。

三块各测各的：
- S2 关键词粗判与采样链：分类函数、连击门槛、"不是视频类前台就不 OCR"；
- S3 刷剧陪伴：四态冒话、同态冷却、池内轮换、不弹卡；
- S4 自动番茄钟：切入开跑 / 切走暂停 / 切回接着跑 / 跑完收工 / 与手动番茄钟让位。

默认全部走假识别器（注入的 reader），只有一节真跑 RapidOCR——那一节是唯一能证明
"真的能认出中文字幕"的，引擎装不上就明确跳过，不假装通过。

红线也在这里量：识别结果不落任何盘、全程不调模型（桩模型一次都不该被喊到）。
"""

from __future__ import annotations

import inspect
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, use_scene_vote, wait_for

from PySide6.QtGui import QImage

from life.screen import browser as browser_module
from life.screen import monitor as mon
from life.screen.browser import TITLE_FOCUS, TITLE_WATCH, classify_title, is_browser
from life.screen.companion import SCENE_LINES
from life.screen.focus_timer import is_focus_proc
from life.screen.monitor import (
    SCENE_ANGRY,
    SCENE_FESTIVE,
    SCENE_HORROR,
    OcrReader,
    classify_text,
    is_watchable,
)


class FakeReader:
    """假识别器：读什么由测试说了算，省掉一次 4 秒的真 OCR。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    """假截屏：只记自己被叫了几次。"""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _fake_foreground(monitor, proc_holder, title_holder=None) -> None:
    """把前台进程换成可控的假值；给 title_holder 就顺手把窗口标题也换成假的。"""
    mon.foreground.foreground_pid = lambda: 1
    monitor._pids.name_of = lambda pid: proc_holder["proc"]
    if title_holder is not None:
        mon.foreground.foreground_title = lambda: title_holder["title"]
    # v2-mood-screen ①：全屏检测在离屏环境里会拿到真前台窗口（或桌面）矩形，把它钉成
    # False 让"记事本不采样"这些老断言照旧成立；要测全屏的那一节单独把它翻成 True。
    mon.is_fullscreen_foreground = lambda: False


def _sample(monitor, rounds: int = 1, timeout: float = 5.0) -> None:
    for _ in range(rounds):
        monitor.sample()
        wait_for(lambda: not monitor._reading, timeout=timeout)
        pump(0.02)


def main() -> int:
    box, stub, app = make_env()
    monitor = app.screen_monitor
    companion = app.screen_companion
    focus = app.screen_focus_timer
    pet = app.pet_window
    pet.show()
    pump(0.05)

    proc = {"proc": "potplayer.exe"}
    reader = FakeReader()
    grabber = FakeGrabber()
    _fake_foreground(monitor, proc)
    monitor._reader = reader
    monitor._grabber = grabber
    # 场景判定现在是"一分钟多数票"（v2-mood-detect 再审·终）。这一节验的是采样链，
    # 不是那 60 秒本身，所以塞个假钟：窗口 5 秒、每拍走 5 秒——两拍就填满，
    # 正好复现旧用例里"第一拍不报、第二拍成立"的节奏。
    clock = use_scene_vote(monitor, window=5.0)

    def _tick(rounds: int = 1) -> None:
        """走 N 拍：每拍把假钟往前 5 秒，再采一次。

        必须**每拍之间**走钟——投票和窗口都挂在 `clock` 上，一口气走完再连采两拍，
        两票会落在同一个时刻，老票又被窗口剪掉，多数票就永远凑不出来。
        """
        for _ in range(rounds):
            clock.advance()
            _sample(monitor)


    section("S2 关键词粗判：三态 + 反例")
    cases = [
        ("别过去那里有血", SCENE_HORROR),
        ("有鬼啊快跑", SCENE_HORROR),
        ("你凭什么骂人", SCENE_ANGRY),
        ("恭喜恭喜新婚快乐", SCENE_FESTIVE),
    ]
    for line, want in cases:
        got = classify_text(line)
        check(f"{line!r} → {want}", got == want, str(got))
    check("没有命中就返回 None（无命中不触发）", classify_text("今天天气不错我们出去走走") is None, "")
    check("空串也不触发", classify_text("") is None, "")
    check(
        "并列时恐怖优先（恐怖一票、笑容一票 → 恐怖）",
        classify_text("有鬼还恭喜") == SCENE_HORROR,
        str(classify_text("有鬼还恭喜")),
    )
    check("播放器值得 OCR", is_watchable("potplayer.exe"), "")
    check("浏览器值得 OCR", is_watchable("chrome.exe"), "")
    check("记事本不值得 OCR", not is_watchable("notepad.exe"), "")
    check("取不到前台进程时不 OCR", not is_watchable(""), "")

    section("S2 采样链：多数票门槛 + 只在该看的时候看")
    reader.text = "别过去那里有血"
    proc["proc"] = "notepad.exe"
    before = grabber.calls
    _tick()
    check(
        "前台是记事本 → 一次都不截屏（不去看人家屏幕）",
        grabber.calls == before and reader.calls == 0,
        f"截屏={grabber.calls} 识别={reader.calls}",
    )
    check("没看也没场景", monitor.scene == "", monitor.scene)

    proc["proc"] = "potplayer.exe"
    seen: list[str] = []
    monitor.scene_changed.connect(seen.append)
    _tick()
    check("第一拍命中还不报（窗口没满）", seen == [], str(seen))
    _tick()
    check(
        "第二拍填满窗口、多数票是恐怖 → 场景成立",
        seen == [SCENE_HORROR] and monitor.scene == SCENE_HORROR,
        str(seen),
    )
    _tick()
    check("第三拍不重复报同一个场景", seen == [SCENE_HORROR], str(seen))

    # 字幕没了 = 这几拍弃权（v2-mood-recall-fix：弃权不投票，也不等于散场）。
    # 场景维持不变——不秒退、不闪烁；真散场靠下面的硬证据。
    reader.text = ""
    _tick(2)
    check("字幕没了那几拍（弃权）→ 场景维持不变，不散场",
          seen == [SCENE_HORROR] and monitor.scene == SCENE_HORROR, str(seen))

    proc["proc"] = "notepad.exe"      # 硬证据：前台不是视频
    _tick()
    check("切走（硬闸命中）→ 立刻散场", seen[-1] == "" and monitor.scene == "", str(seen))

    section("S3 刷剧陪伴：三态冒话 + 同态冷却 + 不弹卡（v2-mood-tri：哭泣那一池已删）")
    # 先把专注态清干净：本节验的是陪伴自己的池子与冷却，而 v2-mood-detect 之后
    # **专注在演就不冒剧情反应**（场景互斥）。不清的话，前面 S4 那节留下的番茄钟
    # 会把这几句全挡掉——那验的就不是池子，是互斥闸了（互斥闸自己由
    # tests/test_scene_guard.py 专门验）。清完再显式验一次"专注中确实不冒"。
    app.screen_focus_timer.reset()
    app.pomodoro_service.timer.reset()
    pump(0.05)
    expected = {
        SCENE_HORROR: "我怕怕…",
        SCENE_ANGRY: "太气人，真想捶他几下",
        SCENE_FESTIVE: "好开心",
    }
    check(
        "三态语句池第一句就是定稿原文",
        all(SCENE_LINES[key][0] == line for key, line in expected.items()),
        str({k: SCENE_LINES[k][0] for k in expected}),
    )
    check(
        "哭泣那一池整池没了（v2-mood-tri：用户定稿的四句也不留，原文抄进报告第九十六节）",
        "cry" not in SCENE_LINES
        and all("我也好想哭" not in line and "别哭了" not in line
                and "哭出来会好受点" not in line
                for pool in SCENE_LINES.values() for line in pool),
        str(sorted(SCENE_LINES)),
    )
    for scene, line in expected.items():
        pet._bubble.hide_bubble()
        pet._speech_until = 0.0
        # 前面 S2 那节采样时陪伴已经在冒过话了，这里把"说到哪句了"一起清掉，
        # 不然验的就不是"定稿那句"而是池子里的第 N 句
        companion._last_spoken.clear()
        companion._cursor.clear()
        companion.on_scene(scene)
        pump(0.05)
        check(
            f"{scene} 冒的是定稿那句",
            pet._bubble.isVisible() and pet._bubble._label.text() == line,
            repr(pet._bubble._label.text()),
        )

    pet._bubble.hide_bubble()
    pet._speech_until = 0.0
    companion._last_spoken.clear()
    companion._cursor.clear()
    companion.on_scene(SCENE_HORROR)
    pump(0.05)
    first_line = pet._bubble._label.text()
    pet._bubble.hide_bubble()
    companion.on_scene(SCENE_HORROR)
    pump(0.05)
    check(
        "同态冷却：连着两次只冒一次",
        not pet._bubble.isVisible() and first_line == SCENE_LINES[SCENE_HORROR][0],
        f"第二次可见={pet._bubble.isVisible()}",
    )

    companion._last_spoken.clear()
    companion._cursor.pop(SCENE_HORROR, None)
    said = []
    for _ in range(len(SCENE_LINES[SCENE_HORROR])):
        companion._last_spoken.clear()
        pet._bubble.hide_bubble()
        pet._speech_until = 0.0
        companion.on_scene(SCENE_HORROR)
        pump(0.05)
        said.append(pet._bubble._label.text())
    check(
        "池子里轮着挑，一轮内不重样",
        len(set(said)) == len(said) and all(s in SCENE_LINES[SCENE_HORROR] for s in said),
        str(said),
    )

    pet._bubble.hide_bubble()
    companion._last_spoken.clear()
    for scene in SCENE_LINES:
        companion.on_scene(scene)
    pump(0.05)
    check("冒话不弹卡（overlay 没被打开）", not app.overlay.is_open(), str(app.overlay.is_open()))

    # 场景互斥（v2-mood-detect）：专注在演的时候，上面这四句一句都不该冒。
    pet._bubble.hide_bubble()
    pet._speech_until = 0.0
    companion._last_spoken.clear()
    companion._cursor.clear()
    app.screen_focus_timer.enter("chaoxing.exe")
    pump(0.05)
    pet._bubble.hide_bubble()      # 进专注（v2-focus-quiet 之后已不冒开场词，但仍先收掉旧气泡）
    companion.on_scene(SCENE_HORROR)
    pump(0.05)
    check("专注在演 → 剧情反应不冒（场景互斥）",
          not pet._bubble.isVisible(), repr(pet._bubble._label.text()))
    app.screen_focus_timer.reset()
    pump(0.05)
    companion.on_scene(SCENE_HORROR)
    pump(0.05)
    check("退出专注 → 同一句照旧冒（反向对照）",
          pet._bubble.isVisible() and pet._bubble._label.text() == SCENE_LINES[SCENE_HORROR][0],
          repr(pet._bubble._label.text()))

    section("S4 自动番茄钟：切入开跑 / 切走暂停 / 切回接着跑")
    check("网课应用算专注", is_focus_proc("chaoxing.exe"), "")
    check("创作应用算专注", is_focus_proc("photoshop.exe"), "")
    check("IDE 算专注（走工作类）", is_focus_proc("pycharm64.exe"), "")
    check("浏览器不算专注", not is_focus_proc("chrome.exe"), "")
    check("取不到进程不算专注", not is_focus_proc(""), "")

    section("S1 工作·看资料：AI 助手 / AI 编码助手 / 阅读与文档")
    # 用户报的现象：开着 ima 番茄钟会暂停（判成"其他"），而 VS Code 会跑。
    # 这一批是补进内置"工作"表的，判定前真实取值全部是 False。
    look_things_up = [
        ("ima.copilot.exe", "腾讯 ima"),        # False → True
        ("claude.exe", "Claude 桌面版/CLI"),     # False → True
        ("cc-switch.exe", "Claude Code 配置切换器"),  # False → True
        ("cursor.exe", "Cursor 编辑器"),         # False → True
        ("fastpdf.exe", "PDF 阅读器"),           # False → True
        ("tongyi.exe", "通义千问 PC"),
        ("doubao.exe", "豆包 PC"),
        ("chatgpt.exe", "ChatGPT 桌面版"),
        ("windsurf.exe", "Windsurf"),
        ("trae.exe", "Trae"),
        ("sumatrapdf.exe", "SumatraPDF"),
        ("calibre.exe", "Calibre"),
        ("zotero.exe", "Zotero"),
        ("notion.exe", "Notion"),
    ]
    still_broken = [p for p, _ in look_things_up if not is_focus_proc(p)]
    check(
        f"看资料类的 {len(look_things_up)} 个应用都算专注了",
        not still_broken,
        f"仍不算 {still_broken}",
    )

    # 反例：这次补的是"看资料"，不能把摸鱼/娱乐/社交也拉进来
    must_stay_out = [
        ("wechat.exe", "社交"), ("steam.exe", "娱乐"), ("taobao.exe", "摸鱼"),
        ("potplayer.exe", "娱乐"), ("cloudmusic.exe", "娱乐"), ("chrome.exe", "其他"),
        ("explorer.exe", "其他"), ("", "取不到进程"),
    ]
    leaked = [p for p, _ in must_stay_out if is_focus_proc(p)]
    check(
        f"反例：{len(must_stay_out)} 个非工作应用一个都没被拉进来",
        not leaked,
        f"误拉进来 {leaked}",
    )

    # 表本身的结构：dict 字面量重复 key 会静默吃掉前一条，得挡住
    from monitor.category import BUILTIN, builtin_category

    keys = list(BUILTIN)
    check(
        f"内置表 {len(keys)} 条：无重复 key、全是小写 .exe",
        len(keys) == len(set(keys)) and all(k == k.lower() and k.endswith(".exe") for k in keys),
        f"重复={[k for k in set(keys) if keys.count(k) > 1]}",
    )
    check(
        "工作类比以前多（只增不减，没顺手改别家的分类）",
        sum(1 for v in BUILTIN.values() if v == "工作") >= 73
        and builtin_category("wechat.exe") == "社交"
        and builtin_category("steam.exe") == "娱乐",
        f"工作 {sum(1 for v in BUILTIN.values() if v == '工作')} 条",
    )
    check(
        "不带前缀的泛名没收（copilot.exe 谁家都可能叫，不敢收）",
        "copilot.exe" not in BUILTIN and not is_focus_proc("copilot.exe"),
        f"{builtin_category('copilot.exe')}",
    )

    focus.reset()
    pet._bubble.hide_bubble()
    proc["proc"] = "explorer.exe"
    _sample(monitor)
    pump(0.05)
    check(
        "还没进专注应用：没在跑、浮窗也不显示",
        not focus.running and not app.pomodoro_bubble.isVisible(),
        f"running={focus.running} 浮窗={app.pomodoro_bubble.isVisible()}",
    )

    # 进专注那一下冒不冒话，看**广播**，不看气泡文本——气泡里可能还留着上一节的旧句子，
    # 光看文本分不出"这一拍有没有新冒"。v2-focus-quiet 之后进场是静默的，只亮闹钟。
    spoken: list[str] = []
    app.bus.pet_speak_proactive.connect(spoken.append)
    spoken.clear()
    proc["proc"] = "chaoxing.exe"
    _sample(monitor)
    pump(0.05)
    check(
        "切到网课 → 自动开跑",
        focus.running and focus.proc == "chaoxing.exe",
        f"running={focus.running} proc={focus.proc}",
    )
    check(
        "开跑时浮窗挂上了",
        app.pomodoro_bubble.isVisible(),
        str(app.pomodoro_bubble.isVisible()),
    )
    check(
        "开跑一个字没冒（进专注静默，只亮闹钟）",
        spoken == [],
        repr(spoken),
    )
    app.bus.pet_speak_proactive.disconnect(spoken.append)

    planned = focus.planned
    pump(1.2)
    check(
        "真在倒计时（剩余在往下走）",
        0 < focus.remaining < planned,
        f"{focus.remaining} / {planned}",
    )

    kept = focus.remaining          # 切走**之前**取，切走之后它会被清回整轮
    proc["proc"] = "explorer.exe"
    _sample(monitor)
    pump(0.05)
    check(
        "切走 → 这一轮结束（v2-focus-notes2 ①：原来只是暂停、剩余留住）",
        (not focus.running) and (not focus.in_progress) and focus.remaining == planned
        and 0 < kept < planned,
        f"running={focus.running} remaining={focus.remaining} 切走时={kept}",
    )
    check(
        "切走 → 浮窗当场收掉（原来会一直挂在屏幕上）",
        not app.pomodoro_bubble.isVisible(),
        str(app.pomodoro_bubble.isVisible()),
    )

    proc["proc"] = "photoshop.exe"
    _sample(monitor)
    pump(0.05)
    check(
        "切回创作类 → 重新起一轮（整轮开始，不是接着剩的）",
        focus.running and focus.remaining == planned,
        f"remaining={focus.remaining}/{planned} 切走时={kept}",
    )

    app.config.screen.focus_minutes = 1
    focus.reset()
    pet._bubble.hide_bubble()
    pet._speech_until = 0.0
    proc["proc"] = "explorer.exe"
    _sample(monitor)
    proc["proc"] = "chaoxing.exe"
    _sample(monitor)
    # 进了专注再把剩余拨到 2 秒，好在这一拍里等它跑完
    # （不能先拨再采样：切走那一下现在就把这一轮结束了，拨的会被清掉）
    focus._remaining = 2
    done = wait_for(lambda: not focus.running and focus.remaining == 0, timeout=8.0)
    pump(0.1)
    check("一轮跑完 → 归零收工", done, f"running={focus.running} remaining={focus.remaining}")
    check(
        "收工时说了一句，浮窗收起来",
        "到啦" in pet._bubble._label.text() and not app.pomodoro_bubble.isVisible(),
        f"文本={pet._bubble._label.text()!r} 浮窗={app.pomodoro_bubble.isVisible()}",
    )

    section("S4 与手动番茄钟让位：同一时刻只有一套在显示")
    app.pomodoro_service.timer.start_focus()
    pump(0.05)
    focus._remaining = 100
    focus._running = True
    app._on_screen_focus_changed()
    pump(0.05)
    check(
        "手动那套在跑时，自动这套不抢浮窗",
        app.pomodoro_service.timer.is_active,
        f"手动 is_active={app.pomodoro_service.timer.is_active}",
    )
    app.pomodoro_service.timer.reset()
    focus.reset()

    section("S4 专注门控：两套番茄钟都算数（主动搭话别在看网课时插嘴）")
    app.pomodoro_service.timer.reset()
    focus.reset()
    check(
        "两套都没跑 → 不算专注",
        app._is_focusing() is False,
        str(app._is_focusing()),
    )
    focus._running = True
    check(
        "自动那套在跑 → 算专注",
        app._is_focusing() is True,
        str(app._is_focusing()),
    )
    focus._running = False
    focus._remaining = focus.planned - 30
    check(
        "没在跑就不算专注（v2-focus-notes2 ① 之后切走直接回整轮，"
        "这里是「有进度但没在跑」的兜底口径，人已经不在正事上了）",
        app._is_focusing() is False,
        str(app._is_focusing()),
    )
    focus.reset()
    app.pomodoro_service.timer.start_focus()
    pump(0.05)
    check(
        "手动那套在跑 → 照样算专注",
        app._is_focusing() is True,
        str(app._is_focusing()),
    )
    app.pomodoro_service.timer.reset()
    focus.reset()

    section("S4 扩展：浏览器前台读标题分类（破例）")
    check("浏览器认得出", is_browser("chrome.exe") and is_browser("msedge.exe"), "")
    check("非浏览器不认（播放器不走这条路）", not is_browser("potplayer.exe"), "")

    titles = [
        ("学习通 - 超星", TITLE_FOCUS),
        ("中国大学MOOC(慕课)_国家精品课程在线学习平台", TITLE_FOCUS),
        ("腾讯课堂 - 在线教育平台", TITLE_FOCUS),
        ("Coursera | 在线课程", TITLE_FOCUS),
        ("学堂在线 - 精品在线课程学习平台", TITLE_FOCUS),
        ("Figma: 在线协作设计工具", TITLE_FOCUS),
        ("某电视剧 第01集 - 在线观看", TITLE_WATCH),
        ("爱奇艺-在线视频", TITLE_WATCH),
        ("哔哩哔哩 (゜-゜)つロ 干杯~", TITLE_WATCH),
        ("今日头条", ""),
        ("", ""),
    ]
    for title, want in titles:
        got = classify_title(title)
        check(f"{title[:22]!r} → {want or '不占'}", got == want, repr(got))
    check(
        "网课和剧集同时命中时按专注算（安静起倒计时，比误开口代价小）",
        classify_title("哔哩哔哩课堂 第01集") == TITLE_FOCUS,
        repr(classify_title("哔哩哔哩课堂 第01集")),
    )

    # ---- 端到端：chrome 进学习通 → 自动番茄钟 ----
    title = {"title": ""}
    _fake_foreground(monitor, proc, title)
    reader.text = ""
    # 先改配置再 reset：reset 是照当时的时长把剩余拨回整轮的
    app.config.screen.focus_minutes = 25
    focus.reset()
    pet._bubble.hide_bubble()

    proc["proc"] = "chrome.exe"
    title["title"] = "某电视剧 第01集 - 在线观看"
    before = grabber.calls
    _sample(monitor)
    pump(0.05)
    check("chrome 在看剧 → 标题判成 watch", monitor.title_kind == TITLE_WATCH, monitor.title_kind)
    check(
        "看剧时照旧 OCR（走 S2 刷剧那条）",
        grabber.calls > before,
        f"截屏 {before} → {grabber.calls}",
    )
    check(
        "看剧不进番茄钟",
        not focus.running,
        f"running={focus.running}",
    )

    title["title"] = "学习通 - 超星"
    before, reads_before = grabber.calls, reader.calls
    _sample(monitor)
    pump(0.05)
    check("同一个 chrome 换到学习通 → 判成 focus", monitor.title_kind == TITLE_FOCUS, monitor.title_kind)
    check(
        "网课时一次都不截屏（人上课，不去看人家屏幕）",
        grabber.calls == before and reader.calls == reads_before,
        f"截屏 {before} → {grabber.calls}，识别 {reads_before} → {reader.calls}",
    )
    check(
        "网课 → 自动番茄钟跑起来了",
        focus.running and focus.remaining == focus.planned,
        f"running={focus.running} remaining={focus.remaining}/{focus.planned}",
    )
    check("浮窗挂上了", app.pomodoro_bubble.isVisible(), str(app.pomodoro_bubble.isVisible()))

    pump(1.2)
    check(
        "真在倒计时",
        0 < focus.remaining < focus.planned,
        f"{focus.remaining}/{focus.planned}",
    )
    kept = focus.remaining

    title["title"] = "某电视剧 第01集 - 在线观看"
    _sample(monitor)
    pump(0.05)
    check(
        "同一个 chrome 切回剧集 → 这一轮结束、回整轮（v2-focus-notes2 ①）",
        (not focus.running) and focus.remaining == focus.planned and kept < focus.planned,
        f"running={focus.running} remaining={focus.remaining} 切走时={kept}",
    )

    title["title"] = "Figma: 在线协作设计工具"
    _sample(monitor)
    pump(0.05)
    check(
        "换到在线创作 → 重新起一轮（整轮开始）",
        focus.running and focus.remaining == focus.planned,
        f"remaining={focus.remaining}/{focus.planned} 切走时={kept}",
    )

    proc["proc"] = "explorer.exe"
    title["title"] = ""
    _sample(monitor)
    pump(0.05)
    check(
        "离开浏览器 → 结束这一轮、回空闲",
        (not focus.running) and (not focus.in_progress) and monitor.title_kind == "",
        f"running={focus.running} in_progress={focus.in_progress} "
        f"title_kind={monitor.title_kind!r}",
    )

    section("S1 扩展词表：小众网校 / 垂直课程站 / 境外小平台 / 内训平台")
    # 这批的共同点：名字里没有"课堂/课程/慕课/培训"这类泛词，加词表之前**一个字都兜不到**。
    # 括号里是修复前的真实取值（全部为空）。
    long_tail = [
        "极客时间 - IT技术学习平台",            # 空
        "Geekbang | 极客邦",                    # 空
        "小鹅通-知识付费与在线教育技术服务商",      # 空
        "有道精品课 - 网易有道旗下在线教育平台",    # 空
        "网易有道课堂",                          # 空
        "沪江网校 - 在线学习",                   # 空
        "学而思网校",                            # 空
        "猿辅导在线教育",                        # 空（"辅导"不在表里）
        "三节课 - 数字人才培养",                  # 空
        "开课吧-数字化人才在线教育平台",           # 空
        "拉勾教育",                              # 空
        "起点学院",                              # 空
        "蓝墨云班课",                            # 空
        "学银在线",                              # 空
        "国家智慧教育平台",                       # 空
        "好大学在线",                            # 空
        "Udacity | Learn the latest tech skills",  # 空
        "DataCamp | Learn Data Science Online",    # 空
        "Moodle: 在线学习平台",                    # 空
        "Pluralsight | 技术技能平台",              # 空
        "UMU 互动学习平台",                        # 空
        "腾讯乐享",                               # 空
    ]
    missed = [t for t in long_tail if classify_title(t) != TITLE_FOCUS]
    check(
        f"补进来的 {len(long_tail)} 个长尾平台标题都能判成网课",
        not missed,
        f"仍漏判 {len(missed)} 个：{missed[:3]}",
    )

    # 这几个**不用**重复收词：名字里的泛词已经把标题盖住了。
    # 写这条是为了钉住"不重复堆词"——将来有人看到平安知鸟没在词表里，别以为漏了。
    covered_by_generic = [
        ("网易云课堂 - 学习改变命运", "课堂"),
        ("imooc 慕课网", "慕课"),
        ("平安知鸟 - 企业培训", "培训"),
        ("酷学院 - 企业培训平台", "培训"),
        ("Canvas LMS", "canva"),      # 误打误撞命中的，见 browser.py 里的说明
    ]
    for title, why in covered_by_generic:
        check(
            f"靠『{why}』兜住、不必重复收词：{title[:20]}",
            classify_title(title) == TITLE_FOCUS,
            repr(classify_title(title)),
        )

    # 反例：收词时被**故意**挡在门外的，一个都不许被误判。
    # 每一条都对应 browser.py 里被否掉的那个词。
    must_not_focus = [
        ("有道云笔记 - 记录，成为更好的自己", "不收光『有道』"),
        ("乐享生活 - 品味每一天", "不收光『乐享』"),
        ("起点中文网 - 小说阅读", "不收光『起点』"),
        ("拉勾招聘 - 互联网人才求职平台", "不收光『拉勾』"),
        ("中国科学院", "不收光『学院』"),
        ("企业微信", "不收光『企业微信』：多数时候是聊天"),
        ("知乎 - 有问题，就会有答案", "普通网页"),
        ("京东 - 正品低价、品质保障", "普通网页"),
        ("淘宝网", "普通网页"),
        ("百度一下，你就知道", "普通网页"),
    ]
    wrong = [(t, classify_title(t)) for t, _ in must_not_focus if classify_title(t) != ""]
    check(
        f"收词纪律：{len(must_not_focus)} 个『差点收进来』的标题一个都没误判",
        not wrong,
        f"误判了 {wrong}",
    )

    # 匹配规则没动：还是"大小写不敏感的子串"。
    check(
        "规则不变：纯子串、大小写不敏感",
        classify_title("COURSERA | 在线课程") == TITLE_FOCUS
        and classify_title("Geekbang".upper()) == TITLE_FOCUS,
        f"{classify_title('COURSERA | 在线课程')!r} / {classify_title('GEEKBANG')!r}",
    )
    for name, table in (
        ("COURSE_WORDS", browser_module.COURSE_WORDS),
        ("CREATIVE_WORDS", browser_module.CREATIVE_WORDS),
        ("WATCH_WORDS", browser_module.WATCH_WORDS),
    ):
        dups = [w for w in set(table) if table.count(w) > 1]
        check(
            f"{name}：{len(table)} 条，全小写、无重复",
            not dups and all(w == w.lower() and w.strip() for w in table),
            f"重复={dups}",
        )

    section("S4 扩展：破例的闸门")
    calls = {"n": 0}

    def _count_title() -> str:
        calls["n"] += 1
        return "学习通 - 超星"

    mon.foreground.foreground_title = _count_title
    proc["proc"] = "chrome.exe"
    app.config.screen.read_browser_title = False
    _sample(monitor)
    check(
        "总闸关掉 → 一次标题都不读",
        calls["n"] == 0 and monitor.title_kind == "",
        f"读了 {calls['n']} 次 title_kind={monitor.title_kind!r}",
    )
    app.config.screen.read_browser_title = True
    _sample(monitor)
    check("总闸打开 → 读了", calls["n"] == 1 and monitor.title_kind == TITLE_FOCUS, f"读了 {calls['n']} 次")

    calls["n"] = 0
    proc["proc"] = "potplayer.exe"
    _sample(monitor)
    check(
        "不是浏览器 → 一个字的标题都不读",
        calls["n"] == 0,
        f"读了 {calls['n']} 次",
    )

    section("S5 红线：不调模型 / 不落盘 / 腔调隔离")
    check(
        "整条屏幕感知链一句话都没发给模型（桩模型一次没被喊）",
        not stub.prompts,
        f"模型收到 {len(stub.prompts)} 条",
    )
    check(
        "冒的话不经过角色卡系统（没有玄机/命理腔的入口）",
        not hasattr(companion, "_persona")
        and not hasattr(companion, "_psych_service")
        and "命理" not in "".join(SCENE_LINES[SCENE_HORROR]),
        "",
    )
    check(
        "语句池里没有危机词（冒话前那道拦截是双保险）",
        all(not _crisis(line) for pool in SCENE_LINES.values() for line in pool),
        "",
    )
    check(
        "屏幕感知的状态只在内存里（对象上没有落盘入口）",
        not hasattr(monitor, "save_state")
        and not hasattr(focus, "save_state")
        and not hasattr(companion, "save_state"),
        "",
    )
    # 破例读标题这条：进的是标题，出的只能是 focus / watch 这个标签。
    # 把标题原文在对象上留一份，破例就变成"悄悄记下用户在看什么"，那是不行的。
    secret = "学习通 - 超星：第九章 抽象代数"
    mon.foreground.foreground_title = lambda: secret
    proc["proc"] = "chrome.exe"
    _sample(monitor)
    dump = repr(vars(monitor)) + repr(vars(focus)) + repr(vars(companion))
    check(
        "标题原文没在任何对象上留下（只留 focus/watch 这个标签）",
        secret not in dump
        and "抽象代数" not in dump
        and monitor.title_kind in ("", TITLE_FOCUS, TITLE_WATCH),
        f"title_kind={monitor.title_kind!r} 泄漏={secret in dump}",
    )
    check(
        "留在对象上的只有那个标签本身",
        monitor.title_kind == TITLE_FOCUS and "第九章" not in dump,
        f"title_kind={monitor.title_kind!r}",
    )
    check(
        "读标题这条线不碰 pet.db（browser 模块里没有落盘入口）",
        not hasattr(browser_module, "save_state")
        and "open(" not in inspect.getsource(browser_module),
        "",
    )

    section("S2 真跑一次 RapidOCR：真的认得出中文字幕")
    real = _real_ocr_case()
    if real is None:
        check("RapidOCR 没装 / 起不来 → 明确跳过（不假装通过）", True, "skipped")
    else:
        got, text = real
        check(
            f"真 OCR 认出 {text!r} 并判成恐怖",
            got == SCENE_HORROR,
            f"场景={got}",
        )

    drain_threads()
    box.cleanup()
    return summary("屏幕感知与陪伴")


def _crisis(text: str) -> bool:
    from life.psych.crisis import is_crisis

    return is_crisis(text)


def _real_ocr_case():
    """真跑一次：渲染一张中文字幕图 → RapidOCR → 分类。起不来就返回 None。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except ImportError:
        return None
    reader = OcrReader()
    if reader._ensure() is None:
        return None
    font_path = "C:/Windows/Fonts/msyh.ttc"
    if not Path(font_path).exists():
        return None
    text = "别过去那里有血"
    image = Image.new("RGB", (960, 160), (16, 16, 16))
    ImageDraw.Draw(image).text((40, 50), text, font=ImageFont.truetype(font_path, 56), fill=(255, 255, 255))
    path = Path(tempfile.gettempdir()) / "pet_test_screen_ocr.png"
    image.save(path)
    try:
        got_text = reader.read(QImage(str(path)))
    finally:
        path.unlink(missing_ok=True)
    return classify_text(got_text), got_text


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
