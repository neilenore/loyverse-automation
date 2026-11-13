#!/usr/bin/env python3
"""
Loyverse -> Telegram shift report  (Python test version of loyverse_shift_report.php)

Same logic as the PHP script, plus test options. No extra packages needed (stdlib only).

Setup:
    export LOYVERSE_TOKEN="..."         # Back Office -> Settings -> Access tokens
    export TELEGRAM_BOT_TOKEN="..."     # only needed with --send / normal run
    export TELEGRAM_CHAT_ID="..."       # -100xxxxxxxxxx or @channelname

Reports only TODAY's closed shifts (Manila time). Meant to run once a day,
near the end of the day (e.g. 23:50), after the last shift has closed.

    python3 loyverse_shift_report.py --dry-run     # preview today's reports, send nothing
    python3 loyverse_shift_report.py --dry-run --raw   # also dump the raw shift JSON
    python3 loyverse_shift_report.py               # send today's reports to Telegram

A shift is only sent once, even if the script runs more than once in a day.
"""

import argparse
import html
import json
import os
import sys
import urllib.parse
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

# ============================== CONFIG ==============================
LOYVERSE_TOKEN     = os.environ.get("LOYVERSE_TOKEN", "PASTE_LOYVERSE_ACCESS_TOKEN")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "PASTE_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "PASTE_CHAT_ID")
TELEGRAM_TOPIC_ID  = os.environ.get("TELEGRAM_TOPIC_ID", "").strip()
TIMEZONE           = "Asia/Manila"
CURRENCY           = "₱"
STATE_FILE         = os.path.join(os.path.dirname(os.path.abspath(__file__)), "shift_report_state.json")
# ====================================================================

LOYVERSE_API = "https://api.loyverse.com/v1.0"
TZ = ZoneInfo(TIMEZONE)


def log(msg):
    print(f"[{datetime.now(TZ):%Y-%m-%d %H:%M:%S}] {msg}", file=sys.stderr)


# ----------------------------- HTTP ------------------------------------

def http_json(method, url, body=None, headers=None):
    headers = dict(headers or {})
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read().decode()
    except urllib.error.HTTPError as e:
        safe = url.replace(TELEGRAM_BOT_TOKEN, "***")
        raise RuntimeError(f"HTTP {e.code} from {safe}: {e.read().decode()[:300]}")
    return json.loads(raw) if raw else {}


def loyverse(path):
    return http_json("GET", LOYVERSE_API + path, headers={
        "Authorization": f"Bearer {LOYVERSE_TOKEN}",
        "Accept": "application/json",
    })


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def parse_dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


# ----------------------------- Loyverse --------------------------------

def fetch_shifts(since_utc):
    shifts, cursor = [], None
    while True:
        q = {"limit": 250, "created_at_min": iso_utc(since_utc)}
        if cursor:
            q["cursor"] = cursor
        res = loyverse("/shifts?" + urllib.parse.urlencode(q))
        shifts += res.get("shifts", [])
        cursor = res.get("cursor")
        if not cursor:
            return shifts


def fetch_payment_types():
    res = loyverse("/payment_types?show_deleted=true")
    return {pt["id"]: pt["name"] for pt in res.get("payment_types", [])}


def lookup_name(path):
    try:
        return loyverse(path).get("name") or "Unknown"
    except Exception as e:
        log(f"Lookup failed for {path}: {e}")
        return "Unknown"


def fetch_catalog():
    """Return category names and item/variant -> category mappings from Loyverse."""
    categories, cursor = {}, None
    while True:
        q = {"limit": 250}
        if cursor:
            q["cursor"] = cursor
        res = loyverse("/categories?" + urllib.parse.urlencode(q))
        for category in res.get("categories", []):
            categories[category.get("id")] = category.get("name") or "Uncategorized"
        cursor = res.get("cursor")
        if not cursor:
            break

    item_category_by_name, item_category_by_variant, item_name_by_variant = {}, {}, {}
    cursor = None
    while True:
        q = {"limit": 250}
        if cursor:
            q["cursor"] = cursor
        res = loyverse("/items?" + urllib.parse.urlencode(q))
        for item in res.get("items", []):
            category_id = item.get("category_id")
            category_name = categories.get(category_id, "Uncategorized")
            item_name = item.get("item_name")
            if item_name:
                item_category_by_name[item_name.casefold()] = category_name
            for variant in item.get("variants", []):
                variant_id = variant.get("variant_id") or variant.get("id")
                if variant_id:
                    item_category_by_variant[variant_id] = category_name
                    if item_name:
                        item_name_by_variant[variant_id] = item_name
        cursor = res.get("cursor")
        if not cursor:
            break

    return categories, item_category_by_name, item_category_by_variant, item_name_by_variant


def sales_summary_today(store_id, start, end, item_category_by_name,
                        item_category_by_variant, item_name_by_variant, limit=3):
    """Aggregate main products and Loyverse categories. Refunds subtract units; cancelled receipts are skipped."""
    totals, category_totals, cursor = {}, {}, None
    while True:
        q = {"store_id": store_id, "created_at_min": iso_utc(start),
             "created_at_max": iso_utc(end), "limit": 250}
        if cursor:
            q["cursor"] = cursor
        res = loyverse("/receipts?" + urllib.parse.urlencode(q))
        for receipt in res.get("receipts", []):
            if receipt.get("cancelled_at"):
                continue
            sign = -1 if receipt.get("receipt_type") == "REFUND" else 1
            for li in receipt.get("line_items", []):
                # Use the catalog's parent item name, not the variant/modifier label.
                variant_id = li.get("variant_id")
                name = item_name_by_variant.get(variant_id) or li.get("item_name") or "Item"
                quantity = sign * float(li.get("quantity") or 0)
                totals[name] = totals.get(name, 0) + quantity
                category = item_category_by_variant.get(variant_id)
                if not category:
                    category = item_category_by_name.get(name.casefold(), "Uncategorized")
                category_totals[category] = category_totals.get(category, 0) + quantity
        cursor = res.get("cursor")
        if not cursor:
            break

    total_items_sold = sum(totals.values())
    ranked = sorted(((n, q) for n, q in totals.items() if q > 0), key=lambda x: (-x[1], x[0].casefold()))
    category_totals = {name: qty for name, qty in category_totals.items() if qty > 0}
    return total_items_sold, sorted(category_totals.items(), key=lambda x: (-x[1], x[0].casefold())), ranked[:limit]


def category_bar_chart(category_totals):
    """Compact text-only bar chart that renders in a normal Telegram message."""
    if not category_totals:
        return []
    max_value = max(float(value) for _, value in category_totals)
    max_label = max(len(name) for name, _ in category_totals)
    lines = []
    for name, value in category_totals:
        blocks = max(1, round(float(value) / max_value * 12))
        bar = "█" * blocks
        lines.append(f"{name:<{max_label}}  {bar:<12} {qty(value)}")
    return lines


# ----------------------------- Report ----------------------------------

def money(v):
    return f"{CURRENCY}{float(v or 0):,.2f}"


def qty(q):
    return f"{q:,.0f}" if float(q).is_integer() else f"{q:,.3f}".rstrip("0").rstrip(".")


def h(s):
    return html.escape(str(s), quote=True)


def build_report(s, store, payment_types, total_items_sold, category_totals, top_items):
    opened = parse_dt(s["opened_at"]).astimezone(TZ)
    closed = parse_dt(s["closed_at"]).astimezone(TZ)
    dur = closed - opened
    days, secs = dur.days, dur.seconds
    dur_txt = f"{secs // 3600}h {secs % 3600 // 60:02d}m" + (f" (+{days}d)" if days else "")

    diff = float(s.get("actual_cash") or 0) - float(s.get("expected_cash") or 0)
    if abs(diff) < 0.005:
        diff_label = "✅ Balanced"
    elif diff > 0:
        diff_label = "🟡 Over " + money(diff)
    else:
        diff_label = "🔴 Short " + money(abs(diff))

    L = [
        f"🧾 <b>SHIFT REPORT</b> — {h(store)}",
        "",
        f"🕐 {opened:%d %b %Y %H:%M} → {closed:%d %b %Y %H:%M}",
        f"⏱ Duration: {dur_txt}",
        "",
        "💰 <b>SALES</b>",
        "Gross sales: " + money(s.get("gross_sales")),
        "Refunds: " + money(s.get("refunds")),
        "Discounts: " + money(s.get("discounts")),
        "<b>Net sales: " + money(s.get("net_sales")) + "</b>",
    ]
    if s.get("tip"):
        L.append("Tips: " + money(s["tip"]))
    if s.get("surcharge"):
        L.append("Surcharge: " + money(s["surcharge"]))

    L += ["", f"📦 <b>TOTAL ITEMS SOLD TODAY: {qty(total_items_sold)}</b>"]

    if category_totals:
        L += ["", "📊 <b>ITEMS SOLD BY CATEGORY</b>", "<pre>"]
        L.extend(h(line) for line in category_bar_chart(category_totals))
        L.append("</pre>")

    if top_items:
        L += ["", "🏆 <b>TOP 3 ITEMS TODAY</b>"]
        for i, (name, q) in enumerate(top_items, 1):
            L.append(f"{i}. {h(name)} — {qty(q)} sold")

    if s.get("payments"):
        L += ["", "💳 <b>PAYMENTS</b>"]
        for p in s["payments"]:
            L.append(f"• {h(payment_types.get(p.get('payment_type_id'), 'Other'))}: {money(p.get('money_amount'))}")

    L += [
        "",
        "💵 <b>CASH DRAWER</b>",
        "Starting cash: " + money(s.get("starting_cash")),
        "Cash payments: " + money(s.get("cash_payments")),
        "Cash refunds: " + money(s.get("cash_refunds")),
        "Paid in: " + money(s.get("paid_in")),
        "Paid out: " + money(s.get("paid_out")),
        "Expected: " + money(s.get("expected_cash")),
        "Actual: " + money(s.get("actual_cash")),
        f"<b>Difference: {diff_label}</b>",
    ]

    if s.get("cash_movements"):
        L += ["", "📝 <b>PAY IN / PAY OUT</b>"]
        for m in s["cash_movements"]:
            sign = "−" if m.get("type") == "PAY_OUT" else "+"
            when = parse_dt(m["created_at"]).astimezone(TZ).strftime("%H:%M") if m.get("created_at") else ""
            line = f"• {when} {sign}{money(m.get('money_amount'))}"
            if m.get("comment"):
                line += " — " + h(m["comment"])
            L.append(line)

    return "\n".join(L)


def to_plain(text):
    """Strip Telegram HTML tags for console preview."""
    return html.unescape(text.replace("<b>", "").replace("</b>", ""))


# ----------------------------- Telegram --------------------------------

def send_telegram(text):
    # Telegram forum topics require message_thread_id in the sendMessage payload.
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    if TELEGRAM_TOPIC_ID:
        try:
            payload["message_thread_id"] = int(TELEGRAM_TOPIC_ID)
        except ValueError:
            raise RuntimeError(
                f"TELEGRAM_TOPIC_ID must be a number; got {TELEGRAM_TOPIC_ID!r}"
            )

    res = http_json(
        "POST",
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        payload,
    )
    if not res.get("ok"):
        raise RuntimeError(f"Telegram error: {res}")

    # Confirm Telegram accepted the message in the requested topic.
    result = res.get("result", {})
    returned_topic = result.get("message_thread_id")
    if TELEGRAM_TOPIC_ID and returned_topic is not None:
        expected_topic = int(TELEGRAM_TOPIC_ID)
        if returned_topic != expected_topic:
            raise RuntimeError(
                f"Telegram sent to topic {returned_topic}, "
                f"but topic {expected_topic} was requested."
            )

    log(
        "Telegram message sent"
        + (f" to topic {TELEGRAM_TOPIC_ID}" if TELEGRAM_TOPIC_ID else " to General")
        + "."
    )


# ----------------------------- State -----------------------------------

def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {"reported": {}}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    os.replace(tmp, STATE_FILE)


def prune(reported):
    cutoff = datetime.now(timezone.utc) - timedelta(days=7)
    return {k: v for k, v in reported.items() if parse_dt(v) > cutoff}


# ----------------------------- Main ------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Loyverse shift report to Telegram")
    ap.add_argument("--dry-run", action="store_true", help="print today's reports, don't send, don't save state")
    ap.add_argument("--raw", action="store_true", help="also print raw shift JSON")
    args = ap.parse_args()

    send = not args.dry_run
    today_start = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    reported = load_state().get("reported", {}) if send else {}
    # Look back 2 days so overnight shifts that opened yesterday but closed today are included
    shifts = fetch_shifts(today_start - timedelta(days=2))
    today_closed = [s for s in shifts if s.get("closed_at") and parse_dt(s["closed_at"]) >= today_start]
    still_open = [s for s in shifts if not s.get("closed_at")]
    to_report = sorted((s for s in today_closed if s["id"] not in reported), key=lambda s: s["closed_at"])

    log(f"Today: {len(today_closed)} closed shift(s), {len(to_report)} to report"
        + (f", {len(still_open)} still open (not reported)" if still_open else "") + ".")
    if not to_report:
        if send:
            save_state({"reported": prune(reported)})
        return

    payment_types = fetch_payment_types()
    _, item_category_by_name, item_category_by_variant, item_name_by_variant = fetch_catalog()
    stores = {}

    for s in to_report:
        store = stores.setdefault(s["store_id"], lookup_name(f"/stores/{s['store_id']}"))
        close_local = parse_dt(s["closed_at"]).astimezone(TZ)
        total_items_sold, category_totals, top = sales_summary_today(
            s["store_id"], today_start, close_local,
            item_category_by_name, item_category_by_variant, item_name_by_variant
        )
        text = build_report(s, store, payment_types, total_items_sold, category_totals, top)

        if args.raw:
            print(json.dumps(s, indent=2))
        print("-" * 40)
        print(to_plain(text))
        print("-" * 40)

        if send:
            send_telegram(text)
            log(f"Sent report for shift {s['id']} ({store}).")
            reported[s["id"]] = s["closed_at"]
            save_state({"reported": reported})

    if send:
        save_state({"reported": prune(reported)})


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log(f"ERROR: {e}")
        sys.exit(1)
