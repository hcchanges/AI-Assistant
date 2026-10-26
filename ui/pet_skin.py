"""
宠物皮肤：把设计稿神兽图作为桌宠的主力视觉。

图片资源由 tools/prepare_pet_skin.py 从设计稿转换而来（白底扣透明、裁掉多余白边），
放在 pet/assets/ 下。本模块只负责"取图 → 缩放到窗口尺寸 → 底对齐贴到画布"。

姿势与状态的对应（由设计者指定）：
    待机   sit_happy      坐姿开心（默认静止时就是这张）
    说话   jump_excited   跳跃兴奋
    腾空   jump_excited   弹跳飞行时也用它（本来就是跳起来的姿势）
    休眠   lie_sleep      趴卧睡觉
    拖动   walk_side      侧身漫步

悠闲姿态（指令 2 的行为库）复用上面这几张图，映射见 BEHAVIOR_POSE：
    坐下/趴下/打盹/发呆 用坐姿、趴卧、歪头；溜达/追尾巴用侧身漫步；蹦跳/扑空用跳跃兴奋。

贴图约定：所有姿势等比缩放到 160x160 画布内、水平居中、底边对齐画布底部，
这样不同姿势的"脚"都落在同一条地面上，落地挤压（底边为锚点）时不会悬空。

国风神兽整图换皮（v2-skin-guofeng）：场景态（专注 / 电影）+ 情绪（怒 / 恐 / 笑）
会换成一整张**抠净透明底 png**（用户已抠好白底），而不是姿态帧动画。`scene_skin()` 给
映射，`PetSkin.scene_pixmap()` 运行期按 alpha 裁掉透明边再贴画布；没配图的态（工作 /
游戏 / 悠闲 / 睡觉）保持上面那套水墨立绘姿势图不动。图从固定路径
`E:\\电子宠物\\国风神兽-*.png` 读，不新增配置。
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np
from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap

from core.paths import bundle_dir, scene_skin_dir
from life.screen.scene_guard import KIND_FOCUS, KIND_WATCH
from life.screen.scenes import SCENE_ANGRY, SCENE_FESTIVE, SCENE_HORROR
from pet.behavior import Behavior
from pet.state import PetState

logger = logging.getLogger(__name__)

CANVAS = 160                      # 桌宠窗口边长（逻辑像素）
PIXEL_RATIO = 2.0                 # 高分屏按 2 倍输出，缩放后不糊
ASSETS_DIR = bundle_dir() / "pet" / "assets"

# 姿势名（与 pet/assets/ 下的文件名一致）
POSE_IDLE = "sit_happy"
POSE_IDLE_ALT = "tilt_confused"
POSE_TALK = "jump_excited"
POSE_SLEEP = "lie_sleep"
POSE_DRAG = "walk_side"

ALL_POSES = (POSE_IDLE, POSE_IDLE_ALT, POSE_TALK, POSE_SLEEP, POSE_DRAG)

# ---- 国风神兽整图换皮（v2-skin-guofeng） ----
# 场景态 + 情绪 → 一张整图 png（用户已抠净透明底，运行期按 alpha 裁边，非姿态帧动画）。
# 图随仓库走（pet/assets/scenes），源码运行和打包态都用同一套路径；缺图只降级，
# 不让整只宠物消失。四张图只作资源，不进任何库。
SCENE_SKIN_DIR = scene_skin_dir()   # 开发态 <repo>/pet/assets/scenes；打包态 _MEIPASS/pet/assets/scenes

SKIN_FOCUS_WORK = "国风神兽-认真工作.png"   # 专注 → 认真工作
SKIN_ANGRY = "国风神兽-叉腰愤怒.png"         # 电影·愤怒
SKIN_HORROR = "国风神兽-举手害怕.png"        # 电影·恐惧
SKIN_FESTIVE = "国风神兽-快乐喜悦.png"       # 电影·笑容

# 电影态内的情绪 → 整图（情绪只对电影态生效，见 scene_skin）
_SCENE_SKIN_EMOTION: dict[str, str] = {
    SCENE_HORROR: SKIN_HORROR,
    SCENE_ANGRY: SKIN_ANGRY,
    SCENE_FESTIVE: SKIN_FESTIVE,
}


def scene_skin(kind: str, emotion: str = "") -> str | None:
    """按场景态+情绪选一张国风神兽整图；没配图的态回 None（保持默认水墨立绘）。

    用户给的映射（v2-skin-guofeng）：
        专注               → 认真工作
        电影 + 怒 / 恐 / 笑 → 叉腰愤怒 / 举手害怕 / 快乐喜悦
        工作 / 游戏 / 悠闲 / 睡觉 → 没给图，回 None 保持默认
    电影态里还没认出情绪（emotion 空串）也回 None——那张默认水墨坐姿立绘先顶着。
    情绪只认电影态：游戏 / 专注里即使 monitor 报了个场景，也按它们自己那档走。
    """
    if kind == KIND_FOCUS:
        return SKIN_FOCUS_WORK
    if kind == KIND_WATCH:
        return _SCENE_SKIN_EMOTION.get(emotion or "")
    return None

# 悠闲姿态用哪张图（缺图的 5 个动作还没进库，见 pet/behavior.py 开头说明）
BEHAVIOR_POSE: dict[str, str] = {
    Behavior.WANDER.value: POSE_DRAG,       # 溜达：侧身
    Behavior.HOP.value: POSE_TALK,          # 蹦跳：跳起来的姿势
    Behavior.SIT.value: POSE_IDLE,          # 坐下：坐姿
    Behavior.LIE.value: POSE_SLEEP,         # 趴下：趴卧
    Behavior.SLEEP.value: POSE_SLEEP,       # 打盹：趴卧 + zzz
    Behavior.LOOK.value: POSE_IDLE_ALT,     # 发呆：歪着头看
    Behavior.CHASE_TAIL.value: POSE_DRAG,   # 追尾巴：侧身原地打转
    Behavior.POUNCE.value: POSE_TALK,       # 扑空：跳起来往前扑
}

# "抓得住脸/嘴"的范围：相对贴图内容框（去掉四周空白后的实际画面）的比例，x0, y0, x1, y1。
# 按各张图的轮廓定的：坐姿/歪头/跳跃是正面，头在上方居中；趴卧的头在左边；侧身图脸朝左，头在前方。
FACE_BOX: dict[str, tuple[float, float, float, float]] = {
    POSE_IDLE: (0.26, 0.15, 0.74, 0.46),
    POSE_IDLE_ALT: (0.24, 0.12, 0.80, 0.46),
    POSE_TALK: (0.22, 0.05, 0.82, 0.48),
    POSE_SLEEP: (0.04, 0.15, 0.50, 0.45),
    POSE_DRAG: (0.06, 0.02, 0.45, 0.34),
}


class PetSkin:
    """按姿势提供可直接绘制的 QPixmap（已贴合画布、带透明底）。"""

    def __init__(self, canvas: int = CANVAS, assets_dir: Path = ASSETS_DIR,
                 scene_skin_dir: Path = SCENE_SKIN_DIR):
        self.canvas = canvas
        self.assets_dir = assets_dir
        self.scene_skin_dir = scene_skin_dir
        self._composed: dict[str, QPixmap] = {}
        self._scene_composed: dict[str, QPixmap] = {}   # 国风神兽整图（v2-skin-guofeng）
        self._content_box: dict[str, QRect] = {}   # 各姿势实际画面的位置（画布坐标）

    # ---------- 对外 ----------

    def face_rect(self, pose: str) -> QRect:
        """某姿势"脸/嘴"在画布上的矩形（用来判断鼠标是不是按在脸上）。"""
        self.pixmap(pose)                      # 先确保已经量过内容框
        box = self._content_box.get(pose)
        if box is None:
            return QRect()
        ratios = FACE_BOX.get(pose)
        if ratios is None:
            return QRect()
        x0, y0, x1, y1 = ratios
        left = box.left() + int(round(box.width() * x0))
        top = box.top() + int(round(box.height() * y0))
        right = box.left() + int(round(box.width() * x1))
        bottom = box.top() + int(round(box.height() * y1))
        return QRect(left, top, max(1, right - left), max(1, bottom - top))


    def pose_for(
        self,
        state: PetState,
        *,
        behavior: Behavior | None = None,
        dragging: bool = False,
        airborne: bool = False,
        forced: Behavior | None = None,
    ) -> str:
        """按当前情境选择该显示哪张姿势图。

        优先序：**拖动 > 场景态强制姿态 > 腾空 > 悠闲姿态 > 状态**。

        `forced`（v2-pose-sync）是场景态钉住的那一张，**压在腾空与状态之上**：它是
        "这一场该有的样子"，不是"一次动作"。压在**状态**之上治的是"切进专注十分钟后
        它自己进休眠、姿态又趴回去"；压在**腾空**之上治的是"切换那一拍还在空中、
        `airborne` 把坐姿顶掉，画面在新旧两张图之间跳"。拖动仍然最优先——那是用户
        此刻手上在做的动作，谁都盖不过。
        """
        if dragging:
            return POSE_DRAG
        if forced is not None:
            pose = BEHAVIOR_POSE.get(forced.value)
            if pose is not None:
                return pose
        if airborne:
            return POSE_TALK      # 空中姿态复用跳跃兴奋
        if behavior is not None:
            pose = BEHAVIOR_POSE.get(behavior.value)
            if pose is not None:
                return pose
        if state == PetState.SLEEPING:
            return POSE_SLEEP
        if state == PetState.TALKING:
            return POSE_TALK
        return POSE_IDLE

    def pixmap(self, pose: str) -> QPixmap:
        """取某姿势已贴合画布的图（带缓存）。"""
        cached = self._composed.get(pose)
        if cached is None:
            cached = self._compose(pose)
            self._composed[pose] = cached
        return cached

    def scene_pixmap(self, filename: str) -> QPixmap:
        """取一张国风神兽整图已贴合画布的图（带缓存，v2-skin-guofeng）。

        跟 `pixmap` 分开存：姿势图是手工抠好的透明 PNG，整图也是抠净透明底 png（运行期
        只按 alpha 裁边、不再抠白底），缺图（用户挪走 / 删了）时退回默认坐姿，别让整只
        宠物消失。
        """
        cached = self._scene_composed.get(filename)
        if cached is None:
            cached = self._compose_scene(filename)
            self._scene_composed[filename] = cached
        return cached

    # ---------- 内部 ----------

    def _compose(self, pose: str) -> QPixmap:
        source_path = self.assets_dir / f"{pose}.png"
        source = QPixmap(str(source_path))
        if source.isNull():
            raise FileNotFoundError(
                f"宠物皮肤图缺失: {source_path}\n"
                "请先在项目根目录执行：.venv\\Scripts\\python.exe tools\\prepare_pet_skin.py"
            )
        return self._compose_to_canvas(source, pose)

    def _compose_scene(self, filename: str) -> QPixmap:
        """把一张抠净透明底的 png 整图按 alpha 裁边、再贴合画布（v2-skin-guofeng）。

        缺图不抛：这四张是用户放在固定路径上的资源，挪走 / 删了只是少一张换皮，
        不该把整只宠物一起带走——退回默认坐姿（场景态里反正也是钉坐姿那一张）。
        """
        source_path = self.scene_skin_dir / filename
        image = QImage(str(source_path))
        if image.isNull():
            logger.warning("国风神兽整图缺失: %s，退回默认坐姿", source_path)
            return self.pixmap(POSE_IDLE)
        cutout = _crop_alpha(image)
        if cutout.isNull():
            logger.warning("国风神兽整图没内容: %s，退回默认坐姿", source_path)
            return self.pixmap(POSE_IDLE)
        return self._compose_to_canvas(QPixmap.fromImage(cutout), filename)

    def _compose_to_canvas(self, source: QPixmap, key: str) -> QPixmap:
        """把一张已抠底的源图等比缩放、水平居中、底边对齐贴进画布（姿势与整图共用）。"""
        canvas_px = int(self.canvas * PIXEL_RATIO)
        target = QPixmap(canvas_px, canvas_px)
        target.setDevicePixelRatio(PIXEL_RATIO)
        target.fill(Qt.transparent)

        scale = min(self.canvas / source.width(), self.canvas / source.height())
        width = max(1, round(source.width() * scale))
        height = max(1, round(source.height() * scale))

        painter = QPainter(target)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        # 水平居中、底边对齐（脚踩在同一地面上）
        left = (self.canvas - width) // 2
        top = self.canvas - height
        painter.drawPixmap(left, top, width, height, source)
        painter.end()
        self._content_box[key] = QRect(left, top, width, height)
        return target


# ---------- 透明底 → 裁到内容外接框（运行期裁边） ----------


def _crop_alpha(image: QImage) -> QImage:
    """把一张抠净透明底的整图裁到 alpha 内容外接框。

    图已经是用户抠好的透明 png（白底没了），这里不再泛洪抠白——只裁掉四周的透明边，
    让神兽贴画布时能撑满。整张全透明（理论上不该发生）时回一张空图，让调用方退回
    默认坐姿。
    """
    rgba = image.convertToFormat(QImage.Format_RGBA8888)
    height, width = rgba.height(), rgba.width()
    buffer = np.frombuffer(bytes(rgba.constBits()), dtype=np.uint8)
    alpha = buffer.reshape(height, width, 4)[:, :, 3]
    opaque = alpha > 0
    rows = np.flatnonzero(opaque.any(axis=1))
    cols = np.flatnonzero(opaque.any(axis=0))
    if rows.size == 0 or cols.size == 0:
        return QImage()
    x0, y0, x1, y1 = int(cols[0]), int(rows[0]), int(cols[-1]), int(rows[-1])
    return rgba.copy(x0, y0, x1 - x0 + 1, y1 - y0 + 1)
