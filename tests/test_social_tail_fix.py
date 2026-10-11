"""社交"材料取哪一段"的治本修复（v2-social-tail-context-fix）的自动测试。

真机现象（2026-10-07 14:23，微信茗风场，截图坐实）：宠物**一直开着**、对方明明刚回了
一句「哟，你小子这么会，是在撩我么？」，它却拿"忙 / 累 / 没空"那几句零散话去拟稿。
这一轮掀了两张桌子，两处各一条：

**① 抓屏那一刀切错了（主因）。** 真机那扇独立聊天窗（759×1352 物理像素、DPR 1.5）上，
图标栏右缘 ≈x=88，聊天区底色从 x=90 就开始了，可从底色到**对方头像**只有约 30 物理
像素——`detect_chat_left` 那条"连着 40 逻辑像素（=60 物理像素）都空"的闸一条都过不了，
于是 `chat_area_box` 退回按版面硬切（图标栏 + 会话列表 = 465 物理像素），把聊天区左边
三分之二连同**每行开头的字**一起切掉，OCR 只认回各行的尾巴：

    旧：'啦。但我看你 / 息 / 呢？ / 看不 / 套的呀 / 我么？'      ← 真机那一屏跑出来的原样
    新：'哟，你小子这么会，是在撩我么？'

**② 材料取的是"整屏对方的话"。** 时间戳（`昨天22:09` / `00:19` / `12:53` / `14:22`）、
撤回提示（`你撤回了一条消息`）跟真话一起进了材料，而且**已经回过一遍的旧话**也在里面。

为什么这两件事必须一起修：只修 ① 是"把碎片拼回整句"，只修 ② 是"从一锅粥里捞大块的
——可那锅粥本来就是碎的"。两处都改完，材料才是那**一句**。

红线：零新依赖（新加的只有标准库 `re`）；pet.db / mind.db 零写入；不模拟按键、不碰剪贴板、
不读窗口标题；`_classify_incoming` / `_is_duplicate_source` / 两道冷却 / 气泡色卡一分没动
（④ 逐条钉了 tripwire）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    ensure_app,
    make_env,
    pump,
    section,
    summary,
    wait_for,
)

from PySide6.QtGui import QColor, QImage, QPainter  # noqa: E402

from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    LISTEN_COOLDOWN_S,
    SOCIAL_DONE_COOLDOWN_S,
    _is_chat_chrome,
    _tail_context,
)
from life.screen.social_window import (  # noqa: E402
    BG_BAND,
    BG_RUN_MIN_PX,
    BG_RUN_PX,
    CHAT_LIST_PX,
    SIDEBAR_PX,
    SENDER_OTHER,
    SENDER_UNKNOWN,
    SENDER_USER,
    ChatMessage,
    ChatRead,
    _column_is_background,
    _dominant_rgb,
    chat_area_box,
    chat_client_rect,
    detect_chat_left,
)

# ---------- 真机量到的版面（2026-10-07，本机 DPR=1.5，微信 4.x 独立聊天窗） ----------
#
# 这几个数是**量出来的**（逐列剖面，见收尾报告第一百四十二节），不是照常量抄的：抄常量
# 等于拿被测对象画被测对象，第二条腿一坏整节还是绿（v2-social-input-guard 修过同一处假绿）。
REAL_W, REAL_H = 737, 1339      # 那扇窗的物理像素（截图 759×1352，边上被裁掉几个像素）
REAL_SIDEBAR = 88               # 图标栏右缘（60 逻辑像素 × 1.5 ≈ 90）
REAL_LEFT = 90                  # 聊天区底色从这一列开始
REAL_AVATAR = 120               # 对方头像左缘——底色只剩 30 物理像素
REAL_BUBBLE = 195               # 对方气泡左缘（头像 + 一道缝）

SIDEBAR_RGB = QColor(220, 220, 225)     # 真机图标栏
CHAT_RGB = QColor(250, 250, 250)        # 真机聊天区底色
PEER_RGB = QColor(255, 253, 247)        # 真机对方气泡
SELF_RGB = QColor(157, 242, 159)        # 真机绿气泡
AVATAR_RGB = QColor(176, 198, 222)      # 头像（随便一个"绝不是底色"的颜色）
TEXT_RGB = QColor(60, 60, 60)           # 气泡里那行字（真机上是字，这里是同宽的深色条）
LIST_RGB = QColor(243, 243, 243)        # 真机会话列表底色（跟聊天区底色差 7 > BG_TOL 6）

DPR = 1.5
HARD_CUT = int((SIDEBAR_PX + CHAT_LIST_PX) * DPR)      # 按版面硬切 = 465


def _real_window() -> QImage:
    """照真机那扇独立聊天窗的几何画一帧：图标栏 + 一条窄到底色的缝 + 对方头像 + 气泡。

    这一帧要复现的**只有一个形状**：聊天区底色从图标栏右缘起、到对方头像只有 30 物理
    像素。40 逻辑像素那条长闸因此一条都过不了——真机上就卡在这儿。

    气泡里那行字画成深色条是**必须的**：真机上"没字"的列只有图标栏右边那 30 个像素，
    别处不是头像就是字；这里要是把气泡画成一块纯色，`is_other_bubble` 那类浅色底在本模块
    的容差里跟聊天区底色差不到 `BG_TOL`，反而会拼出一长条"空列"，把长闸喂成"中了"
    （第一版就这么假绿过一次：量出来 170，而不是真机的 None）。
    """
    image = QImage(REAL_W, REAL_H, QImage.Format_RGB32)
    image.fill(CHAT_RGB)
    painter = QPainter(image)
    painter.fillRect(0, 0, REAL_SIDEBAR, REAL_H, SIDEBAR_RGB)
    y = int(REAL_H * 0.30)
    for _ in range(7):                                   # 对方那几条：头像 + 气泡 + 那行字
        painter.fillRect(REAL_AVATAR, y, 50, 50, AVATAR_RGB)
        painter.fillRect(REAL_BUBBLE, y + 4, 300, 52, PEER_RGB)
        painter.fillRect(REAL_BUBBLE + 14, y + 16, 260, 20, TEXT_RGB)
        y += 96
    painter.fillRect(REAL_W - 60 - 240, int(REAL_H * 0.28), 240, 52, SELF_RGB)
    painter.fillRect(REAL_W - 60 - 226, int(REAL_H * 0.28) + 12, 200, 20, TEXT_RGB)
    painter.end()
    return image


def _list_window() -> QImage:
    """会话列表那种版面（主窗）：图标栏 + 250 逻辑像素宽的列表 + 聊天区。

    列表底色跟聊天区底色差 7（真机上就是 250 与 243 这种），所以列表那一段的列**一列都
    不算"空列"**。这一帧证的是：**第二条腿的落脚点（图标栏右缘那 18 个物理像素）正好在
    列表里**，它在主窗上根本无从响起来。
    """
    image = QImage(1140, 1350, QImage.Format_RGB32)
    image.fill(CHAT_RGB)
    painter = QPainter(image)
    sidebar = int(SIDEBAR_PX * DPR)
    list_w = int(CHAT_LIST_PX * DPR)
    painter.fillRect(0, 0, sidebar, 1350, SIDEBAR_RGB)
    painter.fillRect(sidebar, 0, list_w, 1350, LIST_RGB)
    y = 60
    for _ in range(9):                                   # 列表里每一行都有头像有字
        painter.fillRect(sidebar + 16, y, 88, 88, AVATAR_RGB)
        painter.fillRect(sidebar + 118, y + 8, list_w - 150, 34, QColor(150, 150, 150))
        painter.fillRect(sidebar + 118, y + 52, list_w - 180, 30, QColor(170, 170, 170))
        y += 132
    chat_x0 = sidebar + list_w
    y = 120
    for _ in range(7):                                   # 聊天区：缝 20 物理像素，然后气泡
        painter.fillRect(chat_x0 + 20, y, 300, 52, PEER_RGB)
        y += 96
    painter.end()
    return image


def _rows(db_path, table: str) -> int:
    import sqlite3
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


# ---------- 真机那一屏的形状（**内容是自造的**：真机上那几句是用户的私事，不进仓库） ----------
#
# 结构照搬那张图：跨天时间戳 / 撤回提示 / 我方绿气泡 / 对方白气泡混排。色卡把这些非气泡行
# 一律认成 other（真机认出来就是 `昨天22:09` / `00:19` / `12:53` / `14:22` / `你撤回了一条
# 消息` 五条全 other），所以这里也照那个口径摆——要验的正是"认成 other 之后，材料这一层
# 还得把它们挑出去"。
NOISE = ("昨天22:09", "00:19", "12:53", "14:22")
RECALL = "你撤回了一条消息"
ANSWERED = "那我们改天再约吧，你先把事情忙完"      # 已经回过的那句旧话（旧口径会把它带上）
NEWEST = "哟，那你这话是冲着谁说的？"
SCREEN_BASE = [
    (SENDER_OTHER, "昨天22:09"),
    (SENDER_USER, "行，这条我认"),
    (SENDER_OTHER, "00:19"),
    (SENDER_OTHER, ANSWERED),
    (SENDER_OTHER, "12:53"),
    (SENDER_USER, "好，回头我找你"),
    (SENDER_OTHER, RECALL),
    (SENDER_OTHER, "那你说说看，什么样的人合适？"),
    (SENDER_OTHER, "14:22"),
    (SENDER_USER, "哈哈，套路得人心"),
]
MINE_LAST = (SENDER_USER, "那先这样，回头聊")      # 我自己刚发的一句（末尾归属该判 self）
SCREEN_SELF = SCREEN_BASE + [MINE_LAST]
SCREEN_LIVE = SCREEN_SELF + [
    (SENDER_OTHER, "14:22"),                          # 跨天/间隔时间戳：不是谁说的话
    (SENDER_OTHER, RECALL),                           # 撤回提示：也不是
    (SENDER_OTHER, "以下为新消息"),
    (SENDER_OTHER, NEWEST),                           # 真话：材料只该剩这一句
]


def main() -> int:
    ensure_app()

    # ---------- ① 抓屏那一刀：独立聊天窗的窄缝也算"进聊天区了" ----------

    section("① 独立聊天窗：底色只留 30 物理像素也得认出来（真机那一刀切错就在这儿）")

    real = _real_window()
    left = detect_chat_left(real, DPR)
    check("第二条腿认出了贴着图标栏的聊天区左缘（真机量到的是 90）",
          left == REAL_LEFT, "None" if left is None else str(left))
    check("认出来的左缘在图标栏右缘之外（那条栏永远切掉）",
          left is None or left >= REAL_SIDEBAR,
          "None" if left is None else f"{left} vs 图标栏右缘 {REAL_SIDEBAR}")
    check("它比按版面硬切（图标栏 + 会话列表 = 465）**靠左得多**",
          left is not None and left < HARD_CUT,
          "None" if left is None else f"{left} vs {HARD_CUT}")
    check("退回硬切那条路还在（窗口太小 / 量不出来时照旧切 465）",
          chat_client_rect(real.rect(), DPR).left() == HARD_CUT,
          str(chat_client_rect(real.rect(), DPR)))

    box = chat_area_box(real, DPR)
    check("★ 裁出来的那块从聊天区左缘起（老口径从 465 起，把对方气泡开头的字砍掉）",
          box is not None and box[0] == REAL_LEFT, str(box))
    check("★ 对方气泡的左缘落在裁出来的帧里（它开头的字不再被削）",
          box is not None and box[0] <= REAL_BUBBLE,
          f"裁到 {box[0] if box else None}，气泡从 {REAL_BUBBLE} 起")
    check("上/下两刀照旧（标题栏 56 逻辑像素、底部切掉输入区）",
          box is not None and box[1] == int(56 * DPR) and box[3] > 0, str(box))
    check("右边照旧留 8 逻辑像素（滚动条那一条）",
          box is not None and box[0] + box[2] == REAL_W - int(8 * DPR), str(box))

    # 主窗不回归：第二条腿在主窗上**无从响起来**
    main_win = _list_window()
    bg = _dominant_rgb(main_win, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    my0 = int(main_win.height() * BG_BAND[0])
    my1 = int(main_win.height() * BG_BAND[1])
    near = max(4, int(BG_RUN_MIN_PX * DPR))          # 第二条腿的落脚宽度
    zone = range(int(SIDEBAR_PX * DPR), int(SIDEBAR_PX * DPR) + near)
    check(f"主窗：第二条腿落脚那 {near} 个物理像素**全在会话列表里**，一列都不是空列",
          bg is not None and not any(
              _column_is_background(main_win, x, bg, my0, my1) for x in zone),
          f"bg={bg}")
    mleft = detect_chat_left(main_win, DPR)
    check("主窗：左边界仍是硬切那条线（会话列表整块切干净，一个名字都不留给 OCR）",
          mleft in (None, HARD_CUT), "None" if mleft is None else str(mleft))
    mbox = chat_area_box(main_win, DPR)
    check("主窗照样按版面切：会话列表整块被切干净",
          mbox is not None and mbox[0] == HARD_CUT, str(mbox))

    noise = QImage(400, 400, QImage.Format_RGB32)
    painter = QPainter(noise)
    for i in range(0, 400, 8):
        painter.fillRect(i, 0, 4, 400, QColor(200, 60, 60))
        painter.fillRect(0, i, 400, 4, QColor(30, 30, 130))
    painter.end()
    check("画面花了 → 两条腿都不硬给边界（老实回 None，退回按版面切）",
          detect_chat_left(noise, DPR) is None,
          "给了 " + str(detect_chat_left(noise, DPR)))

    # ---------- ② 材料那一层：装潢挑出去、只取"我上一次开口以来" ----------

    section("② 材料取哪一段：时间戳/撤回提示不进去，已经回过的旧话也不进去")

    for line in NOISE + (RECALL, "以下为新消息", "对方撤回了一条消息", "昨天", "星期二"):
        check(f"装潢行「{line}」判得出来", _is_chat_chrome(line) is True, "")
    check("光秃秃一个日期词也算装潢（有的版本只印这个）",
          _is_chat_chrome("2026年10月7日") is True and _is_chat_chrome("10月7日") is True, "")
    check("时段 + 时间也认（上午/下午/凌晨）",
          all(_is_chat_chrome(s) for s in ("下午2:22", "上午 9:05", "凌晨12:00:30")), "")
    for line in ("我下午3:00到", NEWEST, "要泡妹，就要懂套路。", "在吗", "？", "😁"):
        check(f"人话「{line}」不算装潢", _is_chat_chrome(line) is False, "")

    read = ChatRead(lines=list(SCREEN_LIVE))
    tail = _tail_context(read)
    check("★ 材料就是对方**刚回的那一句**（真机那一屏要的就是这个）",
          tail == NEWEST, repr(tail))
    check("★ 时间戳一条都没进去", not any(n in tail for n in NOISE), repr(tail))
    check("★ 撤回提示没进去", RECALL not in tail, repr(tail))
    check("★ 已经回过一遍的旧话没进去（老口径正是把它带上了）",
          ANSWERED not in tail, repr(tail))
    check("自己发的那句也没进去（这条老口径本来就没破）",
          "套路得人心" not in tail and "回头我找你" not in tail, repr(tail))

    # 时间戳 / 撤回提示夹在"我上一次开口"与真话之间（真机那一屏的形状）→ 照样只剩真话
    sandwiched = ChatRead(lines=[
        (SENDER_USER, "好，回头我找你"),
        (SENDER_OTHER, "14:22"),
        (SENDER_OTHER, RECALL),
        (SENDER_OTHER, "以下为新消息"),
        (SENDER_OTHER, NEWEST),
    ])
    check("★ 夹在中间的时间戳 / 撤回提示 / 系统提示全被挑出去",
          _tail_context(sandwiched) == NEWEST, repr(_tail_context(sandwiched)))

    # 对方连着发两条（中间没有我说话）→ **只取最底下那一条**（v2-social-ui-batch 批次8-A 改口径）
    #
    # 老口径是"我上一次开口之后的话一条不落"，两条都进材料。真机 2026-10-07 19:04-19:05
    # 坐实了它的代价：那屏对方连回了十一二行，材料成了十几行记录，模型每次挑哪一句全看
    # 运气；同一段时间四趟 POST 的材料各不相同，四条方案对象也就跟着错开。
    # 现在跟提示词那边"**对方最近发来的那条**"（`SOCIAL_DRAFT_PROMPT` 1a：一条消息可能
    # 占好几行）对齐——材料就是视觉最底部那一条对方气泡，碰到的第一条就停。
    two = ChatRead(lines=[
        (SENDER_OTHER, "先问一句：忙完了吗？"),
        (SENDER_USER, "刚忙完"),
        (SENDER_OTHER, "那正好，周末一起爬山吧"),
        (SENDER_OTHER, "我票都看好了"),
    ])
    two_tail = _tail_context(two)
    check("★ 对方连回两条 → 材料只取最底下那一条（批次8-A 收窄后）",
          two_tail == "我票都看好了", repr(two_tail))
    check("上面那条同样是「回过的话」，不进材料（收窄之后连它一起挡在外面）",
          "爬山" not in two_tail, repr(two_tail))
    check("我上一次开口之前那句不进材料", "忙完了吗" not in two_tail, repr(two_tail))

    # 一条消息在屏幕上折成两行 → 两行是同一条，都得留着（v2-social-multiline 那条口径没破）
    folded_first = "我想说的其实不是这个"
    folded_second = "我是说下个月那件事要不要提前定下来"
    folded = ChatRead(
        lines=[(SENDER_USER, "在的"), (SENDER_OTHER, folded_first),
               (SENDER_OTHER, folded_second)],
        groups=[ChatMessage(sender=SENDER_USER, lines=["在的"]),
                ChatMessage(sender=SENDER_OTHER,
                            lines=[folded_first, folded_second])],
    )
    folded_tail = _tail_context(folded)
    check("折行的两行都留着（按条取尾那条口径没破）",
          folded_tail == f"{folded_first}\n{folded_second}", repr(folded_tail))

    # 最新那条长过上限 → 整条都在（"绝不切句子"）
    long_newest = "很长的最后一条" * 80
    long_read = ChatRead(lines=[(SENDER_USER, "嗯"), (SENDER_OTHER, long_newest)])
    long_tail = _tail_context(long_read)
    check("★ 最新那条一长，它的开头不许被削（「绝不切句子」那条老口径保住了）",
          long_tail == long_newest, str(len(long_tail)))

    # 一屏里最后开口的是自己：自动那条路不会来要材料（末尾归属判 self），手动那条路还会来
    # ——给"对方最近那一条"，别清成空的
    mine_last = ChatRead(lines=[(SENDER_OTHER, "在吗"), (SENDER_USER, "在的，刚到家")])
    check("最后一句是自己发的：材料退回「对方最近那一条」（不清成空）",
          _tail_context(mine_last) == "在吗", repr(_tail_context(mine_last)))

    # 降级那条路一个字没动：认不出说话人时仍旧按行取尾
    check("认不出说话人 → 仍旧整屏按行取尾（降级路没动）",
          _tail_context(ChatRead(lines=[(SENDER_UNKNOWN, "A\nB")]))
          == smod._tail_lines("A\nB"), "")
    check("一屏全是装潢 → 老实回空串（不是 None，也不编一句）",
          _tail_context(ChatRead(lines=[(SENDER_OTHER, "12:53"),
                                       (SENDER_OTHER, RECALL)])) == "", "")

    # ---------- ③ 端到端：这份 read 走完自动监听，喂进提示词的就是那一句 ----------

    section("③ 端到端：自动监听拿到的材料 = 对方刚回的那一句（时间戳/撤回/旧话都不在）")

    box_env, stub, app = make_env()
    service = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True

    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)

    hframe = {"v": "F0"}
    hpage = {"lines": []}
    service._grab_chat = lambda: hframe["v"]
    service._read_chat = lambda array: ChatRead(lines=list(hpage["lines"]))
    service._idle_seconds = lambda: 10.0
    service._clock = lambda: 1000.0
    # 冒话那道冷却不在这一节（③ 端到端）的射程里——这儿验的是**自动监听**那条线，先按住：
    # 不按住的话，第一次"非单聊 → 单聊"的跃迁会冒一句邀请，邀请一挂上 `_awaiting`，
    # `_poll_listen` 后面几拍就全被它挡回去（挡轮询是既有的设计，见它门口那行）。
    # v2-social-due-sentinel 之前这一格是**靠"开机 30 分钟内一律静默"那道 bug 蒙对的**
    # ——刻度 1000 秒 < 冷却 1800 秒，`_due()` 一律判"冷却没到"。哨兵一修，这层蒙的
    # 掩护就没了，得显式按住（跟 test_social_auto 第 473 行那条老规矩一个写法）。
    service._last_asked = 1000.0
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False
    # v2-social-helping-window（C-1 收窄）：自动那条路只在**进了帮聊窗口之后**才出候选
    # （点过 [好呀] / 喊过"帮我回"）。本节验的是"进去之后喂给模型的是哪一段"，所以先把
    # 这一格填上（真机落戳那一下在 `test_social_continuous.py` ① 里验）。本模块没接会话
    # 标识那只手，`_cooldown_key()` 一直是空串，填一次就一直开着。
    service._mark_helping()
    stub.candidates_result = {"candidates": [
        {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄"},
    ]}
    MARK = "用户要回一条消息"          # SOCIAL_DRAFT_PROMPT 的开头，用来认哪一段是起草提示词

    def _poll(new_frame: str, lines: list, *, expect: bool) -> None:
        service._running = False
        service.reset_done()
        service._last_listen_at = 0.0
        stub.prompts.clear()
        spoken.clear()
        hframe["v"] = new_frame
        hpage["lines"] = list(lines)
        service._poll_listen()
        if expect:
            wait_for(lambda: bool(stub.prompts))
        wait_for(lambda: not service._listen_reading and not service._running)
        drain_threads()
        pump(0.1)

    # 第一拍：先认个底（尾巴还是空的，只打底）
    _poll("F1", SCREEN_BASE, expect=False)
    check("打底那一拍不拟（这一屏先认个底）", not stub.prompts, f"{len(stub.prompts)} 段提示词")

    # 第二拍：我自己刚发了一句 → 末尾归属判 self，不拟、也不吭声（静默不插嘴）
    _poll("F2", SCREEN_SELF, expect=False)
    check("末尾是自己发的 → 不拟草稿", not stub.prompts, f"{len(stub.prompts)} 段提示词")
    check("末尾是自己发的 → 静默不插嘴（不弹提示、也不报错）",
          not spoken, "、".join(spoken) or "（什么都没说）")

    _poll("F3", SCREEN_LIVE, expect=True)
    prompt = next((p for p in stub.prompts if MARK in p), "")
    check("对方回话 → 提示词出来了", bool(prompt), f"{len(stub.prompts)} 段提示词")
    check("★ 提示词里是对方刚回的那一句", NEWEST in prompt, prompt[:60].replace("\n", " / "))
    check("★ 时间戳一条都没进提示词", not any(n in prompt for n in NOISE),
          "、".join(n for n in NOISE if n in prompt) or "（干净）")
    check("★ 撤回提示没进提示词", RECALL not in prompt,
          "（混进来了）" if RECALL in prompt else "（干净）")
    check("★ 已经回过的旧话没进提示词",
          ANSWERED not in prompt, "（混进来了）" if ANSWERED in prompt else "（干净）")
    check("喂下去的材料正好是这一句（去重闸比的就是它）",
          service._last_draft_source == NEWEST, repr(service._last_draft_source))

    # ---------- ④ 红线：零新依赖 / 两库零写入 / 去重与冷却一分没动 ----------

    section("④ 红线：这只手只认字面、不碰色卡 / 去重 / 冷却 / 两个库")

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    import ast
    import inspect

    src = inspect.getsource(smod)
    refs: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
        elif isinstance(node, ast.Import):
            refs.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.update(a.name for a in node.names)
    check("零新依赖：只多了标准库 re，没有第三方",
          "re" in refs and not ({"requests", "numpy", "PIL", "psutil", "pyperclip"} & refs),
          "、".join(sorted({"requests", "numpy", "PIL", "psutil", "pyperclip"} & refs))
          or "（一处都没有）")
    chrome_src = inspect.getsource(smod._is_chat_chrome)
    check("装潢判据只认字面：一个字都不碰气泡色（不靠颜色猜）",
          "bubble" not in chrome_src and "rgb" not in chrome_src, "")
    check("长闸那条常量一分没动（第二条腿是**加**上去的，不是把闸放松）",
          BG_RUN_PX == 40, str(BG_RUN_PX))
    check("两道冷却一分没动", LISTEN_COOLDOWN_S == 8.0 and SOCIAL_DONE_COOLDOWN_S == 20.0,
          f"{LISTEN_COOLDOWN_S} / {SOCIAL_DONE_COOLDOWN_S}")
    check("末尾归属那条判据没动（还是「最后一行分得出说话人的」那条）",
          ChatRead(lines=[(SENDER_OTHER, "真话"), (SENDER_OTHER, "14:22")]).tail_sender
          == SENDER_OTHER, "")
    check("末尾跟着时间戳/撤回提示也照样认得出说话人（Db 那条改造**不必做**：该判的没塌）",
          ChatRead(lines=[(SENDER_OTHER, "真话"), (SENDER_OTHER, RECALL)]).tail_sender
          == SENDER_OTHER, "")

    check("pet.db utterance 一行没多",
          _rows(app.config.db_path, "utterance") == pet_before,
          f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
    check("mind.db 碎片一条没多", app.mind_store.fragment_count() == mind_before, "")

    drain_threads()
    box_env.cleanup()
    return summary("社交材料取哪一段（v2-social-tail-context-fix）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
