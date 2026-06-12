#!/usr/bin/env python3
"""
TIPSTER PRO IA — Generador automático (GitHub Actions)
Usa: MLB Stats API (gratis) + The Odds API (gratis 500/mes) + Claude API
Corre a las 5am CDT en GitHub Actions — sin necesidad de tener la compu encendida
"""
import anthropic, json, os, requests
from datetime import datetime, timezone, timedelta

# ── Zona horaria CDMX (CDT verano = UTC-5) ───────────────────────────────────
CT = timezone(timedelta(hours=-5))
dt = datetime.now(CT)
today = dt.strftime("%Y-%m-%d")
DIAS  = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
MESES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto",
         "Septiembre","Octubre","Noviembre","Diciembre"]
fecha_display = f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month-1]} {dt.year}"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ODDS_KEY      = os.environ.get("ODDS_API_KEY", "")

# ── 1. MLB Schedule (API oficial, sin costo) ─────────────────────────────────
def fetch_mlb_schedule():
    try:
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&date={today}&hydrate=probablePitcher(note),team")
        data = requests.get(url, timeout=12).json()
        games = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                t = g.get("teams", {})
                status = g.get("status", {}).get("detailedState", "")
                if status not in ("Scheduled", "Pre-Game", "Warmup"):
                    continue
                away = t.get("away", {}).get("team", {}).get("name", "")
                home = t.get("home", {}).get("team", {}).get("name", "")
                awp  = t.get("away", {}).get("probablePitcher", {}).get("fullName", "TBD")
                hwp  = t.get("home", {}).get("probablePitcher", {}).get("fullName", "TBD")
                gtime= g.get("gameDate", "")[:16].replace("T"," ")
                games.append({"away":away,"home":home,"awp":awp,"hwp":hwp,"time":gtime})
        return games
    except Exception as e:
        print(f"  ⚠ MLB API: {e}")
        return []

# ── 2. Odds API — helper ─────────────────────────────────────────────────────
def fetch_odds(sport_key):
    if not ODDS_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params={"apiKey": ODDS_KEY, "regions":"us",
                    "markets":"h2h,totals", "oddsFormat":"american"},
            timeout=12
        )
        remaining = r.headers.get("x-requests-remaining","?")
        print(f"  Odds API ({sport_key}) → {len(r.json()) if r.ok else r.status_code} games | requests left: {remaining}")
        return r.json() if r.ok else []
    except Exception as e:
        print(f"  ⚠ Odds API ({sport_key}): {e}")
        return []

# ── 3. Formatear datos para Claude ───────────────────────────────────────────
def build_context(mlb_sched, mlb_odds, nba_odds, cup_odds):
    lines = [f"FECHA: {today} ({fecha_display})\n"]

    # MLB schedule
    lines.append("=== MLB PARTIDOS HOY ===")
    if mlb_sched:
        for g in mlb_sched:
            lines.append(f"• {g['away']} @ {g['home']}  ({g['time']} UTC)")
            lines.append(f"  Pitchers: {g['awp']} (visitante) vs {g['hwp']} (local)")
    else:
        lines.append("  (Sin partidos o API no disponible)")

    # MLB odds
    def fmt_odds(games, label):
        if not games:
            return
        lines.append(f"\n=== {label} ===")
        for g in games[:12]:
            away, home = g.get("away_team",""), g.get("home_team","")
            for bm in g.get("bookmakers",[])[:1]:
                for mkt in bm.get("markets",[]):
                    if mkt["key"] == "h2h":
                        oc = {o["name"]: o["price"] for o in mkt["outcomes"]}
                        draw = oc.get("Draw","")
                        draw_str = f" / Draw {draw}" if draw else ""
                        lines.append(f"• {away} @ {home}  ML: {away} {oc.get(away,'')} / {home} {oc.get(home,'')}{draw_str}")
                    elif mkt["key"] == "totals":
                        for o in mkt["outcomes"]:
                            if o["name"] == "Over":
                                lines.append(f"  Total: {o.get('point','')} Over {o['price']}")

    fmt_odds(mlb_odds,  "LÍNEAS MLB (The Odds API)")
    fmt_odds(nba_odds,  "LÍNEAS NBA")
    fmt_odds(cup_odds,  "COPA DEL MUNDO FIFA 2026")
    return "\n".join(lines)

# ── 4. Claude API ─────────────────────────────────────────────────────────────
PROMPT_SYSTEM = """Eres un tipster profesional y analista cuantitativo de apuestas deportivas.
Casa principal: Winpot (MXN). Referencia: Bet365.
METODOLOGÍA OBLIGATORIA:
1. prob_implicita real = prob_raw / (1 + vig) donde vig ≈ 0.045
2. prob_propia = tu estimación con base en los datos provistos
3. EV% = (prob_propia × cuota_decimal) - 1. Solo incluye pick si EV > 0
4. Stake: Kelly fraccional 1/4. Máx 0.3u por pick, total sesión ≤ 2u
REGLAS DEL JSON:
- prob_implicita y prob_propia como PORCENTAJES (ej: 58.3 no 0.583)
- tipo debe ser uno de: Moneyline | Total | Run Line | Total Goles | Prop Pitcher
- Solo picks con EV > 0; el resto va a no_apostar con razón clara
- Responde ÚNICAMENTE JSON válido, sin markdown, sin texto extra"""

def generate_picks(context):
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    schema = json.dumps({
        "fecha": today, "fecha_display": fecha_display,
        "generado_a": "automatico-github-actions",
        "nota_lineas": "Datos: MLB Stats API + The Odds API. Cuotas Winpot estimadas.",
        "bankroll": {"exposicion_total": "Xu", "max_por_juego": "0.3u", "nota": "Kelly 1/4"},
        "picks": [{"liga":"MLB","matchup":"AWAY @ HOME","hora":"H:MM PM EDT","pick":"...",
                   "tipo":"Moneyline","cuota_winpot":1.85,"cuota_bet365":1.87,
                   "prob_implicita":55.5,"prob_propia":61.0,"ev_pct":5.3,
                   "prob_acierto":61,"estrellas":3,"stake":"0.2u","razonamiento":"..."}],
        "no_apostar": [{"matchup":"...","liga":"...","razon":"..."}],
        "parlay_sugerido": {"patas":["Pick A (cuota X.XX)","Pick B (cuota X.XX)"],
                            "cuota_total":3.5,"cuota_con_boost":4.9,
                            "ev_pct":15.0,"stake":"0.15u","nota":"Boost Winpot 40%"},
        "resumen_ejecutivo": [{"pick":"...","tipo":"Moneyline","liga":"MLB",
                               "cuota":1.85,"ev_pct":5.3,"stake":"0.2u","estrellas":3}]
    }, indent=2)

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        system=PROMPT_SYSTEM,
        messages=[{"role":"user","content":
            f"Genera los picks de hoy con estos datos reales:\n\n{context}\n\n"
            f"Responde con este esquema JSON (rellena con datos reales):\n{schema}"}]
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        if raw.endswith("```"):
            raw = raw[:-3].strip()
    return json.loads(raw)

# ── 5. Guardar archivos ───────────────────────────────────────────────────────
def save_all(data):
    for fname, content in [
        (f"picks-{today}.json", json.dumps(data, ensure_ascii=False, indent=2)),
        ("latest.json",          json.dumps(data, ensure_ascii=False, indent=2)),
        ("picks-data.js",        f"// Auto-generado {today}\nwindow.PICKS_DATA = "
                                  + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"),
    ]:
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  ✅ {fname}")

# ── MAIN ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n🏆 TIPSTER PRO IA — {fecha_display}")
    print("=" * 50)

    print("📅 Fetching MLB schedule...")
    mlb_sched = fetch_mlb_schedule()
    print(f"   {len(mlb_sched)} partidos")

    print("💰 Fetching odds...")
    mlb_odds = fetch_odds("baseball_mlb")
    nba_odds = fetch_odds("basketball_nba")
    cup_odds = fetch_odds("soccer_fifa_world_cup")

    context = build_context(mlb_sched, mlb_odds, nba_odds, cup_odds)
    print("\n--- CONTEXTO (preview) ---")
    print(context[:800])
    print("...")

    print("\n🤖 Llamando Claude API...")
    try:
        picks_data = generate_picks(context)
        n = len(picks_data.get("picks", []))
        print(f"✅ {n} picks generados:")
        for p in picks_data.get("picks", []):
            print(f"   {'★'*p.get('estrellas',1)} {p['matchup']} — {p['pick']} EV+{p['ev_pct']}%")

        print("\n💾 Guardando archivos...")
        save_all(picks_data)
        print("\n🎉 Listo. GitHub Actions hará el push automático.")

    except Exception as e:
        print(f"❌ Error: {e}")
        fallback = {
            "fecha": today, "fecha_display": fecha_display,
            "generado_a": "error", "nota_lineas": f"Error: {str(e)[:120]}",
            "bankroll": {"exposicion_total":"0u","max_por_juego":"0u","nota":"Error de generación"},
            "picks": [],
            "no_apostar": [{"matchup":"Error del sistema","liga":"Sistema","razon":str(e)[:200]}],
            "parlay_sugerido":{"patas":[],"cuota_total":0,"cuota_con_boost":0,"ev_pct":0,"stake":"0u","nota":""},
            "resumen_ejecutivo":[]
        }
        save_all(fallback)
        raise
