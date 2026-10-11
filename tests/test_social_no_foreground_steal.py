"""点 [好呀] 后气泡抢前台、抓屏整条停摆（v2-social-no-foreground-steal）的自动测试。

真机那条腿：点 [好呀] 那一刻前台是宠物自己的窗（点按钮就发生在它上面），而
`_grab_social_chat` 判"前台是宠物自己"→ return None，抓屏/OCR/起草整条停摆，只能等用户
手动切回微信（实测 B 档那一问挂到 35.7 秒）。修法两件事：

1. **不抢前台（主修）**：点 [好呀] 后气泡不再把宠物窗顶到前台；退路是——挂住那一问
   （`_pending_draft` 非空）期间，`_grab_social_chat` 撞上"前台是宠物自己"时**不再
   return None**，而是复用点按前已拍的上一拍整帧（`_social_raw_image`）继续流水线；
2. **消 #8 第二趟左沿**：`title_band_box` 与 `chat_area_box` 的左沿本就是同一个
   `_chat_left`，改成先算一次传进去，省掉每抓一帧的第二趟左沿（0.37~0.9s 白卡）。

红线：零新依赖；不碰 config.yaml / .vscode；pet.db / mind.db 零写入；`chat_area_box` 等
裁剪几何框值不变（只改参数传递）；584c707 / 2afc76d / e64700f / b9b6caf / afebdcb /
#6 / #7 / #8 全不动。
"""

from __future__ import annotations

import ast
import inspect
import os
import sqlite3
import sys
import textwrap
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
    wait_for,
)

import monitor.foreground as fgmod  # noqa: E402
from core.bootstrap import DesktopPetApp  # noqa: E402
from life.screen import social_window as sw  # noqa: E402
from life.screen.social import (  # noqa: E402
    NO_PEER_LINE_HINT,
    SocialReplyService,
)
from life.screen.social_window import (  # noqa: E402
    chat_area_box,
    title_band_box,
)

PEER_LINE = "那你周六到底来不来呀"


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


class _FakeMonitor:
    """假 monitor：只认一屏单聊里的一句对方的话，`detect_screen_boxes` 回空（判不出列表）。"""

    def __init__(self) -> None:
        self.rows: list = []

    def detect_screen_boxes(self, array):
        return []

    def read_screen_lines(self, array):
        return list(self.rows)


def main() -> int:
    from PySide6.QtGui import QColor, QImage

    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    app.config.screen.social_cooldown_min = 30

    candidates: list[list[dict]] = []
    spoken: list[str] = []
    handled: list[str] = []
    app.bus.social_candidates.connect(candidates.append)
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)
    app.chatter_service.handle = handled.append

    # 上一拍已拍的整帧（点按前社交窗还在前台时抓的那一帧）
    raw = QImage(742, 1342, QImage.Format_RGB888)
    raw.fill(QColor(250, 250, 250))

    clock = {"t": 1000.0}
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""

    # 前台猴补成"宠物自己"：pid == 本进程 pid，`is_pet_window` 必判真
    real_pid = fgmod.foreground_pid
    real_title = fgmod.foreground_title

    def _foreground_is_pet() -> None:
        fgmod.foreground_pid = lambda: os.getpid()
        fgmod.foreground_title = lambda: ""

    def _restore_foreground() -> None:
        fgmod.foreground_pid = real_pid
        fgmod.foreground_title = real_title

    def _reset() -> None:
        """回到"手上没字、什么都没在跑"的那一刻。"""
        service._social_foreground = True
        service._running = False
        service._awaiting = False
        service._card_awaiting = False
        service._chat_reading = False
        service._listen_reading = False
        service._pending_draft = None
        service._pending_draft_at = 0.0
        service._pending_hinted = False
        service._chat_text = ""
        service._chat_text_at = 0.0
        service._last_draft_source = ""
        service._pending_auto_source = ""
        service._last_asked = 0.0
        service._last_listen_at = 0.0
        service._page_kind = None
        service._session_id = ""
        service._peer_name = ""
        stub.prompts.clear()
        candidates.clear()
        spoken.clear()
        handled.clear()

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 前台是宠物自己、但挂着一问 → 复用上一拍整帧，不 return None ----------

        section("① 前台是宠物自己、但挂着一问 → 复用上一拍整帧，不 return None")

        _foreground_is_pet()
        app._social_raw_image = raw
        service._pending_draft = ""
        app._social_peer_hint = "微信"          # 这一问不验标题带那条腿，先关掉它的 OCR
        result = app._grab_social_chat()
        check("★ 挂着一问时抓屏不再被「宠物前台」拦死（回来一个数组）",
              result is not None, type(result).__name__ if result is not None else "None")

        # 反向：没挂着一问 → 照旧 None（老行为，别把宠物窗采成社交画面）
        service._pending_draft = None
        result2 = app._grab_social_chat()
        check("★ 没挂着一问 → 照旧 None（宠物自己在前台不采社交画面）",
              result2 is None, repr(result2))
        _restore_foreground()

        # ---------- ② 左沿只量一次：`_chat_left` 先算一次传进去，两条带子共用 ----------

        section("② 左沿只量一次：`_chat_left` 先算一次传进去，两条带子共用")

        src_grab = inspect.getsource(DesktopPetApp._grab_social_chat)
        check("★ 源码级：抓屏那一拍只调 `_chat_left(` 一次（#8 第二趟没了）",
              src_grab.count("_chat_left(") == 1, f"{src_grab.count('_chat_left(')} 次")
        check("★ 源码级：那一次的值传进了 `title_band_box` 与 `chat_area_box`",
              "title_band_box(image, self._screen_dpr(), left)" in src_grab
              and "chat_area_box(image, self._screen_dpr(), left)" in src_grab, "")

        # 行为级：left 传进去就一次 `_chat_left` 都不跑；不传才在内部量
        canvas = QImage(742, 1342, QImage.Format_RGB888)
        canvas.fill(QColor(250, 250, 250))
        calls = {"n": 0}
        real_chat_left = sw._chat_left

        def counting_left(img, dpr=1.0):
            calls["n"] += 1
            return real_chat_left(img, dpr)

        sw._chat_left = counting_left
        try:
            a = chat_area_box(canvas, 1.0, left=99)
            b = title_band_box(canvas, 1.0, left=99)
            check("★ left 传进去 → 两条带子一次 `_chat_left` 都不跑，左沿就是传进去的 99",
                  calls["n"] == 0 and a is not None and a[0] == 99
                  and b is not None and b[0] == 99,
                  f"calls={calls['n']} a={a} b={b}")
            c = chat_area_box(canvas, 1.0)
            check("★ 不传 left → 才在内部量一次（回退腿没断）",
                  calls["n"] == 1 and c is not None, f"calls={calls['n']}")
        finally:
            sw._chat_left = real_chat_left

        # ---------- ③ 端到端：点 [好呀]（前台是宠物自己）→ 复用上一拍 → 卡片上屏 ----------

        section("③ 端到端：点 [好呀]（前台是宠物自己）→ 复用上一拍 → 卡片上屏，不再等人工切窗")

        _foreground_is_pet()
        _reset()
        app._social_raw_image = raw
        app._social_peer_hint = "微信"
        app._social_session_hint = "微信"
        monitor = _FakeMonitor()
        monitor.rows = [((40, 60, 120, 76), PEER_LINE)]
        real_monitor = app.screen_monitor
        app.screen_monitor = monitor
        service._grab_chat = app._grab_social_chat
        service._read_chat = app._read_social_chat
        stub.candidates_result = {"candidates": [
            {"draft": "来，那周六见", "why": "接住对方的行动"},
        ]}

        t0 = time.monotonic()
        took = service.confirm("好呀")
        check("认下了这活儿", took is True, str(took))
        ok = wait_for(lambda: len(candidates) == 1, timeout=5.0)
        drain_threads()
        pump(0.1)
        elapsed = time.monotonic() - t0
        check("★ 没切回微信、卡片也上屏了（挂住那问靠上一拍帧补上）",
              ok and len(candidates) == 1, f"{len(candidates)} 组 / {elapsed:.2f}s")
        check("★ 耗时在桩内阈值内（不含人工切窗等待；真机旧口径挂到 35.7s）",
              elapsed < 5.0, f"{elapsed:.2f}s")
        prompt = stub.prompts[0] if stub.prompts else ""
        check("★ 上一拍帧里的对方原话真的流进了提示词",
              PEER_LINE in prompt, prompt[:80].replace("\n", " / "))
        check("★ 没冒那句「还没读到」（这一趟抓得成）",
              NO_PEER_LINE_HINT not in spoken, " | ".join(spoken) or "（安静）")
        check("没落到通用聊天兜底", handled == [], repr(handled))
        _restore_foreground()
        app.screen_monitor = real_monitor

        # ---------- ④ 红线 ----------

        section("④ 红线：裁剪几何一行没改 + 既有语义没动 + 两库零写入")

        check("★ `chat_area_box` 裁剪框值没动（只多了 `left` 参数，几何常量原样）",
              "TITLE_BAR_PX" in inspect.getsource(chat_area_box)
              and "CHAT_MIN_SIDE" in inspect.getsource(chat_area_box), "")
        check("★ `title_band_box` 几何也没动（`TITLE_BAND_PX` 原样）",
              "TITLE_BAND_PX" in inspect.getsource(title_band_box), "")
        check("★ b9b6caf 的 `looks_like_list_boxes` 输入数组没动",
              "looks_like_list_boxes(boxes, width)"
              in inspect.getsource(DesktopPetApp._read_social_chat), "")

        refs: set[str] = set()
        for node in ast.walk(ast.parse(textwrap.dedent(src_grab))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("抓屏这条线没多一个第三方 import", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before,
              f"{mind_before} → {app.mind_store.fragment_count()}")
    finally:
        _restore_foreground()
        drain_threads()
        box.cleanup()

    return summary("点 [好呀] 不抢前台、复用上一拍帧（v2-social-no-foreground-steal）")


if __name__ == "__main__":
    raise SystemExit(main())
