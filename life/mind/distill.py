"""对话提炼（S3）：一轮聊完，把它压成关系 / 情绪 / 事件三条线 + 几条生活碎片。

什么时候算"聊完了"：用户停了 ``mind.idle_end_min`` 分钟没再说话。桌宠没有
"挂断"这个动作，只能靠沉默来判——这比每句都提炼要省得多，也更符合语义
（半句话提炼出来的东西本来就是残的）。除此之外 ``distill_now`` 可以手动催一次，
后台定时那条线用的就是它。

还有第三次机会在退出路径上：``flush``。用户往往是说完最后一句话就把程序关了，
离空闲判定还差好几分钟，那些话本该留着却随进程一起没了。所以关窗前再抢一次——
但**只等 ``FLUSH_TIMEOUT_S`` 秒**：提炼要联网，宁可少记几句，也不能让"关不掉"。

发给模型的只有**对话文字**。屏幕感知那条线（OCR、窗口标题）一概不进这里——
这是 S7 明写的红线：摘要可以上云，屏幕内容不行。

问谁则是可切的（A++ 本地优先）：本机 ``data/models/`` 里放着量化模型就**在本地算**，
对话原文一个字节都不出本机；没有模型（或本地这趟没跑成）就**自动回落 API**，
行为跟以前一模一样。切换点全在 ``_ask`` 一个方法里，写库那条链不受影响。

写回来的东西分三处：关系/情绪/事件是"当前状态"，覆写 ep_summary；
碎片是"能日后拿来搭话的细节"，追加进 fragment；提炼结果另外回给上层一份
（``_on_distilled``），记忆簿据此落库（B 养成）。谁都不写 pet.db。
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable

from PySide6.QtCore import QObject, QTimer

from brain.llm.factory import LLMFactory
from brain.prompts.templates import MIND_DISTILL_PROMPT
from core.config import Config
from core.worker import run_async
from life.mind import local_llm
from life.mind.store import MindStore

logger = logging.getLogger(__name__)

# 多久看一次"用户是不是已经歇了"
CHECK_MS = 30 * 1000
# 一轮最多攒多少句再提炼：聊了两小时不能整段塞给模型
MAX_TURNS = 40
# 一次最多落几条碎片：模型有时候会一口气给十几条，挑前面几条就够，免得库变垃圾场
MAX_FRAGMENTS = 3
# 退出抢提炼最多等多久（秒）。等的是模型那一趟往返，超了就放手——
# 退出路径上"关不掉"比"少记几句"严重得多
FLUSH_TIMEOUT_S = 5.0


class MindDistiller(QObject):
    """攒对话 → 判定聊完 → 提炼 → 进加密库。"""

    def __init__(
        self,
        config: Config,
        store: MindStore,
        on_distilled: Callable[[dict[str, Any]], None] | None = None,
        parent: QObject | None = None,
    ):
        super().__init__(parent)
        self._config = config
        self._store = store
        self._on_distilled = on_distilled

        self._turns: list[str] = []
        # 最后一句话是什么时候说的（monotonic 秒）。用 -inf 表示"还没说过"，
        # 免得开机不满 idle_end_min 的机器一上来就判成"聊完了"
        self._last_turn_at = float("-inf")
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_MS)
        self._timer.timeout.connect(self._check_idle)
        # 正在提炼时别再催一次：同一批对话提两遍会写出重复碎片
        self._running = False
        # 最近一趟提炼是在本地算的还是回落的 API（设置页提示 / 测试断言用）
        self._last_used_local = False

    # ---------- 对外 ----------

    def start(self) -> None:
        if self._config.mind.enabled:
            self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def note_turn(self, text: str) -> None:
        """用户说了句话。只留用户这侧——宠物自己回了什么不影响记什么。"""
        if not self._config.mind.enabled:
            return
        text = (text or "").strip()
        if not text:
            return
        self._last_turn_at = time.monotonic()
        self._turns.append(text)
        if len(self._turns) > MAX_TURNS:
            self._turns = self._turns[-MAX_TURNS:]

    def discard_pending(self) -> int:
        """把攒着还没提炼的对话丢掉（v2-reset-state），返回丢了几条。

        清空之后必须调它：缓冲区里那几句要是不丢，下一轮空闲判定会照常把它们提炼
        成碎片写回库里——用户刚看着"已清空"，过几分钟记忆又自己长回来了。
        `_last_turn_at` 一并复位成"还没说过"：不然复位那一刻就算在"刚聊完"，
        下一次检查立刻又催一轮。
        """
        dropped = len(self._turns)
        self._turns = []
        self._last_turn_at = float("-inf")
        return dropped

    @property
    def pending(self) -> int:
        """攒着还没提炼的句数（测试与设置页看进度用）。"""
        return len(self._turns)

    @property
    def used_local(self) -> bool:
        """最近一趟提炼是不是在本地算的（False = 回落了 API）。"""
        return self._last_used_local

    def local_ready(self) -> bool:
        """本机现在有没有能用的本地模型（设置页提示 / 首屏探测用）。"""
        return local_llm.available(self._config.local_models_dir)

    def distill_now(self) -> bool:
        """把攒着的对话立刻提炼掉。返回这次有没有真的派出去。"""
        if self._running or not self._turns or not self._config.mind.enabled:
            return False
        batch, self._turns = self._turns, []
        self._running = True
        run_async(
            self._run,
            batch,
            on_done=self._done,
            on_failed=lambda exc: self._failed(exc, batch),
        )
        return True

    def flush(self, timeout_s: float = FLUSH_TIMEOUT_S) -> bool:
        """退出前把攒着的对话抢提炼一次。返回是否在超时前写完。

        退出路径是主线程，不能在这里直接调 ``_run``（联网那一趟会把窗口钉住），
        所以丢给一个后台线程跑，自己最多等 ``timeout_s`` 秒。三件事保证它拖不垮退出：

        - 超时就走：线程是 daemon，剩下的让它自己跑完或随进程结束；
        - 不抛异常：这是关窗路径，任何异常都得咽下去，只记日志；
        - 不占退出名额：写不进去就是少记几句，库里已有的东西一条不动。

        ``_running`` 时直接返回 False：那一批已经派出去了，本来就在写，
        这里再抢一次只会写出重复碎片。所以这里**不**碰 ``_done`` / ``_failed``
        那套收尾（重排队的 bookkeeping 对马上要死的进程没意义）。
        """
        if self._running or not self._turns or not self._config.mind.enabled:
            return False
        batch, self._turns = self._turns, []
        self._running = True
        failure: list[Exception] = []

        def _work() -> None:
            try:
                result = self._run(batch)
            except Exception as exc:  # noqa: BLE001 —— 关窗路径，什么都别往外扔
                failure.append(exc)
                return
            # 记忆簿跟着这批一起落。这条路**不走 _done**，不在这儿补一刀的话，
            # "关窗前说的最后几句话"就是记了却白记——碎片进了库，日记没有。
            self._notify(result)

        worker = threading.Thread(target=_work, name="mind-flush", daemon=True)
        worker.start()
        worker.join(timeout_s)
        if worker.is_alive():
            # _running 故意留 True：那一批还在后台写，别再让谁派第二批
            logger.warning("情感记忆：退出抢提炼超时（%.1fs），这 %d 句没记上",
                           timeout_s, len(batch))
            return False
        self._running = False
        if failure:
            logger.error("情感记忆：退出抢提炼失败: %s", failure[0])
            return False
        logger.info("情感记忆：退出前抢提炼了 %d 句", len(batch))
        return True

    # ---------- 内部 ----------

    def _check_idle(self) -> None:
        """闲着够久就当这一轮聊完了。"""
        if not self._turns:
            return
        if time.monotonic() - self._last_turn_at >= self._config.mind.idle_end_min * 60:
            self.distill_now()

    def _ask(self, prompt: str) -> dict[str, Any]:
        """这一趟问谁：本机有量化模型就在本地算，没有就照旧走 API。

        A++ 本地优先。**只换"调谁"**——两边回的 JSON 走的是同一段解析，
        后面写库那条链（profile / ep_summary / fragment）一个字都不动。

        本地跑砸了（权重换了一半、内存不够、吐出来不是 JSON）不往上抛：在这儿
        当成"本机这趟不行"，回落 API。宁可多花一趟钱，也不能把这批对话丢了。
        回落是**预期内**的常态，不是故障——Gitee 用户拉下来就是这样。
        """
        if self._config.mind.local_model:
            try:
                data = local_llm.infer(self._config.local_models_dir, prompt)
            except local_llm.LocalUnavailable as exc:
                logger.info("情感记忆：本机这次没用上（%s），回落 API 提炼", exc)
            else:
                self._last_used_local = True
                return data
        self._last_used_local = False
        return LLMFactory.create(self._config).chat_json(prompt)

    def _run(self, batch: list[str]) -> dict[str, Any]:
        """后台线程里跑：问模型 → 落库。"""
        known = self._known_text()
        turns = "\n".join(f"- {line}" for line in batch)
        prompt = MIND_DISTILL_PROMPT.format(known=known, turns=turns)
        data = self._ask(prompt)

        profile = data.get("profile") or {}
        if isinstance(profile, dict) and any(str(v).strip() for v in profile.values()):
            self._store.save_profile(**profile)

        events = [str(e).strip() for e in (data.get("events") or []) if str(e).strip()]
        self._store.save_ep(
            rel_state=str(data.get("rel_state") or "").strip(),
            emotion_snapshot=str(data.get("emotion_snapshot") or "").strip(),
            events=events,
        )

        saved: list[dict[str, Any]] = []
        for item in data.get("fragments") or []:
            if len(saved) >= MAX_FRAGMENTS:
                break
            if not isinstance(item, dict):
                continue
            text = str(item.get("text") or "").strip()
            if not text:
                continue
            tags = [str(t).strip() for t in (item.get("tags") or []) if str(t).strip()]
            fragment_id = self._store.add_fragment(text, tags)
            saved.append({"id": fragment_id, "text": text, "tags": tags})

        # 把**正文**也带出去（不只是计数）：强事件那条线要认"这次聊到的是什么事"，
        # 记忆簿要拿原话拼今天的日记。只回个数字它们没法干活。
        #
        # 注意这里回的是**这一轮模型说的**关系/情绪，不是库里的当前值。库里那份是覆写的
        # "当前状态"，会一直挂着上次那句——拿它去判"这一轮有没有大事"，会把上一轮的
        # 旧事当成这一轮的新事。要"这一轮发生了什么"，就得用这一轮的原文。
        return {
            "turns": len(batch),
            "events": events,
            "fragments": len(saved),
            "fragment_items": saved,
            "rel_state": str(data.get("rel_state") or "").strip(),
            "emotion_snapshot": str(data.get("emotion_snapshot") or "").strip(),
        }

    def _done(self, result: dict[str, Any]) -> None:
        self._running = False
        logger.info(
            "情感记忆提炼完成：%d 句 → 事件 %d 条、碎片 %d 条",
            result.get("turns", 0), len(result.get("events") or []), result.get("fragments", 0),
        )
        self._notify(result)

    def _notify(self, result: dict[str, Any]) -> None:
        """把提炼结果交给上层（记忆簿 / 强事件经验）。**这条路上不能抛**——
        flush 是在关窗路径上走它的，一个回调异常不该把退出拖成崩溃。"""
        if self._on_distilled is None:
            return
        try:
            self._on_distilled(result)
        except Exception as exc:  # noqa: BLE001 —— 见上：记忆已经入库了，回调塌了就算了
            logger.error("情感记忆：提炼回调没跑成（%s），这批记忆本身已经入库", exc)

    def _failed(self, exc: Exception, batch: list[str]) -> None:
        """提炼失败不吞掉对话：放回队首，下个检查点再试一次。"""
        self._running = False
        logger.error("情感记忆提炼失败: %s", exc)
        self._turns = batch + self._turns

    def _known_text(self) -> str:
        """已经记住的那部分，给模型当"有没有变化"的对照。"""
        ep = self._store.ep()
        profile = self._store.profile()
        lines: list[str] = []
        if profile:
            lines.append("画像：" + "、".join(f"{k}={v}" for k, v in profile.items()))
        if ep["rel_state"]:
            lines.append("关系：" + ep["rel_state"])
        if ep["emotion_snapshot"]:
            lines.append("情绪：" + ep["emotion_snapshot"])
        if ep["events"]:
            lines.append("事件：" + "；".join(str(e) for e in ep["events"][-3:]))
        return "\n".join(lines) if lines else "（还没有）"
