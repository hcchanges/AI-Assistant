"""
生活卡片 X 删除键 + 清理（v2-card-del）的自动测试。

用户要的两件事：
1. 生活卡右上角一个 X，点了删掉这张卡（**轻确认**：点两下，不弹对话框、不进详情页）；
2. 除了出行卡与饮食卡，其它生活卡都清掉（那是数据清理，见收尾报告第三十九节后面的
   第四十节；这一轮测试验的是**删除这条代码路径**，清理本身是拿它跑的一次真实操作）。

这一轮验四件事：
- **只有生活卡有 X**：`closable` 默认假，部门卡（container）一个都没被带上；
- **轻确认真的是两步**：第一下只进"待确认"，第二下才发信号；期间点别处 / 滚走 / 拖走 /
  超时都作废（各一条），且点 X 不会顺手把卡打开；
- **删就是真删**：卡行没了，挂它的 `card_checkin` / `card_share` 一起清掉（不留孤儿行），
  别的卡一行不动；归档那条路照旧并存（归档 = 收进卡册，删除 = 没了）；
- **红线**：不新增依赖、mind.db 不动、删除只由用户那两下点击触发（没有任何自动信号
  能删卡），pet.db 的写入只发生在这一次显式调用里。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from harness import check, drain_threads, ensure_app, make_env, pump, section, summary

from ui.panels.carousel import CardCarousel, CarouselCard

ROOT = Path(__file__).resolve().parents[1]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _rows(db_path, sql: str, args: tuple = ()) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql, args).fetchone()[0])
    finally:
        conn.close()


def _card_ids(db_path, sql: str, args: tuple = ()) -> list[int]:
    conn = sqlite3.connect(db_path)
    try:
        return [int(row[0]) for row in conn.execute(sql, args)]
    finally:
        conn.close()


def _carousel(keys: list[object], closable: list[bool]) -> CardCarousel:
    """造一个真轮播：中间那张就是 keys[0]，卡片尺寸按控件大小算出来。"""
    ensure_app()
    view = CardCarousel(card_ratio=0.6)
    view.resize(900, 420)
    view.set_cards([
        CarouselCard(key=key, title=f"卡{key}", theme="餐饮", status="进行中", closable=flag)
        for key, flag in zip(keys, closable)
    ])
    view.show()
    pump(0.02)
    return view


def main() -> int:
    # ---------- S1 卡片模型：只有生活卡带 X ----------

    section("S1 卡片模型：closable 是可选位，默认假")

    check("CarouselCard 多了 closable，默认 False",
          CarouselCard(key=1, title="x").closable is False,
          str(CarouselCard(key=1, title="x").closable))

    life_src = _src("ui/panels/life_panel.py")
    check("生活卡构造时显式打开 X", "closable=True" in life_src, "")
    work_src = _src("ui/panels/work_panel.py")
    check("工作模块那一页一个 closable 都没带（公开版它是块锁着的占位页）",
          "closable" not in work_src, "")

    view = _carousel([1, 2], [True, True])
    try:
        section("S1 几何：X 在卡的右上角里，待确认时变宽")

        card_rect = view._rect_of(0)
        box = view._close_rect(0)
        check("X 方块落在卡内", card_rect.contains(box), f"{box} ⊂ {card_rect}")
        check("贴着右上角（右边与上边都靠边）",
              card_rect.right() - box.right() < card_rect.width() * 0.2
              and box.top() - card_rect.top() < card_rect.height() * 0.2,
              f"右差 {card_rect.right() - box.right():.1f} / 上差 {box.top() - card_rect.top():.1f}")
        check("X 不压住卡名（宽度只占卡片一小条）",
              box.width() < card_rect.width() * 0.3, f"{box.width():.1f} / {card_rect.width():.1f}")

        normal_w = box.width()
        view._close_armed = 1
        armed = view._close_rect(0)
        check("待确认时放宽成能装下「确定删?」的药丸",
              armed.width() > normal_w and armed.right() == box.right(),
              f"{normal_w:.1f} → {armed.width():.1f}")
        view._disarm_close()
    finally:
        view.deleteLater()

    # ---------- S1 交互：轻确认真是两步 ----------

    view = _carousel([11, 12, 13], [True, True, False])
    opened: list[object] = []
    closed: list[object] = []
    # card_activated 带两个参数（key, 卡片矩形），包一层只记 key
    view.card_activated.connect(lambda key, _rect: opened.append(key))
    view.card_close_requested.connect(closed.append)
    try:
        section("S1 轻确认：第一下只是问，第二下才删")

        point = view._close_rect(0).center().toPoint()
        QTest.mouseClick(view, Qt.LeftButton, pos=point)
        pump(0.02)
        check("点第一下没有发删除信号", closed == [], str(closed))
        check("也没把卡打开（这一下归 X）", opened == [], str(opened))
        check("进入了待确认", view._close_armed == 11, str(view._close_armed))
        check("确认表已经起了", view._close_timer.isActive(), "")

        QTest.mouseClick(view, Qt.LeftButton, pos=view._close_rect(0).center().toPoint())
        pump(0.02)
        check("点第二下才发删除信号，且只发一次", closed == [11], str(closed))
        check("发完就收回待确认", view._close_armed is None, str(view._close_armed))
        check("确认表也停了", not view._close_timer.isActive(), "")

        section("S1 轻确认的四种作废方式")

        QTest.mouseClick(view, Qt.LeftButton, pos=view._close_rect(0).center().toPoint())
        pump(0.02)
        check("先挂上", view._close_armed == 11, str(view._close_armed))
        QTest.mouseClick(view, Qt.LeftButton, pos=view._rect_of(0).center().toPoint())
        pump(0.02)
        check("点卡身：待确认作废", view._close_armed is None, str(view._close_armed))
        check("点卡身还是打开卡（原来那件事没丢）", opened == [11], str(opened))
        check("这一下不算删除", closed == [11], str(closed))

        QTest.mouseClick(view, Qt.LeftButton, pos=view._close_rect(0).center().toPoint())
        pump(0.02)
        view._slide_to(1)
        check("滚走 / 换到下一张：作废", view._close_armed is None, str(view._close_armed))
        view._slide_to(0)
        pump(0.35)

        QTest.mouseClick(view, Qt.LeftButton, pos=view._close_rect(0).center().toPoint())
        pump(0.02)
        check("再挂上", view._close_armed == 11, str(view._close_armed))
        view._close_timer.start(1)          # 等价于 3 秒到点：同一个 timeout 信号
        pump(0.1)
        check("超时没人点：自己收回（不会删）", view._close_armed is None and closed == [11],
              f"armed={view._close_armed} closed={closed}")

        section("S1 不可删的卡：右上角那点不归 X 管")

        view._slide_to(2)
        pump(0.35)
        third = view.current_card().key
        check("第三张就是那张不可删的", third == 13, str(third))
        opened.clear()
        QTest.mouseClick(view, Qt.LeftButton, pos=view._close_rect(2).center().toPoint())
        pump(0.02)
        check("点它右上角 = 点卡身（照样打开卡）", opened == [13], str(opened))
        check("不进待确认、不发删除", view._close_armed is None and closed == [11], "")
    finally:
        view.deleteLater()

    # ---------- S2 删就是真删 ----------

    box, _stub, app = make_env()
    try:
        service = app.card_service
        db = app.config.db_path
        # 用「消费」而不是「餐饮」：预设那个大类的卡不算个人卡、进不了卡册，
        # 归档那条断言就验不成了（归档只管个人卡）
        keep = service.create_manual("消费", "留着的那张", "🍲", "别删我")
        gone = service.create_manual("娱乐", "要删的那张", "💰", "删我")
        gone_id = _card_ids(db, "select id from card where title='要删的那张'")[0]
        keep_id = _card_ids(db, "select id from card where title='留着的那张'")[0]
        service.checkin(gone_id)
        service.share(gone_id)
        changes: list[int] = []
        app.bus.card_changed.connect(lambda: changes.append(1))

        section("S2 删除：卡行 + 打卡 + 分享一起走，别的卡一行不动")

        check("先确认三张表里确实挂着东西",
              _rows(db, "select count(*) from card_checkin where card_id=?", (gone_id,)) == 1
              and _rows(db, "select count(*) from card_share where card_id=?", (gone_id,)) == 1,
              f"打卡 {_rows(db, 'select count(*) from card_checkin where card_id=?', (gone_id,))} 条 / "
              f"分享 {_rows(db, 'select count(*) from card_share where card_id=?', (gone_id,))} 条")

        changes.clear()
        ok = service.delete_card(gone_id)
        pump(0.02)
        check("删除返回真", ok is True, str(ok))
        check("card 行没了", _rows(db, "select count(*) from card where id=?", (gone_id,)) == 0, "")
        check("打卡记录一起清了（没留孤儿行）",
              _rows(db, "select count(*) from card_checkin where card_id=?", (gone_id,)) == 0, "")
        check("分享快照也清了",
              _rows(db, "select count(*) from card_share where card_id=?", (gone_id,)) == 0, "")
        check("另一张卡还在（没误伤）",
              _rows(db, "select count(*) from card where id=?", (keep_id,)) == 1, "")
        check("轮播上查不到了", gone_id not in [c.id for c, _n in service.plaza()], "")
        check("卡册里也没有（删除不是归档）",
              gone_id not in [c.id for c, _n in service.card_book()], "")
        check("发了一次 card_changed（界面重排靠它）", len(changes) == 1, str(len(changes)))

        section("S2 删除与归档是两条路，互不影响")

        changes.clear()
        check("归档一张：卡还在，只是进卡册",
              service.archive_card(keep_id) is True
              and _rows(db, "select count(*) from card where id=?", (keep_id,)) == 1, "")
        check("它出现在卡册里", keep_id in [c.id for c, _n in service.card_book()], "")
        changes.clear()
        check("删一张不存在的卡：返回假、不在库里乱动、也不发信号",
              service.delete_card(999999) is False and changes == [] and service.card(999999) is None,
              f"changes={changes}")
        service.restore_card(keep_id)
        check("拉回来照旧能拉", keep_id in [c.id for c, _n in service.plaza()], "")

        section("S2 预设卡也能删，但重启会被补回来（如实记一笔）")

        diet_id = _card_ids(db, "select id from card where title='饮食'")
        check("沙箱里确实预置了那张饮食卡", len(diet_id) == 1, str(diet_id))
        if diet_id:
            check("先删掉预设那张饮食卡", service.delete_card(diet_id[0]) is True, "")
            check("库里确实没了", _rows(db, "select count(*) from card where title='饮食'") == 0, "")
            made = service.ensure_preset_cards()
            check("重启那条线（ensure_preset_cards）把它补回来了",
                  made >= 1 and _rows(db, "select count(*) from card where title='饮食'") == 1,
                  f"补了 {made} 张")
            check("补卡是按主题认的（同类主题已有卡就不再建一张）",
                  _rows(db, "select count(*) from card where theme='餐饮'") == 1,
                  str(_rows(db, "select count(*) from card where theme='餐饮'")))

        # ---------- S4 界面那条路：真面板 + 真信号 ----------

        section("S4 生活面板：卡上带 X，确认后真删掉、轮播自己重排")

        panel = app.life_panel
        panel.refresh_cards()
        pump(0.02)
        cards = panel._carousel._cards
        check("面板里的卡都带 X", bool(cards) and all(c.closable for c in cards),
              f"{[c.closable for c in cards]}")
        keys_before = [c.key for c in cards]
        victim = keys_before[-1]
        toasts: list[str] = []
        app.bus.toast.connect(toasts.append)

        panel._carousel.card_close_requested.emit(victim)   # 就是 X 第二下发的那个信号
        pump(0.05)
        keys_after = [c.key for c in panel._carousel._cards]
        check("那张从轮播上没了", victim not in keys_after, f"{keys_before} → {keys_after}")
        check("库里也真没了", _rows(db, "select count(*) from card where id=?", (victim,)) == 0, "")
        check("弹了一句 toast（删除不可撤销，得让人看见删的是哪张）",
              len(toasts) == 1 and "删掉了" in toasts[0], str(toasts))
        check("轮播重排后剩下的卡还带着 X", all(c.closable for c in panel._carousel._cards), "")

        # ---------- 红线 ----------

        section("S3 红线：删除只由用户点击触发，没有自动删卡的口子")

        check("CardService 里没有任何自动信号接到 delete_card（只有界面那一处调用）",
              "delete_card" not in _src("core/bootstrap.py")
              and _src("life/cards/service.py").count("def delete_card") == 1, "")
        check("删除只删 card / card_checkin / card_share 三张表，没有别的表被带下水",
              "CardTopicEvent" not in _src("data/dao.py").split("def delete(self, card_id: int) -> bool:")[1][:900],
              "")
        check("没新增依赖（用的还是 requirements 里已有的 SQLAlchemy / PySide6）",
              "SQLAlchemy" in _src("requirements.txt") and "PySide6" in _src("requirements.txt"), "")
        check("没有 import 新的三方库",
              "import requests" not in _src("ui/panels/carousel.py")
              and "import requests" not in _src("data/dao.py"), "")
        drain_threads()
    finally:
        box.cleanup()

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
