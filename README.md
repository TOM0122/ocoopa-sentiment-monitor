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

## Backfill

Backfill seeds the historical baseline and never sends real-time red alerts:

```bash
python3 -m ocoopa_monitor.cli backfill --days 60
```

Backfilled mentions are stored with `backfill=true`, analyzed, and grouped into incident history.

## Delivery

Alerts and reports are always stored in the database. To also push to a generic webhook:

```bash
export OCOOPA_ALERT_WEBHOOK_URL="https://..."
```

The webhook payload is JSON and can be adapted for Slack, Feishu, or an internal relay.

## Current Source Defaults

- Google News RSS searches for high-sensitivity and regular keyword queries.
- CPSC Recall API via `saferproducts.gov` for recall monitoring.

The CPSC source follows the public recall API documented by CPSC. Confirm live ToS, exact parameters, and rate limits before production deployment.

## Tests

```bash
PYTHONPATH="$PWD/src" python3 -m unittest discover -s tests
```
