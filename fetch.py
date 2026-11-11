"""Pull the team's training data from intervals.icu and build the dashboard page.

Runs on GitHub Actions. Needs one secret: INTERVALS_API_KEY (the coach's API key
from intervals.icu -> Settings -> Developer Settings). Uses only the Python
standard library, so there is nothing to install.

Who is on the dashboard:
  - everyone the API key's account coaches or follows on intervals.icu, plus the coach
  - riders.json can rename riders, add riders by ID, or hide them
"""

import base64
import datetime as dt
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from zoneinfo import ZoneInfo

API = "https://intervals.icu/api/v1"
TZ = ZoneInfo(os.environ.get("TEAM_TIMEZONE", "America/Indiana/Indianapolis"))
POWER_DURATIONS = [(5, "5 s", "p5s"), (60, "1 min", "p1m"), (180, "3 min", "p3m"),
                   (720, "12 min", "p12m"), (1200, "20 min", "p20m")]
HERE = os.path.dirname(os.path.abspath(__file__))


# ---------------------------------------------------------------- API access

def _auth_header():
    key = os.environ.get("INTERVALS_API_KEY", "").strip().strip('"').strip("'")
    if key.upper().startswith("API_KEY:"):
        key = key[8:]
    if not key:
        sys.exit("INTERVALS_API_KEY is not set. Add it under Settings -> Secrets and variables -> Actions.")
    token = base64.b64encode(f"API_KEY:{key}".encode()).decode()
    return {"Authorization": f"Basic {token}", "Accept": "application/json",
            "User-Agent": "team-training-dashboard/1.0 (GitHub Actions; python urllib)"}


def get(path, **params):
    """GET an intervals.icu API path. Returns parsed JSON, or None on 403/404."""
    url = f"{API}{path}"
    if params:
        url += "?" + urllib.parse.urlencode(params)
    for attempt in range(4):
        req = urllib.request.Request(url, headers=_auth_header())
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (401,):
                sys.exit("intervals.icu rejected the API key (401). Check the INTERVALS_API_KEY secret.")
            if e.code in (403, 404):
                body = e.read().decode("utf-8", "replace")[:200].replace("\n", " ")
                print(f"  ! {e.code} on {path} (no access): {body}")
                return None
            if e.code == 429 or e.code >= 500:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        except urllib.error.URLError:
            time.sleep(5 * (attempt + 1))
    print(f"  ! gave up on {path}")
    return None


# ---------------------------------------------------------------- roster

def load_settings():
    path = os.path.join(HERE, "riders.json")
    if not os.path.exists(path):
        return {"names": {}, "add": [], "hide": []}
    with open(path, encoding="utf-8") as f:
        s = json.load(f)
    return {"names": s.get("names", {}), "add": s.get("add", []), "hide": s.get("hide", [])}


def _athlete_list(answer):
    """Pull (id, name) pairs out of whatever shape the athletes endpoint returns."""
    out = []
    if isinstance(answer, dict):
        answer = answer.get("athletes") or answer.get("list") or []
    for a in answer or []:
        if not isinstance(a, dict):
            continue
        aid = a.get("id") or a.get("athlete_id")
        name = a.get("name") or a.get("athlete_name") or " ".join(
            x for x in (a.get("firstname"), a.get("lastname")) if x)
        if aid:
            aid = str(aid)
            out.append((aid if aid.startswith("i") else "i" + aid, name or aid))
    return out


def find_riders(settings):
    riders = {}
    me = get("/athlete/0")
    if not me:
        sys.exit("intervals.icu did not accept the API key (403 on your own account). "
                 "Copy the current key from intervals.icu -> Settings -> Developer Settings and "
                 "update the INTERVALS_API_KEY secret on GitHub. Paste only the key itself.")
    if me.get("id"):
        riders[str(me["id"])] = me.get("name") or str(me["id"])
    found = _athlete_list(get("/athletes"))
    if not found:  # older route: the coach's athlete summary lists coached athletes
        found = _athlete_list(get("/athlete/0/athlete-summary.json"))
    for aid, name in found:
        riders.setdefault(aid, name)
    for aid in settings["add"]:
        riders.setdefault(aid, aid)
    for aid in settings["hide"]:
        riders.pop(aid, None)
    for aid, name in settings["names"].items():
        if aid in riders:
            riders[aid] = name
    return riders


# ---------------------------------------------------------------- per rider

def best_power(curves_answer):
    """{'56d': {secs: (watts, wkg, activity_id)}, '1y': {...}}, activity names by id."""
    out, names = {}, {}
    if not curves_answer:
        return out, names
    lists = curves_answer.get("list", []) if isinstance(curves_answer, dict) else curves_answer
    for c in lists or []:
        secs = c.get("secs") or []
        vals = c.get("values") or c.get("watts") or []
        wkg = c.get("watts_per_kg") or []
        acts = c.get("activity_id") or []
        table = {}
        for s, _, _ in POWER_DURATIONS:
            if s in secs:
                i = secs.index(s)
                if i < len(vals) and vals[i]:
                    table[s] = (round(vals[i]),
                                round(wkg[i], 1) if i < len(wkg) and wkg[i] else None,
                                acts[i] if i < len(acts) else None)
        out[c.get("id")] = table
    if isinstance(curves_answer, dict):
        for aid, a in (curves_answer.get("activities") or {}).items():
            names[aid] = (a.get("name"), (a.get("start_date_local") or "")[:10])
    return out, names


def rider_summary(aid, name, today):
    print(f"- {name} ({aid})")
    monday = today - dt.timedelta(days=today.weekday())
    last_monday = monday - dt.timedelta(days=7)
    month_start = today.replace(day=1)
    year_start = today.replace(month=1, day=1)
    eight_weeks_start = monday - dt.timedelta(weeks=7)
    oldest = min(year_start, eight_weeks_start, today - dt.timedelta(days=14))

    acts = get(f"/athlete/{aid}/activities", oldest=oldest.isoformat(), newest=today.isoformat())
    if acts is None:
        return {"id": aid, "rider": name, "state": "blocked"}, []

    hidden = 0
    rides, feed, recent_rides = [], [], []
    for a in acts:
        if not a.get("type"):  # Strava-only activities come back as empty stubs
            hidden += 1
            continue
        day = dt.date.fromisoformat((a.get("start_date_local") or "")[:10])
        rec = {
            "day": day,
            "is_ride": "Ride" in a["type"],
            "secs": a.get("moving_time") or 0,
            "meters": a.get("distance") or 0,
        }
        rides.append(rec)
        if rec["is_ride"]:
            recent_rides.append({
                "start": a.get("start_date_local") or "", "date": day.isoformat(),
                "name": a.get("name") or "", "type": a["type"],
                "hours": round(rec["secs"] / 3600, 1),
                "miles": round(rec["meters"] / 1609.344, 1) if rec["meters"] else None,
                "avg_watts": round(a["icu_average_watts"]) if a.get("icu_average_watts") else None,
                "np": round(a["icu_weighted_avg_watts"]) if a.get("icu_weighted_avg_watts") else None,
                "load": a.get("icu_training_load"),
            })
        if day >= today - dt.timedelta(days=13):
            feed.append({
                "rider": name, "start": a.get("start_date_local") or "", "date": day.isoformat(),
                "name": a.get("name") or "", "type": a["type"],
                "hours": round(rec["secs"] / 3600, 1),
                "miles": round(rec["meters"] / 1609.344, 1) if rec["meters"] else None,
                "load": a.get("icu_training_load"),
            })

    def hours(start, end=None):
        secs = sum(r["secs"] for r in rides if r["is_ride"] and r["day"] >= start and (end is None or r["day"] < end))
        return round(secs / 3600, 1)

    year_rides = [r for r in rides if r["is_ride"] and r["day"] >= year_start]

    # Weekly ride hours and miles for the last 8 calendar weeks (oldest first).
    weeks = []
    for i in range(7, -1, -1):
        start = monday - dt.timedelta(weeks=i)
        end = start + dt.timedelta(days=7)
        wk = [r for r in rides if r["is_ride"] and start <= r["day"] < end]
        weeks.append({"week": start.isoformat(),
                      "hours": round(sum(r["secs"] for r in wk) / 3600, 1),
                      "miles": round(sum(r["meters"] for r in wk) / 1609.344),
                      "rides": len(wk)})

    # Fitness, fatigue and form for the last 8 weeks; today's values for the leaderboard.
    fitness = form = None
    trend = []
    well = get(f"/athlete/{aid}/wellness", oldest=(today - dt.timedelta(days=55)).isoformat(), newest=today.isoformat())
    if well:
        days = sorted([w for w in well if w.get("ctl") is not None], key=lambda w: w.get("id", ""))
        for w in days:
            atl = w.get("atl") or 0
            trend.append({"date": w.get("id"), "fitness": round(w["ctl"], 1),
                          "fatigue": round(atl, 1), "form": round(w["ctl"] - atl, 1)})
        if days:
            fitness = round(days[-1]["ctl"])
            form = round(days[-1]["ctl"] - (days[-1].get("atl") or 0))
    newest_ride = next((a for a in sorted(acts, key=lambda a: a.get("start_date_local") or "", reverse=True)
                        if a.get("type") and "Ride" in a["type"] and a.get("icu_rolling_ftp")), None)
    if fitness is None and newest_ride and newest_ride.get("icu_ctl") is not None:
        fitness = round(newest_ride["icu_ctl"])
        form = round(newest_ride["icu_ctl"] - (newest_ride.get("icu_atl") or 0))

    eftp = eftp_wkg = None
    if newest_ride:
        eftp = round(newest_ride["icu_rolling_ftp"])
        if newest_ride.get("icu_weight"):
            eftp_wkg = round(newest_ride["icu_rolling_ftp"] / newest_ride["icu_weight"], 2)

    curves, act_names = best_power(get(f"/athlete/{aid}/power-curves.json", curves="56d,1y", type="Ride"))
    recent, year = curves.get("56d", {}), curves.get("1y", {})

    row = {
        "id": aid, "rider": name, "state": "ok", "hidden_strava": hidden,
        "hours_this_week": hours(monday),
        "hours_last_week": hours(last_monday, monday),
        "hours_month": hours(month_start),
        "hours_ytd": round(sum(r["secs"] for r in year_rides) / 3600, 1),
        "miles_ytd": round(sum(r["meters"] for r in year_rides) / 1609.344),
        "fitness": fitness, "form": form, "eftp_watts": eftp, "eftp_wkg": eftp_wkg,
        "prs": [],
        "weeks": weeks,
        "trend": trend,
        "recent_rides": sorted(recent_rides, key=lambda r: r["start"], reverse=True)[:10],
    }
    for secs, label, key in POWER_DURATIONS:
        best = recent.get(secs)
        row[key] = best[0] if best else None
        is_pr = bool(best and year.get(secs) and best[0] >= year[secs][0])
        row[key + "_pr"] = is_pr
        if is_pr:
            ride_name, ride_date = act_names.get(best[2], (None, None))
            row["prs"].append({"rider": name, "duration": label, "watts": best[0], "wkg": best[1],
                               "date": ride_date, "ride": ride_name})
    return row, feed


# ---------------------------------------------------------------- build

def main():
    key = os.environ.get("INTERVALS_API_KEY", "").strip()
    print(f"API key loaded: {len(key)} characters (intervals.icu keys are usually about 24)")
    today = dt.datetime.now(TZ).date()
    settings = load_settings()
    riders = find_riders(settings)
    print(f"Riders found: {len(riders)}")

    rows, feed = [], []
    for aid, name in riders.items():
        row, rider_feed = rider_summary(aid, name, today)
        rows.append(row)
        feed.extend(rider_feed)

    feed.sort(key=lambda a: a["start"], reverse=True)
    data = {
        "updated": dt.datetime.now(TZ).strftime("%a %b %-d, %-I:%M %p"),
        "today": today.isoformat(),
        "riders": rows,
        "feed": feed[:30],
    }

    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        page = f.read()
    blob = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    page = page.replace("/*DATA*/null", blob)
    os.makedirs(os.path.join(HERE, "site"), exist_ok=True)
    with open(os.path.join(HERE, "site", "index.html"), "w", encoding="utf-8") as f:
        f.write(page)
    # A copy of the numbers for the Claude dashboard, which reads it from the repository.
    os.makedirs(os.path.join(HERE, "data"), exist_ok=True)
    with open(os.path.join(HERE, "data", "team.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    ok = sum(1 for r in rows if r["state"] == "ok")
    print(f"Built site/index.html with {ok} of {len(rows)} riders.")


if __name__ == "__main__":
    main()
