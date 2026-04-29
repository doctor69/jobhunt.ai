#!/usr/bin/env python3
"""
Debug script to inspect apply flows on Remotive and Arbeitnow job pages.
Reports visible apply buttons, form fields, submit buttons, and final URLs.
Does NOT submit any forms.
"""

import asyncio
import json
from playwright.async_api import async_playwright, Page, TimeoutError as PWTimeout

URLS = [
    "https://remotive.com/remote-jobs/software-development/senior-full-stack-react-developer-2088711",
    "https://www.arbeitnow.com/jobs/companies/coding-partners/senior-full-stack-engineer-berlin-391720",
]


async def find_apply_buttons(page: Page) -> list[dict]:
    """Find all potential apply buttons/links on the page."""
    results = []

    # Broad selector for anything that looks like an apply trigger
    selectors = [
        "a", "button", "input[type='button']", "input[type='submit']",
    ]

    seen_texts = set()
    for sel in selectors:
        elements = await page.locator(sel).all()
        for el in elements:
            try:
                if not await el.is_visible():
                    continue
                text = (await el.inner_text()).strip()
                href = await el.get_attribute("href") or ""
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                el_id = await el.get_attribute("id") or ""
                el_class = await el.get_attribute("class") or ""
                el_type = await el.get_attribute("type") or ""
                aria_label = await el.get_attribute("aria-label") or ""
                data_qa = await el.get_attribute("data-qa") or ""
                data_cy = await el.get_attribute("data-cy") or ""
                data_testid = await el.get_attribute("data-testid") or ""

                # Only include elements that look apply-related
                combined = (text + href + el_id + el_class + aria_label +
                            data_qa + data_cy + data_testid).lower()
                if not any(w in combined for w in ("apply", "application", "submit")):
                    continue

                key = f"{tag}:{text}:{href}"
                if key in seen_texts:
                    continue
                seen_texts.add(key)

                results.append({
                    "tag": tag,
                    "text": text[:120],
                    "href": href[:200] if href else None,
                    "id": el_id,
                    "class": el_class[:100],
                    "type": el_type,
                    "aria_label": aria_label,
                    "data_qa": data_qa,
                    "data_cy": data_cy,
                    "data_testid": data_testid,
                })
            except Exception:
                continue

    return results


async def find_form_fields(page: Page) -> list[dict]:
    """Find all form fields on the page."""
    results = []
    field_sels = [
        "input:not([type='hidden']):not([type='submit']):not([type='button'])",
        "textarea",
        "select",
    ]
    for sel in field_sels:
        elements = await page.locator(sel).all()
        for el in elements:
            try:
                visible = await el.is_visible()
                tag = await el.evaluate("el => el.tagName.toLowerCase()")
                name = await el.get_attribute("name") or ""
                el_id = await el.get_attribute("id") or ""
                el_type = await el.get_attribute("type") or "text"
                placeholder = await el.get_attribute("placeholder") or ""
                label_text = ""

                # Try to find associated label
                if el_id:
                    try:
                        lbl = page.locator(f"label[for='{el_id}']")
                        if await lbl.count():
                            label_text = (await lbl.first.inner_text()).strip()
                    except Exception:
                        pass

                results.append({
                    "tag": tag,
                    "type": el_type,
                    "name": name,
                    "id": el_id,
                    "placeholder": placeholder,
                    "label": label_text,
                    "visible": visible,
                })
            except Exception:
                continue
    return results


async def find_submit_buttons(page: Page) -> list[dict]:
    """Find all submit-style buttons."""
    results = []
    selectors = [
        "button[type='submit']",
        "input[type='submit']",
        "button:not([type='button'])",  # buttons without explicit type default to submit in forms
    ]
    seen = set()
    for sel in selectors:
        elements = await page.locator(sel).all()
        for el in elements:
            try:
                visible = await el.is_visible()
                text = (await el.inner_text()).strip() if await el.evaluate(
                    "el => el.tagName.toLowerCase()") != "input" else ""
                value = await el.get_attribute("value") or ""
                el_id = await el.get_attribute("id") or ""
                el_class = await el.get_attribute("class") or ""
                el_type = await el.get_attribute("type") or ""
                aria_label = await el.get_attribute("aria-label") or ""

                key = f"{sel}:{text}:{value}"
                if key in seen:
                    continue
                seen.add(key)

                results.append({
                    "selector": sel,
                    "text": text[:120],
                    "value": value,
                    "id": el_id,
                    "class": el_class[:100],
                    "type": el_type,
                    "aria_label": aria_label,
                    "visible": visible,
                })
            except Exception:
                continue
    return results


async def inspect_page(page: Page, url: str, label: str):
    """Full inspection: load page, find apply buttons, click best one, inspect form."""
    print(f"\n{'='*70}")
    print(f"SITE: {label}")
    print(f"URL:  {url}")
    print(f"{'='*70}")

    # Load with longer timeout
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as e:
        print(f"[ERROR] Could not load page: {e}")
        return

    await asyncio.sleep(3)
    print(f"\nFinal URL after initial load: {page.url}")
    print(f"Page title: {await page.title()}")

    # ── Step 1: Find apply buttons ─────────────────────────────────────────────
    print(f"\n--- APPLY BUTTONS ON INITIAL PAGE ---")
    apply_btns = await find_apply_buttons(page)
    if not apply_btns:
        print("  (none found)")
    for b in apply_btns:
        print(f"  [{b['tag']}] text={b['text']!r}  href={b['href']!r}")
        if b['id']:    print(f"        id={b['id']!r}")
        if b['class']: print(f"        class={b['class']!r}")
        if b['aria_label']: print(f"        aria-label={b['aria_label']!r}")
        if b['data_qa']:    print(f"        data-qa={b['data_qa']!r}")
        if b['data_cy']:    print(f"        data-cy={b['data_cy']!r}")
        if b['data_testid']: print(f"        data-testid={b['data_testid']!r}")

    # ── Step 2: Check for form fields already present ──────────────────────────
    print(f"\n--- FORM FIELDS ON INITIAL PAGE ---")
    fields = await find_form_fields(page)
    visible_fields = [f for f in fields if f["visible"]]
    if not visible_fields:
        print("  (no visible form fields)")
    for f in visible_fields:
        print(f"  [{f['tag']}] type={f['type']!r}  name={f['name']!r}  "
              f"id={f['id']!r}  placeholder={f['placeholder']!r}  label={f['label']!r}")

    # ── Step 3: Check submit buttons on initial page ───────────────────────────
    print(f"\n--- SUBMIT BUTTONS ON INITIAL PAGE ---")
    submits = await find_submit_buttons(page)
    visible_submits = [s for s in submits if s["visible"]]
    if not visible_submits:
        print("  (none visible)")
    for s in visible_submits:
        print(f"  [{s['type'] or 'button'}] text={s['text']!r}  value={s['value']!r}  "
              f"id={s['id']!r}  class={s['class']!r}")

    # ── Step 4: Try to click the best apply button and see what happens ────────
    print(f"\n--- CLICKING APPLY BUTTON ---")

    url_before_click = page.url
    clicked = False
    clicked_info = None

    # Try to find and click the most likely apply button
    # Priority: buttons/links with "apply" text, data-qa, etc.
    best_candidates = [b for b in apply_btns if
                       "apply" in b["text"].lower() and b["tag"] in ("a", "button")]

    if not best_candidates:
        best_candidates = apply_btns  # fall back to all

    for candidate in best_candidates[:5]:
        try:
            tag = candidate["tag"]
            text = candidate["text"]
            href = candidate.get("href")

            print(f"  Trying to click: [{tag}] {text!r} href={href!r}")

            # If it's a direct external link, navigate instead of clicking
            if href and href.startswith("http") and not any(
                    domain in href for domain in ["remotive.com", "arbeitnow.com"]):
                print(f"  -> External link, navigating to: {href}")
                await page.goto(href, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(2)
                clicked = True
                clicked_info = candidate
                break

            # Find the element and click it
            loc = None
            # Try by text+tag
            if text:
                # Escape single quotes in text for selector
                safe_text = text.replace("'", "\\'")
                loc = page.locator(f"{tag}:has-text('{safe_text}')").first
            elif candidate.get("id"):
                loc = page.locator(f"#{candidate['id']}").first
            elif candidate.get("data_qa"):
                loc = page.locator(f"[data-qa='{candidate['data_qa']}']").first

            if loc:
                try:
                    await loc.wait_for(state="visible", timeout=3000)
                    # Check href again after finding element
                    actual_href = await loc.get_attribute("href") or ""
                    if actual_href.startswith("http"):
                        print(f"  -> Link has href, navigating: {actual_href[:100]}")
                        await page.goto(actual_href, wait_until="domcontentloaded", timeout=30000)
                    else:
                        await loc.click(timeout=5000)
                    await asyncio.sleep(3)
                    clicked = True
                    clicked_info = candidate
                    break
                except Exception as e:
                    print(f"  -> Click failed: {e}")
                    continue
        except Exception as e:
            print(f"  -> Error with candidate: {e}")
            continue

    if not clicked:
        print("  Could not click any apply button")
    else:
        print(f"  Successfully clicked: {clicked_info['text']!r}")

    # ── Step 5: Inspect page after clicking ───────────────────────────────────
    print(f"\n--- STATE AFTER CLICKING APPLY ---")
    print(f"  URL before: {url_before_click}")
    print(f"  URL after:  {page.url}")
    print(f"  Title: {await page.title()}")

    if page.url != url_before_click:
        print(f"\n  --> PAGE NAVIGATED (redirect/new page)")

    print(f"\n--- FORM FIELDS AFTER CLICKING APPLY ---")
    await asyncio.sleep(2)  # Wait for any modals/animations
    fields_after = await find_form_fields(page)
    visible_after = [f for f in fields_after if f["visible"]]
    if not visible_after:
        print("  (no visible form fields)")
    for f in visible_after:
        print(f"  [{f['tag']}] type={f['type']!r}  name={f['name']!r}  "
              f"id={f['id']!r}  placeholder={f['placeholder']!r}  label={f['label']!r}")

    print(f"\n--- SUBMIT BUTTONS AFTER CLICKING APPLY ---")
    submits_after = await find_submit_buttons(page)
    visible_sub_after = [s for s in submits_after if s["visible"]]
    if not visible_sub_after:
        print("  (none visible)")
    for s in visible_sub_after:
        print(f"  [{s['type'] or 'button'}] text={s['text']!r}  value={s['value']!r}  "
              f"id={s['id']!r}  class={s['class']!r}")

    # ── Step 6: Also grab all buttons text on final page ──────────────────────
    print(f"\n--- ALL VISIBLE BUTTONS/LINKS ON FINAL PAGE (apply-related) ---")
    final_btns = await find_apply_buttons(page)
    if not final_btns:
        print("  (none found)")
    for b in final_btns[:20]:
        print(f"  [{b['tag']}] {b['text']!r}  href={b['href']!r}  id={b['id']!r}")

    # ── Step 7: Dump full HTML snippet around apply section ───────────────────
    print(f"\n--- PAGE SOURCE SNIPPET (apply-related elements) ---")
    try:
        snippet = await page.evaluate("""() => {
            const results = [];
            // Find elements with 'apply' in various attributes
            const allEls = document.querySelectorAll('a, button, input, form');
            for (const el of allEls) {
                const text = (el.innerText || el.value || '').toLowerCase();
                const id = (el.id || '').toLowerCase();
                const cls = (el.className || '').toLowerCase();
                const href = (el.href || '').toLowerCase();
                const dqa = (el.getAttribute('data-qa') || '').toLowerCase();
                if (text.includes('apply') || id.includes('apply') ||
                    cls.includes('apply') || href.includes('apply') ||
                    dqa.includes('apply')) {
                    results.push({
                        tag: el.tagName,
                        id: el.id,
                        class: el.className,
                        text: (el.innerText || el.value || '').trim().substring(0, 100),
                        href: el.href || '',
                        type: el.type || '',
                        dataQa: el.getAttribute('data-qa') || '',
                        dataCy: el.getAttribute('data-cy') || '',
                        dataTestId: el.getAttribute('data-testid') || '',
                        outerHTML: el.outerHTML.substring(0, 300),
                    });
                }
            }
            return results;
        }""")
        for el in snippet[:20]:
            print(f"\n  TAG={el['tag']} id={el['id']!r} class={el['class'][:80]!r}")
            print(f"  text={el['text']!r}  href={el['href'][:100]!r}")
            print(f"  type={el['type']!r}  data-qa={el['dataQa']!r}  data-cy={el['dataCy']!r}")
            print(f"  outerHTML: {el['outerHTML'][:200]!r}")
    except Exception as e:
        print(f"  Error getting snippet: {e}")


HEADLESS_SHELL = "/opt/pw-browsers/chromium_headless_shell-1194/chrome-linux/headless_shell"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            executable_path=HEADLESS_SHELL,
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--ignore-certificate-errors"],
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

        for url in URLS:
            page = await ctx.new_page()
            label = "Remotive" if "remotive.com" in url else "Arbeitnow"
            try:
                await inspect_page(page, url, label)
            except Exception as e:
                print(f"\n[FATAL ERROR on {label}]: {e}")
            finally:
                await page.close()

        await browser.close()

    print(f"\n{'='*70}")
    print("DEBUG INSPECTION COMPLETE")
    print(f"{'='*70}")


if __name__ == "__main__":
    asyncio.run(main())
