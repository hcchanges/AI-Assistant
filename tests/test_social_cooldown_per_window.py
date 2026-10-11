"""社交冒话那道冷却改成「按这一场是谁」分格（v2-social-cooldown-per-window）的自动测试。

改之前 `_last_asked` 是**一个标量**，于是那道 30 分钟冷却是**跨应用、跨人共用**的：
跟甲聊过、冒过一句，这半小时里切到乙那边一句都不冒了——真机 21:28:06 / 21:38:24 /
22:28:41 那几条「冷却挡下」就是这么来的。用户报的「切回个人聊天窗口不是每次都能触发」
有一半是它。

这一节把那一格换成 `_last_asked_by_window: dict[str, float]`，键取 `_cooldown_key()`
（= 眼下这一场是谁，`_session_id`），`_due()` 读的是**当前那一格**；`_last_asked`
做成 property 让既有调用点与既有用例逐字不用改。

三条守住的老语义：
- **哨兵按窗口复制**：某一格「没记过」（或缺席）= 「这一场从没冒过」，直接放行
  （v2-social-due-sentinel 的 `None` 语义，钟口径一个字没动）；
- **弹过就占这一格 30 分钟**：不理它 / 点了它都算，切走再切回来仍占着；
- **纯内存**：进程重启即清，不落盘、不进日志（日志只记标识的**长度**，不记名字）。

红线：零新依赖；不碰 config.yaml / .vscode；不写 pet.db / mind.db；不动 `_awaiting`
状态机与时钟；不动白名单 / 列表免 OCR / 危机两道闸。
"""

from __future__ import annotations

import inspect
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, section, summary  # noqa: E402

from life.screen import social as smod  # noqa: E402
from life.screen.social import (  # noqa: E402
    _ASKED_MAX_WINDOWS,
    OFFER_HANG_S,
    PAGE_LIST,
    PAGE_PERSONAL,
    SocialReplyService,
)


def _rows(db_path: str, table: str) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return int(conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
    finally:
        conn.close()


def main() -> int:
    box, stub, app = make_env()
    service: SocialReplyService = app.social_reply
    app.config.screen.social_enabled = True
    app.config.screen.social_auto_listen = True
    app.config.screen.ocr_enabled = True
    # 冷却写死成 30 分钟（跟 config 默认一致），别跟着本机配置走。
    app.config.screen.social_cooldown_min = 30

    offers: list[str] = []
    app.bus.social_offer.connect(offers.append)

    ident = {"s": "甲"}
    clock = {"t": 1000.0}
    service._session_provider = lambda: ident["s"]
    service._clock = lambda: clock["t"]
    service._examples_provider = lambda query: ""
    service._social_foreground = True
    service._last_proc = "Weixin.exe"

    def _switch(who: str) -> None:
        """换到某个人那一场（走真实的 `_sync_session`，不是直接塞 `_session_id`）。"""
        ident["s"] = who
        service._sync_session()

    def _knock() -> bool:
        """从会话总列表点进某个人的单聊那一拍——走真实的 `note_page_kind` 跃迁闸。

        回 True = 这一拍真冒了（`social_offer` 发了出去）。
        """
        service._awaiting = False
        service._running = False
        service._page_kind = PAGE_LIST          # 先退出来，「非单聊 → 单聊」才成立
        app.chat_bubble.hide_offer()
        before = len(offers)
        service.note_page_kind(PAGE_PERSONAL)
        return len(offers) - before == 1

    pet_before = _rows(app.config.db_path, "utterance")
    mind_before = app.mind_store.fragment_count()

    try:
        # ---------- ① 同一场：冒过就占住这一格 30 分钟 ----------

        section("① 同一场（还是甲）：冒过一句 → 这 30 分钟里再跃迁也不冒")

        _switch("甲")
        check("前置：甲这一场没有冷却记录（从没冒过）",
              service._last_asked is None, repr(service._last_asked))
        check("★ 甲这一场冒了", _knock() is True, f"{len(offers)} 次")
        check("★ 落戳记在**甲**那一格上",
              set(service._last_asked_by_window) == {"甲"},
              repr(sorted(service._last_asked_by_window)))
        check("★ 记的是那一刻的刻度", service._last_asked_by_window["甲"] == 1000.0,
              repr(service._last_asked_by_window))

        clock["t"] += 60.0
        check("★ 一分钟后再跃迁（还是甲）→ 被冷却挡下，不冒", _knock() is False,
              f"{len(offers)} 次")
        check("挡下这一拍页型退回了空串（这次跃迁没被消费掉）",
              service._page_kind == "", repr(service._page_kind))

        # ---------- ② 换一个人就是新的一格：立刻放行 ----------

        section("② 换到乙那一场 → 新的一格没冒过，立刻放行（这就是要修的那一半）")

        _switch("乙")
        check("前置：当前这格是乙，乙还没冒过",
              service._cooldown_key() == "乙" and service._last_asked is None,
              repr(service._cooldown_key()))
        check("★ 乙这一场**不被甲的冷却连坐**，当场冒出来", _knock() is True,
              f"{len(offers)} 次")
        check("★ 两场各占一格，互不覆盖",
              set(service._last_asked_by_window) == {"甲", "乙"},
              repr(sorted(service._last_asked_by_window)))

        clock["t"] += 30.0
        _switch("甲")
        check("★ 切回甲（甲才过 90 秒）→ 甲那一格还占着，照样不冒", _knock() is False,
              f"{len(offers)} 次")

        # ---------- ③ 满 30 分钟只放行**那一格** ----------

        section("③ 冷却跑完 30 分钟：那一格放行，别的格子不受影响")

        clock["t"] += 1800.0
        check("★ 甲满 1800 秒 → 放行，补冒这一句", _knock() is True, f"{len(offers)} 次")
        check("钟的口径没动：这一格记的就是当时的 monotonic 刻度",
              service._last_asked_by_window["甲"] == clock["t"],
              repr(service._last_asked_by_window))
        check("乙那一格被 prune 顺带清掉了（它早过冷却，留着没用）",
              "乙" not in service._last_asked_by_window,
              repr(sorted(service._last_asked_by_window)))

        # ---------- ④ 挂问期间换窗口：过期仍按「冒话那一刻那一格」算 ----------

        section("④ 挂问挂到一半用户切走了 → 过期判定要回头找**冒话那一格**")

        clock["t"] += 1800.0 + 10.0     # 先推过甲那一格的冷却，好让它再冒一次
        check("前置：甲这一场冒了一问挂着", _knock() is True and service._awaiting is True,
              f"awaiting={service._awaiting}")
        check("前置：落章记下了是**甲**那一格", service._asked_key == "甲",
              repr(service._asked_key))

        _switch("乙")                      # 挂问还在，人切到乙去了
        check("前置：这一场已经换成乙了（这正是那个坑）",
              service._cooldown_key() == "乙", repr(service._cooldown_key()))
        check("★ 拿**当前这一格**去取是取不到的（老写法会判成没过期）",
              service._last_asked is None, repr(service._last_asked))

        clock["t"] += OFFER_HANG_S + 1.0
        check("★ 但过期判定照样算得出来（它认的是 `_asked_key`）",
              service._drop_stale_offer() is True, str(service._awaiting))
        check("★ 那一问确实收掉了（没被换窗口搅成孤儿）",
              service.awaiting_confirm is False, str(service.awaiting_confirm))

        # ---------- ⑤ 读不出会话名 / 合并窗口：退化成等价全局冷却 ----------

        section("⑤ 会话名读不出来（或几场同名）→ 全场共用一格，等价于改之前的老行为")

        service._session_id = ""           # 标题读不出：装配层给的就是空
        service._awaiting = False
        service._last_asked_by_window.clear()
        clock["t"] += 3600.0
        check("★ 空标识这一格从没冒过 → 照旧放行（不会变成永不冒）",
              _knock() is True, f"{len(offers)} 次")
        check("★ 它记在空串那一格上", set(service._last_asked_by_window) == {""},
              repr(sorted(service._last_asked_by_window)))
        clock["t"] += 60.0
        check("★ 认不出是谁时全场共用这一格 → 这一小时里谁都不冒（= 改之前的行为）",
              _knock() is False, f"{len(offers)} 次")

        # ---------- ⑥ 纯内存 / prune / 上限 / 哨兵按窗口复制 ----------

        section("⑥ 这本账：重启即清、过期就删、超 64 格丢最旧、永不写 0.0")

        fresh = SocialReplyService(config=app.config, bus=app.bus)
        check("★ 重启即清：新起的服务这本账是空的（纯内存，不落盘）",
              fresh._last_asked_by_window == {} and fresh._last_asked is None,
              repr(fresh._last_asked_by_window))

        service._last_asked_by_window.clear()
        service._session_id = "旧的"
        service._last_asked = 5000.0
        service._session_id = "新的"
        service._last_asked = 5000.0 + 1801.0        # 写这一刻顺手 prune
        check("★ 过期的那一格被顺手删掉（不删的话这本账只增不减）",
              "旧的" not in service._last_asked_by_window
              and "新的" in service._last_asked_by_window,
              repr(sorted(service._last_asked_by_window)))

        service._last_asked_by_window.clear()
        for i in range(_ASKED_MAX_WINDOWS * 2):
            service._session_id = f"w{i:03d}"
            service._last_asked = 90_000.0           # 同一个刻度：谁都不过期，只能靠上限
        check(f"★ 格数封顶在 {_ASKED_MAX_WINDOWS}（防 `_session_id` 被灌出无穷多键）",
              len(service._last_asked_by_window) == _ASKED_MAX_WINDOWS,
              str(len(service._last_asked_by_window)))
        check("★ 丢的是最旧那批，最新的那几格还在",
              "w127" in service._last_asked_by_window
              and "w000" not in service._last_asked_by_window,
              repr(sorted(service._last_asked_by_window))[:80])

        check("★ 生产路径**永不**写 0.0（`None` 才是「从没冒过」的哨兵）",
              0.0 not in service._last_asked_by_window.values(),
              repr(sorted(service._last_asked_by_window.values())))
        service._last_asked_by_window.clear()
        service._session_id = "哨兵"
        check("★ 某一格从没冒过 → 直接放行（哨兵语义按窗口复制）",
              service._due() is True, "")

        # ---------- ⑦ 红线（源码层 / 两库） ----------

        section("⑦ 红线：只改「这一格记在谁头上」/ 冷却时长与钟口径没动 / 两库零写入")

        svc_src = inspect.getsource(smod)
        boot_src = inspect.getsource(type(app))
        due_src = inspect.getsource(SocialReplyService._due)
        drop_src = inspect.getsource(SocialReplyService._drop_stale_offer)
        ask_src = inspect.getsource(SocialReplyService._ask_offer)
        key_src = inspect.getsource(SocialReplyService._cooldown_key)

        check("★ `_last_asked` 是 property（既有调用点与既有用例逐字不用改）",
              isinstance(inspect.getattr_static(SocialReplyService, "_last_asked"), property),
              "")
        check("★ 读的是当前那一格（`_last_asked_by_window.get(self._cooldown_key())`）",
              "_last_asked_by_window.get(self._cooldown_key())" in svc_src, "")
        check("★ 过期那条腿认 `_asked_key`，不认当前窗口（挂问期间能换人）",
              "_last_asked_by_window.get(self._asked_key)" in drop_src, "")
        check("★ 落戳那一刻把 `_asked_key` 一起记上（跟值同拍）",
              "_asked_key = self._cooldown_key()" in ask_src, "")
        check("★ 冷却时长一个字没动（还是 `social_cooldown_min` 分钟）",
              "int(self._config.screen.social_cooldown_min)) * 60" in due_src, "")
        # 按**字节码**查（`co_names` 是这两个函数真正引用的全局名）——按源码查会被它们
        # 自己那段讲 "time.monotonic" 的 docstring 误伤。
        check("★ 钟口径没动：判定那两条腿都不引 `time` / `monotonic`，只读注入的 `_clock`",
              "time" not in SocialReplyService._due.__code__.co_names
              and "monotonic" not in SocialReplyService._due.__code__.co_names
              and "time" not in SocialReplyService._drop_stale_offer.__code__.co_names
              and "monotonic" not in SocialReplyService._drop_stale_offer.__code__.co_names
              and "_clock()" in due_src and "_clock()" in drop_src, "")
        check("★ 会话标识不进日志，只记长度（真机排查看得出换人了，看不出是谁）",
              "len(self._cooldown_key())" in svc_src and "这一场标识 %d 字" in svc_src, "")
        check("★ 这本账只在服务内部：装配层不知道它（`bootstrap` 里一处都没有）",
              "_last_asked_by_window" not in boot_src and "_last_asked" not in boot_src, "")
        check("★ `_cooldown_key` 只读 `_session_id`，不另开一只手去读窗口标题",
              "_session_provider" not in key_src and "_grab" not in key_src, "")
        check("★ `_awaiting` / `_running` 状态机一个字没动",
              "_awaiting =" not in key_src and "_running =" not in key_src, "")

        check("两库零写入：pet.db utterance 一行没多",
              _rows(app.config.db_path, "utterance") == pet_before,
              f"{pet_before} → {_rows(app.config.db_path, 'utterance')}")
        check("两库零写入：mind.db 碎片一条没多",
              app.mind_store.fragment_count() == mind_before, "")
    finally:
        drain_threads()
        box.cleanup()

    return summary("社交冒话冷却按「这一场是谁」分格（v2-social-cooldown-per-window）")


if __name__ == "__main__":
    raise SystemExit(main())
