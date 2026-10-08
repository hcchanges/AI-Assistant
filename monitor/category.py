"""
应用分类与显示名（对应指令 5 第 3 节）。

只按进程名分类：内置一份常见软件表，用户在设置页指定的类别覆盖它。
浏览器里具体在看哪个网站，属于"网站级"分析，不在这里做。
"""
from __future__ import annotations

# 类别清单（指令 5 第 3 节：工作 / 娱乐 / 社交 / 摸鱼 / 休息）
CATEGORIES = ("工作", "娱乐", "社交", "摸鱼", "休息", "其他")
DEFAULT_CATEGORY = "其他"

# 内置分类：进程名（小写） -> 类别。认不出来的都归"其他"，用户可在设置页改。
BUILTIN: dict[str, str] = {
    # ---- 工作：编辑器 / IDE / 办公 / 数据库 / 终端 ----
    "code.exe": "工作",
    "code - insiders.exe": "工作",
    "devenv.exe": "工作",
    "pycharm64.exe": "工作",
    "idea64.exe": "工作",
    "goland64.exe": "工作",
    "webstorm64.exe": "工作",
    "clion64.exe": "工作",
    "studio64.exe": "工作",
    "sublime_text.exe": "工作",
    "notepad++.exe": "工作",
    "notepad.exe": "工作",
    "typora.exe": "工作",
    "obsidian.exe": "工作",
    "winword.exe": "工作",
    "excel.exe": "工作",
    "powerpnt.exe": "工作",
    "wps.exe": "工作",
    "et.exe": "工作",
    "wpp.exe": "工作",
    "outlook.exe": "工作",
    "foxmail.exe": "工作",
    "onenote.exe": "工作",
    "acrobat.exe": "工作",
    "acrord32.exe": "工作",
    "xmind.exe": "工作",
    "cmd.exe": "工作",
    "powershell.exe": "工作",
    "pwsh.exe": "工作",
    "windowsterminal.exe": "工作",
    "wt.exe": "工作",
    "putty.exe": "工作",
    "mobaxterm.exe": "工作",
    "navicat.exe": "工作",
    "dbeaver.exe": "工作",
    "sqlyog.exe": "工作",
    "matlab.exe": "工作",
    "rstudio.exe": "工作",
    "jupyter-notebook.exe": "工作",
    "docker desktop.exe": "工作",
    "photoshop.exe": "工作",
    "illustrator.exe": "工作",
    "coreldrw.exe": "工作",
    "acad.exe": "工作",
    "unity.exe": "工作",
    "blender.exe": "工作",
    "xshell.exe": "工作",
    "xftp.exe": "工作",
    # ---- 工作沟通（办公 IM 归工作，不算闲聊） ----
    "dingtalk.exe": "工作",
    "wxwork.exe": "工作",
    "feishu.exe": "工作",
    "lark.exe": "工作",
    "teams.exe": "工作",
    "zoom.exe": "工作",
    # ---- 工作·看资料（S1 扩展）：AI 助手 / AI 编码助手 / 阅读与文档 ----
    # 这一类是"坐下来看东西、写东西"，跟 IDE 同类，所以归工作。
    # 收词照老规矩：只收**名字只可能指这一个软件**的。像 "copilot.exe" 这种不带
    # 前缀的名字不敢收（谁家都可能叫 Copilot），"ima.copilot.exe" 这种带前缀的才收。
    #
    # 注意这一张表不止番茄钟在用：resolve_category 还被 monitor/service.py 拿去
    # 归类活动时长（写 activity_session），所以加进来的软件在"工作卡/活动统计"
    # 里也会被算成工作时间，这是有意的——在这些上面花的时间本来就该算工作。
    #
    # ① AI 助手 / 知识库
    "ima.copilot.exe": "工作",      # 腾讯 ima（本机实测确认）
    "claude.exe": "工作",           # Claude 桌面版 / CLI（本机实测确认）
    "chatgpt.exe": "工作",
    "tongyi.exe": "工作",           # 通义千问 PC
    "doubao.exe": "工作",           # 豆包 PC
    "kimi.exe": "工作",
    "m365copilot.exe": "工作",
    # ② AI 编码助手 / 编辑器（Cursor 是 VS Code 分叉，进程名跟 code.exe 不是一个）
    "cursor.exe": "工作",           # 本机实测确认（装了）
    "windsurf.exe": "工作",
    "trae.exe": "工作",
    "cc-switch.exe": "工作",        # Claude Code 配置切换器（本机实测确认）
    # ③ 阅读 / 文档
    "fastpdf.exe": "工作",          # 本机实测确认
    "sumatrapdf.exe": "工作",
    "foxitreader.exe": "工作",
    "foxitpdfreader.exe": "工作",
    "calibre.exe": "工作",
    "zotero.exe": "工作",
    "notion.exe": "工作",
    "yuque.exe": "工作",
    # ---- 社交 ----
    "wechat.exe": "社交",
    "weixin.exe": "社交",
    "qq.exe": "社交",
    "qqnt.exe": "社交",
    "telegram.exe": "社交",
    "discord.exe": "社交",
    "whatsapp.exe": "社交",
    "skype.exe": "社交",
    "line.exe": "社交",
    # ---- 娱乐：游戏 / 影音 ----
    "steam.exe": "娱乐",
    "steamwebhelper.exe": "娱乐",
    "wegame.exe": "娱乐",
    "wegamehelper.exe": "娱乐",
    "epicgameslauncher.exe": "娱乐",
    "battle.net.exe": "娱乐",
    "potplayer.exe": "娱乐",
    "potplayermini.exe": "娱乐",
    "potplayermini64.exe": "娱乐",
    "vlc.exe": "娱乐",
    "mpc-hc64.exe": "娱乐",
    "mpv.exe": "娱乐",
    "cloudmusic.exe": "娱乐",
    "qqmusic.exe": "娱乐",
    "kugou.exe": "娱乐",
    "spotify.exe": "娱乐",
    # ---- 摸鱼：购物、短视频、社区 ----
    "taobao.exe": "摸鱼",
    "douyin.exe": "摸鱼",
    "kuaishou.exe": "摸鱼",
    "xiaohongshu.exe": "摸鱼",
    "weibo.exe": "摸鱼",
    "zhihu.exe": "摸鱼",
}


def friendly_name(proc: str) -> str:
    """显示用的应用名：去掉 .exe 后缀。"""
    return proc[:-4] if proc.lower().endswith(".exe") else proc


def builtin_category(proc: str) -> str:
    return BUILTIN.get(proc.lower(), DEFAULT_CATEGORY)


def resolve_category(proc: str, overrides: dict[str, str]) -> str:
    """分类结果：用户指定优先，其次内置表，最后归"其他"。"""
    key = proc.lower()
    return overrides.get(key) or BUILTIN.get(key, DEFAULT_CATEGORY)
