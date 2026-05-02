#!/usr/bin/env python3
from __future__ import annotations
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
RESUMES_DIR = DATA_DIR / "resumes"
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
    cfg["ziprecruiter_email"] = os.environ.get("ZIPRECRUITER_EMAIL", cfg.get("ziprecruiter_email", ""))
    cfg["ziprecruiter_password"] = os.environ.get("ZIPRECRUITER_PASSWORD", cfg.get("ziprecruiter_password", ""))
    cfg["roberthalf_email"] = os.environ.get("ROBERTHALF_EMAIL", cfg.get("roberthalf_email", ""))
    cfg["roberthalf_password"] = os.environ.get("ROBERTHALF_PASSWORD", cfg.get("roberthalf_password", ""))
    cfg["jobot_email"] = os.environ.get("JOBOT_EMAIL", cfg.get("jobot_email", ""))
    cfg["jobot_password"] = os.environ.get("JOBOT_PASSWORD", cfg.get("jobot_password", ""))
    return cfg


# ── Playwright helpers ────────────────────────────────────────────────────────

async def human_type(page: Page, selector, text: str):
    """Fill the first VISIBLE element matching selector with human-like typing."""
    loc = page.locator(selector) if isinstance(selector, str) else selector
    # Find first visible element — skip hidden ones (modals, off-screen fields, etc.)
    target = None
    count = await loc.count()
    for i in range(min(count, 5)):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                target = el
                break
        except Exception:
            continue
    if target is None:
        return
    await target.click(timeout=5000)
    await target.fill("")
    for ch in text:
        await target.type(ch, delay=random.randint(40, 130))


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


def _find_similar_resume(job: dict) -> tuple[str | None, str | None, list | None]:
    """
    Look for a cached resume from a previous job with ≥50% keyword overlap.
    Returns (resume_text, cover_text, keywords) or (None, None, None).
    """
    if not RESUMES_DIR.exists():
        return None, None, None
    job_kws = {k.lower() for k in (job.get("matched_keywords") or [])}
    if not job_kws:
        return None, None, None

    best_score = 0.0
    best_id: str | None = None
    for meta_file in RESUMES_DIR.glob("*_meta.json"):
        try:
            meta = json.loads(meta_file.read_text())
            cached_kws = {k.lower() for k in (meta.get("keywords") or [])}
            if not cached_kws:
                continue
            overlap = len(job_kws & cached_kws) / max(len(job_kws), len(cached_kws))
            if overlap > best_score:
                best_score = overlap
                best_id = meta.get("job_id")
        except Exception:
            continue

    if best_score >= 0.50 and best_id:
        r = RESUMES_DIR / f"{best_id}_resume.txt"
        c = RESUMES_DIR / f"{best_id}_cover.txt"
        k = RESUMES_DIR / f"{best_id}_keywords.json"
        if r.exists() and c.exists():
            kws = json.loads(k.read_text()) if k.exists() else None
            print(f"  [cache] Reusing similar job resume (keyword overlap: {best_score:.0%})")
            return r.read_text(), c.read_text(), kws

    return None, None, None


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
    if "jobot.com" in host:
        return "jobot"
    if "dice.com" in host:
        return "dice"
    if "workday.com" in host:
        return "workday"
    # Job board sources — need special handling to extract external apply URL
    if "remotive.com" in host:
        return "remotive"
    if "arbeitnow.com" in host:
        return "arbeitnow"
    # Additional ATS platforms
    if "ashbyhq.com" in host:
        return "greenhouse"   # Ashby forms are structurally similar to Greenhouse
    if "workable.com" in host:
        return "workable"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "breezy.hr" in host:
        return "generic"
    if "jazzhr.com" in host or "resumatoradmin.com" in host:
        return "generic"
    if "bamboohr.com" in host:
        return "generic"
    if "recruitee.com" in host:
        return "generic"
    if "icims.com" in host:
        return "generic"
    if "myworkdayjobs.com" in host:
        return "workday"
    if "taleo.net" in host:
        return "generic"
    if "successfactors.com" in host:
        return "generic"
    if "themuse.com" in host:
        return "generic"
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


# ── Remotive ─────────────────────────────────────────────────────────────────

async def apply_remotive(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    """
    Remotive is a job aggregator. The job page has a direct external link to the
    employer's ATS (Lever, Greenhouse, Workable, etc.) as the Apply button.
    Strategy: load the Remotive job page, find the external apply link, navigate
    there, detect the real platform, and fill the form.
    """
    print(f"  [Remotive] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)  # let SPA render

    # Find the external apply URL — Remotive renders it as an <a> with class
    # containing "apply" that points to an external domain.
    apply_href = None
    for sel in [
        "a.apply-button", "a[class*='apply-button']", "a[class*='apply_button']",
        "a[class*='apply-btn']", "a[class*='apply_btn']",
        "a:has-text('Apply Now')", "a:has-text('Apply now')",
        "a:has-text('Apply')",
    ]:
        loc = page.locator(sel)
        try:
            await loc.first.wait_for(state="visible", timeout=3000)
            href = await loc.first.get_attribute("href") or ""
            if href.startswith("http") and "remotive.com" not in href:
                apply_href = href
                break
            elif href.startswith("http"):
                # It's a remotive link — click it and wait for redirect
                await loc.first.click()
                try:
                    await page.wait_for_url(lambda u: u != job["url"], timeout=5000)
                except Exception:
                    pass
                if page.url != job["url"]:
                    # Already on the external ATS — don't re-navigate
                    print(f"  [Remotive] Redirected to: {page.url}")
                    return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
                break
        except Exception:
            continue

    if not apply_href:
        print("  [Remotive] No external link — trying inline form")
        return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)

    print(f"  [Remotive] External apply URL: {apply_href}")
    await page.goto(apply_href, wait_until="domcontentloaded", timeout=30000)
    await nap(2, 3)

    success = await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
    if success:
        print(f"  [Remotive] Submitted!")
    else:
        print(f"  [Remotive] Form found but could not submit")
    return success


# ── Arbeitnow ─────────────────────────────────────────────────────────────────

async def apply_arbeitnow(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    """
    Arbeitnow is a job aggregator. Each job page has an Apply button that links
    directly to the employer's external site or ATS. Extract the external URL,
    navigate there, and fill the application form.
    """
    print(f"  [Arbeitnow] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)  # let SPA render

    apply_href = None
    for sel in [
        "a.apply-btn", "a[class*='apply-btn']", "a[class*='apply_btn']",
        "a[class*='apply-button']",
        "a:has-text('Apply Now')", "a:has-text('Apply now')",
        "a:has-text('Apply for this job')", "a:has-text('Apply')",
    ]:
        loc = page.locator(sel)
        try:
            await loc.first.wait_for(state="visible", timeout=3000)
            href = await loc.first.get_attribute("href") or ""
            if href.startswith("http") and "arbeitnow.com" not in href:
                apply_href = href
                break
            elif href.startswith("http"):
                # Arbeitnow own-domain link — click and wait for redirect
                await loc.first.click()
                try:
                    await page.wait_for_url(lambda u: u != job["url"], timeout=5000)
                except Exception:
                    pass
                if page.url != job["url"]:
                    # Already on the external ATS — don't re-navigate
                    print(f"  [Arbeitnow] Redirected to: {page.url}")
                    return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
                break
        except Exception:
            continue

    if not apply_href:
        print("  [Arbeitnow] No external link — trying inline form")
        return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)

    print(f"  [Arbeitnow] External apply URL: {apply_href}")
    await page.goto(apply_href, wait_until="domcontentloaded", timeout=30000)
    await nap(2, 3)

    success = await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
    if success:
        print(f"  [Arbeitnow] Submitted!")
    else:
        print(f"  [Arbeitnow] Form found but could not submit")
    return success


# ── Generic fallback ──────────────────────────────────────────────────────────

# Every text variation seen across company career pages and job boards
_APPLY_BUTTON_TEXTS = [
    "Apply Now", "Apply now", "Apply For This Job", "Apply for this job",
    "Apply For This Position", "Apply for this position", "Apply For Job",
    "Apply Here", "Apply here", "Apply Online", "Apply Today",
    "Quick Apply", "Easy Apply", "1-Click Apply", "One-Click Apply",
    "Submit Application", "Submit Your Application",
    "Apply", "APPLY",
]

async def _click_apply_button(page: Page) -> bool:
    """
    Try every known apply button pattern. If the button is an external link,
    navigate there directly. Returns True if something was clicked/navigated.
    """
    # 1. Text-based selectors
    for text in _APPLY_BUTTON_TEXTS:
        for tag in ("button", "a"):
            loc = page.locator(f"{tag}:has-text('{text}')")
            try:
                await loc.first.wait_for(state="visible", timeout=2000)
                href = await loc.first.get_attribute("href") if tag == "a" else None
                if href and href.startswith("http"):
                    await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                else:
                    await loc.first.click()
                await nap(2, 3)
                return True
            except Exception:
                continue

    # 2. Common data attributes and class-based selectors
    for sel in [
        "[data-qa='apply-button']", "[data-cy='apply-button']",
        "[data-testid*='apply' i]", "[id*='apply-button' i]",
        "[class*='apply-btn' i]", "[class*='apply_btn' i]",
        "[class*='apply-button' i]",
        "a[href*='/apply']", "a[href*='apply?']",
        "a[href*='jobs.lever.co']", "a[href*='greenhouse.io']",
        "a[href*='ashbyhq.com']", "a[href*='workable.com']",
        "a[href*='smartrecruiters.com']",
        "input[value*='Apply' i]",
    ]:
        loc = page.locator(sel)
        try:
            await loc.first.wait_for(state="visible", timeout=2000)
            href = await loc.first.get_attribute("href")
            if href and href.startswith("http"):
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
            else:
                await loc.first.click()
            await nap(2, 3)
            return True
        except Exception:
            continue

    # 3. Playwright role-based (catches aria-label variations)
    try:
        import re as _re
        btn = page.get_by_role("button", name=_re.compile(r"apply", _re.I))
        await btn.first.wait_for(state="visible", timeout=2000)
        await btn.first.click()
        await nap(2, 3)
        return True
    except Exception:
        pass

    # 4. Last resort: any visible link whose href contains an ATS domain
    try:
        import re as _re
        ats_pattern = _re.compile(
            r"(lever\.co|greenhouse\.io|ashbyhq\.com|workable\.com|"
            r"smartrecruiters\.com|bamboohr\.com|breezy\.hr|recruitee\.com|"
            r"jazzhr\.com|icims\.com|myworkdayjobs\.com|taleo\.net)",
            _re.I
        )
        for a in await page.locator("a[href]").all():
            try:
                if not await a.is_visible():
                    continue
                href = await a.get_attribute("href") or ""
                if ats_pattern.search(href):
                    await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                    await nap(2, 3)
                    return True
            except Exception:
                continue
    except Exception:
        pass

    return False


async def _first_visible(page: Page, selector: str):
    """Return the first visible element for selector, or None."""
    loc = page.locator(selector)
    count = await loc.count()
    for i in range(min(count, 5)):
        el = loc.nth(i)
        try:
            if await el.is_visible():
                return el
        except Exception:
            continue
    return None


async def _fill_generic_form(page: Page, job: dict, cover_letter: str, config: dict,
                              pdf_path, salary_ask: int) -> bool:
    """Fill whatever application form is currently visible and submit it."""
    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

    _name_parts = (config.get("full_name") or "").split()
    for sel, val in [
        ("input[name='name'], input[id='name'], input[placeholder*='full name' i]",
         config.get("full_name", "")),
        ("input[name*='first' i], input[id*='first' i], input[placeholder*='first' i]",
         _name_parts[0] if _name_parts else ""),
        ("input[name*='last' i], input[id*='last' i], input[placeholder*='last' i]",
         _name_parts[-1] if len(_name_parts) > 1 else (_name_parts[0] if _name_parts else "")),
        ("input[type='email'], input[name='email'], input[id*='email' i]",
         config.get("email", "")),
        ("input[type='tel'], input[name*='phone' i], input[id*='phone' i]",
         config.get("phone", "")),
        ("input[placeholder*='linkedin' i], input[name*='linkedin' i]",
         config.get("linkedin_url", "")),
        ("input[placeholder*='website' i], input[name*='website' i], "
         "input[placeholder*='portfolio' i]",
         config.get("portfolio_url", "")),
    ]:
        if not val:
            continue
        el = await _first_visible(page, sel)
        if el is None:
            continue
        try:
            if await el.input_value():
                continue
            await human_type(page, el, val)
        except Exception:
            continue

    # Cover letter / message textarea — first visible one
    for sel in [
        "textarea[name*='cover' i]", "textarea[id*='cover' i]",
        "textarea[placeholder*='cover' i]", "textarea[placeholder*='message' i]",
        "textarea[name*='message' i]", "textarea",
    ]:
        el = await _first_visible(page, sel)
        if el is None:
            continue
        try:
            if not await el.input_value():
                await human_type(page, el, cover_letter[:2000])
                break
        except Exception:
            continue

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    # Radio "Yes" buttons (work auth, etc.)
    for lbl in await page.locator("label:has-text('Yes')").all():
        try:
            if await lbl.is_visible():
                await lbl.click(timeout=800)
        except Exception:
            pass

    await nap()

    for sel in [
        "button[type='submit']:has-text('Submit')",
        "button:has-text('Submit Application')",
        "button:has-text('Submit Your Application')",
        "button:has-text('Send Application')",
        "button:has-text('Complete Application')",
        "button:has-text('Submit')",
        "button:has-text('Send')",
        "input[type='submit']",
        "button[type='submit']",
        "button:has-text('Continue')",
        # Scoped to form/dialog so we don't re-click the page-level Apply button
        "form button:has-text('Apply Now')",
        "form button:has-text('Apply now')",
        "form button:has-text('Apply')",
        "[role='dialog'] button:has-text('Apply Now')",
        "[role='dialog'] button:has-text('Apply')",
    ]:
        if await click_if_visible(page, sel):
            await nap(2, 4)
            return True

    return False


async def _fill_form_at_current_page(
    page: Page, job: dict, cover_letter: str, config: dict,
    pdf_path, salary_ask: int, *, depth: int = 0
) -> bool:
    """
    Fill and submit whatever application form is (or becomes) visible on the
    CURRENT page. Does NOT navigate away via job["url"]. Handles pages that
    themselves have an intermediate Apply button before showing the actual form.
    depth guards against infinite recursion.
    """
    if depth > 2:
        return False

    current_url = page.url
    platform = detect_platform(current_url)

    # Delegate to a known ATS's fill-logic if we recognise it.
    # We replicate just the form-fill portion to avoid re-navigating.
    if platform == "greenhouse":
        return await _fill_greenhouse_form(page, job, cover_letter, config, pdf_path, salary_ask)
    if platform == "lever":
        return await _fill_lever_form(page, job, cover_letter, config, pdf_path, salary_ask)
    if platform == "workday":
        # Workday is complex; fall through to generic
        pass

    # Check if we already see a form
    has_form = await _first_visible(page, "form input[type='email'], form input[type='text']") is not None

    if not has_form:
        # There may be another Apply button on this external landing page
        # (e.g., the ATS shows the job description first, with its own Apply button)
        # Also handles inline/modal forms (URL doesn't change but form appears after click)
        clicked = await _click_apply_button(page)
        if clicked:
            # Wait for navigation or modal DOM injection — whichever comes first
            try:
                await page.wait_for_url(lambda u: u != current_url, timeout=5000)
            except Exception:
                pass  # URL unchanged — assume a modal appeared
            if page.url != current_url:
                return await _fill_form_at_current_page(
                    page, job, cover_letter, config, pdf_path, salary_ask, depth=depth + 1
                )
            # Modal path — re-detect platform (unlikely to change, but cheap check)
            platform = detect_platform(page.url)
            if platform == "greenhouse":
                return await _fill_greenhouse_form(page, job, cover_letter, config, pdf_path, salary_ask)
            if platform == "lever":
                return await _fill_lever_form(page, job, cover_letter, config, pdf_path, salary_ask)
            # Only proceed if a form is now visible
            has_form_now = await _first_visible(
                page, "form input[type='email'], form input[type='text'], input[type='email']"
            ) is not None
            if not has_form_now:
                return False

    return await _fill_generic_form(page, job, cover_letter, config, pdf_path, salary_ask)


async def _fill_greenhouse_form(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path, salary_ask: int
) -> bool:
    """Fill a Greenhouse application form on the current page (no re-navigation)."""
    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

    for sel, val in [
        ("#first_name", config.get("full_name", "").split()[0] if config.get("full_name") else ""),
        ("#last_name", config.get("full_name", "").split()[-1] if config.get("full_name") else ""),
        ("#email", config.get("email", "")),
        ("#phone", config.get("phone", "")),
    ]:
        if not val:
            continue
        fld = page.locator(sel)
        if await fld.count() and not (await fld.first.input_value()):
            await human_type(page, fld, val)

    fld = page.locator("input[placeholder*='linkedin' i], input[id*='linkedin' i]")
    if await fld.count():
        await human_type(page, fld, config.get("linkedin_url", ""))

    fld = page.locator("textarea")
    if await fld.count() and not (await fld.first.input_value()):
        await human_type(page, fld, cover_letter)

    if salary_ask:
        await fill_salary_fields(page, salary_ask)

    await nap()
    if await click_if_visible(page, "input#submit_app, button:has-text('Submit Application')"):
        await nap(2, 4)
        print("  [Greenhouse/inline] Submitted!")
        return True
    return False


async def _fill_lever_form(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path, salary_ask: int
) -> bool:
    """Fill a Lever application form on the current page (no re-navigation)."""
    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

    for sel, val in [
        ("input[name='name']", config.get("full_name", "")),
        ("input[name='email']", config.get("email", "")),
        ("input[name='phone']", config.get("phone", "")),
        ("input[name='urls[LinkedIn]']", config.get("linkedin_url", "")),
    ]:
        if not val:
            continue
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
        print("  [Lever/inline] Submitted!")
        return True
    return False


async def apply_generic(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [Generic] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    # Check if the page already IS an application form (visible fields only)
    has_form = await _first_visible(page, "form input[type='email'], form input[type='text']") is not None

    if not has_form:
        start_url = page.url
        clicked = await _click_apply_button(page)
        if not clicked:
            print("  [Generic] No apply button or form found")
            return False

        # After the click/navigation, handle the destination intelligently.
        # IMPORTANT: do NOT call a full handler (which would re-navigate to job["url"]).
        # Instead use _fill_form_at_current_page which works on whatever page we
        # landed on after clicking Apply.
        new_url = page.url
        if new_url != start_url:
            print(f"  [Generic] Redirected to: {new_url}")
            success = await _fill_form_at_current_page(
                page, job, cover_letter, config, pdf_path, salary_ask
            )
            if success:
                print(f"  [Generic] Submitted (via redirect)!")
            else:
                print(f"  [Generic] Form found but could not submit")
            return success

    success = await _fill_generic_form(page, job, cover_letter, config, pdf_path, salary_ask)
    if success:
        print(f"  [Generic] Submitted!")
    else:
        print(f"  [Generic] Form found but could not submit")
    return success


async def _cloudflare_blocked(page) -> bool:
    """Return True if Cloudflare has intercepted the page with a challenge."""
    title = (await page.title()).lower()
    if "just a moment" in title or "attention required" in title:
        return True
    # Check for cf-challenge form or Turnstile widget
    for sel in ["#cf-challenge-running", ".cf-turnstile", "#challenge-form"]:
        if await page.locator(sel).count():
            return True
    return False


# ── ZipRecruiter ─────────────────────────────────────────────────────────────

async def apply_ziprecruiter(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    print(f"  [ZipRecruiter] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    if await _cloudflare_blocked(page):
        print("  [ZipRecruiter] Cloudflare bot protection detected — skipping (cannot automate)")
        return False

    # ZipRecruiter uses "Apply Now" or "Quick Apply"
    # Prefer 1-Click Apply (logged-in, pre-saved profile — no form to fill)
    one_click = await _first_visible(page, "button.quick_apply_btn[data-quickApply='one_click']")
    if one_click:
        await one_click.click()
        await nap(2, 3)
        # Confirm submission page
        if "ziprecruiter.com" in page.url:
            print("  [ZipRecruiter] 1-Click applied!")
            return True

    # Standard Apply Now / Quick Apply button
    if not await click_if_visible(
        page,
        "button:has-text('Apply Now'), a:has-text('Apply Now'), "
        "button:has-text('Quick Apply'), a:has-text('Quick Apply')",
    ):
        print("  [ZipRecruiter] No apply button — trying inline form")
        return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)

    await nap(2, 3)

    # ZipRecruiter may open a modal or redirect to employer ATS; detect which
    current_url = page.url
    if "ziprecruiter.com" not in current_url:
        platform = detect_platform(current_url)
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        return await handler(page, job, cover_letter, config, pdf_path, salary_ask)

    if pdf_path:
        await upload_resume_if_possible(page, pdf_path)

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
    await nap(3, 5)  # Salesforce/Angular SPA needs extra render time

    start_url = page.url

    # Click Apply — may redirect to online.roberthalf.com portal or external ATS
    clicked = await click_if_visible(
        page,
        "a:has-text('Apply Now'), button:has-text('Apply Now'), "
        "a:has-text('Apply'), button:has-text('Apply')",
    )
    if not clicked:
        print("  [Robert Half] No apply button found")
        return False

    # Wait for navigation (Salesforce portal or external ATS)
    try:
        await page.wait_for_url(lambda u: u != start_url, timeout=8000)
    except Exception:
        pass

    current_url = page.url
    print(f"  [Robert Half] Post-click URL: {current_url}")

    # If redirected to an external ATS, hand off
    if "roberthalf.com" not in current_url:
        platform = detect_platform(current_url)
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        return await handler(page, job, cover_letter, config, pdf_path, salary_ask)

    # Still on Robert Half — use generic form filler
    # (logged-in users see a one-tap confirm; logged-out see a form)
    await nap(2, 3)
    success = await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
    if success:
        print("  [Robert Half] Submitted!")
    else:
        print("  [Robert Half] Could not submit form")
    return success


# ── Jobot ────────────────────────────────────────────────────────────────────

async def apply_jobot(
    page: Page, job: dict, cover_letter: str, config: dict, pdf_path: Path | None = None, salary_ask: int = 0
) -> bool:
    """
    Jobot logged-in Easy Apply flow:
      1. Click Easy Apply → application submitted immediately with saved profile
      2. Elevator Pitch step appears — fill textarea with cover letter + click Submit
      3. "Application Received" confirmation

    External ATS redirects are delegated to the appropriate handler.
    """
    print(f"  [Jobot] {job['title']} @ {job['company']}")
    await page.goto(job["url"], wait_until="domcontentloaded", timeout=30000)
    await nap(2, 4)

    # If this is a search-results URL with &j=<id>, the job detail panel loads
    # on the right side — give it a moment to render
    if "jobot.com/search" in page.url and "&j=" in page.url:
        await nap(1, 2)

    apply_clicked = await click_if_visible(
        page,
        "button:has-text('Easy Apply'), a:has-text('Easy Apply'), "
        "button:has-text('Apply Now'), a:has-text('Apply Now'), "
        "button:has-text('Apply'), a:has-text('Apply')",
    )
    if not apply_clicked:
        print("  [Jobot] No apply button — trying inline form")
        return await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)

    await nap(2, 3)

    # Check if redirected to an external ATS
    current_url = page.url
    if "jobot.com" not in current_url:
        platform = detect_platform(current_url)
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        return await handler(page, job, cover_letter, config, pdf_path, salary_ask)

    # ── Elevator Pitch step ───────────────────────────────────────────────────
    # After Easy Apply the application is received; Jobot then asks for an
    # Elevator Pitch (textarea + "Submit Elevator Pitch" button).
    # We use the cover letter as the pitch — it's already job-tailored.
    try:
        await page.wait_for_selector(
            "button:has-text('Submit Elevator Pitch')", timeout=6000
        )
        print("  [Jobot] Elevator Pitch step detected — filling…")

        # Fill the pitch textarea
        pitch_area = page.locator("textarea").first
        if await pitch_area.count():
            await pitch_area.click()
            # Condense cover letter to ≤1000 chars — Jobot wants a concise pitch
            pitch_text = cover_letter[:1000]
            await pitch_area.fill(pitch_text)
            await nap(1, 2)

        await page.locator("button:has-text('Submit Elevator Pitch')").first.click()
        await nap(2, 3)

        # Confirm final "Application Received" state
        if await page.locator(
            ":has-text('Application Received'), :has-text('application received')"
        ).count():
            print("  [Jobot] Application Received — fully submitted!")
            return True

        print("  [Jobot] Elevator Pitch submitted — assuming success")
        return True

    except Exception:
        pass  # no elevator pitch step — check for simpler confirmation below

    # ── Simple confirmation panel (profile pre-filled, just confirm) ──────────
    for confirm_sel in [
        "button:has-text('Submit Application')",
        "button:has-text('Confirm Application')",
        "button:has-text('Confirm Apply')",
        "button:has-text('Confirm')",
        "[role='dialog'] button:has-text('Apply')",
        "[class*='modal'] button:has-text('Apply')",
        "[class*='panel'] button:has-text('Apply')",
        "[class*='drawer'] button:has-text('Apply')",
        "[class*='apply-panel'] button",
    ]:
        loc = page.locator(confirm_sel)
        try:
            await loc.first.wait_for(state="visible", timeout=2000)
            await loc.first.click()
            await nap(2, 3)
            for ok_sel in [
                ":has-text('Application Received')", ":has-text('Application Submitted')",
                ":has-text('Successfully Applied')", ":has-text('Applied!')",
                ":has-text('Thank you')",
            ]:
                if await page.locator(ok_sel).count():
                    print("  [Jobot] Application submitted!")
                    return True
            print("  [Jobot] Confirmation clicked — assuming submitted")
            return True
        except Exception:
            continue

    # Fallback to generic form filling
    success = await _fill_form_at_current_page(page, job, cover_letter, config, pdf_path, salary_ask)
    if success:
        print("  [Jobot] Submitted!")
    else:
        print("  [Jobot] Form found but could not submit")
    return success


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
    "jobot": apply_jobot,
    "dice": apply_dice,
    "remotive": apply_remotive,
    "arbeitnow": apply_arbeitnow,
    "generic": apply_generic,
}


# ── Main loop ─────────────────────────────────────────────────────────────────

async def run(max_apply: int = 5):
    config = load_config()
    jobs: list[dict] = _load_json(JOBS_PATH, [])
    applied: list[dict] = _load_json(APPLIED_PATH, [])
    applied_ids = {a["id"] for a in applied if a.get("status") == "applied"}

    MAX_RETRIES = 3
    queue = [
        j for j in jobs
        if (
            j.get("status") == "approved"
            or (
                j.get("status") in ("error", "failed")
                and j.get("retry_count", 0) < MAX_RETRIES
            )
        )
        and j["id"] not in applied_ids
    ][:max_apply]

    if not queue:
        print("No eligible jobs found (approved or pending retry). Run the scanner first.")
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

        # ZipRecruiter login — enables 1-Click / Quick Apply
        # Note: ZipRecruiter uses Cloudflare bot protection; login may be blocked
        needs_zr = any(detect_platform(j["url"]) == "ziprecruiter" for j in queue)
        if needs_zr and config.get("ziprecruiter_email") and config.get("ziprecruiter_password"):
            print("Logging into ZipRecruiter…")
            await page.goto("https://www.ziprecruiter.com/authn/login", wait_until="domcontentloaded")
            await nap(1, 2)
            if await _cloudflare_blocked(page):
                print("  [ZipRecruiter] Cloudflare challenge on login page — skipping ZR login")
            else:
                el = await _first_visible(page, "input[type='email'], input[name='email']")
                if el:
                    await human_type(page, el, config["ziprecruiter_email"])
                el = await _first_visible(page, "input[type='password'], input[name='password']")
                if el:
                    await human_type(page, el, config["ziprecruiter_password"])
                await click_if_visible(page, "button[type='submit'], form button:has-text('Log in'), form button:has-text('Sign in')")
                await nap(4, 6)

        # Robert Half login — Salesforce Experience Cloud portal (heavy SPA)
        needs_rh = any(detect_platform(j["url"]) == "roberthalf" for j in queue)
        if needs_rh and config.get("roberthalf_email") and config.get("roberthalf_password"):
            print("Logging into Robert Half…")
            await page.goto("https://online.roberthalf.com/s/login", wait_until="domcontentloaded")
            await nap(3, 4)  # Salesforce Lightning SPA needs extra time
            # Salesforce uses name="username" not name="email"
            el = await _first_visible(
                page,
                "input[name='username'], input[type='email'], input[name='email']"
            )
            if el:
                await human_type(page, el, config["roberthalf_email"])
            else:
                print("  [RobertHalf] Could not find email/username field — skipping login")
            el = await _first_visible(page, "input[type='password'], input[name='password']")
            if el:
                await human_type(page, el, config["roberthalf_password"])
            await click_if_visible(page, "button[type='submit'], input[type='submit']")
            await nap(4, 6)

        # Jobot login — two-step: email page → submit → password page → submit
        needs_jobot = any(detect_platform(j["url"]) == "jobot" for j in queue)
        if needs_jobot and config.get("jobot_email") and config.get("jobot_password"):
            print("Logging into Jobot…")
            await page.goto("https://jobot.com/login/email-sign-in", wait_until="domcontentloaded")

            # Step 1: email
            try:
                print("  [Jobot] Waiting for email field…")
                await page.wait_for_selector("input[type='email']", timeout=8000)
                await page.locator("input[type='email']").first.click()
                await page.locator("input[type='email']").first.type(config["jobot_email"], delay=30)
                print(f"  [Jobot] Email typed — clicking submit…")
                # Click the submit button directly; fall back to Enter
                submitted = False
                for sel in ["button[type='submit']", "button:has-text('Sign in')",
                             "button:has-text('Sign In')", "button:has-text('Continue')"]:
                    try:
                        await page.locator(sel).first.click(timeout=2000)
                        submitted = True
                        print(f"  [Jobot] Clicked: {sel}")
                        break
                    except Exception:
                        continue
                if not submitted:
                    await page.keyboard.press("Enter")
                    print("  [Jobot] Pressed Enter to submit email")
            except Exception as e:
                print(f"  [Jobot] Email step failed: {e}")

            # Step 2: password page
            try:
                print("  [Jobot] Waiting for password field…")
                await page.wait_for_selector("input[type='password']", timeout=10000)
                print("  [Jobot] Password field found — filling…")
                await page.locator("input[type='password']").first.click()
                await page.locator("input[type='password']").first.type(config["jobot_password"], delay=30)
                val = await page.locator("input[type='password']").first.input_value()
                print(f"  [Jobot] Password field length after type: {len(val)}")
                print("  [Jobot] Clicking Sign In…")
                submitted = False
                for sel in ["button[type='submit']", "button:has-text('Sign In')",
                             "button:has-text('Sign in')", "button:has-text('Log in')"]:
                    try:
                        await page.locator(sel).first.click(timeout=2000)
                        submitted = True
                        print(f"  [Jobot] Clicked: {sel}")
                        break
                    except Exception:
                        continue
                if not submitted:
                    await page.keyboard.press("Enter")
                    print("  [Jobot] Pressed Enter to submit password")
                await nap(4, 6)
                print(f"  [Jobot] Post-login URL: {page.url}")
            except Exception as e:
                print(f"  [Jobot] Password step failed: {e}")

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
                desc = job.get("description", "")
                RESUMES_DIR.mkdir(parents=True, exist_ok=True)
                cache_txt  = RESUMES_DIR / f"{job['id']}_resume.txt"
                cache_pdf  = RESUMES_DIR / f"{job['id']}_resume.pdf"
                cache_covr = RESUMES_DIR / f"{job['id']}_cover.txt"
                cache_kws  = RESUMES_DIR / f"{job['id']}_keywords.json"
                cache_meta = RESUMES_DIR / f"{job['id']}_meta.json"

                if cache_txt.exists() and cache_covr.exists():
                    # Exact cache hit — reuse text + cover
                    print(f"\n[cache] Exact cache hit for {job['title']} @ {job['company']}")
                    visible_text = cache_txt.read_text()
                    cover        = cache_covr.read_text()
                    cached_kws   = json.loads(cache_kws.read_text()) if cache_kws.exists() else None
                    if cache_pdf.exists():
                        pdf_path = cache_pdf
                    else:
                        # Rebuild PDF from cached text — Playwright only, no API call
                        print(f"  [cache] Rebuilding PDF from cached text…")
                        pdf_path, _ = build_resume_pdf(
                            job["title"], desc, job["company"],
                            visible_resume=visible_text,
                            output_dir=RESUMES_DIR,
                            cached_keywords=cached_kws,
                        )
                else:
                    # Check for a similar job's cached resume (≥50% keyword overlap)
                    sim_text, sim_cover, sim_kws = _find_similar_resume(job)

                    if sim_text:
                        # Similar cache hit — reuse text/cover, regenerate PDF for this company
                        visible_text = sim_text
                        cover        = sim_cover
                        pdf_path, keywords = build_resume_pdf(
                            job["title"], desc, job["company"],
                            visible_resume=visible_text,
                            output_dir=RESUMES_DIR,
                            cached_keywords=sim_kws,
                        )
                    else:
                        # No cache — call Claude API for everything
                        print(f"\nTailoring resume for: {job['title']} @ {job['company']}")
                        visible_text = tailor_resume(job["title"], desc, job["company"])
                        cover        = generate_cover_letter(job["title"], desc, job["company"])
                        pdf_path, keywords = build_resume_pdf(
                            job["title"], desc, job["company"],
                            visible_resume=visible_text,
                            output_dir=RESUMES_DIR,
                        )

                    # Persist all cache files for this job
                    cache_txt.write_text(visible_text)
                    cache_covr.write_text(cover)
                    cache_kws.write_text(json.dumps(keywords, indent=2))
                    cache_meta.write_text(json.dumps({
                        "job_id":   job["id"],
                        "title":    job["title"],
                        "company":  job["company"],
                        "score":    job.get("score", 0),
                        "keywords": keywords,
                        "matched_keywords": job.get("matched_keywords", []),
                    }, indent=2))
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
