"""消重复 OCR + 未登录页泄漏修复（v2-social-dup-ocr-and-login-leak）的自动测试。

两处改动各验一件：

1. **消重复 OCR**：`_read_social_screen` 的闸门原来只认 `_chat_reading`。真机 2026-10-10
   01:19:53/54 那一趟，自动监听（listen 路径，立的是 `_listen_reading`）还在认同一屏，
   前台采样 5 秒一发的那一声又赶进来 `_read_social_screen` 再抓一帧、同一屏各烧 5.5 秒。
   闸门补 `or self._listen_reading` 后，listen 在认时 screen 路径直接让位——同一屏只认一趟。

2. **未登录页不冒 offer**：微信没登录那一屏中间就一个「进入微信」大按钮（真机 424x572
   那帧认到 4 个字正是它），`classify_chat_page` 认不出任何页型就默认 `PAGE_PERSONAL`，
   `note_page_kind` 当「非单聊 → 单聊」跃迁把 offer 冒在没登录时。加 `PAGE_LOGIN` + 固定名
   「进入微信 / 登录 / 扫一扫登录 / 切换账号」（复用 `_fixed_page_name` 那张表的样式），
   登录页归非单聊，offer 不冒；**不碰「认不出就默认 personal」的口径 ①**。

3. **登录页固定名硬化（v2-social-login-page-fix）**：真机二维码登录屏认到的是「扫码登录」
   （不是「扫一扫登录」），且「进入微信」这种按钮字 OCR 也可能拆成两框。多字名从「整行
   相等」改成「对表头拼接串做子串判断」，二字短名「登录」仍逐行相等。

红线：零新依赖；不碰默认 personal 口径；不碰 `_running`/`_awaiting` 状态机；不写库；
不碰 config.yaml / .vscode；不改时钟。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, make_env, section, summary, wait_for

from life.screen import social as smod
from life.screen.social import (
    PAGE_LIST,
    PAGE_LOGIN,
    PAGE_PERSONAL,
    SocialReplyService,
    classify_chat_page,
    is_personal_chat,
)
from life.screen.social_window import SENDER_OTHER, ChatRead

OTHER = SENDER_OTHER


def _read(lines: list[str], *, page_kind: str = "") -> ChatRead:
    """把一行行文字当"对方说的话"拼一份 `ChatRead`（登录页那一屏没有自己那半边）。"""
    rows = [(OTHER, text) for text in lines]
    return ChatRead(lines=rows, raw="\n".join(lines), page_kind=page_kind)


def main() -> int:
    box, stub, app = make_env()
    app.config.screen.enabled = True
    app.config.screen.social_enabled = True
    now = {"t": 10_000.0}

    def _new_service() -> SocialReplyService:
        return SocialReplyService(
            config=app.config, bus=app.bus, clock=lambda: now["t"],
            idle_seconds=lambda: 99.0,
        )

    # ---------- ① 消重复 OCR ----------

    section("① 消重复 OCR：listen 在认同一屏时，screen 路径不再抓第二帧、不起第二趟")

    svc = _new_service()
    svc._social_foreground = True
    grabs: list[str] = []
    reads: list[str] = []
    svc._grab_chat = lambda: (grabs.append("grab"), "FRAME")[1]
    # `_read_chat` 回一份非单聊的空 read，让 `_on_screen_read` 回来时不顺手冒 offer
    svc._read_chat = lambda array: (reads.append("ocr"), ChatRead(page_kind=PAGE_LIST))[1]

    ok1 = svc._read_social_screen()
    check("正路：没在认字时抓了一帧、起了 OCR（返回 True）",
          ok1 is True and len(grabs) == 1, f"ok={ok1} grabs={len(grabs)}")

    wait_for(lambda: len(reads) == 1 and svc._chat_reading is False, timeout=3.0)
    check("第一趟 OCR 确实跑回来了", len(reads) == 1, f"{len(reads)} 趟")

    # 只立 `_listen_reading`（此刻 `_chat_reading` 已落回 False）：验的正是这条新闸
    svc._listen_reading = True
    ok2 = svc._read_social_screen()
    check("★ listen 还在认同一屏 → screen 路径让位（返回 False、不抓第二帧）",
          ok2 is False and len(grabs) == 1, f"ok={ok2} grabs={len(grabs)}")

    svc._listen_reading = False
    svc._chat_reading = True
    ok3 = svc._read_social_screen()
    check("`_chat_reading` 老闸照旧（返回 False、不抓帧）",
          ok3 is False and len(grabs) == 1, f"ok={ok3} grabs={len(grabs)}")

    check("★ 从头到尾 OCR 只跑了一趟（同一屏没被认两遍）", len(reads) == 1, f"{len(reads)} 趟")

    # ---------- ② 未登录页不冒 offer ----------

    section("② 未登录页：登录页固定名归非单聊，offer 不冒、默认 personal 口径没动")

    for name in ("进入微信", "登录", "扫一扫登录", "切换账号"):
        got = classify_chat_page(_read([name, "微信"]))
        check(f"登录页固定名「{name}」（表头那一行）→ PAGE_LOGIN", got == PAGE_LOGIN, f"判成 {got}")
        check(f"「{name}」页不放行（is_personal_chat False）",
              is_personal_chat(_read([name, "微信"])) is False, "")

    check("登录名只在表头那几行认（沉到消息区就当人话 → 单聊）",
          classify_chat_page(_read(["茗风", "好", "昨天14:22", "我这边不催你", "进入微信"]))
          == PAGE_PERSONAL, "")
    check("「登录」不在表头那一带（当人话）→ 单聊",
          classify_chat_page(_read(["茗风", "登录后记得改密码"])) == PAGE_PERSONAL, "")

    # ★ 拆行 / 二维码屏（v2-social-login-page-fix）：OCR 把「进入微信」拆成两框（中间还
    #   夹着头像误认的「壹」）时，逐行相等双双落空；二维码登录屏认到的更是「扫码登录」
    #   （不是「扫一扫登录」）。两样都靠"表头拼接串子串"这一手拦住。
    check("★ 拆行形态：head=['壹','进入','微信'] → 仍是 PAGE_LOGIN",
          classify_chat_page(_read(["壹", "进入", "微信"])) == PAGE_LOGIN, "")
    check("★ 二维码登录屏「扫码登录」→ PAGE_LOGIN（真机 21:35:23 那帧）",
          classify_chat_page(_read(["微信", "扫码登录"])) == PAGE_LOGIN, "")
    check("「扫码登录」页不放行（is_personal_chat False）",
          is_personal_chat(_read(["微信", "扫码登录"])) is False, "")
    check("多字登录名只在表头那几行认（「扫码登录」沉到消息区就放过）",
          classify_chat_page(_read(["茗风", "好", "昨天14:22", "我这边不催你", "扫码登录"]))
          == PAGE_PERSONAL, "")

    # ★ 登录页提示句（v2-social-login-hint-fix）：二维码登录屏上那句「需在手机上完成登录」
    #   不在上面四个按钮名里，老口径漏掉它 → 落到默认 personal → offer / 自动拟都冒在没
    #   登录时（真机 22:30:26）。「完成登录」这一族子串都收进表头子串档。
    check("★ 「需在手机上完成登录」→ PAGE_LOGIN（真机 22:30:26 那帧）",
          classify_chat_page(_read(["微信", "需在手机上完成登录"])) == PAGE_LOGIN, "")
    check("★ 「完成登录」→ PAGE_LOGIN（同一族提示句，子串也收）",
          classify_chat_page(_read(["微信", "完成登录"])) == PAGE_LOGIN, "")
    check("★ 登录提示句页不放行（is_personal_chat False）",
          is_personal_chat(_read(["微信", "需在手机上完成登录"])) is False, "")
    check("提示句只在表头那几行认（「完成登录」沉到消息区就放过）",
          classify_chat_page(_read(["茗风", "好", "昨天14:22", "我这边不催你", "完成登录"]))
          == PAGE_PERSONAL, "")

    # 口径 ① 默认放行没动
    check("认不出的页型仍默认 personal（口径 ① 没动）",
          classify_chat_page(_read(["茗风", "好"])) == PAGE_PERSONAL, "")
    check("空 read 仍当单聊", classify_chat_page(ChatRead()) == PAGE_PERSONAL, "")
    check("空串页型仍算单聊（`is_personal_kind(\"\")`）", smod.is_personal_kind("") is True, "")
    check("PAGE_LOGIN 进了排除名单（`is_personal_kind` False）",
          smod.is_personal_kind(PAGE_LOGIN) is False, "")
    check("排除名单里那几种（含 login）都不算单聊",
          all(not smod.is_personal_kind(k) for k in smod.NON_PERSONAL_PAGES), "")

    # 服务那条链：登录页不冒、进到单聊才冒
    svc3 = _new_service()
    svc3._social_foreground = True
    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)
    svc3.note_page_kind(PAGE_LOGIN)
    check("★ 登录页 → 不冒 offer", offers == [], f"{len(offers)} 次")
    check("登录页的页型照记（事实照记、不冒话）", svc3._page_kind == PAGE_LOGIN, repr(svc3._page_kind))
    svc3.note_page_kind(PAGE_PERSONAL)
    check("★ 从登录页进到单聊 → 才算跃迁、冒 offer（登录页没把跃迁消费掉）",
          len(offers) == 1 and svc3.awaiting_confirm is True, f"{len(offers)} 次")

    # ---------- ③ 红线（源码层） ----------

    section("③ 红线：默认 personal 口径 / 状态机 / 时钟 都没动")

    classify_src = inspect.getsource(smod.classify_chat_page)
    check("默认 personal 口径没动：`classify_chat_page` 末尾仍是 `return PAGE_PERSONAL`",
          classify_src.rstrip().endswith("return PAGE_PERSONAL"), "")
    check("空串算单聊口径没动：`is_personal_kind` 仍是 `(kind or \"\") not in NON_PERSONAL_PAGES`",
          'return (kind or "") not in NON_PERSONAL_PAGES'
          in inspect.getsource(smod.is_personal_kind), "")
    gate_src = inspect.getsource(smod.SocialReplyService._read_social_screen)
    check("消重复 OCR 的闸门补上了 `_listen_reading`（与 `_chat_reading` 并列）",
          "or self._chat_reading or self._listen_reading" in gate_src, "")
    # 这两处改动（分类 + 抓屏闸门）不碰 _running/_awaiting、不读任何钟
    for name, src in (("classify_chat_page", classify_src),
                      ("_read_social_screen", gate_src)):
        for tok in ("_running", "_awaiting", "monotonic", "time.", "clock("):
            check(f"{name} 不碰 `{tok}`（状态机 / 时钟口径没动）", tok not in src, "")

    return summary("消重复 OCR + 登录页泄漏修复与固定名硬化（v2-social-dup-ocr-and-login-leak + v2-social-login-page-fix）")


if __name__ == "__main__":
    raise SystemExit(main())
