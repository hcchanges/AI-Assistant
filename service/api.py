"""线下约会推理服务（v2-cloud-service）：把教练链抽成一个可远程调用的 HTTP 入口。

**这一层要解决什么**：眼镜/手机只负责采画面与声音，重活（pose、ASR、Jev 判定、
规则匹配）放到一台可迁移的机器上跑。PC 端原有能力一行不动，`service/` 是**新增的
并行入口**。

**本批只做"壳"**：

- `POST /infer`：收 `{session_id, state}`，或只收 `{frames, audio}` 先确认收到；
  返回教练提示（说什么 / 能否测试升级 / 禁忌）；
- `POST /context`：登记会话的对象卡上下文（字段对齐 `data/models.py::Card`）；
- `GET /healthz`：探活。
- 会话与对象卡**只存内存**：进程重启就没了；落库放到 v2-date-card-writeback。
- **原始帧 / 原始音频不进会话、不进响应**：只记"收到几帧 / 有没有音频"，用完即丢。
- 没装 FastAPI 也能跑：自带一个标准库 `ThreadingHTTPServer` 实现；
  装了 FastAPI 时 `create_fastapi_app()` 额外给一个 ASGI 入口。两种入口共用同一套
  `InferenceService`，不复制判定逻辑。

**边界**：本批不接真实模型（pose / ASR 在③④两批），Jev 判定由调用方注入
`judge`（⑤批接上）；没注入时只用批次①的 `date_rules.get_prompt`。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from life.cards import date_rules

logger = logging.getLogger(__name__)

SERVICE_NAME = "date-coach"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000
MAX_BODY_BYTES = 2 * 1024 * 1024        # 单次请求体上限：2MB（预处理后的 state 足够）

# 对象卡上下文只收这些格子——与 `data/models.py::Card` 的字段名对齐，
# 多端（手机 / 眼镜 / PC）拿到的就是同一份卡视图。
CARD_FIELDS: tuple[str, ...] = (
    "name", "peer_name", "title", "theme", "summary", "goal",
    "likes", "dislikes", "strategy", "relation_stage",
    "tags", "interests", "emotion_state", "anxiety", "age",
)


def normalize_card(card: Any) -> dict[str, Any]:
    """只留下与 Card 对齐的那几格；认不出的键丢掉，避免把内部字段带出去。"""
    source = card if isinstance(card, Mapping) else {}
    out: dict[str, Any] = {}
    for key in CARD_FIELDS:
        value = source.get(key)
        if value is None or value == "":
            continue
        if key == "tags" and isinstance(value, (list, tuple)):
            value = ",".join(str(part) for part in value)
        out[key] = value
    return out


def _count_frames(frames: Any) -> int:
    """只数帧数，不保留任何一帧。"""
    if frames is None:
        return 0
    try:
        return len(frames)
    except TypeError:
        return 1


@dataclass
class SessionContext:
    """一个客户端连接的上下文。`session_id` 由客户端给，缺省是 `default`。"""

    session_id: str
    peer: str = ""
    stage: str = date_rules.STAGE_ICEBREAK
    signals: dict[str, Any] = field(default_factory=dict)
    card: dict[str, Any] = field(default_factory=dict)   # 没挂 peer 时的会话内卡
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)


class InferenceService:
    """会话 + 对象卡 + 推理的纯内存实现。HTTP 只是它的一层皮。"""

    def __init__(
        self,
        judge: Callable[[Mapping[str, Any], Mapping[str, Any]], Mapping[str, Any] | None] | None = None,
        prompt_fn: Callable[[Any, Mapping[str, Any] | None], Any] | None = None,
    ) -> None:
        self._judge = judge
        self._prompt_fn = prompt_fn or date_rules.get_prompt
        self._sessions: dict[str, SessionContext] = {}
        # 对象卡按 peer_name 共享：同一对象的手机端与眼镜端看到同一张卡（多端复用）。
        self._cards: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()

    # ---------- 会话 ----------

    def session(self, session_id: Any) -> SessionContext:
        sid = str(session_id or "default").strip() or "default"
        with self._lock:
            if sid not in self._sessions:
                self._sessions[sid] = SessionContext(sid)
            return self._sessions[sid]

    def reset(self, session_id: Any) -> dict[str, Any]:
        sid = str(session_id or "default").strip() or "default"
        with self._lock:
            self._sessions.pop(sid, None)
        return {"ok": True, "session_id": sid}

    def _card_of(self, session: SessionContext) -> dict[str, Any]:
        if session.peer:
            return dict(self._cards.get(session.peer, {}))
        return dict(session.card)

    # ---------- 上下文 ----------

    def set_context(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """登记会话的对象卡 / 阶段 / 信号。只改指到的那一个 session。"""
        session = self.session(payload.get("session_id"))
        card = normalize_card(payload.get("card"))
        peer = str(
            payload.get("peer")
            or card.get("peer_name")
            or session.peer
            or ""
        ).strip()
        with self._lock:
            if peer:
                session.peer = peer
                merged = dict(self._cards.get(peer, {}))
                merged.update({key: value for key, value in card.items() if key != "peer_name"})
                merged["peer_name"] = peer
                self._cards[peer] = merged
            elif card:
                session.card.update(card)
            if payload.get("stage"):
                session.stage = date_rules.normalize_stage(payload.get("stage"))
            if isinstance(payload.get("signals"), Mapping):
                session.signals.update(dict(payload["signals"]))
            session.updated_at = time.time()
        return {
            "ok": True,
            "session_id": session.session_id,
            "peer": session.peer,
            "stage": session.stage,
            "card": self._card_of(session),
        }

    # ---------- 推理 ----------

    def infer(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """一次提示请求。

        输入二选一：
        - `state`：已经预处理好的结构化状态（pose / ASR 的产物，见③④）；
        - `frames` / `audio`：只确认收到并计数，**本批不解析、不保留**。
        """
        if not isinstance(payload, Mapping):
            raise ValueError("请求体必须是 JSON 对象")
        session = self.session(payload.get("session_id"))
        if any(key in payload for key in ("card", "peer", "stage", "signals")):
            self.set_context(payload)

        state = payload.get("state")
        state = dict(state) if isinstance(state, Mapping) else {}
        raw_frames = payload.get("frames")
        raw_audio = payload.get("audio")

        # 从 state 里取信号：既支持 {"signals": {...}}，也支持把信号直接摊在 state 上。
        signals: dict[str, Any] = {}
        inner = state.get("signals")
        if isinstance(inner, Mapping):
            signals.update(dict(inner))
        signals.update({key: value for key, value in state.items()
                        if key in date_rules.SIGNAL_KEYS or key in (
                            "she_extends_topic", "meal_done", "kiss_done",
                            "mutual_yes", "overnight_yes", "silence",
                            "elapsed_minutes",
                        )})
        if isinstance(payload.get("signals"), Mapping):
            signals.update(dict(payload["signals"]))

        card = self._card_of(session)
        verdict: dict[str, Any] = {}
        if self._judge is not None:
            try:
                judged = self._judge(state, card)
                verdict = dict(judged) if isinstance(judged, Mapping) else {}
            except Exception as exc:      # noqa: BLE001 —— 判定后端失败就退回规则
                logger.info("云端推理：judge 没成（%s），退回规则匹配", exc)
                verdict = {}

        stage = verdict.get("stage") or state.get("stage") or session.stage
        snapshot: dict[str, Any] = {}
        snapshot.update(session.signals)
        snapshot.update(signals)
        if isinstance(verdict.get("signals"), Mapping):
            snapshot.update(dict(verdict["signals"]))
        if verdict.get("stage"):
            snapshot.setdefault("stage", verdict.get("stage"))

        prompt = self._prompt_fn(stage, snapshot)
        data = prompt.as_dict() if hasattr(prompt, "as_dict") else dict(prompt)
        session.updated_at = time.time()
        session.stage = date_rules.normalize_stage(data.get("stage") or stage)
        with self._lock:
            session.signals.update(snapshot)
        return {
            **data,
            "session_id": session.session_id,
            "peer": session.peer,
            "card": card,
            "trace": {
                "frames_received": _count_frames(raw_frames),
                "audio_received": raw_audio is not None,
                "state_keys": sorted(state),
                "judged": bool(verdict),
                "judge_stage": verdict.get("stage", ""),
            },
        }

    def health(self) -> dict[str, Any]:
        with self._lock:
            return {
                "ok": True,
                "service": SERVICE_NAME,
                "sessions": len(self._sessions),
                "cards": len(self._cards),
                "fastapi": fastapi_available(),
            }


# ---------- 标准库 HTTP 入口 ----------


def fastapi_available() -> bool:
    try:
        import fastapi  # noqa: F401
    except Exception:      # noqa: BLE001 —— 没装就是没有，不影响标准库入口
        return False
    return True


def _json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False).encode("utf-8")


def make_handler(service: InferenceService):
    """给 ThreadingHTTPServer 用的处理器；把 service 闭包进去，方便测试各起一个。"""

    class Handler(BaseHTTPRequestHandler):
        server_version = "DateCoach/1.0"

        def _send(self, code: int, data: Any) -> None:
            body = _json_bytes(data)
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:                       # noqa: N802 —— stdlib 约定
            path = urlparse(self.path).path
            if path in ("/healthz", "/health", "/"):
                self._send(200, service.health())
            else:
                self._send(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:                      # noqa: N802
            path = urlparse(self.path).path
            if path not in ("/infer", "/context"):
                self._send(404, {"ok": False, "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0 or length > MAX_BODY_BYTES:
                    self._send(400, {"ok": False, "error": "bad content length"})
                    return
                raw = self.rfile.read(length)
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, Mapping):
                    raise ValueError("JSON 顶层必须是对象")
            except (ValueError, UnicodeDecodeError) as exc:
                self._send(400, {"ok": False, "error": f"bad JSON: {exc}"})
                return
            try:
                if path == "/context":
                    self._send(200, service.set_context(payload))
                else:
                    self._send(200, service.infer(payload))
            except ValueError as exc:
                self._send(400, {"ok": False, "error": str(exc)})
            except Exception as exc:      # noqa: BLE001 —— 单次请求失败不拖垮服务
                logger.exception("云端推理请求失败")
                self._send(500, {"ok": False, "error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            logger.debug("service " + fmt, *args)

    return Handler


def make_server(service: InferenceService | None = None,
                host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> ThreadingHTTPServer:
    """建一个标准库 HTTP 服务（不启动）。port=0 时由系统挑空闲端口，供测试用。"""
    svc = service if service is not None else InferenceService()
    server = ThreadingHTTPServer((host, port), make_handler(svc))
    server.service = svc        # 便于测试拿到同一个实例
    return server


def serve(service: InferenceService | None = None,
          host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """前台启动服务（Ctrl+C 结束）。AutoDL / 本机都用这一个入口。"""
    server = make_server(service, host=host, port=port)
    actual_host, actual_port = server.server_address[:2]
    logger.info("date-coach 已启动：http://%s:%s（FastAPI=%s）",
                actual_host, actual_port, fastapi_available())
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("date-coach 收到中断，退出")
    finally:
        server.server_close()


# ---------- 可选 FastAPI 入口 ----------


def create_fastapi_app(service: InferenceService | None = None):
    """装了 FastAPI 才返回 ASGI app；没装返回 None（标准库入口照旧可用）。

    FastAPI 只是同一套 `InferenceService` 的另一层皮，没有另写判定逻辑。
    """
    try:
        from fastapi import FastAPI, Request
    except Exception:      # noqa: BLE001
        return None
    svc = service if service is not None else InferenceService()
    app = FastAPI(title=SERVICE_NAME)

    @app.get("/healthz")
    def healthz():                    # noqa: ANN202 —— FastAPI 自己包装返回值
        return svc.health()

    @app.post("/infer")
    async def infer(request: Request):    # noqa: ANN202
        return svc.infer(await request.json())

    @app.post("/context")
    async def context(request: Request):  # noqa: ANN202
        return svc.set_context(await request.json())

    return app


__all__ = [
    "CARD_FIELDS",
    "DEFAULT_HOST",
    "DEFAULT_PORT",
    "InferenceService",
    "MAX_BODY_BYTES",
    "SERVICE_NAME",
    "SessionContext",
    "create_fastapi_app",
    "fastapi_available",
    "make_server",
    "normalize_card",
    "serve",
]
