"""
笔记保存路径（v2-notes-path）的自动测试。

用户真机反馈：**抓屏笔记不知存哪**。S0 查下来是三件事叠在一起：

1. 落点默认是 `data/notes`（相对项目根）——笔记埋在程序目录里面，还是 gitignore 藏着的；
2. 设置页八页签里**一个字都没提 notes**，路径改不了；
3. 连开关 `notes.enabled` 都只有配置里有（默认 False）——想开也没地方点。

这一轮验四件事：

- **①默认落在独立的「笔记」文件夹**：默认值是项目上一级的「笔记」（跟用户的 存档 / 资料
  并排），不是 `技术/data/notes`；抓屏一拍真的落进配置的那个文件夹，目录没有会自动建；
- **②设置页能改、改了立刻生效、重启保持**：输入框 + 浏览按钮；保存后 config.yaml 里真
  写进去了（`Config.save()` 的白名单原先**没有 notes**，不加进去就是静默不落盘），
  重开配置读回来还是那个路径，跑着的服务不用重启就写新地方；
- **③路径非法有兜底**：填了用不了的路径 → 保存时退回默认并在提示里说明；连落盘那一步
  也兜底（配置的文件夹写不进去 → 落到默认文件夹再试一次）；
- **红线**：只写笔记文件，pet.db 的 utterance / memory 与 mind.db 的碎片一条不多。

**落点全在沙箱临时目录里**（harness 的规矩：绝不碰用户真实数据）。所以"默认那个文件夹
长什么样"验的是 `Config.notes_dir_default` 算出来的路径；连"退回默认"那几条也把默认
文件夹常量临时指到临时目录——不然跑一趟测试就会在用户的磁盘上建出 `E:\电子宠物\笔记`。
真机上建这个文件夹这件事，由收尾时的真实值探针另跑一次（自建自清）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary

import yaml
from PySide6.QtWidgets import QPushButton, QTabWidget

from core import config as config_mod
from core.config import NOTES_DIR_DEFAULT, Config, NotesSection, notes_dir_usable

import ui.settings as settings_mod

ROOT = Path(__file__).resolve().parents[1]
TAB_NAMES = [
    "模型", "宠物", "记忆管理", "提醒", "番茄钟", "活动", "陪玩", "工作", "笔记",
    "知识库",      # v2-kb-init：放 .md/.txt 文档的地方，问答照着它答
]


def _rows(db_path) -> int:
    """直接数 pet.db 某张表的行数（真读盘，不是看对象属性）。"""
    con = sqlite3.connect(str(db_path))
    try:
        return con.execute("select count(*) from utterance").fetchone()[0]
    finally:
        con.close()


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


def _write_note(app, title: str) -> None:
    """走真实那条落盘：提炼结果回来 → 渲染成一段 → 追加进文件。"""
    app.screen_notes._on_notes(
        {"title": title, "key_points": ["一句要点"], "todos": [], "questions": []}
    )
    pump(0.05)


def _mds(root) -> list[str]:
    root = Path(root)
    return sorted(p.name for p in root.glob("*.md")) if root.is_dir() else []


def _body(root) -> str:
    files = _mds(root)
    return (Path(root) / files[0]).read_text(encoding="utf-8") if files else ""


def _buttons(dialog, text: str) -> list[QPushButton]:
    return [b for b in dialog.findChildren(QPushButton) if b.text() == text]


class _FakeFileDialog:
    """替掉 QFileDialog：Qt 那个类型不让在类上挂替身，直接换掉设置页看到的这个名字。

    真的弹一个选文件夹窗口，离屏跑会挂在那儿等人点——测试里只关心"选完有没有回填"。
    """

    picked = ""

    @staticmethod
    def getExistingDirectory(*_args, **_kwargs) -> str:
        return _FakeFileDialog.picked


def main() -> int:
    box, _stub, app = make_env()
    notes = app.screen_notes
    old_default_in_repo = ROOT / "data" / "notes"

    section("① 默认就落在独立的「笔记」文件夹，不在程序目录里")
    check("配置里的默认值就是「../笔记」", NotesSection().dir == NOTES_DIR_DEFAULT == "../笔记", NotesSection().dir)
    default_dir = app.config.notes_dir_default
    check(
        "默认文件夹解析成「项目上一级 / 笔记」（跟 存档 / 资料 并排）",
        default_dir == (ROOT.parent / "笔记").resolve(),
        str(default_dir),
    )
    check("默认文件夹确实叫「笔记」", default_dir.name == "笔记", default_dir.name)
    check(
        "默认文件夹在程序目录**外面**",
        ROOT not in default_dir.parents and default_dir != old_default_in_repo,
        str(default_dir),
    )

    section("① 抓屏一拍 → 笔记真的落进配置的那个文件夹（目录自动建）")
    target = box.dir / "笔记" / "深一层"          # 还不存在的目录，验自动创建
    app.config.notes.dir = str(target)
    check("还没写之前这个文件夹不存在", not target.exists(), str(target))
    _write_note(app, "线性回归")
    files = _mds(target)
    check("笔记落在配置的文件夹里了", len(files) == 1, str(files))
    check("目录是程序自己建出来的（含中间层）", target.is_dir(), str(target))
    check("笔记正文写进去了", "线性回归" in _body(target), _body(target)[:60])
    check("没有往老落点 data/notes 写东西", not old_default_in_repo.exists(), str(old_default_in_repo))

    dialog = _dialog(app)
    try:
        section("② 设置页多了一页「笔记」")
        tabs = dialog.findChild(QTabWidget)
        names = [tabs.tabText(i) for i in range(tabs.count())]
        check("页签多了「笔记」与「知识库」、老八页顺序没动", names == TAB_NAMES, str(names))
        check(
            "这一页有：开关 / 间隔 / 路径输入框 / 浏览按钮 / 打开文件夹",
            all(
                hasattr(dialog, attr)
                for attr in (
                    "_notes_enabled_check", "_notes_interval_spin",
                    "_notes_dir_edit", "_notes_dir_hint",
                )
            )
            # 这两个按钮**不数个数**："浏览…" / "打开文件夹" 是同款控件文案，
            # 设置页上不止一页在用（v2-kb-init 的「知识库」页也各有一个）。
            # 这里要的是"笔记页有这两样东西"，不是"全局只有两个"。
            and _buttons(dialog, "浏览…")
            and _buttons(dialog, "打开文件夹"),
            "",
        )
        check("回填的是当前配置里的落点", dialog._notes_dir_edit.text() == str(app.config.notes.dir),
              dialog._notes_dir_edit.text())
        # 提示里那条路径夹着零宽断行点（U+200B）：路径没空格，不给断点的话标签最小宽度
        # 就等于整条路径的宽度，整页会被顶出横向滚动条。断言前先去掉它再比对原文。
        hint_text = dialog._notes_dir_hint.text()
        check("页面上写着现在的落点（省得再猜「存哪」）",
              str(app.config.notes_dir) in hint_text.replace("\u200b", ""), hint_text)
        check("落点那条提示带着断行点（长路径折得动行，不会把整页顶宽）",
              "\u200b" in hint_text and dialog._notes_dir_hint.minimumSizeHint().width() < 400,
              f"最小宽 {dialog._notes_dir_hint.minimumSizeHint().width()}")

        section("② 改了保存 → 写进 config.yaml、重启读回来还在、服务不用重启就写新地方")
        new_dir = box.dir / "换个地方"
        dialog._notes_dir_edit.setText(str(new_dir))
        dialog._notes_enabled_check.setChecked(True)
        dialog._notes_interval_spin.setValue(9)
        # 真的走"保存"那条路，并把 bootstrap 那根线接上（真机上 settings 的 saved 就是这么接的）
        dialog.saved.connect(app._on_settings_saved)
        dialog._save()

        check("内存里的配置改了", app.config.notes.dir == str(new_dir), app.config.notes.dir)
        check("间隔也改了", app.config.notes.interval_min == 9, str(app.config.notes.interval_min))
        on_disk = yaml.safe_load(box.config_path.read_text(encoding="utf-8")) or {}
        check(
            "config.yaml 里真写进去了（save() 的白名单原先没有 notes）",
            (on_disk.get("notes") or {}).get("dir") == str(new_dir),
            str(on_disk.get("notes")),
        )
        check("开关也落盘了", bool((on_disk.get("notes") or {}).get("enabled")) is True, str(on_disk.get("notes")))
        reloaded = Config.load(box.config_path)
        check(
            "重启读回来还是那个路径（持久）",
            reloaded.notes.dir == str(new_dir) and str(reloaded.notes_dir) == str(new_dir),
            reloaded.notes.dir,
        )
        check("保存后抓屏笔记这条线真跑起来了", notes.recording, f"recording={notes.recording}")

        before_new, before_old = _mds(new_dir), _body(target)
        _write_note(app, "换地方之后第一条")
        check("新笔记进了新文件夹（没重启）", len(_mds(new_dir)) == len(before_new) + 1, str(_mds(new_dir)))
        check(
            "旧文件夹里没混进新笔记",
            "换地方之后第一条" not in _body(target) and _body(target) == before_old,
            "",
        )

        section("② 浏览按钮 / 打开文件夹按钮")
        picked = box.dir / "浏览挑的"
        with patch.object(settings_mod, "QFileDialog", _FakeFileDialog):
            _FakeFileDialog.picked = str(picked)
            dialog._browse_notes_dir()
            check("浏览选完回填到输入框", dialog._notes_dir_edit.text() == str(picked),
                  dialog._notes_dir_edit.text())
            _FakeFileDialog.picked = ""      # 用户点了取消
            dialog._browse_notes_dir()
            check("浏览点取消不清空原来的值", dialog._notes_dir_edit.text() == str(picked),
                  dialog._notes_dir_edit.text())

        opened: list[str] = []
        open_target = box.dir / "点开看看"
        with patch.object(settings_mod.os, "startfile", lambda p: opened.append(p), create=True):
            dialog._notes_dir_edit.setText(str(open_target))
            dialog._open_notes_dir()
        check("打开文件夹：叫的是文件管理器、路径就是输入框那个", opened == [str(open_target)], str(opened))
        check("打开文件夹：文件夹不存在就先建出来", open_target.is_dir(), "")

        section("③ 路径非法有兜底（保存时退回默认）")
        # 把"默认文件夹"临时指到临时目录：不然退回默认那一下会在用户磁盘上真建出「笔记」
        sandbox_default = box.dir / "默认的笔记"
        with patch.object(config_mod, "NOTES_DIR_DEFAULT", str(sandbox_default)), \
             patch.object(settings_mod, "NOTES_DIR_DEFAULT", str(sandbox_default)):
            dialog._notes_dir_edit.setText(str(box.config_path / "笔记"))   # 祖先是个文件，建不出来
            check("这个路径确实是用不了的", not notes_dir_usable(box.config_path / "笔记"),
                  str(box.config_path / "笔记"))
            dialog._save()
            check("保存没被拦住，退回默认值", app.config.notes.dir == str(sandbox_default), app.config.notes.dir)
            check("输入框也改成默认值了", dialog._notes_dir_edit.text() == str(sandbox_default),
                  dialog._notes_dir_edit.text())
            check(
                "提示里说明了为什么退回",
                "退回默认" in dialog._status.text() and str(sandbox_default) in dialog._status.text(),
                dialog._status.text(),
            )
            on_disk = yaml.safe_load(box.config_path.read_text(encoding="utf-8")) or {}
            check("落盘的也是默认值（不是那个坏路径）",
                  (on_disk.get("notes") or {}).get("dir") == str(sandbox_default), str(on_disk.get("notes")))

            dialog._notes_dir_edit.setText("")          # 清空 = 用默认，不该变成"存到项目根"
            dialog._save()
            check("路径留空 → 退回默认", app.config.notes.dir == str(sandbox_default), app.config.notes.dir)
            check("留空不会被解析成项目根目录", app.config.notes_dir != ROOT, str(app.config.notes_dir))
            check("退回默认之后笔记照样写得进去", _write_note(app, "退回默认这条") is None
                  and len(_mds(sandbox_default)) == 1, str(_mds(sandbox_default)))
    finally:
        dialog.reject()

    section("③ 落盘那一步也兜底：配置的文件夹写不进去 → 落到默认文件夹")
    fallback = box.dir / "默认的笔记2"
    broken = box.config_path / "写不进去"          # 祖先是个文件
    app.config.notes.dir = str(broken)
    with patch.object(config_mod, "NOTES_DIR_DEFAULT", str(fallback)):
        check("兜底目标（默认文件夹）现在指向临时目录",
              app.config.notes_dir_default == fallback, str(app.config.notes_dir_default))
        _write_note(app, "兜底这条")
    check("坏路径写不进去，笔记落到了默认文件夹", len(_mds(fallback)) == 1, str(_mds(fallback)))
    check("落到默认文件夹的那条内容是对的", "兜底这条" in _body(fallback), _body(fallback)[:60])
    check("坏路径下面什么都没建出来", not broken.exists(), str(broken))

    section("红线：只写笔记文件，两个库一个字节没动")
    pet_utterance = _rows(app.config.db_path)
    memory_rows = len(app.memory_dao.recent(100))
    fragments = app.mind_store.fragment_count()
    app.config.notes.dir = str(box.dir / "红线检查")
    _write_note(app, "红线这条")
    check("写了一条笔记（前置条件）", len(_mds(box.dir / "红线检查")) == 1, "")
    check("pet.db 的 utterance 一行没多", _rows(app.config.db_path) == pet_utterance,
          f"{pet_utterance} → {_rows(app.config.db_path)}")
    check("pet.db 的 memory 一行没多", len(app.memory_dao.recent(100)) == memory_rows, "")
    check("mind.db 一条碎片都没写", app.mind_store.fragment_count() == fragments, "")
    check("笔记没落进程序目录", not old_default_in_repo.exists(), str(old_default_in_repo))

    section("红线：零新依赖（挑文件夹用的是 Qt 自带的 QFileDialog）")
    src = (ROOT / "ui" / "settings.py").read_text(encoding="utf-8")
    check("浏览按钮用的是 Qt 自带的 QFileDialog", "    QFileDialog,\n" in src, "")
    check("没有为了挑文件夹引入第三方库", "tkinter" not in src and "win32" not in src, "")

    drain_threads()
    box.cleanup()
    return summary("笔记保存路径（v2-notes-path）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
