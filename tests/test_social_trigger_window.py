"""仅单聊触发帮聊（v2-social-trigger-window-gate）的自动测试。

用户拍板的口径：**群聊不算个人聊天窗口**。帮聊那两条（主动 offer、自动监听起草）只在
"跟某个人一对一"的聊天窗里发生；群聊 / 会话总列表 / 订阅号 / 服务号 / 公众号 /
文件传输助手 / 小程序一律**不触发**——闸门没过就走"不触发"，不报错、不改任何既有记账。

判据全在**认出来的字**上（`life.screen.social.classify_chat_page`），证据是真机
2026-10-08 那批 96 张帧逐张过认字线的读数（`%TEMP%/p18/probe_page_kind.txt`）：那几帧
群聊最上面一行是**群名栏**「群名 (N)」，消息区里还有整行 `群公告` 的那种卡片标题；单聊
帧那一行就是光秃秃一个联系人名（`茗风`）。① 把真机帧的原文逐行抄进用例（不依赖盘上还
有没有那几张图），②③ 再走服务那条链，④ 守着"认不出就放行"这条老口径。

红线：pet.db / mind.db 零写入；零新依赖；不模拟按键 / 不碰剪贴板；`config.yaml` /
`.vscode` 一字不碰。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.screen import social as smod
from life.screen.social import (
    PAGE_CHANNEL,
    PAGE_FILEHELPER,
    PAGE_GROUP,
    PAGE_LIST,
    PAGE_MINI,
    PAGE_PERSONAL,
    SocialReplyService,
    chat_page_kind,
    classify_chat_page,
    is_personal_chat,
)
from life.screen.social_window import SENDER_OTHER, SENDER_USER, ChatRead

OTHER = SENDER_OTHER
USER = SENDER_USER

# `social.py` 本来就用着的那些顶层 import（v2-social-trigger-window-gate 一个都没多要）。
_BASE_REFS = frozenset({
    "__future__", "PySide6", "brain", "core", "hashlib", "life", "logging", "monitor",
    "re", "time", "typing",
})


# ---- 真机帧原文（`probe_page_kind.txt` 逐行抄下来的，只有那几帧多了"口 / X / 发送"
# ---- 这种窗上装潢，一个字的正文都没动）----

# 165214：群聊「0922期AI编程副业实...(25)」——群名栏读成全角括号那一版。
GROUP_FRAME_FULL = [
    "口", "X",
    "0922期AI编程副业实...（25）（",
    "同学们好，今晚20.…>",
    "16:51",
    "班班-子豪老师【周末休息】@知乎知学堂",
    "群公告",
    "同学们好，今晚20:00咱们有AI编程",
    "正式课的学习哦～",
    "课程主题：给AI销售打造一套知识库",
    "666.0K",
    "发送",
]

# 165548：同一场群聊，群名栏这一次读成**半角**括号——半角全角都得认。
GROUP_FRAME_HALF = [
    "口",
    "0922期AI编程副业实...(25)",
    "3.2M",
    "16:51",
    "班班-子豪老师【周末休息】@知乎知学堂",
    "群公告",
    "给AI销售打造一套知识",
    "库.pdf",
    "发送",
]

# 175550：单聊「茗风」——表头就俩字，消息区只有绿 / 白气泡，一条群证据都没有。
PERSONAL_FRAME = [
    "口", "X",
    "茗风",
    "口",
    "那我该不该接受呢",
    "06:51",
    "你自己心里有数吧，想接受就接受，别回",
    "头又纠结。我这边不催你，你想好了再",
    "说。",
    "人家女孩子脸皮薄，想不想接受也得先看",
    "看你的行动呀。",
    "好呀，下次有机会再说",
    "发送",
]

# 123217：单聊，但屏上有一条系统提示「你撤回了一条消息」——**撤回不是群聊标记**
# （单聊里撤回自己的消息一样印这行），这条钉住"别拿撤回当群证据"。
PERSONAL_FRAME_WITH_RECALL = [
    "口", "X",
    "茗风",
    "你撤回了一条消息",
    "哦，什么样的人才是合适的呢？",
    "看不",
    "出来，你小子撩妹还挺有一套的呀",
    "昨天14:22",
    "哈哈，名人不是说：自古深情留不住，唯",
    "有套路得人心。要泡妹，就要懂套路。",
    "发送",
]

# 123259：会话总列表（16-β 那条腿认的那一种）：每行一条纯时间戳，还印着
# `文件传输助手` / `服务号` / `公众号` / 群名行。
LIST_FRAME = [
    "口",
    "Q搜索",
    "GitMindAl思想星球",
    "12:11",
    "刚刚！GPT-6来了，免费用户也能用！",
    "宝安固戌地铁站周边二手闲置2群",
    "12:31",
    "[6条]nemophilist:有收纳箱出手的吗",
    "艾邦AI戒指产业交流群",
    "12:10",
    "[15条]\"Hua\"与群里其他人都不是朋友关系，请注意隐私安全",
    "李北京互联网撩妹得吃5群Q",
    "12:06",
    "[22条]冬日暖阳：填完反馈回来给xdm看看",
    "公众号",
    "12:04",
    "[3条]南山通：10月10日起，坐飞机将有新变化！",
    "电子烟精英人才招聘6群",
    "11:42",
    "文件传输助手",
    "11:14",
]

# 142602：**漏帧**——聊天区被会话列表那一列盖住了（茗风的单聊），屏上 `服务号` /
# `公众号` / `微信支付` 各自独占一行。固定名要是按"整屏出现即算"，这一帧就会被误判成
# "服务号页"、offer 直接不冒；所以那几条只看**表头那一带**，这儿的表头是 `口 / Q搜索 /
# 茗风 / 好`，一个固定名都没有。
LEAK_FRAME_PERSONAL = [
    "口",
    "Q 搜索",
    "茗风",
    "好",
    "昨天19:05",
    "宝安固戌地铁站周",
    "[17条]阳光阴暗小子",
    "服务号",
    "那我该不该接受呢",
    "交通银行微银行：1元",
    "张总一悟（何文镜",
    "公众号",
    "[2条] 深圳周末去哪玩",
    "中国平安",
    "微信支付",
    "已支付￥50.00",
    "发送",
]

# 那一帧里**自己发过话**（绿气泡）——真机读数 user=5，这正是它没被 16-β 判成总列表的
# 原因（`looks_like_list_page` 头一句：绿气泡是"这是聊天窗"的硬证据）。抄进用例时也得
# 把它们摆上，不然抄出来的是另一帧。
LEAK_FRAME_SELF = [
    "撩你？我这是正常发挥，你",
    "想了。行吧，就当是吧，反",
    "你自己心里有数吧，想接受",
]


def _read(lines: list[str], *, self_lines: list[str] | None = None,
          page_kind: str = "") -> ChatRead:
    """按"白气泡是对方说的 +（可选）绿气泡是自己发的"拼一份 `ChatRead`。"""
    rows = [(USER, text) for text in (self_lines or [])]
    rows += [(OTHER, text) for text in lines]
    return ChatRead(lines=rows, raw="\n".join(text for _s, text in rows),
                    page_kind=page_kind)


def _head(lines: list[str], page_kind: str = "") -> list[str]:
    """给"某一页的表头那一行"造一屏：表头 + 一句人话（免得落进空 read 那条口径）。"""
    return _read(lines or ["还有什么要说的吗"], page_kind=page_kind)


def main() -> int:
    # ---------- ① 页型判定：真机帧原文 ----------

    section("① 页型判定（文本层）：真机帧原文上分得出哪一页是哪一页")

    cases = [
        ("群聊 165214（全角括号的群名栏）", _read(GROUP_FRAME_FULL), PAGE_GROUP, False),
        ("群聊 165548（半角括号的群名栏）", _read(GROUP_FRAME_HALF), PAGE_GROUP, False),
        ("单聊 175550（表头就一个联系人名）", _read(PERSONAL_FRAME), PAGE_PERSONAL, True),
        ("单聊 123217（屏上有一条「你撤回了一条消息」）",
         _read(PERSONAL_FRAME_WITH_RECALL), PAGE_PERSONAL, True),
        ("会话总列表 123259", _read(LIST_FRAME), PAGE_LIST, False),
        ("漏帧 142602（列表列盖在单聊上、`服务号` 独占一行）",
         _read(LEAK_FRAME_PERSONAL, self_lines=LEAK_FRAME_SELF), PAGE_PERSONAL, True),
    ]
    for name, read, want, personal in cases:
        got = classify_chat_page(read)
        check(f"{name} → {want}", got == want, f"判成 {got}")
        check(f"{name} → is_personal_chat {personal}",
              is_personal_chat(read) is personal, "")

    # 群聊那两条腿各自单独也能立住（真机那三帧两条腿都中，这里把它们拆开验）
    check("群名栏那一条腿单独立得住（把「群公告」那几行拿掉照样是群聊）",
          classify_chat_page(_read(
              ["0922期AI编程副业实...（25）（", "同学们好，今晚20.…>", "16:51"])) == PAGE_GROUP, "")
    check("群公告那一条腿单独立得住（把群名栏那一行拿掉照样是群聊）",
          classify_chat_page(_read(
              ["茗风", "好", "群公告", "同学们好，今晚20:00咱们有AI编程"])) == PAGE_GROUP, "")
    check("群名栏那条腿不认「人话里带个括号数字」——除非它落进表头那一带",
          classify_chat_page(_read(
              ["茗风", "好", "昨天14:22", "我这边不催你", "好呀", "报名费（200）"]))
          == PAGE_PERSONAL, "")
    check("「（200）」那种写法真落进表头那一带时按群聊办（宁严不松，见 §164-戊）",
          classify_chat_page(_read(["报名费（200）", "你交不交"])) == PAGE_GROUP, "")

    # 固定名那几页：**只看表头那一带**，理由见 142602 那一帧
    for name, head, want in (
        ("文件传输助手", ["文件传输助手", "已发送"], PAGE_FILEHELPER),
        ("订阅号", ["订阅号", "刚刚更新"], PAGE_CHANNEL),
        ("服务号", ["服务号", "交通银行微银行"], PAGE_CHANNEL),
        ("公众号", ["公众号", "深圳周末去哪玩"], PAGE_CHANNEL),
        ("小程序", ["小程序", "跳一跳"], PAGE_MINI),
    ):
        got = classify_chat_page(_read(head))
        check(f"{name} 页（表头那一行就是页名）→ {want}", got == want, f"判成 {got}")
        check(f"{name} 页不放行（is_personal_chat False）",
              is_personal_chat(_read(head)) is False, "")
    leak = _read(LEAK_FRAME_PERSONAL, self_lines=LEAK_FRAME_SELF)
    check("固定名那几条腿**只在表头那一带**认：漏帧上 `服务号` 独占一行也不算",
          "服务号" in LEAK_FRAME_PERSONAL
          and LEAK_FRAME_PERSONAL.index("服务号") >= 4
          and classify_chat_page(leak) == PAGE_PERSONAL, "")
    check("固定名那几条腿不认「整行只多几个字」的那种（`服务号通知` 不是页名）",
          classify_chat_page(_read(["服务号通知", "你好"])) == PAGE_PERSONAL, "")

    # 判定顺序：总列表那条腿在最前头（列表页上那些整行的页名不该再往下判）
    check("既是总列表、又印着一整行 `文件传输助手` → 按总列表办（那条腿在先）",
          classify_chat_page(_read(LIST_FRAME)) == PAGE_LIST, "")

    # 装配层判过的页型在 read 上，**先信它**（16-β 清成空 read 之后文本层什么都看不出来）
    check("read 上挂了页型就先信它（不拿清过的空 read 反推）",
          chat_page_kind(ChatRead(page_kind=PAGE_LIST)) == PAGE_LIST, "")
    check("read 上没挂页型才现判一次",
          chat_page_kind(_read(PERSONAL_FRAME)) == PAGE_PERSONAL, "")

    # ---------- ② offer 闸门：只在"非单聊 → 单聊"跃迁那一拍冒 ----------

    section("② offer 闸门：群聊 / 总列表 / 订阅号 … 一律不冒；单聊才冒，且只冒一次")

    from harness import make_env

    box, stub, app = make_env()
    app.config.screen.social_enabled = True
    app.config.screen.ocr_enabled = True
    # 冷却是 30 分钟（`social_cooldown_min`），起始刻度取在这之后——否则"第一次冒"
    # 会被 `_due()` 挡掉，验出来的不是闸门那件事。
    now = {"t": 10_000.0}

    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    def _new_service() -> SocialReplyService:
        svc = SocialReplyService(
            config=app.config, bus=app.bus, clock=lambda: now["t"],
            idle_seconds=lambda: 99.0,
        )
        svc._social_foreground = True
        return svc

    svc = _new_service()
    for kind in (PAGE_GROUP, PAGE_LIST, PAGE_CHANNEL, PAGE_FILEHELPER, PAGE_MINI):
        svc.note_page_kind(kind)
    check("群聊 / 总列表 / 订阅号 / 文件助手 / 小程序 连着报过来 → 一次都不冒",
          offers == [], f"{len(offers)} 次")

    svc.note_page_kind(PAGE_PERSONAL)
    check("★ 从群聊点进某个人的单聊（非单聊 → 单聊的跃迁）→ 这一拍冒了",
          len(offers) == 1 and svc.awaiting_confirm is True, f"{len(offers)} 次")

    svc.note_page_kind(PAGE_PERSONAL)
    svc.note_page_kind(PAGE_PERSONAL)
    check("★ 连着停在单聊上（每一拍都报单聊）→ 不重复弹",
          len(offers) == 1, f"{len(offers)} 次")

    svc._awaiting = False
    svc.note_page_kind(PAGE_LIST)
    now["t"] += 3600.0
    svc.note_page_kind(PAGE_PERSONAL)
    check("回到列表、再点进单聊 → 又是一次跃迁，冷却也过了 → 再冒一句",
          len(offers) == 2, f"{len(offers)} 次")

    svc._awaiting = False
    now["t"] += 3600.0
    svc.note_page_kind(PAGE_PERSONAL)
    check("★ 跃迁之外（页型没动）再多报几次也不冒——不按冷却空喊",
          len(offers) == 2, f"{len(offers)} 次")

    # 冷启动：页型那格还是空的（什么都没认出来）时进到单聊，算一次跃迁
    svc2 = _new_service()
    svc2.note_page_kind(PAGE_PERSONAL)
    check("冷启动第一拍（什么都没认出来 → 单聊）也算跃迁 → 冒",
          len(offers) == 3 and svc2.awaiting_confirm is True, f"{len(offers)} 次")

    # 前台已经不是社交软件了：晚到的那一屏不许再冒
    svc3 = _new_service()
    svc3._social_foreground = False
    svc3.note_page_kind(PAGE_PERSONAL)
    check("前台已经不是社交软件了 → 晚到的单聊页也不冒",
          len(offers) == 3, f"{len(offers)} 次")

    # 走出聊天窗再进来：页型那格清掉，下一趟第一拍照旧算跃迁
    svc4 = _new_service()
    svc4.note_page_kind(PAGE_PERSONAL)
    svc4.on_foreground("Weixin.exe")
    svc4._awaiting = False
    svc4.on_foreground("chrome.exe")        # 走出聊天窗：页型清回空串
    now["t"] += 3600.0
    svc4.on_foreground("Weixin.exe")
    svc4.note_page_kind(PAGE_PERSONAL)
    check("走出聊天窗再进来 → 页型那格清过，第一拍照旧算跃迁",
          len(offers) == 5, f"{len(offers)} 次")

    # ---------- ③ 自动监听：非单聊不起草 ----------

    section("③ 自动监听：群聊 / 总列表那一屏不认作对方的消息、不起草")

    svc5 = _new_service()
    svc5._last_listen_tail = "上一句"
    drafted: list[str] = []
    svc5._auto_generate = lambda text: drafted.append(text)

    svc5._on_listen_read(_read(GROUP_FRAME_FULL))
    check("★ 群聊那一屏：不拟（不进帮聊）", drafted == [], repr(drafted))
    check("★ 群聊那一屏：尾巴一行都不推进（不许记成「这条处理过了」）",
          svc5._last_listen_tail == "上一句", repr(svc5._last_listen_tail))
    check("群聊那一屏的页型记下来了", svc5._page_kind == PAGE_GROUP, svc5._page_kind)

    svc5._on_listen_read(ChatRead(page_kind=PAGE_LIST))     # 16-β 清过的空 read
    check("会话总列表那一屏：也不拟", drafted == [], repr(drafted))

    svc5._page_kind = ""
    svc5._last_listen_tail = "上一句"
    svc5._on_listen_read(_read(["茗风", "那我该不该接受呢", "好呀，下次有机会再说"]))
    check("★ 单聊那一屏、对方回了新消息 → 照旧直接拟（闸门没把监听卡死）",
          len(drafted) == 1 and "下次有机会再说" in drafted[0], repr(drafted))

    # ---------- ④ 认不出就放行（别把功能整条闷死） ----------

    section("④ 认不出就放行：空 read / 老桩的一行字符串 / 老口径那两条路")

    check("空 read → 当单聊（默认放行）",
          classify_chat_page(ChatRead()) == PAGE_PERSONAL, "")
    check("老桩塞进来的一行字符串 → 当单聊",
          classify_chat_page("茗风：还好啊，感觉还挺愉快的") == PAGE_PERSONAL, "")
    check("空串页型算单聊（`is_personal_kind`）", smod.is_personal_kind("") is True, "")
    check("排除名单里那几种都不算", all(
        not smod.is_personal_kind(k) for k in smod.NON_PERSONAL_PAGES), "")
    check("`looks_like_list_page` 的语义一个字没动（总列表那条腿是复用的那一份）",
          smod.looks_like_list_page(_read(LIST_FRAME)) is True
          and smod.looks_like_list_page(_read(PERSONAL_FRAME)) is False, "")

    svc6 = _new_service()
    svc6.on_foreground("Weixin.exe")
    check("★ 没接 OCR 那两只手（测试老桩 / 关掉 OCR）那一条老口径照旧：当场冒",
          svc6.awaiting_confirm is True, str(svc6.awaiting_confirm))

    svc8 = _new_service()
    svc8._grab_chat = lambda: None            # 接了手、这一拍取不到帧
    svc8._read_chat = lambda array: _read(PERSONAL_FRAME)
    svc8.on_foreground("Weixin.exe")
    check("★ 这一拍连帧都取不到（无从判起）→ 也照老口径当场冒，不静默闷死",
          svc8.awaiting_confirm is True, str(svc8.awaiting_confirm))

    svc7 = SocialReplyService(config=app.config, bus=app.bus, clock=lambda: now["t"])
    svc7.on_foreground("Weixin.exe")
    check("★ 只给 config / bus 的老桩服务照旧当场冒（`test_social` 那一批靠这条不红）",
          svc7.awaiting_confirm is True, str(svc7.awaiting_confirm))

    # ---------- ⑤ 红线 ----------

    section("⑤ 红线：零新 import / 两库零写入 / 判定不看颜色")

    before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    import ast
    import inspect

    refs: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(smod))):
        if isinstance(node, ast.Import):
            refs.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.add((node.module or "").split(".")[0])
    check("`social.py` 用的还是原来那几样（零新依赖）",
          refs <= _BASE_REFS, repr(sorted(refs - _BASE_REFS)))
    src = inspect.getsource(smod.classify_chat_page) + inspect.getsource(smod.is_personal_chat)
    check("判定里没有一处碰像素 / 取色（`classify_chat_page` 只看认出来的字）",
          "bubble_rgb" not in src and "image" not in src and "QImage" not in src, "")
    check("两库零写入（utterance 条数没变）",
          _rows(app.config.db_path, "utterance") == before, "")
    check("两库零写入（记忆碎片数没变）",
          app.mind_store.fragment_count() == mind_before, "")

    box.cleanup()
    return summary("仅单聊触发帮聊（v2-social-trigger-window-gate）")


def _rows(db_path, table: str) -> int:
    import sqlite3

    con = sqlite3.connect(str(db_path))
    try:
        return con.execute(f"select count(*) from {table}").fetchone()[0]
    finally:
        con.close()


if __name__ == "__main__":
    raise SystemExit(main())
