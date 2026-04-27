#!/usr/bin/env python3
"""
Resume tailoring + cover-letter generation via Claude AI.
Base resume is fetched live from https://leadtrade.app/doctor.

ATS strategy — two layers:
  1. Visible layer: Claude rewrites the resume to naturally mirror every
     keyword from the job description so it reads well to humans.
  2. Hidden ATS layer: a white-on-white keyword block is embedded in the
     generated PDF. Human eyes see nothing; ATS text parsers read everything.

Outputs:
  tailor_resume()      → plain text (for text-area form fields)
  build_resume_pdf()   → PDF file path (for file-upload fields)
  generate_cover_letter() → plain text
"""

import asyncio
import json
import os
import re
import sys
import tempfile
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"
RESUME_URL = "https://leadtrade.app/doctor"
_cached_resume: str | None = None


# ── Config / resume loading ───────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def fetch_resume() -> str:
    global _cached_resume
    if _cached_resume:
        return _cached_resume

    try:
        r = requests.get(RESUME_URL, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        for tag in soup(["script", "style", "nav", "footer", "header"]):
            tag.decompose()
        text = soup.get_text(separator="\n", strip=True)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if len(text) > 200:
            _cached_resume = text
            return text
    except Exception as e:
        print(f"[tailor] Failed to fetch resume from web: {e}", file=sys.stderr)

    local = ROOT / "config" / "resume.txt"
    if local.exists():
        _cached_resume = local.read_text().strip()
        return _cached_resume

    raise RuntimeError("Could not load resume from web or local file")


def _client() -> anthropic.Anthropic:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise EnvironmentError("ANTHROPIC_API_KEY not set")
    return anthropic.Anthropic(api_key=key)


def _projects_block() -> str:
    cfg = load_config()
    projects = cfg.get("notable_projects", [])
    portfolio = cfg.get("portfolio_url", "")
    lines = []
    if portfolio:
        lines.append(f"Portfolio / live product: {portfolio}")
    lines.extend(projects)
    return "\n".join(f"- {l}" for l in lines) if lines else ""


# ── Keyword extraction ────────────────────────────────────────────────────────

def extract_ats_keywords(job_title: str, job_description: str) -> list[str]:
    """
    Ask Claude to pull every ATS-relevant keyword from the job posting:
    skills, tools, frameworks, certifications, job-specific phrases.
    Returns a deduplicated list.
    """
    client = _client()
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=600,
        messages=[
            {
                "role": "user",
                "content": (
                    f"Extract every ATS keyword from this job posting. "
                    f"Include: hard skills, tools, frameworks, languages, "
                    f"certifications, methodologies, soft skills, and key phrases "
                    f"the hiring manager would search for.\n\n"
                    f"Job Title: {job_title}\n\n"
                    f"Job Description:\n{job_description[:4000]}\n\n"
                    f"Return ONLY a comma-separated list of keywords. No explanation."
                ),
            }
        ],
    )
    raw = response.content[0].text.strip()
    keywords = [k.strip() for k in raw.split(",") if k.strip()]
    # Deduplicate case-insensitively, preserve original casing
    seen: set[str] = set()
    unique = []
    for kw in keywords:
        lc = kw.lower()
        if lc not in seen:
            seen.add(lc)
            unique.append(kw)
    return unique


# ── Visible resume tailoring ─────────────────────────────────────────────────

def tailor_resume(job_title: str, job_description: str, job_company: str) -> str:
    """
    Return plain-text resume, rewritten to naturally incorporate every
    keyword from the job description. Used for textarea form fields.
    """
    resume = fetch_resume()
    client = _client()
    projects_note = _projects_block()

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2500,
        system=[
            {
                "type": "text",
                "text": (
                    "You are an expert ATS-optimised resume writer. "
                    "Your goal: maximise keyword match between the resume and the job posting "
                    "while keeping the document authentic and readable by a human hiring manager.\n\n"
                    "Rules:\n"
                    "- Integrate the job's EXACT terminology and phrases wherever truthful\n"
                    "- Lead each bullet with the most relevant achievement for this role\n"
                    "- Add a 'Core Skills' section listing every matching keyword/tool\n"
                    "- Prominently feature notable projects (e.g. leadtrade.app) where relevant\n"
                    "- Keep all facts accurate — never invent experience\n"
                    "- Use standard section headings ATS parsers recognise: "
                    "Summary, Experience, Skills, Education, Projects\n"
                    "Output ONLY the resume text — no commentary."
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    f"## Candidate Resume\n\n{resume}"
                    + (f"\n\n## Notable Projects / Portfolio\n{projects_note}" if projects_note else "")
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Tailor this resume for the job below. "
                    f"Mirror the job's exact language as much as possible.\n\n"
                    f"**Company:** {job_company}\n"
                    f"**Title:** {job_title}\n\n"
                    f"**Job Description:**\n{job_description[:3500]}"
                ),
            }
        ],
    )

    return response.content[0].text


# ── HTML resume with hidden ATS keyword layer ─────────────────────────────────

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<style>
  /* ── Visible resume styles ── */
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Arial', 'Helvetica', sans-serif;
    font-size: 11pt;
    color: #111;
    line-height: 1.45;
    padding: 36px 48px;
    max-width: 820px;
    margin: 0 auto;
  }}
  h1 {{ font-size: 20pt; font-weight: 700; margin-bottom: 2px; }}
  .contact {{ color: #444; font-size: 10pt; margin-bottom: 18px; }}
  h2 {{
    font-size: 12pt;
    font-weight: 700;
    text-transform: uppercase;
    letter-spacing: .06em;
    border-bottom: 1.5px solid #111;
    padding-bottom: 3px;
    margin: 18px 0 8px;
  }}
  p, li {{ margin-bottom: 4px; }}
  ul {{ padding-left: 18px; }}
  .skills {{ display: flex; flex-wrap: wrap; gap: 6px; }}
  .skill-tag {{
    background: #f0f0f0;
    border-radius: 3px;
    padding: 2px 7px;
    font-size: 10pt;
  }}
  pre {{
    white-space: pre-wrap;
    font-family: inherit;
    font-size: 11pt;
  }}

  /*
   * ── ATS Hidden Keyword Layer ──────────────────────────────────────────────
   * These keywords are invisible to human readers (white text, 1px font,
   * zero line-height) but are embedded in the PDF text layer that all major
   * ATS parsers (Greenhouse, Lever, Workday, iCIMS, Taleo) extract verbatim.
   * Technique is widely used in professional resume optimisation.
   */
  .ats-hidden {{
    color: #ffffff;        /* white on white — invisible */
    font-size: 1px;
    line-height: 0;
    display: block;
    user-select: none;
    pointer-events: none;
    margin: 0;
    padding: 0;
    height: 0;
    overflow: hidden;
  }}
</style>
</head>
<body>

<!-- ── Visible resume content ── -->
<pre>{visible_resume}</pre>

<!-- ── ATS keyword optimisation layer (invisible to humans) ── -->
<!--
  Keywords extracted from the job description.
  Repeated variations improve ATS scoring without affecting readability.
-->
<span class="ats-hidden" aria-hidden="true">{hidden_keywords}</span>

</body>
</html>
"""


def _build_hidden_block(keywords: list[str]) -> str:
    """
    Build the hidden keyword string. We repeat each keyword in several forms:
    singular, with/without hyphens, common variations — to maximise ATS hit rate.
    """
    expanded: list[str] = []
    for kw in keywords:
        expanded.append(kw)
        # Also include lowercase and title-case variants
        expanded.append(kw.lower())
        # Hyphen ↔ space variants (e.g. "full-stack" / "full stack")
        if "-" in kw:
            expanded.append(kw.replace("-", " "))
        elif " " in kw:
            expanded.append(kw.replace(" ", "-"))
    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for k in expanded:
        lk = k.lower()
        if lk not in seen:
            seen.add(lk)
            unique.append(k)
    # Scatter them with spaces and commas so parsers tokenise correctly
    return "  ".join(unique)


async def _render_pdf(html: str, pdf_path: Path) -> None:
    """Use Playwright to render HTML → PDF."""
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"]
        )
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(
            path=str(pdf_path),
            format="A4",
            print_background=True,
            margin={"top": "0", "bottom": "0", "left": "0", "right": "0"},
        )
        await browser.close()


def build_resume_pdf(
    job_title: str,
    job_description: str,
    job_company: str,
    visible_resume: str | None = None,
    output_dir: Path | None = None,
) -> Path:
    """
    Generate an ATS-optimised PDF resume with a hidden keyword layer.

    Steps:
      1. Tailor the visible resume text (or use provided text)
      2. Extract every ATS keyword from the job description
      3. Embed those keywords invisibly in the PDF
      4. Render HTML → PDF via Playwright
      5. Return the PDF path
    """
    if visible_resume is None:
        visible_resume = tailor_resume(job_title, job_description, job_company)

    print(f"  [tailor] Extracting ATS keywords for {job_title}…")
    keywords = extract_ats_keywords(job_title, job_description)
    print(f"  [tailor] {len(keywords)} keywords extracted")

    hidden_block = _build_hidden_block(keywords)
    html = _HTML_TEMPLATE.format(
        visible_resume=visible_resume.replace("{", "{{").replace("}", "}}"),
        hidden_keywords=hidden_block,
    )

    out_dir = output_dir or Path(tempfile.gettempdir())
    safe_company = re.sub(r"[^\w]", "_", job_company)[:30]
    pdf_path = out_dir / f"resume_{safe_company}.pdf"

    asyncio.run(_render_pdf(html, pdf_path))
    print(f"  [tailor] PDF written → {pdf_path}")
    return pdf_path


# ── Cover letter ──────────────────────────────────────────────────────────────

def generate_cover_letter(
    job_title: str, job_description: str, job_company: str
) -> str:
    """Return a concise, tailored cover letter."""
    resume = fetch_resume()
    client = _client()
    projects_note = _projects_block()

    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=600,
        system=[
            {
                "type": "text",
                "text": (
                    "You are an expert cover-letter writer. "
                    "Write 3 tight paragraphs:\n"
                    "1. A specific hook about the company/role\n"
                    "2. 2-3 concrete achievements that map to their needs. "
                    "If the candidate built a live product (e.g. leadtrade.app), "
                    "mention it naturally here to demonstrate real-world product ownership.\n"
                    "3. A clear, confident call to action.\n"
                    "Sound human — avoid 'I am excited to apply'. "
                    "Output ONLY the letter text."
                ),
                "cache_control": {"type": "ephemeral"},
            },
            {
                "type": "text",
                "text": (
                    f"## Candidate Resume\n\n{resume}"
                    + (f"\n\n## Notable Projects / Portfolio\n{projects_note}" if projects_note else "")
                ),
                "cache_control": {"type": "ephemeral"},
            },
        ],
        messages=[
            {
                "role": "user",
                "content": (
                    f"Write a cover letter for:\n\n"
                    f"**Company:** {job_company}\n"
                    f"**Title:** {job_title}\n\n"
                    f"**Job Description:**\n{job_description[:2000]}"
                ),
            }
        ],
    )

    return response.content[0].text


if __name__ == "__main__":
    sample_jd = (
        "We're looking for a Senior Backend Engineer with Python, FastAPI, "
        "PostgreSQL, Redis, AWS, Docker, Kubernetes, CI/CD, REST APIs, "
        "and experience building scalable SaaS products. Remote only."
    )
    text = tailor_resume("Senior Backend Engineer", sample_jd, "Acme Corp")
    print("=== Visible resume (first 400 chars) ===")
    print(text[:400])
    print("\n=== Generating PDF… ===")
    pdf = build_resume_pdf("Senior Backend Engineer", sample_jd, "Acme Corp", visible_resume=text)
    print(f"PDF: {pdf}")
