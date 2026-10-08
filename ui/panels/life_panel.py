"""
生活模块面板（指令 22 改版；指令 25-A 调轮播构成）。

打开就是大卡轮播：一屏一张、卡片占满轮播区（约 70–80% 屏高），
滚轮 / 左右方向键轮动、中间那张略大、松手吸附——用的还是指令 17 那套 CardCarousel。
轮播只剩生活卡：预设 2 张（饮食 / 出行）→ 活跃个人卡（≤4 张）→ 玄学卡。
番茄钟 / 游戏陪玩 / 玄机先生那三张功能入口卡已全部下架（玄机先生更早一轮随玄学页下线，
另外两张本轮按「删三卡」摘掉）。功能页与业务代码都还在，只是轮播上不再出现入口。
收进卡册的卡不在这里，点底部「卡册」去看。
点生活卡：放大定格到卡详情（装配层负责）。
卡右上角有个 X（v2-card-del）：轻确认两下就**永久删掉**这张卡（跟"收进卡册"那条路并存，
卡册是归档、X 是删除）；删完发 card_changed，轮播自己重排。

生活页不再有聊天（指令 22 第 2 项）：消息区、输入框、「说给它听」整块删掉，
想说话就点宠物本体——聊天一直在它旁边那个气泡里（指令 7 修正一）。
底部留「+ 手动建一张」「卡册」「导入分享码」和「已收集 N 张」。

这里只做展示与转发，业务都在 life/chatter.py、life/cards/service.py、memory/store.py 里。
"""

from __future__ import annotations

from typing import Callable

from PySide6.QtCore import QRect
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from core.config import Config
from core.events import EventBus
from life.cards.service import CardService, is_personal_card, is_preset_card
from life.cards.templates import CARD_STATUS_TITLES
from ui.card_dialogs import CardBookDialog, CardFormDialog, ImportShareDialog
from ui.panels.carousel import CardCarousel, CarouselCard
from ui.theme import card_theme_color

INTRO_TEMPLATE = "认领你的生活卡牌，{pet_name}到点会主动喊你。"

# 大卡宽 / 轮播区宽（指令 23 第 4 项）：留出两侧各 ≥24px 的边，让邻卡露个边不被挡死
LIFE_CARD_RATIO = 0.74


class LifePanel(QWidget):
    def __init__(
        self,
        config: Config,
        bus: EventBus,
        card_service: CardService,
        on_card: Callable[[int, QRect], None],
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._card_service = card_service
        self._on_card = on_card

        self._build_ui()
        self.refresh_cards()

    # ---------- 界面搭建 ----------

    def _build_ui(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 18, 18, 18)
        layout.setSpacing(10)

        self._intro = QLabel(INTRO_TEMPLATE.format(pet_name=self._config.pet.name), self)
        self._intro.setObjectName("MutedLabel")
        self._intro.setWordWrap(True)
        layout.addWidget(self._intro)

        # 大卡轮播：卡片占满这块的高度（fill=True）、宽度留边（card_ratio，两侧各露 ≥24px 邻卡边缘），
        # 滚轮 / 左右键轮动，中间那张略大 —— 生活页的主体就是它
        self._carousel = CardCarousel(
            f"还没有卡片。多跟{self._config.pet.name}说几次同一件事，它就会提议给你做一张。",
            self,
            card_ratio=LIFE_CARD_RATIO,
            fill=True,
        )
        self._carousel.card_activated.connect(self._open_card)
        # 卡右上角的 X（v2-card-del）：轻确认点两下才发出来，这儿负责真删 + 说一声
        self._carousel.card_close_requested.connect(self._delete_card)
        layout.addWidget(self._carousel, 1)

        bottom = QHBoxLayout()
        manual_button = QPushButton("+ 手动建一张", self)
        manual_button.setToolTip("选大类、起个名字、挑个图标，建好就上轮播")
        manual_button.clicked.connect(self._create_card_manually)
        bottom.addWidget(manual_button)

        self._book_button = QPushButton("卡册", self)
        self._book_button.setToolTip("收起来不占轮播的卡，随时能拉回来")
        self._book_button.clicked.connect(self._open_card_book)
        bottom.addWidget(self._book_button)

        import_button = QPushButton("导入分享码", self)
        import_button.setToolTip("粘贴朋友发来的分享码，把他的卡加进你的广场")
        import_button.clicked.connect(self._import_shared_card)
        bottom.addWidget(import_button)

        bottom.addStretch(1)
        self._wall_hint = QLabel("", self)
        self._wall_hint.setObjectName("MutedLabel")
        bottom.addWidget(self._wall_hint)
        layout.addLayout(bottom)

    # ---------- 卡牌轮播 ----------

    def refresh_cards(self) -> None:
        """重排轮播（指令 25-A 的终态）：

        预设 2 张 → 活跃个人卡（≤4 张）→ 玄学卡。收进卡册的卡不在其中（plaza() 只给活跃卡）。

        番茄钟 / 游戏陪玩两张功能入口卡已下架（用户要求：删三卡）。
        按"只删 UI 入口、功能留着"的边界，focus/pomodoro.py、life/playmate.py
        那些功能代码一行没动，只是轮播上不再出现这两张卡。
        """
        rows = self._card_service.plaza()
        preset = [row for row in rows if is_preset_card(row[0])]
        personal = [row for row in rows if is_personal_card(row[0])]
        others = [row for row in rows if not is_preset_card(row[0]) and not is_personal_card(row[0])]
        self._carousel.set_cards(
            [self._life_card(card, count) for card, count in preset]
            + [self._life_card(card, count) for card, count in personal]
            + [self._life_card(card, count) for card, count in others]
        )
        self._wall_hint.setText(f"已收集 {len(rows)} 张")
        self._book_button.setText(f"卡册（{len(self._card_service.card_book())}）")

    @staticmethod
    def _life_card(card, count: int) -> CarouselCard:
        return CarouselCard(
            key=card.id,
            title=f"{card.icon} {card.title}" if card.icon else card.title,
            theme=card.theme,
            status=f"{CARD_STATUS_TITLES.get(card.status, card.status)} · 打卡 {count} 次",
            accent=card_theme_color(card.theme),
            hint=card.goal or card.summary or "",
            closable=True,     # 生活卡都能删（X 在右上角，轻确认两下）
        )

    def _open_card(self, key: object, rect: QRect) -> None:
        """点中轮播中间那张卡：交给装配层放大定格。"""
        self._on_card(int(key), rect)

    def _delete_card(self, key: object) -> None:
        """X 被确认了：把这张卡永久删掉（v2-card-del）。

        删完 card_changed 会走回 refresh_cards，轮播自己重排，这儿不用手动刷。
        弹一句 toast 是有意的：删除不可撤销，得让人看见"确实删的是这张"。
        """
        card = self._card_service.card(int(key))
        title = self._card_line(card) if card is not None else f"#{key}"
        if self._card_service.delete_card(int(key)):
            self._bus.toast.emit(f"删掉了「{title}」")
        else:
            self._bus.toast.emit("这张没删掉，再试一次")

    # ---------- 对外 ----------

    def apply_pet_name(self, pet_name: str) -> None:
        self._intro.setText(INTRO_TEMPLATE.format(pet_name=pet_name))

    # ---------- 操作 ----------

    def _create_card_manually(self) -> None:
        """建一张自己的卡（指令 13 第 6 节；指令 25-A 第 3 项改成三步）。"""
        if self._card_service.personal_full():
            self._bus.toast.emit("个人卡位满了，先去卡册收一张起来再建")
            return
        dialog = CardFormDialog(self)
        accepted = dialog.exec() == QDialog.Accepted
        category, title, icon, description = dialog.values()
        dialog.deleteLater()
        if not accepted:
            return
        made = self._card_service.create_manual(category, title, icon, description)
        self._bus.toast.emit(f"建好了：「{made}」" if made else "这张卡没建成，看看卡位是不是满了")

    def _open_card_book(self) -> None:
        """卡册（指令 25-A 第 4 项）：看收起来的卡，或者拉回一张。

        拉回时位子满 4 张的话，对话框会先问"收哪一张起来"，这里只管按它说的做。
        """
        rows = self._card_service.card_book()
        if not rows:
            self._bus.toast.emit("卡册是空的")
            return
        dialog = CardBookDialog(
            rows=[self._book_row(card, count) for card, count in rows],
            active_rows=[
                (card.id, self._card_line(card))
                for card, _count in self._card_service.plaza()
                if is_personal_card(card)
            ],
            full=self._card_service.personal_full(),
            parent=self,
        )
        accepted = dialog.exec() == QDialog.Accepted
        choice = dialog.choice()
        dialog.deleteLater()
        if not accepted or choice is None:
            return
        restore_id, archive_id = choice
        if archive_id is not None and not self._card_service.archive_card(archive_id):
            self._bus.toast.emit("那张没被收起来，卡位还是满的")
            return
        if self._card_service.restore_card(restore_id):
            self._bus.toast.emit("拉回来了，去轮播上找它")
        else:
            self._bus.toast.emit("没能拉回来，再试一次吧")

    @staticmethod
    def _card_line(card) -> str:
        return f"{card.icon} {card.title}（{card.theme}）" if card.icon else f"{card.title}（{card.theme}）"

    def _book_row(self, card, count: int) -> tuple[int, str, str]:
        """卡册里一行：主行是卡名，副行是分类 + 打卡次数 + 最近一次（历史查得到）。"""
        subtitle = f"{card.theme} · 打卡 {count} 次"
        last = self._card_service.last_checkin_at(card.id)
        if last is not None:
            subtitle += f" · 最近 {last.strftime('%m-%d %H:%M')}"
        return card.id, self._card_line(card), subtitle

    def _import_shared_card(self) -> None:
        """导入别人给的分享码。"""
        if self._card_service.personal_full():
            self._bus.toast.emit("个人卡位满了，先去卡册收一张起来再导入")
            return
        dialog = ImportShareDialog(self)
        accepted = dialog.exec() == QDialog.Accepted
        code = dialog.code()
        dialog.deleteLater()
        if not accepted:
            return
        made = self._card_service.import_share(code)
        self._bus.toast.emit(
            f"已加进广场：「{made}」" if made else "这段分享码看不懂，看看有没有复制全"
        )
