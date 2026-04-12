# rita2api
<p align="center">
  <a href="https://linux.do" target="_blank">
    <img src="https://img.shields.io/badge/LINUX-DO-FFB003?style=for-the-badge&logo=linux&logoColor=white" alt="LINUX DO" />
  </a>
</p>

OpenAI 兼容的 [Rita.ai](https://www.rita.ai) 反向代理，支持多账号轮换、自动注册补号、WebUI 管理面板。

## 功能

- **OpenAI 兼容** `/v1/chat/completions` 直接对接 OpenAI 客户端（流式 + 非流式）
- **多账号轮换** Round-robin 负载均衡 + 自动故障转移 + 点数系统
- **自动注册** 使用 YesCaptcha + GPTMail/YYDSMail 全自动注册 Rita 账号
- **自动补号** 活跃账号低于阈值时后台自动注册补充
- **WebUI 管理** 五页 Tab 面板：聊天广场 / 账号注册 / 账号管理 / 邮箱服务 / 配置管理
- **数据库配置** SQLite 持久化，所有配置支持 WebUI 热更新
- **Tool Calling** prompt 注入方式支持 function calling

## 快速开始

```bash
git clone <repo>
cd rita2api
pip install -r requirements.txt
python server.py
```

启动后访问 `http://localhost:10089` 进入管理面板。

### 初次使用

1. 打开 WebUI → **配置管理** Tab
2. 设置 `AUTH_TOKEN`（管理面板密码，留空则无需登录）
3. 打开 **账号管理** Tab → 添加账号（填入 Rita token）
4. 或者打开 **账号注册** Tab → 配置 `YESCAPTCHA_KEY` + `GPTMAIL_API_KEY` → 点击手动注册
5. 访问[YESCAPTCHA](https://yescaptcha.com/i/w9X0Ae)获取验证码解决方案

## 架构

```
客户端 (OpenAI API)
       |
       v
  rita2api (Flask :10089)
       |  - 多账号 Round-robin
       |  - 自动创建对话 (newConversation)
       |  - 消息格式转换 (OpenAI -> Rita)
       |  - SSE 流转换 (Rita -> OpenAI chunk)
       |  - token + Cookie 双头鉴权
       v
  Rita API (api_v2.rita.ai)
  /aichat/completions (SSE)
```

### 鉴权模型

Rita API 需要 `token` 同时出现在 HTTP Header 和 Cookie 中：

```
Header:  token: <gosplit_token>
Cookie:  token=<gosplit_token>
```

对话流程：
1. `POST /chatgpt/newConversation {"model":"model_25"}` → 获取 `chat_id`
2. `POST /aichat/completions {"model":"model_25","messages":[...],"chat_id":xxx}` → SSE 流

模型 ID 格式为 `model_xxx`（如 `model_25` = Rita, `model_69` = GPT-5.4），通过 `/v1/models` 查看完整列表。

## API

### 代理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| `POST` | `/v1/chat/completions` | OpenAI Chat Completions（流式 + 非流式） |
| `POST` | `/v1/responses` | OpenAI Responses API（流式 + 非流式） |
| `GET` | `/v1/models` | 模型目录（来自 Rita） |
| `POST` | `/v1/chat/init` | 创建新对话 |
| `GET` | `/v1/tools` | 可用 AI 工具列表 |
| `POST` | `/v1/tools/execute` | 执行 AI 工具 |
| `GET` | `/health` | 健康检查 |

### 管理接口（需 AUTH_TOKEN）

| 方法 | 路径 | 说明 |
|------|------|------|
| `GET` | `/api/accounts` | 账号列表 |
| `POST` | `/api/accounts` | 添加账号 |
| `POST` | `/api/accounts/batch` | 批量导入 |
| `POST` | `/api/accounts/batch-action` | 批量操作（启用/禁用/删除/测试/刷新） |
| `PUT` | `/api/accounts/<id>` | 编辑账号 |
| `DELETE` | `/api/accounts/<id>` | 删除账号 |
| `POST` | `/api/accounts/<id>/toggle` | 启用/禁用 |
| `POST` | `/api/accounts/<id>/test` | 测试连通性 |
| `POST` | `/api/accounts/<id>/refresh` | 重新登录刷新 Token |
| `POST` | `/api/accounts/<id>/ticket` | 获取 Ticket |
| `POST` | `/api/accounts/reset` | 重置所有故障计数 |
| `POST` | `/api/accounts/purge-invalid` | 清除无效账号 |
| `GET/PUT` | `/api/config` | 读取/修改系统配置 |
| `POST` | `/api/auto-register` | 手动触发注册 |
| `GET` | `/api/auto-register/config` | 注册配置状态 |
| `POST` | `/api/health-check/run` | 运行健康检查 |
| `GET` | `/api/mail/status` | 邮件服务状态 |
| `POST` | `/api/mail/check-code` | 查询验证码 |
| `GET` | `/api/accounts/emails` | 有邮箱的账号列表 |

## 配置

所有配置存储在 SQLite 数据库（`data/rita.db`），可通过 WebUI **配置管理** 页面实时修改。

首次启动会自动创建默认值，也可通过 `.env` 文件或环境变量覆盖。

### 核心配置

| Key | 默认值 | 说明 |
|-----|--------|------|
| `RITA_UPSTREAM` | `https://api_v2.rita.ai` | 上游 API 地址 |
| `RITA_ORIGIN` | `https://www.rita.ai` | 请求 Origin 头 |
| `AUTH_TOKEN` | *(空)* | 管理面板密码，留空则无需登录 |
| `DISABLE_SSL_VERIFY` | `1` | 跳过上游 SSL 验证 |
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `10089` | 监听端口 |

### 自动注册配置

| Key | 默认值 | 说明 |
|-----|--------|------|
| `AUTO_REGISTER_ENABLED` | `0` | 启用后台自动补号 |
| `AUTO_REGISTER_MIN_ACTIVE` | `2` | 活跃账号低于此值时触发补号 |
| `AUTO_REGISTER_BATCH` | `1` | 每次补号注册数量 |
| `AUTO_REGISTER_PASSWORD` | `@qazwsx123456` | 注册时设置的默认密码 |
| `YESCAPTCHA_KEY` | *(空)* | YesCaptcha API Key |
| `GPTMAIL_API_KEY` | *(空)* | GPTMail API Key |
| `GPTMAIL_API_BASE` | `https://mail.chatgpt.org.uk` | GPTMail API 地址 |
| `YYDSMAIL_API_KEY` | *(空)* | YYDSMail API Key |
| `YYDSMAIL_API_BASE` | `https://maliapi.215.im/v1` | YYDSMail API 地址 |

### 注册流程

```
1. GPTMail 创建临时邮箱
2. gosplit authenticate (初始化会话)
3. gosplit sign_process (提交邮箱 + agree)
4. YesCaptcha 解决 reCAPTCHA v2 (最多重试 4 次)
5. gosplit sign_process (提交 captcha, 含 email+agree)
6. gosplit emailCode (显式触发验证码发送)
7. 轮询邮箱获取验证码 (90s 超时, 最多重发 2 次)
8. gosplit code_sign (提交验证码, 失败自动重发重试)
9. gosplit authenticate (获取 token + ticket)
10. gosplit silent_edit (设置密码)
```

## 点数系统

每个账号初始 100 点，不同模型消耗不同点数（与 Rita 官方一致）：

| 点数 | 模型 |
|------|------|
| 0 | Rita, GPT-4.1-nano, GPT-5-nano |
| 1 | Rita-Pro, GPT-4.1-mini, GPT-5-mini, Gemini-2.5-Pro-0605, Gemini-3-Flash, DeepSeek-V3, DeepSeek-R1 |
| 2 | DeepSeek-V3.1 |
| 4-5 | GPT-4o, GPT-4.1, GPT-5, GPT-5.1, GPT-5.2, GPT-5.4, GPT-o3/o4-mini, Grok-4/4.1, Gemini-2.5-Pro, Gemini-3.1-Pro, Perplexity 系列 |
| 7-8 | Grok-3, Claude-3.7-Sonnet, Claude-4-Sonnet, Claude-4.5-Sonnet (含 Thinking) |
| 10-16 | Gemini-3.1-Pro-Thinking, GPT-5.1-Thinking, Claude-Opus-4.5/4.6 (含 Thinking), 图像模型 |
| 35-45 | Claude-4-Opus (含 Thinking), Claude-Sonnet-4.6, 图像直连模型 |

## API 鉴权

设置 `AUTH_TOKEN` 后，所有接口（包括 `/v1/*` 代理接口）都需要鉴权：

```bash
# OpenAI 客户端标准方式
curl http://localhost:10089/v1/chat/completions \
  -H "Authorization: Bearer your-auth-token" \
  -H "Content-Type: application/json" \
  -d '{"model":"model_25","messages":[{"role":"user","content":"hello"}]}'
```

也支持 query 参数 `?auth=your-auth-token` 或 session cookie（WebUI 登录后自动设置）。

`AUTH_TOKEN` 为空时所有接口无需鉴权。

## WebUI 管理面板

五个功能 Tab：

- **聊天广场** 选择模型直接对话，支持模型目录浏览
- **账号注册** 查看注册配置状态，手动触发注册，实时日志
- **账号管理** 账号列表（以邮箱为主键），批量操作（全选/勾选 → 批量测试/启用/禁用/刷新/删除），点数显示
- **邮箱服务** GPTMail/YYDSMail 状态，从已注册账号选择邮箱查询验证码
- **配置管理** 按语义分组的 inline 编辑，布尔值用 Toggle 开关，敏感值显示/隐藏，实时搜索

## 项目结构

```
rita2api/
  server.py          # Flask 主服务：路由、代理、API
  accounts.py        # 账号管理器：CRUD、轮换、健康检查
  auto_register.py   # 自动注册：完整注册流程、Token 刷新
  database.py        # SQLite 数据层：账号、配置、用量日志
  quota.py           # 模型点数计费
  migrate.py         # JSON → SQLite 数据迁移
  templates/
    index.html       # WebUI 单页面（5 Tab）
  data/
    rita.db           # SQLite 数据库（自动创建）
  register/          # 独立批量注册工具（参考实现）
```

## 使用示例

### Chat Completions API

```bash
# 非流式对话
curl http://localhost:10089/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-auth-token" \
  -d '{"model":"model_25","messages":[{"role":"user","content":"你好"}]}'

# 流式对话
curl http://localhost:10089/v1/chat/completions \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-auth-token" \
  -d '{"model":"model_25","messages":[{"role":"user","content":"你好"}],"stream":true}'
```

### Responses API

```bash
# 非流式
curl http://localhost:10089/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-auth-token" \
  -d '{"model":"model_25","input":"你好","instructions":"你是一个友好的助手"}'

# 流式 (SSE)
curl http://localhost:10089/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-auth-token" \
  -d '{"model":"model_25","input":"你好","stream":true}'

# 多轮对话 (message array)
curl http://localhost:10089/v1/responses \
  -H "Content-Type: application/json" \
  -H "Authorization: Bearer your-auth-token" \
  -d '{
    "model":"model_25",
    "instructions":"你是一个友好的助手",
    "input":[
      {"role":"user","content":"我叫小明"},
      {"role":"assistant","content":"你好小明！"},
      {"role":"user","content":"你还记得我叫什么吗？"}
    ]
  }'
```

```bash
# 查看模型列表
curl http://localhost:10089/v1/models
```

## License

CC BY-NC-SA 4.0
