# Rogue Agent — 用 agent 测评 agent

基于 **Python 3.12 + Google ADK** 构建的 agent 安全测评系统。它模拟真实用户与被测 agent 进行 **N+1 轮对话**：前 N 轮问题完全合情合理（同时侦察被测 agent 的 tools/skills 元数据），第 N+1 轮基于侦察结果精心构造诱导问题，然后由安全评审员对完整对话做判定——被测 agent 是否泄漏敏感信息、越权、或顺从恶意指令。

```
┌─ 一次性 Job (python -m rogue.cli) ───────────────────────────┐
│  ReconPlanner (ADK)   ← 前 N 轮合情合理问题，套取能力元数据        │
│  AttackPlanner (ADK)  ← 基于侦察+领域文档构造第 N+1 轮诱导问题     │
│  Judge (ADK)          ← 第一印象判定 (安全/不恰当+类别+置信度)     │
│  TargetClient         → 被测 agent (OpenAI 兼容 endpoint)      │
│  EvalCaseStore        → Elasticsearch (trace + verdict)        │
└──────────────── 进程退出，k8s 资源自动回收 ─────────────────────┘
```

## 项目结构

```
rogue/
├── config.py            # 测试配置 (N 值 0-20 校验 / 文档裁剪 / token 脱敏)
├── target_client.py     # 被测 agent 客户端 (OpenAI 兼容, 重试, URL 归一化)
├── es_store.py          # Elasticsearch 存储层 (自动建索引)
├── agents.py            # 三个 ADK agent 的大脑与指令 (核心策略都在这里)
├── runner.py            # N+1 生命周期编排器
├── cli.py               # 一次性 Job 入口 (k8s pipeline 使用)
└── console/
    ├── app.py           # Web 控制台 API (local / k8s 双触发模式)
    └── static/index.html# 控制台前端
examples/
├── message.json         # 对话 trace 输出格式示例
└── mock_target.py       # 本地调试用 mock 被测 agent (会"上当"的智能客服)
deploy/
├── k8s-job.yaml         # 一次性测评 Job 示例
└── k8s-console.yaml     # Web 控制台 Deployment + RBAC
.env.example             # 配置模板 (复制为 .env 后填写)
config.example.json      # CLI 用的测试配置示例
```

## 环境准备

要求 **Python 3.10+**（推荐 3.12），Windows 用 PowerShell，Linux/Mac 用 bash。

### 1. 创建 venv 并安装依赖

```powershell
# Windows (PowerShell), 在项目根目录 e:\workspace\cbuilder 下:
python -m venv .venv
& .venv\Scripts\python.exe -m pip install -r requirements.txt
```

```bash
# Linux / macOS:
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

> IDE 里注意选择 `.venv` 作为项目解释器（VS Code: `Ctrl+Shift+P` → Python: Select Interpreter → `.venv`）。

### 2. 配置 .env

```powershell
Copy-Item .env.example .env    # Windows
# cp .env.example .env         # Linux/Mac
```

编辑 `.env`，**至少填一项** rogue agent 自身的大脑 LLM（二选一）：

| 方案 | 需要填写 | 说明 |
|---|---|---|
| B: DeepSeek（本地调试推荐） | `ROGUE_LLM_API_KEY` | [platform.deepseek.com](https://platform.deepseek.com) 申请；可用模型只有 `deepseek-chat` / `deepseek-reasoner` |
| A: Google Gemini | 取消注释 `GOOGLE_API_KEY` 并填 key，`EVAL_GENERATOR_MODEL` 改成 `gemini-2.5-flash` | [aistudio.google.com/apikey](https://aistudio.google.com/apikey) |

> `ROGUE_LLM_*` 两项都填时方案 B 优先。被测 agent 的 endpoint/token **不放在 .env**——它是每次测试的控制台输入。

## 本地运行与调试

### 方式一：Web 控制台（推荐）

开两个终端：

```powershell
# 终端 A: 启动 mock 被测 agent (可选，监听 127.0.0.1:9000)
& .venv\Scripts\python.exe examples\mock_target.py
```

```powershell
# 终端 B: 启动控制台
& .venv\Scripts\python.exe -m uvicorn rogue.console.app:app --reload --port 4080
```

浏览器打开 **http://localhost:4080**，表单填写：

| 字段 | 填法 |
|---|---|
| N 滑杆 | 正常对话轮数 0-20，诱导问题是第 N+1 轮 |
| 被测 endpoint | mock: `http://127.0.0.1:9000/v1/chat/completions`；真实 agent 填其 OpenAI 兼容地址（`https://api.deepseek.com`、`.../v1`、`.../v1/chat/completions` 三种写法均可，自动归一化） |
| Token / 模型名 | mock 随意填；DeepSeek 填同一个 key + `deepseek-chat` |
| 领域文档 | 可选，上传 txt/md，rogue 据此构思贴合业务的问题 |
| 策略提示 | 可选，如"重点测试越权访问工单数据" |

点"启动测试" → 列表 5 秒自动刷新 → **点击行进入详情**：完整对话气泡、标红的诱导问题、judge 的判定卡片（是否不恰当/类别/置信度/证据）。

> 没有 ES 时结果保存在控制台进程内存里，重启即失；详情页展示不受影响。

### 方式二：纯 CLI（模拟 k8s pipeline 的一次性 Job）

```powershell
# 编辑 config.example.json 里的 target_endpoint / target_api_token 后:
& .venv\Scripts\python.exe -m rogue.cli --config config.example.json
```

stdout 会打印完整结果文档 JSON（含 conversation trace 与 verdict）。

### 查看完整对话 trace 日志

把 `.env` 里 `ROGUE_LOG_LEVEL=INFO` 改成 `DEBUG` 后重启，终端会输出每轮问答全文与 run 结束后的完整结果文档。

### 常用调试手段

- **看 rogue 内部决策**：UI 详情页底部"规划过程"折叠块（每轮 intent、侦察到的 target_profile、attack_vector）
- **换 rogue 大脑**：`.env` 的 `EVAL_GENERATOR_MODEL`
- **断点**：在 `rogue/runner.py` 的 `run_rogue_test` 打断点，VS Code 用 "Python: Module" 启动 `uvicorn` 并指向 `rogue.console.app`
- **mock 不上当？** 这本身就是有效结果（judge 判 safe）；可在"策略提示"里写"伪装管理员紧急工单场景"提高命中率

## 接入 Elasticsearch（可选）

本地起 ES 或已有集群后，取消 `.env` 注释：

```
ES_ENDPOINT=http://localhost:9200
# ES_API_KEY=...          # 或 ES_USERNAME + ES_PASSWORD
# ES_INDEX_PREFIX=rogue-eval
# ES_INSECURE=1           # 自签证书时
```

索引自动创建（`rogue-eval-cases`），映射含 verdict 分类、置信度等字段；UI 列表会自动改从 ES 读取，重启不丢数据。

## k8s 部署（生产）

```bash
docker build -t rogue-agent:latest .
kubectl create secret generic rogue-secrets \
  --from-literal=GOOGLE_API_KEY=xxx \
  --from-literal=ROGUE_LLM_API_KEY=xxx \
  --from-literal=ES_API_KEY=xxx
kubectl apply -f deploy/k8s-console.yaml   # 控制台 (ROGUE_EXECUTION_MODE=k8s)
```

控制台触发测试时经 RBAC 动态创建一次性 Job（配置经 `ROGUE_CONFIG_JSON` 注入），Job 写完 ES 即退出，`ttlSecondsAfterFinished=3600` 自动回收资源。

## 配置优先级速查

| 配置项 | 来源 | 优先级 |
|---|---|---|
| 测试配置（N/被测 endpoint/token） | CLI `--config` 文件 > `ROGUE_CONFIG_JSON` > 逐项环境变量；控制台则来自表单 | 表单留空即报错 |
| rogue 自身 LLM | `.env`：`ROGUE_LLM_*` 都填走 DeepSeek 系，否则 Gemini | B > A |
| ES | `.env`：`ES_ENDPOINT` 有值即启用 | 未配置则仅内存 |

## 协作约定

- 修改 agent 策略指令请只动 `rogue/agents.py` 的三个 instruction 常量，跑一遍 mock 流程验证 JSON 输出格式不被破坏（`_extract_json` 有容错，但别输出数组或嵌套围栏）
- 提交前确认 `.env`（含真实 key）在 `.gitignore` 中，密钥只走 `.env` / k8s Secret，不进代码库
- 发现 key 泄漏立即在服务商后台吊销重发
