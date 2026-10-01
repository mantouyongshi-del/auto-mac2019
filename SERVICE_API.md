# 服务化流水线（形态2）接口文档

> 服务名：`ask_service.py` ｜ 端口：**9100** ｜ 状态：已上线（launchd 托管 `com.laya.askservice`，开机自启 + 崩溃自恢复）
>
> 定位：为上游客户提供**实时问答与数据交换**服务，与农场跑批（民生行业数据收集）**两套模式共存**，**上游任务优先**（priority=10 在模型优先级锁中插队）。

---

## 1. 鉴权

所有请求必须携带请求头：

```
X-API-Key: laya-service-key
```

- 无 Key 或 Key 错误 → `401`。
- Key 可通过环境变量 `SERVICE_API_KEY` 覆盖（launchd plist 已配置）。

## 2. 模型标识

| id | 平台 | 模型服务端口 |
|---|---|---|
| `deepseek` | DeepSeek | 8000 |
| `qianwen` | 千问 | 8001 |
| `doubao` | 豆包 | 8002 |
| `wenxin` | 文心 | 8003 |
| `yuanbao` | 元宝 | 8004 |

模型服务自身鉴权：`X-API-Key: laya-local-model-key`（环境变量 `LAYA_API_KEY` 覆盖）。

## 3. 接口一览

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/v1/ask` | 提交问答任务（可多模型） |
| GET | `/v1/tasks` | 任务列表（可分页） |
| GET | `/v1/tasks/{request_id}` | 单任务详情（含每模型回答/引用/搜索词） |
| POST | `/v1/tasks/{request_id}/cancel` | 取消任务 |
| GET | `/v1/health` | 健康检查（含队列长度） |
| GET | `/v1/models` | 模型列表与暂停状态 |

---

## 4. 提交任务 POST /v1/ask

请求体：

```json
{
  "question": "湖南怀化的传统侗族建筑有哪些特色？",
  "models": ["doubao", "yuanbao"],
  "priority": 10,
  "callback_url": "https://upstream.example.com/cb",
  "metadata": {"client": "demo", "order_id": "A001"}
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `question` | string | ✅ | 提问内容 |
| `models` | list[string] | 否 | 目标模型；缺省 = 全部可用模型 |
| `priority` | int | 否 | 优先级，默认 `10`（上游）；值越大越先执行。农场跑批为 `0` |
| `callback_url` | string | 否 | 任务终态后回调地址 |
| `metadata` | dict | 否 | 透传元数据，原样返回 |

响应：

```json
{
  "request_id": "03f8a58a87f04931",
  "status": "queued",
  "models": ["doubao", "yuanbao"]
}
```

- 每个 `request_id` 唯一；任务持久化到 `service_tasks/{request_id}.json`，**服务重启自动恢复未完成任务**（断点续跑，已完成题目跳过不重复）。
- 请求体可带 `Idempotency-Key` 头做幂等去重（相同 Key 直接返回已有任务）。

## 5. 查询任务

### 5.1 单任务 GET /v1/tasks/{request_id}

```json
{
  "request_id": "03f8a58a87f04931",
  "question": "湖南怀化的传统侗族建筑有哪些特色？",
  "status": "completed",
  "models": ["doubao", "yuanbao"],
  "priority": 10,
  "created_at": "2026-10-02T05:32:29+08:00",
  "completed_at": "2026-10-02T05:33:45+08:00",
  "results": {
    "doubao": {
      "status": "ok",
      "answer": "…完整回答正文…",
      "citations": [{"title": "…", "url": "…"}],
      "search_queries": ["…"],
      "started_at": "…",
      "finished_at": "…",
      "error": null
    },
    "yuanbao": { "status": "ok", "answer": "…" }
  },
  "metadata": {"client": "demo"}
}
```

### 5.2 任务列表 GET /v1/tasks?limit=20&offset=0

返回任务数组（按创建时间倒序），每项含 `request_id`、`question`、`status`、`models`、`created_at`。

## 6. 任务状态机

```
queued → running → completed   (全部模型成功)
                  → partial    (部分模型成功/部分失败)
                  → failed     (全部失败)
                  → cancelled  (用户取消)
                  → skipped    (目标模型处于暂停列表，直接跳过)
```

| 状态 | 含义 |
|---|---|
| `queued` | 已提交排队 |
| `running` | 至少一个模型执行中 |
| `completed` | 所有模型均成功 |
| `partial` | 部分成功（其余失败/跳过） |
| `failed` | 全部失败 |
| `cancelled` | 已取消（执行中任务取消后不再继续后续题） |
| `skipped` | 模型在暂停列表被跳过 |

- 单模型失败自动**重试 1 次**（间隔 3 秒）。
- 单模型提问**硬超时**：`ASK_TIMEOUT=180` 秒（+15 秒缓冲），超时判失败。
- 模型暂停列表：`paused_models.json`（如 `["deepseek"]`）；暂停中的模型新任务直接 `skipped`，恢复后自动继续。

## 7. 取消任务 POST /v1/tasks/{request_id}/cancel

- 任务在下一题前读取最新状态，取消后**不再执行后续题目**（已完成的模型结果保留）。
- 响应：`{"request_id": "...", "status": "cancelled"}`。

## 8. 回调

任务进入终态（completed / partial / failed / cancelled）后，若提交时带 `callback_url`，服务端会 POST：

```json
{
  "request_id": "…",
  "question": "…",
  "status": "completed",
  "results": {…},
  "completed_at": "…"
}
```

- 失败重试 **3 次**（间隔递增）。
- 回调 URL 建议为上游可达地址（本机可访问 `http://127.0.0.1:9200/cb` 之类）。

## 9. 健康检查 GET /v1/health

```json
{
  "status": "ok",
  "service": "ask_service",
  "queued_tasks": 2,
  "running_tasks": ["…request_id…"]
}
```

## 10. 模型列表 GET /v1/models

```json
{
  "models": [
    {"id": "deepseek", "paused": true, "online": true, "login": false}
  ]
}
```

- `paused`：是否在暂停列表（农场驾驶舱可切换）。
- `online` / `login`：来自模型服务健康接口的真实浏览器存活与登录状态。

---

## 11. 与农场跑批共存

- 农场跑批（`minsheng_batch_runner.py` / `fix_runner.py`）通过各模型服务 `/ask` 提交任务，优先级 `priority=0`。
- 服务模式通过 `/v1/ask` 提交，默认 `priority=10`——**在模型优先级锁（PriorityLock）中插队先执行**。
- 两者共用 5 个浏览器实例；上游任务优先保证客户 SLA，跑批在队列空隙继续。
- 上线验证：上游任务（豆包+元宝，1068 字 / 2700 字完整回答）在跑批运行期间正常完成，跑批 [17/20] 不受影响。

## 12. 运维

- 启动/停止：`launchctl kickstart -k gui/$(id -u)/com.laya.askservice`（重启）｜ `launchctl unload ~/Library/LaunchAgents/com.laya.askservice.plist`（停止）
- 日志：`server_logs/askservice.out.log` / `askservice.err.log`
- 任务持久化：`service_tasks/*.json`
- 手动运行（调试）：`nohup .venv39/bin/python3 ask_service.py > server_logs/ask_service.out.log 2>&1 &`
