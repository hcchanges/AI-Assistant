"""
数据库连接与会话管理。

SQLite 文件由多个线程共用（界面线程 + 调度线程 + 任务线程），
因此建引擎时关掉同线程校验；SQLAlchemy 2.0 对文件型 SQLite 自带连接池，可跨线程使用。
"""

from __future__ import annotations

import logging
from pathlib import Path

from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.orm import Session, sessionmaker

from data.models import Base, PetProfile, User

logger = logging.getLogger(__name__)

# 本机主人的用户名（P0~P6 单用户，P8 接入登录后会被替换）
LOCAL_USER_NAME = "本机主人"


class Database:
    def __init__(self, db_path: Path, pet_name: str = "天渌", persona: str = "暖心呆萌"):
        self.path = db_path
        self.engine = create_engine(
            f"sqlite:///{db_path}", echo=False, connect_args={"check_same_thread": False}
        )
        self.session_factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        self.pet_name = pet_name
        self.persona = persona

    def init(self) -> None:
        """建表并保证本机主人与其宠物档案存在。"""
        Base.metadata.create_all(self.engine)
        self._add_missing_columns()
        with self.session_factory() as session:
            user = session.scalar(select(User).where(User.name == LOCAL_USER_NAME))
            if user is None:
                user = User(name=LOCAL_USER_NAME, role="local")
                session.add(user)
                session.flush()
                session.add(
                    PetProfile(user_id=user.id, pet_name=self.pet_name, persona=self.persona)
                )
                session.commit()
                logger.info("已初始化本机主人与宠物档案")

    def session(self) -> Session:
        return self.session_factory()

    def _add_missing_columns(self) -> None:
        """给已存在的表补上新加的列：create_all 只管建新表，不会改老表。

        只补可空的列（SQLite 加非空列必须有默认值，这里不猜默认值，交给调用方决定）。
        """
        inspector = inspect(self.engine)
        for table in Base.metadata.sorted_tables:
            actual = {column["name"] for column in inspector.get_columns(table.name)}
            for column in table.columns:
                if column.name in actual or not column.nullable:
                    continue
                ddl_type = column.type.compile(self.engine.dialect)
                with self.engine.begin() as conn:
                    conn.execute(
                        text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl_type}')
                    )
                logger.info("已给表 %s 补上列 %s", table.name, column.name)
