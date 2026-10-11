"""
对象卡底座（v2-social-card-base）的自动测试。

这一批只动**数据层 + 意图层**，三段各验一段：

① **对象档案四格**：`Card` 多了 `likes` / `dislikes` / `strategy` / `relation_stage`
   四列（可空，老库加列即兼容），手动开卡框在「情感」大类下多出那四行；
② **三记忆库的 peer 维度**：`struct_profile` / `ep_summary` / `fragment` / `memory_book`
   各加一列 `peer`，按对端分格、互不串；老库打开时自己补列、老行归到「主人自己那一档」；
③ **意图底座按说话者参数化**：解析链 / 意图链 / 规则路由 / 意图判定都收一个 `speaker`，
   提示词里的「用户」提成 `{speaker}` 那一格；**默认 `"user"`，拼出来的提示词与改动前
   逐字相同**，帮聊那条线传 `"peer"`。

口径如实记（测试里够不着的地方不装作验过）：

- **`peer` 列里存的是摘要不是名字**（见 `store._peer_key`）——测试是从**库文件**里读原始
  列值来验这一条，不是读对象属性；
- **提示词那一截**验的是「拼出来的字符串」（模型被桩掉了，模型到底照不照做只能真机看）；
- **老库迁移**造的是真·老库（手写老 schema 的 sqlite 文件）再打开，不是拿新库凑。

红线：只碰沙箱（`harness.Sandbox` 把两个库全指到 `tempfile.mkdtemp`）；零新依赖；
既有社交 / 意图模块一条断言都不改（「user 那条路不变」由本文件自己钉）。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary

from brain.chains.chatter_parse import ChatterParseChain
from brain.chains.intent import IntentChain
from brain.prompts.templates import (
    CHATTER_PARSE_PROMPT,
    SERVICE_INTENT_PROMPT,
    SPEAKER_PEER,
    SPEAKER_USER,
    speaker_label,
)
from data import models as models_mod
from life.cards.classify import CardClassifier
from life.cards.templates import EMOTION_CATEGORY
from life.dialog import router
from life.intent import IntentJudge
from life.mind.store import MindStore, _peer_key
from life.screen import social as smod
from life.screen.social import (
    CARD_CATEGORY,
    GUIDE_CARD_GOAL,
    GUIDE_CARD_ICON,
    GUIDE_CARD_TITLE,
)
from ui.card_dialogs import PROFILE_FIELDS, CardFormDialog

# 那四格的名字就是 `Card` 上的列名：界面、DAO、模型三处必须对得上
PROFILE_KEYS = tuple(key for key, _label, _hint in PROFILE_FIELDS)

# 冻结串：加 speaker 之前那两行原话。`speaker="user"` 时必须**逐字**还是它们。
FROZEN_PARSE_LINE = "用户刚刚对你说："
FROZEN_PARSE_INTENT = '"intent": "用户这句话的意图'
FROZEN_SERVICE_LINE = "用户说了一句话"

# 对端名字，用来验「peer 落库的是摘要不是名字」
PEER = "小明"


def _card_columns() -> dict:
    return {column.name: column for column in models_mod.Card.__table__.columns}


def _raw_card_row(db_path, card_id: int) -> tuple:
    """从**库文件**里读那四格（不走 ORM，验的是真落盘的东西）。"""
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            f"SELECT {', '.join(PROFILE_KEYS)} FROM card WHERE id = ?", (card_id,)
        ).fetchone()
    finally:
        conn.close()
    return tuple(row) if row is not None else ()


def _raw_values(db_path, table: str, column: str) -> list:
    conn = sqlite3.connect(str(db_path))
    try:
        rows = [row[0] for row in conn.execute(f"SELECT {column} FROM {table}")]
    finally:
        conn.close()
    return rows


def _old_mind_db(path: Path, enc) -> None:
    """手写一个**加 peer 之前**形状的 mind.db（老 schema + 老唯一索引 + 几行老数据）。

    `enc` 是同一个库自己那把钥匙的加密函数（`MindStore._enc`）——老数据**本来就是密文**，
    拿明文塞进去的话打开时解不开、会被当成空串，验的就不是"迁移后老数据还在"了。
    """
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(
            """
            CREATE TABLE struct_profile (id INTEGER PRIMARY KEY, name TEXT, gender TEXT,
                occupation TEXT, family TEXT, intro TEXT, updated_at TEXT);
            CREATE TABLE ep_summary (id INTEGER PRIMARY KEY, rel_state TEXT,
                emotion_snapshot TEXT, events_json TEXT, updated_at TEXT);
            CREATE TABLE fragment (id INTEGER PRIMARY KEY AUTOINCREMENT, text TEXT,
                src_time TEXT, tags TEXT);
            CREATE TABLE memory_book (id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT,
                summary TEXT, entries TEXT);
            CREATE UNIQUE INDEX idx_memory_book_date ON memory_book(date);
            """
        )
        conn.execute(
            "INSERT INTO struct_profile (id, name, updated_at) VALUES (1, ?, 'x')",
            (enc("老库里的主人"),),
        )
        conn.execute(
            "INSERT INTO fragment (text, src_time, tags) VALUES (?, '2026-01-01', ?)",
            (enc("老库里的碎片"), enc(["旧标签"])),
        )
        conn.execute(
            "INSERT INTO memory_book (date, summary, entries) VALUES ('2026-01-01', ?, ?)",
            (enc("老库里的那一页"), enc(["老库里的碎片"])),
        )
        conn.commit()
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        service = app.card_service
        store = app.mind_store
        db_path = app.config.db_path

        # ==================================================================
        section("① 卡表多了对象档案四格（可空 = 老库加列即兼容）")
        # ==================================================================
        columns = _card_columns()
        check(
            "卡表有 喜好 / 禁忌 / 攻略 / 到哪一步 四列",
            all(key in columns for key in PROFILE_KEYS),
            f"缺 {[k for k in PROFILE_KEYS if k not in columns]}",
        )
        check(
            "四列都可以为空（老表补列只补可空列，非空的会当场跳过）",
            all(columns[key].nullable for key in PROFILE_KEYS),
            str({k: columns[k].nullable for k in PROFILE_KEYS}),
        )
        check(
            "界面那四格的键就是卡表那四个列名（界面 / DAO / 模型同一份口径）",
            PROFILE_KEYS == ("likes", "dislikes", "strategy", "relation_stage"),
            str(PROFILE_KEYS),
        )

        # ==================================================================
        section("① 建卡：不传对象档案 → 四格是 NULL（跟加它之前一模一样）")
        # ==================================================================
        plain = service.create_manual_card("作息", "早睡的卡", "🌙", "")
        check("卡建出来了", plain is not None, str(plain))
        drain_threads()
        check(
            "四格全是 NULL（老调用点一个字节都不用改）",
            _raw_card_row(db_path, plain.id) == (None,) * len(PROFILE_KEYS),
            str(_raw_card_row(db_path, plain.id)),
        )

        # ==================================================================
        section("① 建卡：传了对象档案 → 四格真落盘、空值不写")
        # ==================================================================
        guide = service.create_manual_card(
            CARD_CATEGORY,
            GUIDE_CARD_TITLE,
            GUIDE_CARD_ICON,
            GUIDE_CARD_GOAL,
            profile={
                "likes": "爱吃辣的，爱看悬疑片",
                "dislikes": "别提加班",
                "strategy": "先聊剧，再约吃饭",
                "relation_stage": "聊得来，还没约过",
            },
        )
        check("对象攻略卡建出来了", guide is not None, str(guide))
        drain_threads()
        row = _raw_card_row(db_path, guide.id)
        check(
            "四格从库文件里读回来就是填的那份",
            row
            == ("爱吃辣的，爱看悬疑片", "别提加班", "先聊剧，再约吃饭", "聊得来，还没约过"),
            str(row),
        )
        partial = service.create_manual_card(
            "穿搭",
            "半填的卡",
            "👕",
            "",
            profile={"likes": "  ", "dislikes": "怕吵", "nonsense": "不该进库"},
        )
        drain_threads()
        check(
            "空值不写、认不出的键丢掉",
            _raw_card_row(db_path, partial.id) == (None, "怕吵", None, None),
            str(_raw_card_row(db_path, partial.id)),
        )

        # ==================================================================
        section("① 后补档案（set_profile）：只写这次给到的")
        # ==================================================================
        service.set_profile(guide.id, strategy="改成先一起看展")
        row = _raw_card_row(db_path, guide.id)
        check("只改了攻略那一格", row[2] == "改成先一起看展", str(row))
        check(
            "这次没给的三格原样留着",
            (row[0], row[1], row[3]) == ("爱吃辣的，爱看悬疑片", "别提加班", "聊得来，还没约过"),
            str(row),
        )
        service.set_profile(guide.id, likes=None, dislikes="")
        check(
            "空值 / None 不覆盖已有内容",
            _raw_card_row(db_path, guide.id)[:2] == ("爱吃辣的，爱看悬疑片", "别提加班"),
            str(_raw_card_row(db_path, guide.id)),
        )

        # ==================================================================
        section("① 同类同名撞上既有卡：档案照样写进那一张")
        # ==================================================================
        again = service.create_manual_card(
            CARD_CATEGORY,
            GUIDE_CARD_TITLE,
            GUIDE_CARD_ICON,
            GUIDE_CARD_GOAL,
            profile={"dislikes": "别在饭点聊工作"},
        )
        check(
            "返回的还是原来那一张（v2-social-card-dedup 的口径没动）",
            again is not None and again.id == guide.id,
            f"{again and again.id} vs {guide.id}",
        )
        check(
            "这次填的禁忌写进了既有卡（这次填的东西不该因为「卡本来就有」丢掉）",
            _raw_card_row(db_path, guide.id)[1] == "别在饭点聊工作",
            str(_raw_card_row(db_path, guide.id)),
        )

        # ==================================================================
        section("① 老 pet.db 打开即补列（不用手工改库）")
        # ==================================================================
        from data.db import Database

        legacy_pet = box.dir / "legacy_pet.db"
        seed = sqlite3.connect(str(legacy_pet))
        try:
            seed.executescript("CREATE TABLE card (id INTEGER PRIMARY KEY, title TEXT);")
            seed.execute("INSERT INTO card (id, title) VALUES (1, '老卡')")
            seed.commit()
        finally:
            seed.close()
        Database(legacy_pet).init()
        cols = {
            row[1]
            for row in sqlite3.connect(str(legacy_pet)).execute("PRAGMA table_info(card)")
        }
        check(
            "老 pet.db 里那四列被自动补上了（老表补列只管可空列）",
            set(PROFILE_KEYS) <= cols,
            str(sorted(cols)),
        )
        kept = sqlite3.connect(str(legacy_pet)).execute("SELECT id, title FROM card").fetchall()
        check("老卡那行原样还在（补列不动数据）", kept == [(1, "老卡")], str(kept))

        # ==================================================================
        section("① 手动开卡框：那四行只对「情感」露面")
        # ==================================================================
        dialog = CardFormDialog(None)
        try:
            check(
                "建卡框里确实有四行（喜好 / 禁忌 / 攻略 / 到哪一步）",
                tuple(dialog._profile_edits) == PROFILE_KEYS,
                str(tuple(dialog._profile_edits)),
            )
            check(
                f"默认那个大类（{dialog._category.currentText()}，不是情感）→ 四行收着",
                dialog._category.currentText() != EMOTION_CATEGORY
                and not any(edit.isVisibleTo(dialog) for _tag, edit in dialog._profile_rows),
                "、".join(
                    f"{k}={e.isVisibleTo(dialog)}" for k, e in dialog._profile_edits.items()
                ),
            )
            dialog._category.setCurrentText(EMOTION_CATEGORY)
            check(
                "切到「情感」→ 四行都露出来",
                all(edit.isVisibleTo(dialog) for _tag, edit in dialog._profile_rows),
                "、".join(
                    f"{k}={e.isVisibleTo(dialog)}" for k, e in dialog._profile_edits.items()
                ),
            )
            dialog._profile_edits["likes"].setText("爱吃辣的")
            dialog._profile_edits["strategy"].setText("   ")
            dialog._category.setCurrentText("出行")
            check(
                "切走 → 又收起来，但填过的字还在（切一下不该把字抹了）",
                not any(edit.isVisibleTo(dialog) for _tag, edit in dialog._profile_rows)
                and dialog._profile_edits["likes"].text() == "爱吃辣的",
                dialog._profile_edits["likes"].text(),
            )
            check(
                "profile() 只回填了的那几格（空白的不算）",
                dialog.profile() == {"likes": "爱吃辣的"},
                str(dialog.profile()),
            )
            check(
                "values() 仍旧是四个值（老调用点不用改）",
                dialog.values()
                == ("出行", dialog._title.text().strip(), dialog._icon.currentText(), ""),
                str(dialog.values()),
            )
        finally:
            dialog.deleteLater()

        # ==================================================================
        section("② 三记忆库：按 peer 分格，互不串")
        # ==================================================================
        store.save_profile(name="主人", occupation="工程师")
        store.save_ep(rel_state="跟主人还生", events=["主人一号事件"])
        store.add_fragment("主人爱吃楼下包子", ["早饭"])
        store.save_memory_book("2026-10-01", "主人那一页", ["主人爱吃楼下包子"])

        store.save_profile(peer=PEER, name=PEER, intro="爱爬山")
        store.save_ep(rel_state=f"跟{PEER}聊得来", events=[f"{PEER}一号事件"], peer=PEER)
        store.add_fragment(f"{PEER}喜欢爬山", ["运动"], peer=PEER)
        store.save_memory_book("2026-10-01", f"{PEER}那一页", [f"{PEER}喜欢爬山"], peer=PEER)

        check(
            "画像：两格各是各的",
            store.profile().get("name") == "主人" and store.profile(peer=PEER).get("name") == PEER,
            f"{store.profile()} / {store.profile(peer=PEER)}",
        )
        check(
            "关系总述：两格各是各的",
            store.ep().get("rel_state") == "跟主人还生"
            and store.ep(peer=PEER).get("rel_state") == f"跟{PEER}聊得来",
            f"{store.ep()} / {store.ep(peer=PEER)}",
        )
        check(
            "碎片：两格各是各的",
            [f["text"] for f in store.fragments(peer="")] == ["主人爱吃楼下包子"]
            and [f["text"] for f in store.fragments(peer=PEER)] == [f"{PEER}喜欢爬山"],
            f"{[f['text'] for f in store.fragments(peer='')]} / "
            f"{[f['text'] for f in store.fragments(peer=PEER)]}",
        )
        check(
            "记忆簿：同一天两个 peer 各一页（不是互相覆盖）",
            store.memory_book("2026-10-01", peer="")["summary"] == "主人那一页"
            and store.memory_book("2026-10-01", peer=PEER)["summary"] == f"{PEER}那一页",
            f"{store.memory_book('2026-10-01', peer='')} / "
            f"{store.memory_book('2026-10-01', peer=PEER)}",
        )
        store.save_memory_book("2026-10-01", f"{PEER}那一页改过", peer=PEER)
        check(
            "同一天同一个 peer 反复写还是那一页（覆写不是追加），且没顺手改掉主人那页",
            len(store.memory_books(limit=50, peer=PEER)) == 1
            and store.memory_book("2026-10-01", peer="")["summary"] == "主人那一页"
            and store.memory_book("2026-10-01", peer=PEER)["summary"] == f"{PEER}那一页改过",
            f"{len(store.memory_books(limit=50, peer=PEER))} / "
            f"{store.memory_book('2026-10-01', peer='')['summary']} / "
            f"{store.memory_book('2026-10-01', peer=PEER)['summary']}",
        )
        check(
            "不传 peer 的老口径 = 全部（历史 + 主人 + 各对端）",
            store.fragment_count() == 2 and len(store.memory_books(limit=50)) == 2,
            f"碎片 {store.fragment_count()} / 记忆簿 {len(store.memory_books(limit=50))}",
        )
        check(
            "传空串 = 只看主人自己那一档",
            len(store.fragments(peer="")) == 1 and len(store.memory_books(limit=50, peer="")) == 1,
            f"{len(store.fragments(peer=''))} / {len(store.memory_books(limit=50, peer=''))}",
        )

        # ==================================================================
        section("② peer 落库的是摘要、不是名字（库文件里翻不到人名）")
        # ==================================================================
        raw_peers = _raw_values(store.db_path, "fragment", "peer")
        probe = sqlite3.connect(str(store.db_path))
        try:
            peer_columns = {
                table: any(row[1] == "peer" for row in probe.execute(f"PRAGMA table_info({table})"))
                for table in ("struct_profile", "ep_summary", "fragment", "memory_book")
            }
        finally:
            probe.close()
        check(
            "四个表物理上真的都有 peer 列（不是只在 SQL 语句里写着）",
            all(peer_columns.values()),
            str(peer_columns),
        )
        check(
            "fragment.peer 里没有一处是明文名字",
            PEER not in raw_peers and "" in raw_peers,
            str(raw_peers),
        )
        check(
            "同一格里的 peer 值 = sha256(名字) 前 32 位（能当键用、又反推不出人名）",
            _peer_key(PEER) in raw_peers and len(_peer_key(PEER)) == 32,
            f"{_peer_key(PEER)} / {raw_peers}",
        )
        check(
            "空串是主人自己那一档（老数据全落这儿）",
            _peer_key("") == "" and _peer_key(None) == "" and _peer_key("  ") == "",
            repr(_peer_key("")),
        )
        check(
            "同一个名字每次都算出同一个键（不然分格会裂）",
            _peer_key(PEER) == _peer_key(f" {PEER} ") and _peer_key(PEER) != _peer_key("小红"),
            f"{_peer_key(PEER)} / {_peer_key(f' {PEER} ')} / {_peer_key('小红')}",
        )

        # ==================================================================
        section("② 老 mind.db 打开即迁移：补列、换索引、老数据归位")
        # ==================================================================
        legacy_path = box.dir / "legacy_mind.db"
        proto = MindStore(box.dir / "key-proto.db")     # 只借它那把钥匙的加密函数
        try:
            _old_mind_db(legacy_path, proto._enc)
        finally:
            proto.close()
        legacy = MindStore(legacy_path)
        try:
            check(
                "老碎片还在（密文解得开），且归到了主人自己那一档",
                [f["peer"] for f in legacy.fragments()] == [""]
                and [f["text"] for f in legacy.fragments()] == ["老库里的碎片"]
                and [f["tags"] for f in legacy.fragments()] == [["旧标签"]],
                str(legacy.fragments()),
            )
            check(
                "老记忆簿那页也还在、也在主人那一档",
                legacy.memory_book("2026-01-01") is not None
                and legacy.memory_book("2026-01-01")["peer"] == ""
                and legacy.memory_book("2026-01-01")["summary"] == "老库里的那一页",
                str(legacy.memory_book("2026-01-01")),
            )
            check(
                "老画像那行也解得开、也在主人那一档（迁移没动密文）",
                legacy.profile().get("name") == "老库里的主人"
                and legacy.profile(peer="小红") == {},
                str(legacy.profile()),
            )
            indexes = {
                row[0]
                for row in sqlite3.connect(str(legacy_path)).execute(
                    "SELECT name FROM sqlite_master WHERE type='index'"
                )
            }
            check(
                "只看日期的那条老唯一索引拆掉了（不然同一天分不了两个 peer）",
                "idx_memory_book_date" not in indexes,
                str(sorted(indexes)),
            )
            check(
                "新的 (date, peer) 唯一索引建上了",
                "idx_memory_book_date_peer" in indexes,
                str(sorted(indexes)),
            )
            legacy.save_memory_book("2026-01-01", "老库里的新一页", peer="小红")
            legacy.add_fragment("老库里的新碎片", peer="小红")
            check(
                "迁移之后照样能按 peer 分格写",
                legacy.memory_book("2026-01-01", peer="小红")["summary"] == "老库里的新一页"
                and len(legacy.fragments(peer="小红")) == 1
                and legacy.memory_book("2026-01-01")["summary"] == "老库里的那一页",
                f"{legacy.memory_book('2026-01-01', peer='小红')} / "
                f"{len(legacy.fragments(peer='小红'))}",
            )
        finally:
            legacy.close()

        # ==================================================================
        section("③ 说话者：代号 → 称呼")
        # ==================================================================
        check(
            "user → 用户、peer → 对方",
            speaker_label("user") == "用户" and speaker_label("peer") == "对方",
            f"{speaker_label('user')} / {speaker_label('peer')}",
        )
        check(
            "认不出的（空 / 生造词）一律按「用户」走——老调用点零行为变化",
            speaker_label("") == "用户"
            and speaker_label(None) == "用户"
            and speaker_label("self") == "用户"
            and speaker_label("PEER") == "对方",
            f"{speaker_label('')} / {speaker_label('self')} / {speaker_label('PEER')}",
        )

        # ==================================================================
        section("③ 「user」那条路逐字不变（冻结串 + 只换主语）")
        # ==================================================================
        now_line = CHATTER_PARSE_PROMPT.format(
            now="2026-10-12 09:00",
            weekday="周一",
            text="今天好累",
            speaker=speaker_label("user"),
        )
        peer_line = CHATTER_PARSE_PROMPT.format(
            now="2026-10-12 09:00",
            weekday="周一",
            text="今天好累",
            speaker=speaker_label("peer"),
        )
        check(
            "解析提示词里那两行还是老原话",
            FROZEN_PARSE_LINE in now_line and FROZEN_PARSE_INTENT in now_line,
            FROZEN_PARSE_LINE,
        )
        check(
            "换成 peer 之后，整段与 user 那份只差「用户」→「对方」（别的逐字相同）",
            peer_line.replace("对方", "用户") == now_line,
            "两段对不上",
        )
        check(
            "解析提示词里没有落下没填的占位",
            "{speaker}" not in now_line and "{speaker}" not in peer_line,
            "",
        )

        now_svc = SERVICE_INTENT_PROMPT.format(
            categories="餐饮、出行", text="借我点钱", speaker=speaker_label("user")
        )
        peer_svc = SERVICE_INTENT_PROMPT.format(
            categories="餐饮、出行", text="借我点钱", speaker=speaker_label("peer")
        )
        check("意图提示词里那行还是老原话", FROZEN_SERVICE_LINE in now_svc, FROZEN_SERVICE_LINE)
        check(
            "换成 peer 之后也只差主语两个字",
            peer_svc.replace("对方", "用户") == now_svc,
            "两段对不上",
        )

        # ==================================================================
        section("③ 两条链：默认走 user，传 peer 就按对方判")
        # ==================================================================
        parse_chain = ChatterParseChain(app.config)
        stub.parse_result = {"scene": "闲聊", "emotion": "平静", "intent": "闲聊"}
        stub.prompts.clear()
        got = parse_chain.analyze("今天好累")
        check("不传 speaker → 这份解析的说话者是 user", got.speaker == SPEAKER_USER, got.speaker)
        check(
            "不传 speaker → 发出去的提示词里还是「用户」",
            FROZEN_PARSE_LINE in stub.prompts[-1],
            stub.prompts[-1][:40],
        )

        stub.prompts.clear()
        got_peer = parse_chain.analyze("在吗？周末有空一起吃饭吗", speaker=SPEAKER_PEER)
        check(
            "传 peer → 这份解析的说话者是 peer",
            got_peer.speaker == SPEAKER_PEER,
            got_peer.speaker,
        )
        check(
            "传 peer → 提示词里主语换成「对方」",
            "对方刚刚对你说" in stub.prompts[-1]
            and '"intent": "对方这句话的意图' in stub.prompts[-1],
            stub.prompts[-1][:60],
        )

        intent_chain = IntentChain(app.config)
        stub.intent_result = {"kind": "life", "theme": "餐饮", "confidence": 0.9, "urgent": True}
        stub.prompts.clear()
        kind, theme, confidence, urgent = intent_chain.classify(
            "周末一起吃饭吗？", speaker=SPEAKER_PEER
        )
        check(
            "意图链传 peer → 提示词判的是「对方」",
            "对方说了一句话" in stub.prompts[-1],
            stub.prompts[-1][:40],
        )
        check(
            "返回值照旧解析（换主语不改结构化抽取）",
            (kind, theme, confidence, urgent) == ("life", "餐饮", 0.9, True),
            f"{kind}/{theme}/{confidence}/{urgent}",
        )
        stub.prompts.clear()
        intent_chain.classify("周末一起吃饭吗？")
        check(
            "不传 speaker → 还是「用户」",
            FROZEN_SERVICE_LINE in stub.prompts[-1],
            stub.prompts[-1][:40],
        )

        # ==================================================================
        section("③ 纯规则那两层：收 speaker，但判据一条都不按它分")
        # ==================================================================
        for line in ("帮我回个消息", "我现在好累", "陪我说会儿话", "嗯嗯", "算了"):
            check(
                f"router.classify 对「{line}」传 user / peer 结果相同",
                router.classify(line) == router.classify(line, speaker=SPEAKER_PEER),
                f"{router.classify(line)} vs {router.classify(line, speaker=SPEAKER_PEER)}",
            )
        judge = IntentJudge(app.config, CardClassifier(app.config))
        for line in ("BOM 方案今天要做完", "见客户快要迟到了", "中午吃啥好呢"):
            check(
                f"IntentJudge.judge 对「{line}」传 user / peer 结果相同",
                judge.judge(line, timed=True)
                == judge.judge(line, timed=True, speaker=SPEAKER_PEER),
                f"{judge.judge(line, timed=True)} vs "
                f"{judge.judge(line, timed=True, speaker=SPEAKER_PEER)}",
            )

        # ==================================================================
        section("③ 帮聊那条线真传了 peer（判的是对方最新那一句）")
        # ==================================================================
        social = app.social_reply
        social._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "好呀，回头说。", "why": "留余地"}]}
        # 整屏里既有前面那句「指令」，也有最后那句「倾诉」——判的必须是**最后那句**
        chat_text = "小明：帮我关掉音乐\n我：好的\n小明：今天好累啊"
        calls: list[tuple[str, str]] = []
        real_classify = smod.dialog_router.classify

        def _recording_classify(text, speaker=SPEAKER_USER):
            calls.append((text, speaker))
            return real_classify(text, speaker=speaker)

        smod.dialog_router.classify = _recording_classify
        try:
            social._write("帮我回一下", chat_text)
        finally:
            smod.dialog_router.classify = real_classify
        check(
            "起草那一趟确实问了意图（走的是真路，不造假）",
            bool(calls),
            str(calls),
        )
        check(
            "问的是对方最新那一条，不是整屏材料",
            bool(calls) and calls[-1][0] == "小明：今天好累啊",
            str(calls[-1][0] if calls else "（没问）"),
        )
        check(
            "传的是 speaker='peer'（主聊天那条线仍旧是默认的 user）",
            bool(calls) and calls[-1][1] == SPEAKER_PEER,
            str(calls[-1][1] if calls else "（没传）"),
        )
        check(
            "对面最后那句「好累」→ 判出来是倾诉，而整屏那句是「指令」（真按最后一句判的）",
            social._peer_route is not None
            and social._peer_route.intent == router.INTENT_VENT
            and real_classify(chat_text, speaker=SPEAKER_PEER).intent == router.INTENT_COMMAND,
            f"{social._peer_route} / 整屏 {real_classify(chat_text, speaker=SPEAKER_PEER)}",
        )
        check(
            "材料为空的那一拍不硬猜（留 None，宁可不注入也别瞎猜）",
            (social._write("帮我回一下", ""), social._peer_route)[1] is None,
            str(social._peer_route),
        )
        check(
            "这一批没往起草提示词里注入对方意图（注入是下一批的事）",
            "对方刚刚对你说" not in (stub.prompts[-1] or "")
            and "倾诉" not in (stub.prompts[-1] or ""),
            "起草提示词里已经混进了意图那条线的东西",
        )

        # ==================================================================
        section("③ 四层入口的默认值都是 user（老调用点不传也还是老样子）")
        # ==================================================================
        check(
            "router.classify / IntentJudge.judge / IntentChain.classify / "
            "ChatterParseChain.analyze 的 speaker 默认都是 user",
            inspect.signature(router.classify).parameters["speaker"].default == SPEAKER_USER
            and inspect.signature(IntentJudge.judge).parameters["speaker"].default
            == SPEAKER_USER
            and inspect.signature(IntentChain.classify).parameters["speaker"].default
            == SPEAKER_USER
            and inspect.signature(ChatterParseChain.analyze).parameters["speaker"].default
            == SPEAKER_USER,
            "",
        )

        drain_threads()
    finally:
        box.cleanup()
    return summary("对象卡底座（v2-social-card-base）")


if __name__ == "__main__":
    raise SystemExit(main())
