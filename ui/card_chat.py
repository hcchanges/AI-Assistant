"""
卡内小助手的小窗（v2-card-tools-A）：嵌在生活卡 / 工作卡底部的那一块聊天区。

**它就是全局那个聊天气泡搬了一间房进来**：同一位画气泡的画师（`_BubbleDelegate`）、
同一张样式表（`ui.theme.CHAT_QSS`）、同一批消息身份（我 / 它 / 提示），
所以看起来跟「AI助理」是一套东西——但它**不是一个窗**：

- 它不是 `ChatBubble`（那个是独立无边框窗口，跟着宠物跑），这是卡面上的一块控件；
- 它只认**一个卡号**：`bind()` 把它绑到某张卡上，之后 `bus.card_chat_reply` 里
  卡号对不上的回话，它一个字都不显示——隔壁卡窗也是同样的道理。

这一层只管**画**：说话往哪去、记在哪儿、跟别处串不串，全在 `life/cards/chat.py`。
组件自己不认识模型、不认识数据库，也不知道"隔离"这回事。
"""

from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.chat_bubble import (
    HISTORY_PAD_BOTTOM,
    ITEM_GAP,
    MAX_ITEMS,
    ROLE_SPEAKER,
    SPEAKER_NOTE,
    SPEAKER_PET,
    SPEAKER_USER,
    _BubbleDelegate,
)
from ui.smooth_scroll import enable_smooth_scroll
from ui.theme import CHAT_QSS

PANEL_HEIGHT = 214      # 卡底这块的高（标题 + 记录区 + 输入行）：矮了聊两句就得滚
HINT = "只在这张卡里说话，不跟别处串"

# 这块是嵌在卡面上的控件（不是独立窗口），得自己把自己圈出来：
# 淡底 + 描边，跟卡上别的区块分开，同时沿用全局聊天气泡那套字体与配色。
_CARD_CHAT_QSS = (
    "QWidget#CardChat {"
    "  background-color: #fdf7ee;"
    "  border: 1px solid #eadfd2;"
    "  border-radius: 10px;"
    "}"
)


class CardChatPanel(QWidget):
    """卡底的小助手窗。**同一时刻只绑一张卡**（卡面本来就一次只开一张）。"""

    send_requested = Signal(str, str)   # (卡号, 用户说的话)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CardChat")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(CHAT_QSS + _CARD_CHAT_QSS)
        self.setFixedHeight(PANEL_HEIGHT)

        self._namespace = ""
        self._busy = False

        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 8, 10, 10)
        layout.setSpacing(6)

        head = QHBoxLayout()
        self._title = QLabel("这张卡的小助手", self)
        self._title.setObjectName("ChatTitle")
        head.addWidget(self._title)
        head.addStretch(1)
        self._hint = QLabel(HINT, self)
        self._hint.setObjectName("ChatTitle")
        head.addWidget(self._hint)
        layout.addLayout(head)

        self._history = QListWidget(self)
        self._history.setObjectName("ChatHistory")
        self._history.setWordWrap(True)
        self._history.setSelectionMode(QListWidget.NoSelection)
        self._history.setFocusPolicy(Qt.NoFocus)
        # 跟全局气泡同一个画师：我靠右主色、它靠左米色、提示居中灰字
        self._history.setItemDelegate(_BubbleDelegate(self._history))
        self._history.setSpacing(ITEM_GAP)
        self._history.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 记录区底下留一口气，跟「AI助理」那个气泡一个口径（v2-social-ui-batch 批次9）：
        # 滚到底时最后一条气泡不贴着输入行，末行不会被切掉半个字。
        self._history.setViewportMargins(0, 0, 0, HISTORY_PAD_BOTTOM)
        # 滚起来也是同一套（批次10）：跟那个气泡共用一处实现，别两处各写一遍。
        self._smoother = enable_smooth_scroll(self._history)
        layout.addWidget(self._history, 1)

        row = QHBoxLayout()
        row.setSpacing(6)
        self._input = QLineEdit(self)
        self._input.setObjectName("ChatInput")
        self._input.setPlaceholderText("说说这张卡的事…")
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, 1)
        self._send_button = QPushButton("发送", self)
        self._send_button.setObjectName("ChatSend")
        self._send_button.setCursor(Qt.PointingHandCursor)
        self._send_button.clicked.connect(self._send)
        row.addWidget(self._send_button)
        layout.addLayout(row)

    # ---------- 对外 ----------

    @property
    def namespace(self) -> str:
        """现在绑的是哪张卡（没绑过就是空串）。"""
        return self._namespace

    @property
    def busy(self) -> bool:
        return self._busy

    def bind(self, namespace: str, title: str, history: list[dict[str, str]]) -> None:
        """绑到这张卡上：换标题、把这张卡聊过的话铺回记录区。

        换卡时记录区是**整个清掉再铺**的——上一张卡的话一句都不留在这儿，
        免得出现"翻到减脂卡还看得见出行卡那句话"。
        """
        self._namespace = namespace
        self._title.setText(f"{title} · 小助手" if title else "这张卡的小助手")
        self._history.clear()
        for item in history:
            self._append(str(item.get("text") or ""), item.get("who") or SPEAKER_PET)
        self.set_busy(False)
        self._input.clear()

    def fill(self, text: str) -> None:
        """把这句常说的话填进输入框（卡上"快捷指令"点一下就是这个）：**不发送**。

        填完把光标放到末尾、输入框拿到焦点——用户接着改两个字就能发，
        这比"替你按了发送"更接近"快捷"两个字的意思。
        """
        if self._busy or not self._namespace:
            return
        self._input.setText(text)
        self._input.setFocus()
        self._input.setCursorPosition(len(text))

    def submit_text(self, text: str) -> None:
        """把一句话当成**用户刚说的**发出去（卡上"按钮 / 自动化"点一下就是这个）。

        走的还是 `_send` 那条路：先上屏、再置忙、再发信号——所以从卡上点出来的这一句
        跟手打的那一句在记录里、在界面上完全一样，没有"另一条暗路"。
        """
        self._input.setText(text)
        self._send()

    def append_user(self, text: str) -> None:
        self._append(text, SPEAKER_USER)

    def append_pet(self, text: str) -> None:
        self._append(text, SPEAKER_PET)
        self.set_busy(False)

    def append_note(self, text: str) -> None:
        """一句交代（居中灰字）：不是宠物说的那句话，别当成回话。"""
        self._append(text, SPEAKER_NOTE)
        self.set_busy(False)

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)
        self._send_button.setText("在想…" if busy else "发送")

    # ---------- 内部 ----------

    def _send(self) -> None:
        text = self._input.text().strip()
        if not text or self._busy or not self._namespace:
            return
        self._input.clear()
        self.append_user(text)          # 自己这句先上屏，别等回话才出现
        self.set_busy(True)
        self.send_requested.emit(self._namespace, text)

    def _append(self, text: str, speaker: str) -> None:
        if not text:
            return
        item = QListWidgetItem(text, self._history)
        item.setData(ROLE_SPEAKER, speaker)
        # 只留最近这些条，跟全局气泡一个口径：越聊越占内存这件事两边都不干
        while self._history.count() > MAX_ITEMS:
            self._history.takeItem(0)
        self._history.scrollToBottom()


__all__ = ["HINT", "PANEL_HEIGHT", "CardChatPanel"]
