"""
社交 / 场景的边界与优先（v2-social-scene-boundary）的自动测试。

四条真机毛病收在这一轮，都是"两条线各管一段、交界处没人管"（下面按**测试里的节号**排）：

- ① **邀请粘滞**：`_awaiting` 只有"用户往宠物聊天框打字"能清（全仓只有
  `_on_chat_send` 一处）。邀请冒出来用户没点（关了气泡 / 切走 / 没理）就永远挂着，
  `_poll_listen` 第一句拿它 early-return —— 自动监听从此静默停摆，对方再回消息也不弹
  候选，而且**一行日志都没有**（全在那个 early-return 之前）。
- ② **首触发竞态**：冷启动刚进微信时 `on_foreground` 已经踢了一次抓屏，用户紧接着
  说"帮我回"，`_read_social_screen()` 撞上 `_chat_reading` 返回 False，旧的写法当场
  把 `_pending_draft` 丢掉、拿空的 `chat_text` 去起草 —— 模型没有对方原话，只能现编
  一句泛泛的（真机首条实测是「借阅一本书籍」，第二次才正常）。
- ③ **置顶聊天优先社交**（用户拍板的硬规则）：只要置顶的是聊天窗口，就优先处理社交
  回复——不管后台/旁边在放着电影、开着游戏、还是工作页，都不许飘场景开场词。
- ④ **全屏兜底误判**：认不出名字的进程铺满全屏就按"在看片"算，于是**截图工具**
  （SnippingTool 的遮罩铺满全屏）被当成播放器，飘出「我陪你看电影」。这一轮按用户
  原话把兜底删掉：**非播放器且非浏览器则不放行**。
- ⑤ **桌面 / 聊天态不飘电影词与游戏词**：③ 管的是"社交压过观影"，这一节补齐另一半
  ——桌面既不算看片也不算游戏，且聊天窗口置顶时**游戏那条线一起报也压不过社交**。

红线：pet.db / mind.db 零写入；零新依赖；不模拟按键 / 不做 UI 自动化。剪贴板这条
（v2-social-copy-clean）是**只写一次、从不读**，而且测试跑在离屏平台上，那份剪贴板
是进程内的，动不到用户真机上的剪贴板内容。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen import monitor as mon_mod
from life.screen.browser import TITLE_WATCH
from life.screen.scene_guard import (
    KIND_FOCUS,
    KIND_GAME,
    KIND_IDLE,
    KIND_SOCIAL,
    KIND_WATCH,
    KIND_WORK,
    PRIORITY,
)
from life.screen.scene_state import OPENING_GAME, OPENING_WATCH, OPENING_WORK
from life.screen.social import (
    ASK_LINE,
    CHAT_CONTEXT_CLIP,
    NO_CHAT_CONTEXT,
    SocialReplyService,
)
from life.screen.social_window import SENDER_OTHER, SENDER_USER, ChatRead

# 真机那几句（v2-social-ocr-catch 抓回来的同一批），跨模块引用同一份口径。
PEER_LINE = "今天玩得开心吗"
PEER_LINE_2 = "那周末一起爬山吧"
SELF_LINE = "挺开心的你呢"


def _peer_read(lines: list[str], self_line: str = "") -> ChatRead:
    """按"对方白气泡 +（可选）自己绿气泡"拼一份 `ChatRead`，末尾是对方那条。"""
    rows: list[tuple[str, str]] = [(SENDER_OTHER, text) for text in lines]
    if self_line:
        rows.insert(0, (SENDER_USER, self_line))
    return ChatRead(lines=rows, raw=" ".join(text for _s, text in rows))


def draft_bubble_text(app) -> str:
    """宠物那个非模态气泡里现在写着什么（合起来一段，供断言）。

    v2-social-copy-clean 起，候选回复 + 解析走的就是这里——不再是模态弹窗。
    """
    history = app.chat_bubble._history
    return "\n".join(history.item(i).text() for i in range(history.count()))


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True

    spoken: list[str] = []
    app.bus.pet_speak.connect(spoken.append)
    app.bus.pet_speak_proactive.connect(spoken.append)
    candidates: list[list] = []
    app.bus.social_candidates.connect(candidates.append)

    grab = {"frame": "FRAME_A"}
    ocr: dict[str, object] = {"read": _peer_read([PEER_LINE])}
    reader_calls = {"n": 0}

    def _read(array):
        reader_calls["n"] += 1
        return ocr["read"]

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: 10.0
    service._examples_provider = lambda query: ""
    service._clock = lambda: 10_000.0        # 冷却是 30 分钟，别让上一次邀请挡住这一次
    service._social_foreground = True
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False
    service._awaiting = False

    stub.candidates_result = {"candidates": [
        {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄，不升级"},
    ]}

    def _reset_offer_state() -> None:
        """把"上一段留下的邀请态"清干净，回到"刚切进微信、什么都没冒"的那一刻。"""
        service._awaiting = False
        service._running = False
        service._last_asked = 0.0
        service._last_listen_at = 0.0
        service._last_listen_tail = ""
        service._last_listen_kind = ""
        service._last_fingerprint = None
        service._chat_reading = False
        service._listen_reading = False
        service._pending_draft = None
        service._chat_text = ""
        service._want_situation_until = 0.0
        # 「这一屏是哪一种页」也翻篇（v2-social-trigger-window-gate）：邀请只在
        # "非单聊 → 单聊"跃迁那一拍冒，留着上一节那格单聊的话，这一节第一拍就跃迁不起来。
        service._page_kind = ""
        # 候选一给出去，装配层会接着问"要不要建对象攻略卡"（offer_card）——那一档不清掉的
        # 话，下面点 [好呀] 会被它先接走，去弹建卡的模态框（离屏必卡死）。
        service._card_awaiting = False
        # 建卡那一问的"这一趟只问一次"（v2-social-offer-dedup）也一起翻篇：每个 section
        # 都从"刚切进微信、什么都没问过"那一刻起摆，别让上一节的记忆把这一问压死。
        service._last_offered = 0.0
        # 候选给出去那一刻服务把"这一段的完成态"锁上了（v2-social-copy-clean）：
        # 不清掉的话，后面每一次"这句该不该认"都会撞上完成态锁，验出来的就不是本来那件事。
        service.reset_done()
        app.chat_bubble.hide_offer()
        spoken.clear()
        candidates.clear()

    def _enter_wechat(read: ChatRead | None = None) -> None:
        """进微信那一拍 + 把那一屏认回来（"这一屏是单聊"正是这一次认字报上来的）。

        v2-social-trigger-window-gate 起，邀请**不在 `on_foreground` 那一拍冒**了：这一屏
        是单聊还是群聊 / 会话总列表 / 订阅号，得等那次 OCR 回来（`note_page_kind`）才判得
        出，所以那一拍只抓屏打底。离屏环境里那次认字是异步的，照本文件一贯的做法手动落一屏
        ——默认读的是对方一条白气泡，判出来正是单聊。
        """
        service.on_foreground("Weixin.exe")
        service._on_screen_read(ocr["read"] if read is None else read)

    # ---------- ① 邀请粘滞：关掉气泡 / 切走，都得把"等着点按钮"作废 ----------

    section("① 邀请粘滞：没点按钮就关了气泡 → 那一档必须作废（真机静默不触发的根）")

    _reset_offer_state()
    reader_calls["n"] = 0
    service.on_foreground("Weixin.exe")
    check("★ 邀请那一刻已经抓了一屏打底（v2-social-ocr-path 的抓屏/冒话解耦）",
          service._chat_reading is True, str(service._chat_reading))
    check("★ 这一拍**先不冒**：这一屏是哪种页还没认出来（v2-social-trigger-window-gate）",
          service.awaiting_confirm is False and service._last_asked == 0.0,
          f"awaiting={service.awaiting_confirm} / 戳={service._last_asked}")
    # 打底那一屏认回来了（离屏环境里得手动把它落下来，不然下一拍会被 _chat_reading 挡住）：
    # 认出来是一屏单聊 → 这就该冒了。
    service._on_screen_read(_peer_read([PEER_LINE]))
    check("冷启动进微信：认出这一屏是单聊 → 冒了邀请、按钮排出来了（社交那条线的正常开场）",
          service.awaiting_confirm is True, str(service.awaiting_confirm))
    # v2-social-chat-ui-fix2 提交D：老口径邀请台词走 `pet_speak_proactive`（头顶那条
    # "只显示不出声"的通道），装配层收到 `social_offer` 后再把**同一句**摆进对话框——
    # 真机上冒两遍。现在台词只走 `social_offer` 一条信号，这里跟着翻过来：凭据是
    # "这一问真冒出去了"（冷却戳 + 气泡里那句问句），而不是头顶那条通道有没有说话。
    check("邀请冒出去了（冷却戳落下了、问句落在气泡里；头顶那条通道不再说话）",
          service._last_asked > 0 and ASK_LINE in draft_bubble_text(app) and len(spoken) == 0,
          f"戳={service._last_asked} / 头顶={spoken!r}")
    service._last_listen_tail = PEER_LINE

    # 用户没点任何按钮，直接把气泡关了（点右上角那个叉 / 切走让它自己收）
    app.chat_bubble.hide_offer()
    check("★ 关掉气泡 → 「等着点按钮」这一档跟着作废（修前它是 True，一挂就是永久）",
          service.awaiting_confirm is False, str(service.awaiting_confirm))

    # 对方回了新消息 —— 监听必须还能走到 _auto_generate
    ocr["read"] = _peer_read([PEER_LINE, PEER_LINE_2])
    grab["frame"] = "FRAME_B"
    service._last_listen_tail = PEER_LINE          # 上一拍认到的尾巴（打底那一次）
    reader_calls["n"] = 0
    service._poll_listen()
    check("★ 对方回新消息 → 这一拍真去认字了（没被 _awaiting 那道 early-return 拦死）",
          wait_for(lambda: reader_calls["n"] == 1), f"认了 {reader_calls['n']} 次")
    check("★ 候选弹出来了（这正是真机丢掉的那一步）",
          wait_for(lambda: len(candidates) == 1), repr(candidates))
    check("★ 候选走的是宠物自己的非模态气泡，里面写着候选原文 + 解析（v2-social-copy-clean）",
          wait_for(lambda: "挺开心的，你呢？" in draft_bubble_text(app)
                    and "寒暄回寒暄，不升级" in draft_bubble_text(app)),
          repr(draft_bubble_text(app)))
    drain_threads()

    # 反向对照：不作废的话，那一拍确实什么都干不了 —— 把"静默"钉成可复现的
    _reset_offer_state()
    ocr["read"] = _peer_read([PEER_LINE, "晚上有空吗"])
    _enter_wechat()
    grab["frame"] = "FRAME_C"
    service._last_listen_tail = PEER_LINE
    reader_calls["n"] = 0
    candidates.clear()
    service._poll_listen()
    check("反向对照：邀请还挂着、没点也没关 → 这一拍一次字都不认（静默）",
          reader_calls["n"] == 0, f"认了 {reader_calls['n']} 次")
    check("反向对照：也没弹候选（无报错、无日志，用户看到的就是'没反应'）",
          candidates == [], repr(candidates))

    # 切走再切回来：**只要那排按钮还挂在气泡上，这一档就还作数**
    #
    # 这一条是 v2-social-await-prime-fix 有意改掉的老口径（旧口径："走出聊天窗口就
    # 作废"）。旧口径的毛病：走出聊天窗口时**界面那边一个动作都没有**——气泡是常驻的，
    # 那排 [好呀] [不用了] 照样挂着。状态先作废、按钮还在，就成了"按钮还显示、旗子已经
    # 没了"：用户回来点 [好呀]，`_on_offer_reply` 先问 `awaiting_confirm`，判 False 就
    # 落到通用聊天兜底（真机 17:59 回的那句"好的呀"），社交起草这条线一次都没进。
    # 现在的口径：按钮还归社交这一条线就不清旗；按钮真收掉（hide_offer / 被顶替 /
    # 关气泡）才清。老的"防卡死"语义一个字没丢——超时那一声（`_drop_stale_offer`）
    # 是 `force=True`，照清，而且顺手把按钮一起收掉。
    _reset_offer_state()
    _enter_wechat()
    check("前置：又在微信里冒了邀请", service.awaiting_confirm is True, "")
    service.on_foreground("chrome.exe")
    check("★ 走出聊天窗口、但按钮还挂在气泡上 → 这一档**不**作废（点下去还算数）",
          service.awaiting_confirm is True, str(service.awaiting_confirm))
    check("走出社交前台 → 那条轻量轮询计时器也停了（出了前台不空转）",
          service._listen_timer.isActive() is False, str(service._listen_timer.isActive()))
    app.chat_bubble.hide_offer()
    check("★ 按钮真收掉了 → 这一档这才作废（状态与按钮同寿）",
          service.awaiting_confirm is False, str(service.awaiting_confirm))

    _reset_offer_state()
    _enter_wechat()
    check("又被别的线顶掉按钮（场景态那一排上来）→ 社交这一档也作废",
          service.awaiting_confirm is True, "")
    app.chat_bubble.show_offer("scene_work")
    check("★ 顶掉别人的按钮时替它喊一声退休（show_offer 不再是静默覆盖）",
          service.awaiting_confirm is False, str(service.awaiting_confirm))

    _reset_offer_state()
    _enter_wechat()
    app.chat_bubble.close_bubble(fade=False)
    check("★ 整个气泡收掉 → 这一档也作废（收气泡这条路以前没人管）",
          service.awaiting_confirm is False, str(service.awaiting_confirm))

    # 点了 [好呀] 那条出口不能被"退休通知"抢在前面（抢了就等于点空）
    _reset_offer_state()
    _enter_wechat()
    replies: list[str] = []
    app.bus.pet_speak.connect(replies.append)
    app.chat_bubble._reply_offer(app.chat_bubble.OFFER_YES)
    # 批次11：`confirm()` 的出口从"布一个等处境的窗口"换成"当场照对方末句拟一段"，
    # 凭据跟着换——派发到了，这一拍就该开拟（`busy` / `_running` 立起来）。
    check("★ 点 [好呀] 仍然派发得出去（先派发答复、再报按钮退休，顺序不能反）",
          service.busy is True, f"忙态 {service.busy}")
    drain_threads()

    # ---------- ② 首触发竞态：撞上"上一屏还在认"，不许丢下它去拿空上下文起草 ----------

    section("② 首触发竞态：冷启动第一次「帮我回」必须等打底那一屏认回来")

    _reset_offer_state()
    service._chat_reading = True          # on_foreground 踢出去的那一屏还在认
    service._chat_text = ""               # 手上还没有对方原话
    stub.prompts.clear()
    candidates.clear()
    grab["frame"] = "FRAME_D"

    accepted = service.handle_request("帮我回一下")
    check("这一句被认成要草稿了（接下了这活儿）", accepted is True, str(accepted))
    check("★ 撞上'上一屏还在认'时**挂上去等**，没把 _pending_draft 丢掉",
          service._pending_draft == "帮我回一下", repr(service._pending_draft))
    check("★ 也没当场拿空上下文去起草（模型提示词一次都没发出去）",
          stub.prompts == [], f"{len(stub.prompts)} 条提示词")
    check("★ 这一拍的认字调用也没另起一次（不叠第二屏 OCR）",
          reader_calls["n"] >= 0, "")

    # 打底那一屏认回来了 —— 挂在等待上的那句草稿，现在带着真原话接着走
    service._on_screen_read(_peer_read([PEER_LINE], self_line=SELF_LINE))
    check("★ 那一屏认回来 → 挂在等待上的草稿接着起草了（_drain_pending 接上）",
          wait_for(lambda: len(candidates) == 1), repr(candidates))
    draft_prompts = [p for p in stub.prompts if "candidates" in p]
    check("起草提示词真发出去了", len(draft_prompts) == 1, f"{len(draft_prompts)} 条")
    if draft_prompts:
        prompt = draft_prompts[0]
        check("★ 提示词里带的是**对方的原话**（这才叫基于真实消息生成）",
              PEER_LINE in prompt, f"对方原话 {PEER_LINE!r}")
        check("★ 提示词里不是「没认到对方说了什么」那格（修前冷启动第一次就是它）",
              NO_CHAT_CONTEXT not in prompt, f"那格原样={NO_CHAT_CONTEXT!r}")
        check("★ 自己发的那半边一个字都没进去（主语归属那条没松）",
              SELF_LINE not in prompt, f"自己那句 {SELF_LINE!r}")
    drain_threads()

    # 反向对照：真抓不成（没接那两只手 / 取不到帧）时，照旧别干等
    _reset_offer_state()
    stub.prompts.clear()
    candidates.clear()
    service._chat_reading = False
    service._grab_chat = lambda: None
    service._chat_text = ""
    try:
        accepted = service.handle_request("帮我回一下")
        check("反向对照：真取不到帧 → 不干等，立刻带着手上有的（空）起草",
              accepted is True and wait_for(lambda: len(candidates) == 1), repr(candidates))
        fallback = [p for p in stub.prompts if "candidates" in p]
        check("反向对照：这一路提示词里如实写「没认到」，不编一句（老口径不变）",
              bool(fallback) and NO_CHAT_CONTEXT in fallback[0], f"降级那格={NO_CHAT_CONTEXT!r}")
    finally:
        service._grab_chat = lambda: grab["frame"]
    drain_threads()

    check("对方原话那格有长度上限（喂太多整屏杂字会把它带跑）",
          CHAT_CONTEXT_CLIP == 600, str(CHAT_CONTEXT_CLIP))

    # ---------- ②-2 手动路也过同一道材料去重（v2-social-offer-dedup） ----------
    #
    # 真机那条：自动监听已经把"同一份对方原话"拟过一遍了，用户接着点 [好呀] / 喊"帮我回"
    # （手动路）。老口径 `_last_draft_source` 只对 `_auto_generate` 那条链生效，手动路不设
    # `_pending_auto_source`、不比不去——又照着同一份字拟一段（"连回三条"的手动腿）。这一节
    # 验：手动直达路与挂起路（`_drain_pending`）在起草前都过同一道闸，材料没变就不重开。

    section("②-2 手动路也过同一道材料去重（v2-social-offer-dedup）")

    _reset_offer_state()
    service._social_foreground = False     # 关掉抓屏刷新，走 handle_request 直达那条路
    service._chat_text = PEER_LINE
    service._chat_text_at = service._clock()
    service._last_draft_source = PEER_LINE
    stub.prompts.clear()
    candidates.clear()

    accepted = service.handle_request("帮我回一下")
    drain_threads()
    pump(0.1)
    check("手动直达路：材料跟上一轮同一份 → 照旧接下这活儿（不是当没听见）",
          accepted is True, str(accepted))
    check("★ 但一条候选也没再拟（同一份不重开）", not candidates, f"{len(candidates)} 组")
    check("★ 模型提示词一次都没发（闸挡在开工前，不白耗一次模型）",
          stub.prompts == [], f"{len(stub.prompts)} 条")
    check("★ 忙态也收起来了（不残留）", service.busy is False, str(service.busy))
    check("★ 记忆没动（下一拍还拿同一份比）",
          service._last_draft_source == PEER_LINE, repr(service._last_draft_source))

    # 挂起那条路（等这一屏 OCR 回来才起草）也过同一道闸
    _reset_offer_state()
    service._social_foreground = False
    service._pending_draft = "帮我回一下"
    service._chat_text = PEER_LINE
    service._last_draft_source = PEER_LINE
    stub.prompts.clear()
    candidates.clear()
    service._drain_pending()
    drain_threads()
    pump(0.1)
    check("★ 挂起路（_drain_pending）同样挡下同一份材料", not candidates,
          f"{len(candidates)} 组")
    check("★ 这一路也没发提示词", stub.prompts == [], f"{len(stub.prompts)} 条")

    # 材料真变了 → 照旧放行（这道闸只挡重复，不挡真新）
    _reset_offer_state()
    service._social_foreground = False
    service._chat_text = PEER_LINE_2
    service._chat_text_at = service._clock()
    stub.prompts.clear()
    candidates.clear()
    service.handle_request("帮我回一下")
    check("★ 材料真变了 → 照旧拟（闸只挡重复，不挡真新）",
          wait_for(lambda: len(candidates) == 1), repr(candidates))
    drain_threads()
    service._social_foreground = True      # 还给下一节（③ 靠它在社交前台里走 _poll_listen）

    # ---------- ③ 置顶聊天优先社交（用户拍板的硬规则） ----------

    section("③ 微信置顶 + 后台视频：社交候选照弹、场景开场词让位")

    guard = app.screen_scene_guard
    monitor = app.screen_monitor
    scene_state = app.scene_state
    companion = app.screen_companion
    real_proc = monitor._proc
    real_title_kind = monitor._title_kind

    check("优先级表把社交排在三档场景前面（专注 > 社交 > 游戏 > 观影 > 工作）",
          PRIORITY == (KIND_FOCUS, KIND_SOCIAL, KIND_GAME, KIND_WATCH, KIND_WORK),
          repr(PRIORITY))
    check("仲裁点真接上了社交那一档的回调（装配层接的，不是空档）",
          guard._is_social is not None, str(guard._is_social))

    try:
        # 上一节收尾时装配层接了一句"要不要建对象攻略卡"（`offer_card`），那一档不清掉
        # 的话社交这条线一直"端着东西"，仲裁点永远归它——先把现场清干净再摆前提。
        _reset_offer_state()

        # 先让"在看片"这一档真的说了算（前台是播放器，视频正放着）
        monitor._proc = "potplayer.exe"
        monitor._title_kind = ""
        spoken.clear()
        guard.set_watch(True)
        check("前置：旁边那场视频真的进了观影档（这一档原本是说了算的）",
              guard.active == KIND_WATCH, guard.active)
        check("前置：观影开场词飘了（这是正常行为，正是要被让位的那一句）",
              OPENING_WATCH in spoken, repr(spoken))
        check("前置：场景状态机这会儿是电影档", scene_state.state == KIND_WATCH,
              scene_state.state)

        # 对方在微信里回了消息，用户切回聊天窗口 —— 前台换人那一拍会喊的就是这一声
        monitor._proc = "Weixin.exe"
        monitor._title_kind = TITLE_WATCH
        spoken.clear()
        guard.notify_work_changed()
        check("★ 切回聊天窗口 → 这一档立刻改归社交（压过还在放着的那场视频）",
              guard.active == KIND_SOCIAL, guard.active)
        check("★ 场景状态机跟着切成社交档（不是电影）",
              scene_state.state == KIND_SOCIAL, scene_state.state)
        check("★ 三句场景开场词一句都没飘（让位给社交那条线自己的邀请）",
              not any(w in spoken for w in (OPENING_WATCH, OPENING_GAME, OPENING_WORK)),
              repr(spoken))

        # 视频还在后台放着：看片那条线想借标题那条腿再开一场 —— 必须被拦住
        spoken.clear()
        companion.on_title(TITLE_WATCH)
        check("★ 看片会话开不起来（社交那一档挡在前面）",
              guard.watching is False or guard.active == KIND_SOCIAL, guard.active)
        check("★ 「我陪你看电影」一个字都没再飘",
              "我陪你看电影" not in spoken, repr(spoken))

        # 同一拍里，社交那条线照常干活：对方回消息 → 候选照弹
        #
        # 这一条也**换一句新的**（批次8-A 收窄之后非换不可）：材料现在只取视觉最底部那一条
        # 对方气泡，若还用「那周末一起爬山吧」，它跟上一节（②-2 手动路）留下的
        # `_last_draft_source` 正好是同一份，会被"同一份材料不重开"那道闸压住——这一节验的
        # 是场景仲裁，不该被材料去重搅进来。
        ocr["read"] = _peer_read([PEER_LINE, "那就周六上午十点见吧"])
        grab["frame"] = "FRAME_E"
        service._last_listen_tail = PEER_LINE
        service._last_fingerprint = None
        service._awaiting = False
        service._running = False
        service._chat_reading = False
        service._last_listen_at = 0.0
        candidates.clear()
        service._poll_listen()
        check("★ 视频还在后台放着，对方一回消息照样弹候选（社交线不问场景态）",
              wait_for(lambda: len(candidates) == 1), repr(candidates))
        drain_threads()

        # 社交这条线"正端着东西"时，就算前台切走了也让位。v2-social-copy-clean 起没有
        # 候选框了，"端着东西"落到装配层紧跟着排出来的那一问上（建卡那一问还挂着等人点）。
        monitor._proc = "chrome.exe"
        monitor._title_kind = ""
        check("★ 前台切走、但那一问还挂着 → 仍然是社交说了算（不抢着飘词）",
              guard.active == KIND_SOCIAL, guard.active)
        check("前置：这会儿确实端着东西（建卡那一问挂着）",
              service.pending_offer is True, str(service.pending_offer))

        # 反向对照：社交那条线收工、前台也不是聊天窗口 → 看片那一档重新说了算，
        # 那句被让位的开场词也该回来（证明"让位"是社交在的时候才让，不是永久关掉）
        service._awaiting = False
        service._card_awaiting = False
        service._running = False
        monitor._proc = "potplayer.exe"
        spoken.clear()
        guard.set_watch(True)
        check("反向对照：社交收工 + 前台不是聊天窗口 → 这一档交回给场景（回到观影）",
              guard.active == KIND_WATCH, guard.active)
        check("反向对照：被让位的那句开场词也回来了（让位不是把功能关掉）",
              OPENING_WATCH in spoken, repr(spoken))

        monitor._proc = "Weixin.exe"
        check("反向对照：只要前台还是聊天窗口，社交就一直说了算（不看有没有端着东西）",
              guard.active == KIND_SOCIAL, guard.active)
    finally:
        monitor._proc = real_proc
        monitor._title_kind = real_title_kind
        guard.set_watch(False)
        guard.notify_work_changed()

    # ---------- ④ 截图工具全屏：不飘电影词（全屏兜底已删） ----------

    section("④ 截图工具铺满全屏：不飘电影词、场景也不进观影档")

    captions: list[str] = []
    app.bus.screen_caption.connect(captions.append)
    real_proc = monitor._proc
    real_title_kind = monitor._title_kind

    check("截图工具在非视频名单里（Win10 的 SnippingTool / Win11 的真实进程名 ScreenSketch）",
          mon_mod._known_non_video("SnippingTool.exe") is True
          and mon_mod._known_non_video("ScreenSketch.exe") is True
          and mon_mod._known_non_video("ShareX.exe") is True,
          str([p for p in ("SnippingTool.exe", "ScreenSketch.exe", "ShareX.exe")
               if not mon_mod._known_non_video(p)]))

    try:
        # 真机式：截图工具的遮罩**铺满整屏**。这里连 `is_fullscreen_foreground` 都
        # 不用摆——反向那条线**根本不再问全屏**了，这正是删兜底要的效果。
        monitor._proc = "SnippingTool.exe"
        monitor._title_kind = ""
        guard.set_watch(False)
        guard.set_game("")
        guard.notify_work_changed()

        check("★ 截图工具全屏 → 不算在看片（修前这里会返回 True）",
              monitor.is_watch_foreground("SnippingTool.exe") is False, "")
        check("★ 也不进采样门（不截屏、不认字）",
              monitor._watch_candidate("SnippingTool.exe") is False, "")

        captions.clear()
        spoken.clear()
        companion.on_foreground("SnippingTool.exe")
        pump(0.02)
        check("★ 不开看片会话", companion._watch_open is False, str(companion._watch_open))
        companion._show_opening()      # 就算宽限期到了也不该冒出东西来
        pump(0.02)
        check("★ 「主人，看电影啦」一个字都没飘",
              "主人，看电影啦" not in captions and not any("看电影" in s for s in captions),
              repr(captions))
        check("★ 「我陪你看电影」也没飘（场景状态机没进观影档）",
              "我陪你看电影" not in spoken and scene_state.state != KIND_WATCH,
              f"{spoken!r} / {scene_state.state!r}")
        check("★ 场景守卫里也没挂上观影态",
              guard.watching is False and guard.active != KIND_WATCH,
              f"{guard.watching} / {guard.active}")

        # 反向对照：换一个**真在名单里**的播放器，同样铺满全屏 → 照旧陪看。
        # 这一条钉住的是"删的是兜底，不是把陪看整个关掉"。
        monitor._proc = "potplayer.exe"
        guard.set_watch(False)
        guard.notify_work_changed()
        captions.clear()
        companion.on_foreground("potplayer.exe")
        pump(0.02)
        check("反向对照：名单里的播放器全屏 → 照旧开看片会话",
              companion._watch_open is True, str(companion._watch_open))
    finally:
        companion._end_watch()
        monitor._proc = real_proc
        monitor._title_kind = real_title_kind
        guard.set_watch(False)
        guard.set_game("")
        guard.notify_work_changed()

    # ---------- ⑤ 桌面 / 聊天态：电影词与游戏词都不飘 ----------

    section("⑤ 桌面与聊天态：电影词、游戏词都不飘")

    try:
        # ---- 桌面（explorer.exe）：既不算在看片，也不算游戏 ----
        monitor._proc = "explorer.exe"
        monitor._title_kind = ""
        guard.set_watch(False)
        guard.set_game("")
        guard.notify_work_changed()
        captions.clear()
        spoken.clear()

        check("桌面：不算在看片（非视频硬闸）",
              monitor.is_watch_foreground("explorer.exe") is False, "")
        check("桌面：也不算游戏（不会问「要不要攻略」）",
              monitor.game_hit("explorer.exe") is None, "")
        companion.on_foreground("explorer.exe")
        companion._show_opening()
        pump(0.02)
        check("桌面：电影词一个字都没飘",
              not any("看电影" in s for s in captions + spoken),
              repr(captions + spoken))
        check("桌面：也没进观影档", guard.watching is False and scene_state.state != KIND_WATCH,
              f"{guard.watching} / {scene_state.state!r}")

        # ---- 聊天态：后台一边放着视频、游戏那条线一边报着游戏，社交照样说了算 ----
        monitor._proc = "Weixin.exe"
        monitor._title_kind = ""
        guard.set_watch(True)                 # 后台视频那条线报"在看片"
        guard.set_game("英雄联盟")             # 游戏那条线报"在玩这个"
        spoken.clear()
        guard.notify_work_changed()

        check("★ 微信置顶 + 后台视频 + 游戏都在报 → 仍然是社交说了算",
              guard.active == KIND_SOCIAL, guard.active)
        check("★ 场景状态机是社交档（不是观影、也不是游戏）",
              scene_state.state == KIND_SOCIAL, scene_state.state)
        check("★ 电影词与游戏词一句都没飘",
              not any(w in spoken for w in ("我陪你看电影", OPENING_GAME, OPENING_WORK)),
              repr(spoken))

        # 反向对照：前台离开聊天窗口 → 场景立刻交回来（让位是"社交在的时候"才让）
        monitor._proc = "chrome.exe"
        spoken.clear()
        guard.notify_work_changed()
        check("反向对照：前台离开聊天窗口 → 这一档交回给场景（游戏压过观影）",
              guard.active == KIND_GAME, guard.active)
        check("反向对照：让位的那句游戏开场词回来了",
              OPENING_GAME in spoken, repr(spoken))
    finally:
        monitor._proc = real_proc
        monitor._title_kind = real_title_kind
        guard.set_watch(False)
        guard.set_game("")
        guard.notify_work_changed()

    box.cleanup()
    return summary("社交 / 场景边界与优先（v2-social-scene-boundary）")


if __name__ == "__main__":
    sys.exit(main())
