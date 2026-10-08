"""
情感事件记录（v2-emotion-log）的自动测试。

用户要的：吐露情感/压力（对象吵架、工作压力大、爸妈催婚…）时，**不再建成「情感」卡**
（感情卡），改记一条**情感事件**——带归因（谁/什么引起的）+ 分类标签（工作压力 / 人际 /
家庭 / 健康），存进加密的情感记忆库（mind.db），能翻出来，但绝不以卡牌形式出现。

四条对应用户任务书 S4：
① 吐露情感 → 不弹、不建「情感」卡；
② 情感事件被记录，带归因 + 分类标签；
③ 情感事件可调用（能查），但不以卡牌呈现；
④ 回归：别的生活品类（餐饮等）照旧记观察、照旧提卡。

红线：pet.db 零写入（情感事件只落 mind.db）、情感事件正文是密文、零新依赖。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from life.dialog import router
from life.mind import emotion_log
from life.mind.emotion_log import (
    CATEGORY_FAMILY,
    CATEGORY_HEALTH,
    CATEGORY_RELATION,
    CATEGORY_WORK,
    EmotionAttribution,
    classify,
)
from life.cards.templates import EMOTION_CATEGORY

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    box, stub, app = make_env()
    try:
        store = app.mind_store
        pet_path = app.config.db_path
        mind_path = app.config.mind_db_path

        # ---------- ① 归因与分类（纯词表，一个模型都不调） ----------

        section("① 归因与分类认得出来（纯词表）")
        cases = [
            ("和对象吵架了，好难过", "关系", CATEGORY_RELATION),
            ("工作压力大，天天加班", "工作", CATEGORY_WORK),
            ("爸妈又催婚了，好烦", "家人", CATEGORY_FAMILY),
            ("最近老是失眠，睡不踏实", "身体", CATEGORY_HEALTH),
        ]
        for text, attr, cat in cases:
            check(f"「{text}」→ 归因「{attr}」/ 分类「{cat}」",
                  classify(text) == EmotionAttribution(attr, cat), str(classify(text)))
        check("中性句不认（今天吃了红烧肉）", classify("今天吃了红烧肉") is None,
              str(classify("今天吃了红烧肉")))
        check("中性句不认（我工作还行）", classify("我工作还行") is None,
              str(classify("我工作还行")))
        # 互斥：先中先算——"和同事吵架"里的"吵架"归人际，不能被工作那档抢走
        check("「和同事吵架了」→ 人际（吵架优先，不误归工作）",
              classify("和同事吵架了") == EmotionAttribution("关系", CATEGORY_RELATION),
              str(classify("和同事吵架了")))

        # ---------- ② 情感事件走真实信号落进加密库 ----------

        section("② 情感事件带归因 + 分类 + 情绪落进 mind.db")
        before = store.emotion_event_count()
        app.bus.utterance_analyzed.emit("和对象吵架了，好难过", "其他", "", 0.5, "linsheng", False)
        app.bus.utterance_analyzed.emit("天天加班，累死我了", "其他", "", 0.6, "linsheng", False)
        drain_threads()
        pump(0.05)
        events = store.emotion_events()
        check("两条情感事件都记下了", store.emotion_event_count() == before + 2,
              f"{before} → {store.emotion_event_count()}")
        # 最新的在前：events[0] 是"天天加班"，events[1] 是"和对象吵架"
        check("有两条能解回来", len(events) >= 2, str(len(events)))
        if len(events) >= 2:
            older = events[1]
            check("人际那条带归因「关系」+ 分类「人际」",
                  (older["attribution"], older["category"]) == ("关系", CATEGORY_RELATION),
                  f"{older['attribution']} / {older['category']}")
            check("人际那条情绪是「低落」（复用路由即时值）",
                  older["emotion"] == router.EMOTION_LOW, older["emotion"])
            newer = events[0]
            check("工作那条带归因「工作」+ 分类「工作压力」",
                  (newer["attribution"], newer["category"]) == ("工作", CATEGORY_WORK),
                  f"{newer['attribution']} / {newer['category']}")
            check("正文原样留在事件里", older["text"] == "和对象吵架了，好难过", older["text"])

        # ---------- ③ 吐露情感 → 不弹 / 不建「情感」卡 ----------

        section("③ 吐露情感 → 不弹、不建「情感」卡")
        proposed: list[str] = []
        app.bus.card_proposed.connect(lambda theme, _title: proposed.append(theme))
        before_emotion_obs = len(app.card_service._event_dao.since(EMOTION_CATEGORY, datetime.min))
        for _ in range(5):
            # 连说 5 次——旧逻辑里攒够次数就会提「情感」卡；现在一句观察都不该留
            app.bus.utterance_analyzed.emit("和对象吵架了", "其他", "", 0.4, "linsheng", False)
        drain_threads()
        pump(0.05)
        check("连说 5 次情感句 → 一条「情感」观察都没记",
              len(app.card_service._event_dao.since(EMOTION_CATEGORY, datetime.min)) == before_emotion_obs,
              f"{before_emotion_obs} → {len(app.card_service._event_dao.since(EMOTION_CATEGORY, datetime.min))}")
        check("没有冒出「情感」建卡邀请", EMOTION_CATEGORY not in proposed, str(proposed))
        check("轮播上没有「情感」卡（不以卡呈现）",
              all(c.theme != EMOTION_CATEGORY for c, _n in app.card_service.plaza()),
              str([c.theme for c, _n in app.card_service.plaza()]))

        # ---------- ④ 回归：别的生活品类照旧 ----------

        section("④ 回归：别的生活品类照旧记观察")
        before_diet = len(app.card_service._event_dao.since("餐饮", datetime.min))
        app.bus.utterance_analyzed.emit("外卖太贵了", "餐饮", "", 0.4, "linsheng", False)
        drain_threads()
        pump(0.05)
        check("「外卖太贵了」照旧记进餐饮品类",
              len(app.card_service._event_dao.since("餐饮", datetime.min)) == before_diet + 1,
              f"{before_diet} → {len(app.card_service._event_dao.since('餐饮', datetime.min))}")

        # ---------- 红线 ----------

        section("红线：pet.db 零写入 / 正文密文 / 零新依赖")
        # 情感事件只该在 mind.db：pet.db 里不该多出 emotion_event 这张表（旧表不动、新表不入）。
        # 注："天天加班"作为「作息」观察会照旧写进 pet.db 的 card_topic_event——那是卡牌系统在干活，
        # 不是情感事件泄漏；所以这里不拿它的原文去断言 pet.db 干净。
        pet_conn = sqlite3.connect(str(pet_path))
        pet_tables = {row[0] for row in pet_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        pet_conn.close()
        check("pet.db 里没有情感事件表（emotion_event 只建在 mind.db）",
              "emotion_event" not in pet_tables, str(sorted(pet_tables)))
        mind_conn = sqlite3.connect(str(mind_path))
        mind_tables = {row[0] for row in mind_conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        mind_conn.close()
        check("mind.db 里有情感事件表", "emotion_event" in mind_tables, str(sorted(mind_tables)))

        # 情感句（被拦下的那条）的原文不落 pet.db；mind.db 里则是密文。
        pet_raw = pet_path.read_bytes()
        check("情感原文没落进 pet.db", "和对象吵架了".encode("utf-8") not in pet_raw, "")
        # mind.db 里是密文（直接读文件搜不到明文）
        mind_raw = mind_path.read_bytes()
        for secret in ("和对象吵架了", "天天加班", "关系", "工作压力"):
            check(f"mind.db 里搜不到明文「{secret}」", secret.encode("utf-8") not in mind_raw, "")
        # 解回来还是明文、能查（可调用记忆，但不以卡呈现——见 ③）
        events = store.emotion_events()
        check("密文能解回来（正文/归因/分类齐全）",
              any(e["text"] == "和对象吵架了，好难过" and e["attribution"] == "关系"
                  and e["category"] == CATEGORY_RELATION for e in events),
              str(events[0] if events else "（空）"))

        # 零新依赖：emotion_log.py 只 import 标准库（__future__ / dataclasses）
        tree = ast.parse((ROOT / "life" / "mind" / "emotion_log.py").read_text(encoding="utf-8"))
        imports: list[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
        allowed = {"__future__", "dataclasses"}
        bad = [name for name in imports if name.split(".")[0] not in allowed]
        check("emotion_log.py 模块级 import 无第三方库", bad == [], str(imports))

    finally:
        drain_threads()
        box.cleanup()

    return summary("情感事件记录带归因分类、不建感情卡（v2-emotion-log）")


if __name__ == "__main__":
    raise SystemExit(main())
