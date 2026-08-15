#!/usr/bin/env python3
"""
Integration test: several consecutive scans against a fake source, checking
that a posting which disappears from the feed is flagged, then purged, and
that an outage never purges anything.

    python scripts/test_scan_purge.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import purge as P
import scan as S

START = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "keywords": ["python", "backend"],
    "min_score": 40,
    "auto_approve_score": 65,
    "max_age_days": 14,
    "purge_missing_days": 2,
    "remote_required": True,
    "sources": ["fake"],
    "purge_verify_links": False,
}

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else f' — {detail}'}")
    if not cond:
        _failures.append(name)


def listing(slug: str, title: str, mirror: bool = False) -> dict:
    host = "www.arbeitnow.co.uk" if mirror else "www.arbeitnow.com"
    suffix = 999111 if mirror else 412330  # boards re-roll this on every scrape
    return {
        "id": S.make_id(f"https://{host}/jobs/acme/{slug}-{suffix}", title, "Acme"),
        "title": title,
        "company": "Acme",
        "location": "Remote",
        "url": f"https://{host}/jobs/acme/{slug}-{suffix}",
        "description": "Remote python backend role, $150,000",
        "tags": "python backend",
        "salary_min": None,
        "salary_max": None,
        "posted_at": "",
        "source": "fake",
        "status": "new",
        "found_at": START.isoformat(),
    }


def run_scan(tmp: Path, feed: list[dict], now: datetime) -> list[dict]:
    """Run scan.main() against a fake source with the clock pinned to `now`."""
    S.SOURCE_MAP["fake"] = lambda config: [dict(j) for j in feed]
    S.load_config = lambda: dict(CONFIG)

    class FrozenClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return now

    real_dt = S.datetime
    S.datetime = FrozenClock
    argv = sys.argv
    sys.argv = ["scan.py", "--no-verify"]
    try:
        S.main()
    finally:
        S.datetime = real_dt
        sys.argv = argv
    return json.loads((tmp / "jobs.json").read_text())


with tempfile.TemporaryDirectory() as td:
    tmp = Path(td)
    (tmp / "jobs.json").write_text("[]")
    (tmp / "applied.json").write_text("[]")
    S.DATA_DIR = P.DATA_DIR = tmp
    S.JOBS_PATH = P.JOBS_PATH = tmp / "jobs.json"
    P.APPLIED_PATH = tmp / "applied.json"
    P.PURGE_LOG_PATH = tmp / "purge_log.json"

    a = listing("senior-python-engineer", "Senior Python Engineer")
    b = listing("backend-engineer", "Backend Engineer")
    a_mirror = listing("senior-python-engineer", "Senior Python Engineer", mirror=True)

    print("run 1 — both postings listed")
    jobs = run_scan(tmp, [a, b], START)
    check("both stored", len(jobs) == 2, jobs)
    check("high scorer auto-approved",
          any(j["status"] == "approved" for j in jobs), jobs)

    print("\nrun 2 — same feed, one posting mirrored under the other domain")
    jobs = run_scan(tmp, [a, a_mirror, b], START + timedelta(hours=6))
    check("mirror does not create a second record", len(jobs) == 2, [j["url"] for j in jobs])
    check("sightings refreshed",
          all(j["last_seen_at"].startswith("2026-08-15T18") for j in jobs), jobs)

    print("\nrun 3 — posting B drops off the feed")
    jobs = run_scan(tmp, [a], START + timedelta(days=1))
    flagged = [j for j in jobs if j.get("missing_since")]
    check("B kept but flagged missing", len(jobs) == 2 and len(flagged) == 1, jobs)
    check("A not flagged", flagged and flagged[0]["title"] == "Backend Engineer", flagged)

    print("\nrun 4 — source outage (returns nothing)")
    jobs = run_scan(tmp, [], START + timedelta(days=2))
    check("outage purges nothing", len(jobs) == 2, jobs)

    print("\nrun 5 — B still gone, past the grace window")
    jobs = run_scan(tmp, [a], START + timedelta(days=4))
    check("B purged as missing", [j["title"] for j in jobs] == ["Senior Python Engineer"], jobs)

    print("\nrun 6 — A still listed well past max_age_days")
    jobs = run_scan(tmp, [a], START + timedelta(days=30))
    check("still-listed job is not expired", len(jobs) == 1, jobs)

    print("\nrun 7 — A finally drops off, grace elapses, then ages out")
    run_scan(tmp, [], START + timedelta(days=31))          # outage: no flag
    run_scan(tmp, [b], START + timedelta(days=32))          # A flagged missing
    jobs = run_scan(tmp, [b], START + timedelta(days=35))   # A purged
    check("A purged once truly gone",
          [j["title"] for j in jobs] == ["Backend Engineer"], jobs)

    log = json.loads((tmp / "purge_log.json").read_text())
    check("purge log written per run", len(log) >= 7, len(log))
    check("log records reasons", any(e["missing"] for e in log), log[-1])

print("\n" + ("ALL PASSED" if not _failures else f"FAILED: {_failures}"))
sys.exit(1 if _failures else 0)
