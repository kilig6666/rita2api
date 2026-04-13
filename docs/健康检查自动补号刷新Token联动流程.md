# Rita2API 健康检查、自动补号、刷新 Token 联动流程说明

## 1. 文档目的

本文档用于说明本项目里三个容易被看成独立功能、但实际上互相联动的运行链路：

- 健康检查
- 自动补号
- 刷新 Token / 重新激活账号

重点回答的问题是：

1. 账号什么时候会被判为可用、失败、失效、禁用
2. 失效号是怎么被踢出池子的
3. 活跃账号不够时是谁来补号
4. 刷新 token 成功后，账号如何重新回到轮换池
5. 这三条链之间当前代码的真实衔接关系和边界在哪里

---

## 2. 一句话总览

本项目当前的运转闭环是：

**注册模块负责造号并入池 → 聊天请求持续消费账号池 → 健康检查周期性淘汰失效号 → 自动补号在活跃账号数或总剩余点数不足时补新号 → 刷新 token / 手动 re-activate 负责把旧号拉回池子。**

---

## 3. 四个状态视角

理解这三个模块之前，先把账号的几个关键状态分清楚。

### 3.1 启用状态 `enabled`

- `enabled=1`：账号允许参与候选
- `enabled=0`：账号不参与正常轮换

### 3.2 失败计数 `failures`

- 普通请求失败时会累加
- 当前阈值是 `3`
- 达到阈值后，虽然账号仍可能是 `enabled=1`，但 `next()` 会跳过它

### 3.3 Token 有效状态 `token_valid`

- 常用于标记 token 是否已经被判失效
- 尤其是健康检查发现 401 时会置为 `0`

### 3.4 活跃状态 `active`

项目里的“活跃账号”并不是单独存库字段，而是由逻辑推导出来的：

- `enabled=1`
- 且 `failures < 3`

自动补号会把这个 `active` 数量作为一个阈值来源；同时还会额外参考 `summary().total_quota`，也就是当前账号池总剩余点数。

---

## 4. 服务启动时会同时拉起什么

服务启动后，会执行两条后台线程：

1. **Token 健康检查线程**
2. **自动补号线程**

也就是说，这两个机制默认是并行存在的，不需要手动额外启动。

它们各自负责：

- 健康检查：清理坏号、自动禁用 401 失效号
- 自动补号：当活跃号太少时自动注册新号

---

## 5. 健康检查线程：职责与真实行为

### 5.1 启动方式

`AccountManager.start_health_checker()` 在启动时拉一个后台线程：

- 线程名：`token-health-checker`
- 启动后先等待 30 秒
- 然后按 `HEALTH_CHECK_INTERVAL` 周期执行
- `HEALTH_CHECK_INTERVAL` 优先读 SQLite `config`，读不到再回退环境变量默认值
- 每轮循环 sleep 前都会重新读取一次，所以改完通常无需重启

### 5.2 检查对象

健康检查每轮只遍历：

- `enabled=1` 的账号

说明：

- 已被禁用的账号不会被这一轮后台自动检查再次测试
- 所以禁用号要么手动恢复，要么通过 refresh/re-activate 恢复后才会重新回到后台巡检视野

### 5.3 检查方式

每个账号通过 `test_account()` 检查：

- 请求 Rita 上游 `/aichat/categoryModels`
- 若返回 `code == 0`，视为账号可用

这是一个轻量但足够代表性的上游连通性检查。

### 5.4 检查成功后的处理

如果账号检查通过：

- `failures=0`
- `last_error=''`
- `token_valid=1`
- `disabled_reason=''`

这意味着后台健康检查不仅是“检测”，还是“修正状态”的过程：

- 某些临时失败号，如果后来恢复正常，会被它自动清理失败痕迹

### 5.5 普通失败后的处理

如果不是 401，只是普通错误：

- `failures += 1`
- `last_error = 错误信息`

此时账号未必立刻禁用，但它更接近被 `next()` 跳过的阈值。

### 5.6 401 的特殊处理

如果健康检查发现上游返回 401：

- `token_valid=0`
- `enabled=0`
- `disabled_reason='token_expired_401'`
- `last_error='Token expired (auto-disabled)'`

这一步是当前系统里**最关键的自动淘汰坏号机制**。

一旦这里触发，该账号后续：

- 不会再参与正常轮换
- 也不会再被后台健康检查线程继续巡检（因为它已经不是 `enabled=1`）

### 5.7 健康检查结果输出

后台健康检查会把本轮统计写到内存状态：

- `last_check`
- `total_checked`
- `ok`
- `failed`
- `auto_disabled`

前端/接口可以通过 `/api/health-check` 读取最近一次后台检查结果。

---

## 6. 手动健康检查接口：与后台线程的区别

项目还提供了一个手动触发接口：

- `POST /api/health-check/run`

它会：

1. 遍历当前所有账号
2. 挨个调用 `test_account()`
3. 返回本次检查结果汇总

### 6.1 与后台线程的相同点

- 都是调用 `test_account()` 打上游 `/aichat/categoryModels`
- 都会统计 `ok_count`
- 都会识别 401

### 6.2 与后台线程的关键不同点

当前代码里，这个手动接口对 401 的“自动禁用”实现并不真正落库。

原因是：

1. 它拿到的是 `acc = acm.get(...)` 的对象副本
2. 然后只改了 `acc.token_valid / acc.enabled / acc.disabled_reason / acc.last_error`
3. 最后调用 `acm._save()`
4. 但 `AccountManager._save()` 在 SQLite 版里是 no-op

因此当前真实行为是：

- **后台健康检查线程的 auto-disable 是会真正写库生效的**
- **手动 `/api/health-check/run` 里的 auto-disable 统计会返回，但不会真正持久化到数据库**

这是当前实现里一个很重要的事实。

---

## 7. 自动补号线程：职责与触发方式

### 7.1 启动前的前置校验

自动补号线程不是无条件启动，它在开始前会检查：

- `AUTO_REGISTER_ENABLED` 是否开启
- `YESCAPTCHA_KEY` 是否已配置
- 当前默认邮箱渠道是否配置完整

如果这些条件不满足，线程直接退出，不会进入循环。

### 7.2 启动后行为

线程启动后会：

1. 先等待 60 秒
2. 然后每隔固定间隔执行一次检查
3. 每次循环重新读取数据库配置
4. 每次循环都会重新计算 `active` / `total_quota` 与阈值的关系

重新读取配置意味着：

- 你在配置页改了 `AUTO_REGISTER_MIN_ACTIVE` / `AUTO_REGISTER_MIN_QUOTA` / `AUTO_REGISTER_BATCH` 等值后
- 不需要重启服务
- 下一轮循环就会生效

### 7.3 触发条件

当前自动补号真正使用的是：

- `AUTO_REGISTER_MIN_ACTIVE`
- `AUTO_REGISTER_MIN_QUOTA`
- `AUTO_REGISTER_BATCH`

逻辑是：

1. 读取 `summary().active`
2. 读取 `summary().total_quota`
3. 只要 `active < AUTO_REGISTER_MIN_ACTIVE` 或 `total_quota < AUTO_REGISTER_MIN_QUOTA`
4. 就会触发补号
5. 本轮最多只补 `AUTO_REGISTER_BATCH` 个

`check_config()` 也会把这三个值一并返回给前端：

- `min_active_accounts`
- `min_total_quota`
- `batch_size`

### 7.4 当前真实语义

也就是说，现在系统是：

- **按活跃账号数补号**
- **也按总剩余点数补号**
- **单轮补号数量再受 `AUTO_REGISTER_BATCH` 限制**

---

## 8. 自动补号与健康检查如何互相影响

这两个线程实际上是闭环关系。

### 8.1 健康检查把坏号踢出池子

一旦后台健康检查把某些账号判成：

- `enabled=0`
- 或 `failures` 增多到不再算 active

那么 `summary().active` 就会下降。

### 8.2 活跃数下降后触发补号

自动补号线程下一轮看到：

- `active` 低于 `AUTO_REGISTER_MIN_ACTIVE`

就会开始补新号。

所以可以理解成：

- 健康检查是“减法”
- 自动补号是“加法”

这两条链共同维持账号池的稳定供给。

---

## 9. 刷新 Token：什么时候需要它

刷新 token 的入口主要用于下面几类场景：

1. 账号邮箱还在，但 token 失效了
2. 账号被健康检查自动禁用了
3. 想尽量保留旧号，不直接删号重注册
4. 批量处理一批可恢复账号

项目提供了：

- 单号刷新：`POST /api/accounts/<id>/refresh`
- 批量刷新：`POST /api/accounts/batch-action` + `action=refresh`

---

## 10. 刷新 Token 的主链路

刷新 token 的核心实现是：

- `auto_register.refresh_account_token()`

它和“新注册”非常像，只是目标从“创建新账号”变成了“用已存在邮箱重新登录并拿新 token”。

流程大致是：

1. `authenticate` 初始化会话
2. `sign_process` 提交邮箱
3. 如需则解 captcha
4. `emailCode` 发送验证码
5. 轮询邮箱拿 OTP
6. `code_sign` 提交 OTP
7. 提取新 token
8. 再次 `authenticate` 获取新 ticket
9. 返回新 token / ticket

### 10.1 和新注册的共同点

共同点包括：

- 仍然需要邮箱验证码
- 仍然可能走 YesCaptcha
- 仍然使用同一套临时邮箱 provider 查询逻辑
- 仍然从 Gosplit 认证链拿 token/ticket

### 10.2 和新注册的差异

差异在于：

- 它不创建新邮箱
- 不新增账号记录
- 目标是更新已有账号的 token，而不是造一个新账号

---

## 11. 刷新成功后如何重新进入轮换池

### 11.1 单号刷新接口

`/api/accounts/<id>/refresh` 在刷新成功后，会调用：

- `acm.reactivate_account(account_id, new_token=new_token)`

### 11.2 `reactivate_account()` 的真实效果

当传入 `new_token` 时，它会：

- 更新 `token`
- `enabled=1`
- `token_valid=1`
- `failures=0`
- `last_error=''`
- `disabled_reason=''`
- `quota_remain=100`

也就是说，刷新成功不是只替换 token，而是做了一次完整“复活”：

- 重新启用
- 清空失败状态
- 清空禁用原因
- 重置点数到 100

### 11.3 对自动补号的反馈

刷新成功后，账号重新变成 active 候选。

这会直接影响：

- `summary().active` 增加
- 若该账号本身还有剩余点数，`summary().total_quota` 也会同步抬升
- 自动补号线程下一轮可能就不需要继续补号了

所以 refresh token 不只是局部修复，也是自动补号系统的一个“减压阀”。

---

## 12. 批量刷新与批量恢复

### 12.1 批量刷新

批量接口 `batch-action` 支持 `action=refresh`：

- 逐个读取账号邮箱信息
- 调 `refresh_account_token()`
- 成功后 `reactivate_account(..., new_token=...)`

它适合：

- 一批账号邮箱还在、只是 token 老化
- 想尽量保留老号，减少重新注册次数

### 12.2 纯恢复（不换 token）

项目还支持：

- `/api/accounts/<id>/reactivate`
- `batch-action` 的 `enable`

这类路径更多是“人工恢复状态”，区别在于：

- **reactivate with new token**：真正适合 token 已失效但已拿到新 token 的情况
- **单纯 enable/reactivate without new token**：只是把状态复原，不保证旧 token 一定仍然可用

---

## 13. Ticket 链路：比 refresh 更轻的一条修复路

项目还提供：

- `POST /api/accounts/<id>/ticket`

底层调用：

- `auto_register.relogin_for_ticket(acc.token)`

这条链路的特点是：

- 不走邮箱验证码
- 不刷新 token
- 只尝试用现有 token 重新拿一个 fresh ticket

适合的场景是：

- token 还活着
- 只是想重新获取 ticket 用于网页侧激活或其他后续动作

所以从重到轻，这几条修复链可以理解成：

1. **重新注册新号**
2. **refresh token（邮箱 OTP 重新登录）**
3. **relogin for ticket（基于现有 token 重新拿 ticket）**

---

## 14. 健康检查、刷新、自动补号三者的典型闭环

### 场景 A：账号被健康检查判 401

1. 后台健康检查命中 401
2. 账号被置为 `enabled=0, token_valid=0`
3. `summary().active` 下降
4. 自动补号线程后续可能开始补新号
5. 如果邮箱还可用，也可以手动 refresh token
6. refresh 成功后账号重新 active
7. 自动补号压力下降

### 场景 B：账号只是偶发失败，不是 401

1. 请求失败或健康检查失败
2. `failures` 增加
3. 只要还没到阈值，账号仍可参与后续轮换
4. 若后来请求成功或健康检查成功，会清 failures
5. 不一定需要 refresh，也不一定触发自动补号

### 场景 C：很多号持续失败导致 active 下降

1. 多个账号 failures 累积，active 数下降
2. 自动补号线程看到 active 不够
3. 自动注册新号补进池子
4. 如果老号邮箱还可恢复，也可手动批量 refresh
5. 最终变成“新号补充 + 旧号修复”并行

---

## 15. 当前实现里的几个关键注意点

### 15.1 后台健康检查会真实写库，手动 health-check/run 不会真正 auto-disable

这点非常重要。

所以如果你看到：

- `/api/health-check/run` 返回里说 auto-disabled 了若干个账号

不要立刻默认数据库里真的已经禁用了；当前这条手动链并没有真正持久化 401 禁用状态。

### 15.2 refresh 成功会把 quota 重置到 100

`reactivate_account(new_token=...)` 会显式把：

- `quota_remain=100`

这意味着 refresh 不只是“换 token”，还会把这个账号恢复成一个完整可用的新鲜状态。

### 15.3 自动补号优先补“数量”，不是优先修“旧号”

自动补号线程不会先尝试 refresh 老号；它只看：

- 当前活跃号够不够

不够就直接注册新号。

所以“修老号”目前还是更依赖：

- 手动 refresh
- 批量 refresh
- 手动 re-activate

### 15.4 已禁用号不会被后台健康检查继续巡检

因为后台线程只查 `enabled=1` 的账号。

所以：

- 一个被自动禁用的号，不会靠后台线程自己“恢复正常”
- 它必须经由 refresh、reactivate 或手工干预重新回到 enabled 状态

---

## 16. 推荐理解方式

如果把这个系统拆成职责分工，可以这样理解：

### 健康检查

负责：

- 定期扫描现有账号
- 发现并踢出真正失效号
- 清理临时失败痕迹

### 自动补号

负责：

- 当“可服务账号数”不足时补充供给
- 保障账号池不会因为失效/失败而见底

### refresh token / reactivate

负责：

- 把仍有价值的旧号拉回来
- 减少纯靠新注册补号的成本
- 在不换邮箱主体的前提下恢复账号池容量

这三条链并不是互相替代，而是：

- **健康检查负责淘汰**
- **自动补号负责新增**
- **refresh 负责修复**

---

## 17. 最终总结

本项目当前围绕账号可用性形成了一个比较清晰的维护闭环：

1. 注册模块把新号放进池子
2. 聊天流量持续消耗账号池
3. 健康检查定时识别并清退失效号
4. 自动补号在活跃号不足时直接补新号
5. refresh token / reactivate 把旧号尽量修回来
6. 修回来的号重新参与轮换，反过来降低补号压力

因此，这三条链合起来，本质上是在解决同一个问题：

**如何让账号池长期保持“有量、可用、可恢复”。**

---

## 18. 相关文档

- [注册整体流程](./注册整体流程.md)
- [注册后聊天请求轮换流程](./注册后聊天请求轮换流程.md)

