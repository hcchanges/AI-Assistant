"""社交帮忙回复（v2-social-reply）：在社交软件里不知道话怎么接，宠物替你起一段草稿。

形状（用户定的）：

    社交软件在前台 ──► 冒一句"要我帮你措辞吗"          ← 只认进程名，不读聊天内容
                             │
                            ├──► 点 [好呀] ──► 当场照对方末句拟（处境那格留空）───┐
                            ├──► 点 [不用了] ──► 到此为止，静默陪着，一句不多说    │
                            └──► 直接在聊天里说"帮我回一下…" ───────────────────┤
                                                                                ▼
                                                                            生成草稿
                                                                                │
                                              └──► 宠物非模态气泡亮出「正文（方框）
                                                   + 解析」，底下挂一个「复制回复」
                                                   按钮——按不按、发不发，全在用户手上

**"要/不要"那道门（v2-social-flow 补的）**：冒话原先只是一句话，气泡上不露轻按钮，
用户看不到任何可点的东西，功能到那句台词就断了。现在冒话与"抓屏笔记/游戏攻略/建卡"
四条线走同一套轻按钮：`bus.social_offer` → 气泡露 [好呀] [不用了] → `confirm()` 分叉。

**三件事钉死在这块。**

1. **不碰聊天软件的"数据"——不模拟按键、不读剪贴板、不做 UI 自动化。** 全仓库没有那
   几只手的任何一行。**v2-social-fewshot 之后多了一只范围钉死的手**：社交软件在前台、
   宠物冒邀请那一拍，截一次**屏幕**、OCR 出上面的字（跟 monitor 认视频字幕同一套），
   当"对方最近说了什么"用——**看一眼、用完就丢**，见下面那一段。
2. **不自动发送。** 草稿只在宠物自己的非模态气泡里亮出来，用户自己粘出去。草稿是
   "给人的建议"，不是"替人做的决定"，发不发、改不改、发给谁，全在他自己手上。
   **v2-social-chat-ui-copy 批次11 起，抄进剪贴板这一步归用户点**：气泡上正文底下挂着
   一个「复制回复」，他按了才写（老口径是"生成那一刻顺手替他放进剪贴板"）。这只手只
   **写**，全仓库仍然没有**读**剪贴板 / 监听剪贴板的那条腿；替他把字放进剪贴板不等于
   替他发出去，屏幕上那扇聊天窗我们一行都没碰。
3. **草稿是用户的话，不是宠物的话。** 提示词里钉了这一条：用他自己的口吻写，不带宠物
   的腔调——带着宠物腔发出去，收到的同事会愣一下。

**第 1 条在 v2-social-fewshot 改了口径（用户拍板）。** 真机上"答非所问"的根因是：
这条线只拿到"用户要回消息"的意图，**不知道对方到底说了什么**，于是自己脑补话题——
对方一句"今天玩得开心吗"，它回一段"关于咱们俩的事我想认真聊一下"。

现在它多了一只**有范围的手**：在**社交软件还在前台、宠物刚冒邀请那一拍**，截一次
前台窗口、OCR 出屏幕上的字，当作"对方最近说了什么"喂给起草提示词。红线从"绝不看"
改成"**看一眼、用完就丢**"，范围钉死：

- **只在这一拍抓**：用户切到宠物打字时前台已经是宠物自己，那时候抓只会抓到宠物窗口，
  所以抓的时机就是"社交软件在前台"这一拍（含主动求助那一拍），不是每一拍；
- **只在社交软件 + OCR 开着时**才抓（v2-social-ocr-path 起不再绑定"要冒邀请"，主动求助
  那一拍也会抓），冒话照旧跟冷却（默认 30 分钟）；
- **只留文字、图当场丢**，文字只活在这一场的内存里，**起草完立刻清空**——不落盘、
  不进 pet.db / mind.db / 卡库、不长期存（`_read_social_screen` / `_take_chat_text`）；
- 抓屏与 OCR 这两只手的**实现放在装配层**（`core/bootstrap.py` 接 `shot` 与 `OcrReader`），
  这个模块自己不 import 任何截屏 / OCR 的库——服务只管"什么时候抓、文字怎么用"。

**风格范例（v2-social-fewshot 第二格）**：全局 `<聊天范例>/` 与对象攻略卡的
《聊天范例.md》两路 few-shot，由 `life/screen/social_examples.py` 取出、按相关度挑几条，
只学语气口吻。**只读用户自己放进去的文件，不写任何库。**

触发方式用户选的"直接在聊天里说"：宠物在社交软件前台时冒一句邀请（把那句该怎么说
一并教了），之后用户在聊天框里说"帮我回一下，同事问我…"就出草稿。**不要求请求时
社交软件还在前台**——用户切到宠物这儿打字的那一刻，前台就是宠物自己，要求它反而是
在给自己下绊子。

**[好呀] 之后为什么还要再问一句"是什么情况"**：草稿唯一的原料是用户自己讲出来的处境
（红线：不许编），而冒话那一刻手边只有进程名一个字符串。所以 [好呀] 不是"马上出草稿"，
是"我接下这活儿，你说吧"——用户讲完处境，草稿才出。这段等待有个窗口（`SITUATION_WINDOW_S`），
过了窗口他再怎么聊都按普通聊天走，不会把三天后一句不相干的话劫成草稿。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from typing import Callable

from PySide6.QtCore import QObject, QTimer

from brain.llm.base import LLMOfflineError
from brain.llm.factory import LLMFactory
from brain.prompts.templates import (
    SOCIAL_DRAFT_PROMPT,
    SOCIAL_DRAFT_SYSTEM,
    SOCIAL_PROFILE_PROMPT,
    SOCIAL_PROFILE_SYSTEM,
)
from core.config import Config
from core.events import EventBus
from core.worker import run_async
from life.cards.templates import EMOTION_CATEGORY
from life.psych import crisis
# 点头的口径与抓屏笔记/游戏攻略共用一张表（"好呀""要""嗯"…），不另立一份，
# 免得同一个按钮在三条线上判出三种结果。
from life.screen.notes import YES_WORDS
# 抓来的那屏字"是谁说的"（v2-social-ocr-isolate）：气泡色分类与切分放在隔壁那个纯判断
# 模块里，这个模块只 import 它的纯函数——`shot` / `OcrReader` 一个都不出现在这儿，
# S3 那条"服务自己不 import 截屏 / OCR 库"的红线一分没松。
from life.screen.social_window import (
    SENDER_OTHER,
    SENDER_USER,
    ChatRead,
    as_chat_read,
    tidy_line,
    tidy_lines,
)
from monitor.category import resolve_category
# 空闲时长（多久没敲键没动鼠标）：自动监听那条线拿它判"这一下是我发的还是对方发的"。
# 跟 monitor 共用同一个系统原语，不另开采样；测试里注入假时钟换掉它。
from monitor.foreground import idle_seconds as _system_idle_seconds

logger = logging.getLogger(__name__)

# 冒话（v2-social-chat-ui-copy 批次11 改短）：只说"要不要我帮你回"，不再先教一遍怎么用。
# 老那句（"哪句不好回，跟我说一声——把情况讲给我听，我帮你想想措辞"）是替一个**已经不
# 存在**的流程写的：那时候点 [好呀] 之后还要用户再讲一遍处境，所以冒话里得先教会他。
# 现在点 [好呀] 直接照对方末句拟（见 `confirm()`），这句就只剩"要不要"这一问了。
ASK_LINE = "聊天么？要不要我帮你回呀？"

# 生成不出来时如实说，别给一段编的（跟游戏攻略、知识库一个态度）
FAILED_LINE = "这段我一时想不好，要不你把想说的意思再讲细一点？"

# 断网兜底候选（v2-offline-fallback）：网络层失败时不报 FAILED_LINE，改给这一条本地
# 候选——结构照旧（draft + why），界面无感；联网恢复后 _write 自动回到 API。
OFFLINE_CANDIDATES = {
    "candidates": [
        {
            "draft": "网络断了，这句先帮不上你，联网了我马上帮你拟~",
            "why": "断网兜底：这会儿没联网，没法按你的处境给像样的回复",
        }
    ]
}

# [好呀] 点完之后，等他说处境的窗口（秒）。过了这个窗口就不再等着——
# 不然他点了[要]又去忙别的，三天后随口一句"今天天气不错"会被当处境写成草稿。
#
# **v2-social-chat-ui-copy 批次11 起没人再装这个窗口了**：点 [好呀] 当场照对方末句拟
# （见 `confirm()`），不再有"正等他讲处境"这一段。这一格与 `_waiting_for_situation`、
# `is_request` 里那条腿都原地留着（本批只拆"装窗口"那一下，不拆机制本身——拆它要动
# `is_request` 的判定口径，那是另一件事）。
SITUATION_WINDOW_S = 180

# 对方最新那条：从聊天窗口 OCR 出来的字最多留多少（v2-social-fewshot）。截屏里还有
# 联系人列表 / 工具栏 / 自己发的那半边，都不是"对方最近说了什么"；喂给模型的这段
# 留短一点，让它专心认对方那条，别被整屏杂字带跑。
#
# 这是个**约数**：留尾时按整行往前捡（`_tail_lines`），所以实际长度会在 600 上下浮动
# ——界线落在两行之间，绝不落在句子中间（v2-social-multiline）。
CHAT_CONTEXT_CLIP = 600
# 手上那屏字算"还新鲜"的时限（秒，v2-social-stale-guard）。
#
# `_chat_text` 是"上一次真抓成的那一屏"，起草时抓不成（前台是宠物自己 / 社交窗被挡 /
# 没开 OCR）才会用到它。两难的取舍：
#
# · 掐得太死（一律不用）→ 把**最常见那条路**一起掐死：用户在微信里看完消息、点回宠物
#   的聊天窗说"帮我回"，这一拍前台已经是宠物自己，抓**必然**抓不成，全靠刚认回来的
#   那屏字顶着；
# · 放得太松 → 几分钟前**另一场戏**里的旧话会被当成"对方刚发来的那条"接过去，用户
#   看到的是答非所问，而且一行日志都没有。
#
# 2 分钟：够覆盖"看完 → 切回来 → 说完一句话"这一串（十几秒量级），又远短于"上一场
# 聊天"的间隔。过期的**当作没有**，如实填 NO_CHAT_CONTEXT 并记一条 warning。
CHAT_TEXT_FRESH_S = 120.0
# 风格范例整段最多塞多少字（挑出来的几条合起来）。范例只学口吻，多了反而挤掉正事。
EXAMPLES_CLIP = 900
# 认不到对方那条时，提示词里那格填这一句——**明说没认到**，让模型别猜（提示词要求第 2 条）。
NO_CHAT_CONTEXT = "（没认到对方说了什么）"
# 一条范例都没有时那一格填的（跟上面一个态度：如实说没有，别现编）
NO_EXAMPLES = "（没有可参考的范例）"
# 点了 [好呀] 而对方那句**一个字都没读到**（当场强制即时抓拍之后仍旧为空）时，宠物
# 如实说这一句——**不拿 `NO_CHAT_CONTEXT` 那格去让模型瞎写一段**，也不落"刚看到消息"
# 这类兜底占位（那等于说"我看见了"，而这一拍压根没看见）。批次17（甲）。
NO_PEER_LINE_HINT = "我还没读到对方说了什么，稍等。"

# ---- 自动监听（v2-social-auto） ----
# 社交软件在前台时，每这么久轻量扫一眼"画面变没变"（只比指纹，不 OCR）；变了才认字。
# 1.5 秒对聊天节奏绰绰有余，又足够"对方回消息刹那"就接上，不抢、不刷屏。
LISTEN_POLL_MS = 1500
# 判"这一下是用户自己刚发的"的空闲阈值（秒）：最近这么点时间里有键盘/鼠标动作，
# 多半是用户在敲、在发，而不是对方回了新消息。
#
# **这是色卡那条腿（v2-social-ocr-isolate）认不出来时的兜底。** 气泡底色分得出谁说的
# 时候直接用色卡，压根轮不到这一条——它只在换了客户端 / 深色主题、颜色对不上时才顶上。
SEND_IDLE_S = 2.5
# 两次自动拟候选之间至少隔这么久（秒）：同一条消息 OCR 认两遍 / 连发两条，别连拟两段。
LISTEN_COOLDOWN_S = 8.0
# 拟完交给用户之后，这一屏**锁**这么久（秒，v2-social-copy-clean）。
#
# 锁的是两件事：①`_done` 置上之后，只要画面尾巴跟完成那一刻一样，一拍都不再重开；
# ②另外再压一道短期冷却——用户去粘贴的那几秒里，聊天窗难免重绘（滚动条、焦点圈、
# 光标），OCR 认出来的字会抖一下。老口径只靠"字变没变"，那一抖就够它再拟一段，
# 真机上看着就是"复制完卡死"。
SOCIAL_DONE_COOLDOWN_S = 20.0
# 邀请那排按钮挂多久算"用户不会理了"（秒，v2-social-fg-residual）。
#
# `_awaiting` 是"正等用户点 [好呀] / [不用了]"，而 `_poll_listen` 头一句就拿它
# early-return：用户既不点、也不关气泡（切走了、被别的事绊住了、压根没注意），
# 这一档能一直挂着——自动监听从此**一声不响**，人明明还在跟对方聊，宠物再也不接。
#
# 这个阈值是"还当他在想"与"别再等了"之间的那条线：比 `SEND_IDLE_S`（2.5 秒）与
# 两道冷却（8 / 20 秒）都长得多，又不至于把气泡上那排按钮留到用户早忘了它。
OFFER_HANG_S = 90.0
# 点过 [好呀] 之后、那一问还挂着等对方那句时，自己看前台的间隔（毫秒，v2-social-on-demand-snap）。
#
# 真机 2026-10-08 20:09:47–20:10:28 那一段：用户点 [好呀] 那一刻前台是宠物自己
# （`_grab_chat` 抓不到微信那块像素），于是只能如实说一句"还没读到"；他切回微信之后，
# 得**再点一次**才有结果——断点就在这两次点击之间。等的那几秒里，唯一在动的是
# `monitor.foreground_changed`（**前台换人了**这条信号是每 5 秒采一次样才发的），
# 外加每一趟 OCR 2.2~5.2 秒。这条腿把前面那 5 秒压掉：挂住期间每 500 毫秒自己看一眼，
# 社交窗一回来立刻抓一屏（OCR 那 2~5 秒省不掉，它是真在认字）。
#
# 500 不是随手取的：比 `LISTEN_POLL_MS`（1500）细，切换回来那一瞬不至于漏掉半秒以上；
# 又不至于把空闲的抓屏（`_grab_chat` 在非社交前台当场 return None，几乎不耗）堆起来。
PENDING_SNAP_MS = 500
# 自动监听这一拍直接起草时的"用户话"那一格：他没开口，这里给一个固定的处境说明。
AUTO_TRIGGER_TEXT = "对方刚回了消息，帮我拟一段回过去的话"

SOCIAL_CATEGORY = "社交"

# 候选回复列完之后，问一句要不要建「对象攻略卡」（v2-social-reply-ext）。建卡归生活
# 模块的「情感」大类——这是 9 大类里的一个，复用 EMOTION_CATEGORY 免得字符串两处漂。
ASK_CARD_LINE = "这段回话之外，要不要我给你建一张「对象攻略卡」，把对方的信息记下来？下次约会、送礼、找话题都用得上。"

# 建对象攻略卡的品类与卡名（卡名 ≤10 字）。
CARD_CATEGORY = EMOTION_CATEGORY
GUIDE_CARD_TITLE = "对象攻略卡"
GUIDE_CARD_ICON = "💗"
GUIDE_CARD_GOAL = "把对方的信息记在这儿，约会、送礼、找话题都用得上"

# 判"这句是在跟我要草稿"。**宁可漏判**（跟知识库那张词表一个态度）：
# 认不出来就当普通聊天，走原来那条路，一个字都不变。所以这里只收**明确在求文**的说法，
# 不收"怎么回"这种半截话——"怎么回去"也会命中它，一个误判就把别的话劫走了。
DEFAULT_DRAFT_WORDS: tuple[str, ...] = (
    "帮我回", "帮我回复", "帮我写回", "帮我写个回", "帮我写条回",
    "帮我措辞", "帮我组织一下话", "帮我组织下语言", "帮我拟一段", "帮我拟个",
    "帮我写一段话回", "不知道怎么回他", "不知道怎么回复", "该回什么",
)


# 聊天窗里那些**不是谁说的话**的行：时间戳 / 日期分隔 / 系统提示（v2-social-tail-context-fix）。
#
# 它们在界面上是"深字打在浅底上"，色卡一律认成**对方说的**——真机 2026-10-07 那屏（茗风）
# 认下来是这样：`昨天22:09` / `00:19` / `12:53` / `14:22` / `你撤回了一条消息` 五条全判成
# other，于是材料成了"时间戳 + 撤回提示 + 真话"的一锅粥。
#
# 判据**只看字面**（用户拍板："按已知文本特征过滤，不靠颜色猜"）：色卡那些阈值是给气泡
# 用的，这些行压根没有气泡，硬拿颜色去认只会认错。
# 两条互斥的形状：①[日期][时段]时间 ②光秃秃一个日期词（有的版本只印"昨天"/"星期二"）
_CHAT_DATE = r"昨天|今天|前天|星期[一二三四五六日天]|\d{1,2}月\d{1,2}日|\d{4}年\d{1,2}月\d{1,2}日"
_CHAT_TIME_LINE = re.compile(
    rf"(?:(?:{_CHAT_DATE})[\s·]*)?(?:上午|下午|中午|凌晨|晚上)?\s*\d{{1,2}}:\d{{2}}(?::\d{{2}})?"
    rf"|(?:{_CHAT_DATE})"
)
_CHAT_CHROME_LINE = re.compile(
    r"以下为新消息|查看更多消息|现在可以开始聊天了|该消息已过期|"
    r"消息已发出，但被对方拒收|撤回了一条消息|拍了拍|\d+\s*条新消息"
)

# **输入框空着时的占位提示**——跟上面的"装潢"是一回事：它不是谁说的话（v2-social-ui-batch 批次8-B）。
#
# 真机 2026-10-07 19:04-19:05 那四趟模型 POST 里，有两趟的材料就是这行字（一趟纯它 10 字，
# 一趟是「那我该不该接受呢」+ 它）：微信输入框空着时打的**灰字占位**，
# 「按住 鼠标语音输入文字」。它满足"深字打在浅底上"，色卡照旧认成**对方说的**；而它又
# 恰好长在输入框里，本该被裁掉那一刀切走——可那一刀是 `input_cut_px` 取 `max(...)`，
# 输入框顶一旦离底边不够远、`detect_input_top` 又走到它那几条降级返回上，这行字就漏进材料。
# 于是模型收到的是"对方说了：按住鼠标语音输入文字"，真机就是这么回出
# 「行，那你先按着语音输入，我等你」的。
#
# 修法：**认字面**，跟时间戳 / 系统提示同一个态度（用户拍板过："按已知文本特征过滤，不靠颜色猜"）。
# 比"再去修那个裁切阈值"稳：裁切那条腿有四个降级返回，还依赖 dpr 与窗口几何，真机上
# 已经证明守不住；而这几行字**是客户端写死的**，字面就是最硬的判据。
#
# 逐个**整行**比（`tidy_line` 之后的内空白也folded掉，OCR 偶尔吐成"按住 鼠标语音输入文字"），
# 不做子串匹配：真有人发来一句"你按住鼠标语音输入文字试试"，那是人话，不该被吞掉。
_CHAT_PLACEHOLDER_LINES = (
    "按住鼠标语音输入文字",
    "按住鼠标说话",
    "按住说话",
    "轻触输入，长按说话",
)
# 比之前先把空白抹平（`"".join(...split())`）：OCR 吐出来的空格位置没准，
# 「按住 鼠标语音输入文字」和「按住鼠标语音输入文字」得算同一行。
_CHAT_PLACEHOLDER_FOLDED = frozenset("".join(p.split()) for p in _CHAT_PLACEHOLDER_LINES)


def _is_chat_placeholder(text: object) -> bool:
    """这一行是不是**输入框空着时的占位提示**（不是谁说的话）。

    只认**整行**等于客户端写死的那几句（见 `_CHAT_PLACEHOLDER_LINES`），空白一律先抹平
    再比。夹着别的内容的行不认：那可能是真话，宁可留着。
    """
    body = "".join(tidy_line(text).split())
    return bool(body) and body in _CHAT_PLACEHOLDER_FOLDED


def _is_chat_chrome(text: object) -> bool:
    """这一行是不是聊天窗的**装潢**（时间戳 / 日期分隔 / 系统提示 / 输入框占位），不是谁说的话。

    三种形状算装潢：**整行就是一个时间**（`14:22` / `昨天22:09` / `2026年10月7日 下午2:22`，
    也含光秃秃一个日期词）、**整行命中系统提示那几个固定说法**（撤回 / 拍了拍 /
    以下为新消息 …）、或者**整行就是输入框空着时的灰字占位**（`按住鼠标语音输入文字`，
    v2-social-ui-batch 批次8-B）。

    故意**不做**的三件事：不认"行为像时间"但夹着别的内容的行（"我下午3:00到"是人话）、
    不认单个标点 / emoji（那是气泡里的内容，宁可留着）、不按颜色猜。
    """
    body = tidy_line(text)
    if not body:
        return True
    if _CHAT_CHROME_LINE.search(body):
        return True
    if _is_chat_placeholder(body):
        return True
    return bool(_CHAT_TIME_LINE.fullmatch(body))


# **会话总列表**在**文本这一层**长什么样（v2-social-listview-leak-fix 16-β）。
#
# 真机 2026-10-08 12:32:59 那一帧坐实的漏法：整页就是会话总列表，OCR 读出 37 行，
# **每一行的字都打在白底上**——色卡那条判据（`is_other_bubble`：最暗通道 ≥210 且通道
# 极差 ≤32）白底浅灰底都过，于是 37 行全被判成"对方说的"；`tail_sender` 成了 'other'、
# `_classify_incoming` 定案 "incoming"，`_tail_context` 再把末行那个纯时间戳当装潢挑掉，
# **"对方原话"于是落到上一行 = 昵称「茗风」**（就这两个字）。
#
# 宽版两栏下这件事结构上不会发生：左界那一刀（`SIDEBAR_PX + CHAT_LIST_PX`）把列表那一列
# 切在裁剪框外面。可这台机器上微信是**窄版单页**（帧宽 742，x=465 那条版面线在窗口里
# 没有对应物），那一刀什么都没切掉——列表整页进了 OCR。几何那一半一时修不了，就先在
# **文本这一层**把这一屏认出来：跟 15-Y 认 `[草稿]` 同一个态度——按已知文本特征判，
# 不靠颜色猜。
#
# 判据就一条，看的是**"时间戳有多密"**：一场单聊里一个时间分隔表示"隔了 5 分钟以上"，
# 一屏至多两三条；而**总列表是每行一条**（那是"这个会话上次说话到现在"）。真机实测：
# 三张列表帧 12 / 12 / 12 条纯时间行（占全屏 32% / 44%），两张单聊帧都是 3 条（12% / 22%）。
#
# **自己发过话就一票否决**：列表页上不可能有绿气泡，而"只收对方话、我一句没说"的单聊
# （自己 0 行）恰恰是宠物最该帮忙的时刻——那种屏上时间戳照样只有两三条，本来就过得去。
LIST_PAGE_TIME_ROWS = 5

# 第二条腿（v2-social-listview-leak-fix-judge）：**"时间列根本没读出来"的列表帧**。
#
# 真机 2026-10-08 14:27:49 / 14:29:54 两帧坐实的第二类漏法：整页还是会话总列表，
# 可这一拍 OCR **一条纯时间行都没读全**（142749 读到 0 条、142954 读到 3 条，都够不着
# 上面那 5 条）。原因是窄版单页把每行读成"昵称一行 + 预览一行"（142749 26 行里
# 一行时间都没有），时间那一列压根没进材料——**时间戳密度这条腿在这种帧上是瞎的**。
#
# 所以补一条**只看字面、不看数量**的腿：列表行**才有的**东西，认出来的字里有**几类**。
# 三类互不依赖，各算一票：
#   · 账号号——`公众号` / `服务号` / `订阅号` / `折叠的群聊`（微信自己印在那几行的角标）
#   · 未读计数——`[17条]` 这种（**单聊里没有这东西**，它只出现在总列表每行的角上）
#   · 群名行——行尾就是"群"字的短行（`宝安固戌地铁站周边二手闲置2群`）
#
# **为什么要"几类"而不是"几行"**：一类能凑够两行太容易——朋友连发两句"你看这个公众号"
# 就成了两行。要**两种不同类型的标记**同时在屏上，1:1 单聊里几乎不可能出现。
#
# 真机 47 帧上的分离度（`%TEMP%/p18/probe_judge.py`，2026-10-08 12:32–15:55 那一批；人工先按
# 认出来的字标了 28 帧——21 聊天 / 5 列表 / 1 其它 / 1 待定，其余 19 帧按同样两类特征归成
# 12 聊天 / 7 列表）：**33 帧单聊三类标记全是 0**，漏判的那几帧列表是 `2/5/3`、`2/4/2`、
# `2/6/2`（账号号 / 未读 / 群名），三类都齐。老阈值 5 一个字没动——它管的是"时间列读得全"
# 那种帧，两条腿各管一半。
LIST_PAGE_MARK_KINDS = 2

# 三类标记的字面（与上面那段注释一一对应，改一处就得改另一处）。
_LIST_MARK_ACCOUNT = re.compile(r"公众号|服务号|订阅号|折叠的群聊")
_LIST_MARK_UNREAD = re.compile(r"\[\d+\s*条\]")
_LIST_MARK_GROUP = re.compile(r"[\u4e00-\u9fa5A-Za-z0-9]{2,20}群$")
# 群名那一类只认短行：预览行可能很长，而真的人话也可能拿"群"字收尾（"你加一下那个群"），
# 加个长度上限让它别把长句子算进来——三类要凑够两类的门槛才是最后那道闸。
_LIST_MARK_GROUP_MAX = 22


def _list_mark_kinds(bodies: list[str]) -> int:
    """这一屏认出来的字里，**有几类**「只有会话总列表才有」的字面。"""
    kinds = 0
    if any(_LIST_MARK_ACCOUNT.search(body) for body in bodies):
        kinds += 1
    if any(_LIST_MARK_UNREAD.search(body) for body in bodies):
        kinds += 1
    if any(_LIST_MARK_GROUP.search(body) and len(body) <= _LIST_MARK_GROUP_MAX
           for body in bodies):
        kinds += 1
    return kinds


def looks_like_list_page(read: object) -> bool:
    """这一屏认出来的字**像不像一页会话总列表**（而不是一场单聊）。

    命中就是"这一拍不是 active 的 1:1 聊天视图"。装配层据此把这一份 `ChatRead` 清成空的
    （`core.bootstrap._read_social_chat`）：不 `_tail_context`、不 `_auto_generate`，
    列表里那些昵称 / 预览行一个都不进材料；**自动与手动两条路共用这一份空 read**。

    **判据是"时间戳密度"与"列表专有字面"两条腿，任一条成立就算**：

    一、整行就是一个时间的行数 ≥ `LIST_PAGE_TIME_ROWS`（5）。**绝对数、不是比例**——
    比例那条专门不取：一场只说了几句话的单聊，两三条时间戳除以十来行就是高比例，
    那正是最容易被误判的屏。绝对数那条在真机上离得开：列表 12 条 vs 单聊 3 条。

    二、**列表专有字面**凑够 `LIST_PAGE_MARK_KINDS`（2）类（v2-social-listview-leak-fix-judge）。
    这一条管的是"时间列压根没读出来"的帧（窄版单页把每行读成"昵称行 + 预览行"，一条
    纯时间行都没有），上面那条腿对它们完全是瞎的。两类是**种类**不是行数，理由与真机
    读数见 `LIST_PAGE_MARK_KINDS` 那段。

    **没取「行内含时间标记」那一维**（本来考虑过）：真机上它跟第一条腿几乎**逐帧相同**——
    47 帧里只有 9 帧不一样（142749 的 0→1，其余 8 帧都是 12→13），多出来的那一条**全是夹在
    别的字里的**（真机 142749 那一条是预览里的"10月8日"），对补漏零贡献（142749 那帧
    `max(纯, 行内)` 还是 1，够不着 2），却会把"我下午3:00到"这种人话算成时间行——
    `tests/test_social_listview_leak.py` 里那条"人话里带时间的那行不算时间行"当场就得推翻。

    **自己那半边只要出现过一行 `SENDER_USER` 就直接返回 False**：绿气泡是"这是聊天窗"
    的硬证据（列表页上一个都没有），也是"消息稀疏、时间分隔多"的老会话的第二道保险。

    跟 `social_window.looks_like_chat_list`（像素那条，16-α 落的地、16-β 没接线）是一对：
    那条从**画面**上认"整屏像列表"，这条从**认出来的字**上认。像"只收对方话的单聊"那种屏，
    绿气泡一个没有、像素那条分不开，只能靠这条的"时间戳密度"分。
    """
    chat = as_chat_read(read)
    if any(sender == SENDER_USER for sender, _text in chat.lines):
        return False        # 自己发过话：这是一场真聊天，不许判列表
    bodies = [tidy_line(text) for _sender, text in chat.lines]
    rows = sum(1 for body in bodies if _CHAT_TIME_LINE.fullmatch(body))
    if rows >= LIST_PAGE_TIME_ROWS:
        return True
    # 时间戳密度那条腿瞎了（时间列没读出来）时，看列表行才有的字面：账号号 / 未读计数 /
    # 群名行，凑够两类就算（v2-social-listview-leak-fix-judge）。
    return _list_mark_kinds(bodies) >= LIST_PAGE_MARK_KINDS


# ---------- 「这一屏是哪一种页」：仅单聊触发帮聊（v2-social-trigger-window-gate）----------
#
# 帮聊那条链（主动 offer + 自动监听起草）原先只认一条：**社交软件在前台**。这一条远远
# 不够——微信在前台时，屏幕上还可能是**会话总列表**、**群聊**、**订阅号 / 服务号 /
# 公众号**、**文件传输助手**、**小程序**，没有哪一屏是"跟某个人一对一"；用户拍板的口径
# 是"**群聊不算个人聊天窗口**"，所以 offer 与自动起草都只该在单聊里发生。
#
# 真机取证（2026-10-08 那批 96 张帧，`%TEMP%/p18/probe_page_kind.py` 逐张过认字线）把
# 现场长什么样钉死了——那一版微信是**窄版单页主窗**，聊天区与表头一起进了裁剪框：
#
#   · **群聊**（165214 / 165548 / 165613，群名「0922期AI编程副业实...(25)」）：最上面
#     那一行是**群名栏**，写法是「群名 (成员数)」——真机读成 `0922期AI编程副业实...（25）（`
#     （全角括号那两帧）与 `0922期AI编程副业实...(25)`（半角那几帧）；消息区里还有
#     **整行就是 `群公告`** 的那种卡片标题（那张卡就是群里发的公告）。
#   · **单聊**（茗风那一批）：表头那一行是光秃秃一个联系人名（175550 那帧就俩字 `茗风`），
#     没有成员数、也没有群公告。**这是两条腿都认得出的分界**。
#   · **会话总列表**（那批里 40 帧）：16-β 的 `looks_like_list_page` 已经认着，直接复用。
#
# 三条口径，理由都在真机帧上：
#
# ① **默认放行**：只有**真认出来**是排除名单里那几种页才拦。认不出来（老桩塞进来的一行
#    字符串、一个字没认到、装配层还没分过类）一律按单聊办——**宁可多冒一句，也不把功能
#    在认不出时整条闷死**。这跟 `looks_like_list_page` 的"宁可闭嘴也不谎报"方向相反，
#    是有意的：那条判错了会把材料搞脏，这条判错了只是多冒一句 / 少冒一句。
# ② **固定名只认「表头那一带」**：`服务号` / `公众号` / `文件传输助手` / `小程序` 这些字面
#    **会话总列表里也印着**——真机 142602 那一帧就是聊天区被会话列表那一列盖住的漏帧，
#    `服务号`、`公众号`、`微信支付` 各自独占一行。要是按"整屏出现即算"，那一帧上茗风的
#    单聊会被误判成"服务号页"，offer 直接不冒。所以这几条**只看头几行**
#    （`PAGE_HEAD_ROWS` = 4）：真机帧上那一带正是窗上那三个按钮（`口` / `X`）+ 搜索框之后
#    的**第一行内容**，也就是表头 / 群名栏所在的位置。
# ③ **群聊认两条腿，任一条成立就算**：群名栏的「群名 (N)」，或者整行就是 `群公告` 的卡片
#    标题。两处都写成**整行相等**而不是"行内含"——会话列表的预览行印的是带方括号的
#    `[群公告]`，而 `(N)` 那种写法在列表帧里压根不出现。
PAGE_PERSONAL = "personal"        # 与某人一对一的聊天窗（放行的那一种）
PAGE_GROUP = "group"              # 群聊
PAGE_LIST = "list"                # 会话总列表 / 搜索页这类非 active 视图（复用 16-β）
PAGE_CHANNEL = "channel"          # 订阅号 / 服务号 / 公众号
PAGE_FILEHELPER = "filehelper"    # 文件传输助手
PAGE_MINI = "mini"                # 小程序

# 排除名单：命中任一种就不是单聊——offer 不冒、自动监听也不起草。
NON_PERSONAL_PAGES = (PAGE_GROUP, PAGE_LIST, PAGE_CHANNEL, PAGE_FILEHELPER, PAGE_MINI)

# 表头那一带的行数（含窗上那三个按钮 / 搜索框那种装潢）：口径 ② 说的"只看头几行"。
PAGE_HEAD_ROWS = 4

# 群名栏那一条腿：括号里跟一个成员数，「0922期AI编程副业实...(25)」。半角全角都认
# （真机那批两种都出现过），行还要够短——真群名栏就一行，而"报名费（200）"那种人话
# 得先撞上"在前几行里"这道限制才可能误判。
_GROUP_HEAD_COUNT = re.compile(r"[（(]\s*\d{1,4}\s*[）)]")
GROUP_HEAD_MAX_CHARS = 40

# 群公告卡片那一行：**整行就是这四个字**才认（真机 165214 那张卡的标题行就是这样）。
# 会话列表的预览行印的是带方括号的 `[群公告]`，单聊里这行只可能是用户自己打出来的。
GROUP_NOTICE_LINE = "群公告"

# 固定名（**整行相等**才认，且只在表头那几行里找）——都是微信自己印的页名。
_FILE_HELPER_NAMES = ("文件传输助手",)
_CHANNEL_NAMES = ("订阅号", "服务号", "公众号")
_MINI_NAMES = ("小程序",)


def _fixed_page_name(body: str) -> str:
    """这一行是不是某个"页名"（文件传输助手 / 订阅号 / 小程序 …）。是就回那一种页。"""
    if body in _FILE_HELPER_NAMES:
        return PAGE_FILEHELPER
    if body in _CHANNEL_NAMES:
        return PAGE_CHANNEL
    if body in _MINI_NAMES:
        return PAGE_MINI
    return ""


def classify_chat_page(read) -> str:
    """这一屏认出来的字**是哪一种页**：`PAGE_*` 那几种之一，认不出特别的就回 `PAGE_PERSONAL`。

    顺序是有讲究的——**总列表先认**（16-β 那条腿最硬，而且列表页上那些 `服务号` /
    `文件传输助手` 整行压根不该再往下判），再是表头那一带的固定名，再是群聊那两条腿。

    跟 `looks_like_list_page` 同一个态度：**只看认出来的字**，不猜颜色、不看像素。判错的
    代价是"少冒一句 / 多冒一句"（口径 ①），所以拿不准时一律往"单聊"那边靠。
    """
    chat = as_chat_read(read)
    if looks_like_list_page(chat):
        return PAGE_LIST
    bodies = [tidy_line(text) for _sender, text in chat.lines]
    bodies = [body for body in bodies if body]
    head = bodies[:PAGE_HEAD_ROWS]
    for body in head:
        kind = _fixed_page_name(body)
        if kind:
            return kind
    if any(len(body) <= GROUP_HEAD_MAX_CHARS and _GROUP_HEAD_COUNT.search(body)
           for body in head):
        return PAGE_GROUP
    if any(body == GROUP_NOTICE_LINE for body in bodies):
        return PAGE_GROUP
    return PAGE_PERSONAL


def is_personal_kind(kind: str) -> bool:
    """这一格页型算不算"单聊"。**空串（还没认出来）算**，见口径 ①。"""
    return (kind or "") not in NON_PERSONAL_PAGES


def is_personal_chat(read) -> bool:
    """这一屏是不是"跟某个人一对一"的聊天窗（v2-social-trigger-window-gate）。

    群聊 / 会话总列表 / 订阅号 / 服务号 / 公众号 / 文件传输助手 / 小程序一律 False——
    帮聊那条链（offer 与自动起草）只在这条回 True 时才走，闸门没过就走"不触发"，
    不报错、也不改任何既有记账。
    """
    return is_personal_kind(classify_chat_page(read))


def chat_page_kind(read) -> str:
    """这一份 read 是哪一种页。

    装配层认字那一步已经判过一次（判在 16-β 把总列表清成空 read **之前**），结果挂在
    `ChatRead.page_kind` 上——**先信它**：清过的那份 read 在文本层已经什么都看不出来了，
    现场判不出来。没挂（老桩塞进来的一行字符串、测试直接摆 `lines`）才在这儿现判一次。
    """
    chat = as_chat_read(read)
    if chat.page_kind:
        return chat.page_kind
    return classify_chat_page(chat)


def _message_material(message) -> str:
    """一条消息 → 材料那几行：装潢行（时间戳 / 系统提示）挑掉，剩下的按行拼回去。

    一条都不剩（整条就是装潢）回空串——调用方当"这条没东西可给"处理。
    """
    if message is None:
        return ""
    return "\n".join(line for line in tidy_lines(message.text)
                     if not _is_chat_chrome(line))


# ---------- 「这一条不是一句能接的话」：碎片闸（v2-social-group-fragment-leak 乙）----------
#
# 批次8-A 把材料收成"视觉最底部那一条对方气泡，就一条"（见 `_tail_context`）。真机
# 2026-10-08 16:52/16:54/16:55 那几拍收上来的"一条"全是 `平知学堂` 四个字——群聊里
# `@知乎知学堂` 那条提及被左边那一刀削掉开头之后的残片（`social_window.detect_chat_left`
# 的甲修的是"别再削"，这道闸修的是"削了也别拿残片当材料"，两条腿各管一半）。
#
# 三道形状，都是**按字面**认（跟时间戳 / 输入框占位提示同一个态度：用户拍板过"按已知
# 文本特征过滤，不靠颜色猜"）：
#   · **纯 @提及**：整条就是一个 `@某人`（`@知乎知学堂` / `@所有人`）。那是**点名**，
#     不是一句话；接话也没法接（提示词要的是"对方最近发来的那条"，一个 @ 给不出信息）；
#   · **光秃秃一个短名**：整条一行、≤ `FRAGMENT_NAME_MAX_CHARS` 字、全是汉字 / 字母
#     （无标点空格数字），且**同一屏里还印着另一行同长度的近邻**（正好差一位、
#     且差的那一位**不是末位**）。群聊里同一个人说两句话，昵称就在两条气泡上各印一遍
#     ——真机那两行就是这么来的（`平知学堂` 与 `乎知学堂`，OCR 把同一个提及读错了一位，
#     错在哪一位是随机的）；而人话里两条只差一个字的短消息，差的那一位**几乎总在末位**
#     （"我知道了"/"我知道啦"、"在吗"/"在呢" 那种语气词），所以末位那条不算；
#   · **装潢**：`_is_chat_chrome` 已经收着时间戳 / 系统提示 / 输入框占位三种（经
#     `_message_material` 那只手挑掉，进不到这里），这一道再兜一个它没收的：
#     **"正在输入"**（对方打字时客户端自己印的那行灰字）。
#
# **收不住的形状老实说在这里**：@ 已经被切没了的四字残片（真机那次 OCR 吐的就是
# `平知学堂`）跟一句真话在字面上分不开——"好的呀""在吗"都是这个形状，而那道"近邻"
# 只是个旁证，单看一条认不出来。那一半归甲管：把 `@` 留在帧里，这道闸才认得出。
FRAGMENT_NAME_MAX_CHARS = 5      # "短名"最多几个字
_AT_MENTION_ONLY = re.compile(r"@[^\s@]{1,24}")       # 整条就是一个 @提及
_FRAGMENT_NAME_OK = re.compile(r"^[一-龥A-Za-z]+$")   # 短名只许汉字 / 字母
_FRAGMENT_TYPING = re.compile(r"正在输入")


def _fold(text) -> str:
    """整行压成"没有空白"的一串（判形状用；`tidy_line` 只管行内空白，这里连空格也不要）。"""
    return "".join(tidy_line(text).split())


def _is_fragment_material(text: str, others) -> bool:
    """这一条材料是不是**碎片**（不是一句能接的话）。命中三道形状之一就算，见上面那段。

    `others` 是**这一屏认出来的所有行**（折过空白），"短名印了两遍"那条要靠它。空串不算
    材料（老口径：调用方当"这条没东西可给"处理），这里也回 True——省得两处各判一遍。
    """
    body = _fold(text)
    if not body:
        return True
    if _FRAGMENT_TYPING.search(body):
        return True
    if _AT_MENTION_ONLY.fullmatch(body):
        return True
    if len(body) <= FRAGMENT_NAME_MAX_CHARS and _FRAGMENT_NAME_OK.fullmatch(body):
        for other in others or ():
            other = _fold(other)
            if (other == body or len(other) != len(body)
                    or not _FRAGMENT_NAME_OK.fullmatch(other)):
                continue            # 要比的是屏上**另一行**（自己那一行不算）
            diff = [i for i, (a, b) in enumerate(zip(other, body)) if a != b]
            # **正好差一位、且不在末位**才算：差在末位的是人话的变体（"我知道了"/"我知道啦"
            # 那种语气词），差在中间/开头的是同一串字被 OCR 读错了一位——群昵称正是这样。
            if len(diff) == 1 and diff[0] < len(body) - 1:
                return True
    return False


def _tail_lines(text: object, limit: int = CHAT_CONTEXT_CLIP) -> str:
    """对方那屏字：**按整行**从末尾往前留，最多约 `limit` 个字——绝不切半句。

    v2-social-multiline。老口径是 `body[-CHAT_CONTEXT_CLIP:]`：按字符直接切尾巴。
    这跟"取末尾"那个初衷（OCR 从上往下读，最新那条在下面）是同一件事，但它有个漏：
    对方最新那条**一长**，切点就落在这条的句子中间——真机上被削掉的正是开头那句
    「这么快就放弃啦？」，只剩末尾半句「我比较欣赏有毅力的男孩哟」，回复自然只答半截。

    改成一行一行往前捡：捡到再加就超了才停，所以**每一行都是完整的**。单行本身就超长
    时整行留着（宁可略超，也不切句子）——OCR 吐出来的"一行"本来就是个完整气泡，
    切它没有意义。

    **只有一行的输入是例外，按老口径切尾。** 那种输入里**没有行边界可守**：它不是
    "对方一条消息占几行"，而是整屏被拍平的那一种（OCR 降级、或者装配层回了一整段
    字符串的老桩），那正是"取末尾、别被整屏旧字带跑"这条要治的东西——一行 850 字
    整条塞给模型，等于把开头那些旧字/用户自己的提问又带回去了。

    行内空白压平、空行丢掉由 `tidy_lines` 做；行与行之间用 `\\n` 拼回去，换行语义不丢。

    **装潢行在这一层就挑掉**（v2-social-ui-batch 批次8-B）：这条路是"认不出说话人"的降级路
    ——材料直接取整屏，没有任何"按条取尾"的机会把输入框那行灰字挡在外面。时间戳 / 系统提示 /
    空输入框的占位提示（`_is_chat_chrome` 那三种形状）都不是谁说的话，留在材料里只会让模型
    对着它们起草。挑掉的是**整行**，人话一个字不动。
    """
    lines = [line for line in tidy_lines(text) if not _is_chat_chrome(line)]
    if not lines:
        return ""
    if len(lines) == 1:
        # 一行 = 没有行边界，退回老口径：取尾，开头那段挡在外面
        only = lines[0]
        return only[-limit:] if len(only) > limit else only
    kept: list[str] = []
    total = 0
    for line in reversed(lines):
        if kept and total + len(line) + 1 > limit:
            break
        kept.append(line)
        total += len(line) + 1
    return "\n".join(reversed(kept))


def _tail_context(read: ChatRead, limit: int = CHAT_CONTEXT_CLIP) -> str:
    """对方那半边：**按条**从末尾往前留，**最新那一条整条不切**。

    v2-social-grab-fix。老口径两条路不一致：手动那条走 `_tail_lines`（按**行**取尾），
    自动监听那条（`_auto_generate`）**压根没裁**——`read.context` 是整屏对方的话，
    顶上那些旧消息全在里面，原样喂给模型。真机上的两件症状就是这么来的：

    - **"等几分钟才反应"**：整屏聊天记录（几千字）当提示词，模型那一趟自然慢；
    - **"识别的是上面那句旧话"**：材料里旧的和新的一样重，模型挑哪句全看运气。

    现在两条路并成一条，都按**条**取尾。单位为什么是"条"不是"行"：一条消息被气泡宽度
    折成两行时那两行**是同一条**，按行切在前一行后面停住，就等于把人家的半句话丢了
    ——这正是「可能要等几个月。这段时间，事情都比较」/「忙」丢成"忙"的那种丢法。
    按条取尾，最新那条永远整条留着，再往前一条一条捡，超了才停（"绝不切句子"这条老口径
    原样保留，只是把守的边界从"行"抬到"条"）。

    一行都没分出说话人时（深色主题 / 换客户端 / 老桩回一整段字符串）退回
    `_tail_lines(read.visible)`：那是**降级**，整屏都得当材料，按行取尾是那种情况下
    唯一还能守住"别被顶上旧字带跑"的做法。

    **v2-social-tail-context-fix 又收了两刀**（真机 2026-10-07 那屏的取证见
    `core/bootstrap._log_tail_trace` 与收尾报告第一百四十二节）：

    1. **只取"我上一次开口以来"对方说的话。** 从末尾往前捡，一碰到用户自己发的
       （`SENDER_USER`）就停——那之前的对方消息，要么已经回过了、要么是上一场的话，
       不是"现在要接的那句"。真机上那屏的最底下正是这个形状：`14:22` 我发完
       「要泡妹，就要懂套路。」，对方接「哟，你小子这么会，是在撩我么？」——停在我那条
       上，材料就只剩最后那一句，一个旧字都不带。
    2. **把装潢挑出去**（`_is_chat_chrome`：时间戳 / 日期分隔 / 撤回提示 / 系统提示）。
       这些行色卡一律认成"对方说的"，混进材料就是"时间戳 + 撤回提示 + 真话"的一锅粥。

    这两刀只收在**认得出说话人**这条路上；`known=False` 那条降级路一个字没动——
    那儿连"谁说的"都不知道，谈"我上一次开口以来"没有依据。

    **v2-social-ui-batch 批次8-A 再收一刀：材料 = 视觉最底部那一条对方气泡，就一条。**

    上面第 1 刀按的是**说话人**（碰到自己发的才停），不是**条数**：真机 2026-10-07
    19:04-19:05 那屏，我上一次开口之后对方连回了十一二行（`_log_tail_trace` 认的是
    "对方 11 行 / 自己 6 行"），按第 1 刀**一条不落全留下**——于是模型每次拿到的是一段
    十几行的记录，而"该回哪一句"全看它自己挑。同一段时间里四趟 POST 的材料分别是
    「上一趟的末句」「这一趟的末句」「我自己发的那句」「输入框的占位提示」，四条方案
    对象也就是这么错开的。

    提示词那边本来就写着"**对方最近发来的那条**"（`brain/prompts/templates.py` 的
    `SOCIAL_DRAFT_PROMPT` 1a：一条消息可能占好几行），材料与该口径对齐即可，
    不用模型去猜"这是哪一条"。所以从末尾往前捡到**第一条有内容的对方气泡**就停：
    上面那些回过的话、上一场的话，一条都不进材料。

    一条气泡被折成多行时那几行**本来就是同一条消息**（`group_rows` 在认字那一层已经
    并成一条了），所以"只取一条"不会把半句话丢掉——这条老口径一分没松。

    末尾那条是**我自己发的**时（`SENDER_USER` 在最下）走下面那个 `latest_other` 兜底，
    口径同出一辙：给的是**对方最近那一条**，仍不是"把历史全捎上"。

    **v2-social-group-fragment-leak 乙再收一刀：最底部那一条要是"碎片"，不算材料。**
    真机 2026-10-08 16:52/16:54/16:55 那几拍，8-A 收上来的是 `平知学堂` 四个字——群公告
    里 `@知乎知学堂` 那条提及被左边那一刀削掉开头之后的残片（那一刀的机理见
    `social_window.detect_chat_left` 甲）。模型拿着四个字当然写不出话，用户看到的就是
    "稍等"（`_draft_now` 里"上一段我还在写"/`NO_PEER_LINE_HINT` 那两句）。所以取到 8-A
    那一条之后再问一句 `_is_fragment_material`：是碎片就**接着往上一句找**，上一条有效
    消息才是"对方最近发来的那条"；一句都没有就回空串，走批次17甲那条空材料的路
    （"稍等、不拟稿"），绝不拿残片去拟。哪三种形状算碎片、哪一半收不住，见函数上面那段。
    """
    if not read.known:
        return _tail_lines(read.visible, limit)
    tail: list[str] = []
    others = [_fold(text) for _sender, text in read.lines]   # 这一屏所有行（判"短名印两遍"用）
    for message in reversed(read.messages):
        if message.sender == SENDER_USER:
            break                 # 我上一次开口：再往前都是回过的话，不往材料里带
        if message.sender != SENDER_OTHER:
            continue              # 认不出说话人的行不当材料（老口径一样）
        body = _message_material(message)
        if body and not _is_fragment_material(body, others):
            tail.append(body)
            break                 # ★ 批次8-A：只取这一条，不再往前卷（见下面那段）
        # 这一条是**碎片**（纯 @提及 / 光秃秃一个短名 / 装潢）：不算数，接着往**上**找
        # 上一条有效消息（v2-social-group-fragment-leak 乙）。上面没有有效消息时落到
        # 下面那个兜底；兜底也是碎片就回空串——"稍等、不拟稿"那条路（批次17甲）上游已有。
    tail.reverse()                # 捡的时候是从末尾往前，拼回去要正过来（现在最多一条）
    if not tail:
        # 这一屏最后开口的是自己。自动那条路这时**不会来要材料**（末尾归属判成 self，
        # 一早就"不插嘴"了）；手动那条路还会来——用户可能刚自己回了一句、又想换个说法。
        # 那一档给**对方最近那一条**，别把材料清成空的（材料一空，提示词那格就变成
        # "没认到对方说了什么"，他明明看得见对方刚说的那句）。碎片照样不收（见上一段）。
        latest = read.latest_other
        body = _message_material(latest) if latest is not None else ""
        if body and not _is_fragment_material(body, others):
            tail = [body]
    if not tail:
        return ""
    kept: list[str] = []
    total = 0
    for text in reversed(tail):
        if kept and total + len(text) + 1 > limit:
            break
        kept.append(text)
        total += len(text) + 1
    return "\n".join(reversed(kept))


def is_social_proc(proc: str | None, overrides: dict[str, str] | None = None) -> bool:
    """这个前台应用算不算社交软件。

    复用内置分类表里现成的「社交」一类（wechat / qq / telegram / discord …），
    跟 `focus_timer.is_focus_proc` 复用同一个 `resolve_category` 的写法一致——
    分类表是用户可覆盖的那一份，所以管理员在设置里改过分类，这里跟着改。
    """
    if not proc:
        return False
    return resolve_category(proc.lower(), overrides or {}) == SOCIAL_CATEGORY


class SocialReplyService(QObject):
    """社交软件前台时冒一句，用户在聊天里要草稿就写一段给他。**只提议、不代发。**"""

    def __init__(
        self,
        config: Config,
        bus: EventBus,
        clock: Callable[[], float] | None = None,
        can_offer: Callable[[], bool] | None = None,
        grab_chat: Callable[[], object | None] | None = None,
        read_chat: Callable[[object], str] | None = None,
        examples_provider: Callable[[str], str] | None = None,
        offline_candidates_provider: Callable[[], dict | None] | None = None,
        card_exists: Callable[[], bool] | None = None,
        session_provider: Callable[[], str] | None = None,
        idle_seconds: Callable[[], float] | None = None,
        offer_visible: Callable[[], bool] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._bus = bus
        self._clock = clock if clock is not None else time.monotonic
        # 空闲时长那只手（v2-social-auto）：测试注入假值；默认共用 monitor 的系统原语。
        self._idle_seconds = idle_seconds if idle_seconds is not None else _system_idle_seconds
        # 气泡上已经排着别人的按钮时（笔记正等答复等）就不冒这句：按钮只有一排，
        # 两条线同时问会互相顶掉。跟 game_guide 的 can_offer 是同一个道理。
        self._can_offer = can_offer if can_offer is not None else (lambda: True)
        # 截屏 / OCR 那两只手（v2-social-fewshot）由装配层注入——**这个模块自己一个
        # 截屏 / OCR 的库都不 import**，服务只管"什么时候抓、抓来的字怎么用"。
        # 不接（测试 / 关掉 OCR）就整条降级成"没有对方原话"，别的照旧。
        self._grab_chat = grab_chat
        self._read_chat = read_chat
        # 风格范例（全局 + 卡级）从哪儿来；不接就给空串，提示词那格填"没有可参考的范例"。
        self._examples_provider = (
            examples_provider if examples_provider is not None else (lambda query: "")
        )
        # 断网兜底那句按熟悉度挑（v2-offline-social-stage）：由装配层接亲密度那边；
        # 不接（或它说取不到等级）就回本模块的 OFFLINE_CANDIDATES 那一条。
        self._offline_candidates_provider = offline_candidates_provider
        # 「对象攻略卡」是不是已经有一张了（v2-social-card-dedup）：由装配层接做卡那边
        # 的 `CardService.find_card`；不接（测试 / 没装卡模块）就当"还没有"，照旧问。
        self._card_exists = card_exists if card_exists is not None else (lambda: False)
        # 「这一场对着的是谁」（v2-social-session-reset）：装配层在抓屏那一拍顺手记下的
        # 会话标识（独立聊天窗的标题写的就是对方 / 群名）。**它不是新读一次窗口标题**，
        # 见 `bootstrap._social_session_id`。不接（测试 / 关掉那条线）就整条降级成
        # "认不出换人"，行为跟加它之前逐字相同。
        self._session_provider = session_provider
        # 「那排 [好呀] [不用了] 这会儿还挂在气泡上吗」——**由装配层注入的一问**
        # （v2-social-await-prime-fix 改动一）。它问的是界面那边的实况（气泡当前排的
        # 按钮主题是不是社交这一条），不是本服务记的状态。不接（测试 / 没装界面）
        # 就是 None，`dismiss_offer` 退回"来一声就清"的老口径——不接这一问的调用点
        # 行为逐字不变。见 `dismiss_offer` 里的错位那一段。
        self._offer_visible = offer_visible
        # 上一拍认到的"这一场是谁"。取不到时留空串——空串一律当"认不出"，不动状态。
        self._session_id = ""
        # **只存内存**：本场从聊天窗认出来的字（对方最近那条在里面）。生成时用掉即清，
        # 不落盘、不进任何库（v2-social-fewshot 的范围钉在这几行）。
        self._chat_text = ""
        # 那屏字是什么时候认回来的（v2-social-stale-guard）：起草时抓不成会拿它比一下
        # 新鲜度，过期的当没有。取走即归零（见 `_take_chat_text`）。
        self._chat_text_at = 0.0
        self._chat_reading = False
        # 社交软件此刻在不在前台（v2-social-ocr-path）：主动求助那一拍靠它判断还值不值得
        # 再抓一屏。每次 on_foreground 都刷新，跟"冒不冒话"是两件事。
        self._social_foreground = False
        # 这一屏是哪一种页（v2-social-trigger-window-gate）：装配层每认一次字就报一次
        # （`note_page_kind`）。**只在"非单聊 → 单聊"跃迁那一拍冒邀请**；连着停在单聊上
        # 不重复问。走出社交前台清回空串，下一趟进来重新算一次跃迁。
        self._page_kind = ""
        # 最近一次前台切到的是哪个进程：`social_offer` 那一声带的就是它（只为日志）。
        self._last_proc = ""
        # 主动求助那一拍、等着这一屏 OCR 回来再起草的暂存（可能为空）。抓不成就不设，
        # 直接带着手上已有的字起草。
        self._pending_draft: str | None = None
        # 那一问是什么时候挂上去的（批次17 甲修正）：点了 [好呀] 但社交窗口不在前台、
        # 这一拍抓不成时，这一问**挂住等切回来**（不再当场硬抓一屏空的）。挂过
        # `OFFER_HANG_S` 当过期（见 `_drain_pending`）——跟邀请那排按钮同一个寿命口径。
        self._pending_draft_at = 0.0
        # 只存内存：上次在社交软件前台冒话是什么时候、有没有一段草稿正在生成
        self._last_asked = 0.0
        # 最近一次"要不要建对象攻略卡"那一问的时间点（v2-social-offer-dedup）：同一趟
        # 社交前台里只问一次，走出聊天窗再回来（`on_foreground`）才归零、重新问一次。
        self._last_offered = 0.0
        self._running = False
        # 冒过话、按钮还挂着，正等用户点 [好呀] / [不用了]
        self._awaiting = False
        # 用户点了 [好呀]，正等他把处境讲出来；到点了自动作废（见 SITUATION_WINDOW_S）
        self._want_situation_until = 0.0
        # 候选回复列完之后，正等用户点"要不要建对象攻略卡"（v2-social-reply-ext）
        self._card_awaiting = False
        # 最近一次帮忙回复的处境原文：建卡抽信息时用它当原料（只存内存，不落库）
        self._last_request = ""

        # ---- 自动监听（v2-social-auto）：只存内存、只比指纹、变了才 OCR ----
        # 轻量轮询计时器：社交软件在前台时跑，每 LISTEN_POLL_MS 扫一眼画面指纹。
        self._listen_timer = QTimer(self)
        self._listen_timer.setInterval(LISTEN_POLL_MS)
        self._listen_timer.timeout.connect(self._poll_listen)
        self._listen_reading = False          # 这一拍正在认字（别叠两拍 OCR）
        self._last_fingerprint: str | None = None   # 上一拍画面的签名
        # 挂住那一问的"按需抓拍"计时器（v2-social-on-demand-snap）：只在
        # `_pending_draft` 挂着（点了 [好呀]、正等对方那句）时跑，一拍一看前台，
        # 社交窗一回来立刻抓一屏——不再等前台采样那 5 秒一拍。见 `_poll_pending_snap`。
        self._snap_timer = QTimer(self)
        self._snap_timer.setInterval(PENDING_SNAP_MS)
        self._snap_timer.timeout.connect(self._poll_pending_snap)
        # 这条腿自己那一份画面签名：**抓拍这条腿**（前台变了那一抓 / 挂住的那一拍）认过的
        # 最近一屏，同一屏就不再烧一趟 OCR（跟 `_poll_listen` 那份分开——两份各管各的路，
        # 谁也不改谁的判断）。新起的一问（点 [好呀]）会把它清零。
        self._snap_seen_fp: str | None = None
        # 这一问那句"还没读到"说过没有：挂住那一问可以一拍一拍地看，但那句话**一问只说
        # 一次**（同一句提示反复冒出来比不说更烦）。新起一问时清零。
        self._pending_hinted = False
        self._last_listen_tail = ""           # 上一拍认出来的尾巴（判"变了没有"）
        self._last_listen_kind = ""           # 上一拍的归属（incoming / self），防同态重复说话
        self._last_listen_at = 0.0            # 上次自动拟候选的时间点（冷却用）
        # 上一次自动拟候选**喂给模型的那份对方原话**（v2-social-listen-dedup）：同一份
        # 材料不重开一轮。只存内存、只存文本（压平过，比的是"内容变没变"）。
        self._last_draft_source = ""
        # 这一拍正在拟的那一份（拟成功了才落到 `_last_draft_source`，见 `_on_candidates`）。
        self._pending_auto_source = ""
        # ---- 完成态锁（v2-social-copy-clean）----
        # 这一段已经拟完、交给用户了。在他动手之前，这一屏再怎么抖（光标、复制粘贴引出的
        # 重绘、切窗口回来的重排）都不许重开一轮——重开就是真机那个"复制完就卡死"。
        self._done = False
        self._done_tail = ""                  # 完成那一刻的画面尾巴（拿它认"还是这一屏"）
        self._done_at = 0.0                   # 完成的时间点（短期冷却是从这儿算的）
        # 完成那一刻认到的**每一行**（v2-social-grab-fix）：判"画面换没换"用它，比拿
        # 一整条扁平字符串比相等准得多——见 `_lock_verdict`。
        self._done_lines: set[str] = set()
        # 上一拍真认回来的每一行：`mark_done` 拿它当"完成那一刻那一屏"的底。
        self._last_read_lines: set[str] = set()
        # 冷却期内看见过**没处理过的**新内容（那一屏滚过，分不出是新消息还是抖动，锁压着
        # 没放行）。立着的话，冷却一过就主动把那一屏重新拿回来判一次——不立的话，画面
        # 不再变、指纹那条路也不会再看一眼，那条消息就永远咽下去了。
        self._locked_pending = False

    # ---------- 接线 ----------

    def attach(self, monitor) -> None:
        """挂到采样器上。只看前台换人这条信号——聊天内容那条路根本不存在。"""
        monitor.foreground_changed.connect(self.on_foreground)

    def set_offline_candidates_provider(self, provider: Callable[[], dict | None]) -> None:
        """接上"断网那句按熟悉度挑"的来源（v2-offline-social-stage）。

        来源返回 None（等级取不到）时照旧用 OFFLINE_CANDIDATES——兜底不该两级都塌。
        """
        self._offline_candidates_provider = provider

    def set_card_exists_provider(self, provider: Callable[[], bool]) -> None:
        """接上"「对象攻略卡」建过没有"的判据（v2-social-card-dedup）。

        由装配层接 `CardService.find_card(CARD_CATEGORY, GUIDE_CARD_TITLE)`——**判据在
        做卡那个模块里，社交这条线不自己存一份**（用户可能是在卡界面里自己建的那一张，
        社交这边无从知道）。接的是个回调、每次现问，所以"刚建完"立刻就认得出来。
        """
        self._card_exists = provider

    @property
    def enabled(self) -> bool:
        return bool(self._config.screen.enabled and self._config.screen.social_enabled)

    @property
    def busy(self) -> bool:
        """上一段草稿还没写完——这中间不再开第二段。"""
        return self._running

    @property
    def pending_offer(self) -> bool:
        """社交这条线这会儿还端着东西吗（邀请等着点 / 候选等着挑 / 草稿正在写）。

        `scene_guard` 拿它当"聊天窗口说了算"的一半判据（v2-social-scene-boundary）：
        这会儿场景开场词要让位，别再飘「我陪你看电影」。前台那一半由装配层**现算**
        进程名，不读这里的 `_social_foreground`——那份是本服务自己的 handler 跑完才
        更新的，而前台是一条信号派发给所有线的，排在社交前面的线问的时候它还是上一拍。
        """
        return bool(self._awaiting or self._card_awaiting or self._running)

    @property
    def awaiting_confirm(self) -> bool:
        """冒过话了、按钮还挂着，正等用户点 [好呀] / [不用了]。

        界面拿它来决定"这排按钮该归谁"——跟 `screen_notes.awaiting_confirm`、
        `game_guide.awaiting_confirm` 是同一个口径。
        """
        return self._awaiting

    @property
    def in_personal_chat(self) -> bool:
        """眼下这一屏还是不是**单聊**（`note_page_kind` 认下来的那一档页型）。

        `v2-social-card-timeout` 要的那一句：帮聊那一则的 3 分钟清理线，认"用户回到对话
        窗口"时得是**原来那条单聊**——人切回微信、却停在群聊 / 会话总列表上，那不算回来。
        这里只把已经记着的那个页型读出去：不改判定、不碰 OCR、不碰监听闸、不碰去重。
        """
        return is_personal_kind(self._page_kind)

    def dismiss_offer(self, *, force: bool = False) -> None:
        """按钮收掉了（用户在框里自己说了话，或者界面换了一茬）：等于这次邀请作废。

        只清"等着点按钮"这一档，**不动**"正等他说处境"那一档——后者是他自己点了
        [好呀] 换来的，正是接下来那句话的上下文。

        **这是个"必须有人来喊一声"的收口，v2-social-scene-boundary 补了三处入口。**
        在这之前全仓只有 `Bootstrap._on_chat_send` 一处会调它，于是"邀请冒出来、用户
        没点按钮（关了气泡 / 切走 / 压根没理）"这条最平常的路上，`_awaiting` 永远挂在
        True——而 `_poll_listen` 第一句就是拿它 early-return，自动监听从此静默停摆。
        现在补齐：

        - 气泡上那排按钮退休时（`ChatBubble.offer_hidden` → 装配层转喊）；
        - 候选交到用户手上、建卡那一问摆出来时（装配层在亮完气泡之后转喊）；
        - 走出聊天窗口时（`on_foreground` 里自己喊）。

        **但"来一声就清"跟界面那边对不上时，会造出一排点不动的按钮**
        （v2-social-await-prime-fix 改动一，真机 17:59 那一趟）。上面第三处入口——走出
        聊天窗口——**界面那边一个动作都没有**：气泡是常驻的，那排 [好呀] [不用了] 照样
        挂着。这里把 `_awaiting` 一清，界面与服务就成了"按钮还显示、旗子已经没了"的错位：
        用户回来点 [好呀]，装配层先拿 `awaiting_confirm` 问一句（`_on_offer_reply`），
        判 False → 跳过 `confirm()` → 落到通用聊天兜底（真机回的就是那句"好的呀"），
        社交起草这条线**一次都没进**。

        所以清之前先问一句界面（`_offer_visible`）：那排按钮**还归社交这一条线**时，
        这一声喊的不是"邀请结束了"，只是前台短暂离场——这时候**不清**，等按钮真的
        不再显示（`show_offer` 顶替 / `hide_offer` / 关气泡）或超时那一声（`force`，
        见 `_drop_stale_offer`）再清。九成情况下 `_offer_visible` 一进来就已经是 False
        （喊它的那几处都在界面已经收掉按钮之后），拦下的只有"离场"这一条。

        `force=True` 只给超时那个出口用：那时候邀请是**真的**过期了（`OFFER_HANG_S`），
        必须清掉 `_awaiting` 把 `_poll_listen` 放开，不能再等按钮。
        """
        if not force and self._offer_visible is not None and self._offer_visible():
            logger.debug("社交那排按钮还挂在气泡上，这一声不作废邀请（等按钮真消失或超时）")
            return
        self._awaiting = False

    @property
    def done(self) -> bool:
        """这一段拟完交给用户了、正锁着（v2-social-copy-clean）。"""
        return self._done

    def mark_done(self) -> None:
        """候选交到用户手上了：**把这一屏锁上**，直到画面真的换了内容。

        装配层在把候选亮出来（非模态气泡）并把复制做完之后喊这一声。这一声记下三样：
        这一屏的尾巴、**这一屏逐行的字**、以及时刻。判据见 `_lock_verdict`。

        老口径没有这一档：候选框一关，`_last_listen_tail` 停在"上一拍"，用户去粘贴时
        聊天窗重绘一下就被判成"来了新消息"，于是又拟一段——真机上那叫"复制完就卡死"。

        **v2-social-grab-fix 起，锁只挡"这一屏的重开"，不挡对方新发消息**：老口径是拿
        一条扁平字符串比相等，一滴抖动（OCR 少认了一行）就判成"变了"，而"变了"又要再过
        20 秒冷却才放行；反过来，冷却期里真来了新消息，也会被这条规则咽掉。现在改成
        逐行比（`_lock_verdict`）。
        """
        self._done = True
        self._done_tail = self._last_listen_tail
        self._done_lines = set(self._last_read_lines)
        self._done_at = self._clock()
        self._locked_pending = False

    def reset_done(self) -> None:
        """解开完成态锁。出了聊天窗口、或者用户又开了一轮，都该走这条。"""
        self._done = False
        self._done_tail = ""
        self._done_lines = set()
        self._done_at = 0.0
        self._locked_pending = False

    def _lock_verdict(self, chat: ChatRead) -> str:
        """锁着的时候，这一屏该不该放行：`"open"` = 放行 / `"hold"` = 还锁着。

        判据按"能不能一眼看出这是新内容"从硬到软排：

        1. **一行没多**（这一屏认到的行跟完成那一刻一模一样）→ 还是那一屏，`hold`。
           这是"已完成态的重开"那条本意，也是抖动的一条：粘贴引起的重绘变的多半是
           行内的抖动，行本身不变。
        2. **只多不少**（老行一行没少、底下多出来没见过的）→ 画面没滚，底下接上了新内容
           →`open`。这是最常见的"对方又发了一条"，**冷却不挡它**。
        3. **最新那一行是没见过的、而且认出来是对方说的** → 画面滚过（顶上的老行被顶掉
           了），但末尾这一行是新的白气泡 → 对方刚说的新话 → `open`。
        4. 以上都不成立（认到的行有出有进，分不清是滚动还是抖动）→ 交给
           `SOCIAL_DONE_COOLDOWN_S` 那段短期冷却兜底：冷却里 `hold`，过了 `open`。

        一行都没认到 / 完成那一刻没留底（比无可比）：只按冷却兜底，别凭一个空值放行。

        立在 `hold` 上、而这一屏**确实多出了没见过的行**时，顺手把 `_locked_pending`
        立起来——那是"咽在嘴里还没处理的新内容"，交给 `_poll_listen` 在冷却过后主动
        重看一次（画面不再变的话，指纹那条路是不会再看第二眼的）。
        """
        old = self._done_lines
        new_lines = set(tidy_lines(chat.visible))
        cooled = self._clock() - self._done_at >= SOCIAL_DONE_COOLDOWN_S
        rows = [(sender, text) for sender, text in chat.lines if text]
        fresh: set[str] = set()
        if old and new_lines:
            fresh = new_lines - old
            if not fresh:
                return "hold"                       # ① 一行没多：还是完成那一刻那一屏
            if not (old - new_lines):
                return "open"                       # ② 只多不少：底下接上了新内容
            if rows and rows[-1][0] == SENDER_OTHER and rows[-1][1] not in old:
                return "open"                       # ③ 末尾那行是没见过的白气泡
        else:
            # 比无可比（这一屏一行都没认到 / 完成那一刻没留底）：只按冷却兜底放着，
            # `_locked_pending` 一动不动——拿"什么都没认到"当依据去清掉一个真的待办，
            # 就是又丢一条消息。
            return "open" if cooled else "hold"
        if cooled:
            return "open"                           # ④ 冷却也过了：当新一轮办
        self._locked_pending = bool(fresh)           # 咽着新内容：冷却一过主动重看
        return "hold"

    # ---------- 冒话 ----------

    def on_foreground(self, proc: str) -> None:
        """前台切到社交软件：隔一阵冒一句，**并把 [好呀] [不用了] 露出来**。

        v2-social-flow 之前这里只发一句台词，按钮那一排不露——用户看到话，
        手上却没处点，功能到那句台词就断了（模块⑧的真机反馈）。

        v2-social-ocr-path 把"抓屏"和"冒话"解了耦：冒话还过冷却那几道闸，抓屏
        **只要社交软件在前台就抓**（下面第一件事）。这样用户没过邀请、直接跑回来
        喊"帮回"，手上也已经有对方最近那条了（真机"答非所问"正是卡在这）。

        v2-social-trigger-window-gate 起多了**最后一道闸：仅单聊**。这一拍照旧抓屏
        （抓屏本身不受闸门影响），但**不再当场 emit**——这一屏是群聊 / 会话总列表 /
        订阅号 / 文件传输助手 / 小程序 还是某人一对一的单聊，得等那次 OCR 回来才知道，
        由 `note_page_kind` 在"非单聊 → 单聊"跃迁那一拍收口。等不到那次认字（没接 OCR
        那两只手 / 取帧失败）照老口径当场冒，见下面那句的注释。
        """
        # 不管冒不冒话，先记下"社交软件在不在前台"——主动求助那一拍靠它判断还
        # 值不值得再抓一屏。
        was_social = self._social_foreground
        self._social_foreground = is_social_proc(proc)
        self._last_proc = proc or ""
        if was_social and not self._social_foreground:
            # 走出聊天窗口 = 那排 [好呀] [不用了] 跟着没了，这次邀请到此作废
            # （v2-social-scene-boundary）。不清的话 `_awaiting` 会一直挂着，而
            # `_poll_listen` 头一句就拿它 early-return——再切回来时监听是停摆的，
            # 对方回消息也不弹候选，且**一行日志都不会有**（真机那个"静默不触发"）。
            self.dismiss_offer()
            # 完成态锁也一起解开（v2-social-copy-clean）：人都走出聊天窗了，下一趟
            # 进来是一轮新的，不该还带着上一轮的锁。
            self.reset_done()
            # "这一屏已拟过"那份记忆也一起清（v2-social-listen-dedup）：下一趟进来是
            # 新的一轮，同一份字该重新看一遍，别拿上一趟的记忆把人家挡在外面。
            self._last_draft_source = ""
            self._pending_auto_source = ""
            # 建卡那一问的"这一趟只问一次"也跟着翻篇（v2-social-offer-dedup）：下一趟
            # 进来是新一轮候选，该重新问一遍，别拿上一趟的记忆把这一问永远压死。
            self._last_offered = 0.0
            # 这一屏是哪一种页那格也清回去（v2-social-trigger-window-gate）：下一趟进来
            # 重新算一次"非单聊 → 单聊"的跃迁，别端着上一趟的页型把第一拍挡掉。
            self._page_kind = ""
        # 自动监听（v2-social-auto）：进/出社交前台，起/停那个轻量轮询计时器。
        self._sync_listen_timer()
        if not self.enabled or not self._social_foreground:
            return
        # 抓屏与冒话解耦（v2-social-ocr-path）：社交软件在前台就抓一屏，刷新对方
        # 最近那条（覆盖上一场的旧值）；抓不抓得成都不挡冒话那条线。
        started = self._read_social_screen()
        # **认得出这一屏是哪一种页才冒**（v2-social-trigger-window-gate）：这一屏是哪一种
        # 页得等那一次 OCR 回来才知道（约 4 秒），回来时 `_on_screen_read` 把页型报给
        # `note_page_kind`，**由它收口冒不冒**；所以这一拍不再当场 emit。
        #
        # 等不到那一次认字的两种情形**照老口径当场冒**：压根没接 OCR 那两只手（老桩 /
        # 关掉 OCR），或者这一拍连帧都取不到——两种都是"无从判起"，按口径 ① 的
        # "默认放行"办。上一拍的字还在认（`_chat_reading`）不算：它认的就是本屏，
        # 回来照样会报页型。
        if not started and not self._chat_reading:
            self._ask_offer(self._last_proc)

    def _ask_offer(self, proc: str) -> bool:
        """该冒这一句就冒（几道闸都过的话）。返回 True = 真冒了。

        闸门是：还有别的按钮占着气泡（`_can_offer`）、上一问没结果（`_running` /
        `_awaiting`）、冷却没到（`_due`）、台词撞危机词——**外加调用方那道**
        "仅单聊"（`v2-social-trigger-window-gate`，见 `on_foreground` 与 `note_page_kind`）。
        闸门没过就静默不冒（不报错、不记账），跟加那道闸之前逐字相同。

        **只发 `social_offer` 这一条**（v2-social-chat-ui-fix2 提交D）。老口径在这前面还有
        一句 `pet_speak_proactive.emit(ASK_LINE)`——头顶浮动气泡先冒一遍这句台词，紧接着
        装配层收到 `social_offer` 走 `_pop_offer`（那里第一件事就是
        `pet_window.hide_speech_bubble()`）把头顶那条收掉、再把**同一句**摆进对话框。
        真机上就是同一问句冒两遍。现在问句只走 `social_offer` 这一条：装配层既负责
        把问句摆进气泡、又负责把 [好呀] [不用了] 排出来，`_pop_offer` 里那句
        `hide_speech_bubble()` 照样先收头顶（防别的线留着），只是这会儿本来就没东西可收。
        """
        if self._running or self._awaiting:
            return False
        if not self._can_offer():
            logger.debug("气泡上已经挂着别的按钮，这一句不冒（社交）")
            return False
        if not self._due():
            return False
        # 台词写死也照样过一遍危机词（跟 companion.py / game.py 同款，红线不跳步）
        if crisis.is_crisis(ASK_LINE):
            logger.warning("社交邀请台词命中危机词，跳过这一句")
            return False
        self._last_asked = self._clock()
        self._awaiting = True
        logger.debug("社交软件在前台（%s），冒一句要不要帮忙措辞", proc)
        self._bus.social_offer.emit(proc or "")
        return True

    def note_page_kind(self, kind: str) -> None:
        """装配层报一次"这一屏是哪一种页"（v2-social-trigger-window-gate）。

        **冒邀请那一拍在这里**：只有**状态从"非单聊"跃迁到"单聊"**那一次才算数——用户从
        会话总列表 / 群聊里点进某个人的单聊，这一格就从别的页型变成单聊，offer 正是该在
        这一拍冒。连着停在单聊上（每一拍都报单聊：对方连发几条、滚动、光标闪）**不再问
        第二遍**——真机上那看着就是"同一句话反复问"。

        判据就是**"页型真的变了、而且变到的这一格算单聊"**：空串（还没认出来）按"单聊"
        那条默认口径算（见 `is_personal_kind`），所以从"没认出来"进到"单聊"也算一次跃迁
        ——冷启动那第一拍正落在这一档上；反过来"两拍都没认出来"（页型那格没动）不算，
        免得连不上装配层时每一拍都冒一句。

        走出聊天窗时 `on_foreground` 把那一格清回空串，下一趟进来重新算一次跃迁。
        """
        prev = self._page_kind
        self._page_kind = kind or ""
        if not is_personal_kind(self._page_kind) or prev == self._page_kind:
            return
        # 认字是后台跑的（约 4 秒），回来时前台可能早切走了——跟 `_on_listen_read` 门口
        # 那道闸同一个道理，这儿再确认一次。
        if not self.enabled or not self._social_foreground:
            return
        self._ask_offer(self._last_proc)

    # ---------- 对方最新那条（v2-social-fewshot：截屏 OCR，读完即弃） ----------

    def _read_social_screen(self) -> bool:
        """趁社交软件在前台，截一屏、后台 OCR 出屏幕上的字。

        分两步、跨两个线程：**取帧留在界面线程**（Qt 抓屏只该在主线程做，且只是拷内存），
        **识别丢后台**（一次约 4 秒，摆界面线程上必卡）——跟 monitor 认字幕那套一模一样。

        取不到帧 / 没接那两只手 / 上一屏还在认，都安静跳过：这一场就没有"对方原话"，
        起草照旧，只是提示词那格填"没认到"。

        返回 True = 真把识别踢出去了（回调会回来）；False = 没踢成（啥也不会发生）。
        **False 有两种，调用方得分得清**（v2-social-scene-boundary）：一种是"真抓不成"
        （没接那两只手 / 取不到帧），那就带着手上已有的字接着走；另一种是 `_chat_reading`
        已经是 True——**上一屏正在认，它认的就是本场的对方原话**，该挂上去等，不是丢开。
        """
        if self._grab_chat is None or self._read_chat is None or self._chat_reading:
            return False
        try:
            array = self._grab_chat()
        except Exception as exc:      # noqa: BLE001 —— 抓屏失败不该把冒话这条线带崩
            logger.debug("社交窗口取帧失败（%s），这一场没有对方原话", exc)
            return False
        if array is None:
            return False
        self._chat_reading = True
        # 抓拍这条腿认过这一屏了，记一笔（v2-social-on-demand-snap）：挂住那一问的
        # 按需抓拍（`_snap_pending`）拿它判"这一屏认过了"，别把同一屏再烧一趟 OCR。
        self._snap_seen_fp = self._fingerprint(array)
        run_async(
            self._read_chat,
            array,
            on_done=self._on_screen_read,
            on_failed=self._on_screen_read_failed,
        )
        return True

    def _on_screen_read(self, text: object) -> None:
        """认出字了：**只留对方那半边**，图那边用完就已经丢了。

        v2-social-ocr-isolate 起，装配层回的不再是一行扁平的字，而是切好发言人的
        `ChatRead`（气泡色分绿/白）。这里取它的 `context`——对方说的才进"对方原话"，
        自己发的那半边一个字都不进来。老口径（回字符串的桩）走 `as_chat_read` 兜住。
        """
        self._chat_reading = False
        read = as_chat_read(text)
        # 取**末尾**不是开头（v2-social-ocr-path）：OCR 从上往下读，开头是旧消息，
        # 对方最新那条压在下面。留末尾一段，让它专心认"最近对方那条"，别被整屏旧字带跑。
        # 切的位置：**一条一条**往回捡（v2-social-grab-fix 起，`_tail_context`），
        # 最新那条**整条**留着——它被气泡宽度折成几行时，切在中间就是把人家的半句话丢掉。
        # 老口径是 `_tail_lines(read.context)`（按行切），行对了、但被折行的消息仍可能被切。
        self._last_read_lines = set(tidy_lines(read.visible))
        self._chat_text = _tail_context(read)
        if self._chat_text:
            # 认回来的时刻（v2-social-stale-guard）：起草那一拍抓不成时要靠它判"这屏字
            # 还新不新鲜"。太旧的当没有，别拿上一场的旧话充数。
            self._chat_text_at = self._clock()
            logger.debug(
                "社交窗口认到 %d 个字（对方 %d 行 / 自己 %d 行），这一场拿它当对方原话",
                len(self._chat_text),
                sum(1 for s, _t in read.lines if s == SENDER_OTHER),
                sum(1 for s, _t in read.lines if s == SENDER_USER),
            )
        self._drain_pending()
        # 最后把"这一屏是哪一种页"报给服务（v2-social-trigger-window-gate）：装配层认字
        # 那一步已经判过一次（挂在 `ChatRead.page_kind` 上，判在 16-β 把总列表清成空 read
        # **之前**）。**报页型不动材料那条线**：16-β"非 active 视图清成空 read"的语义
        # 一个字没改，手动那条路（用户自己喊"帮回"）照旧拿 `_tail_context(read)` 当材料
        # ——用户明着开口那一趟不是"触发 offer"，跟批次8"空材料那条只加在自动路上"
        # 同一个先例。摆在材料那几行**后头**：那一问真要冒，也等这一屏的字先记完；
        # 而且真有草稿在拟（`_drain_pending` 刚把 `_running` 立起来）时它自己就不冒了。
        self.note_page_kind(chat_page_kind(read))

    def _on_screen_read_failed(self, exc: Exception) -> None:
        """认不出来：这一场就没有对方原话，如实当"没认到"，**绝不编一句**。"""
        self._chat_reading = False
        logger.debug("社交窗口认字失败（%s），这一场没有对方原话", exc)
        self._drain_pending()

    def _drain_pending(self) -> None:
        """主动求助那一拍挂在"等这一屏 OCR"上的草稿，现在 OCR 回来了（或失败了）：
        带着手上这屏字（可能为空）接着起草。没挂就是普通冒话那一路，什么也不做。
        """
        if self._pending_draft is None:
            return
        text, self._pending_draft = self._pending_draft, None
        hung_at = self._pending_draft_at
        hung_for = self._clock() - hung_at if hung_at else 0.0
        self._pending_draft_at = 0.0
        # 这一拍有结论了（拟 / 作废 / 再挂回去），按需抓拍那条腿先收掉；下面若决定
        # 再挂回去，会重新起（并留住画面签名——同一屏不重复认）。
        self._stop_pending_snap()
        # 批次17（甲修正）：挂在等待上的若是"点 [好呀]"那一趟（`text` 是空串），挂到
        # `OFFER_HANG_S` 才等到字就当作废——跟邀请那排按钮同一个寿命，别让几分钟前那一问
        # 在他早聊到别处去之后突然冒一段。
        if not text and hung_for > OFFER_HANG_S:
            self._running = False
            logger.info(
                "点 [好呀] 那一问挂了 %.0f 秒才等到字（超过 %.0f 秒），当过期不拟",
                hung_for, OFFER_HANG_S,
            )
            return
        chat_text = self._take_chat_text()
        # 批次17（甲）：挂在等待上的如果是**点 [好呀]** 那一趟（`text` 是空串），而这一屏
        # 认回来仍旧没有对方的话（空白屏 / 会话总列表被 16-β 清成空 read / 认字失败），
        # 就不拿「（没认到对方说了什么）」那格去开一轮瞎写——如实说一句（一问只说一次），
        # 忙态收掉，**但那一问留着接着等**（v2-social-on-demand-snap，见下）。
        # 喊「帮我回」那条路（`text` 非空）**不受这条管**：那是用户明着要的（批次8 口径）。
        if not text and not chat_text:
            # v2-social-on-demand-snap：这一屏没读到对方的话，**不再当场把这一问作废**。
            #
            # 真机 20:10:23.595 那句「如实说一句」就是逼用户"再点一次"的那一下：他点完
            # [好呀] 切回微信，切回来那一拍抓到的多半还是**会话总列表**（还没点进那个
            # 对话框），于是这一问当场作废；他找到人、点进去，得再点一次帮聊才有结果。
            #
            # 作废那条线改成跟那一问同寿（上面的 `OFFER_HANG_S`，跟邀请那排按钮一个
            # 寿命）：挂回去、接着一拍一看，拿到对方那句就拟——用户那一趟就够，不必点第二下。
            # `_running` 照老口径放掉（那一趟的"忙"到此为止），只是把问题留着；
            # `_pending_draft_at` **不动**——寿命是"从点下去算起"的总账，不是每一拍重算。
            if hung_at and hung_for <= OFFER_HANG_S:
                self._running = False
                # 「还没读到」**一问只说一次**：抓不成那一趟（`_draft_now` 里）已经说过的
                # 就不再刷屏；真空对话（抓得成、只是这屏里没有对方的话）第一次仍旧如实
                # 说一句——那时候正是用户在盯着看"点了怎么没动静"。
                if not self._pending_hinted:
                    self._pending_hinted = True
                    self._bus.pet_speak.emit(NO_PEER_LINE_HINT)
                self._pending_draft = text
                self._pending_draft_at = hung_at
                self._start_pending_snap()
                logger.debug("点 [好呀] 那一趟这一屏还没有对方的话，把这一问挂回去接着等")
                return
            self._running = False
            logger.debug("点 [好呀] 那一趟等回来的这一屏没有对方的话，不拟，如实说一句")
            self._bus.pet_speak.emit(NO_PEER_LINE_HINT)
            return
        # 挂起那条路跟直达那条路共用同一道闸（v2-social-offer-dedup）：等这一屏 OCR 的
        # 空档里，自动监听可能已经把同一份对方原话拟过了，这里别又照着它开一轮。
        source = self._normalize_listen(chat_text)
        if self._is_duplicate_source(source):
            self._running = False
            return
        self._pending_auto_source = source
        run_async(
            self._write,
            text,
            chat_text,
            on_done=self._on_candidates,
            on_failed=self._on_failed,
        )

    def _take_chat_text(self) -> str:
        """把本场那屏字取走，**取走即作废**——生成完就丢，这就是"读完即弃"。"""
        text, self._chat_text = self._chat_text, ""
        self._chat_text_at = 0.0
        return text

    def _has_fresh_chat_text(self) -> bool:
        """手上那屏字现在还能不能用（非空、且没过新鲜期）——**只判不取**。

        取走（取走即作废）只发生在真起草那一拍（`_take_chat_text` /
        `_take_chat_text_for_draft`）；这一刀是给 `_draft_now` 问"手上到底有没有能用
        的材料"的：没有才当场强制抓一屏（批次17 甲）。
        """
        return (bool(self._chat_text)
                and (self._clock() - self._chat_text_at) <= CHAT_TEXT_FRESH_S)

    def _take_chat_text_for_draft(self, *, grab_failed: bool) -> str:
        """起草真要用的那屏字：**这一拍没抓成时，过期的旧字当没有**（v2-social-stale-guard）。

        `_chat_text` 是"上一次真抓成的那一屏"，它可能是几分钟前**另一场戏**里的字。
        抓不成（前台是宠物自己 / 社交窗被挡 / 没开 OCR）时若不问一句就交出去，模型会
        拿上一屏的旧话当"对方刚发来的那条"去接——用户看到的是答非所问，而且**一行
        日志都没有**。

        三档（`grab_failed` = 这一拍社交那条路试过抓、但没抓成）：

        - 有字且还在新鲜期内（`CHAT_TEXT_FRESH_S`）→ 照用。最常见的用法正是"在微信
          看完消息、点回宠物的聊天窗说帮我回"——那一拍前台已经是宠物自己，抓必然
          抓不成，不许把这条主路一起掐死；只在 `grab_failed` 时记一条 debug。
        - 有字但已过期 → **丢掉不用**，如实走"没认到对方原话"，记一条 warning
          （这条 warning 就是"静默回退"那件事的痕迹）。
        - 没字 → 回空串（提示词那格填 `NO_CHAT_CONTEXT`）；`grab_failed` 时也记一条
          warning——"社交窗当前不可采、本场没有对方原话"本身值得留痕。
        """
        text = self._chat_text
        if not text:
            if grab_failed:
                logger.warning(
                    "社交窗当前不可采（被自身窗口遮挡 / 未在前台），本场无对方原话"
                    "——手上也没有认到的字可降级，起草按「没认到」走"
                )
            return ""
        age = self._clock() - self._chat_text_at
        if age <= CHAT_TEXT_FRESH_S:
            if grab_failed:
                logger.debug(
                    "社交窗这一拍没抓成（前台多半已换成宠物自己），用 %.0f 秒前认回来的那屏字",
                    age,
                )
            return self._take_chat_text()
        logger.warning(
            "社交窗当前不可采（被自身窗口遮挡 / 未在前台），本场无对方原话"
            "——手上那屏字是 %.0f 秒前认的（超过 %.0f 秒新鲜期），当它过期丢掉，"
            "不拿上一屏旧字充数",
            age, CHAT_TEXT_FRESH_S,
        )
        self._take_chat_text()      # 一样取走即作废，只是不交出去
        return ""

    # ---------- 自动监听（v2-social-auto） ----------

    def _sync_listen_timer(self) -> None:
        """按当前状态起/停那个轻量轮询计时器。

        要同时满足：总闸开着、自动监听开着、OCR 开着、社交软件在前台。有一条不满足
        就停——出了社交前台 / 关了 OCR，那个计时器不空转。停的时候顺手把指纹清掉，
        下次进来从"没看过"重新比。
        """
        should_run = (
            self.enabled
            and self._config.screen.social_auto_listen
            and self._config.screen.ocr_enabled
            and self._social_foreground
        )
        if should_run and not self._listen_timer.isActive():
            self._last_fingerprint = None
            self._listen_timer.start()
        elif not should_run and self._listen_timer.isActive():
            self._listen_timer.stop()
            self._last_fingerprint = None

    def _drop_stale_offer(self) -> bool:
        """邀请那排按钮挂过 `OFFER_HANG_S` 就自动作废（v2-social-fg-residual）。回 True = 这一拍收掉了。

        `_awaiting` 是"正等用户点 [好呀] / [不用了]"。用户点了走 `confirm`、关了气泡走
        `dismiss_offer`、点别处让按钮换主走 `_on_offer_hidden`——**但"既不点也不理"这条
        最平常的路上没有人来喊这一声**（气泡是常驻的，它自己不超时），`_awaiting` 于是
        一直挂着。而 `_poll_listen` 头一句就拿它 early-return：自动监听从此静默停摆，
        人明明还在跟对方一来一回，宠物再也不接。真机那条"邀请冒出来之后就再也不动了"
        就是它（另一条支路是 `monitor._proc` 那一格，见 `monitor._poll_foreground`）。

        收的时候**按"这次邀请没被接"处理**：`dismiss_offer()` 清掉"正等点按钮"，
        `reset_done()` 顺手把完成态锁也解开——上一段候选的锁留在这儿，会跟"重新开始
        监听"打架（锁一立着，`_lock_verdict` 那一关照样能把新内容咽回去）。

        **这一声要连按钮一起收**（v2-social-await-prime-fix 改动一）：这里跟"离场"那条路
        不一样——那边是"用户只是走开一下，邀请还有效"，这边是**邀请真的过期了**。只清
        `_awaiting` 而把那排按钮留在气泡上，就又是"按钮还显示、旗子已经没了"的错位，用户
        点下去照样落到通用聊天兜底。发一声 `social_offer_expired`，装配层把气泡那排收掉。

        `force=True` 是必须的：`dismiss_offer` 平时会被"按钮还挂着"挡回去（那正是为了
        治离场那条路），而这里恰恰要在按钮还挂着的时候把它清掉。
        """
        if not self._awaiting:
            return False
        hung = self._clock() - self._last_asked
        if hung < OFFER_HANG_S:
            return False
        logger.info(
            "社交邀请挂了 %.0f 秒没人应（阈值 %.0f 秒），收掉它、自动监听接着跑",
            hung, OFFER_HANG_S,
        )
        self.dismiss_offer(force=True)
        self.reset_done()
        # 界面那边跟着把那排按钮收掉：状态与按钮同寿，用户不会再点到一个已经不作数的
        # 邀请（跟 `chat_bubble.hide_offer()` 一条链，装配层接线）。
        self._bus.social_offer_expired.emit()
        return True

    def _forget_session_memory(self) -> None:
        """换了一场（换了个人）：把"上一场已经拟过什么"整套记忆清掉（v2-social-session-reset）。

        十格，缺一个都会在换会话之后显形：

        - `_done` / `_done_tail` / `_done_at` / `_done_lines` / `_locked_pending`
          （`reset_done` 一把清）：上一场那一段的完成态锁不该锁着这一场；
        - `_last_draft_source` / `_pending_auto_source`：**这一条是主犯**——上一场拟过的
          那份对方原话留着，新会话里对方说的又是同一句（"在吗""好的"这种短句最典型），
          去重闸就把这一场当"拟过了"咽掉，一行日志都不冒；
        - `_last_offered`：建卡那一问的"这一趟只问一次"跟着翻篇；
        - `_last_listen_tail` / `_last_listen_kind`：这一屏是**新一场的底**，不跟上一场的
          尾巴比。这两格跟 `on_foreground` 里那段清场**不一样**是有原因的：那一段是
          "走出社交前台"，那时计时器已经停了、指纹也清了；这里计时器照跑、人在原地，
          不清这两格就等于"刚点开的新会话"被当成"对方发了新消息"，当场照着人家的旧话
          拟一段。同理，调用方在换人那一拍会把指纹也作废，这一屏无论如何重新认一遍。

        **不动 `_awaiting`、不收那排按钮**：按钮归谁由界面那半说了算（见
        `dismiss_offer`），"换个会话"不是"用户不理这一问"，在这儿收掉会把界面与状态
        弄岔。邀请挂太久自有 `_drop_stale_offer` 兜底。
        """
        self.reset_done()
        self._last_draft_source = ""
        self._pending_auto_source = ""
        self._last_offered = 0.0
        self._last_listen_tail = ""
        self._last_listen_kind = ""

    def _sync_session(self) -> bool:
        """这一场还是不是上一场那个聊天对象；换了人就清记忆（v2-social-session-reset）。

        **为什么非有不可**：在微信里换个会话，"跟前台有关的一切"一个都没变——进程还是
        wechat.exe，`on_foreground` 不会发第二次，于是那条"走出社交前台"的清场一次都
        不触发。上一场的 `_last_draft_source` / `_done` 就原样带进了下一场，把新会话的
        对方原话挡在门外（真机那条"聊完一个对话、换个对话对方回话就不动了"，跨会话
        死区就是这么来的）。

        只问一句"这一场跟上一次是不是同一个人"，三档：

        - **取不到**（空串：还没抓过、或装配层那次没认出来）→ 什么都不动。宁可漏清
          一场，也不误清一场；
        - **跟上次一样 / 上一次还没记下** → 只把这一场记上；
        - **换了人** → `_forget_session_memory()` 清掉上一场，回 True。

        回 True 的调用方要把画面指纹一起作废：换人这一屏得重新认一遍，它就是新一场的底。

        **日志只记长度不记名字**：会话标识就是对方 / 群名，属于用户内容，跟抓来的那屏
        字一个口径——够看出"换人了"，又不把人家在跟谁聊写进日志。
        """
        if self._session_provider is None:
            return False
        ident = self._normalize_listen(self._session_provider())
        if not ident:
            return False
        previous = self._session_id
        self._session_id = ident
        if not previous or previous == ident:
            return False
        logger.debug(
            "社交窗换了会话（上一场标识 %d 字 → 这一场 %d 字），上一场的拟稿记忆一并清掉",
            len(previous), len(ident),
        )
        self._forget_session_memory()
        return True

    def _poll_listen(self) -> None:
        """轻量轮询一拍：只比画面指纹，变了才 OCR；没变一次 OCR / 一次模型都不耗。

        这是"节奏由对方定、不轮询不刷屏"的落点——平时每 1.5 秒只是抓一屏、算个
        哈希（毫秒级），几乎不花钱；只有画面真变了（来了新消息）才去认字、拟草稿。
        """
        if not self._social_foreground:
            return
        # 邀请挂太久了自己收掉（v2-social-fg-residual）：见下面 `_drop_stale_offer`。
        # 摆在 early-return **前面**是必须的——它就挂在 `_awaiting` 那一格上，
        # 排在后面等于永远轮不到。
        self._drop_stale_offer()
        if self._running or self._awaiting or self._chat_reading or self._listen_reading:
            return
        if self._grab_chat is None or self._read_chat is None:
            return
        # 冷却期内看见过没处理过的新内容、被完成态锁压住了（v2-social-grab-fix）：画面
        # 要是不再变，指纹那条路不会再看第二眼，那条消息就永远咽下去了。冷却一过主动把
        # 指纹清掉，逼下一拍重新认一次——那时候 `_lock_verdict` 的"冷却也过了"那条
        # 就该放行。只在这一种情况下强制重认，平时照旧"画面没变就不认字"。
        if (self._locked_pending
                and self._clock() - self._done_at >= SOCIAL_DONE_COOLDOWN_S):
            self._last_fingerprint = None
        try:
            array = self._grab_chat()
        except Exception as exc:      # noqa: BLE001 —— 抓屏失败不该把监听这条线带崩
            logger.debug("自动监听取帧失败（%s），这一拍跳过", exc)
            return
        if array is None:
            self._last_fingerprint = None
            return
        # 换会话了（v2-social-session-reset）：先清掉上一场的记忆，**再把指纹作废**——
        # 这一屏是新一场的底，得重新认一遍（`_on_listen_read` 认到尾巴为空只打底，
        # 不会拿新会话顶上那句旧话当"对方刚发的"）。摆在这儿而不是更前面：会话标识
        # 是装配层拿**这一拍抓的这帧**读出来的，同一个拍里清掉才算数。
        if self._sync_session():
            self._last_fingerprint = None
        fp = self._fingerprint(array)
        if fp == self._last_fingerprint:
            return                       # 画面没变：什么都不做
        self._last_fingerprint = fp
        # 画面变了：后台认字（约 4 秒），回来判归属、决定要不要直接拟。
        self._listen_reading = True
        run_async(
            self._read_chat,
            array,
            on_done=self._on_listen_read,
            on_failed=self._on_listen_read_failed,
        )

    @staticmethod
    def _fingerprint(array) -> str:
        """把这一帧压成一小段签名，用来判"画面变没变"。

        只抽 8x8 网格再哈希：新消息气泡是一整块文字，肯定翻盘；而光标一闪、轻微
        抗锯齿这种几像素的动静，抽稀之后几乎不影响签名，不会每拍都误触 OCR。
        `array` 可能是真数组（有 `.tobytes`），也可能是测试塞进来的字符串——都兜住。
        """
        try:
            if hasattr(array, "tobytes"):
                small = array[::8, ::8]
                data = small.copy().tobytes() if hasattr(small, "copy") else small.tobytes()
            else:
                data = repr(array).encode("utf-8")
        except Exception:      # noqa: BLE001 —— 形状对不上就退回整块 repr，别让哈希塌
            data = repr(array).encode("utf-8")
        return hashlib.md5(data).hexdigest()

    # ---- 挂住那一问的按需抓拍（v2-social-on-demand-snap） ----
    #
    # 病根（真机 2026-10-08 20:09:47–20:10:28，日志原文见收尾报告）：
    # 用户点 [好呀] 那一刻前台是**宠物自己**，`_grab_chat` 抓不到微信那块像素——
    # 不是"缓存旧帧"、也不是"发出去没等回来"，是这一拍物理上就抓不到。于是只能如实
    # 说一句"还没读到"；他切回微信之后「前台换人了」这条信号要等**每 5 秒一次**的采样
    # 才发出来，再加上每一趟 OCR 2.2~5.2 秒，用户看到的就是"报没读到 → 干等 → 再点一次"。
    #
    # 这条腿压掉的就是前面那 5 秒：挂住期间自己每 500 毫秒看一眼前台，社交窗一回来
    # 立刻抓一屏（OCR 那 2~5 秒省不掉，那是真在认字）。

    def _start_pending_snap(self, *, fresh: bool = False) -> None:
        """挂住那一问开始"一拍一看"。`fresh=True` 表示新起的一问——画面签名也从零开始。

        非 fresh 那一档是"这一屏已经认过、判过、还是空的，挂回去接着等"：签名**留着**，
        否则同一屏会被一拍一拍地反复烧 OCR（每一趟 2~5 秒）。
        """
        if fresh:
            self._snap_seen_fp = None
            self._pending_hinted = False
        if not self._snap_timer.isActive():
            self._snap_timer.start()

    def _stop_pending_snap(self) -> None:
        """这一问有结论了（拟了 / 作废了）：把这条腿收掉。幂等。

        **不清签名**：签名是"抓拍这条腿认过哪一屏"，跟这一问的死活无关；新起一问时
        （`fresh=True`）才清。在这儿清掉，挂回去那一路就会反复重认同一屏。
        """
        if self._snap_timer.isActive():
            self._snap_timer.stop()

    def _poll_pending_snap(self) -> None:
        """挂住那一问的一拍：自看一眼前台，社交窗在就抓一屏去认。

        几道不叠拍的闸（都是"省一趟 OCR"，不影响判据）：

        - 那一问已经没了（拟了 / 收掉了）→ 收掉这条腿。
        - 挂过 `OFFER_HANG_S`（跟邀请那排按钮同一个寿命）→ 收掉，并让 `_drain_pending`
          按过期如实说一句——跟批次17 甲那条线同一处口径，不另开一条。
        - 本服务正认着字（`_chat_reading` / `_listen_reading`）→ 让那一拍先回来。
        - 既有的自动轮询计时器活着 → 让给它（那边 1.5 秒一拍、带一整套纪律）；这条腿
          只在"轮询没在跑、而这一问还挂着"时出力。
        """
        if self._pending_draft is None:
            self._stop_pending_snap()
            return
        if self._pending_draft_at and self._clock() - self._pending_draft_at > OFFER_HANG_S:
            self._stop_pending_snap()
            self._drain_pending()
            return
        if self._chat_reading or self._listen_reading:
            return
        if self._listen_timer.isActive():
            return
        self._snap_pending()

    def _snap_pending(self) -> bool:
        """这一拍替挂住那一问抓一屏去认（同一屏不重复认）。回 True = 真把认字踢出去了。

        抓不成（前台还是宠物自己 / 别的软件 / 没开 OCR）就如实回 False，下一拍再看——
        `_grab_chat` 在这几种情况下是当场 return None 的，几乎不耗。
        """
        if self._grab_chat is None or self._read_chat is None:
            return False
        try:
            array = self._grab_chat()
        except Exception as exc:      # noqa: BLE001 —— 抓屏失败不该把这一问带崩
            logger.debug("挂住那一问这一拍取帧失败（%s），下一拍再看", exc)
            return False
        if array is None:
            return False
        # 换会话了（v2-social-session-reset）：先清掉上一场的拟稿记忆，**再把这一份签名
        # 作废**——这一屏是新一场的底。跟 `_poll_listen` 那一处同一套（会话标识是装配层
        # 拿这一拍刚抓的这帧读出来的，同一个拍里清掉才算数）。这一条腿正好跑在"用户刚从
        # 会话总列表点进某个对话"那一瞬，最是换人的时候。
        if self._sync_session():
            self._snap_seen_fp = None
        fp = self._fingerprint(array)
        if fp == self._snap_seen_fp:
            return False               # 这一屏已经认过、判过了：不重复烧一趟 OCR
        self._snap_seen_fp = fp
        self._chat_reading = True
        run_async(
            self._read_chat,
            array,
            on_done=self._on_screen_read,
            on_failed=self._on_screen_read_failed,
        )
        return True

    @staticmethod
    def _normalize_listen(text: object) -> str:
        """OCR 认出来的字压成一行，供"变没变 / 谁发的"比较用。

        **这里拍平换行是故意的**（v2-social-multiline）：它只喂给 `==` 比相等，
        不喂模型。"同一条消息换不换行"对"画面变没变"没有意义——OCR 每拍重新认一遍，
        同一屏行内空白抖动一下就会判成"变了"，反倒连拟两段。换行语义那件事在
        `ChatRead` / `_tail_lines` 那一层保住，两条线不打架。
        """
        return " ".join(str(text or "").split())

    def _is_duplicate_source(self, source: str) -> bool:
        """这份对方原话（压平后）是不是上一轮已经拟过的那一份（v2-social-offer-dedup）。

        自动（`_auto_generate`）与手动（`handle_request` / `_drain_pending`）两条路在
        **起草前**都过这一道闸：材料（喂给模型的那份对方原话）真变了才放行，没变却被
        当成变了就压住。真机"连回三条"里后两条，多半就是同一份被宠物浮窗污染过的字又
        拟了一遍。空材料（没认到对方原话）不算重复——没材料时每次都得照常拟。
        """
        if source and source == self._last_draft_source:
            logger.debug(
                "这份对方原话上一轮已经拟过了（%d 字），这一轮不重开（v2-social-offer-dedup）",
                len(source),
            )
            return True
        return False

    def _classify_incoming(self, read: object, old_tail: str, idle_sec: float) -> str:
        """判这次尾巴变化是谁发的：返回 "incoming"（对方）/ "self"（用户自己）/ "none"
        ——外加一个 **"locked"**（这一段拟完了、这一屏还锁着，这一拍什么都不办）。

        `"locked"` 与 `"none"` 的区别只在**调用方怎么记账**：`"none"` 是"这一拍看过了、
        没什么可办的"（尾巴照记），`"locked"` 是"这一屏我先扣下、还没办"（尾巴**不记**，
        见 `_on_listen_read`）。记了就等于把那条消息咽掉——冷却一过也没人会再看它一眼。

        **两条腿，硬的那条在前（v2-social-ocr-isolate）。**

        ① **气泡色**：`read.tail_sender` 是从"最后一行分得出说话人的那条"读出来的——
        绿气泡 = 用户自己刚发的，白气泡 = 对方刚回的。这是直接证据，有它就不看第二条。
        ② **空闲时长**（`SEND_IDLE_S`，老口径）：色卡认不出来时的兜底。最近几秒有键鼠
        动作 = 多半是用户自己在敲、在发；没有 = 对方回了。

        哪条都不成立就 "none"——字没变（光标 / 滚动那种假变化）、或者这是第一次认到
        这一屏（只是打底）。**宁可闭嘴也不谎报**这条没动。

        参数收 `ChatRead`（或能 `as_chat_read` 的东西）而不是一行字符串：色卡那条腿要
        的就是"谁说的"这份信息，光有一排字给不出来。

        **比的是 `visible`（两边都算），不是 `context`（只有对方）**：用户自己刚发的
        那条也是新内容，只盯对方那半边就看不见"我刚发出去"这档，末尾归属跟着塌。
        分工是——`visible` 判"变没变"，`tail_sender` 判"新的是谁发的"，`context`
        只用在最后喂模型那一步。
        """
        chat = as_chat_read(read)
        new = self._normalize_listen(chat.visible)
        old = self._normalize_listen(old_tail)

        # **完成态锁（v2-social-copy-clean）压在最前头**：这一段已经拟完交给用户了，
        # 他正要去粘贴。这会儿聊天窗难免重绘（焦点圈、滚动条、光标），认出来的字会抖
        # 一下——不锁的话那一抖就被当成"来了新消息"，又拟一段，真机上看着就是卡死。
        #
        # **v2-social-grab-fix 起，判据从"整条尾巴比相等 + 等够冷却"改成逐行比**
        # （`_lock_verdict`）：冷却只挡"这一屏的重开"，**不挡对方新发消息**。
        # 返回 `"locked"` 是一个跟 `"none"` 分开的口径——"none" 是"这一拍没什么可办的"，
        # 调用方照旧把它当处理过的；"locked" 是"这一屏我先扣下"，调用方**不许**把它
        # 记成"处理过了"，否则冷却一过也没人再回头看它一眼。
        if self._done:
            if self._lock_verdict(chat) == "hold":
                return "locked"
            self.reset_done()          # 放行：这一屏不是完成那一刻那一屏了

        if not new or new == old:
            return "none"          # 字没变：光标 / 滚动那种假变化，不当新消息
        if not old:
            return "none"          # 第一次认到这一屏：只是打底，还不算"来了新消息"
        sender = chat.tail_sender
        if sender == SENDER_USER:
            return "self"          # 末尾是绿气泡：这条是用户自己发的，直接定案
        if sender == SENDER_OTHER:
            return "incoming"      # 末尾是白气泡：对方回了新的，也直接定案
        if idle_sec < SEND_IDLE_S:
            return "self"
        return "incoming"

    def _on_listen_read(self, text: object) -> None:
        """这一拍认字回来了：判归属，对方回的就直接拟候选（跳确认），自己发的就如实说。"""
        self._listen_reading = False
        # **前台已经不是社交软件了 → 这一刻不起草，而且这一屏当没看过**
        # （v2-social-input-draft-exclude）。
        #
        # 认字是后台跑的（约 4 秒），回来时前台可能早切走了；而抓的那一帧也未必是聊天窗
        # ——真机现场（2026-10-07 15:46:11）那一拍前台是 explorer.exe，抓下来的是任务栏
        # 那一条（帧 2560x72），OCR 把任务栏的搜索框认成 `Q 搜索`，宠物照拟一段。
        # `_poll_listen` 门口那道 early-return 拦不住这件事：它看的是**抓屏那一刻**的
        # `_social_foreground`，不是**现在**。抓屏那一头另有一道按当下进程名拦的闸
        # （`bootstrap._grab_social_chat`），两道各管一头。
        #
        # **摆在最前面、而且要把指纹退回去，是为了"当没看过"而不是"咽掉"**：这一格在
        # `_auto_generate` 里放晚了——那时 `_last_listen_tail` 已经推进过了，一闸就把
        # 对方刚回的那句记成"处理过了"（下一拍 `_classify_incoming` 判 `new == old` →
        # "none"），人切回聊天窗也再没人回头看它一眼。指纹退成 None，下一拍在社交窗里
        # 会重新认这一屏，材料该拟还是照拟。
        if not self._social_foreground:
            self._last_fingerprint = None
            logger.debug("前台已经不是社交软件了，这一屏当没看过（不认字、不起草）")
            return
        read = as_chat_read(text)
        # **这一屏认出来不是单聊就不进帮聊**（v2-social-trigger-window-gate）：群聊里
        # 对方那句话可能是张三说的，自动拟一段"回他"就答错了人；会话总列表 / 订阅号 /
        # 文件传输助手那几种页更是一句人话都没有。页型由装配层认字那一步判好挂在 read 上。
        #
        # **当没看过，不当"处理过了"**：尾巴（`_last_listen_tail`）一行都不推进——推进了
        # 就等于告诉下一拍"这条我处理过了"（跟下面 `kind == "locked"` 那一支同一个道理）。
        # 指纹不动：这一页静止时下一拍照样"没变"，不会每 1.5 秒白认一次字；它真变了
        # （对方又发一条、用户自己点去别处）下一拍自然重新认一遍。
        kind = chat_page_kind(read)
        self.note_page_kind(kind)
        if not is_personal_kind(kind):
            logger.debug("这一屏不是单聊（%s），不认作对方的消息、不起草", kind)
            return
        # 存的是 `visible`（两边都算）——下一拍要拿它比"画面变没变"，只存对方那半边
        # 会让"我自己刚发了一句"这档在下一拍被当成"没变"。
        new_tail = self._normalize_listen(read.visible)
        # 逐行那一份也留一手（v2-social-grab-fix）：完成态锁判"画面换没换"比的是**行**，
        # 不是一整条扁平的字符串——一整条比相等太脆，OCR 少认一行就判成"变了"。
        # `mark_done` 就是拿这一份当"完成那一刻那一屏"的底。
        self._last_read_lines = set(tidy_lines(read.visible))
        kind = self._classify_incoming(read, self._last_listen_tail, self._idle_seconds())
        if kind == "locked":
            # **这一屏还没办**，尾巴不许推进（v2-social-grab-fix）：推进了就等于告诉
            # 下一拍"这条我处理过了"，而它其实还压着没办——冷却一过也没人回头看它。
            return
        if kind == "none":
            if new_tail:
                self._last_listen_tail = new_tail
            return
        self._last_listen_tail = new_tail
        if kind == "self":
            # 用户自己刚发的：**静默不插嘴**（v2-social-remove-ownmsg-prompt）——一条提示
            # 都不弹（旧口径在这儿冒一句"这条是你自己发的，对方还没回新内容"，用户正打字，
            # 这一句既没用又碍事）。也不拟草稿、不替换成别的文案、不报错，只把归属记下。
            self._last_listen_kind = "self"
            return
        # incoming：对方回了新消息 → 跳确认，直接带这一屏字拟候选。
        self._last_listen_kind = "incoming"
        # 喂模型的是**对方那半边、按条取尾**（v2-social-grab-fix），**不是** `new_tail`
        # （`visible`，两边都算，里面还混着用户自己刚发的那句）。老口径这一格递下去的是
        # `read.context`——整屏对方的话（顶上那些旧消息全在里面），而且**一次裁剪都没有**：
        # 几千字的提示词，模型既慢、又会挑着旧消息回。真机"等几分钟才反应、而且回的是
        # 上面那句旧话"就是这一格。现在跟手动那条路走同一个口径。
        self._auto_generate(_tail_context(read))

    def _on_listen_read_failed(self, exc: Exception) -> None:
        """这一拍认字失败：当没看见，安静跳过（不谎报、不硬编）。"""
        self._listen_reading = False
        logger.debug("自动监听认字失败（%s），这一拍跳过", exc)

    def _auto_generate(self, chat_text: str) -> None:
        """对方回了新消息：跳确认，直接拟一段候选（不先问「要帮忙吗」）。

        复用起草那条现成的链（`_write` → `_on_candidates` → 装配层亮出来），只是把
        "用户讲处境"换成固定的一句、把"对方最近那条"换成刚认到的一屏字。冷却内
        不叠第二段（同一条消息 OCR 认两遍、对方连发两条，别连拟两段）。

        **同一份对方原话不重开一轮（v2-social-listen-dedup）。** 这条路上游那两道
        去重都挡不住真机那条：宠物自己浮窗里的字一变，"画面指纹"与 `visible` 的文本
        比相等就都翻了，可 `_tail_context` 取出来的**对方原话一个字没变**——于是过了
        8 秒（`LISTEN_COOLDOWN_S`）就照着同一句话再拟一段，真机上看着就是"同一个方案
        隔一会儿蹦一段"。这里比的是**喂给模型的那一份**（`chat_text`）：

        - 比 `visible` 没用：它混着用户自己刚发的那句、也混着屏幕上别处的动静；
        - 比 `chat_text` 才对应"模型看到的材料换了没有"这个真问题。

        手动那条路（用户自己开口喊帮回）走 `handle_request`，跟这里**共用同一道闸**
        （v2-social-offer-dedup）：同一份材料在那儿也照压不误——材料真变了才放行，
        没变被当变就压住，两条路一个口径。
        """
        if self._running:
            return
        source = self._normalize_listen(chat_text)
        if not source:
            # ★ 批次8：材料一个字都没有，这趟不拟。
            #
            # 这一条只加在**自动**这条路上，手动那条（`handle_request`）照旧——
            # 用户自己开口喊"帮我回"，材料没认到也得照拟一段（那是他明着要的）。
            # 自动这条不一样：它是宠物自己看着屏幕决定的，材料为空说明**这一拍压根
            # 没有对方的话**，拟出来的只会是对着"没认到对方说了什么"瞎写的一段。
            #
            # 批次8-A/B 收窄材料之后这一格才有意义：收窄会让"表上看着变了、材料其实
            # 是空"的那些拍（屏幕上只多了一行时间戳、或者只多了一行输入框占位灰字）
            # 真的落到空材料上——不挡的话，每一拍都会照着占位提示起草一次，
            # 正是真机那四趟 POST 里"杂音"那一趟的去向。
            #
            # `_is_duplicate_source` 那句"空材料不算重复"一个字没动：它管的是"同一份
            # 材料别拟两遍"，跟"压根没材料要不要拟"是两回事（红线：去重口径不动）。
            logger.debug("这一拍没认到对方的话，不拟（批次8：材料边界）")
            return
        if self._is_duplicate_source(source):
            return
        now = self._clock()
        if now - self._last_listen_at < LISTEN_COOLDOWN_S:
            return
        self._last_listen_at = now
        self._running = True
        # 拟成功了才认这一屏（v2-social-listen-dedup）：失败了下一拍照旧可以重来，
        # 不然"拟不出来"会把这一屏永久咽掉。
        self._pending_auto_source = source
        # 这里不落 `_chat_text`（那是给主动求助路径用的）；直接用手上这屏字当对方原话，
        # 生成完即丢。**这屏字是切过的**（v2-social-ocr-isolate）：`_on_listen_read`
        # 传进来的是 `ChatRead.context`，自己发的那半边与宠物窗口那几块都已经不在里面。
        run_async(
            self._write,
            AUTO_TRIGGER_TEXT,
            chat_text,
            on_done=self._on_candidates,
            on_failed=self._on_failed,
        )

    def _due(self) -> bool:
        """现在该不该冒这句（社交这一档是**跨应用**共用的冷却）。"""
        cooldown = max(0, int(self._config.screen.social_cooldown_min)) * 60
        return self._clock() - self._last_asked >= cooldown

    # ---------- 要 / 不要 ----------

    def confirm(self, answer: str) -> bool:
        """用户点了轻按钮。返回 True = 这活儿我接了，False = 不用了（静默陪伴）。

        [好呀]：**当场照对方末句拟一段**（v2-social-chat-ui-copy 批次11）。
        老口径是再问一句"那你说说是什么情况"，把用户接下来打的那句当处境——那一步
        多了一次来回，用户点 [好呀] 要的是能直接粘的那段，不是又被问一句。现在处境那格
        直接留空（`_draft_now("")`），提示词里也补了"那格可能是空的"这一条
        （`SOCIAL_DRAFT_PROMPT` 第 3a 条）。
        [不用了]：什么都不说——不追问、不劝、不换成别的建议，回到安静陪着。
        """
        self._awaiting = False
        if not self.enabled:
            return False
        # 用户又开口要一轮了：上一轮的完成态锁解开（v2-social-copy-clean），
        # 不然他重新问一次会被自己的锁挡在外面。
        self.reset_done()
        if not self._is_yes(answer):
            self._want_situation_until = 0.0
            logger.debug("用户说不用，这次就不写了")
            return False
        return self._draft_now("")

    @staticmethod
    def _is_yes(answer: str) -> bool:
        a = (answer or "").strip()
        return any(word in a for word in YES_WORDS)

    def _waiting_for_situation(self) -> bool:
        """点了 [好呀] 之后，还在等他把处境讲出来（过了窗口就不等了）。"""
        if self._want_situation_until <= 0:
            return False
        if self._clock() > self._want_situation_until:
            self._want_situation_until = 0.0
            logger.debug("等处境等过窗口了，这次就不写了")
            return False
        return True

    # ---------- 用户主动给上下文 ----------

    def is_request(self, text: str) -> bool:
        """这句是不是在跟我要一段回话的草稿。**危机词面前一律不算。**

        危机词最高优先是红线：`handle_request` 会**跳过普通回话那条链**，所以要是
        在这儿放行，"帮我回一下，可是我真的不想活了"就会被当成起草稿的活儿，
        把该走的危机话术整个盖掉。宁可这句草稿不做，也不许越过去。

        两条路都算"要草稿"：① 他自己在聊天里说出求文的词（老入口，一直保留）；
        ② 他刚点了 [好呀]、正等着讲处境——这时候他讲的就是处境，不必再带求文的词。
        """
        if not self.enabled:
            return False
        body = (text or "").strip()
        if not body or crisis.is_crisis(body):
            return False
        if self._waiting_for_situation():
            return True
        return any(word and word in body for word in self._words())

    def handle_request(self, text: str) -> bool:
        """要草稿就认下来（返回 True，调用方别再走普通回话），不是就返回 False。

        返回 True 只表示"这活儿我接了"，**不代表发出去**——草稿最后亮在宠物自己的
        非模态气泡里（气泡上挂一个「复制回复」，按了才抄进剪贴板），发不发由用户自己定
        （见模块开头第 2 条）。

        这里只管**两道闸**（危机词 / 算不算要草稿），过了闸的活儿交给 `_draft_now`——
        点 [好呀] 那条路（`confirm()`）从**闸后面**进来，两条路共用同一段拟文。
        """
        if (text or "").strip() and crisis.is_crisis(text):
            # 红线：危机词最高优先。这一句归危机话术，草稿这条线当没接过活儿；
            # 顺手把"正等处境"那个窗口也作废——它不该跨过一次危机还留在这儿。
            self._want_situation_until = 0.0
            return False
        if not self.is_request(text):
            return False
        return self._draft_now(text)

    def _draft_now(self, text: str) -> bool:
        """这一趟真去拟一段（v2-social-chat-ui-copy 批次11 从 `handle_request` 里抽出来）。

        **两条路共用它**，区别只在 `text`：① 用户自己在聊天里说"帮我回一下…"，
        `text` 就是他讲的那段处境；② 用户点了 [好呀]，`text` 是**空串**（批次11 起
        不再先问一遍处境）——空处境那档由 `SOCIAL_DRAFT_PROMPT` 第 3a 条兜住。

        抽出来是因为这两条路除了"处境那一格给什么"之外，**抓屏 / 挂起等认字 / 去重闸 /
        起线程**每一步都得一模一样：抄一份出来迟早会漂（批次11 之前 `confirm()` 走的是
        "再问一句"，所以没这问题；现在它直接拟文，就必须共用同一段）。
        """
        self._awaiting = False
        self._want_situation_until = 0.0
        self._card_awaiting = False
        # 他明确又要一段了：上一轮的完成态锁解开（v2-social-copy-clean）。
        self.reset_done()
        self._last_request = text
        if self._running:
            self._bus.pet_speak.emit("上一段我还在写，稍等一下。")
            return True
        self._running = True
        # 主动求助也抓屏（v2-social-ocr-path）：社交软件要是还挂在前台（语音喊的、
        # 没切走前台），再抓一屏刷新对方最近那条，带着它起草；抓不成（没开 OCR /
        # 上一屏还在认 / 取不到帧）就带着手上已有的（可能为空）走，照旧降级"没认到"。
        grab_failed = False
        # 批次17（甲）：点 [好呀]（`text` 是空串）而**手上没有能用的材料**（一个字都没有、
        # 或那屏字已经过了新鲜期）时，不等"社交窗在前台"那个**采样信号**——当场强制即时
        # 抓一屏再说（`snap_now` 那个脾气：不排节流、当下就拍）。原来这一格只在
        # `_social_foreground` 为真时才抓，于是"人已经点回宠物自己这扇窗、手上又没有字"
        # 的这一拍直接带着空材料去拟，模型只能对着「（没认到对方说了什么）」瞎写一段。
        # 喊「帮我回」那条路（`text` 非空）不在此列：那是用户明着要的（批次8 口径）。
        force_snap = not text and not self._has_fresh_chat_text()
        if ((self._social_foreground or force_snap)
                and self._grab_chat is not None and self._read_chat is not None):
            grab_failed = True
            self._pending_draft = text
            self._pending_draft_at = self._clock()
            if self._read_social_screen():
                return True          # 等这一屏认回来再起草（_on_screen_read 会接下去）
            if self._chat_reading:
                # **上一屏还在认**（v2-social-scene-boundary）：那不是"抓不成"，是
                # "这一场正在认、结果马上就到"——`on_foreground` 一进社交前台就抓的
                # 那一屏正是它。这时候把 `_pending_draft` 丢掉，就是冷启动第一次
                # "帮我回"的离题根源：手上 `_chat_text` 还是空的，模型拿不到对方原话，
                # 只能现编一句泛泛的（真机实测首条是「借阅一本书籍」，第二次才正常）；
                # 第二次为什么正常，也正是因为这屏已经认回来了。
                # 挂回去等它：`_on_screen_read` 落地时会 `_drain_pending` 接着起草。
                logger.debug("社交窗口上一屏还在认，这次起草挂上去等它接上")
                return True
            if force_snap:
                # 批次17（甲修正）：点 [好呀] 而手上**一个字都没有**（这一拍也真没抓成）——
                # 那一刻前台正是宠物自己
                # 那扇窗（用户刚点完按钮），社交窗必被挡着，在这儿硬抓只能抓回一屏
                # 空的（批次17 甲原来就是这么干的）。不再当场收摊、也不硬抓：把这一问
                # **挂住**（`_pending_draft` 留着，时间戳上面那几行已记），如实说一句就走。
                # 用户切回微信那一刻，`on_foreground` 里那条既有的抓拍路
                # （`_read_social_screen`）会把字认回来，`_on_screen_read` 末尾的
                # `_drain_pending` 自动接上起草——跟"专注结束补上被压下的那一问"（14-F）
                # 同一种做法：挂住的旗子 + 那条沿上的收口。
                #
                # v2-social-on-demand-snap：**那"一刻"不再只等前台采样那一拍**（真机实测
                # 它是 5 秒一次），挂住期间自己每 `PENDING_SNAP_MS` 看一眼前台，社交窗一
                # 回来立刻抓——见 `_poll_pending_snap`。用户那一趟就够，不必点第二次。
                self._running = False
                logger.debug("点 [好呀] 但社交窗不在前台、这一拍抓不成，把这一问挂住等切回来")
                self._start_pending_snap(fresh=True)
                self._pending_hinted = True     # 这一句已经说了：挂住那几拍不再重复
                self._bus.pet_speak.emit(NO_PEER_LINE_HINT)
                return True
            # 喊「帮我回」（`text` 非空）那条路照旧：用户明着要的，带着他讲的那段处境
            # 往下走（批次8 口径），不在这儿等。
            self._pending_draft = None
            self._pending_draft_at = 0.0
        # 本场那屏字在这儿取走（取走即作废）：起草要用它，用完这条线上就没有了。
        # **过了新鲜期的当没有**（v2-social-stale-guard）：不拿上一屏旧字静默充数。
        chat_text = self._take_chat_text_for_draft(grab_failed=grab_failed)
        # 批次17（甲）：点了 [好呀] 却**一个字都没读到** —— 不拟，如实说一句"还没读到，
        # 稍等"，忙态收掉（不收掉的话这条线从此卡在"上一段我还在写"上）。不落"刚看到消息"
        # 这类兜底占位：那等于说"我看见了"。
        #
        # 这一格到这儿只剩"真没得抓"那几种（没接那两只手 / 没开 OCR / 连帧都取不到），
        # v2-social-on-demand-snap 把"抓回来仍旧没有对方的话"那一种挪去了 `_drain_pending`：
        # 那种情形那一问**留着接着等**（挂住那一拍一看），不再当场作废。
        if not text and not chat_text:
            self._running = False
            logger.debug("点了 [好呀] 但对方那句一个字都没读到（当场抓拍之后仍空），不拟")
            self._bus.pet_speak.emit(NO_PEER_LINE_HINT)
            return True
        # 手动这条路也过同一道"材料没变不重开"的闸（v2-social-offer-dedup）：同一份
        # 对方原话在自动监听那边已经拟过一遍了，用户再点「好呀 / 帮我回」不该又照着它
        # 拟一段（真机"连回三条"的手动腿）。挡下时把刚立起来的忙态收掉，静默让位。
        source = self._normalize_listen(chat_text)
        if self._is_duplicate_source(source):
            self._running = False
            return True
        self._pending_auto_source = source
        run_async(
            self._write,
            text,
            chat_text,
            on_done=self._on_candidates,
            on_failed=self._on_failed,
        )
        return True

    # ---------- 起草 ----------

    def _provider(self):
        """一条既有的普通对话通道。起草与抽对方信息都从这儿取模型，`LLMFactory.create`
        在模块里只有这一个引用点（红线：不发新网络请求、不另开口子）。"""
        return LLMFactory.create(self._config)

    def _write(self, text: str, chat_text: str = "") -> dict:
        """后台线程里起草：要一个 `{"candidates": [...]}` 的 JSON（1 条，带解析）。

        `chat_text` 是本场从社交窗口 OCR 出来的字（对方最近那条在里面，可能为空）。
        两路风格范例现取（`{examples}`）：全局 `<聊天范例>/` + 命中的对象攻略卡
        《聊天范例.md》。范例读不出来只记日志、当没有——辅助料不该挡住起草。
        """
        try:
            examples = (self._examples_provider(f"{chat_text}\n{text}") or "").strip()
        except Exception as exc:      # noqa: BLE001 —— 见上
            logger.debug("社交范例取不出来（%s），这一段不摆", exc)
            examples = ""
        prompt = SOCIAL_DRAFT_PROMPT.format(
            chat_context=chat_text or NO_CHAT_CONTEXT,
            text=text,
            examples=(examples[:EXAMPLES_CLIP] or NO_EXAMPLES),
        )
        try:
            return self._provider().chat_json(prompt, system=SOCIAL_DRAFT_SYSTEM) or {}
        except LLMOfflineError:
            logger.warning("断网，社交帮回改走本地候选")
            return self._offline_candidates()

    def _offline_candidates(self) -> dict:
        """断网时给的那条本地候选：先问接进来的来源（按熟悉度挑），问不到用默认那条。"""
        provider = self._offline_candidates_provider
        if provider is not None:
            try:
                picked = provider()
            except Exception as exc:      # noqa: BLE001 —— 兜底文案不该因为读库失败而塌
                logger.error("断网候选取不出来（%s），用默认那条", exc)
            else:
                if isinstance(picked, dict) and picked.get("candidates"):
                    return picked
        return OFFLINE_CANDIDATES

    @staticmethod
    def _normalize_candidates(data: object) -> list[dict[str, str]]:
        """模型给的候选回复过一遍闸：只要 `candidates` 数组里 `draft` 非空的那些。

        每条保 `draft`（正文）与 `why`（解析），`why` 缺了就留空串。全都不合格返回空表，
        调用方照 `FAILED_LINE` 处理——**绝不自己编一条摆上去**。
        """
        if not isinstance(data, dict):
            return []
        raw = data.get("candidates")
        if not isinstance(raw, list):
            return []
        out: list[dict[str, str]] = []
        for item in raw:
            if not isinstance(item, dict):
                continue
            draft = str(item.get("draft") or "").strip()
            if not draft:
                continue
            why = str(item.get("why") or "").strip()
            out.append({"draft": draft, "why": why})
        # v2-social-single-ui：候选只留第一条（模型偶尔还回 3~5 条，这里兜底砍到 1）。
        return out[:1]

    def _on_candidates(self, data: object) -> None:
        self._running = False
        source, self._pending_auto_source = self._pending_auto_source, ""
        candidates = self._normalize_candidates(data)
        if not candidates:
            self._bus.pet_speak.emit(FAILED_LINE)
            return
        if source:
            # 这一屏的对方原话已经拟过了（v2-social-offer-dedup）：同一份材料不再重开。
            # 自动 / 手动两条路在起草前都设了 `_pending_auto_source`，这一格统一落定。
            self._last_draft_source = source
        # 交出去之前先把这一屏锁上（v2-social-copy-clean）：信号是直连的，装配层就在
        # 这一句里把候选亮出来、把复制做完；锁在这儿立着，他那边做完之后不管谁去动
        # 窗口、动剪贴板，都不会再把这一段重开一轮。
        self.mark_done()
        self._bus.social_candidates.emit(candidates)

    def _on_failed(self, exc: Exception) -> None:
        """模型不可用：如实说想不出来，**不给一段编的**。"""
        self._running = False
        # 这一拍没拟成，就不算"这一屏已拟过"（v2-social-listen-dedup）：下一拍照旧能重来，
        # 不然一次断网/超时会把这一屏永久咽掉。
        self._pending_auto_source = ""
        logger.error("起草社交回复失败：%s", exc)
        self._bus.pet_speak.emit(FAILED_LINE)

    # ---------- 建对象攻略卡（v2-social-reply-ext） ----------

    @property
    def awaiting_card(self) -> bool:
        """候选回复列完了，正等用户点"要不要建对象攻略卡"。

        界面拿它决定这排按钮归谁——跟 `awaiting_confirm` 一个口径，只是这一问发生在
        候选之后、建卡之前。
        """
        return self._card_awaiting

    def offer_card(self) -> None:
        """候选回复给出去之后：把"要不要建对象攻略卡"这一问排出来。

        台词这边不自己发（由界面 `_pop_offer` 摆进气泡），这里只管把状态切到
        "等这一问的答复"，并通知界面排按钮。

        **已经有这张卡了就不再问**（v2-social-card-dedup）：那一问的答复是"抽对方信息
        → 建卡"，卡都在了还问一遍、点头之后又建一张同名的，就是真机上"对象攻略卡攒了
        好几张"那条。查重那只手由装配层注入（`set_card_exists_provider`）——**判据在做卡
        那个模块**（`CardService.find_card`），社交这条线自己不存"建过没有"这份记忆，
        两处各存一份迟早对不上（用户就可能是在卡界面里自己建的那一张）。
        """
        if self._card_exists():
            # 这一问根本没排出来，别把上一轮的"正等答复"留在身上（`pending_offer`
            # 拿它当"社交这条线还端着东西吗"的一半判据）。
            self._card_awaiting = False
            logger.debug("「对象攻略卡」已经有一张了，这一问不再排（v2-social-card-dedup）")
            return
        # 同一趟社交前台里只问一次（v2-social-offer-dedup）：装配层每完成一轮候选都喊
        # 一次 `offer_card`，连拟几条 = 连着问几遍"要不要建卡"（真机"连回三条"问三遍）。
        # 问过且人还没走出聊天窗，就不再问；切出去再回来（`on_foreground` 把
        # `_last_offered` 归零）才重新问一次。
        if self._last_offered:
            self._card_awaiting = False
            logger.debug("这一趟社交前台里已经问过建卡了，不再问（v2-social-offer-dedup）")
            return
        self._last_offered = self._clock()
        self._card_awaiting = True
        self._bus.social_card_offer.emit()

    def confirm_card(self, answer: str) -> bool:
        """用户对"要不要建对象攻略卡"点了轻按钮。返回 True = 要建，False = 不用了。

        [不用了] 就只回候选那几段，什么都不多说、不劝。
        """
        self._card_awaiting = False
        if not self.enabled:
            return False
        return self._is_yes(answer)

    @property
    def last_request(self) -> str:
        """最近一次帮忙回复的处境原文（建卡抽对方信息用它当原料）。"""
        return self._last_request

    def extract_profile(self, on_done, on_failed=None) -> None:
        """从最近那次帮忙回复的对话里抽对方信息（兴趣/爱好/约会地点…），异步。

        原料是 `_last_request`（用户自己讲的那段处境），**不读聊天正文**。抽出来的
        只是一段纯文本，交给界面放进一个可编辑框里，用户改完才写进卡专属库（C 混合）。
        """
        text = self._last_request
        if not text:
            on_done("")
            return
        run_async(
            self._extract,
            text,
            on_done=on_done,
            on_failed=on_failed or (lambda exc: logger.error("抽对方信息失败：%s", exc)),
        )

    def _extract(self, text: str) -> str:
        """后台线程里抽：一个 `{"profile": "..."}` 的 JSON。"""
        prompt = SOCIAL_PROFILE_PROMPT.format(text=text)
        try:
            data = self._provider().chat_json(prompt, system=SOCIAL_PROFILE_SYSTEM) or {}
        except LLMOfflineError:
            logger.warning("断网，抽对方信息跳过（本地无此能力）")
            return ""
        if not isinstance(data, dict):
            return ""
        return str(data.get("profile") or "").strip()

    # ---------- 内部 ----------

    def _words(self) -> tuple[str, ...]:
        """词表：配置里给了就用配置的，留空就用内置那张。"""
        words = tuple(self._config.screen.social_words or ())
        return words or DEFAULT_DRAFT_WORDS


__all__ = [
    "ASK_CARD_LINE", "ASK_LINE", "CARD_CATEGORY", "CHAT_CONTEXT_CLIP", "CHAT_TEXT_FRESH_S",
    "DEFAULT_DRAFT_WORDS",
    "EXAMPLES_CLIP", "FAILED_LINE", "GUIDE_CARD_GOAL", "GUIDE_CARD_ICON",
    "GUIDE_CARD_TITLE", "GROUP_HEAD_MAX_CHARS", "GROUP_NOTICE_LINE",
    "LIST_PAGE_MARK_KINDS", "LIST_PAGE_TIME_ROWS", "NON_PERSONAL_PAGES", "NO_CHAT_CONTEXT",
    "NO_EXAMPLES",
    "NO_PEER_LINE_HINT",
    "OFFER_HANG_S", "OFFLINE_CANDIDATES", "PAGE_CHANNEL", "PAGE_FILEHELPER", "PAGE_GROUP",
    "PAGE_HEAD_ROWS", "PAGE_LIST", "PAGE_MINI", "PAGE_PERSONAL", "PENDING_SNAP_MS",
    "SITUATION_WINDOW_S",
    "SOCIAL_CATEGORY",
    "SocialReplyService",
    "chat_page_kind", "classify_chat_page", "is_personal_chat", "is_personal_kind",
    "is_social_proc",
    "looks_like_list_page",
]
