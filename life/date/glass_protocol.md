# 乐奇 GLASS 通道协议（v2-glass-channel）

这是**我们自己的应用层协议**，不是 Rokid 的线协议。桌面端与 Android 手机端之间
按这个信封收发；手机端再按 Rokid 官方 SDK 把字节送到眼镜。

## 只读确认（2026-10-22）

- Rokid（乐奇）Glass3 有公开的手机端 / 眼镜端 SDK 文档（`x-docs.rokid.com`）。
- 手机端公开接口包括 `IBleClient`、`IClassicBluetoothClient`、
  `IBleMessageListener` / `IClassicBTMessageListener`，可做 BLE 扫描/连接与文本消息收发。
- 官方 SDK 是 Android/Java 库；本仓库是 Python 桌面端，**没有可调用的 Python SDK**。
- GATT UUID、鉴权字段、经典蓝牙配对细节**没有拿到，不臆测**。

## 信封

UTF-8 JSON，单包不超过 **4096 字节**（BLE 友好）：

```json
{
  "v": 1,
  "type": "coach",
  "id": "7f3a9c2d1e40",
  "ts": 1760000000.0,
  "payload": {
    "session_id": "phone-1",
    "stage": "icebreak",
    "stage_name": "破冰",
    "suggestion": "……",
    "can_test": false,
    "test": null,
    "taboos": ["……"],
    "next_hint": "……",
    "coach_only": true,
    "speak_for_user": false
  }
}
```

字段：`v`（目前只能是 1）、`type`、`id`（1~64 字符）、`ts`（数字）、`payload`（对象）。
未知顶层字段、缺字段、超长、非 UTF-8 JSON 一律拒收。

类型：

| type | 方向 | 必填 payload |
|---|---|---|
| `coach` | 服务端 → 眼镜 | `suggestion` |
| `context` | 手机端 → 服务端 | `session_id` |
| `ack` | 双向 | `ref` |
| `error` | 双向 | `message` |
| `health` | 双向 | 无 |

## 原始媒体不走这条通道

`frames` / `frame` / `audio` / `pcm` / `image` / `video` / `screenshot` / `raw` /
`bytes` / `blob` 这些键（任意层级）和任何二进制值都会被 `Envelope` 拒收。
通道只走结构化特征、文字与教练提示。

## 传输抽象

桌面端只依赖 `GlassTransport`：

```python
class GlassTransport(Protocol):
    name: str
    def available(self) -> bool: ...
    def send(self, data: bytes) -> bool: ...
    def recv(self) -> bytes | None: ...
    def close(self) -> None: ...
```

已提供：

- `NullTransport`：默认空实现，发不出去但不崩；
- `LoopbackTransport`：本机联调 / 测试；
- `HttpTransport`：JSON-over-HTTP 中继（只发；收流待补）。

## TODO（手机端按官方 SDK 实现）

1. Android 手机端实现 `GlassTransport`：BLE 扫描 → 连接 → 用官方 `IBleClient`
   发 `sendTextMessage`，用 `IBleMessageListener` 收回包；经典蓝牙走
   `IClassicBluetoothClient` / `IClassicBTMessageListener` 作为备选。
2. 鉴权：手机端拿到官方要求的凭据后，在桌面端 `HttpTransport(token=...)` 或
   手机本地通道里加；**不猜字段名**。
3. `HttpTransport.recv()`：补长轮询或 WebSocket；当前只发不收。
4. 分片：4096 字节以上的提示先在服务端截短（提示本身就是一句话级别），不在这层偷偷分片。

## 失败口径

- 通道没接 / 传输失败 / 信封不合规 → `send_*()` 返回 `{ok: False, reason: ...}`，不抛；
- 收到的包不合规 → `receive()` 抛 `GlassProtocolError`，调用方记一笔、丢这一包。
