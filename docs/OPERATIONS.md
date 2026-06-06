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

- **高敏车道**每 15 分钟:Google News RSS + CPSC(免费、扛时效)。
- **常规车道**每小时:Brave / GNews / Google News RSS / PRNewswire。
- **红色高危** → 实时推送钉钉群;`needs_human_review`(低置信/证据未完全校验)的红色仍推送,但文案标注「需人工核实」且不 @ 手机号。
- **每日 09:00(北京时间)** 生成并**推送**中文日报到钉钉(routine 推送,不 @ 手机号)。
- **源健康告警**:scheduler 每 30 分钟检查抓取源;**P0 源**(Google News RSS / CPSC / AboutLawsuits)失联或连续失败时推钉钉并 @ 负责人(P1 商业 API 配额失败属预期,不告警)。同一源失败只告警一次,恢复后再失败会重新告警。

### 配额策略(免费档)

高敏车道只用免费、无限额的 RSS/CPSC,所以商业 API 配额打满也不会拖垮时效。Brave/GNews 在常规车道每小时约 24 次/天,落在免费额度内。是否升级付费档,以试运行期间「是否有重要信号只在每小时商业源出现、被 15 分钟免费源漏掉」为依据再决定。

## 4. 常用命令

```bash
python -m ocoopa_monitor.cli doctor --production --json   # 上线自检
python -m ocoopa_monitor.cli bootstrap                    # 静默冷启动(backfill 两车道 + 标记)
python -m ocoopa_monitor.cli backfill --days 180          # 仅高敏车道回溯(也会标记 bootstrap)
python -m ocoopa_monitor.cli run-lane high                # 手动跑一次高敏车道
python -m ocoopa_monitor.cli daily-report                 # 手动生成中文日报
python -m ocoopa_monitor.cli health --json                # 抓取源健康检查
python -m ocoopa_monitor.cli review list                  # 列出近期告警(供人工复核)
python -m ocoopa_monitor.cli review mark <id> confirmed       # 标记已确认(继续告警)
python -m ocoopa_monitor.cli review mark <id> false_positive  # 标记误报(该事件永久不再实时告警)
python -m ocoopa_monitor.cli review mark <id> muted --days 7  # 静音 7 天(到期自动恢复;省略 --days = 无限期)
python -m ocoopa_monitor.cli keyword list                     # 列出全部监控词(含停用)
python -m ocoopa_monitor.cli keyword add "<词>" --category legal --lane high  # 新增监控词(下次抓取即生效)
python -m ocoopa_monitor.cli keyword disable "<词>"           # 停用某词
python -m ocoopa_monitor.cli keyword enable "<词>"            # 重新启用
```

> **关键词热更新**:诉讼公开后冒出的律所名、案号、新型号,用 `keyword add` 加入即可,无需改代码或重部署,下一次车道抓取自动生效。

> **人工反馈闭环(M2)**:`review mark` 作用于「事件」(同一 event_fingerprint),不是单条消息。标记 `false_positive`/`muted` 后,该事件的后续实时告警会被抑制(仍进历史库与日报);`confirmed` 不抑制、仅留痕。被抑制的告警在 run-lane 统计里计入 `alerts_suppressed_muted`。

## 5. 已知边界 / 待补

- **集体诉讼招募源未接**:律师导流站白名单待法务批准后加入(免费 RSS/白名单抓取),目前该信号只能经新闻/搜索间接捕捉。
- **中文社媒(小红书/微博/抖音)** 暂未覆盖,日报会标注该缺口。
- 当前阶段为**有人盯的试运行**,建议每日浏览日报校验召回,而非完全无人值守依赖。
