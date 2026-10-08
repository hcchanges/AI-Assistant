"""
设置页：模型配置（含热切换与密钥）、宠物人设、提醒与免打扰。

对应方案 P4。密钥写进 Windows 凭据管理器，config.yaml 里只留凭据名。
保存后不需要重启：各服务都持有同一个 Config 对象，下次调用模型时按新配置创建实例。
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
from typing import Any

from PySide6.QtCore import QTime, Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTimeEdit,
    QVBoxLayout,
    QWidget,
)

from brain.llm.factory import LLMFactory
from core import autostart
from core.config import (
    KNOWLEDGE_DIR_DEFAULT,
    NOTES_DIR_DEFAULT,
    WORK_ROLE_ADMIN,
    WORK_ROLE_USER,
    Config,
    ConfigError,
    knowledge_dir_usable,
    notes_dir_usable,
    secret_get,
    secret_set,
)
from core.events import EventBus
from core.reltime import memory_text, reminder_text
from core.worker import run_async
from data.dao import ActivitySessionDAO, AppCategoryDAO, PetProfileDAO, ReminderDAO
from memory.store import MemoryStore
from monitor.category import CATEGORIES, builtin_category, friendly_name
from pet.persona import CUSTOM_KEY, PERSONA_KEYS, USER_CALL_CHOICES, preset_of
from scheduler.jobs import ReminderJobs
from ui.panels.container import fit_dialog_on_screen
from ui.theme import ACCENT, PANEL_QSS, TEXT_MUTED

logger = logging.getLogger(__name__)

MEMORY_KIND_TEXT = {"fact": "事实", "preference": "偏好", "event": "事件"}

REMINDER_STATUS_TEXT = {
    "pending": "待触发",
    "fired": "已提醒",
    "muted": "已静音（免打扰或超频次）",
    "missed": "已错过（软件当时没开）",
}


def parse_words(text: str) -> tuple[str, ...]:
    """把输入框里那串词拆成词表（逗号 / 顿号 / 分号都认，中文标点也认）。

    不按空格拆：工作站点词里有正常带空格的（"google 文档"），按空格拆会把它劈成两半。
    """
    for separator in ("，", "、", "；", ";", "|", "\n"):
        text = text.replace(separator, ",")
    return tuple(word.strip() for word in text.split(",") if word.strip())


def wrap_path(text: str) -> str:
    """在路径的分隔符后面塞零宽断行点（U+200B），让长路径折得动行。

    路径里没有空格，Qt 自动折行找不到断点，会把整条路径当一个"长单词"：标签的最小
    宽度就等于整条路径的宽度，一格都不肯让（实测一条临时路径要 660px），设置窗整体
    就会冒出横向滚动条（v2-notes-path 加"笔记"页时踩到的）。U+200B 是标准做法——
    看不见、不占宽度，只多给折行一个机会。复制出去会带上这个字符，但这条提示只是
    给人看落点，真要改路径请用上面的输入框。
    """
    break_point = "\u200b"   # 零宽空格：用转义写，别把隐形字符直接塞进源码
    return text.replace("\\", "\\" + break_point).replace("/", "/" + break_point)


class SettingsDialog(QDialog):
    saved = Signal()

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        pet_profile_dao: PetProfileDAO,
        activity_dao: ActivitySessionDAO,
        category_dao: AppCategoryDAO,
        memory_store: MemoryStore,
        reminder_dao: ReminderDAO,
        jobs: ReminderJobs,
        user_id: int,
        intimacy: Any = None,
        memory_book: Any = None,
        reset: Any = None,
        parent: QWidget | None = None,
    ):
        super().__init__(parent, Qt.Window)
        self._config = config
        self._bus = bus
        self._pet_profile_dao = pet_profile_dao
        self._activity_dao = activity_dao
        self._category_dao = category_dao
        self._memory_store = memory_store
        self._reminder_dao = reminder_dao
        self._jobs = jobs
        self._user_id = user_id
        # 亲密度 / 记忆簿（B 养成）：不传就是没有这一块，界面跟以前一样。
        # 两个各自独立——只传其中一个也不会塌。星念（_star_cards）在
        # v2-life-card-star 砍掉了：设置页不再显示，参数也不再收。
        self._intimacy = intimacy
        self._memory_book = memory_book
        # 清空记忆与好感度（v2-reset-state）：不传就不显示那块按钮，
        # 这一页跟以前一模一样（别的页面/测试都还照老样子构造这个对话框）。
        self._reset = reset

        self.setObjectName("SettingsDialog")
        self.setStyleSheet(PANEL_QSS)
        self.setWindowTitle("设置")
        self.resize(520, 640)

        layout = QVBoxLayout(self)
        tabs = QTabWidget(self)
        # 每一页都套一层滚动区（指令 26 ①）：以前页面比窗口高，多出来的部分是被
        # 直接切掉的，没有滚动条也够不着——记忆管理页堆了亲密度 / 记忆簿 /
        # 记忆 / 提醒四大块，正是被切得最狠的那页。套上之后页面再长也能滚到底。
        tabs.addTab(self._scrolled(self._build_llm_tab()), "模型")
        tabs.addTab(self._scrolled(self._build_pet_tab()), "宠物")
        tabs.addTab(self._scrolled(self._build_memory_tab()), "记忆管理")
        tabs.addTab(self._scrolled(self._build_reminder_tab()), "提醒")
        tabs.addTab(self._scrolled(self._build_pomodoro_tab()), "番茄钟")
        tabs.addTab(self._scrolled(self._build_activity_tab()), "活动")
        tabs.addTab(self._scrolled(self._build_playmate_tab()), "陪玩")
        tabs.addTab(self._scrolled(self._build_work_tab()), "工作")
        tabs.addTab(self._scrolled(self._build_notes_tab()), "笔记")
        tabs.addTab(self._scrolled(self._build_knowledge_tab()), "知识库")
        layout.addWidget(tabs, 1)

        self._status = QLabel("", self)
        self._status.setObjectName("MutedLabel")
        self._status.setWordWrap(True)
        layout.addWidget(self._status)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        cancel = QPushButton("取消", self)
        cancel.clicked.connect(self.reject)
        buttons.addWidget(cancel)
        save = QPushButton("保存", self)
        save.setObjectName("PrimaryButton")
        save.clicked.connect(self._save)
        buttons.addWidget(save)
        layout.addLayout(buttons)

        # 记忆管理页要跟着后台变化刷新（指令 19 ②）
        bus.memory_changed.connect(self.refresh_memories)
        bus.memory_changed.connect(self.refresh_intimacy)
        bus.reminder_changed.connect(self.refresh_reminders)

        self._loading = False   # 回填配置期间为 True：不要触发"改性格就重置风格"的逻辑
        self._saved_hint = ""   # 保存成功那行提示的附注（笔记路径退回默认之类），由 _collect_fields 填
        self._load_from_config()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        # 先按屏幕九成封顶定尺寸、再居中（指令 26 ①；指令 19 ① 那条"别顶出屏幕"
        # 由封顶直接覆盖掉，不用再夹一次位置）
        fit_dialog_on_screen(self)

    # ---------- 界面 ----------

    def _scrolled(self, page: QWidget) -> QScrollArea:
        """把一页装进滚动区：内容比窗口高时出现竖向滚动条，横向不要。

        两处讲究：
        1. `setWidgetResizable(True)`——窗口比内容大时页面铺满视口（跟以前一样），
           窗口比内容小时靠视口裁、滚动条滚；
        2. 去掉边框、横向条常关——设置页里没有需要横着看的东西，多一条横条只是噪音。
        """
        area = QScrollArea(self)
        area.setWidgetResizable(True)
        area.setFrameShape(QFrame.NoFrame)
        area.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # 滚动区的视口（和它托着的页面）会自己刷一层浅灰底（实测 #efefef），跟这一页原来
        # 的白底对不上，套上去就多出一块灰。只把**这两层**设成透明，让底下的页签面板透上来；
        # 选择器写成两级是有意的——再往里的控件（列表、标签）就选不中了，它们的底色不动。
        # （试过 setAutoFillBackground(False)，不管用，视口是走样式表刷的，实测。）
        area.setStyleSheet("QScrollArea > QWidget > QWidget { background: transparent; }")
        area.setWidget(page)
        return area

    def _build_llm_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._provider_combo = QComboBox(page)
        self._provider_combo.addItems(list(self._config.llm.providers.keys()))
        self._provider_combo.currentTextChanged.connect(self._load_provider)
        form.addRow("当前使用的模型", self._provider_combo)

        self._protocol_label = QLabel("-", page)
        self._protocol_label.setObjectName("MutedLabel")
        form.addRow("接入协议", self._protocol_label)

        self._base_url_edit = QLineEdit(page)
        form.addRow("接口地址", self._base_url_edit)

        self._model_edit = QLineEdit(page)
        form.addRow("模型名", self._model_edit)

        self._temperature_spin = QDoubleSpinBox(page)
        self._temperature_spin.setRange(0.0, 2.0)
        self._temperature_spin.setSingleStep(0.1)
        form.addRow("温度（越高越活泼）", self._temperature_spin)

        self._key_edit = QLineEdit(page)
        self._key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("API Key", self._key_edit)

        self._key_hint = QLabel("", page)
        self._key_hint.setObjectName("MutedLabel")
        self._key_hint.setWordWrap(True)
        form.addRow("", self._key_hint)

        self._test_button = QPushButton("测试连接", page)
        self._test_button.clicked.connect(self._test_connection)
        form.addRow("", self._test_button)
        return page

    def _build_pet_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._name_edit = QLineEdit(page)
        form.addRow("宠物名字", self._name_edit)

        self._persona_combo = QComboBox(page)
        self._persona_combo.addItems(PERSONA_KEYS)
        self._persona_combo.currentTextChanged.connect(self._on_persona_changed)
        form.addRow("性格", self._persona_combo)

        self._persona_hint = QLabel("", page)
        self._persona_hint.setObjectName("MutedLabel")
        self._persona_hint.setWordWrap(True)
        form.addRow("", self._persona_hint)

        self._persona_prompt_edit = QPlainTextEdit(page)
        self._persona_prompt_edit.setPlaceholderText(
            "只有上面选「自定义」时才用这段：例如 一只话不多但很会做饭的猫，喜欢哼歌"
        )
        self._persona_prompt_edit.setFixedHeight(64)
        form.addRow("自定义人设", self._persona_prompt_edit)

        self._user_call_combo = QComboBox(page)
        self._user_call_combo.setEditable(True)
        self._user_call_combo.addItems(USER_CALL_CHOICES)
        form.addRow("它怎么称呼你", self._user_call_combo)

        self._user_call_hint = QLabel("", page)
        self._user_call_hint.setObjectName("MutedLabel")
        self._user_call_hint.setWordWrap(True)
        form.addRow("", self._user_call_hint)

        self._actions_check = QCheckBox("说话带括号动作描写，例如（尾巴扫过手背）", page)
        form.addRow("说话风格", self._actions_check)

        self._kaomoji_check = QCheckBox("说话带颜文字，例如 (=^･ω･^=)", page)
        form.addRow("", self._kaomoji_check)

        self._hover_check = QCheckBox("鼠标停在它身上时，扭头看你 + 冒个小气泡", page)
        self._hover_check.setToolTip("单击它就能聊；这个只是停下来看它一眼的小反应，可以关掉")
        form.addRow("呼出提示", self._hover_check)

        self._hover_sec_spin = QDoubleSpinBox(page)
        self._hover_sec_spin.setRange(0.2, 5.0)
        self._hover_sec_spin.setSingleStep(0.1)
        self._hover_sec_spin.setSuffix(" 秒")
        form.addRow("停留多久才提示", self._hover_sec_spin)

        note = QLabel(
            "性格会同时影响它说话的风格与行为倾向（行为倾向要等行为库上线后生效）。"
            "改完立刻见效，不用重启，也不会清空已经记住的事。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _build_memory_tab(self) -> QWidget:
        """记忆管理（指令 19 ②）：主界面不再显示记忆/提醒列表，要清理时来这儿。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)

        note = QLabel(
            "这些是它记住的事与设下的提醒。平时不用管，想清理时在这里删掉就行。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        layout.addWidget(note)

        # 亲密度（B+ 语义阶段）：报"走到这条路的哪儿了"，仍然不叫"好感度"
        if self._intimacy is not None:
            layout.addWidget(self._sub_title("你们处到哪儿了"))
            self._stage_label = QLabel("-", page)
            self._stage_label.setObjectName("MutedLabel")
            self._stage_label.setWordWrap(True)
            layout.addWidget(self._stage_label)

            # 五档刻度：到了的那几档点亮成主色，没到的灰着。
            # 用富文本而不是五个控件——刻度只是"看一眼走到哪"的装饰，
            # 不值得为它铺一排 widget，也不该各自去抢焦点。
            self._stage_marks = QLabel(page)
            self._stage_marks.setTextFormat(Qt.RichText)
            self._stage_marks.setWordWrap(True)
            layout.addWidget(self._stage_marks)

            # 档内进度条：不是"总进度"，是"离下一档还差多少"。
            # 门槛是累计经验（0/30/100/220/400），所以每一档的条都从 0 重新走。
            self._stage_bar = QProgressBar(page)
            self._stage_bar.setTextVisible(True)
            layout.addWidget(self._stage_bar)

            self._nickname_edit = QLineEdit(page)
            self._nickname_edit.setPlaceholderText("留空就是没给过")
            layout.addWidget(self._nickname_edit)
            nickname_hint = QLabel(
                "私密称呼：只在你们俩之间用的叫法。处到最亲近那档，它才会这么叫你。",
                page,
            )
            nickname_hint.setObjectName("MutedLabel")
            nickname_hint.setWordWrap(True)
            layout.addWidget(nickname_hint)

        # 记忆簿：页头是那句阶段标签，底下一天一段，每段带当天的原话
        if self._memory_book is not None:
            layout.addWidget(self._sub_title("记忆簿"))
            self._book_list = QListWidget(page)
            self._book_list.setWordWrap(True)
            layout.addWidget(self._book_list, 1)

        if self._intimacy is not None or self._memory_book is not None:
            self.refresh_intimacy()

        layout.addWidget(self._sub_title("它记着的"))
        self._memory_list = QListWidget(page)
        self._memory_list.setWordWrap(True)
        layout.addWidget(self._memory_list, 1)
        forget = QPushButton("忘掉选中的这条", page)
        forget.clicked.connect(self._forget_memory)
        layout.addWidget(forget)

        layout.addWidget(self._sub_title("它设下的提醒"))
        self._reminder_list = QListWidget(page)
        self._reminder_list.setWordWrap(True)
        layout.addWidget(self._reminder_list, 1)
        cancel = QPushButton("取消选中的提醒", page)
        cancel.clicked.connect(self._cancel_reminder)
        layout.addWidget(cancel)

        # 一键清空（v2-reset-state）：上面那些是"一条一条删"，这个是"整个归零"。
        # 放在这一页最底下，跟"清空活动历史"同一个位置习惯——破坏性的按钮不跟
        # 日常操作混在一排。
        if self._reset is not None:
            layout.addWidget(self._sub_title("从零开始"))
            hint = QLabel(
                "清掉它记着的事和你们处到哪一档，回到刚认识那天的样子。"
                "卡片、打卡、提醒、活动历史、星念、你的画像、配置和知识库都不动。"
                "清掉就找不回来了，没有回收站。",
                page,
            )
            hint.setObjectName("MutedLabel")
            hint.setWordWrap(True)
            layout.addWidget(hint)
            self._reset_button = QPushButton("清空记忆与好感度", page)
            self._reset_button.clicked.connect(self._clear_memory)
            layout.addWidget(self._reset_button)

        self.refresh_memories()
        self.refresh_reminders()
        return page

    def refresh_intimacy(self) -> None:
        """把亲密度 / 记忆簿两块刷一遍。没接哪块就跳过哪块。"""
        self._refresh_stage()
        self._refresh_book()

    def _refresh_stage(self) -> None:
        """亲密度：一句话 + 五档刻度 + 档内进度条。"""
        if self._intimacy is None or not hasattr(self, "_stage_label"):
            return
        self._stage_label.setText(self._intimacy.stage_line())
        data = self._intimacy.progress()

        # 刻度：到了的点亮，没到的灰着。用 · 连起来，一眼能数出走到第几个
        marks = []
        for name, reached in data["stages"]:
            color = ACCENT if reached else TEXT_MUTED
            weight = "600" if reached else "400"
            marks.append(f'<span style="color:{color};font-weight:{weight}">{name}</span>')
        self._stage_marks.setText("　·　".join(marks))

        # 进度条：满级（span=0）时不给一根永远空着的条，直接填满并改文案
        if data["span"] <= 0:
            self._stage_bar.setRange(0, 1)
            self._stage_bar.setValue(1)
            self._stage_bar.setFormat("已经是最亲近的了")
        else:
            self._stage_bar.setRange(0, data["span"])
            self._stage_bar.setValue(data["done"])
            self._stage_bar.setFormat(f"{data['done']} / {data['span']}")

    def _refresh_book(self) -> None:
        """记忆簿：页头一句阶段话，底下一段一天，每段带当天的原话明细。"""
        if self._memory_book is None or not hasattr(self, "_book_list"):
            return
        self._book_list.clear()
        # 页头：这句标的是"这段关系现在到哪一档"，不是这一天发生了什么
        header = self._memory_book.header()
        if header:
            head_item = QListWidgetItem(f"— {header} —")
            head_item.setForeground(QColor(TEXT_MUTED))
            self._book_list.addItem(head_item)
        for page in self._memory_book.timeline(limit=7):
            self._book_list.addItem(
                QListWidgetItem(f"{page.get('date', '')}　{page.get('summary', '')}")
            )
            # 原话：缩进一格挂在日期底下。时间和关键词跟在后面——
            # 关键词本来就是这一条被记下来的由头，摆回去比藏起来有用
            for entry in page.get("entries") or []:
                note = "　".join(b for b in (
                    str(entry.get("time") or "")[5:16],
                    "、".join(entry.get("tags") or []),
                ) if b)
                text = str(entry.get("text") or "")
                line = f"　　· {text}" + (f"　（{note}）" if note else "")
                item = QListWidgetItem(line)
                item.setForeground(QColor(TEXT_MUTED))
                self._book_list.addItem(item)

    def _sub_title(self, text: str) -> QLabel:
        label = QLabel(text, self)
        label.setObjectName("MutedLabel")
        return label

    def refresh_memories(self) -> None:
        self._memory_list.clear()
        for memory in self._memory_store.recent(50):
            kind = MEMORY_KIND_TEXT.get(memory.kind, memory.kind)
            # 正文里可能留着"明早"这类相对说法，列表里按今天重新说一遍
            item = QListWidgetItem(f"[{kind}] {memory_text(memory)}")
            item.setData(Qt.UserRole, memory.id)
            item.setToolTip(
                f"记住于 {memory.created_at.strftime('%Y-%m-%d %H:%M')}，"
                f"重要度 {memory.importance:.2f}"
            )
            self._memory_list.addItem(item)

    def refresh_reminders(self) -> None:
        self._reminder_list.clear()
        for reminder in self._reminder_dao.list_all():
            status = REMINDER_STATUS_TEXT.get(reminder.status, reminder.status)
            item = QListWidgetItem(
                f"{reminder.trigger_at.strftime('%m-%d %H:%M')} · "
                f"{reminder_text(reminder)} · {status}"
            )
            item.setData(Qt.UserRole, reminder.id)
            self._reminder_list.addItem(item)

    def _forget_memory(self) -> None:
        item = self._memory_list.currentItem()
        if item is None:
            self._status.setText("先在上面选一条记忆")
            return
        self._memory_store.forget(int(item.data(Qt.UserRole)))
        self.refresh_memories()
        self._status.setText("已经忘掉这条了")

    def _cancel_reminder(self) -> None:
        item = self._reminder_list.currentItem()
        if item is None:
            self._status.setText("先在上面选一条提醒")
            return
        self._jobs.cancel(int(item.data(Qt.UserRole)))
        self._status.setText("这条提醒取消啦")

    def _build_reminder_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        self._quiet_start = QTimeEdit(page)
        self._quiet_start.setDisplayFormat("HH:mm")
        form.addRow("免打扰开始", self._quiet_start)

        self._quiet_end = QTimeEdit(page)
        self._quiet_end.setDisplayFormat("HH:mm")
        form.addRow("免打扰结束", self._quiet_end)

        self._rate_spin = QSpinBox(page)
        self._rate_spin.setRange(1, 20)
        form.addRow("每小时最多提醒次数", self._rate_spin)

        self._autostart_check = QCheckBox("开机自动启动宠物", page)
        form.addRow("", self._autostart_check)

        note = QLabel(
            "落在免打扰时段、或超过每小时上限的提醒不会弹屏，只在“提醒”列表里标为已静音，"
            "软件没开着时到点的提醒标为已错过。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _build_pomodoro_tab(self) -> QWidget:
        page = QWidget(self)
        form = QFormLayout(page)

        # ---- 自动番茄钟（v2-focus-web ①）：前台落在工作/网课类应用或工作网页上时自己起表 ----
        # 这三项原先只有 config.yaml 里有，界面上一个字都没有——用户想改时长、想关掉、
        # 想让自家那套办公系统被认出来，都没有地方点。
        self._auto_focus_check = QCheckBox("前台是工作/网课类应用或网页时，自动开始", page)
        form.addRow("自动专注", self._auto_focus_check)

        self._auto_focus_spin = self._minutes_spin(page, 1, 180)
        form.addRow("自动专注一轮", self._auto_focus_spin)

        self._focus_words_edit = QLineEdit(page)
        self._focus_words_edit.setPlaceholderText("公司OA, 工单系统（多个词用逗号隔开）")
        form.addRow("工作站点词", self._focus_words_edit)

        focus_note = QLabel(
            "「工作站点词」是给浏览器里的工作网页用的：标题里出现这些词就按专注算。\n"
            "浏览器标题里没有网址，只有页面标题，所以认的是标题里的字；"
            "内置词表收的是常见网课/创作/办公系统，自家那套系统用它认不出来时填这里。",
            page,
        )
        focus_note.setObjectName("MutedLabel")
        focus_note.setWordWrap(True)
        form.addRow("", focus_note)

        self._focus_spin = self._minutes_spin(page, 1, 180)
        form.addRow("专注时长", self._focus_spin)

        self._short_spin = self._minutes_spin(page, 1, 60)
        form.addRow("短休时长", self._short_spin)

        self._long_spin = self._minutes_spin(page, 1, 120)
        form.addRow("长休时长", self._long_spin)

        self._long_every_spin = QSpinBox(page)
        self._long_every_spin.setRange(1, 12)
        self._long_every_spin.setSuffix(" 个番茄")
        form.addRow("每几个番茄长休", self._long_every_spin)

        self._pomodoro_mute_check = QCheckBox("专注期间屏蔽其他提醒", page)
        form.addRow("", self._pomodoro_mute_check)

        self._pomodoro_sound_check = QCheckBox("阶段切换时提示音", page)
        form.addRow("", self._pomodoro_sound_check)

        note = QLabel(
            "时长以分钟为单位。改动从下一个阶段开始生效，不会打断正在进行的计时；"
            "“专注期间屏蔽其他提醒”改了会立刻生效。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    @staticmethod
    def _minutes_spin(parent: QWidget, minimum: int, maximum: int) -> QSpinBox:
        spin = QSpinBox(parent)
        spin.setRange(minimum, maximum)
        spin.setSuffix(" 分钟")
        return spin

    def _build_activity_tab(self) -> QWidget:
        """活动页（指令 5 第 5 节隐私要求 + 指令 6 第 3 节提醒开关）。"""
        page = QWidget(self)
        layout = QVBoxLayout(page)
        form = QFormLayout()

        self._activity_enabled_check = QCheckBox("记录前台应用（只记应用名与用时，不读窗口标题）", page)
        form.addRow("", self._activity_enabled_check)

        self._activity_retention_spin = QSpinBox(page)
        self._activity_retention_spin.setRange(1, 365)
        self._activity_retention_spin.setSuffix(" 天")
        form.addRow("记录保留", self._activity_retention_spin)

        self._activity_blacklist_edit = QPlainTextEdit(page)
        self._activity_blacklist_edit.setPlaceholderText(
            "一行一个进程名，例如 keepass.exe。名单里的应用完全不记录。"
        )
        self._activity_blacklist_edit.setFixedHeight(60)
        form.addRow("不记录的应用", self._activity_blacklist_edit)

        self._activity_clear_button = QPushButton("清空活动历史", page)
        self._activity_clear_button.clicked.connect(self._clear_activity)
        form.addRow("", self._activity_clear_button)
        layout.addLayout(form)

        hint = QLabel(
            "应用分类：认不出来的应用默认算「其他」，可以在这里指定；记录只存在本机。",
            page,
        )
        hint.setObjectName("MutedLabel")
        hint.setWordWrap(True)
        layout.addWidget(hint)

        self._category_table = QTableWidget(0, 2, page)
        self._category_table.setHorizontalHeaderLabels(["应用", "类别"])
        self._category_table.verticalHeader().setVisible(False)
        header = self._category_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        header.setSectionResizeMode(1, QHeaderView.ResizeToContents)
        layout.addWidget(self._category_table, 1)
        return page

    def _build_playmate_tab(self) -> QWidget:
        """陪玩页：挑一个「能看图」的模型，配好截屏节奏。

        陪玩借用的那家供应商，接口地址和 Key 跟「模型」页共用，所以这里只要填模型名。
        """
        page = QWidget(self)
        form = QFormLayout(page)

        self._vision_provider_combo = QComboBox(page)
        self._vision_provider_combo.addItems(list(self._config.llm.providers.keys()))
        self._vision_provider_combo.currentTextChanged.connect(self._load_vision_provider)
        form.addRow("借哪家的接口", self._vision_provider_combo)

        self._vision_model_edit = QLineEdit(page)
        self._vision_model_edit.setPlaceholderText("能看图的模型名，例如 qwen-vl-max、glm-4v-plus")
        form.addRow("看图模型名", self._vision_model_edit)

        self._vision_key_edit = QLineEdit(page)
        self._vision_key_edit.setEchoMode(QLineEdit.Password)
        form.addRow("API Key", self._vision_key_edit)

        self._vision_key_hint = QLabel("", page)
        self._vision_key_hint.setObjectName("MutedLabel")
        self._vision_key_hint.setWordWrap(True)
        form.addRow("", self._vision_key_hint)

        self._vision_test_button = QPushButton("测试看图（截当前屏幕发给它）", page)
        self._vision_test_button.clicked.connect(self._test_vision)
        form.addRow("", self._vision_test_button)

        self._vision_sample_spin = QSpinBox(page)
        self._vision_sample_spin.setRange(1, 60)
        self._vision_sample_spin.setSuffix(" 秒")
        form.addRow("多久看一眼画面", self._vision_sample_spin)

        self._vision_threshold_spin = QDoubleSpinBox(page)
        self._vision_threshold_spin.setRange(1.0, 100.0)
        self._vision_threshold_spin.setSingleStep(1.0)
        form.addRow("画面变化超过多少才分析", self._vision_threshold_spin)

        self._vision_gap_spin = QSpinBox(page)
        self._vision_gap_spin.setRange(5, 600)
        self._vision_gap_spin.setSuffix(" 秒")
        form.addRow("两次提示至少隔", self._vision_gap_spin)

        self._vision_width_spin = QSpinBox(page)
        self._vision_width_spin.setRange(320, 2560)
        self._vision_width_spin.setSingleStep(64)
        self._vision_width_spin.setSuffix(" 像素")
        form.addRow("发给模型前缩到", self._vision_width_spin)

        note = QLabel(
            "陪玩要一个能看图的模型（普通对话模型不行），它借用上面那家的接口地址与 Key。\n"
            "打开：跟宠物说「开始陪玩」；关掉：说「不用陪玩了」。\n"
            "截图只在本机内存里比一下变化，画面变了才发给模型，不发就不花钱。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)

        # ---- 画面情绪（S4/S5）----
        # 放在这一页是因为它跟上面那条是同一种事：都要把画面交给一个模型看。
        # 区别在钱和隐私——本地档不出本机、不花钱；云端档要往外发。
        from life.screen.mood_cloud import CONSENT_NOTICE      # 局部导入，别拖慢设置窗

        self._mood_combo = QComboBox(page)
        self._mood_combo.addItem("关：不判画面情绪", "off")
        self._mood_combo.addItem("本地：本机离线判（默认）", "local")
        self._mood_combo.addItem("云端：把这一帧发出去判", "cloud")
        self._mood_combo.currentIndexChanged.connect(self._on_mood_changed)
        form.addRow("画面情绪", self._mood_combo)

        self._mood_min_spin = QDoubleSpinBox(page)
        self._mood_min_spin.setRange(0.30, 0.99)
        self._mood_min_spin.setSingleStep(0.01)
        self._mood_min_spin.setDecimals(2)
        form.addRow("本地判到多少分才开口", self._mood_min_spin)

        # 告知不做成"弹一次就没了"的模态框：那玩意儿点掉就看不见了，而这档要的
        # 恰恰是"随时能看见自己授权了什么"。所以在这里常驻，原样显示，不缩写。
        self._mood_notice = QLabel(CONSENT_NOTICE, page)
        self._mood_notice.setObjectName("MutedLabel")
        self._mood_notice.setWordWrap(True)
        form.addRow("", self._mood_notice)

        self._mood_cloud_check = QCheckBox("我明白上面写的，允许把画面发出去", page)
        form.addRow("", self._mood_cloud_check)
        return page

    def _on_mood_changed(self) -> None:
        """只有选中云端档，才亮出告知与授权这两行。

        离开云端档就等于撤销授权（勾选框自己清掉）——这档宁可每次都重新点一遍，
        也别留一个"上次好像勾过"的模糊状态。
        """
        cloud = self._mood_combo.currentData() == "cloud"
        self._mood_notice.setVisible(cloud)
        self._mood_cloud_check.setEnabled(cloud)
        if not cloud:
            self._mood_cloud_check.setChecked(False)

    def _build_work_tab(self) -> QWidget:
        """工作页：身份切换（调试用）+ 宠物点评开关。

        真正的账号体系还没做，管理员 / 员工先靠这一档切：切完重新打开工作模块，
        员工看不到「编辑底层连接」，自建卡的新建 / 编辑 / 删除两边都能用。
        点评开关原来的位置在工作页右上角（勾选框），指令 23 第 3 项挪到这里，默认关。
        """
        page = QWidget(self)
        form = QFormLayout(page)

        self._role_combo = QComboBox(page)
        self._role_combo.addItem("管理员", WORK_ROLE_ADMIN)
        self._role_combo.addItem("员工", WORK_ROLE_USER)
        form.addRow("身份切换（调试用）", self._role_combo)

        self._comment_check = QCheckBox("宠物点评工作结果", page)
        self._comment_check.setToolTip(
            "打开后，工作模块里出的结果末尾会带一句宠物自己的看法；关掉就只出报告"
        )
        form.addRow("结果末尾带点评", self._comment_check)

        note = QLabel(
            "身份切换只影响工作模块：选「员工」后，工作模块里看不到「编辑底层连接」（底层流程不对员工开放）。\n"
            "切换后重新打开工作模块生效；身份与点评开关都存进配置，重启保持。点评默认关。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _build_notes_tab(self) -> QWidget:
        """笔记页（v2-notes-path）：抓屏笔记开不开、隔多久抓、笔记存哪儿。

        真机反馈是"抓屏笔记不知存哪"——原先这三项只有配置里有，界面上一处都摸不着，
        连开关都是关着的没人能打开。现在三样都摆出来，落点默认是项目上一级的
        「笔记」文件夹（跟 存档 / 资料 并排，不埋在程序目录里），可以改、可以点开看。
        """
        page = QWidget(self)
        form = QFormLayout(page)

        self._notes_enabled_check = QCheckBox("打开抓屏笔记", page)
        self._notes_enabled_check.setToolTip(
            "只在网课 / 工作 / 创作类软件在前台时抓屏认字，别的场景一拍都不抓"
        )
        form.addRow("抓屏笔记", self._notes_enabled_check)

        self._notes_interval_spin = QSpinBox(page)
        self._notes_interval_spin.setRange(1, 120)
        self._notes_interval_spin.setSuffix(" 分钟")
        form.addRow("隔多久抓一次", self._notes_interval_spin)

        path_row = QWidget(page)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self._notes_dir_edit = QLineEdit(path_row)
        self._notes_dir_edit.setPlaceholderText(str(self._config.notes_dir_default))
        path_layout.addWidget(self._notes_dir_edit, 1)
        browse = QPushButton("浏览…", path_row)
        browse.clicked.connect(self._browse_notes_dir)
        path_layout.addWidget(browse)
        open_dir = QPushButton("打开文件夹", path_row)
        open_dir.clicked.connect(self._open_notes_dir)
        path_layout.addWidget(open_dir)
        form.addRow("笔记保存到", path_row)

        self._notes_dir_hint = QLabel("", page)
        self._notes_dir_hint.setObjectName("MutedLabel")
        self._notes_dir_hint.setWordWrap(True)
        form.addRow("", self._notes_dir_hint)
        # 这条提示要显示落点全路径，而路径里没有空格：Qt 折行找不到断点，整条路径会被当成
        # 一个"长单词"，标签的最小宽度就等于路径宽度（实测一条临时路径要 660px，整页跟着
        # 冒出横向滚动条）。塞零宽断行点只给折行机会，不占宽度、看不见（v2-notes-path）。

        note = QLabel(
            "笔记是一天一个 Markdown 文件（<日期>.md），只写这个文件夹；\n"
            "原截图认完字就删，不进数据库、不进画像库。留空 = 用默认文件夹。\n"
            "已经存下的旧笔记不会被搬走——换个文件夹只影响以后新记的。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _build_knowledge_tab(self) -> QWidget:
        """知识库页（v2-kb-init）：放 .md/.txt 文档的地方，宠物问答会去翻它。

        原先「知识库」只在 `config.yaml` 里有个 `knowledge.dir`，**设置页一个字都没有**
        ——用户把文档放进了文件夹却不知道宠物该从哪儿读，出问题也没地方看一眼。
        这一页就补那个入口：目录可改、可浏览、可打开，下面直接说清"放什么、多久生效"。
        """
        page = QWidget()
        form = QFormLayout(page)
        form.setLabelAlignment(Qt.AlignRight | Qt.AlignVCenter)

        intro = QLabel(
            "把公司制度、说明书、手册这类文档（.md / .txt）丢进这个文件夹，\n"
            "问相关问题时宠物会**照着文档回答**，而不是凭印象编。",
            page,
        )
        intro.setObjectName("MutedLabel")
        intro.setWordWrap(True)
        form.addRow("", intro)

        path_row = QWidget(page)
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        self._kb_dir_edit = QLineEdit(path_row)
        self._kb_dir_edit.setPlaceholderText(str(self._config.knowledge_dir_default))
        path_layout.addWidget(self._kb_dir_edit, 1)
        kb_browse = QPushButton("浏览…", path_row)
        kb_browse.clicked.connect(self._browse_kb_dir)
        path_layout.addWidget(kb_browse)
        kb_open = QPushButton("打开文件夹", path_row)
        kb_open.clicked.connect(self._open_kb_dir)
        path_layout.addWidget(kb_open)
        form.addRow("文档文件夹", path_row)

        self._kb_dir_hint = QLabel("", page)
        self._kb_dir_hint.setObjectName("MutedLabel")
        self._kb_dir_hint.setWordWrap(True)
        form.addRow("", self._kb_dir_hint)

        note = QLabel(
            "**放进去就能查**——检索是每次提问时现读文件夹的，不用等重启、也没有索引要刷。\n"
            "留空 = 用默认文件夹。填的路径用不了（盘没挂、名字不对）会自动退回默认那个。\n"
            "只读不写：宠物不会往这个文件夹里放东西，也不会改你的文档。",
            page,
        )
        note.setObjectName("MutedLabel")
        note.setWordWrap(True)
        form.addRow("", note)
        return page

    def _browse_kb_dir(self) -> None:
        """选一个文件夹当知识库；选完立刻回填（保存还是要按「保存」）。"""
        start = self._kb_dir_edit.text().strip() or str(self._config.knowledge_dir_default)
        chosen = QFileDialog.getExistingDirectory(self, "选一个文件夹放知识库文档", start)
        if chosen:
            self._kb_dir_edit.setText(chosen)

    def _open_kb_dir(self) -> None:
        """在文件管理器里打开知识库文件夹：用户"我把文档放哪了"最直接的答案。"""
        text = self._kb_dir_edit.text().strip()
        target = self._config.abs_path(text) if text else self._config.knowledge_dir_default
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._status.setText(f"这个文件夹建不出来：{exc}")
            return
        try:
            if hasattr(os, "startfile"):          # Windows
                os.startfile(str(target))         # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            self._status.setText(f"叫文件管理器打开失败：{exc}")

    def _kb_docs_hint(self) -> str:
        """这一行提示：现在的落点 + 里面有多少份认得出的文档。

        份数当场扫一遍目录——用户"我放进去了怎么没反应"的时候，这一行就能回话：
        "0 份文档"说明放错文件夹了，路径那一栏立刻就成了要改的东西。
        """
        root = self._config.knowledge_dir
        try:
            count = sum(
                1 for item in root.iterdir()
                if item.is_file() and item.suffix.lower() in (".md", ".txt")
            )
        except OSError:
            count = 0
        return f"现在的落点：{wrap_path(str(root))}　（里面有 {count} 份 .md / .txt 文档）"

    def _browse_notes_dir(self) -> None:
        """选一个文件夹当笔记落点；选完立刻回填到输入框（保存还是要按「保存」）。"""
        start = self._notes_dir_edit.text().strip() or str(self._config.notes_dir_default)
        chosen = QFileDialog.getExistingDirectory(self, "选一个文件夹放笔记", start)
        if chosen:
            self._notes_dir_edit.setText(chosen)

    def _open_notes_dir(self) -> None:
        """在文件管理器里打开笔记文件夹：真机那句"不知存哪"最直接的答案。

        输入框里填了就用它，没填就用默认那个；文件夹还不存在就先建出来——
        点了"打开文件夹"却什么都没发生，比不开更让人困惑。
        """
        text = self._notes_dir_edit.text().strip()
        target = self._config.abs_path(text) if text else self._config.notes_dir_default
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._status.setText(f"这个文件夹建不出来：{exc}")
            return
        try:
            if hasattr(os, "startfile"):          # Windows
                os.startfile(str(target))         # noqa: S606
            elif sys.platform == "darwin":
                subprocess.Popen(["open", str(target)])
            else:
                subprocess.Popen(["xdg-open", str(target)])
        except OSError as exc:
            self._status.setText(f"叫文件管理器打开失败：{exc}")

    def _load_categories(self) -> None:
        """列出最近一周出现过的应用，让用户给它们指定类别。"""
        table = self._category_table
        table.setRowCount(0)
        overrides = self._category_dao.get_map()
        for proc in self._activity_dao.procs_since(7):
            row = table.rowCount()
            table.insertRow(row)
            item = QTableWidgetItem(friendly_name(proc))
            item.setData(Qt.UserRole, proc)   # 写回时要用原始进程名
            item.setFlags(item.flags() & ~Qt.ItemIsEditable)
            table.setItem(row, 0, item)
            combo = QComboBox(table)
            combo.addItems(CATEGORIES)
            combo.setCurrentText(overrides.get(proc.lower(), builtin_category(proc)))
            table.setCellWidget(row, 1, combo)
        if table.rowCount() == 0:
            self._status.setText("还没有活动记录，用一段时间后这里会列出常用应用")

    def _save_categories(self) -> None:
        """把表里的指定写库；和内置分类一致的就不用存。"""
        for row in range(self._category_table.rowCount()):
            item = self._category_table.item(row, 0)
            combo = self._category_table.cellWidget(row, 1)
            if item is None or combo is None:
                continue
            proc = item.data(Qt.UserRole)
            category = combo.currentText()
            if category == builtin_category(proc):
                self._category_dao.remove(proc)
            else:
                self._category_dao.set_category(self._user_id, proc, category)

    def _clear_activity(self) -> None:
        answer = QMessageBox.question(
            self, "清空活动历史", "确定要把记录下来的前台应用历史全部删掉吗？"
        )
        if answer != QMessageBox.Yes:
            return
        self._activity_dao.clear_all()
        self._load_categories()
        self._status.setText("活动历史已清空")

    def _clear_memory(self) -> None:
        """清空记忆与好感度：**问两次**才动手。

        一次确认就够的话，误点一下的代价是"处了几个月的关系归零"，而且没有回收站。
        所以第一次把清单摊开（真数出来会清掉多少条），第二次只剩一句"真的要清吗"——
        第二次那个对话框里不再重复清单，是因为要点"是"两回的人已经看过一遍了。
        两个框的默认键都是"否"：回车 / 手快都不至于把关系清掉。
        """
        report = self._reset.preview()
        if report.total() <= 0:
            self._status.setText("本来就没有记着的东西，不用清")
            return
        first = QMessageBox.question(
            self,
            "清空记忆与好感度",
            "会清掉这些：\n\n"
            f"{report.detail()}\n\n"
            "卡片、打卡、提醒、活动历史、情绪记录、星念、你的画像、配置和知识库都不动。\n\n"
            "要继续吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if first != QMessageBox.Yes:
            self._status.setText("没有清，什么都没动")
            return
        second = QMessageBox.warning(
            self,
            "最后确认一次",
            "清掉就找不回来了——它不会再记得你们聊过什么，也会回到刚认识那档。\n\n"
            "真的要清吗？",
            QMessageBox.Yes | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if second != QMessageBox.Yes:
            self._status.setText("没有清，什么都没动")
            return
        done = self._reset.reset()
        self.refresh_memories()
        self.refresh_intimacy()
        self._status.setText(f"记忆与好感度已清空：{done.plain()}")

    # ---------- 读取配置 ----------

    def _load_from_config(self) -> None:
        self._loading = True
        index = self._provider_combo.findText(self._config.llm.active)
        if index >= 0:
            self._provider_combo.setCurrentIndex(index)
        self._temperature_spin.setValue(self._config.llm.temperature)
        self._load_provider(self._provider_combo.currentText())

        self._name_edit.setText(self._config.pet.name)
        self._persona_combo.setCurrentText(self._config.pet.persona)
        self._persona_prompt_edit.setPlainText(self._config.pet.persona_prompt)
        self._user_call_combo.setCurrentText(self._config.pet.user_call)
        if hasattr(self, "_nickname_edit"):
            self._nickname_edit.setText(self._config.mind.intimate_nickname)
        self._actions_check.setChecked(self._config.pet.style_actions)
        self._kaomoji_check.setChecked(self._config.pet.style_kaomoji)
        self._hover_check.setChecked(self._config.pet.hover_hint)
        self._hover_sec_spin.setValue(self._config.pet.hover_hint_sec)
        self._on_persona_changed(self._persona_combo.currentText())

        start, end = self._config.schedule.quiet_hours
        self._quiet_start.setTime(QTime.fromString(start, "HH:mm"))
        self._quiet_end.setTime(QTime.fromString(end, "HH:mm"))
        self._rate_spin.setValue(self._config.schedule.max_notify_per_hour)
        self._autostart_check.setChecked(autostart.is_enabled())

        pomo = self._config.pomodoro
        self._focus_spin.setValue(pomo.focus_min)
        self._short_spin.setValue(pomo.short_break_min)
        self._long_spin.setValue(pomo.long_break_min)
        self._long_every_spin.setValue(pomo.long_break_every)
        self._pomodoro_mute_check.setChecked(pomo.mute_other_reminders)
        self._pomodoro_sound_check.setChecked(pomo.notify_sound)

        act = self._config.activity
        self._activity_enabled_check.setChecked(act.enabled)
        self._activity_retention_spin.setValue(act.retention_days)
        self._activity_blacklist_edit.setPlainText("\n".join(act.blacklist))
        self._load_categories()

        vis = self._config.vision
        index = self._vision_provider_combo.findText(vis.provider)
        if index >= 0:
            self._vision_provider_combo.setCurrentIndex(index)
        self._vision_model_edit.setText(vis.model)
        self._vision_sample_spin.setValue(vis.sample_sec)
        self._vision_threshold_spin.setValue(vis.change_threshold)
        self._vision_gap_spin.setValue(vis.min_gap_sec)
        self._vision_width_spin.setValue(vis.max_width)
        self._load_vision_provider(self._vision_provider_combo.currentText())

        # 画面情绪档位（先设档位，`_on_mood_changed` 会按档位决定告知与授权亮不亮；
        # 不是云端档就**不**回填授权框——配置里若留着上次的 true，也不给它显示成已授权）
        scr = self._config.screen
        mood_index = self._mood_combo.findData(scr.mood)
        self._mood_combo.setCurrentIndex(mood_index if mood_index >= 0 else 0)
        self._mood_min_spin.setValue(scr.mood_min_score)
        if scr.mood == "cloud":
            self._mood_cloud_check.setChecked(bool(scr.mood_cloud))

        # 自动番茄钟（v2-focus-web ①）：开关 / 一轮时长 / 自定义工作站点词
        self._auto_focus_check.setChecked(scr.focus_enabled)
        self._auto_focus_spin.setValue(scr.focus_minutes)
        self._focus_words_edit.setText("，".join(scr.focus_words))

        # 工作页：身份切换（调试用）与点评开关
        self._role_combo.setCurrentIndex(self._role_combo.findData(self._config.work.role))
        self._comment_check.setChecked(self._config.work.pet_comment)

        # 笔记页（v2-notes-path）：开关 / 间隔 / 落点
        notes = self._config.notes
        self._notes_enabled_check.setChecked(notes.enabled)
        self._notes_interval_spin.setValue(notes.interval_min)
        self._notes_dir_edit.setText(notes.dir)
        self._notes_dir_hint.setText(f"现在的落点：{wrap_path(str(self._config.notes_dir))}")

        # 知识库页（v2-kb-init）：只有一个落点，没有开关（检索本身由 rag 总闸管）
        self._kb_dir_edit.setText(self._config.knowledge.dir)
        self._kb_dir_hint.setText(self._kb_docs_hint())
        self._loading = False

    def _load_provider(self, name: str) -> None:
        if not name:
            return
        provider = self._config.llm.provider(name)
        self._protocol_label.setText(provider.protocol)
        self._base_url_edit.setText(provider.base_url)
        self._model_edit.setText(provider.model)

        env_name = self._config.llm.provider(name).api_key_ref
        from core.config import env_key_name  # 局部导入避免循环
        env_var = env_key_name(provider.api_key_ref)
        import os

        if os.environ.get(env_var):
            self._key_edit.setText("")
            self._key_edit.setPlaceholderText(f"已从环境变量 {env_var} 读取")
            self._key_hint.setText(
                f"当前 Key 来自环境变量 {env_var}（优先级最高）。\n"
                f"如需临时覆盖，在这里填入新的 Key 会存入 Windows 凭据管理器；"
                f"清空输入框则恢复使用环境变量。"
            )
        else:
            self._key_edit.setText(secret_get(provider.api_key_ref) or "")
            self._key_edit.setPlaceholderText("")
            self._key_hint.setText(
                f"Key 会存进 Windows 凭据管理器（凭据名 {provider.api_key_ref}），不写进配置文件。\n"
                f"也支持环境变量 {env_var}（优先级更高）。"
            )

    def _load_vision_provider(self, name: str) -> None:
        """陪玩借用的那家：显示它的 Key 从哪来（只是在界面上提示，不动当前使用的模型）。"""
        if not name:
            return
        from core.config import env_key_name

        import os

        ref = self._config.llm.provider(name).api_key_ref
        env_var = env_key_name(ref)
        if os.environ.get(env_var):
            self._vision_key_edit.setText("")
            self._vision_key_edit.setPlaceholderText(f"已从环境变量 {env_var} 读取")
            self._vision_key_hint.setText(f"这家已经配好 Key（环境变量 {env_var}），不用再填。")
        else:
            self._vision_key_edit.setText(secret_get(ref) or "")
            self._vision_key_edit.setPlaceholderText("")
            self._vision_key_hint.setText(
                f"看图要用这家的 Key；会存进 Windows 凭据管理器（凭据名 {ref}），不写进配置文件。"
            )

    def _on_persona_changed(self, key: str) -> None:
        """切换性格时刷新说明文字，并决定"自定义人设"能不能填。

        用户主动换性格时，顺手把说话风格的两个开关拨到这个性格的默认值；
        打开设置页回填的时候（_loading）不动，免得把用户自己调过的偏好冲掉。
        """
        preset = preset_of(key)
        if preset is not None:
            self._persona_hint.setText(
                f"{preset.blurb}（自称“{preset.self_call}”，默认称呼你为“{preset.user_call}”）"
            )
            self._user_call_hint.setText(
                f"留空就用这个性格自带的称呼“{preset.user_call}”，也可以自己填一个。"
            )
            if not self._loading:
                self._actions_check.setChecked(preset.actions)
                self._kaomoji_check.setChecked(preset.kaomoji)
        else:
            self._persona_hint.setText("人设由你自己写：下面那段文字会直接交给模型。")
            self._user_call_hint.setText("留空就用「你」，也可以自己填一个。")
        custom = key == CUSTOM_KEY
        self._persona_prompt_edit.setEnabled(custom)
        if custom and not self._loading and not self._persona_prompt_edit.toPlainText().strip():
            self._persona_prompt_edit.setFocus()

    # ---------- 测试连接 ----------

    def _test_connection(self) -> None:
        if not self._collect_fields():
            return
        self._test_button.setEnabled(False)
        self._status.setText("正在测试连接…")
        run_async(
            LLMFactory.test_connection,
            self._config,
            on_done=self._on_test_done,
            on_failed=self._on_test_failed,
        )

    def _on_test_done(self, reply: str) -> None:
        self._test_button.setEnabled(True)
        self._status.setText(f"连接正常，模型回复：{reply}")

    def _on_test_failed(self, exc: Exception) -> None:
        self._test_button.setEnabled(True)
        self._status.setText(f"连接失败：{exc}")

    def _test_vision(self) -> None:
        """截一帧当前屏幕发给看图模型，确认它真能看清画的是什么。"""
        if not self._collect_fields():
            return
        from life.playmate import grab_screen_jpeg

        image = grab_screen_jpeg(self._config)
        if not image:
            self._status.setText("没截到屏幕画面，稍后再试")
            return
        self._vision_test_button.setEnabled(False)
        self._status.setText("正在把当前屏幕发给看图模型…")
        run_async(
            LLMFactory.test_vision,
            self._config,
            image,
            on_done=self._on_vision_test_done,
            on_failed=self._on_vision_test_failed,
        )

    def _on_vision_test_done(self, reply: str) -> None:
        self._vision_test_button.setEnabled(True)
        text = reply.replace("\n", " ").strip()
        self._status.setText(f"看图正常，它说：{text[:120]}")

    def _on_vision_test_failed(self, exc: Exception) -> None:
        self._vision_test_button.setEnabled(True)
        self._status.setText(f"看图失败：{exc}")

    # ---------- 保存 ----------

    def _collect_fields(self) -> bool:
        """把界面上的内容写进内存里的 Config，并做校验，返回是否成功。"""
        self._saved_hint = ""           # 保存成功那行提示的附注（路径退回默认之类），每轮重算
        name = self._provider_combo.currentText()
        base_url = self._base_url_edit.text().strip()
        model = self._model_edit.text().strip()
        pet_name = self._name_edit.text().strip()
        persona = self._persona_combo.currentText().strip()
        persona_prompt = self._persona_prompt_edit.toPlainText().strip()
        user_call = self._user_call_combo.currentText().strip()

        if not pet_name:
            self._status.setText("宠物名字不能为空")
            return False
        if not base_url or not model:
            self._status.setText("接口地址和模型名都要填")
            return False
        if not persona:
            self._status.setText("性格不能为空")
            return False
        if persona == CUSTOM_KEY and not persona_prompt:
            self._status.setText("选了「自定义」性格，就要写一段人设描述")
            return False

        provider = self._config.llm.provider(name)
        api_key = self._key_edit.text().strip()
        if api_key:
            # 用户手动填了 -> 存凭据管理器（环境变量存在时仍以环境变量为准，但这里也存一份兜底）
            secret_set(provider.api_key_ref, api_key)
        else:
            # 输入框为空：如果当前显示的是环境变量占位符，说明用户想恢复用环境变量，清掉凭据
            from core.config import env_key_name
            import os
            if os.environ.get(env_key_name(provider.api_key_ref)):
                try:
                    import keyring
                    keyring.delete_password("desktoppet", provider.api_key_ref)
                except keyring.errors.PasswordDeleteError:
                    pass

        # 陪玩借用的那家：Key 也在这里能填（和「模型」页指向同一家时，两处是同一个凭据）
        vision_ref = self._config.llm.provider(
            self._vision_provider_combo.currentText()
        ).api_key_ref
        vision_key = self._vision_key_edit.text().strip()
        if vision_key:
            secret_set(vision_ref, vision_key)

        provider.base_url = base_url
        provider.model = model
        self._config.llm.active = name
        self._config.llm.temperature = self._temperature_spin.value()
        self._config.pet.name = pet_name
        self._config.pet.persona = persona
        self._config.pet.persona_prompt = persona_prompt
        self._config.pet.user_call = user_call
        self._config.pet.style_actions = self._actions_check.isChecked()
        self._config.pet.style_kaomoji = self._kaomoji_check.isChecked()
        self._config.pet.hover_hint = self._hover_check.isChecked()
        self._config.pet.hover_hint_sec = self._hover_sec_spin.value()
        self._config.schedule.quiet_hours = [
            self._quiet_start.time().toString("HH:mm"),
            self._quiet_end.time().toString("HH:mm"),
        ]
        self._config.schedule.max_notify_per_hour = self._rate_spin.value()
        self._config.app.autostart = self._autostart_check.isChecked()

        pomo = self._config.pomodoro
        pomo.focus_min = self._focus_spin.value()
        pomo.short_break_min = self._short_spin.value()
        pomo.long_break_min = self._long_spin.value()
        pomo.long_break_every = self._long_every_spin.value()
        pomo.mute_other_reminders = self._pomodoro_mute_check.isChecked()
        pomo.notify_sound = self._pomodoro_sound_check.isChecked()

        # 自动番茄钟（v2-focus-web ①）：开关 / 一轮时长 / 工作站点词
        scr = self._config.screen
        scr.focus_enabled = self._auto_focus_check.isChecked()
        scr.focus_minutes = self._auto_focus_spin.value()
        scr.focus_words = parse_words(self._focus_words_edit.text())

        act = self._config.activity
        act.enabled = self._activity_enabled_check.isChecked()
        act.retention_days = self._activity_retention_spin.value()
        act.blacklist = [
            line.strip()
            for line in self._activity_blacklist_edit.toPlainText().splitlines()
            if line.strip()
        ]

        vis = self._config.vision
        vis.provider = self._vision_provider_combo.currentText()
        vis.model = self._vision_model_edit.text().strip()
        vis.sample_sec = self._vision_sample_spin.value()
        vis.change_threshold = self._vision_threshold_spin.value()
        vis.min_gap_sec = self._vision_gap_spin.value()
        vis.max_width = self._vision_width_spin.value()

        # 画面情绪：只在"档位是云端 **且** 授权框勾着"时才把授权写成 true。
        # 其余情况一律落成 false——授权离了云端档没有意义，留着只会变成一笔糊涂账。
        scr = self._config.screen
        scr.mood = self._mood_combo.currentData() or "off"
        scr.mood_min_score = self._mood_min_spin.value()
        scr.mood_cloud = scr.mood == "cloud" and self._mood_cloud_check.isChecked()

        self._config.work.role = self._role_combo.currentData()      # 身份切换（调试用）
        self._config.work.pet_comment = self._comment_check.isChecked()   # 结果末尾带宠物点评

        # 笔记（v2-notes-path）：开关 / 间隔 / 落点。路径填了但用不了**不拦保存**，
        # 退回默认那个「笔记」文件夹并在提示里说一声——路径打错不该让人连开关都存不下去。
        notes = self._config.notes
        notes.enabled = self._notes_enabled_check.isChecked()
        notes.interval_min = self._notes_interval_spin.value()
        typed = self._notes_dir_edit.text().strip()
        if not typed:
            notes.dir = NOTES_DIR_DEFAULT
            self._notes_dir_edit.setText(NOTES_DIR_DEFAULT)
        else:
            notes.dir = typed
        if not notes_dir_usable(self._config.notes_dir):
            notes.dir = NOTES_DIR_DEFAULT
            self._notes_dir_edit.setText(NOTES_DIR_DEFAULT)
            self._saved_hint = f"（{typed} 用不了，笔记已退回默认文件夹 {self._config.notes_dir}）"
        # 存完就把"现在的落点"这行重写一遍——包括上面退回默认那一下，不然页面上挂着的
        # 还是改之前（或那个用不了的）路径，"存哪"照样是个猜谜。
        self._notes_dir_hint.setText(f"现在的落点：{wrap_path(str(self._config.notes_dir))}")

        # 知识库（v2-kb-init）：跟笔记一个待遇——路径填了但用不了**不拦保存**，
        # 退回默认那个并在提示里说一声。校验的是**用户填的那个**（`knowledge_dir`
        # 自己已经会退默认，用它去校验会永远"可用"，坏路径就报不出来了）。
        knot = self._config.knowledge
        kb_typed = self._kb_dir_edit.text().strip()
        if not kb_typed:
            knot.dir = KNOWLEDGE_DIR_DEFAULT
            self._kb_dir_edit.setText(KNOWLEDGE_DIR_DEFAULT)
        else:
            knot.dir = kb_typed
        if not knowledge_dir_usable(self._config.abs_path(knot.dir)):
            knot.dir = KNOWLEDGE_DIR_DEFAULT
            self._kb_dir_edit.setText(KNOWLEDGE_DIR_DEFAULT)
            extra = f"（{kb_typed} 用不了，知识库已退回默认文件夹 {self._config.knowledge_dir}）"
            self._saved_hint = f"{self._saved_hint}{extra}" if self._saved_hint else extra
        self._kb_dir_hint.setText(self._kb_docs_hint())
        # 私密称呼（B+ 挚友档）：留空就是没给过，语气退回"最亲近"那一档
        if hasattr(self, "_nickname_edit"):
            self._config.mind.intimate_nickname = self._nickname_edit.text().strip()
        try:
            self._config.validate()
        except ConfigError as exc:
            self._status.setText(str(exc))
            return False
        return True

    def _save(self) -> None:
        if not self._collect_fields():
            return
        try:
            self._config.save()
        except OSError as exc:
            self._status.setText(f"写入配置文件失败：{exc}")
            return
        self._save_categories()
        try:
            autostart.set_enabled(self._config.app.autostart)
            self._pet_profile_dao.update(self._config.pet.name, self._config.pet.persona)
        except Exception as exc:   # 写注册表或写库失败：说清楚，别让人以为保存成功了
            self._status.setText(f"设置没完全保存：{exc}")
            return
        self._status.setText("已保存" + (self._saved_hint or ""))
        self.saved.emit()
        self.accept()
