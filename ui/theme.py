"""界面配色与样式表。颜色集中在这里，改风格只改这一个文件。"""

from __future__ import annotations

from string import Template

from PySide6.QtGui import QFont

BG = "#fdf7ee"          # 面板底色（暖米）
CARD = "#ffffff"
ACCENT = "#e79a4d"      # 主色（暖橘）
ACCENT_DARK = "#cf8339"
TEXT = "#4a3a2f"
TEXT_MUTED = "#8c7a6b"
BORDER = "#eadfd2"
BUBBLE_BG = "#fffdf7"
BUBBLE_BORDER = "#e8d8c0"

# ---------- 卡片里的字（指令 25-F：精致简洁的高级感） ----------
# 用思源宋体（Noto Serif SC）：系统自带、字重从极细到特粗齐全，比默认的微软雅黑细腻，
# 衬线的书卷气也正"高级"。字号小的时候字重不能拉满（糊成一团），也不能太细（笔画会断），
# 卡名取 Medium、描述取 Regular 正好；中文在 10~17px 这个尺寸上加一点字距会明显透气。
CARD_FONT = "Noto Serif SC"     # 职能卡（轮播里的卡位 + 选择页里的卡）的字体族
CARD_TITLE_WEIGHT = 500         # 卡名字重：Medium
CARD_TITLE_SPACING = 6.0        # 卡名字距（百分比，正数 = 拉开）
CARD_SUB_WEIGHT = 400           # 描述字重：Regular（浅一号，但不至于断笔）
CARD_SUB_SPACING = 5.0          # 描述字距（百分比）


def card_font(size: int, weight: int, spacing: float, pixel: bool = False) -> QFont:
    """卡片里的字：字体族 + 字重 + 字距。

    size 默认按磅算（轮播里自己画字），pixel=True 时按像素算（样式表那套尺寸体系）。
    字重给 100~900 的数值，Qt 在思源这套字里挑对应的那一档。
    """
    font = QFont(CARD_FONT)
    if pixel:
        font.setPixelSize(size)
    else:
        font.setPointSize(size)
    font.setWeight(QFont.Weight(weight))
    if spacing:
        font.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100.0 + spacing)
    return font

PANEL_QSS = Template(
    """
QWidget#PanelWindow, QDialog#SettingsDialog {
    background-color: $bg;
}
QLabel {
    color: $text;
    background: transparent;
}
QLabel#TitleLabel { font-size: 16px; font-weight: 600; }
QLabel#MutedLabel { color: $muted; font-size: 12px; }
QLabel#WhyLabel { color: $text; font-size: 15px; font-weight: 700; }
QLabel#ModuleHint { color: $muted; font-size: 11px; }
QPushButton {
    background-color: $card;
    border: 1px solid $border;
    border-radius: 10px;
    padding: 7px 12px;
    color: $text;
}
QPushButton:hover { background-color: #fdf1e0; }
QPushButton#PrimaryButton {
    background-color: $accent;
    border: none;
    color: #ffffff;
    font-weight: 600;
}
QPushButton#PrimaryButton:hover { background-color: $accent_dark; }
QPushButton#EntryButton { font-size: 15px; padding: 22px 16px; text-align: left; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox, QTimeEdit {
    background: $card;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 6px 8px;
    color: $text;
}
QListWidget, QTextBrowser {
    background: $card;
    border: 1px solid $border;
    border-radius: 10px;
    color: $text;
}
QTabWidget::pane {
    border: 1px solid $border;
    border-radius: 10px;
    background: $card;
}
QTabBar::tab {
    padding: 6px 14px;
    border-radius: 8px;
    color: $text;
}
QTabBar::tab:selected { background: $accent; color: #ffffff; }
QCheckBox { color: $text; }
QProgressBar {
    background: $bg;
    border: 1px solid $border;
    border-radius: 7px;
    min-height: 13px;
    max-height: 13px;
    text-align: center;
    color: $muted;
    font-size: 10px;
}
QProgressBar::chunk { background: $accent; border-radius: 6px; }
QScrollArea { background: transparent; border: none; }
QWidget#DeptSlotBody { background: transparent; }
QFrame#DeptSlotCard {
    background: $card;
    border: 1px solid $border;
    border-radius: 12px;
}
QFrame#DeptSlotCard:hover { background: #fdf1e0; }
QFrame#DeptEmptySlot {
    background: $card;
    border: 2px dashed #d8c7b0;
    border-radius: 12px;
}
QFrame#DeptEmptySlot:hover { background: #fdf1e0; }
QLabel#DeptSlotBrief { color: $muted; }
QLabel#DeptSlotEmptyMark { color: $muted; font-size: 34px; }
QPushButton#OverlayClose {
    background: transparent;
    border: none;
    color: $muted;
    font-size: 18px;
    padding: 0;
}
QPushButton#OverlayClose:hover { color: $text; }
"""
).substitute(
    bg=BG,
    card=CARD,
    accent=ACCENT,
    accent_dark=ACCENT_DARK,
    text=TEXT,
    muted=TEXT_MUTED,
    border=BORDER,
)

BUBBLE_QSS = Template(
    """
QWidget#SpeechBubble { background: transparent; }
QLabel#BubbleText {
    color: $text;
    font-size: 13px;
    background: transparent;
}
QPushButton#BubbleAsk {
    background: #fffdf7;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 3px 10px;
    color: $text;
    font-size: 12px;
}
QPushButton#BubbleAsk:hover { border-color: $accent; color: $accent; }
"""
).substitute(text=TEXT, border=BORDER, accent=ACCENT)

# 宠物旁的聊天气泡框（指令 7 修正一）：整体透明，内部控件自己上色
CHAT_QSS = Template(
    """
QWidget#ChatBubble { background: transparent; }
QLabel#ChatTitle { color: $muted; font-size: 12px; background: transparent; }
QListWidget#ChatHistory {
    background: transparent;
    border: none;
    color: $text;
    font-size: 14px;
}
QLineEdit#ChatInput {
    background: #fffdf7;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 7px 10px;
    color: $text;
    font-size: 14px;
}
QPushButton#ChatSend {
    background-color: $accent;
    border: none;
    border-radius: 8px;
    padding: 7px 14px;
    color: #ffffff;
    font-size: 14px;
}
QPushButton#ChatSend:disabled { background-color: #e3d5c4; }
/* 按住说话（C 多模态）：按下时变强调色，让人一眼看出"正在听" */
QPushButton#ChatMic {
    background: #fffdf7;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 7px 12px;
    color: $text;
    font-size: 13px;
}
QPushButton#ChatMic:pressed { background-color: $accent; color: #ffffff; border-color: $accent; }
QPushButton#ChatMic:disabled { color: $muted; }
QPushButton#ChatOffer {
    background: #fffdf7;
    border: 1px solid $border;
    border-radius: 8px;
    padding: 5px 12px;
    color: $text;
    font-size: 13px;
}
QPushButton#ChatOffer:hover { border-color: $accent; color: $accent; }
QPushButton#ChatClose {
    background: transparent;
    border: none;
    color: $muted;
    font-size: 14px;
    padding: 0 4px;
}
QPushButton#ChatClose:hover { color: $text; }
"""
).substitute(text=TEXT, muted=TEXT_MUTED, border=BORDER, accent=ACCENT)

# ---------- 工作模块：流程视图配色（指令 16） ----------
# 三种连线各一个颜色，图例在面板上对着看：蓝=主线、红=NG 回流、橙虚线=并行分支
FLOW_MAIN = "#2f6fb0"
FLOW_NG = "#c9483c"
FLOW_PARALLEL = "#e08a2a"
FLOW_NODE_BORDER = "#e0d3c2"
FLOW_NODE_TEXT = "#4a3a2f"
FLOW_DEPT_TEXT = "#8c7a6b"
FLOW_START = "#7bbd8f"      # 开始 / 结束节点：柔绿描边
FLOW_DECISION = "#cf8339"   # 判定节点：暖橘描边

# 工作模块轮播卡（指令 18）：功能卡用流程主线的蓝，占位模板卡用暖灰，一眼分得出
WORK_TEMPLATE = "#b9a893"

# ---------- 生活卡牌：主题色（指令 17 轮播卡面的色块标识） ----------
# 9 大类（指令 25-A：建卡时的分类标签）各一个色，加上玄学主题的色；
# 老数据里可能还有"情感陪护"这类旧主题，也留着色，收进卡册的卡面才不至于没颜色。
CARD_THEME_COLORS = {
    "心理安抚": "#7ba7d0",
    "餐饮": "#e08a2a",
    "穿搭": "#d07ba7",
    "出行": "#5aa89a",
    "健身": "#7bbd8f",
    "情感": "#d07b7b",
    "作息": "#7a8fd0",
    "消费": "#d0a95a",
    "娱乐": "#c58ad0",
    "命理": "#9b8ad0",
    "情感陪护": "#d07b7b",
    "姻缘": "#c58ad0",
    "事业": "#6f86d6",
    "财运": "#d0a95a",
    "学业": "#5a9ad0",
    "运势": "#b08ad0",
}


def card_theme_color(theme: str) -> str:
    """取主题色；没登记过的主题用主色兜底。"""
    return CARD_THEME_COLORS.get(theme, ACCENT)


# 生活模块里「游戏陪玩」那张入口卡的色（指令 22）：番茄钟那张借下面的专注色
PLAYMATE_ENTRY_COLOR = "#6f86d6"

# 生活模块里「玄机先生」那张入口卡的色（指令 24）：借玄学主题的紫
METAPHYSICS_ENTRY_COLOR = "#b08ad0"


# ---------- 番茄钟配色（专注=暖橙，短休=柔绿，长休=蓝紫，一眼能分清） ----------

POMODORO_FOCUS = ACCENT        # 专注：暖橙
POMODORO_BREAK = "#7bbd8f"     # 短休：柔绿
POMODORO_LONG_BREAK = "#6f86d6"  # 长休：蓝紫（和短休明显区分开）
POMODORO_IDLE = "#c9bbae"      # 空闲：灰
POMODORO_TRACK = "#efe4d6"     # 环形进度条的底槽

POMODORO_PHASE_COLORS = {
    "focus": POMODORO_FOCUS,
    "short_break": POMODORO_BREAK,
    "long_break": POMODORO_LONG_BREAK,
    "idle": POMODORO_IDLE,
}
