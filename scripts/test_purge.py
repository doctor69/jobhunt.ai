#!/usr/bin/env python3
"""
Offline tests for the purge rules — no network, no fixtures on disk.

    python scripts/test_purge.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import purge as P

NOW = datetime(2026, 8, 15, 12, 0, tzinfo=timezone.utc)
CONFIG = {
    "max_age_days": 14,
    "purge_missing_days": 2,
    "min_score": 40,
    "remote_required": True,
}

_failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}{'' if cond else f' — {detail}'}")
    if not cond:
        _failures.append(name)


def job(jid, **kw):
    j = {
        "id": jid,
        "title": kw.pop("title", "Backend Engineer"),
        "company": kw.pop("company", "Acme"),
        "url": kw.pop("url", f"https://boards.example.com/jobs/{jid}"),
        "source": kw.pop("source", "arbeitnow"),
        "status": kw.pop("status", "new"),
        "score": kw.pop("score", 70),
        "remote": kw.pop("remote", True),
        "found_at": kw.pop("found_at", (NOW - timedelta(days=1)).isoformat()),
    }
    j.update(kw)
    P.ensure_metadata(j)
    return j


def run(jobs, **kw):
    kw.setdefault("verify", False)
    kw.setdefault("now", NOW)
    kept, summary = P.purge_jobs(jobs, CONFIG, **kw)
    return {j["id"] for j in kept}, summary


# ── identity ─────────────────────────────────────────────────────────────────
print("dedupe_key / normalize_url")
a = job("a", url="https://www.arbeitnow.com/jobs/companies/clera/remote-ml-engineer-413326")
b = job("b", url="https://www.arbeitnow.co.uk/jobs/companies/clera/remote-ml-engineer-162947")
c = job("c", title="Frontend Engineer",
        url="https://www.arbeitnow.com/jobs/companies/clera/remote-frontend-eng-11111")
check("mirror domains + rerolled suffix collapse", a["dedupe_key"] == b["dedupe_key"])
check("different postings stay distinct", a["dedupe_key"] != c["dedupe_key"])
check("bad timestamp does not raise", P.parse_ts("not-a-date") is None)
check("naive timestamp gets utc", P.parse_ts("2026-08-01T10:00:00").tzinfo is timezone.utc)

# ── duplicates ───────────────────────────────────────────────────────────────
print("\nduplicate")
kept, s = run([job("a", score=60), job("b", score=90,
                                       url="https://www.arbeitnow.co.uk/jobs/companies/clera/x-1")
               | {"dedupe_key": job("a")["dedupe_key"]}])
check("one survivor per posting", len(kept) == 1, kept)
check("counted as duplicate", s["duplicate"] == 1, s)

dup_applied = job("b", status="applied", dedupe_key=job("a")["dedupe_key"])
kept, s = run([job("a"), dup_applied])
check("applied twin's status survives dedupe", len(kept) == 1)

dup_rejected = job("b", status="rejected", dedupe_key=job("a")["dedupe_key"])
kept, s = run([job("a", status="new"), dup_rejected])
check("rejected outranks new (no resurfacing)", kept == {"b"}, kept)

merged, _ = P.purge_jobs(
    [job("a", score=50, found_at=(NOW - timedelta(days=9)).isoformat()),
     job("b", score=95, dedupe_key=job("a")["dedupe_key"],
         last_seen_at=NOW.isoformat(), found_at=(NOW - timedelta(days=2)).isoformat())],
    CONFIG, verify=False, now=NOW,
)
check("merge keeps best score", merged[0]["score"] == 95, merged[0])
check("merge keeps oldest found_at", merged[0]["found_at"].startswith("2026-08-06"), merged[0])
check("merge keeps freshest last_seen_at", merged[0]["last_seen_at"] == NOW.isoformat())

# ── missing ──────────────────────────────────────────────────────────────────
print("\nmissing")
gone = job("gone")
kept, s = run([gone], seen_keys=set(), healthy_sources={"arbeitnow"})
check("first miss only flags", kept == {"gone"} and "missing_since" in gone, gone)

stale = job("stale", missing_since=(NOW - timedelta(days=3)).isoformat())
kept, s = run([stale], seen_keys=set(), healthy_sources={"arbeitnow"})
check("dropped after the grace window", kept == set() and s["missing"] == 1, s)

kept, _ = run([job("stale2", missing_since=(NOW - timedelta(days=3)).isoformat())],
              seen_keys=set(), healthy_sources=set())
check("dead source cannot purge its jobs", kept == {"stale2"}, kept)

seen = job("seen", missing_since=(NOW - timedelta(days=9)).isoformat())
kept, _ = run([seen], seen_keys={"seen"}, healthy_sources={"arbeitnow"})
check("re-sighting clears the missing flag", "missing_since" not in seen, seen)

kept, _ = run([job("a", status="applied", missing_since=(NOW - timedelta(days=30)).isoformat())],
              seen_keys=set(), healthy_sources={"arbeitnow"})
check("applied job is never purged", kept == {"a"}, kept)

by_key = job("newid", missing_since=(NOW - timedelta(days=3)).isoformat())
kept, _ = run([by_key], seen_keys={by_key["dedupe_key"]}, healthy_sources={"arbeitnow"})
check("sighting matched on dedupe_key too", kept == {"newid"}, kept)

# ── expiry ───────────────────────────────────────────────────────────────────
print("\nexpired")
kept, s = run([job("old", last_seen_at=(NOW - timedelta(days=20)).isoformat())])
check("unseen for max_age_days expires", kept == set() and s["expired"] == 1, s)

kept, _ = run([job("live", found_at=(NOW - timedelta(days=40)).isoformat(),
                   last_seen_at=(NOW - timedelta(hours=2)).isoformat())])
check("long-running but still-listed job stays", kept == {"live"}, kept)

kept, _ = run([job("legacy", found_at=(NOW - timedelta(days=30)).isoformat(),
                   last_seen_at="")])
check("legacy record falls back to found_at", kept == set(), kept)

# ── threshold ────────────────────────────────────────────────────────────────
print("\nbelow_threshold")
kept, s = run([job("low", score=10)], seen_keys={"low"})
check("rescored under min_score is dropped", kept == set() and s["below_threshold"] == 1, s)

kept, _ = run([job("low2", score=10)], seen_keys=set())
check("unseen job is not threshold-purged", kept == {"low2"}, kept)

kept, _ = run([job("onsite", remote=False)], seen_keys={"onsite"})
check("no longer remote is dropped", kept == set(), kept)

# ── dead links ───────────────────────────────────────────────────────────────
print("\ndead_link")
verdicts = {"d404": "dead", "dok": "alive", "derr": "unknown"}
real_check = P.check_url
P.check_url = lambda url, timeout=12: verdicts[url.rsplit("/", 1)[-1]]
try:
    jobs = [job("d404", url="https://x.test/j/d404"),
            job("dok", url="https://x.test/j/dok"),
            job("derr", url="https://x.test/j/derr")]
    kept, s = P.purge_jobs(jobs, {**CONFIG, "purge_verify_links": True},
                           seen_keys={"d404", "dok", "derr"},
                           healthy_sources={"arbeitnow"}, now=NOW)
    ids = {j["id"] for j in kept}
    check("404 posting removed", "d404" not in ids, ids)
    check("live posting kept", "dok" in ids, ids)
    check("unreachable posting kept", "derr" in ids, ids)
    check("check timestamps recorded", all(j.get("link_checked_at") for j in kept), kept)
    check("counted as dead_link", s["dead_link"] == 1, s)

    # recheck window: a job checked recently is not re-fetched
    calls = []
    P.check_url = lambda url, timeout=12: calls.append(url) or "alive"
    P.purge_jobs([job("fresh", link_checked_at=(NOW - timedelta(hours=1)).isoformat())],
                 {**CONFIG, "purge_verify_links": True}, now=NOW)
    check("recently checked link is skipped", calls == [], calls)
finally:
    P.check_url = real_check

print("\n" + ("ALL PASSED" if not _failures else f"FAILED: {_failures}"))
sys.exit(1 if _failures else 0)
