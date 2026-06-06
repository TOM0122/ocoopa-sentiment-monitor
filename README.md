# Ocoopa Public Opinion Monitor

M1 implementation for an internal Ocoopa public-opinion and PR risk monitoring agent.

The core pipeline is intentionally runnable with only the Python standard library:

- keyword hot-load from the database
- high-sensitivity and regular ingestion lanes
- RSS and CPSC recall API fetchers
- source health monitoring and fetcher isolation
- conservative URL/content/event dedupe
- rule-first analysis with pluggable LLM provider interface
- evidence grounding for quotes and high-risk claims
- backfill mode that writes history without real-time alerts
- daily Chinese report generation

Optional FastAPI dependencies are listed in `requirements.txt`; the command-line workflow and tests do not require them.

## Quick Start

```bash
python3 -m ocoopa_monitor.cli init-db
python3 -m ocoopa_monitor.cli seed
python3 -m ocoopa_monitor.cli run-lane high
python3 -m ocoopa_monitor.cli daily-report
```

For local development without installing the package, set:

```bash
export PYTHONPATH="$PWD/src"
```

The default database is `./ocoopa_monitor.db`. Override with:

```bash
export OCOOPA_DB_PATH=/path/to/monitor.db
```

Run the built-in scheduler:

```bash
python3 -m ocoopa_monitor.cli scheduler
```

It runs the high-sensitivity lane every 15 minutes by default, the regular lane hourly, and generates a Beijing-time daily report around 09:00.

Validate deployment configuration before starting production:

```bash
python3 -m ocoopa_monitor.cli doctor --production
```

The doctor command reports whether required secrets are configured without printing secret values.

## Backfill

Backfill seeds the historical baseline and never sends real-time red alerts:

```bash
python3 -m ocoopa_monitor.cli backfill --days 180
```

Backfilled mentions are stored with `backfill=true`, analyzed, and grouped into incident history.

## Delivery

Alerts and reports are always stored in the database.

Use DingTalk custom robot delivery:

```bash
export OCOOPA_ALERT_CHANNEL=dingtalk
export OCOOPA_ALERT_WEBHOOK_URL="https://oapi.dingtalk.com/robot/send?access_token=..."
export OCOOPA_ALERT_WEBHOOK_SECRET="SEC..."
export OCOOPA_ALERT_AT_MOBILES="13800000000,13900000000"
```

The DingTalk channel sends markdown messages, signs requests with the robot secret, supports mobile @ mentions, and locally backs off at 20 messages per minute by default.

Generic webhook delivery is still available:

```bash
export OCOOPA_ALERT_WEBHOOK_URL="https://..."
```

The webhook payload is JSON and can be adapted for Slack, Feishu, or an internal relay.

## LLM Provider

Production analysis uses DeepSeek through a provider abstraction:

```bash
export OCOOPA_LLM_PROVIDER=deepseek
export OCOOPA_LLM_MODEL=deepseek-v4-flash
export OCOOPA_LLM_API_KEY="..."
export OCOOPA_LLM_BASE_URL=https://api.deepseek.com
```

The model name, endpoint, and API key are all configurable. If DeepSeek is unavailable during a run, the analysis service falls back to the local rule-only provider and records that fallback in `evidence_check_notes`; API keys are not stored in the database or logs.

## Commercial Sources

Commercial search/news APIs are optional but recommended for production recall:

```bash
export OCOOPA_SERPAPI_API_KEY="..."
export OCOOPA_GNEWS_API_KEY="..."
```

The high-sensitivity lane uses a single Boolean query per commercial source to control cost:

```text
Ocoopa (fire OR death OR lawsuit OR recall OR CPSC OR "class action")
```

## Docker Deployment

Create a `.env` file from `.env.example`, then run:

```bash
docker compose up -d --build
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli doctor --production
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli seed
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli backfill --days 180
```

The container stores the SQLite database in the `ocoopa-data` Docker volume at `/data/ocoopa_monitor.db`.

## Human Setup Checklist

Before production cutover, a human operator must provide:

- DingTalk custom robot webhook URL.
- DingTalk robot signing secret.
- Mobile numbers to @ for red alerts.
- DeepSeek API key with access to the configured model.
- SerpAPI API key.
- GNews API key.
- Deployment host or container runtime.

Do not commit `.env`, API keys, webhook secrets, or production database files.

## Current Source Defaults

- Google News RSS searches for high-sensitivity and regular keyword queries.
- CPSC Recall API via `saferproducts.gov` for recall monitoring.
- SerpAPI and GNews are enabled when API keys are configured.
- PRNewswire RSS is included as a regular-lane redundancy source.

The CPSC source follows the public recall API documented by CPSC. Confirm live ToS, exact parameters, and rate limits before production deployment.

## Tests

```bash
PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests
```
