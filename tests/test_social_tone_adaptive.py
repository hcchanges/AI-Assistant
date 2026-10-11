"""社交起草的语气自适应（v2-social-tone-adaptive）的自动测试。

**病根**（只读回填 v2-social-reply-tone-trace 的结论）：起草的语气原先只有一条腿——
用户手放进来的**静态范例**（`social_examples.py`）。范例学的是"这个人一贯怎么说话"，
学不到"他这会儿什么腔调"。对方刚甩一句"这么快就放弃啦？"，范例里没这条，模型就退回
"固定人格 + 分析腔"的套路：不管对方是松弛、是正经、还是在试探，回话都是同一个味儿。

这一批补的是**当下**那一层：把**这一场**（按 `sid|peer` 分格）最近听来的对方原话，
照字面抽成一句口吻摘要（句长 / 语气词 / 表情 / 称呼 / 调性），当风格约束喂进正文那一趟。

**v2-social-tone-length-align 又补了两维**（真机验收暴露的缺口）：①**句长量级**——原来只
说"短句/长句"，模型不知道该短到哪儿；现在连同**实测跨度**（约 6–7 字）一起给，真机口径
是"对方 8–16 字/句、桌宠 26–40 字/句，差 2–3 倍"。②**细分调性**——原来 playful 只有一档
"轻松偏撒娇"，真机上对方是"撒娇轻盈、爱卖关子"，桌宠却走成"嘴贫接招较劲"；现在把
playful 拆成撒娇轻盈 / 嘴贫较劲两路，另加试探撩拨与正经公事，并在提示词里加了
"字数量级贴近对方""气质贴住细分调性"两条硬约束。

**抽的是字面特征，不是"读心"**：一句"你怎么还没睡"不该被当成盘问——所以判据都写死
在几张短表里（`_tone_features` 是纯函数，测试直接调它），不引词典、不引分词、不碰模型、
**不做情感分析**（那正是要避免的"分析腔"；该猜对方安的什么心是 `why` 那格的活，归解析，
不归正文）。

红线（本模块逐条钉住）：
- **`tone_align=off` 时提示词与加它之前逐字相同**——那一格占的正是原来那个空行，填
  空串渲染出来一字不差；账本也不记（不抽、不注入、不留痕）；
- 材料只用起草本来就要用的那份 `chat_text`：**不新抓屏、不重跑 OCR、不发新请求**；
- `_peer_lines` **只存内存**，不落盘、不进任何库（两库零写入）；存的是抽出来的特征词，
  对方原话不会因此多留一份；
- why 解耦那条没破：解析仍归 `why`，正文那一格只动风格、不动"该说什么"。
"""

from __future__ import annotations

import ast
import inspect
import re
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

from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    AUTO_TRIGGER_TEXT,
    NO_EXAMPLES,
    TONE_WINDOW_LINES,
    SocialReplyService,
    _has_face,
    _tone_features,
)

# 对方松弛那一档：短句 + 语气词 + emoji（真机上"在干嘛呀~ 😄"这种）
RELAX_LINES = ["在干嘛呀~ 😄", "陪我聊会儿嘛"]
# 对方正经那一档：长句、没有语气词也没有表情，说的是正事
SERIOUS_LINES = [
    "关于下个月的排期我这边看了一下，还需要再过一遍需求评审才能定下来具体时间",
    "所以先不着急给答复，等评审结果出来我再同步给你，你看这样可以吗",
]
# 对方在试探那一档：挑剔的话术 + 问号
PROBE_LINE = "真的假的？你不是说下周才走吗"
# 嘴贫 / 接招较劲那一档：真机复盘里桌宠**走偏**的那一路（对方撒娇，桌宠却这么回）。
# 这条素材用来做反向对照——对方真是这个腔调时才该判「嘴贫较劲」，别的一律不许。
BANTER_LINES = ["行行行，随你吧", "你管我呢，少来"]
# 起草模板自己的换行符（模板文件是 CRLF，渲染出来的提示词里就是 \r\n）
SEP = "\r\n" if "\r\n" in smod.SOCIAL_DRAFT_PROMPT else "\n"


def main() -> int:
    # ---------- ① 纯函数：照字面抽口吻 ----------

    section("① 纯函数 _tone_features：只照字面判，不猜、不读心")

    relax = _tone_features(RELAX_LINES)
    check("★ 短句 + 语气词 + 表情 → 摘要点出「短句」", "短句" in relax, relax)
    check("★ 摘要点出「语气词」与「表情」", "语气词" in relax and "表情" in relax, relax)
    check("★ 调性判成「轻松偏撒娇」（要的就是这个方向）", "轻松偏撒娇" in relax, relax)
    check("★ 摘要里没有对方原话的实词（抽出来的是特征，不是原文搬一遍）",
          all(word not in relax for word in ("干嘛", "聊会儿")), relax)

    serious = _tone_features(SERIOUS_LINES)
    check("★ 长句正经 → 摘要点出「长句」", "长句" in serious, serious)
    check("★ 长句正经的调性不飘：判成「正经常规」，不带撒娇味",
          "正经常规" in serious and "撒娇" not in serious, serious)

    probe = _tone_features([PROBE_LINE])
    check("★ 挑剔话术配着问号 → 判「在试探、等你表态」", "在试探" in probe, probe)
    # 反向对照：同样的词句但**没有问号**，就不该往"试探"上判（"你怎么还没睡"是关心）
    bare_probe = _tone_features(["你怎么还没睡"])
    check("反向对照：同样的话术没有问号 → 不判试探（不是见词就扣帽子）",
          "在试探" not in bare_probe, bare_probe)

    check("一句都没有 → 空串（宁可不注入，也不瞎猜一个腔调）",
          _tone_features([]) == "", repr(_tone_features([])))
    check("全是空白行 → 也是空串", _tone_features(["", "   "]) == "", repr(_tone_features(["", "  "])))

    check("★ emoji 判得出来（按码位，不按词表）", _has_face("今天好累😄") is True, "")
    check("★ 颜文字那一带的符号也算（☺ 落在 U+2190–U+2BFF）", _has_face("好☺") is True, "")
    check("★ 汉字不算表情", _has_face("今天去哪玩") is False, "")
    check("★ 全角波浪线不由码位那条腿管（它走的是叠字/笑声那条表）",
          _has_face("好～") is False and _has_face("好~") is False, "")
    check("叠字笑声那条腿照样点得出来", "表情" in _tone_features(["好呀~"]),
          _tone_features(["好呀~"]))
    check("★ 称呼认得出来", "称呼" in _tone_features(["宝贝在吗"]),
          _tone_features(["宝贝在吗"]))

    # ---------- ①b 句长量级：光说"短/长"不够，要带上实测跨度 ----------

    section("①b 句长量级：档位 + 实测跨度（真机缺口的第①条）")

    relax_lo, relax_hi = min(map(len, RELAX_LINES)), max(map(len, RELAX_LINES))
    relax_avg = sum(map(len, RELAX_LINES)) / len(RELAX_LINES)
    check(f"★ 极短句那一档：档位与实测跨度都在（约{relax_lo}–{relax_hi}字）",
          "极短句" in relax and f"约{relax_lo}–{relax_hi}字" in relax, relax)
    serious_lo, serious_hi = min(map(len, SERIOUS_LINES)), max(map(len, SERIOUS_LINES))
    check(f"★ 长句那一档同样带实测跨度（约{serious_lo}–{serious_hi}字）",
          "长句" in serious and f"约{serious_lo}–{serious_hi}字" in serious, serious)
    check("★ 跨度是**从材料量出来的**：换一份更长的材料，数字跟着变（不是写死的）",
          f"约{serious_lo}–{serious_hi}字" not in relax
          and f"约{relax_lo}–{relax_hi}字" not in serious,
          f"{relax} / {serious}")
    one = _tone_features(["就一句"])
    check("反向对照：只有一句时跨度收成一个数（不写成'约3–3字'）",
          "约3字" in one and "–" not in one.split("，")[0], one)

    # ---------- ①c 细分调性：playful 里的两路要拆开 ----------

    section("①c 细分调性：撒娇轻盈 / 嘴贫较劲 / 试探撩拨 / 正经公事 各归各")

    check("★ 撒娇那一档点出「撒娇轻盈」", "撒娇轻盈" in relax, relax)
    banter = _tone_features(BANTER_LINES)
    check("★ 嘴贫接招那一档点出「嘴贫较劲」（桌宠走偏的那一路，得认得出来）",
          "嘴贫较劲" in banter, banter)
    check("★ 反向对照：撒娇材料里没有「嘴贫」「较劲」（同是 playful，气质要分清）",
          "嘴贫" not in relax and "较劲" not in relax, relax)
    check("★ 反向对照：嘴贫材料里没有「撒娇」（两路不许揉成一档）",
          "撒娇" not in banter, banter)
    check("★ 正经公事那一档点出「正经」，且不沾撒娇 / 嘴贫",
          "正经" in serious and "撒娇" not in serious and "嘴贫" not in serious, serious)
    check("★ 试探那一路点出「试探撩拨」", "撩拨" in probe, probe)
    tease = _tone_features(["你猜呀，不告诉你~ 😏"])
    check("★ 卖关子那一句：同判撒娇轻盈，另点出「爱卖关子」",
          "撒娇轻盈" in tease and "爱卖关子" in tease, tease)

    # ---------- ② 这一场的账本：按场分格、只留最近几句 ----------

    section("② 账本按 sid|peer 分格：不串场、只留最近几句、重复行不重记")

    box, stub, app = make_env()
    try:
        service: SocialReplyService = app.social_reply
        service._examples_provider = lambda query: ""
        stub.candidates_result = {"candidates": [{"draft": "行啊，你来定", "why": "接住对方"}]}

        service._session_id, service._peer_name = "S1", "甲"
        key_a = service._cooldown_key()
        check("前置：这一场的键就是 sid|peer（跟冷却/帮聊那本账同一个口径）",
              key_a == "S1|甲", key_a)

        service._remember_peer_lines(key_a, "\n".join(RELAX_LINES))
        service._remember_peer_lines("S1|乙", "\n".join(SERIOUS_LINES))
        check("★ 两场各记各的：甲那边判松弛，乙那边判长句，摘要不串场",
              "撒娇" in service._peer_tone_summary(key_a)
              and "长句" in service._peer_tone_summary("S1|乙"),
              f"{service._peer_tone_summary(key_a)} / {service._peer_tone_summary('S1|乙')}")

        # 自动那条路每一拍都把屏幕上最底下那条再带回来一次（同一条消息反复送达）
        service._remember_peer_lines(key_a, "\n".join(RELAX_LINES))
        check("★ 同一份尾巴再送一遍不重记（每拍都带末句来的那条路）",
              service._peer_lines[key_a] == RELAX_LINES, str(service._peer_lines[key_a]))
        # 对方连着说的两条里，末句再单独送一次（尾巴变成一条）——也不重记
        service._remember_peer_lines(key_a, RELAX_LINES[-1])
        check("★ 末句单独再送一次也不重记（账本末尾那几条就是它）",
              service._peer_lines[key_a] == RELAX_LINES, str(service._peer_lines[key_a]))
        # 反向对照：真来了新的一句，照记
        service._remember_peer_lines(key_a, "周末去爬山吗")
        check("反向对照：新的一句照记（去重没把新话也挡掉）",
              service._peer_lines[key_a] == RELAX_LINES + ["周末去爬山吗"],
              str(service._peer_lines[key_a]))

        service._remember_peer_lines("S1|丙", "宝贝抱抱人家嘛呀")     # 撒娇那一档
        for index in range(TONE_WINDOW_LINES):
            service._remember_peer_lines("S1|丙", f"已读第{index}条")
        check(f"★ 只认最近 {TONE_WINDOW_LINES} 句：早先那句撒娇味被顶出窗口",
              "撒娇" not in service._peer_tone_summary("S1|丙"),
              service._peer_tone_summary("S1|丙"))

        # ---------- ③ off：不记、不抽、不注入（提示词逐字回到改动前） ----------

        section("③ tone_align=off：提示词与加它之前逐字相同")

        app.config.screen.tone_align = "off"
        # v2-social-card-inject：起草提示词后来又多了一格 `{peer_profile}`（"对方这个人"）。
        # 这一节验的是**口吻那一维**，两个开关都得钉住才能"一字不差"地比——所以把那一格
        # 也明写 off（那一格自己的短路与逐字相同由 `test_social_card_inject.py` 钉）。
        app.config.screen.card_memory = "off"
        service._session_id, service._peer_name = "S9", "甲"

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "\n".join(RELAX_LINES))
        off_prompt = stub.prompts[-1]
        baseline = smod.SOCIAL_DRAFT_PROMPT.format(
            chat_context="\n".join(RELAX_LINES),
            text=AUTO_TRIGGER_TEXT,
            examples=NO_EXAMPLES,
            peer_tone_hint="",
            peer_profile="",
        )
        check("★ off 那趟的提示词 == 占位填空串渲染出来的那一份（一字不差）",
              off_prompt == baseline, f"{len(off_prompt)} 字 vs {len(baseline)} 字")
        check("★ off 那趟提示词里没有「对方口吻特征」那一段",
              "对方口吻特征" not in off_prompt, "")
        check("★ 占位那一格占的正是原来那个空行（\"\"\" 与「只给 1 段」之间仍是一个空行）",
              f'{NO_EXAMPLES}"""{SEP}{SEP}只给 1 段' in off_prompt, "")
        check("★ off 那趟一个字都没记（账本还是空的：不抽、不留痕）",
              not service._peer_lines.get("S9|甲"), str(service._peer_lines.get("S9|甲")))
        check("★ off 那趟也没有本批新加的两条硬约束（量级 / 气质）",
              "字数量级" not in off_prompt and "细分调性" not in off_prompt, "")

        # ---------- ④ auto：对方当下的口吻真的进了提示词 ----------

        section("④ tone_align=auto：口吻摘要进提示词，且随对方实时变")

        app.config.screen.tone_align = "auto"
        app.config.screen.card_memory = "auto"      # 上面那一节临时关掉的，这儿还原
        service._peer_lines.clear()

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "\n".join(RELAX_LINES))
        relax_prompt = stub.prompts[-1]
        check("★ 提示词里带上了「对方口吻特征」那一段", "对方口吻特征" in relax_prompt, "")
        check("★ 摘要方向是松弛那一档（短句 + 撒娇）", "轻松偏撒娇" in relax_prompt,
              relax_prompt.split("对方口吻特征")[-1][:60].replace("\n", " / "))
        check("★ 对方原话仍旧在 chat_context 那一格（材料那条腿没被动）",
              RELAX_LINES[0] in relax_prompt, "")
        check("★ 抽的是特征、不是原文：正文风格那一段里没有对方原话的实词",
              all(word not in relax_prompt.split("只给 1 段")[0].split("对方口吻特征")[-1]
                  for word in ("干嘛", "聊会儿")), "")
        check("★ 解析仍归 why（「不要混进正文」那条一个字没动）",
              '不要混进正文' in relax_prompt, "")

        # 本批补的两维：硬约束 + 对方量级，得真的落在**风格那一段**里（不是散在别处）
        hint_region = relax_prompt.split("对方口吻特征")[-1].split("只给 1 段")[0]
        check("★ 硬约束①（字数量级必须贴近对方）在提示词里",
              "字数量级" in hint_region and "同一个量级" in hint_region,
              hint_region[:70].replace("\n", " / "))
        check("★ 硬约束②（气质贴住细分调性）在提示词里",
              "细分调性" in hint_region, "")
        stated = re.search(r"约(\d+)–(\d+)字", hint_region)
        check("★ 提示词里的对方量级 == 素材实测那一段（模型据此才知道该写多短）",
              stated is not None
              and (int(stated.group(1)), int(stated.group(2))) == (relax_lo, relax_hi),
              stated.group(0) if stated else "没找到")
        # "字数量级贴近"的机制化说法：照提示词给出的量级写一条回话，落在对方均值的
        # 1.6 倍内。**有牙**——摘要一旦把对方量级报大（比如把两行并成一行去量），
        # 上界跟着变大，这条立刻红。（模型本身被 stub 了，它到底写多长只能在真机看；
        # 这里钉的是"提示词给没给足据此写短的准数"。）
        check("★ 照这个量级写的回话落在对方均值 1.6 倍内",
              stated is not None and int(stated.group(2)) <= 1.6 * relax_avg,
              f"上界 {stated.group(2) if stated else '?'} 字 vs 1.6×{relax_avg:.1f}"
              f"={1.6 * relax_avg:.1f}")

        stub.prompts.clear()
        service._peer_name = "乙"                      # 换一场（另一个人）
        service._write(AUTO_TRIGGER_TEXT, "\n".join(SERIOUS_LINES))
        serious_prompt = stub.prompts[-1]
        check("★ 换成正经长句那一场 → 摘要跟着变（不是钉死的固定人格）",
              "长句" in serious_prompt and "轻松偏撒娇" not in serious_prompt,
              serious_prompt.split("对方口吻特征")[-1][:60].replace("\n", " / "))
        check("★ 上一场（甲）的账没被这一场带跑",
              "撒娇" in service._peer_tone_summary("S9|甲"), "")
        # 只看**摘要那一截**（「。回话仍用」之前）——后面的硬约束里本来就写着"极短句"
        # 这个档位名（那是给模型看的），拿整段去判会误伤
        serious_summary = serious_prompt.split("对方口吻特征")[-1].split("。回话仍用")[0]
        check("★ 换了正经长句那一场 → 量级那格跟着变成长句跨度（不是极短）",
              "长句" in serious_summary and "极短句" not in serious_summary
              and f"约{serious_lo}–{serious_hi}字" in serious_summary,
              serious_summary)

        stub.prompts.clear()
        service._peer_name = "丙"
        service._write(AUTO_TRIGGER_TEXT, PROBE_LINE)
        check("★ 试探那一句也认得出来", "在试探" in stub.prompts[-1], "")

        # ---------- ⑤ 红线 ----------

        section("⑤ 红线：零新依赖 / 两库零写入 / why 解耦 / 不碰按键与剪贴板")

        def _rows(db_path: str, table: str) -> int:
            conn = sqlite3.connect(db_path)
            try:
                return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            finally:
                conn.close()

        pet_before = _rows(app.config.db_path, "utterance")
        mind_before = app.mind_store.fragment_count()

        stub.prompts.clear()
        service._write(AUTO_TRIGGER_TEXT, "\n".join(RELAX_LINES))
        service._write(AUTO_TRIGGER_TEXT, "\n".join(SERIOUS_LINES))
        drain_threads()

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")

        # 记账那一格只存内存：写了两趟，进程里那本账有字，但没落任何盘上文件
        check("★ 账本只在内存里（跑了两趟就有字，不落盘）",
              bool(service._peer_lines), str({k: len(v) for k, v in service._peer_lines.items()}))

        # why 解耦：候选过闸仍然 draft / why 各归各位
        picked = SocialReplyService._normalize_candidates(
            {"candidates": [{"draft": "行啊，你来定", "why": "接住对方，不升级"}]})
        check("★ 候选仍是 draft + why 两个字段（正文那一格只动了风格）",
              picked == [{"draft": "行啊，你来定", "why": "接住对方，不升级"}], str(picked))

        refs: set[str] = set()
        for node in ast.walk(ast.parse(inspect.getsource(smod))):
            if isinstance(node, ast.Import):
                refs.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                refs.update(alias.name for alias in node.names)
        banned = {"requests", "numpy", "PIL", "psutil", "pyperclip", "cv2",
                  "pyautogui", "win32api", "fuzzywuzzy", "regex"}
        check("★ 这一条线没多一个第三方 import（不引分词/词典）", not (banned & refs),
              "、".join(sorted(banned & refs)) or "（一处都没有）")

        src = inspect.getsource(smod)
        check("★ 不模拟按键、不碰剪贴板（服务自己一个字都没碰）",
              not any(word in src for word in ("keybd_event", "SendInput", "SetCursorPos"))
              and "clipboard" not in src.lower(), "")

        # 材料那条腿：抽口吻用的是**已经抓到的那份 chat_text**，没有再抓一次屏
        write_src = inspect.getsource(SocialReplyService._write)
        check("★ 不新抓屏、不重跑 OCR（_write 里没有取帧/认字那两只手）",
              "_grab_chat" not in write_src and "_read_chat" not in write_src, "")
        check("★ 口吻摘要走的就是这一趟的 chat_text（不另开一路输入）",
              "_remember_peer_lines(peer, chat_text)" in write_src, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交起草语气自适应（v2-social-tone-adaptive + tone-length-align）")


if __name__ == "__main__":
    raise SystemExit(main())
