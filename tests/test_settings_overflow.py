"""
设置页弹窗溢出修复（v2-settings-overflow）的自动测试。

用户真机测出来的毛病：设置窗里"记忆管理"那一页（亲密度 / 记忆簿 / 记忆 / 提醒
四大块摞在一起）比窗口高，多出来的部分被直接切掉，而且窗口里没有滚动条，底部够不着。
（星念块在 v2-life-card-star 砍掉，这里不再造星念数据。）

这一轮验四件事：
- **每页都套了滚动区**：八页一个不落，且滚动区不吃横向滚动条；
- **窗口不再被内容顶出去**：对话框的最小高度从 725px 掉到很小，`fit_dialog_on_screen`
  把超高的窗口压到屏幕可用区的九成并居中，真 show 出来底边不越过屏幕底边；
- **记忆管理页真能滚到底**：造真数据把页面撑长，滚到最下面那两个按钮都落在视口里，
  并且滚到底之后**真点**一下"忘掉选中的这条"，那条记忆在库里真没了；
- **红线**：不动数据（pet.db 的 utterance 一行不增、mind.db 行数不变）、不加依赖。
"""

from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFrame, QPushButton, QScrollArea, QTabWidget

from harness import check, drain_threads, make_env, pump, section, summary

import ui.settings as settings_mod
from ui.panels.container import DIALOG_MAX_RATIO, fit_dialog_on_screen

ROOT = Path(__file__).resolve().parents[1]
TAB_NAMES = [
    "模型", "宠物", "记忆管理", "提醒", "番茄钟", "活动", "陪玩", "工作", "笔记",
    "知识库",      # v2-kb-init：放 .md/.txt 文档的地方，问答照着它答
]


def _src(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def _rows(db_path, sql: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(sql).fetchone()[0])
    finally:
        conn.close()


def _dialog(app, parent=None):
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
        parent=parent if parent is not None else app.panel,
    )


def _memory_tab(dialog) -> tuple[QTabWidget, QScrollArea]:
    tabs = dialog.findChild(QTabWidget)
    for index in range(tabs.count()):
        if tabs.tabText(index) == "记忆管理":
            tabs.setCurrentIndex(index)
    pump(0.15)
    return tabs, tabs.currentWidget()


def _in_viewport(scroll: QScrollArea, widget, container) -> tuple[bool, str]:
    """控件是不是整个落在滚动区视口里（两边都换算到容器的坐标里比）。"""
    viewport = scroll.viewport()
    top = widget.mapTo(container, widget.rect().topLeft()).y()
    bottom = widget.mapTo(container, widget.rect().bottomRight()).y()
    vp_top = viewport.mapTo(container, viewport.rect().topLeft()).y()
    vp_bottom = viewport.mapTo(container, viewport.rect().bottomRight()).y()
    return (top >= vp_top and bottom <= vp_bottom), f"控件 {top}→{bottom} 视口 {vp_top}→{vp_bottom}"


def _fill_real_data(app) -> None:
    """把两张表和记忆/提醒都填上真数据——走各自的真写入接口，不是往控件里硬塞字。"""
    for index in range(30):
        app.memory_store.remember("fact", f"第 {index} 条：它记住的一件事，稍微长一点", 0.5)
    for index in range(6):
        app.reminder_dao.add(
            app.user.id,
            datetime.now() + timedelta(hours=index + 1),
            f"第 {index} 条提醒：起来喝口水",
            None,
        )
    for index in range(7):
        app.mind_store.save_memory_book(
            date=f"2026-10-0{index + 1}",
            summary=f"第 {index} 天它一直在旁边陪着",
            entries=[f"楼下的包子真不错（{index}）", f"今天说了句挺长的话（{index}）"],
        )


def main() -> int:
    box, _stub, app = make_env()
    try:
        utterances_before = _rows(app.config.db_path, "select count(*) from utterance")
        mind_fragments_before = _rows(app.config.db_path, "select count(*) from memory")

        # ---------- S1 结构：八页一个不落，都套了滚动区 ----------

        section("S1 结构：每个页签外面都套了一层滚动区")

        dialog = _dialog(app)
        try:
            tabs = dialog.findChild(QTabWidget)
            names = [tabs.tabText(i) for i in range(tabs.count())]
            check("页签还是这几页、顺序没变（v2-notes-path 加「笔记」、v2-kb-init 加「知识库」）",
                  names == TAB_NAMES, str(names))
            kinds = [type(tabs.widget(i)).__name__ for i in range(tabs.count())]
            check("每一页拿到的都是 QScrollArea（不是光秃秃的页面）",
                  all(isinstance(tabs.widget(i), QScrollArea) for i in range(tabs.count())),
                  str(kinds))
            check("滚动区里托着的确实还是原来那一页（页面在，只是外面多了一层）",
                  tabs.widget(2).widget() is not None
                  and hasattr(dialog, "_memory_list") and hasattr(dialog, "_stage_bar"),
                  type(tabs.widget(2).widget()).__name__)

            ok_resize = all(tabs.widget(i).widgetResizable() for i in range(tabs.count()))
            check("每一页的滚动区都是 widgetResizable（窗口比内容大时页面铺满，不缩成一小块）",
                  ok_resize, str([tabs.widget(i).widgetResizable() for i in range(tabs.count())]))
            check("每一页的滚动区都去掉了边框（不然页面四周会多一圈框）",
                  all(tabs.widget(i).frameShape() == QFrame.NoFrame for i in range(tabs.count())), "")
            check("横向滚动条一律常关（设置页没有要横着看的东西）",
                  all(tabs.widget(i).horizontalScrollBarPolicy() == Qt.ScrollBarAlwaysOff
                      for i in range(tabs.count())), "")

            # 视口底色：套上滚动区之后视口会自己刷一层浅灰，得压成透明才对得上原来的白底
            styles = [tabs.widget(i).styleSheet() for i in range(tabs.count())]
            check("视口底色被压成透明（不然设置页里会多出一块灰）",
                  all("background: transparent" in s for s in styles), styles[0])
            check("那条透明规则只挑两级（再往里的列表、标签底色不动）",
                  all("> QWidget > QWidget" in s for s in styles), "")

            section("S1 尺寸：窗口不再被内容顶出去")

            hint = dialog.minimumSizeHint()
            check("对话框的最小高度不再是把人顶出屏幕的那个数（改前实测 725）",
                  hint.height() < 400, f"minimumSizeHint 高 = {hint.height()}")
            check("最小高度是被滚动区接住了，不是把内容砍掉换来的",
                  tabs.widget(2).widget().minimumSizeHint().height() > hint.height(),
                  f"记忆页最小高 {tabs.widget(2).widget().minimumSizeHint().height()} "
                  f"> 对话框最小高 {hint.height()}")
        finally:
            dialog.deleteLater()

        section("S1 定位：按屏幕九成封顶，然后居中")

        area = QGuiApplication.primaryScreen().availableGeometry()
        probe = _dialog(app)
        try:
            probe.resize(520, 2000)          # 故意弄成一个比屏幕还高的窗口
            fit_dialog_on_screen(probe)
            check("超过屏幕九成的窗口被压回九成以内",
                  probe.height() <= int(area.height() * DIALOG_MAX_RATIO) + 1,
                  f"{probe.height()} ≤ {int(area.height() * DIALOG_MAX_RATIO)}"
                  f"（可用区高 {area.height()}）")
            centre_gap = abs((probe.x() + probe.width() // 2)
                             - (area.left() + area.width() // 2))
            check("水平居中（跟屏幕中心差 ≤1px）", centre_gap <= 1, f"差 {centre_gap}px")
            vertical_gap = abs((probe.y() + probe.height() // 2)
                               - (area.top() + area.height() // 2))
            check("垂直也居中（不是钉在顶上）", vertical_gap <= 1, f"差 {vertical_gap}px")

            probe.resize(520, 500)           # 比屏幕矮的窗口不该被拉高
            fit_dialog_on_screen(probe)
            check("比屏幕矮的窗口保持原样，不会被硬拉高", probe.height() == 500, str(probe.height()))
            probe.resize(520, 80)            # 小到不合理时才兜到下限，不是放任它缩没
            fit_dialog_on_screen(probe)
            check("小到不合理的窗口兜在下限上（标题栏 + 一行内容放得下）",
                  probe.height() >= 320, str(probe.height()))
        finally:
            probe.deleteLater()

        # ---------- S2 / S4 记忆管理页：真能滚到底、真能操作 ----------

        _fill_real_data(app)
        section("S2 记忆管理页：页面比视口长，滚动条真的能滚")

        dialog = _dialog(app)
        try:
            dialog.show()
            pump(0.25)
            tabs, scroll = _memory_tab(dialog)
            dialog.refresh_memories()
            dialog.refresh_reminders()
            dialog.refresh_intimacy()
            pump(0.15)

            bottom = dialog.mapToGlobal(dialog.rect().bottomLeft()).y()
            check("真 show 出来，窗口底边不越过屏幕可用区底边",
                  bottom <= area.bottom(), f"底边 {bottom} / 可用区 {area.bottom()}")
            check("窗口里两个表都在：记忆簿 / 记忆列表",
                  dialog._book_list.count() > 0 and dialog._memory_list.count() > 0,
                  f"记忆簿 {dialog._book_list.count()} / 记忆 {dialog._memory_list.count()}")
            check("星念块已经不在了（v2-life-card-star 砍掉）",
                  not hasattr(dialog, "_star_list"), "")

            page = scroll.widget()
            bar = scroll.verticalScrollBar()
            check("页面比视口长（所以要滚），滚动条跑得起来",
                  page.height() > scroll.viewport().height() and bar.maximum() > 0,
                  f"页面 {page.height()} vs 视口 {scroll.viewport().height()}，"
                  f"滚动范围 0..{bar.maximum()}")
            for name in ("_book_list", "_memory_list"):
                widget = getattr(dialog, name)
                check(f"{name} 是滚动区那一页里的（跟着一起滚）",
                      page.isAncestorOf(widget), type(widget.parent()).__name__)

            bar.setValue(bar.minimum())
            pump(0.1)
            forget = [b for b in dialog.findChildren(QPushButton) if b.text() == "忘掉选中的这条"][0]
            at_top, detail_top = _in_viewport(scroll, forget, dialog)
            check("滚在顶上时，最下面那个按钮还在视口外（说明滚动是真在起作用）",
                  not at_top, detail_top)
            check("滚在顶上时，页面最上面的两块（亲密度 + 记忆簿）看得到",
                  all(_in_viewport(scroll, w, dialog)[0]
                      for w in (dialog._stage_bar, dialog._book_list)),
                  f"亲密度 {_in_viewport(scroll, dialog._stage_bar, dialog)[1]}")

            section("S4 滚到底：最下面那两块和两个按钮都够得着、点得动")

            bar.setValue(bar.maximum())
            pump(0.15)
            for name, label in (("_memory_list", "记忆列表"),
                                ("_reminder_list", "提醒列表")):
                widget = getattr(dialog, name)
                inside, detail = _in_viewport(scroll, widget, dialog)
                check(f"滚到底后「{label}」整个在视口里", inside, detail)
            for button in dialog.findChildren(QPushButton):
                if button.text() not in ("忘掉选中的这条", "取消选中的提醒"):
                    continue
                inside, detail = _in_viewport(scroll, button, dialog)
                check(f"滚到底后「{button.text()}」整个在视口里", inside, detail)
            top_gone, detail_gone = _in_viewport(scroll, dialog._book_list, dialog)
            check("滚到底后，页面最上面的记忆簿已经滚出视口（两头真的跑得动）",
                  not top_gone, detail_gone)

            # 真点：滚到底之后按一下，库里那条记忆真没了
            before = dialog._memory_list.count()
            dialog._memory_list.setCurrentRow(0)
            target = [b for b in dialog.findChildren(QPushButton) if b.text() == "忘掉选中的这条"][0]
            QTest.mouseClick(target, Qt.LeftButton, pos=target.rect().center())
            pump(0.2)
            check("滚到底后真点一下「忘掉选中的这条」：列表少一条",
                  dialog._memory_list.count() == before - 1,
                  f"{before} → {dialog._memory_list.count()}")

            section("S1 别的页：不需要滚的不给滚动条")

            leaks = []
            for index in range(tabs.count()):
                tabs.setCurrentIndex(index)
                pump(0.08)
                view = tabs.widget(index)
                hb = view.horizontalScrollBar()
                if hb.maximum() != 0:
                    leaks.append(f"{tabs.tabText(index)} 横向 {hb.minimum()}..{hb.maximum()}")
            check("每一页都没有冒出横向滚动条（v2-notes-path 的「笔记」页当初就是因为"
                  "落点全路径折不动行，把整页顶宽 260px）", not leaks, str(leaks))
            tabs.setCurrentIndex(7)      # 工作页：内容很短，不该有竖条
            pump(0.1)
            work_view = tabs.widget(7)
            check("内容本来就短的页（工作）不会凭空多出竖滚动条",
                  work_view.verticalScrollBar().maximum() == 0,
                  str(work_view.verticalScrollBar().maximum()))
        finally:
            dialog.close()
            dialog.deleteLater()

        # ---------- 红线 ----------

        section("红线：只动显示，不动数据")

        check("pet.db 的 utterance 一行没多（打开设置、滚动、删一条记忆都不写它）",
              _rows(app.config.db_path, "select count(*) from utterance") == utterances_before,
              f"{utterances_before} → {_rows(app.config.db_path, 'select count(*) from utterance')}")
        check("记忆条数是真减了一条（上一步的删除是真删，不是只从界面上抹掉）",
              _rows(app.config.db_path, "select count(*) from memory") == mind_fragments_before + 30 - 1,
              f"{mind_fragments_before} → {_rows(app.config.db_path, 'select count(*) from memory')}"
              f"（测试自己塞了 30 条）")
        check("设置页的改动只在视图层：settings.py 里没有新的写库调用",
              "sqlite3.connect" not in _src("ui/settings.py")
              and "session.commit" not in _src("ui/settings.py"), "")
        check("没新增依赖（用的还是 requirements 里已有的 PySide6）",
              "PySide6" in _src("requirements.txt"), "")
        check("滚动区是标准控件，没为它引第三方库",
              "import requests" not in _src("ui/settings.py")
              and "QScrollArea" in _src("ui/settings.py"), "")
        drain_threads()
    finally:
        box.cleanup()

    return summary()


if __name__ == "__main__":
    raise SystemExit(main())
