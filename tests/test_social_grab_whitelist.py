"""社交工具白名单（v2-social-grab-whitelist）的自动测试。

背景：前台根本不是微信时（未登录微信、'ima.copilot' 之类的窗口在最前面），社交帮聊
还是触发、还是抓了一屏，冒出来的候选里是**别的窗口**的字。根因是"算不算社交前台"
这件事认的是**分类表**（`is_social_proc`）——那张表里除了微信还躺着 QQ / Telegram /
Discord 一串，而且**进程名读不出时放行**（"宁可漏挡"），于是"不知道是什么的那个前台
窗口"也会被当聊天窗采下来。

改法：单立一份白名单 `SOCIAL_ALLOWED_PROCS = {"weixin.exe", "wechat.exe"}`（用集合判断，
将来多支持一个工具只加一条）。**抓取侧那道硬闸**改成
`if not proc_name or proc_name.lower() not in SOCIAL_ALLOWED_PROCS: return None`——空名不抓
（口径由"宁可漏挡"翻成"宁可漏采"）+ 非白名单进程不抓。**触发侧**（`on_foreground`）
也跟着认白名单，不然前台站着 QQ 时会冒出一句"要不要我帮你回"却永远抓不到内容；冷启动
那道"可见窗口兜底"**没砍**（`_prime_social_foreground` 仍然会拿可见的微信窗把
`_social_foreground` 置真）。`is_social_proc` 留着没删，仍答"分类表说它算不算社交"，
只是不再管触发。

红线：零新依赖；不碰默认 personal 口径；不碰 `_running` / `_awaiting` 状态机；不写库；
不碰 config.yaml / .vscode；不改时钟；不模拟按键。
"""

from __future__ import annotations

import inspect
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, pump, section, summary

from life.screen import shot as shot_mod
from life.screen import social as smod
from life.screen.social import (
    SOCIAL_ALLOWED_PROCS,
    SocialReplyService,
    is_allowed_social_proc,
)

# 名单里那两个都是微信（4.x 与老版），大小写照认；分类表说"社交"但不支持的那些。
ALLOWED_NAMES = ("Weixin.exe", "weixin.exe", "WEIXIN.EXE", "WeChat.exe", "wechat.exe", "WeChat.EXE")
BANNED_NAMES = (
    "ima.copilot.exe",          # 真机肇事者：腾讯 ima，分类表里是「工作」
    "chrome.exe", "msedge.exe", "Code.exe", "devenv.exe", "notepad.exe", "explorer.exe",
    "qq.exe", "QQ.exe", "QQNT.exe", "Telegram.exe", "discord.exe", "whatsapp.exe", "skype.exe",
)


def main() -> int:
    box, stub, app = make_env()
    app.config.screen.enabled = True
    app.config.screen.social_enabled = True
    app.config.screen.ocr_enabled = True
    now = {"t": 10_000.0}

    # 红线：整节不写库——记下开工前两个库的读数，收工再对一遍
    import sqlite3

    def _rows(db_path: str, table: str) -> int:
        conn = sqlite3.connect(db_path)
        try:
            return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
        finally:
            conn.close()

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    def _new_service() -> SocialReplyService:
        return SocialReplyService(
            config=app.config, bus=app.bus, clock=lambda: now["t"],
            idle_seconds=lambda: 99.0,
        )

    def _offer_counter() -> list[str]:
        seen: list[str] = []
        app.bus.social_offer.connect(seen.append)
        return seen

    # ---------- ① 名单本体 ----------

    section("① 白名单本体：只有微信（两个 exe 名）在名单里")

    for name in ALLOWED_NAMES:
        check(f"「{name}」在名单里 → True", is_allowed_social_proc(name) is True, "")
    for name in BANNED_NAMES:
        check(f"「{name}」不在名单里 → False", is_allowed_social_proc(name) is False, "")

    check("两端空白照认（`strip()` 过一道）",
          is_allowed_social_proc("  Weixin.exe\n") is True, "")
    check("空串不算（认不出是谁 = 不支持）", is_allowed_social_proc("") is False, "")
    check("None 不算（不抛）", is_allowed_social_proc(None) is False, "")
    check("纯空白不算", is_allowed_social_proc("   ") is False, "")

    check("`SOCIAL_ALLOWED_PROCS` 是集合（留扩展口，不是写死单值比较）",
          isinstance(smod.SOCIAL_ALLOWED_PROCS, (set, frozenset))
          and "weixin.exe" in smod.SOCIAL_ALLOWED_PROCS, str(smod.SOCIAL_ALLOWED_PROCS))

    # 两层分开：分类表那层一个字没动，QQ 照样"算社交"，但**不支持**
    check("分类表那层没动：`is_social_proc('QQ.exe')` 仍是 True",
          smod.is_social_proc("QQ.exe") is True, "")
    check("★ 分层：QQ 分类表里算社交、白名单里不算（触发只认白名单）",
          smod.is_social_proc("QQ.exe") is True
          and is_allowed_social_proc("QQ.exe") is False, "")
    check("分类表那层没动：微信仍认得出", smod.is_social_proc("Weixin.exe") is True, "")

    # ---------- ② 触发那一层：on_foreground ----------

    section("② 触发：前台不在名单里就不置 `_social_foreground`，一句也不冒")

    svc_a = _new_service()
    offers_a = _offer_counter()
    svc_a.on_foreground("ima.copilot.exe")
    check("★ 前台是 ima → `_social_foreground` 仍是 False（整条线不动）",
          svc_a._social_foreground is False, str(svc_a._social_foreground))
    check("★ 前台是 ima → 一句 offer 都不冒", offers_a == [], f"{len(offers_a)} 次")
    svc_a.on_foreground("Weixin.exe")
    check("★ 从 ima 真的切到微信 → 才置 True、才冒这一句",
          svc_a._social_foreground is True and len(offers_a) == 1, f"{len(offers_a)} 次")
    app.bus.social_offer.disconnect(offers_a.append)

    svc_b = _new_service()
    offers_b = _offer_counter()
    svc_b.on_foreground("QQ.exe")
    check("★ 前台是 QQ（分类表里算社交、可榜单里没有）→ 不置 True、不冒",
          svc_b._social_foreground is False and offers_b == [], f"{len(offers_b)} 次")
    app.bus.social_offer.disconnect(offers_b.append)

    svc_c = _new_service()
    offers_c = _offer_counter()
    svc_c.on_foreground("WeChat.exe")
    check("老版微信 `WeChat.exe` 照旧触发", svc_c._social_foreground is True and len(offers_c) == 1,
          f"{len(offers_c)} 次")
    app.bus.social_offer.disconnect(offers_c.append)

    svc_d = _new_service()
    svc_d.on_foreground("explorer.exe")
    check("普通窗口（explorer）→ 不置 True", svc_d._social_foreground is False, "")

    # ---------- ③ 冷启动：焦点 / 可见窗口两问 ----------

    section("③ 冷启动：焦点答不出时枚举桌面，也只认名单里那一档")

    monitor = app.screen_monitor
    real_now, real_visible = monitor.foreground_now, monitor.visible_procs
    real_service = app.social_reply
    try:
        # 枚举那一条腿：分类表说"社交"的 QQ 不再被挑中
        monitor.visible_procs = lambda: ["explorer.exe", "QQ.exe"]
        check("★ 桌面上可见的只有 QQ（不在名单里）→ 挑不出任何东西",
              app._visible_social_proc(monitor) == "", repr(app._visible_social_proc(monitor)))
        monitor.visible_procs = lambda: ["QQ.exe", "WeChat.exe"]
        check("同一份可见表里只要有微信 → 挑得出它",
              app._visible_social_proc(monitor) == "WeChat.exe",
              repr(app._visible_social_proc(monitor)))
        monitor.visible_procs = lambda: ["explorer.exe", "chrome.exe", "Code.exe"]
        check("一个可见的微信都没有 → 空串",
              app._visible_social_proc(monitor) == "", "")

        # 焦点 + 枚举合起来那一问
        probe = _new_service()
        app.social_reply = probe
        monitor.foreground_now = lambda: "ima.copilot.exe"
        monitor.visible_procs = lambda: ["explorer.exe", "chrome.exe", "Code.exe"]
        probe._social_foreground = True          # 先立着，看这一问会不会把它扳回 False
        app._prime_social_foreground()
        check("★ 焦点是 ima、桌面上也没有可见的微信 → 冷启动认不下来（不置 True）",
              probe._social_foreground is False, str(probe._social_foreground))

        probe2 = _new_service()
        app.social_reply = probe2
        monitor.visible_procs = lambda: ["explorer.exe", "Weixin.exe"]
        app._prime_social_foreground()
        check("★ 焦点是 ima、但桌面上有可见的微信 → 照认（老口径救回来的那一档没坏）",
              probe2._social_foreground is True, str(probe2._social_foreground))

        probe3 = _new_service()
        app.social_reply = probe3
        monitor.visible_procs = lambda: ["QQ.exe"]
        app._prime_social_foreground()
        check("★ 焦点是 ima、可见的只有 QQ → 认不下来（枚举那一条腿也认白名单）",
              probe3._social_foreground is False, str(probe3._social_foreground))

        monitor.foreground_now = lambda: "Weixin.exe"
        probe4 = _new_service()
        app.social_reply = probe4
        app._prime_social_foreground()
        check("焦点本来就是微信 → 老口径一个字没变（照认）",
              probe4._social_foreground is True, str(probe4._social_foreground))
    finally:
        app.social_reply = real_service
        monitor.foreground_now, monitor.visible_procs = real_now, real_visible

    # ---------- ④ 抓屏那道闸 ----------

    section("④ 抓屏：前台不在名单里（含**进程名读不出**）一个像素都不采")

    from monitor import foreground as fg_mod
    from PySide6.QtGui import QImage

    # 掩码是现问"哪些窗可见"算出来的，宠物窗得真显示出来（跟 test_social 一样）
    app.pet_window.show()
    app.chat_bubble.show()
    pump(0.05)

    real_title, real_pid = fg_mod.foreground_title, fg_mod.foreground_pid
    real_grab = shot_mod.grab_foreground_window
    real_name_of = app._proc_name_of
    grabbed: list = []

    def _fake_grab(pet_rect=None, window_rect=None, extra_masks=None):
        grabbed.append(extra_masks)
        image = QImage(1000, 800, QImage.Format_RGB32)
        image.fill(0xFFFFFF)
        return image

    def _set_name(name):
        """把"这一拍前台是谁"钉住——`_grab_social_chat` 唯一读进程名的地方就是它。"""
        app._proc_name_of = lambda pid: (name or "")

    try:
        shot_mod.grab_foreground_window = _fake_grab
        fg_mod.foreground_title = lambda: "微信"      # 不是宠物自己
        fg_mod.foreground_pid = lambda: 4242

        for name in ("ima.copilot.exe", "chrome.exe", "Code.exe", "explorer.exe", "QQ.exe"):
            _set_name(name)
            grabbed.clear()
            check(f"前台是「{name}」→ 不采（连抓都不抓）",
                  app._grab_social_chat() is None and not grabbed, f"抓了 {len(grabbed)} 次")

        # ★ 口径收严的那一格：进程名读不出，老口径放行、新口径不采
        _set_name(None)
        grabbed.clear()
        check("★ 进程名读不出（空串）→ **不采**（老口径是放行，这是本次翻的那一格）",
              app._grab_social_chat() is None and not grabbed, f"抓了 {len(grabbed)} 次")

        for name in ("Weixin.exe", "wechat.exe"):
            _set_name(name)
            grabbed.clear()
            image = app._grab_social_chat()
            check(f"前台是「{name}」→ 照采（白名单不误挡真微信）",
                  bool(grabbed) and image is not None, f"抓了 {len(grabbed)} 次")
    finally:
        fg_mod.foreground_title, fg_mod.foreground_pid = real_title, real_pid
        shot_mod.grab_foreground_window = real_grab
        app._proc_name_of = real_name_of

    # ---------- ⑤ 顺带对齐的两处（话头 / 卡片归属） ----------

    section("⑤ 顺带对齐：话头仲裁与卡片归属也认白名单（不然会'不说话却占着话头'）")

    real_service5 = app.social_reply
    real_proc = monitor._proc
    try:
        probe5 = _new_service()
        app.social_reply = probe5
        check("前置：刚建的服务手上没端着东西（`pending_offer` False）",
              probe5.pending_offer is False, str(probe5.pending_offer))
        monitor._proc = "QQ.exe"
        check("★ 前台是 QQ（不支持）→ 不再算'聊天窗口说了算'（别压着场景那条线）",
              app._social_holds_floor() is False, "")
        monitor._proc = "Weixin.exe"
        check("前台是微信 → 照旧说了算（老口径没坏）",
              app._social_holds_floor() is True, "")
    finally:
        monitor._proc = real_proc
        app.social_reply = real_service5

    # ---------- ⑥ 红线（源码层） ----------

    section("⑥ 红线：分类表那层没删 / 判据是集合不是 == / 状态机与时钟没动")

    allow_src = inspect.getsource(smod.is_allowed_social_proc)
    check("判据走集合成员，不是写死比较：源码里有 `in SOCIAL_ALLOWED_PROCS`",
          "in SOCIAL_ALLOWED_PROCS" in allow_src, "")
    check("没写成 `== \"Weixin.exe\"` 那种硬编码",
          "==" not in allow_src.replace("!=", ""), allow_src.strip().splitlines()[-1])

    check("`is_social_proc` 仍在、仍走分类表（`resolve_category` 还在里面）",
          "resolve_category" in inspect.getsource(smod.is_social_proc), "")

    on_fg_src = inspect.getsource(smod.SocialReplyService.on_foreground)
    check("触发那一层换成了白名单判据",
          "is_allowed_social_proc(proc)" in on_fg_src, "")
    check("触发那一层不再拿 `is_social_proc` 定 `_social_foreground`",
          "is_social_proc(proc)" not in on_fg_src, "")

    grab_src = inspect.getsource(app._grab_social_chat)
    check("抓屏硬闸：空名不抓 + 非白名单不抓，且**用集合判断**（不是 `==`）",
          "if not proc_name or proc_name.lower() not in SOCIAL_ALLOWED_PROCS:" in grab_src, "")
    check("老口径那半句（`proc_name and not …`，空名放行）已经不在了",
          "proc_name and not" not in grab_src, "")

    check("默认 personal 口径没动：`classify_chat_page` 末尾仍是 `return PAGE_PERSONAL`",
          inspect.getsource(smod.classify_chat_page).rstrip().endswith("return PAGE_PERSONAL"), "")

    for tok in ("_running", "_awaiting", "monotonic", "time.", "clock("):
        check(f"is_allowed_social_proc 不碰 `{tok}`（状态机 / 时钟口径没动）",
              tok not in allow_src, "")
    # `on_foreground` 的**老注释**里本来就提到 `_awaiting`（"不清的话它会一直挂着"），
    # 所以这一处别看"出现过没有"，看**有没有给它赋值**——那才是碰状态机。
    check("`on_foreground` 不碰状态机：没给 `_running` / `_awaiting` 赋值",
          "_running =" not in on_fg_src and "_awaiting =" not in on_fg_src, "")
    for tok in ("monotonic", "time.", "clock("):
        check(f"`on_foreground` 不碰 `{tok}`（时钟口径没动）", tok not in on_fg_src, "")

    # 零新依赖：本模块顶层 import 集合没多出第三方库（判据只用内建集合与字符串方法）
    check("零新依赖：`social.py` 顶层没有为新判据新进任何第三方库",
          "import psutil" not in inspect.getsource(smod)
          and "from PIL" not in inspect.getsource(smod), "")

    # 不写库：这一节只在临时沙箱里构造服务，不碰 pet.db / mind.db
    check("两库零写入：pet.db utterance 一行没多",
          _rows(app.config.db_path, "utterance") == pet_before,
          f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
    check("两库零写入：mind.db 碎片一条没多",
          app.mind_store.fragment_count() == mind_before,
          f"{mind_before} → {app.mind_store.fragment_count()}")
    check("全程在临时沙箱里跑（数据根没指到真实的 E 盘）",
          os.path.isdir(str(box.dir)) and "pet_test_" in str(box.dir), str(box.dir))

    return summary("社交工具白名单（v2-social-grab-whitelist）")


if __name__ == "__main__":
    raise SystemExit(main())
