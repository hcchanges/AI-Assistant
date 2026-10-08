"""
番茄钟专注交互修正（v2-focus-interact）的自动测试。

真机模块⑤「番茄钟与专注」反馈两点：

① **专注期点击无反应（f7）**：专注态下点宠物不该有任何反应——不弹对话、不冒话、
   不扯嘴巴、不算"被碰过"。**拖动不在此列**（宠物压在屏幕上时得挪得走）。
② **右键菜单干净（f8）**：专注那两条（「结束专注」/「番茄钟」）当时只在专注时才露，
   时钟不走了就不该再出现在菜单上。
   （**v2-focus-notes2 ③ 之后更进一步**：用户要的是"右键菜单只留 工作/生活/设置/退出，
   不要多出按键（专注同样）"——那两条整个摘掉了，不管在不在专注，菜单都是那四条。
   本节断言跟着改成"四条固定、专注时也不多冒"。）

S0 查出来的实况（修复前）：

- 点击那条路上**一个专注判断都没有**：`ui/pet_window.py` 的 `mouseReleaseEvent`
  单击直接 `chat_requested.emit()` → `open_chat()`；顺带还有两个口子
  （悬停留够冒「？」、按脸进扯嘴巴），也都没挡。
- 菜单那两条是 `PetWindow._build_menu` / `PetTray._rebuild_menu` 里**无条件
  addAction** 的：文案跟着状态变，但**条目一直在**，退出专注之后还杵在那儿。

判据（S0 时定的口径，这里逐条钉住）：

- 专注态 = `Bootstrap._is_focusing()`（手动番茄钟在专注 **或** 自动那套在跑）——
  项目里"正在专注"的既有定义，不另起一套，本改只多了一个调用点。
- 菜单条目 = **四条固定**（工作/生活/设置/退出）。v2-focus-interact 那一轮用的是
  `Bootstrap._screen_focus_in_progress()` 按状态增减，v2-focus-notes2 ③ 把那条路
  整个拿掉了（连 `_screen_focus_in_progress` 与 `focus_menu_text` 一起删），
  所以现在的判据是"不管什么状态，都正好四条"。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# 公开版：菜单里工作模块那一项写的是锁着的说法（ui/pet_window.py / ui/tray.py）
LOCKED_WORK = "🔒 工作模块（待解锁）"

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent
from PySide6.QtWidgets import QPushButton

from life.screen import monitor as mon

ROOT = Path(__file__).resolve().parents[1]

# 专注相关的那几个菜单条目（「开始专注」是空闲态的老写法，一并算进来）：
# v2-focus-notes2 ③ 之后**任何状态下一个都不该出现**。
FOCUS_ITEMS = ("开始专注", "结束专注", "番茄钟")


# ---------- 假前台（省掉真 OCR，跟 test_focus_web 同一套路） ----------


class FakeReader:
    def __init__(self, text: str = "") -> None:
        self.text = text

    def read_array(self, array) -> str:
        return self.text


class FakeGrabber:
    def __call__(self, rect=None) -> QImage:
        return QImage(8, 8, QImage.Format_RGB32)


# ---------- 小工具 ----------


def _mouse(kind, local, global_, button, buttons) -> QMouseEvent:
    return QMouseEvent(
        kind, QPointF(*local), QPointF(*global_), button, buttons, Qt.NoModifier
    )


def _press(pet, local=(40, 40)) -> None:
    pet.mousePressEvent(
        _mouse(
            QEvent.MouseButtonPress,
            local,
            (local[0] + pet.x(), local[1] + pet.y()),
            Qt.LeftButton,
            Qt.LeftButton,
        )
    )


def _release(pet, local=(40, 40)) -> None:
    pet.mouseReleaseEvent(
        _mouse(
            QEvent.MouseButtonRelease,
            local,
            (local[0] + pet.x(), local[1] + pet.y()),
            Qt.LeftButton,
            Qt.NoButton,
        )
    )


def _click(pet, local=(40, 40)) -> None:
    """真发一对按下/松开（走的是 mousePressEvent / mouseReleaseEvent 本体）。"""
    _press(pet, local)
    _release(pet, local)


def _plain_click(pet, local=(40, 40)) -> None:
    """单击那条路，把命中判定钉成"没按在脸上"。

    (40, 40) 算不算"脸"是**跟着姿势变的**（实测：`sit_happy` 时不算、拖过一次之后
    变成 `jump_excited` 就算了）。这里要单独验的是"弹不弹对话"，按脸那条路
    （进「扯嘴巴」）由后面那两条检查单独验——不钉的话，姿势一变这条就被抢走，
    测的就成了另一条路。
    """
    real = pet._face_hit
    pet._face_hit = lambda _pos: False
    try:
        _click(pet, local)
    finally:
        pet._face_hit = real


def _drag(pet, start=(40, 40), delta=(60, 25)) -> None:
    """真发 按下 → 拖动 → 松开（拖动这条路必须一直是通的）。"""
    _press(pet, start)
    target = (start[0] + delta[0], start[1] + delta[1])
    pet.mouseMoveEvent(
        _mouse(
            QEvent.MouseMove,
            target,
            (target[0] + pet.x(), target[1] + pet.y()),
            Qt.NoButton,
            Qt.LeftButton,
        )
    )
    pet.mouseReleaseEvent(
        _mouse(
            QEvent.MouseButtonRelease,
            target,
            (target[0] + pet.x(), target[1] + pet.y()),
            Qt.LeftButton,
            Qt.NoButton,
        )
    )


def _settle(pet) -> None:
    """拖完松手会进 Q 弹物理（故意留着不拦）；这里把它按回静止，免得影响后面几项。"""
    pet._settled = True
    pet._vy = 0.0
    pet._sx = pet._sy = 1.0
    pet._squash_t = 0.0
    pet._mouth_pull = 0.0
    pet._pull_velocity = 0.0
    pet._anger_until = 0.0
    pet._set_physics_mode(False)


def _menu_rows(menu) -> list[str]:
    """菜单上的条目，分隔符写成空串（方便看有没有连着两条）。"""
    return ["" if a.isSeparator() else a.text() for a in menu.actions()]


def _pet_menu(pet) -> list[str]:
    return [t for t in _menu_rows(pet._build_menu()) if t]


def _tray_menu(app) -> list[str]:
    # 托盘菜单是**每次弹出前**重排的（aboutToShow），模拟一次"用户点开托盘菜单"
    app.tray._menu.aboutToShow.emit()
    return [t for t in _menu_rows(app.tray._menu) if t]


def _no_focus_items(texts: list[str]) -> bool:
    return not any(item in texts for item in FOCUS_ITEMS)


def _no_double_sep(rows: list[str]) -> bool:
    """首尾不是分隔符，且没有连着两条（条件渲染最容易在这儿留下空档）。"""
    if not rows or rows[0] == "" or rows[-1] == "":
        return False
    return all(not (rows[i] == "" and rows[i + 1] == "") for i in range(len(rows) - 1))


def _import_roots(path: Path) -> set[str]:
    """这个文件顶层 import 了哪些模块根（用来验"零新依赖"）。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def _count(table: str, db_path) -> int:
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


def main() -> int:
    box, _stub, app = make_env()
    pet = app.pet_window
    bubble = pet._bubble
    focus = app.screen_focus_timer          # 自动那套（菜单判据看它）
    manual = app.pomodoro_service.timer     # 手动那套（面板里点起来的）
    monitor = app.screen_monitor
    pet.show()
    pump(0.05)
    pet._settled = True
    app.config.screen.focus_minutes = 25
    app.config.screen.scene_stable_samples = 2
    monitor._reader = FakeReader()
    monitor._grabber = FakeGrabber()

    chats: list[int] = []
    pet.chat_requested.connect(lambda: chats.append(1))
    touches: list[int] = []
    pet.touched.connect(lambda: touches.append(1))

    def idle() -> None:
        """回到空闲：两套番茄钟都停、聊天框收掉、气泡收掉、计数清零。"""
        focus.reset()
        manual.reset()
        # 不带淡出：带淡出的话 isVisible() 还得再等 260ms 才翻假，下一项
        # （"这会儿能不能冒浮动气泡"）就会读到一个正在收的聊天框。
        app.chat_bubble.close_bubble(fade=False)
        pet.hide_speech_bubble()
        _settle(pet)
        chats.clear()
        touches.clear()
        pump(0.02)

    def focus_on() -> None:
        """进专注态（自动那套跑起来）。起来时会主动说一句开场词，这里不拦。"""
        focus.enter("chrome.exe")
        pump(0.02)

    # ---------- ① 专注期点击不搭理 ----------

    section("① 专注期点击：不弹对话、不冒话、不扯嘴巴（拖动照旧）")

    idle()
    _plain_click(pet, (40, 40))
    pump(0.05)
    check("前置对照：空闲态点一下 → 真会弹对话（说明下面验的是同一条路）",
          bool(chats), f"chat_requested {len(chats)} 次")

    idle()
    focus_on()
    check("专注态确实是专注态（判据由装配层给，不在界面上另起一套）",
          app._is_focusing() and pet._focus_silenced(),
          f"_is_focusing={app._is_focusing()}")
    _plain_click(pet, (40, 40))
    pump(0.05)
    check("专注态点一下 → 不弹对话",
          chats == [], f"chat_requested {len(chats)} 次")

    # 悬停留够那一下（冒「？」）走的是同一个槽
    idle()
    pet.hide_speech_bubble()
    pet._on_hover_timeout()
    pump(0.02)
    hover_free = bubble.isVisible()
    idle()
    focus_on()
    pet.hide_speech_bubble()     # 开场词会占住气泡；先把"正在说正文"那笔账清掉
    pet._on_hover_timeout()
    pump(0.02)
    check("前置对照：空闲态悬停到点 → 真会冒话（对照）",
          hover_free, f"空闲态气泡可见={hover_free}")
    check("专注态悬停到点 → 不冒话",
          not bubble.isVisible(), f"专注态气泡可见={bubble.isVisible()}")

    # 按脸进"扯嘴巴"：把命中判定固定成"按哪儿都算按脸"，单独验那道专注闸
    idle()
    real_face_hit = pet._face_hit
    pet._face_hit = lambda _pos: True
    try:
        _press(pet, (40, 40))
        free_grabbed = pet._mouth_grabbed
        _release(pet, (40, 40))
        _settle(pet)

        focus_on()
        _press(pet, (40, 40))
        focus_grabbed = pet._mouth_grabbed
        _release(pet, (40, 40))
        _settle(pet)
    finally:
        pet._face_hit = real_face_hit
    check("前置对照：空闲态按脸 → 真进「扯嘴巴」",
          free_grabbed is True, f"空闲态 grabbed={free_grabbed}")
    check("专注态按脸 → 不进「扯嘴巴」（松手也哼不出那句「别扯啦」）",
          focus_grabbed is False, f"专注态 grabbed={focus_grabbed}")

    # "被碰过"那笔账（久未互动计时 / 状态机）不该在专注期被记
    idle()
    _plain_click(pet, (40, 40))
    idle_touches = len(touches)
    idle()
    focus_on()
    _plain_click(pet, (40, 40))
    pump(0.05)
    check("专注态点一下 → 不算「被碰过」（久未互动计时不重置）",
          len(touches) == 0, f"空闲态 {idle_touches} 次 / 专注态 {len(touches)} 次")

    # 拖动必须一直是通的：宠物压在屏幕上时得挪得走
    idle()
    focus_on()
    pet._face_hit = lambda _pos: False      # 只验拖动这条路，别让按脸抢了
    try:
        before = (pet.x(), pet.y())
        _drag(pet, start=(40, 40), delta=(60, 25))
        pump(0.05)
    finally:
        pet._face_hit = real_face_hit
    after = (pet.x(), pet.y())
    _settle(pet)
    check("专注态**拖得动**（拦的是搭理，不是挪开）",
          after != before, f"{before} → {after}")

    idle()
    _plain_click(pet, (40, 40))
    pump(0.05)
    check("退出专注后点一下 → 又正常弹对话（屏蔽没粘住）",
          bool(chats), f"chat_requested {len(chats)} 次")

    # 手动那套（面板里点「开始专注」起的）也得算专注态
    idle()
    manual.start_focus()
    pump(0.05)
    _plain_click(pet, (40, 40))
    pump(0.05)
    check("手动番茄钟在专注时也算：点一下同样不弹对话",
          manual.is_focusing and chats == [],
          f"手动 is_focusing={manual.is_focusing} chat={len(chats)}")
    idle()

    # ---------- ② 菜单不再随专注状态增减 ----------

    section("② 右键菜单：四条固定，专注时不额外冒条目")

    idle()
    pet_rows = _menu_rows(pet._build_menu())
    pet_texts = [t for t in pet_rows if t]
    check("空闲态：菜单上没有「开始专注」「结束专注」「番茄钟」任意一条",
          _no_focus_items(pet_texts), str(pet_texts))
    # v2-chat-core #1 把「语音输入（按住说话）」从两条菜单里摘了，所以这条不再要求它。
    check("空闲态：菜单正好四条（工作/生活/设置/退出），一条不多一条不少",
          pet_texts == [LOCKED_WORK, "生活", "设置", "退出"], str(pet_texts))
    check("空闲态：没留下连着两条分隔符的空档（条件渲染最容易在这儿露馅）",
          _no_double_sep(pet_rows), str(pet_rows))
    tray_rows = _menu_rows(app.tray._menu)
    check("空闲态：托盘菜单也同款（一边隐一边显就不同款了）",
          _no_focus_items([t for t in tray_rows if t]) and _no_double_sep(tray_rows),
          str(tray_rows))

    focus_on()
    busy_texts = _pet_menu(pet)
    check("专注态：菜单**还是**那四条，不因为「在专注」就多冒两条（v2-focus-notes2 ③）",
          busy_texts == [LOCKED_WORK, "生活", "设置", "退出"], str(busy_texts))
    busy_tray = _tray_menu(app)
    check("托盘同款：专注时也只有 显示/隐藏 + 四条",
          [t for t in busy_tray if t] == ["隐藏桌宠", LOCKED_WORK, "生活", "设置", "退出"],
          str(busy_tray))

    # 切走 → **这一轮就结束了**（v2-focus-notes2 ①）。原先这里是"暂停、两条仍要在"，
    # 那个暂停态被用户点名去掉了：浮窗不该留在屏幕上没出口。
    pump(1.1)                     # 先让时钟真走掉一点，否则跟"没开始过"分不开
    kept = focus.remaining
    focus.leave()
    pump(0.02)
    left_texts = _pet_menu(pet)
    check("切走 → 不再有「暂停态」：回空闲、进度清零、菜单照旧四条",
          (not focus.running) and (not focus.in_progress)
          and focus.remaining == focus.planned
          and left_texts == [LOCKED_WORK, "生活", "设置", "退出"],
          f"running={focus.running} in_progress={focus.in_progress} "
          f"remaining={focus.remaining}/{focus.planned} 切走时={kept}")

    # 上面 leave() 已经把这一轮结束了，这里直接看菜单——不再靠"点一下菜单收尾"。
    after_rows = _menu_rows(pet._build_menu())
    after_texts = [t for t in after_rows if t]
    check("退出专注后：还是没有专注条目（f8 要的就是这个）",
          _no_focus_items(after_texts), str(after_texts))
    check("退出专注后：菜单没有留下空档",
          _no_double_sep(after_rows), str(after_rows))
    check("托盘那边也同步没有",
          _no_focus_items(_tray_menu(app)), str(_tray_menu(app)))

    # ---------- 回归：v2-focus-web 那几件事，一件都不能坏 ----------

    section("回归：自动启停照旧（切走即结束、再进来重新起一轮）")

    idle()
    focus_on()
    check("自动番茄钟还起得来（进专注应用 → 跑）", focus.running, f"running={focus.running}")
    pump(1.1)
    kept = focus.remaining
    focus.leave()
    check("切走 → 这一轮结束、回整轮（v2-focus-notes2 ①：不再留住剩余）",
          (not focus.running) and (not focus.in_progress)
          and focus.remaining == focus.planned and kept < focus.planned,
          f"remaining={focus.remaining}/{focus.planned} 切走时={kept}")
    focus.enter("chrome.exe")
    check("再切回来 → 重新起一轮（从整轮开始，不是接着剩的）",
          focus.running and focus.remaining == focus.planned,
          f"running={focus.running} remaining={focus.remaining}/{focus.planned}")

    idle()
    app.handle_action("focus")
    pump(0.02)
    check("程序化那一下还能手动起一轮（前台认不出来的场景仍测得动）",
          focus.running and focus.manual, f"running={focus.running} manual={focus.manual}")
    app.handle_action("focus")
    pump(0.02)
    check("再来一下 → 结束、跟前台脱钩的那一轮也作废",
          (not focus.running) and (not focus.manual), f"running={focus.running}")

    section("回归：工作网页照旧认得出、非工作网页照旧不抢")

    proc = {"proc": "chrome.exe"}
    title = {"title": ""}
    mon.foreground.foreground_pid = lambda: 1
    monitor._pids.name_of = lambda pid: proc["proc"]
    mon.foreground.foreground_title = lambda: title["title"]

    def _sample() -> None:
        monitor.sample()
        wait_for(lambda: not monitor._reading, timeout=5.0)
        pump(0.02)

    idle()
    title["title"] = "飞书 - 工作台"
    _sample()
    check("工作网页（飞书）→ 自动番茄钟起表",
          focus.running, f"running={focus.running} title_kind={monitor.title_kind!r}")
    title["title"] = "爱奇艺 - 电视剧"
    _sample()
    check("剧集页面 → 不抢成番茄钟（照旧走刷剧那条）",
          (not focus.running) and monitor.title_kind == "watch",
          f"running={focus.running} title_kind={monitor.title_kind!r}")

    idle()
    app.config.screen.focus_enabled = False
    title["title"] = "飞书 - 工作台"
    _sample()
    check("focus_enabled 关掉时 → 前台在专注应用也不起表",
          not focus.running, f"running={focus.running}")

    section("回归：番茄钟面板（休息那条路）照旧到得了")

    idle()
    proc["proc"] = "explorer.exe"
    title["title"] = ""
    _sample()
    check("换成资源管理器 → 不抢表", not focus.running, f"running={focus.running}")

    app.config.screen.focus_enabled = True
    app.config.screen.enabled = True
    focus.apply_config()
    app.handle_action("pomodoro")
    pump(0.1)
    panel = app.pomodoro_panel
    labels = [b.text() for b in panel.findChildren(QPushButton)]
    check("面板三个按钮都在（开始专注 / 重置 / 跳过本阶段）",
          all(any(t in lb for lb in labels) for t in ("开始专注", "重置", "跳过")),
          str(labels))
    manual.reset()
    pump(0.05)
    [b for b in panel.findChildren(QPushButton) if "开始专注" in b.text()][0].click()
    pump(0.05)
    check("点「开始专注」→ 手动番茄钟进专注", manual.is_focusing, str(manual.phase))
    [b for b in panel.findChildren(QPushButton) if "跳过" in b.text()][0].click()
    pump(0.05)
    check("点「跳过本阶段」→ 真进得了休息（休息这一档到得了）",
          manual.phase.value in ("short_break", "long_break"), manual.phase.value)
    [b for b in panel.findChildren(QPushButton) if "重置" in b.text()][0].click()
    pump(0.05)
    check("点「重置」= 退出专注 → 回空闲", manual.phase.value == "idle", manual.phase.value)
    app.panel.close()

    # ---------- 红线 ----------

    section("红线：这一改只读状态，不落库、不加依赖")

    app.config.screen.focus_enabled = True
    idle()
    utterance = _count("utterance", app.config.db_path)
    memory = _count("memory", app.config.db_path)
    focus_on()
    for _ in range(3):
        _plain_click(pet, (40, 40))
    pet._on_hover_timeout()
    pet._build_menu()
    _tray_menu(app)
    pump(0.05)
    _settle(pet)
    check("点/悬停/建菜单这几下：pet.db 的 utterance 一行没多",
          _count("utterance", app.config.db_path) == utterance,
          f"{utterance} → {_count('utterance', app.config.db_path)}")
    check("pet.db 的 memory 一行没多",
          _count("memory", app.config.db_path) == memory,
          f"{memory} → {_count('memory', app.config.db_path)}")

    allowed = set(sys.stdlib_module_names) | {"PySide6"} | {
        p.name for p in ROOT.iterdir() if p.is_dir() and not p.name.startswith(".")
    }
    for rel in ("ui/pet_window.py", "ui/tray.py"):
        roots = _import_roots(ROOT / rel)
        check(f"{rel} 的 import 全是标准库 + PySide6 + 本项目（零新依赖）",
              roots <= allowed, str(sorted(roots - allowed)))

    pet_src = (ROOT / "ui" / "pet_window.py").read_text(encoding="utf-8")
    tray_src = (ROOT / "ui" / "tray.py").read_text(encoding="utf-8")
    check("屏蔽判据只读一次、没在窗口里另存一份状态",
          pet_src.count("self._focusing") == 2, str(pet_src.count("self._focusing")))
    check("四处「搭理」都被同一道闸挡住（单击 / 悬停 / 按脸 / 那笔账）",
          pet_src.count("self._focus_silenced()") == 4,
          str(pet_src.count("self._focus_silenced()")))
    check("两道菜单的专注条目彻底摘干净了（v2-focus-notes2 ③）",
          "focus_menu_text" not in pet_src and "focus_menu_text" not in tray_src
          and "_focus_state" not in pet_src and "_focus_state" not in tray_src,
          f"pet={pet_src.count('focus_menu_text')}/tray={tray_src.count('focus_menu_text')}")

    idle()
    drain_threads()
    box.cleanup()
    return summary("番茄钟专注交互（v2-focus-interact）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
