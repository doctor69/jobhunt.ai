#!/usr/bin/env python3
"""
Job scanner — fetches jobs from multiple free APIs, scores them against
keywords in config.json, and writes results to data/jobs.json.
Runs every 6 hrs via GitHub Actions.
"""

import json
import hashlib
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config" / "config.json"
JOBS_PATH = DATA_DIR / "jobs.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def load_existing_jobs():
    if JOBS_PATH.exists():
        with open(JOBS_PATH) as f:
            jobs = json.load(f)
        return {j["id"]: j for j in jobs}
    return {}


def make_id(url: str, title: str, company: str) -> str:
    raw = f"{url}{title}{company}".lower()
    return hashlib.md5(raw.encode()).hexdigest()[:12]


import re as _re

def _extract_salary(text: str) -> int | None:
    """Parse the first salary figure found in plain text."""
    patterns = [
        r'\$\s*(\d{2,3})[kK]',                          # $120k
        r'\$\s*(\d{2,3}),\d{3}',                        # $120,000
        r'(\d{2,3})[kK]\s*[-–to]+\s*\$?(\d{2,3})[kK]', # 120K-150K
        r'USD\s+(\d{2,3})[kK]',                          # USD 120K
        r'(\d{2,3}),\d{3}\s*[-–]',                      # 120,000-
    ]
    for pat in patterns:
        m = _re.search(pat, text, _re.IGNORECASE)
        if m:
            val = int(m.group(1))
            return val * 1000 if val < 500 else val
    return None


def score_job(job: dict, config: dict) -> dict:
    """
    Custom scoring scale (max 100):
      Remote gate  : non-remote jobs score 0 when remote_required=true
      Remote bonus : fully remote → +40 pts  |  hybrid → +15 pts
      Salary tiers : $180k+ → +35  |  $150k → +30  |  $130k → +25
                     $100k → +20   |  $80k  → +10   |  <$80k → +5
      Keywords     : +5 per match, capped at 25 pts
    """
    text = " ".join([
        job.get("title", ""),
        job.get("description", ""),
        job.get("tags", ""),
    ]).lower()
    loc = job.get("location", "").lower()

    # ── Remote detection ──────────────────────────────────────────────────────
    remote_words = {"remote", "anywhere", "work from home", "wfh", "fully remote",
                    "100% remote", "remote-first", "distributed"}
    hybrid_words = {"hybrid"}

    is_remote = any(w in loc for w in remote_words) or any(w in text for w in remote_words)
    is_hybrid = (not is_remote) and (any(w in loc for w in hybrid_words) or "hybrid" in text)
    is_onsite = not is_remote and not is_hybrid

    # Hard gate: only fully remote jobs pass when remote_required=true
    if config.get("remote_required", True) and not is_remote:
        job["score"] = 0
        job["matched_keywords"] = []
        job["remote"] = False
        return job

    score = 40  # fully remote baseline

    # ── Salary ────────────────────────────────────────────────────────────────
    sal = job.get("salary_min") or _extract_salary(text)
    if sal:
        if sal >= 180_000:   score += 35
        elif sal >= 150_000: score += 30
        elif sal >= 130_000: score += 25
        elif sal >= 100_000: score += 20
        elif sal >= 80_000:  score += 10
        else:                score += 5
    job["salary_parsed"] = sal  # store so the dashboard can display it

    # ── Keywords ──────────────────────────────────────────────────────────────
    matched = [kw for kw in config.get("keywords", []) if kw.lower() in text]
    score += min(len(matched) * 5, 25)

    job["score"] = min(score, 100)
    job["matched_keywords"] = matched
    job["remote"] = is_remote
    return job


# ── Sources ──────────────────────────────────────────────────────────────────

def fetch_remotive(config: dict) -> list[dict]:
    jobs = []
    categories = config.get("remotive_categories", ["software-dev"])
    for cat in categories:
        try:
            url = f"https://remotive.com/api/remote-jobs?category={cat}&limit=50"
            r = requests.get(url, headers=HEADERS, timeout=20)
            r.raise_for_status()
            for j in r.json().get("jobs", []):
                jobs.append({
                    "id": make_id(j["url"], j["title"], j["company_name"]),
                    "title": j["title"],
                    "company": j["company_name"],
                    "location": j.get("candidate_required_location", "Remote"),
                    "url": j["url"],
                    "description": (j.get("description") or "")[:3000],
                    "tags": " ".join(j.get("tags") or []),
                    "salary_min": j.get("salary_min"),
                    "salary_max": j.get("salary_max"),
                    "posted_at": j.get("publication_date", ""),
                    "source": "remotive",
                    "status": "new",
                    "found_at": datetime.now(timezone.utc).isoformat(),
                })
        except Exception as e:
            print(f"[remotive/{cat}] {e}", file=sys.stderr)
    return jobs


def fetch_arbeitnow(config: dict) -> list[dict]:
    jobs = []
    try:
        r = requests.get(
            "https://www.arbeitnow.com/api/job-board-api",
            headers=HEADERS,
            timeout=20,
        )
        r.raise_for_status()
        for j in r.json().get("data", []):
            jobs.append({
                "id": make_id(j["url"], j["title"], j["company_name"]),
                "title": j["title"],
                "company": j["company_name"],
                "location": j.get("location", "Remote"),
                "url": j["url"],
                "description": (j.get("description") or "")[:3000],
                "tags": " ".join(j.get("tags") or []),
                "salary_min": None,
                "salary_max": None,
                "posted_at": str(j.get("created_at", "")),
                "source": "arbeitnow",
                "status": "new",
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f"[arbeitnow] {e}", file=sys.stderr)
    return jobs


def fetch_themuse(config: dict) -> list[dict]:
    jobs = []
    try:
        categories = config.get("muse_categories", ["Engineering"])
        for cat in categories:
            r = requests.get(
                "https://www.themuse.com/api/public/jobs",
                params={"category": cat, "page": 1, "descending": "true"},
                headers=HEADERS,
                timeout=20,
            )
            r.raise_for_status()
            for j in r.json().get("results", []):
                loc = (j.get("locations") or [{}])[0].get("name", "Remote")
                url = j.get("refs", {}).get("landing_page", "")
                jobs.append({
                    "id": make_id(url, j["name"], j["company"]["name"]),
                    "title": j["name"],
                    "company": j["company"]["name"],
                    "location": loc,
                    "url": url,
                    "description": (j.get("contents") or "")[:3000],
                    "tags": " ".join(c["name"] for c in j.get("categories") or []),
                    "salary_min": None,
                    "salary_max": None,
                    "posted_at": j.get("publication_date", ""),
                    "source": "themuse",
                    "status": "new",
                    "found_at": datetime.now(timezone.utc).isoformat(),
                })
    except Exception as e:
        print(f"[themuse] {e}", file=sys.stderr)
    return jobs


# ── Dice ─────────────────────────────────────────────────────────────────────

def fetch_dice(config: dict) -> list[dict]:
    jobs = []
    keywords = config.get("keywords", [])
    query = " ".join(keywords[:4]) if keywords else "software engineer"
    try:
        r = requests.get(
            "https://job-search-api.svc.dhigroupinc.com/v1/dice/jobs/search",
            params={
                "q": query,
                "countryCode": "US",
                "radius": 30,
                "radiusUnit": "mi",
                "page": 1,
                "pageSize": 50,
                "language": "en",
            },
            headers={**HEADERS, "accept": "application/json"},
            timeout=20,
        )
        r.raise_for_status()
        for j in r.json().get("data", []):
            url = j.get("detailUrl") or j.get("applyUrl") or ""
            if not url.startswith("http"):
                url = f"https://www.dice.com/jobs/detail/{j.get('id', '')}"
            jobs.append({
                "id": make_id(url, j.get("title", ""), j.get("company", "")),
                "title": j.get("title", ""),
                "company": j.get("company", ""),
                "location": j.get("location", "Remote"),
                "url": url,
                "description": (j.get("jobDescription") or "")[:3000],
                "tags": " ".join(j.get("skills") or []),
                "salary_min": None,
                "salary_max": None,
                "posted_at": j.get("postedDate", ""),
                "source": "dice",
                "status": "new",
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f"[dice] {e}", file=sys.stderr)
    return jobs


# ── ZipRecruiter ──────────────────────────────────────────────────────────────

def fetch_ziprecruiter(config: dict) -> list[dict]:
    """Scrape ZipRecruiter search results (HTML, no API key needed)."""
    from bs4 import BeautifulSoup
    jobs = []
    keywords = config.get("keywords", [])
    query = "+".join(keywords[:3]) if keywords else "software+engineer"
    location = config.get("location", "Remote")
    try:
        url = (
            f"https://www.ziprecruiter.com/candidate/search"
            f"?search={query}&location={location}&days=7"
        )
        r = requests.get(url, headers=HEADERS, timeout=25)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for article in soup.select("article.job_result, div[data-job-id]"):
            title_el = article.select_one("h2 a, .job_title a, a[data-job-title]")
            co_el = article.select_one(".hiring_company_text, a.hiring_company, .company_name")
            loc_el = article.select_one(".location, .job_location")
            desc_el = article.select_one(".job_description, p.job_snippet")
            link_el = article.select_one("a[href*='/jobs/'], a[data-job-url]")

            title = title_el.get_text(strip=True) if title_el else ""
            company = co_el.get_text(strip=True) if co_el else ""
            href = link_el["href"] if link_el and link_el.get("href") else ""
            if not href.startswith("http"):
                href = "https://www.ziprecruiter.com" + href

            if not title or not href:
                continue

            jobs.append({
                "id": make_id(href, title, company),
                "title": title,
                "company": company,
                "location": loc_el.get_text(strip=True) if loc_el else location,
                "url": href,
                "description": desc_el.get_text(strip=True) if desc_el else "",
                "tags": "",
                "salary_min": None,
                "salary_max": None,
                "posted_at": "",
                "source": "ziprecruiter",
                "status": "new",
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f"[ziprecruiter] {e}", file=sys.stderr)
    return jobs


# ── Robert Half ───────────────────────────────────────────────────────────────

def fetch_roberthalf(config: dict) -> list[dict]:
    from bs4 import BeautifulSoup
    jobs = []
    keywords = config.get("keywords", [])
    query = " ".join(keywords[:3]) if keywords else "software engineer"
    try:
        r = requests.get(
            "https://www.roberthalf.com/us/en/jobs",
            params={"keywords": query, "location": "remote", "industry": "Technology"},
            headers={**HEADERS, "accept": "application/json, text/html"},
            timeout=25,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        for card in soup.select("div.job-card, article.job-listing, li.job-result"):
            title_el = card.select_one("h2, h3, .job-title, a.title")
            co_el = card.select_one(".company, .employer")
            loc_el = card.select_one(".location, .job-location")
            link_el = card.select_one("a[href*='/job/'], a[href*='/jobs/']")
            desc_el = card.select_one(".description, p")

            title = title_el.get_text(strip=True) if title_el else ""
            href = link_el["href"] if link_el and link_el.get("href") else ""
            if not href.startswith("http"):
                href = "https://www.roberthalf.com" + href
            if not title or not href:
                continue

            jobs.append({
                "id": make_id(href, title, co_el.get_text(strip=True) if co_el else ""),
                "title": title,
                "company": co_el.get_text(strip=True) if co_el else "Robert Half",
                "location": loc_el.get_text(strip=True) if loc_el else "Remote",
                "url": href,
                "description": desc_el.get_text(strip=True) if desc_el else "",
                "tags": "",
                "salary_min": None,
                "salary_max": None,
                "posted_at": "",
                "source": "roberthalf",
                "status": "new",
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f"[roberthalf] {e}", file=sys.stderr)
    return jobs


# ── Jobot ─────────────────────────────────────────────────────────────────────

def fetch_jobot(config: dict) -> list[dict]:
    """Scrape Jobot job listings via their JSON search API."""
    jobs = []
    keywords = config.get("keywords", [])
    query = " ".join(keywords[:4]) if keywords else "software engineer"
    remote = config.get("remote_required", True)
    try:
        params = {
            "q": query,
            "l": "Remote" if remote else config.get("location", ""),
            "page": 1,
        }
        resp = requests.get(
            "https://jobot.com/search",
            params=params,
            headers=HEADERS,
            timeout=15,
        )
        resp.raise_for_status()
        # Jobot's search returns HTML; parse <script id="__NEXT_DATA__"> JSON
        import re
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', resp.text, re.S)
        if not m:
            return jobs
        data = json.loads(m.group(1))
        listings = (
            data.get("props", {})
                .get("pageProps", {})
                .get("jobList", {})
                .get("jobs", [])
        )
        for item in listings:
            jid = str(item.get("id", ""))
            if not jid:
                continue
            slug = item.get("slug", jid)
            jobs.append({
                "id": f"jobot_{jid}",
                "title": item.get("title", ""),
                "company": item.get("company", {}).get("name", "Unknown") if isinstance(item.get("company"), dict) else item.get("company", "Unknown"),
                "location": item.get("location", "Remote"),
                "description": item.get("description", ""),
                "url": f"https://jobot.com/jobs/detail/{jid}/{slug}",
                "source": "jobot",
                "status": "new",
                "found_at": datetime.now(timezone.utc).isoformat(),
            })
    except Exception as e:
        print(f"[jobot] {e}", file=sys.stderr)
    return jobs


# ── Main ─────────────────────────────────────────────────────────────────────

SOURCE_MAP = {
    "remotive": fetch_remotive,
    "arbeitnow": fetch_arbeitnow,
    "themuse": fetch_themuse,
    "dice": fetch_dice,
    "ziprecruiter": fetch_ziprecruiter,
    "roberthalf": fetch_roberthalf,
    "jobot": fetch_jobot,
}


def main():
    print("=== Job Scanner Starting ===")
    config = load_config()
    existing = load_existing_jobs()

    raw_jobs: list[dict] = []
    for src in config.get("sources", list(SOURCE_MAP.keys())):
        fn = SOURCE_MAP.get(src)
        if fn:
            print(f"Fetching from {src}…")
            raw_jobs.extend(fn(config))
        else:
            print(f"[warn] Unknown source: {src}", file=sys.stderr)

    auto_approve_score = config.get("auto_approve_score", 65)
    max_age_days = config.get("max_age_days", 14)
    cutoff = datetime.now(timezone.utc) - __import__("datetime").timedelta(days=max_age_days)

    # Drop jobs older than max_age_days; always keep applied ones for records
    before = len(existing)
    existing = {
        jid: j for jid, j in existing.items()
        if j.get("status") == "applied"
        or not j.get("found_at")
        or datetime.fromisoformat(j["found_at"]).replace(tzinfo=timezone.utc) >= cutoff
    }
    expired = before - len(existing)
    if expired:
        print(f"Pruned {expired} job(s) older than {max_age_days} days")

    added = approved = 0

    for job in raw_jobs:
        job = score_job(job, config)
        if job["score"] < config.get("min_score", 30):
            continue
        if job["id"] not in existing:
            # Auto-approve high-scoring new jobs so apply.py picks them up
            # immediately without any manual step
            if job["score"] >= auto_approve_score:
                job["status"] = "approved"
                approved += 1
            existing[job["id"]] = job
            added += 1
        else:
            # Refresh score/keywords but never downgrade a status the user
            # (or a previous run) has already set to applied/rejected
            prev = existing[job["id"]]
            prev["score"] = job["score"]
            prev["matched_keywords"] = job["matched_keywords"]
            prev["remote"] = job.get("remote", prev.get("remote"))
            prev["salary_parsed"] = job.get("salary_parsed")
            # If score just crossed the threshold and job is still 'new', approve it
            if prev.get("status") == "new" and job["score"] >= auto_approve_score:
                prev["status"] = "approved"
                approved += 1

    all_jobs = sorted(
        existing.values(),
        key=lambda j: (-j["score"], j.get("found_at", "")),
    )

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    with open(JOBS_PATH, "w") as f:
        json.dump(all_jobs, f, indent=2)

    print(f"=== Done: {added} new | {approved} auto-approved | {expired} expired | {len(all_jobs)} total ===")


if __name__ == "__main__":
    main()
