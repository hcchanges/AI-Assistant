"""
数据访问对象（DAO）。

上层只调用这里的方法，不直接写 SQL / 不用 Session。
将来升级到 B 阶段（远程服务）时，换掉这几个类的实现即可，模型与业务不动。
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import sessionmaker

from data.db import LOCAL_USER_NAME
from data.models import (
    ActivitySession,
    AppCategory,
    Card,
    CardCheckin,
    CardInvite,
    CardShare,
    CardTopicEvent,
    CustomCard,
    Memory,
    MoodEvent,
    PetProfile,
    PomodoroSession,
    Reminder,
    ServiceConsent,
    Utterance,
    User,
)

# 对象攻略卡那几个结构化格子。放这儿是因为读写两头都要认它：
# 建卡时（`CardDAO.add`）与后补时（`CardDAO.set_profile`）。
#
# 前四个是**手动填**的（v2-social-card-base，建卡框里那四行）；后四个是**后台静默补**
# 的（v2-social-card-autodistill，`life/mind/peer_distill.py` 抽出来直接调
# `CardDAO.set_profile` 写）。四个新的**不进建卡框**，但**必须在这张表里**——`set_profile`
# 就是按它过滤字段的，漏在这儿等于那四格写不进库（静默丢，一句日志都没有）。
_PROFILE_COLUMNS = (
    "likes", "dislikes", "strategy", "relation_stage",
    "age", "emotion_state", "anxiety", "interests",
)


def _profile_columns(profile: dict[str, str] | None) -> dict[str, str]:
    """把"对象档案"那本字典收成能直接丢给 `Card(...)` 的那几个关键字。

    认不出的键直接丢掉、空值不写（跟 `set_profile` 同一个口径）：批量传一本大字典
    进来不该把不相干的列一起写脏。`None` / 不是字典 → 一个关键字都不给，那几列留 NULL。
    """
    if not isinstance(profile, dict):
        return {}
    return {
        key: str(profile[key]).strip()
        for key in _PROFILE_COLUMNS
        if profile.get(key) is not None and str(profile[key]).strip()
    }


class UserDAO:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def local_user(self) -> User:
        with self._session_factory() as session:
            user = session.scalar(select(User).where(User.name == LOCAL_USER_NAME))
            if user is None:
                raise RuntimeError("数据库尚未初始化，未找到本机主人记录")
            return user


class PetProfileDAO:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def update(self, pet_name: str, persona: str) -> None:
        with self._session_factory() as session:
            profile = session.scalar(select(PetProfile).order_by(PetProfile.id))
            if profile is None:
                return
            profile.pet_name = pet_name
            profile.persona = persona
            session.commit()


class UtteranceDAO:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, user_id: int, raw_text: str) -> Utterance:
        with self._session_factory() as session:
            utter = Utterance(user_id=user_id, raw_text=raw_text)
            session.add(utter)
            session.commit()
            return utter

    def update_analysis(
        self,
        utterance_id: int,
        scene: str | None,
        time_hint: str | None,
        time_iso: datetime | None,
        emotion: str | None,
        module: str | None,
    ) -> None:
        with self._session_factory() as session:
            utter = session.get(Utterance, utterance_id)
            if utter is None:
                return
            utter.scene = scene
            utter.time_hint = time_hint
            utter.time_iso = time_iso
            utter.emotion = emotion
            utter.module = module
            session.commit()

    def count_all(self) -> int:
        """一共攒了多少条对话记录（清空前给用户过目用的）。"""
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Utterance)) or 0)

    def clear_all(self) -> int:
        """清空全部对话记录（v2-reset-state），返回删掉的条数。

        提醒表里那根 `utter_id` 指的是 utterance.id，删了对话它就指向一个不存在的
        行。**提醒本身要留着**（清的是记忆，不是提醒），所以先把这根线剪断再删对话——
        留下的提醒只是"不再挂在那句原话上"，内容、时间、状态一样不少。
        """
        with self._session_factory() as session:
            session.execute(update(Reminder).values(utter_id=None))
            result = session.execute(delete(Utterance))
            session.commit()
            return int(result.rowcount or 0)


class MemoryDAO:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(
        self,
        user_id: int,
        kind: str,
        content: str,
        importance: float,
        abs_time: datetime | None = None,
    ) -> Memory:
        with self._session_factory() as session:
            memory = Memory(
                user_id=user_id,
                kind=kind,
                content=content,
                importance=importance,
                abs_time=abs_time,
            )
            session.add(memory)
            session.commit()
            return memory

    def recent(self, limit: int = 50) -> list[Memory]:
        with self._session_factory() as session:
            stmt = select(Memory).order_by(Memory.created_at.desc()).limit(limit)
            return list(session.scalars(stmt))

    def by_ids(self, ids: list[int]) -> list[Memory]:
        if not ids:
            return []
        with self._session_factory() as session:
            return list(session.scalars(select(Memory).where(Memory.id.in_(ids))))

    def delete(self, memory_id: int) -> None:
        with self._session_factory() as session:
            session.execute(delete(Memory).where(Memory.id == memory_id))
            session.commit()

    def count_all(self) -> int:
        """一共记住多少条（清空前给用户过目用的）。"""
        with self._session_factory() as session:
            return int(session.scalar(select(func.count()).select_from(Memory)) or 0)

    def clear_all(self) -> int:
        """清空全部长期记忆（v2-reset-state），返回删掉的条数。

        只删表里的行，**向量库那半边归调用方清**（memory/vector.py 的 clear）——
        正文在这儿、索引在那儿，DAO 不该跨出自己这一层去动 chroma。
        """
        with self._session_factory() as session:
            result = session.execute(delete(Memory))
            session.commit()
            return int(result.rowcount or 0)


class ReminderDAO:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, user_id: int, trigger_at: datetime, content: str, utter_id: int | None) -> Reminder:
        with self._session_factory() as session:
            reminder = Reminder(
                user_id=user_id, trigger_at=trigger_at, content=content, utter_id=utter_id
            )
            session.add(reminder)
            session.commit()
            return reminder

    def get(self, reminder_id: int) -> Reminder | None:
        with self._session_factory() as session:
            return session.get(Reminder, reminder_id)

    def list_all(self, limit: int = 50) -> list[Reminder]:
        """界面用：按触发时间倒序展示全部提醒。"""
        with self._session_factory() as session:
            stmt = select(Reminder).order_by(Reminder.trigger_at.desc()).limit(limit)
            return list(session.scalars(stmt))

    def count_fired_since(self, since: datetime) -> int:
        """统计某时刻之后已弹屏的提醒条数，用于限制打扰频率。"""
        with self._session_factory() as session:
            stmt = select(Reminder).where(
                Reminder.status == "fired", Reminder.fired_at >= since
            )
            return len(list(session.scalars(stmt)))

    def pending(self) -> list[Reminder]:
        with self._session_factory() as session:
            stmt = select(Reminder).where(Reminder.status == "pending").order_by(Reminder.trigger_at)
            return list(session.scalars(stmt))

    def delete(self, reminder_id: int) -> None:
        with self._session_factory() as session:
            session.execute(delete(Reminder).where(Reminder.id == reminder_id))
            session.commit()

    def set_status(self, reminder_id: int, status: str, fired_at: datetime | None = None) -> None:
        with self._session_factory() as session:
            reminder = session.get(Reminder, reminder_id)
            if reminder is None:
                return
            reminder.status = status
            if fired_at is not None:
                reminder.fired_at = fired_at
            session.commit()


class PomodoroSessionDAO:
    """番茄钟会话读写与今日统计（对应指令 1 第 5 节）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def start(self, user_id: int, kind: str, planned_sec: int) -> PomodoroSession:
        with self._session_factory() as session:
            record = PomodoroSession(
                user_id=user_id,
                kind=kind,
                planned_sec=planned_sec,
                status="running",
                started_at=datetime.now(),
            )
            session.add(record)
            session.commit()
            return record

    def finish(self, session_id: int, actual_sec: int, status: str) -> None:
        with self._session_factory() as session:
            record = session.get(PomodoroSession, session_id)
            if record is None:
                return
            record.actual_sec = actual_sec
            record.status = status
            record.ended_at = datetime.now()
            session.commit()

    def update_actual(self, session_id: int, actual_sec: int) -> None:
        """只更新已专注时长，不结算这条记录（关程序时先把已专注的时长记下来）。"""
        with self._session_factory() as session:
            record = session.get(PomodoroSession, session_id)
            if record is None:
                return
            record.actual_sec = actual_sec
            session.commit()

    def latest_running(self) -> PomodoroSession | None:
        """最近一条还没结算的会话（上次程序没正常结束、或退出时正在进行的那条）。"""
        with self._session_factory() as session:
            stmt = (
                select(PomodoroSession)
                .where(PomodoroSession.status == "running")
                .order_by(PomodoroSession.started_at.desc())
                .limit(1)
            )
            return session.scalars(stmt).first()

    def today_range(self) -> tuple[datetime, datetime]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def today_completed_focus_count(self) -> int:
        """今日完成的番茄数（只算走完的专注阶段）。"""
        start, end = self.today_range()
        with self._session_factory() as session:
            stmt = select(func.count()).select_from(PomodoroSession).where(
                PomodoroSession.kind == "focus",
                PomodoroSession.status == "completed",
                PomodoroSession.started_at >= start,
                PomodoroSession.started_at < end,
            )
            return int(session.scalar(stmt) or 0)

    def today_focus_seconds(self) -> int:
        """今日累计专注时长（含未走完的部分，按实际时长算）。"""
        start, end = self.today_range()
        with self._session_factory() as session:
            stmt = select(func.coalesce(func.sum(PomodoroSession.actual_sec), 0)).where(
                PomodoroSession.kind == "focus",
                PomodoroSession.started_at >= start,
                PomodoroSession.started_at < end,
            )
            return int(session.scalar(stmt) or 0)

    def today_hourly_focus_seconds(self) -> list[tuple[int, int]]:
        """今日各小时的专注秒数，返回 [(小时, 秒), ...]，只含有时长的小时。"""
        start, end = self.today_range()
        buckets = [0] * 24
        with self._session_factory() as session:
            stmt = select(PomodoroSession).where(
                PomodoroSession.kind == "focus",
                PomodoroSession.started_at >= start,
                PomodoroSession.started_at < end,
            )
            for record in session.scalars(stmt):
                buckets[record.started_at.hour] += max(0, record.actual_sec)
        return [(hour, seconds) for hour, seconds in enumerate(buckets) if seconds > 0]

    def recent(self, limit: int = 20) -> list[PomodoroSession]:
        with self._session_factory() as session:
            stmt = (
                select(PomodoroSession)
                .order_by(PomodoroSession.started_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt))


class ActivitySessionDAO:
    """前台应用使用记录读写与统计（对应指令 5 第 7 节 / 指令 6 第 2 节）。

    只存应用进程名与类别；同一应用连续使用可能被拆成多段（每 5 分钟落一次盘），
    所以统计一律用 SUM 聚合，不能假设"一个应用一天只有一条"。
    """

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def record(
        self, user_id: int, proc: str, category: str, start_at: datetime, end_at: datetime
    ) -> None:
        """写入一段使用记录。调用方负责把跨零点的那段切开。"""
        seconds = int((end_at - start_at).total_seconds())
        if seconds <= 0:
            return
        with self._session_factory() as session:
            session.add(
                ActivitySession(
                    user_id=user_id,
                    proc=proc,
                    category=category,
                    start_at=start_at,
                    end_at=end_at,
                    seconds=seconds,
                )
            )
            session.commit()

    def today_range(self) -> tuple[datetime, datetime]:
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)

    def today_top_apps(self, limit: int = 10) -> list[tuple[str, int]]:
        """今日各应用用时（秒），按时长降序。"""
        start, end = self.today_range()
        with self._session_factory() as session:
            stmt = (
                select(ActivitySession.proc, func.sum(ActivitySession.seconds))
                .where(ActivitySession.start_at >= start, ActivitySession.start_at < end)
                .group_by(ActivitySession.proc)
                .order_by(func.sum(ActivitySession.seconds).desc())
                .limit(limit)
            )
            return [(proc, int(seconds or 0)) for proc, seconds in session.execute(stmt)]

    def today_total_seconds(self) -> int:
        """今日总用时（秒）。"""
        start, end = self.today_range()
        with self._session_factory() as session:
            stmt = select(func.coalesce(func.sum(ActivitySession.seconds), 0)).where(
                ActivitySession.start_at >= start, ActivitySession.start_at < end
            )
            return int(session.scalar(stmt) or 0)

    def today_longest_segment(self) -> tuple[str, int] | None:
        """今日连续使用最久的一段（用来指"专注时段"）。"""
        start, end = self.today_range()
        with self._session_factory() as session:
            stmt = (
                select(ActivitySession.proc, ActivitySession.seconds)
                .where(ActivitySession.start_at >= start, ActivitySession.start_at < end)
                .order_by(ActivitySession.seconds.desc())
                .limit(1)
            )
            row = session.execute(stmt).first()
            return (row[0], int(row[1])) if row is not None else None

    def procs_since(self, days: int) -> list[str]:
        """最近这些天出现过的应用（设置页给应用指定类别时用），按出现次数降序。"""
        since = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(
            days=max(0, days - 1)
        )
        with self._session_factory() as session:
            stmt = (
                select(ActivitySession.proc, func.count())
                .where(ActivitySession.start_at >= since)
                .group_by(ActivitySession.proc)
                .order_by(func.count().desc())
            )
            return [proc for proc, _ in session.execute(stmt)]

    def cleanup_before(self, cutoff: datetime) -> int:
        """删掉 cutoff 之前的记录，返回删除条数。"""
        with self._session_factory() as session:
            result = session.execute(delete(ActivitySession).where(ActivitySession.end_at < cutoff))
            session.commit()
            return int(result.rowcount or 0)

    def clear_all(self) -> None:
        """一键清空历史（隐私要求：用户随时能把记录抹掉）。"""
        with self._session_factory() as session:
            session.execute(delete(ActivitySession))
            session.commit()


class AppCategoryDAO:
    """用户给某个应用指定的类别，覆盖内置分类。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def get_map(self) -> dict[str, str]:
        """返回 {进程名小写: 类别}，供分类时查表。"""
        with self._session_factory() as session:
            return {item.proc: item.category for item in session.scalars(select(AppCategory))}

    def set_category(self, user_id: int, proc: str, category: str) -> None:
        key = proc.lower()
        with self._session_factory() as session:
            existing = session.scalar(select(AppCategory).where(AppCategory.proc == key))
            if existing is None:
                session.add(AppCategory(user_id=user_id, proc=key, category=category))
            else:
                existing.category = category
            session.commit()

    def remove(self, proc: str) -> None:
        """取消指定，回到内置分类。"""
        with self._session_factory() as session:
            session.execute(delete(AppCategory).where(AppCategory.proc == proc.lower()))
            session.commit()


class CardDAO:
    """生活卡牌读写（对应指令 12）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(
        self,
        user_id: int,
        title: str,
        theme: str,
        summary: str,
        source_memory_ids: list[int],
        features: list[str],
        goal: str = "",
        persona_id: str | None = None,
        icon: str | None = None,
        profile: dict[str, str] | None = None,
    ) -> Card:
        now = datetime.now()
        with self._session_factory() as session:
            top = session.scalar(select(func.max(Card.plaza_pos))) or 0
            card = Card(
                user_id=user_id,
                title=title,
                theme=theme,
                summary=summary,
                goal=goal,
                source_memory_ids=",".join(str(mid) for mid in source_memory_ids),
                features=",".join(features),
                persona_id=persona_id,
                icon=icon,
                status="active",
                plaza_pos=int(top) + 1,
                created_at=now,
                updated_at=now,
                # 对象档案那几格（v2-social-card-base / -autodistill）：不传就是 NULL，
                # 九类卡一视同仁
                **_profile_columns(profile),
            )
            session.add(card)
            session.commit()
            return card

    def get(self, card_id: int) -> Card | None:
        with self._session_factory() as session:
            return session.get(Card, card_id)

    def by_theme(self, theme: str) -> Card | None:
        """这个主题是不是已经有卡了（有就别再提议）。"""
        with self._session_factory() as session:
            return session.scalars(select(Card).where(Card.theme == theme).limit(1)).first()

    def list_all(self) -> list[Card]:
        """生活广场的展示顺序。"""
        with self._session_factory() as session:
            stmt = select(Card).order_by(Card.plaza_pos, Card.id)
            return list(session.scalars(stmt))

    def list_by_archived(self, archived: bool) -> list[Card]:
        """在轮播上的（archived 假）还是收进卡册的（archived 真）。

        老库里这一列是 NULL，NULL 一律当"没归档"：`== 0` 在 SQLite 里匹配不上 NULL，
        所以按"取反"来筛，别写成等值比较。
        """
        with self._session_factory() as session:
            stmt = select(Card).order_by(Card.plaza_pos, Card.id)
            return [
                card
                for card in session.scalars(stmt)
                if bool(card.archived) is archived
            ]

    def set_title(self, card_id: int, title: str) -> None:
        """改卡名（只给预设卡按新模板归位用，界面没有改名入口）。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.title = title
            card.updated_at = datetime.now()
            session.commit()

    def set_kb_path(self, card_id: int, kb_path: str | None) -> None:
        """记下这张卡的专属知识库目录（v2-card-kb）。建卡时填一次，之后不再动。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.kb_path = kb_path
            card.updated_at = datetime.now()
            session.commit()

    def set_profile(self, card_id: int, **fields: str) -> None:
        """写这张卡的对象档案那几格（v2-social-card-base 的四格 + v2-social-card-autodistill
        的四格）：喜好 / 禁忌 / 攻略策略 / 关系阶段 / 年龄 / 情感状态 / 焦虑原因 / 兴趣爱好。

        只更新这次给到的（`None` / 空串跳过），其余留着——跟 `MindStore.save_profile`
        一个口径：聊到一点补一点，别把上一轮填的抹掉。卡不存在就什么都不做。
        """
        patch = {
            key: str(value).strip()
            for key, value in fields.items()
            if key in _PROFILE_COLUMNS and value is not None and str(value).strip()
        }
        if not patch:
            return
        with self._session_factory() as session:
            session.execute(
                update(Card)
                .where(Card.id == card_id)
                .values(**patch, updated_at=datetime.now())
            )
            session.commit()

    def set_archived(self, card_id: int, archived: bool) -> None:
        """收进卡册 / 从卡册拉回（指令 25-A 第 4 项）。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.archived = archived
            card.updated_at = datetime.now()
            session.commit()

    def delete(self, card_id: int) -> bool:
        """把一张卡**永久**删掉，连同它的打卡与分享记录；返回删掉没有。

        跟 set_archived 分工：归档是"收进卡册、随时能拉回来"，这条是"这张不要了"。
        轮播上的 X 删除键走这儿（v2-card-del）。打卡（card_checkin）与分享（card_share）
        都挂 card_id 外键，先删它们再删卡，别在库里留一串孤儿行。
        """
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return False
            session.execute(delete(CardCheckin).where(CardCheckin.card_id == card_id))
            session.execute(delete(CardShare).where(CardShare.card_id == card_id))
            session.delete(card)
            session.commit()
            return True

    def set_status(self, card_id: int, status: str) -> None:
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.status = status
            card.updated_at = datetime.now()
            session.commit()

    def set_goal(self, card_id: int, goal: str) -> None:
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.goal = goal
            card.updated_at = datetime.now()
            session.commit()

    def set_persona(self, card_id: int, persona_id: str | None) -> None:
        """改这张卡的陪执行角色（空 = 宠物本体）。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.persona_id = persona_id
            card.updated_at = datetime.now()
            session.commit()

    def set_trip(self, card_id: int, destination: str, trip_date: datetime | None) -> None:
        """记下这张出行卡的目的地与出发日期（指令 24 链 a）。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.trip_destination = destination
            card.trip_date = trip_date
            card.updated_at = datetime.now()
            session.commit()

    def set_last_active(self, card_id: int, at: datetime | None = None) -> None:
        """记一次"这孩子还活着"（指令 25-B 第 4 项）：打卡、在卡上说话都算。"""
        with self._session_factory() as session:
            card = session.get(Card, card_id)
            if card is None:
                return
            card.last_active_at = at or datetime.now()
            session.commit()


class CardCheckinDAO:
    """卡牌打卡记录（对应指令 12 的"打卡 / 进度"能力）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, card_id: int, note: str = "") -> CardCheckin:
        with self._session_factory() as session:
            record = CardCheckin(card_id=card_id, note=note, created_at=datetime.now())
            session.add(record)
            session.commit()
            return record

    def recent(self, card_id: int, limit: int = 20) -> list[CardCheckin]:
        with self._session_factory() as session:
            stmt = (
                select(CardCheckin)
                .where(CardCheckin.card_id == card_id)
                .order_by(CardCheckin.created_at.desc())
                .limit(limit)
            )
            return list(session.scalars(stmt))

    def counts_by_card(self) -> dict[int, int]:
        """每张卡各打卡多少次，一次查完（广场上要显示 N 张卡的进度）。"""
        with self._session_factory() as session:
            stmt = select(CardCheckin.card_id, func.count()).group_by(CardCheckin.card_id)
            return {card_id: int(count) for card_id, count in session.execute(stmt)}


class CardTopicEventDAO:
    """主题观察记录读写（指令 13 阈值模型的原料）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(
        self,
        user_id: int,
        theme: str,
        topic: str,
        emotion_score: float,
        text: str,
        negative: bool = False,
    ) -> CardTopicEvent:
        with self._session_factory() as session:
            event = CardTopicEvent(
                user_id=user_id,
                theme=theme,
                topic=topic,
                emotion_score=emotion_score,
                negative=negative,
                text=text,
                created_at=datetime.now(),
            )
            session.add(event)
            session.commit()
            return event

    def since(self, theme: str, since: datetime) -> list[CardTopicEvent]:
        """某个主题在窗口内的记录，按时间正序（通道 B 要按顺序算会话间隔）。"""
        with self._session_factory() as session:
            stmt = (
                select(CardTopicEvent)
                .where(CardTopicEvent.theme == theme, CardTopicEvent.created_at >= since)
                .order_by(CardTopicEvent.created_at)
            )
            return list(session.scalars(stmt))

    def prune(self, before: datetime) -> int:
        """删掉已经出了窗口的记录：它们不再影响任何判定。"""
        with self._session_factory() as session:
            result = session.execute(delete(CardTopicEvent).where(CardTopicEvent.created_at < before))
            session.commit()
            return int(result.rowcount or 0)

    def retag(self, event_id: int, theme: str, negative: bool) -> None:
        """把一条"未分类"的记录改归到模型判出来的品类（指令 25-B 第 1 项 ②）。"""
        with self._session_factory() as session:
            event = session.get(CardTopicEvent, event_id)
            if event is None:
                return
            event.theme = theme
            event.negative = negative
            session.commit()


class CardInviteDAO:
    """邀请记录读写（指令 25-B 第 3 项的限频硬规则）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, user_id: int, theme: str) -> CardInvite:
        """记一次邀请，先不写结果（用户答复或超时都留 None）。"""
        with self._session_factory() as session:
            row = CardInvite(user_id=user_id, theme=theme, created_at=datetime.now())
            session.add(row)
            session.commit()
            return row

    def latest(self, user_id: int, theme: str) -> CardInvite | None:
        """这个品类最近一次邀请。"""
        with self._session_factory() as session:
            stmt = (
                select(CardInvite)
                .where(CardInvite.user_id == user_id, CardInvite.theme == theme)
                .order_by(CardInvite.created_at.desc())
                .limit(1)
            )
            return session.scalars(stmt).first()

    def resolve(self, user_id: int, theme: str, accepted: bool) -> None:
        """用户答复了：把该品类最近一条还没答复的邀请标上结果。"""
        with self._session_factory() as session:
            stmt = (
                select(CardInvite)
                .where(
                    CardInvite.user_id == user_id,
                    CardInvite.theme == theme,
                    CardInvite.accepted.is_(None),
                )
                .order_by(CardInvite.created_at.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                return
            row.accepted = accepted
            session.commit()


# 重情绪记录的保留窗口（指令 26-A）：与"同一主题连着第二次"的判定窗口是同一个
MOOD_WINDOW = timedelta(days=7)


class MoodEventDAO:
    """重情绪记录的读写（指令 26-A 第 3 项）：只为"连着第二次重下来"这一件事服务。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, user_id: int, topic: str, text: str) -> MoodEvent:
        """记一条重情绪；顺手清掉窗口外的旧账（那些再也不会被用到）。"""
        now = datetime.now()
        with self._session_factory() as session:
            session.execute(delete(MoodEvent).where(MoodEvent.created_at < now - MOOD_WINDOW))
            row = MoodEvent(user_id=user_id, topic=topic, text=text, created_at=now)
            session.add(row)
            session.commit()
            return row

    def count_since(self, user_id: int, topic: str, since: datetime) -> int:
        """这个主题从 since 那会儿起，一共重了几次。"""
        with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(MoodEvent)
                .where(
                    MoodEvent.user_id == user_id,
                    MoodEvent.topic == topic,
                    MoodEvent.created_at >= since,
                )
            )
            return int(session.scalar(stmt) or 0)

    def last_cared_at(self, user_id: int, topic: str) -> datetime | None:
        """这个主题上一次主动关怀是什么时候：刚问过就先别再提。"""
        with self._session_factory() as session:
            stmt = (
                select(MoodEvent.created_at)
                .where(
                    MoodEvent.user_id == user_id,
                    MoodEvent.topic == topic,
                    MoodEvent.cared.is_(True),
                )
                .order_by(MoodEvent.created_at.desc())
                .limit(1)
            )
            return session.scalars(stmt).first()

    def mark_cared(self, user_id: int, topic: str) -> None:
        """把该主题最近一条标成"已经关怀过"。"""
        with self._session_factory() as session:
            stmt = (
                select(MoodEvent)
                .where(MoodEvent.user_id == user_id, MoodEvent.topic == topic)
                .order_by(MoodEvent.created_at.desc())
                .limit(1)
            )
            row = session.scalars(stmt).first()
            if row is None:
                return
            row.cared = True
            session.commit()


class ServiceConsentDAO:
    """主动弹卡的渐进授权读写（指令 26-B 修订）：点头次数 + 上次拒绝时刻。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def state(self, user_id: int, theme: str) -> tuple[int, datetime | None]:
        """(连着点了几次头, 上次拒绝的时刻)；没有记录就是 (0, None)。"""
        with self._session_factory() as session:
            row = self._find(session, user_id, theme)
            return (row.accepts, row.declined_at) if row is not None else (0, None)

    def accept(self, user_id: int, theme: str) -> int:
        """点头一次，返回攒到几次（攒够了就不再问、以后直接弹）。"""
        with self._session_factory() as session:
            row = self._find(session, user_id, theme)
            if row is None:
                row = ServiceConsent(user_id=user_id, theme=theme)
                session.add(row)
            row.accepts = (row.accepts or 0) + 1
            row.declined_at = None          # 点头了，之前那次拒绝就不算数了
            row.updated_at = datetime.now()
            session.commit()
            return row.accepts

    def decline(self, user_id: int, theme: str) -> None:
        """拒绝：点头次数归零，并记下时刻（之后静默一阵，再从头问）。"""
        with self._session_factory() as session:
            row = self._find(session, user_id, theme)
            if row is None:
                row = ServiceConsent(user_id=user_id, theme=theme)
                session.add(row)
            row.accepts = 0
            row.declined_at = datetime.now()
            row.updated_at = datetime.now()
            session.commit()

    @staticmethod
    def _find(session, user_id: int, theme: str) -> ServiceConsent | None:
        stmt = select(ServiceConsent).where(
            ServiceConsent.user_id == user_id, ServiceConsent.theme == theme
        )
        return session.scalars(stmt).first()


class CardShareDAO:
    """卡牌分享快照读写（只存公开部分）。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def add(self, card_id: int, share_id: str, snapshot_public: str) -> CardShare:
        with self._session_factory() as session:
            row = CardShare(
                card_id=card_id,
                share_id=share_id,
                snapshot_public=snapshot_public,
                created_at=datetime.now(),
            )
            session.add(row)
            session.commit()
            return row

    def get(self, share_id: str) -> CardShare | None:
        with self._session_factory() as session:
            stmt = select(CardShare).where(CardShare.share_id == share_id)
            return session.scalars(stmt).first()


class CustomCardDAO:
    """员工自建的工作卡读写（指令 21）：按部门分组，界面按摆放顺序排。"""

    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def list_by_dept(self, dept_id: str) -> list[CustomCard]:
        with self._session_factory() as session:
            stmt = (
                select(CustomCard)
                .where(CustomCard.dept_id == dept_id)
                .order_by(CustomCard.sort, CustomCard.id)
            )
            return list(session.scalars(stmt))

    def get(self, card_id: int) -> CustomCard | None:
        with self._session_factory() as session:
            return session.get(CustomCard, card_id)

    def count_by_dept(self, dept_id: str) -> int:
        with self._session_factory() as session:
            stmt = (
                select(func.count())
                .select_from(CustomCard)
                .where(CustomCard.dept_id == dept_id)
            )
            return int(session.scalar(stmt) or 0)

    def add(self, dept_id: str, title: str, description: str, icon: str) -> CustomCard:
        """新建一张：摆在同部门已有自建卡的最后面。"""
        with self._session_factory() as session:
            top = session.scalar(
                select(func.max(CustomCard.sort)).where(CustomCard.dept_id == dept_id)
            )
            card = CustomCard(
                dept_id=dept_id,
                title=title,
                description=description,
                icon=icon,
                sort=int(top or 0) + 1,
                created_at=datetime.now(),
            )
            session.add(card)
            session.commit()
            return card

    def update(self, card_id: int, title: str, description: str, icon: str) -> None:
        with self._session_factory() as session:
            card = session.get(CustomCard, card_id)
            if card is None:
                return
            card.title = title
            card.description = description
            card.icon = icon
            session.commit()

    def delete(self, card_id: int) -> None:
        with self._session_factory() as session:
            session.execute(delete(CustomCard).where(CustomCard.id == card_id))
            session.commit()
