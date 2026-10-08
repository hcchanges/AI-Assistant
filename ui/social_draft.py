"""对象攻略卡的建卡框（v2-social-reply-ext）。

**这里从头到尾没有"发送"。** 建卡框只给"保存并建卡"和"取消"——面板里没有会话选择、
没有窗口句柄、没有按键模拟。用户拿这段信息做什么，全在他自己手上。

正文是**能选中**的：对方信息要改——这里不做"改完带回来"的编辑器（可改，改完交给
装配层写进卡专属库，界面不碰库）。

**候选回复那个模态弹窗已经整档删掉了（v2-social-copy-clean）。** 原来这里是两个框：
候选框「这样回怎么样（回这条）」+ 这个建卡框。候选框的问题不是样式，是**它是模态的**：
`exec()` 跑的是嵌套事件循环，1.5 秒那个轮询照转，识别到新消息就又是一轮，真机上表现成
卡死 + 弹窗摞弹窗；而它承载的两件事（把候选亮出来 / 让用户复制）本来都不需要模态——
候选走宠物自己的非模态气泡，复制在生成那一刻就静默做完了（见
`core/bootstrap._on_social_candidates`）。于是整套创建 / 单例 / 关闭逻辑一并删除，
不再留任何入口。
"""

from __future__ import annotations

from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from ui.theme import PANEL_QSS


class GuideCardDialog(QDialog):
    """建对象攻略卡前，把抽出来的对方信息亮出来让用户改。**改完的交给装配层写库。**"""

    def __init__(self, profile: str, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("对象攻略卡 · 先看看抽对没有")
        self.setStyleSheet(PANEL_QSS)
        self.setMinimumWidth(480)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 16)
        layout.setSpacing(10)

        hint = QLabel(
            "下面是从你刚才说的话里抽出来的对方信息。\n"
            "看着不对的可以直接改，改完再保存——这份会存进这张卡的专属知识库。",
            self,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._editor = QPlainTextEdit(profile or "", self)
        self._editor.setMinimumHeight(160)
        layout.addWidget(self._editor)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel, parent=self)
        buttons.button(QDialogButtonBox.Cancel).setText("取消")
        buttons.rejected.connect(self.reject)
        save_button = QPushButton("保存并建卡", self)
        save_button.setObjectName("PrimaryButton")
        save_button.clicked.connect(self.accept)
        buttons.addButton(save_button, QDialogButtonBox.AcceptRole)
        layout.addWidget(buttons)

    def values(self) -> str:
        """用户（可能改过的）对方信息正文。"""
        return self._editor.toPlainText().strip()

    def profile(self) -> str:
        """框里现在这份正文（测试用）。"""
        return self._editor.toPlainText()


__all__ = ["GuideCardDialog"]
