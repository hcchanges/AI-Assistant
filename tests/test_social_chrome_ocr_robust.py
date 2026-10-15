# -*- coding: utf-8 -*-
"""抓取层·时间戳错字鲁棒（v2-social-chrome-ocr-robust）的自动测试。

**病根**（真机 2026-10-16 00:35:50，跨天会话）：跨天分隔线「昨天22:41」被 OCR 认成
「作天22:41」（"昨"→"作"首字错）。`_is_chat_chrome` 的时间戳判定是 `fullmatch` 精确比，
错一个字就失配，于是这条时间戳漏过装潢过滤、被 `_tail_context` 当「对方最近发来的那条」
交出去——气泡就报「对方只发来时间戳、无实际内容」。

这一批两刀：

- `_is_chat_chrome` 放宽：新增结构判据 `_CHAT_TIME_LOOSE`（时间必须落在行尾、前缀只许
  短日期词/时段），允许日期词首字被认错，不靠扩词表打地鼠；
- `_tail_context` 主刀：取到的那条若是「时间戳/日期形状」（含错字），当碎片跳过、接着
  往上找真正的那句——跟 `_is_fragment_material` 同一套"不算材料就往上一句找"。

红线（本模块逐条钉住）：
- 人话「我下午3:00到」仍不算装潢（时间夹在中间、行尾还有字）；
- 撤回行「你撤回了一条消息」仍整行命中挑掉；
- 既有社交测试零改动、零新依赖（本模块只引既有纯函数）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary  # noqa: E402

from life.screen.social import (  # noqa: E402
    _is_chat_chrome,
    _is_chrome_fragment,
    _tail_context,
)
from life.screen.social_window import (  # noqa: E402
    SENDER_OTHER,
    SENDER_USER,
    ChatRead,
)


def main() -> int:
    section("① _is_chat_chrome 放宽：日期词首字被认错也算装潢（结构判据，非扩词表）")

    for line in ("作天22:41", "作天 22:41", "令天22:41", "昨天22:41", "今天22:41"):
        check(f"时间戳（含错字）「{line}」判成装潢", _is_chat_chrome(line) is True, "")
    check("纯时间戳仍认（不回归）", _is_chat_chrome("00:31") is True
          and _is_chat_chrome("12:53") is True, "")
    for line in ("我下午3:00到", "在吗", "😁", "刷得是短剧"):
        check(f"人话「{line}」不算装潢（不回归）", _is_chat_chrome(line) is False, "")
    check("撤回行仍整行命中挑掉（不回归）",
          _is_chat_chrome("你撤回了一条消息") is True, "")

    section("② _tail_context 主刀：跨天分隔线（含错字）当碎片跳过，往上找真话")

    read = ChatRead(lines=[
        (SENDER_OTHER, "刷得是短剧"),
        (SENDER_OTHER, "作天22:41"),
    ])
    check("★ 末句是错字时间戳「作天22:41」→ 返回上面那句真话「刷得是短剧」",
          _tail_context(read) == "刷得是短剧", repr(_tail_context(read)))
    check("★ 时间戳没混进材料", "作天22:41" not in _tail_context(read), "")

    normal = ChatRead(lines=[
        (SENDER_OTHER, "刷得是短剧"),
        (SENDER_OTHER, "昨天22:41"),
    ])
    check("正常「昨天22:41」仍当装潢挑掉 → 同样返回「刷得是短剧」",
          _tail_context(normal) == "刷得是短剧", repr(_tail_context(normal)))

    plain = ChatRead(lines=[(SENDER_OTHER, "刷得是短剧")])
    check("对方真实末句无时间戳 → 原样返回", _tail_context(plain) == "刷得是短剧",
          repr(_tail_context(plain)))

    only_ts = ChatRead(lines=[(SENDER_OTHER, "作天22:41")])
    check("一屏只有一条错字时间戳 → 老实回空串（不编一句）",
          _tail_context(only_ts) == "", repr(_tail_context(only_ts)))

    section("③ _is_chrome_fragment 本身：整段时间戳形状（含错字）认得出、真话不误伤")

    check("错字时间戳是 chrome 碎片", _is_chrome_fragment("作天22:41") is True, "")
    check("正常时间戳是 chrome 碎片", _is_chrome_fragment("昨天22:09") is True, "")
    check("折行拼回来的时间戳也认得出", _is_chrome_fragment("作\n天22:41") is True, "")
    check("真话不是 chrome 碎片", _is_chrome_fragment("刷得是短剧") is False, "")

    section("④ 红线：既有口径一个没破")

    check("最后一句是自己发的 → 仍退回对方最近那条",
          _tail_context(ChatRead(lines=[
              (SENDER_OTHER, "在吗"), (SENDER_USER, "在的")])) == "在吗", "")
    check("对方真实末句（带句号）原样返回",
          _tail_context(ChatRead(lines=[
              (SENDER_OTHER, "刚躺下，准备刷会手机。")])) == "刚躺下，准备刷会手机。", "")

    return summary("抓取层·时间戳错字鲁棒（v2-social-chrome-ocr-robust）")


if __name__ == "__main__":
    raise SystemExit(main())
