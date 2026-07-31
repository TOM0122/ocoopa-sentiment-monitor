# 运维手册(Operations / Runbook)

Ocoopa 舆情与 PR 监控 Agent 的部署、冷启动与日常运行说明。

## 1. 生产环境概览

- 部署平台:Railway(项目 `romantic-ambition / production / ocoopa-sentiment-monitor`)
- 数据库:Railway 托管 PostgreSQL,长期留存,不设自动删除
- 启动命令:`python -m ocoopa_monitor.cli scheduler`(见 `railway.json`)
- 模型:DeepSeek(`OCOOPA_LLM_PROVIDER=deepseek`),数据不出境
- 推送:钉钉群自定义机器人(加签)

### 必需环境变量(Railway)

| 变量 | 说明 |
|---|---|
| `OCOOPA_DB_URL` | 设为 `${{Postgres.DATABASE_URL}}`(生产必须 Postgres;勿设 `OCOOPA_DB_PATH`) |
| `OCOOPA_LLM_PROVIDER` | `deepseek` |
| `OCOOPA_LLM_API_KEY` | DeepSeek API key |
| `OCOOPA_LLM_MODEL` | 如 `deepseek-v4-flash`(以官方实际模型 ID 为准) |
| `OCOOPA_ALERT_CHANNEL` | `dingtalk` |
| `OCOOPA_ALERT_WEBHOOK_URL` | 钉钉机器人 webhook |
| `OCOOPA_ALERT_WEBHOOK_SECRET` | 钉钉加签 secret |
| `OCOOPA_ALERT_AT_MOBILES` | 需要 @ 的负责人手机号(逗号分隔,可选) |
| `OCOOPA_REVIEW_TOKEN` | 生产 web 全部读写/执行端点的 Bearer token(必需) |
| `OCOOPA_BRAVE_SEARCH_API_KEY` / `OCOOPA_GNEWS_API_KEY` | 商业 API key(可选,见配额策略) |

> 部署前自检:`python -m ocoopa_monitor.cli doctor --production --json`,`ok=true` 方可上线。

## 2. 冷启动(首次上线 / 清空重跑)

系统保证 **先静默 backfill、再启用实时告警** 的顺序,避免首次运行把存量舆情当新增刷屏。
scheduler 启动时:若数据库未 bootstrap → 自动跑 180 天静默 backfill(全部 `backfill=true`、不发告警)→ 标记完成 → 才开启实时告警;若已 bootstrap(如普通 redeploy)→ 直接进入实时,不重跑、不刷屏。

### 干净冷启动步骤

1. **Redeploy** 到最新 `main`。
2. 在 Railway Postgres 控制台清空业务表(保留 keywords / source_configs 配置):

   ```sql
   TRUNCATE alerts, daily_reports, analysis_results, incident_groups, mentions RESTART IDENTITY CASCADE;
   DELETE FROM system_state;
   ```

3. **重启服务**。scheduler 会自动:重新 seed(应用最新源配置、停用已下线的源)→ 180 天静默 backfill → 标记 bootstrap → 启用实时告警。
   - 或手动执行一次:`python -m ocoopa_monitor.cli bootstrap`,随后再起 scheduler。
4. **验收**:`SELECT count(*) FROM alerts;` 应为 `0`(存量已被静默吸收)。此后仅对真正新增的高危内容告警。

## 3. 日常运行

启动后 scheduler 持续运行,无需人工干预:

- **高敏车道**每 15 分钟:Google News RSS + CPSC API + AboutLawsuits + Reddit Atom(免费、扛时效)。CPSC 旧检索 API 对新公告可能延迟，因此为 P1 补充源；P0 Google News 使用精确查询独立捕获 CPSC.gov 官方公告。
- **常规车道**每小时:Brave / GNews / Google News RSS / PRNewswire。
- **红色高危** → 实时推送钉钉群;`needs_human_review`(低置信/证据未完全校验)的红色仍推送,但文案标注「需人工核实」且不 @ 手机号。
- **每日 09:00 后(北京时间)** 生成并**推送**中文日报到钉钉(routine 推送,不 @ 手机号)。完成日期持久化；重启错过 09:00 窗口会自动补发。
- **召回专项同步**:命中 CPSC 26-659/受影响型号的每条内容进入 `recall_mentions` 统计表。红色内容走告警；其他内容以 routine 消息同步群。所有消息先写 `delivery_outbox`,失败指数退避重试。
- **首次升级迁移**:scheduler 会把升级前数据库中已出现在群日报的召回内容补登记为 `synced`,避免部署后把历史内容逐条重新刷屏；升级后新发现的内容仍实时入队。
- **源健康告警**:scheduler 每 30 分钟检查抓取源;**P0 源**(Google News RSS / AboutLawsuits)失联或连续失败时推钉钉并 @ 负责人(P1 API 配额或数据延迟不直接 @)。同一源失败只告警一次,恢复后再失败会重新告警。
- **跨源告警去重(防刷屏)**:同一事件话题(如 ocoopa+死亡+诉讼)被多家媒体报道时,冷却窗(默认 6 小时,`OCOOPA_ALERT_COOLDOWN_HOURS`)内只推一条红色;但**新的来源类型首次出现**(如首条 CPSC、首个法律站、首家主流媒体)或风险升级会**突破冷却**照常告警。被去重的仍入库与日报,统计计入 `alerts_suppressed_cooldown`。
- **红色未处理升级**:红色告警若超过 `OCOOPA_ALERT_ACK_TIMEOUT_MINUTES`(默认 30 分钟)无人复核,scheduler 会**再 @ 一次负责人**(只升级一次,避免刷屏)。用 CLI `review mark` 或 web 复核页处理任一告警即视为已确认(ack),不再升级。

### 配额策略(免费档)

高敏车道只用免费、无限额的 RSS/CPSC,所以商业 API 配额打满也不会拖垮时效。Brave/GNews 在常规车道每小时约 24 次/天,落在免费额度内。是否升级付费档,以试运行期间「是否有重要信号只在每小时商业源出现、被 15 分钟免费源漏掉」为依据再决定。

## 4. 常用命令

```bash
python -m ocoopa_monitor.cli doctor --production --json   # 上线自检
python -m ocoopa_monitor.cli bootstrap                    # 静默冷启动(backfill 两车道 + 标记)
python -m ocoopa_monitor.cli backfill --days 180          # 仅高敏车道静默回溯(不会标记 bootstrap)
python -m ocoopa_monitor.cli run-lane high                # 手动跑一次高敏车道
python -m ocoopa_monitor.cli daily-report                 # 手动生成中文日报
python -m ocoopa_monitor.cli health --json                # 抓取源健康检查
python -m ocoopa_monitor.cli review list                            # 列出近期红/黄事件(供人工复核)
python -m ocoopa_monitor.cli review mark <incident_id> confirmed       # 标记已确认(继续告警)
python -m ocoopa_monitor.cli review mark <incident_id> false_positive  # 标记误报(该事件永久不再实时告警)
python -m ocoopa_monitor.cli review mark <incident_id> muted --days 7  # 静音 7 天(到期自动恢复;省略 --days = 无限期)
python -m ocoopa_monitor.cli keyword list                     # 列出全部监控词(含停用)
python -m ocoopa_monitor.cli keyword add "<词>" --category legal --lane high  # 新增监控词(下次抓取即生效)
python -m ocoopa_monitor.cli keyword disable "<词>"           # 停用某词
python -m ocoopa_monitor.cli keyword enable "<词>"            # 重新启用
python -m ocoopa_monitor.cli recall list --limit 200           # 召回专项统计表
python -m ocoopa_monitor.cli recall import-group ./group.csv   # 登记群内已同步内容
python -m ocoopa_monitor.cli recall export ./recall.csv        # 导出统一 CSV
python -m ocoopa_monitor.cli recall sync --limit 10             # 队列补发/重试
```

> **关键词热更新**:诉讼公开后冒出的律所名、案号、新型号,用 `keyword add` 加入即可,无需改代码或重部署,下一次车道抓取自动生效。

> **人工反馈闭环(M2)**:`review mark` 作用于「事件」(同一 event_fingerprint),不是单条消息。标记 `false_positive`/`muted` 后,该事件的后续实时告警会被抑制(仍进历史库与日报);`confirmed` 不抑制、仅留痕。被抑制的告警在 run-lane 统计里计入 `alerts_suppressed_muted`。

### Web 复核页(给非技术同事,无需终端)

除 CLI 外,FastAPI 应用提供一个网页复核入口:`GET /review` 列出**近期所有红/黄事件**(含被冷启动 backfill 静默吸收、从未触发实时告警的事件),每条带「确认 / 误报 / 静音7天」按钮,点一下即按事件标记(等价于 `review mark`)。

- **如何在 Railway 跑**:复核页由 FastAPI 提供,与 scheduler 是**两个进程**。新增一个 Railway 服务,指向同一个 Postgres(`OCOOPA_DB_URL=${{Postgres.DATABASE_URL}}`),启动命令:
  ```
  uvicorn ocoopa_monitor.api:app --host 0.0.0.0 --port $PORT
  ```
  (镜像已含 `[api]` 依赖。)
- **必须设 `OCOOPA_REVIEW_TOKEN`**:生产 doctor 会阻止空 token。自动化调用优先使用 `Authorization: Bearer <token>`；现有浏览器页面仍兼容 `?token=<token>`，但查询参数可能进入访问日志，需避免转发完整 URL。
- 把 `https://<服务域名>/review?token=xxx` 发给负责复核的同事收藏即可。

### 运营控制台(看板 / 检索 / 导出)

同一个 web 服务还提供给法务/PR/高层看的只读控制台(同样用 `OCOOPA_REVIEW_TOKEN` 鉴权):

- **看板** `GET /dashboard?token=xxx&days=30`:近 N 天提及总数、实时告警数、风险/情感/来源分布、近期红/黄风险列表(带原文链接)。
- **检索** `GET /console/search?token=xxx&q=<关键词>&risk=<red|yellow|green>&days=30`:按关键词/风险等级过滤,看标题、来源、摘要、链接。
- **导出 CSV** `GET /console/export.csv?token=xxx&days=30`:导出该时间窗内全部提及(含风险/情感/分类/摘要/证据状态),给法务做可追溯报告。

> 控制台是只读聚合,不改数据;反馈仍在 `/review` 或 CLI 进行。`days` 默认 30,可调。

## 5. 已知边界 / 待补

- 钉钉自定义机器人只有发送能力；群历史需 CSV/JSONL 导入。要自动读群，必须另配经批准的钉钉应用与最小读取权限。
- Reddit 已直连公共 Atom；公开网页由 Brave/SerpAPI/GNews/Google News 补充。Facebook、Instagram、TikTok、X、微博、小红书、抖音等关闭索引或需登录的内容，未获官方 API 权限前不能承诺“全量”。
- 当前抓取以标题和公开摘要为主，不等同于全文归档。重要红色内容仍需人工打开原文复核。
- 当前阶段为**有人盯的试运行**；建议每日检查源健康、未投递 outbox、召回台账和日报，不应作为完全无人值守系统。
