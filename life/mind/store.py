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

**peer 维度（v2-social-card-base）**：`struct_profile` / `ep_summary` / `fragment` /
`memory_book` 四张表各多一列 `peer`——原先它们描述的都是"桌宠与主人"这一段，帮聊那条
线要看的是**另一个人**的档案，两者必须分格。这一列里存的是 `sha256(对端名字)` 的摘要
（见 `_peer_key`），不是名字本身：名字属于密文那一档，明文落库会把整库加密破一半。
空串 `''` 是"主人自己那一档"，历史数据与所有不传 peer 的老调用点都落在这儿，行为不变。

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
import hashlib
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

# ---------- peer 维度（v2-social-card-base） ----------
#
# 这四张表原先描述的都是"桌宠与主人"这一段关系：画像只有一行（`id=1`）、关系总述只有一行、
# 碎片与记忆簿没有"这是跟谁的事"这一维。帮聊那条线要看的是**另一个人**的档案（对方喜欢
# 什么、忌讳什么、聊到哪一步了），跟主人的那份必须分格——所以四张表各加一列 `peer`。
#
# **`peer` 列里存的是键，不是名字。** 名字（"小明""领导"）本身是密文那一档的私密信息，
# 明文落在 mind.db 里就把"整库加密"这条承诺破了一半。所以对外收的是名字、进库的是
# `sha256(名字)` 前 32 位十六进制（见 `_peer_key`）：既能当查的坐标用（跟 id / src_time
# 同类，是"拿来查的"，不是载荷），又不可逆、不泄露是谁。空串（`''`）是**主人自己那一档**
# ——历史数据、主聊天那条线、没传 peer 的老调用点全落在这儿，行为与加这一列之前一致。
_PEER_TABLES = ("struct_profile", "ep_summary", "fragment", "memory_book")

# 唯一索引：`struct_profile` / `ep_summary` 由"单例一行"变成"一个 peer 一行"，
# `memory_book` 由"一天一页"变成"一天一个 peer 一页"。
#
# **这两条索引不能在 SCHEMA 里建**：老库那四张表还没有 peer 列，索引先建会直接失败；
# 而且老库上已经有一条 `idx_memory_book_date`（只看日期），跟"按 peer 分"打架，得先拆。
# 所以顺序是：建表 → 补列 → 拆老索引 → 建新索引（见 `_migrate`）。
_INDEXES = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_struct_profile_peer ON struct_profile(peer);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ep_summary_peer ON ep_summary(peer);
CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_book_date_peer ON memory_book(date, peer);
"""

SCHEMA = """
CREATE TABLE IF NOT EXISTS struct_profile (
    id          INTEGER PRIMARY KEY,
    peer        TEXT NOT NULL DEFAULT '',
    name        TEXT,
    gender      TEXT,
    occupation  TEXT,
    family      TEXT,
    intro       TEXT,
    updated_at  TEXT
);
CREATE TABLE IF NOT EXISTS ep_summary (
    id               INTEGER PRIMARY KEY,
    peer             TEXT NOT NULL DEFAULT '',
    rel_state        TEXT,
    emotion_snapshot TEXT,
    events_json      TEXT,
    updated_at       TEXT
);
CREATE TABLE IF NOT EXISTS fragment (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    peer     TEXT NOT NULL DEFAULT '',
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
    peer    TEXT NOT NULL DEFAULT '',
    date    TEXT,
    summary TEXT,
    entries TEXT
);
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


def _peer_key(peer: object) -> str:
    """对端（peer）在库里的键：空串原样留着，其余取 `sha256(名字)` 前 32 位。

    为什么不直接存名字：这一列是**查的坐标**（跟 id / src_time / date 同类），不是载荷；
    可它不是"哪一天""第几条"那种公开坐标，是一个**人名或昵称**——明文落进 mind.db，
    等于在整库密文里开了一格白名单，翻到文件的人一眼就知道"它最近在跟谁聊天"。取摘要
    之后：同一个名字永远算出同一个键（能当键用、能做唯一索引、能按它对表），但拿不到名字。
    32 位十六进制 = 128 位，几个对端之间撞不出重。
    """
    text = str(peer or "").strip()
    if not text:
        return ""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:32]


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
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """把老库补成新形状（v2-social-card-base）：补 peer 列、换记忆簿那条唯一索引。

        `CREATE TABLE IF NOT EXISTS` 对**已经存在**的表一个字都不会改，所以老库那四张表
        不会自己长出 peer 列，得自己来。补列的声明带 `NOT NULL DEFAULT ''`——SQLite 的
        `ALTER TABLE ADD COLUMN` 允许这么干，老行会一律拿空串，正好落进"主人自己那一档"，
        跟加这一列之前的行为完全一致。

        记忆簿那条老索引（`idx_memory_book_date`，只看日期）跟"一天一个 peer 一页"打架，
        先拆掉再建新的。整段是幂等的：跑第二遍时列已经在、老索引已经不在了，什么都没发生。
        """
        for table in _PEER_TABLES:
            self._add_column(table, "peer", "TEXT NOT NULL DEFAULT ''")
        self._conn.execute("DROP INDEX IF EXISTS idx_memory_book_date")
        try:
            self._conn.executescript(_INDEXES)
        except sqlite3.Error as exc:
            # 唯一索引是**护栏**不是命门：真有老库带着重复行（手工改过 / 半截迁移），
            # 建不上也只记一笔——把库整个打不开才是真出事。
            logger.warning("情感记忆库：peer 唯一索引没建上（%s），按没有护栏继续", exc)

    def _add_column(self, table: str, column: str, declaration: str) -> None:
        """表里没这一列就补上（幂等）。表不存在时什么都不做。"""
        try:
            columns = {row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")}
        except sqlite3.Error as exc:
            logger.warning("情感记忆库：%s 的表结构读不出来（%s），这一列不补", table, exc)
            return
        if not columns or column in columns:
            return
        self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")
        logger.info("情感记忆库：%s 补上 %s 列（v2-social-card-base）", table, column)

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

    def save_profile(self, peer: str = "", **fields: Any) -> None:
        """覆写式写入已知项。只更新这次给到的字段，其余留着（聊到一点补一点）。

        ``peer``（v2-social-card-base）：这份画像是**谁的**。默认空串 = 主人自己那一档，
        行为与加这一列之前一模一样；传对端名字（如 `"小明"`）就写到他那格去。
        不传 `peer` 的老调用点（`distill.py`）一个字都不用改。
        """
        who = _peer_key(peer)
        values = {k: v for k, v in fields.items() if k in _PROFILE_FIELDS}
        if not values:
            return
        encrypted = {k: self._enc(v) for k, v in values.items()}
        assignments = ", ".join(f"{k} = ?" for k in encrypted)
        with self._lock:
            self._conn.execute(
                "INSERT OR IGNORE INTO struct_profile (peer) VALUES (?)", (who,)
            )
            self._conn.execute(
                f"UPDATE struct_profile SET {assignments}, updated_at = ? WHERE peer = ?",
                (*encrypted.values(), _stamp(), who),
            )
            self._conn.commit()

    def profile(self, peer: str = "") -> dict[str, str]:
        """这一格的结构画像。没有这一行就回 `{}`（跟加 peer 之前同一个口径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM struct_profile WHERE peer = ?", (_peer_key(peer),)
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
        peer: str = "",
    ) -> None:
        """写关系记忆库 / 情绪记忆库 / 事件记忆库的总述。

        三条线共用一行：它们描述的都是"眼下这段关系的状态"，分开存反而
        会出现三个 updated_at 各说各话。这次没提到的字段沿用上一版。

        ``peer``（v2-social-card-base）：这是**跟谁**的关系总述。默认空串 = 主人自己
        那一档（老行为一字未变）；传对端名字就写到他那格去，与主人那格互不影响。
        """
        who = _peer_key(peer)
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
                "INSERT OR IGNORE INTO ep_summary (peer) VALUES (?)", (who,)
            )
            self._conn.execute(
                f"UPDATE ep_summary SET {assignments}, updated_at = ? WHERE peer = ?",
                (*patch.values(), _stamp(), who),
            )
            self._conn.commit()

    def ep(self, peer: str = "") -> dict[str, Any]:
        """这一格的关系/情绪总述。还没有这一行就给一份空壳（跟加 peer 之前同一个口径）。"""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM ep_summary WHERE peer = ?", (_peer_key(peer),)
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
        self, date: str, summary: str, entries: Sequence[str] = (), peer: str = ""
    ) -> None:
        """写一天的一页。同一天反复写是覆写，不是追加——那天还没过完，
        每提炼一轮就把这页重写一遍，最后留下的就是全的。

        ``peer``（v2-social-card-base）：这一页是**跟谁**的。冲突判据从 `(date)` 变成
        `(date, peer)`——同一天里主人一页、每个对端各一页，互不覆盖。默认空串就是原来
        那一页，老调用点行为一字未变。
        """
        day = (date or "").strip()
        if not day:
            return
        who = _peer_key(peer)
        with self._lock:
            self._conn.execute(
                "INSERT INTO memory_book (date, peer, summary, entries) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(date, peer) DO UPDATE SET summary = excluded.summary, "
                "entries = excluded.entries",
                (day, who, self._enc(summary), self._enc(list(entries))),
            )
            self._conn.commit()

    def memory_book(self, date: str, peer: str = "") -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM memory_book WHERE date = ? AND peer = ?",
                ((date or "").strip(), _peer_key(peer)),
            ).fetchone()
        return _row_to_memory_book(row, self) if row is not None else None

    def memory_books(
        self, limit: int | None = None, peer: object = None
    ) -> list[dict[str, Any]]:
        """最近的在前（按日期倒序，跟 id 顺序一致：一天一个 peer 一个 id）。

        ``peer`` 三态：`None`（默认）= **所有**（老调用点要的就是"这段时间记了什么"，
        跟加这一列之前完全一样）；空串 = 只看主人自己那一档；给了名字 = 只看那一格。
        """
        sql = "SELECT * FROM memory_book"
        params: tuple = ()
        if peer is not None:
            sql += " WHERE peer = ?"
            params = (_peer_key(peer),)
        sql += " ORDER BY date DESC, id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
        with self._lock:
            rows = self._conn.execute(sql, params).fetchall()
        return [_row_to_memory_book(row, self) for row in rows]

    # ---------- 碎片 ----------

    def add_fragment(
        self,
        text: str,
        tags: Iterable[str] = (),
        src_time: datetime | str | None = None,
        peer: str = "",
    ) -> int | None:
        """存一条生活碎片。空文本不存——碎片的价值全在内容里。

        落库之后顺带补一条向量（S1）。**向量失败不算写失败**——正文已经进了加密库，
        索引只是召回的一条腿，塌了还有 SQL LIKE 兜着，不该把这条碎片一起丢掉。

        ``peer``（v2-social-card-base）：这条碎片是**跟谁**的。默认空串 = 主人自己
        那一档，老调用点行为一字未变；传对端名字就落到他那格，检索时按同一格取。
        """
        if not text or not text.strip():
            return None
        who = _peer_key(peer)
        when = src_time.isoformat(timespec="seconds") if isinstance(src_time, datetime) else (
            str(src_time) if src_time else _stamp()
        )
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO fragment (text, peer, src_time, tags) VALUES (?, ?, ?, ?)",
                (self._enc(text), who, when, self._enc(list(tags))),
            )
            self._conn.commit()
            fragment_id = int(cursor.lastrowid)
        self._index_fragment(fragment_id, text, who)
        return fragment_id

    def _index_fragment(self, fragment_id: int, text: str, peer: str = "") -> None:
        if self._index is None:
            return
        try:
            self._index.add(fragment_id, text, peer)
        except TypeError:
            # 老索引对象（只收两个参数）——向量那条腿塌了不连累正文，照写不误
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

    def fragments(
        self, limit: int | None = None, peer: object = None
    ) -> list[dict[str, Any]]:
        """最新的在前。全部读出来解密——检索那边要的也是明文（见 recall.py）。

        ``peer`` 三态同 `memory_books`：`None`（默认）= 所有（老调用点一字不改）；
        空串 = 只看主人自己那一档；给了名字 = 只看那一格（帮聊那条线要的）。
        """
        sql = "SELECT * FROM fragment"
        params: tuple = ()
        if peer is not None:
            sql += " WHERE peer = ?"
            params = (_peer_key(peer),)
        sql += " ORDER BY id DESC"
        if limit is not None:
            sql += " LIMIT ?"
            params = (*params, limit)
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
    """一行 fragment（密文）→ 一条碎片（明文）。tags 那列是 JSON 数组。

    ``peer`` 带出去的是**键**不是名字（见 `_peer_key`）：调用方按它分组 / 过滤够用，
    想拿到名字得自己手上本来就有（帮聊那条线就有）。
    """
    return {
        "id": row["id"],
        "peer": row["peer"] or "",
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
    """一行 memory_book（密文）→ 一页日记（明文）。entries 那列是 JSON 数组。

    ``peer`` 同 `_row_to_fragment`：带出去的是键（空串 = 主人自己那一档）。
    """
    return {
        "id": row["id"],
        "peer": row["peer"] or "",
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
