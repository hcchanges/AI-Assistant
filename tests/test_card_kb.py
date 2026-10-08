"""
每张卡自动关联专属知识库（v2-card-kb）的自动测试。

用户规则：每张生活卡 / 工作卡对应一个专属知识库（卡级 RAG，不是现有那一个全局库）。
这张卡底下的聊天窗里说的话，先查**这张卡自己的**知识库目录，查不到再回退全局库；
全局库照旧保留（无卡上下文的全局提问还是走它）。

验六件事（对着指令的 S4 ①~⑥）：

1. **建卡即建目录**：生活卡建好就有 `知识库/cards/<id>`，工作卡按稳定 key 有 `cards/<key>`；
2. **卡内优先、不串门**：卡 A 放文档，卡内提问命中 A，串不进卡 B、也串不进全局；
3. **卡库空不报错**：目录空时卡内提问走全局兜底（不冒"没有知识库"的错误）；
4. **卡库查不到回退全局**：卡库有文档但对不上问句，退回全局库接着找；
5. **无卡上下文走全局**：全局知识库问答原样保留；
6. **红线**：卡级检索只扫 .md/.txt，一个字不碰 pet.db / mind.db。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.cards.chat import card_kb_root, ensure_card_kb_root
from life.mind.kb import KnowledgeBase


def _card(service, title: str):
    for card, _pos in service.plaza():
        if card.title == title:
            return card
    return None


def _kb(config, app) -> KnowledgeBase:
    return KnowledgeBase(
        config=app.config, recall=app.mind_recall, memory_store=app.memory_store
    )


# ---------- S4-① 建卡即建目录 ----------


def test_card_dir_created() -> None:
    section("① 建生活/工作卡 → 各自生成专属库目录")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        check("生活卡建出来了", trip is not None, "")

        life_root = Path(app.config.knowledge_dir) / "cards" / str(trip.id)
        check("生活卡建好即生成专属目录 知识库/cards/<id>",
              life_root.is_dir(), str(life_root))
        check("kb_path 记的正是这个落点（建卡时顺手填上）",
              trip.kb_path == str(life_root), f"kb_path={trip.kb_path!r}")

        # 按 key 落盘的那种卡（工作卡）在 JSON 里不在 DB，落点按 key 现算（lazy 建目录）
        wroot = ensure_card_kb_root(app.config, "feature_x")
        check("按 key 的专属目录 知识库/cards/<key> 也能建出来",
              wroot == Path(app.config.knowledge_dir) / "cards" / "feature_x"
              and wroot.is_dir(), str(wroot))
        # 约定路径不带特殊字符（key 有特殊字符时也会被净化成安全目录名）
        check("目录名只留字母数字下划线连字符（避特殊字符）",
              card_kb_root(app.config, "a/b:c").name == "a_b_c",
              card_kb_root(app.config, "a/b:c").name)
        check("两张卡的目录不重名（生活 id / 按 key 的各占一间）",
              life_root.name == str(trip.id) and wroot.name == "feature_x", "")
    finally:
        box.cleanup()
        drain_threads()


# ---------- S4-②③④ 卡内优先 + 全局兜底 ----------


def test_card_kb_routing() -> None:
    section("②③④ 卡内优先、不串门、空/查不到回退全局")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        trip = _card(app.card_service, "成都行")
        diet = _card(app.card_service, "少喝奶茶")

        trip_dir = ensure_card_kb_root(app.config, trip.id)
        diet_dir = ensure_card_kb_root(app.config, diet.id)
        empty_dir = ensure_card_kb_root(app.config, "empty_card")
        global_dir = Path(app.config.knowledge_dir)

        # 卡 A 放"年假 5 天"；卡 B 放"全勤 200"；全局放"迟到 50 / 年假上限 15"
        (trip_dir / "年假.md").write_text("- 入职满 1 年，享受 5 天年假。", encoding="utf-8")
        (diet_dir / "考勤.md").write_text("- 全勤奖每月 200 元。", encoding="utf-8")
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "迟到.md").write_text("- 迟到一次扣 50 元。", encoding="utf-8")
        (global_dir / "年假政策.md").write_text("- 年假累计上限 15 天。", encoding="utf-8")

        kb = _kb(app.config, app)

        # ② 卡内优先：卡 A 问年假 → 命中 A 的"5 天"，串不进 B 的"全勤"、也串不进全局的"15 天"
        ctx_a = kb.card_context("年假怎么算", str(trip_dir))
        check("卡 A 问年假 → 命中 A 自己的文档（5 天）", "5 天" in ctx_a, ctx_a.replace("\n", " "))
        check("不串卡 B（「全勤」没进来）", "全勤" not in ctx_a and "200" not in ctx_a, ctx_a)
        check("不串全局（「15 天」没进来）", "15 天" not in ctx_a, ctx_a)

        # 卡 B 问年假：卡 B 目录里没有年假文档 → 回退全局，拿到全局的"15 天"
        ctx_b = kb.card_context("年假怎么算", str(diet_dir))
        check("卡 B 问年假（B 目录没有）→ 回退全局拿到 15 天", "15 天" in ctx_b,
              ctx_b.replace("\n", " "))

        # ③ 卡库空 → 走全局兜底、不报错（回的是全局内容，不是空/错误）
        ctx_empty = kb.card_context("年假怎么算", str(empty_dir))
        check("空卡库问年假 → 全局兜底（15 天），不报错", "15 天" in ctx_empty,
              ctx_empty.replace("\n", " "))

        # ④ 卡库有文档但对不上问句 → 回退全局
        ctx_miss = kb.card_context("迟到扣多少钱", str(trip_dir))
        check("卡 A 问迟到（A 只有年假）→ 回退全局拿到「迟到扣 50」",
              "迟到" in ctx_miss and "50 元" in ctx_miss, ctx_miss.replace("\n", " "))

        # 两边都查不到 → 回空串（卡窗照常聊天，别冒"没有知识库"的错误）
        check("两边都查不到 → 回空串（不是报错文案）",
              kb.card_context("南极科考站怎么样", str(trip_dir)) == "", "")

        # ⑤ 无卡上下文：全局知识库问答原样保留
        ctx_global = kb.context("年假怎么算")
        check("无卡上下文全局提问照旧走全局库（15 天）", "15 天" in ctx_global,
              ctx_global.replace("\n", " ")[:120])
    finally:
        box.cleanup()
        drain_threads()


# ---------- 卡窗真接上这条链 ----------


def test_card_chat_injects_kb() -> None:
    section("卡窗回话真的把卡库原文接进提示词（且不串全局/别卡）")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        app.card_service.create_manual("餐饮", "少喝奶茶", "🥤", "一周两杯")
        trip = _card(app.card_service, "成都行")

        trip_dir = ensure_card_kb_root(app.config, trip.id)
        (trip_dir / "年假.md").write_text("- 满 1 年 5 天年假。", encoding="utf-8")
        (Path(app.config.knowledge_dir) / "迟到.md").write_text(
            "- 迟到一次扣 50 元。", encoding="utf-8")

        # 装配处给的上下文里带着这张卡的 kb 落点
        ctx = app._card_chat_context(f"life:{trip.id}")
        check("卡窗上下文里带着这张卡的 kb 落点",
              ctx.get("kb") == str(trip_dir), f"kb={ctx.get('kb')!r}")

        # 直接跑一遍回话（桩模型）：提示词里该有卡库原文"5 天"，不该有全局的"迟到"
        app.card_chat_service._reply(ctx, "年假怎么算", [])
        prompt = stub.prompts[-1] if stub.prompts else ""
        check("卡窗提示词里带着卡库原文（5 天）", "5 天" in prompt, prompt[:80])
        check("卡窗提示词里没串进全局库（迟到）", "迟到" not in prompt, "")
        check("卡窗提示词里标了知识库那段", "专属知识库" in prompt, "")

    finally:
        box.cleanup()
        drain_threads()


# ---------- 红线：只扫 .md/.txt，不碰 pet.db / mind.db ----------


def test_card_kb_no_db_write() -> None:
    section("红线：卡级检索只读 .md/.txt，不写 pet.db / mind.db")
    box, stub, app = make_env()
    try:
        app.card_service.create_manual("出行", "成都行", "✈️", "玩一周")
        trip = _card(app.card_service, "成都行")
        trip_dir = ensure_card_kb_root(app.config, trip.id)
        doc = trip_dir / "年假.md"
        doc.write_text("- 满 1 年 5 天年假。", encoding="utf-8")

        import hashlib
        import sqlite3

        def _rows(table: str) -> int:
            con = sqlite3.connect(app.config.db_path)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        before_utt = _rows("utterance")
        before_mem = _rows("memory")
        mind_before = hashlib.sha256((box.dir / "mind.db").read_bytes()).hexdigest()
        doc_before = doc.read_bytes()

        kb = _kb(app.config, app)
        for _ in range(3):
            kb.card_context("年假怎么算", str(trip_dir))

        check("pet.db utterance 一行没多", _rows("utterance") == before_utt,
              f"{before_utt} -> {_rows('utterance')}")
        check("pet.db memory 一行没多", _rows("memory") == before_mem,
              f"{before_mem} -> {_rows('memory')}")
        check("mind.db 一个字节没动",
              hashlib.sha256((box.dir / "mind.db").read_bytes()).hexdigest() == mind_before, "")
        check("卡库文档字节没变（只读扫盘）", doc.read_bytes() == doc_before, "")
    finally:
        box.cleanup()
        drain_threads()


def main() -> int:
    test_card_dir_created()
    test_card_kb_routing()
    test_card_chat_injects_kb()
    test_card_kb_no_db_write()
    return summary("每张卡专属知识库（v2-card-kb）：")


if __name__ == "__main__":
    raise SystemExit(main())
