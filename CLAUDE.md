# CLAUDE.md — Ocoopa 舆情监控 Agent

内部 Ocoopa 舆情/PR 风险监控 Agent,已在 Railway 生产试运行,当前围绕 **CPSC 召回 26-659** 做专项跟踪。背景见 [docs/OVERVIEW.md](docs/OVERVIEW.md),运维见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 三条命脉(任何改动不得牺牲)
**时效性 · 防漏报 · 防臆造。** 评审任何改动先问会不会伤这三条。

## 红线(违反=事故)
- **绝不自动对外发布、绝不自动回复**;本系统只做内部监控/告警。
- **合规抓取**:只抓公开内容,不登录、不抓付费墙、不绕反爬、遵守 robots / ToS。
- **防臆造**:模型只能基于抓取原文,不得编造事实或来源;`key_quotes` 必须是原文逐字子串;证据校验不通过 → 标 `needs_human_review`,不得作为既成事实。
- **红色告警触发条件 = `risk_level=="red" and requires_escalation`**,仅此。证据/置信只影响**文案与 @**,**不得**决定发不发(曾因加了 evidence 门导致静默漏报,见下)。
- **不接境外模型**(法务红线):真实 LLM 用 DeepSeek,且必须走 `LLMProvider` 抽象。
- **召回事实以 [docs/CURRENT_RECALL_26-659.md](docs/CURRENT_RECALL_26-659.md) 为唯一基线**(人工核验)。搜索摘要/社媒转述/模型输出**不得**升级为已确认事实;已知召回的普通转载登记为"新闻转载/社媒扩散",不得当作新增伤亡或起火。
- **覆盖度不得夸大**:社媒非全量,某平台零记录 ≠ 该平台无讨论。对外表述必须保留这一限定。

## 架构与约定
- 代码在 `src/ocoopa_monitor/`:`pipeline`(抓取→去重→分析→告警)、`analysis`/`llm`/`risk`/`evidence`、`fetchers/`、`db`、`reports`、`scheduler`、`delivery`/`outbox`、`recall`(召回专项台账)、`syndication`(传播角色/簇)、`topics`、`social`/`public_social_intake`、`interventions`、`operational`、web 渲染层(`review_web`/`console_web`/`analysis_web`/`analysis_details`)、`session_auth`、`api`、`cli`、`sources`、`keywords`、`config`、`normalize`。
- **双数据库后端**:本地/测试 SQLite,生产 PostgreSQL,由 `create_database(settings)` 按 `OCOOPA_DB_URL`/`DATABASE_URL` 选择。`db.py` 里 `Database`(SQLite,`?` 占位)与 `PostgresDatabase`(`%s`、`RETURNING`)是**两套并行实现 —— 任何 db 方法改动必须两边同步改**。
- **两个生产服务**(同一镜像/代码):scheduler(`railway.json` → `cli scheduler`)和 web(`railway.web.json` → `uvicorn ocoopa_monitor.api:app`),连同一个 Postgres。两者密钥不共用,`doctor --role scheduler|web` 分别自检。
- **三条车道**:`high`(15min,只用免费源:Google News RSS + CPSC + AboutLawsuits + Reddit Atom)、`regular`(每小时:Brave 广泛公开社媒索引、GNews、Google News RSS；另有 WHIO/WPRI/KENS/KARE/Boston 25 五个 Facebook 媒体账号各自每天一次的 Brave 定向索引，合计约 29 次 Brave 调用/日)、`licensed`(5min,Brandwatch;三项凭据齐全才启用,首次静默回溯 30 天)。当前不启用 Brandwatch。
- **社媒发现边界与口径**:定向源仅查询公开索引,不登录 Facebook、不抓评论;每个新定向源首次成功采集必须静默入库。看板中的社媒母帖必须区分`广泛索引自动发现`、`媒体账号定向检索`和`人工补录兜底`;零记录只代表索引未返回,不得写成平台/账号没有讨论。
- **源健康不是覆盖证明**:`source_health` 保留每轮 `返回/有效/新或更新入库/过滤/重复/查询对象`。`ok`只表示调用成功;公开社媒源连续失败必须进非 @ 健康提醒，缺少 Brave Key 不得伪装为“成功但无结果”。新增这些字段时仍须同步 SQLite、PostgreSQL 与看板。
- **投递必须走 `delivery_outbox`**:先落库再发送,失败指数退避重试。别绕过 outbox 直接发网络请求。

## 改代码必知的坑
- **给已有表加列**:必须同时在 SQLite `Database._migrate_sqlite` 和 Postgres `PostgresDatabase._PG_COLUMN_MIGRATIONS`(幂等 `ALTER ... ADD COLUMN IF NOT EXISTS`)加迁移，并新增版本化 `migrations/*.sql`。`CREATE TABLE IF NOT EXISTS` 不会改已存在的表——漏了生产会报 `column ... does not exist`(CI 用全新表查不出)。Postgres 迁移执行器当前按分号切分语句，**SQL 注释中不得包含分号**。
- **给 `Settings` 加字段**:除 `config.py` 外,需更新 `tests/` 里所有 `Settings(...)` 构造 —— 目前分布在 5 个测试文件(`test_core`、`test_third_round_optimizations`、`test_recall_hardening`、`test_social_upgrade`、`test_public_social_intake`),漏一个就整套报 `missing positional argument`。
- **新增 web 逻辑**:放进 `review_web` / `console_web` / `analysis_web` / `analysis_details`(**这些模块一律不得 import fastapi**,才能单测);`api.py` 是唯一 import fastapi 的文件,只做路由薄封装(CI 不装 fastapi)。写操作要带 CSRF 与会话校验(见 `session_auth`)。
- 冷启动顺序由 `system_state`(bootstrap 标志)保证:未 bootstrap 时 pipeline 抑制实时告警。别绕过。`bootstrap()` 跑三条车道,且**高敏车道所有 P0 源必须全部成功**,否则抛错不标记(防止带着瞎掉的源进入实时)。
- **统计口径一律用不可变的 `first_seen_at`**(系统首次发现),不得用 `fetched_at`——后者每轮巡检都会变,用它会把旧记录算成当日新增。

## 命令速查
```bash
export PYTHONPATH="$PWD/src"
python3 -m unittest discover -s tests          # 全部测试(Postgres 用例需 OCOOPA_TEST_POSTGRES_URL 否则 skip)
python3 -m ocoopa_monitor.cli doctor --production --role scheduler|web   # 上线自检(按服务角色)
python3 -m ocoopa_monitor.cli bootstrap        # 冷启动:静默 backfill 三车道 + 标记
python3 -m ocoopa_monitor.cli scheduler        # 主循环
python3 -m ocoopa_monitor.cli review list|mark <incident_id> <confirmed|false_positive|muted> [--days N]  # 事件级,覆盖所有红/黄(不只告警过的)
python3 -m ocoopa_monitor.cli keyword list|add "<词>"|enable|disable [--category C --lane high|regular]
python3 -m ocoopa_monitor.cli recall list|export <path>|import-group <path>|sync [--limit N]  # 召回专项台账
```

## 协作纪律
- **走分支 + PR,不直推 main**(用 gh/API 合并)。提交信息中文/英文均可。
- 命脉(时效/漏报/臆造)、合规、预算、法律相关的取舍属业务决策 → 上交用户,不擅自拍板。
- 技术决策点照常拍板。

## 深入文档
| 主题 | 文件 |
|---|---|
| 非技术总览(给领导/法务) | [docs/OVERVIEW.md](docs/OVERVIEW.md) |
| 部署 / 冷启动 / 复核 / 控制台 / 配额 | [docs/OPERATIONS.md](docs/OPERATIONS.md) |
| 开发快速上手 / 环境变量 / 源默认 | [README.md](README.md) |
| **召回 26-659 事实基线(防臆造依据)** | [docs/CURRENT_RECALL_26-659.md](docs/CURRENT_RECALL_26-659.md)(核验状态/使用边界)+ [docs/RECALL_26_659_FACTS.md](docs/RECALL_26_659_FACTS.md)(明细:型号别名/售价/补救/批次码)。**两份并存,改任一份必须同步另一份,不得漂移**;与 CPSC / OCOOPA 官方页冲突时以官方页为准。 |
