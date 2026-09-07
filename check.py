#!/usr/bin/env python3
"""Watch a .UZ domain in the ccTLD.uz registry and report to Telegram.

One check per run. Sends an immediate alert when the registry state changes
(renewed / moved to auction / released), and one summary per day at 08:00
Asia/Tashkent. State lives in state.json next to this file.
"""

import argparse
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone

DOMAIN = os.environ.get("WATCH_DOMAIN", "rank.uz")
WHOIS_URL = "https://cctld.uz/whois/?domain={d}&lang=eng"
AUCTION_URL = "https://domain.uzex.uz/"
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json")

TASHKENT = timezone(timedelta(hours=5))          # UTC+5, no DST
REPORT_HOUR = int(os.environ.get("REPORT_HOUR", "8"))
FAIL_ALERT_AFTER = 4                              # consecutive failures before shouting

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


class ParseError(RuntimeError):
    """The page loaded but did not look like a registry whois response."""


# --------------------------------------------------------------------------
# fetch + parse
# --------------------------------------------------------------------------

def fetch(url, tries=4, timeout=30):
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA, "Accept-Language": "en,ru;q=0.8"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", "replace")
        except Exception as exc:                   # noqa: BLE001 - retry anything
            last = exc
            if attempt < tries - 1:
                time.sleep(3 * (attempt + 1))
    raise last


RE_CODE = re.compile(r"statusd/\?lnk=([A-Z_]+)")
RE_STATUS = re.compile(r"Status:[\s\S]{0,120}?<td[^>]*>([\s\S]{0,300}?)</td>")
RE_REGISTRAR = re.compile(r"Registrar:[\s\S]{0,150}?<td[^>]*>([\s\S]{0,300}?)</td>")
RE_TAGS = re.compile(r"<[^>]+>")


def clean(value):
    """Cell markup -> plain text. The registrar sits inside an <a>, so tags must
    be stripped before entities are decoded: 'OOO &laquo;UNITEL&raquo;'."""
    if not value:
        return None
    text = html.unescape(RE_TAGS.sub(" ", value)).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip() or None
RE_CREATED = re.compile(r"Date created:[\s\S]{0,150}?(\d{2}\.\d{2}\.\d{4})")
RE_UNTIL = re.compile(r"Active until:[\s\S]{0,150}?(\d{2}\.\d{2}\.\d{4})")
RE_NS = re.compile(r"Domain:\s*([a-z0-9.\-]+)\.,\s*IP:\s*([0-9.]+)", re.I)


def parse(page_html, domain):
    """Turn the whois page into a comparable snapshot. Raises ParseError if the
    page is not recognisable - never guess, a wrong guess means a false alarm."""
    not_found = re.search(
        r"Domain\s*<b>\s*" + re.escape(domain) + r"\s*</b>\s*is not found",
        page_html, re.I) is not None

    code_m = RE_CODE.search(page_html)
    status_m = RE_STATUS.search(page_html)

    if not not_found and not (code_m or status_m):
        raise ParseError("neither a status table nor an 'is not found' notice")

    if not_found:
        return {
            "state": "free",
            "status_text": "Free (not in registry)",
            "status_code": None,
            "registrar": None,
            "created": None,
            "expires": None,
            "ns": [],
        }

    status_text = (clean(status_m.group(1)) or "") if status_m else ""
    reg_m = RE_REGISTRAR.search(page_html)
    created_m = RE_CREATED.search(page_html)
    until_m = RE_UNTIL.search(page_html)

    return {
        "state": classify(status_text, code_m.group(1) if code_m else None),
        "status_text": status_text,
        "status_code": code_m.group(1) if code_m else None,
        "registrar": clean(reg_m.group(1)) if reg_m else None,
        "created": created_m.group(1) if created_m else None,
        "expires": until_m.group(1) if until_m else None,
        "ns": sorted("{} ({})".format(n, ip) for n, ip in RE_NS.findall(page_html)),
    }


def classify(status_text, code):
    t = (status_text or "").lower()
    c = (code or "").upper()
    if "redemption" in t or c == "W_RED":
        return "redemption"
    if "auction" in t or "auksion" in t:
        return "auction"
    if "active" in t or c == "ACTIV":
        return "active"
    if "pending" in t or "waiting" in t:
        return "pending_renewal"
    if "free" in t:
        return "free"
    if "reserv" in t:
        return "reserved"
    if "cancel" in t or "deactiv" in t:
        return "blocked"
    return "other"


# --------------------------------------------------------------------------
# telegram
# --------------------------------------------------------------------------

def telegram(text):
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token or not chat:
        print("!! TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID not set - printing instead:")
        print(text)
        return False
    payload = urllib.parse.urlencode({
        "chat_id": chat,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
    }).encode()
    url = "https://api.telegram.org/bot{}/sendMessage".format(token)
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, data=payload)
            with urllib.request.urlopen(req, timeout=30) as resp:
                json.load(resp)
            return True
        except Exception as exc:                   # noqa: BLE001
            print("telegram attempt {} failed: {}".format(attempt + 1, exc))
            if attempt < 3:
                time.sleep(3 * (attempt + 1))
    return False


def esc(value):
    return html.escape(str(value)) if value is not None else "-"


# --------------------------------------------------------------------------
# timeline helpers
# --------------------------------------------------------------------------

def parse_dmy(value):
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except (TypeError, ValueError):
        return None


def estimate_free_date(expires):
    """expiry + 7 working days (pending renewal) + 30 calendar days (redemption)."""
    exp = parse_dmy(expires)
    if not exp:
        return None
    day, counted = exp, 0
    while counted < 7:
        day += timedelta(days=1)
        if day.weekday() < 5:
            counted += 1
    return day + timedelta(days=30)


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False, sort_keys=True)
        fh.write("\n")


def diff(old, new):
    """Human-readable list of what moved between two snapshots."""
    labels = {
        "state": "State",
        "status_text": "Registry status",
        "expires": "Expiry date",
        "registrar": "Registrar",
        "ns": "Nameservers",
    }
    out = []
    for key, label in labels.items():
        before, after = old.get(key), new.get(key)
        if before == after:
            continue
        if key == "ns":
            before = ", ".join(before or []) or "none"
            after = ", ".join(after or []) or "none"
        out.append("{}: {} -> {}".format(label, before or "-", after or "-"))
    return out


MEANING = {
    "free": ("DOMAIN IS FREE", "It left the registry - register it through a .UZ "
             "registrar right now, or check the auction."),
    "auction": ("DOMAIN WENT TO AUCTION", "Bid on it at domain.uzex.uz. You need a "
                "funded UZEX account to place a bid."),
    "active": ("OWNER RENEWED IT", "The current holder paid. It is off the market "
               "again until the new expiry date."),
    "redemption": ("Back in redemption", "Still recoverable by the current owner."),
    "pending_renewal": ("Pending renewal", "Grace window before redemption."),
    "reserved": ("Reserved by the registry", "Not available to the public."),
    "blocked": ("Deactivated / cancelled", "Held by the registry over a dispute."),
}


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------

def change_message(domain, old, new, changes, now):
    headline, advice = MEANING.get(
        new["state"], ("Registry record changed", "Check the details below."))
    lines = [
        "<b>&#9889; {} - {}</b>".format(esc(domain.upper()), esc(headline)),
        "",
        esc(advice),
        "",
        "<b>What changed</b>",
    ]
    lines += ["- " + esc(c) for c in changes]
    lines += [
        "",
        "<b>Now</b>",
        "Status: <b>{}</b>".format(esc(new.get("status_text"))),
        "Expiry: {}".format(esc(new.get("expires"))),
        "Registrar: {}".format(esc(new.get("registrar"))),
        "",
        '<a href="https://cctld.uz/whois/?domain={}&lang=eng">whois</a> | '
        '<a href="{}">auction</a>'.format(urllib.parse.quote(domain), AUCTION_URL),
        "<i>{}</i>".format(now.strftime("%d.%m.%Y %H:%M Tashkent")),
    ]
    return "\n".join(lines)


def daily_message(domain, snap, state, now):
    checks = state.get("checks_since_report", 0)
    fails = state.get("failures_since_report", 0)
    changes = [c for c in state.get("changes", [])
               if c.get("at", "") >= state.get("last_report_at", "")] \
        if state.get("last_report_at") else list(state.get("changes", []))

    lines = ["<b>{} - daily report</b>".format(esc(domain)),
             "<i>{}</i>".format(now.strftime("%d.%m.%Y %H:%M Tashkent")), ""]

    if changes:
        lines.append("<b>&#9888; {} change(s) in the last 24h</b>".format(len(changes)))
        for entry in changes[-6:]:
            lines.append("- {}: {}".format(esc(entry.get("at", "")[:16]),
                                           esc("; ".join(entry.get("changes", [])))))
    else:
        lines.append("<b>Nothing changed.</b>")

    lines += ["",
              "Status: <b>{}</b>".format(esc(snap.get("status_text"))),
              "Expiry: {}".format(esc(snap.get("expires")))]

    if snap.get("state") == "redemption":
        free_on = estimate_free_date(snap.get("expires"))
        if free_on:
            left = (free_on - now.date()).days
            lines.append("Expected to drop: <b>{}</b> ({})".format(
                free_on.strftime("%d.%m.%Y"),
                "in ~{} days".format(left) if left > 0 else "any day now"))

    lines += ["",
              "Checks since last report: {}".format(checks),
              "Failed checks: {}".format(fails),
              "Total checks: {}".format(state.get("checks_total", 0)),
              "",
              '<a href="https://cctld.uz/whois/?domain={}&lang=eng">whois</a> | '
              '<a href="{}">auction</a>'.format(urllib.parse.quote(domain), AUCTION_URL)]
    return "\n".join(lines)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Watch a .UZ domain, alert on Telegram.")
    ap.add_argument("--daily", action="store_true", help="force the daily report")
    ap.add_argument("--test", action="store_true", help="send a test Telegram message")
    ap.add_argument("--selftest", action="store_true", help="run parser tests offline")
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    now = datetime.now(TASHKENT)
    state = load_state()
    state.setdefault("domain", DOMAIN)
    state.setdefault("changes", [])
    state.setdefault("started_at", now.isoformat(timespec="seconds"))

    if args.test:
        ok = telegram("<b>{}</b> monitor test message - {}".format(
            esc(DOMAIN), now.strftime("%d.%m.%Y %H:%M")))
        print("test sent:", ok)
        return 0

    # ---- check ----------------------------------------------------------
    state["checks_total"] = state.get("checks_total", 0) + 1
    state["checks_since_report"] = state.get("checks_since_report", 0) + 1
    state["last_check_at"] = now.isoformat(timespec="seconds")

    snapshot, error = None, None
    try:
        snapshot = parse(fetch(WHOIS_URL.format(d=urllib.parse.quote(DOMAIN))), DOMAIN)
    except Exception as exc:                       # noqa: BLE001
        error = "{}: {}".format(type(exc).__name__, exc)

    if error:
        state["failures_since_report"] = state.get("failures_since_report", 0) + 1
        state["consecutive_failures"] = state.get("consecutive_failures", 0) + 1
        state["last_error"] = error
        print("check FAILED:", error)
        if state["consecutive_failures"] == FAIL_ALERT_AFTER:
            telegram("<b>&#9888; {} monitor is failing</b>\n\n{} checks in a row could "
                     "not read the registry.\nLast error: {}\n\nSilence from now on does "
                     "NOT mean nothing changed - check manually.".format(
                         esc(DOMAIN), FAIL_ALERT_AFTER, esc(error)))
    else:
        recovered = state.get("consecutive_failures", 0) >= FAIL_ALERT_AFTER
        state["consecutive_failures"] = 0
        state.pop("last_error", None)
        previous = state.get("snapshot")
        print("check OK:", snapshot["state"], "|", snapshot["status_text"])

        changes = [] if previous is None else diff(previous, snapshot)

        if previous is None:
            delivered = telegram(
                "<b>&#128065; Watching {}</b>\n\nStatus: <b>{}</b>\nExpiry: {}\n"
                "Registrar: {}\n\nYou will get an instant alert on any change and "
                "a summary every day at {:02d}:00.".format(
                    esc(DOMAIN), esc(snapshot["status_text"]),
                    esc(snapshot["expires"]), esc(snapshot["registrar"]), REPORT_HOUR))
        elif changes:
            print("CHANGE DETECTED:", changes)
            delivered = telegram(change_message(DOMAIN, previous, snapshot, changes, now))
        else:
            delivered = True
            if recovered:
                telegram("<b>&#9989; {} monitor is back online</b>\n\nStatus is still "
                         "<b>{}</b>.".format(esc(DOMAIN), esc(snapshot["status_text"])))

        # Only move the baseline forward once the news is actually out. If Telegram
        # is down, keep the old snapshot so the next run re-detects and re-sends -
        # otherwise a failed send would bury the one alert that matters.
        if delivered:
            if changes:
                state["changes"].append({
                    "at": now.isoformat(timespec="seconds"),
                    "from": previous.get("state"),
                    "to": snapshot.get("state"),
                    "changes": changes,
                })
                state["changes"] = state["changes"][-100:]
            state["snapshot"] = snapshot
        else:
            print("alert NOT delivered - keeping the old snapshot so the next run retries")

    # ---- daily report ---------------------------------------------------
    today = now.date().isoformat()
    due = args.daily or (now.hour >= REPORT_HOUR and state.get("last_report_date") != today)
    if due and state.get("snapshot"):
        if telegram(daily_message(DOMAIN, state["snapshot"], state, now)):
            state["last_report_date"] = today
            state["last_report_at"] = now.isoformat(timespec="seconds")
            state["checks_since_report"] = 0
            state["failures_since_report"] = 0
            print("daily report sent")

    save_state(state)
    return 0


# --------------------------------------------------------------------------
# offline parser tests against markup captured from the live registry
# --------------------------------------------------------------------------

REDEMPTION_HTML = """
<td>&nbsp;<strong>Domain:&nbsp;</strong></td>
<td colspan="3">&nbsp;<a href="http://rank.uz">rank.uz</a>&nbsp;</td>
<td>&nbsp;<strong>Status:&nbsp;</strong></td>
<td colspan="3">&nbsp;Redemption period&nbsp;<a href="/info/statusd/?lnk=W_RED"
target="_blank"><img src="/images/quest.png"></a></td>
<td>&nbsp;<strong>Registrar:&nbsp;</strong></td>
<td colspan="3">&nbsp;<a href="/reg/?id=7">OOO &laquo;UNITEL&raquo;</a></td>
<td>&nbsp;<strong>First NS:&nbsp;</strong></td>
<td colspan="3">&nbsp;Domain: ns3.beeline.uz., IP: 37.110.209.213</td>
<td>&nbsp;<strong>Second NS:&nbsp;</strong></td>
<td colspan="3">&nbsp;Domain: ns4.beeline.uz., IP: 37.110.209.216</td>
<td>&nbsp;<strong>Date created:&nbsp;</strong></td><td colspan="3">&nbsp;13.08.2019 &#1075;.</td>
<td>&nbsp;<strong>Active until:&nbsp;</strong></td><td colspan="3">&nbsp;13.08.2026 &#1075;.</td>
"""

ACTIVE_HTML = REDEMPTION_HTML.replace(
    "Redemption period&nbsp;<a href=\"/info/statusd/?lnk=W_RED\"",
    "Active&nbsp;<a href=\"/info/statusd/?lnk=ACTIV\"").replace(
    "13.08.2026", "13.08.2027")

AUCTION_HTML = REDEMPTION_HTML.replace(
    "Redemption period&nbsp;<a href=\"/info/statusd/?lnk=W_RED\"",
    "Auction&nbsp;<a href=\"/info/statusd/?lnk=W_AUC\"")

FREE_HTML = ('<p style="margin-left:30">Domain <b>rank.uz</b> is not found</p>'
             '<p>You can register the domain through the official registrars.</p>')

GARBAGE_HTML = "<html><body><h1>502 Bad Gateway</h1></body></html>"


def selftest():
    failures = []

    def check(label, got, want):
        if got != want:
            failures.append("{}: got {!r}, want {!r}".format(label, got, want))

    red = parse(REDEMPTION_HTML, "rank.uz")
    check("redemption.state", red["state"], "redemption")
    check("redemption.status", red["status_text"], "Redemption period")
    check("redemption.registrar", red["registrar"], "OOO \u00abUNITEL\u00bb")
    check("redemption.code", red["status_code"], "W_RED")
    check("redemption.expires", red["expires"], "13.08.2026")
    check("redemption.created", red["created"], "13.08.2019")
    check("redemption.ns", red["ns"],
          ["ns3.beeline.uz (37.110.209.213)", "ns4.beeline.uz (37.110.209.216)"])

    act = parse(ACTIVE_HTML, "rank.uz")
    check("active.state", act["state"], "active")
    check("active.expires", act["expires"], "13.08.2027")

    auc = parse(AUCTION_HTML, "rank.uz")
    check("auction.state", auc["state"], "auction")

    free = parse(FREE_HTML, "rank.uz")
    check("free.state", free["state"], "free")
    check("free.expires", free["expires"], None)

    try:
        parse(GARBAGE_HTML, "rank.uz")
        failures.append("garbage: expected ParseError, got a snapshot")
    except ParseError:
        pass

    # a broken page must never be mistaken for a change
    check("diff redemption->redemption", diff(red, parse(REDEMPTION_HTML, "rank.uz")), [])
    d = diff(red, act)
    check("diff redemption->active", sorted(x.split(":")[0] for x in d),
          ["Expiry date", "Registry status", "State"])
    check("free date", estimate_free_date("13.08.2026"), date(2026, 9, 23))
    check("free date none", estimate_free_date(None), None)

    for f in failures:
        print("FAIL", f)
    print("selftest: {} checks failed".format(len(failures)) if failures
          else "selftest: all passed")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
