"""
面板容器：生活 / 工作 两页共用一个窗口，"返回"回到上一级。

面板只有两个一级页面（生活、工作），番茄钟、游戏陪玩、卡牌详情是生活下的二级页面。
用系统原生标题栏（有关闭按钮、能最小化），不做无边框窗口，减少自绘窗口的坑；
因此"当前部门 / 管理员标识"这类标签只能写成标题栏文字（指令 20 第 4 项，见 set_window_note）。
"""

from __future__ import annotations

from PySide6.QtCore import (
    QAbstractAnimation,
    QEasingCurve,
    QTimer,
    QVariantAnimation,
    Qt,
)
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (
    QApplication,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QStackedWidget,
    QVBoxLayout,
    QWidget,
)

from ui.theme import PANEL_QSS

TOAST_MS = 6000
SCREEN_MARGIN = 24      # 窗口离屏幕边缘至少留这么多，免得贴着边看不见边框
PANEL_WIDTH = 560       # 面板宽度（指令 23 只让高度按页变；指令 24 补齐：整窗放大一档到 560）
HEIGHT_ANIM_MS = 180    # 换页时高度过渡时长（指令 23 第 2 项：150–200ms，不许瞬跳）
MIN_HEIGHT = 240        # 再窄的页面也得让标题栏 + 一行内容放得下
# 对话框最多占屏幕可用区的九成（指令 26 ①：设置窗在小屏上别再顶出屏幕）。
# 尺寸封顶之后内容放不下，由页面自己那层滚动区去接——不是把窗口撑大。
DIALOG_MAX_RATIO = 0.9
DIALOG_MIN_WIDTH = 360
DIALOG_MIN_HEIGHT = 320
# 每页打开时的窗口高度（指令 23 第 2 项）：
# 生活页要让大卡有地方；工作页与生活页同高（指令 25-C），部门卡跟着吃满这块高度、
# 不再留一片空白；没写在这张表里的页按内容算（标题栏 + 页面自己的 sizeHint）
PAGE_HEIGHTS = {"life": 700, "work": 700}


def keep_on_screen(window: QWidget) -> None:
    """把窗口收进当前屏幕的可用区（指令 19 ①）。

    小屏笔记本上，窗口按设计高度居中会顶到屏幕上方，标题栏（最小化/关闭）就跑出屏幕了。
    这里做两件事：尺寸压到屏幕可用区内，位置也夹回来，保证窗口整个落在某块屏幕里。
    """
    screen = (
        QGuiApplication.screenAt(window.frameGeometry().center())
        or QGuiApplication.primaryScreen()
    )
    area = screen.availableGeometry()
    width = min(window.width(), max(area.width() - SCREEN_MARGIN, 200))
    height = min(window.height(), max(area.height() - SCREEN_MARGIN, 200))
    if width != window.width() or height != window.height():
        window.resize(width, height)
    x = min(max(window.x(), area.left()), area.right() - window.width() + 1)
    y = min(max(window.y(), area.top()), area.bottom() - window.height() + 1)
    window.move(x, y)


def fit_dialog_on_screen(window: QWidget, ratio: float = DIALOG_MAX_RATIO) -> None:
    """对话框：先按屏幕九成封顶定尺寸，再摆到屏幕正中（指令 26 ①）。

    跟 keep_on_screen 是两回事。keep_on_screen 只管"别顶出屏幕"，尺寸全听窗口自己的；
    可窗口自己的最小尺寸是由**内容**推出来的——内容越高，窗口越高的那条底线就越抬，
    抬过屏高时 resize 会被 Qt 按最小值顶回去，于是底部连保存按钮一起掉到屏幕外，
    而且里面没有滚动区，够不着。

    所以这里换个次序：**先封顶**（宽高都不超过可用区的九成），**再居中**。
    尺寸封顶之后内容放不下由调用方那层的滚动区接住（见 ui/settings.py 的 _scrolled）。
    """
    screen = (
        QGuiApplication.screenAt(window.frameGeometry().center())
        or QGuiApplication.primaryScreen()
    )
    area = screen.availableGeometry()
    width = max(min(window.width(), int(area.width() * ratio)), DIALOG_MIN_WIDTH)
    height = max(min(window.height(), int(area.height() * ratio)), DIALOG_MIN_HEIGHT)
    window.resize(width, height)
    x = area.left() + (area.width() - width) // 2
    y = area.top() + (area.height() - height) // 2
    window.move(max(x, area.left()), max(y, area.top()))


class PanelWindow(QWidget):
    def __init__(self, pet_name: str, pages: dict[str, QWidget], parent: QWidget | None = None):
        super().__init__(parent, Qt.Window)
        self.setObjectName("PanelWindow")
        self.setStyleSheet(PANEL_QSS)
        self.resize(PANEL_WIDTH, PAGE_HEIGHTS["life"])
        self.setMinimumHeight(MIN_HEIGHT)     # 用户拖到再小也不至于把标题栏挤没

        self._titles = {
            "life": "生活模块",
            "pomodoro": "番茄钟",
            "playmate": "游戏陪玩",
            "work": "工作模块",
        }
        # 二级页面的"返回"去处；一级页面（不在这张表里）不显示返回按钮
        self._parents = {
            "pomodoro": "life",
            "playmate": "life",
        }
        self._pet_name = pet_name
        self._current = "life"
        self._notes: dict[str, str] = {}   # 每页附加在标题栏上的说明（部门名 / 管理员标识）
        # "放大定格"层（指令 17）：打开时铺满整个窗口，由装配层挂上来
        self._overlay: QWidget | None = None
        # 按页改高度用（指令 23 第 2 项）：
        # _auto_resize 标出"这一次尺寸正是我们自己要改的"；
        # _manual_size 一旦为真表示用户自己拖过窗口，之后不再自动改高度（判据见 _size_is_ours）
        self._auto_resize = False
        self._manual_size = False
        # 高度动画（指令 23 第 2 项）：只动高度，绝不碰位置。
        # 不用 QPropertyAnimation 直接改 geometry —— 实测（Windows + 150% 缩放）它会和窗口管理器
        # 打架：每切一次页窗口位置就被挪一点，高度动画偶尔干脆不推进，
        # 于是"打开工作页还是生活页那么高"。改成一个整数动画、回调里 resize()，位置全程不动。
        self._height_anim = QVariantAnimation(self)
        self._height_anim.setDuration(HEIGHT_ANIM_MS)
        self._height_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._height_anim.valueChanged.connect(self._apply_anim_height)
        self._height_anim.finished.connect(self._on_height_done)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        header = QHBoxLayout()
        header.setContentsMargins(14, 12, 14, 4)
        self._back_button = QPushButton("← 返回", self)
        self._back_button.clicked.connect(self._go_back)
        header.addWidget(self._back_button)
        self._page_title = QLabel("", self)
        self._page_title.setObjectName("TitleLabel")
        header.addWidget(self._page_title)
        header.addStretch(1)
        layout.addLayout(header)

        self._stack = QStackedWidget(self)
        # 窗口高度按页来（指令 23 第 2 项）：QStackedWidget 的最小高度默认取"所有页里最费地方的那一页"，
        # 番茄钟那一页会把窗口顶到 520+，工作页就压不到 450 了。这里把它放开，改由本页自己算高度。
        self._stack.setMinimumHeight(1)
        self._index: dict[str, int] = {}
        for key, page in pages.items():
            self._index[key] = self._stack.addWidget(page)
        layout.addWidget(self._stack, 1)

        self._toast = QLabel("", self)
        self._toast.setObjectName("MutedLabel")
        self._toast.setWordWrap(True)
        self._toast.setContentsMargins(16, 4, 16, 10)
        layout.addWidget(self._toast)

        self._toast_timer = QTimer(self)
        self._toast_timer.setSingleShot(True)
        self._toast_timer.timeout.connect(lambda: self._toast.setText(""))

        self.show_page("life")
        self._center_on_screen()
        self._apply_screen()

    # ---------- 对外 ----------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 屏幕变小/换屏之后，再打开时把窗口收回来，别让标题栏留在屏幕外
        self._apply_screen()

    def show_page(self, key: str) -> None:
        if key not in self._index:
            return
        self._current = key
        self._stack.setCurrentIndex(self._index[key])
        self._page_title.setText(self._titles.get(key, ""))
        self._back_button.setVisible(key in self._parents)
        self._apply_title()
        self._apply_page_height(key)

    def forget_manual_size(self) -> None:
        """把"用户自己拖过尺寸"这件事清零（面板每被打开一次就调一次）。

        "手动尺寸优先"只在**这一次停留**里算数：用户重新打开面板，就应该按目标页的规范高度来。
        不清的话，只要这次运行里他被拖动/最大化/吸附过一次窗口，之后换页就永远不再按页变高度。
        """
        self._manual_size = False

    def set_window_note(self, page: str, note: str) -> None:
        """给某一页在窗口标题栏上加一段说明（指令 20 第 4 项：部门名 / 管理员标识）。

        原生标题栏塞不进控件，只能拼文字；空串就是这一页不带说明。
        """
        self._notes[page] = note
        if page == self._current:
            self._apply_title()

    def _apply_title(self) -> None:
        parts = [self._pet_name, self._titles.get(self._current, "")]
        note = self._notes.get(self._current, "")
        if note:
            parts.append(note)
        self.setWindowTitle(" · ".join(part for part in parts if part))

    def show_toast(self, text: str) -> None:
        self._toast.setText(text)
        self._toast_timer.start(TOAST_MS)

    def apply_pet_name(self, pet_name: str) -> None:
        self._pet_name = pet_name
        self.show_page(self._current)

    def set_overlay(self, overlay: QWidget) -> None:
        """挂上"放大定格"层（指令 17）：它打开时铺满整个窗口。"""
        self._overlay = overlay

    # ---------- 内部 ----------

    def closeEvent(self, event) -> None:  # noqa: N802
        """标题栏 ✕ 关掉面板：定格层还开着就先把它收干净。

        不收的话 "宠物让位" 这件事没人解除（关闭只认卡片自己的 ✕ / Esc 那条路），
        桌宠就一直藏着了——关了面板它也不回来。
        """
        closer = getattr(self._overlay, "close_overlay", None)
        if callable(closer):
            closer(immediate=True)
        super().closeEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        # 不是我们安排的尺寸，就当用户自己拖过：以后不再按页改高度（指令 23 第 2 项）
        if not self._size_is_ours():
            self._manual_size = True
        # 窗口被拖动缩放时，定格中的卡片也跟着铺满
        if self._overlay is not None and self._overlay.isVisible():
            self._overlay.setGeometry(self.rect())

    def _size_is_ours(self) -> bool:
        """当前的尺寸是我们自己安排的吗。

        不能只靠"此刻有没有设标记"来判断：窗口第一次露面时，系统会补发一次尺寸事件，
        而我们的高度动画每一帧也在改尺寸——只按标记判，这两种都会被当成"用户手动拖过"，
        一旦记上，之后换页就再也不改高度了（重启后打开工作页还是生活页那个高度，就是这么来的）。
        所以真正的判据是**结果对不对**：高度等于这一页该有的高度，就不是用户拖的。
        """
        if not self.isVisible():
            return True                     # 还没露面，谈不上用户改尺寸
        if self._auto_resize:
            return True                     # 正由 _apply_page_height / _apply_screen 改
        if self._height_anim.state() == QAbstractAnimation.Running:
            return True                     # 高度动画的中间帧
        return self.height() == self._target_height(self._current)

    def _apply_page_height(self, key: str) -> None:
        """按页给高度（指令 23 第 2 项）：锚定顶部、平滑过渡，用户手动拖过就不再动。"""
        if self._manual_size:
            return
        target = self._target_height(key)
        if target == self.height():
            return
        self._auto_resize = True
        self._height_anim.stop()
        self._height_anim.setStartValue(self.height())
        self._height_anim.setEndValue(target)
        self._height_anim.start()

    def _apply_anim_height(self, height: object) -> None:
        """动画每一帧：只改高度，左上角不动，所以是"顶部锚定"。"""
        self.resize(self.width(), int(height))

    def _on_height_done(self) -> None:
        self._auto_resize = False

    def _target_height(self, key: str) -> int:
        """这一页该多高：表里写死的照表，没写的按"标题栏 + 页面自己的内容"算。"""
        if key in PAGE_HEIGHTS:
            height = PAGE_HEIGHTS[key]
        else:
            page = self._stack.widget(self._index[key])
            height = self._chrome_height() + page.sizeHint().height()
        return self._fit_to_screen(height)

    def _chrome_height(self) -> int:
        """页面之外那些东西（标题行 + 底部提示条）占的高度。"""
        return max(0, self.height() - self._stack.height())

    def _fit_to_screen(self, height: int) -> int:
        """仍然过屏幕校验（指令 19 ①）：顶部不动，把高度压进屏幕可用区。"""
        screen = (
            QGuiApplication.screenAt(self.frameGeometry().center())
            or QGuiApplication.primaryScreen()
        )
        area = screen.availableGeometry()
        return int(
            max(MIN_HEIGHT, min(height, area.bottom() - self.y() + 1, area.height() - SCREEN_MARGIN))
        )

    def _apply_screen(self) -> None:
        """收进屏幕可用区；这一步是我们自己改的尺寸，别被记成"用户手动拖过"。

        高度动画还在跑的时候不把标记清掉：清掉之后动画的每一帧都会被当成用户拖尺寸，
        换页自适应当场作废。那种情况下交给动画自己的 finished 去清。
        """
        self._auto_resize = True
        keep_on_screen(self)
        if self._height_anim.state() != QAbstractAnimation.Running:
            self._auto_resize = False

    def _go_back(self) -> None:
        self.show_page(self._parents.get(self._current, "life"))

    def _center_on_screen(self) -> None:
        area = QGuiApplication.primaryScreen().availableGeometry()
        self.move(
            area.left() + (area.width() - self.width()) // 2,
            area.top() + (area.height() - self.height()) // 2,
        )
