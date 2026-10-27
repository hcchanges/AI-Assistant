"""
工作卡片详情页（指令 16 第 4、5 节）：本次两张卡 —— BOM 物料优化、爆品卖点预测。

两张卡都是"上传/输入 → 大模型联网检索 → 出结论 + 真实来源"，所以放一个页面里、
用 QStackedWidget 切换，共用一个结果回来刷新的套路（core/events.py 的两条工作信号）。

界面只做展示与转发，联网与计算都在 work/service.py、work/chain.py。
不爬虫：来源链接一律取厂商联网检索返回的 search_info，模型没标注依据就写"未给出处"。

卡底还嵌着这张工作卡自己的 `CardChatPanel`（v2-card-tools-A）：它在卡号 `work:<key>`
底下一份记录里说话，不跟 BOM / 趋势这些页面上的东西搭线，也不跟全局聊天和别的卡串
（见 `life/cards/chat.py` 开头那三条隔离）。

文件下半部分（指令 20 ⑥、指令 21）是"部门卡点开后的选择页"：
部门里的每个卡位排成一张卡片，可上下滚动；空卡位（画「＋」）点了新建自建卡，
自建卡右键能编辑 / 删除，系统功能卡（BOM / 卖点）没有这两个入口。
"""

from __future__ import annotations

import html
from pathlib import Path
from typing import Callable

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    Property,
    QPropertyAnimation,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from core.events import EventBus
from life.cards.chat import WORK_KIND, CardChatService, card_namespace
from life.cards.layout import (
    BLOCK_BODY,
    BLOCK_CHAT,
    BLOCK_HEAD,
    BLOCK_WIDGETS,
    CardLayoutStore,
)
from ui.card_chat import CardChatPanel
from ui.card_layout import RESET_HINT, RESET_TEXT, CardBoard
from ui.card_tools import CardToolStrip
from ui.panels.carousel import ADD_LABEL, SLIDE_MS, WHEEL_PIXELS, WHEEL_STEP
from ui.theme import (
    CARD_SUB_SPACING,
    CARD_SUB_WEIGHT,
    CARD_TITLE_SPACING,
    CARD_TITLE_WEIGHT,
    PANEL_QSS,
    card_font,
)
from work.bom import COLUMN_TITLES, BomError, BomTable
from work.schemas import Alternative, AlternativeResult, TrendReport
from work.service import (
    CUSTOM_CARD_ICONS,
    CUSTOM_CARD_MAX,
    CUSTOM_TITLE_MAX,
    PERIODS,
    PLATFORMS,
    SlotView,
    WorkCardError,
    WorkService,
)

BOM_FILTER = "BOM 文件 (*.csv *.xlsx *.xlsm *.txt)"

# BOM 表格列：前几列来自解析结果，然后一列写状态，最后一列放"找替代"按钮
BOM_COLUMNS = ("code", "name", "spec", "qty", "price")
BOM_STATUS_COLUMN = len(BOM_COLUMNS)
BOM_BUTTON_COLUMN = BOM_STATUS_COLUMN + 1

PAGE_BOM = 0
PAGE_TREND = 1

# 卡片页面名（work/cards.py 的 page_of）→ 堆里的下标
PAGE_OF = {"bom": PAGE_BOM, "trend": PAGE_TREND}


class BomCardPage(QWidget):
    """卡片① BOM 物料优化（研发部）。"""

    def __init__(self, bus: EventBus, service: WorkService, parent: QWidget | None = None):
        super().__init__(parent)
        self._bus = bus
        self._service = service
        self._row_of_index: dict[int, int] = {}      # BOM 行号 → 表格行号
        self._current_index = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        top = QHBoxLayout()
        upload = QPushButton("上传 BOM（Excel / CSV）", self)
        upload.setObjectName("PrimaryButton")
        upload.clicked.connect(self._pick_file)
        top.addWidget(upload)
        self._file_hint = QLabel("还没上传。上传后会自动识别列头。", self)
        self._file_hint.setObjectName("MutedLabel")
        self._file_hint.setWordWrap(True)
        top.addWidget(self._file_hint, 1)
        layout.addLayout(top)

        self._table = QTableWidget(0, BOM_BUTTON_COLUMN + 1, self)
        self._table.setHorizontalHeaderLabels(
            [COLUMN_TITLES[column] for column in BOM_COLUMNS] + ["替代建议", ""]
        )
        self._table.verticalHeader().setVisible(False)
        self._table.setEditTriggers(QTableWidget.NoEditTriggers)
        self._table.setSelectionBehavior(QTableWidget.SelectRows)
        self._table.setSelectionMode(QTableWidget.SingleSelection)
        self._table.itemSelectionChanged.connect(self._on_row_selected)
        layout.addWidget(self._table, 1)

        self._candidate_hint = QLabel("", self)
        self._candidate_hint.setObjectName("MutedLabel")
        self._candidate_hint.setWordWrap(True)
        layout.addWidget(self._candidate_hint)

        self._candidates = QListWidget(self)
        self._candidates.setWordWrap(True)
        self._candidates.setMinimumHeight(90)
        layout.addWidget(self._candidates, 1)

        self._comment = QLabel("", self)
        self._comment.setObjectName("MutedLabel")
        self._comment.setWordWrap(True)
        self._comment.setStyleSheet("border-top: 1px solid #eadfd2; padding-top: 6px;")
        layout.addWidget(self._comment)

        actions = QHBoxLayout()
        accept = QPushButton("接受选中的替代", self)
        accept.clicked.connect(self._accept_selected)
        actions.addWidget(accept)
        ignore = QPushButton("这一行不用替代", self)
        ignore.clicked.connect(self._ignore_current)
        actions.addWidget(ignore)
        actions.addStretch(1)
        self._saving = QLabel("", self)
        self._saving.setObjectName("MutedLabel")
        actions.addWidget(self._saving)
        export = QPushButton("导出优化后 BOM", self)
        export.setObjectName("PrimaryButton")
        export.clicked.connect(self._export)
        actions.addWidget(export)
        layout.addLayout(actions)

        self._refresh_saving()

    # ---------- 上传 ----------

    def _pick_file(self) -> None:
        path, _selected = QFileDialog.getOpenFileName(self, "选择 BOM 文件", "", BOM_FILTER)
        if not path:
            return
        try:
            table = self._service.open_bom(path)
        except BomError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._fill_table(table)
        self._bus.toast.emit(f"已读入 {table.filename}，识别到 {len(table.recognized)} 列")

    def refresh(self) -> None:
        """把服务里已有的 BOM 重新摆上（换卡再回来时用，不丢状态）。"""
        if self._service.bom is not None:
            self._fill_table(self._service.bom)

    def apply_comment(self, text: str) -> None:
        """宠物点评一句：放在候选区末尾，跟报告主体分开。"""
        self._comment.setText(f"宠物点评：{text}")

    def _fill_table(self, table: BomTable) -> None:
        self._row_of_index.clear()
        self._candidates.clear()
        self._current_index = 0
        self._table.setRowCount(len(table.rows))
        for row_position, row in enumerate(table.rows):
            self._row_of_index[row.index] = row_position
            for column, key in enumerate(BOM_COLUMNS):
                self._table.setItem(row_position, column, QTableWidgetItem(row.cell(key)))
            status = QTableWidgetItem("没查")
            self._table.setItem(row_position, BOM_STATUS_COLUMN, status)
            button = QPushButton("找替代", self)
            button.setToolTip("联网检索这一行的替代料（不爬虫，走大模型联网）")
            button.clicked.connect(
                lambda _checked=False, index=row.index: self._find(index)
            )
            self._table.setCellWidget(row_position, BOM_BUTTON_COLUMN, button)
        missing = "、".join(COLUMN_TITLES[key] for key in table.missing)
        hint = f"{table.filename} · {len(table.rows)} 行 · 识别到 {len(table.recognized)} 列"
        if missing:
            hint += f"（没认出来：{missing}）"
        self._file_hint.setText(hint)
        self._candidate_hint.setText("选中一行、点它后面的「找替代」就能联网查。")
        self._refresh_saving()

    # ---------- 找替代 ----------

    def _find(self, row_index: int) -> None:
        position = self._row_of_index.get(row_index)
        if position is None:
            return
        button = self._table.cellWidget(position, BOM_BUTTON_COLUMN)
        if isinstance(button, QPushButton):
            button.setEnabled(False)
            button.setText("查询中…")
        self._set_status(position, "查询中…")
        self._service.find_alternatives(row_index)

    def apply_result(self, row_index: int, result: AlternativeResult | None) -> None:
        """service 的 work_alternatives 信号进来（主线程）。"""
        position = self._row_of_index.get(row_index)
        if position is None:
            return
        button = self._table.cellWidget(position, BOM_BUTTON_COLUMN)
        if isinstance(button, QPushButton):
            button.setEnabled(True)
            button.setText("找替代")

        if result is None:
            self._set_status(position, "不用替代")
        elif result.alternatives:
            self._set_status(position, f"{len(result.alternatives)} 条候选")
        else:
            self._set_status(position, "没找到替代")
        # 结果回来直接把这一行选中，用户不用再点一次
        self._table.selectRow(position)
        self._show_candidates(row_index)

    def _set_status(self, position: int, text: str) -> None:
        item = self._table.item(position, BOM_STATUS_COLUMN)
        if item is not None:
            item.setText(text)

    def _on_row_selected(self) -> None:
        positions = self._table.selectionModel().selectedRows()
        if not positions:
            return
        position = positions[0].row()
        for row_index, row_position in self._row_of_index.items():
            if row_position == position:
                self._show_candidates(row_index)
                return

    def _show_candidates(self, row_index: int) -> None:
        self._current_index = row_index
        self._candidates.clear()
        result = self._service.result_of(row_index)
        if result is None:
            self._candidate_hint.setText("这一行还没查过，点它右边的「找替代」。")
            return
        accepted = self._service.accepted_of(row_index)
        if not result.alternatives:
            note = result.notes or "模型没找到靠谱替代（它宁可说没有，也不该编一个）"
            self._candidate_hint.setText(f"{result.item_label}：{note}")
            return

        hint = f"{result.item_label}：{len(result.alternatives)} 条候选"
        if accepted is not None:
            hint += f" · 已接受「{accepted.model}」"
        if result.notes:
            hint += f" · {result.notes}"
        self._candidate_hint.setText(hint)

        for alternative in result.alternatives:
            mark = "【已接受】" if alternative is accepted else ""
            text = (
                f"{mark}{alternative.model}"
                f"　价格：{alternative.price or '未给出'}"
                f"　供应商：{alternative.vendor or '未给出'}\n"
                f"理由：{alternative.why or '未给出'}\n"
                f"来源：{alternative.source_text}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.UserRole, alternative)
            if alternative.source is not None:
                item.setToolTip(alternative.source.url)
            self._candidates.addItem(item)

    def _accept_selected(self) -> None:
        item = self._candidates.currentItem()
        if item is None or not self._current_index:
            self._bus.toast.emit("先在下面的候选里选一条")
            return
        alternative: Alternative = item.data(Qt.UserRole)
        self._service.accept(self._current_index, alternative)
        self._refresh_saving()

    def _ignore_current(self) -> None:
        if not self._current_index:
            self._bus.toast.emit("先在上面选一行物料")
            return
        self._service.ignore(self._current_index)
        self._refresh_saving()

    def _refresh_saving(self) -> None:
        saved, ratio = self._service.savings()
        if not saved:
            self._saving.setText("还没接受任何替代")
            return
        text = f"预计节省 {saved:,.2f} 元"
        if ratio is not None:
            text += f"（占整张 BOM 的 {ratio * 100:.1f}%）"
        self._saving.setText(text)

    def _export(self) -> None:
        path, _selected = QFileDialog.getSaveFileName(
            self, "导出优化后的 BOM", "bom_optimized.csv", "CSV 文件 (*.csv)"
        )
        if not path:
            return
        try:
            count = self._service.export(path)
        except BomError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"已导出 {count} 行到 {Path(path).name}")


class TrendCardPage(QWidget):
    """卡片② 爆品卖点预测（销售部）。"""

    def __init__(self, bus: EventBus, service: WorkService, parent: QWidget | None = None):
        super().__init__(parent)
        self._bus = bus
        self._service = service

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 10)
        layout.setSpacing(8)

        row = QHBoxLayout()
        self._category = QLineEdit(self)
        self._category.setPlaceholderText("品类 / 关键词，例如：露营折叠椅")
        self._category.returnPressed.connect(self._predict)
        row.addWidget(self._category, 1)

        self._platform = QComboBox(self)
        self._platform.addItems(PLATFORMS)
        row.addWidget(self._platform)

        self._period = QComboBox(self)
        self._period.addItems(PERIODS)
        self._period.setCurrentIndex(1)
        row.addWidget(self._period)

        self._run = QPushButton("开始预测", self)
        self._run.setObjectName("PrimaryButton")
        self._run.clicked.connect(self._predict)
        row.addWidget(self._run)
        layout.addLayout(row)

        self._hint = QLabel("联网检索这个平台上最近的潮流词与热度，给出卖点与依据。", self)
        self._hint.setObjectName("MutedLabel")
        self._hint.setWordWrap(True)
        layout.addWidget(self._hint)

        self._output = QTextBrowser(self)
        self._output.setOpenExternalLinks(True)
        layout.addWidget(self._output, 1)

        self._comment = QLabel("", self)
        self._comment.setObjectName("MutedLabel")
        self._comment.setWordWrap(True)
        self._comment.setStyleSheet("border-top: 1px solid #eadfd2; padding-top: 6px;")
        layout.addWidget(self._comment)

    def _predict(self) -> None:
        category = self._category.text().strip()
        if not category:
            self._bus.toast.emit("先写上要预测的品类或关键词")
            return
        self._run.setEnabled(False)
        self._run.setText("检索中…")
        self._hint.setText(f"正在联网检索「{category}」在{self._platform.currentText()}上的热度…")
        self._service.predict_trends(
            category, self._platform.currentText(), self._period.currentText()
        )

    def apply_report(self, report: TrendReport | None) -> None:
        self._run.setEnabled(True)
        self._run.setText("开始预测")
        if report is None:
            self._hint.setText("这次没拿到结果，可以再试一次。")
            return
        self._hint.setText(
            f"{report.category} · {report.platform} · {report.period} · "
            f"取回 {len(report.sources)} 条来源"
            + ("" if report.sources else "（这次一条来源都没取回，结论请谨慎看）")
        )
        self._comment.setText("")
        self._output.setHtml(_report_html(report))

    def apply_comment(self, text: str) -> None:
        """宠物点评一句：放在报告末尾、跟报告主体分开。"""
        self._comment.setText(f"宠物点评：{text}")


class WorkCardPanel(QWidget):
    """工作卡片详情页：按 key 切到对应的卡片界面。"""

    # 卡底那个小窗里说了一句（v2-card-tools-A）：(卡号, 用户说的话)。
    # 跟生活卡那张面板同一个口径——面板不碰模型、不碰库，只把话往上报。
    card_chat_requested = Signal(str, str)

    def __init__(
        self,
        bus: EventBus,
        service: WorkService,
        chat_service: CardChatService | None = None,
        layout_store: CardLayoutStore | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._service = service
        self._chat_service = chat_service
        # 卡面摆成什么样（v2-card-tools-C）：跟生活卡那边同一个记录层、同一套块号
        self._layout_store = layout_store
        self._layout_ns = ""

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)

        # 工作卡这几块也交给板子排（v2-card-tools-C）：左边一个把手，按住能换位置。
        self.board = CardBoard(self)
        layout.addWidget(self.board, 1)

        self._head = QLabel("", self)
        self._head.setObjectName("MutedLabel")
        self._head.setWordWrap(True)
        self._head.setContentsMargins(14, 8, 14, 0)
        # 卡头这一块除了标题，右边还挂着「重置布局」（**只在这张卡被摆过时才亮**）
        head_holder = QWidget(self)
        head_box = QHBoxLayout(head_holder)
        head_box.setContentsMargins(0, 0, 14, 0)
        head_box.addWidget(self._head, 1)
        self._reset_button = QPushButton(RESET_TEXT, head_holder)
        self._reset_button.setObjectName("LayoutReset")
        self._reset_button.setToolTip(RESET_HINT)
        self._reset_button.setVisible(False)
        self._reset_button.clicked.connect(self._reset_layout)
        head_box.addWidget(self._reset_button, 0, Qt.AlignTop)
        self.board.add_block(BLOCK_HEAD, head_holder)

        self._bom_page = BomCardPage(bus, service, self)
        self._trend_page = TrendCardPage(bus, service, self)
        self._stack = QStackedWidget(self)
        self._stack.addWidget(self._bom_page)
        self._stack.addWidget(self._trend_page)
        self.board.add_block(BLOCK_BODY, self._stack, stretch=1)

        # ---- 卡上长出来的小部件（v2-card-tools-B）----
        # 跟生活卡那边是同一块控件、同一套行为；没有部件时整块收起来。
        self.card_tools = CardToolStrip(self)
        # 交给板子的**是部件区自己**，不是外面那层只为留边距的外壳：部件区没有部件时
        # 会自己收起来，外面套一层的话它收起来了外壳还立着，卡上就留下一截空档和一个
        # 孤零零的把手。边距交给这一行的 margins（左边距 14 + 板子自己的间距）。
        self.board.add_block(BLOCK_WIDGETS, self.card_tools, margins=(14, 0, 14, 8))
        self.card_tools.run_requested.connect(self._on_tool_run)
        self.card_tools.fill_requested.connect(self._on_tool_fill)
        self.card_tools.note_requested.connect(self._on_tool_note)
        self.card_tools.state_changed.connect(self._on_tool_state)
        self.card_tools.remove_requested.connect(self._on_tool_remove)

        # ---- 卡底·卡内小助手（v2-card-tools-A）：这张工作卡自己的一个小窗 ----
        # 卡号用稳定的字符串 key（`work:bom_optimize`）——它不随数据库主键变，
        # 换台机器、重建库，同一张卡的记录还是同一份。
        self.card_chat = CardChatPanel(self)
        self.card_chat.send_requested.connect(self.card_chat_requested)
        # 交给板子的同样是这个小窗自己（边距走这一行的 margins，理由同上）
        self.board.add_block(BLOCK_CHAT, self.card_chat, margins=(14, 0, 14, 12))
        # 松手之后才知道"最后摆成什么样"（理由见生活卡那边同名信号）
        self.board.order_changed.connect(self._on_layout_changed)

        bus.work_alternatives.connect(self._on_alternatives)
        bus.work_trends.connect(self._on_trends)
        bus.work_comment.connect(self._on_comment)
        bus.card_chat_reply.connect(self._on_card_chat_reply)
        bus.card_chat_note.connect(self._on_card_chat_note)
        bus.card_tools_changed.connect(self._on_card_tools_changed)

    def show_card(self, key: str) -> None:
        card = self._service.workflow.card(key)
        if card is None:
            return
        self._head.setText(
            f"{card.title}　（{self._service.card_place(card)}）\n{self._service.card_blurb(key)}"
        )
        self._stack.setCurrentIndex(PAGE_OF.get(self._service.card_page(key), PAGE_BOM))
        # 切回来时把已有结果重新摆上（换卡不丢状态，BOM 也不用重传）
        self._bom_page.refresh()
        self._bind_card_chat(key, card.title)

    def _bind_card_chat(self, key: str, title: str) -> None:
        """把这个小窗绑到这张工作卡上，铺回**这张卡自己**聊过的话。

        同一张卡再点开不重铺（正等回话的那句不能被一次重开冲掉）；换卡则整片清掉再铺。
        """
        namespace = card_namespace(WORK_KIND, key)
        self._bind_card_tools(namespace)
        self._bind_card_layout(namespace)
        if self.card_chat.namespace == namespace:
            return
        history = self._chat_service.history(namespace) if self._chat_service else []
        self.card_chat.bind(namespace, title, history)

    def _bind_card_tools(self, namespace: str) -> None:
        """把这张工作卡的小部件铺上去（每次开卡都重铺，理由见生活卡那边同名方法）。"""
        components = self._chat_service.cards_tools(namespace) if self._chat_service else []
        state = self._chat_service.tools_state(namespace) if self._chat_service else {}
        self.card_tools.bind(namespace, components, state)
        # 部件区可能是刚从"收起来"变成"露出来"的（第一次长出部件），行得跟着放回来
        self.board.sync()

    # ---------- 卡面摆成什么样（v2-card-tools-C） ----------
    # 跟生活卡那边一套做法（同名方法），只有"这张卡是哪几块"不一样。

    def _bind_card_layout(self, namespace: str) -> None:
        """把这张卡上次摆的样子摆回来（没摆过就是默认次序，跟以前一模一样）。"""
        self._layout_ns = namespace
        saved = self._saved_order()
        self.board.apply_order(saved)
        self._reset_button.setVisible(bool(saved))
        QTimer.singleShot(0, self._settle_layout)

    def _saved_order(self) -> list[str]:
        if self._layout_store is None or not self._layout_ns:
            return []
        return self._layout_store.order(self._layout_ns)

    def _settle_layout(self) -> None:
        """摆开之后把每块**现在真实**的坐标写回记录（没摆过就不建记录）。"""
        if self._layout_store is None or not self._layout_ns:
            return
        if not self._saved_order():
            return
        self._layout_store.save(
            self._layout_ns, self.board.order(), self.board.positions(), self.board.keys()
        )

    def _on_layout_changed(self, _order: list) -> None:
        """松手了：把这次摆的样子记下来（次序 + 那一刻量到的坐标）。"""
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
        self.board.apply_order([])
        self._reset_button.setVisible(False)

    # ---------- 卡上小部件的点击（v2-card-tools-B） ----------

    def _on_tool_run(self, namespace: str, text: str) -> None:
        """卡上按钮 / 自动化点了一下：把这句话当成你说的一句，送进这张卡的小窗。"""
        if not text or namespace != self.card_tools.namespace:
            return
        self.card_chat.submit_text(text)

    def _on_tool_fill(self, namespace: str, text: str) -> None:
        if not text or namespace != self.card_tools.namespace:
            return
        self.card_chat.fill(text)

    def _on_tool_note(self, namespace: str, text: str) -> None:
        if namespace != self.card_tools.namespace:
            return
        self.card_chat.append_note(text)

    def _on_tool_state(self, namespace: str, comp_id: str, value: object) -> None:
        """勾选 / 倒计时状态变了：记回这张卡（不重铺界面，理由见生活卡那边同名方法）。"""
        if self._chat_service is None or namespace != self.card_tools.namespace:
            return
        self._chat_service.set_tool_state(namespace, comp_id, value)

    def _on_tool_remove(self, namespace: str, comp_id: str) -> None:
        if self._chat_service is None or namespace != self.card_tools.namespace:
            return
        self._chat_service.remove_tool(namespace, comp_id)

    def _on_card_tools_changed(self, namespace: str) -> None:
        if namespace == self.card_tools.namespace:
            self._bind_card_tools(namespace)

    def _on_card_chat_reply(self, namespace: str, text: str) -> None:
        """卡窗的回话到了。**不是这张卡的一律不接**（界面这道隔离）。"""
        if namespace != self.card_chat.namespace:
            return
        self.card_chat.append_pet(text)

    def _on_card_chat_note(self, namespace: str, text: str) -> None:
        if namespace != self.card_chat.namespace:
            return
        self.card_chat.append_note(text)

    def _on_alternatives(self, row_index: int, result) -> None:
        self._bom_page.apply_result(row_index, result)

    def _on_trends(self, report) -> None:
        self._trend_page.apply_report(report)

    def _on_comment(self, key: str, text: str) -> None:
        """宠物点评一句：按卡片 key 送到对应的页面末尾。"""
        if key == "bom_optimize":
            self._bom_page.apply_comment(text)
        elif key == "sales_trend":
            self._trend_page.apply_comment(text)


DEPT_CARD_HEIGHT = 220      # 选择页里每张职能卡的高度（指令 23 第 1 项：跟着部门卡一起放大、变宽松）
DEPT_SLOT_TITLE_PX = 16     # 选择页里卡名的字号（像素；字体与字重见 ui/theme.py，指令 25-F）
DEPT_SLOT_BRIEF_PX = 11     # 描述的字号（比卡名小一号）
COPIES = 3                  # 选择页的卡片排几份首尾相接（滚过一份就悄悄减掉一份，就滚不到头了）


class LoopScrollArea(QScrollArea):
    """选择页的滚动区：卡片排成 COPIES 份首尾相接，滚动量按"一圈（一份内容的高度）"取模、
    滚过一圈就悄悄减掉一圈，所以怎么滚都滚不到头——最后一张后面第一张从底下接上来，
    反过来滚就是第一张后面最后一张从顶上下来。

    滚动条收起来：列表永远滚不到头，它显示不出"还剩多少"，而且每滚一圈都会往回跳一下。
    卡面上的文字标签会把滚轮吃掉、又不往上冒泡（实测），所以这里用 watch_wheel()
    把子控件上的滚轮统一截到滚动区自己身上，保证鼠标停在哪张卡上都能滚。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._acc = 0.0                 # 滚轮累积角度，够一格才动
        self._cycle = 0.0               # 一份内容有多高（一圈）
        self._pitch = 1.0               # 滚轮一格走多远（一张卡 + 间距）
        self._phase = 0.0               # 当前卷到这一份内容里的哪儿
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._anim = QPropertyAnimation(self, b"phase", self)
        self._anim.setDuration(SLIDE_MS)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)

    def set_cycle(self, cycle: float, pitch: float) -> None:
        """告诉滚动区：一份内容有多高、滚轮一格走多远。"""
        self._cycle = float(cycle)
        self._pitch = max(1.0, float(pitch))

    def _get_phase(self) -> float:
        return self._phase

    def _set_phase(self, value: float) -> None:
        # 滚过一份就减掉一份：COPIES 份内容一模一样，减完画面丝毫不差，看不出来
        self._phase = float(value) % self._cycle if self._cycle > 0 else 0.0
        self.verticalScrollBar().setValue(int(round(self._phase)))

    # 卷到哪儿（滚动动画靠它插值）
    phase = Property(float, _get_phase, _set_phase)

    def reset(self) -> None:
        """从头看起（每次打开选择页都重来）。"""
        self._anim.stop()
        self._acc = 0.0
        self.phase = 0.0

    def watch_wheel(self, widget: QWidget) -> None:
        """盯住这块区域里的所有子控件（含以后新加的），把滚轮截到这里来。"""
        widget.installEventFilter(self)
        for child in widget.findChildren(QWidget):
            child.installEventFilter(self)

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Wheel:
            self.wheelEvent(event)
            return True
        return super().eventFilter(obj, event)

    def wheelEvent(self, event) -> None:  # noqa: N802
        angle = event.angleDelta().y()
        delta = angle if angle else event.pixelDelta().y()
        if self._cycle <= 0 or not delta:
            super().wheelEvent(event)
            return
        self._acc += delta / (WHEEL_STEP if angle else WHEEL_PIXELS)
        steps = int(self._acc)                     # 朝零取整：攒够一格才动
        if not steps:
            event.accept()
            return
        self._acc -= steps
        # 目标是"不折回"的：动画一路往一个方向走（滚过末尾就走到下一份的头），
        # 取相位时才减掉一圈。这样中途不会有"快滚回顶上"的那一下。
        target = (round(self._phase / self._pitch) - steps) * self._pitch
        self._anim.stop()
        self._anim.setStartValue(self._phase)
        self._anim.setEndValue(target)
        self._anim.start()
        event.accept()


class CustomCardFormDialog(QDialog):
    """新建 / 编辑一张自建卡（指令 21）：标题（必填，≤10 字）、描述（选填）、图标（预设里挑一个）。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        title: str = "",
        description: str = "",
        icon: str = CUSTOM_CARD_ICONS[0],
    ):
        super().__init__(parent)
        self._editing = bool(title)
        self.setWindowTitle("改这张卡" if self._editing else "新建卡片")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel("这张卡挂在你打开的那个部门里，一个部门最多 3 张。", self)
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self._title = QLineEdit(title, self)
        self._title.setMaxLength(CUSTOM_TITLE_MAX)
        self._title.setPlaceholderText("例如：入库排期")
        form.addRow(f"标题（最多 {CUSTOM_TITLE_MAX} 字）", self._title)

        self._description = QLineEdit(description, self)
        self._description.setPlaceholderText("一句话说明这张卡是干嘛的（可留空）")
        form.addRow("描述", self._description)

        self._icon = QComboBox(self)
        self._icon.addItems(CUSTOM_CARD_ICONS)
        if icon in CUSTOM_CARD_ICONS:
            self._icon.setCurrentIndex(CUSTOM_CARD_ICONS.index(icon))
        form.addRow("图标", self._icon)
        layout.addLayout(form)

        self._error = QLabel("", self)
        self._error.setObjectName("MutedLabel")
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("改好了" if self._editing else "建好了")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._title.text().strip():
            self._error.setText("给这张卡起个名字吧")
            return
        self.accept()

    def values(self) -> tuple[str, str, str]:
        """(标题, 描述, 图标)。"""
        return (
            self._title.text().strip(),
            self._description.text().strip(),
            self._icon.currentText(),
        )


class DeptSlotCard(QFrame):
    """选择页里的一张职能卡：点它才进这张卡的功能。

    空卡位（还没做出功能）也是一张卡：虚线框 + 「＋」，点了新建自建卡（指令 21）。
    自建卡（custom_id 不是 None）右键能编辑 / 删除；系统功能卡与空卡位都没有这两个入口。
    """

    clicked = Signal()
    edit_requested = Signal(int)
    delete_requested = Signal(int)

    def __init__(
        self,
        title: str,
        brief: str,
        empty: bool = False,
        custom_id: int | None = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._custom_id = custom_id
        self.setObjectName("DeptEmptySlot" if empty else "DeptSlotCard")
        self.setCursor(Qt.PointingHandCursor)
        # 高度定死：三份内容才长得一模一样，一圈的高度才是准的（滚动就靠它取模）
        self.setFixedHeight(DEPT_CARD_HEIGHT)

        layout = QVBoxLayout(self)
        # 卡名往上抬一点、描述位置不动（指令 25-G）：上面收窄 6px，
        # 中间的间距相应收一点，描述那行还落在原来的高度上
        layout.setContentsMargins(20, 12, 20, 18)
        layout.setSpacing(14)

        if empty:
            mark = QLabel(ADD_LABEL, self)
            mark.setObjectName("DeptSlotEmptyMark")
            mark.setAlignment(Qt.AlignCenter)
            layout.addWidget(mark, 1)
            return

        name = QLabel(title, self)
        name.setObjectName("DeptSlotTitle")
        name.setFont(
            card_font(DEPT_SLOT_TITLE_PX, CARD_TITLE_WEIGHT, CARD_TITLE_SPACING, pixel=True)
        )
        name.setWordWrap(True)
        layout.addWidget(name)

        if brief:
            note = QLabel(brief, self)
            note.setObjectName("DeptSlotBrief")
            note.setFont(
                card_font(DEPT_SLOT_BRIEF_PX, CARD_SUB_WEIGHT, CARD_SUB_SPACING, pixel=True)
            )
            note.setWordWrap(True)
            layout.addWidget(note)
        layout.addStretch(1)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.clicked.emit()
        super().mouseReleaseEvent(event)

    def contextMenuEvent(self, event) -> None:  # noqa: N802
        """右键：只有自建卡才有编辑 / 删除；系统功能卡与空卡位弹不出菜单。"""
        if self._custom_id is None:
            return
        menu = QMenu(self)
        menu.addAction("编辑").triggered.connect(
            lambda _checked=False: self.edit_requested.emit(self._custom_id)
        )
        menu.addAction("删除").triggered.connect(
            lambda _checked=False: self.delete_requested.emit(self._custom_id)
        )
        menu.exec(event.globalPos())
        menu.deleteLater()


class WorkDeptPanel(QWidget):
    """部门卡点开后的选择页：铺满窗口，把部门卡里的卡位排成一张张卡片、可上下滚动。

    点哪张卡片才进那张功能卡的详情（装配层负责换页），不在别处直接打开详情；
    空卡位点了新建自建卡，自建卡右键能编辑 / 删除（指令 21）。
    """

    def __init__(
        self,
        bus: EventBus,
        service: WorkService,
        on_card: Callable[[str], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._bus = bus
        self._service = service
        self._on_card = on_card
        self._dept_id = ""
        self._slots: list[DeptSlotCard] = []

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)

        self._title = QLabel("", self)
        self._title.setObjectName("TitleLabel")
        layout.addWidget(self._title)

        self._context = QLabel("", self)
        self._context.setObjectName("ModuleHint")
        self._context.setWordWrap(True)
        layout.addWidget(self._context)

        self._area = LoopScrollArea(self)
        self._area.setWidgetResizable(True)
        self._area.setFrameShape(QFrame.NoFrame)
        self._area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self._body = QWidget()
        self._body.setObjectName("DeptSlotBody")
        self._rows = QVBoxLayout(self._body)
        self._rows.setContentsMargins(0, 0, 8, 0)     # 右边留点空，别贴着滚动条
        self._rows.setSpacing(14)                     # 卡与卡之间也放宽（指令 23 第 1 项）
        self._rows.addStretch(1)
        self._area.setWidget(self._body)
        self._area.watch_wheel(self._body)
        layout.addWidget(self._area, 1)

        # 自建卡新建 / 编辑 / 删除之后，这个部门的卡片自己重铺一遍
        bus.work_cards_changed.connect(self._on_cards_changed)

    def show_dept(self, dept_id: str) -> None:
        """按部门重铺卡片（每次打开都重铺，功能卡与自建卡变了也能跟上）。

        同一批卡排 COPIES 份首尾相接：滚过一份就把滚动量悄悄减掉一份，于是永远滚不到头。
        """
        self._dept_id = dept_id
        # 标题只写部门名（指令 20 第 5 项：不再显示"几张功能卡 / 自建几张"这类计数）
        self._title.setText(self._service.dept_name(dept_id))
        nodes = self._service.nodes_of_dept(dept_id)
        self._context.setText(
            "参与的环节：" + " → ".join(node.name for node in nodes) if nodes else ""
        )
        self._clear_slots()
        slots = self._service.dept_slots(dept_id)
        for copy_index in range(COPIES):
            for slot in slots:
                card = self._make_slot_card(slot)
                self._rows.insertWidget(self._rows.count() - 1, card)
                if copy_index == 0:
                    self._slots.append(card)        # 第一份留着：界面与测试都认它
        step = DEPT_CARD_HEIGHT + self._rows.spacing()
        self._area.set_cycle(len(slots) * step, step)
        self._area.watch_wheel(self._body)          # 新排的卡片也归滚动区管滚轮
        self._area.reset()

    def _make_slot_card(self, slot: SlotView | None) -> DeptSlotCard:
        """按卡位造一张卡片：空卡位点了新建；功能卡点了进详情；自建卡右键能改能删。"""
        if slot is None:
            card = DeptSlotCard("", "", empty=True, parent=self._body)
            card.clicked.connect(self._new_card_clicked)
        elif slot.custom_id is None:
            card = DeptSlotCard(slot.label(), slot.brief, parent=self._body)
            card.clicked.connect(self._open_slot(slot.key))
        else:
            card = DeptSlotCard(
                slot.label(), slot.brief, custom_id=slot.custom_id, parent=self._body
            )
            card.clicked.connect(self._custom_card_clicked)
            card.edit_requested.connect(self._edit_custom_card)
            card.delete_requested.connect(self._delete_custom_card)
        return card

    def _clear_slots(self) -> None:
        while self._rows.count() > 1:                 # 末尾那个 stretch 留着
            item = self._rows.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._slots = []

    def _open_slot(self, card_key: str) -> Callable[[], None]:
        def open_card() -> None:
            self._on_card(card_key)

        return open_card

    def _on_cards_changed(self) -> None:
        """自建卡变了：把当前这个部门的卡片重新铺一遍。"""
        if self._dept_id:
            self.show_dept(self._dept_id)

    # ---------- 自建卡：新建 / 编辑 / 删除（指令 21） ----------

    def _new_card_clicked(self) -> None:
        """点「＋」或空卡位：没满就弹新建的窗，满了就提示先删一张。"""
        if self._service.custom_full(self._dept_id):
            self._bus.toast.emit(f"已满 {CUSTOM_CARD_MAX} 张，先删除一张")
            return
        dialog = CustomCardFormDialog(self)
        accepted = dialog.exec() == QDialog.Accepted
        values = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            made = self._service.add_custom_card(self._dept_id, *values)
        except WorkCardError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"建好了：「{made.title}」")

    def _custom_card_clicked(self) -> None:
        """自建卡还没有功能页，左键点了给一句提示，别让人以为坏了。"""
        self._bus.toast.emit("这张卡是你自己建的，右键可以编辑或删除")

    def _edit_custom_card(self, card_id: int) -> None:
        card = self._service.custom_card(card_id)
        if card is None:
            return
        dialog = CustomCardFormDialog(
            self, title=card.title, description=card.description, icon=card.icon
        )
        accepted = dialog.exec() == QDialog.Accepted
        values = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        try:
            self._service.update_custom_card(card_id, *values)
        except WorkCardError as exc:
            self._bus.toast.emit(str(exc))
            return
        self._bus.toast.emit(f"改好了：「{values[0]}」")

    def _delete_custom_card(self, card_id: int) -> None:
        card = self._service.custom_card(card_id)
        if card is None:
            return
        answer = QMessageBox.question(
            self,
            "删除自建卡",
            f"删除后不可恢复，确定删掉「{card.title}」？",
        )
        if answer != QMessageBox.Yes:
            return
        self._service.delete_custom_card(card_id)
        self._bus.toast.emit(f"删掉了：「{card.title}」")


def _report_html(report: TrendReport) -> str:
    """把卖点报告排成一段 HTML；来源是可点的真链接。"""
    escape = html.escape
    parts: list[str] = []
    if report.summary:
        parts.append(f"<p><b>{escape(report.summary)}</b></p>")

    if report.keywords:
        parts.append("<p><b>潮流词与热度</b></p><ul>")
        for keyword in report.keywords:
            parts.append(
                f"<li>{escape(keyword.word)}　热度：{escape(keyword.heat or '未给')}"
                f"　趋势：{escape(keyword.trend or '未给')}</li>"
            )
        parts.append("</ul>")

    if report.points:
        parts.append("<p><b>Top 卖点</b></p><ol>")
        for point in report.points:
            source = ""
            if point.source is not None:
                source = (
                    f"<br/>来源：<a href='{escape(point.source.url)}'>"
                    f"{escape(point.source.site or point.source.title)}</a>"
                )
            parts.append(
                f"<li>{escape(point.point)}<br/>依据：{escape(point.reason)}{source}</li>"
            )
        parts.append("</ol>")

    if report.advice:
        parts.append("<p><b>怎么用</b></p><ul>")
        for item in report.advice:
            parts.append(f"<li>{escape(item)}</li>")
        parts.append("</ul>")

    parts.append("<p><b>数据来源</b>（厂商联网检索返回，非模型自述）</p>")
    if report.sources:
        parts.append("<ul>")
        for source in report.sources:
            parts.append(
                f"<li>[{source.index}] <a href='{escape(source.url)}'>{escape(source.title)}</a>"
                f"　{escape(source.site)}</li>"
            )
        parts.append("</ul>")
    else:
        parts.append("<p>这次没取回来源链接。</p>")
    parts.append(f"<p>检索时间：{report.fetched_at.strftime('%Y-%m-%d %H:%M:%S')}</p>")
    return "".join(parts)
