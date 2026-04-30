#!/usr/bin/env python3
"""Live page inspector — visits real URLs and prints every button/link/form."""
import asyncio
from playwright.async_api import async_playwright

URLS = [
    "https://www.arbeitnow.com/jobs/companies/coding-partners/senior-full-stack-engineer-berlin-391720",
    "https://www.arbeitnow.com/jobs/companies/sumup/senior-backend-engineer-golang-bank-berlin-169219",
]

async def inspect(url: str):
    print(f"\n{'='*70}")
    print(f"URL: {url}")
    print('='*70)
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path="/opt/pw-browsers/chromium-1194/chrome-linux/chrome",
            args=["--no-sandbox","--disable-dev-shm-usage"],
        )
        ctx = await browser.new_context(ignore_https_errors=True)
        page = await ctx.new_page()
        try:
            await page.goto(url, wait_until="networkidle", timeout=30000)
            await asyncio.sleep(3)
            # Dump a snippet of rendered HTML to understand page structure
            html = await page.content()
            print(f"HTML snippet (first 3000 chars):\n{html[:3000]}\n")
            print(f"Final URL: {page.url}")

            # All visible buttons
            print("\n--- Buttons ---")
            for btn in await page.locator("button").all():
                if await btn.is_visible():
                    txt = (await btn.inner_text()).strip()[:80]
                    cls = await btn.get_attribute("class") or ""
                    print(f"  button text='{txt}' class='{cls}'")

            # All visible links with href
            print("\n--- Links (a[href]) ---")
            for a in await page.locator("a[href]").all():
                if await a.is_visible():
                    txt = (await a.inner_text()).strip()[:50]
                    href = (await a.get_attribute("href") or "")[:80]
                    cls = (await a.get_attribute("class") or "")[:60]
                    if any(w in (txt+href+cls).lower() for w in ("apply","job","career","work")):
                        print(f"  a text='{txt}' href='{href}' class='{cls}'")

            # Form inputs
            print("\n--- Form inputs ---")
            for inp in await page.locator("input, textarea, select").all():
                if await inp.is_visible():
                    t = await inp.get_attribute("type") or await inp.evaluate("e=>e.tagName.toLowerCase()")
                    name = await inp.get_attribute("name") or ""
                    id_ = await inp.get_attribute("id") or ""
                    ph = await inp.get_attribute("placeholder") or ""
                    print(f"  {t} name='{name}' id='{id_}' placeholder='{ph}'")

        except Exception as e:
            print(f"ERROR: {e}")
        finally:
            await browser.close()

async def main():
    for u in URLS:
        await inspect(u)

asyncio.run(main())
