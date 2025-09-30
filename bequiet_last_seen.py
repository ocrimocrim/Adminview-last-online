#!/usr/bin/env python3
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

# ---------- Konfiguration ----------
URL = "https://pr-underworld.com/website/"
GUILD_NAME = "beQuiet"
SERVER_LABEL = "Netherworld"

STATE_FILE = Path("state_last_seen.json")
TIMEOUT = 20

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MODE = os.getenv("MODE", "auto").strip().lower()  # "auto" | "hourly" | "daily"

# ---------- State Helpers ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # last_daily_date ist das Datum in Europe Berlin der letzten Daily Message im ISO Format YYYY-MM-DD
    return {"last_seen": {}, "last_status": {}, "last_daily_date": ""}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- Discord ----------
def post_to_discord(content: str):
    if not WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set; skip posting", file=sys.stderr)
        return
    r = requests.post(WEBHOOK, json={"content": content}, timeout=15)
    try:
        r.raise_for_status()
    except Exception as e:
        print(f"Discord error: {e} {getattr(r, 'text', '')}", file=sys.stderr)

# ---------- Time Helpers ----------
BERLIN = ZoneInfo("Europe/Berlin")

def now_utc():
    return datetime.now(timezone.utc)

def now_berlin():
    return now_utc().astimezone(BERLIN)

def is_berlin_2358(dt: datetime) -> bool:
    return dt.hour == 23 and dt.minute == 58

# ---------- Scraping ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "beQuiet last-seen tracker"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def find_home_server_table(soup: BeautifulSoup, server_label: str):
    """
    Sucht die Server Sektion auf der Startseite und liefert die zugehörige Tabelle
    Erwartete Struktur
    <h4>Underworld</h4> Tabelle
    <h4>Netherworld</h4> Tabelle
    """
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and server_label.lower() in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

def parse_home_bequiet_rows(table):
    """
    Liest Zeilen der Startseiten Tabelle
    Spaltenfolge
    # | Name | Level | Job | Guild
    Der Guild Cell enthält optional ein <img> und danach den Gildennamen als Text
    Jeder Eintrag der Tabelle zählt als aktuell online
    """
    res = []
    tbody = table.find("tbody")
    if not tbody:
        return res
    for tr in tbody.find_all("tr"):
        tds = tr.find_all(["td", "th"])
        if len(tds) < 5:
            continue
        name = tds[1].get_text(strip=True)
        guild_txt = tds[4].get_text(" ", strip=True)
        if GUILD_NAME.lower() in guild_txt.lower():
            res.append({"name": name, "online": True})
    return res

# ---------- Formatting ----------
def human_delta(seconds: int) -> str:
    if seconds < 90:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 90:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 48:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"

def fmt_ts_utc(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(ts))

# ---------- Main Flows ----------
def run_hourly():
    """
    Startseite listet alle aktuell eingeloggten Spieler
    Wir markieren beQuiet Spieler aus der Netherworld Tabelle als online und aktualisieren deren last_seen
    Alle historisch bekannten beQuiet Spieler, die jetzt nicht auftauchen, erhalten Status offline
    """
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return

    state = load_state()
    last_seen = state.setdefault("last_seen", {})
    last_status = state.setdefault("last_status", {})

    beq_rows = parse_home_bequiet_rows(table)
    now_ts = int(time.time())
    currently_online = set()

    for row in beq_rows:
        name = row["name"]
        currently_online.add(name)
        last_seen[name] = now_ts
        last_status[name] = "online"

    # alle bekannten beQuiet Namen, die aktuell nicht gelistet sind, auf offline setzen
    known_names = set(last_seen.keys())
    for name in known_names - currently_online:
        last_status[name] = "offline"

    # neue Namen aus diesem Lauf sicherstellen
    for name in currently_online:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "online")

    save_state(state)

def run_daily_summary():
    """
    Tägliche Übersicht auf Basis der Startseite
    current_online kommt aus der Netherworld Tabelle
    all_names ist die Vereinigung aus historisch bekannten Namen und heutigen Namen
    """
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return

    state = load_state()
    last_seen = state.get("last_seen", {})
    last_status = state.get("last_status", {})

    beq_rows = parse_home_bequiet_rows(table)
    names_today = {r["name"] for r in beq_rows}
    current_online = set(names_today)

    all_names = set(last_seen.keys()) | names_today
    if not all_names:
        post_to_discord("**Netherworld – beQuiet last seen**\nNo members tracked yet.")
        return

    # Sortierung online zuerst, dann nach last_seen absteigend, dann Name
    def sort_key(n):
        online = 1 if n in current_online else 0
        return (-online, -last_seen.get(n, 0), n.lower())

    today_berlin = now_berlin().date().isoformat()
    if state.get("last_daily_date") == today_berlin:
        print(f"Daily already posted for {today_berlin}")
        return

    lines = []
    header = f"**Netherworld – beQuiet last seen** ({today_berlin})"
    for name in sorted(all_names, key=sort_key):
        if name in current_online:
            lines.append(f"• **{name}** — currently online and grinding")
        else:
            ts = last_seen.get(name, 0)
            if ts > 0:
                delta = int(time.time()) - ts
                lines.append(f"• **{name}** — last seen {fmt_ts_utc(ts)} ({human_delta(delta)})")
            else:
                lines.append(f"• **{name}** — no sightings yet")

    content = header + "\n" + "\n".join(lines[:1900])
    post_to_discord(content)

    # als gepostet markieren
    state["last_daily_date"] = today_berlin
    save_state(state)

def main():
    if MODE == "hourly":
        run_hourly()
        return
    if MODE == "daily":
        run_daily_summary()
        return

    # MODE auto verwendet 23 58 Europe Berlin für die Daily Message
    now_b = now_berlin()
    if is_berlin_2358(now_b):
        run_daily_summary()
    else:
        run_hourly()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
