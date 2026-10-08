"""
宠物旁的聊天气泡框（对应指令 7 修正一；指令 26-A 微信化）。

点一下猫咪就在它旁边展开这个小气泡，直接在里面说话，回复也出现在这里，
不再弹独立面板窗口，视线始终留在宠物身上；完整历史仍然会在生活面板里存一份。

消息按微信那样排（指令 26-A）：我说的靠右、主色气泡、白字；它说的靠左、米色气泡、深字；
提示（例如"这张卡收起来了"）居中、灰字、没有气泡。气泡宽度按对话框的 78% 封顶，
行距 1.4、上下留白 10px —— 太挤的话，安抚的话读起来像在赶时间。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QGuiApplication,
    QPainter,
    QPainterPath,
    QPen,
    QTextBlockFormat,
    QTextCharFormat,
    QTextCursor,
    QTextDocument,
)
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QStyledItemDelegate,
    QStyleOptionViewItem,
    QVBoxLayout,
    QWidget,
)

from pet.persona import split_kaomoji
from ui.smooth_scroll import enable_smooth_scroll
from ui.theme import ACCENT, BUBBLE_BG, BUBBLE_BORDER, CHAT_QSS, TEXT, TEXT_MUTED

BUBBLE_W = 300
# 助手窗的标题（v2-pet-persona-rename）：固定「AI助理」，不再跟宠物名走。
# `life/screen/social_window.py` 认「这扇窗是助手窗」时也读这个串（那边另抄了一份常量）。
CHAT_TITLE = "AI助理"
MIN_HISTORY_H = 140     # 聊天记录区最小高度：还没聊几句时就长这样
MAX_HEIGHT_RATIO = 0.6  # 气泡最高到屏幕可用高度的六成，再多就改为在里面滚动，免得盖住整屏
# 记录区**底部**留的那口气（v2-social-ui-batch 批次9）。
#
# 真机图 2 坐实：滚到底时最底下那一行（「把'不亏'这句收进自己」那一行）贴着记录区下缘，
# 下半个字被切掉——记录区跟下面的输入行只隔一个 layout spacing，末行等于顶在边线上。
#
# 两件事一起修才对：①**底部留白**（就是这一格，靠 `setViewportMargins` 把视口往上收，
# 滚到底时最后一条气泡下面自然空出这么多）；②`_append` 里**先长高再滚**（见那儿）。
# 只做①不做②：长高的那一拍滚动位置会停在旧位置，末行照样看不见。
HISTORY_PAD_BOTTOM = 12
PADDING = 10
TAIL_HEIGHT = 12
CORNER = 12
SCREEN_MARGIN = 6
MAX_ITEMS = 40          # 气泡里只留最近这么多条，避免越说越占内存

# 消息的排布（指令 26-A）：字号在 ui/theme.py 的样式表里是 14px，这儿管宽度、行距与留白
ITEM_GAP = 10           # 每条消息之间留的空隙（原来 4，太挤）
LINE_HEIGHT = 140       # 行距（百分比，140 = 1.4 倍）
BUBBLE_PAD_X = 12       # 气泡内左右留白
BUBBLE_PAD_Y = 10       # 气泡内上下留白（要求 ≥10px）
BUBBLE_RADIUS = 12      # 气泡圆角
MAX_BUBBLE_RATIO = 0.78 # 气泡最宽占记录区的比例

# 这条消息是谁说的：我 / 它 / 系统提示
ROLE_SPEAKER = Qt.UserRole + 1
SPEAKER_USER = "user"
SPEAKER_PET = "pet"
SPEAKER_NOTE = "note"

# 这一条属于**上一轮帮聊那一则**（v2-social-chat-ui-copy 批次11 需求5）：正文与解析都打
# 这个标记，好让"记录区只留最新一则"那一步按它把上一轮整条摘掉；用户与宠物的普通聊天
# 记录一条都不动。**认这一格 item data，不认文本**——解析那句里也可能出现「」，
# 按文本挑会误伤真聊天。
ROLE_SOCIAL_DRAFT = Qt.UserRole + 2

# 帮聊那一则套的那个框（v2-social-chat-ui-fix2 提交F）：**外粗内细**两层框。
#
# 真机上用户要一眼分清"哪句是能直接粘走的、哪句是给我看的门道"——所以正文单独关进内层
# 细框，解析留在外层框里、正文下方当注解，复制按钮压在外层框的右下角。两层都用 QFrame
# 的边框画（不加 pixmap、不加新依赖），颜色借现成的 `BUBBLE_BORDER`。
CARD_BORDER_W = 3       # 外层框：粗边（3px），一眼能看出"这一整块是一则"
CARD_PAD = 8            # 外层框内留白
CARD_RADIUS = 12        # 外层框圆角（跟普通气泡同一档）
DRAFT_BORDER_W = 1      # 内层框：细边（1px），装的是能直接粘走的正文
DRAFT_PAD = 6           # 内层框内留白
CARD_GAP = 6            # 正文框 / 解析 / 按钮行之间的间距


class _BubbleDelegate(QStyledItemDelegate):
    """一条消息一个圆角气泡（指令 26-A）：我说的靠右、它说的靠左。

    宽度按 78% 封顶、长句在里面折行；行距用 QTextDocument 撑起来——
    QListWidget 那种纯文本没有"行距"这个说法。
    """

    def sizeHint(self, option: QStyleOptionViewItem, index) -> QSize:  # noqa: N802
        # 帮聊那一则是**一整个复合控件**（v2-social-chat-ui-fix2 提交F）：正文框 + 解析 +
        # 右下角那个「复制回复」都是真控件，高度由控件自己量好后写进 `SizeHintRole`。
        # delegate 不替它算——算两套的话，"量出来的高"和"控件实际占的高"迟早对不上，
        # 那一则就会被切掉底边（跟批次9 记的那个坑同源）。
        if index.data(ROLE_SOCIAL_DRAFT):
            hint = index.data(Qt.SizeHintRole)
            return hint if isinstance(hint, QSize) else QSize(BUBBLE_W, 0)
        speaker = index.data(ROLE_SPEAKER)
        if speaker == SPEAKER_NOTE:
            return super().sizeHint(option, index)
        text = str(index.data(Qt.DisplayRole) or "")
        doc = self._document(
            option, text, self._text_width(option), self._color_for(speaker), self._dim_for(speaker)
        )
        return QSize(
            int(doc.idealWidth()) + BUBBLE_PAD_X * 2,
            int(doc.size().height()) + BUBBLE_PAD_Y * 2,
        )

    def paint(self, painter: QPainter, option: QStyleOptionViewItem, index) -> None:  # noqa: N802
        # 帮聊那一则整个交给它自己的控件去画（上面 `sizeHint` 那段说了为什么）：delegate
        # 一个字都不画。不在这儿挡住的话，控件背后会透出一层"画在条目上"的同样文字。
        if index.data(ROLE_SOCIAL_DRAFT):
            return
        speaker = index.data(ROLE_SPEAKER)
        if speaker == SPEAKER_NOTE:
            super().paint(painter, option, index)
            return
        text = str(index.data(Qt.DisplayRole) or "")
        rect = option.rect
        doc = self._document(
            option, text, self._text_width(option), self._color_for(speaker), self._dim_for(speaker)
        )
        box = QRectF(
            rect.right() - (doc.idealWidth() + BUBBLE_PAD_X * 2)
            if speaker == SPEAKER_USER
            else rect.left(),
            rect.top(),
            doc.idealWidth() + BUBBLE_PAD_X * 2,
            doc.size().height() + BUBBLE_PAD_Y * 2,
        )

        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        path = QPainterPath()
        path.addRoundedRect(box, BUBBLE_RADIUS, BUBBLE_RADIUS)
        mine = speaker == SPEAKER_USER
        painter.fillPath(path, QColor(ACCENT if mine else BUBBLE_BG))
        if not mine:                      # 它的气泡描一道细边，跟底色分开
            painter.setPen(QPen(QColor(BUBBLE_BORDER), 1.0))
            painter.drawPath(path)
        painter.translate(box.left() + BUBBLE_PAD_X, box.top() + BUBBLE_PAD_Y)
        doc.drawContents(painter)
        painter.restore()

    @staticmethod
    def _color_for(speaker: object) -> str:
        return "#ffffff" if speaker == SPEAKER_USER else TEXT

    @staticmethod
    def _dim_for(speaker: object) -> bool:
        """颜文字淡化的开关：**只有它说的话**才淡（v2-kaomoji-dim）。

        我说的话是主色底白字，再淡一档就糊了；系统提示整条本来就是灰的，不必分两档。
        `sizeHint` 与 `paint` 都从这里取，两边算出来的文档必须一模一样——不然
        "量出来的高"和"画出来的高"会差一截，气泡就会叠字或者露白。
        """
        return speaker == SPEAKER_PET

    @staticmethod
    def _text_width(option: QStyleOptionViewItem) -> float:
        """气泡里文字最多能有多宽：记录区宽的 78% 再减掉左右留白。"""
        view = option.widget
        width = view.viewport().width() if view is not None else option.rect.width()
        return max(60.0, width * MAX_BUBBLE_RATIO - BUBBLE_PAD_X * 2)

    @staticmethod
    def _document(
        option: QStyleOptionViewItem,
        text: str,
        width: float,
        color: str,
        dim: bool = False,
    ) -> QTextDocument:
        doc = QTextDocument()
        font = option.font
        doc.setDefaultFont(font)
        block = QTextBlockFormat()
        block.setLineHeight(float(LINE_HEIGHT), QTextBlockFormat.ProportionalHeight.value)
        char = QTextCharFormat()
        char.setForeground(QColor(color))
        char.setFont(font)
        # 颜文字淡一档（v2-kaomoji-dim）：判据跟语音那条线共用 persona.split_kaomoji，
        # 剥掉的和淡化的永远是同一批字符。`dim` 为假时整段一次塞进去，跟改造前逐字节相同。
        dim_char = QTextCharFormat(char)
        dim_char.setForeground(QColor(TEXT_MUTED))
        cursor = QTextCursor(doc)
        cursor.setBlockFormat(block)
        for part, is_kaomoji in (split_kaomoji(text) if dim else [(text, False)]):
            cursor.insertText(part, dim_char if is_kaomoji else char)
        doc.setTextWidth(width)
        return doc


class ChatBubble(QWidget):
    send_requested = Signal(str)   # 用户在这个气泡里说了一句话
    offer_reply = Signal(str)      # 点了邀请的轻按钮，内容是"好呀"或"不用了"
    offer_hidden = Signal(str)     # 那排轻按钮退休了，内容是它原来归哪条线（v2-social-scene-boundary）
    copy_requested = Signal(str)   # 点了「复制回复」，内容是要抄进剪贴板的正文（v2-social-chat-ui-copy）
    user_left = Signal()           # 用户离开这一则：气泡失焦 / 被收起（v2-social-card-timeout）
    user_back = Signal()           # 用户回到这一则：气泡又被激活（v2-social-card-timeout）

    OFFER_YES = "好呀"
    OFFER_NO = "不用了"
    COPY_LABEL = "复制回复"        # 帮聊正文下面那个按钮的字（批次11 需求6）

    def __init__(self, pet_name: str, parent: QWidget | None = None):
        super().__init__(parent, Qt.Tool | Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        self.setObjectName("ChatBubble")
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setStyleSheet(CHAT_QSS)
        self.setFixedWidth(BUBBLE_W)   # 宽度固定，高度按聊了多少内容自己长

        self._pet_name = pet_name
        self._pet_rect: QRect | None = None
        self._busy = False
        self._offer_theme = ""    # 当前挂着哪张卡的邀请；空串表示没有
        # 「复制回复」那一套（v2-social-chat-ui-copy 批次11 → 提交F 搬进那一则的框里）：
        # 手上拿的是哪一版正文、按钮和它那一行分别是谁，三个都跟着**最新那一则**走。
        # 那一则被摘掉时三样一起作废（控件随条目一起删，见 `clear_social_draft`）。
        self._copy_text = ""                    # 空串 = 没得复制
        self._copy_row: QWidget | None = None   # 按钮那一行（靠右）
        self._copy_button: QPushButton | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(PADDING, PADDING, PADDING, PADDING + TAIL_HEIGHT)
        layout.setSpacing(6)

        head = QHBoxLayout()
        # v2-pet-persona-rename：标题不再是「跟<宠物名>聊聊」，固定成「AI助理」——
        # 助手不再以宠物名示人。`pet_name` 仍旧收着（`apply_speaker` 换角色卡时会用）。
        self._title = QLabel(CHAT_TITLE, self)
        self._title.setObjectName("ChatTitle")
        head.addWidget(self._title)
        head.addStretch(1)
        close_button = QPushButton("×", self)
        close_button.setObjectName("ChatClose")
        close_button.setToolTip("收起（Esc）")
        close_button.setCursor(Qt.PointingHandCursor)
        close_button.clicked.connect(lambda: self.close_bubble())
        head.addWidget(close_button)
        layout.addLayout(head)

        self._history = QListWidget(self)
        self._history.setObjectName("ChatHistory")
        self._history.setWordWrap(True)
        self._history.setSelectionMode(QListWidget.NoSelection)
        self._history.setFocusPolicy(Qt.NoFocus)
        # 一条消息一个气泡（指令 26-A）：delegate 负责画，间距交给列表自己留
        self._history.setItemDelegate(_BubbleDelegate(self._history))
        self._history.setSpacing(ITEM_GAP)
        self._history.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 记录区底下留一口气（v2-social-ui-batch 批次9）：视口上收这么多，滚到底时最后一条
        # 气泡下面就空出这一段——末行不再贴着输入行被切掉半个字。
        self._history.setViewportMargins(0, 0, 0, HISTORY_PAD_BOTTOM)
        # 滚起来要滑、不要一跳一跳（v2-social-ui-batch 批次10）：按像素滚 + 拖拽带惯性
        # + 滚轮那一格用短动画滑过去（细节见 ui/smooth_scroll.py 开头）。
        self._smoother = enable_smooth_scroll(self._history)
        layout.addWidget(self._history)

        # 建卡邀请的轻按钮（指令 25-B 第 2 项）：不弹窗，就在对话流里问一句，
        # 平时藏起来；卡牌服务真出邀请了才冒出来，点了就当成用户说了那句话。
        self._offer_row = QWidget(self)
        offer_layout = QHBoxLayout(self._offer_row)
        offer_layout.setContentsMargins(0, 0, 0, 0)
        offer_layout.setSpacing(6)
        self._offer_yes = QPushButton(self.OFFER_YES, self._offer_row)
        self._offer_yes.setObjectName("ChatOffer")
        self._offer_yes.setCursor(Qt.PointingHandCursor)
        self._offer_yes.clicked.connect(lambda: self._reply_offer(self.OFFER_YES))
        offer_layout.addWidget(self._offer_yes)
        self._offer_no = QPushButton(self.OFFER_NO, self._offer_row)
        self._offer_no.setObjectName("ChatOffer")
        self._offer_no.setCursor(Qt.PointingHandCursor)
        self._offer_no.clicked.connect(lambda: self._reply_offer(self.OFFER_NO))
        offer_layout.addWidget(self._offer_no)
        offer_layout.addStretch(1)
        self._offer_row.hide()
        layout.addWidget(self._offer_row)

        # 「复制回复」那个按钮**不在这儿建**（v2-social-chat-ui-fix2 提交F）：它现在长在
        # 帮聊那一则自己的框里、压在外层框的右下角（`_build_social_card`）。建在这里的话
        # 它就成了气泡的一个固定控件——那一则被摘掉、按钮却还杵在框底按着上一版正文。

        row = QHBoxLayout()
        row.setSpacing(6)
        self._input = QLineEdit(self)
        self._input.setObjectName("ChatInput")
        self._input.setPlaceholderText("想跟我说点什么？")
        self._input.returnPressed.connect(self._send)
        row.addWidget(self._input, 1)
        self._send_button = QPushButton("发送", self)
        self._send_button.setObjectName("ChatSend")
        self._send_button.setCursor(Qt.PointingHandCursor)
        self._send_button.clicked.connect(self._send)
        row.addWidget(self._send_button)
        layout.addLayout(row)

        # 淡入淡出：出现和收起都柔和一点，别突然冒出来
        self._fade = QPropertyAnimation(self, b"windowOpacity", self)
        self._fade.setDuration(180)
        self._fade.setEasingCurve(QEasingCurve.InOutQuad)
        self._fade.finished.connect(self._on_fade_finished)

    # ---------- 对外 ----------

    @property
    def busy(self) -> bool:
        return self._busy

    def open_at(self, pet_rect: QRect) -> None:
        """在宠物旁边展开；已经开着就只更新位置。"""
        self._pet_rect = pet_rect
        self._fit_height()
        self.move(*self._position(pet_rect))
        if not self.isVisible():
            self._fade.stop()
            self.setWindowOpacity(0.0)
            self.show()
            self._fade.setStartValue(0.0)
            self._fade.setEndValue(1.0)
            self._fade.start()
        self.raise_()
        self.activateWindow()
        self._input.setFocus()
        # 用户又站到这一则跟前了（v2-social-card-timeout）：帮聊那一则的 3 分钟清理线
        # 据此停表、留卡。`activateWindow()` 真发 `WindowActivate` 时 `event()` 那头也会
        # 报一声——两条路幂等，谁先到都只是"停表"。
        self.user_back.emit()

    def close_bubble(self, fade: bool = True) -> None:
        if not self.isVisible():
            return
        # 框收了 = 用户离开这一则（v2-social-card-timeout）。报在淡出**之前**：那条清理线
        # 要的是"人走了"这一刻，不是"淡完了"那一刻。已经在淡出的框再点一次 × 会走到这儿
        # 第二遍，那头只是把表重新起一遍，幂等。
        self.user_left.emit()
        # 框都收了，上面那排按钮跟着作废（v2-social-scene-boundary）：不走 `hide_offer`
        # 是因为它末尾要 `_fit_height()` 重排一次高度，而这里整框正在淡出——为一次
        # 看不见的重排改尺寸不值得。只摘主题、藏按钮、喊那一声。
        theme, self._offer_theme = self._offer_theme, ""
        if theme:
            self._offer_row.hide()
            self.offer_hidden.emit(theme)
        if not fade:
            self._fade.stop()
            self.hide()
            return
        self._fade.stop()
        self._fade.setStartValue(self.windowOpacity())
        self._fade.setEndValue(0.0)
        self._fade.start()

    def follow(self, pet_rect: QRect) -> None:
        """宠物移动时跟着走。"""
        self._pet_rect = pet_rect
        if self.isVisible():
            pos = self._position(pet_rect)
            if pos != (self.x(), self.y()):
                self.move(*pos)

    def append_user(self, text: str) -> None:
        self._append(text, SPEAKER_USER)

    def append_pet(self, text: str) -> None:
        self._append(text, SPEAKER_PET)
        self.set_busy(False)

    def append_note(self, text: str) -> None:
        self._append(text, SPEAKER_NOTE)
        self.set_busy(False)

    # ---------- 帮聊那一则（v2-social-chat-ui-copy 批次11 → fix2 提交F 起套框） ----------

    def append_social_card(self, body: str, note: str, draft: str) -> None:
        """帮聊那一则：**正文 + 解析 + 「复制回复」套成一件，整条 append 进记录区**。

        v2-social-chat-ui-fix2 提交F。修前是散着的三条：正文走 `append_social_draft`
        （`SPEAKER_PET` 那条渲染路，圆角 + 1px 描边）、解析走 `append_social_note`
        （居中灰字）、按钮挂在气泡底部当固定控件——真机上分不清"哪句能直接粘走、哪句是
        给我看的门道"，而且按钮跟那一则不是一条命（摘掉那一则，按钮还在）。现在：

        - **外层框**（粗边 3px、圆角、留白 8px）——这一整块就是"一则"，`ROLE_SOCIAL_DRAFT`
          也打在它这一条上（`clear_social_draft` 按它整则摘掉，正文 + 解析 + 按钮一次清）；
        - **内层框**（细边 1px、留白 6px）——只装**正文**，能直接粘走的那句；
        - 内层下方、外层之内——**解析**（灰字，跟系统提示同一档色）；
        - 外层右下角——「复制回复」按钮（样式照旧借 `ChatOffer`，信号链一个字没改：
          `clicked` → `_copy_clicked` → `copy_requested` → 装配层那只手写剪贴板）。

        条目的 `DisplayRole` 文本照旧给"正文 + 空行 + 解析"（跟 `social_draft_text` 一个
        口径）：那一则是靠控件显示的，这行文本不画，但它是"气泡里写着什么"这个问法的
        唯一凭据（测试、日志都按它读），不能因为改渲染就把它抽掉。
        """
        width = self._card_width()
        card = self._build_social_card(body, note, draft, width)
        card.show()

        item = QListWidgetItem(f"{body}\n\n{note}")
        item.setData(ROLE_SPEAKER, SPEAKER_PET)
        item.setData(ROLE_SOCIAL_DRAFT, True)
        # 条目高度就是**控件的实际高度**（`_build_social_card` 已经按这个宽度折行量好、
        # 并 `setFixedHeight` 定死）——不另量一套，也不拿桩文本的高：`_content_height`
        # 与 delegate 读的都是这一格，量少了解析末行就被裁（v2-social-card-size 批次11b-补G）。
        item.setSizeHint(QSize(width, card.height()))
        self._history.addItem(item)
        self._history.setItemWidget(item, card)
        while self._history.count() > MAX_ITEMS:
            self._history.takeItem(0)
        self.set_busy(False)
        # 先长高、再滚到底——跟 `_append` 那条老口径同一拍的理由（批次9 的坑）。
        self._fit_height()
        self._history.scrollToBottom()

    def _build_social_card(self, body: str, note: str, draft: str, width: int) -> QFrame:
        """把那一则搭出来（`append_social_card` 的干活部分，单独拆开只为读起来清爽）。"""
        # 字号跟着记录区走：样式表里的 14px 落在 `_history` 的 font 上，这里量之前先让它
        # 落到控件上（跟 `_content_height` 开头同一句），不然解析会比正文大一号。
        self._history.ensurePolished()
        font = self._history.font()

        card = QFrame(self._history)
        card.setObjectName("ChatDraftCard")
        # 用 objectName 选中自己：不带选择器的话这条样式会往子控件上淌，正文框的细边就没了。
        card.setStyleSheet(
            f"QFrame#ChatDraftCard {{ background: transparent;"
            f" border: {CARD_BORDER_W}px solid {BUBBLE_BORDER};"
            f" border-radius: {CARD_RADIUS}px; }}"
        )
        outer = QVBoxLayout(card)
        outer.setContentsMargins(CARD_PAD, CARD_PAD, CARD_PAD, CARD_PAD)
        outer.setSpacing(CARD_GAP)

        # 内层：只装正文
        inner = QFrame(card)
        inner.setObjectName("ChatDraftBody")
        inner.setStyleSheet(
            f"QFrame#ChatDraftBody {{ background: {BUBBLE_BG};"
            f" border: {DRAFT_BORDER_W}px solid {BUBBLE_BORDER};"
            f" border-radius: {BUBBLE_RADIUS}px; }}"
        )
        inner_layout = QVBoxLayout(inner)
        inner_layout.setContentsMargins(DRAFT_PAD, DRAFT_PAD, DRAFT_PAD, DRAFT_PAD)
        body_label = QLabel(body, inner)
        body_label.setObjectName("ChatDraftText")
        body_label.setWordWrap(True)
        body_label.setFont(font)
        body_label.setStyleSheet("background: transparent; border: none;")
        inner_layout.addWidget(body_label)
        outer.addWidget(inner)

        # 解析：灰字，跟系统提示（`SPEAKER_NOTE`）同一档颜色
        note_label = QLabel(note, card)
        note_label.setObjectName("ChatDraftNote")
        note_label.setWordWrap(True)
        note_label.setFont(font)
        note_label.setStyleSheet(f"color: {TEXT_MUTED}; background: transparent; border: none;")
        outer.addWidget(note_label)

        # 右下角：复制回复（照 `_offer_row` 那排轻按钮的写法）
        row = QWidget(card)
        row_layout = QHBoxLayout(row)
        row_layout.setContentsMargins(0, 0, 0, 0)
        row_layout.setSpacing(6)
        row_layout.addStretch(1)
        button = QPushButton(self.COPY_LABEL, row)
        button.setObjectName("ChatOffer")
        button.setCursor(Qt.PointingHandCursor)
        button.clicked.connect(self._copy_clicked)
        row_layout.addWidget(button)
        outer.addWidget(row)

        # 手上那三样跟着这一则走（空正文时按钮按灰，`_copy_clicked` 也再判一道）
        self._copy_text = draft or ""
        self._copy_button = button
        self._copy_row = row
        button.setEnabled(bool(self._copy_text))

        card.setFixedWidth(width)
        card.setFixedHeight(self._card_measure(card, width))
        return card

    def _card_measure(self, card: QWidget, width: int) -> int:
        """这一则在**给定宽度下**真实要占多高（v2-social-card-size 批次11b-补G）。

        老口径是 `card.adjustSize()`，而它用的是 `sizeHint()`——带 `setWordWrap(True)` 的
        `QLabel` 的 sizeHint 是按**不折行**给的自然宽高（离屏量过：卡片宽 262，sizeHint
        却是 582×133），解析一长，卡片就只按"一行"长高，真机上多行解析被裁出框外
        （图证"比较……"之后截断）。

        `heightForWidth(width)` 才是"给定这个宽度、折完行要占多高"，它把内层正文框、
        解析、按钮那一行、两层 padding 与两层边框全算进去（离屏实测 262 宽 → 197，
        正好等于各段之和）。量之前先把布局跑一遍（同 `_card_width` 的理由：气泡头一次
        摆这一则时窗口可能还没显示过）。

        返回 0 或负数（量不出来）时才退回 `sizeHint`——宁可给一个偏小的值，也不能给 0：
        条目高 0 的话整则直接看不见。
        """
        self._history.ensurePolished()
        card.layout().activate()
        measured = card.heightForWidth(width)
        if measured <= 0:
            measured = card.sizeHint().height()
        return max(1, measured)

    def _card_width(self) -> int:
        """那一则能占多宽：记录区视口宽度，**再扣掉滚动条那一格**、留一点边。

        为什么连滚动条一起扣（哪怕这会儿它还没冒出来）：这一则的宽度一建就定死，高度也
        是按这个宽度量的；等聊到滚动条冒出来，视口会窄掉一格，卡片要是照着"没滚动条"的
        宽度做，右边就会压到滚动条底下去。索性按有滚动条算——两种情况都塞得下，多的那
        一格宽只是右侧多留点白（离屏量过：视口 266 时卡片 248，左右都不越界）。

        量之前先把布局跑一遍（`activate()`）：气泡头一次摆这一则时窗口可能还没显示过，
        不跑一遍拿到的是控件的初始尺寸，条目宽度会跟实际视口对不上。
        """
        self.layout().activate()
        viewport = self._history.viewport().width()
        if viewport <= 120:                 # 还没排过版：退回"气泡宽 - 左右留白"
            viewport = BUBBLE_W - 2 * PADDING
        bar = 0
        if self._history.verticalScrollBarPolicy() != Qt.ScrollBarAlwaysOff:
            bar = self._history.verticalScrollBar().sizeHint().width()
        return max(160, viewport - bar - 4)

    def clear_social_draft(self) -> None:
        """把上一轮帮聊那一则整个摘掉，**用户与宠物的普通聊天一条不动**。

        v2-social-chat-ui-copy 批次11 需求5：记录区里只留最新一则。这里不猜"这条草稿
        过没过期"——批次8 那条线已经保证"对方新回一句 → 材料变 → 一定重拟"，所以新草稿
        临到眼前那一刻把旧的摘掉就够，中间那段时间旧的照旧留着给人看。

        摘的是**这一格标记**，不是整框 `clear()`，也不是按文本挑（解析那句里也可能出现
        「」，按文本挑会误伤真聊天）。倒着走是因为 `takeItem` 会挪动后面的行号。

        v2-social-chat-ui-fix2 提交F：正文 + 解析 + 「复制回复」现在同属**一个**条目
        （复合控件），所以 `takeItem` 一下三样一起走——`Qt` 会把挂在条目上的那个控件
        一并删掉，不必再单独去藏按钮。手上那三样也跟着作废：留着按钮（哪怕是删掉的
        控件）却按着上一版正文是坑。
        """
        removed = False
        for row in range(self._history.count() - 1, -1, -1):
            item = self._history.item(row)
            if item is not None and item.data(ROLE_SOCIAL_DRAFT):
                self._history.takeItem(row)
                removed = True
        self._copy_text = ""
        self._copy_row = None
        self._copy_button = None
        if removed:
            self._fit_height()

    def set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._input.setEnabled(not busy)
        self._send_button.setEnabled(not busy)
        self._send_button.setText("在想…" if busy else "发送")
        if not busy and self.isVisible():
            self._input.setFocus()

    # ---------- 建卡邀请的轻按钮（指令 25-B 第 2 项） ----------

    @property
    def offer_theme(self) -> str:
        """这会儿这排按钮归哪条线（空串 = 没摆）。装配层要按它收尾（v2-scene-state：
        场景切走了，工作态那句问句连按钮一起作废）。"""
        return self._offer_theme

    def show_offer(self, theme: str) -> None:
        """卡牌服务出邀请了：把 [好呀] [不用了] 露出来，等用户点一下。

        **顶掉别人的按钮时，替它喊一声退休**（v2-social-scene-boundary）：按钮只有
        一排，这一排上来就意味着上一排没了。在这之前 `_offer_theme` 是**静默覆盖**
        的，被顶掉那条线（社交那排尤其是）永远不知道自己已经出局——而社交服务那边
        `_awaiting` 一挂上，`_poll_listen` 就被它 early-return 拦住，静默停摆。
        同一条线换同一张卡（theme 没变）不算顶掉，不喊。
        """
        previous, self._offer_theme = self._offer_theme, theme
        self._offer_row.show()
        self._fit_height()
        if previous and previous != theme:
            self.offer_hidden.emit(previous)

    def hide_offer(self) -> None:
        """邀请有结果了（受理 / 拒绝 / 超时）：收起轻按钮。"""
        theme, self._offer_theme = self._offer_theme, ""
        if not theme:
            return
        if self._offer_row.isVisible():
            self._offer_row.hide()
            self._fit_height()
        self.offer_hidden.emit(theme)

    def _reply_offer(self, answer: str) -> None:
        """点了轻按钮：当成用户说了这句话，交给会话状态机去走同一个出口。

        **先派发答复、再报退休**（v2-social-scene-boundary）：装配层要按"还挂着这一问"
        的状态去认这活儿归谁——先报退休的话，社交那条线的 `awaiting_confirm` 已经清了，
        `_on_offer_reply` 就找不到人接，[好呀] 点下去等于点空。
        """
        if self._busy or not self._offer_theme:
            return
        theme, self._offer_theme = self._offer_theme, ""   # 先摘主题，防连点派发两次
        self._offer_row.hide()
        self._fit_height()
        self.append_user(answer)
        self.set_busy(True)
        self.offer_reply.emit(answer)
        self.offer_hidden.emit(theme)

    def _copy_clicked(self) -> None:
        """点了「复制回复」：把按着的那版正文报给装配层（它去写剪贴板）。

        这里**不碰剪贴板**——全仓库碰剪贴板的手只有装配层那一只（`_copy_social_draft`），
        气泡只管把"用户点的哪一版"报上去。空正文时按钮已经是灰的，这里再判一道，
        免得被程序化触发时把空气报出去。
        """
        if self._copy_text:
            self.copy_requested.emit(self._copy_text)

    def apply_speaker(self, speaker: str) -> None:
        """换说话人：标题与回复前缀都跟着变（指令 14 的角色标识）。"""
        self._pet_name = speaker
        self._title.setText(f"跟{speaker}聊聊")

    # ---------- 内部 ----------

    def _send(self) -> None:
        text = self._input.text().strip()
        if not text or self._busy:
            return
        self._input.clear()
        self.append_user(text)
        self.set_busy(True)
        self.send_requested.emit(text)

    def _append(self, text: str, speaker: str, role: int = 0) -> None:
        item = QListWidgetItem(text)
        item.setData(ROLE_SPEAKER, speaker)
        if role:                      # 额外打一格标记（批次11：帮聊那一则要能整批摘掉）
            item.setData(role, True)
        if speaker == SPEAKER_NOTE:
            item.setForeground(QColor(TEXT_MUTED))
        self._history.addItem(item)
        while self._history.count() > MAX_ITEMS:
            self._history.takeItem(0)
        # **先长高、再滚到底**（v2-social-ui-batch 批次9）。老口径反着来：先 `scrollToBottom()`
        # 再 `_fit_height()`——而恰恰在这一拍，"内容第一次超过上限"那句会把记录区从旧高度长到
        # 新高度，滚动是在**旧高度**上做的，等视口长完，位置就不在底上了：最新那条明明在
        # 里面，屏幕上却停在它上面，于是被下缘切掉半行（真机图 2 那一屏）。两个顺序一换，
        # 滚动发生在"已经是最终高度"的视口上，滚到底就是真的到底。
        self._fit_height()
        self._history.scrollToBottom()

    def _fit_height(self) -> None:
        """按聊了多少内容把气泡长高：内容多高就多高，到屏幕上限就停（多出来的在里面滚动）。"""
        # 先把记录区复位到最小值，量出"标题 + 输入框 + 边距"这些不随内容变的开销
        self._history.setFixedHeight(MIN_HISTORY_H)
        self.layout().activate()
        chrome = self.sizeHint().height() - MIN_HISTORY_H

        room = self._max_height() - chrome
        # `+ HISTORY_PAD_BOTTOM`：这一格是**给记录区**的，不是给内容的——视口比内容
        # （连同它那两道边框）高出的正是底部那口气（见 `setViewportMargins` 那一处），
        # 所以内容刚好塞得下时不会凭空多出一条滚动条。
        history_h = max(MIN_HISTORY_H,
                        min(self._content_height() + HISTORY_PAD_BOTTOM, room))
        self._history.setFixedHeight(history_h)
        self.layout().activate()
        self.setFixedHeight(history_h + chrome)

        if self._pet_rect is not None:      # 变高了，位置得跟着重算，别顶出屏幕
            self.move(*self._position(self._pet_rect))

    def _content_height(self) -> int:
        """记录区里这些消息排下来一共要占多高。

        高度交给 delegate 算（气泡多宽、行距多少、上下留多少白，都在它那儿），
        这里不另算一套——否则"量出来的"和"画出来的"迟早对不上。
        也不用 QListWidget.sizeHintForRow：那个方法要先真正排版过才准，
        气泡刚打开（还没显示）时它给的是错的值。
        """
        self._history.ensurePolished()   # 样式表里的字号要先落到控件上，量出来才是 14px
        option = QStyleOptionViewItem()
        option.widget = self._history
        option.font = self._history.font()
        option.rect = QRect(0, 0, self._history.viewport().width(), 0)
        delegate = self._history.itemDelegate()
        model = self._history.model()
        total = 2 * self._history.frameWidth()
        for index in range(self._history.count()):
            total += delegate.sizeHint(option, model.index(index, 0)).height()
            total += self._history.spacing()
        return total

    def _max_height(self) -> int:
        """气泡最高能到多少：屏幕可用高度的六成。"""
        screen = self._screen()
        return int(screen.availableGeometry().height() * MAX_HEIGHT_RATIO)

    def _screen(self):
        rect = self._pet_rect
        if rect is not None:
            screen = QGuiApplication.screenAt(rect.center())
            if screen is not None:
                return screen
        return QGuiApplication.primaryScreen()

    def _position(self, pet_rect: QRect) -> tuple[int, int]:
        """优先放在宠物正上方，上方不够就落到宠物下方；左右按屏幕边裁剪。"""
        area = self._screen().availableGeometry()
        x = pet_rect.center().x() - self.width() // 2
        y = pet_rect.top() - self.height() + TAIL_HEIGHT
        if y < area.top() + SCREEN_MARGIN:
            y = pet_rect.bottom() - TAIL_HEIGHT
        x = max(area.left() + SCREEN_MARGIN, min(x, area.right() - self.width() - SCREEN_MARGIN))
        y = max(area.top() + SCREEN_MARGIN, min(y, area.bottom() - self.height() - SCREEN_MARGIN))
        return x, y

    def event(self, event) -> bool:  # noqa: N802 (Qt 命名)
        """气泡窗口被激活 / 失焦那一拍往外报一声（v2-social-card-timeout）。

        只转发、不改行为——整条链的返回还是 `super().event(event)`，界面怎么显示一个字
        没动。帮聊那一则"离开对话窗口满 3 分钟就清掉"的线就等这两声：**离开才开始计，
        回来就停**；所以用户一直坐在框跟前看着那条路上，定时器自始至终不启动（那是这条
        线最要紧的一处防错点）。
        """
        kind = event.type()
        if kind == QEvent.WindowDeactivate:
            self.user_left.emit()
        elif kind == QEvent.WindowActivate:
            self.user_back.emit()
        return super().event(event)

    def keyPressEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        if event.key() == Qt.Key_Escape:
            self.close_bubble()
            return
        super().keyPressEvent(event)

    def _on_fade_finished(self) -> None:
        # 淡出结束后才真正隐藏，并复位透明度，方便下次淡入
        if self.windowOpacity() == 0.0:
            self.hide()
            self.setWindowOpacity(1.0)

    def paintEvent(self, event) -> None:  # noqa: N802 (Qt 命名)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)

        body = QRectF(0.75, 0.75, self.width() - 1.5, self.height() - TAIL_HEIGHT - 0.75)
        path = QPainterPath()
        path.addRoundedRect(body, CORNER, CORNER)

        tail = QPainterPath()
        tail.moveTo(body.center().x() - 9, body.bottom() - 1)
        tail.lineTo(body.center().x(), body.bottom() + TAIL_HEIGHT - 1)
        tail.lineTo(body.center().x() + 9, body.bottom() - 1)
        tail.closeSubpath()

        painter.setPen(QPen(QColor(BUBBLE_BORDER), 1.5))
        painter.setBrush(QColor(BUBBLE_BG))
        painter.drawPath(path.united(tail).simplified())
