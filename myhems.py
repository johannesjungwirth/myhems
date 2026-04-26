"""
myhems v0.7.0
- Config automatisch per Hostname geladen (configs/config_<hostname>.yaml)
- config.yaml als Fallback
- Tagesenergie: PV, Einspeisung, Bezug, Eigenverbrauch (heute + gestern) in energy_history.json
- Regelparameter umbenannt: hochschalten_schwelle / runterschalten_schwelle (min_pv entfernt)
- SOC-Abschaltung stufenweise mit delay statt Sofortabschaltung
"""

import time
import socket
import json
import logging
import threading
import sys
import os
from datetime import date, timedelta
from itertools import combinations as iter_combinations
import requests
import yaml
from flask import Flask, jsonify, render_template_string

VERSION = "0.7.0"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("myhems")

# ─── KONFIGURATION ──────────────────────────────────────────────────────────

def lade_config():
    basis = os.path.dirname(__file__)
    hostname = socket.gethostname()
    teil = hostname.split("-")[-1] if "-" in hostname else hostname
    kandidaten = [
        os.path.join(basis, "configs", f"config_{teil}.yaml"),
        os.path.join(basis, "config.yaml"),
    ]
    for pfad in kandidaten:
        if os.path.exists(pfad):
            with open(pfad) as f:
                cfg = yaml.safe_load(f)
            log.info(f"Config geladen: {pfad} (Standort: '{cfg['standort']['name']}')")
            return cfg
    log.error(f"Keine Config gefunden. Gesucht: {kandidaten}")
    sys.exit(1)

CFG = lade_config()

STANDORT_NAME   = CFG["standort"]["name"]
G               = CFG["geraete"]
MARSTEK_IP      = G["marstek_ip"]
MARSTEK_PORT    = int(G.get("marstek_port", 30000))

def parse_shelly(cfg, default_typ="EM"):
    if isinstance(cfg, str):
        return cfg, default_typ
    return cfg["ip"], cfg.get("typ", default_typ)

SHELLY_PV_IP,      SHELLY_PV_TYP      = parse_shelly(G["shelly_pv"],      "EM")
SHELLY_NETZ_IP,    SHELLY_NETZ_TYP    = parse_shelly(G["shelly_netz"],    "EM")
SHELLY_MARSTEK_IP, SHELLY_MARSTEK_TYP = parse_shelly(G["shelly_marstek"], "Switch")

log.info(f"PV:     {SHELLY_PV_IP} typ={SHELLY_PV_TYP}")
log.info(f"Netz:   {SHELLY_NETZ_IP} typ={SHELLY_NETZ_TYP}")
log.info(f"Marstek:{SHELLY_MARSTEK_IP} typ={SHELLY_MARSTEK_TYP}")

_heizstab_cfg = G["shelly_heizstab"]
if isinstance(_heizstab_cfg, (str, dict)):
    HEIZSTAB_MODUS   = "single"
    ip, _ = parse_shelly(_heizstab_cfg, "Switch")
    HEIZSTAB_SHELLYS = [ip]
else:
    HEIZSTAB_MODUS   = "multi"
    HEIZSTAB_SHELLYS = [parse_shelly(e, "Switch")[0] for e in _heizstab_cfg]

log.info(f"Heizstab-Modus: {HEIZSTAB_MODUS}, Shellys: {HEIZSTAB_SHELLYS}")

R = CFG["regelparameter"]
MIN_SOC                 = int(R["min_soc"])
HOCHSCHALTEN_SCHWELLE   = int(R["hochschalten_schwelle"])
RUNTERSCHALTEN_SCHWELLE = int(R["runterschalten_schwelle"])
DELAY                   = int(R["delay"])
POLL_INTERVAL           = int(R.get("poll_intervall", 5))
SOC_CACHE_MAX           = int(R.get("soc_cache_max", 60))

log.info(f"Regelparameter: min_soc={MIN_SOC}% hoch={HOCHSCHALTEN_SCHWELLE}W runter={RUNTERSCHALTEN_SCHWELLE}W delay={DELAY}s")

# ─── HEIZSTAB-KOMBINATIONEN ─────────────────────────────────────────────────

RELAIS_LEISTUNG = CFG["heizstab"]["relais"]
ANZAHL_RELAIS   = len(RELAIS_LEISTUNG)

def berechne_kombinationen(relais_leistung):
    n = len(relais_leistung)
    kombinationen = {}
    for r in range(1, n + 1):
        for combo in iter_combinations(range(n), r):
            leistung = sum(relais_leistung[i] for i in combo)
            maske = [i in combo for i in range(n)]
            if leistung not in kombinationen:
                kombinationen[leistung] = maske
            else:
                if sum(maske) < sum(kombinationen[leistung]):
                    kombinationen[leistung] = maske
    sortiert = sorted(kombinationen.items())
    result = [(0, [False] * n)] + [(w, m) for w, m in sortiert]
    return result

STUFEN = berechne_kombinationen(RELAIS_LEISTUNG)
ANZAHL_STUFEN = len(STUFEN) - 1

log.info(f"Heizstab: {ANZAHL_RELAIS} Relais {RELAIS_LEISTUNG}W → {ANZAHL_STUFEN} Kombinationen")
for i, (w, m) in enumerate(STUFEN):
    relais_an = [j+1 for j, on in enumerate(m) if on]
    log.info(f"  Kombination {i}: {w}W – Relais {relais_an}")

THERMOSTAT_SCHWELLE = {i: STUFEN[i][0] // 2 for i in range(1, len(STUFEN))}

# ─── TAGESENERGIE ───────────────────────────────────────────────────────────

HISTORY_PFAD  = os.path.join(os.path.dirname(__file__), "energy_history.json")
_history_lock = threading.Lock()

def _leerer_tag():
    return {"pv_wh": 0.0, "einspeisung_wh": 0.0, "bezug_wh": 0.0}

def _lade_history():
    if not os.path.exists(HISTORY_PFAD):
        return {}
    try:
        with open(HISTORY_PFAD) as f:
            return json.load(f)
    except Exception as e:
        log.warning(f"energy_history.json Ladefehler: {e} – starte neu")
        return {}

def _speichere_history(history):
    heute   = date.today().isoformat()
    gestern = (date.today() - timedelta(days=1)).isoformat()
    bereinigt = {k: v for k, v in history.items() if k in (heute, gestern)}
    try:
        with open(HISTORY_PFAD, "w") as f:
            json.dump(bereinigt, f)
    except Exception as e:
        log.warning(f"energy_history.json Schreibfehler: {e}")

_history = _lade_history()
log.info(f"Tagesenergie geladen: {list(_history.keys())}")

def akkumuliere_energie(pv_w, netz_w):
    global _history
    if pv_w is None or netz_w is None:
        return
    heute   = date.today().isoformat()
    delta_h = POLL_INTERVAL / 3600.0
    with _history_lock:
        if heute not in _history:
            _history[heute] = _leerer_tag()
            log.info(f"Neuer Tag: {heute} – Tageszähler zurückgesetzt")
        _history[heute]["pv_wh"] += pv_w * delta_h
        if netz_w < 0:
            _history[heute]["einspeisung_wh"] += abs(netz_w) * delta_h
        else:
            _history[heute]["bezug_wh"] += netz_w * delta_h
        _speichere_history(_history)

def hole_tagesenergie():
    heute   = date.today().isoformat()
    gestern = (date.today() - timedelta(days=1)).isoformat()

    def aufbereite(tag_str):
        with _history_lock:
            d = _history.get(tag_str, _leerer_tag())
        pv  = round(d["pv_wh"] / 1000, 2)
        ein = round(d["einspeisung_wh"] / 1000, 2)
        bez = round(d["bezug_wh"] / 1000, 2)
        ev  = round(max(pv - ein, 0), 2)
        return {"pv_kwh": pv, "einspeisung_kwh": ein, "bezug_kwh": bez, "eigenverbrauch_kwh": ev}

    return {"heute": aufbereite(heute), "gestern": aufbereite(gestern)}

# ─── GLOBALER ZUSTAND ───────────────────────────────────────────────────────

_state = {
    "pv":             None,
    "netz":           None,
    "marstek":        None,
    "soc":            None,
    "stufe":          0,
    "heizstab_w":     0,
    "relais":         [False] * ANZAHL_RELAIS,
    "hausverbrauch":  None,
    "thermostat_aus": False,
    "regeltext":      "Starte...",
    "regeltyp":       "info",
    "timestamp":      0,
}
_state_lock = threading.Lock()

_soc_cache       = None
_soc_cache_zeit  = 0
_regel_stufe     = 0
_letzter_wechsel = 0

# ─── GERÄTE-ZUGRIFF ─────────────────────────────────────────────────────────

def shelly_get(ip, path, timeout=3):
    try:
        r = requests.get(f"http://{ip}{path}", timeout=timeout)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Shelly {ip} Fehler: {e}")
        return None

def lese_em_leistung(ip, typ):
    if typ == "EM1":
        data = shelly_get(ip, "/rpc/EM1.GetStatus?id=0")
        return round(data.get("act_power", 0)) if data else None
    else:
        data = shelly_get(ip, "/rpc/EM.GetStatus?id=0")
        return round(data.get("total_act_power", 0)) if data else None

def lese_pv():
    w = lese_em_leistung(SHELLY_PV_IP, SHELLY_PV_TYP)
    return round(abs(w)) if w is not None else None

def lese_netz():
    return lese_em_leistung(SHELLY_NETZ_IP, SHELLY_NETZ_TYP)

def lese_marstek_leistung():
    data = shelly_get(SHELLY_MARSTEK_IP, "/rpc/Switch.GetStatus?id=0")
    return round(data.get("apower", 0)) if data else None

def lese_marstek_soc():
    global _soc_cache, _soc_cache_zeit
    anfrage = json.dumps({"id": 1, "method": "ES.GetStatus", "params": {"id": 0}}).encode()
    for versuch in range(3):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("", MARSTEK_PORT))
            sock.settimeout(3)
            sock.sendto(anfrage, (MARSTEK_IP, MARSTEK_PORT))
            antwort, _ = sock.recvfrom(4096)
            sock.close()
            data = json.loads(antwort.decode())
            soc = data.get("result", {}).get("bat_soc")
            if soc is not None:
                _soc_cache = round(float(soc), 1)
                _soc_cache_zeit = time.time()
                return _soc_cache
        except Exception as e:
            log.warning(f"Marstek UDP Versuch {versuch+1}/3: {e}")
            try: sock.close()
            except: pass
            time.sleep(0.5)
    if _soc_cache is not None and (time.time() - _soc_cache_zeit) < SOC_CACHE_MAX:
        log.info(f"SOC aus Cache: {_soc_cache}%")
        return _soc_cache
    return None

def setze_relais(nr, ein):
    aktion = "true" if ein else "false"
    if HEIZSTAB_MODUS == "single":
        return shelly_get(HEIZSTAB_SHELLYS[0], f"/rpc/Switch.Set?id={nr}&on={aktion}") is not None
    else:
        return shelly_get(HEIZSTAB_SHELLYS[nr], f"/rpc/Switch.Set?id=0&on={aktion}") is not None

def setze_kombination(neue_stufe, alte_stufe):
    _, alte_maske = STUFEN[alte_stufe]
    _, neue_maske = STUFEN[neue_stufe]
    ok = True
    for i, (alt, neu) in enumerate(zip(alte_maske, neue_maske)):
        if alt and not neu:
            ok &= setze_relais(i, False)
    for i, (alt, neu) in enumerate(zip(alte_maske, neue_maske)):
        if not alt and neu:
            ok &= setze_relais(i, True)
    return ok

# ─── REGELLOGIK ─────────────────────────────────────────────────────────────

def bestimme_regeltext(pv, marstek, netz, soc, stufe, delay_ok):
    if pv is None or marstek is None:
        return "Messfehler – Gerät nicht erreichbar", "error"
    if soc is not None and soc < MIN_SOC and stufe > 0:
        return f"Ladestand {soc} % < Minimum {MIN_SOC} % – schalte stufenweise ab", "blocked"
    if not delay_ok:
        return "Wartezeit zwischen Schaltvorgängen läuft", "waiting"
    if stufe == ANZAHL_STUFEN:
        return f"Maximalstufe {STUFEN[stufe][0]} W aktiv", "max"
    ueberschuss_signal = (marstek > HOCHSCHALTEN_SCHWELLE) or (netz is not None and netz < -HOCHSCHALTEN_SCHWELLE)
    if ueberschuss_signal and (soc is None or soc >= MIN_SOC):
        grund = f"Batterie lädt {marstek} W" if marstek > HOCHSCHALTEN_SCHWELLE else f"Einspeisung {abs(netz)} W"
        return f"{grund} > {HOCHSCHALTEN_SCHWELLE} W – Hochschalten möglich", "ready"
    if stufe > 0 and marstek < -RUNTERSCHALTEN_SCHWELLE:
        return f"Batterie entlädt {abs(marstek)} W > {RUNTERSCHALTEN_SCHWELLE} W – Runterschalten", "down"
    if marstek > 0:
        return f"Batterie lädt {marstek} W – noch {HOCHSCHALTEN_SCHWELLE - marstek} W bis Hochschalten", "waiting"
    return "Kein Überschuss – Heizstab hält Stufe", "holding"

def regelschleife():
    global _regel_stufe, _letzter_wechsel
    log.info(f"myhems v{VERSION} – Standort {STANDORT_NAME} gestartet")
    for r in range(ANZAHL_RELAIS):
        setze_relais(r, False)
    log.info("Alle Relais beim Start ausgeschaltet")

    while True:
        try:
            pv      = lese_pv()
            netz    = lese_netz()
            marstek = lese_marstek_leistung()
            soc     = lese_marstek_soc()
            stufe   = _regel_stufe
            now     = time.time()
            delay_ok = (now - _letzter_wechsel) >= DELAY

            hausverbrauch = None
            if pv is not None and netz is not None and marstek is not None:
                hausverbrauch = round(pv + netz - marstek)

            thermostat_aus = False
            if stufe > 0 and hausverbrauch is not None:
                if hausverbrauch < THERMOSTAT_SCHWELLE.get(stufe, 0):
                    thermostat_aus = True

            regeltext, regeltyp = bestimme_regeltext(pv, marstek, netz, soc, stufe, delay_ok)

            akkumuliere_energie(pv, netz)

            with _state_lock:
                _state.update({
                    "pv":             pv,
                    "netz":           netz,
                    "marstek":        marstek,
                    "soc":            soc,
                    "stufe":          stufe,
                    "heizstab_w":     STUFEN[stufe][0],
                    "relais":         list(STUFEN[stufe][1]),
                    "hausverbrauch":  hausverbrauch,
                    "thermostat_aus": thermostat_aus,
                    "regeltext":      regeltext,
                    "regeltyp":       regeltyp,
                    "timestamp":      int(now),
                })

            if any(v is None for v in [pv, marstek]):
                log.warning("Messwerte unvollständig – überspringe Regelzyklus")
                time.sleep(POLL_INTERVAL)
                continue

            if not delay_ok:
                time.sleep(POLL_INTERVAL)
                continue

            # SOC-Schutz: stufenweise abschalten mit delay
            if stufe > 0 and soc is not None and soc < MIN_SOC:
                neu = stufe - 1
                log.info(f"🔋 SOC {soc}% < {MIN_SOC}% – Stufe {stufe}→{neu} ({STUFEN[neu][0]}W)")
                if setze_kombination(neu, stufe):
                    _regel_stufe = neu
                    _letzter_wechsel = now
                time.sleep(POLL_INTERVAL)
                continue

            ueberschuss_signal = (marstek > HOCHSCHALTEN_SCHWELLE) or (netz is not None and netz < -HOCHSCHALTEN_SCHWELLE)

            if stufe < ANZAHL_STUFEN and ueberschuss_signal and (soc is None or soc >= MIN_SOC):
                neu = stufe + 1
                grund = f"Marstek lädt {marstek}W" if marstek > HOCHSCHALTEN_SCHWELLE else f"Einspeisung {abs(netz)}W"
                log.info(f"▲ Kombination {stufe}→{neu} ({STUFEN[neu][0]}W): {grund}")
                if setze_kombination(neu, stufe):
                    _regel_stufe = neu
                    _letzter_wechsel = now
                time.sleep(POLL_INTERVAL)
                continue

            if stufe > 0 and marstek < -RUNTERSCHALTEN_SCHWELLE:
                neu = stufe - 1
                log.info(f"▼ Kombination {stufe}→{neu} ({STUFEN[neu][0]}W): Marstek entlädt {abs(marstek)}W")
                if setze_kombination(neu, stufe):
                    _regel_stufe = neu
                    _letzter_wechsel = now

        except Exception as e:
            log.error(f"Fehler in Regelschleife: {e}")

        time.sleep(POLL_INTERVAL)

# ─── FLASK ───────────────────────────────────────────────────────────────────

app = Flask(__name__)

@app.route("/api/status")
def api_status():
    with _state_lock:
        state = dict(_state)
    state["version"]         = VERSION
    state["standort"]        = STANDORT_NAME
    state["relais_leistung"] = RELAIS_LEISTUNG
    state["stufen"]          = [(w, m) for w, m in STUFEN]
    state["tagesenergie"]    = hole_tagesenergie()
    state["params"] = {
        "MIN_SOC":                  MIN_SOC,
        "HOCHSCHALTEN_SCHWELLE":    HOCHSCHALTEN_SCHWELLE,
        "RUNTERSCHALTEN_SCHWELLE":  RUNTERSCHALTEN_SCHWELLE,
        "DELAY":                    DELAY,
    }
    return jsonify(state)

# ─── DASHBOARD ───────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="de">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>myhems {{ standort }}</title>
<link href="https://fonts.googleapis.com/css2?family=Syncopate:wght@400;700&family=Share+Tech+Mono&display=swap" rel="stylesheet">
<style>
  :root {
    --bg:#030712;--bg2:#050d1a;
    --border:#164e63;--border2:#0f172a;
    --text:#e0f2fe;--cyan:#38bdf8;--dim:#1e3a5f;
    --yellow:#facc15;--purple:#818cf8;--green:#22c55e;
    --red:#ef4444;--orange:#f59e0b;--blue:#60a5fa;
  }
  *{box-sizing:border-box;margin:0;padding:0;}
  body{background:var(--bg);color:var(--text);font-family:'Share Tech Mono',monospace;min-height:100vh;padding-bottom:40px;}
  .wrapper{max-width:480px;margin:0 auto;}
  .syn{font-family:'Syncopate',sans-serif;}
  .header{border-bottom:1px solid var(--border);padding:14px 16px;display:flex;justify-content:space-between;align-items:center;}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green);display:inline-block;animation:pulse 2s infinite;}
  .dot.error{background:var(--red);box-shadow:0 0 8px var(--red);}
  @keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}
  .content{padding:14px;}
  .card{background:var(--bg2);border:1px solid var(--border);border-radius:6px;padding:12px 14px;margin-bottom:12px;}
  .lbl{font-size:7px;color:var(--cyan);letter-spacing:2px;margin-bottom:4px;}
  .lbl2{font-size:7px;color:var(--cyan);letter-spacing:1px;opacity:.6;margin-bottom:6px;}
  .grid{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:12px;}
  .grid .card{margin-bottom:0;}
  .val{font-size:22px;}
  .sub{font-size:7px;letter-spacing:1px;margin-top:3px;}
  .bed{display:flex;justify-content:space-between;align-items:center;margin-bottom:7px;}
  .bed .bl{font-size:7px;letter-spacing:2px;}
  .bed .bv{font-size:10px;color:var(--dim);}
  .bed.ok .bl{color:var(--green)}.bed.fail .bl{color:var(--red)}
  .regeltext{font-size:11px;line-height:1.5;}
  .regeltext.ready{color:var(--green)}.regeltext.blocked{color:var(--orange)}
  .regeltext.error{color:var(--red)}.regeltext.waiting{color:var(--cyan)}
  .regeltext.down{color:var(--orange)}.regeltext.holding{color:var(--dim)}
  .regeltext.max{color:var(--yellow)}
  .ts{font-size:7px;color:var(--dim);text-align:center;letter-spacing:3px;margin-top:4px;}
  .twarn{background:#1c0a00;border:1px solid #7f1d1d;border-radius:4px;padding:7px 10px;margin-bottom:10px;display:flex;align-items:center;gap:8px;}
  .twarn-dot{width:6px;height:6px;border-radius:50%;background:#ef4444;box-shadow:0 0 6px #ef4444;flex-shrink:0;}
</style>
</head>
<body>
<div class="wrapper">
<div class="header">
  <svg width="160" height="68" viewBox="0 0 420 200" xmlns="http://www.w3.org/2000/svg">
    <rect x="30"  y="130" width="11" height="20" rx="2" fill="#38bdf8" opacity="0.20"/>
    <rect x="44"  y="115" width="11" height="35" rx="2" fill="#38bdf8" opacity="0.38"/>
    <rect x="58"  y="94"  width="11" height="56" rx="2" fill="#38bdf8" opacity="0.58"/>
    <rect x="72"  y="75"  width="11" height="75" rx="2" fill="#38bdf8" opacity="0.82"/>
    <rect x="86"  y="62"  width="11" height="88" rx="2" fill="#facc15"/>
    <rect x="100" y="73"  width="11" height="77" rx="2" fill="#38bdf8"/>
    <rect x="114" y="92"  width="11" height="58" rx="2" fill="#38bdf8" opacity="0.72"/>
    <rect x="128" y="108" width="11" height="42" rx="2" fill="#38bdf8" opacity="0.42"/>
    <rect x="142" y="122" width="11" height="28" rx="2" fill="#38bdf8" opacity="0.20"/>
    <line x1="26" y1="150" x2="160" y2="150" stroke="#0f2a3a" stroke-width="0.6"/>
    <rect x="30"  y="150" width="11" height="20" rx="2" fill="#38bdf8" opacity="0.05"/>
    <rect x="44"  y="150" width="11" height="35" rx="2" fill="#38bdf8" opacity="0.07"/>
    <rect x="58"  y="150" width="11" height="56" rx="2" fill="#38bdf8" opacity="0.07"/>
    <rect x="72"  y="150" width="11" height="75" rx="2" fill="#38bdf8" opacity="0.08"/>
    <rect x="86"  y="150" width="11" height="88" rx="2" fill="#facc15"  opacity="0.04"/>
    <rect x="100" y="150" width="11" height="77" rx="2" fill="#38bdf8" opacity="0.08"/>
    <rect x="114" y="150" width="11" height="58" rx="2" fill="#38bdf8" opacity="0.06"/>
    <rect x="128" y="150" width="11" height="42" rx="2" fill="#38bdf8" opacity="0.04"/>
    <rect x="142" y="150" width="11" height="28" rx="2" fill="#38bdf8" opacity="0.03"/>
    <text x="178" y="104" font-family="Syncopate,sans-serif" font-size="16" font-weight="700" fill="#38bdf8" letter-spacing="8">my</text>
    <text x="174" y="150" font-family="Syncopate,sans-serif" font-size="46" font-weight="700" fill="#e0f2fe" letter-spacing="3">HEMS</text>
    <text x="178" y="168" font-family="Share Tech Mono,monospace" font-size="7" fill="#1e3a5f" letter-spacing="3">HOME ENERGY MGMT SYS</text>
  </svg>
  <div style="margin-left:12px;">
    <div class="syn" style="font-size:7px;color:var(--cyan);letter-spacing:5px;margin-bottom:4px;">MYHEMS · {{ standort|upper }}</div>
    <div class="syn" style="font-size:15px;font-weight:700;letter-spacing:4px;">ENERGIE DASHBOARD</div>
    <div class="syn" style="font-size:7px;color:var(--dim);letter-spacing:2px;margin-top:2px;" id="version">v—</div>
  </div>
  <div style="text-align:right;margin-left:auto;">
    <div class="dot" id="dot"></div>
    <div class="syn" style="font-size:7px;color:var(--cyan);margin-top:2px;letter-spacing:2px;">ONLINE</div>
  </div>
</div>

<div class="content">
  <div class="card">
    <div class="twarn" id="twarn" style="display:none;">
      <div class="twarn-dot"></div>
      <div class="syn" style="font-size:7px;color:#f87171;letter-spacing:1px;">THERMOSTAT HAT HEIZSTAB ABGESCHALTET</div>
    </div>
    <div style="display:flex;justify-content:space-between;align-items:center;">
      <div>
        <div class="syn lbl">HEIZSTAB</div>
        <div class="syn" style="font-size:18px;font-weight:700;" id="heizLabel">—</div>
        <div style="font-size:10px;color:var(--cyan);margin-top:2px;" id="heizSub">—</div>
      </div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;max-width:160px;justify-content:flex-end;" id="relaisDots"></div>
    </div>
  </div>

  <div class="card">
    <div style="display:flex;justify-content:space-between;margin-bottom:6px;">
      <div class="syn lbl">BATTERIE LADESTAND</div>
      <div style="font-size:14px;" id="socVal">— %</div>
    </div>
    <div style="background:var(--bg);border-radius:3px;height:12px;overflow:hidden;position:relative;">
      <div id="socBar" style="height:100%;border-radius:3px;transition:width .5s,background .5s;width:0%;"></div>
      <div style="position:absolute;top:0;left:{{ min_soc }}%;width:1px;height:100%;background:var(--orange);"></div>
    </div>
    <div class="syn" style="font-size:7px;color:var(--dim);margin-top:4px;letter-spacing:2px;">MINDESTWERT {{ min_soc }} %</div>
  </div>

  <div class="grid">
    <div class="card">
      <div class="syn lbl">PV ERZEUGUNG</div>
      <div class="val" id="pvVal" style="color:var(--yellow);">— W</div>
    </div>
    <div class="card">
      <div class="syn lbl">BATTERIE</div>
      <div class="val" id="marstekVal" style="color:var(--purple);">— W</div>
      <div class="syn sub" id="marstekSub" style="color:var(--purple);">—</div>
    </div>
    <div class="card">
      <div class="syn lbl">NETZANSCHLUSS</div>
      <div class="val" id="netzVal">— W</div>
      <div class="syn sub" id="netzSub">—</div>
    </div>
    <div class="card">
      <div class="syn lbl">HAUSVERBRAUCH</div>
      <div class="syn lbl2">INKL. HEIZSTAB</div>
      <div class="val" id="eigenVal" style="color:var(--blue);">— W</div>
    </div>
  </div>

  <div class="card">
    <div class="syn lbl" style="margin-bottom:10px;">BEDINGUNGEN</div>
    <div class="bed" id="bedSOC"><span class="bl syn">—</span><span class="bv">—</span></div>
    <div class="bed" id="bedHoch"><span class="bl syn">—</span><span class="bv">—</span></div>
    <div style="border-top:1px solid var(--border2);margin:10px 0;"></div>
    <div class="syn lbl" style="margin-bottom:6px;">REGELSTATUS</div>
    <div class="regeltext" id="regeltext">Verbinde...</div>
  </div>

  <div class="card">
    <div class="syn lbl" style="margin-bottom:10px;">TAGESENERGIE</div>
    <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:0;">
      <div></div>
      <div class="syn" style="font-size:7px;letter-spacing:2px;color:var(--cyan);padding-bottom:6px;text-align:right;">HEUTE</div>
      <div class="syn" style="font-size:7px;letter-spacing:2px;color:var(--dim);padding-bottom:6px;text-align:right;">GESTERN</div>

      <div class="syn" style="font-size:7px;letter-spacing:1px;color:var(--yellow);padding:6px 0;border-top:1px solid var(--border2);">PV ERZEUGUNG</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--yellow);" id="e-pv-heute">—</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--dim);" id="e-pv-gestern">—</div>

      <div class="syn" style="font-size:7px;letter-spacing:1px;color:var(--green);padding:6px 0;border-top:1px solid var(--border2);">EINSPEISUNG</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--green);" id="e-ein-heute">—</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--dim);" id="e-ein-gestern">—</div>

      <div class="syn" style="font-size:7px;letter-spacing:1px;color:var(--red);padding:6px 0;border-top:1px solid var(--border2);">NETZBEZUG</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--red);" id="e-bez-heute">—</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--dim);" id="e-bez-gestern">—</div>

      <div class="syn" style="font-size:7px;letter-spacing:1px;color:var(--blue);padding:6px 0;border-top:1px solid var(--border2);">EIGENVERBRAUCH</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--blue);" id="e-ev-heute">—</div>
      <div style="font-size:11px;text-align:right;padding:6px 0;border-top:1px solid var(--border2);color:var(--dim);" id="e-ev-gestern">—</div>
    </div>
  </div>

  <div class="syn ts" id="ts">LETZTE AKTUALISIERUNG · —</div>
</div>
</div>

<script>
function fmt(v){if(v==null)return"— W";return(v>0?"+":"")+Math.round(v).toLocaleString("de-DE")+" W";}
function fmtAbs(v){if(v==null)return"— W";return Math.round(v).toLocaleString("de-DE")+" W";}
function fmtKwh(v){if(v==null||v===undefined)return"—";return v.toFixed(2)+" kWh";}
function bed(id,ok,lbl,val){
  const e=document.getElementById(id);
  e.className="bed "+(ok?"ok":"fail");
  e.innerHTML=`<span class="bl syn">${ok?"✓":"✗"} ${lbl}</span><span class="bv">${val}</span>`;
}
function baueRelaisDots(relaisLeistung, relaisZustand) {
  const c = document.getElementById("relaisDots");
  c.innerHTML = "";
  relaisLeistung.forEach((w, i) => {
    const on = relaisZustand[i];
    const label = w >= 1000 ? (w/1000).toFixed(1)+"kW" : w+"W";
    c.innerHTML += `<div style="text-align:center;">
      <div style="width:10px;height:10px;border-radius:50%;margin:0 auto 3px;
        background:${on?"#22c55e":"#1f2937"};
        ${on?"box-shadow:0 0 6px #22c55e;":""}"></div>
      <div class="syn" style="font-size:7px;color:${on?"#22c55e":"#374151"};">${label}</div>
    </div>`;
  });
}
async function update(){
  try{
    const d = await(await fetch("/api/status")).json();
    document.getElementById("dot").className="dot";
    document.getElementById("version").textContent="v"+d.version;
    const w = d.heizstab_w || 0;
    const relais = d.relais || [];
    const aktiv = relais.filter(Boolean).length;
    document.getElementById("heizLabel").textContent = w===0 ? "AUS" : (w/1000).toFixed(1)+" kW";
    document.getElementById("heizSub").textContent = w>0 ? aktiv+" RELAIS AKTIV" : "KEIN HEIZSTAB";
    baueRelaisDots(d.relais_leistung || [], relais);
    document.getElementById("twarn").style.display = d.thermostat_aus ? "flex" : "none";
    const soc = d.soc;
    const sc=document.getElementById("socVal"), sb=document.getElementById("socBar");
    if(soc!=null){
      const c=soc<30?"#ef4444":soc<60?"#f59e0b":"#22c55e";
      sc.textContent=Math.round(soc)+" %";sc.style.color=c;
      sb.style.width=soc+"%";sb.style.background=c;
    } else {sc.textContent="n/v";sc.style.color="#4b5563";sb.style.width="0%";}
    document.getElementById("pvVal").textContent=fmtAbs(d.pv);
    const m=d.marstek||0, mc=m>0?"#818cf8":m<0?"#f87171":"#4b5563";
    document.getElementById("marstekVal").textContent=fmt(d.marstek);
    document.getElementById("marstekVal").style.color=mc;
    document.getElementById("marstekSub").textContent=m>0?"LÄDT":m<0?"ENTLÄDT":"IDLE";
    document.getElementById("marstekSub").style.color=mc;
    const n=d.netz||0, nc=n<0?"#22c55e":n>0?"#ef4444":"#4b5563";
    document.getElementById("netzVal").textContent=fmt(d.netz);
    document.getElementById("netzVal").style.color=nc;
    document.getElementById("netzSub").textContent=n<0?"EINSPEISUNG":n>0?"NETZBEZUG":"AUSGEGLICHEN";
    document.getElementById("netzSub").style.color=nc;
    document.getElementById("eigenVal").textContent=fmtAbs(d.hausverbrauch);
    const p=d.params;
    bed("bedSOC",d.soc==null||d.soc>=p.MIN_SOC,"LADESTAND ≥ "+p.MIN_SOC+" %",d.soc!=null?Math.round(d.soc)+" %":"n/v");
    bed("bedHoch",(d.marstek||0)>p.HOCHSCHALTEN_SCHWELLE||(d.netz||0)<-p.HOCHSCHALTEN_SCHWELLE,
      "ÜBERSCHUSS > "+p.HOCHSCHALTEN_SCHWELLE.toLocaleString("de-DE")+" W",
      fmt(d.marstek));
    const rt=document.getElementById("regeltext");
    rt.textContent=d.regeltext; rt.className="regeltext "+(d.regeltyp||"");
    document.getElementById("ts").textContent="LETZTE AKTUALISIERUNG · "+new Date(d.timestamp*1000).toLocaleTimeString("de-DE");
    if(d.tagesenergie){
      const h=d.tagesenergie.heute, g=d.tagesenergie.gestern;
      document.getElementById("e-pv-heute").textContent    = fmtKwh(h.pv_kwh);
      document.getElementById("e-pv-gestern").textContent  = fmtKwh(g.pv_kwh);
      document.getElementById("e-ein-heute").textContent   = fmtKwh(h.einspeisung_kwh);
      document.getElementById("e-ein-gestern").textContent = fmtKwh(g.einspeisung_kwh);
      document.getElementById("e-bez-heute").textContent   = fmtKwh(h.bezug_kwh);
      document.getElementById("e-bez-gestern").textContent = fmtKwh(g.bezug_kwh);
      document.getElementById("e-ev-heute").textContent    = fmtKwh(h.eigenverbrauch_kwh);
      document.getElementById("e-ev-gestern").textContent  = fmtKwh(g.eigenverbrauch_kwh);
    }
  } catch(e){
    document.getElementById("dot").className="dot error";
    document.getElementById("regeltext").textContent="Verbindungsfehler";
  }
}
update(); setInterval(update,5000);
</script>
</body>
</html>"""

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML,
        standort=STANDORT_NAME,
        min_soc=MIN_SOC,
    )

if __name__ == "__main__":
    t = threading.Thread(target=regelschleife, daemon=True)
    t.start()
    app.run(host="0.0.0.0", port=5000)
