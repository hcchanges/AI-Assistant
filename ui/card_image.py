"""
把一张卡画成图片（对应指令 13 第 7 节的"卡片图"分享形式）。

纯本地绘制、不联网；图里只有卡牌本身的东西（标题 / 主题 / 目标 / 打卡进度），
和分享码一样不含记忆与对话原文。
"""

from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPixmap

from data.models import Card
from life.cards.templates import CARD_STATUS_TITLES
from ui.theme import ACCENT, BG, BORDER, TEXT, TEXT_MUTED

WIDTH = 560
HEIGHT = 320
SCALE = 2               # 先按两倍画再缩回去，线条和字才不毛
CORNER = 20
PADDING = 30
TAIL = 10               # 圆角外留一圈空白，免得边框被裁掉


def render_card_image(card: Card, checkin_count: int, last_checkin: str) -> QPixmap:
    """把卡画成一张可以直接保存或发出去的图。"""
    pixmap = QPixmap((WIDTH + TAIL * 2) * SCALE, (HEIGHT + TAIL * 2) * SCALE)
    pixmap.fill(Qt.transparent)

    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.scale(SCALE, SCALE)
    _paint(painter, card, checkin_count, last_checkin)
    painter.end()
    return pixmap.scaled(
        (WIDTH + TAIL * 2), (HEIGHT + TAIL * 2), Qt.KeepAspectRatio, Qt.SmoothTransformation
    )


def _paint(painter: QPainter, card: Card, checkin_count: int, last_checkin: str) -> None:
    body = QRectF(TAIL, TAIL, WIDTH, HEIGHT)
    painter.setPen(QColor(BORDER))
    painter.setBrush(QColor(BG))
    painter.drawRoundedRect(body, CORNER, CORNER)

    left = int(body.left() + PADDING)
    width = int(body.width() - PADDING * 2)

    # 主题标签
    painter.setPen(QColor(ACCENT))
    painter.setFont(QFont(painter.font().family(), 10, QFont.Bold))
    painter.drawText(QRect(left, int(body.top()) + PADDING, width, 20), Qt.AlignLeft, card.theme)

    # 标题
    painter.setPen(QColor(TEXT))
    painter.setFont(QFont(painter.font().family(), 20, QFont.Bold))
    painter.drawText(QRect(left, int(body.top()) + PADDING + 26, width, 40), Qt.AlignLeft, card.title)

    # 目标（自动换行，最多占三行的高度）
    painter.setPen(QColor(TEXT))
    painter.setFont(QFont(painter.font().family(), 12))
    goal = card.goal or "（还没定目标）"
    painter.drawText(
        QRect(left, int(body.top()) + PADDING + 82, width, 96), Qt.AlignLeft | Qt.TextWordWrap, goal
    )

    # 分隔线
    line_y = body.bottom() - PADDING - 46
    painter.setPen(QColor(BORDER))
    painter.drawLine(left, int(line_y), left + width, int(line_y))

    # 进度
    painter.setPen(QColor(TEXT_MUTED))
    painter.setFont(QFont(painter.font().family(), 10))
    status = CARD_STATUS_TITLES.get(card.status, card.status)
    progress = f"{status} · 已打卡 {checkin_count} 次"
    if last_checkin:
        progress += f" · 最近 {last_checkin}"
    painter.drawText(
        QRect(left, int(line_y) + 10, width, 20), Qt.AlignLeft, progress
    )

    # 落款
    painter.setPen(QColor(TEXT_MUTED))
    painter.setFont(QFont(painter.font().family(), 9))
    painter.drawText(
        QRect(left, int(line_y) + 28, width, 18),
        Qt.AlignLeft,
        f"电子宠物 · 认领于 {card.created_at.strftime('%Y-%m-%d')}",
    )
