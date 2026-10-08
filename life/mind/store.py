"""情感记忆库的存储层（S1/S2）：SQLite + 逐字段 AES（Fernet）。

跟 pet.db 的关系：**没有关系**。这是个独立的库、独立的连接、独立的密钥，
既不读 pet.db 也不写 pet.db——本轮同时把对话写 pet.db 记忆表那条老链路掐了
（见 life/chatter.py），情感记忆从此只落在这里。

为什么整库加密：这里面装的是姓名、职业、家庭、关系状态、情绪和私生活碎片，
比 pet.db 上那些"几点提醒喝水"敏感得多。落盘一律是 Fernet 密文，
直接拿 sqlite 工具打开 data/mind.db 只能看到一串串 base64 令牌。

**明文只有这里这一份。** 碎片另有一份向量存在 chroma 里（见 index.py），
但那边只存向量和 fragment_id，不带正文——不然加密就白做了。

**B 养成那三张表（intimacy / star_cards / memory_book）走的是同一套规矩**，
不是另开的库：同一把钥匙、同一个连接、同一个 `_enc`。唯一放行明文的是
"拿来查的坐标"——id 与时间列（`last_interact` / `created_at` / `date`），
跟 `fragment.src_time` 一个道理；载荷一律密文，连 `level` / `exp` 这种数字也是，
因为它们放一起就说明"这人跟桌宠处到什么份上了"，本身就是关系数据。

密钥从哪来（绝不硬编码）：
1. 环境变量 ``PET_MIND_KEY``；
2. 系统凭据管理器（Windows 凭据管理器，凭据名 ``mind_db_key``）；
3. 两处都没有——首次运行时随机生成一把、存进凭据管理器，之后一直用它。

值本身是 Fernet 钥匙（44 位 urlsafe base64）就直接用；是句口令就按固定盐
派生一把（见 _coerce）。固定盐不是密码学上的最优解，但这里的目标是
"文件被人翻到也不明文"，不是抗离线爆破；为它多开一张表存随机盐，
反而把库的结构搞复杂了。真要提高强度，把环境变量设成一把随机 Fernet 钥匙即可。
"""

from __future__ import annotations

import base64
import json
import logging
import os
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

logger = logging.getLogger(__name__)

# 密钥的取处：环境变量名 / 凭据管理器里的凭据名
KEY_ENV = "PET_MIND_KEY"
KEY_REF = "mind_db_key"

# 口令派生用的固定盐与轮数（见模块开头那段取舍）
_KDF_SALT = b"desktoppet.mind.v1"
_KDF_ITERATIONS = 100_000

# 画像 / 关系总述 / 好感度都只有一行：它们描述的是"此刻是什么状态"，覆写式更新
_SINGLETON_ID = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS struct_profile (
    id          INTEGER PRIMARY KEY,
    name        TEXT,
    gender      TEXT,
    occupation  TEXT,
    family      TEXT,
    intro       TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS ep_summary (
    id               INTEGER PRIMARY KEY,
    rel_state        TEXT,
    emotion_snapshot TEXT,
    events_json      TEXT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS fragment (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    text     TEXT,
    src_time TEXT,
    tags     TEXT
);
CREATE TABLE IF NOT EXISTS intimacy (
    id            INTEGER PRIMARY KEY,
    level         TEXT,
    exp           TEXT,
    last_interact TEXT,
    unlocked      TEXT
);
CREATE TABLE IF NOT EXISTS star_cards (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    title      TEXT,
    kind       TEXT,
    source_id  TEXT,
    created_at TEXT,
    rarity     TEXT
);
CREATE TABLE IF NOT EXISTS memory_book (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    date    TEXT,
    summary TEXT,
    entries TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_book_date ON memory_book(date);
CREATE TABLE IF NOT EXISTS emotion_event (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    text        TEXT,
    emotion     TEXT,
    attribution TEXT,
    category    TEXT,
    src_time    TEXT
);
"""

# 三张表的可写字段（加密入库的那几列）
_PROFILE_FIELDS = ("name", "gender", "occupation", "family", "intro")


def _derive(passphrase: str) -> bytes:
    kdf = PBKDF2HMAC(
        algorithm=hashes.SHA256(),
        length=32,
        salt=_KDF_SALT,
        iterations=_KDF_ITERATIONS,
    )
    return base64.urlsafe_b64encode(kdf.derive(passphrase.encode("utf-8")))


def _coerce(raw: str) -> bytes:
    """把取到的那串东西变成 Fernet 钥匙：是钥匙就用，是口令就派生。"""
    text = raw.strip()
    if not text:
        raise ValueError("情感记忆库的密钥是空的")
    try:
        Fernet(text.encode("ascii"))
    except Exception:
        return _derive(text)
    return text.encode("ascii")


def resolve_key() -> bytes:
    """按 环境变量 → 凭据管理器 → 首次自举 的顺序取密钥。"""
    from core.config import secret_get, secret_set

    env_value = os.environ.get(KEY_ENV)
    if env_value:
        return _coerce(env_value)

    stored = secret_get(KEY_REF)
    if stored:
        return _coerce(stored)

    # 首启：没有就生成一把存进凭据管理器。不落配置文件、不硬编码。
    key = Fernet.generate_key()
    try:
        secret_set(KEY_REF, key.decode("ascii"))
        logger.info("情感记忆库：首次运行，已在系统凭据管理器里生成密钥")
    except Exception as exc:  # 凭据服务不可用（少见）：这把钥匙只活这一次，重启后旧数据读不出来
        logger.error("情感记忆库：密钥没法存进凭据管理器（%s），本次临时用一把", exc)
    return key


class MindStore:
    """情感记忆库。线程安全靠一把锁——后台提炼和界面读会同库。"""

    def __init__(self, db_path: Path | str, key: bytes | str | None = None):
        self.db_path = Path(db_path)
        self._key = _coerce(key) if isinstance(key, str) else (key or resolve_key())
        self._fernet = Fernet(self._key)
        self._lock = threading.Lock()
        # 向量索引（S1）：不在这里建，由 attach_index 后接上。理由是两个东西的
        # 重要程度不一样——加密库打不开就没得记，索引起不来只是检索少一条腿。
        self._index: Any = None

        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._conn.commit()

    def attach_index(self, index: Any) -> None:
        """接上向量索引（S1）。没接上就检索只走 SQL LIKE，功能不塌。"""
        self._index = index

    # ---------- 加解密 ----------

    def _enc(self, value: Any) -> str | None:
        """明文 → 密文令牌。None / 空串照原样留着，免得库里一堆空密文。"""
        if value is None:
            return None
        if isinstance(value, (list, dict)):
            value = json.dumps(value, ensure_ascii=False)
        text = str(value)
        if not text:
            return ""
        return self._fernet.encrypt(text.encode("utf-8")).decode("ascii")

    def _dec(self, token: Any) -> str:
        """密文令牌 → 明文。解不开（换过钥匙 / 手改过库）就当空，不抛给上层。"""
        if not token:
            return ""
        try:
            return self._fernet.decrypt(str(token).encode("ascii")).decode("utf-8")
        except (InvalidToken, ValueError) as exc:
            logger.warning("情感记忆库：有条记录解不开（%s），跳过", type(exc).__name__)
            return ""

    def _dec_json(self, token: Any) -> list:
        text = self._dec(token)
        if not text:
            return []
        try:
            value = json.loads(text)
        except json.JSONDecodeError:
            return []
        return value if isinstance(value, list) else []

    # ---------- 结构画像 ----------

    def save_profile(self, **fields: Any) -> None:
        """覆写式写入已知项。只更新这次给到的字段，其余留着（聊到一点补一点）。"""
        values = {k: v for k, v in fields.items() if k in _PROFILE_FIELDS}
        if not values:
            return
        encrypted = {k: self._enc(v) for k, v in values.items()}
        assignments = ", ".join(f"{k} = ?" for k in encrypted)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO struct_profile (id) VALUES (?)", (_SINGLETON_ID,)
            )
            self._conn.execute(
                f"UPDATE struct_profile SET {assignments}, updated_at = ? WHERE id = ?",
                (*encrypted.values(), _stamp(), _SINGLETON_ID),
            )
            self._conn.commit()

    def profile(self) -> dict[str, str]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM struct_profile WHERE id = ?", (_SINGLETON_ID,)
            ).fetchone()
        if row is None:
            return {}
        known = {k: self._dec(row[k]) for k in _PROFILE_FIELDS}
        return {k: v for k, v in known.items() if v}

    # ---------- 关系 / 情绪 / 事件（覆写更新） ----------

    def save_ep(
        self,
        rel_state: str = "",
        emotion_snapshot: str = "",
        events: Sequence[str] | None = None,
    ) -> None:
        """写关系记忆库 / 情绪记忆库 / 事件记忆库的总述。

        三条线共用一行：它们描述的都是"眼下这段关系的状态"，分开存反而
        会出现三个 updated_at 各说各话。这次没提到的字段沿用上一版。
        """
        patch: dict[str, Any] = {}
        if rel_state:
            patch["rel_state"] = self._enc(rel_state)
        if emotion_snapshot:
            patch["emotion_snapshot"] = self._enc(emotion_snapshot)
        if events:
            patch["events_json"] = self._enc(list(events))
        if not patch:
            return
        assignments = ", ".join(f"{k} = ?" for k in patch)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO ep_summary (id) VALUES (?)", (_SINGLETON_ID,)
            )
            self._conn.execute(
                f"UPDATE ep_summary SET {assignments}, updated_at = ? WHERE id = ?",
                (*patch.values(), _stamp(), _SINGLETON_ID),
            )
            self._conn.commit()

    def ep(self) -> dict[str, Any]:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ep_summary WHERE id = ?", (_SINGLETON_ID,)
            ).fetchone()
        if row is None:
            return {"rel_state": "", "emotion_snapshot": "", "events": [], "updated_at": ""}
        return {
            "rel_state": self._dec(row["rel_state"]),
            "emotion_snapshot": self._dec(row["emotion_snapshot"]),
            "events": self._dec_json(row["events_json"]),
            "updated_at": row["updated_at"] or "",
        }

    # ---------- 好感度（B 养成，单例） ----------

    def save_intimacy(
        self,
        level: int,
        exp: int,
        unlocked: Sequence[str] = (),
        last_interact: str | None = None,
    ) -> None:
        """覆写式写入好感度。跟画像 / 关系总述一个格式：单例一行。

        `last_interact` 传空就记当前时刻——调用方是"刚发生了互动"才来写的，
        让它每次都自己带时间戳没意义。
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO intimacy (id) VALUES (?)", (_SINGLETON_ID,)
            )
            self._conn.execute(
                "UPDATE intimacy SET level = ?, exp = ?, unlocked = ?, last_interact = ? "
                "WHERE id = ?",
                (
                    self._enc(int(level)),
                    self._enc(int(exp)),
                    self._enc(list(unlocked)),
                    last_interact or _stamp(),
                    _SINGLETON_ID,
                ),
            )
            self._conn.commit()

    def intimacy(self) -> dict[str, Any]:
        """好感度现状。还没养过就返回 L0/0（不写库，读不该产生副作用）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM intimacy WHERE id = ?", (_SINGLETON_ID,)
            ).fetchone()
        if row is None:
            return {"level": 0, "exp": 0, "unlocked": [], "last_interact": ""}
        return {
            "level": _to_int(self._dec(row["level"])),
            "exp": _to_int(self._dec(row["exp"])),
            "unlocked": [str(item) for item in self._dec_json(row["unlocked"])],
            "last_interact": row["last_interact"] or "",
        }

    # ---------- 星念 ----------

    def add_star_card(
        self,
        title: str,
        kind: str,
        source_id: str = "",
        rarity: str = "common",
        created_at: str | None = None,
    ) -> int | None:
        """存一张星念。空标题不存——星念的价值全在它记的是哪件事。"""
        text = (title or "").strip()
        if not text:
            return None
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO star_cards (title, kind, source_id, created_at, rarity) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self._enc(text),
                    self._enc(kind),
                    self._enc(source_id),
                    created_at or _stamp(),
                    self._enc(rarity),
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def star_cards(self, limit: int | None = None) -> list[dict[str, Any]]:
        """最新的在前。张数本来就不多（一个里程碑一张），全解出来不心疼。"""
        sql = "SELECT * FROM star_cards ORDER BY id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_star_card(row, self) for row in rows]

    def star_card_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM star_cards").fetchone()[0])

    # ---------- 记忆簿 ----------

    def save_memory_book(
        self, date: str, summary: str, entries: Sequence[str] = ()
    ) -> None:
        """写一天的一页。同一天反复写是覆写，不是追加——那天还没过完，
        每提炼一轮就把这页重写一遍，最后留下的就是全的。"""
        day = (date or "").strip()
        if not day:
            return
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_book (date, summary, entries) VALUES (?, ?, ?) "
                "ON CONFLICT(date) DO UPDATE SET summary = excluded.summary, "
                "entries = excluded.entries",
                (day, self._enc(summary), self._enc(list(entries))),
            )
            self._conn.commit()

    def memory_book(self, date: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_book WHERE date = ?", ((date or "").strip(),)
            ).fetchone()
        return _row_to_memory_book(row, self) if row is not None else None

    def memory_books(self, limit: int | None = None) -> list[dict[str, Any]]:
        """最近的在前（按日期倒序，跟 id 顺序一致：一天只写一个 id）。"""
        sql = "SELECT * FROM memory_book ORDER BY date DESC, id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_memory_book(row, self) for row in rows]

    # ---------- 碎片 ----------

    def add_fragment(
        self, text: str, tags: Iterable[str] = (), src_time: datetime | str | None = None
    ) -> int | None:
        """存一条生活碎片。空文本不存——碎片的价值全在内容里。

        落库之后顺带补一条向量（S1）。**向量失败不算写失败**——正文已经进了加密库，
        索引只是召回的一条腿，塌了还有 SQL LIKE 兜着，不该把这条碎片一起丢掉。
        """
        if not text or not text.strip():
            return None
        when = src_time.isoformat(timespec="seconds") if isinstance(src_time, datetime) else (
            str(src_time) if src_time else _stamp()
        )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO fragment (text, src_time, tags) VALUES (?, ?, ?)",
                (self._enc(text), when, self._enc(list(tags))),
            )
            self._conn.commit()
            fragment_id = int(cursor.lastrowid)
        self._index_fragment(fragment_id, text)
        return fragment_id

    def _index_fragment(self, fragment_id: int, text: str) -> None:
        if self._index is None:
            return
        try:
            self._index.add(fragment_id, text)
        except Exception as exc:  # 索引塌了不能连累正文
            logger.error("情感记忆：碎片 %d 的向量没算成（%s），检索退回字面", fragment_id, exc)

    def fragment(self, fragment_id: int) -> dict[str, Any] | None:
        """按 id 取一条。向量召回回来的是 id，得拿它换回正文。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM fragment WHERE id = ?", (int(fragment_id),)
            ).fetchone()
        return _row_to_fragment(row, self) if row is not None else None

    def fragments(self, limit: int | None = None) -> list[dict[str, Any]]:
        """最新的在前。全部读出来解密——检索那边要的也是明文（见 recall.py）。"""
        sql = "SELECT * FROM fragment ORDER BY id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_fragment(row, self) for row in rows]

    def fragment_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM fragment").fetchone()[0])

    # ---------- 情感事件（v2-emotion-log） ----------

    def add_emotion_event(
        self,
        text: str,
        emotion: str,
        attribution: str,
        category: str,
        src_time: datetime | str | None = None,
    ) -> int | None:
        """记一条情感事件：原文 + 情绪 + 归因 + 分类标签。空原文不存。

        跟碎片一样，正文一律密文；src_time 留明文当查的坐标。
        这条线只记录、不出卡、不行动（见 life/mind/emotion_log.py 的分家说明）。
        """
        if not text or not text.strip():
            return None
        when = src_time.isoformat(timespec="seconds") if isinstance(src_time, datetime) else (
            str(src_time) if src_time else _stamp()
        )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO emotion_event (text, emotion, attribution, category, src_time) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    self._enc(text),
                    self._enc(emotion),
                    self._enc(attribution),
                    self._enc(category),
                    when,
                ),
            )
            self._conn.commit()
            return int(cursor.lastrowid)

    def emotion_events(self, limit: int | None = None) -> list[dict[str, Any]]:
        """最新的在前。全解出来——检索那边要的也是明文（跟 fragments 一个道理）。"""
        sql = "SELECT * FROM emotion_event ORDER BY id DESC"
        params: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (limit,)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_emotion_event(row, self) for row in rows]

    def emotion_event_count(self) -> int:
        with self._lock:
            return int(self._conn.execute("SELECT COUNT(*) FROM emotion_event").fetchone()[0])

    # ---------- 清空（v2-reset-state） ----------

    #: 「清关系与记忆」动的是这四张表。**`struct_profile` 与 `star_cards` 不在里面**：
    #: 画像是"你是谁"，星念是"一路走来的里程碑"，都不算"它记着的事"——
    #: 清了等于把宠物对这个人的基本认识也抹了，那是另一件事，不该混进这个开关。
    CLEARED_TABLES: tuple[str, ...] = ("intimacy", "fragment", "memory_book", "ep_summary")

    def memory_counts(self) -> dict[str, int]:
        """这四张表各有多少行（清空前给用户过目用的）。纯读。"""
        with self._lock:
            return {
                table: int(self._conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
                for table in self.CLEARED_TABLES
            }

    def clear_relationship_and_memory(self) -> dict[str, int]:
        """清掉关系与记忆这四张表，返回**清之前**的行数。

        一次一个事务：四张表要么一起空，要么一条不动——清到一半失败留下"好感度没了
        但记忆还在"的半截状态，比不清更糟。

        **向量索引不在这儿清**：那是另一个库、另一个对象（见 index.py 的 clear），
        由调用方接着清。这里只负责 mind.db 这一半。
        """
        before = self.memory_counts()
        with self._lock:
            try:
                for table in self.CLEARED_TABLES:
                    self._conn.execute(f"DELETE FROM {table}")
                self._conn.commit()
            except Exception:
                self._conn.rollback()
                raise
        logger.info("情感记忆库：关系与记忆已清空 %s", before)
        return before

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _row_to_fragment(row: sqlite3.Row, store: "MindStore") -> dict[str, Any]:
    """一行 fragment（密文）→ 一条碎片（明文）。tags 那列是 JSON 数组。"""
    return {
        "id": row["id"],
        "text": store._dec(row["text"]),
        "src_time": row["src_time"] or "",
        "tags": [str(t) for t in store._dec_json(row["tags"])],
    }


def _row_to_star_card(row: sqlite3.Row, store: "MindStore") -> dict[str, Any]:
    """一行 star_cards（密文）→ 一张星念（明文）。"""
    return {
        "id": row["id"],
        "title": store._dec(row["title"]),
        "kind": store._dec(row["kind"]),
        "source_id": store._dec(row["source_id"]),
        "created_at": row["created_at"] or "",
        "rarity": store._dec(row["rarity"]),
    }


def _row_to_memory_book(row: sqlite3.Row, store: "MindStore") -> dict[str, Any]:
    """一行 memory_book（密文）→ 一页日记（明文）。entries 那列是 JSON 数组。"""
    return {
        "id": row["id"],
        "date": row["date"] or "",
        "summary": store._dec(row["summary"]),
        "entries": [str(item) for item in store._dec_json(row["entries"])],
    }


def _row_to_emotion_event(row: sqlite3.Row, store: "MindStore") -> dict[str, Any]:
    """一行 emotion_event（密文）→ 一条情感事件（明文）。"""
    return {
        "id": row["id"],
        "text": store._dec(row["text"]),
        "emotion": store._dec(row["emotion"]),
        "attribution": store._dec(row["attribution"]),
        "category": store._dec(row["category"]),
        "src_time": row["src_time"] or "",
    }


def _to_int(text: Any) -> int:
    """密文解出来的数字。解不开 / 被手改成别的 → 0，别把整条记录一起废掉。"""
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return 0


def _stamp() -> str:
    return datetime.now().isoformat(timespec="seconds")
