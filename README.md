# Apple Refurb Monitor

Polls Apple's refurbished Mac Studio listings across European storefronts and pushes an
**urgent ntfy alert** the moment a Mac Studio **Ultra** with **256GB** or **512GB** RAM appears.

## What it does NOT do
**No auto-purchase.** Apple's ToS forbids automated ordering and they actively detect it
(device fingerprinting, behavioural signals, Cloudflare/Akamai). A push notification to your
phone gets you to checkout in ~30s, which is competitive for items at this price point and
volume. The risk/reward of auto-checkout is bad: getting flagged means order cancellation
and Apple ID restrictions.

## Regions watched
UK, DE, FR, NL, IT, ES, IE, AT, BE (FR+NL), CH (DE+FR), PL — the 13 European storefronts
Apple currently runs a refurb store in. Confirmed live as of 2026-05; nordics, PT, CZ, HU, GR
and LU have no refurb store. 404s are silently skipped if Apple removes one.

## How it matches
The Apple refurb category page embeds a `"tiles":[...]` JSON blob with each product's full
title, deep URL, price, and **structured `filters.dimensions`** including `refurbClearModel`
(e.g. `macstudio`) and `tsMemorySize` (e.g. `256gb`). We match on those structured fields,
so it works across all locales — no fragile title-string parsing in German/French/etc.

256GB and 512GB RAM are only offered on the **M3 Ultra Mac Studio**, so the memory filter
alone implies "Ultra" — but matching also requires `refurbClearModel == "macstudio"` for
safety.

## Pick an ntfy topic
ntfy topics are public-but-unguessable. Pick something long and random (anyone with the
topic name can read your alerts):

```
echo "justin-apple-refurb-$(openssl rand -hex 4)"
```

Then install the **ntfy** app on your phone (iOS / Android), and **subscribe to that topic**.

## Deploy to Raspberry Pi

```bash
# 1. Copy files
scp apple_refurb_monitor.py requirements.txt apple-refurb-monitor.service justin@<pi>:/tmp/

# 2. On the Pi:
ssh justin@<pi>
sudo mkdir -p /opt/apple-refurb-monitor
sudo mv /tmp/apple_refurb_monitor.py /opt/apple-refurb-monitor/
sudo mv /tmp/requirements.txt /opt/apple-refurb-monitor/
sudo python3 -m venv /opt/apple-refurb-monitor/venv
sudo /opt/apple-refurb-monitor/venv/bin/pip install -r /opt/apple-refurb-monitor/requirements.txt
sudo chown -R justin:justin /opt/apple-refurb-monitor

# 3. Env file (replace the topic):
sudo tee /etc/apple-refurb-monitor.env >/dev/null <<'EOF'
NTFY_TOPIC=justin-apple-refurb-CHANGEME
POLL_INTERVAL_SEC=60
EOF
sudo chmod 640 /etc/apple-refurb-monitor.env

# 4. Install systemd unit
sudo mv /tmp/apple-refurb-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now apple-refurb-monitor.service

# 5. Tail logs
journalctl -u apple-refurb-monitor -f
```

You should immediately get a low-priority "monitor started" alert on your phone, confirming
the topic is wired up.

## Tuning
- `POLL_INTERVAL_SEC=30` for faster reaction (still well within polite limits at ~22 req/30s).
- `REGIONS=uk,de,fr` to narrow scope.
- `HEARTBEAT_HOURS=12` for more frequent heartbeats.

## Test the alert path locally
```bash
NTFY_TOPIC=your-topic POLL_INTERVAL_SEC=60 STATE_PATH=./seen.json \
  python3 apple_refurb_monitor.py
```

## Notes on Apple's pages
The script parses the static HTML of `/{locale}/shop/refurbished/mac/mac-studio`, looking
for product anchors and the words "Mac Studio" + "Ultra" + (256GB|512GB). If Apple changes
their page structure, the matcher in `parse_listings()` may need updating. The check is
intentionally tolerant — title-string based — so cosmetic markup changes won't break it.
