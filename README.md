# Ocoopa Public Opinion Monitor

Internal Ocoopa public-opinion and PR risk monitoring agent. **In production (Railway) — supervised trial stage.**

> New here? Read [docs/OVERVIEW.md](docs/OVERVIEW.md) (non-technical) and [docs/OPERATIONS.md](docs/OPERATIONS.md) (deploy / run / review / console).

The core pipeline runs on the Python standard library; the web console/review UI needs the optional FastAPI extra.

- keyword hot-load + `keyword` CLI for live edits (law-firm names, case numbers, models)
- high-sensitivity (15 min), public-index social discovery (hourly), and optional licensed social (5 min) lanes
- RSS, CPSC recall API, and generic-RSS (AboutLawsuits legal) fetchers; source-health monitoring + fetcher isolation
- conservative URL/content/event dedupe + cross-source topic cooldown (one page per event)
- rule-first analysis with a pluggable LLM provider (DeepSeek in prod), cross-lingual evidence grounding
- deterministic cold-start: silent backfill before real-time alerts (no alert storm on first deploy)
- durable DingTalk outbox with retry for red alerts and recall overview digests; scheduled daily Chinese summaries, source-health pages, and unacked-alert escalation
- dedicated CPSC 26-659 recall registry covering affected models, Reddit Atom, news/RSS, CPSC, legal feeds, and general-web search APIs
- auditable group-history import and UTF-8 CSV statistics-table export
- event review plus per-post response workflow (`待判断` / `建议回应` / `已回应` / `无需回应` / `升级 PR/法务`); no publishing endpoint exists
- read-only operations console: daily trend line, keyword/topic synthesis, management summary, recommended actions, search, and CSV export

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

The default local database is SQLite at `./ocoopa_monitor.db`. Override the local path with:

```bash
export OCOOPA_DB_PATH=/path/to/monitor.db
```

Production must use PostgreSQL. Set `OCOOPA_DB_URL` or Railway's injected `DATABASE_URL`:

```bash
export OCOOPA_DB_URL="postgresql://..."
```

Run the built-in scheduler:

```bash
python3 -m ocoopa_monitor.cli scheduler
```

It runs the high-sensitivity lane every 15 minutes, the public-index lane hourly,
the licensed lane every 5 minutes when configured, and generates a Beijing-time
daily report around 09:00.

Validate deployment configuration before starting production:

```bash
python3 -m ocoopa_monitor.cli doctor --production --role scheduler
python3 -m ocoopa_monitor.cli doctor --production --role web
```

The doctor command reports whether required secrets are configured without printing secret values.

## Backfill

Backfill seeds the historical baseline and never sends real-time red alerts:

```bash
python3 -m ocoopa_monitor.cli backfill --days 180
```

Backfilled mentions are stored with `backfill=true`, analyzed, and grouped into incident history.

## Delivery

Alerts are committed to a durable database outbox before network delivery. Failed
webhook calls remain pending and retry with exponential backoff; a transient
DingTalk failure no longer loses the alert. Routine recall findings are grouped
into overview digests without @ mentions; red alerts remain immediate and keep
the configured on-call @ policy. Public URLs are rendered as the short label
“链接” in DingTalk markdown while the full URL remains available behind it.

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

## CPSC 26-659 Recall Registry

The recall registry is a separate statistics table for the current OCOOPA recall.
It recognizes recall number `26-659`, importer identity, and affected models
`UT3053`, `UT3056`, `ZLS-118`, `ZLS-118S`, `ZLS-118D`, `H01`, and `H01(PD)`.

```bash
# Inspect the unified table
python3 -m ocoopa_monitor.cli recall list --limit 200

# Export a UTF-8 CSV for operations
python3 -m ocoopa_monitor.cli recall export ./outputs/recall-26-659.csv --limit 5000

# Import content that was already shared in the DingTalk group
python3 -m ocoopa_monitor.cli recall import-group ./group-history.csv

# Queue unsynced historical records and retry due deliveries
python3 -m ocoopa_monitor.cli recall sync --limit 10
```

Group imports accept CSV, JSONL, or NDJSON. CSV columns are:
`platform,source_name,source_url,title,content,published_at,author_or_publisher,group_synced_at`.
Rows imported from the group are marked `origin=group_import` and
`sync_status=synced`, so they are registered without being sent back to the
group. A template is available at
[`docs/recall-group-import-template.csv`](docs/recall-group-import-template.csv).

The custom DingTalk robot used by this project can send messages but cannot read
group history. Automatic group-history ingestion therefore requires a separate
read-capable DingTalk app and explicit authorization; until then, use the
auditable import command above.

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
export OCOOPA_BRAVE_SEARCH_API_KEY="..."
export OCOOPA_GNEWS_API_KEY="..."
```

To stay inside free quotas, commercial APIs run on the **regular (hourly) lane**, not the 15-min high-sensitivity lane (which uses only free, unmetered sources). Each commercial source issues a single Boolean query per run to control cost:

```text
Ocoopa (fire OR death OR lawsuit OR recall OR CPSC OR "class action")
```

Brave Search is the recommended public-index source. Its regular query is site
limited to public TikTok, Instagram, Facebook, YouTube, X, Reddit, forum, and
review pages. A zero result means only that the configured source did not
discover a record; it does not prove the platform has no discussion.

### Optional Brandwatch pilot

Brandwatch is disabled unless all three read-only pilot values are present:

```bash
export OCOOPA_BRANDWATCH_TOKEN="..."
export OCOOPA_BRANDWATCH_PROJECT_ID="..."
export OCOOPA_BRANDWATCH_QUERY_ID="..."
```

The connector polls Mentions every five minutes with separate, five-minute-overlap
`sinceAdded` and `sinceUpdated` streams and de-duplicates by `resourceId`. Enabling
it triggers a 30-day silent backfill before real-time delivery. Keep the account
read-only: the application contains no reply, post, like, hide, or delete code.
Provider coverage and latency remain visible on the analysis page and must be
validated during the 14-day pilot before procurement.

## Web authentication

Set independent secrets for the access code and signed session:

```bash
export OCOOPA_REVIEW_TOKEN="one-time-entry-code"
export OCOOPA_SESSION_SECRET="a-different-long-random-secret"
```

Open `/auth/login`; successful login creates `HttpOnly`, `Secure`,
`SameSite=Lax` session cookies. During the migration window,
`OCOOPA_ALLOW_QUERY_TOKEN=true` accepts an old `?token=` URL once, creates a
session, and redirects to a clean URL. Rotate the exposed review token and set
`OCOOPA_ALLOW_QUERY_TOKEN=false` after migration. Automation endpoints continue
to accept only `Authorization: Bearer …`; query-string credentials are never
accepted by automation endpoints.

## Docker Deployment

Create a `.env` file from `.env.example`, then run:

```bash
docker compose up -d --build
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli doctor --production --role scheduler
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli seed
docker compose exec ocoopa-monitor python -m ocoopa_monitor.cli backfill --days 180
```

Docker Compose is intended for local development and stores SQLite in the `ocoopa-data` Docker volume at `/data/ocoopa_monitor.db`. For production, use PostgreSQL instead of container-local SQLite.

## Railway Deployment

Railway can deploy this repository from GitHub using the included `Dockerfile` and `railway.json`.

Recommended Railway setup:

1. Create a new Railway project from the GitHub repository.
2. Add Railway's managed PostgreSQL service to the project.
3. Attach the monitor service to the PostgreSQL service so Railway injects `DATABASE_URL`.
4. Set the same non-database environment variables shown in `.env.example`.
5. Do not set `OCOOPA_DB_PATH` for production. Use `DATABASE_URL`, or set `OCOOPA_DB_URL` to the same value if you need an explicit override.
6. Run one-off commands after the first deploy:

```bash
python -m ocoopa_monitor.cli doctor --production --role scheduler
python -m ocoopa_monitor.cli doctor --production --role web
python -m ocoopa_monitor.cli init-db
python -m ocoopa_monitor.cli seed
python -m ocoopa_monitor.cli backfill --days 180
```

The Railway service start command is `python -m ocoopa_monitor.cli scheduler`. It runs the PostgreSQL migration idempotently on startup before seeding and scheduling.

## Human Setup Checklist

Before production cutover, a human operator must provide:

- DingTalk custom robot webhook URL.
- DingTalk robot signing secret.
- Mobile numbers to @ for red alerts.
- DeepSeek API key with access to the configured model.
- Brave Search API key or SerpAPI API key.
- GNews API key.
- Deployment host or container runtime.

Do not commit `.env`, API keys, webhook secrets, or production database files.

## Current Source Defaults

High-sensitivity lane (15 min, free / unmetered):
- Google News RSS (mainstream-media follow-up)
- CPSC Recall API via `saferproducts.gov`
- AboutLawsuits public RSS (class-action lead-gen; `source_type=legal`)
- Reddit public Atom search for recall/fire/model propagation

Regular lane (hourly):
- Brave site-limited public-social discovery / GNews (when API keys are set)
- Google News RSS redundancy

Licensed lane (5 minutes, disabled by default):
- Brandwatch Mentions API with read-only credentials

“All external discourse” is implemented as best-effort coverage of compliant,
publicly accessible or licensed sources. Closed/private groups and social
platforms that block unauthenticated indexing (for example private Facebook,
Instagram, TikTok, or X content) cannot be claimed as complete without approved
platform API access. General-web search APIs provide secondary discovery for
publicly indexed pages on those platforms.

`seed_sources` deactivates any source removed from `DEFAULT_SOURCES`, so the seed list is the single source of truth. The CPSC source follows the public recall API; confirm live ToS, parameters, and rate limits before relying on it.

## Tests

```bash
PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests
```
