"""微信**搜索态**不再被当成单聊（v2-social-search-state）的自动测试。

**真机缺口**（报告 §183「真机缺口二」）：点开微信的搜索框之后，整屏就是**搜索页**——
最上面一个搜索框（占位字只有「搜索」两个字），下面要么是「搜索指定内容」+ 分组标题
（联系人 / 群聊 / 聊天记录 …），要么是敲了字之后的**分组结果**，要么是「没有找到…相关结果」。
当时手上没有搜索态的真机帧，这一档只在"名字那一格"上按「宁可认不出」压过一道
（`_peer_name_from_band_text` 里那条「含搜索即丢」），**判页型那条链一次都没验过**。

这一批按**真机配色 / 版面**摆了搜索态的帧（742×1342：图标栏右缘 92、页底 250,250,250、
搜索框底 238,238,240——都是 `%TEMP%/pet_social_leak_20261011_215038.png` 那批帧上量出来的），
再拿**真 RapidOCR** 认了一遍：下面那三串行就是真引擎从那三张帧上认出来的**原字**，一个字
没改（13 / 10 / 2 行），连带子那一格也是真跑过的——**搜索态帧上那条标题带认不出任何一行**
（`%TEMP%/ss_probe.txt`：`带子里最靠左那一行 = ''` → 名字 = ''）。真引擎那几趟跑在探针里，
不进本模块（本模块离线、确定性：帧摆好、字喂进去，跟 `test_social_listview_leak.py` 同一个做法）。

复验出来的漏法（**真发生**，不是推演）：16-β 那三条腿对搜索页**全瞎**——没有时间列可数、
没有账号号 / 未读计数 / 群名行那三类字面、也不是加载占位——于是「搜索结果」与「空结果」
两档一路落到 `classify_chat_page` 兜底的 `PAGE_PERSONAL`（真探针读数）：

    搜索结果：10 行 / 自己 0 行 → 列表腿 False → PAGE_PERSONAL
              → 冒 offer、`_tail_context` 把「那就周六见啦」（搜索结果里那条预览）当"对方原话"
    搜索空结果：2 行 / 自己 0 行 → 同上 → 「没有找到“zz”相关结果」成了"对方原话"

修法：`looks_like_search_page` 当成 `looks_like_list_page` 的**第四条腿**（`PAGE_LIST` 那一格
的字面注释里本来就写着「会话总列表 / 搜索页」），于是搜索态走 16-β 那条「清成空 read」老路，
页型记 `PAGE_LIST`、`is_personal_kind` 为假——**不冒 offer、不进材料**。

本测试钉四件事：

**① 判据**：搜索框那一行 + 搜索页专有的分组标题，两条都要；或者空结果那一档（去掉装潢
之后整屏 ≤ `SEARCH_EMPTY_ROWS` 行、且含「没有找到…相关结果」）。三档搜索态 → 列表腿命中
→ `classify_chat_page` 回 `PAGE_LIST`。反面对照一屏都不许误伤：真单聊、话里提「搜索」、
只有半条腿、绿气泡（一票否决）、会话总列表（它该走列表腿，不是搜索腿）。

**② 落点**（`core.bootstrap._read_social_chat` + `SocialReplyService._on_screen_read`）：
命中就把这一份 `ChatRead` 清成空的，**自动与手动两条路共用**同一个空 read——搜索结果里的
昵称 / 微信号 / 预览行一个都不进材料；不冒 offer、不落这一场的冷却戳、页型记 `PAGE_LIST`。

**③ 自动那条腿**：搜索态那一拍一条候选都不给、一次模型都不发、尾巴也不推进；人切回单聊
照旧拟（这道闸没把监听卡死）。

**④ 名字那一格 + 换场**：名字只在**单聊那一档**才认（装配层源码级：认名字那一步挂在
`is_personal_kind(page_kind)` 下）——搜索态那一拍带子即便认得出一行字也落不进名字格；
`_cooldown_key()` 退回裸会话标识；搜索态那一拍照旧只按会话标识认场（不因为搜索页的字
换场、也不拿它当名字）。

红线：零新依赖（判据只用了模块里既有的 `re` / `as_chat_read`）；pet.db / mind.db 零写入；
不模拟按键、不碰剪贴板、不读窗口标题；`classify_chat_page` 的兜底 `return PAGE_PERSONAL`
一个字没删，16-β 那三条腿 / 账号选择腿 / 登录腿一分没动（⑤ 逐条钉）。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
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

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen.social import (  # noqa: E402
    PAGE_LIST,
    PAGE_PERSONAL,
    SEARCH_EMPTY_ROWS,
    SEARCH_HEAD_ROWS,
    SocialReplyService,
    _SEARCH_ONLY_LINES,
    _tail_context,
    classify_chat_page,
    is_personal_kind,
    looks_like_list_page,
    looks_like_search_page,
)
from life.screen.social_window import (  # noqa: E402
    SENDER_OTHER,
    SENDER_USER,
    ChatRead,
    classify_lines,
)

W, H = 742, 1342
# 真机配色（那一批取证帧上量出来的）
PAGE_BG = QColor(250, 250, 250)     # 搜索页底
BOX_BG = QColor(238, 238, 240)      # 搜索框底（跟真机对方气泡同一个灰）
ICON_BG = QColor(228, 225, 228)     # 图标栏底
WHITE = QColor(255, 255, 255)
INK = QColor(25, 25, 25)
GREEN = QColor(149, 236, 105)       # 自己的绿气泡（`is_self_bubble` 那一串判据全过）
ICON_W = 92                         # 图标栏右缘（真机量出来的 92）

# ---- 三档搜索态：真 RapidOCR 从真机风格帧上认出来的原字（13 / 10 / 2 行，一个字没改）----
SEARCH_PLACEHOLDER = [
    "搜索", "搜索指定内容", "聊天记录", "联系人", "群聊", "收藏",
    "朋友圈", "公众号", "文件", "图片与视频", "最近搜索", "茗风", "深圳团建群",
]
SEARCH_RESULTS = [
    "搜索", "联系人", "茗风", "微信号：mingfeng2024", "群聊",
    "深圳团建群(12)", "茗风：明早九点出发", "聊天记录", "茗风", "那就周六见啦",
]
SEARCH_EMPTY = ["ZZZ", "没有找到“zz”相关结果"]

# 真单聊那一屏上的字（同 `test_social_listview_leak.py` 那批读数）
CHAT_LINES = [
    "茗风", "21:43", "咦，啥事呀，这么开心呢。",
    "嘻嘻，你猜猜呗，猜对了有奖哟。", "行，那我猜你心里正偷着乐呢。",
]
# 会话总列表那一屏（搜索框 + 账号号 + 群名）：这一屏该由**列表腿**判非单聊，
# 搜索腿一个字都不该认（它要的是搜索页专有的那几句分组标题）。
LIST_LINES = [
    "搜索", "茗风", "那就周六见啦", "昨天", "公众号", "服务号", "深圳团建群",
    "我明天到", "在吗",
]


def _rows_with_boxes(texts: list[str]) -> list:
    """一串字 → 摆成"搜索页"那种版面：(框, 字) 的表，第一行是搜索框。

    框跟下面 `_page_image` 画出来的方块**逐像素对齐**——`classify_lines` 要拿框去图上取
    底色判"这行是谁说的"，对不上的话这一批验的就不是真那条链了。
    """
    out = []
    for i, text in enumerate(texts):
        if i == 0:                                  # 搜索框那一行（灰底框里）
            out.append(([ICON_W + 30, 62, ICON_W + 30 + 18 * len(text), 106], text))
            continue
        y = 150 + (i - 1) * 46
        out.append(([ICON_W + 34, y, ICON_W + 34 + 18 * len(text), y + 28], text))
    return out


def _page_image(texts: list[str], *, box_row: bool = True) -> QImage:
    """一屏：图标栏 + 页底 + 一个灰搜索框 + 一列深字（都打在白底上，跟真机同款）。"""
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(PAGE_BG)
    p = QPainter(img)
    p.fillRect(QRect(0, 0, ICON_W, H), ICON_BG)               # 图标栏
    if box_row:
        p.fillRect(QRect(ICON_W + 18, 56, 520, 56), BOX_BG)   # 搜索框（灰底）
    for i, text in enumerate(texts):
        if i == 0 and box_row:
            p.fillRect(QRect(ICON_W + 30, 62, 18 * len(text), 44), INK)
            continue
        y = 150 + (i - 1) * 46
        p.fillRect(QRect(ICON_W + 34, y, 18 * len(text), 28), INK)
    p.end()
    return img


def _chat_page_image(peer_tail: str = "") -> QImage:
    """一场单聊（真机那一屏的形状）：白底上对方深字 + 绿块上自己 + 中间一条时间行。"""
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(WHITE)
    p = QPainter(img)
    y = 120
    for text in CHAT_LINES[:3]:
        p.fillRect(QRect(ICON_W + 30, y, 18 * len(text), 30), INK)
        y += 70
    p.fillRect(QRect(345, y, 60, 24), INK)                     # 时间行
    y += 50
    p.fillRect(QRect(300, y, 330, 70), GREEN)                  # 自己：整块绿底
    p.fillRect(QRect(320, y + 16, 290, 34), INK)
    y += 90
    if peer_tail:
        p.fillRect(QRect(ICON_W + 30, y, 18 * len(peer_tail), 30), INK)
    p.end()
    return img


class _FakeMonitor:
    """替掉真 monitor：认字那一步直接回摆好的行（真引擎从这些帧上认出来的那些）。"""

    def __init__(self, lines: list) -> None:
        self.lines = lines

    def read_screen_lines(self, array):        # noqa: ARG002 —— 跟真接口对齐
        return self.lines


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _band_md5(band: QImage) -> str:
    """带子像素的 md5——跟装配层 `_social_peer_name_from_band` 里那一行同源。"""
    import hashlib

    return hashlib.md5(mon._to_numpy(band).tobytes()).hexdigest()


def _find(text: str, needle: str, start: int = 0) -> int:
    """源码里定位一句话：找不到回 -1。

    **故意不用 `str.index`**：那一抛异常就把整个模块带崩，报出来的是"模块没跑完"而不是
    "这条判据没过"——敏感度验（把判据拆掉看它红不红）当场就白做了。
    """
    return text.find(needle, start)


def main() -> int:
    box, stub, app = make_env()
    try:
        from core.bootstrap import _peer_name_from_band_text
        from life.screen import social as smod

        service: SocialReplyService = app.social_reply
        src = inspect.getsource(smod)

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()
        real_monitor = app.screen_monitor

        # 社交软件在前台、这一场认到「甲」（`_cooldown_key()` 就是「甲」）
        service._social_foreground = True
        service._last_proc = "Weixin.exe"
        service._session_provider = lambda: "甲"
        service._peer_provider = lambda: ""
        service._sync_session()
        service._examples_provider = lambda query: ""    # noqa: ARG005
        service._running = False
        service._awaiting = False
        key = service._cooldown_key()
        check("前置：这一场的冷却键就认到「甲」", key == "甲", repr(key))

        offers: list[str] = []
        app.bus.social_offer.connect(offers.append)

        # ---------- ① 判据 ----------

        section("① 判据：三档搜索态都判「非单聊」，反面对照一屏不误伤")

        for label, texts in (("搜索框刚点开", SEARCH_PLACEHOLDER),
                             ("搜索结果分组", SEARCH_RESULTS),
                             ("搜索空结果", SEARCH_EMPTY)):
            read = classify_lines(_page_image(texts), _rows_with_boxes(texts))
            kinds = {s for s, _t in read.lines}
            check(f"★ {label}：认出来的行**全在对方那半边**（真机那一屏就是这样）",
                  kinds == {SENDER_OTHER}, repr(kinds))
            check(f"★ {label}：`looks_like_search_page` 命中",
                  looks_like_search_page(read) is True, "")
            check(f"★ {label}：`looks_like_list_page` 也跟着命中（16-β 那一条照旧）",
                  looks_like_list_page(read) is True, "")
            check(f"★ {label}：`classify_chat_page` 不再是 `PAGE_PERSONAL`（漏法的根）",
                  classify_chat_page(read) != PAGE_PERSONAL, classify_chat_page(read))
            check(f"★ {label}：落的是 `PAGE_LIST`（跟「会话总列表 / 搜索页」同一档）",
                  classify_chat_page(read) == PAGE_LIST, classify_chat_page(read))
            check(f"★ {label}：`is_personal_kind` 为假（帮聊那道闸不放行）",
                  is_personal_kind(classify_chat_page(read)) is False, "")

        # 反面对照：这几屏一个字都不许误伤
        chat = classify_lines(_chat_page_image(), _rows_with_boxes(CHAT_LINES))
        check("反面对照：真单聊 → `PAGE_PERSONAL`",
              classify_chat_page(chat) == PAGE_PERSONAL, classify_chat_page(chat))
        check("反面对照：真单聊不判搜索页", looks_like_search_page(chat) is False, "")

        asked = ChatRead(lines=[(SENDER_OTHER, "我去搜索一下"), (SENDER_OTHER, "稍等"),
                                (SENDER_OTHER, "搜到了发你")])
        check("反面对照：话里提「搜索」，可那一行不是**整行就是搜索两个字** → 不判",
              looks_like_search_page(asked) is False, "")
        check("反面对照：那一屏照旧回 `PAGE_PERSONAL`（人话不许被挡）",
              classify_chat_page(asked) == PAGE_PERSONAL, classify_chat_page(asked))

        veto = ChatRead(lines=[(SENDER_OTHER, "搜索"), (SENDER_OTHER, "聊天记录"),
                               (SENDER_USER, "我搜过了")])
        check("★ 一票否决：自己发过话（绿气泡）就不是搜索页",
              looks_like_search_page(veto) is False, "")
        check("★ 一票否决那一屏照旧回 `PAGE_PERSONAL`（不许整屏闷死）",
              classify_chat_page(veto) == PAGE_PERSONAL, classify_chat_page(veto))

        main_list = classify_lines(_page_image(LIST_LINES), _rows_with_boxes(LIST_LINES))
        check("反面对照：会话总列表那一屏（搜索框 + 账号号 + 群名）**不穿搜索腿**",
              looks_like_search_page(main_list) is False, "")
        check("★ 那一屏照旧判非单聊（由列表腿那一刀判的，两条腿各干各的）",
              classify_chat_page(main_list) == PAGE_LIST, classify_chat_page(main_list))

        check("反面对照：整行只有「搜索」两个字、没有分组标题 → 不判",
              looks_like_search_page(ChatRead(lines=[(SENDER_OTHER, "搜索"),
                                                    (SENDER_OTHER, "在吗")])) is False, "")
        check("反面对照：整行只有「聊天记录」、没有搜索框 → 不判",
              looks_like_search_page(ChatRead(lines=[(SENDER_OTHER, "聊天记录"),
                                                    (SENDER_OTHER, "好东西呀")])) is False, "")
        check("反面对照：空屏（一个字没认到）→ 不判（那是「没认到」，口径 ① 放行）",
              looks_like_search_page(ChatRead()) is False, "")
        check("反面对照：整屏都是装潢（一条时间行）→ 不判",
              looks_like_search_page(ChatRead(lines=[(SENDER_OTHER, "昨天 21:43")])) is False, "")
        check("★ 边界：搜索框落在表头之外（第 5 行才出现）→ 不判",
              looks_like_search_page(ChatRead(lines=[
                  (SENDER_OTHER, "在吗"), (SENDER_OTHER, "今天好热"),
                  (SENDER_OTHER, "你那边呢"), (SENDER_OTHER, "我这边下雨"),
                  (SENDER_OTHER, "搜索"), (SENDER_OTHER, "聊天记录")])) is False, "")
        check("★ 边界：表头那几行里就有搜索框（`SEARCH_HEAD_ROWS` 那一档照旧认）",
              looks_like_search_page(ChatRead(lines=[
                  (SENDER_OTHER, "口"), (SENDER_OTHER, "X"), (SENDER_OTHER, "搜索"),
                  (SENDER_OTHER, "最近搜索"), (SENDER_OTHER, "茗风")])) is True, "")
        check("★ 边界：空结果那一档的容差就是 `SEARCH_EMPTY_ROWS` 行（多一行就不认）",
              looks_like_search_page(ChatRead(lines=[
                  (SENDER_OTHER, "没有找到“zz”相关结果"), (SENDER_OTHER, "aaa"),
                  (SENDER_OTHER, "bbb")])) is False and SEARCH_EMPTY_ROWS == 2, "")
        check("★ 判据那两条腿的常量都在明面上（表头行数 / 搜索页专有标题那一组）",
              SEARCH_HEAD_ROWS >= 1 and "聊天记录" in _SEARCH_ONLY_LINES, "")

        # ---------- ② 落点 ----------

        section("② 落点：搜索态那一屏被清成空 read，材料一个字都取不出来")

        app.screen_monitor = _FakeMonitor(_rows_with_boxes(SEARCH_RESULTS))
        got = app._read_social_chat(mon._to_numpy(_page_image(SEARCH_RESULTS)))
        check("★ 页型记 `PAGE_LIST`（事实照记，供上层那道闸用）",
              got.page_kind == PAGE_LIST, repr(got.page_kind))
        check("★ `ChatRead` 被清成空的（搜索结果一行都不留）",
              not got.lines, repr(got.lines))
        check("★ `_tail_context` 一个字都取不出来（「那就周六见啦」进不了材料）",
              _tail_context(got) == "", repr(_tail_context(got)))
        check("★ 取出来的那一份里没有搜索结果的字（微信号 / 昵称都没漏）",
              "微信号" not in _tail_context(got) and "茗风" not in _tail_context(got), "")
        check("前提：这一帧的字确实认出来了（空 read 是那道闸清的，不是没认到）",
              bool(classify_lines(_page_image(SEARCH_RESULTS),
                                  _rows_with_boxes(SEARCH_RESULTS)).lines), "")

        # 手动那条路（用户自己喊「帮我回」）共用同一处
        service._chat_text = "上一场的旧话"
        service._helping.pop(key, None)
        service._last_asked_by_window.pop(key, None)
        service._page_kind = ""
        service._awaiting = False
        service._running = False
        offers.clear()
        service._on_screen_read(got)
        check("★ 手动路记下的「对方原话」是空的（照旧是「没认到」）",
              service._chat_text == "", repr(service._chat_text))
        check("★ 搜索态不冒 offer（那排按钮压根不该出现）", offers == [], repr(offers))
        check("★ 也不落这一场的冷却戳（别把冷却按在没聊起来的页上）",
              key not in service._last_asked_by_window, repr(service._last_asked_by_window))
        check("★ 页型记成非单聊 `PAGE_LIST`", service._page_kind == PAGE_LIST,
              repr(service._page_kind))
        check("★ 这一场没被记进 `_helping`（帮聊窗口的黏连从根上断了）",
              key not in service._helping, repr(service._helping))

        # ---------- ③ 自动那条腿 ----------

        section("③ 自动监听：搜索态那一拍一次都不拟，人切回单聊照旧拟")

        app.config.screen.social_enabled = True
        app.config.screen.social_auto_listen = True
        app.config.screen.ocr_enabled = True

        candidates: list[list[dict]] = []
        app.bus.social_candidates.connect(candidates.append)
        handled: list[str] = []
        app.chatter_service.handle = handled.append
        stub.candidates_result = {"candidates": [
            {"draft": "那我等你消息", "why": "接住对方那把话头"},
        ]}

        frames = {"now": mon._to_numpy(_chat_page_image(peer_tail="我先去忙了"))}
        monitor_holder = {"m": _FakeMonitor(_rows_with_boxes(CHAT_LINES + ["我先去忙了"]))}
        app.screen_monitor = monitor_holder["m"]
        clock = {"t": 1000.0}
        service._grab_chat = lambda: frames["now"]
        service._read_chat = app._read_social_chat        # 真的那一只：闸就在它里面
        service._idle_seconds = lambda: 10.0
        service._clock = lambda: clock["t"]
        # 冒话那道冷却不在这一批用例的射程里（这儿验的是**自动监听**那条线），先按住
        # ——不按住的话，"非单聊 → 单聊"那次跃迁会冒一句邀请，邀请一挂上 `_awaiting`，
        # 后面几拍就全被它挡回去（挡轮询是既有的设计）。跟
        # `test_social_listview_leak.py` 第 432 行那条老规矩同一个写法。
        service._last_asked = clock["t"]
        service._examples_provider = lambda query: ""     # noqa: ARG005
        service._social_foreground = True
        service._last_fingerprint = None
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_listen_at = 0.0
        service._running = False
        service._awaiting = False
        # C-1 收窄：自动那条路只在**进了帮聊窗口之后**才出候选。本节验的是"搜索页那一屏
        # 被闸挡下、切回单聊还照拟"，所以先把这一格填上（真机落戳那一下在
        # `test_social_continuous.py` ① 里验）。
        service._mark_helping()
        service._done = False
        service._last_read_lines = set()

        def _tick() -> None:
            service._poll_listen()
            wait_for(lambda: not service._listen_reading)
            drain_threads()
            pump(0.1)

        try:
            _tick()
            check("前置：第一拍只打底（还没来新消息就不拟）", not candidates,
                  f"{len(candidates)} 组")
            tail_before = service._last_listen_tail

            clock["t"] += 5.0
            frames["now"] = mon._to_numpy(_page_image(SEARCH_RESULTS))
            monitor_holder["m"].lines = _rows_with_boxes(SEARCH_RESULTS)
            _tick()
            check("★ 人在搜索页那一拍：一条候选都不给、一次模型都不发",
                  candidates == [] and stub.prompts == [],
                  f"{len(candidates)} 组 / {len(stub.prompts)} 条")
            check("★ 也没落到通用聊天兜底", handled == [], repr(handled))
            check("★ 没拿搜索结果去拟（提示词里没有「茗风」/「微信号」）",
                  all("茗风" not in p and "微信号" not in p for p in stub.prompts), "")
            # 尾巴那格是**上一场单聊的尾巴**，一个字都没动（这份 read 是空的，
            # `if new_tail:` 为假，压根走不到赋值那一步）——人切回去还接着判得下去。
            # 注意别拿「茗风」当"没顶掉"的判据：单聊的尾巴里本来就带着名字那一行
            # （真机帧上「茗风」是聊天区第一行），该看的是搜索页**专有**的那几个字。
            check("★ 尾巴没被搜索页顶掉（仍是一场单聊的尾巴，人切回去还能接着判）",
                  service._last_listen_tail == tail_before
                  and not any(w in service._last_listen_tail
                              for w in ("微信号", "那就周六见啦", "深圳团建群")),
                  repr(service._last_listen_tail))

            clock["t"] += 5.0
            frames["now"] = mon._to_numpy(_chat_page_image(peer_tail="那周末一起去爬山？"))
            monitor_holder["m"].lines = _rows_with_boxes(
                CHAT_LINES + ["那周末一起去爬山？"])
            _tick()
            check("★ 切回单聊、对方又发一条 → 照旧拟出来（这道闸没把监听卡死）",
                  len(candidates) == 1 and len(stub.prompts) == 1,
                  f"{len(candidates)} 组 / {len(stub.prompts)} 条")
            check("★ 那一趟提示词里是对方的字，不是搜索页的字",
                  bool(stub.prompts) and all("微信号" not in p for p in stub.prompts),
                  (stub.prompts[0][:80].replace("\n", " / ") if stub.prompts else "（空）"))
        finally:
            app.screen_monitor = real_monitor
            service._grab_chat = None
            service._read_chat = None
            service._helping.pop(key, None)

        # ---------- ④ 名字那一格 + 换场 ----------

        section("④ 名字那一格：搜索态下带子不认字，`_cooldown_key()` 退回裸标识")

        check("★ 搜索框那一行过不了名字那道滤（含「搜索」即丢）",
              _peer_name_from_band_text("搜索", "团子") == "", "")
        check("★ 带冒号的也丢（`搜索:` 那种写法）",
              _peer_name_from_band_text("搜索:", "团子") == "", "")
        check("★ 搜索页的分组标题也丢（它不是名字）",
              _peer_name_from_band_text("搜索指定内容", "团子") == "", "")
        check("反面对照：真名字照旧认得出来",
              _peer_name_from_band_text("茗风", "团子") == "茗风", "")
        check("反面对照：主窗上那个应用名不会被当成对方",
              _peer_name_from_band_text("微信", "团子") == "", "")

        read_src = inspect.getsource(app._read_social_chat)
        check("★ 名字只在单聊那一档才认（源码级：认名字那一步挂在 `is_personal_kind` 下）",
              "is_personal_kind(page_kind)" in read_src
              and "_social_peer_name_from_band(band)" in read_src, "")
        # 带子那一步只在单聊那一档才走：把名字缓存预先种上（模拟「带子上认得出一行真名字」），
        # 搜索页那一拍走完，名字格**一个字都不该动**。
        band = _page_image(SEARCH_RESULTS, box_row=False)
        app._social_peer_band_hint = "上一场的名字"
        app._social_peer_band_md5 = _band_md5(band)
        app._social_peer_band_name = "茗风"
        app._social_peer_band = band
        app.screen_monitor = _FakeMonitor(_rows_with_boxes(SEARCH_RESULTS))
        app._read_social_chat(mon._to_numpy(_page_image(SEARCH_RESULTS)))
        check("★ 搜索态那一拍：带子认得出一行真名字，名字格照样一个字不动",
              app._social_peer_band_hint == "上一场的名字",
              repr(app._social_peer_band_hint))
        app._social_peer_band = band
        app.screen_monitor = _FakeMonitor(_rows_with_boxes(CHAT_LINES))
        app._read_social_chat(mon._to_numpy(_chat_page_image()))
        check("反面对照：单聊那一拍名字照旧认得进名字格（带子那条路没被整条关掉）",
              app._social_peer_band_hint == "茗风",
              repr(app._social_peer_band_hint))
        app.screen_monitor = real_monitor

        service._peer_name = ""
        service._session_provider = lambda: "甲"
        service._peer_provider = lambda: ""
        service._sync_session()
        check("★ 名字取不到时 `_cooldown_key()` 退回裸会话标识",
              service._cooldown_key() == "甲", repr(service._cooldown_key()))

        service._session_provider = lambda: "甲"
        service._sync_session()
        check("★ 同一个人 → 不判换场（搜索页那几行字不参与这件事）",
              service._sync_session() is False, "")
        service._last_draft_source = "上一场拟过的那句"
        service._session_provider = lambda: "乙"
        check("★ 真换了人 → 照旧判换场、上一场的拟稿记忆一并清掉",
              service._sync_session() is True and service._last_draft_source == "", "")
        check("★ 换了人那把键也跟着换成新标识（乙）",
              service._cooldown_key() == "乙", repr(service._cooldown_key()))
        service._session_provider = lambda: ""
        check("★ 会话标识取不到（搜索页上认不出来）→ 什么都不动（宁可漏清一场）",
              service._sync_session() is False and service._cooldown_key() == "乙",
              repr(service._cooldown_key()))

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：两库零写入、兜底没删、老腿没动、零新依赖")

        drain_threads()
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        a = _find(src, "def looks_like_list_page")
        b = _find(src, "def looks_like_list_boxes")
        lp_body = src[a:b] if (a >= 0 and b > a) else ""
        i_load = _find(lp_body, "looks_like_loading_page(chat)")
        i_search = _find(lp_body, "looks_like_search_page(chat)")
        check("★ 搜索腿挂在 `looks_like_list_page` 的**末尾**（前三条腿一条都没挪）",
              i_load >= 0 and i_search > i_load, f"{i_load} / {i_search}")
        check("★ 前三条腿的判据一个字没动（时间戳 ≥5 / 列表字面两类 / 加载屏）",
              "rows >= LIST_PAGE_TIME_ROWS" in lp_body
              and "_list_mark_kinds(bodies) >= LIST_PAGE_MARK_KINDS" in lp_body
              and "def looks_like_loading_page(read" in src, "")
        c = _find(src, "def classify_chat_page")
        d = _find(src, "def is_personal_kind")
        cc_body = src[c:d] if (c >= 0 and d > c) else ""
        check("★ `classify_chat_page` 一个字没改（搜索态是靠 16-β 那一刀落 `PAGE_LIST`）",
              "looks_like_search_page" not in cc_body
              and "return PAGE_PERSONAL" in cc_body, "")
        check("★ 账号选择腿 / 登录腿 / 群聊两条腿都在原地",
              "looks_like_account_pick_page(chat)" in cc_body
              and "_LOGIN_SUBSTR_NAMES" in cc_body
              and "GROUP_NOTICE_LINE" in cc_body, "")
        # 装配层那条顺序：**先判页型、再清成空 read、最后把页型挂上去**——顺序一乱，
        # 搜索态这一刀就白挨（清完之后那份 read 在文本层什么都看不出来了）。
        # 认字之后那一处要**从那条闸往里数**：`read = ChatRead()` 在几何早退那一支里
        # 还有一个（`looks_like_list_boxes` 命中），不锚定就会数到那一个上。
        i_kind = _find(read_src, "page_kind = classify_chat_page(read)")
        i_gate = _find(read_src, "if looks_like_list_page(read):", max(i_kind, 0))
        i_clear = _find(read_src, "read = ChatRead()", max(i_gate, 0))
        i_attach = _find(read_src, "read.page_kind = page_kind", max(i_clear, 0))
        check("★ 装配层顺序没动：先判页型 → 再清成空 read → 最后挂页型",
              -1 < i_kind < i_gate < i_clear < i_attach,
              f"{i_kind} / {i_gate} / {i_clear} / {i_attach}")
        check("★ 这一批只多了一个判据（`looks_like_search_page` 全仓一处定义）",
              src.count("def looks_like_search_page(read") == 1, "")
        check("★ 不模拟按键 / 不碰剪贴板（这一条改动一个都没碰）",
              not any(w in src for w in ("SendInput", "keybd_event", "uiautomation",
                                         "pyautogui", "pynput", "win32clipboard")), "")
        names = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "keyboard", "pynput", "pyautogui", "uiautomation"}
        check("★ 零新依赖（`social.py` 这一批没多一个第三方 import）",
              not (names & banned), "、".join(sorted(names & banned)) or "（一处都没有）")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("微信搜索态不再被当成单聊（v2-social-search-state）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
