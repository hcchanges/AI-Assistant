"""聊天记录区「底部那口气」的自动测试（v2-social-ui-batch 批次9）。

真机图 2 坐实：「AI助理」那个助手窗滚到底时，内容区**最后一行**（「把'不亏'这句收进
自己」那一行）贴着记录区下缘，下半个字被切掉——记录区跟下面的输入行只隔一个 layout
spacing，末行等于顶在边线上，一点缓冲都没有。

两件事一起修才对：

- **底部留白**：`QListWidget#ChatHistory` 用 `setViewportMargins(0, 0, 0, HISTORY_PAD_BOTTOM)`
  把**视口**往上收——留白是留"记录区的"，不是塞在内容后面的。配合 `_fit_height` 里那句
  `_content_height() + HISTORY_PAD_BOTTOM`，内容刚好塞得下时视口正好等于内容高，**不会
  凭空多出一条滚动条**。
- **先长高、再滚到底**（`ChatBubble._append`）：老口径反着来。内容第一次超过上限那一拍，
  记录区要从旧高度长到新高度，而滚动是在旧高度上做的——等视口长完，位置就不在底上了。

这一批只碰**画**那一层：`ui/chat_bubble.py` 与 `ui/card_chat.py` 两个记录区（AI助理
的助手窗 + 卡内小助手），社交那条线一个字没动（⑤ 钉了 tripwire）。

红线：零新依赖（只用了 PySide6 既有的 `setViewportMargins` 与 `QListWidget` 既有接口）；
pet.db / mind.db 零写入；不动社交判定、不动去重与冷却。

**离屏这一层量得到什么、量不到什么（老实说）**：`viewportMargins` / 视口高 / 记录区高 /
`_append` 的调用顺序都是**确定**的，下面按这几条钉。而 `QListView` 的滚动范围在离屏
平台上是**不准的**（它按一个估出来的行高算 `maximum`，实测 40 条时 `maximum` 只有 37），
所以"滚到底 → 末行完整可见"这一条离屏下只能当**粗检**（没被切穿），当不了实锤——
真机上那一屏由图 2 复验。把这条说清楚，比让它假装验过了强。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    ensure_app,
    make_env,
    pump,
    section,
    summary,
)

from PySide6.QtWidgets import QListWidgetItem  # noqa: E402

from ui import card_chat as card_mod  # noqa: E402
from ui import chat_bubble as bubble_mod  # noqa: E402
from ui.card_chat import CardChatPanel, PANEL_HEIGHT  # noqa: E402
from ui.chat_bubble import (  # noqa: E402
    BUBBLE_W,
    HISTORY_PAD_BOTTOM,
    MAX_ITEMS,
    MIN_HISTORY_H,
    ROLE_SPEAKER,
    SPEAKER_PET,
    ChatBubble,
)

LONG_LINE = "这一条挺长的，用来把记录区撑到上限，好看看滚到底时最后一行是什么样"


def _fill_bubble(bubble: ChatBubble, count: int) -> None:
    """往助手窗里塞 `count` 条（走真 `_append` 那条路，不绕过高度与滚动那两拍）。"""
    for index in range(count):
        bubble._append(f"{LONG_LINE}（第 {index + 1} 条）", SPEAKER_PET)


def _fill_panel(panel: CardChatPanel, count: int) -> None:
    for index in range(count):
        panel._append(f"{LONG_LINE}（第 {index + 1} 条）", SPEAKER_PET)


def _last_rect(history):
    """记录区里最后一条的矩形（离屏下 `visualItemRect` 照样给得出来）。"""
    item = history.item(history.count() - 1)
    return history.visualItemRect(item) if item is not None else None


def main() -> int:
    ensure_app()

    # ---------- ① 助手窗（AI助理）：记录区底部留一口气 ----------

    section("① 助手窗：记录区底部真留出了 HISTORY_PAD_BOTTOM 那口气")

    bubble = ChatBubble("天渌")
    history = bubble._history
    check(f"底部留白那一格是 {HISTORY_PAD_BOTTOM}px（≥ 8 才算一口气）",
          HISTORY_PAD_BOTTOM >= 8, str(HISTORY_PAD_BOTTOM))
    check("★ 留白加在**视口**上（不是塞在内容后面）：viewportMargins.bottom 就是它",
          history.viewportMargins().bottom() == HISTORY_PAD_BOTTOM,
          str(history.viewportMargins().bottom()))
    check("左右/上边没跟着一起加（只收底部这一刀）",
          history.viewportMargins().left() == 0
          and history.viewportMargins().top() == 0
          and history.viewportMargins().right() == 0,
          f"{history.viewportMargins().left()}/{history.viewportMargins().top()}"
          f"/{history.viewportMargins().right()}")

    # 内容刚好塞得下那一档：记录区高度 = 内容高 + 那口气（说明这口气是**另给**的，
    # 不是从内容里抠出来的——抠出来的话最后一条照样会被切）
    _fill_bubble(bubble, 2)
    pump(0.05)
    content = bubble._content_height()
    room = bubble._max_height()
    check("★ 记录区高度 = 内容高 + 底部那口气（塞得下时不多不少）",
          MIN_HISTORY_H < history.height() < room
          and history.height() == content + HISTORY_PAD_BOTTOM,
          f"h={history.height()} / 内容 {content} / 上限 {room}")
    check("★ 视口比记录区矮的正好是那口气（留白在视口外，没盖住内容）",
          history.height() - history.viewport().height() == HISTORY_PAD_BOTTOM,
          f"{history.height()} - {history.viewport().height()}")

    # 内容撑到上限那一档：滚到底，最后一条必须**整条**在视口里
    _fill_bubble(bubble, 38)
    # 连着一口气加 38 条时，Qt 的滚动范围要到事件循环转一圈之后才定下来——离屏下不 pump
    # 的话 `scrollToBottom` 用的是**旧范围**，量出来的位置会虚高一截。这里先让所有追加
    # 落定，再明确"滚到底"一次：验的是"滚到底那一刻末行完整不完整"这件事本身。
    pump(0.05)
    history.scrollToBottom()
    pump(0.02)
    rect = _last_rect(history)
    check("内容撑到上限时记录区停在屏幕上限（多出来的在里面滚动，这条老口径没破）",
          history.height() <= bubble._max_height() + 1, str(history.height()))
    check("追加那一拍真去滚了（滚动条走到它这一拍的范围尽头）",
          history.verticalScrollBar().value() == history.verticalScrollBar().maximum(),
          f"{history.verticalScrollBar().value()}/{history.verticalScrollBar().maximum()}")
    check("末条**没被切穿**：整条矩形落在视口内（离屏粗检，见模块开头那段）",
          rect is not None and rect.top() >= 0
          and rect.bottom() <= history.viewport().height(),
          f"末条 {rect.top()}..{rect.bottom()} / 视口高 {history.viewport().height()}")

    # 反向对照：把留白拿掉（= 修前那一版），上面"视口比记录区矮"那条必红
    bare = ChatBubble("天渌")
    bare._history.setViewportMargins(0, 0, 0, 0)
    _fill_bubble(bare, 2)
    pump(0.05)
    check("反向对照：不给留白时，视口与记录区一样高（所以上面那条不是假绿）",
          bare._history.height() - bare._history.viewport().height() == 0,
          f"{bare._history.height()} - {bare._history.viewport().height()}")

    # ---------- ② 先长高、再滚到底（顺序那件事） ----------

    section("② 助手窗追加一条：先长高、再滚到底（老口径反着来）")

    order: list[str] = []
    real_fit = bubble._fit_height
    real_scroll = bubble._history.scrollToBottom

    def spy_fit() -> None:
        order.append("fit")
        real_fit()

    def spy_scroll() -> None:
        order.append("scroll")
        real_scroll()

    bubble._fit_height = spy_fit
    bubble._history.scrollToBottom = spy_scroll
    bubble._append("最后再补一条", SPEAKER_PET)
    bubble._fit_height = real_fit
    bubble._history.scrollToBottom = real_scroll
    check("★ 顺序是「先长高、再滚到底」——滚动落在已经是最终高度的视口上（老口径反着来）",
          order == ["fit", "scroll"], repr(order))
    rect = _last_rect(history)
    check("那一拍确实滚了一次（没有把这一条留在视口外面）",
          history.verticalScrollBar().value() == history.verticalScrollBar().maximum(),
          f"{history.verticalScrollBar().value()}/{history.verticalScrollBar().maximum()}")
    check("滚完之后末条仍在视口内（同上那条粗检）",
          rect is not None and rect.bottom() <= history.viewport().height(),
          f"{rect.top()}..{rect.bottom()} / {history.viewport().height()}")

    # ---------- ③ 卡内小助手：同一个口径 ----------

    section("③ 卡内小助手：同样的底部留白、同样滚到底最后一条完整")

    panel = CardChatPanel()
    panel.resize(420, PANEL_HEIGHT)
    panel.show()
    pump(0.05)
    check("卡内小助手也把留白加在视口上（两个记录区一个口径）",
          panel._history.viewportMargins().bottom() == HISTORY_PAD_BOTTOM,
          str(panel._history.viewportMargins().bottom()))
    check("★ 视口比记录区矮的正好是那口气",
          panel._history.height() - panel._history.viewport().height() == HISTORY_PAD_BOTTOM,
          f"{panel._history.height()} - {panel._history.viewport().height()}")
    check("这一块本身的高度一分没改（214，卡面排版不跟着动）",
          PANEL_HEIGHT == 214 and panel.height() == PANEL_HEIGHT, str(panel.height()))

    _fill_panel(panel, MAX_ITEMS)
    pump(0.05)
    hist = panel._history
    rect = _last_rect(hist)
    check("越聊越长这条老口径没破：只留最近 MAX_ITEMS 条", hist.count() == MAX_ITEMS,
          str(hist.count()))
    check("滚到底那一拍真滚了、末条也没被切穿（离屏粗检，同上）",
          hist.verticalScrollBar().value() == hist.verticalScrollBar().maximum()
          and rect is not None and rect.bottom() <= hist.viewport().height(),
          f"{hist.verticalScrollBar().value()}/{hist.verticalScrollBar().maximum()} / "
          f"末条 {rect.top()}..{rect.bottom()} / 视口 {hist.viewport().height()}")

    # 装配层那只真窗（AI助理）也是同一套：同一个类、同一格留白
    box, stub, app = make_env()
    try:
        real = app.chat_bubble
        check("装配层真建出来的那只助手窗也带上了这口气（不是只有测试里新建的才有）",
              real._history.viewportMargins().bottom() == HISTORY_PAD_BOTTOM,
              str(real._history.viewportMargins().bottom()))

        # ---------- ④ 红线 ----------

        section("④ 红线：零新依赖 / 两库零写入 / 只碰画那一层")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        real._append("这一条也不该落盘", SPEAKER_PET)
        _fill_panel(panel, 3)
        pet_after = _rows(app.config.db_path, "utterance")
        mind_after = app.mind_store.fragment_count()
        check("两库零写入：pet.db utterance 一行没多", pet_after == pet_before,
              f"{pet_before} → {pet_after}")
        check("两库零写入：mind.db 碎片一条没多", mind_after == mind_before,
              f"{mind_before} → {mind_after}")

        import ast
        import inspect

        refs: set[str] = set()
        for mod in (bubble_mod, card_mod):
            for node in ast.walk(ast.parse(inspect.getsource(mod))):
                if isinstance(node, ast.Import):
                    refs.update(a.name.split(".")[0] for a in node.names)
                elif isinstance(node, ast.ImportFrom):
                    refs.update(a.name for a in node.names)
        banned = {"numpy", "PIL", "psutil", "pyperclip", "cv2", "pyautogui",
                  "win32api", "requests"}
        check("两个画气泡的模块都没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        # 这一批只碰"画"：社交那条线里不该出现"视口留白 / 平滑滚动"这些字眼
        from life.screen import social as social_mod

        social_src = inspect.getsource(social_mod)
        touched = [word for word in ("setViewportMargins", "QScroller", "scrollToBottom")
                   if word in social_src]
        check("★ 社交那条线一个字没动（这一批只碰 ui/ 那两个记录区）",
              not touched, "、".join(touched) or "（一处都没有）")

        # 老尺寸常量一个没被顺手改（改大改小都算动排版）
        check("老尺寸常量没被顺手改：气泡宽 300 / 记录区最小高 140",
              BUBBLE_W == 300 and MIN_HISTORY_H == 140, f"{BUBBLE_W} / {MIN_HISTORY_H}")
    finally:
        box.cleanup()

    return summary("聊天记录区底部留白（v2-social-ui-batch 批次9）")


if __name__ == "__main__":
    raise SystemExit(main())
