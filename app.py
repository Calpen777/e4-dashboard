from flask import Flask, jsonify, render_template
import requests, time
from datetime import datetime, timezone

app = Flask(__name__)

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

API_URL="https://api.open-meteo.com/v1/forecast"
CACHE={"ts":0,"data":None}
TTL=600

def risk(temp,p,wind,code):
    snow = 71<=code<=77 or code in (85,86)
    freeze = code in (66,67)
    if wind>=15: return "RÖD","Mycket blåsigt"
    if p>0 and -1<=temp<=1: return "RÖD","Nederbörd runt 0°C"
    if freeze: return "RÖD","Underkylt/frysregn"
    if snow and p>=0.5: return "RÖD","Snöfall"
    if snow or p>0 or wind>=10: return "GUL","Vinterförhållanden"
    return "GRÖN","Stabilt"

def fetch():
    out=[]
    for p in POINTS:
        r=requests.get(API_URL,params={
            "latitude":p["lat"],"longitude":p["lon"],
            "current":"temperature_2m,precipitation,wind_speed_10m,weather_code",
            "timezone":"Europe/Stockholm"
        },timeout=12)
        c=r.json()["current"]
        rk,rs=risk(c["temperature_2m"],c["precipitation"],c["wind_speed_10m"],c["weather_code"])
        out.append({**p,
            "t":c["temperature_2m"],
            "p":c["precipitation"],
            "w":c["wind_speed_10m"],
            "code":c["weather_code"],
            "risk":rk,"reason":rs})
    return out

def get():
    now=time.time()
    if not CACHE["data"] or now-CACHE["ts"]>TTL:
        CACHE["data"]={"updated":datetime.now(timezone.utc).isoformat(),"points":fetch()}
        CACHE["ts"]=now
    return CACHE["data"]

@app.route("/")
def i(): return render_template("index.html")

@app.route("/api/status")
def s(): return jsonify(get())

if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
