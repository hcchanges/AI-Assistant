"""
界面接线与设置的自动测试（离屏渲染，看得见控件但看不见长相）。

覆盖：右键/托盘菜单项 / 面板二级页面与返回 / 陪玩页开关 / 设置页八个标签与读写往返 /
扯嘴巴换性格的轮换与静默。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
from PySide6.QtGui import QGuiApplication, QMouseEvent
from PySide6.QtWidgets import QTabWidget, QWidget

import keyring

from core.config import Config
from life.mind.intimacy import MindIntimacy
from life.mind.memory_book import MemoryBook
from life.mind.store import MindStore
from work.service import DEFAULT_ADMIN_PASS, DEFAULT_ADMIN_USER
import ui.settings as settings_mod
import ui.theme as theme_mod
from pet.persona import PERSONA_KEYS
from ui.pet_window import HOVER_HINT_LINE, ZZZ_TEXT


def menu_texts(menu) -> list[str]:
    return [action.text() for action in menu.actions() if not action.isSeparator()]


def _inside(window: QWidget, area) -> bool:
    """窗口是不是整块落在某块屏幕的可用区里。"""
    return (
        window.x() >= area.left()
        and window.y() >= area.top()
        and window.x() + window.width() <= area.right() + 1
        and window.y() + window.height() <= area.bottom() + 1
    )


def main() -> int:
    box, stub, app = make_env()

    # 设置页保存会把 Key 写进 Windows 凭据管理器、把开机自启写进注册表；
    # 测试里一律拦住，改成只记录，避免动到用户真实的环境。
    written: list[tuple[str, str]] = []
    deleted: list[str] = []
    settings_mod.secret_set = lambda ref, value: written.append((ref, value))
    keyring.delete_password = lambda service, ref: deleted.append(ref)

    spoken: list[str] = []
    toasts: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.toast.connect(toasts.append)
    pump(0.05)

    section("右键菜单与托盘菜单")
    # v2-focus-web ① 补了「自动番茄钟」两条：原先界面上一个控件都没有（浮窗点不了、
    # 设置页那页也没有菜单入口），真机反馈"休息没法测"。
    # v2-focus-interact ② 把这两条改成**只在专注进行中才露出来**：退出专注之后
    # 菜单上还杵着「结束专注/番茄钟」，看着像"这一轮还在"，是冗余。所以这里量到的
    # 空闲态名单短了两项；专注中长什么样由 test_focus_interact.py 盯着。
    # v2-chat-core #1 又把「语音输入（按住说话）」摘了：用户要的是聊天框里只有
    # 文字框 + 发送，两条菜单上那条入口与全局快捷键一并不接。所以空闲态名单
    # 再短一项——引擎还在（life/voice/），只是界面上不再有明路，别的地方盯着。
    check(
        "桌宠右键（空闲态）是 工作/生活/设置/退出（和谁聊、语音输入已删）",
        menu_texts(app.pet_window._build_menu())
        == ["工作", "生活", "设置", "退出"],
        str(menu_texts(app.pet_window._build_menu())),
    )
    check(
        "托盘菜单（空闲态）是 显示/隐藏 + 四项，跟桌宠右键同一套",
        menu_texts(app.tray.contextMenu())
        == ["隐藏桌宠", "工作", "生活", "设置", "退出"],
        str(menu_texts(app.tray.contextMenu())),
    )
    app.tray.set_pet_visible(False)
    check(
        "隐身之后托盘项变成显示桌宠",
        menu_texts(app.tray.contextMenu())[0] == "显示桌宠",
        menu_texts(app.tray.contextMenu())[0],
    )

    section("面板页面与返回")
    panel = app.panel
    panel.show()
    for key, title in (
        ("life", "生活模块"),
        ("work", "工作模块"),
        ("pomodoro", "番茄钟"),
        ("playmate", "游戏陪玩"),
    ):
        panel.show_page(key)
        pump(0.02)
        check(f"能切到「{title}」", panel._page_title.text() == title, panel._page_title.text())

    panel.show_page("playmate")
    pump(0.02)
    check("二级页显示返回按钮", panel._back_button.isVisible())
    panel._go_back()
    pump(0.02)
    check("返回回到生活模块", panel._page_title.text() == "生活模块", panel._page_title.text())
    check("一级页不显示返回按钮", not panel._back_button.isVisible())

    # 工作模块现在打开就是卡片轮播（指令 18），是一级页、没有返回按钮
    panel.show_page("work")
    pump(0.02)
    check("工作模块是一级页（不用先选部门）", not panel._back_button.isVisible())

    panel.show_page("life")
    pump(0.02)

    section("窗口整块落在屏幕里（指令 19 ①③）")
    area = QGuiApplication.primaryScreen().availableGeometry()
    check(
        "主面板放得进屏幕（不再被撑到 800 多）",
        panel.height() <= area.height() and panel.width() <= area.width(),
        f"{panel.width()}x{panel.height()} / 屏幕 {area.width()}x{area.height()}",
    )
    check(
        "主面板整个在屏幕可用区内（标题栏看得见）",
        _inside(panel, area),
        str(panel.geometry()),
    )

    # 换个"离谱"的窗口位置与尺寸，验证夹取逻辑本身
    from ui.panels.container import keep_on_screen

    broken = QWidget()
    broken.resize(4000, 3000)
    broken.move(-600, -900)
    keep_on_screen(broken)
    check(
        "超大窗口被收进屏幕",
        broken.width() <= area.width() and broken.height() <= area.height(),
        f"{broken.width()}x{broken.height()}",
    )
    check("跑出屏幕的位置被夹回来", broken.x() >= area.left() and broken.y() >= area.top(), f"{broken.x()},{broken.y()}")
    check("收完之后整个窗口都在屏幕内", _inside(broken, area), str(broken.geometry()))
    broken.deleteLater()

    section("陪玩页")
    page = app.playmate_panel
    check("初始显示未开启", page._status.text() == "陪玩：未开启", page._status.text())
    check("按钮是开始陪玩", page._button.text() == "开始陪玩", page._button.text())
    check("详情里写着看图模型", app.config.vision.model in page._detail.text(), page._detail.text().splitlines()[0])

    page._button.click()
    pump(0.02)
    check("点开始 → 真的开启了", app.playmate_service.is_active)
    check("点开始 → 按钮跟着变", page._button.text() == "停止陪玩", page._button.text())
    page._button.click()
    pump(0.02)
    check("再点一下关闭", not app.playmate_service.is_active)

    app.bus.playmate_changed.emit(True)      # 模拟从聊天里开启
    pump(0.02)
    check("聊天里开启时页面状态同步", page._status.text() == "陪玩：已开启", page._status.text())
    app.bus.playmate_changed.emit(False)
    pump(0.05)

    section("设置页")
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
        parent=panel,
        work_service=app.work_service,
        work_panel=app.work_panel,
    )
    tabs = dialog.findChild(QTabWidget)
    names = [tabs.tabText(index) for index in range(tabs.count())]
    check(
        "标签都在、顺序没变（老八页 + v2-notes-path 的「笔记」+ v2-kb-init 的「知识库」）",
        # 「知识库」页是 v2-kb-init 加的，settings.py:163 无条件挂上（那页只有一个落点，
        # 没有开关）——这份名单跟着补，不然这条检查永远红着，真丢了页也看不出来。
        names == ["模型", "宠物", "记忆管理", "提醒", "番茄钟", "活动", "陪玩", "工作",
                  "笔记", "知识库"],
        str(names),
    )

    section("记忆管理页（指令 19 ②）")
    check(
        "生活面板不再显示记忆/提醒列表",
        not hasattr(app.life_panel, "_memory_list") and not hasattr(app.life_panel, "_reminder_list"),
        "列表已从主界面移走",
    )
    before = dialog._memory_list.count()
    app.memory_store.remember("preference", "不吃香菜", 0.8)
    dialog.refresh_memories()
    check("新记忆出现在记忆管理页", dialog._memory_list.count() == before + 1, str(dialog._memory_list.count()))
    check(
        "列的是记忆正文",
        "不吃香菜" in dialog._memory_list.item(before).text(),
        dialog._memory_list.item(before).text(),
    )
    dialog._memory_list.setCurrentRow(before)
    dialog._forget_memory()
    check("忘掉后就没了", dialog._memory_list.count() == before, str(dialog._memory_list.count()))

    from datetime import datetime, timedelta

    reminders_before = dialog._reminder_list.count()
    app.reminder_dao.add(app.user.id, datetime.now() + timedelta(hours=2), "开会", None)
    dialog.refresh_reminders()
    check(
        "提醒也列在记忆管理页",
        dialog._reminder_list.count() == reminders_before + 1,
        str(dialog._reminder_list.count()),
    )
    check(
        "提醒里写着事项与状态",
        "开会" in dialog._reminder_list.item(0).text() and "待触发" in dialog._reminder_list.item(0).text(),
        dialog._reminder_list.item(0).text(),
    )

    check("回填了看图模型名", dialog._vision_model_edit.text() == app.config.vision.model, dialog._vision_model_edit.text())
    check("回填了截屏间隔", dialog._vision_sample_spin.value() == app.config.vision.sample_sec)
    check(
        "管理者模式显示「已处于管理者模式」与退出按钮",
        not dialog._admin_box.isHidden()
        and dialog._admin_login_box.isHidden()
        and dialog._admin_logout_button.text() == "退出管理者",
        f"admin_box_hidden={dialog._admin_box.isHidden()} login_box_hidden={dialog._admin_login_box.isHidden()}",
    )

    dialog._logout_admin()
    check("退出管理者写回配置", app.config.work.role == "user", app.config.work.role)
    check("退出后工作服务不再是管理员", not app.work_service.is_admin(), str(app.work_service.is_admin()))
    check("退出后工作页没有底层编辑按钮", app.work_panel._admin_button is None, "")
    check(
        "退出后显示登录框",
        not dialog._admin_login_box.isHidden() and dialog._admin_box.isHidden(),
        f"login_box_hidden={dialog._admin_login_box.isHidden()} admin_box_hidden={dialog._admin_box.isHidden()}",
    )

    dialog._admin_user_edit.setText("wrong")
    dialog._admin_pass_edit.setText("wrong")
    dialog._login_admin()
    check("错误账号密码不改变身份", app.config.work.role == "user", app.config.work.role)
    check("错误提示", dialog._status.text() == "账号或密码错误", dialog._status.text())

    dialog._admin_user_edit.setText(DEFAULT_ADMIN_USER)
    dialog._admin_pass_edit.setText(DEFAULT_ADMIN_PASS)
    dialog._login_admin()
    check("登录成功写回管理员", app.config.work.role == "admin", app.config.work.role)
    check("登录后工作服务是管理员", app.work_service.is_admin(), str(app.work_service.is_admin()))
    check("登录后工作页出现底层编辑按钮", app.work_panel._admin_button is not None, "")
    check(
        "登录提示是已解锁",
        dialog._status.text() == "已登录管理者模式，工作模块已解锁",
        dialog._status.text(),
    )

    dialog._logout_admin()
    check("退出后回到员工身份", app.config.work.role == "user", app.config.work.role)
    check(
        "点评开关默认关、文案是「宠物点评工作结果」",
        dialog._comment_check.isChecked() is False
        and dialog._comment_check.text() == "宠物点评工作结果",
        f"{dialog._comment_check.text()} / {dialog._comment_check.isChecked()}",
    )
    dialog._comment_check.setChecked(True)

    dialog._vision_model_edit.setText("glm-4v-plus")
    dialog._vision_provider_combo.setCurrentText("tongyi")
    dialog._vision_sample_spin.setValue(8)
    dialog._vision_threshold_spin.setValue(22.0)
    dialog._vision_gap_spin.setValue(90)
    dialog._vision_width_spin.setValue(1280)
    dialog._name_edit.setText("小团")
    dialog._persona_combo.setCurrentText("傲娇")
    dialog._key_edit.setText("")
    dialog._vision_key_edit.setText("sk-测试用")
    ok = dialog._collect_fields()
    check("填写能通过校验", ok, dialog._status.text())
    check("看图模型名写回配置", app.config.vision.model == "glm-4v-plus", app.config.vision.model)
    check("截屏间隔写回配置", app.config.vision.sample_sec == 8, str(app.config.vision.sample_sec))
    check("变化阈值写回配置", app.config.vision.change_threshold == 22.0, str(app.config.vision.change_threshold))
    check("最小间隔写回配置", app.config.vision.min_gap_sec == 90, str(app.config.vision.min_gap_sec))
    check("图片宽度写回配置", app.config.vision.max_width == 1280, str(app.config.vision.max_width))
    check("宠物名写回配置", app.config.pet.name == "小团", app.config.pet.name)
    check("性格写回配置", app.config.pet.persona == "傲娇", app.config.pet.persona)
    check("退出管理者后的身份写回配置", app.config.work.role == "user", app.config.work.role)
    check("点评开关写回配置", app.config.work.pet_comment is True, str(app.config.work.pet_comment))
    check("陪玩页的 Key 存进了凭据管理器（已拦住，只做记录）", written == [("llm_dashscope", "sk-测试用")], str(written))

    app.config.save()
    reloaded = Config.load(box.config_path)
    check("存盘后重新读出来还是新值", reloaded.vision.model == "glm-4v-plus", reloaded.vision.model)
    check("宠物名也存住了", reloaded.pet.name == "小团", reloaded.pet.name)
    check("退出管理者状态也存住了", reloaded.work.role == "user", reloaded.work.role)
    check("点评开关也存住了", reloaded.work.pet_comment is True, str(reloaded.work.pet_comment))
    dialog.close()

    # ======================================================================
    # B++ 富化：设置页记忆管理标签里的两段展示（阶段条 / 记忆簿时间线）
    # 星念在 v2-life-card-star 砍掉：这里顺带验"记忆管理页不再有星念这一块"。
    # ======================================================================
    section("记忆管理页（B++ 富化）：阶段条 / 记忆簿时间线（无星念项）")
    # 另开一个探测库喂确定的数据：这一节验的是"界面照数据画成了什么样"，
    # 数据本身由 test_mind 那几节负责。探测库不挂向量索引，add_fragment 不做嵌入。
    probe = MindStore(box.dir / "ui_probe.db")
    probe.save_intimacy(level=0, exp=310)          # L4「知己」，档内 90/180
    probe.add_fragment("今天跟妈妈和好了", tags=["家人"], src_time="2026-10-01 20:00:00")
    probe.save_memory_book("2026-10-01", "记下 1 件事；关系：在慢慢熟起来", ["今天跟妈妈和好了"])
    probe_intimacy = MindIntimacy(probe)
    enr = settings_mod.SettingsDialog(
        config=app.config, bus=app.bus,
        pet_profile_dao=app.pet_profile_dao, activity_dao=app.activity_dao,
        category_dao=app.app_category_dao, memory_store=app.memory_store,
        reminder_dao=app.reminder_dao, jobs=app.jobs, user_id=app.user.id,
        intimacy=probe_intimacy,
        memory_book=MemoryBook(probe, intimacy=probe_intimacy),
        parent=panel,
    )

    # ---- 亲密度：一句话 + 五档刻度 + 档内进度条 ----
    check("那句阶段话说了现在到哪一档",
          enr._stage_label.text() == "知己 · 还差 90 到挚友", enr._stage_label.text())
    marks = enr._stage_marks.text()
    check("五档刻度都在",
          all(name in marks for name in ("陌生", "熟络", "朋友", "知己", "挚友")), marks)
    check("到了的用主色点亮，没到的灰着",
          marks.count(theme_mod.ACCENT) == 4 and marks.count(theme_mod.TEXT_MUTED) == 1,
          f"主色 {marks.count(theme_mod.ACCENT)} 处 / 灰 {marks.count(theme_mod.TEXT_MUTED)} 处")
    check("刻度是富文本（不是把标签名直接印出来）",
          enr._stage_marks.textFormat() == Qt.RichText, str(enr._stage_marks.textFormat()))
    check("进度条量的是档内进度，不是总经验",
          enr._stage_bar.minimum() == 0 and enr._stage_bar.maximum() == 180
          and enr._stage_bar.value() == 90,
          f"{enr._stage_bar.value()}/{enr._stage_bar.maximum()}")
    check("条上写着走过多少（不写「好感度」三个字）",
          enr._stage_bar.format() == "90 / 180", enr._stage_bar.format())

    # ---- 星念已砍（v2-life-card-star）：记忆管理页不再有星念这一块 ----
    check("星念列表控件已经不在了", not hasattr(enr, "_star_list"), "")
    check("星念说明控件也已经不在了", not hasattr(enr, "_star_hint"), "")

    # ---- 记忆簿：页头 + 一天一段 + 当天原话（时间 + 关键词）----
    rows = [enr._book_list.item(i).text() for i in range(enr._book_list.count())]
    check("页头那句也在记忆簿顶上", rows[0] == "— 知己 · 还差 90 到挚友 —", rows[0])
    check("底下一天一段（日期 + 概括）",
          rows[1] == "2026-10-01　记下 1 件事；关系：在慢慢熟起来", rows[1])
    check("当天原话挂在下面，带时间和关键词",
          rows[2].startswith("　　· 今天跟妈妈和好了") and "10-01 20:00" in rows[2]
          and "家人" in rows[2], rows[2])
    check("原话那行灰一号（跟日期行分得开）",
          enr._book_list.item(2).foreground().color().name() == theme_mod.TEXT_MUTED.lower(),
          enr._book_list.item(2).foreground().color().name())

    # ---- 刷新：数据变了，再刷一遍要跟上 ----
    probe.save_intimacy(level=0, exp=400)          # 直接推满级
    enr.refresh_intimacy()
    check("推满级后刻度全亮、进度条不再是一根走不满的条",
          enr._stage_marks.text().count(theme_mod.TEXT_MUTED) == 0
          and enr._stage_bar.value() == enr._stage_bar.maximum()
          and enr._stage_bar.format() == "已经是最亲近的了",
          f"{enr._stage_marks.text().count(theme_mod.TEXT_MUTED)} 处灰 / "
          f"{enr._stage_bar.value()}/{enr._stage_bar.maximum()} / {enr._stage_bar.format()}")

    # ---- 红线：展示层只读不写 ----
    # 数据已经喂完了，下面只许"读 → 画"。比字节而不是比行数：一行没多但文件被
    # 重写过（比如顺手做了个 optimize / 写了个时间戳）同样是破线。
    before_mind = (box.dir / "ui_probe.db").read_bytes()
    before_pet = (box.dir / "pet.db").read_bytes()
    for _ in range(3):
        enr.refresh_intimacy()
    pump(0.05)
    check("连刷三遍界面 → 两个库一个字节都没变",
          (box.dir / "ui_probe.db").read_bytes() == before_mind
          and (box.dir / "pet.db").read_bytes() == before_pet,
          f"mind.db {len(before_mind)} 字节 / pet.db {len(before_pet)} 字节，前后一致")

    enr.close()
    probe.close()

    # 两个都不接的老样子：这一节之外那个 dialog 就是这么建的，一路用到这儿没塌
    # （星念参数在 v2-life-card-star 已删，_star_list 这块控件本来就不存在了）
    check("不接这两样时，那两块控件根本不建（老样子一个字不变）",
          not hasattr(dialog, "_stage_marks") and not hasattr(dialog, "_book_list"),
          "见本节之前那整段设置页读写往返")

    section("回归：审查发现的两处交互问题")

    def send_press(window, local, glob) -> None:
        window.mousePressEvent(
            QMouseEvent(
                QEvent.Type.MouseButtonPress, local, glob, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
            )
        )

    def send_move(window, local, glob) -> None:
        window.mouseMoveEvent(
            QMouseEvent(
                QEvent.Type.MouseMove, local, glob, Qt.LeftButton, Qt.LeftButton, Qt.NoModifier
            )
        )

    pet = app.pet_window
    pet.show()
    pump(0.05)
    pet._anger_shakes_window = True
    pet._start_anger()                     # 模拟刚扯完脸蛋、正在生气的那 2.6 秒
    body = QPointF(80.0, 155.0)            # 身体下方，避开脸
    send_press(pet, body, QPointF(pet.x() + 80.0, pet.y() + 155.0))
    send_move(pet, body, QPointF(pet.x() + 200.0, pet.y() + 155.0))
    dragged_x = pet.x()
    pet._on_tick()                         # 下一拍：生气抖动不该把窗口拨回生气时的位置
    check("生气期间拖动宠物不会被拨回去", pet.x() == dragged_x, f"{dragged_x} → {pet.x()}")
    check("生气期间拖动不会顺手换性格", not pet._persona_switch_pending)
    pet._dragged = False                   # 松手（不走抛掷，省得腾空姿势影响下面看姿势）
    pet._drag_origin = None
    pet._settled = True

    from pet.behavior import Behavior

    pet._behavior = Behavior.LIE            # 趴卧：头明显偏在贴图一侧
    pet._behavior_dir = 1
    pose = pet._current_pose()
    face = pet._skin.face_rect(pose)
    drawn_head_x = pet.width() - 1 - face.center().x()   # paintEvent 里 mirror=-1，画出来是翻转的
    check(
        f"趴卧时按画出来的头能扯嘴（{pose}）",
        pet._face_hit(QPoint(drawn_head_x, face.center().y())),
    )
    check(
        "趴卧时按另一头不该触发扯嘴",
        not pet._face_hit(QPoint(face.center().x(), face.center().y())),
    )
    pet._behavior = None
    pet._anger_until = 0.0
    pet._anger_shakes_window = True

    app.pet_window.hide()
    pump(0.05)
    check("宠物隐身时陪玩不再涂白", app.playmate_service._pet_rect() is None)
    app.pet_window.show()
    pump(0.05)
    check("宠物可见时照常涂白", app.playmate_service._pet_rect() is not None)

    section("扯脸蛋换性格：轮换 + 静默")
    app.config.pet.persona = PERSONA_KEYS[0]
    spoken.clear()
    toasts.clear()
    pump(0.05)
    app._cycle_persona()
    pump(0.05)
    check("换到了下一个性格", app.config.pet.persona == PERSONA_KEYS[1], app.config.pet.persona)
    check("换性格时宠物不说话", spoken == [], str(spoken))
    check("换性格时面板不弹提示", toasts == [], str(toasts))

    app.config.pet.persona = PERSONA_KEYS[-2]
    app._cycle_persona()
    check("最后一个之后回到第一个", app.config.pet.persona == PERSONA_KEYS[0], app.config.pet.persona)

    app.config.pet.persona = "自定义"
    app.config.pet.persona_prompt = ""
    app._cycle_persona()
    check("没写人设的「自定义」不参与轮换", app.config.pet.persona != "自定义", app.config.pet.persona)

    section("状态词让位：气泡里正说着正文时，悬停轻提示和打盹 zzz 都不抢")
    pet = app.pet_window
    pet.show()
    pump(0.05)
    line = "我在这儿呢，慢慢说。"
    pet.speak(line)
    pump(0.05)
    check("正文进了气泡", pet._bubble._label.text() == line, pet._bubble._label.text())
    pet._on_hover_timeout()
    pump(0.05)
    check("悬停轻提示没把正文顶掉", pet._bubble._label.text() == line, pet._bubble._label.text())
    check("气泡也没被提前收掉", pet._bubble.isVisible(), str(pet._bubble.isVisible()))

    from pet.behavior import Behavior

    pet.play_behavior(Behavior.SLEEP, 1000)
    pump(0.05)
    check("打盹的 zzz 也没顶掉正文", pet._bubble._label.text() == line, pet._bubble._label.text())
    pet.stop_behavior()

    section("zzz 归睡觉这个动作管：动作一结束它就走，也不连坐别人写的话（v2-pose-sync）")
    pet.hide_speech_bubble()
    pet.play_behavior(Behavior.SLEEP, 30_000)
    pump(0.05)
    check("打着盹：zzz 挂在头顶",
          pet._bubble.isVisible() and pet._bubble._label.text() == ZZZ_TEXT,
          f"可见={pet._bubble.isVisible()} 文本={pet._bubble._label.text()!r}")
    pet.stop_behavior()
    check("★ 睡觉结束 → zzz 跟着收掉（修前它会自己慢慢淡出，人已经醒了头上还挂着）",
          pet._bubble.isVisible() is False, f"可见={pet._bubble.isVisible()}")

    pet.play_behavior(Behavior.SLEEP, 30_000)
    pump(0.02)
    # 睡觉这会儿别的话飘上来（开场白 / 情绪词那条路就是往同一个气泡里写）：后写的赢，
    # 这条记成 speech 档。走 `speak()` 的话会先把状态机切到"说话"、顺手把动作停了，
    # 就验不到"收 zzz 时不误伤别人"这一条了，所以这里直接写气泡。
    pet._bubble.show_text("我先说一句。", pet.frameGeometry(), 30_000)
    pump(0.05)
    check("正文盖掉 zzz（任意时刻只一条气泡）",
          pet._bubble._label.text() == "我先说一句。", pet._bubble._label.text())
    pet.stop_behavior()
    check("★ 睡觉结束只收自己那条 zzz，屏上这句正文一个字不动",
          pet._bubble.isVisible() and pet._bubble._label.text() == "我先说一句。",
          f"可见={pet._bubble.isVisible()} 文本={pet._bubble._label.text()!r}")
    pet.hide_speech_bubble()
    pet._speech_until = 0.0

    pet._speech_until = 0.0          # 正文的停留时间过去
    pet._bubble.hide_bubble()
    pet._on_hover_timeout()
    pump(0.05)
    check("正文过去后轻提示照常冒", pet._bubble._label.text() == HOVER_HINT_LINE, pet._bubble._label.text())

    section("聊天框开着时不冒轻提示：它就停在宠物头顶，会糊在输入行上")
    pet._speech_until = 0.0
    pet._bubble.hide_bubble()
    app.chat_bubble.close_bubble(fade=False)
    pump(0.1)
    app.open_chat()          # 走真实入口，顺带验"开框先收掉头顶那条"
    pump(0.1)
    check("聊天框开着了", app.chat_bubble.isVisible(), str(app.chat_bubble.isVisible()))
    pet._on_hover_timeout()
    pump(0.05)
    check("这会儿不冒轻提示", not pet._bubble.isVisible(), f"气泡可见={pet._bubble.isVisible()}")
    app.chat_bubble.close_bubble(fade=False)
    pump(0.1)
    pet._on_hover_timeout()
    pump(0.05)
    check(
        "聊天框收起来后轻提示照常冒",
        pet._bubble.isVisible() and pet._bubble._label.text() == HOVER_HINT_LINE,
        f"可见={pet._bubble.isVisible()} 文本={pet._bubble._label.text()!r}",
    )

    section("状态气泡与对话气泡合成一条：框开着时浮动气泡全让位（不再两个尖角）")
    pet._speech_until = 0.0
    pet._bubble.hide_bubble()
    app.chat_bubble.close_bubble(fade=False)
    pump(0.1)
    # 修复前：气泡已经挂在头顶，这时候打开聊天框，两条窗各画一个 9px 尖角 = 用户说的"两个脚"
    pet.speak("我先说一句话。")
    pump(0.05)
    check("开框前正文气泡在屏上", pet._bubble.isVisible(), str(pet._bubble.isVisible()))
    pet_rect = pet.frameGeometry()
    app.open_chat()
    pump(0.15)
    check(
        "开框后头顶那条立刻收掉",
        not pet._bubble.isVisible(),
        f"气泡可见={pet._bubble.isVisible()}（True 就是两个尖角）",
    )
    check("聊天框自己是开着的", app.chat_bubble.isVisible(), str(app.chat_bubble.isVisible()))
    pet.speak("框开着的时候我也想说一句。")
    pump(0.05)
    check("框开着时 speak() 不冒浮动气泡", not pet._bubble.isVisible(), f"气泡可见={pet._bubble.isVisible()}")
    pet.play_behavior(Behavior.SLEEP, 1000)
    pump(0.05)
    check("框开着时 zzz 不冒", not pet._bubble.isVisible(), f"气泡可见={pet._bubble.isVisible()}")
    pet.stop_behavior()
    pet._start_anger()
    pump(0.05)
    check("框开着时生气台词不冒", not pet._bubble.isVisible(), f"气泡可见={pet._bubble.isVisible()}")
    app.chat_bubble.close_bubble(fade=False)
    pump(0.15)
    pet.speak("框关了，我接着说。")
    pump(0.05)
    check(
        "关掉聊天框后浮动气泡照常冒",
        pet._bubble.isVisible() and pet._bubble._label.text() == "框关了，我接着说。",
        f"可见={pet._bubble.isVisible()} 文本={pet._bubble._label.text()!r}",
    )
    pet._bubble.follow(pet_rect)

    drain_threads()
    box.cleanup()
    return summary("界面与设置")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
