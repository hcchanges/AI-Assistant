"""社交抓取的隔离层（v2-social-ocr-isolate）：认得出"这是谁的窗口、这行是谁说的"。

跟 `social.py` 的分工：那一位管「什么时候抓、抓来的字怎么用」，这一位只做**纯判断**——
给定一帧画面和 OCR 出来的字框，回答两个问题：

1. **这块画面该不该采**（`is_pet_window` / `chat_client_rect`）；
2. **这一行字是谁说的**（`sender_from_bubble` / `classify_lines`）。

**为什么要单开一个模块。** `social.py` 有一条钉死的红线：它自己不 import 任何截屏 / OCR
的库（那两只手由装配层注入，`tests/test_social.py` 的 S3 那一节按源码验）。这里的活儿
是"看图上的颜色"，天然要碰像素，所以挪到隔壁——`social.py` 只 import 这一位的**纯函数**，
`shot` / `OcrReader` / `grab_foreground_window` 一个都不出现在它那儿，红线一分没松。

**色卡这一手的由来。** 老口径认"末尾归属"只有两条线索：字变没变 + 最近几秒有没有键鼠
动作（`social.SEND_IDLE_S`）。真机上这两条都靠不住——用户自己敲了一半没发，字也变了、
也没空闲；对方回消息时用户正好在别处动鼠标，就判成"自己发的"。微信里"谁说的"本来就
写在气泡颜色上：**自己发的那半边是绿的，对方那半边是白的**。读一眼气泡底色，比猜空闲
时长硬得多，这才是这一轮真正补上的那条腿。

**判不出来就说判不出来。** 色卡认不出（换了个深色主题的客户端、气泡是自己画的图），
这一行就记 `SENDER_UNKNOWN`，上层退回老的空闲时长那套猜法——**不硬编一个"对方"**。
"""

from __future__ import annotations

import logging

from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# ---------- 发言人 ----------

SENDER_USER = "user"        # 自己发的（绿气泡那半边）
SENDER_OTHER = "other"      # 对方发的（浅色气泡那半边）
SENDER_UNKNOWN = ""         # 认不出来（不做假设，交回上层）

# 微信自己那半边气泡：**绿**。
#
# 老口径拿的是一个固定的色号 #95EC69 = (149,236,105)（微信 3.x 的绿），容差 26 去比。
# 真机上跑不通：微信 4.x 的绿实测是 **(157,242,159)**，蓝通道差 54，直接被判成"认不出"——
# 于是用户自己发的每一条都落到 UNKNOWN，末尾归属那条腿整个失灵。
#
# 改成判**色相**而不是判色号：绿通道明显压过红蓝两条，且够亮。"自己发的是绿的"这条
# 事实在 3.x / 4.x / 各种换肤下都成立，具体是哪个绿不重要。白、灰、蓝、黑一律不是。
SELF_BUBBLE_RGB = (149, 236, 105)     # 3.x 的绿，留作参照（测试基线）
SELF_GREEN_RGB = (157, 242, 159)      # 4.x 实测的绿
SELF_GREEN_MIN = 150                  # 绿通道下限
SELF_GREEN_MARGIN = 40                # 绿比红、蓝各高出这么多才算"自己那半边"
SELF_LUMA_MIN = 170                   # 太暗的绿（照片、深色皮肤）不算气泡

# "浅色 = 对方"这条判据：三通道都不低于 210、且彼此相差不超过 32，才算近白。
# 两条一起要，是为了把浅灰的**联系人列表底色**（约 #F5F5F5，差 0）和
# 浅冷暖色的**工具栏**分开——单看"够亮"会把整块侧栏也算成对方的气泡。
OTHER_MIN_CHANNEL = 210
OTHER_MAX_SPREAD = 32

# 取一行字的气泡底色时，往字框外扩这么多像素去找背景（字框只框住笔画，背景在框外面
# 那一圈留白里）。扩太少会全采到笔画，扩太多会翻到隔壁气泡上。
BUBBLE_PAD = 5

# ---------- 折行续行认亲（v2-social-grab-fix） ----------
#
# 微信里"一条消息写长了、被气泡宽度折成两行"和"对方连发两条"在屏幕上都长成两行字，
# **必须分开看**：前者是**同一条**（回复里那两行都得接住），后者是两条。
#
# 老口径只给每一行单独标一个说话人，没有"这一行属于哪一条"这回事。于是只要某一行标不出
# 说话人（气泡底色认不出、或者那一行压在气泡边缘上），`ChatRead.incoming` 就把它整行丢掉
# —— 真机上"折行后半段没了、回复只答前半句"就是这么丢的。
#
# 这里补一条**认亲**：某一行认不出说话人，但它跟上一行**贴得很近**、**左缘对齐**，就认作
# 上一行的续行，继承它的说话人。两个判据都要：
#   · 同一个气泡里的上下两行 —— 两条必然都成立（行距小、左内边距同一个）；
#   · 不同气泡之间 —— 至少有一条不成立（气泡之间有空档；自己那半边靠右对齐，左缘对不上）。
# 所以宁可漏认（照旧记 UNKNOWN，上层按降级办）也不乱认。
BUBBLE_JOIN_GAP_RATIO = 0.35   # 行间空档 ≤ 上一行高度的这个比例，才算同一气泡里的续行
BUBBLE_JOIN_DX_RATIO = 0.25    # 左缘偏差 ≤ 行高的这个比例，才算同一个气泡
# 框抖动的那几个像素要**先减掉再比**（v2-social-material-empty-fix 15-X 的连带）。
# 为什么非加这一条不可：判续行用的是 OCR 自己的框，而框的位置随**送进去那张图的尺寸**
# 轻微浮动——RapidOCR 会先缩放到模型认得的尺寸，图高变了，缩放比就变了，同一行可能差
# 1~2 像素。**真机/合成窗上量到的实例**（对方一条消息折成「…事情都比较」/「忙」两行）：
# 底部那一刀从老口径 144 收到 15-X 的 84（这一刀该收，裁出来的图高了 60 像素）之后，
# 两行的框从"70..101 / 111..144"变成"70..101 / 112..143"——空档 10 → 11，而阈值是
# 31×0.35 = 10.85：**11 > 10.85**，同一条消息当场被拆成两条，`latest_other` 只剩"忙"
# （正好是 v2-social-grab-fix 治过的"只答末半句"）。差值只有 0.15 像素，这就是拿浮点
# 阈值卡一个本身在抖的量。所以给空档留 2 物理像素的余量：**抖出来的那点差值不算**，
# 真气泡之间那道空档（合成窗上 76、真机上远大于此）照旧隔着老远，一条也并不过来。
BUBBLE_JOIN_JITTER_PX = 2      # 物理像素：框抖动余量，比之前先减掉

# 判"这个像素是笔画不是背景"的亮度上限：笔画是深色的，背景是浅色的。
TEXT_LUMA_MAX = 110


def _rgb_near(rgb, target, tol: int) -> bool:
    return all(abs(int(a) - int(b)) <= tol for a, b in zip(rgb, target))


def is_self_bubble(rgb, tol: int | None = None) -> bool:
    """这个底色算不算"自己发的"：**绿的那半边**（不认色号，认色相）。

    判据三条一起要：绿通道够亮（`SELF_GREEN_MIN`）、绿比红蓝都高出 `SELF_GREEN_MARGIN`、
    整体不暗（`SELF_LUMA_MIN`）。3.x 的 (149,236,105) 与 4.x 实测的 (157,242,159)
    都过；白 / 灰 / 蓝 / 黑 / 照片上的暗绿都不过。

    `tol` 留着只为兼容老调用（当年是"比色号"的容差）；现在用不上了，传了也当没传。
    """
    if rgb is None:
        return False
    r, g, b = (int(v) for v in rgb[:3])
    if g < SELF_GREEN_MIN:
        return False
    if g - max(r, b) < SELF_GREEN_MARGIN:
        return False
    return max(r, g, b) >= SELF_LUMA_MIN


def is_other_bubble(rgb) -> bool:
    """这个底色算不算"对方发的"（浅色 / 近白）。"""
    if rgb is None:
        return False
    r, g, b = (int(v) for v in rgb[:3])
    if min(r, g, b) < OTHER_MIN_CHANNEL:
        return False
    return max(r, g, b) - min(r, g, b) <= OTHER_MAX_SPREAD


def sender_from_bubble(rgb) -> str:
    """一个气泡底色 → 发言人。认不出来给 `SENDER_UNKNOWN`（空串），**不猜**。"""
    if is_self_bubble(rgb):
        return SENDER_USER
    if is_other_bubble(rgb):
        return SENDER_OTHER
    return SENDER_UNKNOWN


# ---------- 窗口隔离 ----------

# 微信主窗的版面。**这几个数是真机上量的**（2026-10-06，DPR=1.5 的机器，微信 4.x），
# 单位是**逻辑像素**，跟窗口宽高无关——图标栏与会话列表都是固定宽的，所以不能再按
# 比例切。老口径按比例切（左切 32%、下切 26%）在真机上两头出事：
#   · 759px 宽的窗上左切 242px，对方的气泡本来从 x≈174 就开始了 → **每条消息的开头
#     都被削掉**（"不因裁剪区域错位而漏掉"就是修这个）；
#   · 下切 26% 落在窗口 74% 处，而真机上输入区从 83.8% 才开始 → 白白丢掉两成消息区，
#     越是新的消息越在下头，正好把最要紧的那条扔掉。
SIDEBAR_PX = 60          # 最左那条图标栏（没文字，但也不该进 OCR）
CHAT_LIST_PX = 250       # 会话列表（有名字有预览，**必须切掉**，不然当成"对方原话"）
TITLE_BAR_PX = 56        # 顶部标题栏（联系人名 / 搜索框）
INPUT_BOX_PX = 96        # 底部输入区（**必须切掉**：用户敲了一半的草稿也是白底黑字）
# 底部输入区**至少**要切掉窗口高度的这个比例（v2-social-input-guard）：输入区在真机上
# 大体是**固定高度**的东西（不是按窗口比例长的），而那个固定的 96 逻辑像素只在一扇
# 1352 高的窗上量过。窗口拉到 2000+ 像素高时（最大化 / 4K）固定那一刀相对就薄了，
# 所以再压一条**按比例的下限**，两条取大——见 `bottom_cut_px`。
INPUT_BOTTOM_MIN_FRAC = 0.08
# 真机实测的输入区上沿落在窗口高度的哪儿（2026-10-08 重测，本机 DPR=1.5，微信 4.x，
# 群聊单页视图 742×1342 物理像素）：83.8% —— 输入区上沿在 y=1124，距底 218 物理像素，
# 那条紧贴输入框上方的对方末句才不再被一起切走。老值 93.8%（y=1269，距底 83）是拿
# 独立聊天窗量的，切点太高，把输入框上方最要紧的那条消息吃掉了（见
# `_detect_input_top_by_edge` 的失效现场）。
#
# 这个数摆在这儿是给**合成窗**用的：`tests/test_social_ocr_catch.py` 按它画输入区，
# **不再按 `INPUT_BOX_PX` 画**——拿被测常量画被测对象是自证，输入区画多高由常量说了算，
# "草稿没漏进来"就永远绿（v2-social-input-guard 修的就是那处假绿）。
INPUT_TOP_FRAC = 0.838
# 上面那两条是**按版面拍的固定值**（来源是一次测量），而输入区在真机上**随草稿长高**：
# 用户在输入框里敲上两行，它的上沿就往上抬一截，白底黑字的草稿于是落进裁剪框，被
# `is_other_bubble` 认成"对方原话"（v2-social-input-draft-exclude 的真机现场：TAIL 里
# 那半句"我这还没发力呢"就是**没发出去的草稿**）。固定那一刀对这种会长的东西没辙，
# 所以再加一条**像素量**的腿（`detect_input_top`），由 `input_cut_px` 两条一起看。
#
# v2-social-material-empty-fix（15-X）把两腿的次序**倒过来了**：老口径是"版面那条永远
# 保底"（`max(bottom_cut_px, 实测)`，**绝不会比 144 少**），那个 `max()` 把"输入区其实比
# 144 薄"这件事结构性地挡掉了——真机 2026-10-08 的 744×1342 那一帧上，`detect_input_top`
# 给 1327 / None，两种都算成 `max(144, 15)` = 144，切点落在 1198，而真机输入区从 1269
# 才开始：**紧贴输入框上方那条对方末句（距底约 73）被一起切走**，材料于是是空的。
# 现在：实测上沿优先（但要"不薄于按比例那条"才采信，防 1327 那种错锚），比例那条兜底，
# 底下再压一道绝对下限。`INPUT_BOX_PX` / `INPUT_BOTTOM_MIN_FRAC` 那两条不再参与底部那一刀，
# 只留给 `chat_client_rect`（QRect 那条没有像素数据的硬切）。
INPUT_ANCHOR_TOL = 30    # 实测上沿与"按比例那条"差多少之内才信实测（见 `input_cut_px`）。
                         # 比这条线**薄得多**的实测值就是锚错了（真机 1327 → 只剩 15），
                         # 比这条线**厚**的照旧采信（草稿把输入框顶高，批次12 那条腿）。
SAFE_FLOOR = 40          # 底部那一刀的绝对下限：矮窗上按比例算出来会小于一屏该切的量，
                         # 别切出 0 / 负数（真机 DPR 1.5 上约合 27 逻辑像素）。
INPUT_FILL_TOL = 2       # "输入区底色 == 聊天区底色"的容差。真机实测两者是 250 与 255，
                         # 差 5——比 `BG_TOL`(6) 还小，所以量这一处必须用更紧的容差才分得开。
INPUT_BAND_FRAC = 0.5    # 紧贴底部那一带里，至少这么多比例是同一个色，才算"真是一整块
                         # 输入区"。画面花掉时没有哪个色占得到一半（`_noise_frame` 那种
                         # 八色拼花只有约 1/8），这条就是拦它的。
INPUT_SCAN_MAX_FRAC = 0.35   # 从底往上最多找这么多：再往上就不是输入区，是消息区了
INPUT_ROW_SAMPLES = 48   # 估"这一行是什么色"时每行抽这么多点（纯 Python 逐点取色，抽稀）
INPUT_SKIP_PX = 4        # 最底下这几行可能是窗口边框 / 圆角，不算数
INPUT_BAND_RIGHT_FRAC = 0.5   # 只看右半边：主窗的输入区不到满宽，左半是会话列表那一列
# 第二条量法（"深字段"腿，v2-social-input-region-leak-fix 1c）：只在 fill 那条腿给不出结果时用。
# 判据是"白底黑字"——输入框里的草稿文字是深色笔画打在浅底上，一行里深色像素占比落在
# (0, 上限) 之间；整行全深（边框 / 深色主题 / 花屏）不算，占比超过 `INPUT_DARK_MAX_FRAC` 就排除。
INPUT_DARK_MAX_FRAC = 0.4  # 一行里深色像素占比的上限：超过它就不是"白底黑字"
INPUT_DARK_GAP = 3         # 深字段顶之上要连续这么多行"整行浅色"才算真到顶（分得清行间空档）
# 第三条量法（"边框线"腿，v2-social-input-edge-line）：fill 分不开（输入区底 == 聊天区底）、
# dark 又只瞄得见草稿那行时，改锚输入框上沿那条**横贯整宽**的边框线——它比聊天区底深一点
# （真机 231~249，底是 250）、又远没到"字"那么深（< 110）。判据是"一行里这么多比例的像素
# 都落在这个亮度区间里"，再配上"线上面是空白"才认。见 `_detect_input_top_by_edge`。
INPUT_EDGE_FRAC = 0.8      # 一行里"边框线色"像素占抽样的比例下限：够宽才算一条横贯的边框线
INPUT_EDGE_MAX_PX = 8      # 一条"边框线"最多这么高（物理像素）。再厚就不是线，是一整块浅色
                          # 区域（群公告气泡 / 图片卡片 / 大面积灰底）——真机那条只有 2 行
                          # （1124-1125），厚的那一块认成"输入框上沿"会一刀切掉几百像素。
RIGHT_PAD_PX = 8         # 右边留一点，免得把滚动条认成字
# 裁完比这个还小就宁可别裁了：小窗上切一刀只剩一条缝，反倒什么也认不到。
CHAT_MIN_SIDE = 120

# 像素法找"聊天区左边界"用的参数。
#
# `BG_TOL` 只有 6：会话列表底与聊天区底往往是**同一个浅色系**（255 与 243 这种），
# 容差一松，列表右边那段空白的列就会被当成"聊天区的空列"，边界一路左移到列表里头去。
# 取样也不能分桶——255 与 250 分到同一格里，等于又白送 8 点容差。按**原色**比。
BG_TOL = 6               # 与聊天区底色差多少以内算"还是这块底色"
BG_COL_FRAC = 0.85       # 一列里至少这么多比例是底色，才算"空列"
BG_RUN_PX = 40           # 连续这么多列都空，才算真进了聊天区（列表里两个头像之间也有空列）
# 兜底那条短闸（v2-social-tail-context-fix）：独立聊天窗的聊天区**紧挨着图标栏**，从底色
# 到对方头像只有约 30 物理像素，40 逻辑像素那条闸一条都过不了。见 `detect_chat_left`。
BG_RUN_MIN_PX = 12
BG_BAND = (0.25, 0.75)   # 统计用的行区间：避开标题栏与输入区
BG_SAMPLE = 4            # 取样步长（纯 Python 逐点取色，抽稀着来）
LEFT_CEIL = 0.60         # 找出来的左边界超过这个比例就不信它，退回按版面切

# —— 单页视图那两条腿（v2-social-group-fragment-leak 甲）——
#
# 真机 2026-10-08 16:52/16:54/16:55 那几帧（742×1342，群聊，从会话总列表点进去的**单页**）：
# 主候选的底色估成了**群公告气泡的填充色** (238,238,240)，而聊天区底色是 (250,250,250)，
# 差 34 远超 `BG_TOL`(6)——上面两条腿一起空转回 None，`chat_area_box` 于是退回按版面硬切
# `(60+250)×1.5 = 465`：**单页视图上那条版面线在窗口里没有对应物**，一刀削掉左起 370 像素，
# 群公告每一行的开头都被切走，OCR 只认回各行的尾巴，材料成了"平知学堂"这种残片。
BG_ALT_STRIP_PX = 24     # 副底色候选：紧贴图标栏右缘取这么宽一条带（物理像素），见
                         # `_alt_background_rgb`——那儿在两种版面上都一定是"边角底色"
CHAT_LIST_STRIP_PX = 46  # 数头像方块时看的那一条竖带有多宽（物理像素；真机头像约 54 见方）
CHAT_LIST_BLOCK_MIN_H = 20   # 一个"方块"至少这么高（物理像素）才算头像，见 `has_chat_list_column`
CHAT_LIST_BLOCK_MIN_N = 1    # 至少要数到这么多块才认"左侧有会话列表列"
CHAT_LIST_BLOCK_DARK_DIV = 4 # 一行的取样点里"非底色"的个数要占到 带宽/4 才算"这一行有东西"。
                             # 取样是每 2 像素取一个，所以它等价于**取样点的一半**
SINGLE_PAGE_PAD_PX = 8   # 判成单页视图时保留的极小左边距（逻辑像素）——放弃左切，但留一点
                         # 余量，别把窗口圆角 / 描边当字

# 助手窗的标题（v2-pet-persona-rename）：`ui/chat_bubble.py` 的 `CHAT_TITLE`，固定串。
# 那边另有一份常量，两处都改才作数（这一层不 import ui，免得 life → ui 反向依赖）。
PET_CHAT_TITLE = "AI助理"
# 换角色卡时 `ChatBubble.apply_speaker` 仍按这个写法改标题（那扇窗还是咱们自己的）。
PET_CARD_TITLE_FMT = "跟{}聊聊"


def is_pet_window(*, title: str, pid: int | None, pet_pid: int | None,
                  pet_name: str = "") -> bool:
    """这扇窗是不是宠物自己的（宠物本体 / 「AI助理」助手窗 / 气泡）。

    两道判据，任中一条就算：**进程号是咱们自己**（最硬的一条——气泡、助手窗都是本
    进程开的 Qt 工具窗，标题五花八门甚至为空，但 pid 一定是自己）；或者标题上写着
    「AI助理」（助手窗，v2-pet-persona-rename 起固定这么叫）/ 宠物名 / 「跟XX聊聊」
    （换过角色卡的那扇窗）。

    进程号那条为什么最要紧：`ChatBubble` 是 `Qt.Tool | FramelessWindowHint`，**根本没
    设过标题**（`GetWindowTextW` 回空串），只按标题认会把它整个漏掉——而它恰恰就悬在
    聊天窗上面，漏掉它等于把宠物自己说的话当成"对方原话"。
    """
    if pet_pid and pid and int(pid) == int(pet_pid):
        return True
    text = (title or "").strip()
    if not text:
        return False
    if PET_CHAT_TITLE in text:
        return True
    if pet_name:
        if text == pet_name:
            return True
        if PET_CARD_TITLE_FMT.format(pet_name) in text:
            return True
    return False


def bottom_cut_px(height: int, dpr: float = 1.0) -> int:
    """底部该切掉多少像素：`max(96 逻辑像素折算, 窗口高度的 8%)`。

    那一条固定的 96 逻辑像素（DPR 1.5 上是 144 物理像素）在**实测那扇窗**上足够：
    真机量到的输入区从 83.8% 才开始，1352 高的窗上它只有约 218 物理像素。但"固定"
    这个形状是**窗越高越薄**——输入区在真机上大体固定高，窗口却能拉到 2000+ 像素。
    所以再压一条**按比例的下限**（`INPUT_BOTTOM_MIN_FRAC`），两条取大，谁宽听谁的。

    高度取不到（0 / 负数）时只出固定那一刀：别拿一个坏尺寸乘出个负数，把整块画面切没。
    """
    fixed = int(INPUT_BOX_PX * _scale(dpr))
    if height <= 0:
        return fixed
    return max(fixed, int(height * INPUT_BOTTOM_MIN_FRAC))


def _scale(dpr) -> float:
    """物理像素 / 逻辑像素。取不到就当 1（宁可少切一点，也不按错的倍数切）。"""
    try:
        value = float(dpr)
    except (TypeError, ValueError):
        return 1.0
    return value if value > 0.1 else 1.0


def chat_client_rect(rect, dpr: float = 1.0):
    """从一个窗口矩形里裁出"聊天消息列表"那一块；太小或不合法就原样返回。

    `rect` 是 `QRect`（或任何有 left/top/width/height 的东西）。返回同类型的矩形——
    用 `.translated` / `.intersected` 那几个 Qt 方法算，别自己拼坐标。

    `dpr` 是这块画面里"物理像素 / 逻辑像素"的倍数：抓下来的帧是物理像素，而版面那几个
    数是逻辑像素，不乘这一步在高分屏上会少切一半。

    这是**按版面硬切**的那条路（不知道画面长什么样时用）；有像素时优先走
    `chat_area_box`——它会自己找出聊天区真正的左边界，连"这是个没有会话列表的独立
    聊天窗"这种版面都认。
    """
    if rect is None:
        return None
    try:
        left = int(rect.left())
        top = int(rect.top())
        width = int(rect.width())
        height = int(rect.height())
    except AttributeError:
        return rect
    if width <= 0 or height <= 0:
        return rect
    scale = _scale(dpr)
    x0 = left + int((SIDEBAR_PX + CHAT_LIST_PX) * scale)
    y0 = top + int(TITLE_BAR_PX * scale)
    x1 = left + width - int(RIGHT_PAD_PX * scale)
    y1 = top + height - bottom_cut_px(height, dpr)
    if (x1 - x0) < CHAT_MIN_SIDE or (y1 - y0) < CHAT_MIN_SIDE:
        return rect            # 小窗切了就没了，原样交回去
    return rect.__class__(x0, y0, x1 - x0, y1 - y0)


# ---------- 像素法：从画面里量出聊天区 ----------

def _dominant_rgb(image, x0f=0.5, x1f=1.0, y0f=0.0, y1f=1.0, step=BG_SAMPLE):
    """一块区域里最占地方的那个颜色。

    为什么"最常见"就够：聊天窗里聊天区面积最大，它的底色（浅灰 / 白）一定是整帧里
    最多的那一色；会话列表、图标栏再花也只是零头。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    counts: dict[tuple[int, int, int], int] = {}
    ys = range(max(0, int(height * y0f)), max(1, int(height * y1f)), step)
    xs = range(max(0, int(width * x0f)), max(1, int(width * x1f)), step)
    for y in ys:
        for x in xs:
            rgb = _pixel(image, x, y)
            if rgb is None:
                continue
            counts[rgb] = counts.get(rgb, 0) + 1      # **原色**比，不分桶（见 BG_TOL 那段）
    if not counts:
        return None
    return max(counts, key=counts.get)


def _column_is_background(image, x: int, bg, y0: int, y1: int) -> bool:
    """这一列有多少比例是聊天区底色。够高就是"空列"（没有气泡、没有字）。"""
    total = 0
    hit = 0
    for y in range(y0, max(y0 + 1, y1), 2):
        rgb = _pixel(image, x, y)
        if rgb is None:
            continue
        total += 1
        if _rgb_near(rgb, bg, BG_TOL):
            hit += 1
    if not total:
        return False
    return hit / total >= BG_COL_FRAC


def _first_empty_run(image, bg, y0: int, y1: int, start: int, stop: int,
                     run: int) -> int | None:
    """从 `start` 往右扫，回**第一条"连着 `run` 列都是底色"的空列**的 x；找不到回 None。

    贴着画面右端那一截不算数：`min(width, x + run)` 照旧放行（老口径就这样——`stop`
    是扫描范围的天花板，不是"必须比到头"）。取不到尺寸（画面是空的 / 形状对不上）时
    范围退化成空，老实回 None，绝不抛。
    """
    width, _height = _image_size(image)
    stop = min(stop, width)
    if stop <= start:
        return None
    for x in range(start, stop):
        if not _column_is_background(image, x, bg, y0, y1):
            continue
        if all(_column_is_background(image, xx, bg, y0, y1)
               for xx in range(x, min(width, x + run))):
            return x
    return None


def detect_chat_left(image, dpr: float = 1.0) -> int | None:
    """像素法找聊天区的左边界：**连续一长条空列**开始的地方。找不到给 None。

    会话列表那一段里，每一行都有头像和字，随便哪一列都过不了"85% 都是底色"这条；
    图标栏是另一种底色，也过不了。所以第一条"从这个 x 开始、连着 40 列都空"的位置，
    就是聊天区的左边——比按比例切准，而且**没有会话列表的独立聊天窗**也认。

    **第二条腿（v2-social-tail-context-fix）：贴着图标栏那一档。** 上面那条 40 逻辑
    像素（DPR 1.5 上 60 物理像素）的闸在真机的独立聊天窗上**一条都过不了**——2026-10-07
    那扇 759×1352 的窗量下来是：图标栏右缘 ≈ x=88，聊天区底色从 x=90 就开始了，可从
    底色到**对方头像**只有约 30 物理像素（对方的头像紧贴左缘，气泡紧挨头像）。于是

        `chat_area_box` 量不出左边界 → 退回按版面硬切（图标栏 + 会话列表 = 465 物理像素）
        → 聊天区左边三分之二连同每行开头的字一起被切掉，OCR 只认回各行的尾巴
        → 材料成了"零散几句"（真机上那句"是在撩我么？"就只剩"我么？"）。

    所以第一条腿扫完不中时，再扫**紧挨图标栏右缘**那一小段：只要那儿已经是一段底色
    （`BG_RUN_MIN_PX`，比长闸短得多），就认那儿是聊天区的左边。

    这一条腿**只认得出一种版面**：图标栏右缘紧贴着就是聊天区底色——独立聊天窗。
    会话列表那种版面在这儿顶着的是**列表底色**（跟聊天区底色差得开，`BG_TOL` 那条闸
    就是为它留的 6），所以主窗上这条腿一次都不会响；而它一旦响，方向是"**别切**"，
    比硬切掉半屏字安全。真要碰上"列表底色跟聊天区底色一模一样"的窗口，第一条腿
    （那 250 逻辑像素宽的整段列表都是空列）早就先响了，轮不到这条腿。

    这两条腿都**只管一种底色**：主候选（`_dominant_rgb` 取右半边主色）。底色估错时两条腿
    一起空转，于是有了**第三条腿（v2-social-group-fragment-leak 甲）**：拿**副候选**再跑
    一遍这两条腿。

    为什么要副候选：主候选取"右半边主色"，右半边被一条**大气泡**占着时，取回来的就是那条
    气泡的**填充色**。真机 2026-10-08 那扇 742×1342 的群聊单页窗上，群公告气泡的填充色是
    (238,238,240)、聊天区底色是 (250,250,250)，差 34（`BG_TOL` 只有 6）——聊天区每一列在
    主候选眼里都"有东西"，两条腿一次都不响，退回按版面硬切 465：**单页视图上那条版面线
    在窗口里没有对应物**（会话列表压在这个视图的下面），一刀削掉左起 370 像素，群公告每行
    开头都被切走，材料成了 `平知学堂` 这种残片。副候选取的是"紧贴图标栏右缘那一小段的主色"
    （`_alt_background_rgb`）——那儿在两种版面上都一定是聊天区/列表的**边角**，不会是气泡
    正中，取回来才像"底色"。它与主候选相同就不重复跑（主候选已经把两条腿跑过一遍了）。

    三条腿都不中就给 None，交回 `chat_client_rect` 那条按版面硬切的路（**这一条一个字没改**
    ——`detect_chat_left` 的 None 仍是"像素这一层量不出来"，"量不出来时左边那刀怎么办"由
    调用方 `_chat_left` 按"有没有会话列表列"分档，见那儿）。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    bg = _dominant_rgb(image, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    if bg is None:
        return None
    y0 = int(height * BG_BAND[0])
    y1 = max(int(height * BG_BAND[1]), y0 + 1)
    scale = _scale(dpr)
    run = max(8, min(int(BG_RUN_PX * scale), width // 8))
    floor = max(1, int(SIDEBAR_PX * scale))      # 再左也不可能是聊天区（那是图标栏）
    ceiling = max(floor + 1, int(width * LEFT_CEIL))
    start = min(floor, max(0, width - 1))
    near = max(4, int(BG_RUN_MIN_PX * scale))

    def _two_legs(colour) -> int | None:
        """长短两条腿都拿 `colour` 当底色跑一遍（口径与老代码一字不差）。"""
        left = _first_empty_run(image, colour, y0, y1, start, ceiling, run)
        if left is not None:
            return left
        return _first_empty_run(image, colour, y0, y1, start, start + near, near)

    left = _two_legs(bg)
    if left is not None:
        return left
    alt = _alt_background_rgb(image, dpr)
    if alt is not None and not _rgb_near(alt, bg, BG_TOL):
        return _two_legs(alt)
    return None


def _alt_background_rgb(image, dpr: float = 1.0):
    """**副底色候选**：紧贴图标栏右缘那一小段的主色（v2-social-group-fragment-leak 甲）。

    为什么是"图标栏右缘那一小段"：那一竖条在两种版面上都是**边角**——主窗上是会话列表的
    最左边一条（列表底），单页窗上是聊天区的最左边一条（聊天区底）。它不会是"一条大气泡
    的正中"，所以拿它当底色比"右半边主色"稳。取不到尺寸 / 形状对不上回 None（调用方当
    "没有副候选"，一个字不猜）。
    """
    width, _height = _image_size(image)
    if width <= 0:
        return None
    x0 = max(0, int(SIDEBAR_PX * _scale(dpr)))
    x1 = min(width, x0 + max(4, int(BG_ALT_STRIP_PX * _scale(dpr))))
    if x1 <= x0:
        return None
    return _dominant_rgb(image, x0 / width, x1 / width, BG_BAND[0], BG_BAND[1])


def has_chat_list_column(image, dpr: float = 1.0) -> bool:
    """左侧那一段里**有没有"会话列表列结构"**：一条竖带里数得着头像方块（甲）。

    只在 `detect_chat_left` 三条腿全不中时才问它一句，用来分"量不出来时左边那刀怎么办"
    （`_chat_left`）：**有结构 = 主窗**，保留 `(SIDEBAR_PX + CHAT_LIST_PX)` 那条版面兜底；
    **没结构 = 单页视图**（会话压在这一页下面，对话从图标栏右边就开始了），放弃左切。

    数的是**头像方块**（一条 46 物理像素宽的竖带里，连着 ≥ `CHAT_LIST_BLOCK_MIN_H` 行都有
    东西），不是"文字行"：头像是一块实心的方，行位置在真机上很稳；文字行太碎，凑阈值的
    那条线会把"三人群、只有名字没有预览"的列表误判成单页。

    真机 81 帧（`%TEMP%/p18/probe_fix.py`，2026-10-08 抓到的那一批）量下来的分档：
    **主窗（有会话列表列）一律 7 块**；1:1 单页窗 2–3 块（那是聊天区里对方自己的头像，
    落在同一条 x 带上）；群聊单页窗与 435×228 那种小窗 **0 块**。阈值取 1（"一个都没有
    才算没有结构"）：0 块那几帧正是三条腿全不中的那几帧（141124 / 142602 / 165214 /
    165548 / 165613），而**花屏帧量到的是 1 块**（整条带子都是一块杂色）——于是它照样走
    "有结构"那一档，`test_social_ocr_catch.py` 里"画面花了 → 左边界退回硬切 465"那条断言
    一字不动。

    **已知限制**（留给真帧再修）：1:1 单页窗的对方头像落在同一条 x 带上，会被数成 2–3 块；
    那种帧要是三条腿全不中，会被判成"有结构"、退回 465 硬切。真机上没碰上——1:1 那几帧
    的主候选或副候选总有一条能量出来（见上面 81 帧的读数）。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return False
    scale = _scale(dpr)
    x0 = int(SIDEBAR_PX * scale)
    x1 = min(width, x0 + max(8, int(CHAT_LIST_STRIP_PX * scale)))
    if x1 - x0 < 8:
        return False
    bg = _dominant_rgb(image, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    if bg is None:
        return False
    y0 = int(height * BG_BAND[0])
    y1 = max(int(height * BG_BAND[1]), y0 + 1)
    span = x1 - x0
    min_h = max(4, int(CHAT_LIST_BLOCK_MIN_H * scale))
    blocks = 0
    run = 0
    for y in range(y0, y1):
        dark = 0
        for x in range(x0, x1, 2):
            rgb = _pixel(image, x, y)
            if rgb is None:
                continue
            if not _rgb_near(rgb, bg, BG_TOL):
                dark += 1
        # 用乘法比、不做整除：`dark × 4 ≥ 带宽` 就是 `dark ≥ 带宽/4` 本身。整除那一版在
        # 真机 141124 那帧上正好卡在刀口（一行 17 个取样点，整除取 17 就"中"、除以 4
        # 得 17.25 就不中），而 81 帧的分档读数是按"乘以 4"那条量出来的——分档得跟
        # 量它的那把尺子一致，不然同一帧两个数。
        if dark * CHAT_LIST_BLOCK_DARK_DIV >= span:
            run += 1
            continue
        if run >= min_h:
            blocks += 1
        run = 0
    if run >= min_h:
        blocks += 1
    return blocks >= CHAT_LIST_BLOCK_MIN_N


# ---------- 像素法：从画面里量出"输入区上沿"（v2-social-input-draft-exclude） ----------

def _row_dominant(image, y: int, x0: int, x1: int):
    """这一行里最占地方的那个颜色（只抽 `INPUT_ROW_SAMPLES` 个点估）。取不到给 None。"""
    width = x1 - x0
    if width <= 0:
        return None
    step = max(1, width // INPUT_ROW_SAMPLES)
    counts: dict[tuple[int, int, int], int] = {}
    for x in range(x0, x1, step):
        rgb = _pixel(image, x, y)
        if rgb is None:
            continue
        counts[rgb] = counts.get(rgb, 0) + 1
    if not counts:
        return None
    return max(counts, key=counts.get)


def _band_fill(image, y0: int, y1: int, x0: int, x1: int):
    """一块区域的主色，以及它占了多少比例。量不出回 `(None, 0.0)`。"""
    width = x1 - x0
    if width <= 0 or y1 <= y0:
        return None, 0.0
    step = max(1, width // INPUT_ROW_SAMPLES)
    counts: dict[tuple[int, int, int], int] = {}
    total = 0
    for y in range(y0, y1):
        for x in range(x0, x1, step):
            rgb = _pixel(image, x, y)
            if rgb is None:
                continue
            counts[rgb] = counts.get(rgb, 0) + 1
            total += 1
    if not total:
        return None, 0.0
    best = max(counts, key=counts.get)
    return best, counts[best] / total


def _band_lightest(image, y0: int, y1: int, x0: int, x1: int, exclude=None,
                   tol: int = 0, darker_than=None):
    """这一带里**最浅**（亮度最高）的那个颜色；与 `exclude` 分不开的不算。量不出回 None。

    **为什么锚"最浅"而不是"最占地方"**（v2-social-leak-fix-anchor）：输入框底下那条工具栏 /
    按钮（真机上就是表情 / 加号 / 发送那一排）比输入框底色**深**，却可能占掉底带的一半
    以上——按"最占地方的"取锚就会锚到工具栏身上，从底往上第一行就对不上，量出来只剩工具栏
    那几十像素（= 量浅 / 退化）。输入框底色是这一带里**最浅**的那一层，锚它才找得到输入框
    真底。`exclude` 传聊天区底色：输入区矮于版面那条下限时聊天区底色也会落进这条带子，
    它是"不是输入框"的色，得先排掉。

    一两个抗锯齿的亮点不许把锚抢走：只收出现次数够多的色（≥ 抽样的 2%）。

    `darker_than` 传这一带的**主色**（输入区该有的底）：锚到的"最浅色"要是比它还深，那这个
    锚就不是输入框底，而是压在下头那条工具栏 / 边框（v2-social-input-edge-line 的失效现场：
    输入区底 == 聊天区底、都被 `exclude` 排掉，只剩更深的工具栏可锚）——老实回 None 别硬猜。
    """
    width = x1 - x0
    if width <= 0 or y1 <= y0:
        return None
    step = max(1, width // INPUT_ROW_SAMPLES)
    counts: dict[tuple[int, int, int], int] = {}
    total = 0
    for y in range(y0, y1):
        for x in range(x0, x1, step):
            rgb = _pixel(image, x, y)
            if rgb is None:
                continue
            counts[rgb] = counts.get(rgb, 0) + 1
            total += 1
    if not total:
        return None
    floor = max(2, total // 50)          # 2%：一两颗散点不算
    best = None
    best_luma = -1.0
    for rgb, count in counts.items():
        if count < floor:
            continue
        if exclude is not None and _rgb_near(rgb, exclude, tol):
            continue
        luma = (rgb[0] + rgb[1] + rgb[2]) / 3.0
        if luma > best_luma:
            best_luma, best = luma, rgb
    if darker_than is not None and best is not None \
            and (best[0] + best[1] + best[2]) < (darker_than[0] + darker_than[1] + darker_than[2]):
        return None                                # 锚比这一带的主色还深：不是输入框底
    return best


def detect_input_top(image, dpr: float = 1.0) -> int | None:
    """像素法找"输入区上沿"：回那一行的 y；量不出来给 None。

    **为什么非量不可**：`INPUT_BOX_PX` / `INPUT_BOTTOM_MIN_FRAC` 是按"输入区固定高"这个
    形状拍的，而真机上输入区**随草稿长高**——用户敲了两行，上沿就抬上来一截，白底黑字的
    草稿落进裁剪框，`is_other_bubble` 把它认成"对方原话"（真机那次 TAIL 里混着半句没发出去
    的草稿，正是这么来的）。固定那一刀对"会长高的东西"没辙，只能当场量。

    **三条腿**（v2-social-input-region-leak-fix 1c / v2-social-input-edge-line）：第一条
    （fill）从窗口底往上逐行看"这一行的主色还是不是紧贴底部那一条的主色"，一断就停，断的那
    一行就是上沿——它在"输入区与聊天区底色分得开"（真机 250 与 255，差 5）时够用；分不开
    （`fill ≈ bg`，换肤 / 深色主题 / 底部那条带子被白气泡占了）时它老实回 None，交第二条
    （edge）锚**输入框上沿那条横贯整宽的边框线**兜底（真机群聊单页 250==250 的现场：fill
    锚到工具栏、dark 只瞄得见草稿，两条都量不出输入区上沿）；edge 也没有（没那条线 / 线上面
    不是空白）才交第三条（dark）用**深字段**兜底：输入框里的草稿是白底黑字，从底往上撞见的
    那段深色笔画就是草稿，取它最上面一行当上沿——前提是它顶上有一道"整行浅色"空隙（输入框顶
    与消息区之间的空白 / 边框），找不到就回 None，宁可不切，退回版面那一刀。

    四处退化都老实回 None（交给版面那条腿）：
      · 紧贴底部那一带不是一整块同色（画面花了 / 底下是别的窗口）——`INPUT_BAND_FRAC`；
      · 输入区与聊天区底色分不开（真机是 250 与 255，只有 5 的差）——`INPUT_FILL_TOL`；
      · 一路够到 `INPUT_SCAN_MAX_FRAC` 还没断——那不是输入区，是整屏一个色；
      · 量出来的上沿太靠上，切完剩不下聊天区（`CHAT_MIN_SIDE`）——宁可少切也别把消息吃掉。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    top = _detect_input_top_by_fill(image, dpr, width, height)
    if top is not None:
        return top
    top = _detect_input_top_by_edge(image, dpr, width, height)
    if top is not None:
        return top
    return _detect_input_top_by_dark(image, dpr, width, height)


def _detect_input_top_by_fill(image, dpr: float, width: int, height: int) -> int | None:
    """第一条量法：从窗口底往上，量出"输入框底"再扫到"输入区上沿"。

    **锚换到了"最浅的那个色"**（v2-social-leak-fix-anchor）。老口径锚的是"紧贴底部那一带里
    最占地方的颜色"，真机上那条带子里除了输入框底色，还压着输入框底下那一条工具栏 /
    发送按钮（比输入框底色**深**）。工具栏一旦占掉带子的一半以上，"最占地方的"就锚到它
    身上——从底往上第一行是工具栏、跟锚对不上，量出来只剩工具栏那几十像素，`input_cut_px`
    被 `max()` 钉回 `bottom_cut_px`（144），**草稿从顶上漏进材料**（就是这一轮要治的）。
    输入框底色是这条带子里**最浅**的那一层，锚它才找得到输入框真底。

    扫法跟着分两段：
      1. **先跳过输入框底下那条工具栏 / 按钮 / 边框**（既不是输入框底、也不是聊天区底）；
         只在这条"紧贴底部那一带"（`band_top` 往上到底）里跳——出了带子还没找到输入框底，
         就说明量不出来，回 None，别硬猜；
      2. 从输入框底往上，一行还是这个色就继续，断了那一行就是**上沿**。

    锚点不拿 `INPUT_BOX_PX` 反推——那是被测常量，拿它定锚就是自证；锚在"版面那条下限之内"
    （`bottom_cut_px` 说这一段一定在输入区里），比反推硬。

    **"必须连着底"这个形状是这条判据的命根子**：聊天区里的白气泡再宽也**连不到底**
    （底下永远隔着输入区），所以谁也吃不到消息区里去。

    三处退化都老实回 None（交给第二条"深字段"腿）：
      · 底带里没有哪个色占得到一半（画面花了）——`INPUT_BAND_FRAC`；
      · 底带里没有"不是聊天区底、又够亮"的那个色（两边同色 / 底带全是工具栏）——`_band_lightest`；
      · 底那一行直接就是聊天区底色（这儿压根没有输入框）。
    """
    x0 = max(0, int(width * INPUT_BAND_RIGHT_FRAC))
    y_start = height - max(1, INPUT_SKIP_PX)          # 最底下那几行（边框）不算
    floor = bottom_cut_px(height, dpr)
    band_top = height - floor                          # 这一段一定在输入区里面
    if band_top <= 0 or band_top >= y_start:
        return None                                    # 窗太矮，连"底那一条"都排不出来
    prominent, frac = _band_fill(image, band_top, y_start, x0, width)
    if prominent is None or frac < INPUT_BAND_FRAC:
        return None                                    # 底带不是一整块同色：画面花了
    bg = _dominant_rgb(image, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    fill = _band_lightest(image, band_top, y_start, x0, width, bg, INPUT_FILL_TOL,
                          darker_than=prominent)
    if fill is None:
        return None                                    # 底带里没有"不是聊天区底"的浅色（同色/全是工具栏）
    cap = max(0, height - int(height * INPUT_SCAN_MAX_FRAC))
    y = y_start - 1
    # 第一段：跳过输入框底下那条工具栏 / 按钮 / 边框（只在那条带子里跳）
    while y >= band_top:
        row = _row_dominant(image, y, x0, width)
        if row is None:
            return None
        if _rgb_near(row, fill, INPUT_FILL_TOL):
            break                                      # 撞见输入框底了
        if bg is not None and _rgb_near(row, bg, INPUT_FILL_TOL):
            return None                                # 底那一行就是聊天区底色：没输入框可量
        y -= 1
    else:
        return None                                    # 带子里全是工具栏 / 按钮，找不到输入框底
    # 第二段：从输入框底往上，行还是这个色就继续，断了就是上沿
    top = None
    while y >= cap:
        row = _row_dominant(image, y, x0, width)
        if row is None or not _rgb_near(row, fill, INPUT_FILL_TOL):
            top = y + 1                                # 这一行断了：上沿就在下面一行
            break
        y -= 1
    if top is None:
        return None                                    # 一路够到上限还没断
    if top - int(TITLE_BAR_PX * _scale(dpr)) < CHAT_MIN_SIDE:
        return None                                    # 切完剩不下聊天区，别切
    return top


def _row_edge_frac(image, y: int, x0: int, x1: int, bg_luma: float) -> float | None:
    """这一行里"边框线色"（比聊天区底深、又比字浅）像素占抽样的比例。量不出回 None。

    边框线 = 输入框那个圆角矩形的上沿那条线：真机上它横贯整宽，颜色比聊天区底（250）深
    一点点（231~249），但远没到"字"那么深（< `TEXT_LUMA_MAX`）。判据卡在 `[TEXT_LUMA_MAX,
    bg_luma)` 之间，正好把"一整行白底黑字"（深色笔画）与"一整行聊天区底"（不比底深）都排掉。
    """
    width = x1 - x0
    if width <= 0:
        return None
    step = max(1, width // INPUT_ROW_SAMPLES)
    edge = 0
    total = 0
    for x in range(x0, x1, step):
        rgb = _pixel(image, x, y)
        if rgb is None:
            continue
        total += 1
        luma = (rgb[0] + rgb[1] + rgb[2]) / 3.0
        if TEXT_LUMA_MAX <= luma < bg_luma:
            edge += 1
    if not total:
        return None
    return edge / total


def _detect_input_top_by_edge(image, dpr: float, width: int, height: int) -> int | None:
    """第二条量法（v2-social-input-edge-line）：靠**输入框上沿那条边框线**兜底。

    真机失效现场（也是这一轮要治的）：输入区底 == 聊天区底（群聊单页视图上都是 250），fill
    那条腿把"最浅且不是聊天区底"的色锚到了输入框**底下**那条工具栏（247），量出来只剩 15
    像素；dark 那条腿锚的是草稿那一行（1277），也不是输入区上沿。两条腿都给不出输入区上沿，
    底部那一刀只能按老比例切，切点太高，把紧贴输入框上方那条对方末句吃掉了。

    这一条换个锚：输入框的上沿有一条**横贯整宽**的边框线（圆角矩形的上边框），它比聊天区底
    深一点（真机 231~249，底是 250）、又远没到"字"那么深（< 110）。从窗口底往上找"一行里 ≥
    `INPUT_EDGE_FRAC` 的像素是边框线色"的横线，**撞见的第一条够薄的**就是它——真机那条在
    y=1124（距底 218），正是输入框顶；再往下的工具栏那一段又厚又花，高度超过
    `INPUT_EDGE_MAX_PX`，不算线，跳过去。

    接受前提三条：① 这条线**只有线那么薄**（高 ≤ `INPUT_EDGE_MAX_PX`——太厚就是群公告气泡 /
    图片卡片那种一整块浅色，不是边框线）；② 它落在 `INPUT_SCAN_MAX_FRAC` 之内；③ 切完还剩下
    聊天区（`CHAT_MIN_SIDE`）。三条有一条不成立就回 None，宁可不切，退回版面那一刀。
    """
    bg = _dominant_rgb(image, 0.5, 1.0, BG_BAND[0], BG_BAND[1])
    if bg is None:
        return None                                    # 聊天区底都量不出，别猜边框线比它深还是浅
    bg_luma = (bg[0] + bg[1] + bg[2]) / 3.0
    y_start = height - max(1, INPUT_SKIP_PX)
    cap = max(0, height - int(height * INPUT_SCAN_MAX_FRAC))
    top = None
    run_top = None                                     # 当前这段"边框线色"的最上一行
    run_bottom = None                                  # 这一段的最下一行（y 最大那行）
    y = y_start - 1
    while y >= cap:
        frac = _row_edge_frac(image, y, 0, width, bg_luma)
        if frac is None:
            return None
        if frac >= INPUT_EDGE_FRAC:
            if run_top is None:
                run_bottom = y                         # 这一段是从这儿开始的
            run_top = y                                # 还在这一段里：上沿跟着往上走
        elif run_top is not None:
            if run_bottom - run_top + 1 <= INPUT_EDGE_MAX_PX:
                top = run_top                          # 从底往上撞见的第一条够薄的线
                break
            run_top = run_bottom = None
        y -= 1
    if top is None and run_top is not None and run_bottom - run_top + 1 <= INPUT_EDGE_MAX_PX:
        top = run_top
    if top is None:
        return None                                    # 没找到够薄的边框线
    if top - int(TITLE_BAR_PX * _scale(dpr)) < CHAT_MIN_SIDE:
        return None                                    # 切完剩不下聊天区，别切
    return top


def _row_dark_frac(image, y: int, x0: int, x1: int) -> float | None:
    """这一行里**深色**像素（亮度 < `TEXT_LUMA_MAX`）占抽样的比例。量不出回 None。

    深色 = 笔画（文字 / 图标）；浅色 = 背景。抽稀着来（`INPUT_ROW_SAMPLES`），跟
    `_row_dominant` 同一个取样步长，纯 Python 逐点取色。
    """
    width = x1 - x0
    if width <= 0:
        return None
    step = max(1, width // INPUT_ROW_SAMPLES)
    dark = 0
    total = 0
    for x in range(x0, x1, step):
        rgb = _pixel(image, x, y)
        if rgb is None:
            continue
        total += 1
        if (rgb[0] + rgb[1] + rgb[2]) / 3 < TEXT_LUMA_MAX:
            dark += 1
    if not total:
        return None
    return dark / total


def _detect_input_top_by_dark(image, dpr: float, width: int, height: int) -> int | None:
    """第二条量法（v2-social-input-region-leak-fix 1c）：靠草稿的**深字段**兜底。

    真机最可能的失效现场是 `fill ≈ bg`——输入区与聊天区底色分不开，fill 那条腿回 None。
    这条腿换个锚：输入框里的草稿是白底黑字，从窗口底往上一定先撞见一段深色笔画（草稿），
    再往上是聊天区。于是：

    1. 从底往上，先跳过"整行浅色"（草稿以下输入框里的空白），撞见第一行"白底黑字"
       （深色占比落在 `(0, INPUT_DARK_MAX_FRAC)`）当草稿底；
    2. 一路往上，只要还是深字段就继续，撞见非深字段（整行浅色 / 整行深）就停；
       中间允许 ≤ `INPUT_DARK_GAP` 行的整行浅色空档（草稿多行的行间空档）；
    3. **接受前提**：深字段顶之上要出现**连续 `INPUT_DARK_GAP` 行整行浅色**空隙
       （输入框顶与消息区之间的空白 / 边框），找不到就回 None——没有这道空隙，就分不清
       上面已经是聊天区、还是只是草稿内部的行间空档，宁可不切，退回版面那一刀。

    草稿在真机上是**左对齐**的，所以这条腿看**整行宽**（跟 fill 只看右半边不同）：看右半
    会漏掉短草稿。花屏 / 整行深 / 纯色帧都老实回 None（见 `_row_dark_frac` 的三分类）。
    """
    x0 = 0
    y_start = height - max(1, INPUT_SKIP_PX)
    cap = max(0, height - int(height * INPUT_SCAN_MAX_FRAC))
    top = None
    light_run = 0
    y = y_start - 1
    while y >= cap:
        frac = _row_dark_frac(image, y, x0, width)
        if frac is None:
            return None
        if 0.0 < frac < INPUT_DARK_MAX_FRAC:
            top = y                                     # 深字段，接着往上找
            light_run = 0
        elif frac == 0.0:
            light_run += 1                              # 整行浅色：空档
            if top is not None and light_run >= INPUT_DARK_GAP:
                break                                   # 深字段顶上出现够长的空档，到头了
        else:
            return None                                 # 整行深 / 花：判不了，别猜
        y -= 1
    if top is None or light_run < INPUT_DARK_GAP:
        return None                                     # 没撞见深字段 / 没见着顶上空档
    if top - int(TITLE_BAR_PX * _scale(dpr)) < CHAT_MIN_SIDE:
        return None                                     # 切完剩不下聊天区，别切
    return top


def input_cut_px(image, dpr: float = 1.0) -> int:
    """这一帧底部该切掉多少：**先按实测的输入区上沿，实测不可信才按版面比例**（15-X）。

    老口径是 `max(版面那条, 实测)` —— "版面那条永远保底，绝不会比原来切得少"。那个
    `max()` 把"输入区其实比 144 薄"这件事**结构性地**挡掉了：真机 2026-10-08 那两帧
    （744×1342）上，`detect_input_top` 一次给 1327、一次给 None，`max(144, 15)` 与
    `max(144, …)` 都落回 144，切点 1198；而真机上输入区从 1269（93.8%）才开始——
    **紧贴输入框上方那条对方末句（距底约 73）被一起切走**，材料于是空的。

    现在的次序：

    1. `frac_cut = 高 × (1 − INPUT_TOP_FRAC)`：版面实测的比例，真机那扇 1342/1352 高的
       窗上就是 83。它不碰像素，**涂白 / 同色 / 画面花都不影响它**；
    2. 像素腿量到的那条上沿，**只有在不薄于比例线（差在 `INPUT_ANCHOR_TOL` 之内）时才采信**：
       真机 1327 那一下只算出 15，比 83 薄得多——那是锚到了贴底的工具栏 / 边框，不是输入区
       上沿，丢掉；草稿把输入框顶高（量出来比比例线厚）照旧采信，**批次12 那条腿一分没松**；
    3. 底下压一道 `SAFE_FLOOR`：矮窗上别切出 0 / 负数。

    **已知限制**（留给真帧再修）：输入区大体是**固定高**的东西，而 `INPUT_TOP_FRAC` 是拿
    一扇 1352 高的窗量出来的——窗一矮（比如 300 物理像素），比例那条会比真输入区薄，
    这时只有像素腿能救；两处都量不出来（同色 / 花屏 + 矮窗）时会漏一点。

    尺寸都取不到（0 / 负数）时只出 `bottom_cut_px` 那条老口径：别拿一个坏尺寸乘出个负数，
    把整块画面切没。
    """
    height = _image_size(image)[1]
    if height <= 0:
        return bottom_cut_px(height, dpr)
    frac_cut = int(height * (1.0 - INPUT_TOP_FRAC))
    cut = frac_cut
    top = detect_input_top(image, dpr)
    measured = None if top is None else height - top
    if measured is not None and measured >= frac_cut - INPUT_ANCHOR_TOL:
        cut = measured                    # 实测优先（"不薄于比例线"才信，见函数头第 2 条）
    cut = max(SAFE_FLOOR, cut)
    logger.debug(
        "输入区那一刀：帧高 %d | 比例那条 %d | detect_input_top %s | 实测 %s | 最终 cut %d",
        height, frac_cut, top, measured, cut,
    )
    return cut


def _chat_left(image, dpr: float = 1.0) -> int:
    """左沿那一刀切在哪儿：**量得出来就用量出来的；量不出来时按"有没有会话列表列"分两档**。

    这一条是 `chat_area_box` 与 `cut_edges_box` 共用的落点（两边原来各写一遍"None 就退回
    `(SIDEBAR_PX + CHAT_LIST_PX)`"），v2-social-group-fragment-leak 甲把分档加在这儿：

      · `detect_chat_left` 量得出 → 用它（老口径，一个字没改）；
      · 量不出、但左侧数得着会话列表列结构 → **主窗**：照旧退回按版面硬切
        `(SIDEBAR_PX + CHAT_LIST_PX)`（老口径，一个字没改）；
      · 量不出、左侧压根没有列结构 → **单页视图**（会话压在这一页下面，对话从图标栏右边
        就开始了）：**放弃左切**，只留 `SINGLE_PAGE_PAD_PX` 那点边距。真机 2026-10-08
        那几帧上，硬切 465 干的就是"把群公告每行开头削掉"，材料成了残片。

    为什么"放弃左切"这一档敢这么松：`cut_edges_box` 本来就有同一个态度——左边那一刀会
    把这帧切成负的就放弃左切、保留完整宽度（那一段的注释写着"那种尺寸上本来也认不出什么
    字，而留着完整宽度总比切坏强"）。宁可让 OCR 多认一列图标栏的杂字，也别把对方的话
    削成半句。**这一档只有"量不出来"时才走得到**：量得出来的帧一分没变。

    顺带把左沿的**来源**打进日志（v2-social-group-fragment-leak 丙）：量出来的 / 副候选
    量出来的 / 判成单页放弃左切 / 按版面兜底——跟"输入区那一刀"同一类，出问题时一眼看得出
    这一帧的左沿是量出来的还是拍的。只加一行 DEBUG，行为一个字没动。
    """
    width = _image_size(image)[0]
    scale = _scale(dpr)
    left = detect_chat_left(image, dpr)
    if left is not None:
        source = "实测"
    elif has_chat_list_column(image, dpr):
        left = int((SIDEBAR_PX + CHAT_LIST_PX) * scale)
        source = "有会话列表列，按版面兜底"
    else:
        left = max(0, int(SINGLE_PAGE_PAD_PX * scale))
        source = "单页视图（左侧没有会话列表列），放弃左切"
    logger.debug("聊天区左沿：帧宽 %d | %s | 最终 left %d", width, source, left)
    return left


def chat_area_box(image, dpr: float = 1.0) -> tuple[int, int, int, int] | None:
    """一帧窗口画面 → "聊天消息列表"那一块 `(x, y, w, h)`。Qt 无关（不 import Qt）。

    左边界先走像素法（`detect_chat_left`），找到了就用它——这样靠左的对方气泡一个字
    都不会被削掉；找不到才退回 `chat_client_rect` 的按版面硬切。上下右三条边照旧按
    版面的固定像素切（标题栏 / 输入区 / 滚动条），它们不随窗口宽度变，不需要找。

    "找不到时切哪儿"这一档在 `_chat_left`（甲：有会话列表列才按版面切，单页视图放弃左切）。

    量不出来（画面是空的、形状对不上）给 None——调用方就当没裁，别硬算。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    scale = _scale(dpr)
    left = _chat_left(image, dpr)
    top = int(TITLE_BAR_PX * scale)
    right = width - int(RIGHT_PAD_PX * scale)
    bottom = height - input_cut_px(image, dpr)
    if (right - left) < CHAT_MIN_SIDE or (bottom - top) < CHAT_MIN_SIDE:
        return None            # 切了也没剩什么，交给调用方原样用
    return (left, top, right - left, bottom - top)


def cut_edges_box(image, dpr: float = 1.0) -> tuple[int, int, int, int] | None:
    """最后一刀（v2-social-leak-mask）：**至少**把标题栏与会话列表切掉，切不动给 None。

    什么时候走到这儿：`chat_area_box` 量不出（画面花了 / 形状对不上），退回
    `chat_client_rect` 又**原样交回**——那是"窗口太小、切完剩不下东西"那一档
    （`CHAT_MIN_SIDE` 那道闸）。原来这时只剩"切掉底部输入区"一刀，于是**标题栏**
    （独立聊天窗上写的就是联系人 / 群名）和**会话列表**（群名 + 消息预览）整块留在
    画面里。那两处都是浅底深字，`is_other_bubble` 认下来正好是"对方"——群名就这么
    变成"对方原话"（v2-social-ctx-pollution-trace 的 A-4）。

    跟 `chat_area_box` 的关系：**同一条版面**（左 `_chat_left`、上
    `TITLE_BAR_PX`、右 `RIGHT_PAD_PX`、下 `input_cut_px`），只放开"切完够不够大"
    那道闸。所以它不是另立一套口径，是同一个口径在"小窗"上的那一档。

    三处退化，都有意为之：
      · 左边那一刀会把这帧切成负的（窗比版面还窄）→ **放弃左边那一刀、保留完整宽度**，
        上下照切。那种尺寸上本来也认不出什么字，而"留着完整宽度"总比切坏强。
      · 左边**量不出来、左侧又没有会话列表列**（单页视图）→ 同上，放弃左切
        （`_chat_left` 的甲那一档；同一个态度，只是触发它的是"判成单页"而不是"切完是负的"）。
      · 连上下都切不动 → 给 None，调用方退回"只切底部"那条老路（本函数绝不返回比
        老路更坏的东西）。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    scale = _scale(dpr)
    top = max(0, min(int(TITLE_BAR_PX * scale), height))
    bottom = max(0, min(height - input_cut_px(image, dpr), height))
    if bottom - top < 1:
        return None            # 连标题栏和输入区都切不动，这帧没救
    left = _chat_left(image, dpr)
    left = max(0, min(int(left), width))
    right = max(0, min(width - int(RIGHT_PAD_PX * scale), width))
    if right - left < 1:
        left, right = 0, width
    return (left, top, right - left, bottom - top)


# ---------- 视图判据：这一帧像不像"会话总列表"（v2-social-listview-leak-fix 16-α） ----------
#
# **只用于取证落图，一个字都不参与"跳过 / 不起草"的判定**（那只等 16-β 拿到真帧再定）。
#
# 为什么需要它：微信切到"会话总列表"那一屏（右侧整片都是列表，没有气泡、没有单聊
# 输入框）时，现在的链路照样把它当聊天读——前台进程没变（`is_social_proc` 认的是
# **应用**不是**视图**）、`chat_area_box` 只要框够大就给、列表行浅底深字在色卡眼里
# 就是"对方"。故障链的只读枚举见收尾报告第一百五十四节。
#
# 两条一起看——(b) 是决断，(a) **只进日志、不参与判定**：
#   (a) **量不量得到输入框带**：总列表那一屏没有单聊输入框；
#   (b) **有字行够多、且大多数行到尾都离右边一大截**：列表的节奏——每行都是"头像 +
#       昵称 / 末条预览"从同一个 x 起头、右边整片留白。聊天里**自己的绿气泡靠右**，
#       必定有一批行的字顶到右边去，(b) 当场就不成立——"看不出气泡流"那一条就落在
#       这儿（比"行首离散度"结实：只收对方消息的单聊也可疑，但**任何**靠右的泡都能
#       把 (b) 否掉）。
#
# **(a) 为什么不做闸**（合成帧实测，probe_16a）：真机那种"列表行一路排到输入区上面"的
# 帧上，`detect_input_top` 会回一个**假的上沿**——它第二条"深字段"腿从底往上撞见的那段
# 深色，正是**列表最后一行**的昵称 / 末条预览，量出来跟"草稿漏进裁剪框"长得一模一样
# （合成帧上回的是 1182，那一行就是列表第 18 行）。拿它当闸，列表帧会被自己挡在门外，
# 判据恒回 False ——跟 16-α"先把真帧抓下来"的目的正好反着。它也不是全没用：真聊天
# 常常压根回 None（15-X 实测的 744×1342 那帧就是），所以两个方向都不足以定案——**只当
# 线索记一笔**，将来配着真帧看这一格在两种视图下各是什么值。
#
# **量哪一块**：从**聊天区那条版面线**（`SIDEBAR_PX + CHAT_LIST_PX`）右边量到右留白。
# 不从 `chat_area_box` 量起，有两个原因：
#   · `chat_area_box` 的左界走 `detect_chat_left`，而**在总列表那一屏它会返回列表内部
#     的 x**——那正好是列表右边那片空白，量出来一个字都没有，判据恒回 False，永远抓不到；
#   · 主窗左边那一列**在两种视图下都可能是列表**，把它算进来会把"主窗正常聊天"也判成
#     列表（它的左列本来就是列表），于是每一拍都落图，等于没判。
#
# **已知的假阳性**（照实记）：只收到过对方消息、自己一句话没说的单聊，也是"左对齐 +
# 右边留白"，会被当成列表——**连底下明明有输入框的那些帧也照判**（合成帧实测：那一档
# `detect_input_top` 回 1250、判据仍回 True）。取证阶段可以接受：命中只多落几张 %TEMP%
# 图，`_LEAK_DUMP_MIN_GAP` 还压着一道（同一屏最多 10 秒一张），复验完就删。16-β 定闸时
# 要用真帧把这一档分开——那正是 header / 行高节奏 / 输入框那条 fill 腿要补的。
LIST_ROW_SAMPLES = 48          # 竖直方向抽多少行判"有没有字 / 字从哪儿起头、到哪儿收"
LIST_TEXT_FRAC = 0.02          # 一行里深色占比过它才算"这一行有字"
LIST_ROW_MIN = 6               # 至少这么多"有字行"才谈得上"列表节奏"
LIST_RIGHT_GAP_FRAC = 0.25     # 行尾离右边至少留这么宽（占块宽）才算"右边是空的"
LIST_TAIL_FRAC = 0.8           # 多少比例的行要满足上面那条留白


def _row_text_span(image, y: int, x0: int, x1: int):
    """这一行：深色（笔画）占比 + **第一个 / 最后一个深色像素的 x**。量不出回 None。

    跟 `_row_dark_frac` 同一个取样口径（`INPUT_ROW_SAMPLES`），只是多要两个边界 x——
    视图判据要的正是"这一行的字从哪儿起头、到哪儿收尾"。一个深色像素都没有时两个 x
    都给 -1（"有字"那条由调用方按占比判，这里不替它决定）。
    """
    width = x1 - x0
    if width <= 0:
        return None
    step = max(1, width // INPUT_ROW_SAMPLES)
    dark = 0
    total = 0
    first = -1
    last = -1
    for x in range(x0, x1, step):
        rgb = _pixel(image, x, y)
        if rgb is None:
            continue
        total += 1
        if (rgb[0] + rgb[1] + rgb[2]) / 3 < TEXT_LUMA_MAX:
            dark += 1
            if first < 0:
                first = x
            last = x
    if not total:
        return None
    return (dark / total, first, last)


def looks_like_chat_list(image, dpr: float = 1.0) -> bool:
    """这一帧**像不像"会话总列表 / 非 active 1:1 聊天"**（拿不准一律回 False）。

    **取证用**：命中只意味着"这一屏值得落一张整帧下来看"，不意味着"这一拍别认字"。
    调用方在**认字之前**调它（跟 OCR 要看的同一块画面），落盘仍旧留给后台线程。

    判据见上面那段注释；量不出尺寸、框太小、行数不够，全都老实回 False——**宁可漏抓，
    不可乱抓**：这条判据将来要长成"该不该跳过这一拍"的闸，宽松的那一侧会在真机上把
    正经聊天挡掉，那比多落几张图严重得多。
    """
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return False
    scale = _scale(dpr)
    # 量"聊天区那条版面线右半边"：左界不取 `detect_chat_left`（见上面那段注释）
    x0 = int((SIDEBAR_PX + CHAT_LIST_PX) * scale)
    y0 = int(TITLE_BAR_PX * scale)
    x1 = width - int(RIGHT_PAD_PX * scale)
    y1 = height - input_cut_px(image, dpr)
    span_w = x1 - x0
    if span_w < CHAT_MIN_SIDE or (y1 - y0) < CHAT_MIN_SIDE:
        return False
    step = max(1, (y1 - y0) // LIST_ROW_SAMPLES)
    lefts: list[int] = []
    rights: list[int] = []
    for y in range(y0, y1, step):
        span = _row_text_span(image, y, x0, x1)
        if span is None or span[0] < LIST_TEXT_FRAC:
            continue                  # 这一行没字（行间空档 / 纯底色）
        lefts.append(span[1])
        rights.append(span[2])
    if len(lefts) < LIST_ROW_MIN:
        return False                  # (b) 有字行太少，谈不上"列表节奏"
    tail = sum(1 for r in rights if (x1 - r) >= span_w * LIST_RIGHT_GAP_FRAC)
    hit = tail >= len(rights) * LIST_TAIL_FRAC
    if hit:
        # 判据是**取证**用的，命中时把几个数一起打出来：将来拿真帧校准 16-β 的阈值，
        # 靠的就是这几行日志（行首离散多少、行尾留了多少、输入框那格量的什么值）——
        # 光有一张图看不出阈值该定哪儿。`detect_input_top` 这一格跟上面 `input_cut_px`
        # 那条"输入区那一刀"的日志是同一个数（同一条腿），搁在这儿是为了**跟行节奏
        # 挨着读**——两种视图下它各回什么，正是 16-β 要分开的那一刀。
        logger.debug(
            "社交漏源取证：这一帧像会话列表（有字行 %d | 行首 x %d..%d | 行尾留白超线 "
            "%d/%d | 量的是 x %d..%d | 输入框上沿 %s）",
            len(lefts), min(lefts), max(lefts), tail, len(rights), x0, x1,
            detect_input_top(image, dpr),
        )
    return hit


# ---------- 像素取样（QImage 与 numpy 数组都认） ----------

def _pixel(image, x: int, y: int):
    """取一个像素的 (r, g, b)；取不到给 None。

    `image` 可能是 `QImage`（`pixelColor`），也可能是 OCR 那条链传下来的 numpy 数组
    （`array[y, x]`）。两条都认，是为了这一位能同时站在界面线程（QImage）与后台线程
    （数组）两边用，而不必为了"取个颜色"把整帧再转一次。
    """
    if image is None:
        return None
    if hasattr(image, "pixelColor"):
        if x < 0 or y < 0 or x >= image.width() or y >= image.height():
            return None
        color = image.pixelColor(x, y)
        return (color.red(), color.green(), color.blue())
    try:
        pixel = image[y, x]
    except Exception:      # noqa: BLE001 —— 形状对不上就当取不到，别把整条线带崩
        return None
    try:
        values = [int(v) for v in pixel[:3]]
    except (TypeError, ValueError, IndexError):
        return None
    if len(values) < 3:
        return None
    return (values[0], values[1], values[2])


def _image_size(image) -> tuple[int, int]:
    if image is None:
        return (0, 0)
    if hasattr(image, "width") and hasattr(image, "height"):
        try:
            return (int(image.width()), int(image.height()))
        except TypeError:
            pass
    try:
        height, width = image.shape[:2]
        return (int(width), int(height))
    except Exception:      # noqa: BLE001
        return (0, 0)


def box_bounds(box) -> tuple[int, int, int, int] | None:
    """OCR 的字框 → (x0, y0, x1, y1)；认不出形状给 None。

    RapidOCR 两代返回的形状都在：四个角点 `[[x,y], ...]`，或直接 `(x0,y0,x1,y1)`。
    """
    if box is None:
        return None
    try:
        points = list(box)
    except TypeError:
        return None
    if not points:
        return None
    if all(isinstance(p, (list, tuple)) and len(p) >= 2 for p in points):
        xs = [float(p[0]) for p in points]
        ys = [float(p[1]) for p in points]
        return (int(min(xs)), int(min(ys)), int(max(xs)), int(max(ys)))
    if len(points) >= 4:
        a, b, c, d = (float(v) for v in points[:4])
        return (int(min(a, c)), int(min(b, d)), int(max(a, c)), int(max(b, d)))
    return None


def bubble_rgb(image, box, pad: int = BUBBLE_PAD):
    """一行字**背后那块气泡**的底色：往字框外扩一圈取背景像素，取中位数。

    为什么要中位数而不是平均：字框里绝大部分是笔画（深色）与气泡底（浅色）两拨，
    平均出来是个谁都不像的中间色；按亮度排序取中位，取到的才是那**多数**的一拨。

    取不到（没有帧、框不合法、一个背景像素都没有）给 None——调用方按"认不出来"处理。
    """
    bounds = box_bounds(box)
    if bounds is None:
        return None
    width, height = _image_size(image)
    if width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = bounds
    x0 -= pad
    y0 -= pad
    x1 += pad
    y1 += pad
    x0 = max(0, x0)
    y0 = max(0, y0)
    x1 = min(width - 1, x1)
    y1 = min(height - 1, y1)
    if x0 > x1 or y0 > y1:
        return None
    samples: list[tuple[int, int, int]] = []
    for y in range(y0, y1 + 1):
        for x in range(x0, x1 + 1):
            rgb = _pixel(image, x, y)
            if rgb is None:
                continue
            if (rgb[0] + rgb[1] + rgb[2]) / 3 < TEXT_LUMA_MAX:
                continue           # 笔画，不是背景
            samples.append(rgb)
    if not samples:
        return None
    samples.sort(key=lambda item: item[0] + item[1] + item[2])
    return samples[len(samples) // 2]


# ---------- 行与行（换行语义） ----------

def tidy_line(text) -> str:
    """一行字内部的空白压平：`"  今天  玩得 开心 "` → `"今天 玩得 开心"`。

    只压**行内**空白，不碰行与行之间的换行——OCR 有时会把同一条消息的一行切碎再吐出来，
    行内的多空格是噪声；但**换行本身是意思**（见下）。
    """
    return " ".join(str(text or "").split())


def tidy_lines(text) -> list[str]:
    """一段多行文字 → 逐行压过空白、**空行丢掉**、顺序不变的列表。

    **换行语义（v2-social-multiline）**：微信里同一个人的一条消息在屏幕上占几行时，
    那几行就是**同一条**，行序 = 屏幕从上到下。老口径一路 `" ".join(text.split())`
    把换行拍成空格，模型拿到的是一长串，再叠上提示词里"只回最近那一条"的老说法，
    真机上的症状就是**只答末半句**（「这么快就放弃啦？」整句被丢掉）。所以这里一律
    按行留住，喂进 context 时用 `"\\n"` 拼回去。
    """
    return [tidy_line(line) for line in str(text or "").splitlines() if tidy_line(line)]


def _joinable(prev, nxt) -> bool:
    """这两行看着是不是**同一个气泡里**的上下两行（折行的续行）。

    两个判据一起要，见 `BUBBLE_JOIN_GAP_RATIO` 那一段：行间空档够小（含认出来的框
    轻微叠在一起的情况，那是同一行被切碎）、且左缘对得上。

    任一方没有框（老桩没给框、OCR 降级）就给 False——**没有依据就不认亲**。
    """
    if prev is None or nxt is None:
        return False
    px0, py0, _px1, py1 = prev
    nx0, ny0, _nx1, _ny1 = nxt
    height = max(1, py1 - py0)
    if abs(ny0 - py1) > height * BUBBLE_JOIN_GAP_RATIO + BUBBLE_JOIN_JITTER_PX:
        return False
    return abs(nx0 - px0) <= max(3, height * BUBBLE_JOIN_DX_RATIO)


def _repair_senders(rows) -> list[tuple]:
    """给认不出说话人的行**认亲**：跟邻行是同一气泡里的上下行，就继承它的说话人。

    `rows` 是 `[(框, 文字, 说话人)]`，按屏幕自上而下。**只补 UNKNOWN，绝不动已有的
    判断**——色卡说得出是谁就是谁。两趟：先向下（续行跟在它首行后面），再向上
    （首行认不出、续行认得出的那种）。补不上的照样留 UNKNOWN，上层按降级处理。

    这一手治的就是"折行后半段在 `incoming` 里消失"：那一行不是没人说，是**底色没认出来**，
    而它明明贴在上面那一行底下、左缘还对齐——这是同一个气泡，说话人跟上一行一样。
    """
    out = [[box, text, sender] for box, text, sender in rows]
    for i in range(1, len(out)):
        if not out[i][2] and out[i - 1][2] and _joinable(out[i - 1][0], out[i][0]):
            out[i][2] = out[i - 1][2]
    for i in range(len(out) - 2, -1, -1):
        if not out[i][2] and out[i + 1][2] and _joinable(out[i][0], out[i + 1][0]):
            out[i][2] = out[i + 1][2]
    return [(box, text, sender) for box, text, sender in out]


# ---------- 一次抓取的结果 ----------

@dataclass
class ChatMessage:
    """屏幕上**一条**消息：谁说的 + 它占的那几行（折行拼回来了）。

    跟 `(sender, text)` 那种逐行的形状差在粒度上：一条消息被气泡宽度折成两行时，
    那是**一条**、两行；`lines` 里则是两行各占一项。喂模型、判"最新那条"用的都是这一层。
    """

    sender: str = SENDER_UNKNOWN
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        """这一条的全部内容，行与行之间用 `\\n`（换行语义不丢）。"""
        return "\n".join(self.lines)


def group_rows(rows) -> list[ChatMessage]:
    """`[(框, 文字, 说话人)]` → 按**条**归组：说话人相同、且上下**贴着**的连续几行是同一条。

    两个断开条件，任中一条就另起一条：**换了说话人**（中间插了别人的话），或者
    **行间空档够大**（`_joinable` 为假 —— 那是另一个气泡，比如对方连着发的两条）。

    为什么断开判据非要有坐标：只看"说话人变没变"的话，"对方连发两条"（先调侃、后评价）
    会被并成一条，接着 `_tail_context` 就把它当成"最新那一条"整段留着——上头那句旧话
    又跟着回来了，正是这一轮要治的。所以**有框才归条**；没框（老桩、测试直接摆 `lines`）
    的降级写在 `ChatRead.messages` 那边，一行算一条，不硬猜。
    """
    out: list[ChatMessage] = []
    prev = None
    for box, text, sender in rows or []:
        if not text:
            continue
        if (out and out[-1].sender == sender and prev is not None
                and _joinable(prev, box)):
            out[-1].lines.append(text)
        else:
            out.append(ChatMessage(sender=sender, lines=[text]))
        prev = box
    return out

@dataclass
class ChatRead:
    """一次社交窗口 OCR 的结果：**按发言人切好的行** + 未切的原文。

    `lines` 每项是 `(sender, text)`，`sender` 取 `SENDER_USER` / `SENDER_OTHER` /
    `SENDER_UNKNOWN`；顺序与 OCR 从上往下读到的一致（下面那份是"最近"的）。
    """

    lines: list[tuple[str, str]] = field(default_factory=list)
    raw: str = ""
    # `classify_lines` 按框归好的**条**（折行拼回来了）。空 = 没有框可比（老桩 / 测试
    # 直接摆 `lines`），这时 `messages` 退化成"一行算一条"，不硬猜哪几行是同一条。
    groups: list[ChatMessage] = field(default_factory=list)
    # 这一帧里被丢掉的**会话列表草稿预览**行数（`is_chat_list_draft`，15-Y）。留着只是给
    # 装配层一个"这一屏漏了会话列表"的证据：>0 就把这一帧落一张临时图，供真机复验
    # （`core.bootstrap._dump_social_leak`）。它不参与任何判断。
    list_draft_rows: int = 0
    # 这一屏是**哪一种页**（v2-social-trigger-window-gate）：装配层在认字那一步判一次
    # （`life.screen.social.classify_chat_page`），值取 `social.PAGE_*`。**必须在 16-β
    # 把会话总列表清成空 read 之前判**——清完这份 read 在文本层就什么都看不出来了。
    # 挂在这儿是让手动与自动那两条回调拿到**同一份**页型，"仅单聊"那道闸才有一处收口。
    # 空串 = 没判过（老桩 / 测试直接摆 `lines`），那边按"单聊"办。
    page_kind: str = ""

    @property
    def known(self) -> bool:
        """有没有至少一行**真分出了**说话人（色卡这一手有没有起作用）。"""
        return any(sender for sender, _text in self.lines)

    @property
    def messages(self) -> list[ChatMessage]:
        """这一屏按**条**看：折行拼回来了的那一条一条。

        `lines` 是"行"这一层（判"画面变没变"、认末尾归属都用它），`messages` 是"条"这一层
        ——**喂模型、取"最新那条"都用这一层**（v2-social-grab-fix）。真抓来的那份是
        `classify_lines` 按框归好的；没有框可比时退化成一行一条：**宁可把一条看成几条，
        也别把几条看成一条**——看成几条最多是多带一点上下文，看成一条会把上一条旧话
        当成"最新那条"整段留着。
        """
        if self.groups:
            return self.groups
        return [ChatMessage(sender=sender, lines=[text])
                for sender, text in self.lines if text]

    @property
    def latest_other(self) -> ChatMessage | None:
        """最后一条**对方**的消息，折行拼全了的那一条；一条都没有给 None。

        这是"取对话最新一条对方消息"这句话的落点：不是取**首行**（那只是折行的第一段），
        也不是取**整屏**（顶上那些旧消息会把模型带跑）。
        """
        for message in reversed(self.messages):
            if message.sender == SENDER_OTHER and message.text:
                return message
        return None

    @property
    def incoming(self) -> str:
        """对方说的那几句，**一行一句**（`\n` 分隔），按屏幕从上到下的原顺序。

        **换行是有意思的**（v2-social-multiline），不许拍平成空格：同一个人的一条消息
        在屏幕上占几行时，那几行是**同一条**；拍平之后模型看到的是一长串，再叠上提示词
        里"只回最近那一条"的老口径，就成了"只答末半句"——真机上漏掉的那句试探正是这么
        丢的。行与行之间保留 `\n`，模型才分得清"他连着说了两件事"。
        """
        return "\n".join(text for sender, text in self.lines if sender == SENDER_OTHER)

    @property
    def outgoing(self) -> str:
        """自己发的那几句，一行一句（同上，保留换行）。"""
        return "\n".join(text for sender, text in self.lines if sender == SENDER_USER)

    @property
    def visible(self) -> str:
        """这一屏**看得见的全部字**，两边都算。

        它跟 `context` 是两件事，别混：判"画面是不是来了新内容"要用**全部**——用户
        自己发的那条也是新内容，只盯着对方那半边会把"我刚发出去"这整档漏掉；而喂给
        模型的那份（`context`）才只要对方那半边。

        这里同样按行拼（换行语义跟 `incoming` 一致）。**判"变没变"那条腿不受影响**：
        `social._normalize_listen` 本来就把两边的空白都压成单个空格再比。
        """
        return self.raw or "\n".join(text for _sender, text in self.lines)

    @property
    def context(self) -> str:
        """喂给解析 / 起草的"对方消息列表"。

        色卡认出过说话人（`known`）就**只给对方的**——自己发的那半边一个字都不进去，
        这也正是这一轮要治的"拿自己发的内容当对方原话"。一行都没分出来（换了个客户端、
        深色主题、测试里塞进来的纯字符串）就退回整段 `visible`：那是**降级**，不是
        "对方说的"，上层照旧按"认不准"办（末尾归属那条会退回空闲时机那套猜法）。
        """
        if self.known:
            return self.incoming
        return self.visible

    @property
    def tail_sender(self) -> str:
        """最后一行**分得出说话人**的那条是谁发的；一条都分不出给 UNKNOWN。

        这是"末尾归属"那条判据的新腿：末尾写着绿气泡 = 用户自己刚发的，白气泡 =
        对方刚回的。**认不出就不给答案**，上层退回空闲时机那套。
        """
        for sender, text in reversed(self.lines):
            if text and sender:
                return sender
        return SENDER_UNKNOWN


def as_chat_read(value) -> ChatRead:
    """把注入进来的读数统一成 `ChatRead`。

    `_read_chat` 那只手老口径回的是**一行字符串**（v2-social-fewshot 起），这一轮起
    装配层回的是切好发言人的 `ChatRead`。两条都收：字符串当"整段都是没分类的原文"，
    于是老测试里那些塞字符串的桩一个字都不用改。
    """
    if isinstance(value, ChatRead):
        return value
    body = "\n".join(tidy_lines(value))
    if not body:
        return ChatRead()
    return ChatRead(lines=[(SENDER_UNKNOWN, body)], raw=body)


def parse_ocr_lines(result) -> list[tuple[object, str]]:
    """RapidOCR 的返回 → `[(字框, 文字), ...]`，保持自上而下的原顺序。

    跟 `monitor._extract_text` 认同一批形状（旧版 `(list, elapsed)` 元组 / 新版结果
    对象上的 `.txts`），只是那边只要字、这边还要框——没有框就没法知道一行字背后是
    哪个颜色，也就分不出谁说的。
    """
    if result is None:
        return []
    if isinstance(result, tuple) and len(result) == 2 and isinstance(
        result[0], (list, type(None))
    ):
        result = result[0]
    if result is None:
        return []
    texts = getattr(result, "txts", None)
    if texts is not None:
        boxes = getattr(result, "boxes", None)
        if boxes is None:
            boxes = [None] * len(texts)
        return [(box, str(text)) for box, text in zip(boxes, texts)]
    out: list[tuple[object, str]] = []
    for line in result:
        if isinstance(line, (list, tuple)) and len(line) >= 2:
            out.append((line[0], str(line[1])))
    return out


# 会话列表里"这条会话还压着没发出去的草稿"时，预览行长这样：`[草稿] 你敲的那半句`。
# 它是**列表那一列的东西**，不是聊天区里谁说的话（真机 2026-10-08 09:58 那帧的
# `[草稿 行，那我就等着看。别到时候光打雷不下雨，我可记着呢。` 就是这么被当成"对方原话"
# 的，v2-social-material-empty-fix 15-Y）。客户端的标记是写死的，所以按**字面**认，
# 跟时间戳 / 输入框占位提示同一个态度（用户拍板过："按已知文本特征过滤，不靠颜色猜"）。
CHAT_LIST_DRAFT_PREFIX = "[草稿"


def is_chat_list_draft(text: object) -> bool:
    """这一行是不是**会话列表的草稿预览**（`[草稿] …`）。空白先压平再比前缀。

    只认**行首**：夹在句子中间的 `[草稿` 是人话（"他把 [草稿 两个字读出来了"），不该吞。
    """
    return tidy_line(text).startswith(CHAT_LIST_DRAFT_PREFIX)


def classify_lines(image, lines) -> ChatRead:
    """把 OCR 出来的 `(框, 字)` 逐行按气泡底色分给谁，回一份 `ChatRead`。

    三步：**① 逐行取气泡底色定说话人 → ② 给认不出的行认亲（`_repair_senders`：
    跟邻行是同一气泡里的上下行就继承它的说话人）→ ③ 逐行收进 `ChatRead.lines`。**

    空白行直接丢掉；分不出颜色、又认不出亲的那行照样**留着**（文字还是要用的），只是记成
    UNKNOWN——`incoming` 不收它，但 `visible` 与降级那条路都还在，**不静默丢行**。

    **唯一的例外是会话列表的草稿预览**（`is_chat_list_draft`，15-Y）：那一行是**列表**里的
    东西，既不是对方说的、也不是已发出去的自己那条，整行丢掉并计数（`ChatRead.list_draft_rows`，
    装配层拿这个数当"这一帧漏了列表"的证据去落图复验）。它也是**这里唯一静默丢掉的一行**——
    理由：它的归属是确定的（就是列表预览），不是"认不出"。
    """
    rows: list[tuple] = []
    drafts = 0
    for box, text in lines or []:
        body = tidy_line(text)
        if not body:
            continue
        if is_chat_list_draft(body):
            drafts += 1
            continue
        rows.append((box_bounds(box), body,
                     sender_from_bubble(bubble_rgb(image, box))))
    rows = _repair_senders(rows)
    out: list[tuple[str, str]] = [(sender, body) for _box, body, sender in rows]
    return ChatRead(
        lines=out,
        raw="\n".join(text for _sender, text in out),
        groups=group_rows(rows),
        list_draft_rows=drafts,
    )


__all__ = [
    "BG_BAND", "BG_COL_FRAC", "BG_RUN_MIN_PX", "BG_RUN_PX", "BG_SAMPLE", "BG_TOL",
    "BUBBLE_JOIN_DX_RATIO",
    "BUBBLE_JOIN_GAP_RATIO", "BUBBLE_JOIN_JITTER_PX", "BUBBLE_PAD",
    "CHAT_LIST_DRAFT_PREFIX", "CHAT_LIST_PX", "CHAT_MIN_SIDE", "ChatMessage", "ChatRead",
    "INPUT_ANCHOR_TOL", "INPUT_BAND_FRAC", "INPUT_BOTTOM_MIN_FRAC", "INPUT_BOX_PX",
    "INPUT_EDGE_FRAC", "INPUT_EDGE_MAX_PX", "INPUT_FILL_TOL", "INPUT_SCAN_MAX_FRAC",
    "INPUT_TOP_FRAC",
    "LEFT_CEIL", "LIST_RIGHT_GAP_FRAC", "LIST_ROW_MIN",
    "LIST_ROW_SAMPLES", "LIST_TAIL_FRAC", "LIST_TEXT_FRAC",
    "OTHER_MAX_SPREAD", "OTHER_MIN_CHANNEL", "PET_CARD_TITLE_FMT", "PET_CHAT_TITLE",
    "RIGHT_PAD_PX", "SAFE_FLOOR",
    "SELF_BUBBLE_RGB", "SELF_GREEN_MARGIN", "SELF_GREEN_MIN", "SELF_GREEN_RGB",
    "SELF_LUMA_MIN", "SENDER_OTHER", "SENDER_UNKNOWN", "SENDER_USER", "SIDEBAR_PX",
    "TEXT_LUMA_MAX", "TITLE_BAR_PX", "as_chat_read", "bottom_cut_px", "box_bounds",
    "bubble_rgb",
    "chat_area_box", "chat_client_rect", "classify_lines", "cut_edges_box",
    "detect_chat_left", "detect_input_top", "has_chat_list_column", "input_cut_px",
    "group_rows", "is_chat_list_draft", "is_other_bubble", "is_pet_window", "is_self_bubble",
    "looks_like_chat_list",
    "parse_ocr_lines",
    "sender_from_bubble", "tidy_line", "tidy_lines",
]
