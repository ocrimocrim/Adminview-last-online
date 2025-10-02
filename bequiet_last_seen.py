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

WEBHOOK = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
MODE = os.getenv("MODE", "auto").strip().lower()  # auto | hourly | daily
FORCE_POST = os.getenv("FORCE_POST", "").strip()  # "1" = Testlauf mit Post

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
    names = [line.strip() for line in text.splitlines() if line.strip()]
    uniq, seen = [], set()
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
        print(f"Discord error: {e}", file=sys.stderr)
        return False

def chunk_text(content: str, limit: int = 1900) -> list[str]:
    chunks, remaining = [], content
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut_at = limit
        nl = remaining.rfind("\n", 0, limit + 1)
        if nl != -1 and nl >= int(limit * 0.6):
            cut_at = nl + 1
        chunks.append(remaining[:cut_at].rstrip("\n"))
        remaining = remaining[cut_at:].lstrip("\n")
    return chunks

def post_long_to_discord(content: str, limit=1900, with_counters=True) -> bool:
    chunks = chunk_text(content, limit)
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
            payload += suffix
        ok = post_to_discord(payload) and ok
    return ok

# ---------- Zeit ----------
BERLIN = ZoneInfo("Europe/Berlin")
def now_utc(): return datetime.now(timezone.utc)
def now_berlin(): return now_utc().astimezone(BERLIN)
def today_berlin_date(): return now_berlin().date().isoformat()
def is_berlin_daily_window(dt): return dt.hour == 23 and 20 <= dt.minute <= 59

# ---------- HTTP ----------
def fetch_html(url: str) -> str:
    r = requests.get(url, headers={"User-Agent": "beQuiet last-seen tracker"}, timeout=TIMEOUT)
    r.raise_for_status()
    return r.text

# ---------- Parsing ----------
def find_table_under_heading(soup, needle: str):
    for h in soup.find_all(["h3", "h4", "h5", "h6"]):
        heading = h.get_text(strip=True)
        if heading and needle in heading.lower():
            tbl = h.find_next("table")
            if tbl and tbl.find("tbody"):
                return tbl
    return None

# Homepage
def parse_home_rows(table):
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if not tds: continue
        name = tds[0].get_text(strip=True)
        guild = tds[-1].get_text(strip=True) if len(tds) >= 2 else ""
        if name: rows.append({"name": name, "guild": guild})
    return rows

def parse_home_bequiet_rows(table):
    return [r for r in parse_home_rows(table) if GUILD_NAME.lower() in r["guild"].lower()]

# Ranking
def parse_ranking_netherworld_rows(table):
    rows = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 6:
            name = tds[1].get_text(strip=True)
            guild = tds[5].get_text(strip=True)
            if name: rows.append({"name": name, "guild": guild})
    return rows

# Monstercount
def parse_monstercount_names(table):
    out = []
    for tr in table.find("tbody").find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) >= 1:
            n = tds[0].get_text(strip=True)
            if n: out.append(n)
    return out

# ---------- Format ----------
def human_delta(sec):
    m,s = divmod(sec,60); h,m = divmod(m,60); d,h = divmod(h,24)
    if d>0: return f"{d}d {h}h"
    if h>0: return f"{h}h {m}m"
    if m>0: return f"{m}m"
    return f"{s}s"

def fmt_ts_utc(ts): 
    return datetime.fromtimestamp(ts,tz=timezone.utc).astimezone(BERLIN).strftime("%Y-%m-%d %H:%M")

# ---------- Sync ----------
def sync_members_from_home_and_ranking(members:set[str], state:dict)->set[str]:
    today=today_berlin_date()
    if state.get("last_ranking_sync_date")==today: return members
    # Homepage
    home_html=fetch_html(URL); home_soup=BeautifulSoup(home_html,"html.parser")
    home_tbl=find_table_under_heading(home_soup,SERVER_LABEL.lower())
    home_rows=parse_home_rows(home_tbl) if home_tbl else []
    # Ranking
    try:
        r_html=fetch_html(RANKING_URL); r_soup=BeautifulSoup(r_html,"html.parser")
        r_tbl=find_table_under_heading(r_soup,"netherworld")
        r_rows=parse_ranking_netherworld_rows(r_tbl) if r_tbl else []
    except Exception as e:
        print(f"Ranking fetch error {e}",file=sys.stderr); r_rows=[]
    beq_home={r["name"] for r in home_rows if GUILD_NAME.lower() in r["guild"].lower()}
    beq_rank={r["name"] for r in r_rows if GUILD_NAME.lower() in r["guild"].lower()}
    to_add=(beq_home|beq_rank)-members
    to_remove={r["name"] for r in home_rows+r_rows if r["name"] in members and GUILD_NAME.lower() not in r["guild"].lower() and r["guild"]}
    updated=(members|to_add)-to_remove
    if to_add: print("Add:",", ".join(sorted(to_add)))
    if to_remove: print("Remove:",", ".join(sorted(to_remove)))
    save_members(sorted(updated))
    state["last_ranking_sync_date"]=today; save_state(state)
    return updated

# ---------- Hourly ----------
def run_hourly():
    members=set(load_members())
    html=fetch_html(URL); soup=BeautifulSoup(html,"html.parser")
    tbl=find_table_under_heading(soup,SERVER_LABEL.lower())
    if not tbl: return
    beq=parse_home_bequiet_rows(tbl)
    online={r["name"] for r in beq}
    state=load_state(); last_seen=state.setdefault("last_seen",{}); last_status=state.setdefault("last_status",{})
    now_ts=int(time.time())
    for n in members: last_seen.setdefault(n,0); last_status.setdefault(n,"offline")
    for n in online: last_seen[n]=now_ts; last_status[n]="online"
    for n in members-online: last_status[n]="offline"
    save_state(state)

# ---------- Daily ----------
def build_daily_text(members,all_names,online,last_seen,mc_today,test):
    header=f"**Netherworld – beQuiet last seen** ({today_berlin_date()})"
    if test: header+=" Test"
    now_ts=int(time.time())
    lines=[]
    for n in sorted(all_names,key=lambda x:(-int(x in online),-last_seen.get(x,0),x.lower())):
        if n in online: lines.append(f"• **{n}** — currently online and grinding")
        else:
            ts=last_seen.get(n,0)
            if n in mc_today and ts==0: lines.append(f"• **{n}** — seen today via Monstercount")
            elif n in mc_today: lines.append(f"• **{n}** — seen today via Monstercount ({human_delta(now_ts-ts)})")
            elif ts>0: lines.append(f"• **{n}** — last seen {fmt_ts_utc(ts)} ({human_delta(now_ts-ts)})")
            else: lines.append(f"• **{n}** — no sightings yet")
    return header+"\n"+"\n".join(lines)

def run_daily_summary(update_state_date,test):
    members=set(load_members())
    html=fetch_html(URL); soup=BeautifulSoup(html,"html.parser")
    tbl=find_table_under_heading(soup,SERVER_LABEL.lower())
    if not tbl: return
    beq=parse_home_bequiet_rows(tbl)
    online={r["name"] for r in beq}
    state=load_state(); members=sync_members_from_home_and_ranking(members,state)
    try:
        mc_html=fetch_html(MONSTERCOUNT_URL); mc_soup=BeautifulSoup(mc_html,"html.parser")
        mc_tbl=find_table_under_heading(mc_soup,SERVER_LABEL.lower())
        mc=set(parse_monstercount_names(mc_tbl)) if mc_tbl else set()
    except: mc=set()
    last_seen=state.setdefault("last_seen",{}); last_status=state.setdefault("last_status",{})
    now_ts=int(time.time()); today=now_berlin().date()
    today_midnight=int(datetime.combine(today,datetime.min.time(),tzinfo=BERLIN).astimezone(timezone.utc).timestamp())
    mc_today=set()
    for n in members: last_seen.setdefault(n,0); last_status.setdefault(n,"offline")
    for n in online: last_seen[n]=now_ts; last_status[n]="online"
    for n in (members&mc):
        if last_seen.get(n,0)<today_midnight: last_seen[n]=now_ts
        mc_today.add(n)
    for n in members-online: last_status[n]="offline"
    today_str=today_berlin_date()
    if update_state_date and state.get("last_daily_date")==today_str:
        save_state(state); return
    all_names=set(last_seen)|members|online
    content=build_daily_text(members,all_names,online,last_seen,mc_today,test)
    if post_long_to_discord(content):
        if update_state_date: state["last_daily_date"]=today_str
        save_state(state)
    else: save_state(state)

# ---------- Main ----------
def main():
    print("[DEBUG] MODE=",MODE,"FORCE_POST=",FORCE_POST,file=sys.stderr)
    if FORCE_POST=="1": run_daily_summary(False,True); return
    if MODE=="hourly": run_hourly(); return
    if MODE=="daily": run_daily_summary(True,False); return
    if is_berlin_daily_window(now_berlin()): run_daily_summary(True,False)
    else: run_hourly()

if __name__=="__main__":
    try: main()
    except Exception as e:
        print("Error:",e,file=sys.stderr); sys.exit(1)
