#!/usr/bin/env python3
"""
Playwright job-application automation.

Reads jobs with status="approved" from data/jobs.json, tailors the resume
for each one via Claude AI, then submits applications through LinkedIn Easy
Apply, Indeed, or a generic form handler.

Usage:
    python scripts/apply.py            # apply to up to 5 approved jobs
    python scripts/apply.py 10         # apply to up to 10

Credentials come from environment variables (set as GitHub Secrets):
    ANTHROPIC_API_KEY
    LINKEDIN_EMAIL / LINKEDIN_PASSWORD
    APPLICANT_EMAIL / APPLICANT_NAME / APPLICANT_PHONE / LINKEDIN_URL
"""

import asyncio
import json
import os
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import Browser, BrowserContext, Page, async_playwright
from playwright.async_api import TimeoutError as PWTimeout

# Resolve sibling module without installing as a package
sys.path.insert(0, str(Path(__file__).parent))
from tailor import generate_cover_letter, tailor_resume

ROOT = Path(__file__).parent.parent
DATA_DIR = ROOT / "data"
CONFIG_PATH = ROOT / "config" / "config.json"
JOBS_PATH = DATA_DIR / "jobs.json"
APPLIED_PATH = DATA_DIR / "applied.json"


# ── I/O helpers ──────────────────────────────────────────────────────────────

def _load_json(path: Path, default):
    if path.exists():
        with open(path) as f:
            return json.load(f)
    return default


def _save_json(path: Path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def load_config() -> dict:
    cfg = _load_json(CONFIG_PATH, {})
    # Overlay GitHub Secrets so nothing sensitive is committed
    cfg["linkedin_email"] = os.environ.get("LINKEDIN_EMAIL", cfg.get("linkedin_email", ""))
    cfg["linkedin_password"] = os.environ.get("LINKEDIN_PASSWORD", cfg.get("linkedin_password", ""))
    cfg["email"] = os.environ.get("APPLICANT_EMAIL", cfg.get("email", ""))
    cfg["full_name"] = os.environ.get("APPLICANT_NAME", cfg.get("full_name", ""))
    cfg["phone"] = os.environ.get("APPLICANT_PHONE", cfg.get("phone", ""))
    cfg["linkedin_url"] = os.environ.get("LINKEDIN_URL", cfg.get("linkedin_url", ""))
    return cfg


# ── Playwright helpers ────────────────────────────────────────────────────────

async def human_type(page: Page, selector, text: str):
    """Fill a field with character-by-character human-like typing."""
    loc = page.locator(selector) if isinstance(selector, str) else selector
    await loc.first.click()
    await loc.first.fill("")
    for ch in text:
        await loc.first.type(ch, delay=random.randint(40, 130))


async def nap(lo=1.0, hi=3.0):
    await asyncio.sleep(random.uniform(lo, hi))


async def click_if_visible(page: Page, selector: str) -> bool:
    loc = page.locator(selector)
    try:
        await loc.first.wait_for(state="visible", timeout=3000)
        await loc.first.click()
        return True
    except PWTimeout:
        return False


# ── Platform detection ────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    host = urlparse(url).hostname or ""
    if "linkedin.com" in host:
        return "linkedin"
    if "indeed.com" in host:
        return "indeed"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "lever.co" in host:
        return "lever"
    return "generic"


# ── LinkedIn Easy Apply ───────────────────────────────────────────────────────

async def apply_linkedin(
    page: Page, job: dict, cover_letter: str, config: dict
) -> bool:
    print(f"  [LinkedIn] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    # Click Easy Apply button
    applied = await click_if_visible(
        page,
        "button.jobs-apply-button, button:has-text('Easy Apply')",
    )
    if not applied:
        print("  [LinkedIn] No Easy Apply button")
        return False

    for step in range(15):
        await nap(1, 2)

        # Phone
        for sel in [
            "input[id*='phoneNumber']",
            "input[name*='phone']",
            "input[placeholder*='phone' i]",
        ]:
            fld = page.locator(sel)
            if await fld.count() and not (await fld.first.input_value()):
                await human_type(page, fld, config.get("phone", ""))

        # Cover letter textarea
        for sel in [
            "textarea[id*='cover']",
            "textarea[name*='cover']",
            "textarea[placeholder*='cover' i]",
        ]:
            fld = page.locator(sel)
            if await fld.count() and not (await fld.first.input_value()):
                await human_type(page, fld, cover_letter[:1500])

        # Radio "Yes" (work auth, etc.)
        for lbl in await page.locator("label:has-text('Yes')").all():
            try:
                await lbl.click(timeout=1500)
            except Exception:
                pass

        # Dropdowns: pick the first non-empty option
        for sel_el in await page.locator("select").all():
            opts = await sel_el.evaluate(
                "el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))"
            )
            real = [o for o in opts if o["v"] and o["t"].strip() not in ("", "Select…", "Select")]
            if real:
                try:
                    await sel_el.select_option(real[0]["v"], timeout=1500)
                except Exception:
                    pass

        # Navigation
        if await click_if_visible(page, "button:has-text('Submit application')"):
            await nap(2, 4)
            print("  [LinkedIn] Submitted!")
            return True

        if not await click_if_visible(
            page,
            "button:has-text('Review'), button:has-text('Next'), "
            "button:has-text('Continue'), button[aria-label*='Continue']",
        ):
            print(f"  [LinkedIn] Stuck at step {step}")
            break

    return False


# ── Indeed ────────────────────────────────────────────────────────────────────

async def apply_indeed(
    page: Page, job: dict, cover_letter: str, config: dict
) -> bool:
    print(f"  [Indeed] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if not await click_if_visible(
        page, "button:has-text('Apply now'), a:has-text('Apply now'), #indeedApplyButton"
    ):
        print("  [Indeed] No apply button")
        return False

    await nap(2, 3)

    for sel, val in [
        ("input[name='name'], input[id*='name']", config.get("full_name", "")),
        ("input[type='email']", config.get("email", "")),
        ("input[type='tel']", config.get("phone", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    fld = page.locator("textarea[name*='cover'], textarea[id*='cover'], textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter[:2000])

    await nap()

    if await click_if_visible(page, "button:has-text('Submit'), button[type='submit']"):
        await nap(2, 3)
        print("  [Indeed] Submitted!")
        return True

    return False


# ── Greenhouse ────────────────────────────────────────────────────────────────

async def apply_greenhouse(
    page: Page, job: dict, cover_letter: str, config: dict
) -> bool:
    print(f"  [Greenhouse] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    for sel, val in [
        ("#first_name", config.get("full_name", "").split()[0] if config.get("full_name") else ""),
        ("#last_name", config.get("full_name", "").split()[-1] if config.get("full_name") else ""),
        ("#email", config.get("email", "")),
        ("#phone", config.get("phone", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    # LinkedIn profile
    fld = page.locator("input[placeholder*='linkedin' i], input[id*='linkedin' i]")
    if await fld.count():
        await human_type(page, fld, config.get("linkedin_url", ""))

    # Cover letter (some Greenhouse forms have one)
    fld = page.locator("textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter)

    await nap()

    if await click_if_visible(page, "input#submit_app, button:has-text('Submit Application')"):
        await nap(2, 4)
        print("  [Greenhouse] Submitted!")
        return True

    return False


# ── Lever ─────────────────────────────────────────────────────────────────────

async def apply_lever(
    page: Page, job: dict, cover_letter: str, config: dict
) -> bool:
    print(f"  [Lever] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if not await click_if_visible(page, "a:has-text('Apply'), button:has-text('Apply')"):
        print("  [Lever] No apply button")
        return False

    await nap(2, 3)

    for sel, val in [
        ("input[name='name']", config.get("full_name", "")),
        ("input[name='email']", config.get("email", "")),
        ("input[name='phone']", config.get("phone", "")),
        ("input[name='urls[LinkedIn]']", config.get("linkedin_url", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    fld = page.locator("textarea[name='comments']")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter)

    await nap()

    if await click_if_visible(page, "button:has-text('Submit application'), button[type='submit']"):
        await nap(2, 4)
        print("  [Lever] Submitted!")
        return True

    return False


# ── Generic fallback ──────────────────────────────────────────────────────────

async def apply_generic(
    page: Page, job: dict, cover_letter: str, config: dict
) -> bool:
    print(f"  [Generic] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if not await click_if_visible(
        page,
        "a:has-text('Apply Now'), button:has-text('Apply Now'), "
        "a:has-text('Apply'), button:has-text('Apply')",
    ):
        print("  [Generic] No apply button found")
        return False

    await nap(2, 3)

    for sel, val in [
        ("input[name='name'], input[placeholder*='name' i]", config.get("full_name", "")),
        ("input[type='email'], input[name='email']", config.get("email", "")),
        ("input[type='tel'], input[name='phone']", config.get("phone", "")),
        (
            "input[placeholder*='linkedin' i], input[name*='linkedin' i]",
            config.get("linkedin_url", ""),
        ),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    fld = page.locator("textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter)

    await nap()

    if await click_if_visible(
        page, "button[type='submit'], input[type='submit'], button:has-text('Submit')"
    ):
        await nap(2, 4)
        print("  [Generic] Submitted!")
        return True

    return False


PLATFORM_HANDLERS = {
    "linkedin": apply_linkedin,
    "indeed": apply_indeed,
    "greenhouse": apply_greenhouse,
    "lever": apply_lever,
    "generic": apply_generic,
}


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(max_apply: int = 5):
    config = load_config()
    jobs: list[dict] = _load_json(JOBS_PATH, [])
    applied: list[dict] = _load_json(APPLIED_PATH, [])
    applied_ids = {a["id"] for a in applied}

    queue = [
        j for j in jobs
        if j.get("status") == "approved" and j["id"] not in applied_ids
    ][:max_apply]

    if not queue:
        print("No jobs with status='approved' found. Approve jobs in the dashboard first.")
        return

    print(f"Applying to {len(queue)} job(s)…")

    async with async_playwright() as pw:
        browser: Browser = await pw.chromium.launch(
            headless=config.get("headless", True),
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        ctx: BrowserContext = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1440, "height": 900},
        )
        page: Page = await ctx.new_page()

        # LinkedIn login (reused across all LinkedIn jobs)
        needs_li = any(detect_platform(j["url"]) == "linkedin" for j in queue)
        if needs_li and config.get("linkedin_email") and config.get("linkedin_password"):
            print("Logging into LinkedIn…")
            await page.goto("https://www.linkedin.com/login", wait_until="domcontentloaded")
            await human_type(page, "#username", config["linkedin_email"])
            await human_type(page, "#password", config["linkedin_password"])
            await page.click("[type='submit']")
            await nap(4, 7)

        for job in queue:
            platform = detect_platform(job["url"])
            handler = PLATFORM_HANDLERS.get(platform, apply_generic)
            record: dict = {
                "id": job["id"],
                "title": job["title"],
                "company": job["company"],
                "url": job["url"],
                "platform": platform,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            }

            try:
                print(f"\nTailoring resume for: {job['title']} @ {job['company']}")
                cover = generate_cover_letter(
                    job["title"], job.get("description", ""), job["company"]
                )
                success = await handler(page, job, cover, config)
                record["status"] = "applied" if success else "failed"
            except Exception as exc:
                print(f"  ERROR: {exc}")
                record["status"] = "error"
                record["error"] = str(exc)

            applied.append(record)

            # Update job status in the jobs list
            for j in jobs:
                if j["id"] == job["id"]:
                    j["status"] = record["status"]
                    break

            delay = random.uniform(
                config.get("apply_delay_min", 5),
                config.get("apply_delay_max", 12),
            )
            print(f"  Waiting {delay:.1f}s before next application…")
            await asyncio.sleep(delay)

        await browser.close()

    _save_json(JOBS_PATH, jobs)
    _save_json(APPLIED_PATH, applied)

    n_ok = sum(1 for r in applied if r["status"] == "applied")
    print(f"\n=== Done: {n_ok}/{len(queue)} applied successfully ===")


if __name__ == "__main__":
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 5
    asyncio.run(run(limit))
