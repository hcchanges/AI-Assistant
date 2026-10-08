"""
输入框草稿别当对方话 + 非社交前台不起草（v2-social-input-draft-exclude）的自动测试。

真机（2026-10-07 15:46 DEBUG 日志）坐实的两条病灶：

1. **草稿进了材料**：用户在微信底部输入框敲了一半、没发出去的话，被 OCR 进 `_tail_context`
   （TAIL 里那半句"我这还没发力呢"就是输入框里的草稿）——草稿每变一次 `source` 就变一次，
   去重闸放行，于是"反复起草"成了连打的主因。根因是 `bottom_cut_px` 那个常量是按"输入区
   **固定高**"拍的（84 物理像素），而输入区真机上是**随草稿长高**的——草稿一多，输入框
   上沿抬上去，白底黑字的草稿就落进裁剪框，`is_other_bubble` 把它认成"对方原话"。

2. **非社交前台也起草**：15:46:13 前台切到 explorer.exe，抓下来的是任务栏（帧 2560x72），
   任务栏的搜索框被认成 `Q 搜索`，宠物照拟一段。根因是服务里那个 `_social_foreground` 是
   **采样信号**设的，前台切走之后、信号还没到之前的那一两拍它仍旧是上一拍的 True。

这一轮两道一起修：① 抓屏那一刀**当场量**输入区上沿（`detect_input_top` / `input_cut_px`）；
② 抓屏与认字回来两处都加"当下前台是社交软件才继续"的硬闸。

**v2-social-material-empty-fix（15-X）把①的次序倒过来了**：老口径是 `max(版面那条, 实测)`
——"绝不会比 144 少"，于是真机上（744×1342、`detect_input_top` 给 1327 / None）算出来永远
是 144，切点 1198，把紧贴输入框上方那条对方末句一起切走。现在**实测优先**（但要"不薄于按比例
那条"才采信，防 1327 那种错锚），比例那条兜底，底下再压 `SAFE_FLOOR`。第①②节的期望值跟着
换成新口径，**别把这里的期望值当成"老口径也对"**。

红线：零新依赖（只多标准库）；pet.db / mind.db 零写入；不模拟按键 / 不碰剪贴板。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    pump,
    section,
    skip,
    summary,
    wait_for,
)

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import QColor, QFont, QFontDatabase, QImage, QPainter  # noqa: E402

from life.screen import monitor as mon  # noqa: E402
from life.screen.social import SocialReplyService, _tail_context  # noqa: E402
from life.screen.social_window import (  # noqa: E402
    INPUT_BOX_PX,
    INPUT_TOP_FRAC,
    SAFE_FLOOR,
    SENDER_OTHER,
    SENDER_USER,
    SENDER_UNKNOWN,
    ChatRead,
    bottom_cut_px,
    chat_area_box,
    classify_lines,
    detect_input_top,
    input_cut_px,
)
from life.screen.social_window import _detect_input_top_by_dark  # noqa: E402

DPR = 1.5
CHAT_BG = QColor(255, 255, 255)     # 聊天区底
INPUT_BG = QColor(250, 250, 250)    # 真机实测的输入区底色（跟聊天区底只差 5）
PEER_BG = QColor(250, 250, 250)     # 对方气泡（白底黑字，跟输入区同色）

FONT_CANDIDATES = (
    "C:/Windows/Fonts/NotoSerifSC-VF.ttf",
    "C:/Windows/Fonts/msyh.ttc",
)

PEER_LINE = "今天玩得开心吗"
DRAFT_LINE = "我想说的是这个"


def _install_real_font() -> str:
    for path in FONT_CANDIDATES:
        if not Path(path).exists():
            continue
        fid = QFontDatabase.addApplicationFont(path)
        fams = QFontDatabase.applicationFontFamilies(fid)
        if fams:
            return fams[0]
    return ""


def _frame(width: int, height: int, input_top: int) -> QImage:
    """一扇简化微信窗：聊天区（白）+ 对方气泡（白底黑字）+ 输入区（250）+ 草稿。

    颜色与几何只取量尺要的那几个：聊天区底 255、输入区底 250（差 5）、对方气泡 250。
    `input_top` 就是"输入区上沿"，传得比版面那条更靠上就模拟"草稿把输入框顶高了"。
    """
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(CHAT_BG)
    p = QPainter(img)
    p.setFont(QFont("Sans", 20))
    # 对方气泡（左半，白底黑字）
    p.fillRect(QRect(60, 200, 360, 80), PEER_BG)
    p.setPen(QColor(20, 20, 20))
    p.drawText(QRect(78, 214, 340, 60), 0, PEER_LINE)
    # 输入区（从 input_top 到底，250 底）
    p.fillRect(QRect(0, input_top, width, height - input_top), INPUT_BG)
    p.setPen(QColor(30, 30, 30))
    p.drawText(QRect(90, input_top + 20, 600, 60), 0, DRAFT_LINE)
    p.end()
    return img


def _noise_frame(width: int = 760, height: int = 1352) -> QImage:
    """一帧花花绿绿的画面：底那一带没有哪个色占得到一半，量尺该老实回 None。"""
    palette = (QColor(200, 60, 60), QColor(60, 200, 60), QColor(60, 60, 200),
               QColor(220, 220, 40), QColor(40, 220, 220), QColor(220, 40, 220),
               QColor(30, 30, 30), QColor(240, 240, 240))
    img = QImage(width, height, QImage.Format_RGB32)
    p = QPainter(img)
    seed = 12345
    for y in range(0, height, 4):
        for x in range(0, width, 4):
            seed = (seed * 1103515245 + 12345) & 0x7FFFFFFF
            p.fillRect(QRect(x, y, 4, 4), palette[(seed >> 16) % len(palette)])
    p.end()
    return img


def _frame_fillblind(width: int, height: int, input_top: int) -> QImage:
    """fill 那条腿失明的现场：输入区与聊天区**同色**，只有草稿是深色。

    真机换肤 / 深色主题 / 底部那条带子被白气泡占了时，`fill ≈ bg`，第一条腿回 None；
    这时候只能靠草稿的**深字段**兜底（v2-social-input-region-leak-fix 1c）。草稿在真机上
    **左对齐**、白底黑字，所以这里只画一窄条深色（左对齐），让一行里深色占比落在
    `(0, INPUT_DARK_MAX_FRAC)` 之间——跟"白底黑字"同一档，而不是画满整行（满行是"边框 /
    深色主题"，那条该回 None）。
    """
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(CHAT_BG)                              # 聊天区与输入区同色（255），fill 分不开
    p = QPainter(img)
    p.fillRect(QRect(30, input_top, 160, 60), QColor(30, 30, 30))   # 草稿（左对齐深字段）
    p.end()
    return img


INPUT_CHROME_PX = 52        # 合成窗里"输入框底下那条工具栏"的物理像素高


def _frame_chrome(width: int, height: int, input_top: int) -> QImage:
    """fill 那条腿量浅的现场：输入框被撑高，底下压着一条比它深的工具栏。

    真机那张图：输入框被粘进来的草稿撑高（>144px），它底下还有一条工具栏 / 发送键
    （比输入框底色深）。老口径锚"底带里最占地方的色"，那条带子里工具栏与输入框底色
    一半一半时，从底往上第一行就对不上 → 量出来只剩工具栏那几十像素 → `input_cut_px`
    被 144 钉住 → 顶上那段草稿漏进材料（v2-social-leak-fix-anchor 要治的）。

    另外两条是给"深字段"腿堵门的：输入框上沿压一条**整行深**的分隔线，草稿又紧贴着
    上沿画（顶上没有连续的整行浅色空隙），所以那条腿在这扇窗上量不出东西，只能靠 fill 腿。
    """
    img = QImage(width, height, QImage.Format_RGB32)
    img.fill(CHAT_BG)
    p = QPainter(img)
    p.setFont(QFont("Sans", 20))
    # 对方气泡（左半，白底黑字）
    p.fillRect(QRect(60, 200, 360, 80), PEER_BG)
    p.setPen(QColor(20, 20, 20))
    p.drawText(QRect(78, 214, 340, 60), 0, PEER_LINE)
    # 输入框（从 input_top 到底，250 底）
    p.fillRect(QRect(0, input_top, width, height - input_top), INPUT_BG)
    # 输入框底下那条工具栏（比输入框底深、又不到"深色"那一档）
    p.fillRect(QRect(0, height - INPUT_CHROME_PX, width, INPUT_CHROME_PX),
               QColor(232, 232, 232))
    # 草稿：紧贴输入框上沿画（顶上没有整行浅色空隙）
    p.setPen(QColor(30, 30, 30))
    p.drawText(QRect(40, input_top + 2, 600, 60), 0, DRAFT_LINE)
    # 输入框上沿那条分隔线：整行够深 → 深字段腿到这儿就判不了，回 None
    p.fillRect(QRect(0, input_top - 3, width, 3), QColor(70, 70, 70))
    p.end()
    return img


class Probe:
    """接住「候选给了什么」「宠物说了什么」。"""

    def __init__(self, app) -> None:
        self.spoken: list[str] = []
        self.candidates: list[list[dict]] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.pet_speak_proactive.connect(self.spoken.append)
        app.bus.social_candidates.connect(self.candidates.append)

    def reset(self) -> None:
        self.spoken.clear()
        self.candidates.clear()


def main() -> int:
    box, stub, app = make_env()
    try:
        family = _install_real_font()

        # ---------- ① 量尺：输入区比版面那条厚时才多切 ----------

        section("① 量尺：切点按实测的输入区上沿，实测不可信才退回按比例那条")

        check("版面那条（`chat_client_rect` 还在用）照旧是 max(96*dpr, 高*8%)",
              bottom_cut_px(1352, DPR) == int(INPUT_BOX_PX * DPR) == 144,
              str(bottom_cut_px(1352, DPR)))

        for w, h in ((760, 1352), (1140, 1350)):
            # 输入区上沿在 83.8%：真机 2026-10-08 读数（1342 高的帧上 y=1124，距底 218）。
            # 这个字面量是**照抄测量值**，不是从 `INPUT_TOP_FRAC` 读的——读常量画被测对象
            # 就是自证（`_input_top` 那处注释说的同一件事）。
            short_top = int(h * 0.838)
            img_s = _frame(w, h, short_top)
            top_s = detect_input_top(img_s, DPR)
            check(f"{w}x{h} 输入区上沿在 83.8%（真机读数）→ 量到上沿、实切跟到上沿",
                  top_s is not None and abs(top_s - short_top) <= 3
                  and input_cut_px(img_s, DPR) == h - top_s
                  and input_cut_px(img_s, DPR) != bottom_cut_px(h, DPR),
                  f"量到 {top_s} / 真 {short_top} / 实切 {input_cut_px(img_s, DPR)}"
                  f" / 版面那条 {bottom_cut_px(h, DPR)}")
            box_s = chat_area_box(img_s, DPR)
            check(f"{w}x{h} 短输入区：chat_area_box 底边 = 输入区上沿（紧贴的那条不再被切）",
                  box_s is not None and box_s[1] + box_s[3] == h - input_cut_px(img_s, DPR),
                  "None" if box_s is None else str(box_s))

            # 草稿把输入框顶高（271px / 379px > 版面那条 219）→ 实切跟到真上沿
            for frac in (0.80, 0.72):
                tall_top = int(h * frac)
                img_t = _frame(w, h, tall_top)
                top_t = detect_input_top(img_t, DPR)
                cut_t = input_cut_px(img_t, DPR)
                check(f"{w}x{h} 输入区顶高到 {frac:.2f}（{h - tall_top}px>144）→ 量到上沿、"
                      f"实切跟到那儿（草稿区整个切掉）",
                      top_t is not None and abs(top_t - tall_top) <= 3
                      and cut_t == h - tall_top,
                      f"量到 {top_t} / 真 {tall_top} / 实切 {cut_t}")
                box_t = chat_area_box(img_t, DPR)
                check(f"{w}x{h} 高输入区：chat_area_box 底边 = 输入区上沿（草稿不进裁剪框）",
                      box_t is not None and box_t[1] + box_t[3] == tall_top,
                      "None" if box_t is None else str(box_t))

        # fill 那条腿失明时（输入区与聊天区同色），靠草稿的深字段兜底（v2-social-input-region-leak-fix 1c）
        for w, h in ((760, 1352), (1140, 1350)):
            for frac in (0.80, 0.72):
                tall_top = int(h * frac)
                img_b = _frame_fillblind(w, h, tall_top)
                top_b = detect_input_top(img_b, DPR)
                cut_b = input_cut_px(img_b, DPR)
                check(f"fill≈bg {w}x{h} 输入框顶到 {frac:.2f} → 深字段量到上沿、实切跟到那儿",
                      top_b is not None and abs(top_b - tall_top) <= 3
                      and cut_b == h - tall_top,
                      f"量到 {top_b} / 真 {tall_top} / 实切 {cut_b}")
                box_b = chat_area_box(img_b, DPR)
                check(f"fill≈bg {w}x{h} 高输入区：chat_area_box 底边 = 输入区上沿（草稿不进裁剪框）",
                      box_b is not None and box_b[1] + box_b[3] == tall_top,
                      "None" if box_b is None else str(box_b))

        # 输入框底下压着一条比它深的工具栏（老锚会锚到工具栏上，从底往上第一行就对不上 → 量浅）
        for w, h in ((760, 1352), (1140, 1350)):
            tall_top = int(h * 0.72)          # 输入框被撑到 378px / 378px，远超版面那条 144
            img_c = _frame_chrome(w, h, tall_top)
            dark_c = _detect_input_top_by_dark(img_c, DPR, w, h)
            top_c = detect_input_top(img_c, DPR)
            cut_c = input_cut_px(img_c, DPR)
            check(f"★ {w}x{h} 输入框被撑高 + 底下一条深工具栏 → 量到真上沿（不锚在工具栏上）",
                  top_c is not None and abs(top_c - tall_top) <= 3,
                  f"量到 {top_c} / 真 {tall_top}")
            check(f"★ {w}x{h} 这条上沿不是深字段腿量出来的（它瞄的是草稿那一行）",
                  dark_c != top_c, f"深字段腿 {dark_c} / 最终 {top_c}")
            check(f"★ {w}x{h} 撑高那扇窗：实切 = 整段草稿（{h - tall_top}px，不是被钉住的 144）",
                  cut_c == h - tall_top, f"实切 {cut_c} / 该切 {h - tall_top}")
            box_c = chat_area_box(img_c, DPR)
            check(f"★ {w}x{h} 撑高那扇窗：chat_area_box 底边 = 输入区上沿（草稿不进裁剪框）",
                  box_c is not None and box_c[1] + box_c[3] == tall_top,
                  "None" if box_c is None else str(box_c))

        # ---------- ② 退化：量不出来就退回按比例那条，绝不多切 ----------

        section("② 退化：量不出来退回按比例那条（不是版面那条），绝不多切")

        # "量不出来"的兜底不再是 `bottom_cut_px`（144），而是按真机比例算的
        # `高 × (1 − INPUT_TOP_FRAC)`（1352 上是 219 = 83.8% 那条）——那才是输入区上沿该在的地方。
        frac_1352 = int(1352 * (1 - INPUT_TOP_FRAC))

        noise = _noise_frame()
        check("画面花了（底那一带没哪个色占一半）→ detect_input_top 回 None",
              detect_input_top(noise, DPR) is None,
              "给了 " + str(detect_input_top(noise, DPR)))
        check("画面花了 → input_cut_px 退回按比例那条（219，不是版面那条 144）",
              input_cut_px(noise, DPR) == frac_1352 == 219,
              str(input_cut_px(noise, DPR)))

        # 输入区与聊天区同色（都是纯白）→ 量尺分不开，退回按比例那条
        flat = QImage(760, 1352, QImage.Format_RGB32)
        flat.fill(QColor(255, 255, 255))
        check("输入区与聊天区同色 → detect_input_top 回 None（分不开就不硬量）",
              detect_input_top(flat, DPR) is None,
              "给了 " + str(detect_input_top(flat, DPR)))
        check("同色 → input_cut_px 退回按比例那条",
              input_cut_px(flat, DPR) == frac_1352,
              str(input_cut_px(flat, DPR)))

        # 实测比比例线**薄得多** = 锚错了（真机 1327 那种：只算出 15）：丢掉，按比例切。
        # 这一帧的构造：聊天区底 250，只有贴底 40 物理像素是更浅的 255 —— 底带里"最浅且
        # 不是底色"的那个色只有那一小条，量出来 40，正是"锚到贴底的工具栏 / 边框"的形状。
        mis = QImage(760, 1352, QImage.Format_RGB32)
        mis.fill(QColor(250, 250, 250))
        pm = QPainter(mis)
        pm.fillRect(QRect(0, 1352 - 40, 760, 40), QColor(255, 255, 255))
        pm.end()
        mis_top = detect_input_top(mis, DPR)
        check("★ 实测只有 40（比比例线薄得多 = 锚错了）→ 丢掉实测，切回按比例那条",
              mis_top is not None and 1352 - mis_top == 40
              and input_cut_px(mis, DPR) == frac_1352,
              f"detect={mis_top} 实测={None if mis_top is None else 1352 - mis_top} "
              f"实切={input_cut_px(mis, DPR)}")

        check("空画面不炸", detect_input_top(QImage(), DPR) is None, "")
        check("形状对不上也不炸", detect_input_top(None, DPR) is None, "")
        check("尺寸都取不到时退回版面那条老口径（别拿坏尺寸乘出负数）",
              input_cut_px(QImage(), DPR) == bottom_cut_px(0, DPR), "")
        check("小到排不出底那一条也不炸（退回绝对下限 `SAFE_FLOOR`）",
              input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR) == SAFE_FLOOR,
              str(input_cut_px(QImage(10, 10, QImage.Format_RGB32), DPR)))

        # ---------- ③ 端到端：草稿不进 _tail_context（需要真字体） ----------

        section("③ 端到端：输入框有草稿 + 聊天区有对方话 → _tail_context 只含对方话")

        if not family:
            skip("端到端那帧（草稿不进材料）", "没装上能画中文的真字体")
        else:
            for w, h in ((760, 1352), (1140, 1350)):
                img = _frame(w, h, int(h * 0.80))       # 输入框被顶高，草稿落进原裁剪框
                area = chat_area_box(img, DPR)
                check(f"{w}x{h} 先量得出要裁哪一块", area is not None, str(area))
                if area is None:
                    continue
                crop = img.copy(QRect(*area))
                arr = mon._to_numpy(crop)
                read = classify_lines(arr, mon.OcrReader().read_lines(arr))
                tail = _tail_context(read)
                check(f"{w}x{h} ★ 草稿不在 _tail_context 里（连打主因）",
                      DRAFT_LINE not in tail,
                      "（混进来了）" if DRAFT_LINE in tail else "（干净）")
                check(f"{w}x{h} ★ 对方那句还在 _tail_context 里（没有误伤）",
                      PEER_LINE in tail, repr(tail[:60]))
                check(f"{w}x{h} 草稿也不在整屏 context 里",
                      DRAFT_LINE not in read.context,
                      "（混进来了）" if DRAFT_LINE in read.context else "（干净）")

            # 撑高 + 底下一条深工具栏那扇窗（批次12b 的真机现场）：草稿一个字都不许进材料
            for w, h in ((760, 1352), (1140, 1350)):
                img = _frame_chrome(w, h, int(h * 0.72))
                area = chat_area_box(img, DPR)
                check(f"撑高窗 {w}x{h} 先量得出要裁哪一块", area is not None, str(area))
                if area is None:
                    continue
                crop = img.copy(QRect(*area))
                arr = mon._to_numpy(crop)
                read = classify_lines(arr, mon.OcrReader().read_lines(arr))
                tail = _tail_context(read)
                check(f"★ 撑高窗 {w}x{h} 草稿不在 _tail_context 里（漏进材料的根因）",
                      DRAFT_LINE not in tail,
                      "（混进来了）" if DRAFT_LINE in tail else "（干净）")
                check(f"★ 撑高窗 {w}x{h} 对方那句还在 _tail_context 里（没有误伤）",
                      PEER_LINE in tail, repr(tail[:60]))
                check(f"★ 撑高窗 {w}x{h} 草稿也不在整屏 context 里",
                      DRAFT_LINE not in read.context,
                      "（混进来了）" if DRAFT_LINE in read.context else "（干净）")

        # ---------- ④ 服务层闸：认字回来时前台已经不是社交软件 → 当没看过 ----------

        section("④ 服务层闸：认字回来时前台已经不是社交软件 → 不起草、指纹作废")

        service: SocialReplyService = app.social_reply
        probe = Probe(app)
        app.config.screen.social_enabled = True
        app.config.screen.social_auto_listen = True
        service._social_foreground = False
        service._last_fingerprint = "这一屏已经认过的指纹"
        service._last_listen_tail = "茗风：周末有空一起爬山吗"   # 上一拍已经推进过

        probe.reset()
        service._on_listen_read(ChatRead(lines=[(SENDER_OTHER, "茗风：那就周六见吧")]))
        drain_threads()
        pump(0.05)
        check("★ 前台不是社交软件 → 一条候选都不拟", not probe.candidates,
              f"{len(probe.candidates)} 组")
        check("★ 也不吭声（不冒「我琢磨一下」）", not probe.spoken,
              " | ".join(probe.spoken) or "（安静）")
        check("★ 这一屏当没看过：指纹作废（下一拍在社交窗里会重新认）",
              service._last_fingerprint is None, repr(service._last_fingerprint))
        check("★ 尾巴没被推进（对方那句没被记成'处理过了'）",
              service._last_listen_tail == "茗风：周末有空一起爬山吗",
              repr(service._last_listen_tail))
        check("在途标志也清掉了（认字那笔账结掉）", service._listen_reading is False,
              str(service._listen_reading))

        # 反向对照：前台是社交软件 → 照旧走判定那一步（这条别被误挡）
        service._social_foreground = True
        service._last_fingerprint = None
        service._listen_reading = False
        probe.reset()
        service._on_listen_read(ChatRead(lines=[(SENDER_OTHER, "茗风：那就周六见吧")]))
        drain_threads()
        pump(0.1)
        check("对照：前台是社交软件 → 判定那一步照走（不因这道闸哑火）",
              service._last_listen_tail != "茗风：周末有空一起爬山吗"
              or service._last_listen_kind != "", "")

        # ---------- ⑤ 装配层闸：抓屏那一刻前台不是社交软件 → 一个像素都不采 ----------

        section("⑤ 装配层闸：抓屏那一刻前台不是社交软件 → 一个像素都不采")

        from life.screen import shot as shot_mod
        from monitor import foreground as fg_mod

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
            fg_mod.foreground_title = lambda: ""
            fg_mod.foreground_pid = lambda: 9999
            shot_mod.grab_foreground_window = _fake_grab

            app.screen_monitor._pids.name_of = lambda pid: "explorer.exe"
            grabbed.clear()
            check("前台是 explorer.exe → 不采（这一拍整个打回，连抓都不抓）",
                  app._grab_social_chat() is None and not grabbed,
                  f"抓了 {len(grabbed)} 次")

            app.screen_monitor._pids.name_of = lambda pid: "notepad.exe"
            grabbed.clear()
            check("前台是记事本（工作类）→ 也不采",
                  app._grab_social_chat() is None and not grabbed,
                  f"抓了 {len(grabbed)} 次")

            app.screen_monitor._pids.name_of = lambda pid: "wechat.exe"
            grabbed.clear()
            image = app._grab_social_chat()
            check("前台是微信 → 照采（这道闸只挡非社交，不误挡真社交）",
                  bool(grabbed), f"抓了 {len(grabbed)} 次")
            check("采回来的是裁过的消息区，不是整扇窗",
                  image is not None and image.shape[1] < 1000 and image.shape[0] < 800,
                  "None" if image is None else f"{image.shape[1]}x{image.shape[0]}")
        finally:
            fg_mod.foreground_title, fg_mod.foreground_pid = real_title, real_pid
            shot_mod.grab_foreground_window = real_grab
            app.screen_monitor._pids.name_of = real_name

        # ---------- ⑥ 红线：零新依赖 / 两库零写入 ----------

        section("⑥ 红线：零新依赖 / 两库零写入")

        import sqlite3

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        import ast
        import inspect

        from life.screen import social_window as swmod

        src = inspect.getsource(swmod)
        refs: set[str] = set()
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                refs.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(a.name for a in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2"}
        check("量尺只用标准库 / 既有的像素取样那只手，没有新 import 的第三方库",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")
        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("输入框草稿别当对方话 + 非社交前台不起草（v2-social-input-draft-exclude）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
