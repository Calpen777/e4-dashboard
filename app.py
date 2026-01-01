from flask import Flask, jsonify, render_template, request
import requests, time, os
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo

app = Flask(__name__)
TZ = ZoneInfo("Europe/Stockholm")

API_URL = "https://api.open-meteo.com/v1/forecast"
CACHE = {}   # cache per route-id
TTL = 600    # 10 min

# ====== ROUTES (fasta delsträckor i km) ======
ROUTES = {
    "E4_SKEL_STH": {
        "label": "E4: Skellefteå → Stockholm",
        "points": [
            {"name": "Skellefteå", "lat": 64.7507, "lon": 20.9528, "distance_km_from_prev": 0},
            {"name": "Umeå", "lat": 63.8258, "lon": 20.2630, "distance_km_from_prev": 140},
            {"name": "Örnsköldsvik", "lat": 63.2909, "lon": 18.7153, "distance_km_from_prev": 115},
            {"name": "Sundsvall", "lat": 62.3908, "lon": 17.3069, "distance_km_from_prev": 110},
            {"name": "Hudiksvall", "lat": 61.7274, "lon": 17.1056, "distance_km_from_prev": 100},
            {"name": "Gävle", "lat": 60.6745, "lon": 17.1417, "distance_km_from_prev": 90},
            {"name": "Uppsala", "lat": 59.8586, "lon": 17.6389, "distance_km_from_prev": 95},
            {"name": "Stockholm", "lat": 59.3293, "lon": 18.0686, "distance_km_from_prev": 70},
        ],
    },
    "E6_GBG_MLM": {
        "label": "E6: Göteborg → Malmö",
        "points": [
            {"name": "Göteborg", "lat": 57.7089, "lon": 11.9746, "distance_km_from_prev": 0},
            {"name": "Kungsbacka", "lat": 57.4872, "lon": 12.0761, "distance_km_from_prev": 30},
            {"name": "Varberg", "lat": 57.1056, "lon": 12.2508, "distance_km_from_prev": 55},
            {"name": "Halmstad", "lat": 56.6744, "lon": 12.8578, "distance_km_from_prev": 70},
            {"name": "Helsingborg", "lat": 56.0465, "lon": 12.6945, "distance_km_from_prev": 100},
            {"name": "Malmö", "lat": 55.6050, "lon": 13.0038, "distance_km_from_prev": 70},
        ],
    },
    "E18_KSD_STH": {
        "label": "E18: Karlstad → Stockholm",
        "points": [
            {"name": "Karlstad", "lat": 59.3793, "lon": 13.5036, "distance_km_from_prev": 0},
            {"name": "Örebro", "lat": 59.2753, "lon": 15.2134, "distance_km_from_prev": 110},
            {"name": "Västerås", "lat": 59.6099, "lon": 16.5448, "distance_km_from_prev": 90},
            {"name": "Enköping", "lat": 59.6361, "lon": 17.0777, "distance_km_from_prev": 35},
            {"name": "Stockholm", "lat": 59.3293, "lon": 18.0686, "distance_km_from_prev": 80},
        ],
    },
}

def risk(temp, p, wind, code):
    # WMO-koder (grovt)
    snow = (71 <= code <= 77) or (code in (85, 86))
    freeze = code in (66, 67)

    if wind >= 15:
        return "RÖD", "Mycket blåsigt (≥15 m/s)"
    if p > 0 and -1 <= temp <= 1:
        return "RÖD", "Nederbörd runt 0°C (halka-risk)"
    if freeze:
        return "RÖD", "Underkylt/frysregn"
    if snow and p >= 0.5:
        return "RÖD", "Snöfall"

    if snow or p > 0 or wind >= 10:
        return "GUL", "Vinterförhållanden"
    return "GRÖN", "Stabilt"

def parse_start_time(start_str: str | None):
    # "2026-01-02T10:00" eller "10:00"
    if not start_str:
        return datetime.now(TZ)
    s = start_str.strip()
    try:
        if "T" in s:
            dt = datetime.fromisoformat(s)
            if dt.tzinfo is None:
                return dt.replace(tzinfo=TZ)
            return dt.astimezone(TZ)
        if len(s) == 5 and s[2] == ":":
            now = datetime.now(TZ)
            hh = int(s[:2]); mm = int(s[3:])
            return now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    except Exception:
        pass
    return datetime.now(TZ)

def choose_hourly_at_eta(hourly, eta_dt: datetime):
    times = hourly.get("time", [])
    if not times:
        return None

    target = eta_dt.replace(minute=0, second=0, microsecond=0)
    target_str = target.strftime("%Y-%m-%dT%H:00")

    try:
        idx = times.index(target_str)
    except ValueError:
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

    def get(name, default=None):
        arr = hourly.get(name, [])
        return arr[idx] if idx < len(arr) else default

    return {
        "time": times[idx],
        "temperature_2m": get("temperature_2m", None),
        "precipitation": get("precipitation", 0),
        "wind_speed_10m": get("wind_speed_10m", None),
        "weather_code": get("weather_code", -1),
    }

def fetch_point(lat, lon):
    params = {
        "latitude": lat,
        "longitude": lon,
        "timezone": "Europe/Stockholm",
        "current": "temperature_2m,precipitation,wind_speed_10m,weather_code",
        "hourly": "temperature_2m,precipitation,wind_speed_10m,weather_code",
    }
    r = requests.get(API_URL, params=params, timeout=15)
    r.raise_for_status()
    j = r.json()
    return j.get("current", {}), j.get("hourly", {})

def get_cached_route(route_id: str):
    now = time.time()
    entry = CACHE.get(route_id)
    if not entry or now - entry["ts"] > TTL:
        route = ROUTES[route_id]
        raw = []
        for p in route["points"]:
            cur, hourly = fetch_point(p["lat"], p["lon"])
            raw.append({"p": p, "cur": cur, "hourly": hourly})
        CACHE[route_id] = {
            "ts": now,
            "updated": datetime.now(timezone.utc).isoformat(),
            "raw": raw
        }
    return CACHE[route_id]

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/api/routes")
def routes():
    return jsonify({
        "routes": [{"id": rid, "label": r["label"]} for rid, r in ROUTES.items()]
    })

@app.route("/api/status")
def status():
    route_id = request.args.get("route", "E4_SKEL_STH")
    if route_id not in ROUTES:
        route_id = "E4_SKEL_STH"

    start = parse_start_time(request.args.get("start"))
    try:
        speed_kmh = float(request.args.get("speed", "85"))
        if speed_kmh <= 0:
            speed_kmh = 85.0
    except Exception:
        speed_kmh = 85.0

    cached = get_cached_route(route_id)
    raw = cached["raw"]
    route_label = ROUTES[route_id]["label"]

    # ETA från fasta km
    cum_km = 0.0
    etas = []
    for i, item in enumerate(raw):
        if i == 0:
            cum_km = 0.0
        else:
            cum_km += float(item["p"].get("distance_km_from_prev", 0))
        eta_dt = start + timedelta(seconds=(cum_km / speed_kmh) * 3600)
        etas.append((eta_dt, cum_km))

    out = []
    for i, item in enumerate(raw):
        p = item["p"]; cur = item["cur"]; hourly = item["hourly"]
        eta_dt, km_from_start = etas[i]

        # current
        t_now = float(cur.get("temperature_2m", float("nan")))
        p_now = float(cur.get("precipitation", float("nan")))
        w_now = float(cur.get("wind_speed_10m", float("nan")))
        code_now = int(cur.get("weather_code", -1))
        r_now, reason_now = risk(t_now, p_now, w_now, code_now)

        # eta (närmsta timme)
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
            eta_time = eta_dt.strftime("%Y-%m-%dT%H:00")

        out.append({
            "name": p["name"],
            "lat": p["lat"],
            "lon": p["lon"],
            "km_from_start": km_from_start,
            "now": {"t": t_now, "p": p_now, "w": w_now, "code": code_now, "risk": r_now, "reason": reason_now},
            "eta": {
                "time": eta_time,
                "clock": eta_dt.strftime("%H:%M"),
                "t": t_eta, "p": p_eta, "w": w_eta, "code": code_eta,
                "risk": r_eta, "reason": reason_eta
            }
        })

    return jsonify({
        "route_id": route_id,
        "route_label": route_label,
        "updated": cached["updated"],
        "start": start.isoformat(),
        "speed_kmh": speed_kmh,
        "points": out
    })

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
