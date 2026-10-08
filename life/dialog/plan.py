"""计划/安排槽位追踪与前后矛盾（v2-contradict-detect）。

location.py 那条线只比「人现在在哪座城」（TravelConflict）。这一轮补的是它漏掉的一类：
**同一件事**（比如"今晚这顿饭"）的行为主体 / 内容，跨轮说法对不上——先说自己做红烧肉，
过几轮又说到朋友家聚餐、让他们做。这两句谁也没换城市，location 那条线抓不到，宠物就
静默地接了下去。

这里补上：记住上一句里**明确报出来的**两个槽位——「谁做饭」「在哪吃」，这一句再报
且跟上一句不一样时，产出一条 `PlanConflict`。只认词表里明写的说法，不猜、不编。
纯内存、不写库，跟"一场对话"同寿（进程退出就没了）。

v2-contradict-detect 补强：上面那条同槽比对要求"同一个槽位前后都明说过"，抓不到
**跨槽位**的那一类——「我今晚亲自做红烧肉」→「今晚到朋友家聚餐」：前一句只说谁做，
后一句只说在哪吃，两边各自都凑不出"前后都明说"。但**自己做饭本身就在家吃**，所以
这两句其实是对"在哪吃"说了两遍，只是前一遍是隐含的。补了两条最硬的蕴含（见
`_implied_place` / `_implied_cooker`），只推这两条，不引申。

`conflict_hint()` 是地点矛盾与安排矛盾的统一出口，回话那一头只认它。
"""

from __future__ import annotations

from dataclasses import dataclass

from life.dialog.location import TravelConflict, conflict_hint as _travel_conflict_hint


@dataclass(frozen=True)
class PlanConflict:
    """同一件事前后说法对不上（谁做饭 / 在哪吃）。"""

    slot: str       # 对不上的槽位："做饭" / "吃饭地点"
    previous: str   # 前面说成啥（如"自己做饭" / "在家吃"）
    current: str    # 这一句说成啥


@dataclass(frozen=True)
class MealPlan:
    """这一句里明确报出来的晚饭安排；认不出就留空串。"""

    cooker: str = ""   # "自己做饭" / "别人做饭"
    place: str = ""    # "在家吃" / "去朋友家吃" / "出去吃"


# 先得有"吃 / 饭"这一茬，才谈得上谁做、在哪吃。认不出这句在说吃的，就一个槽位都不抽。
_FOOD_ANCHOR = ("饭", "菜", "吃", "下厨", "聚餐", "红烧肉")

# 谁做饭——自己这头的说法
_SELF_COOK = (
    "自己做饭", "自己烧饭", "自己煮饭", "自己做菜", "自己烧菜", "自己煮菜",
    "亲自做饭", "亲自下厨", "亲自做", "亲自烧",
    "我做饭", "我做菜", "我下厨", "我烧饭", "我烧菜", "我煮饭", "我煮菜",
    "我来做", "我来烧", "我来煮", "我亲自做", "我亲自下厨",
    "自己做", "自己烧", "自己煮", "自己下厨", "我做", "我烧", "我煮",
)
# 谁做饭——别人这头的说法
_OTHER_COOK = (
    "他们做好饭", "他们来做饭", "他们来烧", "他们做饭", "他们做菜", "他们下厨",
    "他们烧饭", "他们烧菜", "他们煮饭", "他们煮菜", "他们做", "他们烧", "他们煮",
    "她们做饭", "她们做菜", "她们做",
    "别人做饭", "别人做菜", "别人做",
    "朋友做饭", "朋友做菜", "朋友做", "朋友来做饭",
    "他做饭", "他做菜", "他下厨", "他做", "他烧", "他煮",
    "她做饭", "她做菜", "她做",
    "让朋友做", "请朋友做", "请他们做", "让他们做",
)
# 在哪吃——在家这头
_AT_HOME = ("在家吃", "在家里吃", "回家吃", "家里吃", "在家", "家里", "自己家")
# 在哪吃——出去 / 别人那这头
_AWAY = (
    "去朋友家", "到朋友家", "朋友家", "去他们家", "到他们家", "他们家",
    "出去聚餐", "出去吃", "外面吃", "下馆子", "出去", "外面",
)


def extract_meal_plan(text: str) -> MealPlan:
    """抽这一句里明说的晚饭安排。认不出就是两个空串。"""
    t = text or ""
    if not any(a in t for a in _FOOD_ANCHOR):
        return MealPlan()

    cooker = ""
    for w in _SELF_COOK:
        if w in t:
            cooker = "自己做饭"
            break
    if not cooker:
        for w in _OTHER_COOK:
            if w in t:
                cooker = "别人做饭"
                break

    place = ""
    for w in _AT_HOME:
        if w in t:
            place = "在家吃"
            break
    if not place:
        for w in _AWAY:
            if w in t:
                # 朋友家 / 他们家 → 去朋友家吃；出去 / 外面 / 下馆子 / 聚餐 → 出去吃
                place = (
                    "出去吃"
                    if any(k in w for k in ("出去", "外面", "下馆子", "聚餐"))
                    else "去朋友家吃"
                )
                break

    return MealPlan(cooker=cooker, place=place)


# ---------- 跨槽位的隐含冲突（v2-contradict-detect 补强） ----------
#
# 上面那两条同槽比对要求"同一个槽位前后都明说过"。「我今晚亲自做红烧肉」→「今晚到
# 朋友家聚餐」这一对抓不到：前一句只说谁做（place 空），后一句只说在哪吃（cooker 空），
# 两个槽位各自都凑不出"前后都明说"。但**做饭那一桩本身已经把在哪吃钉死了**——
# 自己做饭就是在自己家吃。所以这两句其实是对同一个槽位（在哪吃）说了两遍，只是前一遍
# 是隐含的。
#
# 只补这两条最硬的蕴含，不引申：
#   自己做饭 ⇒ 在家吃；      去别人那儿 / 出去吃 ⇒ 别人做饭。
# 反过来不推：在家吃未必是自己做（可能叫外卖、热剩饭），别人做饭也未必不在家
# （朋友来家里做）。推了这两条会造出"我在家吃饭 → 他们做饭"这种假矛盾。
def _implied_place(plan: MealPlan) -> str:
    """这句里明说的吃饭地点；没说但做饭那一桩已经钉死了，就用钉死的那个。"""
    if plan.place:
        return plan.place
    if plan.cooker == "自己做饭":
        return "在家吃"
    return ""


def _implied_cooker(plan: MealPlan) -> str:
    """这句里明说的做饭人；没说但"去别人那儿 / 出去吃"已经蕴含不是自己做了。

    反向那条（在家吃 ⇒ 自己做饭）**不推**——在家吃完全可能是叫外卖、热剩饭，
    推了会造出"我在家吃饭 → 他们做饭"这种假矛盾。
    """
    if plan.cooker:
        return plan.cooker
    if plan.place and plan.place != "在家吃":
        return "别人做饭"
    return ""


class PlanTracker:
    """记住上一句里明确报出来的晚饭安排，这一句报得不一样就报冲突。

    用法：每句用户的话都过一遍 `note()`；返回非 None 就是这一句跟前面说的对不上。
    两个槽位独立比：只有「做饭」这一桩对不上时，不牵连「在哪吃」。
    """

    def __init__(self) -> None:
        self._last = MealPlan()

    @property
    def last(self) -> MealPlan:
        """上一次记下的安排；还没说过就是两个空串。"""
        return self._last

    def note(self, text: str) -> PlanConflict | None:
        current = extract_meal_plan(text)
        previous = self._last
        # 这一句没提到的槽位沿用旧值，好让下一轮还能比。
        self._last = MealPlan(
            cooker=current.cooker or previous.cooker,
            place=current.place or previous.place,
        )
        # 只有"前后都把同一个槽位明说过、又不一样"，才算矛盾。
        if current.cooker and previous.cooker and current.cooker != previous.cooker:
            return PlanConflict(slot="做饭", previous=previous.cooker, current=current.cooker)
        if current.place and previous.place and current.place != previous.place:
            return PlanConflict(
                slot="吃饭地点", previous=previous.place, current=current.place
            )
        # 跨槽位（v2-contradict-detect 补强）：前一句只说了另一桩，但那一桩已经把这一桩
        # 钉死了（自己做饭 ⇒ 在家吃）。仅当前后**没有**同一个槽位都明说过时才会走到这里，
        # 所以上面两条同槽比对仍然优先、判词不变。
        prev_place = _implied_place(previous)
        if current.place and prev_place and current.place != prev_place:
            return PlanConflict(slot="吃饭地点", previous=prev_place, current=current.place)
        prev_cooker = _implied_cooker(previous)
        if current.cooker and prev_cooker and current.cooker != prev_cooker:
            return PlanConflict(slot="做饭", previous=prev_cooker, current=current.cooker)
        return None

    def reset(self) -> None:
        """忘掉上一句的安排（换了一场对话时用）。"""
        self._last = MealPlan()


def plan_conflict_hint(conflict: PlanConflict) -> str:
    """把安排上的前后矛盾拼成提示词，让宠物先问一句。"""
    return (
        "【前后对不上，先问清楚】关于「" f"{conflict.slot}" "」，他前面说的是「"
        f"{conflict.previous}」，这一句又说成「{conflict.current}」。"
        "**先把这个不一致摆出来问他一句**（用你自己的话说，别照抄这一行），"
        "确认到底是改了主意、还是顺口说岔了；不要当没看见接着聊。"
    )


def conflict_hint(conflict) -> str:
    """地点矛盾与安排矛盾的统一出口；认不出的类型给空串（当没冲突）。"""
    if isinstance(conflict, TravelConflict):
        return _travel_conflict_hint(conflict)
    if isinstance(conflict, PlanConflict):
        return plan_conflict_hint(conflict)
    return ""
