"""
移除星念卡（v2-life-card-star）的自动测试。

产品决策：用户真机反馈"不要设置星念卡"——星念卡功能整个砍掉：
- 设置页"记忆管理"不再有星念这一块（不配置、不初始化星念 UI）；
- 数据层不再立卡、不再加载（提炼回调不 observe，主动搭话不再拿星念开场）；
- 生活卡保持只有 出行 + 饮食 两张，无新增 / 残留卡。

这一轮验四件事（对应 S4 清单）：
① 设置页无星念卡项；
② 生活卡仅出行 + 饮食；
③ 记忆簿 / 亲密度（星念以外的两块）不受影响，照旧显示；
④ 删除同步（l4）保持现状——本轮没碰它，留待用户补测。

红线：零写入（只删星念接线，不新增任何落盘口子）、零新依赖（纯移除）。
"""

from __future__ import annotations

import inspect
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

import ui.settings as settings_mod
from life.cards.templates import LIFE_TEMPLATES, PRESET_THEMES

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def main() -> int:
    box, _stub, app = make_env()
    try:
        # ---------- ① 设置页无星念卡项 ----------

        section("① 设置页：星念卡项已移除")

        sig = inspect.signature(settings_mod.SettingsDialog.__init__)
        check("SettingsDialog 不再收 star_cards 参数",
              "star_cards" not in sig.parameters, str(list(sig.parameters)))

        dialog = settings_mod.SettingsDialog(
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
        pump(0.05)
        check("没有 _star_list 这个控件（星念列表不建）",
              not hasattr(dialog, "_star_list"), "")
        check("没有 _star_hint 这个控件（星念说明不建）",
              not hasattr(dialog, "_star_hint"), "")
        settings_src = _src("ui/settings.py")
        check("设置页源码不再创建星念列表控件（_star_list 已删）",
              "_star_list" not in settings_src, "")
        check("设置页源码不再创建星念说明控件（_star_hint 已删）",
              "_star_hint" not in settings_src, "")
        dialog.close()
        dialog.deleteLater()

        # ---------- ② 生活卡仅出行 + 饮食 ----------

        section("② 生活卡：仅 出行 + 饮食 两张")

        titles = [t.title for t in LIFE_TEMPLATES]
        check("预设生活卡只剩两张（饮食 / 出行）", titles == ["饮食", "出行"], str(titles))
        check("预设主题就是 餐饮 / 出行", PRESET_THEMES == ("餐饮", "出行"), str(PRESET_THEMES))
        check("预设卡列表里没有任何「星念」主题", all("星念" not in t for t in titles), str(titles))

        # ---------- ③ 记忆簿 / 亲密度不受影响 ----------

        section("③ 记忆簿 / 亲密度（星念以外的两块）照旧")

        check("亲密度服务还在（没被星念连带删掉）", app.mind_intimacy is not None, "")
        check("记忆簿服务还在（没被星念连带删掉）", app.mind_memory_book is not None, "")

        dialog2 = settings_mod.SettingsDialog(
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
        pump(0.05)
        check("亲密度阶段条照旧建出来", hasattr(dialog2, "_stage_bar"), "")
        check("记忆簿列表照旧建出来", hasattr(dialog2, "_book_list"), "")
        dialog2.close()
        dialog2.deleteLater()

        # ---------- ④ 删除同步（l4）保持现状 ----------

        section("④ 删除同步（l4）：本轮没碰，保持现状")

        card_src = _src("life/cards/service.py")
        check("delete_card 还在（删除同步入口没动）", "def delete_card" in card_src, "")
        check("删卡仍走 DAO 的 delete（卡 + 打卡 / 分享一起清）",
              "self._card_dao.delete(card_id)" in card_src, "")
        check("删卡仍广播 card_changed 让轮播重排",
              "self._bus.card_changed.emit()" in card_src, "")

        # ---------- 红线 ----------

        section("红线：零写入接线、只删不增")

        boot_src = _src("core/bootstrap.py")
        check("装配层不再构造 StarCardService", "StarCardService" not in boot_src, "")
        check("装配层不再持有 self.mind_star_cards 属性",
              "self.mind_star_cards" not in boot_src, "")
        check("提炼回调不再 observe 立卡", "observe(result)" not in boot_src, "")

        proactive_src = _src("life/mind/proactive.py")
        check("主动搭话不再拿星念开场（_milestone 已删）", "def _milestone" not in proactive_src, "")
        check("主动搭话服务不再持有 _star_cards 属性", "_star_cards" not in proactive_src, "")

        # 只砍接线、不删底层：star_cards 文件与表还在（数据兼容，老数据不丢）
        check("star_cards.py 文件还在（底层没删，只是不接线）",
              (ROOT / "life/mind/star_cards.py").exists(), "")
        store_src = _src("life/mind/store.py")
        check("star_cards 表结构还在（不删表，老数据还在）",
              "CREATE TABLE IF NOT EXISTS star_cards" in store_src, "")

        drain_threads()
    finally:
        drain_threads()
        box.cleanup()

    return summary("移除星念卡（v2-life-card-star）")


if __name__ == "__main__":
    raise SystemExit(main())
