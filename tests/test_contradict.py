"""
对话矛盾检测补强（v2-contradict-detect）的自动测试。

用户真机反馈的 bug：先说"今晚做饭，亲自做红烧肉"，几轮后又说"今晚到朋友家聚餐，
希望他们做好饭"——两句话对"今晚谁做饭 / 在哪吃"直接矛盾，宠物却静默地接了下去。
原有矛盾检测（life/dialog/location.py）只比"人现在在哪座城"，抓不到这种
**同一事件的行为主体 / 内容**冲突。

这一轮新增 life/dialog/plan.py：记住上一句明确报出来的「谁做饭 / 在哪吃」两个槽位，
这一句报得不一样就产出一条 `PlanConflict`，回话那头先把矛盾摆出来问一句。

对应任务书 S4：
① 先"自己红烧肉"后"朋友家他们做饭" → 提示矛盾并确认；
② 先回广州后到上海 → 仍提示（地点那条线照旧）；
③ 不相关的两件事不误报矛盾。
"""

from __future__ import annotations

import ast
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.dialog import location, plan
from life.dialog.location import TravelConflict
from life.dialog.plan import MealPlan, PlanConflict, PlanTracker, conflict_hint, extract_meal_plan
from life.dialog.router import INTENT_CHAT, DialogRoute

ROOT = Path(__file__).resolve().parents[1]

# 任务书里的那对矛盾说法
FIRST = "我自己做红烧肉"      # 谁做饭 = 自己
SECOND = "去朋友家，他们做饭"  # 谁做饭 = 别人，在哪吃 = 朋友家
HOME_EAT = "我在家吃饭"       # 在哪吃 = 在家
OUT_EAT = "我出去吃饭"        # 在哪吃 = 出去
AWAY_EAT = "今晚到朋友家聚餐"  # 只说了在哪吃，没说谁做（v2-contradict-detect 补强的跨槽位用例）
UNRELATED = "他们做好了项目"   # 不相关（没有吃/饭这茬）


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _sys_text(messages: list[dict[str, str]]) -> str:
    return "\n".join(m["content"] for m in messages if m.get("role") == "system")


def _last_request(stub) -> list[dict[str, str]]:
    return stub.messages[-1]


def table_rows(db_path, table: str) -> int:
    conn = sqlite3.connect(str(db_path))
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    try:
        svc = app.psych_service
        chatter = app.chatter_service

        # ---------- ① 词表：抽得准，不相关的不抽 ----------

        section("① 词表抽「谁做饭 / 在哪吃」")

        got_first = extract_meal_plan(FIRST)
        check("「我自己做红烧肉」→ 谁做饭=自己，地点没提",
              got_first == MealPlan(cooker="自己做饭", place=""), str(got_first))
        got_second = extract_meal_plan(SECOND)
        check("「去朋友家，他们做饭」→ 谁做饭=别人、在哪吃=朋友家",
              got_second == MealPlan(cooker="别人做饭", place="去朋友家吃"), str(got_second))
        check("「我在家吃饭」→ 在哪吃=在家",
              extract_meal_plan(HOME_EAT) == MealPlan(cooker="", place="在家吃"),
              str(extract_meal_plan(HOME_EAT)))
        check("「我出去吃饭」→ 在哪吃=出去",
              extract_meal_plan(OUT_EAT) == MealPlan(cooker="", place="出去吃"),
              str(extract_meal_plan(OUT_EAT)))
        check("「他们做好了项目」→ 不相关，啥也不抽（没吃/饭这茬）",
              extract_meal_plan(UNRELATED) == MealPlan(), str(extract_meal_plan(UNRELATED)))
        check("「我做一个方案」→ 「做」但不是做饭，不抽",
              extract_meal_plan("我做一个方案") == MealPlan(), "")
        check("「我今天心情不错」→ 没提吃，不抽",
              extract_meal_plan("我今天心情不错") == MealPlan(), "")

        # ---------- ② PlanTracker：跨轮比对 ----------

        section("② 跨轮比对：报矛盾 / 不误报")

        t = PlanTracker()
        check("第一次只记下，不报冲突", t.note(FIRST) is None and t.last.cooker == "自己做饭",
              str(t.last))
        check("后说「他们做饭」→ 报出做饭矛盾（自己 → 别人）",
              t.note(SECOND) == PlanConflict(slot="做饭", previous="自己做饭", current="别人做饭"),
              str(t.note(SECOND)))

        t2 = PlanTracker()
        check("地点矛盾：先说在家、后说出去 → 报",
              t2.note(HOME_EAT) is None, "第一次记下")
        check("地点矛盾报得对（在家吃 → 出去吃）",
              t2.note(OUT_EAT) == PlanConflict(slot="吃饭地点", previous="在家吃", current="出去吃"),
              str(t2.note(OUT_EAT)))

        t3 = PlanTracker()
        check("同槽同值连着说两遍不算矛盾",
              t3.note(FIRST) is None and t3.note(FIRST) is None, "")

        t4 = PlanTracker()
        check("只提一槽不误报：先说做饭、后只提地点（不重提做饭）→ 不报做饭矛盾",
              t4.note(FIRST) is None and t4.note(HOME_EAT) is None, str(t4.last))

        t5 = PlanTracker()
        check("不相关的两件事不误报：自己做饭 → 他们做好了项目 → 不报",
              t5.note(FIRST) is None and t5.note(UNRELATED) is None, str(t5.last))

        t6 = PlanTracker()
        t6.note(FIRST)
        t6.reset()
        check("换一场对话（reset）后不拿旧安排比",
              t6.note(SECOND) is None and t6.last.cooker == "别人做饭", str(t6.last))

        # ---------- ②b 跨槽位：只说了一桩，另一桩被蕴含出来 ----------
        # 上面那两条同槽比对要求"同一个槽位前后都明说过"。任务书里那对说法
        # （「亲自做红烧肉」→「到朋友家聚餐」）两边各只说了一桩，抓不到；
        # 补的是"自己做饭 ⇒ 在家吃"这类最硬的蕴含。

        section("②b 跨槽位蕴含（v2-contradict-detect 补强）")

        t7 = PlanTracker()
        t7.note(FIRST)
        got7 = t7.note(AWAY_EAT)
        check("「自己做饭」→「到朋友家聚餐」报出吃饭地点矛盾（前句只说了谁做）",
              got7 == PlanConflict(slot="吃饭地点", previous="在家吃", current="去朋友家吃"),
              str(got7))

        t8 = PlanTracker()
        t8.note(AWAY_EAT)
        got8 = t8.note(FIRST)
        check("反向：「到朋友家聚餐」→「自己做」报出做饭矛盾",
              got8 == PlanConflict(slot="做饭", previous="别人做饭", current="自己做饭"),
              str(got8))

        t9 = PlanTracker()
        t9.note(FIRST)
        got9 = t9.note(HOME_EAT)
        check("跨槽位不误报：自己做饭 → 在家吃饭（蕴含一致，不是矛盾）",
              got9 is None, str(got9))

        t10 = PlanTracker()
        t10.note(HOME_EAT)
        got10 = t10.note("他们做饭")
        check("跨槽位不误报：在家吃饭 → 他们做饭（不推「在家 ⇒ 自己做」）",
              got10 is None, str(got10))

        t11 = PlanTracker()
        t11.note(FIRST)
        check("跨槽位不误报：不相关的那句照样不报",
              t11.note(UNRELATED) is None, str(t11.last))

        # ---------- ③ 统一出口：地点矛盾与安排矛盾都走 conflict_hint ----------

        section("③ conflict_hint 统一出口")

        hint_travel = conflict_hint(TravelConflict(previous="广州", current="上海"))
        check("地点矛盾 → 提示里带前后对不上 + 两座城",
              "前后对不上" in hint_travel and "广州" in hint_travel and "上海" in hint_travel,
              hint_travel)
        hint_plan = conflict_hint(PlanConflict(slot="做饭", previous="自己做饭", current="别人做饭"))
        check("安排矛盾 → 提示里带前后对不上 + 做饭 + 两种说法",
              "前后对不上" in hint_plan and "做饭" in hint_plan
              and "自己做饭" in hint_plan and "别人做饭" in hint_plan, hint_plan)
        check("没冲突时给空串", conflict_hint(None) == "", repr(conflict_hint(None)))

        # ---------- ④ 回话注入：有冲突就摆出来，没冲突不多嘴 ----------

        section("④ 回话把矛盾摆出来（注入系统提示）")

        stub.messages.clear()
        svc._compose(SECOND, 0.0, DialogRoute(INTENT_CHAT, "平静"),
                     PlanConflict(slot="做饭", previous="自己做饭", current="别人做饭"))
        conflict_sys = _sys_text(_last_request(stub))
        check("提示词里摆出了做饭矛盾，并要求用他自己的话问一句",
              "前后对不上" in conflict_sys and "做饭" in conflict_sys
              and "自己做饭" in conflict_sys and "别人做饭" in conflict_sys, "")

        stub.messages.clear()
        svc._compose(SECOND, 0.0, DialogRoute(INTENT_CHAT, "平静"), None)
        check("没冲突时就不注入这一段（不无中生有）",
              "前后对不上" not in _sys_text(_last_request(stub)), "")

        # ---------- ⑤ 真链：ChatterService 两轮，主动把矛盾摆出来 ----------

        section("⑤ 真链：先自己做饭，后说朋友家他们做饭 → 主动提示")

        chatter._locator.reset()
        chatter._plan_tracker.reset()
        said: list[str] = []
        app.bus.pet_speak.connect(said.append)
        try:
            said.clear()
            chatter.handle(FIRST)
            wait_for(lambda: bool(said), timeout=8.0)
            drain_threads()
            pump(0.2)
            check("第一句记下安排（谁做饭=自己），没急着提示",
                  chatter._plan_tracker.last.cooker == "自己做饭",
                  str(chatter._plan_tracker.last))

            said.clear()
            chatter.handle(SECOND)
            wait_for(lambda: bool(said), timeout=10.0)
            drain_threads()
            pump(0.2)
            check("第二句之后安排已更新（谁做饭=别人、在哪吃=朋友家）",
                  chatter._plan_tracker.last == MealPlan(cooker="别人做饭", place="去朋友家吃"),
                  str(chatter._plan_tracker.last))
            second_sys = _sys_text(_last_request(stub))
            check("第二次回话的系统提示里摆出了做饭矛盾",
                  "前后对不上" in second_sys and "做饭" in second_sys, "")
        finally:
            app.bus.pet_speak.disconnect(said.append)

        # ---------- 红线 ----------

        section("红线：零新依赖 / pet.db 零写入 / 地点线没碰坏")

        owner_pkgs = {"brain", "core", "life", "memory", "monitor", "pet", "ui"}
        tree = ast.parse(_src("life/dialog/plan.py"), filename="life/dialog/plan.py")
        imported: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imported.add(node.module.split(".")[0])
        third_party = sorted(
            mod for mod in imported
            if mod not in sys.stdlib_module_names and mod not in owner_pkgs
            and mod != "__future__"
        )
        check("plan.py 一个第三方包都没引（零新依赖）", third_party == [],
              str(third_party) or "（只用标准库 + 仓内模块）")

        before = table_rows(app.config.db_path, "utterance")
        stub.messages.clear()
        svc._compose(SECOND, 0.0, DialogRoute(INTENT_CHAT, "平静"),
                     PlanConflict(slot="做饭", previous="自己做饭", current="别人做饭"))
        drain_threads()
        check("矛盾检测纯内存、不写 pet.db（直接调 _compose 不落库）",
              table_rows(app.config.db_path, "utterance") == before,
              f"{before} → {table_rows(app.config.db_path, 'utterance')}")

        _loc = location.LocationTracker()
        check("地点追踪那条线照旧（location.LocationTracker 还在、还能报）",
              _loc.note("我先回广州") is None
              and _loc.note("我到上海了")
              == TravelConflict(previous="广州", current="上海"), "")

    finally:
        drain_threads()
        box.cleanup()

    return summary("对话矛盾检测补强（同事件行为主体/内容冲突，跨轮比对）（v2-contradict-detect）")


if __name__ == "__main__":
    raise SystemExit(main())
