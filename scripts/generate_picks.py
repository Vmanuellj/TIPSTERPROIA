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
    "soccer_":               "h2h,totals,spreads,btts",
    "baseball_mlb":          "h2h,totals,spreads",
    "basketball_nba":        "h2h,totals,spreads",
    "americanfootball_nfl":  "h2h,totals,spreads",
}

# Ligas de fútbol "reales" (no Mundial) que alimentan el tab FÚTBOL.
# Fácil de extender: agregar más sport_keys de The Odds API aquí.
FUTBOL_LEAGUES = {
    "soccer_epl":            "Premier League",
    "soccer_mexico_ligamx":  "Liga MX",
}

def _markets_for(sport_key: str) -> str:
    for prefix, mkts in SPORT_MARKETS.items():
        if sport_key.startswith(prefix) or sport_key == prefix:
            return mkts
    return "h2h,totals,spreads"

PREFERRED_BOOKS = ["onexbet", "pinnacle", "betway", "williamhill", "draftkings", "unibet", "bet365"]

# Casas que pedimos a Odds API en cada llamada (misma región eu = mismo costo en
# créditos que pedir una sola). Pinnacle es el mercado "sharp" que usamos para la
# probabilidad justa sin-vig; el resto son referencias de cuota para el usuario.
REFERENCE_BOOKS = "pinnacle,bet365,onexbet,williamhill,betway,unibet"

def _best_bookmaker(bookmakers: list) -> dict | None:
    """Retorna el bookmaker preferido de la lista, en orden de preferencia."""
    bm_by_key = {bm["key"]: bm for bm in bookmakers}
    for key in PREFERRED_BOOKS:
        if key in bm_by_key:
            return bm_by_key[key]
    return bookmakers[0] if bookmakers else None

def fetch_odds_today(sport_key: str, force_all_books: bool = False) -> list:
    if not ODDS_KEY:
        print(f"  ⚠ ODDS_API_KEY no configurada — saltando {sport_key}")
        return []
    try:
        markets = _markets_for(sport_key)
        params = {
            "apiKey":     ODDS_KEY,
            "regions":    "eu",
            "markets":    markets,
            "oddsFormat": "decimal",
        }
        # Pinnacle (sharp) + casas de referencia; si no hay cobertura, usamos todas
        if not force_all_books:
            params["bookmakers"] = REFERENCE_BOOKS
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params=params,
            timeout=15,
        )
        remaining = r.headers.get("x-requests-remaining", "?")
        all_games = r.json() if r.ok else []
        today_games = [g for g in all_games if is_today_cdmx(g.get("commence_time", ""))]
        # Si las casas de referencia no traen nada, reintentar sin filtro de casa
        if not force_all_books and len(today_games) == 0 and len(all_games) == 0:
            print(f"  ↩ Casas de referencia sin datos para {sport_key} — reintentando con todas...")
            return fetch_odds_today(sport_key, force_all_books=True)
        print(f"  Odds API ({sport_key}) mkts={markets} → "
              f"{len(today_games)}/{len(all_games)} juegos HOY | requests restantes: {remaining}")
        return today_games
    except Exception as e:
        print(f"  ⚠ Odds API ({sport_key}): {e}")
        return []

def fetch_props_today(sport_key: str, prop_markets: str, region: str = "us") -> list:
    """Fetch player props para el deporte dado."""
    if not ODDS_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/odds/",
            params={
                "apiKey":     ODDS_KEY,
                "regions":    region,
                "markets":    prop_markets,
                "oddsFormat": "decimal",
            },
            timeout=15,
        )
        if not r.ok:
            return []
        all_games = r.json()
        today_games = [g for g in all_games if is_today_cdmx(g.get("commence_time", ""))]
        print(f"  Props ({sport_key} | {prop_markets}) → {len(today_games)} juegos HOY")
        return today_games
    except Exception as e:
        print(f"  ⚠ Props {sport_key}: {e}")
        return []

# ── 2b. Estadística real de ESPN (API pública, sin API key) ───────────────────
ESPN_SLUGS = {
    "baseball_mlb":         "baseball/mlb",
    "basketball_nba":       "basketball/nba",
    "americanfootball_nfl": "football/nfl",
    "soccer_epl":           "soccer/eng.1",
    "soccer_mexico_ligamx": "soccer/mex.1",
}

def _espn_competitor_blurb(c: dict) -> str:
    """Resumen de un equipo: nombre (récord, local/visitante, forma) — abridor (récord, ERA)."""
    name = c.get("team", {}).get("displayName", "")
    recs = {r.get("type"): r.get("summary") for r in (c.get("records") or [])}
    extras = []
    if recs.get("total"):
        extras.append(recs["total"])
    ha = c.get("homeAway")
    split = recs.get("home") if ha == "home" else recs.get("road")
    if split:
        extras.append(f"{'local' if ha == 'home' else 'visitante'} {split}")
    if c.get("form"):
        extras.append(f"forma {c['form']}")
    s = f"{name} ({', '.join(extras)})" if extras else name
    for pr in (c.get("probables") or []):
        pn = pr.get("athlete", {}).get("displayName", "")
        rec = pr.get("record") or ""
        if pn:
            s += f" — abridor {pn} {rec}".rstrip()
    return s

def fetch_espn_stats(sport_key: str) -> dict:
    """
    Devuelve {(away_name, home_name): blurb} con estadística real de ESPN para hoy.
    Sin API key. Si el deporte está fuera de temporada o falla, regresa {}.
    """
    slug = ESPN_SLUGS.get(sport_key)
    if not slug:
        return {}
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
        data = requests.get(url, timeout=12).json()
    except Exception as e:
        print(f"  ⚠ ESPN {slug}: {e}")
        return {}
    out = {}
    for ev in data.get("events", []):
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors", [])
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        if not (away and home):
            continue
        key = (away.get("team", {}).get("displayName", ""),
               home.get("team", {}).get("displayName", ""))
        out[key] = f"{_espn_competitor_blurb(away)}  @  {_espn_competitor_blurb(home)}"
    n = len(out)
    if n:
        print(f"  ESPN ({slug}) → stats de {n} partidos")
    return out

def _espn_blurb_for(away_g: str, home_g: str, espn_map: dict):
    """Busca el blurb de ESPN que corresponde a un partido de odds (match por nombre)."""
    for (ea, eh), blurb in (espn_map or {}).items():
        if _team_match(away_g, ea) and _team_match(home_g, eh):
            return blurb
        if _team_match(away_g, eh) and _team_match(home_g, ea):
            return blurb
    return None

# ── 3. Construir contexto ─────────────────────────────────────────────────────
def build_context(mlb_sched, mlb_odds, nba_odds, nfl_odds, futbol_odds=None,
                   mlb_props=None, nba_props=None, nfl_props=None, futbol_props=None,
                   espn_stats=None) -> str:
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

    espn_stats = espn_stats or {}

    def fmt_odds_section(games: list, label: str, espn_map: dict = None):
        lines.append(f"\n=== {label} ===")
        if not games:
            lines.append("  Sin juegos hoy.")
            return
        for g in games:
            away  = g.get("away_team", "")
            home  = g.get("home_team", "")
            ctime = utc_str_to_cdmx(g.get("commence_time", "").replace("Z", ""))
            lines.append(f"• {away} @ {home}  ({ctime} CDMX)")
            blurb = _espn_blurb_for(away, home, espn_map)
            if blurb:
                lines.append(f"  ESTADÍSTICAS REALES (ESPN): {blurb}")
            bm = _best_bookmaker(g.get("bookmakers", []))
            if not bm:
                continue
            bm_name = bm.get("title", bm.get("key", "Casa"))
            for mkt in bm.get("markets", []):
                if mkt["key"] == "h2h":
                    oc   = {o["name"]: o["price"] for o in mkt["outcomes"]}
                    draw = oc.get("Draw", "")
                    dstr = f" | Empate {draw}" if draw else ""
                    lines.append(
                        f"  {bm_name} 1X2/ML: {away} {oc.get(away, '—')} / "
                        f"{home} {oc.get(home, '—')}{dstr}"
                    )
                elif mkt["key"] == "spreads":
                    for o in mkt["outcomes"]:
                        pt = o.get("point", "")
                        lines.append(
                            f"  {bm_name} Handicap/Spread: {o['name']} {pt:+g} → {o['price']}"
                            if isinstance(pt, (int, float)) else
                            f"  {bm_name} Handicap/Spread: {o['name']} {pt} → {o['price']}"
                        )
                elif mkt["key"] == "btts":
                    oc = {o["name"]: o["price"] for o in mkt["outcomes"]}
                    lines.append(
                        f"  {bm_name} Ambos Anotan: Sí {oc.get('Yes','—')} / No {oc.get('No','—')}"
                    )
                elif mkt["key"] == "totals":
                    for o in mkt["outcomes"]:
                        if o["name"] == "Over":
                            under_price = next(
                                (x["price"] for x in mkt["outcomes"] if x["name"] == "Under"), "—"
                            )
                            lines.append(
                                f"  {bm_name} Total {o.get('point', '')}: "
                                f"Over {o['price']} / Under {under_price}"
                            )

    def fmt_props_section(games: list, label: str):
        lines.append(f"\n=== {label} ===")
        if not games:
            lines.append("  Sin props disponibles hoy.")
            return
        for g in games:
            away  = g.get("away_team", "")
            home  = g.get("home_team", "")
            ctime = utc_str_to_cdmx(g.get("commence_time", "").replace("Z", ""))
            game_props = []
            for bm in g.get("bookmakers", []):
                for mkt in bm.get("markets", []):
                    for o in mkt.get("outcomes", []):
                        player = o.get("description", o.get("name", ""))
                        name   = mkt.get("key", "").replace("_", " ").title()
                        pt     = o.get("point", "")
                        price  = o.get("price", "")
                        if o.get("name", "").upper() == "OVER" and pt and price:
                            game_props.append(f"  • {player} — {name} Over {pt} @ {price}")
                if game_props:
                    break  # Solo necesitamos una casa para el contexto
            if game_props:
                lines.append(f"• {away} @ {home}  ({ctime} CDMX)")
                lines.extend(game_props[:8])  # Máx 8 props por partido

    fmt_odds_section(mlb_odds, "MLB — CUOTAS HOY", espn_stats.get("baseball_mlb"))
    fmt_odds_section(nba_odds, "NBA — CUOTAS HOY", espn_stats.get("basketball_nba"))
    fmt_odds_section(nfl_odds, "NFL — CUOTAS HOY", espn_stats.get("americanfootball_nfl"))
    for key, label in FUTBOL_LEAGUES.items():
        fmt_odds_section((futbol_odds or {}).get(key, []), f"{label.upper()} — CUOTAS HOY",
                         espn_stats.get(key))
    fmt_props_section(mlb_props or [], "MLB — PROPS DE JUGADORES HOY")
    fmt_props_section(nba_props or [], "NBA — PROPS DE JUGADORES HOY")
    fmt_props_section(nfl_props or [], "NFL — PROPS DE JUGADORES HOY")
    for key, label in FUTBOL_LEAGUES.items():
        fmt_props_section((futbol_props or {}).get(key, []), f"{label.upper()} — PROPS DE JUGADORES HOY")
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

def _parse_matchup(matchup: str):
    """'AWAY @ HOME' o 'AWAY vs HOME' → (away, home) o None."""
    if " @ " in matchup:
        a, h = matchup.split(" @ ", 1)
    elif " VS " in matchup.upper():
        parts = re.split(r'\s+vs\s+', matchup, flags=re.IGNORECASE)
        if len(parts) < 2:
            return None
        a, h = parts[0], parts[1]
    else:
        return None
    return a.strip(), h.strip()

def _median(nums: list) -> float | None:
    s = sorted(nums)
    n = len(s)
    if n == 0:
        return None
    mid = n // 2
    return s[mid] if n % 2 else (s[mid - 1] + s[mid]) / 2

def _consensus_cuota(pick: dict, odds_list: list) -> float | None:
    """
    Cuota de CONSENSO (mediana) para el outcome del pick, recolectando su precio en
    TODAS las casas del partido. La mediana es robusta a líneas atípicas/viejas — es
    "la cuota más promedio en la que se puede encontrar" y evita el EV fake que daba
    tomar el máximo. Cubre h2h/totals/spreads/btts.
    """
    parsed = _parse_matchup(pick.get("matchup", ""))
    if not parsed:
        return None
    away_raw, home_raw = parsed
    pick_txt = (pick.get("pick") or "").upper()
    hcap_val = _extract_handicap_value(pick_txt)

    for game in odds_list:
        away_g = game.get("away_team", "")
        home_g = game.get("home_team", "")
        if not (_team_match(away_raw, away_g) and _team_match(home_raw, home_g)):
            continue
        prices = []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                price = _picked_price_in_market(mkt, pick_txt, away_raw, home_raw,
                                                away_g, home_g, hcap_val)
                if price:
                    prices.append(price)
        med = _median(prices)
        return round(med, 2) if med else None
    return None

# ── 4b. Probabilidad "justa" sin-vig desde el mercado sharp (Pinnacle) ─────────
def _picked_price_in_market(mkt: dict, pick_txt: str, away_raw: str, home_raw: str,
                            away_g: str, home_g: str, hcap_val) -> float | None:
    """Precio del outcome que corresponde al pick, dentro de un mercado dado.
    Reusa las mismas reglas de match de equipos/outcome del resto del módulo."""
    key = mkt.get("key")
    outs = mkt.get("outcomes", [])
    if key == "totals":
        if any(k in pick_txt for k in ("OVER", "MAS DE", "MÁS DE", "+")):
            return next((o["price"] for o in outs if o["name"].upper() == "OVER"), None)
        if any(k in pick_txt for k in ("UNDER", "MENOS DE", "-")):
            return next((o["price"] for o in outs if o["name"].upper() == "UNDER"), None)
    elif key == "spreads":
        is_away = _pick_is_away(pick_txt, away_raw)
        is_home = _pick_is_home(pick_txt, home_raw)
        for o in outs:
            pt = o.get("point")
            if hcap_val is not None and pt is not None and abs(abs(float(pt)) - abs(hcap_val)) > 0.26:
                continue
            if is_away and _team_match(o["name"], away_g):
                return o["price"]
            if is_home and _team_match(o["name"], home_g):
                return o["price"]
    elif key == "btts":
        if any(k in pick_txt for k in ("AMBOS ANOTAN", "BOTH TEAMS", "BTTS", "SI ANOTAN", "SÍ")):
            return next((o["price"] for o in outs if o["name"].upper() in ("YES", "SÍ", "SI")), None)
        if "NO ANOTAN" in pick_txt or "NO BTTS" in pick_txt:
            return next((o["price"] for o in outs if o["name"].upper() == "NO"), None)
    elif key == "h2h":
        if any(k in pick_txt for k in ("DRAW", "EMPATE", "TIE")):
            p = next((o["price"] for o in outs if o["name"].upper() in ("DRAW", "EMPATE", "TIE")), None)
            if p:
                return p
        for o in outs:
            if _team_match(o["name"], away_raw) and _pick_is_away(pick_txt, away_raw):
                return o["price"]
            if _team_match(o["name"], home_raw) and _pick_is_home(pick_txt, home_raw):
                return o["price"]
    return None

def pinnacle_fair(pick: dict, odds_list: list):
    """
    Probabilidad justa (sin-vig) del outcome del pick según Pinnacle (mercado sharp).
    Devuelve (fair_p, cuota_minima) con fair_p en 0..1 y cuota_minima = 1/fair_p,
    o (None, None) si no hay línea de Pinnacle para ese partido/mercado.
    """
    parsed = _parse_matchup(pick.get("matchup", ""))
    if not parsed:
        return None, None
    away_raw, home_raw = parsed
    pick_txt = (pick.get("pick") or "").upper()
    hcap_val = _extract_handicap_value(pick_txt)

    for game in odds_list:
        away_g = game.get("away_team", "")
        home_g = game.get("home_team", "")
        if not (_team_match(away_raw, away_g) and _team_match(home_raw, home_g)):
            continue
        pinn = next((bm for bm in game.get("bookmakers", []) if bm.get("key") == "pinnacle"), None)
        if not pinn:
            return None, None
        for mkt in pinn.get("markets", []):
            prices = [o.get("price") for o in mkt.get("outcomes", []) if o.get("price")]
            if len(prices) < 2:
                continue
            picked = _picked_price_in_market(mkt, pick_txt, away_raw, home_raw, away_g, home_g, hcap_val)
            if not picked:
                continue
            inv_sum = sum(1.0 / p for p in prices)   # incluye el vig
            if inv_sum <= 0:
                continue
            fair_p = (1.0 / picked) / inv_sum          # prob sin-vig del outcome
            if fair_p > 0:
                return round(fair_p, 4), round(1.0 / fair_p, 2)
        return None, None
    return None, None

# Guardrail: si la prob propia supera a la prob justa sin-vig de Pinnacle por más de
# esto (en fracción), el modelo está sobre-confiado vs el mercado sharp → se descarta.
SHARP_GUARDRAIL = 0.07

def fix_cuotas_reales(picks_data: dict, all_odds: dict) -> dict:
    """
    Post-procesa los picks con el marco de la ficha (prob casino / prob propia / cuota mín):
      1. cuota = mediana de consenso entre casas (robusta a outliers).
      2. prob_casino (prob_implicita) = 100 / cuota.
      3. prob_propia = estimación del modelo (anclada a datos reales de ESPN en el prompt).
      4. cuota_minima = 100 / prob_propia (precio que tu casa debe superar).
      5. EV = (prob_propia/100 × cuota − 1) × 100.
      6. Pinnacle es guardrail OCULTO: si prob_propia supera a la prob justa sin-vig por
         > SHARP_GUARDRAIL, el pick se descarta a no_apostar (sobre-confianza).
    De paso marca p["sport_key"] para que check_results.py sepa qué scores pedir.
    """
    liga_map = {
        "mlb": ("baseball_mlb", all_odds.get("baseball_mlb", [])),
        "nba": ("basketball_nba", all_odds.get("basketball_nba", [])),
        "nfl": ("americanfootball_nfl", all_odds.get("americanfootball_nfl", [])),
    }
    for key, label in FUTBOL_LEAGUES.items():
        liga_map[label.lower()] = (key, all_odds.get(key, []))

    good_picks = []
    moved_to_na = []

    for p in picks_data.get("picks", []):
        liga_key = (p.get("liga") or "").lower()
        odds_list = []
        for needle, (sport_key, odds) in liga_map.items():
            if needle in liga_key:
                p["sport_key"] = sport_key
                odds_list = odds
                break

        # 1. Cuota de consenso (mediana). Si no hay, usa la de Claude.
        cuota = _consensus_cuota(p, odds_list)
        if cuota:
            p["cuota_bet365"]     = cuota
            p["cuota_verificada"] = True
        else:
            p["cuota_verificada"] = False
            cuota = p.get("cuota_bet365") or 0
        if not cuota:
            moved_to_na.append({"matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
                                "razon": "Sin cuota disponible."})
            continue

        # 2-5. Marco de probabilidades de la ficha
        prob_propia = p.get("prob_propia", 50) or 50
        p["prob_implicita"] = round(100 / cuota, 1)              # prob casino
        p["cuota_minima"]   = round(100 / prob_propia, 2)        # break-even a prob propia
        ev = round((prob_propia / 100 * cuota - 1) * 100, 1)
        p["ev_pct"] = ev

        # 6. Guardrail sharp (oculto): descarta sobre-confianza vs Pinnacle
        fair_p, _ = pinnacle_fair(p, odds_list)
        if fair_p is not None and (prob_propia / 100 - fair_p) > SHARP_GUARDRAIL:
            print(f"  ⚠️ SOBRE-CONFIANZA [{p['matchup']}] {p['pick']}  "
                  f"propia {prob_propia}% vs sharp {round(fair_p*100,1)}%")
            moved_to_na.append({
                "matchup": p["matchup"], "liga": p.get("liga", ""),
                "razon": (f"Probabilidad propia ({prob_propia}%) muy por encima del mercado "
                          f"sharp ({round(fair_p*100,1)}%) — descartado por sobre-confianza."),
            })
            continue

        if ev > 0:
            print(f"  ✅ [{p['matchup']}] {p['pick']}  cuota {cuota}  EV {ev}%  "
                  f"(casino {p['prob_implicita']}% vs propia {prob_propia}%)")
            good_picks.append(p)
        else:
            print(f"  ⚠️ EV≤0 [{p['matchup']}] {p['pick']}  cuota {cuota}  EV {ev}%")
            moved_to_na.append({
                "matchup": p["matchup"], "liga": p.get("liga", ""),
                "razon": f"EV negativo con cuota de consenso ({cuota}). Prob casino {p['prob_implicita']}% ≥ propia {prob_propia}%.",
            })

    picks_data["picks"] = good_picks
    picks_data["no_apostar"] = picks_data.get("no_apostar", []) + moved_to_na
    picks_data["nota_lineas"] = (
        f"Cuota de consenso (mediana entre casas). Probabilidad propia calculada con "
        f"estadística real de ESPN. La cuota mínima es el precio que tu casa (PlayDoIt/Winpot) "
        f"debe superar para que haya valor. {len(good_picks)} picks con EV+. Horario {TZ_LABEL} CDMX."
    )
    return picks_data

# ── 5. Claude API ─────────────────────────────────────────────────────────────
PROMPT_SYSTEM = f"""Eres un tipster profesional y analista cuantitativo de apuestas deportivas.
Casa de referencia principal: 1xBet (cuotas decimales europeas). Si no hay 1xBet, usa la mejor casa disponible.
Hoy es {today} — horario CDMX ({TZ_LABEL}, UTC{CDMX_OFFSET:+d}).

REGLAS ABSOLUTAS:
1. SOLO genera picks de partidos que aparezcan explícitamente en el contexto de datos.
2. Todos los horarios deben mostrarse en hora CDMX (CDT/CST).
3. NO inventes partidos, equipos, ni cuotas que no estén en el contexto.
4. CUOTAS REALES: el contexto incluye cuotas REALES para estos mercados:
   - "1X2/ML" → moneyline (usa para picks Moneyline)
   - "Handicap/Spread: EQUIPO +X.X → Y.YY" → Asian Handicap (usa para picks AH)
   - "Total X.X: Over Y.YY / Under Z.ZZ" → totales goles/carreras (usa para picks O/U)
   - "Ambos Anotan: Sí X.XX / No Y.YY" → BTTS (usa para picks ambos anotan)
   Copia la cuota EXACTA del contexto. Si un mercado no aparece, NO lo inventes.
5. Solo propón tipos de apuesta para los cuales tengas la cuota real en el contexto.
6. PROPS DE JUGADORES: el contexto incluye secciones "— PROPS DE JUGADORES HOY" para
   MLB, NBA, NFL y cada liga de fútbol (Premier League, Liga MX). Si hay props
   disponibles con EV positivo, inclúyelos como picks con tipo "Prop Jugador".
   Formato del pick: "NOMBRE JUGADOR Over/Under X.X [stat]" (ej: "Gerrit Cole Over 6.5
   Strikeouts", "Patrick Mahomes Over 275.5 Passing Yards", "Erling Haaland Anytime
   Goalscorer"). Solo incluye props si la cuota y el stat aparecen EXPLÍCITAMENTE en
   el contexto. Busca props en TODOS los deportes con datos disponibles, no solo MLB/NBA.
7. LIGAS DE FÚTBOL: además de MLB/NBA/NFL, el contexto puede incluir Premier League y
   Liga MX. Trátalas igual que cualquier otra liga — mismas reglas de cuotas reales y EV.
   En fútbol el empate cuenta como resultado propio para picks de moneyline (1X2).

METODOLOGÍA OBLIGATORIA (por cada pick):
1. prob_implicita = 100 / cuota_bet365 (usando la cuota exacta del contexto)
2. prob_propia = tu estimación REAL basada en las ESTADÍSTICAS REALES (ESPN) del contexto:
   récords, récord local/visitante, ERA de equipo y de pitchers probables, AVG, líderes,
   forma reciente. NO adivines: apóyate en esos números. El razonamiento (razonamiento)
   DEBE citar estadísticas concretas de las provistas (ej. "record 50-40, ERA del abridor
   2.00, forma WWLWW"). Prohibido inventar cifras que no estén en el contexto.
3. EV% = (prob_propia/100 × cuota_decimal - 1) × 100  →  solo incluir si EV > 0.
   IMPORTANTE: sé CONSERVADOR con prob_propia. El sistema descarta automáticamente picks
   donde tu prob_propia quede muy por encima del mercado sharp (sobre-confianza). No infles
   probabilidades — una prob_propia realista y bien justificada vale más que una alta.
4. Stake: Kelly fraccional 1/4. Máx 0.3u por pick. Total sesión ≤ 3u
5. Parlays: 2-3 patas con correlación positiva; cuota mínima 1.20 por pata
6. ESTRELLAS (escala de confianza 1 a 5, no 1 a 3):
   5 = confianza máxima, edge muy claro y bien soportado por los datos (usar poco)
   4 = confianza alta
   3 = confianza media
   2 = confianza baja
   1 = especulativo / valor marginal
   Reserva el 5 para el pick más fuerte del día, si lo hay — no todos los días debe
   haber un pick de 5 estrellas.

Responde ÚNICAMENTE JSON válido, sin markdown, sin texto extra."""

SCHEMA_PICK = {
    "liga":           "MLB | NBA | NFL | Premier League | Liga MX",
    "matchup":        "AWAY @ HOME",
    "hora":           "H:MM AM/PM CDT (CDMX)",
    "pick":           "descripción concreta",
    "tipo":           "Moneyline | Total | Run Line | Total Goles | Spread | Prop Jugador",
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

    print("\n💰 Obteniendo odds HOY (mejor casa disponible por deporte)...")
    mlb_odds = fetch_odds_today("baseball_mlb")
    nba_odds = fetch_odds_today("basketball_nba")
    nfl_odds = fetch_odds_today("americanfootball_nfl")
    futbol_odds = {key: fetch_odds_today(key) for key in FUTBOL_LEAGUES}

    print("\n🎯 Obteniendo props de jugadores HOY...")
    mlb_props = fetch_props_today("baseball_mlb", "batter_home_runs,batter_hits,pitcher_strikeouts")
    nba_props = fetch_props_today("basketball_nba", "player_points,player_rebounds,player_assists,player_threes")
    nfl_props = fetch_props_today("americanfootball_nfl", "player_pass_yds,player_rush_yds,player_receptions,player_anytime_td")
    futbol_props = {key: fetch_props_today(key, "player_goal_scorer_anytime", region="eu") for key in FUTBOL_LEAGUES}

    print("\n📊 Obteniendo estadística real de ESPN (récords, ERA, forma)...")
    espn_stats = {sk: fetch_espn_stats(sk) for sk in ESPN_SLUGS}

    all_odds = {
        "baseball_mlb": mlb_odds,
        "basketball_nba": nba_odds,
        "americanfootball_nfl": nfl_odds,
        **futbol_odds,
    }

    context = build_context(mlb_sched, mlb_odds, nba_odds, nfl_odds, futbol_odds,
                             mlb_props, nba_props, nfl_props, futbol_props, espn_stats)
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
