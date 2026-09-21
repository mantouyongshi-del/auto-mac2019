# DeepSeek 自动化问答服务（原 Laya + DeepSeek）

> 本文档记录了从需求到搭建完成的完整过程。

---

## 一、需求背景

用户需要搭建一个本地服务，接收问题后自动操作 DeepSeek 网页版进行提问，获取 AI 回答和引用信源，整理后返回给业务系统。

> 注：2026-09-21 起按需求调整——**不再调用 Laya 分类模型**，所有问题直接进入 DeepSeek 回答，响应不再包含 decision 字段。

### 核心流程
```
业务系统 ──POST /ask──> DeepSeek 服务
                         │
                         ├─ Playwright 操作 DeepSeek 网页版
                         ├─ 抓取回答正文 + 引用信源
                         ├─ 本地保存日志
                         └─ 返回 JSON
```

---

## 二、环境信息

### 硬件
| 项目 | 配置 |
|---|---|
| 机型 | MacBook Pro 16,1（2019 Intel） |
| CPU | Intel i7-9750H 6核12线程 |
| 内存 | 32GB |
| GPU | AMD Radeon Pro 5300M 4GB（不可用于ML） |
| 系统 | macOS Intel x86_64 |

### 关键约束
- **无 CUDA / 无 MPS**：只能 CPU 推理
- **torch 2.3+ 不再支持 Intel Mac**：必须用 torch 2.2.2（最后支持版本）
- **torch 2.2.2 最高支持 Python 3.12**：系统自带 Python 3.9 可用

---

## 三、搭建过程记录

### 1. 拉取代码
```bash
git clone https://github.com/NandhaKishorM/laya.git
```

### 2. 创建 Python 环境
```bash
/usr/bin/python3 -m venv .venv39
.venv39/bin/pip install "torch==2.2.2" "numpy==1.26.4"
.venv39/bin/pip install "transformers>=4.45.0" "safetensors" "huggingface_hub"
.venv39/bin/pip install fastapi "uvicorn[standard]" pydantic playwright
.venv39/bin/pip install -e .
```

### 3. 下载模型
- 镜像：`HF_ENDPOINT=https://hf-mirror.com`
- 模型仓库：`convaiinnovations/laya`
- 三个 checkpoint：英文(804MB) + 多语言(614MB) + typed-decisions(804MB)，共 2.2GB

### 4. 实测性能
| 场景 | 耗时 |
|---|---|
| 英文推理 | ~1000ms/条 |
| 中文推理 | ~400ms/条 |
| 模型加载 | ~35秒（首次） |
| 内存峰值 | ~6GB |

---

## 四、服务架构

### 文件结构
```
~/laya/                   # 项目实体（桌面 laya 为快捷方式）
├── core/                 # 【公共层】所有模型共享
│   ├── browser_base.py      #   浏览器基类：Playwright 启动/关闭、反检测、逐字输入、等待完成、新会话主流程
│   └── server_base.py       #   FastAPI 应用工厂：/ask、/batch_ask、健康检查、锁、日志
├── bots/                 # 【模型适配层】每个模型一个文件，只写差异部分
│   ├── deepseek.py         #   DeepSeek：选择器、新对话按钮、引用提取
│   ├── qianwen.py          #   千问：contenteditable 输入框、来源卡片引用
│   └── (doubao.py / kimi.py ... 待接入)
├── run_deepseek.py       # 【入口】DeepSeek 服务（端口 8000，薄封装）
├── run_qianwen.py        # 【入口】千问服务（端口 8001，薄封装）
├── profiles/             # 各模型 Chrome 持久化登录态
│   ├── deepseek/
│   └── qianwen/
├── laya-model/           # Laya 本地模型包（备用，暂不调用）
├── .venv39/               # Python 3.9 虚拟环境（共用）
├── server_logs/           # 本地请求日志（JSONL + launchd 输出）
├── archive/               # 上游仓库研究材料
├── LICENSE                # Apache 2.0 许可证
└── PROJECT_NOTES.md       # 本文档
```

> 多模型架构：新增一个模型 = 在 `bots/` 写一个适配文件（实现 new_chat / is_logged_in / extract_answer / extract_citations）+ 一个 `run_xxx.py` 入口（指定端口）+ 一个 plist。公共逻辑全部复用 core，不重复造轮子。
> 端口规划：DeepSeek 8000，千问 8001，后续 8002+。

> 注：2026-09-21 完成多模型架构重构——从单文件 server.py 拆分为 core 公共层 + bots 模型适配层。
> 同日完成路径迁移：项目实体从 `~/Desktop/laya` 迁至 `~/laya`（macOS 桌面受 TCC 保护，launchd 无法读取 `~/Desktop` 下的文件），桌面 `laya` 为指向 `~/laya` 的快捷方式。

### API 接口

**POST /ask**
- 请求：`{"question": "你的问题"}`
- 响应：
```json
{
  "question": "原始问题",
  "answer": "DeepSeek 回答正文",
  "citations": [
    {"ref": "-1", "url": "https://来源URL"}
  ],
  "timestamp": "时间戳"
}
```

**POST /batch_ask**（批量引擎）
- 请求：`{"questions": ["问题1", "问题2", "..."]}`（单次最多 100 个）
- 行为：每题前自动"开启新对话"（独立会话，互不干扰）；串行执行；单题失败跳过并标记 error，不影响后续
- 响应：
```json
{
  "total": 2,
  "succeeded": 1,
  "failed": 1,
  "results": [
    {"question": "问题1", "answer": "...", "citations": [...], "timestamp": "..."},
    {"question": "问题2", "error": "问题为空"}
  ]
}
```
- 成功项与失败项均写入当日 JSONL 日志

**GET /**
- 健康检查：`{"status": "ok"}`

---

## 五、关键技术点

### DeepSeek 页面选择器
| 元素 | 选择器 |
|---|---|
| 回答正文 | `.ds-assistant-message-main-content` |
| 引用链接 | `.ds-assistant-message-main-content a[href^='http']` |
| 输入框 | `textarea` |
| 智能搜索开关 | `text=智能搜索`（选中状态含 `ds-toggle-button--selected`） |
| 新对话（独立会话） | `text=开启新对话`（外层为 `<div tabindex="0">`） |
| 发送 | 回车 |

### 千问页面选择器
| 元素 | 选择器 |
|---|---|
| 回答正文 | `[class*='markdown']`（取 last） |
| 引用链接 | 页面所有外链 `a[href^='http']`（来源卡片，排除 qianwen/aliyun 域名） |
| 输入框 | `[contenteditable='true']`（富文本编辑区，非 textarea） |
| 新对话（独立会话） | `text=新建对话` |
| 发送 | 回车 |

### 反检测措施
- 隐藏 `navigator.webdriver`
- 伪造 plugins/languages/chrome 对象
- 逐字输入（50-150ms/字随机延迟）
- 操作前随机停顿（0.5-2秒）

### 本地日志
- 路径：`server_logs/YYYY-MM-DD.jsonl`
- 格式：每行一条 JSON
- 内容：问题、时间、回答、引用列表

---

## 六、日常使用

### 启动服务

**已托管至 launchd（推荐，开机/登录自启 + 断线自动重启）**
```bash
# 查看服务状态
launchctl list | grep laya

# 手动启动（已加载时无需执行）
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.laya.deepseek.plist

# 彻底停止（kill 进程会被自动拉起）
launchctl bootout gui/$(id -u)/com.laya.deepseek
```

**手动前台启动（调试用）**
```bash
cd ~/laya
HF_ENDPOINT=https://hf-mirror.com .venv39/bin/python run_deepseek.py
```

### 调用接口
```bash
curl -X POST http://127.0.0.1:8000/ask \
  -H "Content-Type: application/json" \
  -d '{"question": "你的问题"}'
```

### 查看日志
```bash
cat ~/laya/server_logs/$(date +%Y-%m-%d).jsonl          # 请求日志
tail -f ~/laya/server_logs/launchd.out.log               # 服务输出
tail -f ~/laya/server_logs/launchd.err.log               # 服务错误
```

---

## 七、注意事项

1. **登录态**：首次需手动登录 DeepSeek，之后 `.browser_profile` 保持登录态
2. **串行处理**：同一时间只能处理一个请求
3. **频率**：每天约100条，风控风险低
4. **无 GPU**：纯 CPU 推理，速度有限
5. **模型缓存**：在 `~/.cache/huggingface/`，不在项目目录内

---

*文档生成时间：2026-09-21*
