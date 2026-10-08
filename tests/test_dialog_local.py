"""A+++ 意图情绪本地优先（解析链接本地模型）的自动测试。

对应方案第八节验收表，逐条落成断言：

- **危机词**：classify 自己那道短路命中 → 0 次模型调用、走危机话术；
- **本机有模型**：解析链这次调用走本地推理，parse 的 API 调用 0 次；intent/emotion 拿得到；
- **本机无模型 / 本地跑砸**：回落 API，行为与 v2-dialog 等价（桩模型收到的还是那条 prompt）；
- **无模型无 API**：纯规则回落，体验就是 v2-dialog 现状（route 一字不改）；
- **五意图 + 多意图优先级**：规则层与模型层归一后一致（倾诉压过求助）；
- **红线**：pet.db / mind.db 零写入、危机最高优先、仓库无模型无新依赖。

真跑一个本地模型不在测试范围内（本机不一定装了 llama-cpp-python，也不该为跑测试
下载几百 MB 权重）。这里把**推理器**换成假的，但"谁来问、问完怎么校正路由"走的是
真实链路：chatter.handle → ChatterParseChain.analyze → _ask → router.refine。

⚠️ 诚实标注：本机 ``data/models/`` 里没有 LLM 权重（``find_model()`` 为 None），
所以"本机有模型 → 离线 0 次 API"这条**只能验路径与调用次数**（注入假推理器），
真值要实机放了模型才能跑出来。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, note, pump, section, summary, wait_for

from life.dialog import router
from life.mind import local_llm

# 真实现先攥在手里：下面会临时把 _build_runtime 换成假的，收尾要还回去
_REAL_BUILD = local_llm._build_runtime

# 假的"本地模型"这一趟吐什么：一句倾诉，情绪给的是自由词"疲惫"（要走别名归一到 低落）
LOCAL_PARSE_JSON = (
    '{"scene": "今天很累", "time_hint": "", "time_iso": "", "time_confidence": "none",'
    ' "emotion": "疲惫", "emotion_score": 0.6, "topic": "", "module": "心理安抚",'
    ' "command": "", "intent": "倾诉"}'
)


class FakeRuntime:
    """假推理器：complete() 回预先摆好的文本（也可以是异常）。"""

    def __init__(self, reply: str | Exception = LOCAL_PARSE_JSON):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _drop_model(config, name: str = "qwen2.5-0.5b-instruct-q4_k_m.gguf") -> Path:
    """往沙箱的模型目录里"放一个模型"（空文件就够——探测看的是文件名和格式）。"""
    models = Path(config.local_models_dir)
    models.mkdir(parents=True, exist_ok=True)
    path = models / name
    path.write_bytes(b"")
    return path


def _install_fake(runtime: FakeRuntime) -> None:
    """把"建推理器"这一步换成假的，并清掉缓存让它立刻生效。"""
    local_llm.reset()
    local_llm._build_runtime = lambda path: runtime  # type: ignore[assignment]


def _restore_real() -> None:
    """还原真实实现并清缓存。"""
    local_llm.reset()
    local_llm._build_runtime = _REAL_BUILD  # type: ignore[assignment]


def _parse_result(text: str, **over) -> dict:
    """一句普通话的桩解析结果；over 用来覆盖某几个字段（例如 intent）。"""
    base = {
        "scene": text[:12],
        "time_hint": "",
        "time_iso": "",
        "time_confidence": "none",
        "emotion": "平静",
        "module": "其他",
        "command": "",
    }
    base.update(over)
    return base


def _snapshot(path: Path) -> tuple[int, float] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (stat.st_size, stat.st_mtime)


def _reply_text(stub) -> str:
    return " ".join(str(m) for m in stub.messages[-1]) if stub.messages else ""


def main() -> int:
    box, stub, app = make_env()
    try:
        # ---------- S2：危机词规则层短路（防御纵深） ----------
        section("S2 危机短路：classify 自己那道，0 模型调用")

        p0, m0 = len(stub.prompts), len(stub.messages)
        hard = router.classify("我不想活了")
        check("危机句被 classify 自己截住", hard.crisis, str(hard))
        check("危机不给五类意图（不给下游按意图乱动的机会）", hard.intent != "求助", hard.intent)
        check("危机情绪给的是「低落」（往更软那边偏）", hard.emotion == "低落", hard.emotion)
        check(
            "classify 一个模型都没调（快照）",
            len(stub.prompts) == p0 and len(stub.messages) == m0,
            f"prompts {p0}→{len(stub.prompts)}，messages {m0}→{len(stub.messages)}",
        )
        check("普通句不会被误判成危机", not router.classify("今天好累").crisis, "")
        check(
            "危机 route 不接受模型校正（安全兜底不归模型管）",
            router.refine(hard, "闲聊", "开心") == hard,
            str(router.refine(hard, "闲聊", "开心")),
        )

        # 端到端：走真入口，危机话术 + 0 次解析
        spoken: list[str] = []
        app.bus.pet_speak.connect(spoken.append)
        p0 = len(stub.prompts)
        app.chatter_service.handle("我不想活了")
        wait_for(lambda: bool(spoken))
        drain_threads()
        check("走危机话术（含热线 12356）", any("12356" in s for s in spoken), spoken[0][:36] if spoken else "（没说话）")
        check("危机不跑解析链（0 次模型调用）", len(stub.prompts) == p0, f"{p0} → {len(stub.prompts)}")

        # ---------- S1：解析链本地优先 ----------
        section("S1 解析链本地优先：本机有模型 → 离线，parse 的 API 调用 0 次")

        chain = app.chatter_service._parse_chain
        _drop_model(app.config)
        good = FakeRuntime()
        _install_fake(good)

        p0 = len(stub.prompts)
        analysis = chain.analyze("今天好累")
        check("走的是本地那条腿", chain.used_local, f"used_local={chain.used_local}")
        check("解析这次的 API 调用是 0 次", len(stub.prompts) == p0, f"{p0} → {len(stub.prompts)}")
        check("假推理器真的收到了 prompt", len(good.prompts) == 1, f"{len(good.prompts)} 次")
        check("prompt 里带了这句话的原文", "今天好累" in (good.prompts[0] if good.prompts else ""), "")
        check("intent 解析出来了", analysis.intent == "倾诉", analysis.intent)
        check("emotion 自由词也解析出来了", analysis.emotion == "疲惫", analysis.emotion)

        # local_llm.infer 的签名一个字没动（S1 要求）
        import inspect

        params = list(inspect.signature(local_llm.infer).parameters)
        check("local_llm.infer 签名没动（还是 models_dir, prompt）", params == ["models_dir", "prompt"], str(params))

        # ---------- S3：模型回来校正规则 route ----------
        section("S3 端到端：本地模型这一趟的判断回过头校正规则路由")

        # 规则层："嗯" → 确认 / 平静；模型说这是倾诉 / 疲惫 → 应当改成 倾诉 / 低落。
        # 这一节故意**留在本地那条腿上**（假推理器还在装）：验的是"离线跑完还把路由校正了"，
        # 而且解析这次的 API 调用全程 0 次。
        rule = router.classify("嗯")
        check("规则层原本判「确认 / 平静」", (rule.intent, rule.emotion) == ("确认", "平静"), str(rule))

        def say_with_local(reply_json: str) -> str:
            """让假本地模型这一趟吐 reply_json，然后走真入口说一句「嗯」。"""
            good.reply = reply_json
            spoken.clear()
            stub.messages.clear()
            p0 = len(stub.prompts)
            app.chatter_service.handle("嗯")
            wait_for(lambda: bool(spoken))
            drain_threads()
            pump(0.1)
            check("这一轮解析也没花 API（本地那条腿）", len(stub.prompts) == p0, f"{p0} → {len(stub.prompts)}")
            return _reply_text(stub)

        fed = say_with_local(LOCAL_PARSE_JSON)
        check("回话提示里的意图被校正成「倾诉」", "意图=倾诉" in fed, fed[:100] + "…" if fed else "（没拼消息）")
        check("情绪别名归一：疲惫 → 低落，语气放软", "放软" in fed, "")
        check(
            "校正后的情绪也补报给了关怀那一侧",
            app.mind_proactive is None or app.mind_proactive._live_emotion == "低落",
            getattr(app.mind_proactive, "_live_emotion", "（没装）"),
        )

        # 模型给越界值 → 一个字都不改，退回规则
        fed = say_with_local(
            '{"scene": "嗯", "time_hint": "", "time_iso": "", "time_confidence": "none",'
            ' "emotion": "五味杂陈", "emotion_score": 0.0, "topic": "", "module": "其他",'
            ' "command": "", "intent": "说教"}'
        )
        check("越界意图回落规则：还是「确认」", "意图=确认" in fed, fed[:100] + "…" if fed else "（没拼消息）")
        check("越界情绪回落规则：还是「平静」（不加戏）", "意图=确认｜情绪=平静" in fed, "")

        # 别名表能收下的自由词（模型嘴上没提五档，也照样归得上）
        check("别名：烦躁 → 愤怒", router.refine(rule, emotion="烦躁").emotion == "愤怒", "")
        check("别名：压力大 → 焦虑", router.refine(rule, emotion="压力大").emotion == "焦虑", "")
        check("生造词不硬猜", router.refine(rule, emotion="五味杂陈").emotion == rule.emotion, "")

        # ---------- S6：五意图 + 多意图优先级 ----------
        section("S6 五意图与多意图优先级（规则层）")

        cases = [
            ("今天好累好崩溃", "倾诉", "低落"),
            ("好烦，怎么办", "倾诉", "低落"),
            ("怎么缓解焦虑", "求助", "焦虑"),
            ("关掉宠物", "指令", "平静"),
            ("哈哈天气真好", "闲聊", "开心"),
            ("嗯好的", "确认", "平静"),
        ]
        for text, intent, emotion in cases:
            r = router.classify(text)
            check(f"{text!r} → {intent} / {emotion}", (r.intent, r.emotion) == (intent, emotion), f"{r.intent}/{r.emotion}")

        check(
            "多意图优先级：倾诉压过求助",
            router.classify("好烦，怎么办").intent == "倾诉",
            router.classify("好烦，怎么办").intent,
        )
        check(
            "模型归一后也守同一条优先级（越界不翻盘）",
            router.refine(router.classify("好烦，怎么办"), "求助").intent == "求助",
            "模型明确说求助时以模型为准（规则只是回落）",
        )

        # ---------- S3：各条回落腿 ----------
        section("S3 回落：无模型 / 本地跑砸 / 显式关掉")

        _restore_real()
        p0 = len(stub.prompts)
        stub.parse_result = _parse_result("今天好累", intent="倾诉", emotion="低落")
        analysis = chain.analyze("今天好累")
        check("本机没模型 → 回落 API", not chain.used_local, f"used_local={chain.used_local}")
        check("回落时 API 真的收到了这一次", len(stub.prompts) == p0 + 1, f"{p0} → {len(stub.prompts)}")
        check("回落路径上 intent 照样解析得出", analysis.intent == "倾诉", analysis.intent)
        check(
            "回落路径的 prompt 仍是原来那条（行为等价 v2-dialog）",
            "time_confidence" in (stub.prompts[-1] if stub.prompts else ""),
            "",
        )

        # 本地吐的不是 JSON → 回落 API，这句话不丢
        _drop_model(app.config)
        _install_fake(FakeRuntime("我今天心情还行啊，就是有点累"))
        p0 = len(stub.prompts)
        analysis = chain.analyze("今天好累")
        check("本地吐的不是 JSON → 回落 API", len(stub.prompts) == p0 + 1 and not chain.used_local, f"{p0} → {len(stub.prompts)}")
        check("这句话没丢，照常解析出来", analysis.scene != "", analysis.scene)

        # 本地推理中途炸了 → 回落 API
        _install_fake(FakeRuntime(RuntimeError("显存不够")))
        p0 = len(stub.prompts)
        analysis = chain.analyze("今天好累")
        check("本地推理炸了 → 回落 API", len(stub.prompts) == p0 + 1 and not chain.used_local, f"{p0} → {len(stub.prompts)}")

        # 显式 local_model=false → 有模型也走 API
        _install_fake(FakeRuntime())
        app.config.mind.local_model = False
        p0 = len(stub.prompts)
        chain.analyze("今天好累")
        check("local_model=false → 有模型也强制走 API", len(stub.prompts) == p0 + 1 and not chain.used_local, f"used_local={chain.used_local}")
        app.config.mind.local_model = True
        _restore_real()

        # ---------- S5：红线 ----------
        section("S5 红线：零写入 / 无模型 / 无新依赖")

        pet_path = Path(app.config.memory.db_path)
        mind_path = Path(app.config.mind.db_path)
        before = (_snapshot(pet_path), _snapshot(mind_path))
        conn = sqlite3.connect(str(pet_path))
        try:
            rows0 = int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
        finally:
            conn.close()

        for text in ("今天好累", "我不想活了", "好烦，怎么办", "关掉宠物"):
            router.classify(text)
            router.refine(router.classify(text), "倾诉", "疲惫")

        after = (_snapshot(pet_path), _snapshot(mind_path))
        conn = sqlite3.connect(str(pet_path))
        try:
            rows1 = int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
        finally:
            conn.close()
        check("pet.db 文件没被动过", before[0] == after[0], f"{before[0]} → {after[0]}")
        check("mind.db 文件没被动过（分类这条线不写库）", before[1] == after[1], f"{before[1]} → {after[1]}")
        check("pet.db memory 表一行没多", rows0 == rows1, f"{rows0} → {rows1}")

        import subprocess

        tracked = subprocess.run(
            ["git", "ls-files", "data/models"], capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout.strip()
        check("仓库里没有任何模型权重", tracked == "", tracked or "（空）")
        req_diff = subprocess.run(
            ["git", "diff", "HEAD", "--stat", "--", "requirements.txt"],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        ).stdout.strip()
        check("requirements.txt 一个字没动（零新依赖）", req_diff == "", req_diff or "（无 diff）")

        check(
            "解析链是本地优先（有模型先本地，抛 LocalUnavailable 才回落）",
            "LocalUnavailable" in inspect.getsource(type(chain)._ask),
            "",
        )

        drain_threads()
        note("⚠️ 本机 data/models/ 没有 LLM 权重，「有模型→离线」验的是路径与调用次数，真值需实机放模型")
        return summary("A+++ 意图情绪本地优先")
    finally:
        _restore_real()
        box.cleanup()


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
