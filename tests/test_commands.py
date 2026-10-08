"""
指令分发的自动测试（打桩模型，不联网）。

分两段验：
1. 用户说一句话 → 吐嘈解析出的 command → 事件总线上真的发出了这个命令；
2. 命令发到应用层 → 真的打到了对应的动作上（开设置、开面板、隐身、退出、开关陪玩…）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (
    Recorder,
    add_no_key_provider,
    check,
    drain_threads,
    make_env,
    pump,
    section,
    summary,
    wait_for,
)

from brain.chains.chatter_parse import (
    COMMAND_ACTIVITY_REPORT,
    COMMAND_HIDE_PET,
    COMMAND_OPEN_LIFE,
    COMMAND_OPEN_SETTINGS,
    COMMAND_OPEN_WORK,
    COMMAND_QUIT,
    COMMAND_START_PLAYMATE,
    COMMAND_STOP_PLAYMATE,
)


class Chat:
    """驱动一次聊天并收集它产生的信号。"""

    def __init__(self, app, stub):
        self.app, self.stub = app, stub
        self.spoken: list[str] = []
        self.commands: list[str] = []
        self.toasts: list[str] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.user_command.connect(self.commands.append)
        app.bus.toast.connect(self.toasts.append)

    def say(self, text: str, command: str = "", reply: str = "记住啦") -> "Chat":
        self.spoken.clear()
        self.commands.clear()
        self.stub.parse_result = {
            "scene": "测试",
            "time_hint": "",
            "time_iso": "",
            "time_confidence": "none",
            "emotion": "平静",
            "module": "其他",
            "command": command,
            "reply": reply,
        }
        self.app.chatter_service.handle(text)
        wait_for(lambda: bool(self.spoken))
        return self


def main() -> int:
    box, stub, app = make_env()
    chat = Chat(app, stub)

    # 第一段只验"解析 → 总线"这一步，所以先把应用层的响应摘掉，
    # 否则说一句"打开设置"会真的弹出设置窗口，把测试卡住。
    app.bus.user_command.disconnect(app._on_user_command)

    section("聊天里的指令 → 事件总线")
    cases = [
        ("看看我今天都在干嘛", COMMAND_ACTIVITY_REPORT),
        ("躲起来", COMMAND_HIDE_PET),
        ("打开生活", COMMAND_OPEN_LIFE),
        ("打开工作", COMMAND_OPEN_WORK),
        ("打开设置", COMMAND_OPEN_SETTINGS),
        ("关掉程序吧", COMMAND_QUIT),
        ("开始陪玩", COMMAND_START_PLAYMATE),
        ("不用陪玩了", COMMAND_STOP_PLAYMATE),
    ]
    for text, want in cases:
        chat.say(text, command=want)
        check(f"「{text}」→ {want}", chat.commands == [want], f"实收 {chat.commands}")

    chat.say("今天工作有点累", command="")
    check("只是聊到工作不发命令", chat.commands == [], f"实收 {chat.commands}")

    section("命令 → 应用层的动作")
    settings = Recorder(app, "open_settings")
    panel = Recorder(app, "open_panel")
    hide = Recorder(app, "_hide_pet")
    toggle = Recorder(app, "_toggle_pet_visible")
    quit_now = Recorder(app, "_quit_now")
    today = Recorder(app.activity_service, "announce_today")

    app._on_user_command(COMMAND_OPEN_SETTINGS)
    check("open_settings 打到 open_settings()", settings.called)

    app._on_user_command(COMMAND_OPEN_LIFE)
    check("open_life 打到 open_panel('life')", panel.last == ("life",), str(panel.last))

    app._on_user_command(COMMAND_OPEN_WORK)
    check("open_work 打到 open_panel('work')", panel.last == ("work",), str(panel.last))

    app._on_user_command(COMMAND_HIDE_PET)
    check("hide_pet 打到隐身", hide.called)

    app._on_user_command(COMMAND_ACTIVITY_REPORT)
    check("activity_report 打到今日活动", today.called)

    app._on_user_command(COMMAND_QUIT)
    check("quit 不会立刻退出（先把回话留住）", not quit_now.called)
    pump(1.6)
    check("quit 延迟一下真的退出了", quit_now.called)

    check("toggle 不该被命令触发", not toggle.called)

    section("陪玩开关：没 Key 与有 Key")
    pet_speak = Recorder(app.pet_window, "speak")
    no_key = add_no_key_provider(app.config)
    original_provider = app.config.vision.provider
    app.config.vision.provider = no_key
    app._on_user_command(COMMAND_START_PLAYMATE)
    check("没配看图 Key 时不开启", not app.playmate_service.is_active)
    said = pet_speak.last[0] if pet_speak.last else ""
    check(
        "没配 Key 时告诉用户去哪填",
        "Key" in said and "设置" in said,
        said or "（它没说话）",
    )

    pet_speak.restore()
    app.config.vision.provider = original_provider
    app._on_user_command(COMMAND_START_PLAYMATE)
    check("配了 Key 就能开启", app.playmate_service.is_active)
    app._on_user_command(COMMAND_STOP_PLAYMATE)
    check("说不用陪玩了就关闭", not app.playmate_service.is_active)

    settings.restore()
    panel.restore()
    hide.restore()
    toggle.restore()
    quit_now.restore()
    today.restore()

    drain_threads()
    box.cleanup()
    return summary("指令分发")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
