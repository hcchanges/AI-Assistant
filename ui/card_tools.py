"""
卡上长出来的小部件（v2-card-tools-B）：卡窗里说一句"加个喝水提醒"，卡面上就多一个。

**这一层只管把部件摆出来、把点击接住**——部件是什么、能不能用、记在哪儿，全在
`life/cards/tools.py`（白名单与规范化）和 `life/cards/chat.py`（生成与落盘）。
摆在这儿的东西**一个都不认识模型、数据库和文件**：它只会发三个信号：

| 信号 | 谁点的 | 卡上要发生什么 |
| --- | --- | --- |
| `run_requested(卡号, 话)` | 按钮 / 自动化 | 把这句话当成你说的一句，送进这张卡的小窗 |
| `fill_requested(卡号, 话)` | 快捷指令 | 把这句常说的话**填进输入框**（不发送，你还能改） |
| `note_requested(卡号, 话)` | 计时器到点 | 在卡窗里留一句居中灰字的交代（不调模型、不算你说的话） |
| `state_changed(卡号, 部件号, 状态)` | 勾选 / 倒计时 | 把状态记回这张卡（只有这一份文件） |
| `remove_requested(卡号, 部件号)` | 每个部件右上角的 × | 从卡上拿掉它 |

**能点的都在卡里**：这些信号没有一个通向 pet.db、系统提醒、shell 或文件系统。
哪怕模型在 props 里写了句命令，它最后也只是一段被当成文字显示的字符串
（`normalize_components` 那道白名单还会先把它丢掉）。

**卡上没有部件时整块收起来**：`bind()` 收到空表就 `hide()`，老卡的卡面跟以前一模一样，
不多一条空框。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from life.cards.tools import (
    TYPE_AUTOMATION,
    TYPE_BUTTON,
    TYPE_LIST,
    TYPE_REMINDER,
    TYPE_SHORTCUT,
    TYPE_TIMER,
)

#: 每个部件右上角那个 × 说什么（鼠标悬停时）
REMOVE_HINT = "从这张卡上拿掉它（列表里勾的东西也一起没）"
#: 倒计时按秒走时按钮上的写法
TIMER_IDLE = "▶ {label} · {minutes:02d}:00"

_TOOL_QSS = (
    "QWidget#CardTools {"
    "  background-color: #f6f2ea;"
    "  border: 1px solid #e6ddcf;"
    "  border-radius: 10px;"
    "}"
)


class CardToolStrip(QWidget):
    """卡面上那一排小部件。**同一时刻只绑一张卡**（卡面本来就一次只开一张）。"""

    run_requested = Signal(str, str)        # (卡号, 送进卡窗的一句话)
    fill_requested = Signal(str, str)       # (卡号, 填进输入框的一句话)
    note_requested = Signal(str, str)       # (卡号, 卡窗里的一句交代)
    state_changed = Signal(str, str, object)  # (卡号, 部件号, 状态)
    remove_requested = Signal(str, str)     # (卡号, 部件号)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("CardTools")
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet(_TOOL_QSS)

        self._namespace = ""
        self._components: list[dict] = []
        self._checks: dict[str, QCheckBox] = {}     # 提醒：部件号 → 那个勾选框
        self._lists: dict[str, list[QCheckBox]] = {}  # 列表：部件号 → 每条的勾选框
        self._timers: dict[str, _Timer] = {}        # 计时器：部件号 → 那个倒计时

        self._layout = QVBoxLayout(self)
        self._layout.setContentsMargins(10, 8, 10, 10)
        self._layout.setSpacing(6)

    # ---------- 对外 ----------

    @property
    def namespace(self) -> str:
        return self._namespace

    def bind(self, namespace: str, components: list[dict], state: dict) -> None:
        """绑到这张卡上：把它的小部件整个重铺一遍。

        换卡是**整片清掉再铺**——上一张卡的部件一个都不许留在这儿（跟卡窗一个道理）。
        到点还没响的倒计时会跟着停掉（它属于上一张卡）。
        """
        self._stop_timers()
        self._namespace = namespace
        self._components = [c for c in components if isinstance(c, dict)]
        self._checks.clear()
        self._lists.clear()
        self._clear_rows()
        for comp in self._components:
            self._layout.addWidget(self._build_row(comp, state if isinstance(state, dict) else {}))
        if not self._components:
            self.hide()
        else:
            self.show()

    def components(self) -> list[dict]:
        """现在摆着的部件（测试与外面看"卡上到底有什么"用）。"""
        return list(self._components)

    # ---------- 铺一行 ----------

    def _clear_rows(self) -> None:
        while self._layout.count():
            item = self._layout.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.deleteLater()

    def _build_row(self, comp: dict, state: dict) -> QWidget:
        kind = comp.get("type")
        props = comp.get("props") if isinstance(comp.get("props"), dict) else {}
        comp_id = str(comp.get("id") or "")
        label = str(comp.get("label") or "")
        row = QWidget(self)
        box = QHBoxLayout(row)
        box.setContentsMargins(0, 0, 0, 0)
        box.setSpacing(6)
        if kind == TYPE_LIST:
            box.addWidget(self._build_list(row, comp_id, label, props, state))
        elif kind == TYPE_REMINDER:
            box.addWidget(self._build_reminder(row, comp_id, label, props, state))
        elif kind == TYPE_TIMER:
            box.addWidget(self._build_timer(row, comp_id, label, props))
        else:                                   # 按钮 / 快捷指令 / 自动化
            box.addWidget(self._build_button(row, kind, label, props))
        box.addStretch(1)
        box.addWidget(self._build_remove(row, comp_id))
        return row

    def _build_button(self, parent: QWidget, kind: object, label: str, props: dict) -> QPushButton:
        button = QPushButton(f"▶ {label}", parent)
        button.setCursor(Qt.PointingHandCursor)
        if kind == TYPE_SHORTCUT:
            button.setToolTip("点一下，把这句话填进下面的输入框（还能改）")
            text = str(props.get("text") or "")
            button.clicked.connect(lambda: self.fill_requested.emit(self._namespace, text))
        elif kind == TYPE_AUTOMATION:
            button.setToolTip("点一下，把准备好的这句话送进这张卡的小窗")
            prompt = str(props.get("prompt") or "")
            button.clicked.connect(lambda: self.run_requested.emit(self._namespace, prompt))
        else:
            button.setToolTip("点一下，把这句话送进这张卡的小窗")
            button.clicked.connect(lambda: self.run_requested.emit(self._namespace, label))
        return button

    def _build_reminder(
        self, parent: QWidget, comp_id: str, label: str, props: dict, state: dict
    ) -> QCheckBox:
        box = QCheckBox(f"{props.get('at') or ''} {label}".strip(), parent)
        raw = state.get(comp_id)
        # 先摆好勾、再接信号：反过来的话"铺回上次的勾选"本身会被当成一次新的点击
        box.setChecked(bool(raw.get("done")) if isinstance(raw, dict) else False)
        box.toggled.connect(lambda done: self._save(comp_id, {"done": bool(done)}))
        box.setToolTip("在这张卡上勾掉（不会去动系统的提醒）")
        self._checks[comp_id] = box
        return box

    def _build_timer(self, parent: QWidget, comp_id: str, label: str, props: dict) -> QPushButton:
        minutes = int(props.get("minutes") or 0)
        button = QPushButton(TIMER_IDLE.format(label=label, minutes=minutes), parent)
        button.setCursor(Qt.PointingHandCursor)
        timer = _Timer(self, button, comp_id, label, minutes * 60)
        timer.finished.connect(self._on_timer_done)
        button.clicked.connect(timer.toggle)
        self._timers[comp_id] = timer
        return button

    def _build_list(
        self, parent: QWidget, comp_id: str, label: str, props: dict, state: dict
    ) -> QWidget:
        holder = QWidget(parent)
        col = QVBoxLayout(holder)
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)
        title = QLabel(label, holder)
        title.setObjectName("ToolListTitle")
        col.addWidget(title)
        raw = state.get(comp_id)
        checked = set(raw.get("checked") or []) if isinstance(raw, dict) else set()
        boxes: list[QCheckBox] = []
        for index, text in enumerate(props.get("items") or []):
            item = QCheckBox(str(text), holder)
            item.setChecked(index in checked)
            item.toggled.connect(lambda _on, i=index: self._save_list(comp_id, i))
            col.addWidget(item)
            boxes.append(item)
        self._lists[comp_id] = boxes
        return holder

    def _build_remove(self, parent: QWidget, comp_id: str) -> QPushButton:
        button = QPushButton("×", parent)
        button.setObjectName("ToolRemove")
        button.setFixedWidth(22)
        button.setCursor(Qt.PointingHandCursor)
        button.setToolTip(REMOVE_HINT)
        # 这一下会让卡面重铺，也就是把这一行（连同这个按钮自己）拆掉。
        # **能这么写是因为拆用的是 `deleteLater()`**——真实删除推到下一轮事件循环，
        # 这一轮的 clicked 早就返回了。要是哪天 `_clear_rows` 改成同步 delete，
        # 这里就会变成"在自己的信号里删自己"，得跟着往后挪一拍。
        button.clicked.connect(
            lambda: self.remove_requested.emit(self._namespace, comp_id)
        )
        return button

    # ---------- 状态 ----------

    def _save(self, comp_id: str, value: object) -> None:
        self.state_changed.emit(self._namespace, comp_id, value)

    def _save_list(self, comp_id: str, index: int) -> None:
        """整条清单的勾选状态一起记：勾一条就重算一遍，读回来时不会只剩最后一下。"""
        boxes = self._lists.get(comp_id) or []
        checked = [i for i, box in enumerate(boxes) if box.isChecked()]
        self._save(comp_id, {"checked": checked})

    def _on_timer_done(self, comp_id: str, label: str) -> None:
        """倒计时到点：在卡窗里留一句交代（**不调模型、不算用户说的话**）。"""
        self._save(comp_id, {"done": True})
        self.note_requested.emit(self._namespace, f"「{label}」到点了。")

    def _stop_timers(self) -> None:
        for timer in self._timers.values():
            timer.stop()
        self._timers.clear()


class _Timer(QObject):
    """卡上的一个倒计时按钮。**只在界面里数秒**：不设系统提醒、不写任何文件。

    再点一下停、走完在卡窗里交代一句。到点后的"已完成"由调用方记进卡上的状态里，
    所以重开卡面时按钮上是"过去了"而不是又从头数——但它**不会**在桌宠关着的时候响
    （那是系统级提醒的事，这条路不做，见 life/cards/tools.py 开头）。
    """

    finished = Signal(str, str)     # (部件号, 标签)

    def __init__(
        self, parent: QWidget, button: QPushButton, comp_id: str, label: str, seconds: int
    ):
        super().__init__(parent)
        self._button = button
        self._comp_id = comp_id
        self._label = label
        self._total = max(1, seconds)
        self._left = self._total
        self._clock = QTimer(self)
        self._clock.setInterval(1000)
        self._clock.timeout.connect(self._tick)

    @property
    def running(self) -> bool:
        return self._clock.isActive()

    def toggle(self) -> None:
        if self.running:
            self.stop()
        else:
            self._left = self._total
            self._clock.start()
            self._paint()

    def stop(self) -> None:
        self._clock.stop()
        self._left = self._total
        self._paint()

    def _tick(self) -> None:
        self._left -= 1
        if self._left <= 0:
            self._clock.stop()
            self._left = self._total
            self._paint()
            self.finished.emit(self._comp_id, self._label)
            return
        self._paint()

    def _paint(self) -> None:
        if self.running:
            self._button.setText(f"⏸ {self._label} · {self._mmss()}")
        else:
            self._button.setText(TIMER_IDLE.format(label=self._label, minutes=self._total // 60))

    def _mmss(self) -> str:
        return f"{self._left // 60:02d}:{self._left % 60:02d}"


__all__ = ["REMOVE_HINT", "TIMER_IDLE", "CardToolStrip"]
