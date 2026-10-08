"""画面运动特征（v2-game-fix）：连拍几帧，看它"**怎么动**"，判这是游戏还是电影。

**为什么需要这一层。** 判"在不在看片"那条链上（`monitor.is_watch_foreground`）原来只有
两条线索：前台**进程名**认不认得出，和窗口**铺没铺满**屏。进程名那张游戏表注定认不全
（见 `game.py` 开头的挑人原则），认不出名字的全屏游戏就一路顺着"全屏 → 按在看片算"
兜底下去，真机上打游戏被弹「主人，看电影啦」正是这么来的。用户给的第二条线索就是
画面本身：**游戏和电影的"动法"不一样**。

**怎么分。** 一次连抢 4 帧（每隔 `BURST_GAP_MS` = 90 毫秒），缩成 64×36 的灰度小图，
然后逐对比：

- **运动能量** `energy`：两帧逐像素差的平均值（归一化到 0..1）。电影里一个静止的
  对白镜头，这个数贴近 0；游戏几乎永远有东西在动。
- **全局平移可解释度** `coherence`：在 ±4 格的范围里搜一个**单一的**整体位移
  (dx, dy)，看它能解释掉多少差异（**只在整数格上搜，不插值**，理由见 `best_shift`）。
  电影镜头**平滑运镜**时整幅画面一起平移，一个位移就解释得干干净净（coherence 接近
  1）；游戏画面里 HUD 钉死不动的、角色在世界里乱跑，**没有任何一个整体位移能同时
  解释这两件事**，残差几乎不减（coherence 接近 0）。

一"对"同时满足 `energy` 够大 **且** `coherence` 够小，就记一票"**这一对是游戏式的动**"。
整段里这种对比占到 2/3 以上，才判 `MOTION_GAME`。

**它只否、不给。** 这一层永远只会说"**像游戏**"，从来说不出"像电影"——没有证据（帧太小、
取不到画面、numpy 不在、判不准）一律返回 `None`，调用方按"没这条线索"处理，**行为退回
加这一层之前**。理由是误报代价不对称：说错成"游戏"只是少陪看一次，说错成"电影"就会
对着游戏冒「主人，看电影啦」。宁可漏，不可错。

**它办不到什么（照实写）。** 判据吃的是"**画面里有没有互不同步的局部运动**"，所以
**整幅刚性平移的画面**——不管是游戏里第一人称镜头随鼠标抖，还是电影里一次缓慢运镜——
只要一帧的位移不到一格，在整数网格上就谁也解释不了，可解释度直接掉 0，**与游戏乱动
不可分**。合成素材上的标定（8 张真实照片 × 22 种连拍仿真，见收尾报告第九十七节）：
游戏 11 种命中 6 种（全是"局部乱动"那一族），电影 11 种误判 1 种（"每帧平移 6 像素"，
恰好踩在一格以内）。**这一层只负责把最典型的一批游戏从兜底里捞出来，进程名那张表
仍是主力**；漏判的那些游戏，靠的是名单覆盖，不是这一层。

**两处刻意的不做**：
- **不落盘**。连拍出来的帧跟别处一样，内存里过一遍就算完，一个字都不写盘（red line）。
- **不引新包**。只用 PySide6 的缩放、加上仓库里本就有的 numpy（`rapidocr-onnxruntime`
  与 `onnxruntime` 的传递依赖，`mood_cv.py` 一直在用）。没有 opencv、没有光流库。
  万一哪台机器上 numpy 真的不在，这一层静默失效，判定退回原来那条链。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage

logger = logging.getLogger(__name__)

# 判读用的格子。**粗有粗的好处**：一格是 1/64 幅宽（960 宽的一帧上约 15 像素）。实测
# 换成更细的网格（96×54 / 128×72 / 192×108）反而更差——游戏乱动的可解释度中位数从
# 0.000 一路涨到 0.19 / 0.60 / 0.72，跟电影运镜糊成一片。粗网格等于给"整体位移"上了锁。
GRID_W = 64
GRID_H = 36

# 一次判读抢几帧、隔多久。4 帧 = 3 对；间隔取 90 毫秒，三段间隔共 270 毫秒——是一次"看一眼"
# 的量级，也是这套判据能工作的下限：间隔再短，真实画面里的运镜位移会落到**一格以内**，
# 亚格的差在整数网格上谁也解释不了，会被错当成"游戏式的乱动"（实测 55 毫秒那一档，
# "电影平移 12 像素"就是靠这条误判的）。
BURST_FRAMES = 4
BURST_GAP_MS = 90

# 比这更小的一帧是假的（测试里的占位图），量不出运动，直接当没看见。
GRID_MIN_SRC = 16

# 搜索整体位移的范围（格）。±4 格在 64 格的宽度上是 ±6%，够覆盖一次运镜的帧间位移。
SEARCH = 4

# ---- 判据（阈值来历见收尾报告第九十七节的标定表） ----
#
# 能量下限：两帧之间平均差 2.55 个灰阶。这一条同时管两件事——把"根本没动"和
# "动得太小、小到量不出来"一起挡在门外。实测（8 张真实照片 × 22 个用例）：
#   静止对白镜头 0.0002、"电影平移 3 像素" 0.0044、"电影平移 6 像素" 0.0088。
# **这条线以下的画面判不了**：位移落到一格以内（960 宽的一帧上一格 15 像素），整数
# 网格上没有任何一个位移解释得了它，可解释度直接掉到 0，跟游戏乱动长得一模一样。
# "电影每帧平移 6 像素"就是踩在这条线上的那个用例，0.010 档下 8 张照片里有 4 张会
# 被判成游戏（往上抬到 0.020 能把这 1 个误判清掉，但游戏命中从 6 种掉到 3 种）。
# 取 0.010 是有意的：**误判方向是"把电影当游戏"**，代价只是少陪看一次；而反过来的
# "把游戏当电影"正是本条要修的那个 bug，宁可这边松一点。何况这条误判还得先绕过
# `is_watchable` ——本地播放器和浏览器影视根本走不到运动特征这一步。
ENERGY_MIN = 0.010
# 可解释度上限：残差至少要留住 95%，才算"一个整体位移完全解释不了"。
# 实测电影运镜是 0.49~0.93，游戏乱动是 0.000~0.013——中间隔着五十倍，放宽到 0.10
# 结果一模一样（那 0.013 是网格边缘的毛刺），所以贴着 0 取。
COHERENCE_MAX = 0.05
# 这种"游戏式的动"要占满多少对。一"刀"硬切也只毁掉一对，够不着这条线。
BUSY_SHARE = 2.0 / 3.0

# 判读结论：只有"像游戏"这一个正向结论，没有"像电影"。
MOTION_GAME = "game"


def _numpy():
    """要 numpy 的时候现拿——不在就整个这一层失效（给 None），不拖垮调用方。"""
    try:
        import numpy as np
    except ImportError:      # pragma: no cover —— 本机有，纯粹是别把话说过头
        return None
    return np


@dataclass(frozen=True)
class MotionReading:
    """一次连拍的判读（只给日志与测试看，不进任何持久化）。"""

    frames: int
    pairs: int
    energy: float          # 各对运动能量的平均（0..1）
    coherence: float       # 各对可解释度的平均（0..1）
    busy_share: float      # "游戏式的动"的对数占比
    verdict: str | None    # MOTION_GAME 或 None

    def line(self) -> str:
        """一行日志（**只有数字，没有画面**，跟这条链上别处一个纪律）。"""
        return (
            "运动特征：%d 帧 / %d 对，能量 %.4f，可解释度 %.3f，游戏式 %.2f → %s"
            % (
                self.frames,
                self.pairs,
                self.energy,
                self.coherence,
                self.busy_share,
                "像游戏" if self.verdict else "没结论",
            )
        )


# ---------- 取帧签名 ----------

def signature(image):
    """一帧 → (GRID_H, GRID_W) 的灰度数组；量不出来给 None。

    先缩再转灰：缩放本身要重采样，缩到 64×36 之后再逐点取灰，比在全尺寸上转灰快得多。
    """
    np = _numpy()
    if np is None:
        return None
    if image is None or not isinstance(image, QImage) or image.isNull():
        return None
    if min(image.width(), image.height()) < GRID_MIN_SRC:
        return None             # 太小的一帧（测试占位图）量不出运动，当没看见
    small = image.scaled(
        GRID_W, GRID_H, Qt.IgnoreAspectRatio, Qt.SmoothTransformation
    ).convertToFormat(QImage.Format_RGB888)
    width, height = small.width(), small.height()
    if width != GRID_W or height != GRID_H:
        return None
    stride = small.bytesPerLine()
    # bytesPerLine 会按 4 字节对齐（64×3=192 正好整除，仍然照规矩裁），别把行尾填充当像素
    raw = np.frombuffer(bytes(small.constBits()), dtype=np.uint8)[: stride * height]
    rgb = raw.reshape(height, stride)[:, : width * 3].reshape(height, width, 3).astype(np.int32)
    # 灰度用整数加权（ITU-R BT.601），避开浮点，判读结果与平台无关
    gray = (rgb[:, :, 0] * 299 + rgb[:, :, 1] * 587 + rgb[:, :, 2] * 114) // 1000
    return gray.astype(np.float32)


# ---------- 纯数学部分（不碰 Qt，可离线喂数据验） ----------

def _mean_abs_diff(a, b, dx: int, dy: int) -> tuple[float, int]:
    """把 b 整体挪 (dx, dy) 之后，与 a 重叠那块的逐像素平均差；返回 (差, 重叠点数)。

    只算**重叠区**：平移之后露出来的边没有对手方，把它算进去等于凭空多出一圈假差异。
    """
    np = _numpy()
    h, w = a.shape
    ys, ye = max(0, -dy), min(h, h - dy)
    xs, xe = max(0, -dx), min(w, w - dx)
    if ye <= ys or xe <= xs:
        return 0.0, 0
    part_a = a[ys:ye, xs:xe]
    part_b = b[ys + dy: ye + dy, xs + dx: xe + dx]
    return float(np.mean(np.abs(part_a - part_b))), int(part_a.size)


def best_shift(a, b, search: int | None = None) -> tuple[float, float, float]:
    """搜一个**单一的整体位移**，让 b 挪过去之后最像 a；返回 (dx, dy, 残差)。

    **只在整数格上搜，不插值。** 亚格精化那版试过、实测否决掉了（见收尾报告第九十七节）：
    一旦允许把 b 双线性挪到格与格之间，**随机抖动也能被"解释"掉一大半**——仿真的
    "游戏全屏抖动"可解释度从 0.001 一路涨到 0.4，跟电影运镜糊成一片，判据的分离度
    反而没了。整数格等于给位移上了锁：真在一起平移的解得开，各动各的锁死。
    """
    if search is None:
        search = SEARCH
    grid: dict[tuple[int, int], float] = {}
    for dy in range(-search, search + 1):
        for dx in range(-search, search + 1):
            diff, count = _mean_abs_diff(a, b, dx, dy)
            if count > 0:
                grid[(dx, dy)] = diff
    if not grid:
        return 0.0, 0.0, 0.0
    (bx, by), best = min(grid.items(), key=lambda kv: kv[1])
    return float(bx), float(by), best


def _pair(a, b) -> tuple[float, float, float, float]:
    """一对相邻帧：返回 (能量 0..1, 可解释度 0..1, dx, dy)。"""
    zero, _ = _mean_abs_diff(a, b, 0, 0)
    dx, dy, residual = best_shift(a, b)
    energy = zero / 255.0
    if zero <= 1e-6:
        coherence = 1.0         # 一模一样的两帧：一个"零位移"就解释完了
    else:
        coherence = max(0.0, min(1.0, 1.0 - residual / zero))
    return energy, coherence, dx, dy


def read(frames) -> MotionReading | None:
    """一串帧签名 → 判读；帧不够（少于 3 帧）或签名不齐时给 None。

    为什么至少要 3 帧：**一次镜头切换**（电影里的硬切）在数值上长得跟游戏很像——画面
    整个换掉，没有任何整体位移解释得了。只有一对帧时，"切了一刀"和"游戏在动"分不开；
    有 3 对以上才能用"是不是**每一对**都这样"把它们分开（见 BUSY_SHARE）。
    """
    np = _numpy()
    if np is None:
        return None
    sigs = [f for f in frames if f is not None and getattr(f, "shape", None) == (GRID_H, GRID_W)]
    if len(sigs) < 3:
        return None

    energies: list[float] = []
    coherences: list[float] = []
    busy = 0
    for a, b in zip(sigs, sigs[1:]):
        energy, coherence, _dx, _dy = _pair(a, b)
        energies.append(energy)
        coherences.append(coherence)
        if energy >= ENERGY_MIN and coherence <= COHERENCE_MAX:
            busy += 1

    pairs = len(sigs) - 1
    busy_share = busy / pairs
    verdict = MOTION_GAME if busy_share >= BUSY_SHARE else None
    return MotionReading(
        frames=len(sigs),
        pairs=pairs,
        energy=sum(energies) / pairs,
        coherence=sum(coherences) / pairs,
        busy_share=busy_share,
        verdict=verdict,
    )


def read_images(images) -> MotionReading | None:
    """一串 QImage → 判读（签名那一步在这儿做）。取不到画面的帧直接丢掉。

    传 `None` / 空串当"一帧都没取到"处理，给 None —— 这一层从头到尾的规矩是
    "没证据就不开口"，别让调用方还要先自己判一次空。
    """
    return read([signature(image) for image in (images or [])])


__all__ = [
    "BURST_FRAMES", "BURST_GAP_MS", "BUSY_SHARE", "COHERENCE_MAX", "ENERGY_MIN",
    "GRID_H", "GRID_W", "MOTION_GAME", "MotionReading", "SEARCH", "best_shift",
    "read", "read_images", "signature",
]
