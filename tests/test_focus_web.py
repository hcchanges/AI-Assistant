"""
专注/番茄钟：工作网页识别 + 暂停可退出（v2-focus-web）的自动测试。

真机两条反馈合并修：① 打开工作网页识别不了、时钟不走、休息没法测；
② 专注暂停后无法退出，卡住测不了别的。

S0 查出来的实况（括号里是**修复前的真实取值**）：

- 判定只看「前台进程名」+「浏览器窗口标题」两样，URL 读不到（Windows 窗口标题里只有
  页面标题）。浏览器进程名归"其他"，所以只有标题那一腿能救它；而标题词表原先只有
  网课（COURSE_WORDS）与创作工具（CREATIVE_WORDS）——
  飞书/企业微信/Google 文档/GitHub/Jira/禅道/Confluence/数据看板/订单管理系统
  **全部判成空串**（修复前逐条实测 `''`），于是不进专注、不起表、时钟不走。
  （「企业微信」「有道云笔记」修复前也是空串，但这两个**保持**空串：S1 收词时明确否过，
  理由见 browser.py 里 WORK_WORDS 上面那段，要用的人在设置页标「工作站点词」。）
- 自动番茄钟**界面上一个控件都没有**（浮窗 `WindowDoesNotAcceptFocus`，点不了）；
  暂停（=切走）之后 `in_progress` 恒真、浮窗一直挂着，切回专注应用又接着跑，
  只能退出程序。手动那套有「重置」=退出、还有短休/长休（"休息没法测"就是它），
  但它的面板**没有任何用户可达入口**（入口卡被摘掉、菜单里没有、全仓库没有
  `open_panel("pomodoro")`，只有 test_carousel 直接调 show_page）。
  （**v2-focus-notes2 ①③ 之后这条改掉了**：切走即结束——不再有"暂停"那一态，
  浮窗当场收掉；右键菜单也不再临时多冒「结束专注」/「番茄钟」两条，四条固定。
  本文件里原先冲着暂停态写的那几项断言已经跟着改成新行为。）
- 另外两个真 bug：`focus_enabled` 关掉时 `_reevaluate` 是 `return` 而不是 `leave()`
  （开关关了表照跑）；`Config.save()` 的落盘名单里**没有 screen 段**，
  设置页改完专注时长/开关是静默不落盘的。

这一轮验五件事：
- **①工作网页认得出**：内置工作词表 + 用户自定义「工作站点词」（标题腿），
  chrome 开着工作网页 → 起表、真倒计时、浮窗挂上；
- **非工作网页不误启动**：爱奇艺/剧集照旧走刷剧、不抢成番茄钟；
- **休息真的到得了**：菜单能打开番茄钟面板（专注/短休/长休/重置都在那一页）；
- **②离开即结束**（v2-focus-notes2 ①）：切走不再停在"暂停"，直接回空闲、浮窗收掉、
  一切解锁；再没有"暂停态出不去"这个状态；
- **红线**：手动起的那一轮只存内存，pet.db / mind.db 一行不多；零新依赖；
  screen 段真落盘（改完重启还在）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

import yaml
from PySide6.QtGui import QImage
from PySide6.QtWidgets import QPushButton, QTabWidget

from life.screen import monitor as mon
from life.screen.browser import TITLE_FOCUS, TITLE_WATCH, classify_title
from life.screen.focus_timer import is_focus_proc

import ui.settings as settings_mod

ROOT = Path(__file__).resolve().parents[1]


class FakeReader:
    """假识别器：省掉一次 4 秒的真 OCR。"""

    def __init__(self, text: str = "") -> None:
        self.text = text
        self.calls = 0

    def read_array(self, array) -> str:
        self.calls += 1
        return self.text


class FakeGrabber:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, rect=None) -> QImage:
        self.calls += 1
        return QImage(8, 8, QImage.Format_RGB32)


def _fake_foreground(monitor, proc_holder, title_holder) -> None:
    mon.foreground.foreground_pid = lambda: 1
    monitor._pids.name_of = lambda pid: proc_holder["proc"]
    mon.foreground.foreground_title = lambda: title_holder["title"]


def _sample(monitor, rounds: int = 1, timeout: float = 5.0) -> None:
    for _ in range(rounds):
        monitor.sample()
        wait_for(lambda: not monitor._reading, timeout=timeout)
        pump(0.02)


def _rows(db_path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("select count(*) from utterance").fetchone()[0]
    finally:
        con.close()


def _menu_texts(widget) -> list[str]:
    """这个窗口右键菜单上的文案（按当前状态现场重排一次）。"""
    return [a.text() for a in widget._build_menu().actions() if not a.isSeparator()]


def main() -> int:
    box, _stub, app = make_env()
    monitor = app.screen_monitor
    focus = app.screen_focus_timer
    pet = app.pet_window
    pet.show()
    pump(0.05)

    proc = {"proc": "chrome.exe"}
    title = {"title": ""}
    reader, grabber = FakeReader(), FakeGrabber()
    _fake_foreground(monitor, proc, title)
    monitor._reader = reader
    monitor._grabber = grabber
    app.config.screen.scene_stable_samples = 2

    # ---------- ① 工作网页认得出 ----------

    section("① 标题腿：工作网页从『认不出来』到『算专注』")
    # 左边是修复前逐条实测的真实取值（全部为空串），右边是修复后应有的样子。
    work_titles = [
        ("飞书 - 工作台", "''"),
        ("云效 - 流水线详情", "''"),
        ("Google 文档 - 周报 - Google Chrome", "''"),
        ("GitHub - hc/pet: 电子宠物", "''"),
        ("Jira - 需求看板", "''"),
        ("禅道 - 我的地盘", "''"),
        ("Confluence - 项目空间", "''"),
        ("数据看板 - 后台管理", "''"),
        ("订单管理系统", "''"),
        ("金山文档 - 在线编辑", "focus（原本就认得）"),
    ]
    for text, before in work_titles:
        got = classify_title(text)
        check(f"{text[:24]!r} → 专注（修复前 {before}）", got == TITLE_FOCUS, repr(got))

    section("① 非工作网页不误判（不回退 S1 的老词表与刷剧那条）")
    check("剧集标题照旧判 watch", classify_title("某电视剧 第01集 - 在线观看") == TITLE_WATCH, "")
    check("爱奇艺照旧判 watch", classify_title("爱奇艺-在线视频") == TITLE_WATCH, "")
    check("资讯页照旧不占（不误判成专注）", classify_title("今日头条") == "", repr(classify_title("今日头条")))
    check("空白标题不占", classify_title("") == "", "")

    section("① 补工作词表时，S1 那把『收词尺子』没被撑大（跟 test_screen 同一条纪律）")
    # 这两条是 S1 明确否掉的词，补 WORK_WORDS 时最容易顺手收回来——收回来就是回退。
    # 想在自己单位的企业微信/笔记页面上算专注的，走设置页「工作站点词」，不动内置表。
    for text, why in (
        ("企业微信", "多数时候是同事聊天，且它的桌面端本来就归『工作』"),
        ("有道云笔记 - 记录，成为更好的自己", "个人记事本，不是工作页面"),
    ):
        check(f"{text[:14]!r} 仍不占专注（{why}）", classify_title(text) == "",
              repr(classify_title(text)))
    check("用户真标了就认（企业微信走『工作站点词』这条路）",
          classify_title("企业微信", ("企业微信",)) == TITLE_FOCUS, "")

    section("① v2-focus-procs 那批应用一个都没丢（不回退）")
    for name, why in (
        ("ima.copilot.exe", "那次补的看资料类"),
        ("claude.exe", "那次补的 AI 助手"),
        ("zotero.exe", "那次补的文献阅读"),
        ("pycharm64.exe", "原有 IDE"),
        ("chaoxing.exe", "网课"),
        ("photoshop.exe", "创作"),
    ):
        check(f"{name} 还算专注（{why}）", is_focus_proc(name), "")
    check("浏览器本身仍不算专注（要靠标题那条腿）", not is_focus_proc("chrome.exe"), "")

    section("① 自定义工作站点词：自家系统也能标进来")
    check(
        "没标之前，自家系统的标题认不出来",
        classify_title("公司OA - 待办事项") == "",
        repr(classify_title("公司OA - 待办事项")),
    )
    check(
        "标了之后算专注（大小写不敏感）",
        classify_title("公司OA - 待办事项", ("公司oa",)) == TITLE_FOCUS,
        repr(classify_title("公司OA - 待办事项", ("公司oa",))),
    )
    app.config.screen.focus_words = ("公司OA", "工单系统")
    check(
        "词表进了判定（'工单系统' 也认）",
        classify_title("工单系统 - 我的待办", app.config.screen.focus_words) == TITLE_FOCUS,
        "",
    )
    app.config.screen.focus_words = ()

    # ---------- ① 端到端：chrome 进工作网页 → 计时走动 ----------

    section("① 端到端：chrome 开着工作网页 → 起表、时钟真的走")
    app.config.screen.focus_minutes = 25
    focus.reset()
    app.pomodoro_bubble.hide_bubble()

    proc["proc"] = "chrome.exe"
    title["title"] = "某电视剧 第01集 - 在线观看"
    before = grabber.calls
    _sample(monitor)
    check("看剧 → 判 watch、照旧 OCR（走 S2 那条）", monitor.title_kind == TITLE_WATCH and grabber.calls > before,
          f"title_kind={monitor.title_kind!r} 截屏 {before} → {grabber.calls}")
    check("看剧不起表", not focus.running, f"running={focus.running}")

    title["title"] = "飞书 - 工作台"
    before, reads_before = grabber.calls, reader.calls
    _sample(monitor)
    check("同一个 chrome 换到飞书 → 判 focus（修复前是空串）",
          monitor.title_kind == TITLE_FOCUS, repr(monitor.title_kind))
    check("工作网页上不截屏（人在干活，不去看人家屏幕）",
          grabber.calls == before and reader.calls == reads_before,
          f"截屏 {before} → {grabber.calls}")
    check("工作网页 → 自动番茄钟跑起来了",
          focus.running and focus.remaining == focus.planned,
          f"running={focus.running} remaining={focus.remaining}/{focus.planned}")
    check("浮窗挂上了", app.pomodoro_bubble.isVisible(), str(app.pomodoro_bubble.isVisible()))

    pump(1.2)
    check("时钟真的在走（不是停在整轮）",
          0 < focus.remaining < focus.planned, f"{focus.remaining}/{focus.planned}")
    kept = focus.remaining

    title["title"] = "爱奇艺-在线视频"
    _sample(monitor)
    check("同一个 chrome 切去刷剧 → 这一轮**结束**（v2-focus-notes2 ①：不再停在暂停）",
          not focus.running and not focus.in_progress and focus.remaining == focus.planned,
          f"running={focus.running} remaining={focus.remaining} 切走时={kept}")
    check("切走时确实跑过一段（不是压根没起表）", 0 < kept < focus.planned, f"{kept}/{focus.planned}")

    # ---------- ② 离开即结束 + 菜单四条固定 ----------

    section("② 切走即结束：浮窗当场收掉、门控解锁，没有「暂停态」这个状态")

    check("不在跑、也没留下进度（in_progress 恒假）", not focus.in_progress, str(focus.in_progress))
    check("浮窗收掉了", not app.pomodoro_bubble.isVisible(), str(app.pomodoro_bubble.isVisible()))
    check("专注门控解锁（主动搭话不再被压着）", app._is_focusing() is False, str(app._is_focusing()))

    section("③ 右键菜单只有四条：工作 / 生活 / 设置 / 退出（不随状态增减）")
    texts = _menu_texts(pet)
    check("桌宠右键菜单正好四条",
          [t for t in texts if t] == ["工作", "生活", "设置", "退出"], str(texts))
    check("专注进行中也不多冒「结束专注」「番茄钟」",
          "结束专注" not in texts and "番茄钟" not in texts, str(texts))
    # 托盘菜单是**每次弹出前**重排的（aboutToShow），所以这里模拟一次"用户点开托盘菜单"。
    app.tray._menu.aboutToShow.emit()
    tray_texts = [a.text() for a in app.tray._menu.actions()]
    check("托盘菜单同款：显示/隐藏 + 四条（没有专注那两条）",
          [t for t in tray_texts if t] == ["隐藏桌宠", "工作", "生活", "设置", "退出"],
          str(tray_texts))

    # 专注进行中，菜单**还是**四条——这才是"不随状态增减"
    focus.start_manual()
    pump(0.05)
    busy_texts = _menu_texts(pet)
    check("专注跑着的时候，菜单仍是那四条",
          [t for t in busy_texts if t] == ["工作", "生活", "设置", "退出"], str(busy_texts))
    app.handle_action("focus")     # 程序化出口（界面上改在番茄钟面板，见下面那一节）
    pump(0.05)
    check("程序化「结束专注」→ 归空闲", not focus.running and not focus.in_progress,
          f"running={focus.running} remaining={focus.remaining}")
    check("浮窗收掉了", not app.pomodoro_bubble.isVisible(), "")

    section("① 手动「开始专注」：认不出来也能测（只存内存）")
    app.handle_action("focus")
    pump(0.05)
    check("点「开始专注」→ 真跑起来", focus.running and focus.manual,
          f"running={focus.running} manual={focus.manual}")
    check("浮窗挂上了", app.pomodoro_bubble.isVisible(), "")

    proc["proc"] = "explorer.exe"
    title["title"] = ""
    _sample(monitor)
    check("手动起的那一轮跟前台脱钩：换成资源管理器也不暂停",
          focus.running, f"running={focus.running} proc={proc['proc']}")
    app.handle_action("focus")
    pump(0.05)
    check("再点一下 → 结束", not focus.running and not focus.manual, f"running={focus.running}")

    section("② focus_enabled 关掉时，跑着的自动番茄钟要真停（原先照跑）")
    proc["proc"] = "chrome.exe"
    title["title"] = "飞书 - 工作台"
    _sample(monitor)
    check("先跑起来", focus.running, f"running={focus.running}")
    app.config.screen.focus_enabled = False
    focus.apply_config()
    check("关开关 + 生效 → 停下来、回整轮", not focus.running and not focus.in_progress,
          f"running={focus.running} remaining={focus.remaining}/{focus.planned}")
    _sample(monitor)
    check("开关关着时前台在专注应用也不起表", not focus.running, f"running={focus.running}")
    app.config.screen.focus_enabled = True
    focus.apply_config()
    check("开关打开后又能起", True, "")

    # ---------- ① 休息：面板真的到得了 ----------

    section("① 菜单能进番茄钟面板（专注 → 短休，休息测得了）")
    app.handle_action("pomodoro")
    pump(0.1)
    check("面板打开了、停在番茄钟页", app.panel.isVisible() and app.panel._current == "pomodoro",
          f"可见={app.panel.isVisible()} 当前页={app.panel._current}")
    panel = app.pomodoro_panel
    labels = [b.text() for b in panel.findChildren(QPushButton)]
    check("面板上三个按钮都在（开始专注 / 重置 / 跳过本阶段）",
          all(any(t in label for label in labels) for t in ("开始专注", "重置", "跳过")),
          str(labels))
    check("空闲时「重置」是灰的（没在跑就没什么可重置）",
          not [b for b in panel.findChildren(QPushButton) if "重置" in b.text()][0].isEnabled(), "")

    timer = app.pomodoro_service.timer
    timer.reset()
    app.config.pomodoro.focus_min = 25
    timer.start_focus()
    pump(0.05)
    check("点「开始专注」→ 手动番茄钟进专注", timer.is_focusing, str(timer.phase))
    timer.skip()
    pump(0.05)
    check("「跳过本阶段」→ 真进得了短休（休息这一档到得了）",
          timer.phase.value in ("short_break", "long_break"), timer.phase.value)
    timer.reset()
    pump(0.05)
    check("「重置」= 退出专注 → 回空闲", timer.phase.value == "idle", timer.phase.value)
    app.panel.close()

    # ---------- 红线 ----------

    section("红线：手动/自动番茄钟都只存内存，两个库一行不多")
    pet_utterance = _rows(app.config.db_path)
    memory_rows = len(app.memory_dao.recent(100))
    fragments = app.mind_store.fragment_count()
    focus.reset()
    focus.start_manual()
    pump(0.2)
    app.handle_action("focus")
    drain_threads()
    check("自动那套真的跑过一轮（前置条件）", not focus.in_progress, "")
    check("pet.db 的 utterance 一行没多", _rows(app.config.db_path) == pet_utterance,
          f"{pet_utterance} → {_rows(app.config.db_path)}")
    check("pet.db 的 memory 一行没多", len(app.memory_dao.recent(100)) == memory_rows, "")
    check("mind.db 一条碎片都没写", app.mind_store.fragment_count() == fragments, "")

    section("红线：零新依赖（都用现成的 Qt 与标准库）")
    browser_src = (ROOT / "life" / "screen" / "browser.py").read_text(encoding="utf-8")
    focus_src = (ROOT / "life" / "screen" / "focus_timer.py").read_text(encoding="utf-8")
    check("browser.py 没引第三方库", "import requests" not in browser_src and "import numpy" not in browser_src, "")
    check("focus_timer.py 还是只用 PySide6 + 标准库",
          "from PySide6" in focus_src and "import requests" not in focus_src, "")

    section("红线：专注设置真落盘（screen 段原先不在 save() 名单里）")
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
        intimacy=app.mind_intimacy,
        memory_book=app.mind_memory_book,
        parent=app.panel,
    )
    try:
        tabs = dialog.findChild(QTabWidget)
        tab_names = [tabs.tabText(i) for i in range(tabs.count())]
        check("番茄钟页还在原位置", tab_names[4] == "番茄钟", str(tab_names))
        check("页面上有那三项（自动专注开关 / 一轮时长 / 工作站点词）",
              hasattr(dialog, "_auto_focus_check") and hasattr(dialog, "_auto_focus_spin")
              and hasattr(dialog, "_focus_words_edit"), "")
        check("回填的是当前配置",
              dialog._auto_focus_check.isChecked() is True
              and dialog._auto_focus_spin.value() == app.config.screen.focus_minutes, "")

        dialog._auto_focus_spin.setValue(33)
        dialog._auto_focus_check.setChecked(True)
        dialog._focus_words_edit.setText("公司OA，工单系统, 数据大屏")
        dialog._save()

        check("内存里的配置改了", app.config.screen.focus_minutes == 33
              and app.config.screen.focus_words == ("公司OA", "工单系统", "数据大屏"),
              f"{app.config.screen.focus_minutes} / {app.config.screen.focus_words}")
        on_disk = yaml.safe_load(box.config_path.read_text(encoding="utf-8")) or {}
        screen_raw = on_disk.get("screen") or {}
        check("config.yaml 里真写进去了（save() 原先没有 screen 这一段）",
              screen_raw.get("focus_minutes") == 33, str(screen_raw.get("focus_minutes")))
        check("词表以列表形态落盘（元组 yaml 存不下，得转一道）",
              screen_raw.get("focus_words") == ["公司OA", "工单系统", "数据大屏"],
              str(screen_raw.get("focus_words")))
        from core.config import Config
        reloaded = Config.load(box.config_path)
        check("重启读回来还是那些（持久）",
              reloaded.screen.focus_minutes == 33
              and tuple(reloaded.screen.focus_words) == ("公司OA", "工单系统", "数据大屏"),
              f"{reloaded.screen.focus_minutes} / {reloaded.screen.focus_words}")
    finally:
        dialog.reject()

    drain_threads()
    box.cleanup()
    return summary("专注/番茄钟（v2-focus-web）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
