#!/usr/bin/env python3
"""Monitor Apple refurbished stores across Europe for Mac Studio Ultra with 256GB/512GB RAM.

Env vars:
  NTFY_TOPIC          (required for alerts) ntfy.sh topic name, e.g. "justin-apple-refurb-x7k2"
  NTFY_SERVER         (optional) default https://ntfy.sh
  POLL_INTERVAL_SEC   (optional) default 60
  STATE_PATH          (optional) default /var/lib/apple-refurb-monitor/seen.json
  HEARTBEAT_HOURS     (optional) default 24
  REGIONS             (optional) comma list of locale codes to override REGIONS list
"""

from __future__ import annotations

import json
import os
import random
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin

import requests

# (locale-path, display name) — Apple operates a refurb store in each of these.
# If Apple returns 404 for a locale (e.g. country removed from refurb program),
# it is silently skipped.
DEFAULT_REGIONS: list[tuple[str, str]] = [
    # Confirmed live Apple refurb storefronts in Europe (as of 2026-05).
    # Any returning 404 will be silently skipped.
    ("uk", "United Kingdom"),
    ("de", "Germany"),
    ("fr", "France"),
    ("nl", "Netherlands"),
    ("it", "Italy"),
    ("es", "Spain"),
    ("ie", "Ireland"),
    ("at", "Austria"),
    ("be-fr", "Belgium (FR)"),
    ("be-nl", "Belgium (NL)"),
    ("ch-de", "Switzerland (DE)"),
    ("ch-fr", "Switzerland (FR)"),
    ("pl", "Poland"),
]

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
BASE_URL = "https://www.apple.com"
PATH = "/shop/refurbished/mac"

# Target: Mac Studio with M3 Ultra (only Ultra goes to 256/512GB).
TARGET_MODEL_SLUG = "macstudio"
TARGET_MEMORY_SIZES = {"256gb", "512gb"}

POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "60"))
REGION_JITTER_SEC = (0.8, 2.5)
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/apple-refurb-monitor/seen.json"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "24"))

def get_regions() -> list[tuple[str, str]]:
    override = os.environ.get("REGIONS", "").strip()
    if not override:
        return DEFAULT_REGIONS
    wanted = {r.strip().lower() for r in override.split(",") if r.strip()}
    return [r for r in DEFAULT_REGIONS if r[0] in wanted]


def fetch_region(session: requests.Session, locale: str) -> str | None:
    url = f"{BASE_URL}/{locale}{PATH}"
    try:
        r = session.get(url, timeout=20)
        if r.status_code in (404, 410):
            return None
        r.raise_for_status()
        return r.text
    except requests.RequestException as e:
        print(f"[warn] {locale}: {e}", file=sys.stderr, flush=True)
        return None


def _extract_json_array(text: str, key: str) -> list | None:
    """Find `"<key>":[ ... ]` in text and return the parsed array, scanning balanced brackets."""
    needle = f'"{key}":['
    idx = text.find(needle)
    if idx < 0:
        return None
    start = idx + len(needle) - 1  # position of '['
    depth = 0
    in_str = False
    escape = False
    for i in range(start, len(text)):
        c = text[i]
        if escape:
            escape = False
            continue
        if c == "\\" and in_str:
            escape = True
            continue
        if c == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def parse_listings(html: str, locale: str) -> list[dict]:
    """Extract Mac Studio Ultra (256/512GB) tiles from a refurb category page."""
    tiles = _extract_json_array(html, "tiles") or []
    matches: list[dict] = []
    for t in tiles:
        dims = (t.get("filters") or {}).get("dimensions") or {}
        if dims.get("refurbClearModel", "").lower() != TARGET_MODEL_SLUG:
            continue
        if dims.get("tsMemorySize", "").lower() not in TARGET_MEMORY_SIZES:
            continue
        url_path = t.get("productDetailsUrl", "")
        price_block = t.get("price") or {}
        prev = price_block.get("previousPrice") or {}
        price_str = ""
        if prev.get("raw_amount"):
            price_str = f"{price_block.get('priceCurrency', '')} {prev['raw_amount']}".strip()
        matches.append({
            "title": t.get("title", "").strip(),
            "url": urljoin(BASE_URL, url_path) if url_path else "",
            "locale": locale,
            "price": price_str,
            "part_number": t.get("partNumber", ""),
            "memory": dims.get("tsMemorySize", ""),
            "storage": dims.get("dimensionCapacity", ""),
        })
    return matches


def load_state() -> dict:
    if STATE_PATH.exists():
        try:
            data = json.loads(STATE_PATH.read_text())
            data.setdefault("seen", {})
            data.setdefault("last_heartbeat", 0)
            return data
        except (json.JSONDecodeError, OSError):
            pass
    return {"seen": {}, "last_heartbeat": 0}


def save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    tmp.replace(STATE_PATH)


def send_ntfy(
    title: str,
    message: str,
    *,
    priority: str = "default",
    click: str | None = None,
    tags: list[str] | None = None,
) -> None:
    if not NTFY_TOPIC:
        print(f"[ntfy-disabled] {title}: {message}", flush=True)
        return
    url = f"{NTFY_SERVER.rstrip('/')}/{NTFY_TOPIC}"
    # ntfy requires ASCII-safe headers; encode title to RFC 2047-ish fallback.
    safe_title = title.encode("ascii", "replace").decode("ascii")
    headers = {"Title": safe_title, "Priority": priority}
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = ",".join(tags)
    try:
        requests.post(url, data=message.encode("utf-8"), headers=headers, timeout=15)
    except requests.RequestException as e:
        print(f"[warn] ntfy: {e}", file=sys.stderr, flush=True)


def run_once(session: requests.Session, regions: list[tuple[str, str]], state: dict) -> int:
    new_matches = 0
    for locale, name in regions:
        html = fetch_region(session, locale)
        if html is None:
            continue
        for item in parse_listings(html, locale):
            key = item.get("part_number") or item["url"]
            key = f"{locale}:{key}"
            if key in state["seen"]:
                continue
            state["seen"][key] = {
                "first_seen": int(time.time()),
                "title": item["title"],
                "region": name,
                "price": item.get("price", ""),
                "memory": item.get("memory", ""),
                "storage": item.get("storage", ""),
                "url": item["url"],
            }
            new_matches += 1
            mem = item.get("memory", "").upper()
            stor = item.get("storage", "").upper()
            spec_suffix = f"\nRAM: {mem}  SSD: {stor}" if mem else ""
            price_suffix = f"\nPrice: {item['price']}" if item.get("price") else ""
            send_ntfy(
                title=f"Mac Studio Ultra {mem} available - {name}",
                message=f"{item['title']}{spec_suffix}{price_suffix}\n{item['url']}",
                priority="urgent",
                click=item["url"],
                tags=["rotating_light", "shopping_cart"],
            )
            print(f"[MATCH] {name}: {item['title']} ({mem}) -> {item['url']}", flush=True)
        time.sleep(random.uniform(*REGION_JITTER_SEC))
    return new_matches


def main() -> int:
    regions = get_regions()
    if not NTFY_TOPIC:
        print("warn: NTFY_TOPIC not set; alerts will only print to stdout", file=sys.stderr, flush=True)
    session = requests.Session()
    session.headers.update({
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-GB,en;q=0.9",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    })
    state = load_state()
    print(f"started; polling {len(regions)} regions every ~{POLL_INTERVAL_SEC}s", flush=True)
    send_ntfy(
        "Apple refurb monitor started",
        f"Watching {len(regions)} European storefronts for Mac Studio Ultra 256GB/512GB.",
        priority="low",
        tags=["white_check_mark"],
    )
    while True:
        cycle_start = time.time()
        try:
            run_once(session, regions, state)
        except Exception as e:
            print(f"[error] cycle: {e}", file=sys.stderr, flush=True)
        if time.time() - state.get("last_heartbeat", 0) > HEARTBEAT_HOURS * 3600:
            state["last_heartbeat"] = int(time.time())
            send_ntfy(
                "Apple refurb monitor heartbeat",
                f"Still running. {len(state['seen'])} matched URLs in history.",
                priority="min",
                tags=["heartbeat"],
            )
        save_state(state)
        elapsed = time.time() - cycle_start
        time.sleep(max(5.0, POLL_INTERVAL_SEC - elapsed))


if __name__ == "__main__":
    sys.exit(main() or 0)
