# -*- coding: utf-8 -*-
"""乐奇 GLASS 通道（v2-glass-channel）的自动测试。

**这一批验什么**：

1. 应用层信封：版本 / 类型 / 必填字段 / 体积 / JSON 合法性；
2. 原始媒体（帧 / 音频 / 截图 / bytes）在任意层级都被拒收；
3. 手机端传输抽象：没接不崩、loopback 能真收发、HTTP 中继可注入；
4. 对象卡只带 Card 已有列，未知字段不外泄；
5. 协议文档写清官方 SDK 现状与 TODO，不臆测 GATT/鉴权字段。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from harness import check, section, summary

from life.date import glass
from life.date.glass import Envelope, GlassChannel, GlassProtocolError


def expect_error(fn, label: str) -> str:
    try:
        fn()
    except GlassProtocolError as exc:
        return str(exc)
    return ""


def good(**overrides) -> Envelope:
    data = {
        "type": glass.TYPE_COACH,
        "payload": {"suggestion": "坐近一点，看她眼睛"},
        "id": "msg-1",
        "ts": 1760000000.0,
    }
    data.update(overrides)
    return Envelope(**data)


def main() -> int:
    section("① 信封：版本 / 类型 / 必填字段 / 体积")

    envelope = good()
    raw = envelope.encode()
    decoded = Envelope.decode(raw)
    check("★ 编码后能原样解回来",
          decoded.type == glass.TYPE_COACH and decoded.id == "msg-1"
          and decoded.payload.get("suggestion") == envelope.payload["suggestion"],
          str(decoded.as_dict()))

    check("未知顶层字段 → 拒收",
          bool(expect_error(lambda: Envelope.decode(json.dumps({
              "v": 1, "type": "coach", "id": "x", "ts": 0, "payload": {"suggestion": "x"},
              "extra": 1,
          }).encode()), "extra")), "")
    check("缺字段 → 拒收",
          bool(expect_error(lambda: Envelope.decode(
              b'{"v":1,"type":"coach","id":"x","payload":{"suggestion":"x"}}'), "ts")), "")
    check("★ 版本不对 → 拒收",
          bool(expect_error(lambda: Envelope(v=2, type="coach", id="x", ts=0.0,
                                             payload={"suggestion": "x"}).encode(), "v")), "")
    check("类型不认识 → 拒收",
          bool(expect_error(lambda: Envelope(type="hack", id="x", ts=0.0,
                                             payload={}).encode(), "type")), "")
    check("id 为空 → 拒收",
          bool(expect_error(lambda: Envelope(type="coach", id="", ts=0.0,
                                             payload={"suggestion": "x"}).encode(), "id")), "")
    check("id 超过 64 字符 → 拒收",
          bool(expect_error(lambda: Envelope(type="coach", id="x" * 65, ts=0.0,
                                             payload={"suggestion": "x"}).encode(), "id")), "")
    check("ts 不是数字 → 拒收",
          bool(expect_error(lambda: Envelope(type="coach", id="x", ts="now",
                                             payload={"suggestion": "x"}).encode(), "ts")), "")
    check("payload 不是对象 → 拒收",
          bool(expect_error(lambda: Envelope.decode(
              b'{"v":1,"type":"coach","id":"x","ts":0,"payload":[]}'), "payload")), "")

    check("★ coach 缺 suggestion → 拒收",
          bool(expect_error(lambda: Envelope(type="coach", id="x", ts=0.0,
                                             payload={}).encode(), "suggestion")), "")
    check("context 缺 session_id → 拒收",
          bool(expect_error(lambda: Envelope(type="context", id="x", ts=0.0,
                                             payload={"card": {}}).encode(), "session")), "")
    check("ack 缺 ref → 拒收",
          bool(expect_error(lambda: Envelope(type="ack", id="x", ts=0.0,
                                             payload={}).encode(), "ref")), "")
    check("error 缺 message → 拒收",
          bool(expect_error(lambda: Envelope(type="error", id="x", ts=0.0,
                                             payload={}).encode(), "message")), "")
    check("health 无必填字段 → 可发",
          bool(Envelope(type="health", id="x", ts=0.0, payload={}).encode()), "")
    check("★ 超过 4096 字节 → 拒收",
          bool(expect_error(lambda: Envelope(type="coach", id="x", ts=0.0,
                                             payload={"suggestion": "x" * 5000}).encode(),
                            "4096")), "")

    section("② 原始媒体在任意层级都拒收")

    check("★ payload 里的 frames → 拒收",
          bool(expect_error(lambda: good(payload={"suggestion": "x",
                                                  "frames": [1, 2]}).encode(), "frames")), "")
    check("★ 嵌套的 audio → 拒收",
          bool(expect_error(lambda: good(payload={"suggestion": "x",
                                                  "state": {"audio": "raw"}}).encode(),
                            "audio")), "")
    check("键名大小写不敏感（Image）",
          bool(expect_error(lambda: good(payload={"suggestion": "x",
                                                  "Image": "raw"}).encode(), "Image")), "")
    check("★ 二进制值 → 拒收",
          bool(expect_error(lambda: good(payload={"suggestion": b"raw"}).encode(), "二进")), "")
    check("截图 / 视频键同样拒收",
          bool(expect_error(lambda: good(payload={"suggestion": "x",
                                                  "screenshot": "x"}).encode(), "screenshot")), "")

    section("③ 通道：没接不崩 / loopback 真收发")

    unbound = GlassChannel()
    sent = unbound.send_coach({"suggestion": "你好"})
    check("★ 没接手机端 → 返回失败而不是抛异常",
          sent["ok"] is False and "未接" in sent["reason"], str(sent))
    check("没接时 describe 说清楚", unbound.describe()["available"] is False
          and unbound.describe()["protocol"] == 1, str(unbound.describe()))
    check("没接时收包返回 None", unbound.receive() is None, "")

    loop = glass.LoopbackTransport()
    channel = GlassChannel(loop)
    result = channel.send_coach({
        "stage": "icebreak", "suggestion": "先聊约会地点", "can_test": False,
        "test": None, "taboos": ["别查户口"], "next_hint": "等她主动延伸话题",
        "frames": [b"raw"], "audio": b"raw", "state": {"pose": {}},
    }, session_id="phone-1")
    check("★ loopback 发出去了", result["ok"] is True and len(loop.outbox) == 1,
          str(result))
    coach = Envelope.decode(loop.outbox[0])
    check("★ 发的是 coach 信封，带 suggestion",
          coach.type == glass.TYPE_COACH
          and coach.payload.get("suggestion") == "先聊约会地点", str(coach.payload))
    check("★ 只带教练提示字段，不带 state / 原始帧音频",
          "state" not in coach.payload and "frames" not in coach.payload
          and "audio" not in coach.payload, str(sorted(coach.payload)))
    check("★ 明确不替用户说话", coach.payload.get("coach_only") is True
          and coach.payload.get("speak_for_user") is False, "")
    check("消息 id 非空、字节数合理",
          bool(coach.id) and 0 < result["bytes"] <= glass.MAX_MESSAGE_BYTES, str(result))

    missing = channel.send_coach({"can_test": False})
    check("没有 suggestion → 发失败但通道不崩",
          missing["ok"] is False and "suggestion" in missing["reason"], str(missing))

    section("④ context：对象卡只带 Card 列")

    ctx = channel.send_context(
        "phone-2",
        card={"peer_name": "小A", "likes": "咖啡", "tags": ["约会", "咖啡"],
              "secret": "内部字段", "internal_id": 42},
        stage="亲密",
        signals={"distance": 0.8, "不认识的信号": 1, "silence": False},
    )
    check("context 发出去了", ctx["ok"] is True, str(ctx))
    packet = Envelope.decode(loop.outbox[-1])
    check("★ 未知卡字段被丢掉", "secret" not in packet.payload["card"]
          and "internal_id" not in packet.payload["card"], str(packet.payload["card"]))
    check("★ 卡里保留 peer_name / 喜好", packet.payload["card"].get("peer_name") == "小A"
          and packet.payload["card"].get("likes") == "咖啡", "")
    check("★ tags 列表收成逗号串", packet.payload["card"].get("tags") == "约会,咖啡",
          str(packet.payload["card"].get("tags")))
    check("★ 阶段名归一成 key", packet.payload.get("stage") == "intimate",
          str(packet.payload.get("stage")))
    check("★ 未知信号被丢掉、已知信号保留",
          "不认识的信号" not in packet.payload["signals"]
          and packet.payload["signals"].get("distance") == 0.8,
          str(packet.payload["signals"]))

    check("context 没 session_id → 发失败",
          channel.send_context("", card={})["ok"] is False, "")
    check("ack / error 都能组包",
          channel.send_ack("msg-1")["ok"] is True
          and channel.send_error("通道忙", ref="msg-1")["ok"] is True, "")
    error_packet = Envelope.decode(loop.outbox[-1])
    check("error 带 ref", error_packet.type == glass.TYPE_ERROR
          and error_packet.payload.get("ref") == "msg-1", str(error_packet.payload))

    inbound = Envelope(type=glass.TYPE_CONTEXT, id="in-1", ts=0.0,
                       payload={"session_id": "phone-3"}).encode()
    loop.inbox.append(inbound)
    received = channel.receive()
    check("★ 能从传输层收到并解开", received is not None
          and received.type == glass.TYPE_CONTEXT
          and received.payload.get("session_id") == "phone-3", "")
    check("也能显式喂 raw", channel.receive(inbound) is not None, "")
    check("收包队列空 → None", channel.receive() is None, "")
    check("★ 坏包 → GlassProtocolError",
          bool(expect_error(lambda: channel.receive(b"not-json"), "json")), "")
    check("超长收包 → GlassProtocolError",
          bool(expect_error(lambda: channel.receive(b"x" * (glass.MAX_MESSAGE_BYTES + 1)),
                            "4096")), "")

    section("⑤ HTTP 中继：可注入、失败不崩")

    calls: list[tuple] = []

    def fake_post(endpoint, body, headers, timeout):
        calls.append((endpoint, body, headers, timeout))
        return (200, b"ok")

    relay = glass.HttpTransport("http://relay.local/glass", token="t0k", post=fake_post)
    check("HTTP 中继可用", relay.available() is True, "")
    check("★ 发送成功", relay.send(b'{"v":1}') is True, "")
    check("带 Bearer 鉴权头", calls and calls[0][2].get("Authorization") == "Bearer t0k",
          str(calls[0][2]) if calls else "")
    check("body 原样交给中继", calls and calls[0][1] == b'{"v":1}', "")

    def fail_post(endpoint, body, headers, timeout):
        return (500, b"no")

    check("非 2xx → False",
          glass.HttpTransport("http://x", post=fail_post).send(b"{}") is False, "")

    def boom_post(*args):
        raise RuntimeError("断网")

    check("★ 中继抛异常 → False，不崩",
          glass.HttpTransport("http://x", post=boom_post).send(b"{}") is False, "")
    empty = glass.HttpTransport("")
    check("端点为空 → 不可用且发送失败", empty.available() is False
          and empty.send(b"{}") is False, "")
    check("HTTP 中继目前只发不收", relay.recv() is None, "")

    channel.close()
    check("close 后通道不可用", channel.available is False, "")

    section("⑥ 协议文档 / 红线")

    doc = Path(glass.__file__).resolve().parent / "glass_protocol.md"
    doc_text = doc.read_text(encoding="utf-8")
    check("★ 协议文档存在，写清 4096 与原始媒体不走",
          "4096" in doc_text and "原始" in doc_text, "")
    check("★ 文档记录官方 SDK 现状与 TODO（不臆测）",
          "IBleClient" in doc_text and "TODO" in doc_text and "不臆测" in doc_text, "")
    source = Path(glass.__file__).read_text(encoding="utf-8")
    check("★ 不 import 任何 Android / 厂商 SDK",
          not any(token in source for token in
                  ("import android", "com.rokid", "jnius", "pyjnius")), "")
    check("通道层不落库（无 sqlite / DAO）",
          not any(token in source for token in
                  ("sqlite3", "from data.dao", "import data.dao")), "")
    check("发送结果可 JSON 序列化", json.dumps(result, ensure_ascii=False) != "", "")

    return summary("乐奇 GLASS 通道（v2-glass-channel）")


if __name__ == "__main__":
    raise SystemExit(main())
