#!/usr/bin/env python3
"""
Purge — removes jobs from data/jobs.json that are no longer true.

Runs as part of every scan (see scan.py) and can also be run standalone:

    python scripts/purge.py --dry-run     # report what would go, change nothing
    python scripts/purge.py               # clean the stored jobs, no scanning
    python scripts/purge.py --no-verify   # skip the HTTP dead-link checks

A job is dropped for one of five reasons:

  duplicate       another record points at the same posting (job boards mirror
                  the same listing across domains with a fresh numeric suffix,
                  so make_id() saw it as brand new on every scan)
  missing         its source was scanned successfully but stopped listing it,
                  and it has stayed missing for purge_missing_days
  expired         not seen in any feed for max_age_days
  dead_link       the posting URL answers 404/410, redirects back to the board
                  root, or the page says the role is filled/closed
  below_threshold rescored under min_score, or no longer remote while
                  remote_required is on

Applied jobs are never purged — they are the application record.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
JOBS_PATH = DATA_DIR / "jobs.json"
APPLIED_PATH = DATA_DIR / "applied.json"
CONFIG_PATH = ROOT / "config" / "config.json"
PURGE_LOG_PATH = DATA_DIR / "purge_log.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

# An applied job is the record that we applied — it outlives the posting.
PROTECTED_STATUSES = {"applied"}

# Lower rank wins when two records describe the same posting. A rejected job
# outranks a new one on purpose: dropping it would let the posting come back as
# "new", get auto-approved, and be applied to after the user said no.
STATUS_RANK = {"applied": 0, "rejected": 1, "approved": 2, "failed": 3, "error": 4, "new": 5}

# Only these responses are treated as proof the posting is gone. Anything else
# (403 from a bot wall, 429, timeouts, 5xx) leaves the job untouched.
DEAD_HTTP_STATUSES = {404, 410, 451}
GONE_MARKERS = (
    "no longer available",
    "no longer accepting applications",
    "this position has been filled",
    "position has been filled",
    "this job has expired",
    "job posting has expired",
    "this job is no longer",
    "posting has been removed",
    "job not found",
    "the job you are looking for",
)

REASONS = ("duplicate", "missing", "expired", "dead_link", "below_threshold")


# ── helpers ──────────────────────────────────────────────────────────────────

def parse_ts(value) -> datetime | None:
    """Parse an ISO timestamp defensively — bad data must not kill a scan."""
    if not value or not isinstance(value, str):
        return None
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _ts(value, late: bool = False) -> datetime:
    """parse_ts with a sentinel, so timestamps are always comparable."""
    fallback = datetime.max if late else datetime.min
    return parse_ts(value) or fallback.replace(tzinfo=timezone.utc)


def normalize_url(url: str) -> str:
    """
    Strip the noise that makes one posting look like many:
    scheme, www., country-mirror TLD, tracking query, trailing id suffix.
    """
    u = (url or "").strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    u = u.split("?")[0].split("#")[0].rstrip("/")
    # arbeitnow.com / arbeitnow.co.uk / arbeitnow.de are the same board
    u = re.sub(r"^([a-z0-9-]+)\.(co\.uk|com|de|io|net|org)/", r"\1/", u)
    # ".../remote-backend-engineer-413326" — the suffix is re-rolled per scrape
    u = re.sub(r"-\d{4,}$", "", u)
    return u


def _norm_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def dedupe_key(job: dict) -> str:
    """Stable identity for a posting, independent of the URL it was found at."""
    raw = "|".join([
        _norm_text(job.get("company", "")),
        _norm_text(job.get("title", "")),
        normalize_url(job.get("url", "")),
    ])
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def ensure_metadata(job: dict) -> dict:
    """Backfill the fields purging relies on for records written before it existed."""
    job.setdefault("dedupe_key", dedupe_key(job))
    job.setdefault("last_seen_at", job.get("found_at", ""))
    return job


def mark_seen(job: dict, now: datetime) -> dict:
    """Called for every job a source still lists — resets the staleness clocks."""
    job["last_seen_at"] = now.isoformat()
    job.pop("missing_since", None)
    return job


def load_applied_ids() -> set[str]:
    try:
        with open(APPLIED_PATH) as f:
            return {a["id"] for a in json.load(f) if a.get("status") == "applied"}
    except (OSError, ValueError, KeyError, TypeError):
        return set()


def _rank(job: dict) -> tuple:
    """Sort key for picking which of several duplicate records survives."""
    return (
        STATUS_RANK.get(job.get("status", "new"), 9),
        _ts(job.get("found_at"), late=True),
        -(job.get("score") or 0),
    )


# ── dead-link verification ───────────────────────────────────────────────────

def check_url(url: str, timeout: int = 12) -> str:
    """Return 'dead', 'alive' or 'unknown'. Only hard evidence returns 'dead'."""
    if not url or not url.startswith("http"):
        return "dead"
    try:
        r = requests.get(
            url, headers=HEADERS, timeout=timeout, allow_redirects=True, stream=True
        )
    except requests.RequestException:
        return "unknown"

    try:
        if r.status_code in DEAD_HTTP_STATUSES:
            return "dead"
        if r.status_code != 200:
            return "unknown"

        # Redirected off the posting and back to a board index → posting pulled.
        landed = normalize_url(r.url)
        if "/" not in landed and "/" in normalize_url(url):
            return "dead"

        body = r.raw.read(60_000, decode_content=True) or b""
        text = body.decode("utf-8", "ignore").lower()
        if any(m in text for m in GONE_MARKERS):
            return "dead"
        return "alive"
    finally:
        r.close()


def verify_links(jobs: list[dict], config: dict, now: datetime) -> dict[str, str]:
    """
    Check a budgeted slice of postings per run. Jobs already flagged missing and
    jobs queued for applying are checked first — those are the ones where a dead
    link actually costs something.
    """
    limit = int(config.get("purge_link_check_limit", 150))
    recheck_after = timedelta(days=float(config.get("purge_link_recheck_days", 3)))
    workers = int(config.get("purge_link_check_workers", 8))
    if limit <= 0:
        return {}

    def due(job: dict) -> bool:
        last = parse_ts(job.get("link_checked_at"))
        return last is None or now - last >= recheck_after

    def priority(job: dict) -> tuple:
        return (
            0 if job.get("missing_since") else 1,
            0 if job.get("status") in ("approved", "failed", "error") else 1,
            _ts(job.get("link_checked_at")),
        )

    queue = sorted((j for j in jobs if due(j)), key=priority)[:limit]
    if not queue:
        return {}

    results: dict[str, str] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(check_url, j.get("url", "")): j for j in queue}
        for fut in concurrent.futures.as_completed(futures):
            job = futures[fut]
            try:
                verdict = fut.result()
            except Exception:
                verdict = "unknown"
            job["link_checked_at"] = now.isoformat()
            job["link_status"] = verdict
            results[job["id"]] = verdict
    return results


# ── purge ────────────────────────────────────────────────────────────────────

def purge_jobs(
    jobs: list[dict],
    config: dict,
    *,
    seen_keys: set[str] | None = None,
    healthy_sources: set[str] | None = None,
    verify: bool | None = None,
    now: datetime | None = None,
) -> tuple[list[dict], dict]:
    """
    Return (kept_jobs, summary).

    seen_keys        ids + dedupe_keys returned by the sources in this run
    healthy_sources  sources that answered with results this run. A source that
                     errored or got rate-limited returns nothing, and without
                     this guard that would read as "every job of that source is
                     gone" and wipe them.
    """
    now = now or datetime.now(timezone.utc)
    seen_keys = seen_keys or set()
    healthy_sources = healthy_sources if healthy_sources is not None else set()
    if verify is None:
        verify = bool(config.get("purge_verify_links", True))

    max_age = timedelta(days=float(config.get("max_age_days", 14)))
    missing_grace = timedelta(days=float(config.get("purge_missing_days", 2)))
    min_score = config.get("min_score", 30)
    remote_required = config.get("remote_required", True)
    applied_ids = load_applied_ids()

    removed: list[dict] = []
    counts = {r: 0 for r in REASONS}

    def protect(job: dict) -> bool:
        return job.get("status") in PROTECTED_STATUSES or job["id"] in applied_ids

    def drop(job: dict, reason: str) -> None:
        counts[reason] += 1
        removed.append({
            "id": job.get("id"),
            "title": job.get("title", "")[:80],
            "company": job.get("company", "")[:60],
            "source": job.get("source"),
            "status": job.get("status"),
            "reason": reason,
        })

    # ── 1. collapse duplicate records of the same posting ────────────────────
    groups: dict[str, list[dict]] = {}
    for job in jobs:
        ensure_metadata(job)
        groups.setdefault(job["dedupe_key"], []).append(job)

    survivors: list[dict] = []
    for group in groups.values():
        group.sort(key=_rank)
        keeper, *dupes = group
        for dupe in dupes:
            # Fold anything worth keeping into the survivor before dropping it.
            keeper["score"] = max(keeper.get("score") or 0, dupe.get("score") or 0)
            keeper["retry_count"] = max(
                keeper.get("retry_count", 0), dupe.get("retry_count", 0)
            )
            if not keeper.get("description") and dupe.get("description"):
                keeper["description"] = dupe["description"]
            # Oldest discovery, freshest sighting.
            if _ts(dupe.get("found_at"), late=True) < _ts(keeper.get("found_at"), late=True):
                keeper["found_at"] = dupe["found_at"]
            if _ts(dupe.get("last_seen_at")) > _ts(keeper.get("last_seen_at")):
                keeper["last_seen_at"] = dupe["last_seen_at"]
                keeper.pop("missing_since", None)
            if protect(dupe):
                # Never lose an application record to deduplication.
                keeper["status"] = dupe["status"]
            drop(dupe, "duplicate")
        survivors.append(keeper)

    # ── 2. flag / drop postings their source has stopped listing ─────────────
    kept: list[dict] = []
    for job in survivors:
        if protect(job):
            job.pop("missing_since", None)
            kept.append(job)
            continue

        source_ok = job.get("source") in healthy_sources
        was_seen = job["id"] in seen_keys or job["dedupe_key"] in seen_keys

        if was_seen:
            # Back on the board — a later disappearance gets a fresh grace window.
            job.pop("missing_since", None)
        elif source_ok:
            first_missing = parse_ts(job.get("missing_since"))
            if first_missing is None:
                job["missing_since"] = now.isoformat()
            elif now - first_missing >= missing_grace:
                drop(job, "missing")
                continue

        # ── 3. nothing has listed it for max_age_days ────────────────────────
        last_seen = parse_ts(job.get("last_seen_at")) or parse_ts(job.get("found_at"))
        if last_seen is not None and now - last_seen >= max_age:
            drop(job, "expired")
            continue

        # ── 4. no longer matches what we are looking for ─────────────────────
        if was_seen:
            if (job.get("score") or 0) < min_score:
                drop(job, "below_threshold")
                continue
            if remote_required and job.get("remote") is False:
                drop(job, "below_threshold")
                continue

        kept.append(job)

    # ── 5. confirm the posting still resolves ────────────────────────────────
    checked = 0
    if verify and kept:
        candidates = [j for j in kept if not protect(j)]
        verdicts = verify_links(candidates, config, now)
        checked = len(verdicts)
        alive = []
        for job in kept:
            if verdicts.get(job["id"]) == "dead":
                drop(job, "dead_link")
            else:
                alive.append(job)
        kept = alive

    summary = {
        "at": now.isoformat(),
        "before": len(jobs),
        "after": len(kept),
        "removed": sum(counts.values()),
        "links_checked": checked,
        "healthy_sources": sorted(healthy_sources),
        **counts,
    }
    return kept, {**summary, "details": removed}


def write_purge_log(summary: dict, keep_runs: int = 50) -> None:
    """Append a run summary to data/purge_log.json (details trimmed, last N runs)."""
    entry = {k: v for k, v in summary.items() if k != "details"}
    entry["sample"] = summary.get("details", [])[:20]
    try:
        with open(PURGE_LOG_PATH) as f:
            log = json.load(f)
        if not isinstance(log, list):
            log = []
    except (OSError, ValueError):
        log = []
    log.append(entry)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(PURGE_LOG_PATH, "w") as f:
        json.dump(log[-keep_runs:], f, indent=2)


def print_summary(summary: dict) -> None:
    print(
        f"Purge: {summary['before']} → {summary['after']} "
        f"(-{summary['removed']}) | "
        + " ".join(f"{r}={summary[r]}" for r in REASONS)
        + f" | links checked={summary['links_checked']}"
    )


# ── standalone entry point ───────────────────────────────────────────────────

def main() -> int:
    ap = argparse.ArgumentParser(description="Purge stale jobs from data/jobs.json")
    ap.add_argument("--dry-run", action="store_true", help="report only, write nothing")
    ap.add_argument("--no-verify", action="store_true", help="skip HTTP dead-link checks")
    ap.add_argument("--verbose", action="store_true", help="list every removed job")
    args = ap.parse_args()

    with open(CONFIG_PATH) as f:
        config = json.load(f)
    with open(JOBS_PATH) as f:
        jobs = json.load(f)

    # Standalone: no scan ran, so no source is known-healthy and nothing counts
    # as "missing" — only duplicates, age, thresholds and dead links apply.
    kept, summary = purge_jobs(jobs, config, verify=not args.no_verify)
    print_summary(summary)

    if args.verbose:
        for r in summary["details"]:
            print(f"  - [{r['reason']}] {r['company']} — {r['title']} ({r['status']})")

    if args.dry_run:
        print("(dry run — data/jobs.json unchanged)")
        return 0

    kept.sort(key=lambda j: (-(j.get("score") or 0), j.get("found_at", "")))
    with open(JOBS_PATH, "w") as f:
        json.dump(kept, f, indent=2)
    write_purge_log(summary)
    print(f"Wrote {len(kept)} job(s) to {JOBS_PATH}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
