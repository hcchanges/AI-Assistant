# -*- coding: utf-8 -*-
"""云端推理服务（v2-cloud-service）的自动测试。

**这一批验什么**：

1. 服务核心能跑：`/infer` 走批次①的规则，返回教练提示；注入 judge 后能换阶段；
2. 会话 / 对象卡上下文隔离：不同 session、不同 peer 不串卡；同 peer 多端共享同一张卡；
3. 原始帧 / 原始音频不保留：只计数、只标记，会话与响应里都没有原始数据；
4. HTTP 入口真能起：标准库服务器在临时端口上跑，GET/POST/坏 JSON 都有正确响应；
5. 没装 FastAPI 也不影响（标准库入口是主路径，FastAPI 只是可选皮）。
"""

from __future__ import annotations

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.cards import date_rules as dr
from service import api
from service.models import load_models


def _post(url: str, payload, timeout: float = 5.0):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def main() -> int:
    section("① 核心推理：规则匹配 + 可选 judge")

    service = api.InferenceService()
    out = service.infer({"session_id": "s1", "state": {"sharing": "high"}})
    check("★ /infer 核心返回教练提示",
          bool(out.get("suggestion")) and out.get("stage") == dr.STAGE_ICEBREAK,
          str(out.get("suggestion"))[:60])
    check("破冰阶段不给升级测试", out.get("can_test") is False and out.get("test") is None,
          str(out.get("can_test")))
    check("结束标志出现时提示可转场", "转场" in out.get("next_hint", ""), out.get("next_hint"))

    def fake_judge(state, card):
        return {
            "stage": dr.STAGE_INTIMATE,
            "signals": {"gaze_away_no_retreat": True, "distance": "close"},
        }

    judged = api.InferenceService(judge=fake_judge).infer(
        {"session_id": "s-judge", "state": {"asr_text": "随便聊"}}
    )
    check("★ 注入 judge 后能换阶段并选出索吻测试",
          judged.get("stage") == dr.STAGE_INTIMATE
          and judged.get("can_test") is True and judged.get("test") == "kiss",
          str({k: judged.get(k) for k in ("stage", "can_test", "test")}))
    check("judge 状态记进 trace", judged.get("trace", {}).get("judged") is True,
          str(judged.get("trace")))

    out = service.infer({
        "session_id": "s2",
        "frames": [b"f1", b"f2", b"f3"],
        "audio": b"raw-audio",
        "state": {"sharing": "low"},
    })
    check("★ 原始帧只计数、原始音频只标记",
          out["trace"]["frames_received"] == 3 and out["trace"]["audio_received"] is True,
          str(out["trace"]))
    check("★ 响应里不夹原始帧/音频",
          "frames" not in out and "audio" not in out, str(sorted(out)))
    stored = service.session("s2")
    check("★ 会话里也不保留原始帧/音频",
          not hasattr(stored, "frames") and not hasattr(stored, "audio"),
          str(sorted(vars(stored))))

    section("② 会话 / 对象卡上下文隔离")

    ctx = service.set_context({
        "session_id": "phone-a",
        "peer": "小A",
        "card": {"name": "小A·攻略卡", "peer_name": "小A", "likes": "咖啡", "secret": "不该带出"},
    })
    check("★ 卡字段按 Card 模型收口（未知字段丢掉）",
          ctx["card"].get("peer_name") == "小A" and "secret" not in ctx["card"],
          str(ctx["card"]))
    service.set_context({
        "session_id": "phone-b",
        "peer": "小B",
        "card": {"peer_name": "小B", "likes": "爬山"},
    })

    a = service.infer({"session_id": "phone-a", "state": {}})
    b = service.infer({"session_id": "phone-b", "state": {}})
    check("★ 不串卡：A 端只看到小A，B 端只看到小B",
          a["peer"] == "小A" and a["card"].get("likes") == "咖啡"
          and b["peer"] == "小B" and b["card"].get("likes") == "爬山",
          f"{a['peer']}/{b['peer']}")

    service.set_context({
        "session_id": "glass-a",
        "peer": "小A",
        "card": {"peer_name": "小A", "strategy": "慢一点"},
    })
    shared = service.infer({"session_id": "glass-a", "state": {}})
    check("★ 同一 peer 多端共享：手机端登记的喜好 + 眼镜端补的策略",
          shared["card"].get("likes") == "咖啡" and shared["card"].get("strategy") == "慢一点",
          str(shared["card"]))
    check("另一张卡完全不受影响",
          service.infer({"session_id": "phone-b", "state": {}})["card"].get("strategy") is None,
          "")

    service.set_context({"session_id": "phone-b", "stage": dr.STAGE_WARMUP})
    check("★ 阶段按 session 隔离",
          service.infer({"session_id": "phone-b", "state": {}})["stage"] == dr.STAGE_WARMUP
          and service.infer({"session_id": "phone-a", "state": {}})["stage"] == dr.STAGE_ICEBREAK,
          "")

    section("③ HTTP 入口：标准库服务器")

    server = api.make_server(service, host="127.0.0.1", port=0)
    port = server.server_address[1]
    base = f"http://127.0.0.1:{port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        status, health = _get(base + "/healthz")
        check("★ GET /healthz 探活", status == 200 and health.get("ok") is True, str(health))
        status, ctx_resp = _post(base + "/context", {
            "session_id": "http-1", "peer": "小C", "card": {"peer_name": "小C"},
        })
        check("★ POST /context 登记对象卡",
              status == 200 and ctx_resp.get("peer") == "小C", str(ctx_resp))
        status, infer_resp = _post(base + "/infer", {
            "session_id": "http-1", "state": {"sharing": "high"},
        })
        check("★ POST /infer 返回文字提示",
              status == 200 and bool(infer_resp.get("suggestion"))
              and infer_resp.get("peer") == "小C",
              str(infer_resp.get("suggestion"))[:40])
        try:
            _post(base + "/infer", b"{not-json")
            bad_json_code = 0
        except urllib.error.HTTPError as exc:
            bad_json_code = exc.code
        check("★ 坏 JSON → 400（不崩服务）", bad_json_code == 400, str(bad_json_code))
        try:
            _post(base + "/nope", {})
            notfound_code = 0
        except urllib.error.HTTPError as exc:
            notfound_code = exc.code
        check("★ 未知路径 → 404", notfound_code == 404, str(notfound_code))
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    section("④ 边界与红线")

    source = Path(api.__file__).read_text(encoding="utf-8")
    check("★ 服务层不落库（无 sqlite / DAO 引用）",
          not any(name in source for name in ("sqlite3", "from data.dao", "import data.dao")),
          "")
    app = api.create_fastapi_app(service)
    check("FastAPI 可选：没装时返回 None，装了才给 ASGI app",
          app is None or hasattr(app, "router"), str(type(app).__name__))
    req = Path(api.__file__).resolve().parent / "requirements.txt"
    req_text = req.read_text(encoding="utf-8")
    check("★ 服务依赖单独放 service/requirements.txt（不动根 requirements）",
          "fastapi" in req_text and "uvicorn" in req_text, "")
    bundle = load_models("不存在的目录")
    check("★ 模型加载入口先留壳：不加载、不崩",
          bundle.loaded is False and bundle.model_dir is None, bundle.describe())

    return summary("云端推理服务（v2-cloud-service）")


if __name__ == "__main__":
    raise SystemExit(main())
