#!/usr/bin/env python3
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

# ---------- Konfiguration ----------
URL = "https://pr-underworld.com/website/"
MONSTERCOUNT_URL = "https://pr-underworld.com/website/monstercount/"
GUILD_NAME = "beQuiet"
SERVER_LABEL = "Netherworld"

STATE_FILE   = Path("state_last_seen.json")
MEMBERS_FILE = Path("bequiet_members.txt")
TIMEOUT = 20

# Webhook-Varianten akzeptieren
WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", os.getenv("DISCORD_WEBHOOK_URL_LASTSEEN", "")).strip()
MODE = os.getenv("MODE", "auto").strip().lower()  # auto | hourly | daily
FORCE_POST = os.getenv("FORCE_POST", "").strip()  # "1" bei Testlauf

# ---------- State ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"last_seen": {}, "last_status": {}, "last_daily_date": ""}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- Mitgliederliste ----------
def load_members() -> list[str]:
    if not MEMBERS_FILE.exists():
        return []
    text = MEMBERS_FILE.read_text(encoding="utf-8")
    names = [line.strip() for line in text.splitlines()]
    names = [n for n in names if n]
    seen, uniq = set(), []
    for n in names:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq

def save_members(names: list[str]):
    uniq_sorted = sorted(set(n.strip() for n in names if n.strip()), key=str.lower)
    MEMBERS_FILE.write_text("\n".join(uniq_sorted) + ("\n" if uniq_sorted else ""), encoding="utf-8")

# ---------- Discord ----------
def post_to_discord(content: str) -> bool:
    if not WEBHOOK:
        print("Skipping post because DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        return False
    if len(content) > 2000:
        print(f"Discord payload blocked locally length={len(content)}", file=sys.stderr)
        return False
    try:
        r = requests.post(WEBHOOK, json={"content": content}, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        resp_txt = ""
        try:
            resp_txt = r.text  # type: ignore[name-defined]
        except Exception:
            pass
        print(f"Discord error: {e} {resp_txt}", file=sys.stderr)
        return False

def chunk_text(content: str, limit: int = 1900) -> list[str]:
    if not content:
        return []
    chunks = []
    remaining = content
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut_at = limit
        nl = remaining.rfind("\n", 0, limit + 1)
        if nl != -1 and nl >= int(limit * 0.6):
            cut_at = nl + 1
        chunk = remaining[:cut_at].rstrip("\n")
        chunks.append(chunk)
        remaining = remaining[cut_at:]
        if remaining.startswith("\n"):
            remaining = remaining[1:]
    return chunks

def post_long_to_discord(content: str, limit: int = 1900, with_counters: bool = True) -> bool:
    if not WEBHOOK:
        print("Skipping post because DISCORD_WEBHOOK_URL is not set", file=sys.stderr)
        return False
    chunks = chunk_text(content, limit=limit)
    if not chunks:
        return False
    total = len(chunks)
    ok = True
    for i, c in enumerate(chunks, start=1):
        payload = c
        if with_counters and total > 1:
            suffix = f"\n\nTeil {i}/{total}"
            if len(payload) + len(suffix) > limit:
                payload = payload[: limit - len(suffix)]
            payload = payload + suffix
        ok = post_to_discord(payload) and ok
    return ok

# ---------- Zeit ----------
BERLIN = ZoneInfo("Europe/Berlin")

def now_utc():
    return datetime.now(timezone.utc)

def now_berlin():
    return now_utc().astimezone(BERLIN)

def today_berlin_date():
    return now_berlin().date().isoformat()

def is_berlin_daily_window(dt: datetime) -> bool:
    # Daily zwischen 23:20 und 23:59 Europe/Berlin
    return dt.hour == 23 and 20 <= dt.minute <= 59

# ---------- HTTP ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "beQuiet last-seen tracker"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

# ---------- Parsing: Homepage Online-Liste ----------
def find_home_server_table(soup: BeautifulSoup, server_label: str):
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and server_label.lower() in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

def parse_home_bequiet_rows(table) -> list[dict]:
    rows = []
    tbody = table.find("tbody")
    if not tbody:
        return rows
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 2:
            continue
        name = tds[0].get_text(strip=True)
        guild = tds[1].get_text(strip=True)
        if not name:
            continue
        if guild and GUILD_NAME.lower() in guild.lower():
            rows.append({"name": name})
    return rows

# ---------- Parsing: Monstercount ----------
def find_monstercount_table(soup: BeautifulSoup, server_label: str):
    """
    Auf der Seite stehen zwei Blöcke mit h4-Überschriften.
    Unter der Überschrift folgt eine Tabelle. Wir wählen die mit 'Netherworld'.
    """
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and server_label.lower() in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

def parse_monstercount_names(table) -> list[str]:
    """
    tbody -> tr
      th scope=row  ignorieren
      td[0] = Name
      td[1] = Zahl
    """
    names = []
    tbody = table.find("tbody")
    if not tbody:
        return names
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 1:
            name = tds[0].get_text(strip=True)
            if name:
                names.append(name)
    return names

# ---------- Formatierung ----------
def human_delta(seconds: int) -> str:
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    d, h = divmod(h, 24)
    if d > 0:
        return f"{d}d {h}h"
    if h > 0:
        return f"{h}h {m}m"
    if m > 0:
        return f"{m}m"
    return f"{s}s"

def fmt_ts_utc(ts: int) -> str:
    dt = datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(BERLIN)
    return dt.strftime("%Y-%m-%d %H:%M")

# ---------- Hourly ----------
def run_hourly():
    member_names = set(load_members())

    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return

    beq_rows = parse_home_bequiet_rows(table)
    currently_online = {row["name"] for row in beq_rows}

    newly_found = sorted(currently_online - member_names, key=str.lower)
    if newly_found:
        updated = sorted(member_names | set(newly_found), key=str.lower)
        save_members(updated)
        member_names = set(updated)
        print(f"Added to members list: {', '.join(newly_found)}")

    state = load_state()
    last_seen = state.setdefault("last_seen", {})
    last_status = state.setdefault("last_status", {})
    now_ts = int(time.time())

    for name in member_names:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "offline")

    for name in currently_online:
        last_seen[name] = now_ts
        last_status[name] = "online"

    for name in member_names - currently_online:
        last_status[name] = "offline"

    save_state(state)

# ---------- Daily + Monstercount ----------
def build_daily_text(member_names: set[str],
                     all_names: set[str],
                     current_online: set[str],
                     last_seen: dict[str, int],
                     mc_today: set[str],
                     test_label: bool) -> str:
    def sort_key(n):
        online = 1 if n in current_online else 0
        return (-online, -last_seen.get(n, 0), n.lower())

    header = f"**Netherworld – beQuiet last seen** ({today_berlin_date()})"
    if test_label:
        header += " Test"

    lines = []
    for name in sorted(all_names, key=sort_key):
        if name in current_online:
            lines.append(f"• **{name}** — currently online and grinding")
        else:
            ts = last_seen.get(name, 0)
            if name in mc_today and ts == 0:
                # Fallback-Info wenn noch nie gesehen wurde
                lines.append(f"• **{name}** — seen today via Monstercount")
            elif name in mc_today and ts > 0:
                # Gesehen über Monstercount, Zeitstempel bereits auf heute gesetzt
                delta = int(time.time()) - ts
                lines.append(f"• **{name}** — seen today via Monstercount ({human_delta(delta)})")
            else:
                if ts > 0:
                    delta = int(time.time()) - ts
                    lines.append(f"• **{name}** — last seen {fmt_ts_utc(ts)} ({human_delta(delta)})")
                else:
                    lines.append(f"• **{name}** — no sightings yet")

    return header + "\n" + "\n".join(lines)

def run_daily_summary(update_state_date: bool, test_label: bool):
    # Mitglieder laden
    member_names = set(load_members())

    # Homepage lesen
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return
    beq_rows = parse_home_bequiet_rows(table)
    names_today = {r["name"] for r in beq_rows}
    current_online = set(names_today)

    # Monstercount lesen
    try:
        mc_html = fetch_html(MONSTERCOUNT_URL)
        mc_soup = BeautifulSoup(mc_html, "html.parser")
        mc_table = find_monstercount_table(mc_soup, SERVER_LABEL)
        mc_names = set(parse_monstercount_names(mc_table)) if mc_table else set()
    except Exception as e:
        print(f"Monstercount fetch/parse error: {e}", file=sys.stderr)
        mc_names = set()

    # Neue Namen, die heute sichtbar sind, in Mitgliederliste aufnehmen
    newly_found = sorted((current_online | mc_names) - member_names, key=str.lower)
    if newly_found:
        updated = sorted(member_names | set(newly_found), key=str.lower)
        save_members(updated)
        member_names = set(updated)
        print(f"Added to members list: {', '.join(newly_found)}")

    # State vorbereiten
    state = load_state()
    last_seen = state.setdefault("last_seen", {})
    last_status = state.setdefault("last_status", {})

    for name in member_names:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "offline")

    # Online jetzt markieren
    now_ts = int(time.time())
    for name in current_online:
        last_seen[name] = now_ts
        last_status[name] = "online"

    # Monstercount als Tagesnachweis verwenden
    # Wenn ein Mitglied heute im Monstercount steht, gilt der Spieler als heute online.
    # Wir setzen last_seen auf jetzt, falls der bisherige Zeitstempel vor dem heutigen Datum liegt.
    today = now_berlin().date()
    today_midnight_utc = datetime.combine(today, datetime.min.time(), tzinfo=BERLIN).astimezone(timezone.utc).timestamp()
    mc_today = set()
    for name in (member_names & mc_names):
        ts = last_seen.get(name, 0)
        if ts < int(today_midnight_utc):
            last_seen[name] = now_ts
        mc_today.add(name)

    # Alle nicht-aktuellen auf offline
    for name in member_names - current_online:
        last_status[name] = "offline"

    # Abbruch, wenn täglicher Post bereits gesetzt wurde und wir keinen Test fahren
    today_str = today_berlin_date()
    if update_state_date and state.get("last_daily_date") == today_str:
        print(f"Daily already posted for {today_str}")
        # State speichern, da wir evtl. durch Monstercount last_seen aktualisiert haben
        save_state(state)
        return

    # Text bauen und posten
    all_names = set(last_seen.keys()) | names_today | member_names | mc_names
    if not all_names:
        post_to_discord("**Netherworld – beQuiet last seen**\nNo members tracked yet.")
        return

    content = build_daily_text(member_names, all_names, current_online, last_seen, mc_today, test_label)
    print(f"[DEBUG] total_length={len(content)}  mc_today={len(mc_today)}", file=sys.stderr)

    if post_long_to_discord(content, limit=1900, with_counters=True):
        if update_state_date:
            state["last_daily_date"] = today_str
        save_state(state)
    else:
        print("Daily not posted because webhook missing or Discord rejected the message", file=sys.stderr)
        # State trotzdem speichern, weil Monstercount-Infos wertvoll sind
        save_state(state)

# ---------- Entry ----------
def main():
    print("[DEBUG] bequiet_last_seen.py with CHUNKING + FORCE_POST + MONSTERCOUNT", file=sys.stderr)

    if FORCE_POST == "1":
        run_daily_summary(update_state_date=False, test_label=True)
        return

    if MODE == "hourly":
        run_hourly()
        return
    if MODE == "daily":
        run_daily_summary(update_state_date=True, test_label=False)
        return

    now_b = now_berlin()
    if is_berlin_daily_window(now_b):
        run_daily_summary(update_state_date=True, test_label=False)
    else:
        run_hourly()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
