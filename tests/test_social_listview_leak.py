"""
会话总列表被当成 1:1 聊天（v2-social-listview-leak-fix 16-β）的自动测试。

**真机坐实**（2026-10-08 12:32:59 的取证图 `pet_social_leak_20261008_123259.png`，帧
742×1342）：这台机器上微信是**窄版单页**（左边没有独立的会话列表列），那一帧**整页就是
会话总列表**——`chat_area_box` 只切掉左侧 92px 的图标栏，于是整页进了 OCR。后面这条链
是真 OCR 实测出来的，不是推演：

    37 行 / 对方 37 / 自己 0     ← 列表每一行都打在白底上，`is_other_bubble` 一律判"对方"
    tail_sender='other'          ← 末行「06:54」也是白底
    _tail_context → '茗风'        ← 末尾那个纯时间戳被 `_is_chat_chrome` 当装潢挑掉，
                                    于是"对方原话"落到上一行＝**昵称**

对照两张真单聊帧：`对方 26 / 自己 7`（33 行）与 `对方 11 / 自己 7`（18 行），
两边纯时间行都只有 **3** 条；三张列表帧是 **12 / 12 / 12** 条。

本测试钉三件事：

**① 判据**（`looks_like_list_page`，文本层）：**两条腿，任一条成立**且自己 0 行 ——

    一、纯时间行 ≥ `LIST_PAGE_TIME_ROWS`（5）。真机读数 12 vs 3 —— **绝对数分得开**；
        比例那条分不开（22% vs 32%），所以专门不取。
    二、**列表专有字面**凑够 `LIST_PAGE_MARK_KINDS`（2）**类**
        （v2-social-listview-leak-fix-judge）：账号号（`公众号` / `服务号`…）、未读计数
        （`[17条]`）、群名短行。这条管的是**时间列压根没读出来**的帧——真机 2026-10-08
        14:27:49 / 14:29:54 两帧就是这样（0 条与 3 条纯时间行，老判据全瞎）。

**② 落点**（`core.bootstrap._read_social_chat`）：命中就把这一份 `ChatRead` 清成空的，
**自动与手动两条路共用**同一个空 read —— 不 `_tail_context`、不 `_auto_generate`，
列表里的昵称 / 预览行一个都不进材料。

**③ 不许误伤**："只收对方话、我一句没说"的单聊（自己 0 行）是本批最要紧的一档：它绿气泡
一个没有，像素那条（`social_window.looks_like_chat_list`）**分不开**；这一条靠时间戳密度
分开（真机只有 3 条）。③ 专门钉了它，另有"正好 5 条 / 只有 4 条"的边界。

红线：零新依赖（判据只用了模块里既有的 `re` / `as_chat_read`）；pet.db / mind.db 零写入；
不模拟按键、不碰剪贴板、不读窗口标题；`_classify_incoming` / `_is_duplicate_source` /
两道冷却 / §D 那条 break 一分没动（⑥ 逐条钉）。
"""

from __future__ import annotations

import re
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
    LIST_PAGE_MARK_KINDS,
    LIST_PAGE_TIME_ROWS,
    SocialReplyService,
    _tail_context,
    looks_like_list_page,
)
from life.screen.social_window import (  # noqa: E402
    SENDER_OTHER,
    SENDER_USER,
    ChatRead,
    classify_lines,
)

W, H = 742, 1342
DPR = 1.5
WHITE = QColor(255, 255, 255)
INK = QColor(25, 25, 25)
# 自己那半边：`is_self_bubble` 的色相判据（G≥150、比另一个通道高 ≥40、亮度 ≥170）全过
GREEN = QColor(149, 236, 105)

# 真机那一帧上的行（照抄实测：12 行会话 + 每行一条时间戳）。最后那行就是要拦的那条：
# 昵称「茗风」在时间戳上面，时间戳被当装潢挑掉之后，"对方原话"就落在它身上。
LIST_ROWS = [
    ("GitMindAI思想星球", "12:11", "刚刚！GPT-6来了，免费用户也能用！"),
    ("家庭群", "12:31", "[6条]nemophilist：谁在群里发的那个"),
    ("公司AI小组", "12:10", "MOO"),
    ("老同学", "12:06", "[22条]天冷加衣别忘了"),
    ("妈妈", "12:04", "[3条]周六回家吃饭"),
    ("楼下超市", "11:42", "您给的那份材料我看了"),
    ("健身教练", "11:14", "明天的课改到三点"),
    ("同事小李", "11:08", "[18条]今天的周报记得交"),
    ("房东", "11:08", "[15条]水电费这个月涨了"),
    ("物业管家", "10:14", "您好，想提醒您现在点击链接报名后即可"),
    ("快递驿站", "10:04", "您的包裹已到驿站"),
    ("茗风", "06:54", "人家女孩子脸皮薄，想不想接受也得先看看你的行动呀。"),
]
# 真单聊帧上的行：纯时间行只有 3 条
CHAT_OTHER = [
    "哦，什么样的人才是合适的呢？",
    "那你自己心里有没有挺中意的一个呀",
    "人家女孩子脸皮薄，想不想接受也得先看看",
    "我这是正常发挥",
    "看你的行动呀。",
]
CHAT_SELF = [
    "我在慢慢了解",
    "不越界。",
    "先看行动这句话我记下了",
]
CHAT_TIMES = ["昨天14:22", "昨天19:05", "06:51"]


def _list_page_image() -> QImage:
    """会话总列表那一屏：白底 + 每行一串深字（昵称:预览）+ 最右一列时间戳。

    行距 100（真机量到的节奏），每行的字都打在白底上——这正是色卡把 37 行全判成
    "对方说的"的来路。
    """
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(WHITE)
    p = QPainter(img)
    for i in range(len(LIST_ROWS)):
        y = 120 + i * 100
        p.fillRect(QRect(110, y, 150, 26), INK)       # 昵称（第一行，右端跟时间戳同一行）
        p.fillRect(QRect(690, y, 40, 24), INK)        # 时间戳
        p.fillRect(QRect(110, y + 30, 280, 26), INK)  # 预览（这一行的第二行）
    p.end()
    return img


def _list_page_lines() -> list:
    """跟上面那屏对齐的 OCR 行：一行三条（昵称 / 时间戳 / 预览），跟真机读数同构。

    真机那一帧的 OCR 顺序就是这么来的（`GitMindAl思想星球` → `12:11` → 预览那句），
    **最后一行没有预览那条**——真机末尾三行是 `…邀约！` / `茗风` / `06:54`，
    所以"末行时间戳被当装潢挑掉之后，材料落在昵称上"这件事就是这么发生的。
    """
    out = []
    for i, (name, ts, preview) in enumerate(LIST_ROWS):
        y = 120 + i * 100
        out.append(([110, y, 260, y + 26], name))
        out.append(([690, y, 730, y + 24], ts))
        if i + 1 < len(LIST_ROWS):          # 末行不带预览（跟真机那一帧一样）
            out.append(([110, y + 30, 430, y + 56], preview))
    return out


def _chat_page_image(other_n: int, self_n: int, times_n: int, peer_tail: str = "") -> QImage:
    """一场单聊：白底上对方的气泡 + 绿块上自己的气泡 + 中间夹几条纯时间行。

    `peer_tail` 非空就在最底下再补一条对方气泡——**末尾那条必须是对方发的**，
    不然归属判成 "self"，自动那条路根本不会拟。真机上"对方刚回一句"就是这个形状。
    """
    img = QImage(W, H, QImage.Format_RGB32)
    img.fill(WHITE)
    p = QPainter(img)
    y = 100
    for _ in range(other_n):
        p.fillRect(QRect(70, y, 300, 34), INK)                    # 对方：白底深字
        y += 70
    for _ in range(times_n):
        p.fillRect(QRect(345, y, 60, 22), INK)                    # 时间行：也在白底上
        y += 50
    for _ in range(self_n):
        p.fillRect(QRect(300, y, 330, 70), GREEN)                 # 自己：整块绿底
        p.fillRect(QRect(320, y + 16, 290, 34), INK)
        y += 90
    if peer_tail:
        p.fillRect(QRect(70, y, 300, 34), INK)
    p.end()
    return img


def _chat_page_lines(other_n: int, self_n: int, times_n: int, peer_tail: str = "") -> list:
    out = []
    y = 100
    oi = si = ti = 0
    for _ in range(other_n):
        out.append(([70, y, 370, y + 34], CHAT_OTHER[oi % len(CHAT_OTHER)]))
        oi += 1
        y += 70
    for _ in range(times_n):
        out.append(([345, y, 405, y + 22], CHAT_TIMES[ti % len(CHAT_TIMES)]))
        ti += 1
        y += 50
    for _ in range(self_n):
        out.append(([320, y + 16, 610, y + 50], CHAT_SELF[si % len(CHAT_SELF)]))
        si += 1
        y += 90
    if peer_tail:
        out.append(([70, y, 370, y + 34], peer_tail))
    return out


class _FakeMonitor:
    """替掉真 monitor：认字那一步直接回摆好的行（真机那几张帧的读数）。"""

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


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- ① 判据：真机读数照抄进来 ----------

        section("① 判据（文本层）：纯时间行 ≥5 且自己 0 行 → 判总列表")

        check("阈值就是 5（真机列表 12 条 vs 单聊 3 条，绝对数分得开）",
              LIST_PAGE_TIME_ROWS == 5, str(LIST_PAGE_TIME_ROWS))

        list_img = _list_page_image()
        list_read = classify_lines(list_img, _list_page_lines())
        n_other = sum(1 for s, _t in list_read.lines if s == SENDER_OTHER)
        n_user = sum(1 for s, _t in list_read.lines if s == SENDER_USER)
        # 不借实现里那个正则，自己数一遍（`12:11` 这种整行就是一个时间）
        n_time = sum(1 for _s, t in list_read.lines
                     if re.fullmatch(r"\d{1,2}:\d{2}", t.strip()))
        check("前提：列表那一屏的每一行都被色卡认成「对方说的」（真机 37/37 同款）",
              n_other == len(list_read.lines) == 35 and n_user == 0,
              f"对方 {n_other} / 自己 {n_user} / 共 {len(list_read.lines)}")
        check("前提：这一屏上有 12 条整行就是一个时间的行（真机 12 条那个数）",
              n_time == 12, str(n_time))
        check("★ 这一屏判得出来：是会话总列表", looks_like_list_page(list_read) is True, "")
        check("★ 老口径确实会拿昵称当对方原话（这就是要拦的那件事，不是空想）",
              _tail_context(list_read) == "茗风", repr(_tail_context(list_read)))

        # ---------- ② 落点：装配层这一拍回的是空 read ----------

        section("② 落点：认出总列表就把这一份 ChatRead 清成空的")

        list_arr = mon._to_numpy(list_img)
        real_monitor = getattr(app, "screen_monitor", None)
        app.screen_monitor = _FakeMonitor(_list_page_lines())
        try:
            got = app._read_social_chat(list_arr)
            check("★ 回的是空 read：一行都没有、`visible` 空",
                  not got.lines and not got.visible and not got.raw,
                  f"lines={got.lines} raw={got.raw!r}")
            check("★ `_tail_context` 一个字都取不出来（昵称进不了材料）",
                  _tail_context(got) == "", repr(_tail_context(got)))
            check("★ 列表那行预览也一个字都不在（`点击链接报名后…` 那类）",
                  "点击链接" not in got.visible and "点击链接" not in got.context, "")
            check("★ 前置：认出来的字里确实有「茗风」（空 read 是这道闸拦的，不是没认到）",
                  any("茗风" in t for _s, t in list_read.lines), "")

            # 反向对照：换成真单聊那一屏（末条是对方刚回的那句），照旧把字认回来
            chat_img = _chat_page_image(other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF),
                                        times_n=len(CHAT_TIMES), peer_tail="周末一起吃个饭？")
            app.screen_monitor = _FakeMonitor(
                _chat_page_lines(other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF),
                                 times_n=len(CHAT_TIMES), peer_tail="周末一起吃个饭？"))
            chat_read = app._read_social_chat(mon._to_numpy(chat_img))
            check("反向对照：单聊那一屏照旧读出字来（这道闸不是把认字整条关掉）",
                  bool(chat_read.lines) and looks_like_list_page(chat_read) is False,
                  f"{len(chat_read.lines)} 行 / 判列表={looks_like_list_page(chat_read)}")
            check("反向对照：取出来的是对方末句",
                  "周末一起吃个饭？" in _tail_context(chat_read),
                  repr(_tail_context(chat_read)))

            # ---------- ③ 不许误伤：只收对方话的单聊 ----------

            section("③ 不许误伤：「只收对方话、我一句没说」的单聊（自己 0 行）")

            app.screen_monitor = _FakeMonitor(
                _chat_page_lines(other_n=11, self_n=0, times_n=len(CHAT_TIMES),
                                 peer_tail="那周末一起去爬山？"))
            peer_only = app._read_social_chat(mon._to_numpy(
                _chat_page_image(other_n=11, self_n=0, times_n=len(CHAT_TIMES),
                                 peer_tail="那周末一起去爬山？")))
            check("前提：这一屏自己 0 行（真机 12:32:17 那类「只收对方话」的屏）",
                  sum(1 for s, _t in peer_only.lines if s == SENDER_USER) == 0,
                  f"{len(peer_only.lines)} 行 / 自己 0")
            check("★ 不判成列表（绿气泡一个没有，靠时间戳密度分开：只有 3 条）",
                  looks_like_list_page(peer_only) is False, "")
            check("★ 材料照旧取得到（宠物最该帮忙的那一档没被误伤）",
                  _tail_context(peer_only) != "", repr(_tail_context(peer_only)))
        finally:
            if real_monitor is not None:
                app.screen_monitor = real_monitor

        # 判据的边界（不走画面，直接摆 read）
        five = ChatRead(lines=[(SENDER_OTHER, "在吗")] + [(SENDER_OTHER, "12:11")] * 5)
        four = ChatRead(lines=[(SENDER_OTHER, "在吗")] + [(SENDER_OTHER, "12:11")] * 4)
        check("边界：正好 5 条纯时间行 → 判列表", looks_like_list_page(five) is True, "")
        check("边界：只有 4 条 → 不判列表", looks_like_list_page(four) is False, "")
        veto = ChatRead(lines=[(SENDER_OTHER, "12:11")] * 9 + [(SENDER_USER, "我发的")])
        check("★ 一票否决：自己发过一句话（绿气泡）时，时间行再多也不判列表",
              looks_like_list_page(veto) is False, "")
        check("空 read / 拿不到 → 不判列表（不误伤正常那一拍）",
              looks_like_list_page(ChatRead()) is False
              and looks_like_list_page("") is False, "")
        real_line = ChatRead(lines=[(SENDER_OTHER, "14:22"), (SENDER_OTHER, "我下午3:00到")])
        check("人话里带时间的那行**不算**纯时间行（整行相等才算）",
              looks_like_list_page(real_line) is False, "")

        # ---------- ③-2 第二条腿：时间列没读出来的列表帧（真机 142749 / 142954）----------
        #
        # 这两帧是真机同一场会话里落下来的（%TEMP%/pet_social_leak_20261008_142749.png /
        # _142954.png），人工按认出来的字标过：**都是会话总列表**。老判据在两帧上都回 False
        # ——142749 一条纯时间行都没有（窄版单页把每行读成"昵称行 + 预览行"），142954 只有
        # 3 条，够不着阈值 5。下面这些 read 就是那两帧的**真读数**（逐行照抄，见
        # %TEMP%/p18/judge_verify.txt）。

        section("③-2 第二条腿：列表专有字面凑够两类就判列表（v2-social-listview-leak-fix-judge）")

        check("新门槛是 2 **类**（不是 2 行）", LIST_PAGE_MARK_KINDS == 2,
              str(LIST_PAGE_MARK_KINDS))

        narrow = ChatRead(lines=[
            (SENDER_OTHER, "茗风"),
            (SENDER_OTHER, "好"),
            (SENDER_OTHER, "宝安固戌地铁站周边二手闲置2群"),
            (SENDER_OTHER, "[17条]阳光阴暗小子：2.5的水箱，满了拿出来倒水"),
            (SENDER_OTHER, "服务号"),
            (SENDER_OTHER, "交通银行微银行：1元-88元还款红包，金秋上福利！（"),
            (SENDER_OTHER, "公众号"),
            (SENDER_OTHER, "[2条]深圳周末去哪玩：1000杯星巴克免费喝！10月8日"),
        ])
        check("★ 142749 那一帧（0 条纯时间行）现在判得出来：是会话总列表",
              looks_like_list_page(narrow) is True, "")
        check("前提：这一帧只靠时间戳那条腿判不出来（把它换成没有那三类标记的同一屏就回 False）",
              looks_like_list_page(ChatRead(lines=[
                  (SENDER_OTHER, "茗风"),
                  (SENDER_OTHER, "好"),
                  (SENDER_OTHER, "宝安固戌地铁站周边二手闲置2"),
                  (SENDER_OTHER, "阳光阴暗小子：2.5的水箱，满了拿出来倒水"),
                  (SENDER_OTHER, "交通银行微银行：1元-88元还款红包，金秋上福利！（"),
                  (SENDER_OTHER, "张总一悟 (何文镜业)"),
                  (SENDER_OTHER, "好的。"),
                  (SENDER_OTHER, "深圳周末去哪玩：1000杯星巴克免费喝！10月8日"),
              ])) is False, "")

        three = ChatRead(lines=[
            (SENDER_OTHER, "GitMindAI思想星球"),
            (SENDER_OTHER, "14:23"),
            (SENDER_OTHER, "Claude刷新物理学世界纪录，真算出来了"),
            (SENDER_OTHER, "14:24"),
            (SENDER_OTHER, "服务号"),
            (SENDER_OTHER, "[17条]阳光阴暗小子：2.5的水箱，满了拿出来倒水"),
            (SENDER_OTHER, "14:16"),
            (SENDER_OTHER, "艾邦AI戒指产业交流群"),
        ])
        check("★ 142954 那一帧（3 条纯时间行，够不着 5）现在也判得出来",
              looks_like_list_page(three) is True, "")
        check("★ 但没有那三类标记时，3 条时间戳照旧不判列表（单聊那条老边界一个字没动）",
              looks_like_list_page(ChatRead(lines=[
                  (SENDER_OTHER, "14:23"),
                  (SENDER_OTHER, "在吗"),
                  (SENDER_OTHER, "14:24"),
                  (SENDER_OTHER, "嗯"),
                  (SENDER_OTHER, "14:16"),
                  (SENDER_OTHER, "那我先忙了"),
              ])) is False, "")

        # 门槛是"类"不是"行"——同一类凑几行都不算
        check("★ 同一类标记出现两行**不算**（要的是两类，不是两行）",
              looks_like_list_page(ChatRead(lines=[
                  (SENDER_OTHER, "你看这个公众号"),
                  (SENDER_OTHER, "那个公众号也行"),
                  (SENDER_OTHER, "在吗"),
              ])) is False, "")
        check("★ 未读标记两行也只算一类（同上）",
              looks_like_list_page(ChatRead(lines=[
                  (SENDER_OTHER, "[2条]你看这个"),
                  (SENDER_OTHER, "[3条]真的假的"),
              ])) is False, "")
        check("★ 人话拿「群」字收尾也不算（群名那一类只认短行，且单类不够门槛）",
              looks_like_list_page(ChatRead(lines=[
                  (SENDER_OTHER, "你加一下那个群"),
                  (SENDER_OTHER, "好的"),
              ])) is False, "")
        check("★ 一票否决照旧在最前面：自己发过话，三类标记全在也不判列表",
              looks_like_list_page(ChatRead(lines=narrow.lines + [
                  (SENDER_USER, "我发的这条"),
              ])) is False, "")
        check("空 read 照旧不判列表（第二条腿也不误伤正常那一拍）",
              looks_like_list_page(ChatRead()) is False, "")

        # ---------- ④ 端到端：自动监听那一拍不拟 ----------

        section("④ 自动监听：聊天屏 → 切到总列表 → 一次都不拟，尾巴也不推进")

        service: SocialReplyService = app.social_reply
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

        chat_lines_a = _chat_page_lines(other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF),
                                        times_n=len(CHAT_TIMES), peer_tail="我先去忙了")
        chat_lines_b = _chat_page_lines(other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF),
                                        times_n=len(CHAT_TIMES), peer_tail="那周末一起去爬山？")
        frames = {"now": mon._to_numpy(_chat_page_image(
            other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF), times_n=len(CHAT_TIMES),
            peer_tail="我先去忙了"))}
        monitor_holder = {"m": _FakeMonitor(chat_lines_a)}
        app.screen_monitor = monitor_holder["m"]        # 认字那一手走真装配层那条路
        clock = {"t": 1000.0}
        service._grab_chat = lambda: frames["now"]
        service._read_chat = app._read_social_chat      # 真的那一只：闸就在它里面
        service._idle_seconds = lambda: 10.0
        service._clock = lambda: clock["t"]
        service._examples_provider = lambda query: ""
        service._social_foreground = True
        service._last_fingerprint = None
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_listen_at = 0.0
        service._running = False
        service._awaiting = False
        service._done = False
        service._last_read_lines = set()

        def _tick() -> None:
            service._poll_listen()
            wait_for(lambda: not service._listen_reading)
            drain_threads()
            pump(0.1)

        try:
            _tick()                                     # 先打底
            check("前置：第一拍只打底（还没来新消息就不拟）", not candidates,
                  f"{len(candidates)} 组")

            clock["t"] += 5.0
            frames["now"] = mon._to_numpy(_list_page_image())   # 人切到会话总列表那一屏
            monitor_holder["m"].lines = _list_page_lines()
            _tick()
            check("★ 切到总列表这一拍：一条候选都不给、一次模型都不发",
                  candidates == [] and stub.prompts == [],
                  f"{len(candidates)} 组 / {len(stub.prompts)} 条")
            check("★ 也没落到通用聊天兜底", handled == [], repr(handled))
            check("★ 没拿列表里的昵称去拟（提示词里没有「茗风」）",
                  all("茗风" not in p for p in stub.prompts), "")
            check("★ 尾巴没被列表那一屏顶掉（仍然是一场单聊的尾巴，人切回去还能接着判）",
                  "茗风" not in service._last_listen_tail,
                  repr(service._last_listen_tail))

            clock["t"] += 5.0
            frames["now"] = mon._to_numpy(_chat_page_image(
                other_n=len(CHAT_OTHER), self_n=len(CHAT_SELF), times_n=len(CHAT_TIMES),
                peer_tail="那周末一起去爬山？"))
            monitor_holder["m"].lines = chat_lines_b
            _tick()
            check("★ 切回聊天窗、对方又发一条 → 照旧拟出来（这道闸没把监听卡死）",
                  len(candidates) == 1 and len(stub.prompts) == 1,
                  f"{len(candidates)} 组 / {len(stub.prompts)} 条")
            check("★ 那一趟提示词里是对方的字，不是列表的字",
                  bool(stub.prompts) and "茗风" not in stub.prompts[0]
                  and "点击链接" not in stub.prompts[0],
                  (stub.prompts[0][:100].replace("\n", " / ") if stub.prompts else "（空）"))

            # ---------- ⑤ 手动路（用户自己喊「帮我回」）拿到的也是空 read ----------

            section("⑤ 手动那条路共用同一处：非 active 视图上不把列表的字当对方原话")

            app.screen_monitor = _FakeMonitor(_list_page_lines())
            service._chat_text = "上一场的旧话"
            service._on_screen_read(app._read_social_chat(mon._to_numpy(_list_page_image())))
            check("★ 手动路记下的「对方原话」是空的（照旧是「没认到」）",
                  service._chat_text == "", repr(service._chat_text))
            check("★ 也没有拿列表的昵称顶上", "茗风" not in service._chat_text, "")
        finally:
            if real_monitor is not None:
                app.screen_monitor = real_monitor

        # ---------- ⑥ 红线 ----------

        section("⑥ 红线：零新依赖 / 两库零写入 / 老口径没动")

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        import ast
        import inspect

        from life.screen import social as smod

        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(smod))):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2"}
        check("判据那一层没有新引入第三方库", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        src = inspect.getsource(smod)
        check("§D 那条 break / 去重 / 冷却的老口径没被顺手改（句还在）",
              "_is_duplicate_source" in src and "LISTEN_COOLDOWN_S" in src, "")
        check("`_classify_incoming` 那两条腿一个没动（还是色卡在前、空闲在后）",
              'return "incoming"' in src and "if idle_sec < SEND_IDLE_S:" in src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before, "")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("会话总列表被当 1:1 聊天（v2-social-listview-leak-fix 16-β）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
