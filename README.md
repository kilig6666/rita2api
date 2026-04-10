# rita2api

A lightweight Flask reverse proxy that bridges the **OpenAI Chat Completions API** to [rita.ai](https://www.rita.ai)'s chat API — with streaming SSE, multi-account rotation, and tool calling support.

## Features

- **OpenAI-compatible** — `/v1/chat/completions` works with standard OpenAI clients
- **Multi-account rotation** — round-robin + auto-failover across multiple Rita accounts
- **Streaming SSE** — full Server-Sent Events support with OpenAI chunk format
- **Tool calling** — prompt injection for function-calling models; AI tools API
- **Model catalog** — exposes Rita's model list via `/v1/models`
- **Conversation continuity** — `chat_id` + `parent` message threading per account

## Quick Start

```bash
git clone <repo>
cd rita2api
pip install -r requirements.txt
cp .env.example .env
# Edit .env with your Rita tokens
python server.py
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `RITA_TOKENS` | *(required)* | Comma-separated session tokens: `token1,token2,token3` |
| `RITA_VISITOR_IDS` | *(required)* | Comma-separated visitor IDs, same count as tokens |
| `RITA_UPSTREAM` | `https://api_v2.rita.ai` | Upstream API base URL |
| `RITA_ORIGIN` | `https://www.rita.ai` | Origin header |
| `HOST` | `0.0.0.0` | Listen address |
| `PORT` | `10089` | Listen port |
| `DEBUG` | `1` | Enable debug logging (`0` to disable) |

### Multi-Account Setup

```bash
# Account 1
RITA_TOKENS=token_abc123,token_def456,token_ghi789
RITA_VISITOR_IDS=vid_abc:xxx,vid_def:yyy,vid_ghi:zzz
```

- **Round-robin**: each request uses the next account in sequence
- **Auto-failover**: if an account fails 3 consecutive requests, it's temporarily skipped
- **Auto-recovery**: successful request resets failure counter
- **Client headers**: clients can pass their own `token`/`visitorid` in headers to bypass rotation

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions (streaming + sync) |
| `POST` | `/v1/chat/completions/streaming` | Explicit streaming with tool support |
| `GET` | `/v1/models` | List available models from Rita catalog |
| `POST` | `/v1/conversations` | List conversation history |
| `POST` | `/v1/chat/init` | Start a new conversation |
| `POST` | `/v1/chat/title` | Get auto-generated conversation title |
| `GET` | `/v1/tools` | List available AI tools (image/video) |
| `POST` | `/v1/tools/execute` | Execute an AI tool |
| `GET` | `/health` | Health check |
| `GET` | `/debug/state` | Show account/conversation state |
| `POST` | `/debug/clear` | Clear conversation state |

## Architecture

```
Client (OpenAI API)
        │
        ▼
   rita2api proxy (Flask, port 10089)
        │  • multi-account rotation
        │  • message format translation
        │  • tool prompt injection
        │  • SSE translation
        ▼
  rita.ai API (api_v2.rita.ai)
  /aichat/completions
```

## Tool Calling

rita2api supports two tool paradigms:

### 1. Function Calling (via prompt injection)
When `tools` are passed in the request, rita2api injects compact tool descriptions
into the user message. The model responds with JSON tool calls, which are converted
to OpenAI `tool_calls` format.

```bash
curl http://localhost:10089/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "What is the weather in Tokyo?"}],
    "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}}],
    "stream": false
  }'
```

### 2. AI Tools (rita native)
rita.ai provides built-in AI tools (image enhancement, background removal, video generation, etc.).
List available tools and execute them:

```bash
# List tools
curl http://localhost:10089/v1/tools

# Execute a tool
curl http://localhost:10089/v1/tools/execute \
  -H "Content-Type: application/json" \
  -d '{"tool_id": 1, "action": "edit_prompt", "prompt": "enhance this image"}'
```

## License

CC BY-NC-SA 4.0

---

# rita2api（中文文档）

将 **OpenAI Chat Completions API** 转接到 [rita.ai](https://www.rita.ai) 聊天 API 的轻量级 Flask 反向代理，支持流式 SSE、多账号轮换和工具调用。

## 功能特性

- **OpenAI 兼容** — `/v1/chat/completions` 可直接对接标准 OpenAI 客户端
- **多账号轮换** — Round-robin 轮询 + 自动故障转移，支持多个 Rita 账号
- **流式 SSE** — 完整的 Server-Sent Events 支持，输出 OpenAI chunk 格式
- **工具调用** — 支持 Function Calling（prompt 注入方案）和 Rita 原生 AI 工具
- **模型目录** — 通过 `/v1/models` 暴露 Rita 全部模型列表
- **对话连续性** — 基于 `chat_id` + `parent` 的消息线程，按账号隔离

## 快速开始

```bash
git clone <repo>
cd rita2api
pip install -r requirements.txt
cp .env.example .env
# 编辑 .env，填入你的 Rita token
python server.py
```

## 配置说明

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `RITA_TOKENS` | *（必填）* | 逗号分隔的会话 token：`token1,token2,token3` |
| `RITA_VISITOR_IDS` | *（必填）* | 逗号分隔的访客 ID，数量与 token 一一对应 |
| `RITA_UPSTREAM` | `https://api_v2.rita.ai` | 上游 API 基础地址 |
| `RITA_ORIGIN` | `https://www.rita.ai` | Origin 请求头 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `10089` | 监听端口 |
| `DEBUG` | `1` | 开启调试日志（`0` 关闭） |

### 多账号配置

```bash
RITA_TOKENS=token_abc123,token_def456,token_ghi789
RITA_VISITOR_IDS=vid_abc:xxx,vid_def:yyy,vid_ghi:zzz
```

- **Round-robin 轮询**：每次请求依次使用下一个账号
- **自动跳过**：某个账号连续失败 3 次后暂时跳过
- **自动恢复**：成功一次即重置失败计数
- **客户端覆盖**：客户端可在请求头传 `token`/`visitorid` 来跳过轮换

## 接口列表

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions（流式 + 同步） |
| `POST` | `/v1/chat/completions/streaming` | 显式流式 + 工具调用支持 |
| `GET` | `/v1/models` | 获取 Rita 模型目录 |
| `POST` | `/v1/conversations` | 获取对话历史 |
| `POST` | `/v1/chat/init` | 创建新对话 |
| `POST` | `/v1/chat/title` | 获取自动生成的对话标题 |
| `GET` | `/v1/tools` | 获取可用 AI 工具（图像/视频） |
| `POST` | `/v1/tools/execute` | 执行 AI 工具 |
| `GET` | `/health` | 健康检查 |
| `GET` | `/debug/state` | 查看账号/对话状态 |
| `POST` | `/debug/clear` | 清除对话缓存 |

## 架构说明

```
客户端（OpenAI API）
        │
        ▼
   rita2api 代理（Flask，端口 10089）
        │  • 多账号轮换
        │  • 消息格式转换
        │  • 工具 prompt 注入
        │  • SSE 格式转换
        ▼
  rita.ai API（api_v2.rita.ai）
  /aichat/completions
```

## 工具调用

rita2api 支持两种工具调用模式：

### 1. Function Calling（prompt 注入方案）
请求中传入 `tools` 后，rita2api 将紧凑的工具描述注入用户消息。模型以 JSON 格式返回工具调用，代理自动转为 OpenAI `tool_calls` 格式。

```bash
curl http://localhost:10089/v1/chat/completions \
  -H "Content-Type: application/json" \
  -d '{
    "model": "gpt-4o",
    "messages": [{"role": "user", "content": "东京今天天气怎么样？"}],
    "tools": [{"type": "function", "function": {"name": "get_weather", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
    "stream": false
  }'
```

### 2. AI 工具（Rita 原生）
rita.ai 提供内置 AI 工具（图像增强、背景移除、视频生成等），可直接列出和执行：

```bash
# 列出工具
curl http://localhost:10089/v1/tools

# 执行工具
curl http://localhost:10089/v1/tools/execute \
  -H "Content-Type: application/json" \
  -d '{"tool_id": 1, "action": "edit_prompt", "prompt": "增强这张图片"}'
```

### 多账号轮换机制

```
请求 1 → 账号 A ✓
请求 2 → 账号 B ✓
请求 3 → 账号 C ✗ (失败 1 次)
请求 4 → 账号 A ✓
请求 5 → 账号 B ✓
请求 6 → 账号 C ✗ (失败 2 次)
请求 7 → 账号 A ✓
请求 8 → 账号 B ✓
请求 9 → 账号 C ✗ (失败 3 次，达到阈值)
请求 10 → 账号 A ✓  ← 自动跳过 C
请求 11 → 账号 B ✓  ← 自动跳过 C
...
全部账号超限 → 重置计数，从头开始
```

## 许可证

CC BY-NC-SA 4.0
