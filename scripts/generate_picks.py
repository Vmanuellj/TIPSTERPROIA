#!/usr/bin/env python3
"""
TIPSTER PRO IA — Generador automático (GitHub Actions)
Usa: MLB Stats API (gratis) + The Odds API (gratis 500/mes) + Claude API
v3: Cuotas reales de Bet365 — se sobreescriben post-generación desde Odds API
"""
import anthropic, json, os, re, requests
from datetime import datetime, timezone, timedelta

# ── Zona horaria CDMX con DST dinámico ───────────────────────────────────────
def get_cdmx_offset():
    now_utc = datetime.now(timezone.utc)
    y = now_utc.year
    apr1 = datetime(y, 4, 1)
    dst_start = apr1 + timedelta(days=(6 - apr1.weekday()) % 7 + 7)
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
    try:
        s = utc_str[:19].replace("T", " ")
        utc_dt = datetime.strptime(s, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        return utc_dt.astimezone(CT).strftime("%Y-%m-%d") == today
    except Exception:
        return False

# ── 1. MLB Schedule ───────────────────────────────────────────────────────────
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

# ── 2. Odds API (Bet365) ──────────────────────────────────────────────────────
# Mercados por tipo de deporte:
#   Fútbol/Soccer: h2h (1X2) + totals (O/U goles) + spreads (Asian Handicap) + btts (ambos anotan)
#   Baseball/Basket: h2h + totals + spreads (run line / punto y medio)
SPORT_MARKETS = {
    "soccer_fifa_world_cup": "h2h,totals,spreads,btts",
    "soccer_":               "h2h,totals,spreads,btts",   # prefijo para cualquier soccer
    "baseball_mlb":          "h2h,totals,spreads",
    "basketball_nba":        "h2h,totals,spreads",
    "americanfootball_nfl":  "h2h,totals,spreads",
}

def _markets_for(sport_key: str) -> str:
    for prefix, mkts in SPORT_MARKETS.items():
        if sport_key.startswith(prefix) or sport_key == prefix:
            return mkts
    return "h2h,totals,spreads"

def fetch_odds_today(sport_key: str) -> list:
    if not ODDS_KEY:
        print(f"  ⚠ ODDS_API_KEY no configurada — saltando {sport_key}")
        return []
    try:
        markets = _markets_for(sport_key)
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params={
                "apiKey":      ODDS_KEY,
                "regions":     "eu",
                "bookmakers":  "bet365",
                "markets":     markets,
                "oddsFormat":  "decimal",
            },
            timeout=15,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        all_games = r.json() if r.ok else []
        today_games = [g for g in all_games if is_today_cdmx(g.get("commence_time", ""))]
        print(f"  Odds API ({sport_key}) mkts={markets} → "
              f"{len(today_games)}/{len(all_games)} juegos HOY | requests restantes: {remaining}")
        return today_games
    except Exception as e:
        print(f"  ⚠ Odds API ({sport_key}): {e}")
        return []

# ── 3. Construir contexto ─────────────────────────────────────────────────────
def build_context(mlb_sched, mlb_odds, nba_odds, cup_odds, nfl_odds) -> str:
    lines = [
        f"FECHA HOY: {today} ({fecha_display})",
        f"ZONA HORARIA: CDMX / {TZ_LABEL} (UTC{CDMX_OFFSET:+d})",
        "",
        "⚠️  REGLA CRÍTICA: Solo analiza partidos que aparezcan EXPLÍCITAMENTE en este",
        "contexto. Si una sección dice 'Sin juegos hoy', esa liga va a NO_APOSTAR.",
        "",
    ]
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
                            f"  Bet365 1X2/ML: {away} {oc.get(away, '—')} / "
                            f"{home} {oc.get(home, '—')}{dstr}"
                        )
                    elif mkt["key"] == "spreads":
                        for o in mkt["outcomes"]:
                            pt = o.get("point", "")
                            lines.append(
                                f"  Bet365 Handicap/Spread: {o['name']} {pt:+g} → {o['price']}"
                                if isinstance(pt, (int, float)) else
                                f"  Bet365 Handicap/Spread: {o['name']} {pt} → {o['price']}"
                            )
                    elif mkt["key"] == "btts":
                        oc = {o["name"]: o["price"] for o in mkt["outcomes"]}
                        lines.append(
                            f"  Bet365 Ambos Anotan: Sí {oc.get('Yes','—')} / No {oc.get('No','—')}"
                        )
                    elif mkt["key"] == "totals":
                        for o in mkt["outcomes"]:
                            if o["name"] == "Over":
                                under_price = next(
                                    (x["price"] for x in mkt["outcomes"] if x["name"] == "Under"), "—"
                                )
                                lines.append(
                                    f"  Bet365 Total {o.get('point', '')}: "
                                    f"Over {o['price']} / Under {under_price}"
                                )

    fmt_odds_section(mlb_odds, "MLB — CUOTAS BET365 HOY")
    fmt_odds_section(nba_odds, "NBA — CUOTAS BET365 HOY")
    fmt_odds_section(cup_odds, "FIFA WORLD CUP 2026 — CUOTAS BET365 HOY")
    fmt_odds_section(nfl_odds, "NFL — CUOTAS BET365 HOY")
    return "\n".join(lines)

# ── 4. Corrección de cuotas reales (Opción A) ─────────────────────────────────
def _team_match(name_a: str, name_b: str) -> bool:
    """True si dos nombres de equipo se refieren al mismo equipo."""
    a = name_a.upper().strip()
    b = name_b.upper().strip()
    if a == b:
        return True
    if a in b or b in a:
        return True
    # Comparar última palabra (apodo del equipo: Tigers, Yankees, etc.)
    wa = [w for w in re.split(r'\W+', a) if len(w) > 2]
    wb = [w for w in re.split(r'\W+', b) if len(w) > 2]
    if wa and wb and wa[-1] == wb[-1]:
        return True
    # Overlap de palabras significativas
    sa, sb = set(wa), set(wb)
    return len(sa & sb) >= 2

def _pick_is_away(pick_txt: str, away_raw: str) -> bool:
    """True si el pick corresponde al equipo visitante."""
    words = [w for w in re.split(r'\W+', away_raw.upper()) if len(w) > 2]
    return any(w in pick_txt for w in words)

def _pick_is_home(pick_txt: str, home_raw: str) -> bool:
    """True si el pick corresponde al equipo local."""
    words = [w for w in re.split(r'\W+', home_raw.upper()) if len(w) > 2]
    return any(w in pick_txt for w in words)

def _extract_handicap_value(pick_txt: str) -> float | None:
    """Extrae el número de handicap del texto del pick (ej: '-2.5' de 'SPAIN -2.5 ASIAN HANDICAP')."""
    m = re.search(r'([+-]?\d+\.?\d*)\s*(?:ASIAN\s+HANDICAP|HANDICAP|SPREAD|RUN\s+LINE)', pick_txt, re.IGNORECASE)
    if m:
        return float(m.group(1))
    return None

def _find_real_cuota(pick: dict, odds_list: list) -> float | None:
    """
    Busca la cuota real de Bet365 para un pick dado.
    Cubre: h2h (1X2/ML), totals (O/U), spreads (AH/Run Line), btts (Ambos Anotan).
    Retorna el precio (float) o None si no se encontró.
    """
    matchup  = pick.get("matchup", "")
    tipo     = (pick.get("tipo") or "").lower()
    pick_txt = (pick.get("pick") or "").upper()

    # Separar equipos — acepta " @ " y " VS "
    if " @ " in matchup:
        away_raw, home_raw = matchup.split(" @ ", 1)
    elif " VS " in matchup.upper():
        parts = re.split(r'\s+vs\s+', matchup, flags=re.IGNORECASE)
        away_raw, home_raw = parts[0], parts[1]
    else:
        return None
    away_raw = away_raw.strip()
    home_raw = home_raw.strip()

    hcap_val = _extract_handicap_value(pick_txt)

    for game in odds_list:
        away_g = game.get("away_team", "")
        home_g = game.get("home_team", "")
        if not (_team_match(away_raw, away_g) and _team_match(home_raw, home_g)):
            continue

        for bm in game.get("bookmakers", []):
            if bm.get("key") != "bet365":
                continue
            for mkt in bm.get("markets", []):

                # ── Totals: Over / Under (goles, carreras, puntos) ─────────
                if mkt["key"] == "totals":
                    if any(k in pick_txt for k in ("OVER", "MAS DE", "MÁS DE", "+")):
                        for o in mkt["outcomes"]:
                            if o["name"].upper() == "OVER":
                                return o["price"]
                    elif any(k in pick_txt for k in ("UNDER", "MENOS DE", "-")):
                        for o in mkt["outcomes"]:
                            if o["name"].upper() == "UNDER":
                                return o["price"]

                # ── Spreads: Asian Handicap / Run Line / Handicap ──────────
                elif mkt["key"] == "spreads":
                    is_away = _pick_is_away(pick_txt, away_raw)
                    is_home = _pick_is_home(pick_txt, home_raw)
                    for o in mkt["outcomes"]:
                        oname = o["name"]
                        opt_point = o.get("point")
                        # Filtrar por valor de handicap si está disponible
                        if hcap_val is not None and opt_point is not None:
                            # El visitante tiene handicap positivo en spreads (la API lo invierte)
                            # Buscar la línea más cercana al valor pedido
                            if abs(float(opt_point) - abs(hcap_val)) > 0.26:
                                continue
                        if is_away and _team_match(oname, away_g):
                            return o["price"]
                        if is_home and _team_match(oname, home_g):
                            return o["price"]

                # ── BTTS: Ambos Anotan / Both Teams to Score ───────────────
                elif mkt["key"] == "btts":
                    if any(k in pick_txt for k in ("AMBOS ANOTAN", "BOTH TEAMS", "BTTS", "SI ANOTAN", "SÍ")):
                        for o in mkt["outcomes"]:
                            if o["name"].upper() in ("YES", "SÍ", "SI"):
                                return o["price"]
                    elif "NO ANOTAN" in pick_txt or "NO BTTS" in pick_txt:
                        for o in mkt["outcomes"]:
                            if o["name"].upper() == "NO":
                                return o["price"]

                # ── Moneyline / 1X2 ───────────────────────────────────────
                elif mkt["key"] == "h2h":
                    # Draw / Empate
                    if any(k in pick_txt for k in ("DRAW", "EMPATE", "TIE")):
                        for o in mkt["outcomes"]:
                            if o["name"].upper() in ("DRAW", "EMPATE", "TIE"):
                                return o["price"]
                    for o in mkt["outcomes"]:
                        oname = o["name"]
                        if _team_match(oname, away_raw) and _pick_is_away(pick_txt, away_raw):
                            return o["price"]
                        if _team_match(oname, home_raw) and _pick_is_home(pick_txt, home_raw):
                            return o["price"]
    return None

def fix_cuotas_reales(picks_data: dict, all_odds: dict) -> dict:
    """
    Post-procesa los picks: sobreescribe cuota_bet365 con el precio real de Bet365,
    recalcula prob_implicita y ev_pct. Si el EV cae a negativo, mueve el pick a no_apostar.
    """
    liga_map = {
        "mlb":  all_odds.get("mlb", []),
        "nba":  all_odds.get("nba", []),
        "copa": all_odds.get("copa", []),
        "nfl":  all_odds.get("nfl", []),
    }

    good_picks = []
    moved_to_na = []

    for p in picks_data.get("picks", []):
        liga_key = (p.get("liga") or "").lower()
        if   "mlb"  in liga_key: odds_list = liga_map["mlb"]
        elif "nba"  in liga_key: odds_list = liga_map["nba"]
        elif any(k in liga_key for k in ("copa","world cup","fifa")): odds_list = liga_map["copa"]
        elif "nfl"  in liga_key: odds_list = liga_map["nfl"]
        else: odds_list = []

        real_cuota = _find_real_cuota(p, odds_list)

        if real_cuota:
            old = p.get("cuota_bet365", "?")
            p["cuota_bet365"]    = real_cuota
            p["cuota_verificada"] = True
            p["prob_implicita"]  = round(100 / real_cuota, 1)
            prob_propia = p.get("prob_propia", 50)
            ev = round((prob_propia / 100 * real_cuota - 1) * 100, 1)
            p["ev_pct"] = ev
            flag = "✅" if ev > 0 else "⚠️ EV NEGATIVO"
            print(f"  {flag} [{p['matchup']}] {p['pick']}  cuota {old}→{real_cuota}  EV {ev}%")
            if ev <= 0:
                moved_to_na.append({
                    "matchup": p["matchup"],
                    "liga":    p.get("liga", ""),
                    "razon":   f"EV negativo tras cuota real Bet365 ({real_cuota}). Claude estimó {old}."
                })
            else:
                good_picks.append(p)
        else:
            p["cuota_verificada"] = False
            print(f"  ⚠ Sin cuota API para: [{p['matchup']}] {p['pick']} — cuota estimada ({p.get('cuota_bet365')})")
            good_picks.append(p)

    picks_data["picks"] = good_picks
    picks_data["no_apostar"] = picks_data.get("no_apostar", []) + moved_to_na
    picks_data["nota_lineas"] = (
        f"Cuotas verificadas con Bet365 vía Odds API. "
        f"Horario {TZ_LABEL} CDMX. {len(good_picks)} picks con EV positivo."
    )
    return picks_data

# ── 5. Claude API ─────────────────────────────────────────────────────────────
PROMPT_SYSTEM = f"""Eres un tipster profesional y analista cuantitativo de apuestas deportivas.
Casa de referencia única: Bet365 (cuotas decimales europeas).
Hoy es {today} — horario CDMX ({TZ_LABEL}, UTC{CDMX_OFFSET:+d}).

REGLAS ABSOLUTAS:
1. SOLO genera picks de partidos que aparezcan explícitamente en el contexto de datos.
2. Para el Mundial FIFA 2026: si la sección "FIFA WORLD CUP 2026" dice "Sin juegos hoy",
   NO pongas ningún partido del Mundial en picks ni en resumen_ejecutivo.
3. Todos los horarios deben mostrarse en hora CDMX (CDT/CST).
4. NO inventes partidos, equipos, ni cuotas que no estén en el contexto.
5. CUOTAS REALES: el contexto incluye cuotas REALES de Bet365 para estos mercados:
   - "1X2/ML" → moneyline (usa para picks Moneyline)
   - "Handicap/Spread: EQUIPO +X.X → Y.YY" → Asian Handicap (usa para picks AH)
   - "Total X.X: Over Y.YY / Under Z.ZZ" → totales goles/carreras (usa para picks O/U)
   - "Ambos Anotan: Sí X.XX / No Y.YY" → BTTS (usa para picks ambos anotan)
   Copia la cuota EXACTA del contexto. Si un mercado no aparece, NO lo inventes.
6. Solo propón tipos de apuesta para los cuales tengas la cuota real en el contexto.

METODOLOGÍA OBLIGATORIA (por cada pick):
1. prob_implicita = 100 / cuota_bet365 (usando la cuota exacta del contexto)
2. prob_propia = tu estimación basada en forma, pitchers, lesiones, H2H, xERA, FIP
3. EV% = (prob_propia/100 × cuota_decimal - 1) × 100  →  solo incluir si EV > 0
4. Stake: Kelly fraccional 1/4. Máx 0.3u por pick. Total sesión ≤ 3u
5. Parlays: 2-3 patas con correlación positiva; cuota mínima 1.20 por pata

Responde ÚNICAMENTE JSON válido, sin markdown, sin texto extra."""

SCHEMA_PICK = {
    "liga":           "MLB | NBA | FIFA World Cup 2026 | NFL",
    "matchup":        "AWAY @ HOME",
    "hora":           "H:MM AM/PM CDT (CDMX)",
    "pick":           "descripción concreta",
    "tipo":           "Moneyline | Total | Run Line | Total Goles | Spread | Prop",
    "cuota_bet365":   1.85,
    "prob_implicita": 55.5,
    "prob_propia":    61.0,
    "ev_pct":         5.3,
    "prob_acierto":   61,
    "estrellas":      3,
    "stake":          "0.2u",
    "razonamiento":   "2-3 líneas con datos concretos",
}

def generate_picks(context: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    schema = {
        "fecha":         today,
        "fecha_display": fecha_display,
        "generado_a":    "automatico-github-actions",
        "nota_lineas":   f"Cuotas verificadas con Bet365. Horario {TZ_LABEL} CDMX.",
        "bankroll":      {"exposicion_total": "Xu", "max_por_juego": "0.3u", "nota": "Kelly 1/4"},
        "picks":         [SCHEMA_PICK],
        "no_apostar":    [{"matchup": "...", "liga": "...", "razon": "..."}],
        "parlay_sugerido": {
            "patas":       ["Pick A (X.XX)", "Pick B (X.XX)"],
            "cuota_total": 3.50,
            "ev_pct":      5.0,
            "stake":       "0.15u",
            "nota":        "razón de la correlación",
        },
        "resumen_ejecutivo": [
            {"pick": "...", "tipo": "Moneyline", "liga": "MLB",
             "cuota": 1.85, "ev_pct": 5.3, "stake": "0.2u", "estrellas": 3}
        ],
    }
    user_msg = (
        f"Genera entre 8 y 10 picks para HOY ({today}).\n"
        f"Usa EXCLUSIVAMENTE los partidos listados en el contexto.\n"
        f"Copia las cuotas exactamente como aparecen en el contexto (son reales de Bet365).\n\n"
        f"DATOS REALES:\n{context}\n\n"
        f"Esquema JSON:\n{json.dumps(schema, indent=2, ensure_ascii=False)}"
    )
    msg = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=6000,
        system=PROMPT_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )
    raw = msg.content[0].text.strip()
    if raw.startswith("```"):
        raw = "\n".join(raw.split("\n")[1:])
        raw = raw.rstrip("`").strip()
    return json.loads(raw)

# ── 6. Guardar archivos ───────────────────────────────────────────────────────
def save_all(data: dict):
    files = {
        f"picks-{today}.json": json.dumps(data, ensure_ascii=False, indent=2),
        "latest.json":         json.dumps(data, ensure_ascii=False, indent=2),
        "picks-data.js": (
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

    all_odds = {"mlb": mlb_odds, "nba": nba_odds, "copa": cup_odds, "nfl": nfl_odds}

    context = build_context(mlb_sched, mlb_odds, nba_odds, cup_odds, nfl_odds)
    print("\n--- CONTEXTO (primeras 1200 chars) ---")
    print(context[:1200])
    print("...\n")

    print("🤖 Llamando Claude API (objetivo: 8-10 picks)...")
    try:
        picks_data = generate_picks(context)
        n = len(picks_data.get("picks", []))
        print(f"\n✅ {n} picks generados por Claude:")
        for p in picks_data.get("picks", []):
            print(f"   ★{'★'*(p.get('estrellas',1)-1)} {p['matchup']} — {p['pick']}  cuota:{p.get('cuota_bet365')}  EV+{p['ev_pct']}%")

        print("\n🔍 Verificando cuotas reales Bet365...")
        picks_data = fix_cuotas_reales(picks_data, all_odds)
        n2 = len(picks_data.get("picks", []))
        print(f"   {n2} picks con EV positivo real tras verificación")

        n2 = len(picks_data.get("picks", []))
        if n2 == 0:
            print("\n⚠️  0 picks con EV positivo — NO se sobreescriben archivos existentes.")
        else:
            print("\n💾 Guardando archivos...")
            save_all(picks_data)
            print("\n🎉 Listo — GitHub Actions hará el push automático.")

    except Exception as e:
        import traceback
        print(f"\n❌ Error: {e}")
        traceback.print_exc()
        traceback.print_exc()
        fallback = {
            "fecha":         today,
            "fecha_display": fecha_display,
            "generado_a":    "error",
            "nota_lineas":   f"Error al generar: {str(e)[:150]}",
            "bankroll":      {"exposicion_total": "0u", "max_por_juego": "0u", "nota": "Error"},
            "picks":         [],
            "no_apostar":    [{"matchup": "Error del sistema", "liga": "Sistema", "razon": str(e)[:300]}],
            "parlay_sugerido": {"patas": [], "cuota_total": 0, "ev_pct": 0, "stake": "0u", "nota": ""},
            "resumen_ejecutivo": [],
        }
        save_all(fallback)
        raise
