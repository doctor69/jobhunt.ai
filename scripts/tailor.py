#!/usr/bin/env python3
"""
Resume tailoring + cover-letter generation via Claude AI.
Base resume is fetched live from https://leadtrade.app/doctor.
Uses prompt caching to avoid re-sending the resume on every call.
"""

import os
import re
import sys
from pathlib import Path

import anthropic
import requests
from bs4 import BeautifulSoup

import json

ROOT = Path(__file__).parent.parent
CONFIG_PATH = ROOT / "config" / "config.json"
RESUME_URL = "https://leadtrade.app/doctor"
_cached_resume: str | None = None


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

    # Fall back to local copy
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


def tailor_resume(job_title: str, job_description: str, job_company: str) -> str:
    """Return a resume tailored to the given job posting."""
    resume = fetch_resume()
    client = _client()
    projects_note = _projects_block()

    # System prompt + resume are cached; only the job details change per call
    response = client.messages.create(
        model="claude-opus-4-7",
        max_tokens=2000,
        system=[
            {
                "type": "text",
                "text": (
                    "You are an expert resume writer. "
                    "When given a resume and a job posting, rewrite the resume "
                    "to emphasize the most relevant experience and skills. "
                    "Rules:\n"
                    "- Keep all facts accurate — do NOT invent experience\n"
                    "- Mirror the job's language and keywords\n"
                    "- Lead with the most relevant achievements\n"
                    "- Prominently feature any notable projects the candidate built "
                    "(especially live products like leadtrade.app) when relevant to the role\n"
                    "- Optimise for ATS keyword matching\n"
                    "Output ONLY the tailored resume text — no commentary."
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
                    f"Tailor this resume for the following job.\n\n"
                    f"**Company:** {job_company}\n"
                    f"**Title:** {job_title}\n\n"
                    f"**Job Description:**\n{job_description[:3000]}"
                ),
            }
        ],
    )

    return response.content[0].text


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
    # Quick smoke test
    sample = tailor_resume(
        "Senior Backend Engineer",
        "Python, FastAPI, PostgreSQL, AWS, startup environment.",
        "Acme Corp",
    )
    print(sample[:500])
