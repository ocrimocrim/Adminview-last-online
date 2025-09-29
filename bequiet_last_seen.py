import os, sys, json, time
from pathlib import Path
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import requests
from bs4 import BeautifulSoup

URL = "https://pr-underworld.com/website/ranking/"
GUILD_NAME = "beQuiet"
SERVER_LABEL = "Netherworld"
ONLINE_IMG_KEY = "website_char_online"   # in the <img src=...> of the "Online" column

STATE_FILE = Path("state_last_seen.json")
TIMEOUT = 20

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MODE = os.getenv("MODE", "auto").strip().lower()  # "auto" | "hourly" | "daily"

# ---------- state helpers ----------
def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    # last_daily_date = Datum (Europe/Berlin) der letzten Daily-Message, ISO-Format (YYYY-MM-DD)
    return {"last_seen": {}, "last_status": {}, "last_daily_date": ""}

def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

# ---------- discord ----------
def post_to_discord(content: str):
    if not WEBHOOK:
        print("No DISCORD_WEBHOOK_URL set; skip posting", file=sys.stderr)
        return
    r = requests.post(WEBHOOK, json={"content": content}, timeout=15)
    try:
        r.raise_for_status()
    except Exception as e:
        print(f"Discord error: {e} {getattr(r, 'text', '')}", file=sys.stderr)

# ---------- time helpers ----------
BERLIN = ZoneInfo("Europe/Berlin")

def now_utc():
    return datetime.now(timezone.utc)

def now_berlin():
    return now_utc().astimezone(BERLIN)

def is_berlin_2358(dt: datetime) -> bool:
    return dt.hour == 23 and dt.minute == 58

# ---------- scraping ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "beQuiet last-seen tracker"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

def find_netherworld_table(soup: BeautifulSoup):
    # Find heading with "Netherworld" and take the next table
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        if SERVER_LABEL.lower() in h.get_text(strip=True).lower():
            return h.find_next("table")
    return None

def parse_bequiet_rows(table):
    """
    Return list of dicts: [{"name": "...", "online": bool}, ...] only for beQuiet
    Netherworld columns: Online | Name | Level | Job | Exp% | Guild
    """
    res = []
    tbody = table.find("tbody")
    if not tbody:
        return res
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 6:
            continue
        # Online icon
        online = False
        img = tds[0].find("img")
        if img and "src" in img.attrs:
            online = ONLINE_IMG_KEY in img["src"]
        # Name
        name = tds[1].get_text(strip=True)
        # Guild cell
        guild_txt = tds[5].get_text(" ", strip=True)
        if GUILD_NAME.lower() in guild_txt.lower():
            res.append({"name": name, "online": online})
    return res

# ---------- formatting ----------
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

# ---------- main flows ----------
def run_hourly():
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_netherworld_table(soup)
    if not table:
        print("Netherworld table not found", file=sys.stderr)
        return

    state = load_state()
    last_seen = state.setdefault("last_seen", {})
    last_status = state.setdefault("last_status", {})

    beq_rows = parse_bequiet_rows(table)
    now_ts = int(time.time())
    seen_today = set()

    for row in beq_rows:
        name = row["name"]
        seen_today.add(name)
        if row["online"]:
            last_seen[name] = now_ts
            last_status[name] = "online"
        else:
            last_status[name] = "offline"

    # ensure keys exist for everyone we've seen
    for name in seen_today:
        last_seen.setdefault(name, 0)
        last_status.setdefault(name, "offline")

    save_state(state)

def run_daily_summary():
    html = fetch_html(URL)
    soup = BeautifulSoup(html, "html.parser")
    table = find_netherworld_table(soup)
    if not table:
        print("Netherworld table not found", file=sys.stderr)
        return

    state = load_state()
    last_seen = state.get("last_seen", {})
    last_status = state.get("last_status", {})

    beq_rows = parse_bequiet_rows(table)
    names_today = {r["name"] for r in beq_rows}
    current_online = {r["name"] for r in beq_rows if r["online"]}

    # union: keep historic names too
    all_names = set(last_seen.keys()) | names_today
    if not all_names:
        post_to_discord("**Netherworld – beQuiet last seen**\nNo members tracked yet.")
        return

    # sort: online first, then by most recent last_seen desc, then name
    def sort_key(n):
        online = 1 if n in current_online else 0
        return (-online, -last_seen.get(n, 0), n.lower())

    today_berlin = now_berlin().date().isoformat()
    if state.get("last_daily_date") == today_berlin:
        print("Daily already posted for", today_berlin)
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

    # mark as posted
    state["last_daily_date"] = today_berlin
    save_state(state)

def main():
    if MODE == "hourly":
        run_hourly()
        return
    if MODE == "daily":
        run_daily_summary()
        return

    # MODE == "auto": nur posten, wenn es in Berlin exakt 23:58 ist,
    # ansonsten normaler Stundenlauf (Last-Seen updaten).
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
