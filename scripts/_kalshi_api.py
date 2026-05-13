"""
Shared helpers for kalshi_bot CLI scripts.

Loads credentials from .env (or environment), builds auth headers,
and provides a paginated GET helper.
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv

# Allow running scripts directly without installing the package
_repo_root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_repo_root / "src"))

from kalshi_bot.auth import auth_headers, load_private_key

_DEMO_BASE = "https://demo-api.kalshi.co"
_LIVE_BASE = "https://api.elections.kalshi.com"


def load_credentials(
    env_path: Path | None = None,
    demo_override: bool | None = None,
) -> tuple[str, object, str]:
    """
    Load API credentials from environment / .env file.

    Parameters
    ----------
    demo_override:
        If True or False, overrides the KALSHI_DEMO env var.
        If None, falls back to KALSHI_DEMO (default: True).

    Returns (key_id, private_key_object, base_url).
    """
    load_dotenv(dotenv_path=env_path or (_repo_root / ".env"), override=False)

    key_id   = os.environ.get("KALSHI_API_KEY_ID", "").strip()
    key_path = os.environ.get("KALSHI_API_PRIVATE_KEY_PATH", "").strip()

    if not key_id or not key_path:
        sys.exit(
            "Error: KALSHI_API_KEY_ID and KALSHI_API_PRIVATE_KEY_PATH must be set "
            "in .env or the environment."
        )

    if demo_override is None:
        demo = os.environ.get("KALSHI_DEMO", "true").strip().lower() in ("1", "true", "yes")
    else:
        demo = demo_override

    private_key = load_private_key(key_path)
    base_url    = _DEMO_BASE if demo else _LIVE_BASE
    env_label   = "demo" if demo else "live"
    print(f"Using {env_label} API ({base_url})", file=sys.stderr)
    return key_id, private_key, base_url


def get_all_pages(
    base_url: str,
    key_id: str,
    private_key,
    path: str,
    collection_key: str,
    params: dict | None = None,
    page_size: int = 200,
) -> list[dict]:
    """
    Page through a Kalshi list endpoint, returning all items.

    Parameters
    ----------
    path:
        API path, e.g. ``"/trade-api/v2/series"``.
    collection_key:
        The JSON key that holds the list in each response, e.g. ``"series"``.
    params:
        Extra query parameters (merged with limit/cursor on each page).
    page_size:
        Items per page (max 1000 for most Kalshi endpoints).
    """
    params   = dict(params or {})
    results  = []
    cursor   = None

    while True:
        page_params = {**params, "limit": page_size}
        if cursor:
            page_params["cursor"] = cursor

        headers = auth_headers(private_key, key_id, "GET", path)
        resp    = requests.get(
            base_url + path, headers=headers, params=page_params, timeout=10
        )
        if resp.status_code != 200:
            sys.exit(f"API error {resp.status_code} — {resp.text}")

        body    = resp.json()
        items   = body.get(collection_key, [])
        results.extend(items)

        cursor = body.get("cursor") or None
        if not cursor or not items:
            break

    return results


def base_arg_parser(description: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=description)
    p.add_argument(
        "--output", "-o",
        metavar="FILE",
        help="Write output to FILE (default: print to stdout).",
    )

    env_group = p.add_mutually_exclusive_group()
    env_group.add_argument(
        "--demo",
        dest="demo_override",
        action="store_true",
        default=None,
        help="Force demo API (overrides KALSHI_DEMO env var).",
    )
    env_group.add_argument(
        "--live",
        dest="demo_override",
        action="store_false",
        help="Force live API (overrides KALSHI_DEMO env var).",
    )

    return p


def demo_override_from_args(args: argparse.Namespace) -> bool | None:
    """Extract the demo/live override from parsed args (None = use env var)."""
    return args.demo_override


def _flatten(row: dict) -> dict:
    """Flatten one level of nested dicts into dot-separated column names."""
    out = {}
    for k, v in row.items():
        if isinstance(v, dict):
            for sub_k, sub_v in v.items():
                out[f"{k}.{sub_k}"] = sub_v
        else:
            out[k] = v
    return out


def write_output(
    data: list[dict],
    args: argparse.Namespace,
    priority_cols: list[str] | None = None,
) -> None:
    if not data:
        print("No data returned.", file=sys.stderr)
        return

    rows = [_flatten(r) for r in data]
    # Union of all keys present in the data
    all_cols = list(dict.fromkeys(k for r in rows for k in r))

    if priority_cols:
        leading  = [c for c in priority_cols if c in all_cols]
        trailing = [c for c in all_cols if c not in leading]
        fieldnames = leading + trailing
    else:
        fieldnames = all_cols

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore",
                            lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    text = buf.getvalue()

    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {len(rows)} rows to {args.output}", file=sys.stderr)
    else:
        print(text, end="")
