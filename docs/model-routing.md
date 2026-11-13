# 模型能力与按需刷新

本分支整合上游 PR #452（截至 `28145a7`）。模型能力保存在数据库中，所有路由、模型展示、OAuth 校验和手动刷新复用 `refresh_credential_models`；进程内只管理正在运行的刷新任务，不缓存模型集合。

## 能力有效期

- `supported_models` 保存真实上游 ID；展示时补充现有 Claude 别名。`models_checked_at` 为 `NULL` 表示未知，成功的空列表表示没有可用模型。
- API 地址、project、access token 的组合散列保存在 `models_context`。上下文不同的旧能力不参与路由或展示，散列不导出到客户端。
- 成功结果有效 300 秒。模型列表查询和生成请求按需触发刷新；空闲时没有定时扫描。
- 查询携带 project，超时 15 秒；过滤名称含 `disabled` 和详情中 `disabled` / `isDisabled` 为 `true` 的模型，保留零额度模型。
- 失败后自动刷新退避 30 秒。同上下文保留已知能力，新上下文或首次失败保持未知；失败保留已保存的 Paid 值和检查时间。
- 同进程同上下文的并发刷新合并，全局最多 8 个探测。手动刷新绕过有效期和退避，也能升级正在排队的自动刷新。
- 条件写入拒绝旧 token/project 的迟到结果。其他进程新写入的成功能力不会被更早的失败覆盖。

## 路由与永久禁用

先排除凭证禁用、模型家族冷却、永久模型禁用和本次已失败的凭证，再按数据库能力选择。未知或过期能力通过统一入口刷新，每个候选至多校验一次。

免费优先在会话和 sticky 亲和之前生效：优先明确支持目标模型的 Non-paid，再回退其他可用凭证。未知 Paid 不计入免费池，Tier 不用于判定模型权限；Paid 专属模型直接按能力路由。

生成请求的 HTTP 404 写入独立的永久禁用表，有效能力和模型并集立即排除该模型。刷新、重启、重新导入或其他进程不会恢复它；模型发现接口的 404 不写入永久禁用。404 换凭证独立于普通重试预算，候选耗尽后返回最后一个原始 404。流式请求在切换后直接发起下一次生成；已经输出内容的流中断后不重放请求。

普通错误保留原有重试开关、次数、间隔和自定义错误码，自动禁用判断与重试独立。

## 面板和兼容

- `POST /creds/refresh-models/{email}` 手动刷新并返回模型列表、数量、检查时间和 Paid。
- `/creds/batch-action` 接受 `refresh_models`；面板批量刷新复用批量任务的进度、停止和互斥逻辑。
- `/creds/model-status/{email}` 保持 `{email, paid}` 响应；普通读取复用已保存的 Paid，`refresh=true` 调用统一入口。
- 凭证摘要提供有效 `supported_models` 和 `models_checked_at`。模型弹窗展示数量、时间、列表、刷新和错误状态，支持键盘、手机和横屏。
- 模型列表查询刷新所有需要更新的启用凭证后返回完整并集，没有 20 个凭证的限制。冷却不影响展示，永久禁用影响展示。
- OAuth 添加和校验会刷新能力；能力发现失败不阻断凭证保存。导入复用未过期 token，同邮箱更新保留 Paid 和永久禁用；导入的模型列表作为未验证缓存，不能授予路由能力。
- 导出增加 `supported_models` 和 `models_checked_at`，旧格式仍可导入。同上下文已有的有效数据库能力不被导入列表覆盖。
- 去重保留原有状态合并规则。保留项 token/project 变化使能力失效，不合并不同凭证的能力列表。
- SQLite 和 PostgreSQL 使用补列迁移，保留凭证、配置、请求日志和永久禁用记录。

## 验证记录（2026-11-13）

Python 使用仓库现有 `.venv/Scripts/python.exe`，前端使用 `pnpm`，没有更换依赖管理方式或创建虚拟环境。

```bash
# GCLI_TEST_POSTGRESQL_URI 必须指向隔离的测试数据库
GCLI_TEST_POSTGRESQL_URI=<isolated-test-uri> .venv/Scripts/python.exe -m pytest tests -q --tb=short
node --test tests/test_batch_verification.mjs tests/test_error_codes.mjs
cd frontend
pnpm build
cd ..
node tests/credential-browser.mjs
```

Python 311 项通过：原有模型路由 202 项、凭证功能 81 项、新增刷新回归 28 项。补列、能力条件写入和状态保留在真实 PostgreSQL 16 和 SQLite 上验证。前端批量任务与错误码检查通过；浏览器覆盖模型弹窗、成功空列表、刷新失败与恢复、Paid、批量刷新停止/部分失败、批量校验、去重、桌面/手机/横屏及减少动态效果。

浏览器脚本将报告和截图保存到 `/tmp/gcli-credential-browser-proof/`。后端全量回归输出为 `/tmp/gcli-final-python-tests.log`。`dist` 由本次 `pnpm build` 重新生成。

验证使用模拟上游 HTTP/OAuth 响应，没有对真实账号发起生成或能力探测。多进程之间允许重复探测，统一读取数据库能力和永久禁用结果。
