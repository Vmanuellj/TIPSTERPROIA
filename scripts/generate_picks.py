#!/usr/bin/env python3
"""
TIPSTER PRO IA — Generador automático (GitHub Actions)
Usa: MLB Stats API (gratis) + The Odds API (gratis 500/mes) + Claude API
Mejoras v2:
  - 8-10 picks diarios
  - Fecha exacta del redeploy (CDMX con DST correcto)
  - Referencia Bet365 (cuotas decimales europeas)
  - Horarios en hora CDMX
  - Filtro estricto: solo partidos de HOY por liga
  - NFL incluido
  - Mundial: solo partidos confirmados HOY via Odds API
"""
import anthropic, json, os, requests
from datetime import datetime, timezone, timedelta

# ── Zona horaria CDMX con DST dinámico ───────────────────────────────────────
def get_cdmx_offset():
    """
    México City: CDT (UTC-5) de segundo domingo de abril al último domingo de octubre.
    CST (UTC-6) el resto del año.
    """
    now_utc = datetime.now(timezone.utc)
    y = now_utc.year
    # Segundo domingo de abril
    apr1 = datetime(y, 4, 1)
    dst_start = apr1 + timedelta(days=(6 - apr1.weekday()) % 7 + 7)
    # Último domingo de octubre
    oct31 = datetime(y, 10, 31)
    dst_end = oct31 - timedelta(days=(oct31.weekday() + 1) % 7)
    naive_now = now_utc.replace(tzinfo=None)
    return -5 if dst_start <= naive_now < dst_end else -6

CDMX_OFFSET = get_cdmx_offset()
TZ_LABEL    = "CDT" if CDMX_OFFSET == -5 else "CST"
CT          = timezone(timedelta(hours=CDMX_OFFSET))
dt          = datetime.now(CT)
today       = dt.strftime("%Y-%m-%d")

DIAS  = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
MESES = ["Enero","Febrero","Marzo","Abril","Mayo","Junio","Julio","Agosto",
         "Septiembre","Octubre","Noviembre","Diciembre"]
fecha_display = f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month-1]} {dt.year}"

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ODDS_KEY      = os.environ.get("ODDS_API_KEY", "")

# ── Helpers de tiempo ─────────────────────────────────────────────────────────
def utc_str_to_cdmx(utc_str: str) -> str:
    """'2026-06-13T20:10:00Z' → '2:10 PM CDT'"""
    try:
        s = utc_str[:19].replace("T", " ")
        utc_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        local  = utc_dt.astimezone(CT)
        hour   = local.hour % 12 or 12
        mins   = local.strftime("%M")
        ampm   = "AM" if local.hour < 12 else "PM"
        return f"{hour}:{mins} {ampm} {TZ_LABEL}"
    except Exception:
        return utc_str

def is_today_cdmx(utc_str: str) -> bool:
    """Regresa True si la fecha UTC cae en el día de HOY en hora CDMX."""
    try:
        s = utc_str[:19].replace("T", " ")
        utc_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today
    except Exception:
        return False

# ── 1. MLB Schedule (API oficial, sin costo) ──────────────────────────────────
def fetch_mlb_schedule():
    try:
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&date={today}&hydrate=probablePitcher(note),team")
        data  = requests.get(url, timeout=12).json()
        games = []
        for d in data.get("dates", []):
            for g in d.get("games", []):
                status = g.get("status", {}).get("detailedState", "")
                if status not in ("Scheduled", "Pre-Game", "Warmup"):
                    continue
                t    = g.get("teams", {})
                away = t.get("away", {}).get("team", {}).get("name", "")
                home = t.get("home", {}).get("team", {}).get("name", "")
                awp  = t.get("away", {}).get("probablePitcher", {}).get("fullName", "TBD")
                hwp  = t.get("home", {}).get("probablePitcher", {}).get("fullName", "TBD")
                gtime = utc_str_to_cdmx(g.get("gameDate", ""))
                games.append({"away": away, "home": home, "awp": awp, "hwp": hwp, "time": gtime})
        return games
    except Exception as e:
        print(f"  ⚠ MLB API: {e}")
        return []

# ── 2. Odds API (Bet365) con filtro de HOY ────────────────────────────────────
def fetch_odds_today(sport_key: str) -> list:
    """Obtiene odds de Bet365 y filtra SOLO partidos de hoy en hora CDMX."""
    if not ODDS_KEY:
        print(f"  ⚠ ODDS_API_KEY no configurada — saltando {sport_key}")
        return []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params={
                "apiKey":      ODDS_KEY,
                "regions":     "eu",
                "bookmakers":  "bet365",
                "markets":     "h2h,totals",
                "oddsFormat":  "decimal",
            },
            timeout=15,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        all_games = r.json() if r.ok else []

        # Filtrar solo los de HOY en CDMX
        today_games = [g for g in all_games
                       if is_today_cdmx(g.get("commence_time", ""))]

        print(f"  Odds API ({sport_key}) → {len(today_games)}/{len(all_games)} juegos HOY "
              f"| requests restantes: {remaining}")
        return today_games
    except Exception as e:
        print(f"  ⚠ Odds API ({sport_key}): {e}")
        return []

# ── 3. Construir contexto para Claude ─────────────────────────────────────────
def build_context(mlb_sched, mlb_odds, nba_odds, cup_odds, nfl_odds) -> str:
    lines = [
        f"FECHA HOY: {today} ({fecha_display})",
        f"ZONA HORARIA: CDMX / {TZ_LABEL} (UTC{CDMX_OFFSET:+d})",
        "",
        "⚠️  REGLA CRÍTICA: Solo analiza partidos que aparezcan EXPLÍCITAMENTE en este",
        f"contexto. Si una sección dice 'Sin juegos hoy', esa liga va a NO_APOSTAR.",
        "",
    ]

    # MLB schedule
    lines.append("=== MLB — PARTIDOS HOY (con pitchers probables) ===")
    if mlb_sched:
        for g in mlb_sched:
            lines.append(f"• {g['away']} @ {g['home']}  ({g['time']} CDMX)")
            lines.append(f"  Pitchers: {g['awp']} (visitante) vs {g['hwp']} (local)")
    else:
        lines.append("  Sin partidos MLB hoy.")

    def fmt_odds_section(games: list, label: str):
        lines.append(f"\n=== {label} ===")
        if not games:
            lines.append("  Sin juegos hoy.")
            return
        for g in games:
            away  = g.get("away_team", "")
            home  = g.get("home_team", "")
            ctime = utc_str_to_cdmx(g.get("commence_time", "").replace("Z", ""))
            lines.append(f"• {away} @ {home}  ({ctime} CDMX)")
            for bm in g.get("bookmakers", []):
                if bm.get("key") != "bet365":
                    continue
                for mkt in bm.get("markets", []):
                    if mkt["key"] == "h2h":
                        oc   = {o["name"]: o["price"] for o in mkt["outcomes"]}
                        draw = oc.get("Draw", "")
                        dstr = f" | Empate {draw}" if draw else ""
                        lines.append(
                            f"  Bet365: {away} {oc.get(away, '—')} / "
                            f"{home} {oc.get(home, '—')}{dstr}"
                        )
                    elif mkt["key"] == "totals":
                        for o in mkt["outcomes"]:
                            if o["name"] == "Over":
                                lines.append(
                                    f"  Total: {o.get('point', '')} "
                                    f"Over {o['price']} / Under (calcular)"
                                )

    fmt_odds_section(mlb_odds, "MLB — CUOTAS BET365 HOY")
    fmt_odds_section(nba_odds, "NBA — CUOTAS BET365 HOY")
    fmt_odds_section(cup_odds, "FIFA WORLD CUP 2026 — CUOTAS BET365 HOY")
    fmt_odds_section(nfl_odds, "NFL — CUOTAS BET365 HOY")

    return "\n".join(lines)

# ── 4. Claude API ─────────────────────────────────────────────────────────────
PROMPT_SYSTEM = f"""Eres un tipster profesional y analista cuantitativo de apuestas deportivas.
Casa de referencia única: Bet365 (cuotas decimales europeas).
Hoy es {today} — horario CDMX ({TZ_LABEL}, UTC{CDMX_OFFSET:+d}).

REGLAS ABSOLUTAS:
1. SOLO genera picks de partidos que aparezcan explícitamente en el contexto de datos.
2. Para el Mundial FIFA 2026: si la sección "FIFA WORLD CUP 2026" dice "Sin juegos hoy",
   NO pongas ningún partido del Mundial en picks ni en resumen_ejecutivo.
3. Todos los horarios deben mostrarse en hora CDMX (CDT/CST).
4. NO inventes partidos, equipos, ni cuotas que no estén en el contexto.

METODOLOGÍA OBLIGATORIA (por cada pick):
1. prob_implicita_fair = devig: p_home / (p_home + p_away) × 100
2. prob_propia = tu estimación basada en datos (forma, pitchers, lesiones, H2H, xERA, FIP)
3. EV% = (prob_propia/100 × cuota_decimal - 1) × 100  →  solo incluir si EV > 0
4. Stake: Kelly fraccional 1/4. Máx 0.3u por pick. Total sesión ≤ 3u
5. Parlays: 2-3 patas con correlación positiva; cuota mínima 1.20 por pata

Responde ÚNICAMENTE JSON válido, sin markdown, sin texto extra."""

SCHEMA_PICK = {
    "liga":          "MLB | NBA | FIFA World Cup 2026 | NFL",
    "matchup":       "AWAY @ HOME",
    "hora":          "H:MM AM/PM CDT (CDMX)",
    "pick":          "descripción concreta",
    "tipo":          "Moneyline | Total | Run Line | Total Goles | Spread | Prop",
    "cuota_bet365":  1.85,
    "prob_implicita": 55.5,
    "prob_propia":   61.0,
    "ev_pct":        5.3,
    "prob_acierto":  61,
    "estrellas":     3,
    "stake":         "0.2u",
    "razonamiento":  "2-3 líneas con datos concretos",
}

def generate_picks(context: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)

    schema = {
        "fecha":         today,
        "fecha_display": fecha_display,
        "generado_a":    "automatico-github-actions",
        "nota_lineas":   f"Fuente: MLB Stats API + Odds API Bet365. Horario {TZ_LABEL} CDMX.",
        "bankroll":      {"exposicion_total": "Xu", "max_por_juego": "0.3u", "nota": "Kelly 1/4"},
        "picks":         [SCHEMA_PICK],
        "no_apostar":    [{"matchup": "...", "liga": "...", "razon": "..."}],
        "parlay_sugerido": {
            "patas":          ["Pick A (X.XX)", "Pick B (X.XX)"],
            "cuota_total":    3.50,
            "cuota_con_boost": 3.50,
            "ev_pct":         5.0,
            "stake":          "0.15u",
            "nota":           "razón de la correlación",
        },
        "resumen_ejecutivo": [
            {"pick": "...", "tipo": "Moneyline", "liga": "MLB",
             "cuota": 1.85, "ev_pct": 5.3, "stake": "0.2u", "estrellas": 3}
        ],
    }

    user_msg = (
        f"Genera entre 8 y 10 picks para HOY ({today}).\n"
        f"Usa EXCLUSIVAMENTE los partidos listados en el contexto de datos.\n"
        f"Si hay pocas oportunidades con EV real, genera menos picks (calidad > cantidad).\n\n"
        f"DATOS REALES:\n{context}\n\n"
        f"Esquema JSON de respuesta:\n{json.dumps(schema, indent=2, ensure_ascii=False)}"
    )

    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=6000,
        system=PROMPT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    raw = msg.content[0].text.strip()
    # Limpiar markdown si viene
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rstrip("`").strip()

    return json.loads(raw)

# ── 5. Guardar archivos ───────────────────────────────────────────────────────
def save_all(data: dict):
    files = {
        f"picks-{today}.json": json.dumps(data, ensure_ascii=False, indent=2),
        "latest.json":         json.dumps(data, ensure_ascii=False, indent=2),
        "picks-data.js":       (
            f"// Auto-generado {today} — TIPSTER PRO IA\n"
            f"window.PICKS_DATA = "
            + json.dumps(data, ensure_ascii=False, indent=2) + ";\n"
        ),
    }
    for fname, content in files.items():
        with open(fname, "w", encoding="utf-8") as f:
            f.write(content)
        print(f"  ✅ {fname}")

# ── MAIN ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print(f"\n🏆 TIPSTER PRO IA — {fecha_display}")
    print(f"   Zona: CDMX / {TZ_LABEL} (UTC{CDMX_OFFSET:+d})")
    print("=" * 55)

    print(f"\n📅 MLB schedule para {today}...")
    mlb_sched = fetch_mlb_schedule()
    print(f"   {len(mlb_sched)} partidos encontrados")

    print("\n💰 Obteniendo odds Bet365 (solo HOY)...")
    mlb_odds = fetch_odds_today("baseball_mlb")
    nba_odds = fetch_odds_today("basketball_nba")
    cup_odds = fetch_odds_today("soccer_fifa_world_cup")
    nfl_odds = fetch_odds_today("americanfootball_nfl")

    context = build_context(mlb_sched, mlb_odds, nba_odds, cup_odds, nfl_odds)

    print("\n--- CONTEXTO (primeras 1200 chars) ---")
    print(context[:1200])
    print("...\n")

    print(f"🤖 Llamando Claude API (objetivo: 8-10 picks)...")
    try:
        picks_data = generate_picks(context)
        n = len(picks_data.get("picks", []))
        print(f"\n✅ {n} picks generados:")
        for p in picks_data.get("picks", []):
            stars = "★" * p.get("estrellas", 1)
            print(f"   {stars} {p['matchup']} — {p['pick']}  EV+{p['ev_pct']}%  {p['stake']}")

        print("\n💾 Guardando archivos...")
        save_all(picks_data)
        print("\n🎉 Listo — GitHub Actions hará el push automático.")

    except Exception as e:
        import traceback
        print(f"\n❌ Error: {e}")
        traceback.print_exc()
        fallback = {
            "fecha":         today,
            "fecha_display": fecha_display,
            "generado_a":    "error",
            "nota_lineas":   f"Error al generar: {str(e)[:150]}",
            "bankroll":      {"exposicion_total": "0u", "max_por_juego": "0u",
                              "nota": "Error de generación"},
            "picks":         [],
            "no_apostar":    [{"matchup": "Error del sistema", "liga": "Sistema",
                               "razon": str(e)[:300]}],
            "parlay_sugerido": {"patas": [], "cuota_total": 0,
                                "cuota_con_boost": 0, "ev_pct": 0,
                                "stake": "0u", "nota": ""},
            "resumen_ejecutivo": [],
        }
        save_all(fallback)
        raise
