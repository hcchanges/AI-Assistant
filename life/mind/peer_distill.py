"""对象视角提炼（v2-social-card-autodistill）：聊完一轮，把「对面这个人」补进攻略卡 + 三记忆库。

跟 `distill.py` 是一对**镜像**，不是第二套机制：

| | `MindDistiller` | `PeerDistiller`（本模块） |
| --- | --- | --- |
| 看谁说的话 | **主人自己**（`utterance_analyzed` 那条总线） | **聊天对象**（`peer_utterance` 那条总线） |
| 什么时候算聊完 | 沉默 `mind.idle_end_min` 分钟 | 同一把尺、同一个常量 |
| 问谁 | `distill.ask_local_first`（本地优先，回落 API） | **同一个函数** |
| 落哪 | `struct_profile` / `ep_summary` / `fragment` 的**主人那一格** | 同样三张表的 **`peer` 那一格** |
| 攒在哪 | 一个列表（只有一段关系） | 一个字典，键是 `sid|peer`（一场一格） |

**为什么要它**：用户手动开好「对象攻略卡」之后，卡上那几格原先只能靠建卡那一下弹框
里手填——建完就空在那儿了（真机图证：所有格子都是空的）。攻略卡的下一半（"桌宠后台
静默自动补全"）在前五批里只做完了字段、peer 维度与注入，**补全这一半从没实现**。
这个模块就是那一半：把对方说过的话攒着，聊完在后台抽一遍，静默写回去。

三条口径，一条都不能松：

- **按 `sid|peer` 分格**：跟社交那条线冒话冷却 / 帮聊窗口**同一把键**（`_cooldown_key()`）。
  换个会话窗就是另一格，两场话不会攒到一起。**落库时分格坐标是"这个人"**（`store._peer_key`，
  同一个人在另一扇窗里还是同一个人），跟主聊天的"主人那一档"彻底分开。
- **认不出对象名就整场不动**：没有名字就没有格，也就无从知道该补哪个人——宁可不抽，
  也绝不把这份话写进主人自己那一格（那是本模块最怕的那条串场）。
- **卡不存在就不跑**：用户还没开那张卡，说明他还没说要记这个人。这条闸由装配层注入
  （`card_exists`），在这儿也再问一次；**不替每一个聊过的人都建一份档案**。

隐私口径（跟 `distill.py` 一样严）：喂给模型的是**对方的话**（屏幕上认出来的字，
只走内存），落库的**只有抽出来的结论**——"他是什么人"，原话一个字都不写；而"问谁"
跟主聊天共用 `ask_local_first`，本机有量化模型就在本地算，**对话原文一个字节都不出本机**。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterable

from PySide6.QtCore import QObject, QTimer

from brain.prompts.templates import PEER_DISTILL_PROMPT
from core.config import Config
from core.worker import run_async
from life.mind.distill import CHECK_MS, FLUSH_TIMEOUT_S, MAX_TURNS, ask_local_first
from life.mind.store import MindStore

logger = logging.getLogger(__name__)

# 一次最多落几条碎片：跟主聊天那份同一个数（模型有时候一口气给十几条）
MAX_FRAGMENTS = 3
# 每一格最多留多少字再抽：一格攒太长（对方刷屏）既慢又偏，取最近的
MAX_CHARS = 1500
# 单独一格字段的字数上限（模型偶尔会写一长句，卡上那几格是"一眼看得到"的）
FIELD_CLIP = 60

# 抽出来直接写进攻略卡的那几格：名字就是 `Card` 上的列名。
# 前四个是 v2-social-card-autodistill 新加的，后四个是 v2-social-card-base 就有的
# ——对卡而言它们是同一批"对象档案"，所以在这儿合成一张表；`CardDAO.set_profile`
# 认得的就是这些键（见 `data/dao.py::_PROFILE_COLUMNS`）。
CARD_FIELDS = (
    "age", "emotion_state", "anxiety", "interests",
    "likes", "dislikes", "strategy", "relation_stage",
)


def _clean(value: Any) -> str:
    """一个字段收成能落库的样子：压空白、去首尾、截长。空 / 不成话的一律回空串。"""
    if value is None:
        return ""
    text = " ".join(str(value).split())
    return text[:FIELD_CLIP]


def _as_list(value: Any, limit: int) -> list[str]:
    """模型给的一串东西收成干净的一列（每条压空白、去重、截长、最多 limit 条）。"""
    if not isinstance(value, (list, tuple)):
        return []
    out: list[str] = []
    for item in value:
        text = _clean(item)
        if text and text not in out:
            out.append(text)
        if len(out) >= limit:
            break
    return out


class PeerDistiller(QObject):
    """攒「对方说的话」→ 判定聊完 → 抽「这个人是谁」→ 写攻略卡 + 三记忆库的 peer 格。"""

    def __init__(
        self,
        config: Config,
        store: MindStore,
        card_exists: Callable[[str], bool] | None = None,
        write_card: Callable[[str, dict[str, str]], None] | None = None,
        on_distilled: Callable[[dict[str, Any]], None] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._store = store
        # 「这张卡在不在」「把这几格写上去」：两只手都由装配层注入——本模块**不认识卡**
        # （跟社交那条线不认识库是同一条口径：谁的东西谁自己拿手，这边只管抽）。
        # 不接（测试 / 没装卡模块）就当"卡不在"，整条不跑：**宁可不抽，也不白烧一趟模型**。
        self._card_exists = card_exists if card_exists is not None else (lambda peer: False)
        self._write_card = write_card
        # 抽完告诉上层一声（装配层现在没接；留着是为了跟 `MindDistiller` 同形——
        # 那边靠这条回调落记忆簿，这边要落什么将来也走这儿）
        self._on_distilled = on_distilled

        # 一场一格：键 = `sid|peer`（社交那边 `_cooldown_key()` 就是它）。
        # `_peer_of` 存的是同一个键上的**对象名**——落库时用它当分格坐标（不是键）。
        self._turns: dict[str, list[str]] = {}
        self._peer_of: dict[str, str] = {}
        # 这一格最后一句是什么时候进来的（monotonic 秒）。用 -inf 表示"还没说过"，
        # 免得开机不满 idle_end_min 的机器一上来就判成"聊完了"（同一个老坑）
        self._last_turn_at: dict[str, float] = {}
        # 正在抽的那几格：同一格别再派第二趟（会写出重复碎片）
        self._running: set[str] = set()
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self._check_idle)
        # 最近一趟是在本地算的还是回落的 API（跟 MindDistiller 同一格，设置页 / 测试看它）
        self._last_used_local = False

    # ---------- 开关 ----------

    @property
    def enabled(self) -> bool:
        """整条开不开：`mind.enabled` 与 `screen.card_autodistill` 两道都要过。

        **明写 `off` 才关**——`!= "off"` 这个写法跟社交那边的 `_card_memory_on` /
        `_tone_align_on` **一字不差**（那两个也是直接比字符串、不 lower 不 strip）：
        写别的值（含 `OFF` / `0` / 留空 / `auto`）一律按开算，功能不该因为一个笔误
        悄悄失效。取不到那一格（老配置 / 老调用点）当 `auto`：默认开，"读不到"不该
        变成"关掉"。
        """
        if not self._config.mind.enabled:
            return False
        value = getattr(self._config.screen, "card_autodistill", "auto")
        return (value if value is not None else "auto") != "off"

    def start(self) -> None:
        if self.enabled:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    # ---------- 攒 ----------

    def note_turn(self, key: str, peer: str, text: str) -> None:
        """对方说了句话（`peer_utterance` 那条总线上的接听口）。

        `key` 是这一场的分格键（`sid|peer`）、`peer` 是对象名、`text` 是对方那半边、
        按条取尾的那一份材料。递进来的是**尾巴**——每一拍都从屏幕最底下重新取一次，
        所以它跟这一格末尾那几条**必定重叠**：对方来一句新的，尾巴就长一行（`[A,B]` →
        `[A,B,C]`）；对方没说话，尾巴原样再来一遍（`[A,B]` → `[A,B]`）。

        所以接的时候先**对尾巴**：找出末尾与这份材料**开头**最长的重合（逐字比），
        只把重合之后的新行接上去。三种情形一条规则全包住——
        · 一模一样 → 重合全长 → 一行不加（**同一句不重记**，不然一格会被同一句灌满）；
        · 多了一行 → 只加那一行（不是整份重记）；
        · 完全不重合（换了话题 / 头一次递）→ 整份接上。
        一条消息被 OCR 折成两行时，尾巴是那两行一起送来的，逐字对才判得准。
        """
        if not self.enabled:
            return
        peer = (peer or "").strip()
        if not peer:
            return     # 认不出对象名：这一场不记（见模块开头第二条口径）
        fresh = [line.strip() for line in (text or "").splitlines()]
        fresh = [line for line in fresh if line]
        if not fresh:
            return
        book = self._turns.setdefault(key, [])
        overlap = 0
        for size in range(min(len(book), len(fresh)), 0, -1):
            if book[-size:] == fresh[:size]:
                overlap = size
                break
        book.extend(fresh[overlap:])
        # 两把闸一起收：条数（`MAX_TURNS`，跟主聊天同一个数）与总长
        if len(book) > MAX_TURNS:
            del book[:-MAX_TURNS]
        while len("\n".join(book)) > MAX_CHARS and len(book) > 1:
            book.pop(0)
        self._peer_of[key] = peer
        self._last_turn_at[key] = time.monotonic()

    @property
    def pending(self) -> int:
        """所有格子里攒着还没抽的行数（测试与诊断看进度用）。"""
        return sum(len(lines) for lines in self._turns.values())

    def pending_for(self, key: str) -> int:
        """这一格攒了几行。"""
        return len(self._turns.get(key, ()))

    def discard_pending(self) -> int:
        """把攒着还没抽的整批丢掉（v2-reset-state 那条线），返回丢了几行。

        跟 `MindDistiller.discard_pending` 一个道理：用户刚在设置里清了记忆，缓冲区里
        那几句要是不丢，下一次空闲判定会照常把它们抽成结论写回库里——"清空了"过几分钟
        又自己长回来。三格一并复位。
        """
        dropped = self.pending
        self._turns.clear()
        self._peer_of.clear()
        self._last_turn_at.clear()
        return dropped

    @property
    def used_local(self) -> bool:
        """最近一趟是不是在本地算的（False = 回落了 API）。"""
        return self._last_used_local

    # ---------- 抽 ----------

    def _check_idle(self) -> None:
        """闲着够久就当这一场聊完了。**一格一格判**：不是全局一个钟。"""
        if not self.enabled:
            return
        limit = self._config.mind.idle_end_min * 60
        now = time.monotonic()
        for key in list(self._turns):
            if key in self._running or not self._turns.get(key):
                continue
            if now - self._last_turn_at.get(key, float("-inf")) >= limit:
                self.distill_now(key)

    def distill_now(self, key: str) -> bool:
        """把**这一格**立刻抽掉。返回这次有没有真的派出去。

        「有没有卡」这道闸摆在**取出材料之前**：没卡时材料原样留在这一格里（他哪天把
        卡开出来，下一拍就补上），不是取出来再扔掉。代价只是每 30 秒重问一次卡在不在
        （一次本地查询，不调模型）——比"用户开卡之前那一段话白攒"划算。
        """
        if not self.enabled or key in self._running:
            return False
        lines = self._turns.get(key) or []
        peer = (self._peer_of.get(key, "") or "").strip()
        if not lines or not peer:
            return False
        if not self._card_exists(peer):
            # 没卡就不抽：不是"抽了没地方放"，是**用户还没说要记这个人**
            logger.debug("对象视角提炼：%s 还没有对象攻略卡，这一场先不抽（%d 行留着）",
                         peer, len(lines))
            return False
        self._turns.pop(key, None)
        self._peer_of.pop(key, None)
        self._running.add(key)
        run_async(
            self._run,
            peer,
            lines,
            on_done=lambda result, k=key: self._done(k, result),
            on_failed=lambda exc, k=key, p=peer, ls=lines: self._failed(k, p, ls, exc),
        )
        return True

    def flush(self, timeout_s: float = FLUSH_TIMEOUT_S) -> bool:
        """退出前把攒着的几格抢抽一次。返回是否在超时前跑完。

        跟 `MindDistiller.flush` 同一套：退出路径是主线程，不能在这儿直接跑联网那一趟，
        丢给一个后台线程、自己最多等 `timeout_s` 秒。三件事保证它拖不垮退出——超时就走
        （线程是 daemon）、不抛异常（关窗路径，只记日志）、不占退出名额（写不进去就是
        少记一格，库里已有的东西一条不动）。
        """
        if not self.enabled:
            return False
        jobs = []
        for key in list(self._turns):
            lines = self._turns.get(key) or []
            peer = (self._peer_of.get(key, "") or "").strip()
            if not lines or not peer or key in self._running:
                continue
            if not self._card_exists(peer):
                continue
            self._turns.pop(key, None)
            self._peer_of.pop(key, None)
            jobs.append((peer, lines))
        if not jobs:
            return False
        failure: list[Exception] = []

        def _work() -> None:
            for peer, lines in jobs:
                try:
                    self._write(peer, lines)
                except Exception as exc:      # noqa: BLE001 —— 关窗路径，什么都别往外扔
                    failure.append(exc)
                    logger.error("对象视角提炼：退出抢抽失败: %s", exc)

        worker = threading.Thread(target=_work, name="peer-distill-flush", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            logger.warning("对象视角提炼：退出抢抽超时（%.1fs），这 %d 格没补上",
                           timeout_s, len(jobs))
            return False
        logger.info("对象视角提炼：退出前抢抽了 %d 格", len(jobs))
        return not failure

    # ---------- 内部 ----------

    def _run(self, peer: str, lines: list[str]) -> dict[str, Any]:
        """后台线程里跑：问模型 → 落库（三记忆库的 peer 格）。"""
        return self._write(peer, lines)

    def _write(self, peer: str, lines: list[str]) -> dict[str, Any]:
        """抽 + 落。**这一步只写记忆库**；攻略卡那几格由 `_done` 在主线程上补
        （写卡要发 `card_changed`，那声信号从主线程发最稳）。"""
        known = self._known_text(peer)
        turns = "\n".join(f"- {line}" for line in lines)
        prompt = PEER_DISTILL_PROMPT.format(known=known, turns=turns)
        data, used_local = ask_local_first(self._config, prompt)
        self._last_used_local = used_local
        if not isinstance(data, dict):
            data = {}

        card = {key: _clean(data.get(key)) for key in CARD_FIELDS}
        card = {key: value for key, value in card.items() if value}
        events = _as_list(data.get("events"), MAX_FRAGMENTS)
        rel_state = _clean(data.get("relation_stage"))
        emotion = "；".join(
            part for part in (_clean(data.get("emotion_state")), _clean(data.get("anxiety"))) if part
        )
        intro = _clean(data.get("intro"))

        # ---- 三记忆库：关系 / 情绪 / 事件（`ep_summary` 一行三格）+ 碎片 ----
        self._store.save_ep(
            rel_state=rel_state, emotion_snapshot=emotion, events=events, peer=peer
        )
        # 一句话基本情况落 struct_profile 的 `intro`：`bootstrap._peer_profile_for` 会把它
        # 拼进起草提示词（v2-social-card-inject 那一格），下次拟草稿就用上了。
        if intro:
            self._store.save_profile(peer=peer, intro=intro)

        saved: list[dict[str, Any]] = []
        for item in data.get("fragments") or []:
            if len(saved) >= MAX_FRAGMENTS:
                break
            if not isinstance(item, dict):
                continue
            text = _clean(item.get("text"))
            if not text:
                continue
            tags = _as_list(item.get("tags"), 4)
            fragment_id = self._store.add_fragment(text, tags, peer=peer)
            saved.append({"id": fragment_id, "text": text, "tags": tags})

        return {
            "peer": peer,
            "turns": len(lines),
            "card": card,
            "events": events,
            "fragments": saved,
            "rel_state": rel_state,
            "emotion_snapshot": emotion,
        }

    def _done(self, key: str, result: dict[str, Any]) -> None:
        self._running.discard(key)
        logger.info(
            "对象视角提炼完成：%s %d 句 → 卡 %d 格、事件 %d 条、碎片 %d 条",
            result.get("peer", ""), result.get("turns", 0), len(result.get("card") or {}),
            len(result.get("events") or []), len(result.get("fragments") or []),
        )
        # 攻略卡那几格在这儿补（主线程）：写卡要发一声 `card_changed` 让卡面刷新，
        # 从主线程发最稳。写卡失败不该把这一趟判成失败——记忆已经落库了。
        card = result.get("card") or {}
        if card and self._write_card is not None:
            try:
                self._write_card(str(result.get("peer") or ""), dict(card))
            except Exception as exc:      # noqa: BLE001 —— 记忆已经入库，写卡塌了就算了
                logger.error("对象视角提炼：攻略卡没写上去（%s），记忆库里那几格已落", exc)
        self._notify(result)

    def _notify(self, result: dict[str, Any]) -> None:
        """把结果交给上层（界面 / 卡片刷新）。**这条路上不能抛**——flush 也会走它。"""
        if self._on_distilled is None:
            return
        try:
            self._on_distilled(result)
        except Exception as exc:      # noqa: BLE001 —— 见上：记忆已经入库了
            logger.error("对象视角提炼：回调没跑成（%s），这批记忆本身已经入库", exc)

    def _failed(self, key: str, peer: str, lines: list[str], exc: Exception) -> None:
        """抽失败不吞掉这一场：放回那一格，下个检查点再试一次（跟主聊天同一个态度）。"""
        self._running.discard(key)
        logger.error("对象视角提炼失败: %s", exc)
        book = self._turns.setdefault(key, [])
        book[:0] = lines
        if len(book) > MAX_TURNS:
            del book[:-MAX_TURNS]
        self._peer_of.setdefault(key, peer)
        self._last_turn_at[key] = time.monotonic()

    def _known_text(self, peer: str) -> str:
        """这一格已经记住的那部分，给模型当「有没有新东西」的对照（跟主聊天同一套）。"""
        lines: list[str] = []
        who = self._store.profile(peer=peer) or {}
        if who:
            lines.append("画像：" + "、".join(f"{k}={v}" for k, v in who.items()))
        ep = self._store.ep(peer=peer)
        if ep["rel_state"]:
            lines.append("关系：" + ep["rel_state"])
        if ep["emotion_snapshot"]:
            lines.append("情绪：" + ep["emotion_snapshot"])
        if ep["events"]:
            lines.append("事件：" + "；".join(str(e) for e in ep["events"][-3:]))
        return "\n".join(lines) if lines else "（还没有）"


def fragment_texts(store: MindStore, peer: str, limit: int = MAX_FRAGMENTS) -> Iterable[str]:
    """这一格最近几条碎片（测试与诊断用的小工具，不参与主链路）。"""
    return [
        str(row.get("text") or "").strip()
        for row in store.fragments(limit=limit, peer=peer)
    ]
