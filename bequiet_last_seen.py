#!/usr/bin/env python3
import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

# ---------- Konfiguration ----------
URL = "https://pr-underworld.com/website/"   # Startseite zeigt alle aktuell Online-Spieler
GUILD_NAME = "beQuiet"
SERVER_LABEL = "Netherworld"

STATE_FILE   = Path("state_last_seen.json")
MEMBERS_FILE = Path("bequiet_members.txt")
TIMEOUT = 20

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MODE = os.getenv("MODE", "auto").strip().lower()  # "auto" | "hourly" | "daily"

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
    names = [n for n in names if n]  # leere Zeilen raus
    # Duplikate entfernen, Reihenfolge stabilisieren
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
        print("No DISCORD_WEBHOOK_URL set; skip posting", file=sys.stderr)
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

# Chunked posting helper for Discord hard limit 2000 chars
def post_long_to_discord(content: str, limit: int = 1900) -> bool:
    if content is None:
        return False
    # splitte an Zeilenumbrüchen wenn möglich
    chunks = []
    remaining = content
    while remaining:
        chunk = remaining[:limit]
        cut = chunk.rfind("\n")
        if cut != -1 and cut >= int(limit * 0.6):
            chunk = chunk[:cut]
            next_index = cut + 1
        else:
            next_index = len(chunk)
        chunks.append(chunk)
        remaining = remaining[next_index:]
    ok = True
    for c in chunks:
        ok = post_to_discord(c) and ok
    return ok

# ---------- Zeit ----------
BERLIN = ZoneInfo("Europe/Berlin")

def now_utc():
    return datetime.now(timezone.utc)

def now_berlin():
    return now_utc().astimezone(BERLIN)

def is_berlin_daily_window(dt: datetime) -> bool:
    # Daily zwischen 23:20 und 23:59 Europe/Berlin
    return dt.hour == 23 and 20 <= dt.minute <= 59

# ---------- Scraping ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "beQuiet last-seen tracker"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def find_home_server_table(soup: BeautifulSoup, server_label: str):
    """
    Startseite hat Überschrift(en) und darunter je eine Tabelle (Underworld/Netherworld).
    Wir suchen die Tabelle, deren Heading den Servernamen enthält.
    """
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and server_label.lower() in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

def parse_home_bequiet_rows(table) -> list[dict]:
    """
    Tabelle enthält Zeilen mit Namen und Gilde. Wir filtern auf Gilde beQuiet.
    """
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

# ---------- Formatierung ----------
def human_delta(seconds: int) -> str:
    # grobe menschenlesbare Dauer
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

# ---------- Logik ----------
def run_hourly():
    """
    - Website laden und aktuell online befindliche beQuiet-Spieler ermitteln
    - State updaten (last_seen für online, Status setzen)
    - Mitgliederliste einlesen; alle daraus sicherstellen
    - Neue gefundene beQuiet-Namen, die noch nicht in der Liste stehen, automatisch zur Datei hinzufügen
    """
    # Mitgliederliste laden
    member_names = set(load_members())

    # Website parsen
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return

    beq_rows = parse_home_bequiet_rows(table)
    currently_online = {row["name"] for row in beq_rows}

    # Mitgliederliste automatisch mit neu gefundenen beQuiet-Namen erweitern
    newly_found = sorted(currently_online - member_names, key=str.lower)
    if newly_found:
        updated = sorted(member_names | set(newly_found), key=str.lower)
        save_members(updated)
        member_names = set(updated)
        print(f"Added to members list: {', '.join(newly_found)}")

    # State pflegen
    state = load_state()
    last_seen = state.setdefault("last_seen", {})
    last_status = state.setdefault("last_status", {})
    now_ts = int(time.time())

    # Alle aus der Liste sollen im State existieren
    for name in member_names:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "offline")

    # Setze Status und last_seen für aktuell Online
    for name in currently_online:
        last_seen[name] = now_ts
        last_status[name] = "online"

    # Alle nicht gesehenen auf offline setzen
    for name in member_names - currently_online:
        last_status[name] = "offline"

    save_state(state)

def run_daily_summary():
    """
    - Am Tagesende Übersicht posten
      * online jetzt
      * sonst letzter bekannter Zeitpunkt aus State
      * zusätzlich alle Namen aus der Mitgliederliste, damit niemand fehlt
    - Neue beQuiet-Namen von der Seite werden auch hier zur Liste hinzugefügt
    """
    member_names = set(load_members())

    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_home_server_table(soup, SERVER_LABEL)
    if not table:
        print("Netherworld table not found on homepage", file=sys.stderr)
        return

    beq_rows = parse_home_bequiet_rows(table)
    names_today = {r["name"] for r in beq_rows}
    current_online = set(names_today)

    # Mitgliederliste mit neu erkannten Namen ergänzen
    newly_found = sorted(current_online - member_names, key=str.lower)
    if newly_found:
        updated = sorted(member_names | set(newly_found), key=str.lower)
        save_members(updated)
        member_names = set(updated)
        print(f"Added to members list: {', '.join(newly_found)}")

    state = load_state()
    last_seen = state.get("last_seen", {})
    last_status = state.get("last_status", {})

    # Jeder aus der Liste soll im State präsent sein
    for name in member_names:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "offline")

    # Gesamtnamen
    all_names = set(last_seen.keys()) | names_today | member_names
    if not all_names:
        post_to_discord("**Netherworld – beQuiet last seen**\nNo members tracked yet.")
        return

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

    content = header + "\n" + "\n".join(lines)
    if post_long_to_discord(content):
        state["last_daily_date"] = today_berlin
        save_state(state)
    else:
        print("Daily not marked as posted because Discord rejected the message", file=sys.stderr)

def main():
    if MODE == "hourly":
        run_hourly()
        return
    if MODE == "daily":
        run_daily_summary()
        return

    # AUTO Modus Daily im Fenster, sonst Hourly
    now_b = now_berlin()
    if is_berlin_daily_window(now_b):
        run_daily_summary()
    else:
        run_hourly()

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
