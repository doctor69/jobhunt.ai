# jobhunt.ai

## Purging stale jobs

`data/jobs.json` only holds postings believed to still be open. Every scan ends
with a purge (`scripts/purge.py`), which drops a job for one of five reasons:

| reason | meaning |
| --- | --- |
| `duplicate` | another record points at the same posting (boards mirror listings across domains with a re-rolled id suffix) |
| `missing` | its source was scanned successfully but no longer lists it, for longer than `purge_missing_days` |
| `expired` | no source has listed it for `max_age_days` |
| `dead_link` | the URL answers 404/410, redirects to the board root, or the page says the role is filled |
| `below_threshold` | rescored under `min_score`, or no longer remote while `remote_required` is on |

Two rules keep the purge from eating good data:

- **Applied jobs are never purged** — they are the application record.
- **Only sources that returned results this run may purge their own jobs**, so an
  outage or a scraper breaking never reads as "every job is gone".

A job that is still listed has its `last_seen_at` refreshed on every scan, so it
never ages out while it is live.

Run it standalone:

```bash
python scripts/purge.py --dry-run     # report only, change nothing
python scripts/purge.py --verbose     # clean, listing every removal
python scripts/purge.py --no-verify   # skip the HTTP dead-link checks
python scripts/scan.py --no-purge     # scan without purging
```

Each run appends a summary to `data/purge_log.json` (last 50 runs).

### Config knobs (`config/config.json`)

| key | default | effect |
| --- | --- | --- |
| `max_age_days` | 14 | expire jobs unseen this long |
| `purge_missing_days` | 2 | grace before a delisted job is dropped |
| `purge_min_source_results` | 1 | results a source must return to count as healthy |
| `purge_verify_links` | true | HTTP-check postings for dead links |
| `purge_link_check_limit` | 150 | link checks per run |
| `purge_link_recheck_days` | 3 | don't re-check a URL more often than this |
| `purge_link_check_workers` | 8 | concurrent link checks |

### Tests

```bash
python scripts/test_purge.py        # purge rules, offline
python scripts/test_scan_purge.py   # multi-scan lifecycle against a fake source
```
