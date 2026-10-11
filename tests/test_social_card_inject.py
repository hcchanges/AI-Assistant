"""社交起草时把"对方这个人"喂进正文（v2-social-card-inject）的自动测试。

**这一批补的是哪一格**：起草提示词 `SOCIAL_DRAFT_PROMPT` 里原来只有三样料——对方最近发
来的那条、用户自己的处境、以往聊过的范例（学语气）。它知道"对方刚说了什么"，不知道
"对方是什么人"：同样一句"周末有空吗"，对着一个刚失恋的人和对着一个认识两年的同事，
该回的话不一样。这一批加了第五格 `{peer_profile}`，把两样合成一句喂进去：

① **对方最新那句话的意图**——批次 `v2-social-card-base` 就算好了（`self._peer_route`，
  传 `speaker="peer"` 走纯规则那一路），这一批终于有人取它；
② **三记忆库里属于这个人那一格的档案**——画像 / 关系总述 / 最近碎片，按**对象名**取
  （`MindStore` 的 `peer=` 那一维，批次 `v2-social-card-base` 加的）。检索在装配层做
  （`bootstrap._peer_profile_for`），社交这条线**一个库都不碰**。

红线（本模块逐条钉住）：
- **`card_memory=off` 时那一格整个跳过**：提示词与加它之前**逐字相同**——那一格挤在
  「只给 1 段」那一行的行首、这块文本自己带着结尾的换行，填空串渲染出来一行不多、一字
  不差（接缝断言在 ①）；
- **不串人**：给甲拟的草稿里不许出现乙的记录，也不许出现主人自己那一档（"别把主人那一
  档端出来当对方"）；
- **认不出对象名就不问**：没有名字就没有格，`_peer_profile_provider` 一次都不该被叫到；
- 不改老行为：不接 provider 时（老调用点 / 只接了几个 provider 的测试）整格空着，与加
  这一批之前一致；
- 两库零写入（这一批**纯读**）、零新依赖、不新增网络请求（还是起草那一趟）。

**口径如实记（够不着的地方不装作验过）**：模型被桩掉了，本模块钉的是**喂进去的那一格**
（"提示词里到底有没有这个人、有没有串人"），不是"模型照着写了"——那只能真机看。
`card_memory=off` 只关"喂进提示词"这一半：`_peer_route` 那一次规则判定照算（它是批次一
就有的、零延迟不调模型不碰库、只存内存），所以 off 那条路"逐字相同"指的是提示词与草稿。
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import (  # noqa: E402
    check,
    drain_threads,
    make_env,
    section,
    summary,
)

from brain.prompts.templates import SPEAKER_PEER, SOCIAL_PEER_PROFILE_BLOCK  # noqa: E402
from core.bootstrap import PEER_PROFILE_CLIP, PEER_PROFILE_FRAGMENTS  # noqa: E402
from core.config import ScreenSection  # noqa: E402
from life.dialog import router as dialog_router  # noqa: E402
from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    AUTO_TRIGGER_TEXT,
    NO_EXAMPLES,
    SocialReplyService,
)

# 起草模板自己的换行符（模板文件是 CRLF，渲染出来的提示词里就是 \r\n）
SEP = "\r\n" if "\r\n" in smod.SOCIAL_DRAFT_PROMPT else "\n"

# 两个**不同的人**：各自画像 / 关系 / 碎片，用来验"不串人"。认人只用这几个标记词。
JIA, YI, BING = "甲", "乙", "丙"
JIA_MARKS = ("叫小明", "考研", "聊了三天", "豆豆")
YI_MARKS = ("叫小红", "看展", "认识两年", "做设计")
OWNER_MARKS = ("桌宠的主人", "主人自己那一档的碎片")
# 主人自己那一档写进去的那条碎片（peer=""）
OWNER_FRAGMENT = "主人自己那一档的碎片"
# 超长的一条碎片（验截断）：明显超过 PEER_PROFILE_CLIP
LONG_FRAGMENT = (
    "他上次说想去看极光，还提过想换一份不那么卷的工作，"
    "说这样就能有更多时间陪家里人，这些话都是他随口带出来的"
)
# 起草那一趟认到的对方原话：**末行**才是"对方最新那一句"（批次一的判据）
PEER_TALK = "他问我周末去不去爬山\n今天上班好累啊，开会开到九点"
PEER_LAST = "今天上班好累啊，开会开到九点"

_CFG_DEFAULT = inspect.signature(ScreenSection).parameters["card_memory"].default


def _utterance_rows(db_path: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM utterance").fetchone()[0])
    finally:
        conn.close()


def _seed(app) -> None:
    """往沙箱库（真 sqlite + 真加密）里按对端分格写两个人、加主人那一档。"""
    store = app.mind_store
    store.save_profile(peer=JIA, name="小明", intro="最近在准备考研，压力大")
    store.save_ep(rel_state="刚加了微信，聊了三天", peer=JIA)
    store.add_fragment("他爱吃辣，上次说想吃火锅", peer=JIA)
    store.add_fragment("他养的猫叫豆豆", peer=JIA)

    store.save_profile(peer=YI, name="小红", intro="喜欢看展，做设计的")
    store.save_ep(rel_state="同事，认识两年", peer=YI)
    for index in range(1, PEER_PROFILE_FRAGMENTS + 3):     # 比该取的多两条
        store.add_fragment(f"乙的第{index}件事", peer=YI)

    # 主人自己那一档（peer=""）——老调用点写的就是这儿，绝不能被当成"对方"
    store.save_profile(peer="", name="主人自己", intro="这只桌宠的主人")
    store.add_fragment(OWNER_FRAGMENT, peer="")


def main() -> int:
    # ---------- ① 开关与接缝：off 那条路逐字回到加它之前 ----------

    section("① card_memory 开关 + off 那条路的接缝（与加它之前逐字相同）")

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "行啊，你来定", "why": "接住对方"}]}
        _seed(app)

        check("前置：新开的这一格代码默认就是 auto（跟 tone_align 同一个口径）",
              _CFG_DEFAULT == "auto", repr(_CFG_DEFAULT))
        check("前置：没接过档案那一手的老构造方式仍是 None（老调用点行为不变）",
              SocialReplyService(config=app.config, bus=app.bus)._peer_profile_provider is None,
              "")

        app.config.screen.card_memory = "off"
        check("★ 明写 off 才算关", service._card_memory_on() is False, "")
        never_off = [w for w in ("OFF", "0", "false", "真假", "")
                     if _switch_on(app, service, w) is not True]
        app.config.screen.card_memory = "off"
        check("★ 写别的值（OFF / 0 / false / 真假 / 空）一律按 auto 算"
              "（功能不该因为笔误静默失效）", not never_off, "、".join(map(repr, never_off)))

        app.config.screen.tone_align = "off"          # 两格都关掉，比的才是"加这一批之前"
        service._session_id, service._peer_name = "S1", JIA
        asked: list[str] = []
        service.set_peer_profile_provider(lambda peer: asked.append(peer) or "不该被问到")

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, PEER_TALK)
        off_prompt = stub.prompts[-1]
        baseline = smod.SOCIAL_DRAFT_PROMPT.format(
            chat_context=PEER_TALK,
            text=AUTO_TRIGGER_TEXT,
            examples=NO_EXAMPLES,
            peer_tone_hint="",
            peer_profile="",
        )
        check("★ off 那趟的提示词 == 两个新格子都填空串渲染出来的那一份（一字不差）",
              off_prompt == baseline, f"{len(off_prompt)} 字 vs {len(baseline)} 字")
        check('★ 接缝：""" 与「只给 1 段」之间仍**一个空行**（那一格没占出一行来）',
              f'{NO_EXAMPLES}"""{SEP}{SEP}只给 1 段' in off_prompt, "")
        check("★ off 那趟压根没问过档案那一手（不是问了再扔）", asked == [], str(asked))
        check("★ off 那趟提示词里没有这一批的任何字样",
              "我手上关于他的记录" not in off_prompt
              and "对方刚那句的意图" not in off_prompt, "")
        check("★ 那一格的位置：它挤在「只给」那一行**行首**，自己不是独立一行"
              "（独立一行的话 off 就多出一个空行了）",
              "{peer_profile}只给" in smod.SOCIAL_DRAFT_PROMPT
              and "{peer_profile}" + SEP not in smod.SOCIAL_DRAFT_PROMPT,
              repr(smod.SOCIAL_DRAFT_PROMPT.split("{peer_tone_hint}")[-1][:30]))

        app.config.screen.card_memory = "auto"
        # auto 但一点料都没有那条路：跟"从没接过 provider 的老调用点"一样逐字不变
        bare = SocialReplyService(config=app.config, bus=app.bus)
        stub.prompts.clear()
        bare._write(AUTO_TRIGGER_TEXT, "")            # 这一拍没认到对方的话
        bare_prompt = stub.prompts[-1]
        check("★ auto 但手上一点材料都没有（没认到对方的话 / 没接档案那一手）"
              "→ 照旧逐字相同",
              bare_prompt == smod.SOCIAL_DRAFT_PROMPT.format(
                  chat_context=smod.NO_CHAT_CONTEXT,
                  text=AUTO_TRIGGER_TEXT,
                  examples=NO_EXAMPLES,
                  peer_tone_hint="",
                  peer_profile="",
              ), "")
        check("★ 同一个没料的 service：那一格不是「填了个空壳」而是整格没写",
              bare._peer_profile_block() == "", repr(bare._peer_profile_block()))

        # ---------- ② 对方意图：批次一算好的那格，这一批真的进提示词了 ----------

        section("② 对方意图进提示词（批次一那格终于有人取用）")

        seen: list[tuple[str, str]] = []
        real_classify = dialog_router.classify

        def spy(text: str, speaker: str = dialog_router.SPEAKER_USER):
            result = real_classify(text, speaker=speaker)
            seen.append((text, speaker))
            return result

        smod.dialog_router.classify = spy
        try:
            app.config.screen.tone_align = "off"
            app.config.screen.card_memory = "auto"
            service._session_id, service._peer_name = "S1", JIA
            service.set_peer_profile_provider(app._peer_profile_for)

            stub.prompts.clear()
            service._write("他问我要不要周末一起爬山", PEER_TALK)
            prompt = stub.prompts[-1]
            route = service._peer_route
            check("★ 判的是**对方最新那一句**、传的是 peer（批次一那条线没被改坏）",
                  seen[-1] == (PEER_LAST, SPEAKER_PEER), str(seen[-1]))
            check("★ 意图算出来了（规则那一路，零延迟不调模型）",
                  route is not None and route.intent == dialog_router.INTENT_VENT, str(route))
            check("★ 对方那句的意图进了提示词那一格",
                  f"对方刚那句的意图是「{route.intent}」" in prompt,
                  repr(prompt.split("关于**对方这个人**")[-1][:70]))
            check("★ 情绪也跟着一起（模型据此才知道是「接住」还是「顺着聊」）",
                  route.emotion in prompt, route.emotion)
            check("★ 档案那一半也在同一趟里（两样合成一句）",
                  "我手上关于他的记录" in prompt, "")

            stub.prompts.clear()
            service._write("想个办法", "帮我想个办法，明天怎么跟他说才好")
            second = stub.prompts[-1]
            second_route = service._peer_route
            check("★ 换一句话意图跟着变（不是钉死的固定档）",
                  second_route is not None
                  and second_route.intent != route.intent
                  and f"「{second_route.intent}」" in second,
                  f"{route.intent} → {second_route.intent if second_route else None}")

            # 危机那一支：classify 的危机短路返回的是防御标记，不是"读出来的意图"
            smod.dialog_router.classify = (
                lambda text, speaker=SPEAKER_PEER: dialog_router.DialogRoute(
                    dialog_router.INTENT_CONFIRM, dialog_router.EMOTION_LOW,
                    matched=False, crisis=True,
                )
            )
            stub.prompts.clear()
            service._write("随便一句", "随便一句")
            crisis_prompt = stub.prompts[-1]
            check("★ 危机短路那一支不进这一格（那是防御标记，不是读出来的意图）",
                  "对方刚那句的意图" not in crisis_prompt, "")
            check("反向对照：同一趟里档案那一半照旧在（只挡了意图那句，没把整格关掉）",
                  "我手上关于他的记录" in crisis_prompt, "")
        finally:
            smod.dialog_router.classify = real_classify

        # ---------- ③ 三库档案按对端分格：不串人、不把主人那档当对方 ----------

        section("③ 三库档案按对端分格（真值：从库里那一格取，不串人）")

        app.config.screen.tone_align = "auto"
        service.set_peer_profile_provider(app._peer_profile_for)

        stub.prompts.clear()
        service._peer_name = JIA
        service._write("他问我要不要周末一起爬山", PEER_TALK)
        jia_prompt = stub.prompts[-1]
        missing = [mark for mark in JIA_MARKS if mark not in jia_prompt]
        check("★ 甲的画像 / 关系 / 碎片三样都进了提示词（一个都不少）",
              not missing, "缺：" + "、".join(missing) if missing else "")
        crossed = [mark for mark in YI_MARKS if mark in jia_prompt]
        check("★ 给甲拟的草稿里一个字都没混进乙的记录（不串人）",
              not crossed, "串进来的：" + "、".join(crossed) if crossed else "")
        owner_leak = [mark for mark in OWNER_MARKS if mark in jia_prompt]
        check('★ 主人自己那一档（peer=""）没被端出来当「对方」',
              not owner_leak, "漏进来的：" + "、".join(owner_leak) if owner_leak else "")

        stub.prompts.clear()
        service._peer_name = YI
        service._write("她问我周五加不加班", "她问我周五加不加班")
        yi_prompt = stub.prompts[-1]
        check("★ 换成乙那一场 → 拿到的是乙的记录",
              all(mark in yi_prompt for mark in YI_MARKS),
              "缺：" + "、".join(m for m in YI_MARKS if m not in yi_prompt))
        check("★ 反过来也不串：乙那一场里没有甲的东西",
              all(mark not in yi_prompt for mark in JIA_MARKS), "")
        check("★ 换人之后甲那一格还好好在库里（这一批纯读，谁也没被覆盖）",
              (app.mind_store.profile(peer=JIA) or {}).get("name") == "小明",
              str(app.mind_store.profile(peer=JIA)))

        check(f"★ 碎片只取最近 {PEER_PROFILE_FRAGMENTS} 条（不是把这一格全端出来）",
              "乙的第5件事" in yi_prompt and "乙的第4件事" in yi_prompt
              and "乙的第3件事" in yi_prompt
              and "乙的第2件事" not in yi_prompt and "乙的第1件事" not in yi_prompt,
              "")

        # 档案那一手的分格口径（直接调装配层那只手，绕开提示词）
        assembled = app._peer_profile_for(JIA)
        check("★ 装配层那只手把三样拼成一句能直接进提示词的档案",
              all(part in assembled for part in ("叫小明", "情况是", "眼下关系：", "最近记下的：")),
              assembled)
        check("★ 认不出的人回空串（没这个人就没有格）",
              app._peer_profile_for("查无此人") == "", repr(app._peer_profile_for("查无此人")))
        check("★ 空名字直接回空串（没有名字就没有格，不该拿主人那一档顶）",
              app._peer_profile_for("") == "" and app._peer_profile_for("   ") == "", "")

        app.mind_store.add_fragment(LONG_FRAGMENT, peer=BING)
        clipped = app._peer_profile_for(BING)
        check(f"★ 超长的碎片截到 {PEER_PROFILE_CLIP} 字（背景料不该把提示词撑长）",
              LONG_FRAGMENT[:PEER_PROFILE_CLIP] in clipped and LONG_FRAGMENT not in clipped,
              clipped)

        # 那一块文本自己带着结尾的换行——off 那条路能一字不差就是靠这个
        check("★ 那一块文本自己带着结尾的换行（off 一字不差的机制）",
              SOCIAL_PEER_PROFILE_BLOCK.endswith(SEP * 2),
              repr(SOCIAL_PEER_PROFILE_BLOCK[-6:]))
        check("★ 有料那一趟：整块接在「只给 1 段」前面、中间空一行",
              "关于**对方这个人**" in yi_prompt.split("只给 1 段")[0]
              and f"痕迹。{SEP}{SEP}只给 1 段" in yi_prompt, "")
        check("★ 底牌那条纪律在（这些原文不许出现在回话里）",
              "这些原文一个字都不许出现在回话里" in yi_prompt, "")

        # ---------- ④ 接线与红线 ----------

        section("④ 接线与红线（纯读 / 零新依赖 / 不新增请求 / 不改老行为）")

        check("★ 装配层真把档案那一手接上了（不是留了个没人接的口子）",
              service._peer_profile_provider == app._peer_profile_for, "")

        rows_before = app.mind_store.fragment_count()
        utterance_before = _utterance_rows(app.config.db_path)
        stub.prompts.clear()
        service._write("再来一段", PEER_TALK)
        service._write("再来一段", "她问我周五加不加班")
        drain_threads()
        check("★ 两库零写入：mind.db 碎片一条没多（这一批纯读）",
              app.mind_store.fragment_count() == rows_before,
              f"{rows_before} → {app.mind_store.fragment_count()}")
        check("★ 两库零写入：pet.db utterance 一行没多",
              _utterance_rows(app.config.db_path) == utterance_before, "")

        stub.prompts.clear()
        service._write("就一条", PEER_TALK)
        check("★ 不新增网络请求：起草仍只有那一趟 chat_json",
              len(stub.prompts) == 1, f"{len(stub.prompts)} 趟")

        social_src = inspect.getsource(smod)
        # 只在 **import 那一行**上看：注释里提到 `MindStore` / `mind_store` 不算碰库
        # （那两处正是"检索在记忆模块那边、这条线不碰"的说明）。
        imports = [line for line in social_src.splitlines()
                   if line.lstrip().startswith(("import ", "from "))]
        touched = [line.strip() for line in imports
                   if "sqlite3" in line or "life.mind" in line or "MindStore" in line]
        check("★ 社交这条线还是**一个库都不碰**（import 里没有 sqlite3 / 记忆模块）",
              not touched, "、".join(touched) or "（一处都没有）")
        write_src = inspect.getsource(SocialReplyService._write)
        check("★ _write 里没有取库那只手（档案是从回调进来的）",
              "_peer_profile_provider" in inspect.getsource(SocialReplyService._peer_profile_block),
              "")
        check("★ _write 只搬那一格、不自己去找人（不读库、不新抓屏）",
              "mind_store" not in write_src and "_grab_chat" not in write_src
              and "peer_profile = self._peer_profile_block()" in write_src, "")

        refs: set[str] = set()
        for node in ast.walk(ast.parse(social_src)):
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(alias.name for alias in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex", "chromadb"}
        check("★ 零新依赖：社交这条线没多一个第三方 import",
              not (banned & refs), "、".join(sorted(banned & refs)) or "（一处都没有）")

        check("★ 配置那一格只认 off，写别的按 auto 算（代码默认就是 auto）",
              _CFG_DEFAULT == "auto" and ScreenSection.card_memory == "auto", "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草「对方这个人」注入（v2-social-card-inject）")


def _switch_on(app, service: SocialReplyService, value: str) -> bool:
    app.config.screen.card_memory = value
    return service._card_memory_on()


if __name__ == "__main__":
    raise SystemExit(main())
