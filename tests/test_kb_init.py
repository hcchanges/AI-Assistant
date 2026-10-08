"""
知识库目录落定与接入 RAG（v2-kb-init）的自动测试。

此前知识库问答（v2-rag）翻的是**回忆**（碎片 / 抓屏笔记 / 旧记忆），没有一个
"放文档"的地方。这一轮落定知识库目录（默认 ../知识库，与笔记同级）、启动时建目录，
并把该目录里的 .md/.txt 文档接进检索链——问"我们公司年假怎么算"能基于文档答，
而不是靠模型硬编。

验四件事（对应 S4）：
① 默认目录创建成功；
② 放入《年假政策》文档 → 问"我们公司年假怎么算？" → 文档内容进上下文（满 1 年 5 天）；
③ 检索相关不跑题（问年假命中、问南极不误触）；
④ 改路径持久生效（配置换目录，检索跟着换）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtWidgets import QTabWidget

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from ui import settings as settings_mod

from core.config import KNOWLEDGE_DIR_DEFAULT, knowledge_dir_usable
from life.mind.kb import KnowledgeBase, _key_tokens, _tokens


def kb_key(text: str) -> list[str]:
    """问句切出来的**实词**（滤过泛词的那一份）——知识库那条腿实际用的线索。"""
    return _key_tokens(_tokens(text))


def _kb_dialog(app):
    """照着 test_notes_path 的写法开一个设置对话框（同一个装配口径）。"""
    return settings_mod.SettingsDialog(
        config=app.config,
        bus=app.bus,
        pet_profile_dao=app.pet_profile_dao,
        activity_dao=app.activity_dao,
        category_dao=app.app_category_dao,
        memory_store=app.memory_store,
        reminder_dao=app.reminder_dao,
        jobs=app.jobs,
        user_id=app.user.id,
        intimacy=app.mind_intimacy,
        memory_book=app.mind_memory_book,
        parent=app.panel,
    )

# 知识库文档样例：用户丢进「知识库」文件夹的《年假政策》。
#
# **刻意跟真机那份一个形状——第一段是一句"本文档为……规定"的前言**。
# 旧版这份样例是"首行就是答案"的玩具文档，于是"一份文档只出一条命中行"这个 bug
# 在测试里根本显不出来：前言抢走那唯一一条名额、正确答案那条永远出不来这件事，
# 只有文档开头有一句含关键词的前言时才会发生（v2-kb-init 真机查不到的根因）。
YEAR_LEAVE_MD = """# 年假政策（测试文档）

本文档为公司员工年假相关规定，供人力资源与各部门参照执行。

## 一、适用范围
全体签订劳动合同且转正的正式员工。

## 二、休假天数
- 员工**入职满 1 年，享受 5 天年假**。
- 此后每满 1 年工龄，年假增加 1 天，累计上限 15 天。
- 入职不满 1 年但已转正的，按转正后剩余月份比例折算当年年假（四舍五入）。

## 三、申请流程
- 提前 3 个工作日在公司 OA 系统提交「年假申请」；
- 直属主管审批通过后生效；

## 四、离职结算
员工离职时，未休年假按日薪的 2 倍折算补偿。
"""


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
    """发一句，等这轮真的回完，返回宠物说了什么（照 test_rag 的写法排空上一轮）。"""
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


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- ① 默认目录创建 ----------

        section("① 默认目录：配置落定 ../知识库，启动时目录已建好")

        check("默认知识库路径常量就是 ../知识库（与笔记同级）",
              KNOWLEDGE_DIR_DEFAULT == "../知识库", KNOWLEDGE_DIR_DEFAULT)
        kb_root = Path(app.config.knowledge_dir)
        check("沙箱里知识库目录已建好（bootstrap 启动时 mkdir 生效）",
              kb_root.is_dir(), str(kb_root))
        check("知识库默认落点与笔记不是同一个目录",
              str(app.config.knowledge_dir) != str(app.config.notes_dir),
              f"知识库 {app.config.knowledge_dir} / 笔记 {app.config.notes_dir}")

        # ---------- ② 放文档 → 问年假 → 基于文档 ----------

        section("② 放入《年假政策》→ 问「年假怎么算」→ 文档内容进上下文")

        kb_root.mkdir(parents=True, exist_ok=True)
        (kb_root / "年假政策.md").write_text(YEAR_LEAVE_MD, encoding="utf-8")

        kb = KnowledgeBase(
            config=app.config,
            recall=app.mind_recall,
            memory_store=app.memory_store,
        )
        # 回忆词表认不出「年假怎么算」，但知识型问答应该认（靠实词命中知识库文档）
        check("「我们公司年假怎么算」不是回忆型问句（回忆词表不认）",
              kb.is_question("我们公司年假怎么算") is False, "")
        check("但知识型问答认得出（问句实词命中知识库文档）",
              kb.context_if_question("我们公司年假怎么算") != "", "")

        hits = kb.collect("我们公司年假怎么算")
        kb_hits = [h for h in hits if h.source == "知识库"]
        check("知识库这条腿真翻到了（来源标「知识库」）",
              len(kb_hits) > 0, "、".join(h.source for h in hits) or "（空）")
        check("翻到的就是年假那条（满 1 年 5 天）",
              any("满 1 年" in h.text or "年假 5 天" in h.text for h in kb_hits),
              " | ".join(h.text for h in kb_hits))
        # 真机那个 bug：文档开头一句"本文档为公司……规定"的前言同样含"年假"，
        # 「一份文档只出一条」让前言把答案挤掉——问"满 1 年几天"答不出来。
        check("前言没有把答案挤掉（一份文档出多条，不是只出第一条命中行）",
              len(kb_hits) >= 2, f"{len(kb_hits)} 条：" + " | ".join(h.text for h in kb_hits))
        check("上下文里真的带着「5 天」这个答案（不是只有前言）",
              "5 天" in kb.context("我们公司年假怎么算"),
              kb.context("我们公司年假怎么算").replace("\n", " ")[:160])
        check("问到哪一节就出哪一节：问申请流程 → 出「3 个工作日」",
              any("3 个工作日" in h.text for h in kb.collect("年假怎么申请")
                  if h.source == "知识库"),
              " | ".join(h.text for h in kb.collect("年假怎么申请") if h.source == "知识库"))
        check("问离职结算 → 出「2 倍折算补偿」",
              any("2 倍折算" in h.text for h in kb.collect("离职年假怎么算")
                  if h.source == "知识库"),
              " | ".join(h.text for h in kb.collect("离职年假怎么算") if h.source == "知识库"))

        ctx = kb.context("我们公司年假怎么算")
        check("拼出的上下文带「别改写」的交代（不许模型自己编）",
              "别改写" in ctx, ctx.replace("\n", " ")[:80])

        # 走真回话链：问句把知识库文档原文拼进喂给模型的 messages
        _speak(app, stub, "我们公司年假怎么算？")
        msgs = stub.messages[-1] if stub.messages else []
        joined = "\n".join(m.get("content", "") for m in msgs)
        check("模型收到了消息", bool(msgs), f"{len(msgs)} 条")
        check("文档原文「满 1 年」进了喂给模型的上下文（不编造的前提）",
              "满 1 年" in joined, joined[:120])
        check("知识库来源也标了出处", "知识库" in joined, "")

        # ---------- ③ 检索相关不跑题 ----------

        section("③ 检索相关不跑题：问年假命中、问南极不误触")

        check("问年假 → 命中知识库", kb.context_if_question("年假怎么算") != "", "")
        check("问请假天数 → 也命中（「天」字在文档里）",
              kb.context_if_question("我们年假有几天") != "", "")
        # 知识库里没有南极，普通聊天不该因为"有个知识库目录"就误触
        check("问南极（知识库里没有）→ 不触发知识库问答",
              kb.context_if_question("南极科考站那边天气怎么样") == "", "")
        check("普通话「今天天气不错」→ 不触发",
              kb.context_if_question("今天天气不错") == "", "")
        # 泛词防护：制度文档里必然有"我们/公司/怎么"这类词，拿它们当检索线索的话
        # 随便一句带"公司"的闲聊都会把整段文档灌给模型——那正是"跑题"。
        check("「我们公司几点下班」→ 不触发（只撞上泛词「公司」，不算实词命中）",
              kb.context_if_question("我们公司几点下班") == "", "")
        check("「我们公司附近有停车场吗」→ 不触发",
              kb.context_if_question("我们公司附近有停车场吗") == "", "")
        check("泛词不进检索线索（_key_tokens 滤掉了「公司」「我们」）",
              "公司" not in kb_key("我们公司年假怎么算"), str(kb_key("我们公司年假怎么算")))
        check("实词留着（「年假」还在）",
              "年假" in kb_key("我们公司年假怎么算"), str(kb_key("我们公司年假怎么算")))

        # ---------- ④ 改路径持久生效 ----------

        section("④ 改路径持久生效：换目录，检索跟着换")

        moved = box.dir / "knowledge_moved"
        app.config.knowledge.dir = str(moved)
        check("配置改完，knowledge_dir 立即指向新目录",
              Path(app.config.knowledge_dir) == moved, str(app.config.knowledge_dir))
        # 新目录还没有文档 → 问年假也不命中（旧目录的文档不再被扫到）
        check("换了目录后，旧文档不再被扫到（检索跟着配置走）",
              KnowledgeBase(
                  config=app.config, recall=app.mind_recall, memory_store=app.memory_store
              ).context_if_question("我们公司年假怎么算") == "", "")
        moved.mkdir(parents=True, exist_ok=True)
        (moved / "考勤.md").write_text("- 迟到一次扣 50 元\n- 全勤奖每月 200 元", encoding="utf-8")
        moved_hits = KnowledgeBase(
            config=app.config, recall=app.mind_recall, memory_store=app.memory_store
        ).collect("迟到一次扣多少钱")
        check("新目录的文档能检索到（「迟到」命中考勤文档）",
              any("迟到" in h.text for h in moved_hits), " | ".join(h.text for h in moved_hits))

        # ---------- ⑤ 坏路径退默认 ----------

        section("⑤ 坏路径兜底：配置的目录建不出来 → 退回默认那个")

        good = box.dir / "knowledge_ok"
        good.mkdir(parents=True, exist_ok=True)
        check("正常路径：校验通过", knowledge_dir_usable(good), str(good))
        app.config.knowledge.dir = str(good)
        check("正常路径：knowledge_dir 就用它（没被兜底抢走）",
              Path(app.config.knowledge_dir) == good, str(app.config.knowledge_dir))

        # 一个建不出来的路径：Windows 上典型的写法是"不存在的盘符 + 非法字符"
        broken = "Z:\\nope\\知识库"
        app.config.knowledge.dir = broken
        fallback = Path(app.config.knowledge_dir)
        check("坏路径 → knowledge_dir 退回默认落点",
              fallback == app.config.knowledge_dir_default,
              f"{broken} → {fallback}")
        check("退回的落点是默认那个「知识库」（不是空空如也）",
              fallback == Path(app.config.knowledge_dir_default), str(fallback))
        check("退回后目录真的存在（兜底是能用的，不是换个名字继续坏）",
              fallback.is_dir(), str(fallback))
        # 退默认之后检索照旧能用：放份文档进去，问它照样命中
        (fallback / "考勤.md").write_text("- 迟到一次扣 50 元", encoding="utf-8")
        check("退默认之后检索照旧通（放到默认目录的文档能查到）",
              any("迟到" in h.text for h in KnowledgeBase(
                  config=app.config, recall=app.mind_recall, memory_store=app.memory_store
              ).collect("迟到扣多少钱")),
              "")
        app.config.knowledge.dir = str(good)

        # ---------- ⑥ 设置页有入口（v2-kb-init）----------

        section("⑥ 设置页有「知识库」一页：路径可看、可改")

        app.config.knowledge.dir = str(good)
        # 先放一份文档再开对话框：提示里那一行"有几份文档"是**开页时当场扫的**，
        # 后放的就该下次开页才数得到（这正好也是要验的行为）
        (good / "请假.md").write_text("- 事假需提前一天申请", encoding="utf-8")
        dialog = _kb_dialog(app)
        try:
            tabs = dialog.findChild(QTabWidget)
            names = [tabs.tabText(i) for i in range(tabs.count())]
            check("设置页多了「知识库」这一页", "知识库" in names, str(names))
            check("这一页有：路径输入框 / 落点提示",
                  hasattr(dialog, "_kb_dir_edit") and hasattr(dialog, "_kb_dir_hint"), "")
            check("回填的是当前配置里的落点",
                  dialog._kb_dir_edit.text() == str(app.config.knowledge.dir),
                  dialog._kb_dir_edit.text())
            check("页面上写着现在的落点（省得再猜「放哪」）",
                  str(app.config.knowledge_dir) in dialog._kb_dir_hint.text().replace("​", ""),
                  dialog._kb_dir_hint.text())
            check("提示里当场报了文档份数（「我放进去了怎么没反应」时能自查）",
                  "1 份" in dialog._kb_dir_hint.text().replace("​", ""),
                  dialog._kb_dir_hint.text())
            # 改一个路径 → 保存 → config 里真的换了，检索跟着换
            dialog._kb_dir_edit.setText(str(good))
            dialog._save()
            check("保存后配置里的落点是填的那个",
                  str(app.config.knowledge.dir) == str(good), str(app.config.knowledge.dir))
            check("提示刷新成了新的落点",
                  str(app.config.knowledge_dir) in dialog._kb_dir_hint.text().replace("​", ""),
                  dialog._kb_dir_hint.text())
            check("新落点的文档马上能查到（不用重启、没有索引要等）",
                  any("事假" in h.text for h in KnowledgeBase(
                      config=app.config, recall=app.mind_recall, memory_store=app.memory_store
                  ).collect("事假怎么请")),
                  "")
        finally:
            dialog.close()

        # ---------- 红线 ----------

        section("红线：检索只读，不写库、不写文档")

        import sqlite3

        def _rows(db, table):
            con = sqlite3.connect(db)
            try:
                return con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            finally:
                con.close()

        before_utt = _rows(app.config.db_path, "utterance")
        before_frag = app.mind_store.fragment_count()
        doc_before = (kb_root / "年假政策.md").read_bytes()
        for _ in range(3):
            KnowledgeBase(
                config=app.config, recall=app.mind_recall, memory_store=app.memory_store
            ).context_if_question("年假怎么算")
        check("pet.db utterance 一行没多", _rows(app.config.db_path, "utterance") == before_utt,
              f"{before_utt} → {_rows(app.config.db_path, 'utterance')}")
        check("mind.db 碎片一条没多", app.mind_store.fragment_count() == before_frag, "")
        check("知识库文档字节没变（只读扫盘）", (kb_root / "年假政策.md").read_bytes() == doc_before, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("知识库目录落定与接入 RAG（v2-kb-init）")


if __name__ == "__main__":
    raise SystemExit(main())
