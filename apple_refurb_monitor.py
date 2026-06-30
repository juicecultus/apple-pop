#!/usr/bin/env python3
"""Monitor Apple refurbished stores across the US, Canada and Europe for Mac Studio Ultra with 256GB/512GB RAM.

Env vars:
  NTFY_TOPIC          (required for alerts) ntfy.sh topic name, e.g. "justin-apple-refurb-x7k2"
  NTFY_SERVER         (optional) default https://ntfy.sh
  POLL_INTERVAL_SEC   (optional) default 60
  STATE_PATH          (optional) default /var/lib/apple-refurb-monitor/seen.json
  HEARTBEAT_HOURS     (optional) default 24
  REGIONS             (optional) comma list of locale codes to override REGIONS list
  TARGET_MEMORY_SIZES (optional) comma list of memory slugs to match,
                      default "256gb,512gb" (Mac Studio M3 Ultra exclusive).
                      Set to "128gb" to smoke-test against current 16" MBP M4 Max stock.
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
    # Confirmed live Apple refurb storefronts (as of 2026-06).
    # Any returning 404 will be silently skipped.
    # North America
    ("us", "United States"),
    ("ca", "Canada"),
    # Europe
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

# Two-tier matching:
#   tier="target"  -> urgent alert. memory slug in TARGET_MEMORY_SIZES.
#   tier="hedge"   -> default-priority alert. refurbClearModel == HEDGE_MODEL_SLUG
#                     AND parsed memory >= HEDGE_MIN_GB. Safety net for high-RAM
#                     Mac Studios in case Apple uses an unexpected slug format
#                     (256gb/512gb have never been observed in any EU refurb
#                     store, so the exact slug is unverified).
def _parse_memory_targets() -> set[str]:
    raw = os.environ.get("TARGET_MEMORY_SIZES", "256gb,512gb")
    return {v.strip().lower() for v in raw.split(",") if v.strip()}

TARGET_MEMORY_SIZES = _parse_memory_targets()
HEDGE_MODEL_SLUG = os.environ.get("HEDGE_MODEL_SLUG", "macstudio").lower()
HEDGE_MIN_GB = int(os.environ.get("HEDGE_MIN_GB", "96"))
# A Mac Studio with >= this much RAM fires an URGENT (target) alert, even if its
# memory isn't 256/512. Default 128 catches the maxed-out M4 Max Studio. Scoped
# to Mac Studio only, so 128GB MacBook Pros do NOT flood urgent alerts.
STUDIO_URGENT_MIN_GB = int(os.environ.get("STUDIO_URGENT_MIN_GB", "128"))


def _memory_slug_to_gb(slug: str) -> int | None:
    """Convert Apple's memory-size slug to GB. Returns None if unparseable.

    Examples: "16gb"->16, "192gb"->192, "1_5tb"->1536, "2tb"->2048.
    """
    s = (slug or "").strip().lower()
    if not s:
        return None
    m = re.fullmatch(r"(\d+)(?:_(\d+))?(gb|tb)", s)
    if not m:
        return None
    whole, frac, unit = m.group(1), m.group(2), m.group(3)
    value = float(whole)
    if frac:
        value += float(f"0.{frac}")
    if unit == "tb":
        value *= 1024
    return int(value)

POLL_INTERVAL_SEC = int(os.environ.get("POLL_INTERVAL_SEC", "60"))
REGION_JITTER_SEC = (1.5, 4.0)  # spacing between regions, to avoid bursting Apple
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "").strip()
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
STATE_PATH = Path(os.environ.get("STATE_PATH", "/var/lib/apple-refurb-monitor/seen.json"))
HEARTBEAT_HOURS = float(os.environ.get("HEARTBEAT_HOURS", "24"))

# Per-request resilience against Apple's intermittent 5xx / read timeouts.
FETCH_CONNECT_TIMEOUT = float(os.environ.get("FETCH_CONNECT_TIMEOUT", "10"))
FETCH_READ_TIMEOUT = float(os.environ.get("FETCH_READ_TIMEOUT", "30"))
FETCH_MAX_ATTEMPTS = int(os.environ.get("FETCH_MAX_ATTEMPTS", "3"))
FETCH_BACKOFF_BASE_SEC = float(os.environ.get("FETCH_BACKOFF_BASE_SEC", "2.0"))

def get_regions() -> list[tuple[str, str]]:
    override = os.environ.get("REGIONS", "").strip()
    if not override:
        return DEFAULT_REGIONS
    wanted = {r.strip().lower() for r in override.split(",") if r.strip()}
    return [r for r in DEFAULT_REGIONS if r[0] in wanted]


def fetch_region(session: requests.Session, locale: str) -> str | None:
    """Fetch a locale's refurb page, retrying transient failures with backoff.

    404/410 (locale not in the refurb program) are permanent -> return None
    immediately, no retry. 5xx, read timeouts, and connection errors are
    transient (Apple rate-limiting) -> retry up to FETCH_MAX_ATTEMPTS with
    exponential backoff + jitter. Returns None only after exhausting attempts,
    so a single bad cycle never permanently drops a region.
    """
    url = f"{BASE_URL}/{locale}{PATH}"
    timeout = (FETCH_CONNECT_TIMEOUT, FETCH_READ_TIMEOUT)
    last_err = ""
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            r = session.get(url, timeout=timeout)
            if r.status_code in (404, 410):
                return None  # permanent: locale not offered
            if r.status_code >= 500:
                last_err = f"{r.status_code} Server Error"
                raise requests.HTTPError(last_err)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_err = str(e) or type(e).__name__
            if attempt < FETCH_MAX_ATTEMPTS:
                # exponential backoff with jitter: base*2^(n-1) +/- up to base/2
                delay = FETCH_BACKOFF_BASE_SEC * (2 ** (attempt - 1))
                delay += random.uniform(0, FETCH_BACKOFF_BASE_SEC)
                time.sleep(delay)
    print(f"[warn] {locale}: failed after {FETCH_MAX_ATTEMPTS} attempts: {last_err}",
          file=sys.stderr, flush=True)
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
    """Extract refurb-mac tiles that warrant an alert, with a tier per item.

    Two kinds of tile carry the memory we care about:
      1. Fixed-config tiles expose `filters.dimensions.tsMemorySize` (e.g. "512gb").
      2. CONFIGURABLE tiles (high-end M3 Ultra Mac Studios) omit tsMemorySize
         entirely — the category tile shows only a base price and you pick the
         memory on the product page (96/256/512GB). These have NO memory slug,
         so memory-only matching silently misses them. This was the bug that
         let a 512GB M3 Ultra slip through.

    Tiers (Mac Studio is the only model with model-scoped rules):
      - "target" (urgent):
          * any model whose memory slug is in TARGET_MEMORY_SIZES (256/512), OR
          * a Mac Studio whose memory can't be read from the tile (null or
            unparseable slug) — configurable Ultras hide memory and can be
            256/512GB, so never miss them; flagged `configurable` for product-
            page enrichment, OR
          * a Mac Studio with parseable RAM >= STUDIO_URGENT_MIN_GB (default 128,
            i.e. the maxed-out M4 Max Studio and every Ultra config).
      - "hedge" (default): a Mac Studio with parseable RAM in [HEDGE_MIN_GB,
        STUDIO_URGENT_MIN_GB) — e.g. a 96GB Studio.
    Non-Studio Macs (MacBook Pro etc.) only ever alert via the 256/512 slug rule,
    so 128GB MacBook Pros do NOT trigger urgent alerts.
    """
    tiles = _extract_json_array(html, "tiles") or []
    matches: list[dict] = []
    for t in tiles:
        dims = (t.get("filters") or {}).get("dimensions") or {}
        # dimensions values can be missing OR explicitly null -> coerce to "".
        memory_slug = (dims.get("tsMemorySize") or "").lower()
        model_slug = (dims.get("refurbClearModel") or "").lower()
        title = t.get("title", "").strip()
        is_studio = model_slug == HEDGE_MODEL_SLUG
        mem_gb = _memory_slug_to_gb(memory_slug)
        # A Studio whose memory we can't determine from the tile (null OR an
        # unparseable slug) -> treat as configurable: enrich + never miss.
        configurable = is_studio and mem_gb is None
        if memory_slug in TARGET_MEMORY_SIZES:
            tier = "target"
        elif configurable:
            tier = "target"
        elif is_studio and mem_gb is not None and mem_gb >= STUDIO_URGENT_MIN_GB:
            tier = "target"
        elif is_studio and mem_gb is not None and mem_gb >= HEDGE_MIN_GB:
            tier = "hedge"
        else:
            continue
        url_path = t.get("productDetailsUrl", "")
        price_block = t.get("price") or {}
        # Try currentPrice.amount first (already formatted with currency symbol);
        # fall back to previousPrice.raw_amount for items with a was-price; finally
        # fall back to a bare raw_amount + currency code.
        price_str = ""
        cur = price_block.get("currentPrice") or {}
        prev = price_block.get("previousPrice") or {}
        if cur.get("amount"):
            price_str = cur["amount"]
        elif prev.get("amount"):
            price_str = prev["amount"]
        elif cur.get("raw_amount"):
            price_str = f"{price_block.get('priceCurrency', '')} {cur['raw_amount']}".strip()
        elif prev.get("raw_amount"):
            price_str = f"{price_block.get('priceCurrency', '')} {prev['raw_amount']}".strip()
        matches.append({
            "tier": tier,
            "title": title,
            "url": urljoin(BASE_URL, url_path) if url_path else "",
            "locale": locale,
            "price": price_str,
            "part_number": t.get("partNumber", ""),
            "memory": memory_slug,
            "storage": (dims.get("dimensionCapacity") or ""),
            "model": model_slug,
            "configurable": configurable,
        })
    return matches


_OPTION_MEM_RE = re.compile(r'<option[^>]*\bvalue="(\d+(?:_\d+)?(?:gb|tb))"', re.IGNORECASE)


def fetch_product_memory_options(session: requests.Session, url: str) -> list[str]:
    """Best-effort: read a configurable product page and return its memory option
    slugs (e.g. ["96gb","256gb","512gb"]). Returns [] on any failure — callers
    must NOT gate the alert on this; it only enriches the message.
    """
    if not url:
        return []
    try:
        r = session.get(url, timeout=(FETCH_CONNECT_TIMEOUT, FETCH_READ_TIMEOUT))
        if r.status_code != 200:
            return []
        # Scope strictly to the memory <select> so storage (TB) options don't
        # leak in. The memory select is labelled "dimensionMemory"; read only up
        # to the next </select>.
        html = r.text
        start = html.find("dimensionMemory")
        if start < 0:
            return []
        end = html.find("</select>", start)
        scope = html[start:end] if end >= 0 else html[start:start + 4000]
        opts = [m.lower() for m in _OPTION_MEM_RE.findall(scope)]
        # de-dupe preserving order
        seen, out = set(), []
        for o in opts:
            if o not in seen:
                seen.add(o)
                out.append(o)
        return out
    except requests.RequestException:
        return []


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
            tier = item.get("tier", "target")
            # Configurable Studios hide memory on the tile; read the real options
            # from the product page (best-effort) so the alert is specific.
            options: list[str] = []
            if item.get("configurable"):
                options = fetch_product_memory_options(session, item["url"])
            state["seen"][key] = {
                "first_seen": int(time.time()),
                "title": item["title"],
                "region": name,
                "price": item.get("price", ""),
                "memory": item.get("memory", ""),
                "storage": item.get("storage", ""),
                "model": item.get("model", ""),
                "url": item["url"],
                "tier": tier,
                "configurable": item.get("configurable", False),
                "memory_options": options,
            }
            new_matches += 1
            stor = item.get("storage", "").upper()
            if item.get("configurable"):
                if options:
                    opts_up = "/".join(o.upper().replace("_", ".") for o in options)
                    mem_label = f"configurable: {opts_up}"
                else:
                    mem_label = "configurable (up to 512GB)"
            else:
                mem_label = item.get("memory", "").upper() or "?"
            spec_suffix = f"\nRAM: {mem_label}"
            if stor and not item.get("configurable"):
                spec_suffix += f"  SSD: {stor}"
            price_suffix = f"\nFrom: {item['price']}" if item.get("price") else ""
            if tier == "target":
                push_title = f"MAC STUDIO ULTRA - {name}"
                priority = "urgent"
                tags = ["rotating_light", "shopping_cart"]
            else:
                push_title = f"Studio hedge {mem_label} - {name}"
                priority = "default"
                tags = ["eyes", "shopping_cart"]
            send_ntfy(
                title=push_title,
                message=f"{item['title']}{spec_suffix}{price_suffix}\n{item['url']}",
                priority=priority,
                click=item["url"],
                tags=tags,
            )
            print(f"[{tier.upper()}] {name}: {item['title']} ({mem_label}) -> {item['url']}", flush=True)
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
        f"Watching {len(regions)} storefronts (US, Canada + Europe) for memory sizes: {sorted(TARGET_MEMORY_SIZES)}.",
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
