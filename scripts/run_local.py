#!/usr/bin/env python3
"""
Local headed test runner.

Loads credentials from .env, forces headless=False + slow_mo so you can
watch every Playwright action in a real browser window and report issues.

Usage:
  python scripts/run_local.py                  # apply to up to 3 approved jobs
  python scripts/run_local.py 1                # apply to 1 job
  python scripts/run_local.py --url URL        # test a specific job URL
  python scripts/run_local.py --platform zr    # test ZipRecruiter login only
  python scripts/run_local.py --platform rh    # test RobertHalf login only
  python scripts/run_local.py --platform jobot # test Jobot login only

Requires: pip install python-dotenv playwright
          playwright install chromium
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

# Load .env before importing apply (apply reads env at import time via load_config)
def _load_dotenv():
    env_path = ROOT / ".env"
    if not env_path.exists():
        print(f"[warn] No .env file found at {env_path}")
        print(f"       Copy .env.example → .env and fill in your credentials")
        return
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())
    print(f"[.env] Loaded credentials from {env_path}")

_load_dotenv()

import apply as _apply_module
from apply import (
    load_config, detect_platform, PLATFORM_HANDLERS,
    apply_generic, _fill_form_at_current_page,
    nap, _first_visible, click_if_visible, human_type,
    _load_json, JOBS_PATH, APPLIED_PATH,
)
from playwright.async_api import async_playwright

SLOW_MO = 200  # ms between each Playwright action — set to 0 to speed up


async def _launch(headless=False):
    config = load_config()
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=headless,
        slow_mo=SLOW_MO,
        args=["--no-sandbox", "--disable-dev-shm-usage"],
    )
    ctx = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        viewport={"width": 1440, "height": 900},
        ignore_https_errors=True,
    )
    page = await ctx.new_page()
    return pw, browser, page, config


async def test_login(platform: str):
    """Log in to a single platform and hold the browser open so you can inspect."""
    pw, browser, page, config = await _launch()
    print(f"\n{'='*60}")
    print(f"Testing login: {platform}")
    print(f"{'='*60}")
    print("Browser will stay open — press Ctrl+C to close.\n")

    try:
        if platform in ("zr", "ziprecruiter"):
            if not config.get("ziprecruiter_email"):
                print("[!] ZIPRECRUITER_EMAIL not set in .env")
                return
            print(f"  Navigating to ZipRecruiter login…")
            await page.goto("https://www.ziprecruiter.com/authn/login", wait_until="domcontentloaded")
            await nap(1, 2)
            el = await _first_visible(page, "input[type='email'], input[name='email']")
            if el:
                await human_type(page, el, config["ziprecruiter_email"])
            el = await _first_visible(page, "input[type='password'], input[name='password']")
            if el:
                await human_type(page, el, config["ziprecruiter_password"])
            print("  Fields filled — waiting 5s before clicking submit (watch the browser)")
            await asyncio.sleep(5)
            await click_if_visible(page, "button[type='submit'], form button:has-text('Log in'), form button:has-text('Sign in')")
            await nap(4, 5)
            print(f"  Post-login URL: {page.url}")

        elif platform in ("rh", "roberthalf"):
            if not config.get("roberthalf_email"):
                print("[!] ROBERTHALF_EMAIL not set in .env")
                return
            print(f"  Navigating to Robert Half login…")
            await page.goto("https://online.roberthalf.com/s/login", wait_until="domcontentloaded")
            await nap(1, 2)
            el = await _first_visible(page, "input[type='email'], input[name='email']")
            if el:
                await human_type(page, el, config["roberthalf_email"])
            el = await _first_visible(page, "input[type='password'], input[name='password']")
            if el:
                await human_type(page, el, config["roberthalf_password"])
            print("  Fields filled — waiting 5s before clicking submit")
            await asyncio.sleep(5)
            await click_if_visible(page, "button[type='submit']")
            await nap(4, 5)
            print(f"  Post-login URL: {page.url}")

        elif platform in ("jobot",):
            if not config.get("jobot_email"):
                print("[!] JOBOT_EMAIL not set in .env")
                return
            print("  Navigating to Jobot login (step 1: email)…")
            await page.goto("https://jobot.com/login/email-sign-in", wait_until="domcontentloaded")
            await page.wait_for_selector("input[type='email']", timeout=8000)
            await page.locator("input[type='email']").first.click()
            await page.locator("input[type='email']").first.type(config["jobot_email"], delay=30)
            print(f"  Email typed: {config['jobot_email']}")
            # Submit email form
            submitted = False
            for sel in ["button[type='submit']", "button:has-text('Sign in')",
                        "button:has-text('Sign In')", "button:has-text('Continue')"]:
                try:
                    await page.locator(sel).first.click(timeout=2000)
                    print(f"  Clicked: {sel}")
                    submitted = True
                    break
                except Exception:
                    continue
            if not submitted:
                await page.keyboard.press("Enter")
                print("  Pressed Enter to submit email")

            # Step 2: password page
            print("  Waiting for password page…")
            await page.wait_for_selector("input[type='password']", timeout=10000)
            print(f"  Password page URL: {page.url}")
            await page.locator("input[type='password']").first.click()
            await page.locator("input[type='password']").first.type(config["jobot_password"], delay=30)
            val = await page.locator("input[type='password']").first.input_value()
            print(f"  Password field length after type: {len(val)}")
            submitted = False
            for sel in ["button[type='submit']", "button:has-text('Sign In')",
                        "button:has-text('Sign in')", "button:has-text('Log in')"]:
                try:
                    await page.locator(sel).first.click(timeout=2000)
                    print(f"  Clicked: {sel}")
                    submitted = True
                    break
                except Exception:
                    continue
            if not submitted:
                await page.keyboard.press("Enter")
                print("  Pressed Enter to submit password")
            await nap(4, 5)
            print(f"  Post-login URL: {page.url}")

        else:
            print(f"[!] Unknown platform: {platform}. Use: zr, rh, jobot")
            return

        print("\n[hold] Browser open — press Ctrl+C when done inspecting")
        await asyncio.Event().wait()

    except KeyboardInterrupt:
        pass
    finally:
        await browser.close()
        await pw.stop()


async def test_url(url: str):
    """Navigate to a URL, attempt to apply, and show every step."""
    pw, browser, page, config = await _launch()
    print(f"\n{'='*60}")
    print(f"Testing apply at: {url}")
    print(f"{'='*60}\n")

    try:
        platform = detect_platform(url)
        print(f"  Detected platform: {platform}")
        cover = "This is a test cover letter for local debugging."
        job = {
            "id": "local_test",
            "title": "Test Job",
            "company": "Test Company",
            "description": "Test description",
            "url": url,
        }
        handler = PLATFORM_HANDLERS.get(platform, apply_generic)
        result = await handler(page, job, cover, config, None, 0)
        print(f"\n  Handler returned: {'SUCCESS' if result else 'FAILED'}")
        print("\n[hold] Browser open — press Ctrl+C when done")
        await asyncio.Event().wait()

    except KeyboardInterrupt:
        pass
    finally:
        await browser.close()
        await pw.stop()


async def run_jobs(max_apply: int):
    """Run the real apply loop with a headed visible browser."""
    print(f"\n{'='*60}")
    print(f"Running apply loop (headed) — max {max_apply} job(s)")
    print(f"{'='*60}\n")

    # Temporarily force headless=False in config
    original_run = _apply_module.run

    async def headed_run(n):
        # Monkey-patch slow_mo into the playwright launch inside run()
        orig_launch = None
        import playwright.async_api as _pw_api
        orig_chromium_launch = None

        # Patch config to force headless=False
        cfg_backup = None
        import apply as _a
        orig_load = _a.load_config
        def patched_load():
            cfg = orig_load()
            cfg["headless"] = False
            return cfg
        _a.load_config = patched_load

        try:
            await _a.run(n)
        finally:
            _a.load_config = orig_load

    await headed_run(max_apply)


def main():
    parser = argparse.ArgumentParser(description="Local headed Playwright test runner")
    parser.add_argument("max_apply", nargs="?", type=int, default=3,
                        help="Max jobs to apply to (default: 3)")
    parser.add_argument("--url", help="Test a specific job URL")
    parser.add_argument("--platform", help="Test login for a platform: zr | rh | jobot")
    args = parser.parse_args()

    if args.platform:
        asyncio.run(test_login(args.platform))
    elif args.url:
        asyncio.run(test_url(args.url))
    else:
        asyncio.run(run_jobs(args.max_apply))


if __name__ == "__main__":
    main()
