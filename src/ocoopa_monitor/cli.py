from __future__ import annotations

import argparse
import json
import sys
from datetime import timedelta
from typing import Any, Dict

from .config import load_settings
from .db import create_database
from .models import utcnow
from .doctor import run_doctor
from .keywords import DEFAULT_KEYWORDS
from .pipeline import MonitorPipeline
from .outbox import DeliveryOutboxWorker
from .delivery import DeliveryClient
from .recall import RecallRegistryService
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
    bootstrap = sub.add_parser("bootstrap")
    bootstrap.add_argument("--days", type=int, default=None)
    report = sub.add_parser("daily-report")
    report.add_argument("--timezone", default="Asia/Shanghai")
    health = sub.add_parser("health")
    health.add_argument("--json", action="store_true")
    scheduler = sub.add_parser("scheduler")
    scheduler.add_argument("--poll-seconds", type=int, default=30)
    doctor = sub.add_parser("doctor")
    doctor.add_argument("--production", action="store_true")
    doctor.add_argument("--json", action="store_true")
    review = sub.add_parser("review")
    review.add_argument("action", choices=["list", "mark"])
    review.add_argument("incident_id", type=int, nargs="?")
    review.add_argument("status", nargs="?", choices=["confirmed", "false_positive", "muted"])
    review.add_argument("--days", type=int, default=None, help="mute duration in days (muted only; omit = indefinite)")
    review.add_argument("--limit", type=int, default=20)
    keyword = sub.add_parser("keyword")
    keyword.add_argument("action", choices=["list", "add", "enable", "disable"])
    keyword.add_argument("term", nargs="?")
    keyword.add_argument("--category", default="custom")
    keyword.add_argument("--lane", choices=["high", "regular"], default="high")
    recall = sub.add_parser("recall")
    recall.add_argument("action", choices=["list", "export", "import-group", "sync"])
    recall.add_argument("path", nargs="?")
    recall.add_argument("--limit", type=int, default=500)

    args = parser.parse_args()
    settings = load_settings()
    db = create_database(settings)

    if args.command == "init-db":
        db.init()
        print("initialized database")
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
        result = pipeline.run_lane("high", backfill=True, since_days=days)
        print_json(result)
        return
    if args.command == "bootstrap":
        db.init()
        db.seed_keywords(DEFAULT_KEYWORDS)
        db.seed_sources(DEFAULT_SOURCES)
        days = args.days or settings.backfill_days
        pipeline = MonitorPipeline(db, settings)
        stats = pipeline.bootstrap(days)
        print_json({"bootstrap": "complete", "backfill_days": days, "stats": stats})
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
        return
    if args.command == "review":
        db.init()
        if args.action == "list":
            rows = db.list_recent_incidents(args.limit)
            if not rows:
                print("no red/yellow incidents yet")
                return
            for r in rows:
                review_flag = " [需人工核实]" if r.get("needs_human_review") else ""
                print(
                    f"#{r['incident_id']} [{r['risk_level_max']}] status={r.get('status')}"
                    f"{review_flag}\n    {r.get('title') or r.get('primary_topic')}\n    {r.get('source_url')}"
                )
            return
        if args.incident_id is None or args.status is None:
            print("usage: review mark <incident_id> <confirmed|false_positive|muted> [--days N]")
            sys.exit(1)
        fingerprint = db.get_fingerprint_by_incident(args.incident_id)
        if not fingerprint:
            print(f"incident #{args.incident_id} not found")
            sys.exit(1)
        muted_until = None
        if args.status == "muted" and args.days:
            muted_until = utcnow() + timedelta(days=args.days)
        updated = db.review_incident(fingerprint, args.status, muted_until)
        if updated:
            db.ack_alerts_by_fingerprint(fingerprint)
        print_json(
            {
                "incident_id": args.incident_id,
                "fingerprint": fingerprint,
                "review_status": args.status,
                "muted_until": muted_until,
                "updated": updated,
            }
        )
        return
    if args.command == "keyword":
        db.init()
        if args.action == "list":
            for kw in db.list_keywords_all():
                print(f"[{'on ' if kw.active else 'off'}] {kw.lane:<7} {kw.category:<10} {kw.term}")
            return
        if not args.term:
            print("usage: keyword <add|enable|disable> <term> [--category C] [--lane high|regular]")
            sys.exit(1)
        if args.action == "add":
            db.upsert_keyword(args.term, args.category, args.lane, True)
            print_json({"added": args.term, "category": args.category, "lane": args.lane})
            return
        updated = db.set_keyword_active(args.term, args.action == "enable")
        if not updated:
            print(f"keyword not found: {args.term}")
            sys.exit(1)
        print_json({"term": args.term, "active": args.action == "enable"})
        return
    if args.command == "recall":
        db.init()
        pipeline = MonitorPipeline(db, settings)
        service = RecallRegistryService(db, pipeline)
        if args.action == "list":
            print_json({"records": db.list_recall_mentions(max(1, min(args.limit, 5000)))})
            return
        if args.action == "sync":
            queued = service.queue_pending(min(max(args.limit, 1), 20))
            result = DeliveryOutboxWorker(db, DeliveryClient.from_settings(settings)).drain(
                min(max(args.limit, 1), 20)
            )
            print_json({"queued": queued, **result})
            return
        if not args.path:
            print(f"usage: recall {args.action} <path>")
            sys.exit(1)
        if args.action == "import-group":
            print_json(service.import_group(args.path))
            return
        count = service.export_csv(args.path, max(1, min(args.limit, 5000)))
        print_json({"exported": count, "path": args.path})
        return
    if args.command == "doctor":
        report = run_doctor(settings, production=args.production)
        payload = {
            "ok": report.ok,
            "errors": report.errors,
            "warnings": report.warnings,
            "settings_summary": report.settings_summary,
        }
        if args.json:
            print_json(payload)
        else:
            print(f"ok={report.ok}")
            for error in report.errors:
                print(f"ERROR: {error}")
            for warning in report.warnings:
                print(f"WARNING: {warning}")
            print_json({"settings_summary": report.settings_summary})
        if not report.ok:
            sys.exit(1)


def print_json(payload: Dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
