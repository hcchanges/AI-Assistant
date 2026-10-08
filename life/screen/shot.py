"""抓屏笔记的取景（S1）：只截前台那扇窗，用完即弃。

跟 life/screen/monitor.py 的取景是三件不同的事，别混：
- monitor 抓的是**全屏底下那条字幕带**，只为认场景，结果只存内存、不上模型；
- playmate 抓的是**全屏**，发给视觉模型看；
- 这一个抓的是**前台窗口那一块**（工作 / 网课应用），交给 OCR 认字，供笔记那条线提炼。

窗口矩形从哪来：Windows 的 `GetForegroundWindow` + `DwmGetWindowAttribute`（拿"可见边界"，
不含窗口阴影那圈透明边）。取不到就退回 `GetWindowRect`；再取不到（非 Windows / 没窗口）
返回 None——**宁可这一拍不抓，也不抓一整屏**：抓整屏会把用户不在看的别的东西也带进去。

原图生命周期（红线"原图不长期落盘"）：`grab_foreground_window` 出的是内存里的 QImage；
写盘只经 `save_temp` 一次，落在系统临时目录，`discard` 立刻删——调用方保证"生成笔记后即删"。
"""

from __future__ import annotations

import ctypes
import logging
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path
from typing import Sequence

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter

logger = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

# 可见边界（不含窗口阴影）：比 GetWindowRect 准，抓出来不带黑边
DWMWA_EXTENDED_FRAME_BOUNDS = 9

if IS_WINDOWS:
    _user32 = ctypes.windll.user32
    _dwmapi = ctypes.windll.dwmapi
    _user32.GetForegroundWindow.restype = wintypes.HWND
    _user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    _user32.GetWindowRect.restype = wintypes.BOOL
    _dwmapi.DwmGetWindowAttribute.argtypes = [
        wintypes.HWND, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint
    ]
    _dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long


def foreground_window_rect() -> QRect | None:
    """前台窗口的屏幕矩形；取不到返回 None。

    先问 DWM 要"可见边界"，拿不到再退回 GetWindowRect。两种都失败（非 Windows、
    没有前台窗口、窗口已最小化）就返回 None。
    """
    if not IS_WINDOWS:
        return None
    hwnd = _user32.GetForegroundWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    got = _dwmapi.DwmGetWindowAttribute(
        hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(rect), ctypes.sizeof(rect)
    )
    if got != 0:
        if not _user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return None
    width = int(rect.right) - int(rect.left)
    height = int(rect.bottom) - int(rect.top)
    if width <= 0 or height <= 0:
        return None
    return QRect(int(rect.left), int(rect.top), width, height)


def pick_screen_index(rect: QRect, geometries: Sequence[QRect]) -> int:
    """rect 该用哪块屏：先看中心落在哪块，没有再取交叠面积最大的那块；都没有给 0。

    抽成纯函数是为了能离线验（离屏跑测试没有真 QScreen）。这也是 v2-mood-fix 修的那个
    bug 的判据：原来挑屏看的是**光标**在哪（QGuiApplication.screenAt(QCursor.pos())），
    于是"电影放在左边那块屏、鼠标搁在主屏"这种日常摆法下，窗口矩形减主屏原点得到的是
    一堆负坐标，跟屏幕矩形求交为空 → 整帧丢掉 → CV 那条腿等于没开。
    """
    if not geometries:
        return 0
    centre = rect.center()
    for index, geo in enumerate(geometries):
        if geo.contains(centre):
            return index
    best_index, best_area = 0, 0
    for index, geo in enumerate(geometries):
        overlap = geo.intersected(rect)
        if overlap.isEmpty():
            continue
        area = overlap.width() * overlap.height()
        if area > best_area:
            best_index, best_area = index, area
    return best_index


def is_fullscreen(
    rect: QRect, geometries: Sequence[QRect], ratio: float = 0.95
) -> bool:
    """这个窗口矩形算不算全屏：几乎铺满某块屏（默认覆盖 95%）。

    v2-mood-screen ① 拿它认"本地播放器全屏放视频"——本地播放器的进程名不在
    WATCH_PROCS 那份表里（那表只收了几家），但全屏视频是看片最典型的形态，
    靠"窗口铺满一块屏"就能认出来，不用把每个播放器都列一遍。

    抽成纯函数是为了离线验（跟 pick_screen_index 一个道理）。
    """
    if not geometries or rect.isEmpty():
        return False
    for geo in geometries:
        overlap = geo.intersected(rect)
        if overlap.isEmpty():
            continue
        if overlap.width() * overlap.height() >= geo.width() * geo.height() * ratio:
            return True
    return False


# ---------- 空白 / 低信息画面（v2-mood-detect） ----------
#
# 为什么需要这一手：`is_fullscreen` 只量窗口**有多大**，量不出窗口里**有什么**。
# 一个最大化的资源管理器（打开一个空文件夹）窗口边界也铺满整块屏，于是被判成
# "全屏放视频"，12 秒后冒出"主人，看电影啦"——用户报的"屏幕空白过多被误判看电影"
# 就是这一条。
#
# 判据是**单一颜色占比**，不是"格子平不平"：本机实测（1920×1080 合成样本）发现，
# "逐格标准差"那条判据会把渐变（1.000）与大色块（0.875）也算成平坦——那等于把天空、
# 墙面这类真实画面一起判成空白，宠物反倒该反应时不反应了。换成"有多少像素落在主色
# 附近"，两类立刻分开：
#
#     空白类    全白/全黑/纯灰 1.000   白底+6 图标 0.980   白底+12 图标 0.931
#     ───────────────────── 断档 ─────────────────────
#     非空白类  白噪声 0.700   渐变 0.281   大色块 0.000   天空+地面 0.000
#
# 取 0.90 落在断档里：离最近的空白样本（0.931）留 0.03 余量，离最近的噪声样本
# （0.700）留 0.20。**偏保守是故意的**——宁可漏判一次"空白"（照旧当在看片），
# 也别把真实画面判成空白，那会让宠物从"多说一句"变成"该说时不说"。
#
# 已知边界（照实写）：整屏接近纯色的真实画面（大片暗场夜景 0.975）会落进空白档——
# 那种画面本来也没什么可判的，代价可接受。文件夹里塞满图标时主色占比会掉到 0.9 以下，
# 那时不再算"空白"——这条判据治的是"空白过多"，不是"任何非视频窗口"。
BLANK_SAMPLE = 64           # 缩到 64×64 再算，够统计又便宜
BLANK_TOL = 24              # 每通道容差：抗压缩噪声与底色渐变
BLANK_RATIO = 0.90          # 主色像素占比到这个数以上 → 判"没内容"
BLANK_MIN_PX = 16           # 一帧最短边小于它就不判（太小的一帧量不出信息量）


def dominant_ratio(image: QImage) -> float:
    """主色像素占比：缩到 64×64，算"落在平均色 BLANK_TOL 以内"的像素比例。

    空白屏（纯色 / 一个空文件夹）接近 1.0；有内容的画面（噪声、渐变、实拍）低得多。
    取**平均色**而不是"出现最多的那个色"：纯色屏两者等价，而带一点压缩噪声的空白屏
    用平均色更稳——不会因为噪声把一个颜色族拆成好几桶而漏判。
    """
    if image is None or image.isNull():
        return 0.0
    small = image.scaled(
        BLANK_SAMPLE, BLANK_SAMPLE, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    ).convertToFormat(QImage.Format_RGB32)
    pixels = [
        small.pixelColor(x, y)
        for y in range(BLANK_SAMPLE)
        for x in range(BLANK_SAMPLE)
    ]
    count = len(pixels)
    mean_r = sum(p.red() for p in pixels) / count
    mean_g = sum(p.green() for p in pixels) / count
    mean_b = sum(p.blue() for p in pixels) / count
    near = sum(
        1
        for p in pixels
        if abs(p.red() - mean_r) <= BLANK_TOL
        and abs(p.green() - mean_g) <= BLANK_TOL
        and abs(p.blue() - mean_b) <= BLANK_TOL
    )
    return near / count


def is_blank(image: QImage) -> bool:
    """这一帧算不算"没内容"（空白屏 / 纯色屏）。取不到画面时给 False（当它不是空白）。

    为什么取不到就给 False：判空白的用途是**否掉**一次"在看片"，判错的代价是不反应。
    拿不到依据时宁可照旧当它可能在放视频，也别凭一个空值把陪看关掉。
    """
    if image is None or image.isNull():
        return False
    if min(image.width(), image.height()) < BLANK_MIN_PX:
        return False        # 太小的一帧（测试里的假帧）量不出信息量，不拿它当依据
    return dominant_ratio(image) >= BLANK_RATIO


def grab_screen_for(rect: QRect) -> tuple[QImage, QRect] | None:
    """抓 rect 所在那块屏的全屏画面，返回 (图, 那块屏的几何)；抓不到给 None。

    抓的是**整块屏**再裁窗口，不是"直接抓窗口"：Windows 上直接抓被遮挡的窗口会拿到
    黑块，整屏抓拿到的始终是用户眼前真实的画面。
    """
    screens = QGuiApplication.screens()
    if not screens:
        return None
    index = pick_screen_index(rect, [s.geometry() for s in screens])
    screen = screens[index]
    pixmap = screen.grabWindow(0)
    if pixmap.isNull():
        return None
    return pixmap.toImage().convertToFormat(QImage.Format_RGB32), screen.geometry()


def grab_foreground_window(
    pet_rect: QRect | None = None,
    window_rect: QRect | None = None,
    extra_masks: Sequence[QRect] | None = None,
) -> QImage | None:
    """截前台窗口那一块；宠物自己那几块涂白，免得把猫也认进笔记。

    pet_rect 是宠物在屏幕上的矩形（回调现问，跟 monitor 一个做法）；
    window_rect 传了就用它（测试用），不传就现问前台窗口。
    取不到窗口 / 画面返回 None。

    `extra_masks` 是**除宠物本体之外**还要涂白的矩形（v2-social-ocr-isolate）：宠物
    说话气泡、「AI助理」助手窗都是独立的小工具窗，悬在聊天窗上面；只遮宠物本体
    的话，那几个窗盖住的那几行字会被 OCR 当成"对方说的话"读走。调用方（装配层）现问
    这几个窗的矩形，这里只负责涂。
    """
    rect = window_rect if window_rect is not None else foreground_window_rect()
    if rect is None or rect.isEmpty():
        return None
    grabbed = grab_screen_for(rect)    # 抓窗口**所在**那块屏的全屏，再裁出窗口
    if grabbed is None:
        return None
    full, geometry = grabbed
    local = rect.translated(-geometry.left(), -geometry.top())
    local = local.intersected(QRect(0, 0, full.width(), full.height()))
    if local.isEmpty():
        return None
    crop = full.copy(local)
    # 宠物自己那几块在 **crop 这块图**里的位置：全局坐标减 crop 在原屏上的左上角
    # （= rect 的左上角）。原来减的是 local.left()（已经减过屏幕原点），副屏上会
    # 偏一个屏幕原点。本体与 extra_masks 走同一条换算，别各写一套。
    masks: list[QRect] = []
    if pet_rect is not None and not pet_rect.isNull() and not pet_rect.isEmpty():
        masks.append(pet_rect)
    masks.extend(extra_masks or ())
    for mask in masks:
        if mask is None or mask.isNull() or mask.isEmpty():
            continue
        local_mask = mask.translated(-rect.left(), -rect.top())
        # 真机上确认"涂白的这块到底盖没盖住宠物自己那几个窗"（v2-social-mask-dpr）：
        # 帧是**物理像素**，掩码必须也是物理像素才盖得住——DPR 1.5 时若掩码只有帧的
        # 2/3，就是没换单位。这只打一条 debug，不参与任何判断。
        logger.debug(
            "涂白：帧 %dx%d 窗口矩形=%s 掩码(帧内)=%s",
            crop.width(), crop.height(),
            (rect.left(), rect.top(), rect.width(), rect.height()),
            (local_mask.left(), local_mask.top(), local_mask.width(), local_mask.height()),
        )
        _whiten(crop, local_mask)
    return crop


def _whiten(image: QImage, rect: QRect) -> None:
    """把 rect 那块涂白（宠物自己别进 OCR）。"""
    clipped = rect.intersected(QRect(0, 0, image.width(), image.height()))
    if clipped.isEmpty():
        return
    painter = QPainter(image)
    painter.fillRect(clipped, QColor(255, 255, 255))
    painter.end()


def save_temp(image: QImage) -> Path | None:
    """把这一张原图落到系统临时目录，返回路径；写不进去返回 None。

    这是**唯一**一次原图落盘，只为 OCR 那一趟有个确定的生命周期可删。
    """
    if image is None or image.isNull():
        return None
    name = f"pet_notes_{int(time.time() * 1000)}_{id(image) & 0xFFFF}.png"
    path = Path(tempfile.gettempdir()) / name
    try:
        if not image.save(str(path), "PNG"):
            return None
    except Exception as exc:      # noqa: BLE001 —— 存不下就算了，笔记那条线不该被这拖垮
        logger.debug("原图临时落盘失败: %s", exc)
        return None
    return path


def discard(path: Path | None) -> None:
    """把原图删掉（生成笔记后立刻调；文件本来就不在也不报错）。"""
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.debug("原图临时文件删不掉: %s", exc)
