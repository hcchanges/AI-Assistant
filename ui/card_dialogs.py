"""
卡牌相关的小对话框：手动建卡、邀请确认、卡册、生成分享码、导入分享码。

几个都很小，凑在一个文件里，免得面板目录里散落一堆几十行的文件。
界面风格沿用 PANEL_QSS，和面板一致。

这里一律只负责"问"：需要改数据的动作（建卡、拉回、换下）都由面板拿去调 CardService，
对话框自己不碰业务。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from life.cards.templates import (
    CARD_ICONS,
    CARD_TITLE_MAX,
    LIFE_CATEGORIES,
    PERSONAL_ACTIVE_MAX,
    category_icon,
)
from ui.theme import PANEL_QSS


class CardFormDialog(QDialog):
    """手动建一张卡（指令 13 第 6 节；指令 25-A 第 3 项改成三步）。

    ① 选大类（8 选 1）→ ② 卡名（默认就是大类名，可改，≤10 字）→ ③ 图标 + 一句话描述。
    换大类时，卡名与图标如果还停在上一类的默认值，就跟着换；用户自己改过就不动他的。
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("建一张自己的卡")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel(
            f"建好就摆进生活广场。个人卡位一共 {PERSONAL_ACTIVE_MAX} 张，满了先收一张进卡册。",
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self._category = QComboBox(self)
        self._category.addItems(LIFE_CATEGORIES)
        form.addRow("① 选大类", self._category)

        self._title = QLineEdit(self)
        self._title.setMaxLength(CARD_TITLE_MAX)
        form.addRow(f"② 卡名（最多 {CARD_TITLE_MAX} 字）", self._title)

        self._icon = QComboBox(self)
        self._icon.addItems(CARD_ICONS)
        form.addRow("③ 图标", self._icon)

        self._description = QLineEdit(self)
        self._description.setPlaceholderText("一句话说明这张卡是干嘛的（可留空）")
        form.addRow("③ 描述", self._description)
        layout.addLayout(form)

        self._error = QLabel("", self)
        self._error.setObjectName("MutedLabel")
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("建好了")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        # 先按第一个大类把默认卡名与默认图标摆好，再开始跟着大类变
        self._last_category = LIFE_CATEGORIES[0]
        self._title.setText(self._last_category)
        self._select_icon(category_icon(self._last_category))
        self._category.currentTextChanged.connect(self._on_category_changed)

    def _on_category_changed(self, category: str) -> None:
        if self._title.text().strip() in ("", self._last_category):
            self._title.setText(category)
        if self._icon.currentText() == category_icon(self._last_category):
            self._select_icon(category_icon(category))
        self._last_category = category

    def _select_icon(self, icon: str) -> None:
        index = self._icon.findText(icon)
        if index >= 0:
            self._icon.setCurrentIndex(index)

    def _on_accept(self) -> None:
        if not self._title.text().strip():
            self._error.setText("给这张卡起个名字吧")
            return
        self.accept()

    def values(self) -> tuple[str, str, str, str]:
        """(大类, 卡名, 图标, 一句话描述)。"""
        return (
            self._category.currentText(),
            self._title.text().strip(),
            self._icon.currentText(),
            self._description.text().strip(),
        )


class CardOfferDialog(QDialog):
    """邀请受理的确认框（指令 25-B 第 2 项）：卡名可编辑，默认就是品类名。

    用户可以把默认的「出行」当场改成「通勤」；图标默认取该大类的图标，也能换。
    确认了才真建卡，取消当作用户改主意（记一次拒绝）。
    """

    def __init__(self, category: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._category = category
        self.setWindowTitle("给它做一张卡吧")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(380)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel(
            f"你最近老提「{category}」这件事，我给它做张卡吧。卡名可以改，比如改成「通勤」。",
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        form = QFormLayout()
        self._title = QLineEdit(category, self)
        self._title.setMaxLength(CARD_TITLE_MAX)
        form.addRow(f"卡名（最多 {CARD_TITLE_MAX} 字）", self._title)

        self._icon = QComboBox(self)
        self._icon.addItems(CARD_ICONS)
        index = self._icon.findText(category_icon(category))
        if index >= 0:
            self._icon.setCurrentIndex(index)
        form.addRow("图标", self._icon)
        layout.addLayout(form)

        self._error = QLabel("", self)
        self._error.setObjectName("MutedLabel")
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("就这么建")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._title.text().strip():
            self._error.setText("给这张卡起个名字吧")
            return
        self.accept()

    def values(self) -> tuple[str, str]:
        """(卡名, 图标)。品类由发起邀请时定死了，这里不改。"""
        return self._title.text().strip(), self._icon.currentText()


class CardBookDialog(QDialog):
    """卡册（归档区，指令 25-A 第 4 项）：收起来、不占轮播的卡，随时能拉回。

    只负责"问"：选中一张点「拉回」，把 (拉回哪张, 换下哪张) 交给调用方去改数据。
    位子满了（活跃个人卡已 4 张）就先弹一个选卡窗，让用户挑一张换下来。
    """

    def __init__(
        self,
        rows: list[tuple[int, str, str]],
        active_rows: list[tuple[int, str]],
        full: bool,
        parent: QWidget | None = None,
    ):
        """rows: 卡册里的卡（id, 主行, 副行）；active_rows: 轮播上那几张个人卡（id, 主行）。"""
        super().__init__(parent)
        self._rows = list(rows)
        self._active_rows = list(active_rows)
        self._full = full
        self._choice: tuple[int, int | None] | None = None

        self.setWindowTitle("卡册")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel(
            "收进卡册的卡不在轮播上，但打卡记录都留着。想让它回来，选中它点「拉回」。"
            + ("\n卡位满了：拉回前会先让你挑一张换下来。" if full else ""),
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget(self)
        self._list.setWordWrap(True)
        for card_id, main, sub in self._rows:
            item = QListWidgetItem(f"{main}\n{sub}")
            item.setData(1, card_id)
            self._list.addItem(item)
        if self._rows:
            self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.button(QDialogButtonBox.Close).setText("关掉")
        buttons.rejected.connect(self.reject)
        restore_button = QPushButton("拉回轮播", self)
        restore_button.setObjectName("PrimaryButton")
        restore_button.clicked.connect(self._restore)
        buttons.addButton(restore_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)

    def _restore(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        card_id = int(item.data(1))
        if not self._full:
            self._choice = (card_id, None)
            self.accept()
            return
        swap = SwapCardDialog(self._active_rows, self)
        picked = swap.chosen() if swap.exec() == QDialog.Accepted else None
        swap.deleteLater()
        if picked is None:
            return
        self._choice = (card_id, picked)
        self.accept()

    def choice(self) -> tuple[int, int | None] | None:
        """(要从卡册拉回的卡, 要收进卡册的卡或 None)。没选就返回 None。"""
        return self._choice


class SwapCardDialog(QDialog):
    """挑一张收进卡册：拉回时位子满了，一换一（指令 25-A 第 4 项）。"""

    def __init__(self, rows: list[tuple[int, str]], parent: QWidget | None = None):
        super().__init__(parent)
        self._rows = list(rows)
        self._picked: int | None = None

        self.setWindowTitle("挑一张收进卡册")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(360)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel("轮播上已经 4 张了。选一张收进卡册，腾出位子给要拉回的那张。", self)
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._list = QListWidget(self)
        self._list.setWordWrap(True)
        for card_id, main in self._rows:
            item = QListWidgetItem(main)
            item.setData(1, card_id)
            self._list.addItem(item)
        if self._rows:
            self._list.setCurrentRow(0)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("就收这张")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        item = self._list.currentItem()
        if item is None:
            return
        self._picked = int(item.data(1))
        self.accept()

    def chosen(self) -> int | None:
        return self._picked


class ShareDialog(QDialog):
    """把这张卡的分享码亮出来，方便复制给别人。"""

    def __init__(self, code: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("分享这张卡")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel(
            "把下面这段发给朋友，他在自己的宠物里点「导入分享码」粘贴就能加入广场。\n"
            "这段码里只有标题、主题、目标和能力开关，没有你的记忆和聊天内容。",
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._code = QPlainTextEdit(code, self)
        self._code.setReadOnly(True)
        self._code.setMinimumHeight(120)
        layout.addWidget(self._code)

        buttons = QDialogButtonBox(QDialogButtonBox.Close, parent=self)
        buttons.button(QDialogButtonBox.Close).setText("关闭")
        buttons.rejected.connect(self.reject)
        buttons.accepted.connect(self.accept)
        copy_button = QPushButton("复制", self)
        copy_button.setObjectName("PrimaryButton")
        copy_button.clicked.connect(self._copy)
        buttons.addButton(copy_button, QDialogButtonBox.ActionRole)
        layout.addWidget(buttons)
        self._copied = QLabel("", self)
        self._copied.setObjectName("MutedLabel")
        layout.addWidget(self._copied)

    def _copy(self) -> None:
        self._code.selectAll()
        self._code.copy()
        self._copied.setText("已经复制到剪贴板了")

    def code(self) -> str:
        return self._code.toPlainText()


class ImportShareDialog(QDialog):
    """粘贴别人给的分享码，加进自己的广场。"""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("导入分享码")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(420)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel("把朋友发来的那段码整段粘贴到下面。", self)
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._code = QPlainTextEdit(self)
        self._code.setPlaceholderText("（在这里粘贴）")
        self._code.setMinimumHeight(120)
        layout.addWidget(self._code)

        self._error = QLabel("", self)
        self._error.setObjectName("MutedLabel")
        layout.addWidget(self._error)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel, parent=self
        )
        buttons.button(QDialogButtonBox.Ok).setText("加进广场")
        buttons.button(QDialogButtonBox.Cancel).setText("算了")
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _on_accept(self) -> None:
        if not self._code.toPlainText().strip():
            self._error.setText("还没粘贴内容")
            return
        self.accept()

    def code(self) -> str:
        return self._code.toPlainText()
