"""
卡牌详情页（对应指令 12）：一张卡的 AI 交互都在这里。

一张卡上的四件事：看进度、改目标与状态、打卡、给这张卡设提醒。
从生活广场点一张卡进来。

卡面只有上下两张框：上半「建议」是 AI 建议流，下半「辅助工具」是工具 / 规划 / 主操作 /
提醒。引导、建议、打卡鼓励的回话都由生活卡牌服务在后台生成，走 pet_speak 由宠物本体
说出来，所以界面线程只负责显示建议与控件。

**卡底多出的小窗（v2-card-tools-A）**：卡面最底下嵌着这张卡自己的 `CardChatPanel`
（`ui/card_chat.py`）。它跟上半那两块不搭线——上半是"服务生成、宠物说"，这个小窗是
"用户在卡里直接跟它说"。**跟全局那个「AI助理」也不是同一个窗**：这里说的话
落在按卡号分开的记录里，不进 pet.db / mind.db，别的卡和全局都看不见（见
`life/cards/chat.py` 开头的三条隔离）。

两张卡各多一块（指令 24 第 4 条，卡间画像互联）：
- 出行卡：多一行"目的地 + 日期 + 帮我规划"，联网查景点/美食/穿搭/防晒；
- 减脂卡：每次打开自动生成一段饮食建议（画像里有近期出行就多一句"外食怎么选"）。
这两块都只是把参数交给 CardService，怎么读画像、怎么回写画像都在服务层。
"""

from __future__ import annotations

from datetime import datetime, time as dt_time, timedelta

from PySide6.QtCore import QDate, QStandardPaths, Qt, QTime, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDateEdit,
    QFileDialog,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPushButton,
    QTextBrowser,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from brain.chains.card_ai import ACTION_ADVISE, ACTION_GUIDE
from core.config import Config
from core.events import EventBus
from data.models import Card
from life.cards.chat import (
    LIFE_KIND,
    CardChatService,
    card_namespace,
    guide_profile_lines,
)
from life.cards.layout import (
    BLOCK_ADVICE,
    BLOCK_CHAT,
    BLOCK_HEAD,
    BLOCK_TOOLS,
    BLOCK_WIDGETS,
    CardLayoutStore,
)
from life.cards.service import CardService, is_diet_card, is_trip_card
from life.cards.templates import (
    CARD_STATUS_TITLES,
    FEATURE_ADVISE,
    FEATURE_CHECKIN,
    FEATURE_GUIDE,
    FEATURE_REMIND,
)
from life.psych.persona_loader import spawn_options
from life.profile.advice import diet_advice, work_advice
from life.profile.store import DIET, TRAVEL, WORK, ProfileStore
from life.profile.tool_registry import (
    CATEGORY_DIET,
    CATEGORY_TRAVEL,
    CATEGORY_WORK,
    match_tools,
    tool_suggest,
)
from pet.persona import strip_action_marks
from ui.card_chat import CardChatPanel
from ui.card_layout import RESET_HINT, RESET_TEXT, CardBoard
from ui.card_tools import CardToolStrip
from ui.card_dialogs import ShareDialog
from ui.card_image import render_card_image

DEFAULT_REMIND_HOUR = 21    # 提醒时间默认晚上九点
TRIP_DEFAULT_DAYS = 7       # 出行日期默认填一周后
TRIP_HOUR = 9               # 出行只用到日期，时刻统一按当天上午九点拼成 datetime
ADVICE_HEIGHT = 72          # 上半建议流的最小高度（跟着框一起长）
ADVICE_WAITING = "正在想…"   # 专业角色的建议还在后台生成时，先占个位
WORK_THEME = "工作"          # 工作卡的主题（与 life/chatter.py 的 WORK_THEME 同一取值）

# 硬两半：上下各一张带描边的框，一眼分得清。
# 上半「建议区」是白卡（和面板底的暖米 #fdf7ee 略开一档，靠描边 #eadfd2 圈出来）；
# 下半「辅助工具区」铺一层明显更深的暖米底 #f0e1c8。底色要跟面板底拉开一档，否则
# 两块几乎同色、一眼分不清（上一版 #f6ecd9 差得太小，用户反馈"两半没体现出来"）。
_UPPER_HALF_QSS = (
    "QWidget#CardUpper {"
    "  background-color: #ffffff;"
    "  border: 1px solid #eadfd2;"
    "  border-radius: 10px;"
    "}"
    "QWidget#CardUpper QTextBrowser {"
    "  background: transparent;"
    "  border: none;"
    "}"
)

_LOWER_HALF_QSS = (
    "QWidget#CardLower {"
    "  background-color: #f0e1c8;"
    "  border: 1px solid #e3d0ae;"
    "  border-radius: 10px;"
    "}"
)


class CardDetailPanel(QWidget):
    # 卡底那个小窗里说了一句（v2-card-tools-A）：(卡号, 用户说的话)。
    # 面板只管转手往上报——"这句话的上下文该从哪儿取、回话走哪条链"都在装配处，
    # 卡面这一层不认识模型也不认识库。
    card_chat_requested = Signal(str, str)

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        service: CardService,
        profile_store: ProfileStore,
        chat_service: CardChatService | None = None,
        layout_store: CardLayoutStore | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._service = service
        self._profile_store = profile_store
        self._chat_service = chat_service
        # 卡面摆成什么样（v2-card-tools-C）：一张卡一份记录，按卡号分。没给也能跑，
        # 就是"这一只桌宠不记布局"（测试里造面板时省事）。
        self._layout_store = layout_store
        self._layout_ns = ""                        # 现在这块板上摆的是哪张卡
        self._card_id: int | None = None
        self._advice_card_id: int | None = None     # 上半框这段建议是请给哪张卡的（防重复请求）
        self._loading = False                       # 回填控件时别把控件变化当成用户操作

        self._build_ui()
        # 卡上不再有对话区：回话由服务走 pet_speak 交给宠物本体说。
        # card_reply 还留着，只为把「在想…」的按钮解锁。
        bus.card_reply.connect(self._on_reply)
        # 专业角色的卡面建议（指令 27）：后台生成完送到上半框
        bus.card_advice.connect(self._on_advice)
        bus.card_changed.connect(self.refresh)
        # 卡内小助手（v2-card-tools-A）：回话按**卡号**分派，不是这张卡的一律不接
        # （`_on_card_chat_reply` 里那道判断就是隔离在界面这一层的落点）
        bus.card_chat_reply.connect(self._on_card_chat_reply)
        bus.card_chat_note.connect(self._on_card_chat_note)
        bus.card_tools_changed.connect(self._on_card_tools_changed)

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 12, 18, 18)
        layout.setSpacing(8)
        # 面板自己只剩"一块板"：卡上那几块东西全部交给板子排（v2-card-tools-C），
        # 面板这层不再直接 addWidget —— 不然拖动换位置换的是板子，外面还按老样子钉着。
        self.board = CardBoard(self)
        layout.addWidget(self.board, 1)

        # ---- 卡头外壳：标题 + 状态 + 目标（不变） ----
        # 卡头（标题 + 状态行那一条）**也算一块**：它能被拖到别处，不再是钉死在顶上的。
        # 做成一个外壳 QWidget 是因为板子摆的是控件，而状态那一行本来是一条布局。
        self._head = QWidget(self)
        head = QVBoxLayout(self._head)
        head.setContentsMargins(0, 0, 0, 0)
        head.setSpacing(8)
        self._title = QLabel("", self._head)
        self._title.setObjectName("TitleLabel")
        head.addWidget(self._title)

        status_row = QHBoxLayout()
        self._meta = QLabel("", self._head)
        self._meta.setObjectName("MutedLabel")
        status_row.addWidget(self._meta, 1)
        status_row.addWidget(QLabel("状态", self._head))
        self._status = QComboBox(self._head)
        self._status.addItems(list(CARD_STATUS_TITLES.values()))
        self._status.currentTextChanged.connect(self._save_status)
        status_row.addWidget(self._status)
        # 「重置布局」挨着状态摆着：**只在这张卡被摆过时才亮出来**（没摆过就没有可重置的，
        # 常驻一个按不动的按钮只是占地方）
        self._reset_button = QPushButton(RESET_TEXT, self._head)
        self._reset_button.setObjectName("LayoutReset")
        self._reset_button.setToolTip(RESET_HINT)
        self._reset_button.setVisible(False)
        self._reset_button.clicked.connect(self._reset_layout)
        status_row.addWidget(self._reset_button)
        head.addLayout(status_row)

        # 对象攻略卡那八格「对方档案」（v2-social-card-uiread）：后台提炼写进 card 表那八列
        # 之后，**在此之前界面上一个地方都不显示**，用户看到的是"这张卡全空"。这里在卡头上
        # 补一块只读小框把它列出来（跟卡窗那格 `data` 共用 `guide_profile_lines`，同一批字）。
        # 八格全空的卡（其余八类，以及还没提炼过的攻略卡）整块收着，不占地方。
        # **不是** `_refresh_profile_panes` 那一套（那是出行 / 餐饮 / 工作卡的画像建议，只以
        # 建议形式露出、不暴露原始字段）；这一块只服务对象攻略卡、露的就是那八格原文。
        self._guide_pane = QLabel("", self._head)
        self._guide_pane.setObjectName("GuideProfilePane")
        self._guide_pane.setWordWrap(True)
        self._guide_pane.setVisible(False)
        head.addWidget(self._guide_pane)
        self.board.add_block(BLOCK_HEAD, self._head)

        # ---- 上半·建议区：一张白卡，里面只有一块只读的建议框（占满剩余高度） ----
        # 卡内不设对话通道（A 方案）：没有气泡、没有输入框，这块只显示建议文字。
        self._upper = QWidget(self)
        self._upper.setObjectName("CardUpper")
        # 普通 QWidget 默认不画样式表背景，得开 WA_StyledBackground 才会把底色 / 描边画出来
        self._upper.setAttribute(Qt.WA_StyledBackground, True)
        self._upper.setStyleSheet(_UPPER_HALF_QSS)
        upper = QVBoxLayout(self._upper)
        upper.setContentsMargins(10, 10, 10, 10)
        upper.setSpacing(8)

        self._upper_title = QLabel("建议", self._upper)
        self._upper_title.setObjectName("MutedLabel")
        upper.addWidget(self._upper_title)

        # 两半卡试点（指令 26-C C1/C2）：上半建议流（出行 / 工作 / 餐饮卡）。
        # 默认藏着；只有这些卡、且 life_cards.profile_tools_enabled 开着时才亮出来。
        # 画像原始信息只进内存、不进这块——这里只显示"建议形式"的结果。
        self._advice_pane = QTextBrowser(self._upper)
        self._advice_pane.setObjectName("AdvicePane")
        self._advice_pane.setMinimumHeight(ADVICE_HEIGHT)
        self._advice_pane.setVisible(False)
        upper.addWidget(self._advice_pane, 1)

        self.board.add_block(BLOCK_ADVICE, self._upper, stretch=1)

        # ---- 下半·辅助工具区：工具 / 行程规划 / 主操作 / 提醒设置（描边 + 淡色底） ----
        self._lower = QWidget(self)
        self._lower.setObjectName("CardLower")
        # 普通 QWidget 默认不画样式表背景，得开 WA_StyledBackground 才会把底色 / 描边画出来
        self._lower.setAttribute(Qt.WA_StyledBackground, True)
        self._lower.setStyleSheet(_LOWER_HALF_QSS)
        lower = QVBoxLayout(self._lower)
        lower.setContentsMargins(10, 10, 10, 10)
        lower.setSpacing(8)

        # 下半的区块小标题：和上半的「建议」一上一下，把两半各自的名字亮出来
        self._lower_title = QLabel("辅助工具", self._lower)
        self._lower_title.setObjectName("MutedLabel")
        lower.addWidget(self._lower_title)

        self._tool_pane = QWidget(self._lower)
        self._tool_layout = QHBoxLayout(self._tool_pane)
        self._tool_layout.setContentsMargins(0, 0, 0, 0)
        self._tool_layout.setSpacing(6)
        self._tool_pane.setVisible(False)
        lower.addWidget(self._tool_pane)

        # 出行卡专用一行（指令 24 链 a）：只有这张卡会显示
        self._trip_row = QWidget(self._lower)
        trip_row = QHBoxLayout(self._trip_row)
        trip_row.setContentsMargins(0, 0, 0, 0)
        trip_row.addWidget(QLabel("去", self._trip_row))
        self._trip_destination = QLineEdit(self._trip_row)
        self._trip_destination.setPlaceholderText("目的地，例如 成都")
        trip_row.addWidget(self._trip_destination, 1)
        self._trip_date = QDateEdit(QDate.currentDate().addDays(TRIP_DEFAULT_DAYS), self._trip_row)
        self._trip_date.setCalendarPopup(True)
        self._trip_date.setDisplayFormat("yyyy-MM-dd")
        trip_row.addWidget(self._trip_date)
        self._trip_button = QPushButton("帮我规划", self._trip_row)
        self._trip_button.setToolTip("联网查景点、美食、穿搭、防晒；美食会避开你的忌口")
        self._trip_button.clicked.connect(self._plan_trip)
        trip_row.addWidget(self._trip_button)
        lower.addWidget(self._trip_row)

        actions = QHBoxLayout()
        self._guide_button = QPushButton("引导我", self._lower)
        self._guide_button.setToolTip("把这张卡变清晰：定目标、拆步骤")
        self._guide_button.clicked.connect(lambda: self._ask(ACTION_GUIDE))
        actions.addWidget(self._guide_button)

        self._advise_button = QPushButton("给点建议", self._lower)
        self._advise_button.clicked.connect(lambda: self._ask(ACTION_ADVISE))
        actions.addWidget(self._advise_button)

        self._checkin_button = QPushButton("打卡", self._lower)
        self._checkin_button.setObjectName("PrimaryButton")
        self._checkin_button.clicked.connect(self._checkin)
        actions.addWidget(self._checkin_button)

        self._share_button = QPushButton("分享", self._lower)
        self._share_button.setToolTip("生成一段可以发给朋友的分享码（不含你的记忆与对话）")
        self._share_button.clicked.connect(self._share)
        actions.addWidget(self._share_button)

        self._image_button = QPushButton("存成图片", self._lower)
        self._image_button.setToolTip("把这张卡画成一张图片保存下来")
        self._image_button.clicked.connect(self._save_image)
        actions.addWidget(self._image_button)
        lower.addLayout(actions)

        remind_row = QHBoxLayout()
        remind_row.addWidget(QLabel("陪执行", self._lower))
        self._persona = QComboBox(self._lower)
        self._persona.setToolTip("这张卡由谁来陪（指令 14）；默认是宠物本体")
        for label, persona_id in spawn_options():
            self._persona.addItem(label, persona_id)
        self._persona.currentIndexChanged.connect(self._save_persona)
        remind_row.addWidget(self._persona)

        remind_row.addWidget(QLabel("提醒我", self._lower))
        self._remind_time = QTimeEdit(QTime(DEFAULT_REMIND_HOUR, 0), self._lower)
        self._remind_time.setDisplayFormat("HH:mm")
        remind_row.addWidget(self._remind_time)
        self._remind_button = QPushButton("设成提醒", self._lower)
        self._remind_button.clicked.connect(self._set_reminder)
        remind_row.addWidget(self._remind_button)
        remind_row.addStretch(1)
        self._remind_row = remind_row
        lower.addLayout(remind_row)

        # 上面留 4 + 板子自己的 8 = 12：跟以前 `addSpacing(12)` 那个间隙一样宽，
        # 上下两框之间照样留得开（间距交给行自己带，板子就不必为空隙摆一个占位控件）
        self.board.add_block(BLOCK_TOOLS, self._lower, margins=(0, 4, 0, 0))

        # ---- 卡上长出来的小部件（v2-card-tools-B）：卡窗里要来的东西摆在这儿 ----
        # 卡上没有部件时整块是收起来的（`CardToolStrip.bind` 收到空表就 hide），
        # 所以老卡的卡面跟以前一模一样，不多一条空框。
        self.card_tools = CardToolStrip(self)
        self.board.add_block(BLOCK_WIDGETS, self.card_tools)
        self.card_tools.run_requested.connect(self._on_tool_run)
        self.card_tools.fill_requested.connect(self._on_tool_fill)
        self.card_tools.note_requested.connect(self._on_tool_note)
        self.card_tools.state_changed.connect(self._on_tool_state)
        self.card_tools.remove_requested.connect(self._on_tool_remove)

        # ---- 卡底·卡内小助手（v2-card-tools-A）：这一张卡自己的一个小窗 ----
        # 它跟全局那个「AI助理」不是一回事（见 ui/card_chat.py 开头）：
        # 那张卡里说的话只落在卡号对应的那一份记录里，别处看不见。
        self.card_chat = CardChatPanel(self)
        self.board.add_block(BLOCK_CHAT, self.card_chat)
        self.card_chat.send_requested.connect(self.card_chat_requested)
        # 松手之后才知道"最后摆成什么样"（拖着的时候每一步都存，就会存一串中间状态）
        self.board.order_changed.connect(self._on_layout_changed)

    # ---------- 对外 ----------

    def show_card(self, card_id: int) -> None:
        """从生活广场点进来：换一张卡，重开时建议重新生成一遍。"""
        self._card_id = card_id
        self._advice_card_id = None      # 同一张卡再点开也重出一段（指令 27）
        self._set_busy(False)
        self._bind_card_chat(card_id)
        self._bind_card_layout(card_id)
        self.refresh()

    # ---------- 卡面摆成什么样（v2-card-tools-C） ----------

    def _bind_card_layout(self, card_id: int) -> None:
        """把这张卡上次摆的样子摆回来（没摆过就是默认次序，跟以前一模一样）。

        **换卡时次序一定要重摆**：不重摆的话，上一张卡拖成的样子会留在这一张卡上。
        摆完亮不亮「重置布局」由"这张卡有没有记录"决定。
        """
        self._layout_ns = card_namespace(LIFE_KIND, card_id)
        self.board.apply_order(self._saved_order())
        self._reset_button.setVisible(bool(self._saved_order()))
        # 摆开的**当下**量一遍坐标，跟记录里对不上就按新的写回（见 life/cards/layout.py
        # 开头那段"次序是用户的意思，坐标是当下的事实"）。单发一拍是因为这会儿窗口
        # 还没排完版，量出来全是 0。
        QTimer.singleShot(0, self._settle_layout)

    def _saved_order(self) -> list[str]:
        if self._layout_store is None or not self._layout_ns:
            return []
        return self._layout_store.order(self._layout_ns)

    def _settle_layout(self) -> None:
        """把这几个块**现在真实**的坐标写回记录（次序一个字不动）。

        没摆过（没有记录）就什么都不做——**不替用户建一份记录**，不然每张卡看一遍
        就多一个文件，而且「重置布局」会在一张根本没动过的卡上亮着。
        """
        if self._layout_store is None or not self._layout_ns:
            return
        if not self._saved_order():
            return
        self._layout_store.save(
            self._layout_ns, self.board.order(), self.board.positions(), self.board.keys()
        )

    def _on_layout_changed(self, _order: list) -> None:
        """松手了：把这次摆的样子记下来（次序 + 那一刻量到的坐标）。

        存的是"板子上真实摆着的次序"，不是信号里带的那一份——两边本来一样，
        但真出了岔子，记录要跟着**看得见的那张卡**走。
        """
        if self._layout_store is None or not self._layout_ns:
            return
        kept = self._layout_store.save(
            self._layout_ns, self.board.order(), self.board.positions(), self.board.keys()
        )
        self._reset_button.setVisible(kept)

    def _reset_layout(self) -> None:
        """「重置布局」：把记录删掉，几块东西回到默认次序。"""
        if self._layout_store is None or not self._layout_ns:
            return
        self._layout_store.clear(self._layout_ns)
        self.board.apply_order([])          # 空次序 → 补成默认次序
        self._reset_button.setVisible(False)

    def _bind_card_chat(self, card_id: int) -> None:
        """把这个小窗绑到这张卡上，并把**这张卡自己**聊过的话铺回去。

        同一张卡再点开不重铺：正等回话的那一句不能被一次重开冲掉。换到另一张卡时
        记录区是整片清掉再铺的——上一张卡的话一句都不许留在这儿。
        """
        card = self._service.card(card_id)
        if card is None:
            return
        namespace = card_namespace(LIFE_KIND, card.id)
        self._bind_card_tools(namespace)
        if self.card_chat.namespace == namespace:
            return
        history = self._chat_service.history(namespace) if self._chat_service else []
        self.card_chat.bind(namespace, card.title, history)

    def _bind_card_tools(self, namespace: str) -> None:
        """把这张卡的小部件铺到卡面上（v2-card-tools-B）。

        **每次开卡都重铺一遍**（不像卡窗那样"同一张卡就不重铺"）：卡上摆什么可能刚变过
        （加了 / 拿掉了），重铺一遍最稳；部件上没有"正等着的输入"会被冲掉。
        """
        components = self._chat_service.cards_tools(namespace) if self._chat_service else []
        state = self._chat_service.tools_state(namespace) if self._chat_service else {}
        self.card_tools.bind(namespace, components, state)
        # 部件区可能是刚从"收起来"变成"露出来"的（第一次长出部件），行得跟着放回来
        self.board.sync()

    # ---------- 卡上小部件的点击（v2-card-tools-B） ----------

    def _on_tool_run(self, namespace: str, text: str) -> None:
        """卡上按钮 / 自动化点了一下：把这句话当成你说的一句，送进这张卡的小窗。"""
        if not text or namespace != self.card_tools.namespace:
            return
        self.card_chat.submit_text(text)

    def _on_tool_fill(self, namespace: str, text: str) -> None:
        """快捷指令点了一下：把那句话填进输入框（不发送）。"""
        if not text or namespace != self.card_tools.namespace:
            return
        self.card_chat.fill(text)

    def _on_tool_note(self, namespace: str, text: str) -> None:
        """计时器到点：在卡窗里留一句交代（不调模型、不算你说的话）。"""
        if namespace != self.card_tools.namespace:
            return
        self.card_chat.append_note(text)

    def _on_tool_state(self, namespace: str, comp_id: str, value: object) -> None:
        """勾选 / 倒计时状态变了：记回这张卡（**只落这一份文件，不刷新界面**）。

        这里**故意不重铺**：重铺会把用户刚点的那一下整个重建一遍，勾选框会闪、
        清单里的滚动位置会跳。状态本来就是自己点出来的，界面上已经是新的了。
        """
        if self._chat_service is None or namespace != self.card_tools.namespace:
            return
        self._chat_service.set_tool_state(namespace, comp_id, value)

    def _on_tool_remove(self, namespace: str, comp_id: str) -> None:
        """卡上那个 × 点了一下：拿掉这个部件，然后把卡面重铺一遍。"""
        if self._chat_service is None or namespace != self.card_tools.namespace:
            return
        self._chat_service.remove_tool(namespace, comp_id)

    def _on_card_tools_changed(self, namespace: str) -> None:
        """（bootstrap 那边加了部件之后会走到这儿）重铺这张卡的部件区。"""
        if namespace == self.card_tools.namespace:
            self._bind_card_tools(namespace)

    def _on_card_chat_reply(self, namespace: str, text: str) -> None:
        """卡窗的回话到了。**不是这张卡的一律不接**——两道隔离里的界面那道。"""
        if namespace != self.card_chat.namespace:
            return
        self.card_chat.append_pet(text)

    def _on_card_chat_note(self, namespace: str, text: str) -> None:
        if namespace != self.card_chat.namespace:
            return
        self.card_chat.append_note(text)

    def refresh(self) -> None:
        """重新读一遍这张卡（打卡、改状态、改目标之后都会调到这里）。"""
        card = self._current_card()
        if card is None:
            return
        count = self._service.checkin_count(card.id)
        last = self._service.last_checkin_at(card.id)
        last_text = f" · 最近打卡 {last.strftime('%m-%d %H:%M')}" if last else ""

        self._loading = True
        self._title.setText(f"{card.icon} {card.title}" if card.icon else card.title)
        self._meta.setText(f"{card.theme} · 已打卡 {count} 次{last_text}")
        self._status.setCurrentText(CARD_STATUS_TITLES.get(card.status, card.status))
        # 下拉里显示"真的在说话的那位"：出行卡默认是导游、饮食卡默认是营养主厨
        # （卡上没显式绑过角色时，服务按主题给一个默认的，见 CardService.persona_of）
        index = self._persona.findData(self._service.persona_of(card))
        if index >= 0:
            self._persona.setCurrentIndex(index)
        # 出行卡才显示"目的地 + 日期"，并把上次填的回显出来
        self._trip_row.setVisible(is_trip_card(card))
        if is_trip_card(card):
            self._trip_destination.setText(card.trip_destination or "")
            if card.trip_date is not None:
                self._trip_date.setDate(
                    QDate(card.trip_date.year, card.trip_date.month, card.trip_date.day)
                )
        self._loading = False

        self._apply_features(card)
        self._refresh_guide_profile(card)
        self._refresh_profile_panes(card)
        # 建议区 / 出行那一行按卡藏了或露了，行跟着对上（见 ui/card_layout.py 的 sync）
        self.board.sync()

    # ---------- 操作 ----------

    def _current_card(self) -> Card | None:
        if self._card_id is None:
            return None
        return self._service.card(self._card_id)

    def _apply_features(self, card: Card) -> None:
        """只显示这张卡开了的能力（卡牌的 AI 能力配置）。"""
        features = card.feature_list()
        for feature, widget in (
            (FEATURE_GUIDE, self._guide_button),
            (FEATURE_ADVISE, self._advise_button),
            (FEATURE_CHECKIN, self._checkin_button),
            (FEATURE_REMIND, self._remind_button),
            (FEATURE_REMIND, self._remind_time),
        ):
            widget.setVisible(feature in features)

    def _refresh_guide_profile(self, card: Card) -> None:
        """把对象攻略卡那八格「对方档案」铺到卡头上（v2-social-card-uiread）。**只读**。

        读端就一个（`life/cards/chat.py::guide_profile_lines`），卡窗那格 `data` 用的是
        同一个——两处露的字一定一样，免得又出现"写在一个地方、读在另一个地方"的断链。
        八格全空的卡整块收着（其余八类卡这八列本来就是 NULL）。
        """
        lines = guide_profile_lines(card)
        self._guide_pane.setVisible(bool(lines))
        self._guide_pane.setText("\n".join(lines))

    def _refresh_profile_panes(self, card: Card) -> None:
        """两半卡（指令 26-C C1/C2）：出行 / 工作 / 餐饮卡都亮「上半建议 + 下半工具」。

        开关关着、或不是这三类卡时两块都收起来。画像只以建议形式呈现，不暴露原始字段。

        上半这一框除了建议流没别的东西（对话区已删），没建议可写时整框收掉——
        否则会剩一个只有「建议」俩字的白框。
        """
        enabled = self._config.life_cards.profile_tools_enabled
        trip = is_trip_card(card)
        diet = is_diet_card(card)
        work = card.theme == WORK_THEME
        has_advice = enabled and (trip or work or diet)
        self._upper.setVisible(has_advice)
        self._advice_pane.setVisible(has_advice)
        self._tool_pane.setVisible(enabled and (trip or diet or work))
        self._clear_tool_pane()
        if not enabled or not (trip or diet or work):
            return
        if trip:
            profile = self._profile_store.get(TRAVEL)
            category = CATEGORY_TRAVEL
        elif work:
            profile = self._profile_store.get(WORK)
            category = CATEGORY_WORK
        else:
            profile = self._profile_store.get(DIET)
            category = CATEGORY_DIET

        # 出行 / 餐饮这两张卡：上半框交给专业角色（导游 / 营养主厨）现说一段（指令 27）。
        # 认的是主题而不是减脂词——「工作」主题但卡名叫「饮食」的卡也命中减脂词，
        # 光看 is_diet_card 会去请一个服务根本不接的角色，框就永远停在"正在想…"。
        if self._service.has_pro_advice(card):
            self._request_advice(card, profile)
        elif work:
            self._advice_pane.setPlainText(strip_action_marks(work_advice(profile)))
        else:
            # 走到这的是"标题/目标里带减脂词、但主题不是餐饮"的卡：上半框照样亮着，
            # 却没有专业角色接。不补这一段的话框里会留着上一张卡的文字。
            self._advice_pane.setPlainText(strip_action_marks(diet_advice(profile)))

        for tool in match_tools(category):
            self._add_tool_button(tool, profile)

    def _request_advice(self, card: Card, profile: dict[str, str]) -> None:
        """让服务去请这张卡的专业角色说一段（后台，回来了走 card_advice）。

        一次打开只请一次：打卡、改目标这些也会触发 refresh，不能每刷一次就调一次模型。
        换卡时 show_card 会把 _advice_card_id 清掉，所以同一张卡再点开仍会重出。
        """
        if self._advice_card_id == card.id:
            return
        self._advice_card_id = card.id
        self._advice_pane.setPlainText(ADVICE_WAITING)
        self._service.advise_card(card.id, profile)

    def _on_advice(self, card_id: int, text: str) -> None:
        """专业角色的建议到了：只填当前这张卡的框，别的卡的不往这儿塞。"""
        if card_id != self._card_id:
            return
        # 渲染出口兜底：不管这段话是谁送来的（模型、规则版、危机话术），进框前统一剥动作描写。
        self._advice_pane.setPlainText(strip_action_marks(text))

    def _clear_tool_pane(self) -> None:
        while self._tool_layout.count():
            item = self._tool_layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _add_tool_button(self, tool, profile: dict[str, str]) -> None:
        btn = QPushButton(f"{tool.icon} {tool.title}", self._tool_pane)
        btn.setToolTip(tool.blurb)
        btn.clicked.connect(lambda _=False, tid=tool.id, prof=profile: self._run_tool(tid, prof))
        self._tool_layout.addWidget(btn)

    def _run_tool(self, tool_id: str, profile: dict[str, str]) -> None:
        """点工具：弹一句建议（默认建议 + 人确认，不自动下单/支付）。"""
        self._bus.toast.emit(f"🧰 {tool_suggest(tool_id, profile)}")

    def _save_status(self, title: str) -> None:
        if self._loading or self._card_id is None:
            return
        status = next((key for key, text in CARD_STATUS_TITLES.items() if text == title), None)
        if status is not None:
            self._service.set_status(self._card_id, status)

    def _save_persona(self) -> None:
        """换这张卡的陪执行角色（指令 14）。

        换了人就让上半框重出一段：同一句话由导游说和由本体说不是一回事。
        （清在 set_persona 之前——它会同步发 card_changed、当场走到 refresh。）
        """
        if self._loading or self._card_id is None:
            return
        self._advice_card_id = None
        self._service.set_persona(self._card_id, str(self._persona.currentData() or ""))

    def _checkin(self) -> None:
        if self._card_id is None:
            return
        # 打卡次数在卡头 meta 上（走 card_changed → refresh），鼓励的话由宠物说
        self._set_busy(True)
        self._service.checkin(self._card_id)

    def _ask(self, action: str) -> None:
        if self._card_id is None:
            return
        self._set_busy(True)
        self._service.ask(action, self._card_id)

    def _plan_trip(self) -> None:
        """链 a：填了目的地与日期，就让服务去联网规划这一趟。"""
        if self._card_id is None:
            return
        destination = self._trip_destination.text().strip()
        if not destination:
            self._bus.toast.emit("先写上要去哪儿，我才好查")
            return
        picked = self._trip_date.date()
        trip_date = datetime(picked.year(), picked.month(), picked.day(), TRIP_HOUR, 0)
        self._bus.toast.emit(f"{trip_date.strftime('%m-%d')} 去{destination}，这就去查")
        self._set_busy(True)
        self._service.plan_trip(self._card_id, destination, trip_date)

    def _share(self) -> None:
        """生成分享码亮出来（码里只有标题 / 主题 / 目标 / 能力开关）。"""
        if self._card_id is None:
            return
        code = self._service.share(self._card_id)
        if not code:
            self._bus.toast.emit("这张卡没能生成分享码")
            return
        dialog = ShareDialog(code, self)
        dialog.exec()
        dialog.deleteLater()

    def _save_image(self) -> None:
        """把这张卡画成图片存下来（发朋友圈用）。"""
        card = self._current_card()
        if card is None:
            return
        last = self._service.last_checkin_at(card.id)
        pixmap = render_card_image(
            card,
            self._service.checkin_count(card.id),
            last.strftime("%m-%d %H:%M") if last else "",
        )
        folder = QStandardPaths.writableLocation(QStandardPaths.DesktopLocation)
        default = f"{folder}/{card.title}.png" if folder else f"{card.title}.png"
        path, _selected = QFileDialog.getSaveFileName(
            self, "把这张卡存成图片", default, "PNG 图片 (*.png)"
        )
        if not path:
            return
        if pixmap.save(path, "PNG"):
            self._bus.toast.emit(f"图片存好了：{path}")
        else:
            self._bus.toast.emit("图片没能保存，换个位置再试试")

    def _set_reminder(self) -> None:
        if self._card_id is None:
            return
        picked = self._remind_time.time()
        at = datetime.combine(
            datetime.now().date(), dt_time(picked.hour(), picked.minute())
        )
        if at <= datetime.now():
            at += timedelta(days=1)      # 今天这个点已经过了，就当成明天的
        if self._service.create_reminder(self._card_id, at):
            self._bus.toast.emit(f"好，{at.strftime('%m-%d %H:%M')} 我会提醒你这件事")
        else:
            self._bus.toast.emit("这条提醒没设上，换个时间再试试")

    # ---------- 回话落地 ----------

    def _on_reply(self, card_id: int, text: str) -> None:
        """这张卡的回话到了：只是把「在想…」的锁解开。

        话本身不在这儿显示——同一段文字服务已经走 pet_speak 交给宠物说了。
        """
        if card_id != self._card_id:
            return          # 别的卡的回话，不管这一页
        self._set_busy(False)

    def _set_busy(self, busy: bool) -> None:
        for button in (
            self._guide_button,
            self._advise_button,
            self._checkin_button,
            self._trip_button,
        ):
            button.setEnabled(not busy)
