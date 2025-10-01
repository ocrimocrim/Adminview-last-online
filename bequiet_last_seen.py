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
RANKING_URL = "https://pr-underworld.com/website/ranking/"
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
    return {
        "last_seen": {},
        "last_status": {},
        "last_daily_date": "",
        "last_ranking_sync_date": ""
    }

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- Mitgliederdatei ----------
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

# ---------- Parsing Helfer ----------
def find_table_under_heading(soup: BeautifulSoup, needle_lower: str):
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and needle_lower in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

# ---------- Homepage Online-Liste ----------
def find_home_server_table(soup: BeautifulSoup, server_label: str):
    return find_table_under_heading(soup, server_label.lower())

def parse_home_rows(table) -> list[dict]:
    """
    Homepage Struktur
      th scope=row Rang
      td[0] Name
      ...
      td[-1] Gilde mit <img> und Text
    """
    rows = []
    tbody = table.find("tbody")
    if not tbody:
        return rows
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if not tds:
            continue
        name = tds[0].get_text(strip=True)
        guild_text = tds[-1].get_text(strip=True) if len(tds) >= 2 else ""
        if name:
            rows.append({"name": name, "guild": guild_text})
    return rows

def parse_home_bequiet_rows(table) -> list[dict]:
    return [r for r in parse_home_rows(table)
            if r.get("guild", "").lower().find(GUILD_NAME.lower()) != -1]

# ---------- Ranking ----------
def find_ranking_table_netherworld(soup: BeautifulSoup):
    return find_table_under_heading(soup, "netherworld")

def parse_ranking_netherworld_rows(table) -> list[dict]:
    """
    Ranking Struktur
      th scope=row Rang
      td[0] Online-Icon
      td[1] Name
      td[2] Level
      td[3] Job-Icon
      td[4] Exp
      td[5] Gilde mit <img> und Text
    """
    rows = []
    tbody = table.find("tbody")
    if not tbody:
        return rows
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 6:
            name = tds[1].get_text(strip=True)
            guild_text = tds[5].get_text(strip=True)
            if name:
                rows.append({"name": name, "guild": guild_text})
    return rows

# ---------- Monstercount ----------
def find_monstercount_table(soup: BeautifulSoup, server_label: str):
    return find_table_under_heading(soup, server_label.lower())

def parse_monstercount_names(table) -> list[str]:
    """
    Monstercount Struktur
      th scope=row Rang
      td[0] Name
      td[1] Zahl
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

# ---------- Ranking Sync täglich ----------
def sync_members_from_home_and_ranking(member_names: set[str], state: dict) -> set[str]:
    """
    Ergänzen nur aus Homepage und Ranking.
    Entfernen nur, wenn der Name auf Homepage oder Ranking sichtbar ist und dort ohne beQuiet steht.
    Monstercount bleibt außen vor.
    """
    today_str = today_berlin_date()
    if state.get("last_ranking_sync_date") == today_str:
        return member_names

    # Homepage lesen
    home_html = fetch_html(URL)
    home_soup = BeautifulSoup(home_html, "html.parser")
    home_table = find_home_server_table(home_soup, SERVER_LABEL)
    home_rows = parse_home_rows(home_table) if home_table else []

    # Ranking lesen
    try:
        r_html = fetch_html(RANKING_URL)
        r_soup = BeautifulSoup(r_html, "html.parser")
        r_table = find_ranking_table_netherworld(r_soup)
        ranking_rows = parse_ranking_netherworld_rows(r_table) if r_table else []
    except Exception as e:
        print(f"Ranking fetch/parse error: {e}", file=sys.stderr)
        ranking_rows = []

    bequiet_home = {r["name"] for r in home_rows if r.get("guild", "").lower().find(GUILD_NAME.lower()) != -1}
    bequiet_rank = {r["name"] for r in ranking_rows if r.get("guild", "").lower().find(GUILD_NAME.lower()) != -1}

    to_add = (bequiet_home | bequiet_rank) - member_names

    non_bequiet_home = {r["name"] for r in home_rows
                        if r["name"] in member_names and r.get("guild", "") and r.get("guild", "").lower().find(GUILD_NAME.lower()) == -1}

    non_bequiet_rank = {r["name"] for r in ranking_rows
                        if r["name"] in member_names and r.get("guild", "").lower().find(GUILD_NAME.lower()) == -1}

    to_remove = non_bequiet_home | non_bequiet_rank

    updated = (member_names | to_add) - to_remove

    if to_add:
        print(f"Members add from home or ranking: {', '.join(sorted(to_add, key=str.lower))}")
    if to_remove:
        print(f"Members remove lost tag: {', '.join(sorted(to_remove, key=str.lower))}")

    save_members(sorted(updated, key=str.lower))
    state["last_ranking_sync_date"] = today_str
    save_state(state)
    return updated

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

    # Members-Datei im Hourly nicht anfassen
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
    now_ts = int(time.time())
    for name in sorted(all_names, key=sort_key):
        if name in current_online:
            lines.append(f"• **{name}** — currently online and grinding")
        else:
            ts = last_seen.get(name, 0)
            if name in mc_today and ts == 0:
                lines.append(f"• **{name}** — seen today via Monstercount")
            elif name in mc_today and ts > 0:
                delta = now_ts - ts
                lines.append(f"• **{name}** — seen today via Monstercount ({human_delta(delta)})")
            else:
                if ts > 0:
                    delta = now_ts - ts
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

    # Ranking Sync einmal täglich
    state = load_state()
    member_names = sync_members_from_home_and_ranking(member_names, state)

    # Monstercount lesen als Tagesnachweis
    try:
        mc_html = fetch_html(MONSTERCOUNT_URL)
        mc_soup = BeautifulSoup(mc_html, "html.parser")
        mc_table = find_monstercount_table(mc_soup, SERVER_LABEL)
        mc_names = set(parse_monstercount_names(mc_table)) if mc_table else set()
    except Exception as e:
        print(f"Monstercount fetch/parse error: {e}", file=sys.stderr)
        mc_names = set()

    # State vorbereiten
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
        save_state(state)
        return

    # Text bauen und posten
    all_names = set(last_seen.keys()) | names_today | member_names
    if not all_names:
        post_to_discord("**Netherworld – beQuiet last seen**\nNo members tracked yet.")
        save_state(state)
        return

    content = build_daily_text(member_names, all_names, current_online, last_seen, mc_today, test_label)
    print(f"[DEBUG] total_length={len(content)}  mc_today={len(mc_today)}", file=sys.stderr)

    if post_long_to_discord(content, limit=1900, with_counters=True):
        if update_state_date:
            state["last_daily_date"] = today_str
        save_state(state)
    else:
        save_state(state)

# ---------- Entry ----------
def main():
    print("[DEBUG] bequiet_last_seen.py with RANKING SYNC + CHUNKING + FORCE_POST", file=sys.stderr)

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
