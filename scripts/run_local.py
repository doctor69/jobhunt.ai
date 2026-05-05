#!/usr/bin/env python3
from __future__ import annotations
"""
Local headed test runner.

Loads credentials from .env, forces headless=False + slow_mo so you can
watch every Playwright action in a real browser window and report issues.

Usage:
  python scripts/run_local.py                    # apply to up to 3 approved jobs
  python scripts/run_local.py 1                  # apply to 1 job
  python scripts/run_local.py --url URL          # test a specific job URL
  python scripts/run_local.py --platform zr      # test ZipRecruiter login only
  python scripts/run_local.py --platform rh      # test RobertHalf login only
  python scripts/run_local.py --platform jobot   # test Jobot login only
  python scripts/run_local.py --scan jobot       # scan Jobot jobs (headed, login visible)
  python scripts/run_local.py --jobot-full       # scan + apply first matching Jobot job

Requires: pip install python-dotenv playwright
          playwright install chromium
"""

import argparse
import asyncio
import imaplib
import email as _email_lib
import re
import os
import sys
import time
from email.utils import parsedate_to_datetime
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


# ── Outlook IMAP 2FA helper ───────────────────────────────────────────────────

def _extract_email_body(msg) -> str:
    """Return plain-text + HTML content from an email.Message, decoded."""
    body = ""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() in ("text/plain", "text/html"):
                try:
                    body += part.get_payload(decode=True).decode("utf-8", errors="ignore")
                except Exception:
                    pass
    else:
        try:
            body = msg.get_payload(decode=True).decode("utf-8", errors="ignore")
        except Exception:
            pass
    return body


def fetch_rh_verification_code(
    imap_user: str,
    imap_password: str,
    min_timestamp: float,
    timeout: int = 60,
) -> str | None:
    """Poll Outlook IMAP for a Robert Half 2FA code sent after min_timestamp.

    Connects to outlook.office365.com:993, searches for emails from roberthalf,
    filters by recency, and extracts the first 6-digit numeric code found.
    Returns the code string or None if the timeout elapses.

    Credentials come from ROBERTHALF_EMAIL + OUTLOOK_APP_PASSWORD env vars.
    Generate an app password at: https://account.live.com/proofs/manage/additional
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            mail = imaplib.IMAP4_SSL("outlook.office365.com", 993)
            mail.login(imap_user, imap_password)
            mail.select("INBOX")
            _, msg_ids = mail.search(None, 'FROM "roberthalf"')
            if msg_ids and msg_ids[0]:
                for msg_id in reversed(msg_ids[0].split()):
                    _, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if not msg_data or not msg_data[0]:
                        continue
                    raw = msg_data[0][1]
                    msg = _email_lib.message_from_bytes(raw)
                    # Skip emails that predate this login attempt (30 s tolerance).
                    try:
                        msg_ts = parsedate_to_datetime(msg.get("Date", "")).timestamp()
                        if msg_ts < min_timestamp - 30:
                            continue
                    except Exception:
                        pass
                    match = re.search(r'\b(\d{6})\b', _extract_email_body(msg))
                    if match:
                        mail.logout()
                        return match.group(1)
            mail.logout()
        except Exception as e:
            print(f"  [imap] {e}", file=sys.stderr)
        remaining = deadline - time.time()
        if remaining > 0:
            time.sleep(min(5, remaining))
    return None


# ─────────────────────────────────────────────────────────────────────────────

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
            await nap(3, 4)  # Salesforce SPA needs extra render time

            # Dismiss acknowledgment / cookie / terms dialog.
            # RH's Salesforce SPA renders these after the initial DOM load, so we
            # wait up to 6 s for any known consent button, then force-click it
            # (the button element may not be "visible" per Playwright's strict check).
            ACK_SELECTORS = [
                "button:has-text('I Understand')",
                "button:has-text('I understand')",
                "button:has-text('I Accept')",
                "button:has-text('I Agree')",
                "button:has-text('Agree')",
                "button:has-text('Accept All')",
                "button:has-text('Accept')",
                "button:has-text('Got it')",
                "button:has-text('OK')",
                "button:has-text('Continue')",
                "button:has-text('Acknowledge')",
                "[class*='acknowledge'] button",
                "[id*='acknowledge'] button",
                "[class*='consent'] button",
                "[id*='consent'] button",
                "[class*='cookie'] button",
            ]
            combined_ack = ", ".join(ACK_SELECTORS)
            try:
                await page.wait_for_selector(combined_ack, timeout=6000, state="attached")
                for ack in ACK_SELECTORS:
                    try:
                        loc = page.locator(ack)
                        if await loc.count():
                            await loc.first.click(force=True, timeout=3000)
                            print(f"  Dismissed acknowledgment: {ack}")
                            await nap(1, 2)
                            break
                    except Exception:
                        continue
            except Exception:
                print("  No acknowledgment dialog detected — proceeding.")

            # After dismissing the ACK dialog the SPA does a route transition —
            # wait for it to settle before touching credentials.
            try:
                await page.wait_for_load_state("domcontentloaded", timeout=10000)
            except Exception:
                pass
            await nap(2, 3)

            EMAIL_SEL = "input[name='username'], input[type='email'], input[name='email']"
            PASS_SEL  = "input[type='password'], input[name='password']"

            try:
                # Wait for the email input to exist in the DOM (MDC keeps it hidden).
                await page.wait_for_selector(EMAIL_SEL, timeout=10000, state="attached")
                print(f"  Email field found in DOM")

                # Activate the MDC text-field wrapper with a real click so its
                # internal state machine opens, then type via keyboard so events fire.
                try:
                    await page.locator(".mdc-text-field").first.click(timeout=3000)
                except Exception:
                    await page.locator(EMAIL_SEL).first.click(force=True)
                await page.keyboard.type(config["roberthalf_email"], delay=50)
                print(f"  Email typed")
                await nap(0.5, 1)

                # Password field
                try:
                    await page.locator(".mdc-text-field").nth(1).click(timeout=3000)
                except Exception:
                    await page.locator(PASS_SEL).first.click(force=True)
                await page.keyboard.type(config["roberthalf_password"], delay=50)
                print(f"  Password typed — clicking submit")
                await nap(0.5, 1)

                # Submit — record time for 2FA email recency check.
                submit_time = time.time()
                submit = page.locator("button[type='submit'], input[type='submit']")
                try:
                    await submit.first.click(timeout=5000)
                except Exception:
                    await submit.first.click(force=True)

                await nap(4, 5)
                print(f"  Post-submit URL: {page.url}")

                # ── 2FA / verification code ───────────────────────────────────
                MFA_SEL = (
                    "input[name*='code' i], input[name*='otp' i], "
                    "input[name*='token' i], input[name*='verify' i], "
                    "input[name*='mfa' i], input[placeholder*='code' i], "
                    "input[placeholder*='verification' i], "
                    "input[type='number'][maxlength='6'], "
                    "input[type='text'][maxlength='6']"
                )
                try:
                    await page.wait_for_selector(MFA_SEL, timeout=8000, state="attached")
                    print("  2FA screen detected — fetching code from Outlook IMAP…")
                    imap_pass = os.environ.get("OUTLOOK_APP_PASSWORD", "")
                    if not imap_pass:
                        print("  [!] OUTLOOK_APP_PASSWORD not set — cannot complete 2FA")
                    else:
                        code = await asyncio.to_thread(
                            fetch_rh_verification_code,
                            config["roberthalf_email"], imap_pass, submit_time,
                        )
                        if code:
                            print(f"  Verification code received: {code}")
                            try:
                                await page.locator(MFA_SEL).first.click(force=True)
                            except Exception:
                                pass
                            await page.keyboard.type(code, delay=100)
                            await nap(0.5, 1)
                            submitted_mfa = False
                            for sel in [
                                "button[type='submit']", "input[type='submit']",
                                "button:has-text('Verify')", "button:has-text('Submit')",
                                "button:has-text('Continue')", "button:has-text('Confirm')",
                            ]:
                                try:
                                    await page.locator(sel).first.click(timeout=3000)
                                    submitted_mfa = True
                                    break
                                except Exception:
                                    continue
                            if not submitted_mfa:
                                await page.keyboard.press("Enter")
                            await nap(3, 4)
                            print(f"  Post-2FA URL: {page.url}")
                        else:
                            print("  [!] Timed out — no verification code received within 60 s")
                except Exception:
                    print(f"  No 2FA screen detected — treating as successful login")

            except Exception as e:
                print(f"  [!] Login failed: {e}")

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

        # For Jobot jobs, log in first so Easy Apply confirmation is available
        if platform == "jobot" and config.get("jobot_email") and config.get("jobot_password"):
            print("  Logging into Jobot before apply test…")
            await _jobot_login(page, config)

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


async def _jobot_login(page, config):
    """Two-step Jobot login — same flow confirmed working in testing."""
    try:
        await page.goto("https://jobot.com/login/email-sign-in", wait_until="domcontentloaded")
        await page.wait_for_selector("input[type='email']", timeout=8000)
        await page.locator("input[type='email']").first.click()
        await page.locator("input[type='email']").first.type(config["jobot_email"], delay=30)
        submitted = False
        for sel in ["button[type='submit']", "button:has-text('Sign in')",
                    "button:has-text('Sign In')", "button:has-text('Continue')"]:
            try:
                await page.locator(sel).first.click(timeout=2000)
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            await page.keyboard.press("Enter")

        await page.wait_for_selector("input[type='password']", timeout=10000)
        await page.locator("input[type='password']").first.click()
        await page.locator("input[type='password']").first.type(config["jobot_password"], delay=30)
        submitted = False
        for sel in ["button[type='submit']", "button:has-text('Sign In')",
                    "button:has-text('Sign in')", "button:has-text('Log in')"]:
            try:
                await page.locator(sel).first.click(timeout=2000)
                submitted = True
                break
            except Exception:
                continue
        if not submitted:
            await page.keyboard.press("Enter")

        await asyncio.sleep(4)
        print(f"  [Jobot] Logged in — URL: {page.url}")
    except Exception as e:
        print(f"  [Jobot] Login failed: {e}")


async def test_jobot_scan():
    """
    Run the Jobot scanner with a headed visible browser so you can watch:
      1. The two-step login (email → password)
      2. The search results page loading
      3. What jobs are found and printed to the console
    """
    import scan as _scan_module
    from scan import _fetch_jobot_playwright

    config = _scan_module.load_config()

    if not config.get("jobot_email"):
        print("[!] JOBOT_EMAIL not set in .env — will scan without login")

    print(f"\n{'='*60}")
    print("Jobot scan test (headed browser)")
    print(f"{'='*60}")
    print("Watch the browser window for login + search steps.\n")

    jobs = await _fetch_jobot_playwright(config, headless=False, slow_mo=SLOW_MO)

    print(f"\n{'='*60}")
    print(f"Scan complete — {len(jobs)} job(s) found")
    print(f"{'='*60}")
    for i, j in enumerate(jobs[:10], 1):
        print(f"  {i:>2}. [{j.get('score', '?')}] {j['title']} @ {j['company']}")
        print(f"       {j['url']}")
    if len(jobs) > 10:
        print(f"  … and {len(jobs) - 10} more")

    if jobs:
        print(f"\nTo test applying to the first job, run:")
        print(f"  python scripts/run_local.py --url \"{jobs[0]['url']}\"")

    return jobs


async def test_jobot_full():
    """
    Full end-to-end Jobot test:
      1. Scan Jobot jobs (headed, login visible)
      2. Score and filter them
      3. Apply to the top-scoring job (headed, apply flow visible)
    """
    import scan as _scan_module
    from scan import _fetch_jobot_playwright, score_job

    config_scan = _scan_module.load_config()
    config_apply = load_config()

    print(f"\n{'='*60}")
    print("Jobot full test: scan → score → apply (headed)")
    print(f"{'='*60}\n")

    # ── Step 1: Scan ─────────────────────────────────────────────────────────
    print("Step 1/3 — Scanning Jobot for jobs (watch browser)…")
    jobs = await _fetch_jobot_playwright(config_scan, headless=False, slow_mo=SLOW_MO)
    if not jobs:
        print("[!] No jobs found — check credentials and try again")
        return

    # ── Step 2: Score ─────────────────────────────────────────────────────────
    print(f"\nStep 2/3 — Scoring {len(jobs)} job(s)…")
    scored = []
    for j in jobs:
        j = score_job(j, config_scan)
        if j["score"] >= config_scan.get("min_score", 30):
            scored.append(j)
    scored.sort(key=lambda x: -x["score"])

    print(f"  {len(scored)} job(s) passed minimum score ({config_scan.get('min_score', 30)})")
    for i, j in enumerate(scored[:5], 1):
        print(f"  {i}. score={j['score']}  {j['title']} @ {j['company']}")
        print(f"     {j['url']}")

    if not scored:
        print("[!] No jobs passed the score threshold — lower min_score in config.json")
        return

    top = scored[0]
    print(f"\nStep 3/3 — Applying to: {top['title']} @ {top['company']}")
    print(f"  URL: {top['url']}")
    confirm = input("  Proceed with apply? [y/N] ").strip().lower()
    if confirm != "y":
        print("  Skipped.")
        return

    # ── Step 3: Apply ─────────────────────────────────────────────────────────
    pw, browser, page, _ = await _launch()
    try:
        # Login first
        if config_apply.get("jobot_email") and config_apply.get("jobot_password"):
            print("  Logging in…")
            await _jobot_login(page, config_apply)

        from apply import apply_jobot, generate_cover_letter
        cover = generate_cover_letter(top["title"], top.get("description", ""), top["company"])
        result = await apply_jobot(page, top, cover, config_apply, None, 0)
        print(f"\n  Apply result: {'SUCCESS' if result else 'FAILED'}")
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

    import apply as _a
    orig_load = _a.load_config
    def patched_load():
        cfg = orig_load()
        cfg["headless"] = False
        return cfg
    _a.load_config = patched_load

    try:
        await _a.run(max_apply)
    finally:
        _a.load_config = orig_load


def main():
    parser = argparse.ArgumentParser(description="Local headed Playwright test runner")
    parser.add_argument("max_apply", nargs="?", type=int, default=3,
                        help="Max jobs to apply to (default: 3)")
    parser.add_argument("--url", help="Test apply at a specific job URL (Jobot URLs auto-login first)")
    parser.add_argument("--platform", help="Test login for a platform: zr | rh | jobot")
    parser.add_argument("--scan", metavar="SOURCE",
                        help="Run a scan source with headed browser (currently: jobot)")
    parser.add_argument("--jobot-full", action="store_true",
                        help="Full Jobot test: scan → score → confirm → apply")
    args = parser.parse_args()

    if args.scan:
        src = args.scan.lower()
        if src == "jobot":
            asyncio.run(test_jobot_scan())
        else:
            print(f"[!] --scan only supports 'jobot' for now (got: {src})")
    elif args.jobot_full:
        asyncio.run(test_jobot_full())
    elif args.platform:
        asyncio.run(test_login(args.platform))
    elif args.url:
        asyncio.run(test_url(args.url))
    else:
        asyncio.run(run_jobs(args.max_apply))


if __name__ == "__main__":
    main()
