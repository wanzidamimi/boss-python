# Boss 招聘采集台（还在测试中）

> **项目状态：还在测试中。** 站点结构、接口字段和风控策略可能变化，采集逻辑会持续调整；请先小批量试跑。

![Boss 招聘采集控制台](docs/images/web-ui.png)

一个带 Web 控制台的 BOSS 直聘职位采集工具：浏览器负责登录态与令牌轮换，详情请求走页面自身请求链路，任务队列串行限速；职位列表与详情分表存储，详情按内容哈希版本化，并支持 RedisBloom 去重、实时进度查看与 CSV / Excel 导出。

## 主要功能

### 采集能力

- **两种采集模式**：推荐职位流、关键词搜索。
- **城市选择**：输入中文城市名自动匹配编码，内置 374 个城市。
- **分页参数**：页数 1-10，每页 1-15 条。
- **翻页延迟**：默认 3 秒，可在页面调整并记忆到本机 Cookie。
- **详情采集**：支持「全部 / 不抓 / 前 N 条」，相邻请求间隔可调，默认 20 秒。
- **浏览器详情引擎**：在采集浏览器中调用页面自身 axios 实例，由站点响应拦截器处理 `__zp_stoken__` 轮换。
- **详情自愈**：页面被重置导致请求上下文丢失时自动重建；遇到 35/36/37 风控码自动刷新并重试；页面不是招聘 SPA 时自动回到列表页。
- **纯接口回退**：没有可用浏览器时回退到 requests 引擎，并执行令牌刷新与风控重试。
- **RedisBloom 去重**：按跨任务稳定的 `encrypt_job_id` 判断是否已采过，按自然日分桶、TTL 1 天；布隆阳性再查 MySQL 确认，Redis 不可用时自动降级。
- **历史详情回填**：`backfill_details.py` 可为已采集职位补详情，支持断点续跑、去重和风控自动停止。

### 账号与登录态

- **多账号管理**：每个账号独立 Chrome profile 与独立 CDP 端口，登录态互不串号。
- **人工登录**：添加账号后弹出独立浏览器，手动扫码或手机号登录，工具自动抓取并保存 Cookie。
- **登录态探活**：任务提交前和执行前都会检测凭证；失效账号会被拦截并提示重新登录。
- **自动轮询账号**：任务不指定账号时，自动选择最久未使用的有效账号。
- **登录态打开职位**：职位列表的「打开」按钮使用带登录态的浏览器打开详情页，只允许 `zhipin.com` 链接。
- **风控冷却**：账号命中详情风控后记录冷却截止时间，避免继续触发安全校验。

### 数据与存储

- **列表与详情分表**：列表写入 `jobs`，详情写入 `job_details`，通过 `lid` 关联。
- **详情版本化**：对业务字段规范化后计算 SHA-256；内容变化才新增版本，未变化只刷新采集时间、观测次数等易变字段，同时保留完整 `raw_json`。
- **请求日志**：`detail_requests` 记录每次详情请求的返回码与时间，便于排查配额和风控。
- **导出**：支持按任务 ID、关键词、城市筛选，导出 CSV / Excel，可选附带详情列。

### Web 控制台

- **实时统计**：职位总量、任务总数、今日任务、失败任务、详情版本数与接口健康状态。
- **任务进度**：运行中的任务显示迷你进度条；点击任务 ID 打开进度弹窗，每 1.5 秒刷新，展示阶段、成功/失败/跳过/总数、预计剩余时间、当前职位和最近事件。
- **任务日志**：每个任务写入 `data/logs/task_<id>.log`，记录参数、阶段、子进程输出、详情信封汇总和异常堆栈。
- **职位浏览**：列表筛选与分页；详情抽屉展示职位描述、技能标签、公司介绍、福利、地址与静态地图、BOSS 头像与活跃状态、版本数和采集时间。
- **账号管理**：添加、删除、重新登录，实时展示登录态探活结果。
- **一键启动**：双击 `启动BOSS服务.bat` 启动服务并打开控制台。

## 快速开始

### 1. 环境要求

- Python 3.10+（当前开发使用 Python 3.13）
- MySQL 5.7+
- Chrome / Chromium（账号登录和浏览器详情引擎）
- Docker（可选，用于 MySQL 与 RedisBloom）

```powershell
python -m pip install -r requirements.txt
```

### 2. 启动 MySQL

项目默认连接 `127.0.0.1:3307` 的 `boss_recruit` 库。表结构见 `sql/init.sql`。

也可以使用 compose 中的隔离实例：

```powershell
docker compose --profile standalone up -d
```

> 注意：compose 中的 MySQL 映射到宿主机 `3308`，端口与默认配置不同；如需使用，请在 `.env` 中把 `BOSS_DB_PORT` 改为 `3308`，并填写对应的 `MYSQL_PASSWORD`。

### 3. 启动 RedisBloom（可选）

```powershell
docker compose up -d redis-bloom
```

使用包含 RedisBloom 模块的 `redis-stack-server` 镜像，映射到 `127.0.0.1:6380`。

### 4. 配置环境变量

复制 `.env.example` 为 `.env`，填写数据库和 Redis 配置。`.env` 已被 `.gitignore` 排除，不会提交。

主要配置项：

| 变量 | 默认值 | 说明 |
|---|---|---|
| `BOSS_DB_HOST` / `BOSS_DB_PORT` | `127.0.0.1` / `3307` | MySQL 地址 |
| `BOSS_DB_USER` / `BOSS_DB_PASSWORD` | `boss` / 空 | MySQL 账号 |
| `BOSS_DB_NAME` | `boss_recruit` | 数据库名 |
| `BOSS_REDIS_URL` | `redis://127.0.0.1:6380/0` | RedisBloom 地址 |
| `BOSS_BLOOM_ENABLED` | `1` | 是否启用详情去重 |
| `BOSS_BLOOM_TTL_DAYS` | `1` | 去重窗口（自然日） |
| `BOSS_BLOOM_ERROR_RATE` / `BOSS_BLOOM_CAPACITY` | `0.01` / `100000` | 布隆误判率与容量 |
| `BOSS_BLOOM_PREFIX` | `boss:detail:bf:` | 布隆 key 前缀 |
| `BOSS_MIN_TASK_INTERVAL` | `300` | 任务之间最小间隔（秒） |
| `BOSS_STOKEN_WAIT` | `60` | 刷新令牌后的等待（秒） |
| `BOSS_CDP_PORT_BASE` | `9300` | 账号 CDP 端口起始值 |
| `BOSS_CHROME_PATH` | Chrome 默认路径 | 指定 Chrome 可执行文件 |

### 5. 启动服务

双击 `启动BOSS服务.bat`，浏览器会自动打开：

```text
http://127.0.0.1:8899
```

也可以手动启动：

```powershell
python -m uvicorn app:app --host 127.0.0.1 --port 8899
```

### 6. 使用流程

1. 在「账号管理」添加账号，在弹出的独立浏览器里手动登录；
2. 在「新建采集任务」选择模式、账号、关键词、城市、页数与详情设置；
3. 提交后任务进入串行队列，点击任务 ID 查看实时进度与日志；
4. 在「职位数据」筛选、查看详情并导出 CSV / Excel。

## 项目结构

```text
.
├── app.py                      # FastAPI 服务、任务队列、账号与查询接口
├── job_api.py                  # 列表与详情采集，浏览器 / requests 双引擎
├── browser_detail.py           # 页面 axios 详情引擎、上下文重建、风控重试
├── detail_hash.py              # 详情字段规范化、SHA-256、入库映射
├── detail_dedupe.py            # RedisBloom 日桶去重
├── progress_store.py           # 任务进度与事件旁路存储
├── backfill_details.py         # 历史详情回填
├── db.py                       # MySQL 访问层、幂等建表与迁移
├── account_manager.py          # 多账号浏览器、登录态抓取与探活
├── stoken_browser.py           # CDP 刷新 stoken 与 Cookie
├── config.py                   # 项目配置与环境变量加载
├── static/index.html           # Web 控制台
├── sql/init.sql                # 数据库初始化脚本
├── tests/                      # 单元测试与 MySQL / Redis 集成测试
├── docs/images/web-ui.png      # README 界面截图
└── docker-compose.yml          # RedisBloom 与隔离 MySQL
```

运行期文件均位于 `.gitignore` 排除的 `data/` 下：

- `data/accounts/<id>.json`：账号凭证；
- `data/profiles/<id>/`：账号独立浏览器 profile；
- `data/progress/task_<id>.json`：任务进度（7 天自动清理）；
- `data/logs/task_<id>.log`：任务日志；
- `data/task_<id>_*.json/csv`：列表与详情产物；
- `data/exports/`：导出的 CSV / Excel 文件。

## 主要接口

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/` | Web 控制台 |
| GET | `/api/stats` | 统计信息 |
| GET | `/api/cities` | 城市码表 |
| GET | `/api/accounts` | 账号列表（含探活） |
| POST | `/api/accounts/login` | 添加账号并打开登录浏览器 |
| GET | `/api/accounts/{id}/login-status` | 查询登录状态 |
| POST | `/api/accounts/{id}/relogin` | 重新登录 |
| DELETE | `/api/accounts/{id}` | 删除账号 |
| POST | `/api/open-link` | 在登录态浏览器打开职位链接 |
| GET / POST | `/api/tasks` | 任务列表 / 新建任务 |
| GET | `/api/tasks/{id}` | 任务详情 |
| GET | `/api/tasks/{id}/progress` | 任务实时进度 |
| GET | `/api/tasks/{id}/log` | 任务日志 |
| GET | `/api/jobs` | 职位查询（带详情标记） |
| GET | `/api/details/{lid}` | 职位最新详情与版本列表 |
| GET | `/api/export` | 导出 CSV / Excel（`with_details=1` 含详情） |

## 测试

运行单元测试：

```powershell
python -m unittest discover -s tests -v
```

测试覆盖详情哈希与版本决策、RedisBloom 去重、详情入库与进度存储。涉及 MySQL / Redis 的测试需要对应实例可用。

## 说明与技术细节

- **去重语义**：`BOSS_BLOOM_TTL_DAYS=1` 表示按自然日去重，跨天视为未采集；设为 2 可检查今天和昨天两个桶。
- **详情引擎**：依赖页面 SPA 的 webpack 模块。站点大版本更新后若模块结构变化，会自动回退到 requests 引擎，此时重新受每令牌约 5 条限制，但会定期刷新令牌。
- **登录态隔离**：不同账号使用不同 Chrome profile 和 CDP 端口，避免 Cookie 串号。
- **安全边界**：凭证仅存本机；`.env`、账号 Cookie、浏览器 profile、日志、导出文件和截图临时目录均不提交到仓库。
- **合规**：仅用于学习研究，内置任务串行限速与请求间隔。请遵守目标站点服务条款，控制访问频率，不要用于骚扰、爬取隐私数据或其他违规用途。
