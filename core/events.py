"""
事件总线：各层之间只通过它通信，避免互相直接引用。

实现为 QObject 信号，因此在后台线程（LLM 调用、定时任务）里 emit 时，
Qt 会自动排队投递到主线程执行，界面更新天然安全。
"""

from __future__ import annotations

from PySide6.QtCore import QObject, Signal


class EventBus(QObject):
    # 宠物说话（text）。桌宠气泡、面板对话区都会监听
    pet_speak = Signal(str)
    # 宠物**自己**开的口（空闲主动搭话 / 记忆碎片 / L4 主动分享），参数同上。
    # 跟 pet_speak 分开是为了在出口处分得开"它想说"和"回用户的话"：前者先冒话、
    # 要不要出声再问用户一句，后者照常出声（v2-proactive-voice）。
    pet_speak_proactive = Signal(str)
    # 屏幕陪伴的字幕（v2-speech-polish ④）：认出在看电影时冒一句陪伴字幕。
    # **跟上面两条都不是一回事**：它只显示、**绝不出声**，而且停几秒就自己散
    # （6 秒）。做成独立信号而不是复用 pet_speak，是因为 pet_speak 那条线在
    # "看电影"的时候靠 monitor.scene 判静音——**开场那一刻还没有任何场景成立**，
    # 判据是空的，复用等于开场字幕一定会念出来。要"不出声"就得走一条结构上
    # 就没有声音的通道，不能指望静音闸兜。
    screen_caption = Signal(str)
    # 吐嘈已入库，界面刷新记忆区
    memory_changed = Signal()
    # 提醒有变化（新建/触发/取消），界面刷新提醒区
    reminder_changed = Signal()
    # 提醒到点，参数为 reminder_id
    reminder_due = Signal(int)
    # 宠物弹到屏幕最前（不改变窗口显示状态，仅置顶聚焦）
    summon_pet = Signal()
    # 番茄钟阶段变化（专注/休息/空闲），界面刷新用
    pomodoro_phase_changed = Signal(str)
    # 番茄钟统计有更新（完成/跳过/中止了一个阶段），界面刷新统计用
    pomodoro_stats_changed = Signal()
    # 面板内的一次性提示（错误、成功等）
    toast = Signal(str)
    # 今日活动报告（排行 + 点评）：单独一条信号，因为它需要在气泡里多停一会儿
    activity_report = Signal(str)
    # 用户在聊天里让宠物做的事（activity_report / hide_pet），由界面层执行
    user_command = Signal(str)
    # 用户在聊天里**明说**要专心（"我要专注写代码"），请开一轮番茄钟（v2-chat-intent ②）。
    # 单独一条无参信号、不带文本：开不开得成由计时器自己判（已经在专注里就 __enter__ 不了），
    # 聊天层不该知道番茄钟有没有开起来。这条只走**屏幕计时器**那条内存里的线，
    # 不碰 `PomodoroTimer.start_focus`——那条会往 pet.db 落一行会话记录（红线）。
    focus_requested = Signal()
    # 陪玩模式开关变化（True 已开启），陪玩页刷新状态用
    playmate_changed = Signal(bool)
    # 一条吐嘈解析完了（原文、话题归类、主题族、情绪强度、说话的角色卡 id、这次要不要出建卡邀请）：
    # 卡牌服务据此判定该不该提议建卡。may_invite=False 表示这一场主动服务已经动过手了（指令 26-B），
    # 这次只记观察、不冒第二句要用户答的话
    utterance_analyzed = Signal(str, str, str, float, str, bool)
    # **对方（聊天对象）刚说了句什么**（这一场的分格键 `sid|peer`、对象名、对方那句话的
    # 尾巴）：语义上跟上面那条是镜像——那条是「主人说了什么」，这条是「**对方**说了什么」。
    # 帮聊那条线（`SocialReplyService`）每认到一屏对方的话就发一声，对象视角提炼器
    # （`life/mind/peer_distill.py`）据此攒着，聊完一轮再统一抽成「这个人是谁」。
    # 跟 `utterance_analyzed` 一样，**发的是原文**，收的那一头自己决定落什么——提炼器只把
    # 抽出来的结论落库，原话一个字都不写（见那边的模块注释）。
    peer_utterance = Signal(str, str, str)

    # 卡牌服务提议建一张卡（品类、建议卡名）：会话层切到"等用户点头"
    card_proposed = Signal(str, str)
    # 用户在对话流里点了「不用了」（品类）：卡牌服务记下这次拒绝，限频期间不再提
    card_offer_declined = Signal(str)
    # 主动服务（指令 26-B）：出了提议（work）露轻按钮 / 直接弹这张卡 / 点头后开工作模块。
    # 弹卡那条带上用户刚说的那句话：场景建议（走最快的路线）得照着它来说。
    service_offer = Signal(str)
    service_show_card = Signal(int, str)
    service_open_work = Signal(str)
    # 卡牌有变化（新建 / 打卡 / 改状态或目标）：生活广场与卡牌详情刷新
    card_changed = Signal()
    # 某张卡上的 AI 回话（卡牌 id、内容）：卡牌详情页解锁"在想…"（话由宠物本体说）
    card_reply = Signal(int, str)
    # 某张卡上半框的专业建议生成好了（卡牌 id、建议正文）：卡牌详情页填进建议框。
    # 跟 card_reply 分开，是因为它只进建议框、不念出来（指令 27）。
    card_advice = Signal(int, str)
    # 工作卡一：某一行算完一条结果了（行号、结果对象）：工作卡片页刷新那一行
    work_alternatives = Signal(int, object)
    # 工作卡二：一次预测完成（结果对象）：工作卡片页刷新结果区
    work_trends = Signal(object)
    # 宠物对刚出的工作结果点评一句（卡片 key、点评文本）：追加到结果末尾，可关闭
    work_comment = Signal(str, str)
    # 底层流程被管理员改了（指令 16 第二步）：流程视图与部门按钮重建
    work_flow_changed = Signal()
    # 员工自建卡有变化（新建 / 编辑 / 删除，指令 21）：部门卡与卡位列表刷新
    work_cards_changed = Signal()

    # 抓屏笔记（C 多模态 · 笔记先行版）：攒了 N 条笔记，问用户要不要发（数量）。
    # 只是问一句，用户没点头之前什么都不推——红线：发前必须确认。
    notes_offer = Signal(int)
    # 用户点头了：把笔记正文交给界面（气泡里念/显示给用户）
    notes_delivered = Signal(str)

    # 游戏攻略（v2-game-guide）：前台认出游戏了，问一句"要不要攻略"（游戏名）。
    # 只是问一句——用户没点头之前一个字都不往外发。跟 notes_offer 同款轻按钮。
    game_offer = Signal(str)
    # 攻略找好了（正文）：界面把它显示在气泡里。找不到时这里给的是"没找着"的交代，
    # 不会是空串、也不会是编的。
    game_guide = Signal(str)

    # 社交帮忙回复（v2-social-reply-ext）：用户在聊天里说"帮我回一下…"，候选回复写好了
    # （`[{"draft","why"}]`）。界面把它们列出来给人挑着复制——这条线**没有**发送口子，
    # 发不发由用户自己定。每条附一句解析（why：语气/策略），候选只给 1 条。
    social_candidates = Signal(object)

    # 候选回复列完之后，问一句"要不要建对象攻略卡"，该把那排 [好呀] [不用了] 露出来
    # （v2-social-reply-ext）。跟 social_offer 同款轻按钮，只是这一问发生在候选之后。
    social_card_offer = Signal()

    # 场景态（v2-scene-state）：工作态里宠物问了一句"我可以帮你整理笔记，需要吗？"，
    # 该把那排 [好呀] [不用了] 露出来了（参数是那一档，`scene_guard.KIND_*`）。
    # 跟 notes_offer / game_offer / social_offer 同款轻按钮；台词由场景状态机自己发，
    # 这条只管排按钮（同 social_offer 的分工，免得同一句话被说两遍）。
    scene_offer = Signal(str)

    # 社交帮忙回复（v2-social-flow）：社交软件在前台，宠物冒了一句"要我帮你措辞吗"，
    # 该把那排 [好呀] [不用了] 露出来了（参数是前台进程名，界面只用来记一笔日志）。
    # 跟 notes_offer / game_offer 同款轻按钮——分手之前功能是断的：只冒话，没处点。
    social_offer = Signal(str)

    # 社交帮忙回复（v2-social-await-prime-fix 改动一）：那句邀请挂过 `OFFER_HANG_S`
    # 没人应，服务自己把它过期了，界面该把那排 [好呀] [不用了] 一起收掉。
    # 为什么非要有这一声：状态与按钮必须**同寿**——服务清掉 `_awaiting` 而气泡上那排
    # 还挂着，就是一个点不动的邀请（`_on_offer_reply` 先问 `awaiting_confirm`，判 False
    # 就落到通用聊天兜底，真机回的是那句"好的呀"）。参数由界面自己取，一个也不传。
    social_offer_expired = Signal()

    # 卡内小助手（v2-card-tools-A）：某张卡自己的小窗回了话。第一个参数是**卡号**
    # （`life:12` / `work:feature_a`，见 `life/cards/chat.card_namespace`），
    # 界面拿它认"这句该落进哪个卡窗"——隔壁那个窗不接，全局那个气泡也不接。
    card_chat_reply = Signal(str, str)
    # 卡上的小部件有变化（v2-card-tools-B）：加上了 / 拿掉了 / 勾选状态变了。
    # 参数同上也是卡号——跟 card_chat_reply 分开，是因为"卡上摆什么"和"卡窗说了什么"
    # 是两件事：界面收到前者要重铺部件（可能整块高度都变），收到后者只添一个气泡。
    card_tools_changed = Signal(str)
    # 卡窗里的一句交代（眼下只有危机表达那条）：**它不算回话，也不是宠物说的那句话**，
    # 界面按居中灰字的提示显示，不按气泡。危机话本身由 pet_speak 交给宠物本体说。
    card_chat_note = Signal(str, str)
