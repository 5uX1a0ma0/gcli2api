# AGY2API

**将 Google Antigravity 转换为 OpenAI、Gemini 和 Claude API 兼容接口**

[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![License: CNC-1.0](https://img.shields.io/badge/License-CNC--1.0-red.svg)](LICENSE)
[![Docker](https://img.shields.io/badge/docker-available-blue.svg)](https://github.com/su-kaka/agy2api/pkgs/container/agy2api)

[English](docs/README_EN.md) | 中文 | [日本語](docs/README_JA.md)

> **⚠️ 重要变更**：GeminiCLI 端点已被谷歌抛弃，本项目已完全移除 GeminiCLI 相关代码，仅保留 Antigravity 模式。

## 安装指南

### Termux 环境

**初始安装**
```bash
curl -o termux-install.sh "https://raw.githubusercontent.com/su-kaka/agy2api/refs/heads/master/termux-install.sh" && chmod +x termux-install.sh && ./termux-install.sh
```

**重启服务**
```bash
cd agy2api
bash termux-start.sh
```

### Windows 环境

**初始安装**
```powershell
iex (iwr "https://raw.githubusercontent.com/su-kaka/agy2api/refs/heads/master/install.ps1" -UseBasicParsing).Content
```

**重启服务**
双击执行 `start.bat`

### Linux 环境

**初始安装**
```bash
curl -o install.sh "https://raw.githubusercontent.com/su-kaka/agy2api/refs/heads/master/install.sh" && chmod +x install.sh && ./install.sh
```

**重启服务**
```bash
cd agy2api
bash start.sh
```

### macOS 环境

**初始安装**
```bash
curl -o darwin-install.sh "https://raw.githubusercontent.com/su-kaka/agy2api/refs/heads/master/darwin-install.sh" && chmod +x darwin-install.sh && ./darwin-install.sh
```

**重启服务**
```bash
cd agy2api
bash start.sh
```

### Docker 环境

**Docker 运行命令**
```bash
docker run -d --name agy2api --network host -e API_PASSWORD=api_pwd -e PANEL_PASSWORD=panel_pwd -e PORT=7861 -v $(pwd)/data/creds:/app/creds -v $(pwd)/data/request_logs:/app/data/request_logs ghcr.io/su-kaka/agy2api:latest
```

**Docker Compose 运行命令**
1. 使用仓库中的 `docker-compose.yml`，或参考以下内容：
    ```yaml
    version: '3.8'

    services:
      agy2api:
        image: ghcr.io/su-kaka/agy2api:latest
        container_name: agy2api
        restart: unless-stopped
        network_mode: host
        environment:
          # 密码配置（分离密码）
          - API_PASSWORD=your_api_password
          - PANEL_PASSWORD=your_panel_password
          - PORT=7861
        volumes:
          - ./data/creds:/app/creds
          - ./data/request_logs:/app/data/request_logs
    ```
2. 启动服务：
    ```bash
    docker-compose up -d
    ```

**从源码构建本分支（x64 Linux，Docker Compose v2）**

`model-routing` 分支包含按凭证可用模型自动分流的修改。`compose.local.yml` 使用本地源码构建镜像，前端产物已包含在 `dist/` 中。

新部署可在克隆后的源码目录运行：

```bash
git clone --branch model-routing --single-branch https://github.com/5uX1a0ma0/gcli2api.git
cd gcli2api
docker compose -f docker-compose.yml -f compose.local.yml up -d --build
```

已有 Compose 部署时，在**原 Compose 项目目录**中克隆源码并构建，原服务在构建期间继续运行：

```bash
git clone --branch model-routing --single-branch https://github.com/5uX1a0ma0/gcli2api.git agy2api-source
export AGY2API_SOURCE_DIR="$PWD/agy2api-source"
docker compose -f docker-compose.yml -f agy2api-source/compose.local.yml build agy2api
```

构建成功后，停止服务并备份原数据，再启动新镜像。以下使用默认数据目录 `data/`，自定义挂载时请备份实际宿主机目录；PostgreSQL 按原方式备份数据库：

```bash
docker compose -f docker-compose.yml stop agy2api
tar -czf "/tmp/agy2api-backup-$(date +%Y%m%d-%H%M%S).tar.gz" data
docker compose -f docker-compose.yml -f agy2api-source/compose.local.yml up -d --no-deps agy2api
docker compose -f docker-compose.yml -f agy2api-source/compose.local.yml logs --tail=100 agy2api
```

覆盖配置保留原 Compose 的环境变量、网络和数据卷。上述示例以 `docker-compose.yml` 和服务名 `agy2api` 为准，实际名称不同时按原部署替换。分流改动新增独立的模型禁用记录表，启动时自动创建并保留现有凭证数据；模型能力缓存将在首次请求或查询模型列表时建立。

后续更新先运行 `git -C agy2api-source pull --ff-only`，然后重新构建并启动。每次操作继续使用两个 `-f` 参数，并设置 `AGY2API_SOURCE_DIR`；也可在原项目 `.env` 中加入 `AGY2API_SOURCE_DIR=./agy2api-source`，持久保存源码目录设置。

回退到原镜像：

```bash
docker compose -f docker-compose.yml up -d --no-deps --pull never agy2api
```

## 核心功能

### 🔄 API 端点和格式支持

**Antigravity 多端点多格式支持**
- **OpenAI 格式端点**：`/v1/chat/completions` 和 `/v1/models`
- **Gemini 原生端点**：`/v1/models/{model}:generateContent` 和 `streamGenerateContent`
- **Claude 格式端点**：`/v1/messages`
- 支持所有 Antigravity 模型（Claude、Gemini 等）
- 自动模型名称映射和思维模式检测
- 多种认证方式：Bearer 头部、x-api-key、x-goog-api-key、URL 参数 key

### 📊 智能凭证管理系统

- 多个 Google OAuth 凭证自动轮换（随机负载均衡）
- 按每个凭证的实际可用模型自动分流，兼容免费/付费凭证混用，与账户 PRO/ULTRA 等级无关
- 凭证管理页显示 Paid / Non-paid / 未识别标记，按可用模型能力识别，模型列表查询不会执行模型生成请求
- 模型级冷却（429 错误自动设置对应模型家族的 CD：argon / gemini / claude+gpt 共享冷却）
- 自动故障检测和凭证禁用
- 凭证使用统计和配额/额度查询
- 批量操作（启用、禁用、删除）
- 以邮箱为唯一键，重复导入自动覆盖去重
- 自动获取 Google 账户邮箱地址、订阅等级（Tier）和 Project ID

### 🌊 流式传输和响应处理

- 真正的实时流式响应（SSE）
- 流式抗截断功能（防止回答被截断，通过"抗截断-"前缀启用）
- 思维链（Thinking）内容分离

### 🎛️ Web 管理控制台

基于 React + Vite + Tailwind 编译的现代化前端界面：
- Antigravity OAuth 认证流程（支持回调 URL 快捷方式）
- 凭证文件上传（JSON/ZIP）、下载、详情查看
- 凭证导出（规范 JSON：refresh_token/email/tier/project_id）
- 系统配置管理（环境变量锁定提示、热更新）
- 批量凭证操作和状态筛选、批量导入（缺失字段自动获取）
- 移动端自适应

## 支持的模型

模型列表按凭证调用 Antigravity `fetchAvailableModels` 动态获取，对外返回所有启用凭证的模型并集。请求按转换后的真实上游模型 ID 选择凭证，随机、会话缓存和粘性模式均会跳过不支持该模型的凭证。

识别结果使用进程内缓存（5 分钟），凭证 Token、Project ID 或 API 地址变化后重新获取。查询失败保留已有结果，尚未识别的凭证暂按未知能力尝试，30 秒后重新查询；明确的“模型不存在”400 会永久禁用该凭证下对应的真实模型 ID，并换用其他凭证，每个失配凭证在同一次请求中最多尝试一次。禁用记录按邮箱和模型 ID 保存在 `disabled_credential_models` 表中，即使模型列表仍宣称可用或查询失败，后续请求也会跳过它；重启、缓存过期、Token/Project/API 地址变化及同邮箱凭证重新导入都不会自动解除禁用。该凭证的其他模型，以及其他凭证支持的同名模型继续可用。此类分流不占用 429 重试次数，也不会触发整张凭证的自动禁用；参数、工具和签名等普通 400 错误保持原有处理。

凭证管理页独立加载 Paid 标记，复用同一份模型能力缓存。当前以 Claude 5.5 或 Gemini 4 模型识别为 Paid；没有这些模型但识别到 Claude 4.6 时显示 Non-paid。查询失败、空列表或仅识别到共享模型时显示“Paid 未识别”。此标记描述已识别的模型能力，账户 PRO/ULTRA 等级仍单独显示。

支持的模型以实际返回的列表为准，例如：

- **Claude 系列**：`claude-sonnet-4-6`、`claude-opus-5-5-high` 等
- **Gemini 系列**：`gemini-3.8-flash`、`gemini-4-argon-high` 等

**特殊功能变体：**
- **抗截断模式**：在模型名称前添加 `抗截断-` 前缀
  - 例：`抗截断-gemini-2.5-pro`
  - 通过合成工具调用绕过截断限制，确保完整回答（流式和非流式均有效）
- **图像生成模型**支持分辨率（`-2k`、`-4k`）和比例（`-16x9`、`-9x16` 等）后缀

## 配置说明

1. 访问 `http://127.0.0.1:7861`（默认端口，可通过 PORT 环境变量修改）
2. 完成 Antigravity OAuth 认证流程（默认密码：`pwd`，可通过环境变量修改）
3. 配置客户端：

**OpenAI 兼容客户端：**
- **端点地址**：`http://127.0.0.1:7861/v1`
- **API 密钥**：`pwd`（默认值，可通过 API_PASSWORD 环境变量修改）

**Gemini 原生客户端：**
- **端点地址**：`http://127.0.0.1:7861`
- **认证方式**：`Authorization: Bearer 密钥`、`x-goog-api-key: 密钥` 或 URL 参数 `?key=密钥`

**Claude 客户端：**
- **端点地址**：`http://127.0.0.1:7861/v1/messages`
- **认证方式**：`x-api-key: 密钥` 或 `Authorization: Bearer 密钥`

## 💾 数据存储

### 存储后端

| 后端 | 说明 | 启用方式 |
|------|------|----------|
| **SQLite**（默认） | 本地文件存储，开箱即用 | 无需配置，数据存储在 `creds/credentials.db` |
| **PostgreSQL** | 云端分布式存储，多实例部署 | 设置 `POSTGRESQL_URI` 环境变量 |

### 凭证数据结构

凭证以**邮箱（email）为唯一键**存储，字段为结构化数据库列（不再使用 JSON blob）：
`email`、`access_token`、`refresh_token`、`client_id`、`client_secret`、`project_id`、`expiry`、`tier`

凭证导入/导出使用规范 JSON 格式：`{"refresh_token": "...", "email": "...", "tier": "...", "project_id": "..."}`（仅 `refresh_token` 必填，其余字段可自动获取）。

### 环境变量配置

**基础配置**
- `PORT`: 服务端口（默认：7861）
- `HOST`: 服务器监听地址（默认：0.0.0.0）

**密码配置**
- `API_PASSWORD`: 聊天 API 访问密码（默认：pwd）
- `PANEL_PASSWORD`: 控制面板访问密码（默认：pwd）

**访问控制配置**
- `IP_WHITELIST_ENABLED`: 启用 IP 白名单（默认：false，不限制）
- `IP_WHITELIST`: IP 白名单列表，逗号或换行分隔（默认：空）。每条支持单个 IP（`192.168.1.1`）、CIDR 网段（`192.168.1.0/24`）、起止范围（`10.0.0.1-10.0.0.100`），IPv4 / IPv6 均可。开启后仅白名单内 IP 可访问面板和 API，其余 IP 直接 403；面板保存后立即生效

**模型名复写配置**
- `MODEL_REWRITE_ENABLED`: 启用模型名复写（默认：false）
- `MODEL_REWRITE_RULES`: 复写规则，每条为 `[匹配正则, 替换模型名]`（默认：空）。自上而下首条命中即替换，替换支持正则反向引用（如 `\1`）。例：把 `gpt-4o` 映射到 `gemini-2.5-pro`。面板保存后立即生效

**存储配置**
- `CREDENTIALS_DIR`: 凭证数据库目录（默认：./creds）
- `POSTGRESQL_URI`: PostgreSQL 连接字符串（设置后启用 PostgreSQL 模式）

**网络和代理配置**
- `PROXY`: 代理地址（支持 `http://`、`https://`、`socks5://`、`socks5h://`，可带账号密码 `user:pass@host:port`）
- `OAUTH_PROXY_URL`: OAuth 认证代理端点
- `GOOGLEAPIS_PROXY_URL`: Google APIs 代理端点
- `ANTIGRAVITY_API_URL`: Antigravity API 端点
- `PATH_PREFIX`: 反代保留路径前缀（如 `/AGY2API`）时配置，由后端在路由匹配前剥掉该前缀；默认：空（不剥离）

**性能和稳定性配置**
- `RETRY_429_ENABLED`: 启用错误自动重试（默认：true）
- `RETRY_429_MAX_RETRIES`: 最大重试次数（默认：5）
- `RETRY_429_INTERVAL`: 重试间隔，秒（默认：1）
- `ANTI_TRUNCATION_MAX_ATTEMPTS`: 抗截断最大重试次数（默认：3）

**自动化配置**
- `AUTO_BAN`: 启用凭证自动封禁（默认：false）
- `AUTO_BAN_ERROR_CODES`: 触发自动封禁的错误码（默认：403）

**响应行为配置**
- `RETURN_THOUGHTS_TO_FRONTEND`: 返回思维链到前端（默认：true）

**敏感词过滤配置**
- `SENSITIVE_WORDS_ENABLED`: 启用敏感词过滤（默认：false）
- `SENSITIVE_WORDS`: 敏感词列表，逗号或换行分隔（默认：空）。命中后整词替换为空，不区分大小写，支持中文，面板保存后立即生效

**请求日志配置**
- `USAGE_LOG_ENABLED`: 启用请求日志（默认：true）。记录每次 API 请求（模型、凭证、token 用量、耗时、状态）到数据库，不自动清理，可在面板手动清理或清空

**请求体详细日志配置**
- `REQUEST_LOG_ENABLED`: 启用请求体详细日志（默认：false）。开启后，当请求命中指定错误码时，把发往上游的完整请求头（已脱敏）与请求体保存为 JSON 文件到 `data/request_logs/`，数据库仅记录文件索引，在"请求日志"详情中查看。主要用于请求体错误时排查 debug；清理/清空日志时会同步删除对应文件（Docker 部署请挂载 `./data/request_logs:/app/data/request_logs` 以持久化）
- `REQUEST_LOG_ERROR_CODES`: 触发详细日志的错误码列表，逗号分隔（默认：400）

**凭证选择模式**
- `CREDENTIAL_SELECT_MODE`: 凭证选择模式（默认：cache）
  - `random` — 完全随机负载均衡，每次请求随机挑选凭证
  - `cache` — 缓存匹配（推荐）：以"模型 + system 指令（或首条 user 消息）"哈希会话 key，相同会话复用同一凭证，命中 Google 上游 prompt cache；凭证 CD 后自动切换并重新绑定
  - `sticky` — 凭证粘性：所有请求锁定同一凭证，直到其进入 CD 或被禁用
- `REDIS_URL`: Redis 连接串（可选）。设置后缓存匹配的会话绑定存入 Redis，多实例共享；未设置时使用进程内存后端

**日志配置**
- `LOG_LEVEL`: 日志级别（DEBUG/INFO/WARNING/ERROR，默认：INFO）
- `ENABLE_LOG`: 完全静默，不打印任何日志（设为 0 启用静默，默认：1）

## API 使用示例

### OpenAI 格式

```bash
curl -X POST "http://127.0.0.1:7861/v1/chat/completions" \
  -H "Authorization: Bearer your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "messages": [
      {"role": "user", "content": "Hello"}
    ],
    "stream": true
  }'
```

### Gemini 原生格式

```bash
curl -X POST "http://127.0.0.1:7861/v1/models/gemini-2.5-flash:generateContent?key=your_api_password" \
  -H "Content-Type: application/json" \
  -d '{
    "contents": [
      {"role": "user", "parts": [{"text": "Hello"}]}
    ]
  }'
```

### Claude 格式

```bash
curl -X POST "http://127.0.0.1:7861/v1/messages" \
  -H "x-api-key: your_api_password" \
  -H "anthropic-version: 2023-06-01" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-sonnet-4-5",
    "max_tokens": 1024,
    "messages": [
      {"role": "user", "content": "Hello, Claude!"}
    ]
  }'
```

### 图像生成（Gemini 原生 SDK）

```python
from io import BytesIO
from PIL import Image
from google.genai import Client
from google.genai.types import HttpOptions
from google.genai import types

client = Client(
    api_key="pwd",
    http_options=HttpOptions(base_url="http://127.0.0.1:7861"),
)

prompt = "画一只猫"

response = client.models.generate_content(
    model="gemini-3.1-flash-image",
    contents=[prompt],
    config=types.GenerateContentConfig(
        image_config=types.ImageConfig(
            aspect_ratio="16:9",
        )
    )
)
for part in response.candidates[0].content.parts:
    if part.text is not None:
        print(part.text)
    elif part.inline_data is not None:
        image = Image.open(BytesIO(part.inline_data.data))
        image.save("generated_image.png")
```

## 🛠️ 前端开发

控制面板前端源码位于 `frontend/` 目录（React + Vite + Tailwind CSS）：

```bash
cd frontend
npm install
npm run dev     # 开发模式，端口 3000，代理后端 API
npm run build    # 构建到 dist/（生产模式使用）
```

后端直接托管 `dist/` 下编译后的静态文件，无需 Node.js 运行时。

## 💬 交流群

欢迎加入 QQ 群交流讨论！

**QQ 群号：1083250744**

<img src="docs/qq-group.jpg" width="200" alt="QQ 群二维码">

## 许可证与免责声明

本项目仅供学习研究使用，禁止商业用途和倒卖。

## ⚠️ 许可证声明

本项目使用 [CNC-1.0 许可证](LICENSE)（反商业许可证）。
