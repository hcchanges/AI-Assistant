"""社交抓取隔离 + 发言者区分 + 弹窗单例（v2-social-ocr-isolate）的自动测试。

真机反馈的三条病，这一轮各治一条：

- **① 抓取源不干净**：抓的是"前台窗口那一整块"，宠物自己的说话气泡、「AI助理」
  助手窗、候选框都悬在聊天窗上面，盖住的那几行字被 OCR 当成"对方说的话"读走；
  窗口里的会话列表 / 工具栏 / 输入框也全是杂字。→ 认进程号剔掉宠物自己的窗、按版面
  只留中间那块消息列表。
- **② 分不出谁说的**：老口径只有"字变没变 + 最近有没有键鼠动作"两条线索，用户敲了
  一半没发也判成"对方回了"。→ 读气泡底色：**绿 = 我发的，浅色 = 对方**。
- **③ 自己发的混进"对方原话"**：解析输入只有对方的才算干净。→ `ChatRead.context`
  在色卡认出说话人时**只给对方的**；末尾写的是绿气泡就如实说"你自己发的"。
- **④ 弹窗摞弹窗**：`exec()` 跑的是嵌套事件循环，1.5 秒那个轮询照转，对方再回一条
  就再 `exec()` 一次 → 两个候选框重叠。**这一轮的单例口径已经被推翻**
  （v2-social-copy-clean：框本身整档删掉，候选改走宠物自己的非模态气泡），
  ④ 改成验"全程一个模态框都不建、一次 `exec()` 都不跑"。

红线：零新依赖（这一轮只用 dataclasses 与既有 PySide6）、pet.db / mind.db 零写入、
不模拟按键、`social.py` 自己不 import 截屏 / OCR 库。剪贴板这条（v2-social-copy-clean
起）是**只写不读**，而（v2-social-chat-ui-copy 批次11 起）亮草稿那一刻**也不写**了
——写不写由用户按「复制回复」决定，全仓写它的手仍然只有 `_copy_social_draft` 一只。
测试跑在离屏平台上，那份剪贴板是进程内的，动不到用户真机上的剪贴板内容。
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    ensure_app,
    make_env,
    note,
    pump,
    section,
    skip,
    summary,
    wait_for,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication, QDialog  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen import shot as shot_mod  # noqa: E402
from ui.theme import BUBBLE_BG  # noqa: E402 —— 浮窗真底色，正是它落在「对方」那档判据里
from life.screen import social as smod  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    CHAT_LIST_PX,
    CHAT_MIN_SIDE,
    INPUT_BOX_PX,
    SENDER_OTHER,
    SENDER_UNKNOWN,
    SENDER_USER,
    SIDEBAR_PX,
    TITLE_BAR_PX,
    ChatRead,
    as_chat_read,
    box_bounds,
    bottom_cut_px,
    bubble_rgb,
    chat_area_box,
    chat_client_rect,
    classify_lines,
    input_cut_px,
    is_other_bubble,
    is_pet_window,
    is_self_bubble,
    parse_ocr_lines,
    sender_from_bubble,
)

ROOT = Path(__file__).resolve().parents[1]

# 真字体候选（跟 test_work_card_font 同一份）：offscreen 下 `QFontDatabase.families()`
# 是空的，不装真字体画出来的"中文"是一串豆腐块，OCR 认不出、这一节会假绿。
FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/NotoSerifSC-Regular.otf",
    "C:/Windows/Fonts/msyh.ttc",
)

SELF_RGB = (149, 236, 105)      # 微信自己那半边（跟 social_window.SELF_BUBBLE_RGB 同值）
OTHER_BG = QColor(255, 255, 255)
CHAT_BG = QColor(245, 245, 245)
DPR = 1.5                       # 真机那台（逻辑 1707×1067 ↔ 抓屏 2560×1600）


def _install_real_font() -> str:
    """装一个能画中文的真字体，返回族名；一个都没装上返回空串。"""
    ensure_app()
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        font_id = QFontDatabase.addApplicationFont(path)
        families = QFontDatabase.applicationFontFamilies(font_id)
        if families:
            return families[0]
    return ""


def _chat_shot(family: str, bubbles: list[tuple[QRect, QColor, str]]) -> QImage:
    """画一张假的聊天窗：每个气泡一块底色 + 一行字。顺序照传进来的来。"""
    image = QImage(760, 520, QImage.Format_RGB32)
    image.fill(CHAT_BG)
    painter = QPainter(image)
    painter.setFont(QFont(family or "Sans", 14))
    painter.setPen(QColor(20, 20, 20))
    for rect, color, text in bubbles:
        painter.fillRect(rect, color)
        painter.drawText(rect.adjusted(16, 0, -16, 0), 0x0001 | 0x0080, text)
    painter.end()
    return image


class _DialogWatch:
    """现抓"这一路上到底建没建过模态框、有没有人真去 `exec()`"。

    v2-social-copy-clean 之后 `_on_social_candidates` 那条路上不该再有 QDialog：
    候选走宠物自己的非模态气泡。这个上下文管理器临时把 `QDialog.__init__` / `QDialog.exec`
    换成记录员——离屏环境里真 `exec()` 会永远卡住，所以既要断言"没建"，也要能说出"建了谁"。
    """

    def __init__(self) -> None:
        self.built: list[str] = []
        self.executed: list[str] = []

    def __enter__(self) -> "_DialogWatch":
        self._real_init = QDialog.__init__
        self._real_exec = QDialog.exec

        def _watch_init(dialog, *args, **kwargs):
            self.built.append(type(dialog).__name__)
            return self._real_init(dialog, *args, **kwargs)

        def _watch_exec(dialog):
            self.executed.append(type(dialog).__name__)
            return self._real_exec(dialog)

        QDialog.__init__ = _watch_init            # type: ignore[method-assign]
        QDialog.exec = _watch_exec                # type: ignore[method-assign]
        return self

    def __exit__(self, *exc: object) -> None:
        QDialog.__init__ = self._real_init        # type: ignore[method-assign]
        QDialog.exec = self._real_exec            # type: ignore[method-assign]


def _bubble_text(app) -> str:
    """宠物那个非模态气泡里现在写着什么（合起来一段，供断言）。"""
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def main() -> int:
    box, stub, app = make_env()
    try:
        service = app.social_reply
        app.config.screen.social_enabled = True
        app.config.screen.ocr_enabled = True
        app.config.screen.social_auto_listen = False
        # 自动监听那条线本来会去真抓屏；这一档单独验，先让它停着。
        service._social_foreground = False
        service._sync_listen_timer()

        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        candidates: list[list[dict]] = []
        app.bus.social_candidates.connect(candidates.append)

        # ---------- ① 抓取源隔离 ----------

        section("① 抓取源隔离：宠物自己的窗一律不采，只留聊天消息列表")

        bootstrap_src = (ROOT / "core/bootstrap.py").read_text(encoding="utf-8")
        draft_src = (ROOT / "ui/social_draft.py").read_text(encoding="utf-8")
        check("装配层还接着 `social_candidates` 这条信号（候选那条线没断）",
              "self.bus.social_candidates.connect(self._on_social_candidates)"
              in bootstrap_src, "")
        check("★ 候选框那个类整档没了：装配层与 ui/social_draft 里都不再提它",
              "SocialCandidatesDialog" not in bootstrap_src
              and "SocialCandidatesDialog" not in draft_src
              and "draft_box_height" not in draft_src, "")

        section("①-1 按进程号认宠物自己的窗（气泡没标题，只有进程号认得住）")

        check("进程号是自己 → 是宠物窗（哪怕标题空着）",
              is_pet_window(title="", pid=1234, pet_pid=1234, pet_name="天渌") is True, "")
        check("标题就是宠物名 → 是宠物窗",
              is_pet_window(title="天渌", pid=99, pet_pid=1234, pet_name="天渌") is True, "")
        check("标题是「AI助理」→ 是宠物窗（助手窗那条，v2-pet-persona-rename 起固定这么叫）",
              is_pet_window(title="AI助理", pid=99, pet_pid=1234, pet_name="天渌") is True, "")
        check("标题是「跟天渌聊聊」→ 还是是宠物窗（换过角色卡的那扇窗）",
              is_pet_window(title="跟天渌聊聊", pid=99, pet_pid=1234, pet_name="天渌") is True, "")
        check("微信主窗 → 不是宠物窗",
              is_pet_window(title="微信", pid=99, pet_pid=1234, pet_name="天渌") is False, "")
        check("标题是「跟老王聊聊」→ 跟宠物名对不上，不算宠物窗",
              is_pet_window(title="跟老王聊聊", pid=99, pet_pid=1234, pet_name="天渌") is False, "")
        check("拿不到进程号时也不认（不因为标题空就误判）",
              is_pet_window(title="", pid=None, pet_pid=1234, pet_name="天渌") is False, "")

        section("①-2 只留聊天消息列表：会话列表 / 标题栏 / 输入框都切掉")

        # v2-social-ocr-catch 起，这条路按**真机量的固定像素**切（图标栏 + 会话列表固定
        # 宽，不随窗口宽度走），不再是按比例——比例在宽窗上会切进聊天区削掉对方气泡。
        window = QRect(100, 100, 1000, 800)
        client = chat_client_rect(window)
        check("左边切掉图标栏 + 会话列表（固定像素，不按比例）",
              client.left() == 100 + SIDEBAR_PX + CHAT_LIST_PX,
              f"{client.left()} vs {100 + SIDEBAR_PX + CHAT_LIST_PX}")
        check("上边切掉标题栏（固定像素）",
              client.top() == 100 + TITLE_BAR_PX, f"{client.top()}")
        check("下边切掉输入框（固定像素，不再一下切掉四分之一）",
              client.bottom() == 100 + 800 - INPUT_BOX_PX - 1, f"{client.bottom()}")
        check("裁完的那块真在窗口里面",
              window.contains(client), f"{client} ⊄ {window}")
        check("高分屏按倍数放大：dpr=1.5 时切的是 1.5 倍像素",
              chat_client_rect(QRect(0, 0, 1500, 1200), dpr=1.5).left()
              == int((SIDEBAR_PX + CHAT_LIST_PX) * 1.5),
              f"{chat_client_rect(QRect(0, 0, 1500, 1200), dpr=1.5).left()}")
        check("小窗切了就没了 → 原样交回（不切成一条缝）",
              chat_client_rect(QRect(0, 0, CHAT_MIN_SIDE, CHAT_MIN_SIDE)) ==
              QRect(0, 0, CHAT_MIN_SIDE, CHAT_MIN_SIDE), "")
        check("空矩形不炸", chat_client_rect(QRect()) == QRect(), "")

        section("①-3 装配层这一拍：前台是宠物自己就一个像素都不采")

        from monitor import foreground as fg_mod

        # 掩码是现问"哪些窗可见"算出来的，所以宠物窗得真显示出来（跟 test_social 一样）
        app.pet_window.show()
        app.chat_bubble.show()
        pump(0.05)

        real_title, real_pid = fg_mod.foreground_title, fg_mod.foreground_pid
        real_grab = shot_mod.grab_foreground_window
        real_name = app.screen_monitor._pids.name_of
        grabbed: list = []

        def _fake_grab(pet_rect=None, window_rect=None, extra_masks=None):
            grabbed.append(extra_masks)
            image = QImage(1000, 800, QImage.Format_RGB32)
            image.fill(CHAT_BG)
            return image

        try:
            fg_mod.foreground_title = lambda: "AI助理"
            fg_mod.foreground_pid = lambda: 4242
            import os as _os
            fg_mod.foreground_pid = lambda: _os.getpid()
            shot_mod.grab_foreground_window = _fake_grab
            # 进程名这一格现在也过白名单（v2-social-grab-whitelist）：pid 4242 在本机读不出
            # 名字 → `_proc_name_of` 回空串 → "认不出是谁就不采"。这一节要验的正是
            # "前台是微信 → 照采"，所以把名字照实桩成微信（`pids.name_of` 是唯一来源）。
            app.screen_monitor._pids.name_of = lambda pid: "weixin.exe"
            grabbed.clear()
            check("前台是宠物自己（进程号）→ 不采",
                  app._grab_social_chat() is None and not grabbed, f"抓了 {len(grabbed)} 次")

            fg_mod.foreground_pid = lambda: 4242
            fg_mod.foreground_title = lambda: "AI助理"
            grabbed.clear()
            check("前台是「AI助理」助手窗 → 不采",
                  app._grab_social_chat() is None and not grabbed, f"抓了 {len(grabbed)} 次")

            fg_mod.foreground_title = lambda: "微信"
            fg_mod.foreground_pid = lambda: 4242
            grabbed.clear()
            image = app._grab_social_chat()
            check("前台是社交软件 → 采，且把宠物那几块当掩码递下去",
                  bool(grabbed) and grabbed[0] is not None and len(grabbed[0]) >= 1,
                  "None" if not grabbed else f"{len(grabbed[0] or [])} 块掩码")
            check("采回来的是裁过的消息列表，不是整扇窗（宽高都变小了）",
                  image is not None and image.shape[1] < 1000 and image.shape[0] < 800,
                  "None" if image is None else f"{image.shape[1]}x{image.shape[0]}")
            check("宠物本体 + 助手窗都进了掩码（盖在聊天窗上那几块都要涂掉）",
                  len(grabbed[0]) >= 2, f"{len(grabbed[0])} 块掩码")
        finally:
            fg_mod.foreground_title, fg_mod.foreground_pid = real_title, real_pid
            shot_mod.grab_foreground_window = real_grab
            app.screen_monitor._pids.name_of = real_name
            app.chat_bubble.hide()
            app.pet_window.hide()

        section("①-4 掩码是真涂白的：宠物那句话认不出来了，对方那句还在")

        family = _install_real_font()
        if not family:
            skip("宠物窗口涂白后 OCR 认不到它那句", "没装上能画中文的真字体")
        else:
            pet_box = QRect(420, 140, 300, 60)
            image = _chat_shot(family, [
                (QRect(20, 40, 300, 60), OTHER_BG, "今天玩得开心吗"),
                (pet_box, QColor(*SELF_RGB), "我帮你想想怎么回"),
                (QRect(20, 240, 300, 60), OTHER_BG, "周末一起爬山吧"),
            ])
            before = mon.OcrReader().read_array(mon._to_numpy(image))
            # 用装配层那只手同一套涂白（`shot._whiten` 就是 grab_foreground_window 里
            # 涂宠物那几块时调的那个），不是测试自造的替身。
            shot_mod._whiten(image, pet_box)      # noqa: SLF001 —— 要验的就是这只手
            after = mon.OcrReader().read_array(mon._to_numpy(image))
            check("涂白前：宠物那句话在里面（前置条件成立）",
                  "我帮你想想" in before, before[:60] or "（一个都没认到）")
            check("涂白后：宠物那句话没了", "我帮你想想" not in after, after[:60])
            check("涂白后：对方那两句照旧在",
                  "今天玩得开心吗" in after and "周末一起爬山吧" in after, after[:60])

        # ---------- ①-5 掩码的单位（v2-social-mask-dpr） ----------
        #
        # 上面 ①-4 那一节**结构上验不出这条病**：它把 mask 和那张图当同一个坐标系用，
        # 于是"涂白有效"永远绿。真机上两者**不是一个坐标系**——帧是物理像素，而掩码来自
        # `frameGeometry()`（Qt 逻辑像素）。DPR 1.5 时掩码只有帧的 2/3、还钉在左上，
        # 浮窗右下那大半（正文所在）根本没涂掉，字被 OCR 读走、底色又是 `#fffdf7`
        # （`ui/theme.BUBBLE_BG`，落在"对方"那档判据里）→ 变成"对方原话"。

        section("①-5 掩码换单位：逻辑像素的浮窗矩形，涂在物理像素的帧上")

        real_dpr = app._screen_dpr
        pet_color = QColor(BUBBLE_BG)         # 浮窗真底色，不是纯白——涂没涂到一眼可分
        try:
            app._screen_dpr = lambda: DPR     # type: ignore[method-assign]
            got = app._to_physical(QRect(100, 200, 300, 400))
            check("逻辑矩形 × 1.5 → 物理矩形（左上 floor、右下 ceil）",
                  got == QRect(150, 300, 450, 600), str(got))
            check("旧口径就是原样交出去，所以这一条以前是真偏的",
                  app._to_physical(QRect(420, 300, 300, 60)) != QRect(420, 300, 300, 60), "")
            app._screen_dpr = lambda: 1.0
            check("DPR=1 时原样交回（低分屏上行为与本条之前一模一样）",
                  app._to_physical(QRect(420, 300, 300, 60)) == QRect(420, 300, 300, 60), "")
            app._screen_dpr = lambda: DPR
            check("空矩形 / 没矩形照样交回，不乘出个坏矩形",
                  app._to_physical(QRect()) == QRect() and app._to_physical(None) is None, "")

            app.pet_window.show()
            app.chat_bubble.show()
            pump(0.05)
            masks = app._social_pet_masks()
            logical = app.chat_bubble.frameGeometry()
            widened = [m for m in masks if abs(m.width() - logical.width() * DPR) <= 1]
            check("★ 装配层交下来的掩码已经是物理像素（助手窗那块按倍数放大了）",
                  logical.width() > 2 and bool(widened),
                  f"助手窗逻辑宽 {logical.width()} → 掩码宽 {[m.width() for m in masks]}")
            app._screen_dpr = lambda: 1.0
            plain = app._social_pet_masks()
            check("DPR=1 那一档不放大（不是把掩码无脑乘一遍）",
                  any(abs(m.width() - logical.width()) <= 1 for m in plain),
                  f"掩码宽 {[m.width() for m in plain]}")
            app._screen_dpr = lambda: DPR
        finally:
            app._screen_dpr = real_dpr
            app.chat_bubble.hide()
            app.pet_window.hide()

        section("①-5b 同一张物理帧：旧口径漏涂 + 误伤，新口径盖得住")

        app._screen_dpr = lambda: DPR     # type: ignore[method-assign] —— 真机那台的分辨率

        # 一张**物理像素**的帧（760×520 逻辑 × 1.5）。三个气泡都按逻辑坐标 × DPR 画上去：
        #   ① 白气泡（对方）在左上   ② 绿气泡在中间   ③ 浮窗（宠物自己）在右下
        # 版面的位置是特意摆的：拿逻辑矩形当物理用的"旧口径"掩码，正好**落在**②那块绿
        # 气泡上，而**够不着**③——真机上这一偏就是这么偏的（偏的方向由浮窗在屏上的位置
        # 决定，不一定每次都落在别人身上）。
        PET_LOGICAL = QRect(420, 300, 300, 60)          # ③ 浮窗（宠物自己）
        frame_logical = [
            (QRect(20, 40, 300, 60), OTHER_BG, "今天玩得开心吗"),
            (QRect(280, 200, 300, 60), QColor(*SELF_RGB), "周末一起爬山吧"),
            (PET_LOGICAL, pet_color, "我帮你想想怎么回"),
        ]

        def _dpr_shot(family: str) -> QImage:
            """画一张物理像素的聊天窗：逻辑矩形 × DPR 落在帧上。"""
            image = QImage(int(760 * DPR), int(520 * DPR), QImage.Format_RGB32)
            image.fill(CHAT_BG)
            painter = QPainter(image)
            painter.setFont(QFont(family or "Sans", int(14 * DPR)))
            painter.setPen(QColor(20, 20, 20))
            for rect, color, text in frame_logical:
                where = QRect(int(rect.left() * DPR), int(rect.top() * DPR),
                              int(rect.width() * DPR), int(rect.height() * DPR))
                painter.fillRect(where, color)
                painter.drawText(where.adjusted(16, 0, -16, 0), 0x0001 | 0x0080, text)
            painter.end()
            return image

        def _phys(rect: QRect) -> QRect:
            return QRect(int(rect.left() * DPR), int(rect.top() * DPR),
                         int(rect.width() * DPR), int(rect.height() * DPR))

        def _center(img: QImage, rect: QRect) -> QColor:
            return img.pixelColor(rect.center())

        pet_phys = _phys(PET_LOGICAL)
        neighbor_phys = _phys(QRect(280, 200, 300, 60))

        old_way = _dpr_shot("")
        shot_mod._whiten(old_way, PET_LOGICAL)          # noqa: SLF001 —— 老口径：逻辑矩形原样用
        check("★ 旧口径：浮窗那块**一个像素都没涂到**（还是浮窗底色，文字照旧交给 OCR）",
              _center(old_way, pet_phys) == pet_color, str(_center(old_way, pet_phys).name()))
        check("★ 旧口径：那一偏反而把旁边**别人**那条气泡涂白了（真消息被抹掉）",
              _center(old_way, neighbor_phys) == QColor(255, 255, 255),
              str(_center(old_way, neighbor_phys).name()))

        new_way = _dpr_shot("")
        shot_mod._whiten(new_way, app._to_physical(PET_LOGICAL))   # noqa: SLF001
        check("★ 新口径：浮窗那块真涂白了（漏不出去了）",
              _center(new_way, pet_phys) == QColor(255, 255, 255),
              str(_center(new_way, pet_phys).name()))
        check("新口径：旁边那条别人的气泡一个像素没动",
              _center(new_way, neighbor_phys) == QColor(*SELF_RGB),
              str(_center(new_way, neighbor_phys).name()))

        if not family:
            skip("掩码换成物理像素后，浮窗那句才真被涂掉（OCR 级）", "没装上能画中文的真字体")
        else:
            before_img = _dpr_shot(family)
            old_img = _dpr_shot(family)
            shot_mod._whiten(old_img, PET_LOGICAL)                  # noqa: SLF001
            new_img = _dpr_shot(family)
            shot_mod._whiten(new_img, app._to_physical(PET_LOGICAL))  # noqa: SLF001
            reader = mon.OcrReader()
            before = reader.read_array(mon._to_numpy(before_img))
            old_after = reader.read_array(mon._to_numpy(old_img))
            new_after = reader.read_array(mon._to_numpy(new_img))
            check("涂白前：浮窗那句在里面（前置条件成立）",
                  "我帮你想想" in before, before[:60] or "（一个都没认到）")
            check("★ 旧口径下它**还在**——这就是「涂不干净」那条病",
                  "我帮你想想" in old_after, old_after[:60])
            check("★ 新口径下它没了", "我帮你想想" not in new_after, new_after[:60])
            check("新口径下别人那条照旧认得到",
                  "今天玩得开心吗" in new_after, new_after[:60])

        app._screen_dpr = real_dpr

        # ---------- ①-5c 掩码兜底名单（v2-social-leak-mask） ----------
        #
        # 掩码原来是**点名制**：宠物本体 + `window._bubble` + `chat_bubble`，就这三块。
        # 名单外任何一个本进程的窗都漏——`GuideCardDialog`（建对象攻略卡那个框）正是漏
        # 掉的那一个：它弹着的时候用户照样能点回微信，前台一变成微信、抓屏就放行，框里
        # 那几行字（"对象攻略卡"、抽出来的对方信息）浅底深字、判出来正好是"对方"，就
        # 这么进了"对方原话"（真机图3）。这一节验的就是换成兜底名单之后**一个不漏**。
        #
        # 这里不再做一遍 OCR 级的"涂白有效"——那一手 ①-4 / ①-5b 已经逐像素验过了
        # （`shot._whiten` 是同一只）。"每个可见顶层窗都进了名单" + "涂白有效"合起来
        # 就是"漏不出去了"，不必再拿一张合成帧自证一遍。

        section("①-5c 掩码兜底名单：本进程可见的顶层窗一个都不许漏")

        from PySide6.QtWidgets import QWidget

        from ui.social_draft import GuideCardDialog

        app.pet_window.show()
        app.chat_bubble.show()
        dialog = GuideCardDialog("喜欢爬山、爱吃火锅", app.pet_window)
        dialog.setGeometry(QRect(300, 200, 520, 300))
        dialog.show()
        pump(0.05)
        try:
            app._screen_dpr = lambda: 1.0     # 这一节只问"在不在名单里"，单位换成恒等好对坐标
            dlg_mask = app._to_physical(dialog.frameGeometry())
            named: list = []
            pet = app._social_pet_rect()
            if pet is not None:
                named.append(pet)
            for widget in (getattr(app.pet_window, "_bubble", None), app.chat_bubble):
                if widget is not None and widget.isVisible():
                    named.append(app._to_physical(widget.frameGeometry()))
            masks = app._social_pet_masks()
            check("★ 前置：老口径点名那几块里**没有**它（漏的就是这一块）",
                  bool(named) and all(m != dlg_mask for m in named),
                  f"{len(named)} 块点名掩码")
            check("★ 建卡那个对话框现在在掩码名单里（框里那几行字漏不出去了）",
                  any(m == dlg_mask for m in masks), f"{len(masks)} 块掩码")

            missing = [
                type(w).__name__ for w in QApplication.topLevelWidgets()
                if w.isVisible() and not w.frameGeometry().isEmpty()
                and not any(m == app._to_physical(w.frameGeometry()) for m in masks)
            ]
            check("★ 本进程所有可见顶层窗都在名单里（不再是点名制，别的框自动跟着收）",
                  not missing, f"漏了 {missing}")

            other = QWidget()
            other.setGeometry(QRect(40, 40, 200, 120))
            other.show()
            pump(0.05)
            try:
                fresh = app._social_pet_masks()
                check("随便再开一个顶层窗，它也自动进名单（不靠一个个点名）",
                      any(m == app._to_physical(other.frameGeometry()) for m in fresh), "")
            finally:
                other.hide()
                other.deleteLater()

            dialog.hide()
            pump(0.05)
            masks_after = app._social_pet_masks()
            check("收起来之后就不涂了（现问、只算可见的）",
                  all(m != dlg_mask for m in masks_after), f"{len(masks_after)} 块掩码")
        finally:
            app._screen_dpr = real_dpr
            dialog.deleteLater()
            app.chat_bubble.hide()
            app.pet_window.hide()

        # ---------- ①-6 小窗兜底那一刀（v2-social-leak-mask） ----------
        #
        # `chat_area_box` 量不出、`chat_client_rect` 又原样交回（窗太小，切完剩不下东西）
        # 时，原来只剩"切掉底部输入区"一刀——**标题栏**（独立聊天窗上写的就是联系人 /
        # 群名）与**会话列表**（群名 + 消息预览）整块留在画面里，浅底深字判出来正是
        # "对方"（真机图2 的"妙妙屋 / 群名"）。这一节把两档读数摆出来：老口径是**整宽**，
        # 新口径两头都收了口。纯几何那条腿（`cut_edges_box` 自己）在
        # `test_social_ocr_catch.py` ④-2 逐条摆开。

        section("①-6 窗小到切不动时，兜底那一刀也收掉标题栏与会话列表")

        real_grab = shot_mod.grab_foreground_window
        real_title, real_pid = fg_mod.foreground_title, fg_mod.foreground_pid
        real_name = app.screen_monitor._pids.name_of
        real_dpr = app._screen_dpr
        try:
            app._screen_dpr = lambda: DPR
            # 一扇**物理像素**的小窗：宽够（不走"太窄"那条），高不够——切掉标题栏与输入区
            # 之后剩不到 `CHAT_MIN_SIDE`，于是"量不出聊天区"，按版面硬切也原样交回。
            #
            # 高从 `200*DPR`(300) 收到 `150*DPR`(225)：15-X 把底部那一刀从版面那条 144
            # 换成"按比例那条 + `SAFE_FLOOR`"之后，同样高的窗上切得少了，300 高这一档
            # 反而能量出聊天区（剩下 176 像素 ≥ `CHAT_MIN_SIDE`），这一节要验的"切不动"
            # 那条路就走不到了。225 高这一档在新口径下切掉 40 仍剩不到 `CHAT_MIN_SIDE`
            # （225−40−84 = 101 < 120），前置与结论都还成立。**这不是把断言放宽**：
            # 收窗高是为了让同一件事（窗小到量不出聊天区）在新口径下重新成立。
            WIN_PHYS_W, WIN_PHYS_H = int(560 * DPR), int(150 * DPR)
            flat = QImage(WIN_PHYS_W, WIN_PHYS_H, QImage.Format_RGB32)
            flat.fill(CHAT_BG)
            shot_mod.grab_foreground_window = (
                lambda pet_rect=None, window_rect=None, extra_masks=None: flat.copy()
            )
            fg_mod.foreground_title = lambda: "微信"      # 不是宠物自己
            fg_mod.foreground_pid = lambda: 4242
            app.screen_monitor._pids.name_of = lambda pid: "weixin.exe"   # 过白名单（同上）

            frame = app._grab_social_chat()
            # "只切底部"那条老路的高度：底部那一刀在 15-X 之后走 `input_cut_px`
            # （这帧是纯色、量不出上沿 → 退回按比例那条，再被 `SAFE_FLOOR` 抬起）
            old_keep = WIN_PHYS_H - input_cut_px(flat, DPR)
            check("前置：这一档帧够矮 —— 按版面硬切也会原样交回",
                  chat_client_rect(QRect(0, 0, WIN_PHYS_W, WIN_PHYS_H), DPR)
                  == QRect(0, 0, WIN_PHYS_W, WIN_PHYS_H), "")
            check("前置：像素法在这个尺寸上也量不出聊天区",
                  chat_area_box(flat, DPR) is None, str(chat_area_box(flat, DPR)))
            check("★ 会话列表那一刀生效了（宽度不再等于整帧宽）",
                  frame is not None and frame.shape[1] < WIN_PHYS_W,
                  "None" if frame is None else f"{frame.shape[1]} / 整帧 {WIN_PHYS_W}")
            check("★ 标题栏那一刀也生效了（比自己只切底部矮出一个标题栏）",
                  frame is not None
                  and frame.shape[0] <= old_keep - int(TITLE_BAR_PX * DPR),
                  "None" if frame is None else f"{frame.shape[0]} / 只切底部 {old_keep}")
        finally:
            app._screen_dpr = real_dpr
            shot_mod.grab_foreground_window = real_grab
            fg_mod.foreground_title, fg_mod.foreground_pid = real_title, real_pid
            app.screen_monitor._pids.name_of = real_name

        # ---------- ② 发言者区分 ----------

        section("② 发言者区分：绿 = 我发的，浅色 = 对方")

        check("微信绿 → user", sender_from_bubble(SELF_RGB) == SENDER_USER, "")
        check("白 → other", sender_from_bubble((255, 255, 255)) == SENDER_OTHER, "")
        check("浅灰（会话列表底色）→ other（近白那档）",
              sender_from_bubble((245, 245, 245)) == SENDER_OTHER, "")
        check("深灰（工具栏 / 深色主题）→ 认不出，不硬编",
              sender_from_bubble((60, 60, 60)) == SENDER_UNKNOWN, "")
        check("蓝色（别的客户端）→ 认不出", sender_from_bubble((80, 140, 230)) == SENDER_UNKNOWN, "")
        check("取不到颜色 → 认不出", sender_from_bubble(None) == SENDER_UNKNOWN, "")
        check("绿/白两条判据互斥",
              is_self_bubble((255, 255, 255)) is False and is_other_bubble(SELF_RGB) is False, "")

        section("②-1 字框形状两种都认（旧版四点框 / 新版 (x0,y0,x1,y1)）")

        check("四点框 → 外接矩形",
              box_bounds([[38.0, 45.0], [169.0, 45.0], [169.0, 66.0], [38.0, 66.0]])
              == (38, 45, 169, 66), str(box_bounds([[38.0, 45.0], [169.0, 45.0],
                                                    [169.0, 66.0], [38.0, 66.0]])))
        check("(x0,y0,x1,y1) 也认", box_bounds((10, 20, 30, 40)) == (10, 20, 30, 40),
              str(box_bounds((10, 20, 30, 40))))
        check("认不出的形状给 None", box_bounds("不明白") is None, "")
        old_style_box = [[0, 0], [1, 0], [1, 1], [0, 1]]
        check("两代 RapidOCR 返回形状都认（旧版 (行表, 耗时) 元组）",
              parse_ocr_lines(([[old_style_box, "甲", 0.9]], 0.1))
              == [(old_style_box, "甲")], str(parse_ocr_lines(([[old_style_box, "甲", 0.9]], 0.1))))

        class _NewStyle:
            txts = ["甲", "乙"]
            boxes = [[[0, 0], [1, 0], [1, 1], [0, 1]], [[2, 2], [3, 2], [3, 3], [2, 3]]]

        check("新版 .txts/.boxes 也认", len(parse_ocr_lines(_NewStyle())) == 2,
              str(parse_ocr_lines(_NewStyle())))
        check("认不出来给空表（不炸）", parse_ocr_lines(None) == [], "")

        section("②-2 取气泡底色：字框外扩取中位数")

        pixels = QImage(220, 80, QImage.Format_RGB32)
        pixels.fill(QColor(*SELF_RGB))
        painter = QPainter(pixels)
        painter.setPen(QColor(20, 20, 20))
        painter.fillRect(QRect(60, 20, 60, 40), QColor(20, 20, 20))   # 假装是笔画
        painter.end()
        got = bubble_rgb(pixels, (60, 20, 120, 60), pad=4)
        check("一堆深色笔画里取到的是绿色底（中位数不是平均）",
              got is not None and is_self_bubble(got), str(got))
        check("纯笔画里取不出底 → None",
              bubble_rgb(pixels, (62, 22, 118, 58), pad=0) is None,
              str(bubble_rgb(pixels, (62, 22, 118, 58), pad=0)))

        section("②-3 真跑一遍：真 RapidOCR 认字 + 真像素取色")

        if not family:
            skip("真 OCR 认窗 → 按气泡色分谁说的", "没装上能画中文的真字体")
        else:
            image = _chat_shot(family, [
                (QRect(20, 40, 300, 60), OTHER_BG, "今天玩得开心吗"),
                (QRect(420, 140, 300, 60), QColor(*SELF_RGB), "挺开心的你呢"),
                (QRect(20, 240, 300, 60), OTHER_BG, "周末一起爬山吧"),
            ])
            array = mon._to_numpy(image)
            lines = mon.OcrReader().read_lines(array)
            check("真 OCR 认出了三行（连框一起）", len(lines) == 3, f"{len(lines)} 行")
            read = classify_lines(array, lines)
            senders = [sender for sender, _text in read.lines]
            check("白气泡那两行 → other，绿气泡那行 → user",
                  senders == [SENDER_OTHER, SENDER_USER, SENDER_OTHER], str(read.lines))
            # v2-social-multiline：这里原来是 `== "今天玩得开心吗 周末一起爬山吧"`（空格拼）。
            # 空格拼把"行"这件事抹掉了，模型看不出对方到底说了一句还是两三句——真机上就
            # 出过"只答末半句"。改成 `\n` 拼，行数这份信息一直留到提示词里。
            check("对方那几句拼起来就是 incoming（一行一句，保换行）",
                  read.incoming == "今天玩得开心吗\n周末一起爬山吧", repr(read.incoming))
            check("自己那句归 outgoing，不进 incoming",
                  read.outgoing == "挺开心的你呢" and "挺开心的你呢" not in read.incoming,
                  read.outgoing)
            check("末尾那条分得出说话人（这次是对方）", read.tail_sender == SENDER_OTHER,
                  read.tail_sender)

        # ---------- ③ 上下文拼接与末尾归属 ----------

        section("③ 解析输入只含对方那句：自己发的混不进去")

        check("色卡认出说话人时，context 只给对方的",
              ChatRead(lines=[(SENDER_OTHER, "对方的问候"), (SENDER_USER, "我自己的话")]).context
              == "对方的问候", "")
        check("一行都没分出来时退回整段 raw（降级，不是「这是对方说的」）",
              ChatRead(lines=[(SENDER_UNKNOWN, "整屏杂字")]).context == "整屏杂字", "")
        check("老口径回字符串的桩照样收（整段当没分类的原文）",
              as_chat_read("茗风：今天玩得开心吗").context == "茗风：今天玩得开心吗", "")
        check("空串 → 空 ChatRead", as_chat_read("").lines == [] and as_chat_read("").context == "", "")

        section("③-1 末尾归属：色卡那条腿压过空闲时长那条腿")

        check("末尾是绿气泡 → self，**哪怕刚空闲了 10 秒**（老口径会误判成对方回了）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_OTHER, "甲"), (SENDER_USER, "乙")]), "甲", 10.0
              ) == "self", "")
        check("末尾是白气泡 → incoming，**哪怕刚敲了键盘**（空闲 0 秒）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_OTHER, "甲"), (SENDER_USER, "乙"),
                                  (SENDER_OTHER, "丙")]), "甲 乙", 0.0
              ) == "incoming", "")
        check("色卡认不出 + 刚敲过 → self（退回老口径）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_UNKNOWN, "甲 乙")]), "甲", 0.5
              ) == "self", "")
        check("色卡认不出 + 闲置久了 → incoming（退回老口径）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_UNKNOWN, "甲 乙")]), "甲", 10.0
              ) == "incoming", "")
        check("字没变 → none（光标 / 滚动那种假变化）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_OTHER, "甲")]), "甲", 10.0
              ) == "none", "")
        check("第一次认到这一屏 → none（只是打底）",
              service._classify_incoming(
                  ChatRead(lines=[(SENDER_OTHER, "甲")]), "", 10.0
              ) == "none", "")

        section("③-2 端到端：自动监听拿到的上下文里没有自己那句话")

        app.config.screen.social_auto_listen = True
        service._social_foreground = True
        # 这一节验的是**自动监听那条线**（认到新消息 → 起草），页型那格先摆成「单聊」：
        # v2-social-trigger-window-gate 起，页型「非单聊 → 单聊」的那一下跃迁会顺带
        # 冒一句邀请，而邀请一挂上 `_awaiting`，`_poll_listen` 后面几拍就全被它挡回去
        # （`_awaiting` 挡轮询是既有的设计，见 `_poll_listen` 门口那行）。真机上那一冒
        # 发生在切进微信那一拍（`on_foreground`），这儿不重放它，只把状态摆平。
        service._page_kind = "personal"
        service._sync_listen_timer()
        service._examples_provider = lambda query: ""
        # 画面 / 字都放可变盒子里：换帧换字就是"来了新消息"，跟 test_social_auto 同款
        frame = {"v": "FRAME_A"}
        lines = {"v": []}
        service._grab_chat = lambda: frame["v"]
        service._read_chat = lambda array: ChatRead(lines=list(lines["v"]))
        service._idle_seconds = lambda: 10.0
        service._last_fingerprint = None
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_listen_at = 0.0
        service._running = False
        stub.candidates_result = {"candidates": [
            {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄"},
        ]}

        # 先打一屏底：第一次认到这一屏只当基线，不算"来了新消息"
        lines["v"] = [
            (SENDER_OTHER, "茗风：今天玩得开心吗"),
            (SENDER_USER, "我：还行吧"),
        ]
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.05)
        check("第一拍只打底（还没来新消息就不拟）", not candidates, f"{len(candidates)} 组")

        # 对方回了新的一条：画面变了、末尾是白气泡 → 直接拟
        #
        # v2-social-helping-window（C-1 收窄）：自动那条路只在**进了帮聊窗口之后**才出候选
        # （点过 [好呀] / 喊过"帮我回"）。本节验的是"进去之后拿到的上下文里没有自己那句"，
        # 所以在这里落一戳（真机落戳那一下在 `test_social_continuous.py` ① 里验）。
        # **摆在打底那一拍之后**：会话标识是打底那一拍才从窗口标题里读出来的
        # （`_sync_session`），这会儿键才定下来。
        service._mark_helping()
        stub.prompts.clear()
        candidates.clear()
        frame["v"] = "FRAME_B"
        lines["v"] = [
            (SENDER_OTHER, "茗风：今天玩得开心吗"),
            (SENDER_USER, "我：还行吧"),
            (SENDER_OTHER, "茗风：那周末一起爬山吗"),
        ]
        service._poll_listen()
        wait_for(lambda: bool(candidates))
        drain_threads()
        pump(0.1)
        prompt = next((p for p in stub.prompts if "他自己的口吻" in p), "")
        check("认到对方新回 → 拟了一段候选", bool(candidates), f"{len(candidates)} 组")
        check("提示词里给的是**对方最新那条**（我上一次开口以来）",
              "茗风：那周末一起爬山吗" in prompt, prompt[:80].replace("\n", " / "))
        # v2-social-tail-context-fix：材料从"整屏对方的话"收窄成"我上一次开口以来对方说的话"。
        # 旧断言要求**上面那句也**在提示词里，那正是老口径——已经回过一遍的旧话还跟着进材料，
        # 真机上"回的是上面那句旧话"就是它喂出来的。改成如实断言"回过的那句被挡在外面"。
        check("已经回过的那句旧话不再进提示词（这一轮收窄的就是它）",
              "茗风：今天玩得开心吗" not in prompt,
              "（跟过来了）" if "茗风：今天玩得开心吗" in prompt else "（干净）")
        check("自己发的那句一个字都没进提示词", "还行吧" not in prompt,
              "（混进去了）" if "还行吧" in prompt else "（干净）")

        section("③-3 末尾是自己发的 → 不拟，也不吭声（静默）")

        spoken.clear()
        candidates.clear()
        service._running = False
        service._last_listen_at = 0.0
        service._last_listen_kind = ""
        # ③-2 给出候选那一刻，服务把"这一段的完成态"锁上了（v2-social-copy-clean）：
        # 这一段验的是**下一条消息该不该拟**，得先当成"隔了 20 秒冷却的另一次社交"。
        service.reset_done()
        frame["v"] = "FRAME_C"
        lines["v"] = [
            (SENDER_OTHER, "茗风：那周末一起爬山吗"),
            (SENDER_USER, "我：好呀，几点"),
        ]
        service._poll_listen()
        wait_for(lambda: not service._listen_reading)
        drain_threads()
        pump(0.05)
        check("一条候选都不拟", not candidates, f"{len(candidates)} 组")
        check("自己发 → 一句都不说（静默不插嘴）",
              not spoken, " | ".join(spoken[-2:]) or "（安静）")

        section("③-4 不同朋友各说各的：换一屏字就换一套上下文，不串场")

        service._running = False
        service._last_listen_at = 0.0
        service._last_listen_kind = ""
        service.reset_done()          # 同上：换一个朋友 = 另一次社交，完成态锁不带着走
        stub.prompts.clear()
        candidates.clear()
        frame["v"] = "FRAME_D"
        lines["v"] = [(SENDER_OTHER, "阿哲：球赛看了吗")]
        service._poll_listen()
        wait_for(lambda: bool(candidates))
        drain_threads()
        pump(0.1)
        friend_b = next((p for p in stub.prompts if "他自己的口吻" in p), "")
        # 判"串没串场"只认**说话人前缀**（「茗风：」），不认那几句正文：提示词模板自己的
        # 要求 1 里就白纸黑字写着"今天玩得开心吗"当例子（templates.py:494），拿正文短语
        # 去搜会把固定文案当成串场。前缀是这一屏独有的，干净。
        check("这一屏是阿哲 → 提示词里是阿哲那句",
              "阿哲：球赛看了吗" in friend_b, friend_b[:70].replace("\n", " / "))
        check("上一场茗风那两句没跟过来（上下文不串场）",
              "茗风" not in friend_b,
              "（串了）" if "茗风" in friend_b else "（干净）")

        section("③-5 单条候选那条规则在自动路径上一样管用（v2-social-single-ui）")

        normalized = service._normalize_candidates({"candidates": [
            {"draft": "第一条", "why": "甲"}, {"draft": "第二条", "why": "乙"},
            {"draft": "第三条", "why": "丙"},
        ]})
        check("自动路径交上来的多条候选砍到 1 条", len(normalized) == 1, f"{len(normalized)} 条")

        # ---------- ④ 全程无模态弹窗，候选走非模态气泡 ----------

        section("④ 全程无模态弹窗：候选走宠物自己的非模态气泡，多轮也不摞框")

        app.config.screen.social_auto_listen = False
        service._social_foreground = False
        service._sync_listen_timer()

        offer_calls: list[int] = []
        real_offer_card = service.offer_card
        service.offer_card = lambda: offer_calls.append(1)

        try:
            with _DialogWatch() as watch:
                before = app.chat_bubble._history.count()
                QApplication.clipboard().setText("亮草稿之前剪贴板里本来放着的东西")
                app.bus.social_candidates.emit([{"draft": "第一条", "why": "甲"}])

                check("★ 这一拍**没有**建任何模态框（候选不再走弹窗）",
                      watch.built == [], repr(watch.built))
                check("★ 更没有人去 `exec()` 跑嵌套事件循环（真机卡死的那个根）",
                      watch.executed == [], repr(watch.executed))
                check("★ 生成那一刻没动剪贴板（批次11 起写不写归「复制回复」那一下）",
                      QApplication.clipboard().text() == "亮草稿之前剪贴板里本来放着的东西",
                      repr(QApplication.clipboard().text()))
                text = _bubble_text(app)
                check("★ 候选 + 解析都摆在宠物那个非模态气泡里（就是原来弹窗那两段）",
                      "第一条" in text and "甲" in text, repr(text))
                check("★ 正文下面挂着「复制回复」，手上拿的就是这一版",
                      app.chat_bubble._copy_row.isVisible()
                      and app.chat_bubble._copy_text == "第一条",
                      repr(app.chat_bubble._copy_text))
                # 批次11 起一则候选是**两条**（正文进方框 + 解析居中小灰字），不再是三段挤
                # 在一条里。而且亮新草稿时会**先把上一轮那一则摘掉**（需求5：只留最新一则），
                # 所以条数不再是简单的 `+2`——判据换成"这一版在、上一轮那版不在了"。
                check("气泡真被摆出来了（不阻塞地亮出来，不是只写日志）",
                      not app.chat_bubble.isHidden() and "第一条" in text,
                      f"{before} -> {app.chat_bubble._history.count()}，"
                      f"显式隐藏={app.chat_bubble.isHidden()}")
                check("★ 上一轮那一则已经被摘掉（批次11 需求5：只留最新一则）",
                      "挺开心的，你呢？" not in text, repr(text))
                check("这一拍还接着问「要不要建对象攻略卡」（只问一次）",
                      len(offer_calls) == 1, f"{len(offer_calls)} 次")

                # 第二轮：对方又回一条 —— 链路照样完整，**框还是那一个**
                offer_calls.clear()
                before2 = app.chat_bubble._history.count()
                app.bus.social_candidates.emit([{"draft": "第二条", "why": "乙"}])
                check("★ 第二轮：还是没有模态框、没有 exec()（不摞框）",
                      watch.built == [] and watch.executed == [],
                      f"{watch.built} / {watch.executed}")
                # 第二轮：上一轮那两条被摘掉、这一轮又摆两条，所以**条数原地不动**；
                # 换的是同一个气泡里的那一则，不是又开一个窗。
                text2 = _bubble_text(app)
                check("★ 第二轮：换的是同一个气泡里的那一则，不是又开一个窗",
                      app.chat_bubble._history.count() == before2
                      and "第二条" in text2 and "第一条" not in text2
                      and not app.chat_bubble.isHidden(),
                      f"{before2} -> {app.chat_bubble._history.count()}，"
                      f"显式隐藏={app.chat_bubble.isHidden()}")
                check("★ 按钮手上那版跟着换成最新那条（要粘的是刚拟的这句）",
                      app.chat_bubble._copy_text == "第二条",
                      repr(app.chat_bubble._copy_text))
                check("★ 而且第二轮照样没偷写剪贴板",
                      QApplication.clipboard().text() == "亮草稿之前剪贴板里本来放着的东西",
                      repr(QApplication.clipboard().text()))
                check("建卡那一问第二轮也还是只问一次",
                      len(offer_calls) == 1, f"{len(offer_calls)} 次")
        finally:
            service.offer_card = real_offer_card

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：零写入 / 零新依赖 / 服务自己不碰截屏 OCR")

        import inspect

        src_window = (ROOT / "life/screen/social_window.py").read_text(encoding="utf-8")
        src_social = inspect.getsource(smod)
        check("隔离层只用标准库（没有 numpy / cv2 / PIL 这些新依赖）",
              not any(w in src_window for w in ("import numpy", "import cv2", "from PIL")),
              "")
        # 按 AST 的 import 判，不按源码里有没有这几个字样：`social.py` 的模块 docstring
        # 里写着「接 `shot` 与 `OcrReader`」那是**说明**，不是它伸的手（同 test_social.py S3）。
        banned_imports = {"shot", "OcrReader", "save_temp", "grab_foreground_window",
                          "read_screen_array", "read_screen_lines"}
        imported: set[str] = set()
        for node in ast.walk(ast.parse(src_social)):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[-1] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imported.update(alias.name for alias in node.names)
                if node.module:
                    imported.add(node.module.split(".")[-1])
        hit = sorted(banned_imports & imported)
        check("服务自己仍不 import 截屏 / OCR 的实现（按 import 判，docstring 提一嘴不算）",
              not hit, f"（真 import 了：{hit}）" if hit else "（一个都没碰）")
        check("服务那条「不自动发送」没动", "不自动发送" in src_social, "")
        check("没有模拟按键 / 读剪贴板那几条腿",
              not any(w in src_window + src_social for w in
                      ("SendInput", "keybd_event", "uiautomation", "pyautogui",
                       "pynput", "win32clipboard")), "")

        import sqlite3

        def _rows(path, table: str) -> int:
            conn = sqlite3.connect(path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        check("pet.db utterance 一行没多",
              _rows(box.dir / "pet.db", "utterance") == 0,
              str(_rows(box.dir / "pet.db", "utterance")))
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == 0, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交抓取隔离 + 发言者区分 + 全程无模态弹窗（v2-social-ocr-isolate）")


if __name__ == "__main__":
    raise SystemExit(main())
