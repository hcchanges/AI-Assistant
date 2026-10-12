"""
ORM 模型（SQLAlchemy 2.0）。

对应技术方案第 5 节。P0~P6 阶段只落地表：
User / PetProfile / Utterance / Memory / Reminder / Pomodoro / Activity / AppCategory / Card。
Workflow 系列（P8 工作模块）与 LifeModuleConfig（P7 生活子模块开关）暂不建表
——六大子项的固定入口已在指令 12 里被生活卡牌取代。
指令 21 加了 CustomCard：工作模块里员工自己建的卡（流程本身仍然只存在 flow_data.json）。
向量本身由 Chroma 自己持久化，不再单独建 MemoryVector 表，避免一份数据存两处。
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class User(Base):
    """账号与角色。P0~P6 只有本机主人一个用户，登录/权限在 P8 落地。"""

    __tablename__ = "user"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), default="local")  # local / admin / employee
    password_hash: Mapped[str | None] = mapped_column(String(128), default=None)
    department: Mapped[str | None] = mapped_column(String(64), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class PetProfile(Base):
    """宠物档案：名字与人格设定。"""

    __tablename__ = "pet_profile"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    pet_name: Mapped[str] = mapped_column(String(64))
    avatar: Mapped[str | None] = mapped_column(String(255), default=None)  # 预留：换成立绘时的资源路径
    persona: Mapped[str] = mapped_column(String(64), default="暖心呆萌")


class Utterance(Base):
    """一条吐嘈，以及从它抽取出的情境/时间/情绪/关联子模块。"""

    __tablename__ = "utterance"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    raw_text: Mapped[str] = mapped_column(Text)
    scene: Mapped[str | None] = mapped_column(String(64), default=None)
    time_hint: Mapped[str | None] = mapped_column(String(64), default=None)   # 原文里的时间说法，如"下班后"
    time_iso: Mapped[datetime | None] = mapped_column(DateTime, default=None)  # 解析成的具体时刻
    emotion: Mapped[str | None] = mapped_column(String(32), default=None)
    module: Mapped[str | None] = mapped_column(String(32), default=None)  # 话题归类：生活子模块 / 工作 / 其他
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Memory(Base):
    """长期记忆（结构化）。语义检索靠 Chroma，这里存正文与元信息。"""

    __tablename__ = "memory"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    kind: Mapped[str] = mapped_column(String(16), default="fact")  # fact / preference / event
    content: Mapped[str] = mapped_column(Text)
    importance: Mapped[float] = mapped_column(Float, default=0.5)  # 0~1，越高越重要
    # 正文里相对时间说法（明早/下周三…）按"说话当天"锚定出的绝对时刻；没说时间则为空
    abs_time: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class Reminder(Base):
    """主动提醒。status: pending 待触发 / fired 已触发 / muted 已静音 / cancelled 已取消"""

    __tablename__ = "reminder"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    trigger_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    content: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    utter_id: Mapped[int | None] = mapped_column(ForeignKey("utterance.id"), default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    fired_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class PomodoroSession(Base):
    """番茄钟会话（对应指令 1 第 5 节）。

    kind: focus 专注 / short_break 短休 / long_break 长休
    status: running 进行中 / completed 走完 / skipped 被跳过 / aborted 被中止
    """

    __tablename__ = "pomodoro_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    kind: Mapped[str] = mapped_column(String(16))
    planned_sec: Mapped[int] = mapped_column(Integer, default=0)
    actual_sec: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="running")
    started_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)


class ActivitySession(Base):
    """前台应用使用记录（对应指令 5 第 7 节 / 指令 6 第 2 节）。

    只记应用进程名与类别，不记窗口标题；同一个应用连续使用会归并成一段。
    跨零点的那一段会在午夜切开，保证"今日用时"按天算得准。
    """

    __tablename__ = "activity_session"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    proc: Mapped[str] = mapped_column(String(64), index=True)
    category: Mapped[str] = mapped_column(String(16), default="其他")
    start_at: Mapped[datetime] = mapped_column(DateTime, index=True)
    end_at: Mapped[datetime] = mapped_column(DateTime)
    seconds: Mapped[int] = mapped_column(Integer, default=0)


class AppCategory(Base):
    """用户给某个应用指定的类别（覆盖内置分类）。"""

    __tablename__ = "app_category"
    __table_args__ = (UniqueConstraint("user_id", "proc"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    proc: Mapped[str] = mapped_column(String(64))
    category: Mapped[str] = mapped_column(String(16))


class Card(Base):
    """生活卡牌（对应指令 12）：围绕用户真实需求的一张"活的功能单元"。

    source_memory_ids / features 用逗号分隔存字符串：都是短小的 id 与能力名列表，
    单独建关系表不值当。
    status: active 进行中 / done 已完成 / paused 暂停
    """

    __tablename__ = "card"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    title: Mapped[str] = mapped_column(String(64))
    theme: Mapped[str] = mapped_column(String(32))          # 主题标签，取值见 life/cards/templates.py
    summary: Mapped[str] = mapped_column(Text, default="")  # 从对话提炼的相关背景（素材来自记忆）
    goal: Mapped[str] = mapped_column(Text, default="")     # 目标 / 阶段，可空
    source_memory_ids: Mapped[str] = mapped_column(String(255), default="")
    features: Mapped[str] = mapped_column(String(255), default="")  # 启用哪些 AI 能力
    persona_id: Mapped[str | None] = mapped_column(String(32), default=None)  # 陪执行角色，空=宠物本体
    # 出行卡专用（指令 24 链 a）：目的地与出发日期，填了才谈得上"规划"；
    # 减脂卡判断"近期出行"另看共享画像里的记忆，不看这里
    trip_destination: Mapped[str | None] = mapped_column(String(64), default=None)
    trip_date: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    # 卡面图标（指令 25-A 第 3 项）：只有自建卡会带；空 = 不画图标
    icon: Mapped[str | None] = mapped_column(String(8), default=None)
    # ---------- 对象档案（v2-social-card-base） ----------
    # 对象攻略卡专用的四个结构化格子：原先对方信息只有《对方信息.md》那一整段自由文本，
    # 帮聊那条线要按"喜好 / 禁忌 / 攻略策略 / 到哪一步了"分开取用，自由文本没法分开取。
    #
    # **四列都可以为空**：只有对象攻略卡会填，其余八类卡建出来就是 NULL；老库那批卡
    # 同样全是 NULL（`data/db.py::_add_missing_columns` 只给老表补可空列，所以这是
    # 加列即兼容，不需要另写迁移）。
    likes: Mapped[str | None] = mapped_column(Text, default=None)
    dislikes: Mapped[str | None] = mapped_column(Text, default=None)
    strategy: Mapped[str | None] = mapped_column(Text, default=None)
    relation_stage: Mapped[str | None] = mapped_column(Text, default=None)
    # ---------- 对象档案·后台补全那四格（v2-social-card-autodistill） ----------
    # 上一组四格是**手动填**的（建卡框里那四行）；这四格是**后台静默补**的——用户早
    # 就把对象攻略卡开好了，聊着聊着宠物把"对面这个人是谁"一点点抽出来填进这儿。
    # 所以它们**不进建卡框**（`ui/card_dialogs.py::PROFILE_FIELDS` 一个字没动）：
    # 手动那条路照旧只有四行，这四格只由 `life/mind/peer_distill.py` 写。
    #
    # 同样**四列都可以为空**，且理由与上面那四列一致：只有对象攻略卡会填，其余八类卡
    # 建出来就是 NULL，老库那批卡也都是 NULL——`data/db.py::_add_missing_columns` 只给
    # 老表补可空列，所以这还是"加列即兼容"，不需要另写迁移。
    age: Mapped[str | None] = mapped_column(Text, default=None)            # 年龄推断
    emotion_state: Mapped[str | None] = mapped_column(Text, default=None)  # 情感状态
    anxiety: Mapped[str | None] = mapped_column(Text, default=None)        # 焦虑 / 烦心的原因
    interests: Mapped[str | None] = mapped_column(Text, default=None)      # 兴趣爱好
    # 专属知识库目录（v2-card-kb）：这张卡对应的卡级 RAG 落点，默认 知识库/cards/<id>。
    # 空 = 老卡还没建（按默认约定路径现算）；建卡时由 CardService 顺手建目录并填上。
    kb_path: Mapped[str | None] = mapped_column(String(512), default=None)
    # 收进卡册（指令 25-A 第 4 项）：True = 不占轮播、躺在卡册里等拉回；None 等同未归档
    archived: Mapped[bool | None] = mapped_column(Boolean, default=None)
    # 最近一次"打卡 / 在卡上跟它说话"的时刻（指令 25-B 第 4 项）：自动归档按它算；
    # 空 = 建了卡之后一直没动过，那就退回按建卡时间算
    last_active_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    status: Mapped[str] = mapped_column(String(16), default="active")
    plaza_pos: Mapped[int] = mapped_column(Integer, default=0)  # 在生活广场里的摆放顺序
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)

    def memory_id_list(self) -> list[int]:
        """相关背景来自哪几条记忆。"""
        return [int(part) for part in self.source_memory_ids.split(",") if part.strip().isdigit()]

    def feature_list(self) -> list[str]:
        """这张卡开了哪些 AI 能力。"""
        return [part for part in self.features.split(",") if part.strip()]


class CardCheckin(Base):
    """卡牌打卡记录：一条 = 一次打卡，进度与时间线都从它算。"""

    __tablename__ = "card_checkin"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id"), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class CardTopicEvent(Base):
    """一条观察记录：某次吐嘈落在哪个主题族、情绪多强（指令 13 阈值模型的原料）。

    每说完一句被归到生活主题的话就记一条；同一句话里反复提到同一件事也只算一条（天然防刷）。
    超过 72 小时窗口的记录就没有用了，观察时会顺手清掉。
    """

    __tablename__ = "card_topic_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    theme: Mapped[str] = mapped_column(String(32), index=True)   # 主题族，取值同话题归类
    topic: Mapped[str] = mapped_column(String(64), default="")   # 这句话给这个族贡献的说法，如"减脂"
    emotion_score: Mapped[float] = mapped_column(Float, default=0.0)   # 0~1 的负向情绪强度
    # 算不算一次"负向"（指令 25-B 第 1 项）：命中本地词表的吐槽词就算；
    # 词表没命中、由模型判类进来的，得情绪分够重才算。空 = 老记录，按情绪分现算
    negative: Mapped[bool | None] = mapped_column(Boolean, default=None)
    text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class CardInvite(Base):
    """邀请记录（指令 25-B 第 3 项）：一次邀请一条，管住"多久能邀、还能不能再邀"。

    accepted 在用户答复时才写：None = 还没答（超时作废的也留 None），True = 受理建了卡，
    False = 明确拒绝（同一个品类 7 天内不再提）。
    """

    __tablename__ = "card_invite"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    theme: Mapped[str] = mapped_column(String(32), index=True)   # 品类，取值同品类判定
    accepted: Mapped[bool | None] = mapped_column(Boolean, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class MoodEvent(Base):
    """一条"重情绪"记录（指令 26-A 第 3 项）：崩溃、连续失眠那一档，每落一次记一条。

    只为一件事存在——同一主题在 7 天里第二次重下来时，主动问一句、提醒休息。
    这类话一律不进卡牌那条线（不把痛苦做成收藏品），所以单独一张表，
    跟"观察记录"（CardTopicEvent）分开，互不干扰。
    """

    __tablename__ = "mood_event"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    # 五个主题之一（加班 / 失眠 / 通勤 / 饮食挫败 / 情绪低落），见 life/psych/comfort.py
    topic: Mapped[str] = mapped_column(String(32), index=True)
    text: Mapped[str] = mapped_column(Text, default="")
    # 这条触发过主动关怀没有：同一个主题关怀过一次，先静默一阵再提
    cared: Mapped[bool | None] = mapped_column(Boolean, default=None)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now, index=True)


class ServiceConsent(Base):
    """主动弹卡的"渐进授权"账（指令 26-B 修订）：这个主题问过几次、点过几次头。

    生活卡本来一律"直接弹=服务"，可连着弹就成了打扰，所以改成先问：
    同一个主题问 POP_ASK_TIMES 次、每次都点头，之后就直接弹；
    拒绝一次就静默 POP_DECLINE_SILENCE，之后从头再问。
    存库是为了重启也算数——不然重启一次，刚攒的点头次数就白攒了。
    """

    __tablename__ = "service_consent"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id"))
    theme: Mapped[str] = mapped_column(String(32), index=True)
    # 连着点头的次数：攒够 POP_ASK_TIMES 就不再问、直接弹
    accepts: Mapped[int] = mapped_column(Integer, default=0)
    # 上次拒绝的时刻：之后静默一阵，不弹也不问
    declined_at: Mapped[datetime | None] = mapped_column(DateTime, default=None)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CustomCard(Base):
    """员工自建的工作卡（指令 21）：部门卡里自己加的卡，每个部门最多 3 张。

    只存卡片本身（标题 / 描述 / 图标 / 排序），功能页还没做，所以它只是这个部门里的一张自建卡；
    系统功能卡由工作模块自带的数据文件管着，不在这张表里，改不到也删不掉（公开版没有这个模块）。
    """

    __tablename__ = "custom_cards"

    id: Mapped[int] = mapped_column(primary_key=True)
    dept_id: Mapped[str] = mapped_column(String(32), index=True)
    title: Mapped[str] = mapped_column(String(32))
    # 列名按指令写成 desc（SQLite 的保留字，SQLAlchemy 会自动加引号）；属性名用 description 更好读
    description: Mapped[str] = mapped_column("desc", Text, default="")
    icon: Mapped[str] = mapped_column(String(8), default="")
    sort: Mapped[int] = mapped_column(Integer, default=0)   # 同一个部门里的摆放顺序，从 1 开始
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)


class CardShare(Base):
    """卡牌的对外分享快照（对应指令 13 第 7 节）。

    只存"可以给别人看"的部分：标题 / 主题 / 目标 / 能力开关。
    记忆、对话原文、来源记忆 id 一律不进这张表——这是隐私红线。
    """

    __tablename__ = "card_share"

    id: Mapped[int] = mapped_column(primary_key=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("card.id"))
    share_id: Mapped[str] = mapped_column(String(32), unique=True, index=True)
    snapshot_public: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.now)
