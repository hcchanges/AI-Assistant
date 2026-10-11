"""
社交换会话清场（v2-social-session-reset）的自动测试。

真机现象：宠物一直开着，在微信里聊完 A 又换到 B 接着聊——**在 B 里对方回话，宠物不动**
（换个对话就哑了）。根因是"换个会话"这件事在前台那一层看不出来：进程还是 wechat.exe，
`on_foreground` 不会发第二次，于是服务里那条"走出社交前台才清场"的支路一次都不触发，
上一场"这份对方原话拟过了 / 这一屏已锁"的记忆原样带进了下一场，把新会话的对方原话
挡在门外。

这一轮：装配层抓屏那一拍**顺手记下会话标识**（就是那次"认前台是不是宠物自己"读到的
标题），服务每拍比一次"跟上一次是不是同一个人"，换了人就清掉上一场那十格。

红线：零新依赖；pet.db / mind.db 零写入；
**不额外读窗口标题**（那只手还是原来那一次读取，见 `bootstrap._social_session_id`）；
`_classify_incoming` / `_is_duplicate_source` / 监听冷却 / 气泡色一分没动。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, pump, section, summary, wait_for

from life.screen import social as smod
from life.screen.social import SocialReplyService
from life.screen.social_window import ChatRead, SENDER_OTHER, SENDER_UNKNOWN


class Probe:
    """接住「候选给了什么」「宠物说了什么」。"""

    def __init__(self, app) -> None:
        self.spoken: list[str] = []
        self.candidates: list[list[dict]] = []
        app.bus.pet_speak.connect(self.spoken.append)
        app.bus.pet_speak_proactive.connect(self.spoken.append)
        app.bus.social_candidates.connect(self.candidates.append)

    def reset(self) -> None:
        self.spoken.clear()
        self.candidates.clear()


def _rows(db_path, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    probe = Probe(app)
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True

    # 可控的假帧 / 假字 / 假空闲 / 假时钟 / 假会话标识（画面变了换 frame，字变了换 lines）
    grab = {"frame": "F0"}
    page = {"lines": []}
    idle = {"sec": 10.0}
    ident = {"s": "茗风"}
    clock = {"t": 1000.0}
    calls = {"n": 0}

    def _read(array) -> ChatRead:
        calls["n"] += 1
        lines = [(s, t) for s, t in page["lines"]]
        # raw 留空 → `visible` 退回"按行拼"，两边 + 认不出的都算（跟真抓那份同一个口径）
        return ChatRead(lines=lines)

    service._grab_chat = lambda: grab["frame"]
    service._read_chat = _read
    service._idle_seconds = lambda: idle["sec"]
    service._session_provider = lambda: ident["s"]
    service._examples_provider = lambda query: ""
    service._clock = lambda: clock["t"]
    # 冒话那道冷却不在这一批用例的射程里（这儿验的是**自动监听 / 换会话**那条线），先按住：
    # 不按住的话，第一次"非单聊 → 单聊"的跃迁会冒一句邀请，邀请一挂上 `_awaiting`，
    # `_poll_listen` 后面几拍就全被它挡回去（挡轮询是既有的设计，见它门口那行）——
    # 连 `_sync_session()` 都轮不到，换会话的清理自然一步都不走。
    # v2-social-due-sentinel 之前这一格是**靠"开机 30 分钟内一律静默"那道 bug 蒙对的**
    # ——刻度 1000 秒 < 冷却 1800 秒，`_due()` 一律判"冷却没到"。哨兵一修，这层蒙的
    # 掩护就没了，得显式按住（跟 test_social_auto 第 473 行那条老规矩一个写法）。
    #
    # v2-social-cooldown-per-window 起，按住这件事**不能再靠写 `_last_asked`**：那道冷却
    # 改成按"这一场是谁"分格记了，而这一批用例**通篇都在换会话**（`ident["s"]` 在
    # 茗风 / 李四 / 赵六 / 空之间来回换）——写进去只按住当时那一格，换成新那一场，冷却
    # 立刻又"没冒过"，邀请照挂。而本节恰恰是"换会话"那件事的用例：邀请一挂上 `_awaiting`，
    # `_poll_listen` 后面几拍全被挡回去，整批失守。
    # 所以直接按住**那道闸本身**：跟窗口无关，而这一批用例本来就不验它（`_due` 的哨兵
    # 语义由 test_social_trigger_window.py ⑨/⑪ 专门盯着）。
    service._due = lambda: False
    service._social_foreground = True
    service._last_fingerprint = None
    service._last_listen_tail = ""
    service._last_listen_kind = ""
    service._last_listen_at = 0.0
    service._running = False
    service._session_id = ""

    def _poll(frame: str, lines: list, *, unlock: bool = True,
              expect_candidate: bool = False) -> None:
        """驱动一拍监听并等它落定。

        `unlock=True` 先把完成态锁解开、冷却从零起算——这一轮看的是"换会话"这一件事，
        完成态锁那套另有专节（test_social_auto.py ④）。**换会话那一拍别用它**：要看的
        正是"上一场留下的锁被清掉了"。
        """
        service._running = False
        if unlock:
            service.reset_done()
            service._last_listen_at = 0.0
        # v2-social-helping-window（C-1 收窄）：自动那条路只在**进了帮聊窗口之后**才出候选
        # （点过 [好呀] / 喊过"帮我回"）。这一整个模块验的是"已经在帮这一场时，换人那一下
        # 记忆怎么清"，所以每一拍都先把这一格补上——**不补的话，正反两组都变成"被这扇门
        # 挡下的"，"换了会话才放行"这条对照就成了空转**（对照组和实验组会一样地不出候选，
        # 用例照样绿，但它什么都没验）。
        #
        # 落的是**眼下这一场**的键（`_cooldown_key()`）；换会话那一拍由 `_poll_listen` 里
        # 的 `_sync_session` 换键，所以下一拍开头这一句补的正是新那一场——两次落的不是同一格。
        service._mark_helping()
        probe.reset()
        calls["n"] = 0
        grab["frame"] = frame
        page["lines"] = list(lines)
        service._poll_listen()
        if expect_candidate:
            wait_for(lambda: bool(probe.candidates))
        else:
            wait_for(lambda: not service._listen_reading and not service._running)
        drain_threads()
        pump(0.1)

    stub.candidates_result = {"candidates": [
        {"draft": "挺开心的，你呢？", "why": "寒暄回寒暄，不升级"},
    ]}

    # ---------- ① 换会话 → 上一场那十格一次清干净 ----------

    section("① 换会话 → 上一场那十格一次清干净（不带着上一场的记忆进新会话）")

    check("会话标识这只手是可选的：不接就整条不生效（老构造照旧）",
          SocialReplyService(config=app.config, bus=app.bus)._session_provider is None, "")
    check("不接那只手时 _sync_session 什么都不做（回 False、不动状态）",
          SocialReplyService(config=app.config, bus=app.bus)._sync_session() is False, "")
    check("装配层把那次标题读取的会话标识接过来了（`session_provider` 已接）",
          service._session_provider is not None, "")

    # 打一屏底：第一次认到这一屏只当基线
    _poll("F1", [(SENDER_OTHER, "在吗")])
    check("第一拍只打底（还没来新消息就不拟）", not probe.candidates,
          f"{len(probe.candidates)} 组")

    # 对方回话 → 拟一段，把"上一场的记忆"喂成真的
    _poll("F2", [(SENDER_OTHER, "在吗"), (SENDER_OTHER, "周末有空一起爬山吗")],
          expect_candidate=True)
    check("茗风这场：对方回话 → 候选出来了（这一场有真记忆可比）",
          len(probe.candidates) == 1, f"{len(probe.candidates)} 组")
    # 材料是**视觉最底部那一条**对方气泡（v2-social-ui-batch 批次8-A 起收窄的口径：
    # 老口径把"我上一次开口之后的对方话一条不落"全留下，记的是"在吗 周末有空一起爬山吗"）。
    # 这一条只当"换会话前真有一点记忆在"的哨兵用，跟着新口径走。
    check("拟过的那份材料记下了（换会话前它确实在）",
          service._last_draft_source == "周末有空一起爬山吗",
          repr(service._last_draft_source))
    check("完成态锁也立着（换会话前它确实在）", service._done is True and bool(service._done_lines),
          f"done={service._done} / 锁了 {len(service._done_lines)} 行")
    service._last_offered = 123.0        # 哨兵：建卡那一问的"这趟只问一次"

    # 换会话（茗风 → 李四）：**这一拍不解锁、不重设冷却**，看的就是清场本身
    ident["s"] = "李四"
    _poll("F3", [(SENDER_OTHER, "在吗")], unlock=False)
    check("换会话认出来了（会话标识换成了新那一场）", service._session_id == "李四",
          repr(service._session_id))
    check("上一场拟过的那份材料清掉了（就是它在挡新会话）",
          service._last_draft_source == "", repr(service._last_draft_source))
    check("在途那一份也清掉了", service._pending_auto_source == "",
          repr(service._pending_auto_source))
    check("建卡那一问的'这趟只问一次'跟着翻篇", service._last_offered == 0.0,
          str(service._last_offered))
    check("完成态锁解开了（上一段的锁不锁新会话）",
          service._done is False and service._done_lines == set()
          and service._locked_pending is False,
          f"done={service._done} / 剩 {len(service._done_lines)} 行")
    check("末尾归属那一格也清了（新会话要从零起）", service._last_listen_kind == "",
          repr(service._last_listen_kind))
    check("尾巴那一格换成了新会话这一屏的底（不是上一场那条尾巴）",
          service._last_listen_tail == "在吗", repr(service._last_listen_tail))
    check("换会话那一拍不拟：不会拿人家刚点开的那句旧话当场拟一段",
          not probe.candidates, f"{len(probe.candidates)} 组")
    check("换会话那一拍重新认了一遍（指纹作废，这一屏就是新一场的底）", calls["n"] >= 1,
          f"{calls['n']} 次")

    # ---------- ② 同一场 / 认不出 → 一格都不动 ----------

    section("② 同一场 / 认不出这一场是谁 → 一格都不动（宁可漏清，不误清）")

    service._last_draft_source = "哨兵材料"
    service._pending_auto_source = "哨兵在途"
    service._last_offered = 7.0
    service._last_listen_kind = "incoming"
    service._done = False                # 完成态锁那道闸另有专节，这儿单看"换没换人"
    service._done_lines = {"哨兵行"}
    _poll("F3", [(SENDER_OTHER, "在吗")], unlock=False)      # 同一场、画面都没变
    check("同一场：材料不动", service._last_draft_source == "哨兵材料",
          repr(service._last_draft_source))
    check("同一场：在途不动", service._pending_auto_source == "哨兵在途",
          repr(service._pending_auto_source))
    check("同一场：建卡那一问的记忆不动", service._last_offered == 7.0,
          str(service._last_offered))
    check("同一场：完成态那一屏的底不动", service._done_lines == {"哨兵行"},
          str(service._done_lines))
    check("同一场：末尾归属不动", service._last_listen_kind == "incoming",
          repr(service._last_listen_kind))

    ident["s"] = ""
    _poll("F4", [(SENDER_OTHER, "嗯嗯")], unlock=False)       # 认不出这一场是谁
    check("认不出（空标识）→ 当没换人：材料不动", service._last_draft_source == "哨兵材料",
          repr(service._last_draft_source))
    check("认不出（空标识）→ 当没换人：完成态那一屏的底不动",
          service._done_lines == {"哨兵行"}, str(service._done_lines))
    check("认不出（空标识）→ 也不把会话标识记成空", service._session_id == "李四",
          repr(service._session_id))
    ident["s"] = "李四"

    # ---------- ③ 对照 / 实验：同一份对方原话，换会话才放行 ----------

    section("③ 对照 / 实验：同一份对方原话，换会话才放行（真机那条死区的本体）")

    # 先把这一场自己的记忆喂出来：对方回话 → 拟过一份「在吗」
    service._last_listen_tail = "在吗"      # 上一节这一屏已经打底过了
    _poll("F5", [(SENDER_OTHER, "在吗")])
    check("（打底）画面没变化的那一拍不拟", not probe.candidates,
          f"{len(probe.candidates)} 组")
    _poll("F6", [(SENDER_OTHER, "在吗"), (SENDER_UNKNOWN, "嗯嗯")], expect_candidate=True)
    check("李四这场：对方回话 → 候选出来了", len(probe.candidates) == 1,
          f"{len(probe.candidates)} 组")
    check("这一场这份材料记下了（下面两组比的就是它）",
          service._last_draft_source == "在吗", repr(service._last_draft_source))

    # 对照组：**同一场**，画面又变了一点（对方新那句没被认出发言人），材料还是「在吗」
    _poll("F7", [(SENDER_OTHER, "在吗"), (SENDER_UNKNOWN, "嗯嗯"), (SENDER_UNKNOWN, "哈哈")])
    check("对照组（同一场）：同一份材料不再重开一轮（去重闸照旧管用）",
          not probe.candidates, f"{len(probe.candidates)} 组")
    check("对照组：材料也没被顶掉", service._last_draft_source == "在吗",
          repr(service._last_draft_source))

    # 实验组：中间**换个会话**，同样是「在吗」+ 一条认不出的新行
    ident["s"] = "王五"
    _poll("F8", [(SENDER_OTHER, "在吗")], unlock=False)
    _poll("F9", [(SENDER_OTHER, "在吗"), (SENDER_UNKNOWN, "嗯嗯")], expect_candidate=True)
    check("实验组（中间换了会话）：同一份材料照拟——这就是修掉的那条死区",
          len(probe.candidates) == 1, f"{len(probe.candidates)} 组")
    check("拟出来的还是模型那一条",
          bool(probe.candidates) and probe.candidates[0][0]["draft"] == "挺开心的，你呢？",
          str(probe.candidates[0][:1] if probe.candidates else "（空）"))

    # 切回去（王五 → 李四）也照旧认得出来：不是"只对第一次换会话有效"
    ident["s"] = "李四"
    _poll("F10", [(SENDER_OTHER, "嗯")], unlock=False)
    check("再换回去也认得出来（切回李四 → 又清一场）",
          service._session_id == "李四" and service._last_draft_source == "",
          f"{service._session_id!r} / {service._last_draft_source!r}")

    # ---------- ④ 那排按钮不归它管：邀请挂着时这一拍本来就整个跳过 ----------

    section("④ 不动那排按钮：邀请挂着时这一拍整个跳过，提示清了它才接着跑")

    service._awaiting = True
    # 假装邀请是**这一场**刚冒的：值 + "记在哪一格"两样一起写（跟 `_ask_offer` 落戳那一刻
    # 同款，v2-social-cooldown-per-window）。只写值不写 `_asked_key` 的话，`_drop_stale_offer`
    # 会去 `_asked_key` 那一格（还是空串）取，取不到就当场 return False——这一节想验的
    # "还没到挂太久那条线"就变成了蒙对。
    service._last_asked = clock["t"]
    service._asked_key = service._cooldown_key()
    service._last_draft_source = "哨兵材料"
    ident["s"] = "赵六"
    _poll("F11", [(SENDER_OTHER, "在吗")], unlock=False)
    check("邀请挂着（_awaiting）→ 这一拍整个跳过，不碰那排按钮",
          service._awaiting is True, str(service._awaiting))
    check("邀请挂着 → 换会话的清理也还没发生（跟这一轮之前一个样）",
          service._last_draft_source == "哨兵材料" and service._session_id == "李四",
          f"{service._last_draft_source!r} / {service._session_id!r}")
    service._awaiting = False
    _poll("F11", [(SENDER_OTHER, "在吗")], unlock=False)
    check("按钮那件事了结之后，换会话立刻认出来、记忆清掉",
          service._session_id == "赵六" and service._last_draft_source == "",
          f"{service._session_id!r} / {service._last_draft_source!r}")

    # ---------- ⑤ 红线：不读标题、零新依赖、两库零写入 ----------

    section("⑤ 红线：这只手不新读标题 / 零新依赖 / 两库零写入")

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    import ast
    import inspect

    src = inspect.getsource(smod)
    refs: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Name):
            refs.add(node.id)
        elif isinstance(node, ast.Attribute):
            refs.add(node.attr)
        elif isinstance(node, ast.Import):
            refs.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            refs.update(a.name for a in node.names)
    check("服务里没有读窗口标题那只手（`foreground_title` 一个字都不出现）",
          "foreground_title" not in refs and "read_browser_title" not in refs,
          "、".join(sorted({"foreground_title", "read_browser_title"} & refs)) or "（一处都没有）")
    check("换会话只吃装配层递进来的那个字符串（`session_provider`）",
          "session_provider" in refs and "_session_id" in refs, "")
    check("零新依赖：还是标准库 + PySide6 那几样，没有新 import",
          not ({"requests", "numpy", "PIL", "psutil", "pyperclip"} & refs),
          "、".join(sorted({"requests", "numpy", "PIL", "psutil", "pyperclip"} & refs))
          or "（一处都没有）")

    import core.bootstrap as boot

    boot_src = inspect.getsource(boot)
    check("装配层把会话标识接进来了（`session_provider=self._social_session_id`）",
          "session_provider=self._social_session_id" in boot_src, "")
    check("会话标识就是抓屏那一拍**已经读到**的那个标题，不额外读一次",
          "self._social_session_hint = (title or \"\").strip()" in boot_src
          and "foreground_title()" not in inspect.getsource(boot.DesktopPetApp._social_session_id),
          "")
    check("会话标识不进日志（`_social_session_id` 里没有 logger）",
          "logger" not in inspect.getsource(boot.DesktopPetApp._social_session_id), "")

    check("pet.db utterance 一行没多",
          _rows(app.config.db_path, "utterance") == pet_before,
          f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
    check("mind.db 碎片一条没多", app.mind_store.fragment_count() == mind_before, "")

    drain_threads()
    box.cleanup()
    return summary("社交换会话清场（v2-social-session-reset）")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
