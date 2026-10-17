#!/usr/bin/env python3
"""Dewpoint watch: post a Slack warning when the forecast temperature comes
within N degrees F of the dewpoint in the next 24 hours.

Stdlib only. Meant to run on a schedule (cron / Task Scheduler), e.g. hourly.

Config (env vars):
  SLACK_WEBHOOK_URL  Slack incoming-webhook URL(s), comma-separated to post to
                     several channels (required unless --dry-run)
  ZIP_CODE           Postal code to watch (required)
  SITE_NAME          Label shown in messages, e.g. "Warehouse 3" (optional)
  COUNTRY            Country code for the postal code (default: us)
  SPREAD_F           Alert when temp - dewpoint <= this (default: 5)
  HOURS              Look-ahead window in hours (default: 24)
  REALERT_HOURS      Repeat the warning this often while risk persists (default: 12)
  SEND_ALL_CLEAR     "1" to post a message when the risk goes away (default: 1)
  STATE_FILE         Where to remember alert state (default: next to this script)
"""
import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

UA = {"User-Agent": "dewpoint-bot/1.0"}


def http_json(url, data=None):
    headers = dict(UA)
    if data is not None:
        data = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=20) as resp:
        body = resp.read().decode()
    return json.loads(body) if body.strip().startswith(("{", "[")) else body


def geocode_zip(zip_code, country):
    """Postal code -> (lat, lon, place name) via zippopotam.us (free, no key)."""
    d = http_json(f"https://api.zippopotam.us/{country}/{urllib.parse.quote(zip_code)}")
    p = d["places"][0]
    name = f"{p['place name']}, {p.get('state abbreviation') or p.get('state', '')}".strip(", ")
    return float(p["latitude"]), float(p["longitude"]), name


def hourly_forecast(lat, lon, hours):
    """Hourly temp/dewpoint in F from Open-Meteo (free, no key), local time."""
    q = urllib.parse.urlencode({
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,dew_point_2m,relative_humidity_2m",
        "temperature_unit": "fahrenheit",
        "timezone": "auto",
        "forecast_hours": hours,
    })
    d = http_json(f"https://api.open-meteo.com/v1/forecast?{q}")
    h = d["hourly"]
    rows = []
    for t, temp, dew, rh in zip(h["time"], h["temperature_2m"],
                                h["dew_point_2m"], h["relative_humidity_2m"]):
        if temp is None or dew is None:
            continue
        rows.append({"time": datetime.fromisoformat(t), "temp": temp,
                     "dew": dew, "rh": rh, "spread": round(temp - dew, 1)})
    return rows, d.get("timezone_abbreviation", "")


def risk_windows(rows, spread_f):
    """Group consecutive at-risk hours into windows."""
    windows, cur = [], None
    for r in rows:
        if r["spread"] <= spread_f:
            if cur is None:
                cur = {"start": r["time"], "end": r["time"], "worst": r}
            else:
                cur["end"] = r["time"]
                if r["spread"] < cur["worst"]["spread"]:
                    cur["worst"] = r
        elif cur is not None:
            windows.append(cur)
            cur = None
    if cur is not None:
        windows.append(cur)
    return windows


def fmt_time(dt):
    return dt.strftime("%a %-I%p") if os.name != "nt" else dt.strftime("%a %#I%p")


def describe_gap(spread):
    """How temp relates to dewpoint, in words: above, at, or below."""
    if spread < -0.5:
        return f"*{-spread:.1f}°F below dewpoint — condensation likely*"
    if spread <= 0.5:
        return "*at dewpoint — condensation/fog likely*"
    return f"{spread:.1f}°F above dewpoint"


def build_alert(label, windows, spread_f, hours, tz):
    lines = [f":droplet: *Dewpoint warning for {label}*",
             f"Forecast temperature is below, at, or within {spread_f:g}°F above the dewpoint "
             f"in the next {hours}h:"]
    for w in windows:
        end = w["end"] + timedelta(hours=1)
        wr = w["worst"]
        lines.append(
            f"• {fmt_time(w['start'])}–{fmt_time(end)} {tz}: worst {fmt_time(wr['time'])}, "
            f"temp {wr['temp']:.0f}°F / dewpoint {wr['dew']:.0f}°F — "
            f"{describe_gap(wr['spread'])} (RH {wr['rh']}%)")
    return "\n".join(lines)


def post_all(webhooks, msg):
    """Post to every webhook; return how many failed. URLs are never logged (they're secrets)."""
    failed = 0
    for i, url in enumerate(webhooks, 1):
        try:
            http_json(url, {"text": msg})
        except Exception as e:  # keep going so the other channels still get it
            failed += 1
            print(f"Slack post to webhook #{i} failed: {e}", file=sys.stderr)
    return failed


def load_state(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    Path(path).write_text(json.dumps(state, indent=2))


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--dry-run", action="store_true", help="print instead of posting to Slack")
    ap.add_argument("--force", action="store_true", help="ignore alert state and always post if at risk")
    ap.add_argument("--test", action="store_true", help="post a setup-check message to every webhook and exit")
    args = ap.parse_args()

    zip_code = os.environ.get("ZIP_CODE")
    webhooks = [u.strip() for u in os.environ.get("SLACK_WEBHOOK_URL", "").split(",") if u.strip()]
    if not zip_code or (not webhooks and not args.dry_run):
        sys.exit("Set ZIP_CODE and SLACK_WEBHOOK_URL (or use --dry-run).")
    country = os.environ.get("COUNTRY", "us").lower()
    spread_f = float(os.environ.get("SPREAD_F", "5"))
    hours = int(os.environ.get("HOURS", "24"))
    realert_h = float(os.environ.get("REALERT_HOURS", "12"))
    all_clear = os.environ.get("SEND_ALL_CLEAR", "1") == "1"
    state_file = os.environ.get("STATE_FILE", str(Path(__file__).with_name("dewpoint_state.json")))

    lat, lon, place = geocode_zip(zip_code, country)
    site = os.environ.get("SITE_NAME", "").strip()
    label = f"{site} — {place} ({zip_code})" if site else f"{place} ({zip_code})"
    rows, tz = hourly_forecast(lat, lon, hours)
    windows = risk_windows(rows, spread_f)

    if args.test:
        # Leaves alert state alone, so a test post can't suppress or trigger real alerts.
        msg = (f":wave: Dewpoint bot for {label} is set up and will post here when the "
               f"temp/dewpoint spread is forecast to be {spread_f:g}°F or less within {hours}h.")
        failed = post_all(webhooks, msg)
        sys.exit(1 if failed else 0)

    state = load_state(state_file)
    now = datetime.now(timezone.utc)
    last = state.get("last_alert")
    stale = last is None or now - datetime.fromisoformat(last) >= timedelta(hours=realert_h)

    msg = None
    if windows:
        if args.force or not state.get("alerting") or stale:
            msg = build_alert(label, windows, spread_f, hours, tz)
            state.update(alerting=True, last_alert=now.isoformat())
    elif state.get("alerting"):
        if all_clear:
            min_spread = min(r["spread"] for r in rows) if rows else None
            msg = (f":white_check_mark: Dewpoint risk cleared for {label}. "
                   f"Smallest temp/dewpoint spread in the next {hours}h is {min_spread:.1f}°F.")
        state.update(alerting=False, last_alert=None)

    if args.dry_run:
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles choke on °
        closest = min(rows, key=lambda r: r["spread"]) if rows else None
        print(f"{label} ({lat:.3f},{lon:.3f}) — {len(rows)} hourly points, "
              f"{len(windows)} risk window(s).")
        if closest:
            print(f"Closest approach: {closest['time']} temp {closest['temp']}°F "
                  f"dew {closest['dew']}°F spread {closest['spread']}°F")
        print("Would post:\n" + msg if msg else "Nothing to post.")
        return

    failed = post_all(webhooks, msg) if msg else 0
    if msg and failed == len(webhooks):
        sys.exit("All Slack posts failed; state not saved so the alert retries next run.")
    save_state(state_file, state)
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
