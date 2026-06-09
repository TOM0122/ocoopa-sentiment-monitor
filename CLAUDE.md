# CLAUDE.md — Ocoopa 舆情监控 Agent

内部 Ocoopa 舆情/PR 风险监控 Agent,已在 Railway 生产试运行。背景见 [docs/OVERVIEW.md](docs/OVERVIEW.md),运维见 [docs/OPERATIONS.md](docs/OPERATIONS.md)。

## 三条命脉(任何改动不得牺牲)
**时效性 · 防漏报 · 防臆造。** 评审任何改动先问会不会伤这三条。

## 红线(违反=事故)
- **绝不自动对外发布、绝不自动回复**;本系统只做内部监控/告警。
- **合规抓取**:只抓公开内容,不登录、不抓付费墙、不绕反爬、遵守 robots / ToS。
- **防臆造**:模型只能基于抓取原文,不得编造事实或来源;`key_quotes` 必须是原文逐字子串;证据校验不通过 → 标 `needs_human_review`,不得作为既成事实。
- **红色告警触发条件 = `risk_level=="red" and requires_escalation`**,仅此。证据/置信只影响**文案与 @**,**不得**决定发不发(曾因加了 evidence 门导致静默漏报,见下)。
- **不接境外模型**(法务红线):真实 LLM 用 DeepSeek,且必须走 `LLMProvider` 抽象。

## 架构与约定
- 代码在 `src/ocoopa_monitor/`:`pipeline`(抓取→去重→分析→告警)、`analysis`/`llm`/`risk`/`evidence`、`fetchers/`、`db`、`reports`、`scheduler`、`delivery`、`review_web`、`console_web`、`api`、`cli`、`sources`、`keywords`、`config`、`normalize`。
- **双数据库后端**:本地/测试 SQLite,生产 PostgreSQL,由 `create_database(settings)` 按 `OCOOPA_DB_URL`/`DATABASE_URL` 选择。`db.py` 里 `Database`(SQLite,`?` 占位)与 `PostgresDatabase`(`%s`、`RETURNING`)是**两套并行实现 —— 任何 db 方法改动必须两边同步改**。
- **两个生产服务**(同一镜像/代码):scheduler(`railway.json` → `cli scheduler`)和 web(`railway.web.json` → `uvicorn ocoopa_monitor.api:app`),连同一个 Postgres。
- 高敏车道(15min)只用免费源(Google News RSS + CPSC + AboutLawsuits 法律源);商业 API(Brave/GNews)在常规车道(每小时)以守免费配额。

## 改代码必知的坑
- **给已有表加列**:必须同时在 SQLite `Database._migrate_sqlite` 和 Postgres `PostgresDatabase._PG_COLUMN_MIGRATIONS`(幂等 `ALTER ... ADD COLUMN IF NOT EXISTS`)加迁移。`CREATE TABLE IF NOT EXISTS` 不会改已存在的表——漏了生产会报 `column ... does not exist`(CI 用全新表查不出)。
- **给 `Settings` 加字段**:除 `config.py` 外,需更新 `tests/` 里所有 `Settings(...)` 构造(test_core 的 `settings()` 助手 + doctor 测试 + factory 测试 + test_third_round_optimizations)。
- **新增 web 逻辑**:放进 `review_web.py` / `console_web.py`(不依赖 fastapi、可单测);`api.py` 只做路由薄封装(CI 不装 fastapi)。
- 冷启动顺序由 `system_state`(bootstrap 标志)保证:未 bootstrap 时 pipeline 抑制实时告警。别绕过。

## 命令速查
```bash
export PYTHONPATH="$PWD/src"
python3 -m unittest discover -s tests          # 全部测试(Postgres 用例需 OCOOPA_TEST_POSTGRES_URL 否则 skip)
python3 -m ocoopa_monitor.cli doctor --production   # 上线自检
python3 -m ocoopa_monitor.cli bootstrap        # 冷启动:静默 backfill 两车道 + 标记
python3 -m ocoopa_monitor.cli scheduler        # 主循环
python3 -m ocoopa_monitor.cli review list|mark <incident_id> <confirmed|false_positive|muted> [--days N]  # 事件级,覆盖所有红/黄(不只告警过的)
python3 -m ocoopa_monitor.cli keyword list|add "<词>"|enable|disable [--category C --lane high|regular]
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
