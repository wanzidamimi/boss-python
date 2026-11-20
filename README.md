# Boss 招聘采集台

一个带 Web 控制台的 BOSS 直聘职位采集工具：浏览器负责生成与轮换令牌，详情请求走页面自身请求链路，任务队列串行限速，多账号登录态隔离；职位与详情分表存储，详情按内容哈希做版本化，并支持 RedisBloom 去重、实时进度查看与 CSV/Excel 导出。

## 主要功能

### 采集能力

- **两种采集模式**：推荐职位流与关键词搜索；支持城市中文选择（内置 374 个城市）、页数 1-10、每页 1-15 条。
- **翻页延迟可调**：默认 3 秒，可自行调整并记忆到本机 Cookie。
- **详情采集（浏览器引擎）**：详情请求在采集浏览器内调用页面自身的 axios 实例，由站点的响应拦截器自动轮换 `__zp_stoken__`，因此不再受"每个令牌约 5 条"的限制；支持"全部 / 不抓 / 前 N 条"，相邻请求间隔可调（默认 20 秒）。
- **详情自愈**：页面被站点重置（重载/安全校验）导致请求上下文丢失时自动重建；遇到 35/36/37 风控码自动刷新页面并重试；页面不是招聘 SPA 时自动导航到列表页；连续上下文失败快速中止，不空转。
- **纯接口回退**：没有可用浏览器时自动退回 requests 引擎，并保留"每 5 条刷新令牌"的自愈逻辑。
- **RedisBloom 详情去重**：按跨任务稳定的 `encrypt_job_id` 判断是否已采过；按自然日分桶、TTL 1 天，跨天自动作废重新采集；布隆阳性再用 MySQL 确认，误判不会漏采；Redis 不可用时自动降级为全部采集。
- **历史详情回填**：`backfill_details.py` 可为已采集职位补详情，支持断点续跑、去重、风控自动停止。

### 账号与登录态

- **多账号管理**：每个账号独立 Chrome profile + 独立 CDP 端口，登录态互不串号；支持添加、切换、删除、重新登录。
- **人工登录**：点击添加后弹出独立浏览器，用户手动扫码/手机号登录，工具自动抓取并保存 Cookie。
- **登录态探活与拦截**：任务提交前和执行前都会检测凭证，失效账号会被拦截并提示重新登录，不会用失效凭证跑任务。
- **自动轮询账号**：任务不指定账号时，自动选择最久未使用的有效账号，分散采集频率。
- **登录态浏览器打开职位**：职位列表的"打开"按钮在带登录态的浏览器中打开详情页，而非无登录态的默认浏览器；只允许 zhipin.com 链接。

### 数据与存储

- **列表与详情分表**：列表数据存 `jobs`，详情存 `job_details`，通过 `lid` 关联；详情表额外记录稳定的 `encrypt_job_id` 便于去重。
- **详情版本化**：对 24 个业务字段做规范化后计算 SHA-256；内容变化才新增版本，未变化只刷新采集时间、观测次数和活跃状态等易变字段；同时保留完整 `raw_json`，方便以后补字段。
- **请求日志**：`detail_requests` 记录每次详情请求的返回码与时间，便于排查配额和风控问题。
- **导出**：支持按任务 ID、关键词、城市筛选，导出 CSV / Excel，可选附带详情列。

### Web 控制台

- **实时统计**：职位总量、任务总数、今日任务、失败任务、详情版本数、接口健康状态。
- **任务进度可视化**：运行中的任务在列表里显示迷你进度条；点击任务 ID 打开进度弹窗，每 1.5 秒刷新，展示阶段（排队/准备/令牌/列表/详情/入库）、进度条、成功/失败/跳过/总数、预计剩余时间、当前职位和最近事件。
- **任务日志**：每个任务一个日志文件（`data/logs/task_<id>.log`），记录参数、阶段、子进程输出、详情信封汇总和异常堆栈；可通过 `GET /api/tasks/{id}/log` 查看。
- **职位数据浏览**：列表筛选、分页展示；点击"查看"打开详情抽屉，展示职位描述（保留换行）、技能标签、公司介绍、福利、地址与静态地图、BOSS 头像与活跃状态、版本数与采集时间。
- **账号管理界面**：添加/删除/重新登录，实时探活状态标红提示。
- **一键启动**：双击 `启动BOSS服务.bat` 启动服务并自动打开控制台页面。

## 快速开始

### 1. 依赖

```bash
pip install -r requirements.txt
```

要求 Python 3.10+（项目使用 3.13）。

### 2. MySQL

默认连接 `127.0.0.1:3307` 的 `boss_recruit` 库（表结构见 `sql/init.sql`）。也可以用项目自带的隔离实例：

```bash
docker compose --profile standalone up -d
```

### 3. RedisBloom（详情去重用，可选）

```bash
docker compose up -d redis-bloom
```

使用 `redis-stack-server` 镜像（含 RedisBloom），映射到 `127.0.0.1:6380`，数据持久化在独立 volume。

### 4. 配置

复制 `.env.example` 为 `.env`，填写数据库与 Redis 配置（`.env` 已被 gitignore 排除）。

### 5. 启动

双击 `启动BOSS服务.bat`，浏览器会自动打开 `http://127.0.0.1:8899`。

### 6. 使用流程

1. 在「账号管理」添加账号，弹出的浏览器里手动扫码/手机号登录；
2. 在「新建采集任务」选择模式、账号、关键词、城市、页数与详情设置；
3. 提交后任务进入串行队列，可在任务列表点击任务 ID 查看实时进度；
4. 采集完成后在「职位数据」查看详情、筛选并导出。

## 配置项

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BOSS_DB_HOST` / `BOSS_DB_PORT` | `127.0.0.1` / `3307` | MySQL 地址 |
| `BOSS_DB_USER` / `BOSS_DB_PASSWORD` | `boss` / 空 | MySQL 账号 |
| `BOSS_DB_NAME` | `boss_recruit` | 数据库名 |
| `BOSS_REDIS_URL` | `redis://127.0.0.1:6380/0` | RedisBloom 地址 |
| `BOSS_BLOOM_ENABLED` | `1` | 是否启用详情去重 |
| `BOSS_BLOOM_TTL_DAYS` | `1` | 去重窗口（自然日），跨天作废 |
| `BOSS_BLOOM_ERROR_RATE` / `BOSS_BLOOM_CAPACITY` | `0.01` / `100000` | 布隆误判率与容量 |
| `BOSS_BLOOM_PREFIX` | `boss:detail:bf:` | 布隆 key 前缀 |
| `BOSS_MIN_TASK_INTERVAL` | `300` | 任务之间最小间隔（秒） |
| `BOSS_STOKEN_WAIT` | `60` | 刷新令牌后的等待（秒） |
| `BOSS_CDP_PORT_BASE` | `9300` | 账号 CDP 端口起始值 |
| `BOSS_CHROME_PATH` | Chrome 默认路径 | 指定 Chrome 可执行文件 |

## 目录与数据文件

```
app.py                 FastAPI 服务、任务队列、账号与查询接口
job_api.py             列表 + 详情采集（浏览器/requests 双引擎）
browser_detail.py      页面 axios 详情引擎（上下文重建、风控重试）
detail_hash.py         详情字段规范化、SHA-256、入库映射
detail_dedupe.py       RedisBloom 日桶去重
progress_store.py      任务进度旁路存储
backfill_details.py    历史详情回填
db.py                  MySQL 访问层与幂等建表/迁移
account_manager.py     多账号浏览器、登录态抓取与探活
stoken_browser.py      CDP 刷新 stoken 与 Cookie
static/index.html      Web 控制台
sql/init.sql           数据库初始化脚本
tests/                 单元测试 + MySQL/Redis 集成测试
```

运行期文件（均在 gitignore 的 `data/` 下）：

- `data/accounts/<id>.json`：账号凭证；
- `data/profiles/<id>/`：账号独立浏览器 profile；
- `data/progress/task_<id>.json`：任务进度（7 天自动清理）；
- `data/logs/task_<id>.log`：任务日志；
- `data/task_<id>_*.json/csv`：列表与详情产物。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 控制台 |
| GET/POST | `/api/tasks` | 任务列表 / 新建任务 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/progress` | 任务实时进度 |
| GET | `/api/tasks/{id}/log` | 任务日志 |
| GET | `/api/jobs` | 职位查询（带详情标记） |
| GET | `/api/details/{lid}` | 职位最新详情与版本列表 |
| GET | `/api/accounts` | 账号列表（含探活） |
| POST | `/api/accounts/login` | 添加账号并打开登录浏览器 |
| POST | `/api/accounts/{id}/relogin` | 重新登录 |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| GET | `/api/stats` | 统计信息 |
| GET | `/api/export` | 导出 CSV/Excel（`with_details=1` 含详情） |
| GET | `/api/cities` | 城市码表 |

## 说明

- **详情去重语义**：`BOSS_BLOOM_TTL_DAYS=1` 表示按自然日去重，跨天视为未采集；如需"采集后 24 小时内跳过"，可设为 2（检查今天和昨天两个桶）。
- **详情引擎**：依赖页面 SPA 的 webpack 模块；站点大版本更新后若模块结构变化，会自动回退到 requests 引擎（此时重新受每令牌约 5 条限制，但会每 5 条刷新令牌）。
- **合规**：仅用于学习研究，内置任务串行限速与请求间隔，请遵守目标站点规则。
