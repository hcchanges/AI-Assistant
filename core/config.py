"""
配置加载与校验。

来源：项目根目录 config.yaml（可编辑）。
密钥读取优先级：环境变量 > Windows 凭据管理器（keyring）。
配置文件里只留 api_key_ref（凭据名/环境变量名），不出现明文 Key。

环境变量名约定：api_key_ref 为 llm_xxx 时，对应环境变量名是 xxx_API_KEY
（例如 deepseek -> DEEPSEEK_API_KEY，dashscope -> DASHSCOPE_API_KEY）。
"""

from __future__ import annotations

import copy
import logging
import os
import shutil
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import keyring
import yaml

logger = logging.getLogger(__name__)

# 保存时用的哨兵："加载时那份里没有这一项"（区别于"这一项是空的"）
_MISSING = object()

# 项目根目录（core/ 的上一级）
ROOT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = ROOT_DIR / "config.yaml"

# 支持的模型接入协议，目前只做 OpenAI 兼容协议（DeepSeek/通义等均兼容）
SUPPORTED_PROTOCOLS = ("openai_compatible",)

# 密钥在 Windows 凭据管理器里的服务名
SERVICE_NAME = "desktoppet"

# api_key_ref 前缀 -> 环境变量后缀的映射规则
_ENV_PREFIX = "llm_"
_ENV_SUFFIX = "_API_KEY"

# 工作模块的临时身份（调试用，指令 20 补）：管理员能编辑底层连接，员工不能
WORK_ROLE_ADMIN = "admin"
WORK_ROLE_USER = "user"
WORK_ROLES = (WORK_ROLE_ADMIN, WORK_ROLE_USER)


def env_key_name(api_key_ref: str) -> str:
    """把凭据名映射成环境变量名：llm_deepseek -> DEEPSEEK_API_KEY。"""
    if api_key_ref.startswith(_ENV_PREFIX):
        name = api_key_ref[len(_ENV_PREFIX) :]
    else:
        name = api_key_ref
    return (name + _ENV_SUFFIX).upper()


class ConfigError(Exception):
    """配置文件缺项或取值非法。"""


@dataclass
class AppSection:
    name: str = "电子宠物"
    autostart: bool = False
    log_level: str = "INFO"
    log_dir: str = "logs"
    # 运行时数据根目录（v2-data-root）：空 = 退回项目内 data/（改版前的老行为）。
    # 配成 E:\电子宠物 之类，db / 向量 / logs / models / 缓存 就都落到它下面的子目录。
    data_dir: str = ""


@dataclass
class LLMProviderConfig:
    name: str = ""
    protocol: str = "openai_compatible"
    base_url: str = ""
    model: str = ""
    api_key_ref: str = ""


@dataclass
class LLMSection:
    active: str = "deepseek"
    temperature: float = 0.7
    providers: dict[str, LLMProviderConfig] = field(default_factory=dict)

    def provider(self, name: str | None = None) -> LLMProviderConfig:
        """取某个供应商配置；name 为空时取当前启用项。"""
        key = name or self.active
        if key not in self.providers:
            raise ConfigError(f"未找到模型供应商配置: {key}")
        return self.providers[key]

    @property
    def active_provider(self) -> LLMProviderConfig:
        return self.provider(self.active)


@dataclass
class PetSection:
    """宠物设定。persona 取值见 pet/persona.py（5 个预设 + 自定义）。"""

    name: str = "天渌"
    persona: str = "软萌黏人"          # 性格预设
    persona_prompt: str = ""           # 自定义性格的人设描述，只有 persona 选“自定义”时用
    user_call: str = "主人"            # 对用户的称呼，留空用性格自带的那套
    style_actions: bool = True         # 说话是否带括号动作描写
    style_kaomoji: bool = True         # 说话是否带颜文字
    position: list[int] = field(default_factory=lambda: [1400, 700])
    # 呼出方式（指令 15）：鼠标停在宠物上够久，就扭头看你 + 冒个小气泡；关掉就完全不理
    hover_hint: bool = True
    hover_hint_sec: float = 0.5        # 停多久才提示（秒）


@dataclass
class MemorySection:
    db_path: str = "db/pet.db"
    vector_path: str = "vector"
    model_path: str = "models/bge-small-zh-v1.5"
    top_k: int = 5


@dataclass
class MindSection:
    """情感记忆库（S1~S5）：另起一个加密库，跟 pet.db 完全隔离。

    为什么不塞进 pet.db：pet.db 是运转数据（提醒、卡牌、活动），明文；
    情感记忆（姓名职业、关系状态、情绪、生活碎片）敏感得多，得整库加密，
    而且它的读写节奏跟运转数据完全两回事。库分开，两边的红线互不干扰。

    密钥不入这个文件：只在这里留"去哪儿取"的约定，真钥匙走环境变量或
    系统凭据管理器（见 life/mind/store.py 的 resolve_key）。
    """

    enabled: bool = True            # 关掉就等于整个情感记忆停摆（不读不写不主动提）
    db_path: str = "db/mind.db"     # 加密库落点（相对 data_dir，见 v2-data-root）
    # 「对话自然结束」的判定：用户停了这么久没再说，就算这一轮聊完了，可以提炼了
    idle_end_min: int = 3
    # 主动搭话的四种由头的参数
    reunion_days: int = 3           # 多久没聊算「久别重逢」
    followup_days: int = 2          # 关键事件隔多久可以跟进问一句
    # 提炼与主动搭话共用的开关：关掉就只记不主动开口
    proactive_enabled: bool = True
    # A++ 本地优先：本机 data/models/ 里放着量化模型（qwen2.5-0.5b/1.5b 之类）就
    # 在本地跑提炼，对话原文一个字都不出本机；没有模型就自动回落 API，行为跟以前
    # 一模一样。显式设成 false = 强制走 API（哪怕本机其实有模型）。
    local_model: bool = True
    # 本地模型权重的落点根目录。已在 .gitignore 里——仓库里不带任何权重文件，
    # 用户自己往里放；放不放都不影响开箱即用（没模型就是走 API）。
    local_model_dir: str = "models"
    # 私密称呼（B+ 挚友档 L5）：只在你们俩之间用的叫法，由用户自己给。
    # 留空 = 没给过，L5 也不会硬凑一个叫法出来（语气退回"最亲近"那一档）。
    intimate_nickname: str = ""


@dataclass
class ScheduleSection:
    quiet_hours: list[str] = field(default_factory=lambda: ["23:00", "07:30"])
    max_notify_per_hour: int = 3


@dataclass
class PomodoroSection:
    """番茄钟参数（对应指令 1 第 6 节）。时长单位是分钟。"""

    focus_min: int = 25            # 专注时长（分钟）
    short_break_min: int = 5       # 短休时长（分钟）
    long_break_min: int = 15       # 长休时长（分钟）
    long_break_every: int = 4      # 每完成几个番茄进入长休
    mute_other_reminders: bool = True   # 专注期间屏蔽其他提醒
    notify_sound: bool = True           # 阶段切换提示音


@dataclass
class ActivitySection:
    """前台应用监控参数（对应指令 5 与指令 6）。

    只记应用进程名与类别，不读窗口标题；全部数据留在本机。
    """

    enabled: bool = True             # 是否记录前台应用
    sample_sec: int = 3              # 采样间隔（秒）
    idle_min: int = 5                # 超过这么久没有键鼠操作，算"人离开了"
    retention_days: int = 14         # 数据保留天数
    blacklist: list[str] = field(default_factory=list)   # 敏感应用，完全不记录（进程名）
    sedentary_min: int = 30          # 久坐阈值（分钟）
    sedentary_repeat_min: int = 30   # 久坐提醒的重复间隔（分钟）
    distract_switches: int = 14      # 分心判定：切换次数
    distract_window_sec: int = 60    # 分心判定：统计窗口（秒）
    distract_cooldown_min: int = 5   # 分心提醒冷却（分钟）
    remind: bool = True              # 久坐/分心提醒总开关（关了照样记录）


@dataclass
class VisionSection:
    """游戏陪玩用的"看图"模型（多模态）与截屏参数。

    复用 llm.providers 里某个供应商的接口地址与 API Key，只是换成能看图的模型名
    （比如通义的 qwen-vl-max、智谱的 glm-4v-plus）。
    """

    provider: str = "tongyi"         # 用哪家的接口（llm.providers 里的键）
    model: str = "qwen-vl-max"       # 视觉模型名
    sample_sec: int = 5              # 截屏间隔（秒）
    change_threshold: float = 15.0   # 画面变化超过它就值得看一眼（0~255 平均差，越大越不敏感）
    min_gap_sec: int = 45            # 两次建议至少隔这么久（秒）
    max_width: int = 1024            # 发给模型前把截图缩到这个宽度内（省流量省钱）


@dataclass
class WorkSection:
    """工作模块（指令 16）：两条工作卡片要"大模型联网、不爬虫"。

    联网是供应商能力，目前只有通义的接口自带（enable_search + 结构化来源），
    所以这里单独指一家，跟 vision（陪玩看图单独用某家）一个套路。
    模型名不在这里重复写：直接用该供应商在 llm.providers 里配的那个。

    role 是"临时身份切换（调试用）"：界面上只有管理员 / 员工两档，切换后重新打开
    工作模块才生效（管理员多一个「编辑底层连接」）。真正的账号体系还没有，所以这一项
    只是调试开关，存在配置里、重启保持。
    """

    provider: str = "tongyi"    # 联网检索用哪家（llm.providers 里的键）
    top_n: int = 5              # 工作卡片输出几条
    pet_comment: bool = False   # 结果末尾让宠物点评一句（带性格腔调；默认关，在设置页打开）
    role: str = WORK_ROLE_ADMIN  # 工作模块按哪种身份显示：管理员 / 员工（调试用）


@dataclass
class CardSection:
    """生活卡牌的品类判定与限频参数（指令 25-B）。

    categories 是"品类判定第一层"的本地词表：9 大类各配一串关键词，存配置文件、
    管理员可编辑。它只决定"这句话归哪一类"，不改别的行为。
    后面几项是出邀请与自动归档的硬规则（天数/置信度）。
    """

    categories: dict[str, list[str]] = field(default_factory=dict)
    classify_confidence: float = 0.7   # 模型判类的置信度下限，低于它不出邀请
    invite_global_days: int = 3        # 同一个品类这么多天内最多邀一次（只限自己那个品类）
    decline_silence_days: int = 7      # 被拒绝过的品类，这么多天内不再提
    archive_idle_days: int = 14        # 这么多天无打卡/交互，自动收进卡册


@dataclass
class LifeCardsSection:
    """两半卡工具开关（指令 26-C）。

    profile_tools_enabled 管的是 26-C 那套两半卡工具（出行：天气防晒/打包，
    饮食：吃什么/热量，工作：待办/纪要/工时）。关掉时卡面只剩上半建议流。

    原先这里还有个 tool_cards_enabled，管番茄钟 / 游戏陪玩两张功能入口卡的显隐。
    那两张卡本轮已下架，开关没人读了，一并删掉——功能页本身照旧在。
    """

    profile_tools_enabled: bool = False


def _words_of(value: object) -> list[str]:
    """读配置里的词表：去空白、丢空项，别的都不动。"""
    return [str(word).strip() for word in (value or []) if str(word).strip()]


@dataclass
class ScreenSection:
    """屏幕感知（S2/S3/S4）+ 画面情绪（A+++ 之后的 CV 腿）：看画面认场景，结果只存内存。

    这一段的开关分三层，别混着看：
    - enabled 是总闸，关掉整个 life/screen 三件套都不启动；
    - ocr_enabled 单独关掉只停"字幕识别"这一环，前台轮询与自动番茄钟照常跑；
    - focus_enabled 管 S4 那个自动番茄钟（前台是网课/创作/工作类软件时才进）；
    - guide_enabled 管 v2-game-guide 那条"要不要攻略"的问句（前台是认得出的游戏时才问）；
    - game_motion 管 v2-game-fix 那条**画面运动特征**线索（认不出名字的全屏窗口靠它
      分辨"在打游戏"还是"在放片"）。游戏态一共五条线索（进程名 / 用户本地名单 /
      引擎窗口类名 / exe 安装路径 / 画面运动），关掉这个就少一条，其余四条照常
      （详见 life/screen/game.py 的 recognize）；
    - social_enabled 管 v2-social-reply 那条"要帮你措辞吗"的问句与起草稿（社交软件在前台时才冒话）；
    - read_browser_title 是浏览器标题那道破例的总闸（见下）；
    - mood 是**画面情绪那一层**的档位（off / local / cloud，见下）。

    隐私口径：**默认不读窗口标题**（沿用 monitor/foreground.py 的老约定，标题里
    可能有文档名和网址）。唯一的破例是 read_browser_title：浏览器前台时读一次标题，
    只为分辨"网页版网课 / 在线创作"还是"在看剧"——判断完只留 focus/watch 这个标签，
    标题本身不存任何字段、不落盘；标题判成网课/创作时，画面识别整个跳过。
    不想要这条破例，把它设成 false，行为就回到"完全不读标题"。
    其余一切（截到的画面、识别出的文字、场景）都只在内存里过一下，pet.db 与磁盘一律不写。

    **关于"不调模型"这句口径的修正**：OCR 那条线（S2/S3/S4）至今仍然一个模型都不调，
    全是本地关键词。但加了画面情绪之后，`mood != "off"` 时 life/screen 这条路上会
    **第一次加载模型**——一个本地的中文 CLIP ONNX（约 132MB，放在 data/models/ 下，
    已 gitignore，仓库不带权重）。它**完全离线**，画面不出本机；判读结果同 OCR 一样
    只存内存。想回到"一点模型都不碰"，把 mood 设成 "off" 即可。

    mood 三档：
    - "off"   不加载任何模型，行为与加这一层之前**逐字相同**；
    - "local" 默认。本机 ONNX 中文 CLIP 判三态（恐怖/愤怒/笑容；v2-mood-tri 砍掉哭泣），
              离线，画面不出本机；
              权重不在时会自动去下载（ModelScope 优先），没下完之前静默退回 OCR；
    - "cloud" 走云端多模态 API。**必须同时满足 mood_cloud=True** 才真的会传画面，
              否则退化成"什么都不发"（见 life/screen/mood_cloud.py 的三道闸）。
    """

    enabled: bool = True
    ocr_enabled: bool = True
    sample_sec: int = 5            # 采样间隔（秒）
    # 旧"连击"参数（v2-mood-detect 再审·终之后判定改走多数票，这两个**不再有消费者**）。
    # 留着是因为本机 config.yaml 里还有这两个键，删掉字段反而会让老配置对不上号。
    scene_stable_samples: int = 2
    scene_fast_samples: int = 1
    # 场景判定的滑动窗口（秒，v2-mood-detect 再审·终）：判定从"连击"改成"一分钟多数票"，
    # 每拍投一票，60 秒里出现最多的那一态才是最终判定；不满窗口 / 平票不输出。
    # 设成 0 = 一拍即定（单测与想要即时反应时用）。
    scene_vote_window_sec: float = 60.0
    scene_cooldown_min: int = 10   # 同一个场景这么多分钟内不重复冒话
    # 开场陪伴字幕（v2-speech-polish ④）：认出在看片子时冒一句「主人，看电影啦，我陪你看吧！」。
    # 只显示不出声，6 秒自己散（时长写在 core/bootstrap.py 的 SCREEN_CAPTION_MS）。
    companion_enabled: bool = True
    # 开场字幕的宽限期（秒）：先给剧情一个被认出来的机会。这段窗口内真认出了恐怖/愤怒/
    # 幽默/喜庆，开场那句就撤掉，直接出对应的表情反应——用户要的"开场即恐怖 → 直接怕怕"。
    # 默认 12 秒：先给剧情一个露头的机会（这一拍判出来、只是多数票窗口还没满），
    # 加上一次 OCR 约 4 秒，12 秒刚好够第一拍结论落地，又不至于让人等太久。
    opening_grace_sec: int = 12
    # **认得出名字的本地播放器**用这一档宽限期（v2-mood-detect 的"本地播放器提速"）。
    # 进程名在 WATCH_PROCS 里就是硬证据（不是"窗口恰好铺满了屏"那种猜的），不必等满
    # 12 秒。浏览器与"全屏兜底"那两档仍走 12 秒——它们证据弱，得给剧情留出被认出来的
    # 时间（见 opening_grace_sec 与 companion.OPENING_DEFER_MS 那两段）。
    opening_grace_fast_sec: int = 4
    # 看电影/刷剧场景成立时，宠物的**语音**让位：话照旧显示在气泡里，只是不出声
    # （v2-mood-fix，用户实测："看电影场景关语音只表情"）。判据是屏幕上真有场景成立
    # （monitor.scene 非空），换了前台就自动散场、说话恢复正常。
    mute_voice_while_watching: bool = True
    focus_enabled: bool = True
    focus_minutes: int = 25        # 自动番茄钟一轮多少分钟
    # 游戏攻略（v2-game-guide）：前台是认得出的游戏时问一句"要不要攻略"。
    # 只看前台**进程名**，不看屏、不读标题；要用户点头才去找，点头之前一个字都不外发。
    guide_enabled: bool = True
    guide_cooldown_min: int = 30   # 同一个游戏这么多分钟内不重复问
    # 攻略问句**晚多久问**（秒，v2-scene-state）：前台一切到游戏，宠物先冒一句开场白
    # （「我陪你一起玩游戏」），隔这么久再问「要我帮你翻份攻略吗？」。两句不挤在同一
    # 个气泡里（开场白那条浮窗停 9 秒）。默认 30 秒；设成 0 = 一切到游戏就问（加这一
    # 档之前的老口径，单测直接 new 服务时用的也是这一档）。
    guide_ask_delay_sec: int = 30
    # 画面运动特征（v2-game-fix）：认不出名字的**全屏**窗口，连抢几帧看画面"怎么动"，
    # 动得像游戏就按游戏算、不进影视判定（见 life/screen/motion.py）。这是补进程名
    # 那张表认不全的那批（Unity/Unreal 游戏没有统一的进程名）。
    # 想回到"只看进程名与窗口类名"就设成 false：这一层整个不参与，行为与加它之前
    # 逐字相同。默认开——关掉它，真机上"打游戏被当成看电影"那个 bug 就只剩下
    # 进程表覆盖的那部分能挡。
    game_motion: bool = True
    # 社交帮忙回复（v2-social-reply）：社交软件在前台时冒一句"要帮你措辞吗"，
    # 用户在聊天里讲处境、要草稿，就写一段**只读**的草稿给他自己复制。
    # 只认前台进程名，**不读聊天内容**；草稿只显示不发送。
    social_enabled: bool = True
    social_cooldown_min: int = 30  # 这一档跨应用共用：这么多分钟内不在社交软件上重复冒话
    social_words: tuple[str, ...] = ()   # 自定义触发词；留空用 life/screen/social.py 的内置表
    # 社交自动监听（v2-social-auto）：社交软件在前台时，对方回消息刹那自动认出来、直接
    # 拟候选，**不再每次点「要帮忙吗」**。节奏由对方定，不轮询不刷屏——轻量比对画面
    # 指纹，变了才 OCR；平时一次 OCR / 一次模型都不耗。关掉就退回老口径（冒话邀请那一套）。
    social_auto_listen: bool = True
    # 社交起草的语气自适应（v2-social-tone-adaptive）：把**对方当下**的口吻（这一场
    # 最近几句怎么说话——句长 / 语气词 / 表情 / 称呼 / 调性）抽成一句摘要，当风格约束
    # 喂进正文那一趟，让草稿顺对方的节奏说话。`off` 时整层不参与：不记、不抽、不注入，
    # 提示词与加它之前逐字相同（静态范例那条腿照旧）。只认 `auto` / `off`，写别的按
    # `auto` 算。
    tone_align: str = "auto"
    read_browser_title: bool = True   # 浏览器标题破例（只做网课/创作分类，仅内存）
    # 自定义"工作站点"词（v2-focus-web ①）：浏览器标题里出现这些词就按专注算。
    # 内置词表收的是常见网课/创作/办公系统，用户自己公司那套系统它不认识，这里补。
    # 留空 = 只用内置词表。跟内置词同一条规则：大小写不敏感的子串匹配。
    focus_words: tuple[str, ...] = ()
    # ---- 画面情绪（CV 腿）----
    mood: str = "local"            # off / local / cloud；local 默认开
    mood_cloud: bool = False       # 传屏授权，**默认关**；mood=="cloud" 且它为 True 才传
    mood_min_score: float = 0.70   # CV 判到什么分数才肯报场景（见 mood_cv.DEFAULT_MIN_SCORE）


# 笔记默认落点（相对项目根）：`技术` 的**上一级**那个独立文件夹「笔记」，
# 跟用户自己的 存档 / 资料 并排，不埋在程序目录里（v2-notes-path）。
# 相对路径由 abs_path 按项目根解析；用户在设置里填绝对路径就按他的来。
NOTES_DIR_DEFAULT = "../笔记"

# 知识库默认落点（相对项目根）：跟「笔记」同级的「知识库」文件夹（v2-kb-init）。
# 用户把 .md/.txt 文档丢进去，问答就能基于文档内容检索（见 life/mind/kb.py 第四条腿）。
KNOWLEDGE_DIR_DEFAULT = "../知识库"

# 聊天范例默认落点（相对项目根）：跟「笔记 / 知识库」同级的「聊天范例」文件夹
# （v2-social-fewshot）。用户把自己的聊天片段（.md/.txt）丢进去，起草社交回话时
# 按相关度挑几条当风格范例——**只学语气口吻**，见 life/screen/social_examples.py。
SOCIAL_EXAMPLES_DIR_DEFAULT = "../聊天范例"


def notes_dir_usable(path: Path) -> bool:
    """这个文件夹能不能拿来放笔记：建得出来、而且写得进去才算。

    给两处用：设置页在保存前拿它校验用户填的路径；抓屏笔记在落盘失败后拿它判断
    该不该退到默认文件夹。**只建目录、不在这里写文件**——校验一个路径不该在人家
    的文件夹里留下东西。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.W_OK)


def knowledge_dir_usable(path: Path) -> bool:
    """这个文件夹能不能拿来放知识库文档：建得出来、而且读得到才算。

    只要求**可读**、不要求可写——知识库那条腿是只读扫盘的（检索时一个字都不写回去），
    用户把文档放在只读的共享目录/文档盘上也该能用。跟 `notes_dir_usable` 的差别就在这儿
    （笔记要往里写文件，所以它查的是 W_OK）。

    **只建目录、不在这里写文件**：校验一个路径不该在人家文件夹里留下东西。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return False
    return os.access(path, os.R_OK)


@dataclass
class NotesSection:
    """抓屏笔记（C 多模态 · 笔记先行版）：前台在网课/工作时，隔一阵把窗口认成字、提炼成笔记。

    只被动触发——复用 S4 的专注判定（前台是网课/创作/工作类软件才动），
    不主动抓、不在用户干别的时抓。原图只经一次内存 + 一个临时文件，生成笔记后即删；
    笔记正文落 <笔记文件夹>/<日期>.md，不写 pet.db / mind.db，也不进画像库。
    落点默认是项目上一级的「笔记」文件夹（NOTES_DIR_DEFAULT），可在设置页改。

    跟屏幕感知（ScreenSection）是两码事：那边只认场景、只存内存、不上模型；
    这条线会把**认出来的字**交给模型提炼成笔记——所以默认关着，要用户自己开。
    """

    enabled: bool = False          # 总闸：默认关，抓屏上模型是件要用户点头的事
    interval_min: int = 5          # 隔多少分钟抓一次
    dir: str = NOTES_DIR_DEFAULT   # 笔记落点（相对项目根；测试沙箱换成临时目录）


@dataclass
class KnowledgeSection:
    """知识库目录（v2-kb-init）：放 .md/.txt 文档的地方，问答基于文档内容检索。

    跟 rag 段的"回忆型问答"是两码事，但共用 KnowledgeBase 那条检索链：
    - 回忆型（问"我之前记过的 X"）翻 碎片 / 知识库 / 笔记 / 旧记忆；
    - 知识型（问"年假怎么算"这种，实词命中文档）翻知识库目录。
    落点默认项目上一级的「知识库」（KNOWLEDGE_DIR_DEFAULT），与「笔记」同级；
    启动时确保目录存在，检索只读（跟笔记腿一样，不写回去）。
    """

    dir: str = KNOWLEDGE_DIR_DEFAULT   # 知识库目录（相对项目根；测试沙箱换成临时目录）


@dataclass
class RagSection:
    """知识库问答（v2-rag）：问"我之前记过的 X"，把本机存的片段翻出来当上下文。

    跟"联网搜答案"是两回事——翻的是**用户自己记过的东西**：加密库里的碎片、
    抓屏笔记、pet.db 里留下的旧记忆。检索全在本机跑（chroma + bge + 读文件），
    一个字节都不出本机；出本机的只有最后拼好的那几行，走既有那条生成链。

    认不认得出"这是在问我记过的事"，只看一张小词表（ask_words）。
    故意往窄了收：认错了就是宠物张口编一段"你上次说…"，比不接这个功能更糟。
    """

    enabled: bool = True           # 总闸。关掉就整条线不接，chatter 一个字都不变
    max_items: int = 4             # 一次最多翻几条进上下文（条数硬上限，不传全库）
    # 留空就用 life/mind/kb.py 里内置那张表；填了就覆盖（方便不改代码调词表）
    ask_words: list[str] = field(default_factory=list)


@dataclass
class VoiceSection:
    """语音合成（说）+ 语音识别（听）（C 多模态）。

    说：先行版用系统自带 SAPI5——零依赖、零权重、零联网（见 life/voice/tts.py 的取舍）。
    VITS/CosyVoice 的权重落点 data/models/ 已为将来预留——换模型后端只动 tts.py，
    调用方一行不改。语音不可用时自动降级成纯文字，不报错、不拖累任何一条说话线。

    听：本机 data/models/ 里放着 whisper 权重（且装了后端）就在本地认，一个字节都
    不出本机；没有就回落 API 识别（复用某个供应商的地址与 Key，只换模型名，跟
    vision 那条线一个套路）。两边都不行就只收文字——**听不见不耽误打字**。

    不常驻监听：麦克风只在"按住说话"那一刻开着（见 life/voice/capture.py），
    没按键就没有任何采集，也没有任何一段音频落盘。
    """

    enabled: bool = True   # 语音总闸；关掉就整条线纯文字
    # 宠物**自己**开口（空闲主动搭话 / 记忆碎片 / L4 分享）时，先冒话、再问一句
    # "要我念出来吗"，点了才出声（v2-proactive-voice）。关掉 = 回到"直接念"的老行为。
    # 只卡它自己主动开口：用户发消息引出的回话照常出声，看电影那条静音照旧管着。
    # 跟 screen.mute_voice_while_watching 一样只写在 config.yaml——Config.save() 的
    # 写入段里没有 voice 这一段，摆个设置页勾选框上去，用户勾完重启就变回去了。
    confirm_proactive: bool = True
    # ---- 听（ASR）----
    asr_enabled: bool = True       # 听的总闸；关掉就只剩打字
    asr_local: bool = True         # 本机有 whisper 就在本地认；显式设 false = 强制走 API
    asr_local_model_dir: str = "models"   # 本机 whisper 权重的落点（相对 data_dir）
    asr_provider: str = ""         # 走 API 时用哪个供应商；空 = 用 llm.active 那个
    asr_model: str = "whisper-1"   # 走 API 时的识别模型名
    asr_hotkey: str = "Ctrl+Alt+Space"   # 全局快捷键（按一下开始、再按一下结束）；留空 = 不用
    asr_max_sec: int = 15          # 一次最多录多久，防着忘了松手


@dataclass
class IntentSection:
    """主动服务的意图词表（指令 26-B）。

    工作那条线两层都对上才动手：work_words（做的东西）+ action_words（任务动作）；
    生活那条线是八个大类的词表（沿用 card.categories）——命中就算，不再要求 help_words
    （见 remind_words 那一段）。

    存配置文件、管理员可编辑；哪个词表留空就等于关掉那条线。

    help_words 是"求助信号"（怎么办 / 来不及）：生活那条线原先拿它当第二层门槛，
    如今只剩一个用处——本地认不出品类、但话里有它又带着情绪时，值得请模型再看一眼
    （needs_fallback）。它不再决定"认出来了要不要动手"。

    remind_words 是显式提醒词（提醒我 / 定闹钟 / 喊我）：命中了就把这句话还给"建提醒"
    那条线，不判成生活意图——用户明说要提醒，就别拿卡去顶它。这是"品类词压过时间词"
    这条优先级的唯一例外，也是唯一还压得住品类词的东西。

    urgent_words 是"特别紧急"那一档（指令 26-B 修订）：生活那条线命中它，
    就不管拒绝后的静默、也不再问一句，直接把卡翻到眼前。留空等于关掉这档，
    所有生活意图一律走"先问三次"的渐进授权。
    """

    work_words: list[str] = field(default_factory=list)
    action_words: list[str] = field(default_factory=list)
    help_words: list[str] = field(default_factory=list)
    remind_words: list[str] = field(default_factory=list)
    urgent_words: list[str] = field(default_factory=list)


# v2-data-root：把项目内 data/ 里那些**运行期**数据搬到 data_dir 下的对应落点。
# 只列运行期产生的文件/目录；data/ 里的 Python 源码、comfort_library.json 这类
# 入库的默认文件不在其列。值是 (旧 data/ 相对路径, 新 data_dir 相对路径)。
#
# **logs 不在其列**（v2-migrate-guard ①）：日志搬家没有任何价值，而且有害——旧
# data/logs/pet.log 是一份死在 10 月 4 日的快照，`copytree(dirs_exist_ok=True)` 会拿它
# **盖掉** data_dir 下当天正在写的那一份，于是真机上每启动一次，当天的日志就少一截、
# 排障直接变瞎。日志本来就该由 data_dir 自己生成，不是搬来的。
_RUNTIME_MIGRATIONS: tuple[tuple[str, str], ...] = (
    ("pet.db", "db/pet.db"),
    ("mind.db", "db/mind.db"),
    ("chroma", "vector"),
    ("models", "models"),
    ("card_chat", "cache/card_chat"),
    ("card_tools", "cache/card_tools"),
    ("card_layout", "cache/card_layout"),
    ("pomodoro_state.json", "cache/pomodoro_state.json"),
    ("notes_state.json", "cache/notes_state.json"),
)

# SQLite 旁挂文件后缀：迁移主库时把它们一起带过去（不存在就跳过）。
# **只在主库真被补过去时才跟着走**——主库跳过了还硬塞一个旧 -wal/-shm 过去，等于往
# 一个活的库里灌别人的页，比不搬危险得多（v2-migrate-guard ②）。
_DB_SIDE_SUFFIXES = ("-wal", "-shm", "-journal")

# 一次性搬家的哨兵（v2-migrate-guard ②B）：首次搬完在 data_dir 下留一个记号，之后每次
# 启动看到它就直接返回，连走一遍都不走。旧 data/ 是死在某一天的一张快照，让"运行期数据"
# 每次启动都去跟它对一遍，本身就是这一轮要停掉的那件事。
_MIGRATION_SENTINEL = ".migrated-v2-data-root"


@dataclass
class Config:
    app: AppSection
    llm: LLMSection
    pet: PetSection
    memory: MemorySection
    mind: MindSection
    schedule: ScheduleSection
    pomodoro: PomodoroSection
    activity: ActivitySection
    vision: VisionSection
    work: WorkSection
    card: CardSection
    life_cards: LifeCardsSection
    screen: ScreenSection
    notes: NotesSection
    knowledge: KnowledgeSection
    rag: RagSection
    intent: IntentSection
    voice: VoiceSection
    path: Path
    raw: dict[str, Any]

    # ---------- 加载 / 保存 ----------

    @classmethod
    def load(cls, path: Path | str = DEFAULT_CONFIG_PATH) -> "Config":
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"配置文件不存在: {path}")
        with path.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}

        app_raw = raw.get("app") or {}
        llm_raw = raw.get("llm") or {}
        pet_raw = raw.get("pet") or {}
        mem_raw = raw.get("memory") or {}
        mind_raw = raw.get("mind") or {}
        sch_raw = raw.get("schedule") or {}
        pomo_raw = raw.get("pomodoro") or {}
        act_raw = raw.get("activity") or {}
        vision_raw = raw.get("vision") or {}
        work_raw = raw.get("work") or {}
        card_raw = raw.get("card") or {}
        life_cards_raw = raw.get("life_cards") or {}
        screen_raw = raw.get("screen") or {}
        intent_raw = raw.get("intent") or {}
        voice_raw = raw.get("voice") or {}

        providers: dict[str, LLMProviderConfig] = {}
        for name, item in (llm_raw.get("providers") or {}).items():
            item = item or {}
            providers[name] = LLMProviderConfig(
                name=name,
                protocol=item.get("protocol", "openai_compatible"),
                base_url=item.get("base_url", ""),
                model=item.get("model", ""),
                api_key_ref=item.get("api_key_ref", f"llm_{name}"),
            )

        cfg = cls(
            app=AppSection(**{k: v for k, v in app_raw.items() if k in AppSection.__dataclass_fields__}),
            llm=LLMSection(
                active=llm_raw.get("active", "deepseek"),
                temperature=float(llm_raw.get("temperature", 0.7)),
                providers=providers,
            ),
            pet=PetSection(**{k: v for k, v in pet_raw.items() if k in PetSection.__dataclass_fields__}),
            memory=MemorySection(**{k: v for k, v in mem_raw.items() if k in MemorySection.__dataclass_fields__}),
            mind=MindSection(**{k: v for k, v in mind_raw.items() if k in MindSection.__dataclass_fields__}),
            schedule=ScheduleSection(
                quiet_hours=sch_raw.get("quiet_hours", ["23:00", "07:30"]),
                max_notify_per_hour=int(sch_raw.get("max_notify_per_hour", 3)),
            ),
            pomodoro=PomodoroSection(
                **{
                    k: v
                    for k, v in pomo_raw.items()
                    if k in PomodoroSection.__dataclass_fields__
                }
            ),
            activity=ActivitySection(
                **{
                    k: v
                    for k, v in act_raw.items()
                    if k in ActivitySection.__dataclass_fields__
                }
            ),
            vision=VisionSection(
                **{
                    k: v
                    for k, v in vision_raw.items()
                    if k in VisionSection.__dataclass_fields__
                }
            ),
            work=WorkSection(
                **{
                    k: v
                    for k, v in work_raw.items()
                    if k in WorkSection.__dataclass_fields__
                }
            ),
            card=CardSection(
                categories={
                    str(name): [str(word) for word in (words or []) if str(word).strip()]
                    for name, words in (card_raw.get("categories") or {}).items()
                },
                classify_confidence=float(card_raw.get("classify_confidence", 0.7)),
                invite_global_days=int(card_raw.get("invite_global_days", 3)),
                decline_silence_days=int(card_raw.get("decline_silence_days", 7)),
                archive_idle_days=int(card_raw.get("archive_idle_days", 14)),
            ),
            life_cards=LifeCardsSection(
                profile_tools_enabled=bool(life_cards_raw.get("profile_tools_enabled", False)),
            ),
            screen=ScreenSection(
                **{
                    k: v
                    for k, v in screen_raw.items()
                    if k in ScreenSection.__dataclass_fields__
                }
            ),
            notes=NotesSection(
                **{k: v for k, v in (raw.get("notes") or {}).items() if k in NotesSection.__dataclass_fields__}
            ),
            knowledge=KnowledgeSection(
                **{k: v for k, v in (raw.get("knowledge") or {}).items() if k in KnowledgeSection.__dataclass_fields__}
            ),
            rag=RagSection(
                **{
                    k: v
                    for k, v in (raw.get("rag") or {}).items()
                    if k in RagSection.__dataclass_fields__
                }
            ),
            intent=IntentSection(
                work_words=_words_of(intent_raw.get("work_words")),
                action_words=_words_of(intent_raw.get("action_words")),
                help_words=_words_of(intent_raw.get("help_words")),
                remind_words=_words_of(intent_raw.get("remind_words")),
                urgent_words=_words_of(intent_raw.get("urgent_words")),
            ),
            voice=VoiceSection(**{k: v for k, v in voice_raw.items() if k in VoiceSection.__dataclass_fields__}),
            path=path,
            raw=raw,
        )
        cfg.validate()
        return cfg

    def _read_on_disk(self) -> dict[str, Any]:
        """现读一遍磁盘上的 config.yaml，作为保存时的底稿。

        读不出来（文件被删了 / 临时手抖写成坏 YAML）就退回启动时读到的那份快照：
        宁可新改动没保住，也不能拿一份空底稿把用户的配置整份写没。
        """
        try:
            data = yaml.safe_load(self.path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as exc:
            logger.warning("保存前重读 config.yaml 失败（%s），这回按启动时那份合并", exc)
            return copy.deepcopy(self.raw)
        return data if isinstance(data, dict) else copy.deepcopy(self.raw)

    def save(self) -> None:
        """把当前配置写回 config.yaml：**只写这次运行真正改过的那几项**。

        为什么不能整体重建：桌宠位置、换性格、番茄钟这些会在运行时自动调用 save()，
        整体重建会把磁盘上我们没读进来的字段/段（用户手动加的 provider、未来新增的配置段）
        一次性抹掉——这正是"配置被重置回默认值"的根因。

        为什么要按项判断、而不是整段照抄内存：程序跑着的时候，管理员可能在外面改了
        config.yaml（给词表加两个字、加个供应商）。桌宠拖一下就会存一次位置，
        要是整段照抄，外面的改动就被这一下吞掉了——"我改了没反应、还被改回去"就是这么来的。
        所以判据是"跟加载时那份比，这一项变了没有"：变了才写，没变就留着文件上那份。
        """
        raw = self._read_on_disk()
        for name, section in (
            ("app", self.app),
            ("pet", self.pet),
            ("memory", self.memory),
            ("schedule", self.schedule),
            ("pomodoro", self.pomodoro),
            ("activity", self.activity),
            ("vision", self.vision),
            ("work", self.work),
            ("card", self.card),
            ("life_cards", self.life_cards),
            ("intent", self.intent),
            # 抓屏笔记（v2-notes-path）：设置页能改"存哪个文件夹"，不加进来
            # 改了就是不落盘的——save() 只写这个名单里的段。
            ("notes", self.notes),
            # 屏幕感知（v2-focus-web）：设置页上"专注时长 / 自动番茄钟开关 / 工作站点词 /
            # 场景阈值"都在这段里，原先整段不在名单里——界面改完是**静默不落盘**的，
            # 重启就回到文件上那份（用户看到的是"我明明改过"）。
            ("screen", self.screen),
        ):
            self._write_section(raw, name, asdict(section))
        self._write_section(
            raw,
            "llm",
            {
                "active": self.llm.active,
                "temperature": self.llm.temperature,
                # providers 是权威来源：用户新增的 provider 在 load 时已读入内存
                "providers": {
                    name: {
                        "protocol": p.protocol,
                        "base_url": p.base_url,
                        "model": p.model,
                        "api_key_ref": p.api_key_ref,
                    }
                    for name, p in self.llm.providers.items()
                },
            },
        )
        with self.path.open("w", encoding="utf-8") as f:
            yaml.safe_dump(raw, f, allow_unicode=True, sort_keys=False)
        self.raw = raw

    def _write_section(self, raw: dict[str, Any], name: str, values: dict[str, Any]) -> None:
        """把一个配置段里"改过的项"写进 raw；没改过的留给文件上那份。"""
        target = raw.setdefault(name, {})
        for field, value in values.items():
            if self._dirty(name, field, value):
                target[field] = self._dumpable(value)

    @staticmethod
    def _dumpable(value: Any) -> Any:
        """把要落盘的值归一成 yaml.safe_dump 认的形态。

        元组转列表：词表字段（screen.social_words / screen.focus_words）是 tuple，
        而 `yaml.safe_dump` 不认 tuple，直接抛 RepresenterError——名单里加上 screen
        之后，只要用户动过一次工作站点词，保存就会炸在这儿。写回文件本来也只能是列表。
        """
        if isinstance(value, tuple):
            return [Config._dumpable(item) for item in value]
        if isinstance(value, list):
            return [Config._dumpable(item) for item in value]
        if isinstance(value, dict):
            return {key: Config._dumpable(item) for key, item in value.items()}
        return value

    def _dirty(self, section: str, field: str, value: Any) -> bool:
        """这一项在这次运行里被改过吗（改过才写回文件）。"""
        return self._value_dirty(value, (self.raw.get(section) or {}).get(field, _MISSING))

    @staticmethod
    def _value_dirty(value: Any, old: Any) -> bool:
        """新值跟"加载时那份"不一样吗。

        嵌套的（llm.providers 里的每个 provider）只看新值里那几个键：
        文件上多出来的未知字段（用户自己加的）不算法改动，也就不会被写没。
        """
        if old is _MISSING:
            return True          # 文件里还没有这一项（新版代码加的字段）：补上
        if isinstance(value, dict) and isinstance(old, dict):
            return any(
                Config._value_dirty(item, old.get(key, _MISSING))
                for key, item in value.items()
            )
        return value != old

    # ---------- 校验 / 工具 ----------

    def validate(self) -> None:
        if not self.llm.providers:
            raise ConfigError("config.yaml 里没有配置任何模型供应商（llm.providers）")
        if self.llm.active not in self.llm.providers:
            raise ConfigError(f"llm.active 指向了不存在的供应商: {self.llm.active}")
        for name, p in self.llm.providers.items():
            if p.protocol not in SUPPORTED_PROTOCOLS:
                raise ConfigError(f"供应商 {name} 的协议 {p.protocol} 暂不支持，只支持 {SUPPORTED_PROTOCOLS}")
            if not p.base_url or not p.model:
                raise ConfigError(f"供应商 {name} 缺少 base_url 或 model")
        if self.memory.top_k <= 0:
            raise ConfigError("memory.top_k 必须大于 0")
        if self.pet.hover_hint_sec <= 0:
            raise ConfigError("pet.hover_hint_sec 必须大于 0")
        if self.schedule.max_notify_per_hour <= 0:
            raise ConfigError("schedule.max_notify_per_hour 必须大于 0")
        if self.rag.max_items <= 0:
            raise ConfigError("rag.max_items 必须大于 0")
        p = self.pomodoro
        if p.focus_min <= 0 or p.short_break_min <= 0 or p.long_break_min <= 0:
            raise ConfigError("番茄钟的专注/短休/长休时长都必须大于 0 分钟")
        if p.long_break_every <= 0:
            raise ConfigError("pomodoro.long_break_every 必须大于 0")
        act = self.activity
        if act.sample_sec <= 0:
            raise ConfigError("activity.sample_sec 必须大于 0")
        if act.retention_days <= 0:
            raise ConfigError("activity.retention_days 必须大于 0")
        vis = self.vision
        if vis.provider not in self.llm.providers:
            raise ConfigError(f"vision.provider 指向了不存在的供应商: {vis.provider}")
        if not vis.model:
            raise ConfigError("vision.model 不能为空")
        if vis.sample_sec <= 0:
            raise ConfigError("vision.sample_sec 必须大于 0")
        if vis.min_gap_sec <= 0:
            raise ConfigError("vision.min_gap_sec 必须大于 0")
        work = self.work
        if work.provider not in self.llm.providers:
            raise ConfigError(f"work.provider 指向了不存在的供应商: {work.provider}")
        if work.top_n <= 0:
            raise ConfigError("work.top_n 必须大于 0")
        if work.role not in WORK_ROLES:
            raise ConfigError(
                f"work.role 只能是 {' / '.join(WORK_ROLES)}（管理员 / 员工），当前是 {work.role!r}"
            )
        card = self.card
        if not card.categories:
            raise ConfigError("card.categories 不能为空：9 大类的关键词表要有内容")
        for name, words in card.categories.items():
            if not words:
                raise ConfigError(f"card.categories.{name} 至少要有一个关键词")
        if not 0.0 < card.classify_confidence <= 1.0:
            raise ConfigError("card.classify_confidence 必须在 0~1 之间（不含 0）")
        for key, value in (
            ("invite_global_days", card.invite_global_days),
            ("decline_silence_days", card.decline_silence_days),
            ("archive_idle_days", card.archive_idle_days),
        ):
            if value <= 0:
                raise ConfigError(f"card.{key} 必须大于 0")

    def abs_path(self, relative: str) -> Path:
        """把配置里的相对路径解析为项目内绝对路径，并确保父目录存在。

        注意：这只管**项目内**的相对路径（知识库 / 笔记用）。运行期数据（db / 向量 /
        logs / models / 缓存）走 data_path——它们要落到 data_dir 下，不是项目根下。
        """
        p = Path(relative)
        if not p.is_absolute():
            p = ROOT_DIR / p
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def data_dir(self) -> Path:
        """运行时数据根目录（v2-data-root）。

        配置里 app.data_dir 非空就按它（如 E:\\电子宠物）；空 = 退回项目内 data/
        （跟改版前完全一样）。知识库 / 笔记 / 资料这些**不是**运行期数据，不归它管
        ——它们走 abs_path / _notes_path 那套，本就在项目外同级。
        """
        configured = (self.app.data_dir or "").strip()
        if configured:
            return Path(configured).expanduser()
        return ROOT_DIR / "data"

    def data_path(self, relative: str) -> Path:
        """把运行期数据的相对路径解析到 data_dir 下，并确保父目录存在。

        兼容旧配置：带 data/ 前缀的旧值（data/pet.db 之类）剥掉前缀再落到 data_dir，
        免得老 config.yaml 没跟着改就解析成 data_dir/data/pet.db。绝对路径原样返回
        （测试沙箱就是往这里塞绝对临时路径）。
        """
        p = Path(relative)
        if p.is_absolute():
            p.parent.mkdir(parents=True, exist_ok=True)
            return p
        text = str(relative).replace("\\", "/")
        if text.startswith("data/"):
            text = text[len("data/"):]
        p = self.data_dir / text
        p.parent.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return self.data_path(self.memory.db_path)

    @property
    def vector_path(self) -> Path:
        return self.data_path(self.memory.vector_path)

    @property
    def mind_db_path(self) -> Path:
        """情感记忆加密库的落点（跟 pet.db 不是一个文件，别混）。"""
        return self.data_path(self.mind.db_path)

    @property
    def notes_dir(self) -> Path:
        """抓屏笔记的落点（<这个文件夹>/<日期>.md）。

        留空当作"用默认那个文件夹"——设置页里清掉路径不该等于"存到项目根目录下"。
        `..` 归一化掉再给人看，否则界面/日志里会出现一串「技术/../笔记」。
        """
        configured = (self.notes.dir or "").strip()
        return self._notes_path(configured or NOTES_DIR_DEFAULT)

    @property
    def notes_dir_default(self) -> Path:
        """笔记的默认落点（项目上一级的「笔记」）：用户填的那个用不了时退到这儿。"""
        return self._notes_path(NOTES_DIR_DEFAULT)

    @property
    def knowledge_dir(self) -> Path:
        """知识库目录的落点（<这个文件夹>/*.md 或 *.txt，v2-kb-init）。

        留空当作"用默认那个文件夹"。跟 notes_dir 同一个解析路子（相对项目根 / 绝对都接）。
        配置的落点**建不出来时退默认**（v2-kb-init 的坏路径兜底）：盘符没挂、路径里带
        非法字符这类情况，退回默认那个「知识库」——总比整个知识库问答静默失效强，
        而且用户能在设置页一眼看到"现在的落点"已经变了。
        """
        configured = (self.knowledge.dir or "").strip()
        if not configured:
            return self.knowledge_dir_default
        path = self._notes_path(configured)
        if knowledge_dir_usable(path):
            return path
        # 兜底那一支顺手把默认目录建出来：检索那条腿一见"目录不存在"就整条跳过，
        # 只换个名字不建目录，等于把知识库问答静默关掉——那不叫兜底。
        default = self.knowledge_dir_default
        knowledge_dir_usable(default)
        return default

    @property
    def knowledge_dir_default(self) -> Path:
        """知识库的默认落点（项目上一级的「知识库」）。"""
        return self._notes_path(KNOWLEDGE_DIR_DEFAULT)

    @property
    def social_examples_dir(self) -> Path:
        """聊天范例的落点（<这个文件夹>/*.md|*.txt，v2-social-fewshot）。

        跟 `notes_dir` / `knowledge_dir` 同一个解析路子（相对项目根 / 绝对路径都接）：
        默认就是项目上一级的「聊天范例」，跟用户的 笔记 / 知识库 并排。
        这个目录**读不到不报错**——没有范例照样能起草，只是少一层口吻参考。
        """
        return self._notes_path(SOCIAL_EXAMPLES_DIR_DEFAULT)

    @staticmethod
    def _notes_path(value: str) -> Path:
        """把配置里的笔记路径解析成绝对路径（相对项目根 / 绝对路径都能接）。"""
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = ROOT_DIR / path
        try:
            return path.resolve()
        except OSError:      # 盘符没挂 / 名字里带怪字符：留着原样，交给写的那边报错
            return path

    @property
    def model_dir(self) -> Path:
        """本地中文向量模型目录（由 tools/fetch_embedding_model.py 下载）。"""
        return self.data_path(self.memory.model_path)

    @property
    def local_models_dir(self) -> Path:
        """本地小模型的落点根目录（data/models/，已在 .gitignore）。

        跟上面的 model_dir 不是一个东西：那个是**向量模型**（bge）的那一层子目录，
        这个是**放 LLM 权重**的那个根。别混。
        """
        return self.data_path(self.mind.local_model_dir)

    @property
    def asr_models_dir(self) -> Path:
        """本地 whisper 权重的落点（默认也是 data/models/，跟 LLM 权重住一个根）。

        自己留一份配置项而不是直接借用 mind 的：语音输入不该因为"情感记忆关掉了"
        就跟着失效——两条线的开关各管各的。
        """
        return self.data_path(self.voice.asr_local_model_dir)

    @property
    def log_dir(self) -> Path:
        return self.data_path(self.app.log_dir)

    # ---------- 运行期数据归置（v2-data-root） ----------

    def prepare_data_root(self) -> str | None:
        """启动时定数据根（要在 setup_logging 之前调）。

        app.data_dir 配了却写不进（盘没挂 / 没权限），就把它清空退回项目内 data/，
        返回被退掉的那个路径（调用方拿去记 ERROR 日志）；没退返回 None。之所以要赶在
        setup_logging 之前：log_dir 也落在 data_dir 下，不可写时得连日志一起退，
        不然光建日志目录这一下就把启动打断了。
        """
        configured = (self.app.data_dir or "").strip()
        if not configured:
            return None
        root = Path(configured).expanduser()
        try:
            root.mkdir(parents=True, exist_ok=True)
            probe = root / ".data_root_probe"
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
        except OSError:
            self.app.data_dir = ""
            return str(root)
        return None

    def migrate_runtime_data(self) -> None:
        """把项目内 data/ 的运行期数据**补齐**到 data_dir（只复制、不删原文件）。

        v2-data-root 那版是"每次启动无脑 `copytree(dirs_exist_ok=True)`"——等于每次
        启动都拿旧 data/ 那张**死的**快照去盖 data_dir 里正在写的东西。真机上的后果：
        `pet.log` 每启动一次就被抹回旧日期那一份，`pomodoro_state.json` 同理。功能上
        看着没炸，排障时才知道不对。

        现在两道闸（v2-migrate-guard ②）：
        - **A 逐项判存在**：dst 已经在（目录判非空）就跳过，只补真缺的那几样。
          这才叫"一次性搬家"——搬过的东西不该被搬第二次。
        - **B 哨兵**：`data_dir/.migrated-v2-data-root` 在就直接返回，连走一遍都不走。

        **只有当这一轮一次都没失败时才落哨兵**：落了哨兵就等于宣告"以后不再走这条路"，
        有东西没搬成还落，那件东西就永远补不上了。失败一次就下次启动再来——A 那道闸
        保证重来只会**补缺**，不会覆盖，所以重来是安全的。

        只在 data_dir 与项目内 data/ 不同、且旧 data/ 里确有那几样东西时才动；
        知识库 / 笔记 / 资料本就在项目外同级，不在这里碰。旧 data/ 里的东西复制完
        仍留在原地，用户核对无误后想省 C 盘空间再自己删。
        """
        old_root = ROOT_DIR / "data"
        new_root = self.data_dir
        if new_root == old_root or not old_root.is_dir():
            return
        sentinel = new_root / _MIGRATION_SENTINEL
        if sentinel.exists():
            logger.debug("运行期数据早搬过了（见 %s），这次一样都不碰", sentinel)
            return
        copied: list[str] = []
        skipped: list[str] = []
        failed: list[str] = []
        for rel_src, rel_dst in _RUNTIME_MIGRATIONS:
            src = old_root / rel_src
            dst = new_root / rel_dst
            done = False
            if src.is_dir():
                # 目录判"非空"才算目标已在：空目录多半是上一次没搬成的残壳，该补就补
                if dst.is_dir() and any(dst.iterdir()):
                    logger.info("跳过 %s：目标 %s 已在，不动它", rel_src, dst)
                    skipped.append(rel_src)
                    continue
                try:
                    shutil.copytree(src, dst, dirs_exist_ok=True)
                    logger.info("补齐 %s -> %s", src, dst)
                    copied.append(rel_src)
                    done = True
                except OSError as exc:
                    logger.warning("迁移目录 %s -> %s 失败（%s）", src, dst, exc)
                    failed.append(rel_src)
            elif src.is_file():
                if dst.exists():
                    logger.info("跳过 %s：目标 %s 已在，不动它", rel_src, dst)
                    skipped.append(rel_src)
                    continue
                try:
                    dst.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dst)
                    logger.info("补齐 %s -> %s", src, dst)
                    copied.append(rel_src)
                    done = True
                except OSError as exc:
                    logger.warning("迁移文件 %s -> %s 失败（%s）", src, dst, exc)
                    failed.append(rel_src)
            if not done:
                # 主库没搬（跳过或没有），旁挂文件**一个都不许跟**——那等于往一个活的
                # 库里灌一份旧 WAL 的页，比不搬危险得多（见 _DB_SIDE_SUFFIXES 那段）。
                continue
            if rel_src.endswith(".db"):
                for suffix in _DB_SIDE_SUFFIXES:
                    side = old_root / (rel_src + suffix)
                    if side.is_file():
                        try:
                            shutil.copy2(side, dst.with_name(dst.name + suffix))
                            logger.info("补齐旁挂文件 %s", side.name)
                        except OSError as exc:
                            logger.warning("迁移 %s -> %s 失败（%s）", side, dst, exc)
                            failed.append(side.name)
        if copied:
            logger.info(
                "运行期数据从 %s 补齐 %d 项到 %s（另有 %d 项目标已在，跳过）",
                old_root, len(copied), new_root, len(skipped),
            )
        else:
            logger.info(
                "运行期数据一项没搬：%d 项目标都已在 %s", len(skipped), new_root,
            )
        if failed:
            # 有没搬成的就不落哨兵：下次启动照旧走一遍，A 那道闸只补缺、不覆盖
            logger.warning("有 %d 项没补齐（%s），哨兵不落，下次启动再试", len(failed), failed)
            return
        try:
            sentinel.write_text(
                f"v2-data-root 的一次性搬家已在本次完成；\n"
                f"旧目录：{old_root}\n"
                f"跳过 {len(skipped)} 项（目标已在）、补齐 {len(copied)} 项。\n"
                f"删掉本文件不会删数据，但会让下一次启动重新走一遍\"只补缺\"的流程。\n",
                encoding="utf-8",
            )
            logger.info("落下一次性搬家哨兵 %s", sentinel)
        except OSError as exc:
            logger.warning("哨兵 %s 写不下去（%s），下次启动还会走一遍", sentinel, exc)

    # ---------- 密钥（环境变量 > 凭据管理器） ----------

    def get_api_key(self, provider: LLMProviderConfig | None = None) -> str:
        """读取某供应商的 API Key，未设置返回空串。"""
        p = provider or self.llm.active_provider
        # 1) 环境变量优先（不落地，最安全）
        env_name = env_key_name(p.api_key_ref)
        env_value = os.environ.get(env_name)
        if env_value:
            return env_value
        # 2) 凭据管理器兜底（设置页手动填的）
        return secret_get(p.api_key_ref) or ""

    def get_api_key_source(self, provider: LLMProviderConfig | None = None) -> str:
        """返回 Key 来源，用于设置页提示：environment / keyring / none。"""
        p = provider or self.llm.active_provider
        if os.environ.get(env_key_name(p.api_key_ref)):
            return "environment"
        if secret_get(p.api_key_ref):
            return "keyring"
        return "none"


def in_quiet_hours(quiet_hours: list[str], now: datetime | None = None) -> bool:
    """现在是否落在免打扰时段内（支持跨零点，例如 23:00 ~ 07:30）。"""
    start_text, end_text = quiet_hours
    start = datetime.strptime(start_text, "%H:%M").time()
    end = datetime.strptime(end_text, "%H:%M").time()
    current = (now or datetime.now()).time()
    if start <= end:
        return start <= current < end
    return current >= start or current < end


def secret_get(ref: str) -> str | None:
    """从系统凭据管理器读取密钥。"""
    try:
        return keyring.get_password(SERVICE_NAME, ref)
    except keyring.errors.KeyringError as exc:  # 凭据服务异常
        logger.error("读取密钥失败 %s: %s", ref, exc)
        return None


def secret_set(ref: str, value: str) -> None:
    keyring.set_password(SERVICE_NAME, ref, value)
