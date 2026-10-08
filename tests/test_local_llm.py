"""本地小模型（A++ 本地优先）的自动测试。

覆盖：
- S1 探测：什么目录才算"本机有模型"（gguf / HF 目录算，bge 那个向量目录不算）；
- S1 降级：装不出后端时 load() 回 None、infer() 抛 LocalUnavailable，不炸线；
- S2/S3 切换：有模型 → 提炼在本地算、桩模型（= API）一个字节都没收到；无模型 →
  回落桩模型，落库结果跟以前一模一样；
- S3 稳健：本地吐的不是 JSON / 推理中途炸了 → 都回落 API，**这批对话不丢**；
- S4 配置：显式 local_model=false → 有模型也强制走 API；
- S5 红线：pet.db 零写入、仓库无模型文件、没新增依赖。

真跑一个本地模型不在测试范围内（本机不一定装了 llama-cpp-python，也不该为跑测试
下载几百 MB 权重）。这里把**推理器**换成假的，但"谁来问、问完落哪儿"走的是真实
链路：distill → _ask → 落 mind.db。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, drain_threads, make_env, note, pump, section, summary, wait_for

from life.mind import local_llm

# 真实现先攥在手里：下面会临时把 _build_runtime 换成假的，收尾要还回去
_REAL_BUILD = local_llm._build_runtime

# 一顿正经的提炼结果，反复用
GOOD_JSON = (
    '{"rel_state": "和妈妈最近有点僵", "emotion_snapshot": "低落，因为跟妈妈吵架",'
    ' "events": ["和妈妈吵了一架"],'
    ' "fragments": [{"text": "想买新能源车，正在看比亚迪", "tags": ["买车"]}],'
    ' "profile": {"intro": "一个爱琢磨的人"}}'
)


class FakeRuntime:
    """假推理器：complete() 回预先摆好的文本（也可以是异常）。"""

    def __init__(self, reply: str | Exception = GOOD_JSON):
        self.reply = reply
        self.prompts: list[str] = []

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.reply, Exception):
            raise self.reply
        return self.reply


def _memory_rows(pet_path: Path) -> int:
    conn = sqlite3.connect(str(pet_path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM memory").fetchone()[0])
    finally:
        conn.close()


def say(app, stub, text: str) -> None:
    """走真实对话链路说一句（跟 test_mind.py 同一套路）。"""
    stub.parse_result = {
        "scene": text[:12], "time_hint": "", "time_iso": "", "time_confidence": "none",
        "emotion": "平静", "module": "其他", "command": "", "reply": "嗯嗯",
    }
    app.chatter_service.handle(text)
    wait_for(lambda: app.mind_distiller.pending > 0, timeout=3.0)


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


def main() -> int:
    import shutil
    import tempfile

    section("S1 探测：什么目录才算'本机有模型'")
    tmp = Path(tempfile.mkdtemp(prefix="pet_models_"))
    try:
        empty = tmp / "empty"
        empty.mkdir()
        check("空目录 → 没有模型", local_llm.find_model(empty) is None, "")

        mixed = tmp / "mixed"
        mixed.mkdir()
        # bge 那个向量目录的翻版：有 config.json，但权重是 .onnx —— 不能被当成 LLM
        bge = mixed / "bge-small-zh-v1.5"
        bge.mkdir()
        (bge / "config.json").write_text("{}", encoding="utf-8")
        (bge / "model.onnx").write_bytes(b"")
        check("bge 那种只有 .onnx 的目录不算 LLM", local_llm.find_model(mixed) is None, "")

        hf = mixed / "qwen2.5-1.5b-instruct"
        hf.mkdir()
        (hf / "config.json").write_text("{}", encoding="utf-8")
        (hf / "model.safetensors").write_bytes(b"")
        check("HF 格式目录（config.json + safetensors）算", local_llm.find_model(mixed) == hf, str(hf.name))

        only_gguf = tmp / "only_gguf"
        only_gguf.mkdir()
        other = only_gguf / "llama-3.2-1b-q4.gguf"
        other.write_bytes(b"")
        check("gguf 认得出", local_llm.find_model(only_gguf) == other, other.name)

        both = tmp / "both"
        both.mkdir()
        plain = both / "some-other-1.5b.gguf"
        plain.write_bytes(b"")
        qwen = both / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
        qwen.write_bytes(b"")
        check("好几个 gguf 时优先挑 qwen 的", local_llm.find_model(both) == qwen, qwen.name)

        nested = tmp / "nested" / "sub"
        nested.mkdir(parents=True)
        deep = nested / "qwen2.5-1.5b-q4.gguf"
        deep.write_bytes(b"")
        check("模型放在子目录里也翻得到", local_llm.find_model(tmp / "nested") == deep, "")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    section("S1 降级：装不出后端就回 None，不炸")
    tmp2 = Path(tempfile.mkdtemp(prefix="pet_models_"))
    try:
        gguf = tmp2 / "qwen2.5-0.5b-instruct-q4_k_m.gguf"
        gguf.write_bytes(b"")
        # 真调一次真实现：本机装没装 llama-cpp-python 都行，**只要不抛异常**
        real = local_llm._build_runtime(gguf)
        note(f"本机真实现的结果：{'建起来了' if real is not None else '回落 API（没装后端）'}")
        check("真实现遇到真权重不抛异常", True, type(real).__name__ if real is not None else "None")

        local_llm.reset()
        local_llm._build_runtime = lambda path: None       # 模拟"没装后端"
        check("没后端时 load() 回 None", local_llm.load(tmp2) is None, "")
        check("没后端时 available() 是 False", local_llm.available(tmp2) is False, "")
        raised = False
        try:
            local_llm.infer(tmp2, "随便问问")
        except local_llm.LocalUnavailable:
            raised = True
        check("没后端时 infer() 抛 LocalUnavailable（调用方据此回落）", raised, "")
    finally:
        local_llm.reset()
        local_llm._build_runtime = _REAL_BUILD  # type: ignore[assignment]
        shutil.rmtree(tmp2, ignore_errors=True)

    section("S1 解析：小模型爱带围栏和废话")
    check("纯 JSON 认得出", local_llm.parse_json(GOOD_JSON).get("rel_state") == "和妈妈最近有点僵", "")
    fenced = local_llm.parse_json('```json\n{"rel_state": "还好"}\n```')
    check("```json 围栏撕得掉", fenced.get("rel_state") == "还好", str(fenced))
    chatty = local_llm.parse_json('好的，我整理一下：\n{"rel_state": "还行"}\n希望有帮助！')
    check("前后带废话也抠得出", chatty.get("rel_state") == "还行", str(chatty))
    bad = False
    try:
        local_llm.parse_json("今天天气不错")
    except ValueError:
        bad = True
    check("一句 JSON 都没有 → ValueError（调用方据此回落）", bad, "")

    # ---------- 接进 distill 的真实链路 ----------
    box, stub, app = make_env()
    try:
        distiller = app.mind_distiller
        pet_path = app.config.db_path
        model_file = _drop_model(app.config)

        section("S3 有模型 → 提炼在本地算，API（桩）一个字节没收到")
        fake = FakeRuntime(GOOD_JSON)
        _install_fake(fake)
        stub.prompts.clear()
        say(app, stub, "我跟我妈吵架了，说我失恋的事")
        stub.mind_result = {"rel_state": "不该走这边", "fragments": [], "events": [], "profile": {}}
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)

        # A+++ 之后本机模型不只提炼那条线在用：每句话入口的解析链（intent/emotion）
        # 走的是同一个 runtime。所以这一轮它被问了两次——解析一次、提炼一次。
        # 断言按"谁问的"拆开，别只数个数：数字变了不代表线断了。
        distill_prompts = [p for p in fake.prompts if "rel_state" in p]
        check("本地推理器被问了两次（入口解析 + 睡前提炼）", len(fake.prompts) == 2, f"{len(fake.prompts)} 次")
        check("其中一次是提炼那条线", len(distill_prompts) == 1, f"提炼 {len(distill_prompts)} 次")
        check("提炼问的 prompt 里有那句对话原文", "失恋" in (distill_prompts[0] if distill_prompts else ""), "")
        check("标成本地跑的", distiller.used_local is True, str(distiller.used_local))
        check("API（桩）没收到提炼 prompt", not any("rel_state" in p for p in stub.prompts),
              f"桩收到 {len(stub.prompts)} 条")
        ep = app.mind_store.ep()
        check("本地那份结果正常落库：rel_state", ep["rel_state"] == "和妈妈最近有点僵", ep["rel_state"])
        check("本地那份结果正常落库：emotion_snapshot", "低落" in ep["emotion_snapshot"], ep["emotion_snapshot"])
        check("本地那份结果正常落库：fragment", app.mind_store.fragment_count() >= 1, str(app.mind_store.fragment_count()))

        section("S3 无模型 → 回落 API，行为与以前一致")
        local_llm.reset()
        model_file.unlink()
        stub.prompts.clear()
        say(app, stub, "今天跟朋友聊了聊家常")
        stub.mind_result = {
            "rel_state": "闲聊家常", "emotion_snapshot": "",
            "events": ["聊了一次家常"],
            "fragments": [{"text": "今天下午闲聊了一会儿", "tags": ["家常"]}],
            "profile": {},
        }
        frags_before = app.mind_store.fragment_count()
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)

        check("标成回落的", distiller.used_local is False, str(distiller.used_local))
        check("API（桩）收到了提炼 prompt", any("rel_state" in p for p in stub.prompts), "")
        check("回落那条路照样落库", app.mind_store.fragment_count() == frags_before + 1,
              f"{frags_before} → {app.mind_store.fragment_count()}")
        check("回落那条路的 rel_state 也写进去了", app.mind_store.ep()["rel_state"] == "闲聊家常",
              app.mind_store.ep()["rel_state"])

        section("S3 本地吐的不是 JSON → 回落 API，这批对话不丢")
        _drop_model(app.config)
        bad_runtime = FakeRuntime("今天天气不错，我先歇会儿。")
        _install_fake(bad_runtime)
        stub.prompts.clear()
        say(app, stub, "我最近在琢磨换工作")
        stub.mind_result = {
            "rel_state": "在琢磨换工作", "emotion_snapshot": "",
            "events": ["考虑换工作"], "fragments": [], "profile": {},
        }
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)
        check("本地真被问过了（解析 + 提炼各一次，都吐的不是 JSON）",
              len(bad_runtime.prompts) == 2, f"{len(bad_runtime.prompts)} 次")
        check("吐的不是 JSON → 回落 API", distiller.used_local is False, "")
        check("这批对话没丢（API 接着做完了）", app.mind_store.ep()["rel_state"] == "在琢磨换工作",
              app.mind_store.ep()["rel_state"])

        section("S3 本地推理中途炸了 → 回落 API，也不丢")
        boom = FakeRuntime(RuntimeError("CUDA out of memory"))
        _install_fake(boom)
        stub.prompts.clear()
        say(app, stub, "我打算下个月去趟云南")
        stub.mind_result = {
            "rel_state": "计划去云南", "emotion_snapshot": "",
            "events": ["计划去云南旅行"], "fragments": [], "profile": {},
        }
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)
        check("本地炸了 → 回落 API", distiller.used_local is False, "")
        check("炸了也没丢对话", app.mind_store.ep()["rel_state"] == "计划去云南",
              app.mind_store.ep()["rel_state"])

        section("S4 显式关掉 → 有模型也强制走 API")
        good = FakeRuntime(GOOD_JSON)
        _install_fake(good)
        app.config.mind.local_model = False
        stub.prompts.clear()
        say(app, stub, "今天吃了包子，挺香")
        stub.mind_result = {
            "rel_state": "聊到吃包子", "emotion_snapshot": "",
            "events": [], "fragments": [{"text": "爱吃包子", "tags": ["吃的"]}], "profile": {},
        }
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)
        check("关了之后本地一次都没被问", good.prompts == [], f"{len(good.prompts)} 次")
        check("关了之后走 API", distiller.used_local is False, "")
        check("API 那条路照样落库", app.mind_store.ep()["rel_state"] == "聊到吃包子",
              app.mind_store.ep()["rel_state"])
        app.config.mind.local_model = True

        section("S4 档位：默认开着（按本机探测）、local_ready() 反映本机实况")
        check("默认 local_model 是 True（有模型就用、没有就回落）", app.config.mind.local_model is True, "")
        check("local_ready() 与本机模型目录一致", distiller.local_ready() is local_llm.available(app.config.local_models_dir),
              f"local_ready={distiller.local_ready()}")

        section("S5 红线：pet.db 零写入")
        _drop_model(app.config)
        ok_runtime = FakeRuntime(GOOD_JSON)
        _install_fake(ok_runtime)
        say(app, stub, "今天又聊了会儿，说了些家常")
        drain_threads()
        pump(0.3)
        before_stat = pet_path.stat()
        before_rows = _memory_rows(pet_path)
        distiller.distill_now()
        wait_for(lambda: not distiller._running, timeout=5.0)
        drain_threads()
        pump(0.1)
        after_stat = pet_path.stat()
        check("pet.db 的 memory 表一行没多", _memory_rows(pet_path) == before_rows,
              f"{before_rows} → {_memory_rows(pet_path)}")
        check("提炼这一下没碰 pet.db（size 与 mtime 都没动）",
              after_stat.st_size == before_stat.st_size and after_stat.st_mtime == before_stat.st_mtime,
              f"size {before_stat.st_size}→{after_stat.st_size}")
        check("情感记忆的正文没有落进 pet.db",
              "和妈妈最近有点僵".encode("utf-8") not in pet_path.read_bytes(), "")

        section("S5 红线：仓库无模型文件、无新依赖")
        root = Path(__file__).resolve().parents[1]
        ignore = (root / ".gitignore").read_text(encoding="utf-8")
        check("data/models/ 在忽略清单里", "data/models/" in ignore, "")
        req = (root / "requirements.txt").read_text(encoding="utf-8").lower()
        check("requirements 里没有 llama-cpp / transformers（不新增依赖）",
              "llama" not in req and "transformers" not in req, "")
        check("local_llm 模块级没 import 推理后端（缺了也不报错）",
              "llama_cpp" not in sys.modules and "transformers" not in sys.modules, "")
    finally:
        _restore_real()
        box.cleanup()
        drain_threads()

    return summary("本地小模型")


if __name__ == "__main__":
    sys.exit(1 if main() else 0)
