from flask import Flask, jsonify, render_template, request
import requests, time, math
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

app = Flask(__name__)

TZ = ZoneInfo("Europe/Stockholm")

POINTS = [
    {"name":"Skellefteå","lat":64.7507,"lon":20.9528},
    {"name":"Umeå","lat":63.8258,"lon":20.2630},
    {"name":"Örnsköldsvik","lat":63.2909,"lon":18.7153},
    {"name":"Sundsvall","lat":62.3908,"lon":17.3069},
    {"name":"Hudiksvall","lat":61.7274,"lon":17.1056},
    {"name":"Gävle","lat":60.6745,"lon":17.1417},
    {"name":"Uppsala","lat":59.8586,"lon":17.6389},
    {"name":"Stockholm","lat":59.3293,"lon":18.0686},
]

API_URL = "https://api.open-meteo.com/v1/forecast"

CACHE = {"ts": 0, "data": None}
TTL = 600  # 10 min cache

def haversine_km(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def risk(temp, p, wind, code):
    snow = (71 <= code <= 77) or (code in (85, 86))
    freeze = code in (66, 67)

    if wind >= 15: return "RÖD", "Mycket blåsigt (≥15 m/s)"
    if p > 0 and -1 <= temp <= 1: return "RÖD", "Nederbörd runt 0°C (halka-risk)"
    if freeze: return "RÖD", "Underkylt/frysregn"
    if snow and p >= 0.5: return "RÖD", "Snöfall"

    if snow or p > 0 or wind >= 10: return "GUL", "Vinterförhållanden"
    return "GRÖN", "Stabilt"

def parse_start_time(start_str: str | None):
    """
    start kan vara:
    - "2026-01-02T10:00"
    - eller bara "10:00" (då används dagens datum i Stockholm-tz)
    Om inget anges: nu.
    """
    if not start_str:
        return datetime.now(TZ)

    s = start_str.strip()
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=TZ)
            else:
                dt = dt.astimezone(TZ)
            return dt
        if len(s) == 5 and s[2] == ":":
            now = datetime.now(TZ)
            hh = int(s[:2]); mm = int(s[3:])
            return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        pass
    return datetime.now(TZ)

def choose_hourly_at_eta(hourly, eta_dt: datetime):
    """
    hourly innehåller:
      time: ["2026-..."], precipitation: [...], temperature_2m: [...], wind_speed_10m: [...], weather_code: [...]
    Välj närmaste timme till ETA.
    """
    times = hourly.get("time", [])
    if not times:
        return None

    # ETA i lokal tid: Open-Meteo returnerar tider i den timezone vi anger (Europe/Stockholm)
    eta_str = eta_dt.strftime("%Y-%m-%dT%H:00")
    # exact match om möjligt
    try:
        idx = times.index(eta_str)
    except ValueError:
        # annars hitta närmaste (grov: jämför str->dt)
        target = eta_dt.replace(minute=0, second=0, microsecond=0)
        best_i, best_diff = 0, None
        for i, ts in enumerate(times):
            try:
                t = datetime.fromisoformat(ts).replace(tzinfo=TZ)
            except Exception:
                continue
            diff = abs((t - target).total_seconds())
            if best_diff is None or diff < best_diff:
                best_diff = diff
                best_i = i
        idx = best_i

    def get(arrname, default=None):
        arr = hourly.get(arrname, [])
        return arr[idx] if idx < len(arr) else default

    return {
        "time": times[idx],
        "precipitation": get("precipitation", 0),
        "temperature_2m": get("temperature_2m", None),
        "wind_speed_10m": get("wind_speed_10m", None),
        "weather_code": get("weather_code", -1),
    }

def fetch_point(point):
    params = {
        "latitude": point["lat"],
        "longitude": point["lon"],
        "timezone": "Europe/Stockholm",
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,precipitation,wind_speed_10m,weather_code",
    }
    r = requests.get(API_URL, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    return j.get("current", {}), j.get("hourly", {})

def build_data():
    # Hämta väder (current + hourly) för alla punkter
    all_points = []
    for p in POINTS:
        cur, hourly = fetch_point(p)
        all_points.append({"p": p, "cur": cur, "hourly": hourly})
    return all_points

def get_cached():
    now = time.time()
    if not CACHE["data"] or now - CACHE["ts"] > TTL:
        CACHE["data"] = {
            "updated": datetime.now(timezone.utc).isoformat(),
            "raw": build_data(),
        }
        CACHE["ts"] = now
    return CACHE["data"]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/status")
def status():
    # starttid och snitthastighet kan skickas via query
    # ex: /api/status?start=10:00&speed=85
    start = parse_start_time(request.args.get("start"))
    try:
        speed_kmh = float(request.args.get("speed", "85"))  # default 85 km/h
        if speed_kmh <= 0:
            speed_kmh = 85.0
    except Exception:
        speed_kmh = 85.0

    cached = get_cached()
    raw = cached["raw"]

    # Beräkna avstånd och ETA kumulativt från startpunkt
    etas = []
    cum_hours = 0.0
    for i, item in enumerate(raw):
        if i == 0:
            cum_hours = 0.0
        else:
            prev = raw[i-1]["p"]
            curp = item["p"]
            dist_km = haversine_km(prev["lat"], prev["lon"], curp["lat"], curp["lon"])
            cum_hours += dist_km / speed_kmh
        eta_dt = start + (cum_hours * 3600) * datetime.resolution  # trick-free? -> nope
        # ovan rad funkar inte korrekt; gör säkert:
        from datetime import timedelta
        eta_dt = start + timedelta(seconds=cum_hours * 3600)
        etas.append(eta_dt)

    out = []
    for i, item in enumerate(raw):
        p = item["p"]
        cur = item["cur"]
        hourly = item["hourly"]

        # current
        t_now = float(cur.get("temperature_2m", float("nan")))
        p_now = float(cur.get("precipitation", float("nan")))
        w_now = float(cur.get("wind_speed_10m", float("nan")))
        code_now = int(cur.get("weather_code", -1))
        r_now, reason_now = risk(t_now, p_now, w_now, code_now)

        # eta forecast (närmsta timme)
        eta_dt = etas[i]
        eta_pick = choose_hourly_at_eta(hourly, eta_dt)
        if eta_pick:
            t_eta = float(eta_pick["temperature_2m"]) if eta_pick["temperature_2m"] is not None else float("nan")
            p_eta = float(eta_pick["precipitation"]) if eta_pick["precipitation"] is not None else float("nan")
            w_eta = float(eta_pick["wind_speed_10m"]) if eta_pick["wind_speed_10m"] is not None else float("nan")
            code_eta = int(eta_pick["weather_code"])
            r_eta, reason_eta = risk(t_eta, p_eta, w_eta, code_eta)
            eta_time = eta_pick["time"]
        else:
            t_eta = p_eta = w_eta = float("nan")
            code_eta = -1
            r_eta, reason_eta = "–", "Ingen prognos"
            eta_time = eta_dt.strftime("%Y-%m-%dT%H:%M")

        out.append({
            "name": p["name"],
            "lat": p["lat"],
            "lon": p["lon"],

            "now": {"t": t_now, "p": p_now, "w": w_now, "code": code_now, "risk": r_now, "reason": reason_now},
            "eta": {
                "start": start.isoformat(),
                "time": eta_time,
                "t": t_eta, "p": p_eta, "w": w_eta, "code": code_eta,
                "risk": r_eta, "reason": reason_eta
            }
        })

    return jsonify({
        "updated": cached["updated"],
        "start": start.isoformat(),
        "speed_kmh": speed_kmh,
        "points": out
    })

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
