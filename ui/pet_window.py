"""
悬浮桌宠窗口：无边框、透明、置顶，可拖动，支持"Q 弹弹跳"。

对应方案 P1/P2、《电子宠物_Trae指令_Q弹弹跳》与《新增指令汇总》的指令 2、指令 7、指令 15：
- 单击 → 在宠物旁边展开聊天气泡（指令 15：从双击改成单击；双击不再单独处理）；
- 鼠标停在它身上够久 → 轻提示：扭头看你 + 冒个小气泡（一句短提示，见 `HOVER_HINT_LINE`），
  不自动展开聊天（指令 15）；
- 按住拖动 → 跟随鼠标；拖到高处松手 → 自由落体 → 落地压扁(Squash) → 弹起拉伸(Stretch)
  → 多次弹跳衰减 → 静止；
- 落差越大，弹得越高、次数越多（恢复系数随落地冲击速度增大）；
- 位置静止后写回 config.yaml，下次启动还在原地；
- 悠闲姿态（指令 2）：默认坐着不动，隔一阵子自己坐下/趴下/打盹/发呆，偶尔溜达一下；
  挑行为与节奏由 pet/behavior.py 决定，这里只负责"怎么演"（位移、形变、翻转、zzz 气泡）；
- 扯嘴巴：按住它的脸往左右拉，头和嘴会跟着手走（脚底钉住），松手 Q 弹回弹；
  拉扯与回弹期间它会生气——气鼓鼓 + 抖一抖 + 哼一句；扯得够远的话，弹回原样后换下一个性格。

分层说明：
- 画什么（神兽皮肤图）由 PetSkin 负责，本类不改它；
- 本类负责"物理 + 形变"：paintEvent 里用 QTransform 以底部中心为锚点缩放皮肤图；
- 定时器三速：待机 100ms 省 CPU，弹跳 16ms、行为动作 33ms，静止后切回。
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import Callable

from PySide6.QtCore import QPoint, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QPainter, QTransform
from PySide6.QtWidgets import QMenu, QWidget

from core.config import Config
from pet.behavior import META, Behavior, BehaviorScheduler
from pet.persona import strip_action_marks
from pet.state import PetState, PetStateMachine
from ui.bubble import KIND_ZZZ, SHOW_MS, SpeechBubble
from ui.pet_skin import CANVAS, POSE_IDLE_ALT, PetSkin

logger = logging.getLogger(__name__)

ANIMATION_INTERVAL_MS = 100   # 待机动画帧间隔
PHYSICS_INTERVAL_MS = 16      # 弹跳物理帧间隔（约 60fps）
BEHAVIOR_INTERVAL_MS = 33     # 行为动作刷新间隔（约 30fps）：溜达要顺滑，太省会一顿一顿
SLEEP_FRAME_STEP = 5          # 休眠时降帧，省点 CPU
CLICK_TOLERANCE_PX = 4
ANIM_FRAME_STEP = 6           # 弹跳时每 6 个物理帧更新一次皮肤图（约 96ms，保持动画节奏）

# ---- 悠闲姿态的动作参数（指令 2） ----
WALK_SPEED = 70.0             # 溜达速度（像素/秒）
WALK_MARGIN = 8               # 溜达时离屏幕左右边缘留的余量
HOP_VELOCITY = 780.0          # 蹦跳的起跳速度（复用 Q 弹物理，落地后自然衰减停下）
POUNCE_SEC = 0.5              # 扑空：前扑动作的时长（秒）
POUNCE_DIST = 90.0            # 扑空前进距离（像素）
POUNCE_HOP = 26.0             # 扑空离地高度（像素）
CHASE_RADIUS = 14.0           # 追尾巴左右摆动幅度（像素）
CHASE_PERIOD = 0.9            # 追尾巴转一圈的时间（秒）
BREATH_PERIOD = 2.6           # 原地动作的呼吸起伏周期（秒）
BREATH_AMPLITUDE = 0.012      # 呼吸起伏幅度（相对身高的比例）
ZZZ_TEXT = "z z z…"           # 打盹时头顶挂的那条（v2-pose-sync：它属于**睡觉这个动作**）
POSE_FACES_LEFT = True        # 侧身贴图（walk_side）原图朝左：往右走时才需要水平镜像

# ---- 扯嘴巴（抓着脸往左右拉，松手 Q 弹回弹 + 生气） ----
MOUTH_MAX_PULL = 46.0         # 最多能扯多远（像素）
PULL_SWITCH_MIN = 24.0        # 扯过这个距离才算"在换性格"（轻轻碰一下不算）
PULL_STRETCH_DIV = 320.0      # 扯得越远横向撑得越开：sx = 1 + |拉扯| / 这个数
PULL_STIFFNESS = 90.0         # 回弹劲度（越大回弹越快）
PULL_DAMPING = 5.0            # 回弹阻尼（越小来回弹得越多）
PULL_STOP_V = 12.0            # 回弹速度小于它就算停（像素/秒）
ANGER_SEC = 2.6               # 生气持续多久（松手后开始算）
ANGER_PUFF = 0.06             # 生气时气鼓鼓的幅度（横向撑大）
ANGER_SHAKE_PX = 2.5          # 生气时抖动幅度（像素）
ANGER_SHAKE_STEP = 0.5        # 抖动相位步进（越大抖得越急）
ANGER_LINE = "哼！别扯啦！"

# ---- 悬停轻提示（指令 15） ----
# 鼠标停在它身上够久 → 扭头看你 + 冒个小气泡。默认不自动展开聊天（参数选了"仅轻提示"）。
HOVER_HINT_MS = 2400          # 轻提示持续多久（气泡与扭头一起收回）
# 从 1600 提到 2400（v2-pet-persona-rename 批次13 补）：气泡里从"一个问号"换成了一整句话，
# 1600ms 一眼扫不完；老值配旧内容（一个符号）是对的，换了内容就得跟着放长。
# v2-pet-persona-rename：原来是「喵？」——去语气词之后只剩一个孤立问号，悬在头顶读起来
# 像"话没说完"或"出错了"。**已裁定换成一句有意义的人设短提示**（AI 助理口吻，问一句
# 要不要帮忙）。这一格是**停下来看你时的轻回应**，不是功能说明，所以只要跟"扭头看你"
# 这个动作配得上就行；要换句子改这一行即可（气泡宽度自适应，不用调版）。
HOVER_HINT_LINE = "有什么我可以帮你的？"


class PetWindow(QWidget):
    chat_requested = Signal()        # 双击宠物（要打开宠物旁的聊天气泡）
    action_requested = Signal(str)   # work / life / settings / quit
    persona_cycle_requested = Signal()  # 扯完脸蛋要换下一个性格
    moved = Signal()                 # 宠物移动了（番茄钟倒计时气泡、聊天气泡要跟着走）
    touched = Signal()               # 用户碰了宠物（供"久未互动"计时用）

    # ---- 物理参数（对应指令第 7 节可调参数表） ----
    GRAVITY = 2600.0      # 重力加速度，越大下落越"重"
    REST_BASE = 0.62      # 低处反弹基础恢复系数
    # 高处反弹上限。必须严格 <1：恢复系数 >1 时每次落地能量反而增加，会越弹越高永不静止
    # （实测 REST_MAX=1.02 时高处落地冲击速度单调递增、弹起后飞出屏幕上万像素）。
    # 取 0.88：高处初始能量足，弹十几下、约 5 秒收停——既"越高越弹"又利落不拖沓。
    # （0.96 虽更"魔性"，但实测高处要弹近 30 下、约 20 秒，太拖沓。）
    REST_MAX = 0.88
    HEIGHT_REF = 600.0    # "越高越弹"映射曲线斜率参考
    SETTLE_V = 280.0      # 静止阈值（反弹速度低于它视为停稳）；调大能让小碎弹更快收尾
    SQUASH_MAX = 0.35     # 落地压扁最大幅度
    SQUASH_DECAY = 7.0    # 压扁回弹衰减速度
    STRETCH_MAX = 0.12    # 自由落体竖直拉伸最大幅度
    DT = 0.016            # 固定物理步长（秒）

    def __init__(
        self,
        config: Config,
        state_machine: PetStateMachine,
        skin: PetSkin | None = None,
        scheduler: BehaviorScheduler | None = None,
        parent: QWidget | None = None,
        focusing: Callable[[], bool] | None = None,
    ):
        super().__init__(
            parent, Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool
        )
        self._config = config
        self._state_machine = state_machine
        self._skin = skin or PetSkin()
        self._scheduler = scheduler or BehaviorScheduler(config.pet.persona)
        # "这会儿算不算正在专注"——专注期间点击不搭理（v2-focus-interact ①），
        # 手动番茄钟在跑也算。由装配层塞一个回调进来，宠物窗口自己不持有计时器。
        # （原先还有个只管"菜单上该写开始还是结束"的 `focus_state`，v2-focus-notes2 ③
        #   把菜单里那两条摘掉之后就没用了，一并删掉。）
        self._focusing: Callable[[], bool] = focusing or (lambda: False)
        self._tick = 0
        self._physics_tick = 0

        # ---- 物理状态 ----
        self._py = 0.0          # 竖直位置（float，避免每帧取整损失精度）
        self._vy = 0.0          # 竖直速度，向下为正
        self._settled = True    # 是否已静止
        self._sx = 1.0          # 水平形变
        self._sy = 1.0          # 竖直形变
        self._squash_t = 0.0    # 落地压扁计时（1→0 衰减）
        self._bounce_count = 0  # 本次弹跳已落地次数（自测/调试用）

        # ---- 悠闲姿态状态（指令 2） ----
        self._behavior: Behavior | None = None
        self._behavior_started = 0.0     # 当前动作开始时刻（monotonic）
        self._behavior_origin_x = 0      # 动作起点的 x，位移都相对它算，不累积误差
        self._behavior_dir = 1           # +1 往右走、-1 往左走（贴图朝向见 POSE_FACES_LEFT）

        # ---- 扯嘴巴状态（抓着嘴巴左右拉） ----
        self._mouth_grabbed = False      # 是否正抓着嘴不放
        self._mouth_pull = 0.0           # 当前横向拉扯量（屏幕像素，右正左负）
        self._pull_velocity = 0.0        # 回弹速度
        self._pull_grab_x = 0            # 按下时的鼠标全局 x，位移都相对它算
        self._anger_until = 0.0          # 生气到什么时候（monotonic）
        self._anger_base_x = 0           # 生气抖动前的窗口 x，抖完要归位
        self._anger_shakes_window = True # 生气抖动是否有权挪窗口（用户一上手就交还控制权）
        self._shake_phase = 0.0          # 抖动相位
        self._persona_switch_pending = False   # 松开后是否要换下一个性格

        # ---- 悬停轻提示（指令 15） ----
        self._hover_timer = QTimer(self)          # 单发：停下够久才提示，快速划过不算
        self._hover_timer.setSingleShot(True)
        self._hover_timer.timeout.connect(self._on_hover_timeout)
        self._hint_until = 0.0                    # 轻提示演到什么时候（monotonic）
        self._speech_until = 0.0                  # 正文气泡提到什么时候（monotonic）
        self._bubble_blocker: Callable[[], bool] | None = None  # 这会儿该不该冒浮动气泡（聊天框开着就闭嘴）

        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setWindowTitle(config.pet.name)
        self.setFixedSize(CANVAS, CANVAS)

        # 拖动状态要先于姿势计算初始化（_current_pose 会读 _dragged）
        self._drag_origin = None
        self._dragged = False

        self._skin_pixmap = self._skin.pixmap(self._current_pose())
        # 国风神兽整图换皮（v2-skin-guofeng）：这会儿钉着哪一张整图（None = 走姿态图）。
        # 由装配层按"场景态 + 情绪"推进来，本类只负责"这会儿要不要演它"。
        self._scene_skin: str | None = None
        self._bubble = SpeechBubble()

        self._timer = QTimer(self)
        self._timer.setInterval(ANIMATION_INTERVAL_MS)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start()

        self._scheduler.behavior_started.connect(self.play_behavior)
        self._scheduler.behavior_finished.connect(lambda _behavior: self.stop_behavior())
        self._state_machine.state_changed.connect(self._on_state_changed)
        self._restore_position()
        self._py = float(self.y())
        self._render()

    # ---------- 对外 ----------

    def speak(self, text: str, bubble: bool = True, show_ms: int = SHOW_MS) -> None:
        """说一句话：切到说话动画；bubble=False 时不弹浮动气泡（聊天框开着时就说进框里）。

        show_ms <= 0 表示**常驻**（v2-scene-state-fix ④）：正文气泡不自动淡出，
        `_speech_until` 记成"一直没说完"，好让 zzz / 悬停「？」别把情绪词顶掉；
        情绪变（新一句 show_text）会自然盖掉，散场 / 专注由 hide_speech_bubble 收掉。
        """
        self._state_machine.speak()
        text = strip_action_marks(text)
        if bubble and not self._bubble_blocked():
            if show_ms > 0:
                self._speech_until = time.monotonic() + show_ms / 1000.0
            else:
                self._speech_until = float("inf")
            self._bubble.show_text(text, self.frameGeometry(), show_ms)

    def set_bubble_blocker(self, blocker: Callable[[], bool]) -> None:
        """装一个"现在能不能冒浮动气泡"的判断，由装配层给。

        头顶的浮动气泡（正文 / 「？」 / zzz / 「哼！」）和聊天框其实是同一个位置的
        两种说法，两条窗各在底部画一个 12px 尖角，并存就是用户看到的"两个脚"。
        所以聊天框开着的时候浮动气泡一律让位——宠物已经在框里说话了，头顶再来一条是多余的。
        做成回调而不是布尔值：省得在聊天框开合的每条路上都记得来通知一次。
        """
        self._bubble_blocker = blocker

    def _bubble_blocked(self) -> bool:
        """这会儿该不该让浮动气泡闭嘴（对话气泡优先级更高）。"""
        return self._bubble_blocker is not None and self._bubble_blocker()

    def hide_speech_bubble(self) -> None:
        """立刻收掉已经在屏上的浮动气泡（打开聊天框时叫一声）。

        光挡住新冒的还不够：气泡可能正挂在头顶，聊天框一开就变成上下两个尖角。
        """
        self._speech_until = 0.0
        self._hint_until = 0.0
        self._bubble.hide_bubble()

    def _speech_on_screen(self) -> bool:
        """气泡里这会儿是不是正说着正文（正文优先，状态词让位）。"""
        return time.monotonic() < self._speech_until

    def wake(self) -> None:
        """用户一开口就把打盹（zzz）的宠物叫醒，并重新开始休眠倒计时。

        单独拆出来是因为"用户发言"和"宠物回应"是两个时刻：宠物说话走 speak()，
        而用户打字的那一刻就该醒来，别等到它憋出回复才从 zzz 里抬头。
        """
        self._state_machine.touch()

    def show_front(self) -> None:
        """弹到屏幕最前（被提醒叫起来时用）。"""
        if not self.isVisible():
            self.show()
        self._state_machine.touch()
        self.raise_()
        self.activateWindow()
        self._sync_behavior_pause()

    def hide_pet(self) -> None:
        self._bubble.hide_bubble()
        self.hide()
        self._sync_behavior_pause()

    def show_pet(self) -> None:
        """从"让位"状态回来（指令 17：全屏卡关掉后恢复），放开行为调度。"""
        self.show()
        self._sync_behavior_pause()

    def set_quiet(self, quiet: bool) -> None:
        """安静模式（番茄钟专注中）：只演安静动作，不再溜达。"""
        self._scheduler.set_quiet(quiet)

    def apply_persona(self, persona: str) -> None:
        """换性格：行为权重跟着变。"""
        self._scheduler.apply_persona(persona)

    def throw_from(self, x: int, y: int) -> None:
        """松手 → 从 (x, y) 自由落体进入弹跳。"""
        self._py = float(y)
        self._vy = 0.0
        self._settled = False
        self._physics_tick = 0
        self._bounce_count = 0
        self._set_physics_mode(True)
        self._sync_behavior_pause()   # 腾空中不挑新动作
        self._render()          # 立即切到腾空姿势，不要停留在"漫步"

    # ---------- 物理循环 ----------

    def _screen_area(self) -> QRect:
        """宠物所在屏幕的可用区域（地面线与溜达边界都按它算）。"""
        screen = (
            QGuiApplication.screenAt(self.frameGeometry().center())
            or QGuiApplication.primaryScreen()
        )
        return screen.availableGeometry()

    def _ground_y(self) -> float:
        """地面线：猫所在屏幕可用区底部对应的窗口 top 坐标。"""
        return float(self._screen_area().bottom() - CANVAS + 1)

    def _physics_step(self) -> None:
        self._vy += self.GRAVITY * self.DT
        self._py += self._vy * self.DT

        ground = self._ground_y()
        if self._py >= ground:
            # 触地
            self._py = ground
            impact = abs(self._vy)
            restitution = min(self.REST_MAX, self.REST_BASE + impact / self.HEIGHT_REF * 0.5)
            self._vy = -impact * restitution
            self._squash_t = 1.0
            self._bounce_count += 1
            if abs(self._vy) < self.SETTLE_V:
                # 能量耗尽 → 静止
                self._vy = 0.0
                self._settled = True
                self._sx = self._sy = 1.0
                self._squash_t = 0.0
                self._set_physics_mode(False)
                self.move(self.x(), int(round(self._py)))
                self._render()          # 从"腾空姿势"切回待机姿势
                self._save_position()
                self._sync_behavior_pause()
                return

        # 自由落体 / 上升：竖直拉伸（速度越快拉得越长）
        speed = abs(self._vy) / 1200.0
        stretch = min(self.STRETCH_MAX, speed * self.STRETCH_MAX)
        self._sx, self._sy = 1.0 - stretch, 1.0 + stretch
        self._decay_squash()  # 落地压扁优先（squash_t>0 时覆盖上面的拉伸）

        self.move(self.x(), int(round(self._py)))
        self.update()

    def _decay_squash(self) -> None:
        """落地压扁随时间回弹到 1.0。"""
        if self._squash_t > 0.0:
            self._squash_t = max(0.0, self._squash_t - self.DT * self.SQUASH_DECAY)
            k = self._squash_t
            self._sx = 1.0 + self.SQUASH_MAX * k
            self._sy = 1.0 - self.SQUASH_MAX * k

    # ---------- 绘制与动画 ----------

    def _on_tick(self) -> None:
        if not self._settled:
            # 弹跳中：每帧更新物理；皮肤图按 ~96ms 节奏更新，保持动画速度不变
            self._physics_step()
            self._physics_tick += 1
            if self._physics_tick % ANIM_FRAME_STEP == 0:
                self._tick += 1
                self._render()
            return
        if self._mouth_grabbed or self._mouth_pull or self._pull_velocity or self._anger_active():
            self._step_deform()
            return
        if self._behavior is not None:
            self._advance_behavior()
            return
        self._tick += 1
        if self._state_machine.state == PetState.SLEEPING and self._tick % SLEEP_FRAME_STEP:
            return
        self._render()

    def _current_pose(self) -> str:
        """当前该显示哪张姿势图（悬停轻提示 > 拖动 > 场景态强制 > 腾空 > 姿态 > 状态）。"""
        if self._hint_active() and self._idle_for_hint():
            return POSE_IDLE_ALT      # 悬停轻提示：扭头看你
        return self._skin.pose_for(
            self._state_machine.state,
            behavior=self._behavior,
            dragging=self._dragged,
            airborne=not self._settled,
            # 场景态钉住的那张（v2-pose-sync）：它说了算，状态机想睡也趴不下去。
            forced=self._scheduler.forced,
        )

    def set_scene_skin(self, skin: str | None) -> None:
        """钉一张国风神兽整图（v2-skin-guofeng），None 表示回到姿态图。

        装配层在场景态 / 情绪变时喊这一句。值没变就不重画——情绪去抖后同一拍
        反复推同一个值不该白刷一遍。
        """
        if skin == self._scene_skin:
            return
        self._scene_skin = skin
        self._render()

    def _scene_skin_active(self) -> bool:
        """这会儿该不该演那张整图：钉了整图、且没被拖动 / 腾空顶掉。

        跟 `_current_pose` 里"强制姿态"同一条纪律——整图换皮是"这一场该有的样子"，
        用户此刻手上在拖它（或刚抛出去还弹着）时，还是回到行走 / 腾空那两张姿态图，
        别拖着一张正面全身图横着飞。
        """
        if not self._scene_skin:
            return False
        return not self._dragged and self._settled

    def _render(self) -> None:
        if self._scene_skin_active():
            self._skin_pixmap = self._skin.scene_pixmap(self._scene_skin)
        else:
            self._skin_pixmap = self._skin.pixmap(self._current_pose())
        self.update()

    def _set_physics_mode(self, active: bool) -> None:
        self._timer.setInterval(PHYSICS_INTERVAL_MS if active else ANIMATION_INTERVAL_MS)

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform | QPainter.Antialiasing)
        w, h = self.width(), self.height()
        # 贴图原图朝左（见 POSE_FACES_LEFT），所以往右走时才镜像，保证"头朝前"
        mirror = -self._behavior_dir if POSE_FACES_LEFT else self._behavior_dir
        if self._scene_skin_active():
            # 整图换皮不镜像（v2-skin-guofeng）：国风神兽是正面全身图，翻过去朝向就乱了。
            mirror = 1
        # 扯嘴的横向撑开 + 生气的气鼓鼓，都叠加在基础形变上
        sx = self._sx * self._pull_stretch() * self._anger_puff()
        transform = QTransform()
        transform.translate(w / 2, h)        # 以底部中心为锚点缩放，符合"贴地挤压"
        transform.scale(sx * mirror, self._sy)
        transform.translate(-w / 2, -h)
        pull = self._mouth_pull * mirror     # 镜像后画布坐标与屏幕方向相反，先换算回去
        if pull:
            # 扯嘴：越靠上（头/嘴）跟手越明显，脚底钉住不动
            shear = QTransform(1.0, 0.0, -pull / h, 1.0, pull, 0.0)
            transform = shear * transform
        painter.setTransform(transform)
        painter.drawPixmap(0, 0, self._skin_pixmap)

    def moveEvent(self, event) -> None:  # noqa: N802
        # 宠物移动时气泡跟着走（不重新计时，避免气泡永远不消失）
        self._bubble.follow(self.frameGeometry())
        self.moved.emit()
        super().moveEvent(event)

    # ---------- 悠闲姿态（指令 2） ----------

    def play_behavior(self, behavior: Behavior, duration_ms: int) -> None:
        """开始演一个悠闲姿态；挑哪个、演多久由调度器说了算。

        **先清旧、再上新**（v2-pose-sync）：新姿态除非本身就是蹦跳，否则先把上一段
        没落地的腾空收掉。不收的话 `_current_pose` 里"腾空"那一档会一直压着新姿态
        （`airborne` 判在 `behavior` 前面），坐姿钉不住、画面在新旧两张图之间跳——
        真机上"切换时偶现两个姿势"就是这个。
        """
        meta = META[behavior]
        if behavior is not Behavior.HOP and not self._settled:
            self._settle_now()
        self._behavior = behavior
        self._behavior_started = time.monotonic()
        self._behavior_origin_x = self.x()
        self._behavior_dir = random.choice((-1, 1))   # 往左还是往右，随机
        if meta.moves:
            # 会动的动作刷新率调高一点，走起来才顺
            self._timer.setInterval(BEHAVIOR_INTERVAL_MS)
            if behavior is Behavior.HOP:
                self._start_hop()
        # 换动作了：上一条 zzz（如果有人挂着）到此为止——打盹的气泡跟着打盹这个动作走。
        if behavior is not Behavior.SLEEP:
            self._bubble.hide_bubble(kind=KIND_ZZZ)
        # 打盹：趴着睡，头顶挂个 zzz（气泡到点自己淡出）。
        # 正文还在气泡里就不挂 zzz，别把人家刚说的话顶掉。
        elif not self._speech_on_screen() and not self._bubble_blocked():
            self._bubble.show_text(
                ZZZ_TEXT, self.frameGeometry(), duration_ms, kind=KIND_ZZZ
            )
        self._render()

    def _settle_now(self) -> None:
        """把"腾空"这笔账当场结掉：落回地面、结束物理，回到静止。

        只在**切换动作**时用（`play_behavior`）。蹦跳正常演完那一路不碰——它本来就
        该按自己的物理落地。
        """
        self._py = self._ground_y()
        self._vy = 0.0
        self._settled = True
        self._squash_t = 0.0
        self._sx = self._sy = 1.0
        self._set_physics_mode(False)
        self._timer.setInterval(ANIMATION_INTERVAL_MS)
        self.move(self.x(), int(round(self._py)))
        # 落地这笔账结了，"腾空中"那道暂停理由跟着消失——跟 `_physics_step` 自然落地
        # 那一支走同一句，别让调度器一直以为它还悬在半空。
        self._sync_behavior_pause()

    def stop_behavior(self) -> None:
        """动作演完（或被打断）：回到静止待机，**并把自己挂的那条气泡收掉**。

        打盹的 zzz 是"睡觉这个动作"的附属品，不是一句独立的话：睡觉一结束（演完，
        或被打断去演别的——比如进专注被强制坐起），它就该跟着消失。修前这里只清
        `_behavior`，zzz 留着按自己的计时器慢慢淡出，于是真机上出现"已经坐起来了、
        头上还挂着 zzz"。**只收 zzz 那一档**，屏上要是别人刚写的话（开场白 / 情绪词）
        一个字都不动。
        """
        if self._behavior is None:
            return
        meta = META[self._behavior]
        self._behavior = None
        self._sx = self._sy = 1.0
        self._behavior_dir = 1
        if self._settled:
            self._timer.setInterval(ANIMATION_INTERVAL_MS)
        if meta.moves:
            # 挪过位置就记下来，下次启动还在新位置（原地动作不用写盘）
            self._save_position()
        self._bubble.hide_bubble(kind=KIND_ZZZ)
        self._render()

    def _start_hop(self) -> None:
        """蹦跳：直接复用 Q 弹物理（给一个起跳速度，落地后自然衰减停下）。"""
        self._settled = False
        self._vy = -HOP_VELOCITY
        self._physics_tick = 0
        self._bounce_count = 0
        self._set_physics_mode(True)
        self._render()

    def _advance_behavior(self) -> None:
        """按当前动作更新位置与形变（每个刷新拍调用一次）。"""
        behavior = self._behavior
        if behavior is None:
            return
        elapsed = time.monotonic() - self._behavior_started
        area = self._screen_area()
        ground = self._ground_y()

        if behavior is Behavior.WANDER:
            x = self._behavior_origin_x + self._behavior_dir * WALK_SPEED * elapsed
            self.move(int(round(self._clamp_x(x, area))), int(round(ground)))
        elif behavior is Behavior.POUNCE:
            # 前扑：猛地窜出去一小段，同时离地小跳一下，最后落回地面高度
            k = min(1.0, elapsed / POUNCE_SEC)
            eased = k * k * (3 - 2 * k)          # 起步和收尾都平滑
            x = self._behavior_origin_x + self._behavior_dir * POUNCE_DIST * eased
            y = ground - POUNCE_HOP * math.sin(math.pi * k)
            self.move(int(round(self._clamp_x(x, area))), int(round(y)))
        elif behavior is Behavior.CHASE_TAIL:
            # 追尾巴：原地小幅左右摆动 + 每半圈翻个面，看着像原地打转
            phase = elapsed * 2 * math.pi / CHASE_PERIOD
            x = self._behavior_origin_x + CHASE_RADIUS * math.sin(phase)
            self.move(int(round(self._clamp_x(x, area))), int(round(ground)))
            self._behavior_dir = 1 if math.cos(phase) >= 0 else -1
        else:
            # 原地动作：给一点呼吸起伏，免得像张静止贴图
            breath = math.sin(elapsed * 2 * math.pi / BREATH_PERIOD)
            self._sy = 1.0 + BREATH_AMPLITUDE * breath
            self._sx = 1.0 - BREATH_AMPLITUDE * breath / 2
        self.update()

    def _clamp_x(self, x: float, area: QRect) -> float:
        """把 x 夹在屏幕可用区内，溜达不越界、不卡到边缘外。"""
        right_limit = area.right() - (CANVAS - 1) - WALK_MARGIN
        return max(area.left() + WALK_MARGIN, min(x, float(right_limit)))

    def _sync_behavior_pause(self) -> None:
        """什么时候不该挑新动作：说话中/睡觉、被拖着、腾空中、被隐藏。

        **场景态强制姿态期间，"进休眠"不算忙**（v2-pose-sync）：那一档正钉着坐姿陪着，
        十分钟没人理它也不该趴回去。这里要是不放行，`set_paused(True)` 会把 `_behavior`
        清掉——虽然 `pose_for` 里强制姿态仍然压着状态，但动作层被停掉后 `_forced` 的
        续期也跟着停，白丢一拍。
        """
        forced = self._scheduler.forced is not None
        busy = (
            (not forced and self._state_machine.state != PetState.IDLE)
            or self._dragged
            or not self._settled
            or not self.isVisible()
        )
        self._scheduler.set_paused(busy)

    def _on_state_changed(self, state) -> None:
        self._render()
        self._sync_behavior_pause()

    # ---------- 扯嘴巴：抓着嘴巴左右拉，松手 Q 弹回弹 + 生气 ----------

    def _face_hit(self, local_pos: QPoint) -> bool:
        """按下的地方是不是"脸/嘴"（在那儿拖才是扯嘴，拖别处还是搬动宠物）。"""
        box = self._skin.face_rect(self._current_pose())
        if box.isNull():
            return False
        # 命中框是按"没翻转的贴图"量的，所以要跟 paintEvent 用同一个镜像符号：
        # paintEvent 里 mirror = -_behavior_dir（见那里），mirror 为负时画面被翻转，
        # 命中 x 也要翻到另一侧，否则趴卧、侧身这种头偏在一侧的姿势会左右颠倒。
        mirror = -self._behavior_dir if POSE_FACES_LEFT else self._behavior_dir
        x = local_pos.x()
        if mirror < 0:
            x = self.width() - 1 - x
        return box.contains(x, local_pos.y())

    def _begin_mouth_pull(self, grab_x: int) -> None:
        self._mouth_grabbed = True
        self._pull_grab_x = grab_x
        self._mouth_pull = 0.0
        self._pull_velocity = 0.0
        self._timer.setInterval(BEHAVIOR_INTERVAL_MS)   # 扯和回弹都要刷新得快
        self._scheduler.interrupt()
        self._state_machine.touch()
        self.touched.emit()
        self._start_anger()

    def _update_mouth_pull(self, global_x: int) -> None:
        offset = float(global_x - self._pull_grab_x)
        self._mouth_pull = max(-MOUTH_MAX_PULL, min(MOUTH_MAX_PULL, offset))
        self.update()

    def _end_mouth_pull(self) -> None:
        """松手：交给回弹物理（_step_deform 里跑阻尼振荡），生气再多留一会儿。

        扯得够远才算"在换性格"，轻轻碰一下不算，免得误触。
        """
        self._mouth_grabbed = False
        self._anger_until = time.monotonic() + ANGER_SEC
        self._persona_switch_pending = abs(self._mouth_pull) >= PULL_SWITCH_MIN

    def _start_anger(self) -> None:
        """生气：气鼓鼓 + 抖一抖 + 哼一句。"""
        self._anger_until = time.monotonic() + ANGER_SEC
        self._anger_base_x = self.x()
        self._anger_shakes_window = True
        self._shake_phase = 0.0
        if not self._bubble_blocked():
            self._bubble.show_text(ANGER_LINE, self.frameGeometry(), int(ANGER_SEC * 1000))

    def _anger_active(self) -> bool:
        return self._anger_until > time.monotonic()

    def _step_deform(self) -> None:
        """每拍处理扯嘴回弹与生气抖动（都在同一个高频节奏里）。"""
        now = time.monotonic()
        if not self._mouth_grabbed and (self._mouth_pull or self._pull_velocity):
            dt = BEHAVIOR_INTERVAL_MS / 1000.0
            accel = -PULL_STIFFNESS * self._mouth_pull - PULL_DAMPING * self._pull_velocity
            self._pull_velocity += accel * dt
            self._mouth_pull += self._pull_velocity * dt
            if abs(self._mouth_pull) < 0.5 and abs(self._pull_velocity) < PULL_STOP_V:
                self._mouth_pull = 0.0
                self._pull_velocity = 0.0
        if self._persona_switch_pending and not self._mouth_pull and not self._pull_velocity:
            # 弹回原样了 → 换下一个性格（扯脸蛋换性格）
            self._persona_switch_pending = False
            self.persona_cycle_requested.emit()

        # 生气的抖动与归位只在"没人碰它"时挪窗口：用户一上手拖，位置就归用户管，
        # 否则每拍都会把窗口拨回生气时的位置，宠物就拖不动了（气鼓鼓的形变照常演）
        if self._anger_until > now:
            self._shake_phase += ANGER_SHAKE_STEP
            if self._anger_shakes_window:
                offset = int(round(ANGER_SHAKE_PX * math.sin(self._shake_phase)))
                self.move(self._anger_base_x + offset, self.y())
        elif self._anger_until:
            # 气消了：位置归位，节奏调回省电档
            self._anger_until = 0.0
            if self._anger_shakes_window:
                self.move(self._anger_base_x, self.y())
            if self._settled and not self._mouth_pull and not self._pull_velocity:
                self._timer.setInterval(ANIMATION_INTERVAL_MS)
        self.update()

    def _pull_stretch(self) -> float:
        """扯得越远，横向撑得越开（松手回弹时这个值跟着振荡回来）。"""
        return 1.0 + abs(self._mouth_pull) / PULL_STRETCH_DIV

    def _anger_puff(self) -> float:
        """生气时气鼓鼓（横向一鼓一鼓的）。"""
        if not self._anger_active():
            return 1.0
        return 1.0 + ANGER_PUFF * (0.6 + 0.4 * math.sin(self._shake_phase))

    # ---------- 位置 ----------

    def _restore_position(self) -> None:
        x, y = self._config.pet.position
        center = QPoint(int(x) + CANVAS // 2, int(y) + CANVAS // 2)
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x = max(area.left(), min(int(x), area.right() - CANVAS))
        y = max(area.top(), min(int(y), area.bottom() - CANVAS))
        self.move(x, y)

    def _save_position(self) -> None:
        self._config.pet.position = [self.x(), self.y()]
        try:
            self._config.save()
        except OSError as exc:
            logger.error("保存桌宠位置失败: %s", exc)

    # ---------- 鼠标交互 ----------

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            # 用户上手了：悬停提示立刻作废（拖拽状态优先，指令 15 的防误触）
            self._hover_timer.stop()
            self._hint_until = 0.0
            if self._settled and not self._focus_silenced() and self._face_hit(
                event.position().toPoint()
            ):
                # 按在脸/嘴上 → 进入"扯嘴巴"模式（宠物不会被搬走）
                self._begin_mouth_pull(int(event.globalPosition().x()))
                event.accept()
                return
            # 用户上手了：立刻停下手上的悠闲动作，安静一会儿再恢复
            self._scheduler.interrupt()
            # 位置从此归用户管：生气抖动别再往生气时的位置拨（否则拖不动）
            self._anger_shakes_window = False
            # 弹跳中按住：立刻停住，跟着手走（可再次抛掷）
            if not self._settled:
                self._settled = True
                self._vy = 0.0
                self._sx = self._sy = 1.0
                self._squash_t = 0.0
                self._set_physics_mode(False)
                self._render()
            self._drag_origin = event.globalPosition().toPoint() - self.frameGeometry().topLeft()
            self._dragged = False
            if not self._focus_silenced():
                # 专注期间这一下不算"被碰过"：久未互动计时、宠物状态机都不动
                self._state_machine.touch()
                self.touched.emit()
            event.accept()
        elif event.button() == Qt.RightButton:
            self._show_menu(event.globalPosition().toPoint())
            event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if self._mouth_grabbed:
            self._update_mouth_pull(int(event.globalPosition().x()))
            event.accept()
            return
        if self._drag_origin is None or not (event.buttons() & Qt.LeftButton):
            return
        target = event.globalPosition().toPoint() - self._drag_origin
        if not self._dragged:
            moved = (target - self.pos()).manhattanLength()
            if moved <= CLICK_TOLERANCE_PX:
                return
            self._dragged = True
            self._bubble.hide_bubble()
            self._render()          # 切到"侧身漫步"（被拖着走）
            self._sync_behavior_pause()
        self.move(target)
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return
        if self._mouth_grabbed:
            self._end_mouth_pull()   # 松手 → 自己弹回去
            event.accept()
            return
        dragged = self._dragged
        self._drag_origin = None
        self._dragged = False       # 先复位拖动标记，抛掷时才能正确切到腾空姿势
        if dragged:
            # 松手 → 从当前位置自由落体弹跳（静止后再保存位置）
            self.throw_from(self.x(), self.y())
        else:
            # 单击 → 唤起聊天（指令 15：从双击改成单击，双击不再单独处理）。
            # 专注期间不搭理这一下（v2-focus-interact ①）：不弹对话、不冒话，
            # 但**拖动照旧**——宠物正好压在屏幕上时，挪开是基本操作。
            self._sync_behavior_pause()
            if not self._focus_silenced():
                self.chat_requested.emit()
        event.accept()

    # ---------- 悬停轻提示（指令 15） ----------

    def enterEvent(self, event) -> None:  # noqa: N802
        """鼠标停在宠物身上：够久才轻提示一下，不自动展开聊天。

        拖拽与弹跳时一律不起表：拖着它的时候鼠标必然在它身上，
        腾空时窗口也可能从鼠标底下飞过，这两种都不该算"停在你身上看你"。
        """
        if self._config.pet.hover_hint and not self._dragged and self._settled:
            self._hover_timer.start(int(self._config.pet.hover_hint_sec * 1000))
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        """鼠标移开（快速划过就是这一下）：取消这次提示。"""
        self._hover_timer.stop()
        super().leaveEvent(event)

    def _on_hover_timeout(self) -> None:
        """停够了：扭头看你 + 冒个小气泡（只演一次，继续停着也不会自动展开聊天）。"""
        if self._focus_silenced():
            return          # 专注期间不冒话（v2-focus-interact ①：不触发互动）
        if self._dragged or not self._settled:
            return          # 这中间开始拖拽/被抛起来了，就当没发生
        if self._speech_on_screen():
            return          # 它正在说话：气泡里的正文比「？」要紧，这次轻提示让位
        if self._bubble_blocked():
            return          # 聊天框开着（气泡会糊在它的输入行上）：这次不冒
        self._hint_until = time.monotonic() + HOVER_HINT_MS / 1000.0
        self._bubble.show_text(HOVER_HINT_LINE, self.frameGeometry(), HOVER_HINT_MS)
        self._render()

    def _hint_active(self) -> bool:
        return self._hint_until > time.monotonic()

    def _focus_silenced(self) -> bool:
        """专注期间：宠物不搭理点击（v2-focus-interact ①）。

        屏蔽的是**四处"搭理"**：单击弹对话、悬停冒话、按脸扯嘴巴、以及"被碰过"那笔账
        （久未互动计时 / 状态机）。**拖动不在屏蔽之列**——宠物正好压在屏幕上时，挪开
        是基本操作，拦着不给动才是真的烦。

        判据由装配层给（`Bootstrap._is_focusing`：手动番茄钟在专注，或自动那套在跑），
        所以"专注态"在这个项目里仍然只有一个定义，不另起一套。
        """
        return bool(self._focusing())

    def _idle_for_hint(self) -> bool:
        """现在是不是"闲着、没在演动作"：扭头只在这时候做，免得溜达到一半歪着头走。"""
        return (
            self._settled
            and not self._dragged
            and self._behavior is None
            and self._state_machine.state == PetState.IDLE
        )

    def _show_menu(self, global_pos) -> None:
        menu = self._build_menu()
        menu.exec(global_pos)
        # 菜单是以本窗口为父建的，父在了它就不会自动销毁；用完手动回收，
        # 否则每右键一次就在窗口下留一个菜单对象
        menu.deleteLater()

    def _build_menu(self) -> QMenu:
        """右键菜单：工作 / 生活 / 设置 / 退出——**就这四条，不随状态增减**。

        「语音输入（按住说话）」在 v2-chat-core #1 摘掉了：聊天框里只剩文字框 + 发送，
        菜单上也不再给它留明路（引擎还在 life/voice/，只是不接线）。
        「结束专注」「番茄钟」两条（v2-focus-web 加的）在 v2-focus-notes2 ③ 摘掉：
        专注进行中时菜单会多冒出两条，那正是用户说的"多出来的按键"。
        """
        menu = QMenu(self)
        # 工作模块那一项写的是锁着的说法（公开版）：点它进的是占位页，
        # 页上写着"需内部授权"。快捷键、条数、其余三项一个字没动。
        for key, text in (
            ("work", "🔒 工作模块（待解锁）"),
            ("life", "生活"),
            ("settings", "设置"),
        ):
            menu.addAction(text).triggered.connect(
                lambda _checked=False, k=key: self.action_requested.emit(k)
            )
        # 专注那两条（「结束专注」/「番茄钟」）在 v2-focus-notes2 ③ 摘掉了：
        # 用户要的右键菜单就是**四条固定的**——工作 / 生活 / 设置 / 退出，
        # 不因为"这会儿在专注"就多冒两条出来。专注的进出改在番茄钟面板上
        # （生活 → 番茄钟：主按钮在跑的时候就是「结束专注」）。
        menu.addAction("退出").triggered.connect(
            lambda _checked=False: self.action_requested.emit("quit")
        )
        return menu
