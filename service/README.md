# 线下约会推理服务（v2-cloud-service）

PC 端原有能力保持不变；这里是新增的并行入口。手机 / 眼镜通过 HTTP 调它，
重活（pose、ASR、判定）以后都放在这一侧。

## 接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 探活，返回会话数 / 卡数 / 是否装了 FastAPI |
| POST | `/infer` | 收 `{session_id, state}`，或 `{frames, audio}` 先确认收到；回教练提示 |
| POST | `/context` | 登记对象卡（字段对齐 `Card`）/ 阶段 / 信号 |

`/infer` 返回：

```json
{
  "stage": "icebreak",
  "stage_name": "破冰",
  "suggestion": "……",
  "can_test": false,
  "test": null,
  "taboos": ["……"],
  "next_hint": "……",
  "session_id": "phone-1",
  "peer": "小A",
  "card": {"peer_name": "小A"},
  "trace": {"frames_received": 0, "audio_received": false, "judged": false}
}
```

**原始帧 / 原始音频不保留**：只计数、只标记有没有，用完即丢；会话里不存。

## 启动

```bash
# 标准库入口（零第三方依赖也能跑）
python -m service.run_server

# 带 FastAPI / uvicorn
pip install -r service/requirements.txt
uvicorn service.api:create_fastapi_app --factory --host 0.0.0.0 --port 8000
```

AutoDL 上：

```bash
bash service/start_autodl.sh
```

可用环境变量：`HOST`（默认 `127.0.0.1`；AutoDL 用 `0.0.0.0`）、`PORT`（默认 `8000`）、
`DATE_MODEL_DIR`（模型目录，③④两批接 pose / ASR 后生效）。

## 边界（本批）

- 判定层复用批次①的 `life/cards/date_rules.get_prompt`；
- Jev 判定由调用方注入 `judge`，⑤批接上；
- 会话与对象卡只存内存，落库在⑦批（v2-date-card-writeback）；
- 没有鉴权与限流（部署到公网前必须补，TODO 已写在 `api.py`）。
