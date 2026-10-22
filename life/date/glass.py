"""乐奇 GLASS 通道（v2-glass-channel）：应用层 JSON 信封 + 手机端传输抽象。

**只读确认（2026-10-22）**：Rokid（乐奇）Glass3 有公开的手机端 / 眼镜端 SDK 文档
（x-docs.rokid.com）。手机端是 Android/Java 库，公开接口包括 `IBleClient`、
`IClassicBluetoothClient`、`IBleMessageListener` / `IClassicBTMessageListener`，
可做 BLE 扫描/连接与文本消息收发。**本仓库是 Python 桌面端，没有可直接调用的
Python SDK**；GATT UUID、鉴权字段等细节没有拿到，**不臆测**。

所以这一层只定义两样东西：

1. **我们自己的应用层 JSON 信封**（见 `glass_protocol.md`）——不是 Rokid 的线协议；
2. **手机端传输接口 `GlassTransport`**——Android 手机端按官方 SDK 实现它
   （TODO），桌面端只负责组包 / 解包 / 失败降级。

红线：信封里永远不放原始帧、原始音频、截图或任何二进制；只走结构化提示与文字。
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol

from life.cards import date_rules as dr

logger = logging.getLogger(__name__)

PROTOCOL_VERSION = 1
MAX_MESSAGE_BYTES = 4096          # BLE 单包友好；超了就拒绝，不偷偷分片
MAX_ID_LEN = 64

TYPE_COACH = "coach"              # 服务端 → 眼镜：教练提示
TYPE_CONTEXT = "context"          # 手机端 → 服务端：对象卡 / 阶段 / 信号
TYPE_ACK = "ack"
TYPE_ERROR = "error"
TYPE_HEALTH = "health"
TYPES: tuple[str, ...] = (TYPE_COACH, TYPE_CONTEXT, TYPE_ACK, TYPE_ERROR, TYPE_HEALTH)

REQUIRED_PAYLOAD: dict[str, tuple[str, ...]] = {
    TYPE_COACH: ("suggestion",),
    TYPE_CONTEXT: ("session_id",),
    TYPE_ACK: ("ref",),
    TYPE_ERROR: ("message",),
    TYPE_HEALTH: (),
}

ENVELOPE_FIELDS = ("v", "type", "id", "ts", "payload")

# 这些键一旦出现就拒收：传输层只走结构化数据
RAW_KEYS = frozenset({
    "frames", "frame", "audio", "pcm", "image", "images", "video",
    "screenshot", "raw", "bytes", "blob",
})

COACH_FIELDS = (
    "stage", "stage_name", "suggestion", "can_test", "test",
    "taboos", "next_hint", "coach_only", "speak_for_user", "emotion", "trace",
)

# 与 `Card` 模型对齐（不把内部字段带出；名字可改，peer_name 是归属键）
GLASS_CARD_FIELDS = (
    "name", "peer_name", "title", "theme", "summary", "goal",
    "likes", "dislikes", "strategy", "relation_stage", "tags",
    "interests", "emotion_state", "anxiety", "age",
)

_SIGNAL_KEYS = set(dr.SIGNAL_KEYS) | {"elapsed_minutes"}


class GlassProtocolError(ValueError):
    """信封不合规（版本/类型/字段/体积/原始数据）。"""


def _reject_raw(value: Any, path: str = "") -> None:
    """递归拒收原始媒体键与二进制值。"""
    if isinstance(value, Mapping):
        for key, item in value.items():
            name = str(key)
            if name.strip().lower() in RAW_KEYS:
                raise GlassProtocolError(f"{path}{name} 不允许走眼镜通道（只传结构化数据）")
            _reject_raw(item, f"{path}{name}.")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_raw(item, f"{path}{index}.")
        return
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise GlassProtocolError(f"{path or 'payload'} 是二进制，不允许走眼镜通道")


def _json_safe(value: Any, depth: int = 0) -> Any:
    """收成 JSON 标量/容器；不认识的类型直接丢掉（不会把对象带出去）。"""
    if depth > 4:
        return None
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {
            str(key): cleaned
            for key, item in value.items()
            for cleaned in (_json_safe(item, depth + 1),)
            if cleaned is not None
        }
    if isinstance(value, (list, tuple)):
        return [item for item in (_json_safe(v, depth + 1) for v in value)
                if item is not None]
    return None


@dataclass(frozen=True)
class Envelope:
    """应用层信封：`{v, type, id, ts, payload}`。"""

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    ts: float = 0.0
    v: int = PROTOCOL_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "v": int(self.v),
            "type": str(self.type),
            "id": str(self.id),
            "ts": float(self.ts),
            "payload": dict(self.payload),
        }

    def validate(self) -> None:
        if isinstance(self.v, bool) or int(self.v) != PROTOCOL_VERSION:
            raise GlassProtocolError(f"协议版本不认识：{self.v}")
        if not isinstance(self.type, str) or self.type not in TYPES:
            raise GlassProtocolError(f"信封类型不认识：{self.type}")
        if not isinstance(self.id, str) or not self.id or len(self.id) > MAX_ID_LEN:
            raise GlassProtocolError("id 必须是 1~64 字符的字符串")
        if isinstance(self.ts, bool) or not isinstance(self.ts, (int, float)):
            raise GlassProtocolError("ts 必须是数字")
        if not isinstance(self.payload, Mapping):
            raise GlassProtocolError("payload 必须是对象")
        _reject_raw(self.payload, "payload.")
        for key in REQUIRED_PAYLOAD.get(self.type, ()):
            value = self.payload.get(key)
            if value is None or (isinstance(value, str) and not value.strip()):
                raise GlassProtocolError(f"{self.type} 信封缺少 {key}")

    def encode(self) -> bytes:
        self.validate()
        try:
            raw = json.dumps(self.as_dict(), ensure_ascii=False,
                             separators=(",", ":")).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise GlassProtocolError(f"payload 不能 JSON 序列化：{exc}") from exc
        if len(raw) > MAX_MESSAGE_BYTES:
            raise GlassProtocolError(
                f"信封 {len(raw)} 字节，超过 {MAX_MESSAGE_BYTES} 上限"
            )
        return raw

    @classmethod
    def decode(cls, raw: Any) -> "Envelope":
        if isinstance(raw, str):
            data = raw.encode("utf-8")
        elif isinstance(raw, (bytes, bytearray, memoryview)):
            data = bytes(raw)
        else:
            raise GlassProtocolError("只接受 bytes / str")
        if len(data) > MAX_MESSAGE_BYTES:
            raise GlassProtocolError(
                f"信封 {len(data)} 字节，超过 {MAX_MESSAGE_BYTES} 上限"
            )
        try:
            body = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise GlassProtocolError(f"不是合法 UTF-8 JSON：{exc}") from exc
        if not isinstance(body, Mapping):
            raise GlassProtocolError("信封顶层必须是对象")
        unknown = set(body) - set(ENVELOPE_FIELDS)
        if unknown:
            raise GlassProtocolError(f"信封有未知字段：{sorted(unknown)}")
        missing = [key for key in ENVELOPE_FIELDS if key not in body]
        if missing:
            raise GlassProtocolError(f"信封缺字段：{missing}")
        if not isinstance(body.get("type"), str) or not isinstance(body.get("id"), str):
            raise GlassProtocolError("type / id 必须是字符串")
        payload = body.get("payload")
        if not isinstance(payload, Mapping):
            raise GlassProtocolError("payload 必须是对象")
        envelope = cls(
            v=body.get("v"),
            type=body.get("type"),
            id=body.get("id"),
            ts=body.get("ts"),
            payload=dict(payload),
        )
        envelope.validate()
        return envelope


class GlassTransport(Protocol):
    """手机端要实现的传输接口（Android 按官方 SDK 实现，见 TODO）。"""

    name: str

    def available(self) -> bool: ...

    def send(self, data: bytes) -> bool: ...

    def recv(self) -> bytes | None: ...

    def close(self) -> None: ...


class NullTransport:
    """没接手机端时的默认空实现：永远发不出去，但也不抛异常。"""

    name = "unbound"

    def __init__(self, reason: str = "手机端通道未接（没有可用的 GlassTransport）"):
        self.reason = reason

    def available(self) -> bool:
        return False

    def send(self, data: bytes) -> bool:
        return False

    def recv(self) -> bytes | None:
        return None

    def close(self) -> None:
        return None


class LoopbackTransport:
    """测试 / 本机联调用：outbox 收发出的字节，inbox 喂收到的字节。"""

    name = "loopback"

    def __init__(self, inbox: list[bytes] | None = None):
        self.outbox: list[bytes] = []
        self.inbox: list[bytes] = list(inbox or [])

    def available(self) -> bool:
        return True

    def send(self, data: bytes) -> bool:
        self.outbox.append(bytes(data))
        return True

    def recv(self) -> bytes | None:
        return self.inbox.pop(0) if self.inbox else None

    def close(self) -> None:
        return None


def _post_bytes(endpoint: str, body: bytes, headers: Mapping[str, str],
                timeout: float) -> tuple[int, bytes]:
    """标准库 POST，给 HTTP 中继用。**单独抽出来是为了测试能换掉它。**"""
    from urllib.request import Request, urlopen

    request = Request(
        endpoint, data=body,
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
        method="POST",
    )
    with urlopen(request, timeout=timeout) as response:
        return int(getattr(response, "status", 200)), response.read()


class HttpTransport:
    """JSON-over-HTTP 中继（手机端或网关提供）。只发不收；收流 TODO 长轮询/WebSocket。"""

    name = "http-relay"

    def __init__(self, endpoint: str = "", token: str = "",
                 post: Any = None, timeout: float = 5.0):
        self._endpoint = str(endpoint or "").strip()
        self._token = str(token or "").strip()
        self._post = post
        self._timeout = float(timeout)

    def available(self) -> bool:
        return bool(self._endpoint)

    def send(self, data: bytes) -> bool:
        if not self.available():
            return False
        headers = {"Authorization": f"Bearer {self._token}"} if self._token else {}
        try:
            if self._post is not None:
                result = self._post(self._endpoint, data, headers, self._timeout)
            else:
                result = _post_bytes(self._endpoint, data, headers, self._timeout)
        except Exception as exc:      # noqa: BLE001 —— 发不出去只算失败，不抛
            logger.info("眼镜 HTTP 中继没发出去：%s", exc)
            return False
        if isinstance(result, bool):
            return result
        if isinstance(result, tuple) and result:
            try:
                return 200 <= int(result[0]) < 300
            except (TypeError, ValueError):
                return False
        return bool(result)

    def recv(self) -> bytes | None:
        return None

    def close(self) -> None:
        return None


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


class GlassChannel:
    """组包 / 发 / 收 / 失败降级。不保存任何原始媒体。"""

    def __init__(self, transport: GlassTransport | None = None):
        self._transport = transport if transport is not None else NullTransport()

    @property
    def available(self) -> bool:
        try:
            return bool(self._transport.available())
        except Exception:      # noqa: BLE001
            return False

    @property
    def transport_name(self) -> str:
        return str(getattr(self._transport, "name", "") or "")

    def describe(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "transport": self.transport_name,
            "protocol": PROTOCOL_VERSION,
            "max_bytes": MAX_MESSAGE_BYTES,
            "reason": "" if self.available else "没有可用的手机端传输",
        }

    def _send(self, envelope: Envelope) -> dict[str, Any]:
        if not self.available:
            return {"ok": False, "id": envelope.id, "bytes": 0,
                    "reason": "手机端通道未接"}
        try:
            raw = envelope.encode()
        except GlassProtocolError as exc:
            return {"ok": False, "id": envelope.id, "bytes": 0, "reason": str(exc)}
        try:
            ok = bool(self._transport.send(raw))
        except Exception as exc:      # noqa: BLE001 —— 传输失败不崩
            logger.info("眼镜通道发送失败：%s", exc)
            ok = False
        return {
            "ok": ok,
            "id": envelope.id,
            "bytes": len(raw),
            "reason": "" if ok else "传输层没发出去",
        }

    def send_coach(self, prompt: Mapping[str, Any], session_id: str = "",
                   message_id: str | None = None) -> dict[str, Any]:
        """服务端 → 眼镜：只发生成好的教练提示，不带 state / 原始数据。"""
        source = prompt if isinstance(prompt, Mapping) else {}
        payload = {key: _json_safe(source[key]) for key in COACH_FIELDS
                   if source.get(key) is not None}
        payload["session_id"] = str(session_id or source.get("session_id") or "")
        payload["coach_only"] = True
        payload["speak_for_user"] = False
        return self._send(Envelope(type=TYPE_COACH, payload=payload,
                                   id=message_id or _new_id()))

    def send_context(self, session_id: str, card: Mapping[str, Any] | None = None,
                     stage: Any = None, signals: Mapping[str, Any] | None = None,
                     message_id: str | None = None) -> dict[str, Any]:
        """手机端 → 服务端：对象卡 / 阶段 / 信号。卡只带 Card 已有的列。"""
        card_payload: dict[str, Any] = {}
        if isinstance(card, Mapping):
            for key in GLASS_CARD_FIELDS:
                value = _json_safe(card.get(key))
                if value in (None, ""):
                    continue
                if key == "tags" and isinstance(value, list):
                    value = ",".join(str(item) for item in value if item)
                card_payload[key] = value
        signal_payload = {}
        if isinstance(signals, Mapping):
            for key, value in signals.items():
                if key in _SIGNAL_KEYS:
                    cleaned = _json_safe(value)
                    if cleaned is not None:
                        signal_payload[key] = cleaned
        payload = {
            "session_id": str(session_id or ""),
            "stage": dr.normalize_stage(stage) if stage is not None else None,
            "signals": signal_payload,
            "card": card_payload,
        }
        payload = {key: value for key, value in payload.items() if value is not None}
        return self._send(Envelope(type=TYPE_CONTEXT, payload=payload,
                                   id=message_id or _new_id()))

    def send_ack(self, ref: str, message_id: str | None = None) -> dict[str, Any]:
        return self._send(Envelope(type=TYPE_ACK, payload={"ref": str(ref or "")},
                                   id=message_id or _new_id()))

    def send_error(self, message: str, ref: str = "",
                   message_id: str | None = None) -> dict[str, Any]:
        payload = {"message": str(message or "")}
        if ref:
            payload["ref"] = str(ref)
        return self._send(Envelope(type=TYPE_ERROR, payload=payload,
                                   id=message_id or _new_id()))

    def receive(self, raw: Any = None) -> Envelope | None:
        """收一包。没给 raw 就从传输层拉；没有 / 不合规就抛 GlassProtocolError。"""
        data = raw
        if data is None and self.available:
            try:
                data = self._transport.recv()
            except Exception as exc:      # noqa: BLE001
                raise GlassProtocolError(f"传输层收包失败：{exc}") from exc
        if data is None:
            return None
        return Envelope.decode(data)

    def close(self) -> None:
        close = getattr(self._transport, "close", None)
        if callable(close):
            close()
        self._transport = NullTransport()


__all__ = [
    "PROTOCOL_VERSION",
    "MAX_MESSAGE_BYTES",
    "TYPES",
    "TYPE_COACH",
    "TYPE_CONTEXT",
    "TYPE_ACK",
    "TYPE_ERROR",
    "TYPE_HEALTH",
    "RAW_KEYS",
    "COACH_FIELDS",
    "GLASS_CARD_FIELDS",
    "GlassProtocolError",
    "Envelope",
    "GlassTransport",
    "NullTransport",
    "LoopbackTransport",
    "HttpTransport",
    "GlassChannel",
]
