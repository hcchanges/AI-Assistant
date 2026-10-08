"""
知识库问答（v2-rag）的自动测试。

覆盖：
- S0/S1 词表判定：指回过去的说法命中、普通话不命中、开关关掉一律不命中；
- S1 三源召回：碎片（复用 A+ 双路召回）/ 抓屏笔记（只读扫盘）/ pet.db 旧记忆，
  顺序按可信度、按条数硬截断、同一句话三处都有只出一条；
- S1 接进回话链：问句才把原文拼进 messages，普通话一字不改；
- S0 红线：危机词最高优先（命中就不翻库、也不调模型）；
- S3 红线：检索只读——跑完 pet.db / mind.db 一行没多、笔记文件字节没变。

模型全程打桩；碎片与旧记忆用真库（MindStore / MemoryStore，落沙箱临时目录），
笔记用真文件。也就是说**召回与拼装走的是真实链路**，只有"生成"那一步是假的。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.mind.kb import DEFAULT_ASK_WORDS, KnowledgeBase

# 判"这是在问我记过的事"的样句。左列该命中，右列**故意**不该命中——
# 「我记得」收进来的话，"我记得明天要开会"会被当成翻旧账，那就认错人了
_ASKED = (
    "我之前记的早饭是什么",
    "我之前说的话你还记得吗",
    "我上次说要去哪来着",
    "我上回提过的那家店叫什么",
    "我跟你说过的那本书",
    "我告诉过你的那个日子",
    "你还记得我不吃辣吗",
    "记不记得我说过要买什么",
    "我记过什么事",
    "我存过的那些东西呢",
)
_NOT_ASKED = (
    "今天天气不错",
    "晚上吃什么好呢",
    "我记得明天要开会",
    "帮我查一下量子力学",
    "你觉得我该选哪个",
    "这段代码怎么写",
    "",
    "   ",
)

# 来源优先级：碎片（用户原话）< 笔记 < 旧记忆。数字越小越靠前
_PRIORITY = {"碎片": 0, "笔记": 1, "记忆": 2}


def _kb(app, **overrides) -> KnowledgeBase:
    """拿应用里那套接线，只按需改配置——测的始终是真实装配出来的那个 KnowledgeBase。

    每次先把三个配置项拨回默认，测试之间才不会互相带脏（rag 段是配置对象上的
    同一个实例，前一个用例改过的 max_items 会一直留着）。
    """
    app.config.rag.enabled = True
    app.config.rag.max_items = 4
    app.config.rag.ask_words = []
    for key, value in overrides.items():
        setattr(app.config.rag, key, value)
    return KnowledgeBase(
        config=app.config,
        recall=app.mind_recall,
        memory_store=app.memory_store,
    )


def _note(app, name: str, body: str) -> Path:
    root = Path(app.config.notes_dir)
    root.mkdir(parents=True, exist_ok=True)
    path = root / name
    path.write_text(body, encoding="utf-8")
    return path


def _seed(app) -> None:
    """三处来源各塞一条。刻意让 09-30 那条笔记跟碎片**逐字撞上**（验去重）。"""
    app.mind_store.add_fragment(
        "爱吃楼下包子铺的鲜肉包", tags=("早饭",), src_time="2026-09-28T08:10:00"
    )
    app.mind_store.add_fragment(
        "周末想去趟旧书店", tags=("计划",), src_time="2026-09-30T21:00:00"
    )
    _note(app, "2026-09-29.md", "- 早饭还是爱吃鲜肉包\n- 修好的伞还没去拿")
    _note(app, "2026-09-30.md", "- 爱吃楼下包子铺的鲜肉包\n- 下午的会推迟了")
    # 旧记忆那条腿（pet.db）：留一条**跟"早饭"沾边的**，好验它真的被翻出来；
    # 下面"翻不到"那一节会拿它验字面关（问南极科考站时它必须被挡掉）
    app.memory_store.remember_many(_extracted("早饭爱喝热豆浆"))


def _rows(db_path, table: str) -> int:
    """直接数 pet.db 某张表有多少行：红线要验的是"文件里真的没多"。"""
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def _parse(text: str, reply: str = "嗯，我记得") -> dict:
    return {
        "scene": text[:12],
        "time_hint": "",
        "time_iso": "",
        "time_confidence": "none",
        "emotion": "平静",
        "module": "其他",
        "command": "",
        "reply": reply,
    }


def _speak(app, stub, text: str) -> list[str]:
    """发一句，等这轮真的回完，返回宠物说了什么。

    **先把上一轮排空**（等线程池 + 转一次事件循环），不然上一轮那句飘出来的
    pet_speak 会落进这一轮的监听里，`wait_for` 立刻满足，而这一轮的模型调用其实
    还没发生——`stub.messages` 就成了空的（这个坑真踩过一次，随机失败）。
    排空之后，这一轮的 `spoken` 只可能是这一轮说的话。
    """
    drain_threads()
    pump(0.1)
    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    stub.parse_result = _parse(text)
    stub.messages.clear()
    app.chatter_service.handle(text)
    wait_for(lambda: bool(spoken))
    app.bus.pet_speak.disconnect(spoken.append)
    return spoken


class _Exploding:
    """会抛的检索腿：用来验"一条腿塌了不连累整次问答"。"""

    def search(self, query: str, limit: int = 5):
        raise RuntimeError("向量库没起来")


def main() -> int:
    box, stub, app = make_env()
    try:
        section("S1 词表判定：指回过去的说法才认，普通话一律不认")
        kb = _kb(app)
        for text in _ASKED:
            check(f"认得出「{text}」", kb.is_question(text) is True)
        for text in _NOT_ASKED:
            check(f"不误认「{text or '（空串）'}」", kb.is_question(text) is False)
        check("「我记得明天要开会」不认（那是在说事，不是在问我）",
              kb.is_question("我记得明天要开会") is False)
        check("「帮我查一下」不认（那是要联网检索，不是翻我自己的库）",
              kb.is_question("帮我查一下量子力学") is False)

        section("S3 总闸：关掉之后一句话都不认")
        off = _kb(app, enabled=False)
        check("关掉后问句也不认", off.is_question("我之前记的早饭是什么") is False)
        check("关掉后 context_if_question 回空串",
              off.context_if_question("我之前记的早饭是什么") == "")

        section("S2 词表可配置：填了就覆盖内置那张")
        custom = _kb(app, ask_words=["翻翻旧账"])
        check("填的词命中", custom.is_question("翻翻旧账看看") is True)
        check("内置那份这时不再生效", custom.is_question("我之前记的早饭是什么") is False)
        check("清空后回落到内置词表", _kb(app).is_question("我之前记的早饭是什么") is True)
        check("内置词表非空", len(DEFAULT_ASK_WORDS) > 0, f"{len(DEFAULT_ASK_WORDS)} 条")

        section("S1 三源召回：碎片在前、笔记次之、旧记忆最后")
        _seed(app)
        kb = _kb(app)
        hits = kb.collect("我之前记的早饭")
        check("翻到了东西", len(hits) > 0, f"{len(hits)} 条")
        check("第一条来自碎片（用户原话优先）",
              bool(hits) and hits[0].source == "碎片",
              hits[0].source if hits else "（空）")
        check("碎片带上记下来的时间",
              bool(hits) and bool(hits[0].when),
              hits[0].when if hits else "（空）")
        check("笔记那条腿也翻到了（只读扫盘这条真的通）",
              any(h.source == "笔记" for h in hits),
              "、".join(h.source for h in hits))
        check("碎片原文出现在结果里",
              any("鲜肉包" in h.text for h in hits),
              " | ".join(h.text for h in hits))
        order = [_PRIORITY.get(h.source, 9) for h in hits]
        check("来源顺序按可信度排（不出现低优先的插在高优先前面）",
              order == sorted(order), "、".join(h.source for h in hits))

        section("S1 去重：同一句话碎片与笔记都有，只出一条")
        dup = kb.collect("我之前记的鲜肉包")
        exact = [h.text for h in dup if h.text == "爱吃楼下包子铺的鲜肉包"]
        check("逐字相同的那条只出现一次", len(exact) == 1, f"{len(exact)} 条")
        check("去重留下了优先级更高的碎片",
              bool(dup) and dup[0].text == "爱吃楼下包子铺的鲜肉包" and dup[0].source == "碎片",
              f"{dup[0].source if dup else '（空）'}：{dup[0].text if dup else ''}")

        section("S2 只传检索到的那几条：条数硬上限")
        capped = _kb(app, max_items=2).collect("我之前记的早饭")
        check("截到 max_items=2 条", len(capped) == 2, f"{len(capped)} 条")
        check("截断留的是优先级高的（碎片）",
              bool(capped) and capped[0].source == "碎片",
              capped[0].source if capped else "（空）")
        check("max_items=1 就只回 1 条", len(_kb(app, max_items=1).collect("我之前记的早饭")) == 1)

        section("S1 翻不到也要说清楚，不能给空串让模型自己编")
        empty_kb = _kb(app)
        # 这句刻意挑一个库里绝对没有的词：pet.db 那条向量腿**没有阈值**，
        # 不自己过一道字面关的话，它照样会把"早饭爱喝热豆浆"塞回来
        distant = empty_kb.collect("我之前记的南极科考站那边的事")
        check("不相关的旧记忆被字面关挡住（不会硬塞回来）",
              all(h.source != "记忆" for h in distant),
              "、".join(h.source for h in distant) or "（一条都没翻到）")
        empty = empty_kb.context("我之前记的南极科考站那边的事")
        check("没翻到也返回非空", bool(empty.strip()), f"{len(empty)} 字")
        check("明说了没记过", "没有" in empty or "没记过" in empty,
              empty.replace("\n", " ")[:60])
        check("明说不许编", "不要编" in empty, "")
        check("翻到了就不带这段交代", "不要编" not in empty_kb.context("我之前记的早饭"), "")

        section("S2 单条长度上限：长笔记行会被截")
        _note(app, "2026-10-01.md", "- 一条很长的记录" + "细节" * 60)
        long_hits = [h for h in _kb(app).collect("我之前记的细节") if h.source == "笔记"]
        check("翻到了那条长笔记", bool(long_hits), f"{len(long_hits)} 条")
        check("单条被截到 80 字以内",
              all(len(h.text) <= 80 for h in long_hits),
              f"最长 {max((len(h.text) for h in long_hits), default=0)} 字")

        section("S2 一条腿塌了不连累整次问答")
        broken = KnowledgeBase(
            config=app.config, recall=_Exploding(), memory_store=app.memory_store
        )
        survived = broken.collect("我之前记的早饭")
        check("碎片腿抛异常，这次问答仍然有结果", len(survived) > 0, f"{len(survived)} 条")
        check("结果里没有碎片那条（它塌了）",
              all(h.source != "碎片" for h in survived),
              "、".join(h.source for h in survived))

        section("S1 接进回话链：问句才拼上下文，普通话一字不改")
        _speak(app, stub, "我之前记的早饭是什么")
        asked_msgs = stub.messages[-1] if stub.messages else []
        joined = "\n".join(m["content"] for m in asked_msgs)
        check("模型收到了消息", bool(asked_msgs), f"{len(asked_msgs)} 条")
        check("提问时拼进了片段原文", "鲜肉包" in joined, "")
        check("拼进去的那段带「别改写」的交代", "别改写" in joined, "")
        check("翻出来的那段排在用户这句话前面",
              all(m["content"] != "我之前记的早饭是什么" for m in asked_msgs[:-1]),
              f"共 {len(asked_msgs)} 条")
        check("最后一条仍是用户这句话",
              bool(asked_msgs) and asked_msgs[-1]["content"] == "我之前记的早饭是什么",
              asked_msgs[-1]["content"][:24] if asked_msgs else "（空）")

        _speak(app, stub, "今天天气不错")
        plain_msgs = stub.messages[-1] if stub.messages else []
        plain = "\n".join(m["content"] for m in plain_msgs)
        check("普通话没拼上下文", "鲜肉包" not in plain and "别改写" not in plain, "")
        check("普通话最后一条仍是用户这句话",
              bool(plain_msgs) and plain_msgs[-1]["content"] == "今天天气不错",
              f"{len(plain_msgs)} 条")

        section("红线：危机词最高优先——前半句像翻旧账也不许盖过去")
        utt_before = _rows(app.config.db_path, "utterance")
        spoken = _speak(app, stub, "我之前记的，可是我真的不想活了")
        check("走的是危机话术（含热线 12356）",
              bool(spoken) and "12356" in spoken[-1], spoken[-1][:40] if spoken else "（没说话）")
        check("危机句一个字都没送模型", not stub.messages, f"{len(stub.messages)} 次")
        check("危机句也没把翻出来的东西说出去",
              bool(spoken) and "鲜肉包" not in spoken[-1], "")
        utt_after = _rows(app.config.db_path, "utterance")
        check("危机句照样不落 pet.db（红线沿用）", utt_after == utt_before,
              f"{utt_before} → {utt_after}")

        section("边界：软表达的分寸不因为接了检索就变")
        # "撑不下去"按既有设计属**软**表达（只断网时本地兜一句），不是硬危机。
        # 接了检索之后这条分寸必须原样：照样送模型，不被新代码顺手硬拦。
        # 注意判"甩没甩热线"只能看**宠物嘴里那句**——角色卡的安全策略里本来就写着
        # 12356，每一轮提示词里都有它，拿提示词判会永远失败
        soft_spoken = _speak(app, stub, "我之前记的早饭，可是我撑不下去了")
        check("软表达没被当成硬危机（宠物嘴里没出现热线）",
              bool(soft_spoken) and "12356" not in soft_spoken[-1],
              soft_spoken[-1][:40] if soft_spoken else "（没说话）")
        soft = stub.messages[-1] if stub.messages else []
        soft_joined = "\n".join(m["content"] for m in soft)
        check("软表达照样送模型", bool(soft), f"{len(soft)} 条")
        check("这句照样翻到了自己记过的东西", "鲜肉包" in soft_joined, "")

        section("S3 红线：检索只读——两库一行没多、笔记字节没变")
        notes_root = Path(app.config.notes_dir)
        notes_before = {p.name: p.read_bytes() for p in notes_root.glob("*.md")}
        frag_before = app.mind_store.fragment_count()
        pet_before = len(app.memory_dao.recent(200))
        for _ in range(3):          # 翻三遍，看有没有哪一遍忍不住写回去
            _kb(app).context_if_question("我之前记的早饭")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == frag_before,
              f"{frag_before} → {app.mind_store.fragment_count()}")
        check("pet.db 记忆一行没多", len(app.memory_dao.recent(200)) == pet_before,
              f"{pet_before} → {len(app.memory_dao.recent(200))}")
        notes_after = {p.name: p.read_bytes() for p in notes_root.glob("*.md")}
        check("笔记文件字节没变（只读扫盘）", notes_after == notes_before,
              f"{len(notes_after)} 个文件")

        section("S3 红线：没有新依赖、没有联网检索的口子")
        import inspect

        import life.mind.kb as kb_mod

        source = inspect.getsource(kb_mod)
        check("kb.py 不 import 任何联网客户端",
              not any(w in source for w in ("requests", "httpx", "urllib", "aiohttp")), "")
        check("kb.py 不 import LLMFactory（检索这条线不认识模型）",
              "LLMFactory" not in source, "")
        check("kb.py 不写库（没有 INSERT/UPDATE/DELETE 这类写操作）",
              not any(w in source for w in ("execute(", "INSERT", "COMMIT", "write_text",
                                            "open(", "mkdir")), "")
        check("检索三源都写明只读", "只读" in source, "")

        section("S0 边界：provider 没接上时，回话链一个字都不变")
        app.psych_service.set_kb_provider(None)
        _speak(app, stub, "我之前记的早饭是什么")
        none_msgs = stub.messages[-1] if stub.messages else []
        check("没接 provider 时问句也不会被加工",
              bool(none_msgs) and "别改写" not in "\n".join(m["content"] for m in none_msgs), "")
        app.psych_service.set_kb_provider(app.kb.context_if_question)
    finally:
        drain_threads()
        box.cleanup()
    return summary("知识库问答")


def _extracted(content: str):
    from memory.extractor import ExtractedMemory

    return [ExtractedMemory(kind="preference", content=content, importance=0.9)]


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
