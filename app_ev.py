"""
EV Şarj Noktası - Cloud API tabanlı panel (geçici, ESP32 gelince yerel yapacağız)
Port: 8903
"""
import time
import os
import json
import sqlite3
from datetime import datetime
from flask import Flask, jsonify, Response, request
import tinytuya
from apscheduler.schedulers.background import BackgroundScheduler

# Tuya Cloud credentials
ACCESS_ID = os.getenv("TUYA_ACCESS_ID", "8k77w67m953c7pckxdvge")
ACCESS_SECRET = os.getenv("TUYA_ACCESS_SECRET")
DEVICE_ID = "bfd515c92ae48e2725jxzg"

if not ACCESS_SECRET:
    print("HATA: TUYA_ACCESS_SECRET environment variable gerekli")
    exit(1)

c = tinytuya.Cloud(accessId=ACCESS_ID, accessSecret=ACCESS_SECRET, apiRegion="eu")

# ===== Charging History Database =====
DB_PATH = os.path.join(os.path.dirname(__file__), "ev_history.db")

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS sessions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            start_ts INTEGER NOT NULL,
            end_ts INTEGER,
            energy_kwh REAL DEFAULT 0,
            start_soc INTEGER,
            end_soc INTEGER,
            avg_power_kw REAL DEFAULT 0,
            max_power_kw REAL DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS readings (
            ts INTEGER NOT NULL,
            dp TEXT NOT NULL,
            value REAL NOT NULL,
            PRIMARY KEY (ts, dp)
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_readings_ts ON readings(ts)")
    conn.commit()
    conn.close()

init_db()

app = Flask(__name__)
CACHE_SECONDS = 5
_cache = {"ts": 0, "data": None}

# Session tracking state
_session_state = {
    "active": False,
    "start_ts": None,
    "start_energy": None,
    "max_power": 0,
    "power_sum": 0,
    "power_count": 0,
    "start_soc": None,
}

def save_readings(dps):
    """Save raw DP readings to DB (every 10 min collector)"""
    ts = int(time.time())
    conn = sqlite3.connect(DB_PATH)
    for dp, val in dps.items():
        if dp in ("3", "4", "9", "14", "18", "25"):
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO readings (ts, dp, value) VALUES (?, ?, ?)",
                    (ts, dp, float(val) if val is not None else 0)
                )
            except:
                pass
    conn.commit()
    conn.close()

def update_session_tracking(dps):
    """Track charging sessions based on work_state and energy"""
    global _session_state
    work_state = dps.get("3")
    energy_once = dps.get("25")  # energy this session in Wh*100 = 0.01 kWh units
    power_w = dps.get("9")  # power in W
    is_charging = work_state == "charger_charging"
    
    if is_charging and not _session_state["active"]:
        # Session started
        _session_state["active"] = True
        _session_state["start_ts"] = int(time.time())
        _session_state["start_energy"] = energy_once
        _session_state["max_power"] = power_w or 0
        _session_state["power_sum"] = power_w or 0
        _session_state["power_count"] = 1
        _session_state["start_soc"] = dps.get("100")
        print(f"[SESSION] Started at {datetime.fromtimestamp(_session_state['start_ts'])}")
        
    elif _session_state["active"]:
        # Update running session
        if power_w:
            _session_state["max_power"] = max(_session_state["max_power"], power_w)
            _session_state["power_sum"] += power_w
            _session_state["power_count"] += 1
            
        if not is_charging:
            # Session ended
            end_ts = int(time.time())
            end_energy = energy_once
            energy_kwh = ((end_energy - _session_state["start_energy"]) / 10000) if _session_state["start_energy"] and end_energy else 0
            avg_power = (_session_state["power_sum"] / _session_state["power_count"]) / 1000 if _session_state["power_count"] else 0
            max_power_kw = _session_state["max_power"] / 1000
            
            if energy_kwh > 0.01:
                conn = sqlite3.connect(DB_PATH)
                conn.execute("""
                    INSERT INTO sessions (start_ts, end_ts, energy_kwh, avg_power_kw, max_power_kw)
                    VALUES (?, ?, ?, ?, ?)
                """, (_session_state["start_ts"], end_ts, energy_kwh, avg_power, max_power_kw))
                conn.commit()
                conn.close()
                print(f"[SESSION] Ended: {energy_kwh:.2f} kWh, avg {avg_power:.2f} kW, max {max_power_kw:.2f} kW")
            
            _session_state = {
                "active": False, "start_ts": None, "start_energy": None,
                "max_power": 0, "power_sum": 0, "power_count": 0, "start_soc": None
            }

# DP etiketleri
DP_LABELS = {
    "3": "Calisma Durumu",
    "4": "Sarj Akimi (A)",
    "9": "Toplam Guc (kW)",
    "14": "Calisma Modu",
    "18": "Ac/Kapa",
    "25": "Bu Oturum Enerjisi (kWh)",
}

WORK_STATE_MAP = {
    "charger_free": "Bosta",
    "charger_insert": "Takili (sarj yok)",
    "charger_wait": "Bekliyor",
    "charger_charging": "Sarj Ediyor",
    "charger_pause": "Duraklatildi",
    "charger_end": "Tamamlandi",
    "charger_fault": "HATA",
}

WORK_MODE_MAP = {
    "charge_now": "Hemen Sarj",
    "charge_pct": "Yuzdeye Gore",
    "charge_energy": "Enerjiye Gore",
    "charge_schedule": "Zamanli",
}

WRITABLE_DPS = {
    "4":  {"type": "integer", "label": "Sarj Akimi (A)", "min": 6, "max": 32, "step": 1},
    "14": {"type": "enum", "label": "Calisma Modu",
           "options": ["charge_now", "charge_pct", "charge_energy", "charge_schedule"]},
    "18": {"type": "bool", "label": "Ac/Kapa"},
}

ICONS = {
    "3": "⚡", "4": "🔧", "9": "📊", "14": "⚙", "18": "⏻", "25": "🔋",
}


def fetch_status(force=False):
    now = time.time()
    if not force and _cache["data"] and now - _cache["ts"] < CACHE_SECONDS:
        return _cache["data"]
    try:
        result = c.getstatus(DEVICE_ID)
        if result.get("success"):
            dps = {}
            code_to_dp = {
                "work_state": "3",
                "charge_cur_set": "4",
                "power_total": "9",
                "work_mode": "14",
                "switch": "18",
                "charge_energy_once": "25",
            }
            for item in result.get("result", []):
                dp = code_to_dp.get(item["code"])
                if dp:
                    dps[dp] = item["value"]
            # Track session
            update_session_tracking(dps)
            data = {"dps": dps, "raw": result}
            _cache["data"] = data
            _cache["ts"] = now
            return data
    except Exception as e:
        return {"error": str(e), "dps": {}}
    return {"dps": {}}


@app.route("/status")
def status():
    data = fetch_status()
    return jsonify(data)


@app.route("/set", methods=["POST"])
def set_value():
    body = request.get_json(force=True, silent=True) or {}
    dp = str(body.get("dp", ""))
    value = body.get("value")

    if dp not in WRITABLE_DPS:
        return jsonify({"ok": False, "error": f"DP {dp} yazilamaz"}), 400

    spec = WRITABLE_DPS[dp]
    try:
        if spec["type"] == "bool":
            payload_value = bool(value) if not isinstance(value, str) else value.lower() in ("1", "true", "on", "acik")
        elif spec["type"] == "integer":
            val = int(value)
            if not (spec["min"] <= val <= spec["max"]):
                return jsonify({"ok": False, "error": f"Deger {spec['min']}-{spec['max']} araliginda olmali"}), 400
            payload_value = val
        elif spec["type"] == "enum":
            if value not in spec["options"]:
                return jsonify({"ok": False, "error": f"Gecersiz: {spec['options']}"}), 400
            payload_value = value
        else:
            return jsonify({"ok": False, "error": "bilinmeyen tip"}), 400

        commands = [{"code": { "4": "charge_cur_set", "14": "work_mode", "18": "switch" }[dp], "value": payload_value}]
        result = c.sendcommand(DEVICE_ID, commands)
        _cache["data"] = None
        return jsonify({"ok": True, "sent": {"dp": dp, "value": payload_value}, "result": result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500


@app.route("/history")
def history():
    hours = int(request.args.get("hours", 168))
    since = int(time.time()) - hours * 3600
    
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    
    sessions = conn.execute("""
        SELECT start_ts, end_ts, energy_kwh, avg_power_kw, max_power_kw
        FROM sessions
        WHERE start_ts >= ?
        ORDER BY start_ts ASC
    """, (since,)).fetchall()
    
    daily = {}
    for s in sessions:
        date_key = datetime.fromtimestamp(s["start_ts"]).strftime("%Y-%m-%d")
        if date_key not in daily:
            daily[date_key] = {"energy": 0, "sessions": 0, "avg_power": 0, "max_power": 0}
        daily[date_key]["energy"] += s["energy_kwh"]
        daily[date_key]["sessions"] += 1
        daily[date_key]["avg_power"] = max(daily[date_key]["avg_power"], s["avg_power_kw"])
        daily[date_key]["max_power"] = max(daily[date_key]["max_power"], s["max_power_kw"])
    
    sorted_dates = sorted(daily.keys())
    conn.close()
    
    return jsonify({
        "sessions": [dict(s) for s in sessions],
        "daily": {d: daily[d] for d in sorted_dates}
    })


# Start background collector (every 10 minutes)
def collector_job():
    data = fetch_status(force=True)
    dps = data.get("dps", {})
    if dps:
        save_readings(dps)
        print(f"[COLLECTOR] Saved readings: {list(dps.keys())}")

scheduler = BackgroundScheduler(daemon=True)
scheduler.add_job(collector_job, "interval", minutes=10, id="ev_collector", replace_existing=True)
scheduler.start()
print("[SCHEDULER] EV collector started (10 min interval)")


@app.route("/")
def home():
    data = fetch_status()
    dps = data.get("dps", {}) if isinstance(data, dict) else {}

    def disp(code, value):
        if value is None:
            return "—"
        if code == "9":
            return f"{value/1000:.3f}"
        if code == "25":
            return f"{value/100:.2f}"
        if code == "3":
            return WORK_STATE_MAP.get(value, value)
        if code == "14":
            return WORK_MODE_MAP.get(value, value)
        if isinstance(value, bool):
            return "Acik" if value else "Kapali"
        return value

    power_on = dps.get("18") == True
    work_state = dps.get("3", "—")
    current_a = dps.get("4", "—")
    power_kw = dps.get("9", "—")

    hero = f"""
    <div class="hero {'on' if power_on else 'off'}">
      <div class="hero-left">
        <div class="hero-status">{'● AKTIF' if power_on else '○ KAPALI'}</div>
        <div class="hero-mode">{WORK_STATE_MAP.get(work_state, work_state)}</div>
        <div class="hero-sub">{current_a}A • {disp('9', power_kw)} kW</div>
      </div>
      <button class="power-btn {'on' if power_on else ''}" onclick="setDp('18', {str(not power_on).lower()})">⏻</button>
    </div>"""

    sensor_cards = ""
    for code in ["3", "4", "9", "14", "25"]:
        if code not in dps:
            continue
        label = DP_LABELS.get(code, code)
        val = disp(code, dps[code])
        unit = "A" if code == "4" else ("kW" if code == "9" else ("kWh" if code == "25" else ""))
        sensor_cards += f"""
        <div class="sensor-card">
          <div class="sensor-icon">{ICONS.get(code, '📟')}</div>
          <div class="sensor-val">{val}{unit}</div>
          <div class="sensor-label">{label}</div>
        </div>"""

    control_cards = ""
    for dp, spec in WRITABLE_DPS.items():
        cur = dps.get(dp)
        icon = ICONS.get(dp, "⚙")
        if spec["type"] == "bool":
            control_cards += f"""
            <div class="control-card">
              <div class="control-head"><span class="c-icon">{icon}</span>{spec['label']}</div>
              <label class="switch">
                <input type="checkbox" {"checked" if cur else ""} onchange="setDp('{dp}', this.checked)">
                <span class="slider"></span>
              </label>
            </div>"""
        elif spec["type"] == "integer":
            cur_v = cur if cur is not None else spec["min"]
            step_tick = 2
            tick_vals = list(range(spec["min"], spec["max"] + 1, step_tick))
            if spec["min"] not in tick_vals:
                tick_vals.insert(0, spec["min"])
            if spec["max"] not in tick_vals:
                tick_vals.append(spec["max"])
            ticks = "".join(f'<option value="{v}">' for v in tick_vals)
            tick_labels = "".join(f'<span>{v}</span>' for v in tick_vals)
            control_cards += f"""
            <div class="control-card wide">
              <div class="control-head"><span class="c-icon">{icon}</span>{spec['label']}
                <span class="temp-readout" id="val{dp}">{cur_v}A</span></div>
              <input type="range" class="temp-slider" id="dp{dp}" min="{spec['min']}" max="{spec['max']}"
                     step="{spec['step']}" value="{cur_v}"
                     oninput="document.getElementById('val{dp}').textContent=this.value+'A'"
                     onchange="setTemp('{dp}')"
                     list="ticks{dp}">
              <datalist id="ticks{dp}">{ticks}</datalist>
              <div class="tick-labels">{tick_labels}</div>
            </div>"""
        elif spec["type"] == "enum":
            opts = "".join(
                f"<option value='{o}' {'selected' if o == cur else ''}>{WORK_MODE_MAP.get(o, o)}</option>"
                for o in spec["options"]
            )
            control_cards += f"""
            <div class="control-card wide">
              <div class="control-head"><span class="c-icon">{icon}</span>{spec['label']}</div>
              <select class="mode-select" id="dp{dp}" onchange="setEnum('{dp}')">{opts}</select>
            </div>"""

    html = """
    <html><head><meta charset='utf-8'>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>EV Sarj Noktasi</title>
    <style>
      :root {
        --bg: #0b0e14; --card: #151a24; --card2: #1b2230; --accent: #22d3ee;
        --accent2: #a78bfa; --warn: #fb7185; --ok: #34d399; --text: #e5e7eb;
        --muted: #8b93a7;
      }
      * { box-sizing: border-box; }
      body { margin: 0; padding: 24px 16px 60px; min-height: 100vh;
        background: radial-gradient(circle at 20% -10%, #1a2340 0%, var(--bg) 55%);
        color: var(--text); font-family: -apple-system, 'Segoe UI', Roboto, sans-serif; }
      h1 { font-size: 1.3rem; font-weight: 600; margin: 0 0 4px; }
      .subtitle { color: var(--muted); font-size: 0.85rem; margin-bottom: 20px; }
      .wrap { max-width: 720px; margin: 0 auto; }

      .hero {
        display: flex; justify-content: space-between; align-items: center;
        background: linear-gradient(135deg, #1b2a4a, #10131f);
        border: 1px solid #2a3550; border-radius: 20px; padding: 22px 24px;
        margin-bottom: 22px; box-shadow: 0 10px 30px -10px rgba(34,211,238,0.15);
      }
      .hero.on { border-color: rgba(52,211,153,0.4); }
      .hero-status { font-size: 0.8rem; letter-spacing: 1px; color: var(--ok); font-weight: 600; }
      .hero.off .hero-status { color: var(--muted); }
      .hero-mode { font-size: 1.6rem; font-weight: 700; margin: 4px 0; text-transform: capitalize; }
      .hero-sub { color: var(--muted); font-size: 0.85rem; }
      .power-btn {
        width: 58px; height: 58px; border-radius: 50%; border: 2px solid #334;
        background: #1a1f2b; color: #667; font-size: 1.4rem; cursor: pointer;
        transition: all 0.25s;
      }
      .power-btn.on {
        background: radial-gradient(circle, #34d39933, transparent); color: var(--ok);
        border-color: var(--ok); box-shadow: 0 0 18px rgba(52,211,153,0.5);
      }

      .offline-banner {
        background: #3a2a10; border: 1px solid #f59e0b; color: #fde68a;
        padding: 10px 14px; border-radius: 10px; margin-bottom: 16px;
        font-size: 0.8rem; display: none; align-items: center; gap: 8px;
      }

      .section-title {
        font-size: 0.75rem; text-transform: uppercase; letter-spacing: 1.5px;
        color: var(--muted); margin: 28px 0 12px; font-weight: 600;
      }

      .sensor-grid {
        display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
        gap: 10px;
      }
      .sensor-card {
        background: var(--card); border: 1px solid #242b3a; border-radius: 14px;
        padding: 14px 10px; text-align: center; transition: transform 0.15s;
      }
      .sensor-card:hover { transform: translateY(-2px); border-color: var(--accent); }
      .sensor-icon { font-size: 1.3rem; margin-bottom: 6px; }
      .sensor-val { font-size: 1.05rem; font-weight: 700; color: var(--accent); }
      .sensor-label { font-size: 0.65rem; color: var(--muted); margin-top: 3px; line-height: 1.2; }

      .control-grid {
        display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 12px;
      }
      .control-card {
        background: var(--card2); border: 1px solid #2a3344; border-radius: 16px;
        padding: 16px; display: flex; flex-direction: column; justify-content: space-between;
        min-height: 72px;
      }
      .control-card.wide { grid-column: 1 / -1; }
      .control-head {
        display: flex; align-items: center; gap: 8px; font-size: 0.9rem; font-weight: 600;
        margin-bottom: 10px;
      }
      .c-icon { font-size: 1.1rem; }
      .temp-readout { margin-left: auto; color: var(--accent); font-weight: 700; }

      .switch { position: relative; display: inline-block; width: 50px; height: 28px; align-self: flex-start; }
      .switch input { opacity: 0; width: 0; height: 0; }
      .slider {
        position: absolute; cursor: pointer; inset: 0; background: #2a3344;
        border-radius: 28px; transition: 0.3s;
      }
      .slider::before {
        content: ""; position: absolute; height: 22px; width: 22px; left: 3px; bottom: 3px;
        background: #ccc; border-radius: 50%; transition: 0.3s;
      }
      input:checked + .slider { background: var(--ok); }
      input:checked + .slider::before { transform: translateX(22px); background: #fff; }

      .temp-slider {
        -webkit-appearance: none; width: 100%; height: 6px; border-radius: 3px;
        background: linear-gradient(90deg, var(--accent2), var(--accent)); outline: none;
      }
      .temp-slider::-webkit-slider-thumb {
        -webkit-appearance: none; width: 22px; height: 22px; border-radius: 50%;
        background: #fff; border: 3px solid var(--accent); cursor: pointer;
      }
      .tick-labels {
        display: flex; justify-content: space-between; margin-top: 4px;
        font-size: 8px; color: var(--muted); user-select: none;
      }
      .tick-labels span { width: 0; }

      .chart-container {
        background: var(--card); border: 1px solid #242b3a; border-radius: 16px;
        padding: 16px; margin-top: 12px; min-height: 280px;
      }
      .chart-controls {
        display: flex; gap: 8px; justify-content: center; margin-top: 10px;
      }
      .chart-btn {
        padding: 6px 14px; border-radius: 20px; border: 1px solid #2a3344;
        background: #10141d; color: var(--muted); font-size: 0.75rem;
        cursor: pointer; transition: all 0.2s;
      }
      .chart-btn:hover { border-color: var(--accent); color: var(--text); }
      .chart-btn.active { background: var(--accent); color: #0b0e14; border-color: var(--accent); }

      .mode-select {
        width: 100%; padding: 10px; border-radius: 10px; border: 1px solid #2a3344;
        background: #10141d; color: var(--text); font-size: 0.95rem;
      }

      #toast {
        position: fixed; bottom: 20px; left: 50%; transform: translateX(-50%) translateY(100px);
        background: #1b2230; border: 1px solid #334; padding: 10px 18px; border-radius: 12px;
        font-size: 0.85rem; transition: transform 0.3s; box-shadow: 0 8px 24px rgba(0,0,0,0.4);
      }
      #toast.show { transform: translateX(-50%) translateY(0); }
      #toast.ok { border-color: var(--ok); color: var(--ok); }
      #toast.err { border-color: var(--warn); color: var(--warn); }
    </style>
    <script>
      async function send(dp, value) {
        const res = await fetch('/set', {
          method: 'POST', headers: {'Content-Type':'application/json'},
          body: JSON.stringify({dp: dp, value: value})
        });
        const data = await res.json();
        const toast = document.getElementById('toast');
        toast.textContent = data.ok ? '✓ Gonderildi' : ('✗ ' + data.error);
        toast.className = 'show ' + (data.ok ? 'ok' : 'err');
        setTimeout(() => toast.classList.remove('show'), 2500);
        if (!data.ok && data.result && data.result.result === false) {
          document.getElementById('offline-banner').style.display = 'flex';
        }
        if (data.ok) setTimeout(() => location.reload(), 1500);
      }
      function setDp(dp, value) { send(dp, value); }
      const _debounceTimers = {};
      function setTemp(dp) {
        if (_debounceTimers[dp]) clearTimeout(_debounceTimers[dp]);
        _debounceTimers[dp] = setTimeout(() => {
          send(dp, parseFloat(document.getElementById('dp'+dp).value));
        }, 450);
      }
      function setEnum(dp) {
        const val = document.getElementById('dp'+dp).value;
        console.log('setEnum', dp, val);
        send(dp, val);
      }

      // Auto-refresh every 10s
      setInterval(() => location.reload(), 10000);
    </script>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1/dist/chart.umd.min.js"></script>
    <script>
      let historyChart = null;
      let currentHours = 24;
      
      async function loadHistory(hours) {
        currentHours = hours;
        document.querySelectorAll('.chart-btn').forEach(b => 
          b.classList.toggle('active', parseInt(b.dataset.hours) === hours));
        const res = await fetch('/history?hours=' + hours);
        const data = await res.json();
        renderChart(data.daily);
      }
      
      function renderChart(daily) {
        const labels = Object.keys(daily);
        const energy = labels.map(d => daily[d].energy.toFixed(2));
        const sessions = labels.map(d => daily[d].sessions);
        const maxPower = labels.map(d => daily[d].max_power.toFixed(1));
        
        const ctx = document.getElementById('historyChart').getContext('2d');
        if (historyChart) historyChart.destroy();
        
        historyChart = new Chart(ctx, {
          type: 'bar',
          data: {
            labels: labels.map(d => d.slice(5)),
            datasets: [{
              label: 'Gunluk kWh',
              data: energy,
              backgroundColor: 'rgba(34, 211, 238, 0.6)',
              borderColor: '#22d3ee',
              borderWidth: 1,
              borderRadius: 6,
              yAxisID: 'y',
            }, {
              label: 'Max Guc (kW)',
              data: maxPower,
              type: 'line',
              borderColor: '#fb7185',
              backgroundColor: 'rgba(251, 113, 133, 0.2)',
              borderWidth: 2,
              tension: 0.3,
              fill: false,
              pointRadius: 4,
              pointHoverRadius: 6,
              yAxisID: 'y1',
            }]
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            interaction: { mode: 'index', intersect: false },
            plugins: {
              legend: { display: true, labels: { color: '#e5e7eb', font: { size: 11 } } },
              tooltip: {
                callbacks: {
                  label: ctx => {
                    const d = labels[ctx.dataIndex];
                    if (ctx.datasetIndex === 0) {
                      return '  ' + ctx.raw + ' kWh (' + sessions[ctx.dataIndex] + ' oturum)';
                    }
                    return '  Max ' + ctx.raw + ' kW';
                  }
                }
              }
            },
            scales: {
              x: { grid: { color: '#2a3344' }, ticks: { color: '#8b93a7', font: { size: 10 } } },
              y: { 
                type: 'linear', position: 'left',
                title: { display: true, text: 'kWh', color: '#8b93a7', font: { size: 10 } },
                grid: { color: '#2a3344' }, ticks: { color: '#8b93a7', font: { size: 10 } },
                beginAtZero: true
              },
              y1: {
                type: 'linear', position: 'right',
                title: { display: true, text: 'kW', color: '#fb7185', font: { size: 10 } },
                grid: { drawOnChartArea: false }, ticks: { color: '#fb7185', font: { size: 10 } },
                beginAtZero: true
              }
            }
          }
        });
      }
      
      document.querySelectorAll('.chart-btn').forEach(btn => {
        btn.addEventListener('click', () => loadHistory(parseInt(btn.dataset.hours)));
      });
      
      // Initial load
      loadHistory(24);
      
      // Auto-refresh chart every 10 min
      setInterval(() => loadHistory(currentHours), 600000);
    </script>
    </head>
    <body>
      <div class="wrap">
        <h1>EV Sarj Noktasi</h1>
        <div class="subtitle">Cloud API uzerinden (gecici — ESP32 gelince yerel olacak)</div>

        """ + hero + """

        <div id="offline-banner" class="offline-banner">
          ⚠ Cihaz Tuya Cloud'da <b>offline</b> — komutlar calismayabilir. ESP32 Bluetooth proxy kuruldugunda yerel kontrol aktif olacak.
        </div>

        <div class="section-title">Kontroller</div>
        <div class="control-grid">""" + control_cards + """</div>

        <div class="section-title">Durum & Sensorler</div>
        <div class="sensor-grid">""" + sensor_cards + """</div>

        <div class="section-title">Sarj Gecmisi (Gunluk kWh)</div>
        <div class="chart-container">
          <canvas id="historyChart"></canvas>
        </div>
        <div class="chart-controls">
          <button class="chart-btn active" data-hours="24">24s</button>
          <button class="chart-btn" data-hours="168">7g</button>
          <button class="chart-btn" data-hours="720">30g</button>
        </div>
      </div>
      <div id="toast"></div>
    </body></html>
    """
    return Response(html, mimetype="text/html")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8903)
