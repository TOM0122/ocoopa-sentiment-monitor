from __future__ import annotations

import argparse
import json
from typing import Any, Dict

from .config import load_settings
from .db import Database
from .keywords import DEFAULT_KEYWORDS
from .pipeline import MonitorPipeline
from .reports import DailyReportService
from .source_health import SourceHealthMonitor
from .sources import DEFAULT_SOURCES


def main() -> None:
    parser = argparse.ArgumentParser(description="Ocoopa public-opinion monitoring agent")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init-db")
    sub.add_parser("seed")
    run_lane = sub.add_parser("run-lane")
    run_lane.add_argument("lane", choices=["high", "regular"])
    backfill = sub.add_parser("backfill")
    backfill.add_argument("--days", type=int, default=None)
    report = sub.add_parser("daily-report")
    report.add_argument("--timezone", default="Asia/Shanghai")
    health = sub.add_parser("health")
    health.add_argument("--json", action="store_true")
    scheduler = sub.add_parser("scheduler")
    scheduler.add_argument("--poll-seconds", type=int, default=30)

    args = parser.parse_args()
    settings = load_settings()
    db = Database(settings.db_path)

    if args.command == "init-db":
        db.init()
        print(f"initialized database: {settings.db_path}")
        return
    if args.command == "seed":
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(DEFAULT_SOURCES)
        print("seeded default keywords and sources")
        return
    if args.command == "run-lane":
        db.init()
        pipeline = MonitorPipeline(db, settings)
        print_json(pipeline.run_lane(args.lane))
        return
    if args.command == "backfill":
        db.init()
        days = args.days or settings.backfill_days
        pipeline = MonitorPipeline(db, settings)
        print_json(pipeline.run_lane("high", backfill=True, since_days=days))
        return
    if args.command == "daily-report":
        db.init()
        service = DailyReportService(db)
        result = service.generate(args.timezone)
        print(result["generated_text_zh"])
        return
    if args.command == "health":
        db.init()
        monitor = SourceHealthMonitor(db)
        unhealthy = monitor.check()
        if args.json:
            print_json({"unhealthy_sources": unhealthy})
        else:
            if not unhealthy:
                print("all sources healthy")
            for source in unhealthy:
                print(
                    f"{source['source_name']} status={source['health_status']} "
                    f"last_success_at={source['last_success_at']} failures={source['consecutive_failures']}"
                )
        return
    if args.command == "scheduler":
        from .scheduler import SimpleScheduler

        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(DEFAULT_SOURCES)
        SimpleScheduler(db, settings).run_forever(args.poll_seconds)


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
