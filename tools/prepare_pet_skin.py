"""
把设计稿神兽图转换成桌宠可直接用的皮肤资源（可复用的转换工具）。

做三件事：
1. 扣底：设计稿是纯白背景、无透明通道，从四边泛洪填充去掉"与边缘相连的近白区域"，
   变成透明背景。只删边缘连通的白，神兽身上本来就白的部分不会被误删。
2. 裁剪：按不透明区域的外接框裁掉四周多余白边，让神兽占满画布，缩放时不浪费像素。
3. 归一化命名：按姿势关键词映射成 ascii 文件名，供代码稳定引用。

重要：pet/assets/ 下的图现在是设计者手工抠好的成品（比自动抠图更精细）。
本脚本默认**不会覆盖已存在的文件**，避免把手工成果冲掉。
只有明确要重新生成时才加 --force：
    .venv\\Scripts\\python.exe tools\\prepare_pet_skin.py --force

用法（在项目根目录执行）：
    .venv\\Scripts\\python.exe tools\\prepare_pet_skin.py

源图默认读上级目录的 国风神兽-*.png，输出到 pet/assets/。
"""

from __future__ import annotations

import sys
from collections import deque
from pathlib import Path

import numpy as np
from PySide6.QtGui import QImage

ROOT_DIR = Path(__file__).resolve().parent.parent
SOURCE_DIR = ROOT_DIR.parent                       # E:\电子宠物
OUTPUT_DIR = ROOT_DIR / "pet" / "assets"

# 近白判定阈值：三通道都不低于它，就认为是背景候选（设计稿背景是 254/254/254）
WHITE_THRESHOLD = 236

# 姿势关键词 -> 输出文件名（不含扩展名）
POSE_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("坐姿开心", "sit_happy"),
    ("歪头疑惑", "tilt_confused"),
    ("跳跃兴奋", "jump_excited"),
    ("趴卧睡觉", "lie_sleep"),
    ("侧身漫步", "walk_side"),
)


def load_rgb(path: Path) -> np.ndarray:
    """读图为 (h, w, 3) 的 int16 数组。"""
    image = QImage(str(path))
    if image.isNull():
        raise FileNotFoundError(f"读不出图片: {path}")
    image = image.convertToFormat(QImage.Format_RGBA8888)
    height, width = image.height(), image.width()
    buffer = np.frombuffer(bytes(image.constBits()), dtype=np.uint8)
    return buffer.reshape(height, width, 4)[:, :, :3].astype(np.int16)


def flood_background(near_white: np.ndarray) -> np.ndarray:
    """从四边泛洪填充，返回"与边缘连通的近白区域"掩码（即要变成透明的部分）。

    用扫描线算法（一次处理一整行连续的段），比逐像素 BFS 快得多。
    """
    height, width = near_white.shape
    filled = np.zeros_like(near_white)
    stack: deque[tuple[int, int]] = deque()

    # 四个边上的近白像素作为种子
    for x in range(width):
        if near_white[0, x]:
            stack.append((x, 0))
        if near_white[height - 1, x]:
            stack.append((x, height - 1))
    for y in range(height):
        if near_white[y, 0]:
            stack.append((0, y))
        if near_white[y, width - 1]:
            stack.append((width - 1, y))

    while stack:
        x, y = stack.pop()
        if filled[y, x] or not near_white[y, x]:
            continue
        # 向左右扩展成一条连续段
        left = x
        while left > 0 and near_white[y, left - 1] and not filled[y, left - 1]:
            left -= 1
        right = x
        while right < width - 1 and near_white[y, right + 1] and not filled[y, right + 1]:
            right += 1
        filled[y, left : right + 1] = True
        # 上下两行里，与这一段相邻的未填充段各自入栈
        for next_y in (y - 1, y + 1):
            if not (0 <= next_y < height):
                continue
            cursor = left
            while cursor <= right:
                if near_white[next_y, cursor] and not filled[next_y, cursor]:
                    stack.append((cursor, next_y))
                    while cursor <= right and near_white[next_y, cursor]:
                        cursor += 1
                cursor += 1
    return filled


def content_bbox(alpha_positive: np.ndarray) -> tuple[int, int, int, int]:
    """不透明区域的外接框 (x0, y0, x1, y1)，x1/y1 为闭区间。"""
    rows = np.flatnonzero(alpha_positive.any(axis=1))
    cols = np.flatnonzero(alpha_positive.any(axis=0))
    return int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])


def convert(source: Path, target_stem: str, overwrite: bool = False) -> dict:
    output = OUTPUT_DIR / f"{target_stem}.png"
    if output.exists() and not overwrite:
        return {"source": source.name, "output": output.name, "skipped": True}

    rgb = load_rgb(source)
    near_white = rgb.min(axis=2) >= WHITE_THRESHOLD
    background = flood_background(near_white)

    alpha = np.where(background, 0, 255).astype(np.uint8)
    opaque = alpha > 0
    x0, y0, x1, y1 = content_bbox(opaque)

    cropped_rgb = rgb[y0 : y1 + 1, x0 : x1 + 1].astype(np.uint8)
    cropped_alpha = alpha[y0 : y1 + 1, x0 : x1 + 1]
    height, width = cropped_alpha.shape

    rgba = np.dstack([cropped_rgb, cropped_alpha])
    rgba = np.ascontiguousarray(rgba)
    image = QImage(rgba.data, width, height, width * 4, QImage.Format_RGBA8888).copy()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    if not image.save(str(output), "PNG"):
        raise OSError(f"写入失败: {output}")

    return {
        "source": source.name,
        "output": output.name,
        "skipped": False,
        "size": (width, height),
        "opaque_ratio": round(float(opaque.mean()), 3),
        "removed_ratio": round(float(background.mean()), 3),
    }


def main() -> int:
    overwrite = "--force" in sys.argv
    if not SOURCE_DIR.exists():
        print(f"源目录不存在: {SOURCE_DIR}")
        return 1

    done = 0
    skipped = 0
    for keyword, stem in POSE_KEYWORDS:
        matches = sorted(SOURCE_DIR.glob(f"*{keyword}*.png"))
        if not matches:
            print(f"跳过（找不到含「{keyword}」的图）")
            continue
        info = convert(matches[0], stem, overwrite=overwrite)
        if info.get("skipped"):
            print(f"已存在，未覆盖（如需重新生成请加 --force）：{info['output']}")
            skipped += 1
            continue
        print(
            f"{info['source']} -> {info['output']}  "
            f"尺寸={info['size']}  不透明占比={info['opaque_ratio']}  去除背景占比={info['removed_ratio']}"
        )
        done += 1

    print(f"\n新生成 {done} 张，跳过 {skipped} 张，输出目录: {OUTPUT_DIR}")
    return 0 if (done or skipped) else 1


if __name__ == "__main__":
    sys.exit(main())
