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
from tailor import build_resume_pdf, generate_cover_letter, tailor_resume

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


def get_salary_ask(job: dict, config: dict) -> int:
    """
    Calculate the salary to state on application forms.
      - No salary data on job → target_salary ($150k default)
      - Job offers >= target   → use their number (don't undersell)
      - Job offers < target    → their rate + 10%
    """
    target = config.get("target_salary", 150_000)
    offered = job.get("salary_min") or job.get("salary_parsed") or 0
    if not offered:
        return target
    if offered >= target:
        return offered
    return int(offered * 1.10)


async def fill_salary_fields(page: Page, salary: int) -> bool:
    """
    Find salary expectation inputs on the current page and fill them.
    Handles text inputs, number inputs, and select dropdowns.
    Returns True if any field was filled.
    """
    filled = False
    salary_str = str(salary)          # "150000"
    salary_k   = str(salary // 1000)  # "150"

    # Text / number inputs
    text_selectors = [
        "input[name*='salary' i]",
        "input[id*='salary' i]",
        "input[placeholder*='salary' i]",
        "input[name*='compensation' i]",
        "input[id*='compensation' i]",
        "input[placeholder*='compensation' i]",
        "input[name*='expected' i]",
        "input[placeholder*='expected' i]",
        "input[name*='desired' i]",
        "input[placeholder*='desired' i]",
    ]
    for sel in text_selectors:
        fld = page.locator(sel)
        if not await fld.count():
            continue
        el = fld.first
        try:
            await el.wait_for(state="visible", timeout=1500)
            val = await el.input_value()
            if val:
                continue  # already filled
            # Detect whether field expects thousands or full number
            placeholder = (await el.get_attribute("placeholder") or "").lower()
            max_attr = await el.get_attribute("max") or ""
            use_k = (
                "k" in placeholder
                or (max_attr.isdigit() and int(max_attr) < 10_000)
            )
            await human_type(page, el, salary_k if use_k else salary_str)
            filled = True
        except Exception:
            continue

    # Select dropdowns
    for sel_el in await page.locator("select").all():
        try:
            label = await sel_el.get_attribute("name") or await sel_el.get_attribute("id") or ""
            if not any(w in label.lower() for w in ("salary", "compensation", "expected", "desired")):
                continue
            opts = await sel_el.evaluate(
                "el => Array.from(el.options).map(o => ({v: o.value, t: o.text}))"
            )
            # Pick the option whose numeric value is closest to our ask
            best = None
            best_diff = float("inf")
            for opt in opts:
                nums = [int(n.replace(",", "")) for n in __import__("re").findall(r"\d[\d,]+", opt["t"])]
                if not nums:
                    continue
                diff = abs(nums[0] - salary)
                if diff < best_diff:
                    best_diff = diff
                    best = opt["v"]
            if best:
                await sel_el.select_option(best, timeout=1500)
                filled = True
        except Exception:
            continue

    if filled:
        print(f"  [salary] Filled ${salary:,}")
    return filled


async def upload_resume_if_possible(page: Page, pdf_path: Path) -> bool:
    """
    Look for a resume/CV file-upload input on the current page and set the
    generated ATS-optimised PDF.  Returns True if a file was attached.
    """
    selectors = [
        "input[type='file'][name*='resume' i]",
        "input[type='file'][name*='cv' i]",
        "input[type='file'][id*='resume' i]",
        "input[type='file'][id*='cv' i]",
        "input[type='file'][accept*='pdf' i]",
        "input[type='file'][accept*='.doc' i]",
        "input[type='file']",          # fallback: any file input
    ]
    for sel in selectors:
        try:
            el = page.locator(sel).first
            await el.wait_for(state="attached", timeout=2000)
            await el.set_input_files(str(pdf_path))
            print(f"  [upload] Resume PDF attached via {sel}")
            await nap(1, 2)
            return True
        except Exception:
            continue
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
    if "ziprecruiter.com" in host:
        return "ziprecruiter"
    if "roberthalf.com" in host:
        return "roberthalf"
    if "dice.com" in host:
        return "dice"
    if "workday.com" in host:
        return "workday"
    return "generic"


# ── LinkedIn Easy Apply ───────────────────────────────────────────────────────

async def apply_linkedin(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
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

        # Salary fields (if any step exposes them)
        if salary_ask:
            await fill_salary_fields(page, salary_ask)

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
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
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

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(page, "button:has-text('Submit'), button[type='submit']"):
        await nap(2, 3)
        print("  [Indeed] Submitted!")
        return True

    return False


# ── Greenhouse ────────────────────────────────────────────────────────────────

async def apply_greenhouse(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [Greenhouse] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

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

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(page, "input#submit_app, button:has-text('Submit Application')"):
        await nap(2, 4)
        print("  [Greenhouse] Submitted!")
        return True

    return False


# ── Lever ─────────────────────────────────────────────────────────────────────

async def apply_lever(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [Lever] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if not await click_if_visible(page, "a:has-text('Apply'), button:has-text('Apply')"):
        print("  [Lever] No apply button")
        return False

    await nap(2, 3)

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

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

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(page, "button:has-text('Submit application'), button[type='submit']"):
        await nap(2, 4)
        print("  [Lever] Submitted!")
        return True

    return False


# ── Generic fallback ──────────────────────────────────────────────────────────

async def apply_generic(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
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

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

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

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(
        page, "button[type='submit'], input[type='submit'], button:has-text('Submit')"
    ):
        await nap(2, 4)
        print("  [Generic] Submitted!")
        return True

    return False


# ── ZipRecruiter ─────────────────────────────────────────────────────────────

async def apply_ziprecruiter(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [ZipRecruiter] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    # ZipRecruiter uses "Apply Now" or "Quick Apply"
    if not await click_if_visible(
        page,
        "button:has-text('Apply Now'), a:has-text('Apply Now'), "
        "button:has-text('Quick Apply'), a:has-text('Quick Apply')",
    ):
        print("  [ZipRecruiter] No apply button — trying generic handler")
        return await apply_generic(page, job, cover_letter, config, pdf_path)

    await nap(2, 3)

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

    # ZipRecruiter may open a modal or redirect; detect which
    current_url = page.url
    if "ziprecruiter.com" not in current_url:
        # Redirected to employer ATS
        platform = detect_platform(current_url)
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        return await handler(page, job, cover_letter, config, pdf_path)

    # Fill modal form
    for sel, val in [
        ("input[name='name'], input[id*='name']", config.get("full_name", "")),
        ("input[type='email'], input[name='email']", config.get("email", "")),
        ("input[type='tel'], input[name='phone']", config.get("phone", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    fld = page.locator("textarea[name*='cover'], textarea[placeholder*='cover' i], textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter[:2000])

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(page, "button:has-text('Submit'), button[type='submit']"):
        await nap(2, 4)
        print("  [ZipRecruiter] Submitted!")
        return True

    return False


# ── Robert Half ───────────────────────────────────────────────────────────────

async def apply_roberthalf(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [Robert Half] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if not await click_if_visible(
        page,
        "a:has-text('Apply'), button:has-text('Apply'), "
        "a:has-text('Apply Now'), button:has-text('Apply Now')",
    ):
        print("  [Robert Half] No apply button")
        return False

    await nap(2, 3)

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

    for sel, val in [
        ("input[name='firstName'], input[id*='firstName']", (config.get("full_name") or "").split()[0]),
        ("input[name='lastName'], input[id*='lastName']", (config.get("full_name") or "").split()[-1]),
        ("input[type='email'], input[name='email']", config.get("email", "")),
        ("input[type='tel'], input[name='phone']", config.get("phone", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    # Cover letter
    fld = page.locator("textarea[name*='cover'], textarea[id*='cover'], textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter[:2000])

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()

    if await click_if_visible(page, "button:has-text('Submit'), button[type='submit']"):
        await nap(2, 4)
        print("  [Robert Half] Submitted!")
        return True

    return False


# ── Dice ─────────────────────────────────────────────────────────────────────

async def apply_dice(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    """
    Dice job pages usually redirect to an employer's ATS.
    We navigate there, detect the platform, and hand off.
    """
    print(f"  [Dice] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    # Click the Apply button — may redirect to external ATS
    apply_btn = page.locator(
        "a[data-cy='apply-button'], button[data-cy='apply-button'], "
        "a:has-text('Apply Now'), button:has-text('Apply Now')"
    )

    if await apply_btn.count():
        href = await apply_btn.first.get_attribute("href")
        if href and href.startswith("http") and "dice.com" not in href:
            # External link — navigate directly
            await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            await nap(2, 3)
        else:
            await apply_btn.first.click()
            await nap(2, 3)

    # After navigation, detect new platform and delegate
    current_url = page.url
    if "dice.com" not in current_url:
        platform = detect_platform(current_url)
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        return await handler(page, job, cover_letter, config, pdf_path)

    # Dice-native apply form (rare but exists)
    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)
    for sel, val in [
        ("input[name='firstName']", (config.get("full_name") or "").split()[0]),
        ("input[name='lastName']", (config.get("full_name") or "").split()[-1]),
        ("input[type='email']", config.get("email", "")),
        ("input[type='tel']", config.get("phone", "")),
    ]:
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    if await click_if_visible(page, "button:has-text('Submit'), button[type='submit']"):
        await nap(2, 4)
        print("  [Dice] Submitted!")
        return True

    return await apply_generic(page, job, cover_letter, config, pdf_path)


PLATFORM_HANDLERS = {
    "linkedin": apply_linkedin,
    "indeed": apply_indeed,
    "greenhouse": apply_greenhouse,
    "lever": apply_lever,
    "ziprecruiter": apply_ziprecruiter,
    "roberthalf": apply_roberthalf,
    "dice": apply_dice,
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
                desc = job.get("description", "")
                visible_text = tailor_resume(job["title"], desc, job["company"])
                cover = generate_cover_letter(job["title"], desc, job["company"])
                pdf_path = build_resume_pdf(
                    job["title"], desc, job["company"],
                    visible_resume=visible_text,
                    output_dir=DATA_DIR,
                )
                salary_ask = get_salary_ask(job, config)
                print(f"  [salary] Target ask: ${salary_ask:,}")
                success = await handler(page, job, cover, config, pdf_path, salary_ask)
                record["status"] = "applied" if success else "failed"
            except Exception as exc:
                print(f"  ERROR: {exc}")
                record["status"] = "error"
                record["error"] = str(exc)

            applied.append(record)

            # Update job status in the jobs list.
            # On error/failed: increment retry_count and keep "approved" so the
            # next run retries automatically. After 3 attempts, mark permanently failed.
            MAX_RETRIES = 3
            for j in jobs:
                if j["id"] == job["id"]:
                    if record["status"] == "applied":
                        j["status"] = "applied"
                    else:
                        retries = j.get("retry_count", 0) + 1
                        j["retry_count"] = retries
                        if retries >= MAX_RETRIES:
                            j["status"] = "failed"
                            print(f"  [retry] Giving up after {retries} attempts → marked failed")
                        else:
                            j["status"] = "approved"  # keep in queue for next run
                            print(f"  [retry] Attempt {retries}/{MAX_RETRIES} — will retry next run")
                    break
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
