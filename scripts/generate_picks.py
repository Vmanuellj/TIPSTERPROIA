#!/usr/bin/env python3
"""
TIPSTER PRO IA — Generador automático (GitHub Actions)
v4 (híbrido, sin The Odds API):
  • Calendario + estadística: MLB Stats API + ESPN (gratis, sin API key)
  • Cuotas: Claude API con herramienta de BÚSQUEDA WEB (web_search) — las obtiene por
    partido de fuentes públicas. Marca cuota_verificada=false / fair_source="web".
  • Tenis: los partidos de hoy los descubre Claude por búsqueda web (ESPN los agrupa por torneo).
"""
import anthropic, json, os, re, requests, sys, time
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

# Override de fecha para PRUEBAS manuales: TARGET_DATE=YYYY-MM-DD genera esa fecha
# (p. ej. mañana, con partidos POR VENIR). Vacío = hoy (uso normal del cron 00:30).
_TARGET = os.environ.get("TARGET_DATE", "").strip()
if _TARGET:
    try:
        dt    = datetime.strptime(_TARGET, "%Y-%m-%d").replace(tzinfo=CT)
        today = dt.strftime("%Y-%m-%d")
        fecha_display = f"{DIAS[dt.weekday()]} {dt.day} de {MESES[dt.month-1]} {dt.year}"
        print(f"  ⚙ TARGET_DATE activo: generando para {today}")
    except Exception as _e:
        print(f"  ⚠ TARGET_DATE inválida ({_TARGET}); uso hoy. {_e}")

ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ODDS_KEY      = os.environ.get("ODDS_API_KEY", "")

# ── Modelo + búsqueda web (híbrido: las cuotas las obtiene Claude por web_search) ──
MODEL          = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-6")
WEB_SEARCH_MAX = int(os.environ.get("WEB_SEARCH_MAX", "20"))  # tope de búsquedas web/run

def _guess_sport_key(liga: str) -> str:
    l = (liga or "").lower()
    if "mlb" in l or "beisbol" in l or "béisbol" in l: return "baseball_mlb"
    if "nba" in l: return "basketball_nba"
    if "nfl" in l: return "americanfootball_nfl"
    if "wta" in l: return "tennis_wta"
    if "atp" in l or "tenis" in l or "tennis" in l: return "tennis_atp"
    return "soccer_generic"

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
#   Fútbol/Soccer: h2h (1X2) + totals (O/U goles) + spreads (Asian Handicap)
#   Baseball/Basket: h2h + totals + spreads (run line / punto y medio)
# NOTA: 'btts' (ambos anotan) NO se pide aquí. Es un "additional market" que The
# Odds API solo sirve en el endpoint por-evento, no en /sports/{key}/odds; pedirlo
# hacía que TODA la petición de fútbol fallara (0 partidos, sin cobrar créditos).
SPORT_MARKETS = {
    "soccer_":               "h2h,totals,spreads",
    "baseball_mlb":          "h2h,totals,spreads",
    "basketball_nba":        "h2h,totals,spreads",
    "americanfootball_nfl":  "h2h,totals,spreads",
    "tennis_":               "h2h",   # tennis: solo ganador del partido (2 vías, sin empate)
}

# Ligas de fútbol que alimentan el tab FÚTBOL.
# Antes era una lista fija de 2 ligas; ahora se descubren EN VIVO desde The Odds
# API (igual que el tenis). Por defecto se limita a las ligas MAYORES para
# controlar el gasto de créditos. Pon FUTBOL_ALL_LEAGUES=True para considerar
# TODAS las ligas de fútbol activas del mundo (más cobertura, mucho más costo).
FUTBOL_ALL_LEAGUES = False

# Ligas mayores con mercados líquidos (las buenas para +EV). Se usan salvo que
# FUTBOL_ALL_LEAGUES sea True. Fácil de extender: agrega más sport_keys aquí.
FUTBOL_MAJOR_KEYS = {
    "soccer_epl",                        # Premier League (Inglaterra)
    "soccer_spain_la_liga",              # La Liga (España)
    "soccer_italy_serie_a",              # Serie A (Italia)
    "soccer_germany_bundesliga",         # Bundesliga (Alemania)
    "soccer_france_ligue_one",           # Ligue 1 (Francia)
    "soccer_uefa_champs_league",         # UEFA Champions League
    "soccer_uefa_europa_league",         # UEFA Europa League
    "soccer_usa_mls",                    # MLS (EE.UU./Canadá)
    "soccer_mexico_ligamx",              # Liga MX (México)
    "soccer_brazil_campeonato",          # Brasileirão Série A
    "soccer_argentina_primera_division", # Primera División (Argentina)
    "soccer_netherlands_eredivisie",     # Eredivisie (Países Bajos)
    "soccer_portugal_primeira_liga",     # Primeira Liga (Portugal)
    "soccer_england_efl_champ",          # Championship (Inglaterra 2ª)
}

# Fallback si la lista de deportes no responde (red/quota): al menos las de siempre.
FUTBOL_FALLBACK = {"soccer_epl": "Premier League", "soccer_mexico_ligamx": "Liga MX"}

# Se llena dinámicamente en main() con {sport_key: título} de las ligas activas.
FUTBOL_LEAGUES = {}

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

def fetch_alt_totals(sport_key: str, event_id: str) -> dict | None:
    """Líneas alternas de totales (Over/Under 0.5..5.5) de UN partido, vía el endpoint
    por-evento de The Odds API. Permite ofrecer Over/Under 1.5 y 3.5 además del 2.5.
    Cuesta ~1 crédito por partido."""
    if not (ODDS_KEY and event_id):
        return None
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/events/{event_id}/odds/",
            params={"apiKey": ODDS_KEY, "regions": "eu",
                    "markets": "alternate_totals", "oddsFormat": "decimal"},
            timeout=15,
        )
        return r.json() if r.ok else None
    except Exception as e:
        print(f"  ⚠ alt totals ({sport_key}): {e}")
        return None

def _merge_alt_totals(game: dict, alt_event: dict) -> None:
    """Fusiona los mercados alternate_totals del evento dentro de los bookmakers del game
    (para que consenso/Pinnacle y el contexto vean las líneas 1.5/2.5/3.5)."""
    if not alt_event:
        return
    existing = {bm.get("key"): bm for bm in game.get("bookmakers", [])}
    for abm in alt_event.get("bookmakers", []):
        alt_mkts = [m for m in abm.get("markets", []) if m.get("key") == "alternate_totals"]
        if not alt_mkts:
            continue
        tgt = existing.get(abm.get("key"))
        if tgt:
            tgt.setdefault("markets", []).extend(alt_mkts)
        else:
            game.setdefault("bookmakers", []).append(
                {"key": abm.get("key"), "title": abm.get("title", ""), "markets": alt_mkts})

def fetch_active_sports(prefix: str) -> list:
    """Keys de deportes ACTIVOS en Odds API que empiezan con `prefix` (ej. 'tennis_').
    Los torneos de tennis cambian con el calendario, así que se consultan en vivo."""
    if not ODDS_KEY:
        return []
    try:
        r = requests.get("https://api.the-odds-api.com/v4/sports/",
                         params={"apiKey": ODDS_KEY}, timeout=12)
        if not r.ok:
            return []
        keys = [s["key"] for s in r.json()
                if s.get("active") and str(s.get("key", "")).startswith(prefix)]
        print(f"  Deportes activos '{prefix}*': {keys}")
        return keys
    except Exception as e:
        print(f"  ⚠ sports API ({prefix}): {e}")
        return []

def fetch_futbol_leagues() -> dict:
    """{sport_key: título} de ligas de fútbol ACTIVAS en Odds API.
    Si FUTBOL_ALL_LEAGUES es False, se limita a FUTBOL_MAJOR_KEYS (ligas mayores)
    para controlar el gasto de créditos. Cada liga cuesta ~4 créditos/día."""
    if not ODDS_KEY:
        return {}
    try:
        r = requests.get("https://api.the-odds-api.com/v4/sports/",
                         params={"apiKey": ODDS_KEY}, timeout=12)
        if not r.ok:
            return {}
        out = {}
        for s in r.json():
            key = str(s.get("key", ""))
            if not (s.get("active") and key.startswith("soccer_")):
                continue
            if not FUTBOL_ALL_LEAGUES and key not in FUTBOL_MAJOR_KEYS:
                continue
            out[key] = s.get("title") or key
        scope = "TODAS" if FUTBOL_ALL_LEAGUES else "mayores"
        print(f"  Ligas de fútbol activas ({scope}): {list(out)}")
        return out
    except Exception as e:
        print(f"  ⚠ ligas fútbol: {e}")
        return {}

def fetch_tennis_today() -> list:
    """Junta los partidos de HOY de todos los torneos de tennis activos (ATP+WTA)."""
    games = []
    for key in fetch_active_sports("tennis_"):
        games.extend(fetch_odds_today(key))   # cada juego ya trae su sport_key real
    print(f"  Tennis → {len(games)} partidos HOY (todos los torneos activos)")
    return games

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
    # Fútbol: slug de ESPN por liga (récord, forma real). Best-effort: si una liga
    # no tiene slug o no juega hoy, simplemente no añade estadística.
    "soccer_epl":                        "soccer/eng.1",
    "soccer_spain_la_liga":              "soccer/esp.1",
    "soccer_italy_serie_a":              "soccer/ita.1",
    "soccer_germany_bundesliga":         "soccer/ger.1",
    "soccer_france_ligue_one":           "soccer/fra.1",
    "soccer_uefa_champs_league":         "soccer/uefa.champions",
    "soccer_uefa_europa_league":         "soccer/uefa.europa",
    "soccer_usa_mls":                    "soccer/usa.1",
    "soccer_mexico_ligamx":              "soccer/mex.1",
    "soccer_brazil_campeonato":          "soccer/bra.1",
    "soccer_argentina_primera_division": "soccer/arg.1",
    "soccer_netherlands_eredivisie":     "soccer/ned.1",
    "soccer_portugal_primeira_liga":     "soccer/por.1",
    "soccer_england_efl_champ":          "soccer/eng.2",
}

# {NOMBRE_EQUIPO_UPPER: url_logo} — se llena desde ESPN en fetch_espn_stats.
# Sirve para pintar logos reales de fútbol en la web (misma fuente que las stats).
ESPN_TEAM_LOGOS: dict = {}
# Etiquetas legibles por liga (antes venían de The Odds API; ahora estáticas).
LIGA_LABELS = {
    "baseball_mlb": "MLB", "basketball_nba": "NBA", "americanfootball_nfl": "NFL",
    "soccer_epl": "Premier League", "soccer_spain_la_liga": "La Liga",
    "soccer_italy_serie_a": "Serie A", "soccer_germany_bundesliga": "Bundesliga",
    "soccer_france_ligue_one": "Ligue 1", "soccer_uefa_champs_league": "UEFA Champions League",
    "soccer_uefa_europa_league": "UEFA Europa League", "soccer_usa_mls": "MLS",
    "soccer_mexico_ligamx": "Liga MX", "soccer_brazil_campeonato": "Brasileirão",
    "soccer_argentina_primera_division": "Primera División (Argentina)",
    "soccer_netherlands_eredivisie": "Eredivisie", "soccer_portugal_primeira_liga": "Primeira Liga",
    "soccer_england_efl_champ": "Championship",
}
SOCCER_KEYS = [k for k in ESPN_SLUGS if k.startswith("soccer_")]

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
        # CRÍTICO: fijar la fecha de HOY (YYYYMMDD). Sin este parámetro, ESPN
        # devuelve el scoreboard "actual" que a las 12:30am puede ser el del día
        # ANTERIOR (mismo partido de la serie, pitchers de ayer) → justificaciones
        # con datos equivocados.
        url = f"https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
        data = requests.get(url, params={"dates": today.replace("-", "")}, timeout=12).json()
    except Exception as e:
        print(f"  ⚠ ESPN {slug}: {e}")
        return {}
    out = {}
    skipped = 0
    for ev in data.get("events", []):
        # Descartar juegos ya jugados (state 'post'): solo queremos el de HOY sin empezar
        state = (ev.get("status", {}).get("type", {}) or {}).get("state", "")
        if state == "post":
            skipped += 1
            continue
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors", [])
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        if not (away and home):
            continue
        # Guardar logo real de cada equipo (misma fuente ESPN) para la web.
        for c in (away, home):
            t = c.get("team", {})
            nm, logo = t.get("displayName", ""), t.get("logo")
            if nm and logo:
                ESPN_TEAM_LOGOS[nm.upper().strip()] = logo
        key = (away.get("team", {}).get("displayName", ""),
               home.get("team", {}).get("displayName", ""))
        out[key] = f"{_espn_competitor_blurb(away)}  @  {_espn_competitor_blurb(home)}"
    n = len(out)
    if n or skipped:
        print(f"  ESPN ({slug}) {today} → {n} partidos de hoy" +
              (f" ({skipped} ya jugados, descartados)" if skipped else ""))
    return out

def fetch_espn_schedule(sport_key: str, liga_label: str) -> list:
    """
    Calendario de HOY desde ESPN (gratis, sin API key) para deportes de equipo
    (MLB/NBA/NFL/fútbol). Devuelve [{away, home, time, sport_key, liga}].
    Descarta partidos ya jugados (state 'post'). Reemplaza al calendario que antes
    daba The Odds API. También aprovecha para cachear logos reales de ESPN.
    """
    slug = ESPN_SLUGS.get(sport_key)
    if not slug:
        return []
    try:
        url = f"https://site.api.espn.com/apis/site/v2/sports/{slug}/scoreboard"
        data = requests.get(url, params={"dates": today.replace("-", "")}, timeout=12).json()
    except Exception as e:
        print(f"  ⚠ ESPN sched {slug}: {e}")
        return []
    games = []
    for ev in data.get("events", []):
        state = (ev.get("status", {}).get("type", {}) or {}).get("state", "")
        if state == "post":
            continue
        comp = (ev.get("competitions") or [{}])[0]
        comps = comp.get("competitors", [])
        away = next((c for c in comps if c.get("homeAway") == "away"), None)
        home = next((c for c in comps if c.get("homeAway") == "home"), None)
        if not (away and home):
            continue
        an = away.get("team", {}).get("displayName", "")
        hn = home.get("team", {}).get("displayName", "")
        if not (an and hn):
            continue
        for c in (away, home):
            t = c.get("team", {})
            nm, logo = t.get("displayName", ""), t.get("logo")
            if nm and logo:
                ESPN_TEAM_LOGOS[nm.upper().strip()] = logo
        games.append({
            "away": an, "home": hn,
            "time": utc_str_to_cdmx((ev.get("date", "") or "").replace("Z", "")),
            "sport_key": sport_key, "liga": liga_label,
        })
    if games:
        print(f"  ESPN sched ({slug}) {today} → {len(games)} partidos de hoy")
    return games

def _espn_blurb_for(away_g: str, home_g: str, espn_map: dict):
    """Busca el blurb de ESPN que corresponde a un partido de odds (match por nombre)."""
    for (ea, eh), blurb in (espn_map or {}).items():
        if _team_match(away_g, ea) and _team_match(home_g, eh):
            return blurb
        if _team_match(away_g, eh) and _team_match(home_g, ea):
            return blurb
    return None

# ── 3. Construir contexto ─────────────────────────────────────────────────────
def _totals_lines(game: dict) -> list:
    """Líneas de total (Over/Under) de consenso —mediana entre casas— para el contexto.
    Muestra el/los punto(s) del mercado principal + las alternas de fútbol 1.5/2.5/3.5."""
    main_pts, alt_pts = set(), set()
    for bm in game.get("bookmakers", []):
        for mkt in bm.get("markets", []):
            k = mkt.get("key")
            if k not in ("totals", "alternate_totals"):
                continue
            for o in mkt.get("outcomes", []):
                if o.get("point") is None:
                    continue
                pv = round(float(o["point"]), 1)
                (main_pts if k == "totals" else alt_pts).add(pv)
    points = sorted(main_pts | {p for p in alt_pts if p in (1.5, 2.5, 3.5)})
    out = []
    for p in points:
        ov, un = [], []
        for bm in game.get("bookmakers", []):
            for mkt in bm.get("markets", []):
                if mkt.get("key") not in ("totals", "alternate_totals"):
                    continue
                for o in mkt.get("outcomes", []):
                    pt = o.get("point")
                    if pt is None or abs(float(pt) - p) >= 0.25 or not o.get("price"):
                        continue
                    if o.get("name") == "Over":
                        ov.append(o["price"])
                    elif o.get("name") == "Under":
                        un.append(o["price"])
        mo, mu = _median(ov), _median(un)
        if mo or mu:
            out.append(f"  Total {p:g}: Over {round(mo,2) if mo else '—'} / "
                       f"Under {round(mu,2) if mu else '—'}")
    return out

def build_context(mlb_sched, team_groups, espn_stats):
    """Contexto con los partidos REALES de hoy (ESPN + MLB). SIN cuotas: Claude las
    busca en la web. team_groups = [(label, sport_key, [games])]."""
    espn_stats = espn_stats or {}
    lines = [
        f"FECHA HOY: {today} ({fecha_display})",
        f"ZONA HORARIA: CDMX / {TZ_LABEL} (UTC{CDMX_OFFSET:+d})",
        "",
        "⚠️  Estos son los UNICOS partidos reales de hoy (fuente: ESPN + MLB oficial).",
        "NO inventes partidos ni equipos fuera de esta lista. Para CADA pick debes BUSCAR",
        "EN LA WEB la cuota actual con la herramienta de búsqueda.",
        "",
        "=== MLB — PARTIDOS DE HOY (pitchers probables oficiales) ===",
    ]
    if mlb_sched:
        for g in mlb_sched:
            lines.append(f"• {g['away']} @ {g['home']}  ({g['time']} CDMX)")
            lines.append(f"  Pitchers: {g['awp']} (visitante) vs {g['hwp']} (local)")
            blurb = _espn_blurb_for(g['away'], g['home'], espn_stats.get('baseball_mlb'))
            if blurb:
                lines.append(f"  ESTADISTICAS REALES (ESPN): {blurb}")
    else:
        lines.append("  Sin partidos MLB hoy.")
    for label, sport_key, games in team_groups:
        lines.append(f"\n=== {label} — PARTIDOS DE HOY ===")
        if not games:
            lines.append("  Sin juegos hoy.")
            continue
        for g in games:
            lines.append(f"• {g['away']} @ {g['home']}  ({g['time']} CDMX)")
            blurb = _espn_blurb_for(g['away'], g['home'], espn_stats.get(sport_key))
            if blurb:
                lines.append(f"  ESTADISTICAS REALES (ESPN): {blurb}")
    lines.append("\n=== TENIS ATP/WTA — PARTIDOS DE HOY ===")
    lines.append(f"  (ESPN no lo lista limpio.) BUSCA EN LA WEB los partidos ATP/WTA de HOY "
                 f"({today}) de torneos en curso y sus cuotas. Maximo 2 picks de tenis.")
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

def _extract_total_point(pick_txt: str) -> float | None:
    """Cantidad de un pick de totales (ej: 2.5 de 'OVER 2.5 GOLES', 10.5 de 'OVER 10.5 CARRERAS')."""
    m = re.search(r'(?:OVER|UNDER|M[ÁA]S\s+DE|MENOS\s+DE|TOTAL)\s+(\d+(?:\.\d+)?)', pick_txt, re.IGNORECASE)
    if m:
        return float(m.group(1))
    m = re.search(r'(\d+\.\d+)', pick_txt)   # fallback: primer número x.x
    return float(m.group(1)) if m else None

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

def _logo_for(team_name: str):
    """URL del logo (ESPN) para un nombre de equipo de odds; None si no hay match."""
    if not team_name:
        return None
    key = team_name.upper().strip()
    if key in ESPN_TEAM_LOGOS:
        return ESPN_TEAM_LOGOS[key]
    for espn_name, url in ESPN_TEAM_LOGOS.items():
        if url and _team_match(team_name, espn_name):
            return url
    return None

_TENNIS_PHOTO_CACHE: dict = {}
def _tennis_photo(player_name: str):
    """URL de la foto (headshot ESPN) de un tenista; None si ESPN no tiene foto.
    Usa el buscador público de ESPN; el campo image.default solo viene cuando el
    headshot existe de verdad (los jugadores sin foto no lo traen)."""
    if not player_name:
        return None
    key = player_name.upper().strip()
    if key in _TENNIS_PHOTO_CACHE:
        return _TENNIS_PHOTO_CACHE[key]
    photo = None
    try:
        r = requests.get("https://site.api.espn.com/apis/search/v2",
                         params={"query": player_name, "limit": 8}, timeout=10)
        data = r.json() if r.ok else {}
        for rt in data.get("results", []):
            if rt.get("type") != "player":
                continue
            for c in rt.get("contents", []):
                if str(c.get("sport", "")).lower() != "tennis":
                    continue
                if not _team_match(player_name, c.get("displayName", "")):
                    continue
                img = c.get("image")
                photo = img.get("default") if isinstance(img, dict) else (img if isinstance(img, str) else None)
                if photo:
                    break
            if photo:
                break
    except Exception as e:
        print(f"  ⚠ foto tenis {player_name}: {e}")
    _TENNIS_PHOTO_CACHE[key] = photo
    return photo

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
    if key in ("totals", "alternate_totals"):
        want = _extract_total_point(pick_txt)   # ej. 1.5 / 2.5 / 3.5
        def _pt_ok(o):
            pt = o.get("point")
            if want is None:
                return True
            return pt is not None and abs(float(pt) - want) < 0.25
        if any(k in pick_txt for k in ("OVER", "MAS DE", "MÁS DE")):
            return next((o["price"] for o in outs if o["name"].upper() == "OVER" and _pt_ok(o)), None)
        if any(k in pick_txt for k in ("UNDER", "MENOS DE")):
            return next((o["price"] for o in outs if o["name"].upper() == "UNDER" and _pt_ok(o)), None)
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

# ── Anclaje de la probabilidad al mercado ────────────────────────────────────
MAX_ADJ         = 5.0   # pts máx que la IA puede desviarse del mercado SHARP (Pinnacle)
MAX_ADJ_NOSHARP = 6.0   # pts máx que la IA puede desviarse del consenso (sin Pinnacle)
MAX_EDGE        = 7.0   # pts máx que prob_propia puede superar/bajar la cuota que TOMAS
MIN_EV          = 0.0   # EV mínimo (en %) para conservar un pick

def _clamp(v, lo, hi):
    return max(lo, min(hi, v))

def _find_game_for(pick: dict, odds_list: list) -> dict | None:
    """Primer juego de odds_list cuyo local/visitante matchea el matchup del pick."""
    parsed = _parse_matchup(pick.get("matchup", ""))
    if not parsed:
        return None
    away_raw, home_raw = parsed
    for game in odds_list:
        if _team_match(away_raw, game.get("away_team", "")) and \
           _team_match(home_raw, game.get("home_team", "")):
            return game
    return None

def fix_cuotas_reales(picks_data: dict, all_odds: dict) -> dict:
    """
    Post-procesa los picks con el marco de la ficha (prob casino / prob propia / cuota mín):
      1. cuota = mediana de consenso entre casas (robusta a outliers).
      2. prob_casino (prob_implicita) = 100 / cuota.
      3. prob_propia = ANCLADA al mercado: la estimación de la IA solo puede desviarse
         ±MAX_ADJ pts de la prob justa sin-vig de Pinnacle (o del consenso si no hay
         Pinnacle). Así la IA no puede "inventar" favoritos que el mercado no ve.
      4. cuota_minima = 100 / prob_propia (precio que tu casa debe superar).
      5. EV = (prob_propia/100 × cuota − 1) × 100  →  se conserva si EV > MIN_EV.
    Marca p["sport_key"] con el sport_key real del juego (para check_results.py).
    """
    liga_map = {
        "mlb":    ("baseball_mlb", all_odds.get("baseball_mlb", [])),
        "nba":    ("basketball_nba", all_odds.get("basketball_nba", [])),
        "nfl":    ("americanfootball_nfl", all_odds.get("americanfootball_nfl", [])),
        "tennis": ("tennis", all_odds.get("tennis", [])),
    }
    for key, label in FUTBOL_LEAGUES.items():
        liga_map[label.lower()] = (key, all_odds.get(key, []))

    good_picks = []
    moved_to_na = []

    for p in picks_data.get("picks", []):
        liga_key = (p.get("liga") or "").lower()
        odds_list, fallback_sk = [], ""
        for needle, (sport_key, odds) in liga_map.items():
            if needle in liga_key:
                fallback_sk = sport_key
                odds_list = odds
                break

        # sport_key robusto: el del juego real (Odds API lo trae); si no, heurística
        game = _find_game_for(p, odds_list)
        p["sport_key"] = (game or {}).get("sport_key") or fallback_sk

        # 1. Cuota de consenso (mediana). Si no hay, usa la de Claude.
        cuota = _consensus_cuota(p, odds_list)
        if cuota:
            p["cuota_bet365"]     = cuota
            p["cuota_verificada"] = True
        else:
            p["cuota_verificada"] = False
            cuota = p.get("cuota_bet365") or 0
        # Totales: exigir cuota REAL a la línea exacta (Over/Under X.X). Si no la hay,
        # se descarta — evita mostrar un "Over 1.5" con la cuota de otra línea (2.5).
        tipo_l = (p.get("tipo") or "").lower()
        pick_u = (p.get("pick") or "").upper()
        is_total = ("total" in tipo_l) or any(k in pick_u for k in
                    ("OVER", "UNDER", "MÁS DE", "MAS DE", "MENOS DE"))
        if is_total and not p.get("cuota_verificada"):
            moved_to_na.append({"matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
                                "razon": "Sin cuota real para esa línea de total (Over/Under) en el mercado."})
            continue
        if not cuota:
            moved_to_na.append({"matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
                                "razon": "Sin cuota disponible."})
            continue

        prob_casino = round(100 / cuota, 1)
        p["prob_implicita"] = prob_casino
        ia_est = p.get("prob_propia", 50) or 50
        p["prob_ia_cruda"] = ia_est                    # lo que estimó la IA (para auditar)

        # 3. Anclaje al mercado
        fair_p, _ = pinnacle_fair(p, odds_list)
        if fair_p is not None:
            base = fair_p * 100
            prob_propia = base + _clamp(ia_est - base, -MAX_ADJ, MAX_ADJ)
            p["prob_justa"]  = round(base, 1)
            p["fair_source"] = "pinnacle"
        else:
            prob_propia = _clamp(ia_est, prob_casino - MAX_ADJ_NOSHARP, prob_casino + MAX_ADJ_NOSHARP)
            p["prob_justa"]  = None
            p["fair_source"] = None
        # Tope duro anti-EV-inflado: la ventaja sobre la cuota que REALMENTE tomas
        # (el consenso) no puede exceder MAX_EDGE pts. Si Pinnacle y el consenso
        # discrepan mucho (línea vieja/errónea de una casa), esto evita EV irreales.
        prob_propia = _clamp(prob_propia, prob_casino - MAX_EDGE, prob_casino + MAX_EDGE)
        prob_propia = round(prob_propia, 1)
        p["prob_propia"]  = prob_propia
        p["cuota_minima"] = round(100 / prob_propia, 2)
        ev = round((prob_propia / 100 * cuota - 1) * 100, 1)
        p["ev_pct"] = ev

        anc = "sharp" if fair_p is not None else "consenso"
        if ev > MIN_EV:
            print(f"  ✅ [{p['matchup']}] {p['pick']}  cuota {cuota}  EV {ev}%  "
                  f"(casino {prob_casino}% → propia {prob_propia}% ancla:{anc}, IA cruda {ia_est}%)")
            good_picks.append(p)
        else:
            print(f"  ⚠️ EV≤{MIN_EV} [{p['matchup']}] {p['pick']}  cuota {cuota}  EV {ev}% "
                  f"(propia {prob_propia}% ancla:{anc})")
            moved_to_na.append({
                "matchup": p["matchup"], "liga": p.get("liga", ""),
                "razon": (f"Sin valor: EV {ev}%. Anclada al mercado, la prob real ({prob_propia}%) "
                          f"no supera lo que implica la cuota ({prob_casino}%)."),
            })

    picks_data["picks"] = good_picks
    picks_data["no_apostar"] = picks_data.get("no_apostar", []) + moved_to_na
    picks_data["nota_lineas"] = (
        f"Cuota de consenso (mediana entre casas). Probabilidad ANCLADA al mercado sharp "
        f"(Pinnacle) — la IA no inventa favoritos. La cuota mínima es el precio que tu casa "
        f"(PlayDoIt/Winpot) debe superar. {len(good_picks)} picks con EV+. Horario {TZ_LABEL} CDMX."
    )
    return picks_data

# ── 4c. Auto-aprendizaje: calibración desde resultados históricos ─────────────
import glob as _glob

def _sport_label(sk, liga=""):
    sk = (sk or "").lower()
    if sk.startswith("baseball"):         return "MLB"
    if sk.startswith("basketball"):       return "NBA"
    if sk.startswith("americanfootball"): return "NFL"
    if sk.startswith("tennis"):           return "Tennis"
    if sk.startswith("soccer"):           return "Fútbol"
    l = (liga or "").lower()
    if "mlb" in l:    return "MLB"
    if "nba" in l:    return "NBA"
    if "nfl" in l:    return "NFL"
    if "tennis" in l or "tenis" in l: return "Tennis"
    if any(k in l for k in ("liga", "premier", "futbol", "fútbol")): return "Fútbol"
    return liga or "Otros"

def _stake_u(s):
    try:
        return float(re.sub(r'[^\d.]', '', s or "0"))
    except Exception:
        return 0.0

def _tipo_norm(t):
    t = (t or "").lower()
    if "moneyline" in t or t == "ml": return "Moneyline"
    if "total" in t:                  return "Total"
    if any(k in t for k in ("spread", "run line", "handicap")): return "Spread"
    if "prop" in t:                   return "Prop"
    return (t.title() or "Otro")

def _agg(rows):
    """Agrega n, W/L, %acierto y ROI (u) de una lista de resultados."""
    w = l = pu = 0
    staked = profit = 0.0
    for r in rows:
        res = r.get("resultado")
        st, cu = _stake_u(r.get("stake")), (r.get("cuota") or 0)
        if res == "win":    w += 1;  profit += st * (cu - 1); staked += st
        elif res == "loss": l += 1;  profit -= st;            staked += st
        elif res == "push": pu += 1;                          staked += st
    return {
        "n": w + l + pu, "wins": w, "losses": l, "pushes": pu,
        "win_pct": round(w / (w + l) * 100, 1) if (w + l) else 0.0,
        "roi": round(profit / staked * 100, 1) if staked > 0 else 0.0,
    }

def build_learning_report(directory="."):
    """
    Lee todos los results-*.json, mide calibración y rendimiento por deporte/tipo/
    estrellas, y devuelve (summary_dict, texto_lecciones). El texto se inyecta al
    prompt para que la IA corrija su criterio (auto-aprendizaje por retroalimentación).
    """
    rows = []
    for f in sorted(_glob.glob(os.path.join(directory, "results-*.json"))):
        try:
            with open(f, encoding="utf-8-sig") as fh:
                d = json.load(fh)
        except Exception:
            continue
        for r in d.get("picks", []):
            if r.get("resultado") in ("win", "loss", "push"):
                r = dict(r)
                r["_sport"] = _sport_label(r.get("sport_key"), r.get("liga"))
                rows.append(r)

    summary = {"generado": today, "n_resueltos": len(rows)}
    if len(rows) < 10:
        summary["estado"] = "cold_start"
        lecciones = ("APRENDIZAJE: aún recopilando datos (pocos picks resueltos). "
                     "Mantente conservador y equilibrado entre deportes.")
        summary["lecciones"] = lecciones
        return summary, lecciones

    decided = [r for r in rows if r.get("resultado") in ("win", "loss")]
    by_sport = {sp: _agg([r for r in rows if r["_sport"] == sp])
                for sp in sorted(set(r["_sport"] for r in rows))}
    by_tipo  = {tp: _agg([r for r in rows if _tipo_norm(r.get("tipo")) == tp])
                for tp in sorted(set(_tipo_norm(r.get("tipo")) for r in rows))}
    by_star  = {}
    for s in sorted(set(r.get("estrellas") for r in rows if r.get("estrellas"))):
        sub = [r for r in decided if r.get("estrellas") == s]
        if sub:
            by_star[str(s)] = {"n": len(sub),
                               "win_pct": round(sum(1 for r in sub if r["resultado"] == "win") / len(sub) * 100, 1)}
    # Calibración: prob prometida vs %acierto real por bucket
    calib = []
    for lo, hi, lbl in [(0, 52, "≤52"), (52, 60, "52-60"), (60, 68, "60-68"), (68, 200, "68+")]:
        sub = [r for r in decided if r.get("prob_propia") is not None and lo <= r["prob_propia"] < hi]
        if sub:
            claimed = round(sum(r["prob_propia"] for r in sub) / len(sub), 1)
            actual  = round(sum(1 for r in sub if r["resultado"] == "win") / len(sub) * 100, 1)
            calib.append({"rango": lbl, "n": len(sub), "prometido": claimed,
                          "real": actual, "sesgo": round(claimed - actual, 1)})

    summary.update({"estado": "activo", "global": _agg(rows), "por_deporte": by_sport,
                    "por_tipo": by_tipo, "por_estrellas": by_star, "calibracion": calib})

    g = summary["global"]
    L = [f"Global: {g['wins']}-{g['losses']} ({g['win_pct']}% acierto), ROI {g['roi']:+}%/u en {g['n']} picks."]
    sp_rank = [(sp, a) for sp, a in by_sport.items() if a["n"] >= 5]
    if sp_rank:
        best  = max(sp_rank, key=lambda x: x[1]["roi"])
        worst = min(sp_rank, key=lambda x: x[1]["roi"])
        L.append(f"Mejor deporte: {best[0]} ({best[1]['win_pct']}%, ROI {best[1]['roi']:+}%).")
        if worst[0] != best[0] and worst[1]["roi"] < 0:
            L.append(f"Peor deporte: {worst[0]} ({worst[1]['win_pct']}%, ROI {worst[1]['roi']:+}%) — sé más selectivo ahí.")
    tp_rank = [(tp, a) for tp, a in by_tipo.items() if a["n"] >= 5 and a["roi"] < 0]
    if tp_rank:
        worst_t = min(tp_rank, key=lambda x: x[1]["roi"])
        L.append(f"Tipo flojo: {worst_t[0]} ({worst_t[1]['win_pct']}%, ROI {worst_t[1]['roi']:+}%) — evita salvo ventaja clara.")
    with_prob = [r for r in decided if r.get("prob_propia") is not None]
    if with_prob:
        claimed = sum(r["prob_propia"] for r in with_prob) / len(with_prob)
        actual  = sum(1 for r in with_prob if r["resultado"] == "win") / len(with_prob) * 100
        bias = claimed - actual
        if bias >= 4:
            L.append(f"Calibración: prometiste {round(claimed,1)}% promedio pero ganó {round(actual,1)}% — sobreestimas ~{round(bias,1)} pts, baja tus probabilidades.")
        elif bias <= -4:
            L.append(f"Calibración: subestimas ~{round(-bias,1)} pts — puedes ser un poco más agresivo.")
    if by_star.get("5") and by_star.get("3"):
        ok = by_star["5"]["win_pct"] >= by_star["3"]["win_pct"]
        L.append(f"5★ ganan {by_star['5']['win_pct']}% vs 3★ {by_star['3']['win_pct']}%" +
                 (" — la confianza discrimina bien." if ok else " — tus 5★ NO ganan más; modera la confianza."))

    lecciones = "APRENDIZAJE DE PICKS PASADOS (úsalo para calibrar tu criterio):\n- " + "\n- ".join(L)
    summary["lecciones"] = lecciones
    return summary, lecciones

# ── 5. Claude API ─────────────────────────────────────────────────────────────
PROMPT_SYSTEM = f"""Eres un tipster profesional y analista cuantitativo de apuestas deportivas.
Hoy es {today} — horario CDMX ({TZ_LABEL}, UTC{CDMX_OFFSET:+d}).

Tienes una herramienta de BUSQUEDA WEB. Usala para obtener las CUOTAS ACTUALES de los
partidos reales listados en el contexto. Fuentes utiles: oddspedia, oddsportal, flashscore,
actionnetwork, ESPN BET, betano, bet365, 1xbet. Cuotas en formato decimal europeo.

REGLAS ABSOLUTAS:
1. SOLO genera picks de partidos que aparezcan en el contexto (fuente ESPN/MLB), mas los
   partidos de tenis ATP/WTA de HOY que encuentres por busqueda web. NO inventes partidos.
2. Todos los horarios en hora CDMX ({TZ_LABEL}).
3. CUOTAS: NO inventes cuotas. Cada cuota (cuota_bet365) debe provenir de una BUSQUEDA WEB
   real y reciente. Si no encuentras una cuota fiable para un partido/mercado, NO lo incluyas
   como pick. En 'razonamiento' menciona brevemente la casa/fuente de la cuota.
4. DATOS DE HOY: pitchers, records, forma y stats del contexto son de HOY. Usalos tal cual;
   no cites datos de enfrentamientos anteriores ni de tu memoria.
5. Mercados validos: Moneyline (1X2/ML), Totales (Over/Under con la linea EXACTA), Spread/
   Handicap, y props de jugador si hallas cuota real. En futbol el empate es resultado propio.

METODOLOGIA (por cada pick):
0. PROCESO DE ANALISIS (razona a fondo ANTES de decidir): por cada partido candidato:
   (a) revisa la cuota/linea REAL del mercado que buscaste; (b) forma tu propia estimacion con
   las ESTADISTICAS REALES (ESPN) del contexto; (c) compara — donde crees que el mercado esta
   ligeramente mal y POR QUE (dato estadistico concreto); (d) solo si puedes articular una
   ventaja PEQUENA, concreta y defendible con datos, conviertelo en pick. Descarta corazonadas
   sin dato. Cruza varias casas/fuentes para la cuota. Calidad antes que cantidad.
1. prob_implicita = 100 / cuota_bet365 (con la cuota real que buscaste).
2. prob_propia = tu estimacion basada en las ESTADISTICAS REALES (ESPN) del contexto y el
   consenso del mercado que viste al buscar. El 'razonamiento' DEBE citar datos concretos.
3. ANCLAJE AL MERCADO (CRITICO): tu prob_propia NO debe alejarse mas de ~4 puntos de la
   probabilidad implicita de la cuota (100/cuota). El mercado casi siempre tiene razon; tu
   trabajo NO es inventar un favorito sino detectar una ventaja PEQUENA y defendible. En
   beisbol/futbol, aun el mejor equipo rara vez pasa de ~62% en un juego.
4. EV% = (prob_propia/100 x cuota - 1) x 100. Se MUY conservador: un EV realista vive entre
   +2% y +6%. Un EV de +10% casi siempre es una sobreestimacion tuya — bajalo. Prefiere +3%
   real que +12% inflado (el sistema lo va a recortar de todas formas).
5. UN SOLO pick por partido: no combines Moneyline + Total + BTTS del mismo juego; elige el
   de mayor valor. (La correlacion de varios picks del mismo partido infla el riesgo.)
6. DIVERSIFICA: no concentres casi todos los picks en una sola liga/competicion si hay valor
   en otras. Reparte entre las ligas/deportes disponibles.
7. Stake: Kelly fraccional 1/4. Max 0.3u por pick. Total sesion <= 3u.
8. Parlays: 2-3 patas de PARTIDOS DISTINTOS con correlacion positiva; cuota minima 1.20 por pata.
9. ESTRELLAS 1-5 (5 = edge muy claro, usalo poco; puede no haber pick de 5 estrellas).

Responde UNICAMENTE JSON valido, sin markdown, sin texto extra."""

SCHEMA_PICK = {
    "liga":           "MLB | NBA | NFL | Premier League | Liga MX | Tennis ATP | Tennis WTA",
    "matchup":        "AWAY @ HOME",
    "hora":           "H:MM AM/PM CDT (CDMX)",
    "pick":           "descripcion concreta",
    "tipo":           "Moneyline | Total | Run Line | Total Goles | Spread | Prop Jugador",
    "sport_key":      "baseball_mlb | basketball_nba | americanfootball_nfl | soccer_epl | tennis_atp | ...",
    "cuota_bet365":   1.85,
    "prob_implicita": 55.5,
    "prob_propia":    61.0,
    "ev_pct":         5.3,
    "prob_acierto":   61,
    "estrellas":      3,
    "stake":          "0.2u",
    "razonamiento":   "2-3 lineas con datos concretos + casa/fuente de la cuota",
}

def _salvage_json(raw: str):
    """Best-effort: recupera un JSON truncado balanceando llaves/corchetes al final."""
    if not raw:
        return None
    s = raw.rstrip()
    s = re.sub(r',\s*"[^"]*"\s*:?\s*[^,{}\[\]]*$', '', s).rstrip().rstrip(',')
    cands = [s]
    last = s.rfind('}')
    if last != -1:
        cands.append(s[:last + 1])
    for cand in cands:
        if not cand:
            continue
        opens = cand.count('{') - cand.count('}')
        obr   = cand.count('[') - cand.count(']')
        fixed = cand + (']' * max(obr, 0)) + ('}' * max(opens, 0))
        try:
            obj = json.loads(fixed)
            if isinstance(obj, dict):
                return obj
        except Exception:
            continue
    return None

def generate_picks(context: str) -> dict:
    client = anthropic.Anthropic(api_key=ANTHROPIC_KEY)
    schema = {
        "fecha":         today,
        "fecha_display": fecha_display,
        "generado_a":    "automatico-github-actions",
        "nota_lineas":   f"Cuotas de consenso por busqueda web (no verificadas con feed). Horario {TZ_LABEL} CDMX.",
        "bankroll":      {"exposicion_total": "Xu", "max_por_juego": "0.3u", "nota": "Kelly 1/4"},
        "picks":         [SCHEMA_PICK],
        "no_apostar":    [{"matchup": "...", "liga": "...", "razon": "..."}],
        "parlay_sugerido": {
            "patas":       ["Pick A (X.XX)", "Pick B (X.XX)"],
            "cuota_total": 3.50,
            "ev_pct":      5.0,
            "stake":       "0.15u",
            "nota":        "razon de la correlacion",
        },
        "resumen_ejecutivo": [
            {"pick": "...", "tipo": "Moneyline", "liga": "MLB",
             "cuota": 1.85, "ev_pct": 5.3, "stake": "0.2u", "estrellas": 3}
        ],
    }
    user_msg = (
        f"Genera entre 7 y 12 picks para HOY ({today}) — apunta a ~10 si hay valor suficiente.\n"
        f"Prioriza CALIDAD: incluye menos de 7 SOLO si de verdad no hay más apuestas con ventaja real.\n"
        f"PRIORIDAD: futbol primero, luego NBA/NFL, luego tenis. De MLB incluye COMO MAXIMO "
        f"3 picks y de TENIS COMO MAXIMO 2 (solo los de mayor EV).\n"
        f"Usa la BUSQUEDA WEB para obtener la cuota real de cada partido. Solo incluye un pick "
        f"si encontraste una cuota real y reciente; si no, dejalo fuera.\n"
        f"REGLAS DE CALIDAD: (a) MAXIMO 1 pick por partido; (b) DIVERSIFICA ligas/deportes, no "
        f"pongas casi todo en una sola competicion; (c) se CONSERVADOR: EV realista 2-6%, evita "
        f"prob_propia a mas de 4 puntos de la implicita de la cuota.\n\n"
        f"PARTIDOS REALES DE HOY:\n{context}\n\n"
        f"Esquema JSON de salida (responde SOLO el JSON):\n{json.dumps(schema, indent=2, ensure_ascii=False)}"
    )
    MAXTOK = int(os.environ.get("MAX_TOKENS", "16000"))
    def _extract(m):
        parts = [b.text for b in m.content if getattr(b, "type", "") == "text"]
        r = "\n".join(parts).strip()
        mm = re.search(r"```(?:json)?\s*(.*?)```", r, re.S) if "```" in r else None
        if mm:
            r = mm.group(1).strip()
        a, b = r.find("{"), r.rfind("}")
        if a != -1 and b != -1 and b > a:
            r = r[a:b + 1]
        return r
    THINK = int(os.environ.get("THINK_BUDGET", "4000"))  # extended thinking; 0 = desactivado
    if 0 < THINK < 1024:
        THINK = 1024
    def _create(mtok, think_on):
        kw = dict(
            model=MODEL, max_tokens=mtok, system=PROMPT_SYSTEM,
            tools=[{"type": "web_search_20250305", "name": "web_search", "max_uses": WEB_SEARCH_MAX}],
            messages=[{"role": "user", "content": user_msg}],
        )
        if think_on and THINK > 0:
            kw["thinking"] = {"type": "enabled", "budget_tokens": THINK}
        return client.messages.create(**kw)
    data, last_err, mt = None, None, MAXTOK
    think_on = THINK > 0
    for attempt in range(3):
        try:
            msg = _create(mt, think_on)
            sr = getattr(msg, "stop_reason", "?")
            raw = _extract(msg)
            print(f"  🤖 intento {attempt+1} (thinking={'on' if think_on else 'off'}): stop_reason={sr}, json_len={len(raw)}")
            try:
                data = json.loads(raw)
                break
            except Exception as je:
                last_err = je
                print(f"  ⚠ JSON inválido: {str(je)[:140]}")
                salv = _salvage_json(raw)
                if salv is not None and salv.get("picks"):
                    data = salv
                    print(f"  🩹 JSON recuperado por salvage ({len(salv.get('picks', []))} picks)")
                    break
                if sr == "max_tokens":
                    mt = min(mt + 8000, 32000)   # truncó: más presupuesto y reintenta
        except Exception as ae:
            last_err = ae
            print(f"  ⚠ error API/red (intento {attempt+1}): {str(ae)[:180]}")
            if think_on:
                think_on = False   # fallback: reintenta SIN extended thinking (ruta conocida)
                print("  ↩ reintento sin extended thinking")
            else:
                time.sleep(5 * (attempt + 1))
    if data is None:
        raise RuntimeError(f"Claude no devolvió JSON válido tras 3 intentos: {str(last_err)[:200]}")
    for p in data.get("picks", []):
        p.setdefault("cuota_verificada", False)
        p["fair_source"] = "web"
        try:
            c  = float(p.get("cuota_bet365") or 0)
            pp = float(p.get("prob_propia") or 0)
            if c > 1 and p.get("prob_implicita") in (None, ""):
                p["prob_implicita"] = round(100.0 / c, 1)
            if pp > 0 and p.get("cuota_minima") in (None, ""):
                p["cuota_minima"] = round(100.0 / pp, 2)
            if c > 1 and pp > 0 and p.get("ev_pct") in (None, ""):
                p["ev_pct"] = round((pp / 100.0 * c - 1) * 100, 1)
            if not p.get("sport_key"):
                p["sport_key"] = _guess_sport_key(p.get("liga", ""))
        except Exception:
            pass
    return data

# ── 6. Guardar archivos ───────────────────────────────────────────────────────
ANCHOR_MAX   = int(os.environ.get("ANCHOR_MAX", "3"))    # prob_propia no se aleja más de esto de la implícita
PER_LIGA_MAX = int(os.environ.get("PER_LIGA_MAX", "6"))  # máximo de picks por competición

def _norm_match(m: str) -> str:
    m = (m or "").lower().replace(" @ ", " vs ").replace(" vs. ", " vs ")
    toks = sorted(t.strip() for t in re.split(r"\s+vs\s+", m) if t.strip())
    return " vs ".join(toks)

def apply_conservatism(data: dict) -> dict:
    """Post-proceso determinista para calidad/exposición:
    1) Ancla prob_propia a ±ANCHOR_MAX de la implícita de la cuota y recalcula EV.
    2) Máximo 1 pick por partido (el de mayor EV).
    3) Tope de PER_LIGA_MAX picks por competición.
    Los descartados pasan a 'no_apostar'."""
    picks = data.get("picks", [])
    # 1) Ancla al mercado (usa la cuota que Claude buscó como referencia)
    for p in picks:
        try:
            c = float(p.get("cuota_bet365") or 0)
            if c > 1:
                imp = 100.0 / c
                pp  = float(p.get("prob_propia") or imp)
                pp  = max(imp - ANCHOR_MAX, min(imp + ANCHOR_MAX, pp))
                p["prob_implicita"] = round(imp, 1)
                p["prob_propia"]    = round(pp, 1)
                p["prob_acierto"]   = int(round(pp))
                p["ev_pct"]         = round((pp / 100.0 * c - 1) * 100, 1)
                p["cuota_minima"]   = round(100.0 / pp, 2)
        except Exception:
            pass
    # 2) Máximo 1 pick por partido (mayor EV)
    best = {}
    for p in picks:
        k = _norm_match(p.get("matchup", ""))
        if k and (k not in best or (p.get("ev_pct", 0) or 0) > (best[k].get("ev_pct", 0) or 0)):
            best[k] = p
    kept    = list(best.values())
    dropped = [p for p in picks if p not in kept]
    # 3) Tope por liga (conserva los de mayor EV)
    from collections import defaultdict
    byliga = defaultdict(list)
    for p in sorted(kept, key=lambda x: -(x.get("ev_pct", 0) or 0)):
        byliga[p.get("liga", "?")].append(p)
    final = []
    for liga, ps in byliga.items():
        final += ps[:PER_LIGA_MAX]
        dropped += ps[PER_LIGA_MAX:]
    final.sort(key=lambda x: -(x.get("ev_pct", 0) or 0))
    data["picks"] = final
    for p in dropped:
        data.setdefault("no_apostar", []).append({
            "matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
            "razon": "Descartado por diversificación/exposición (1 pick por partido, tope por liga)."})
    return data

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
    print(f"   Fuente: ESPN (calendario+stats) + Claude web_search (cuotas)")
    print("=" * 55)

    # ── Guarda anti-regeneración: si los picks de HOY ya existen (con picks reales),
    # no regenerar. Evita que los crons de respaldo gasten búsquedas/tokens de más.
    # FORCE=1 o TARGET_DATE saltan la guarda.
    _FORCE = os.environ.get("FORCE", "").strip().lower() not in ("", "0", "false", "no")
    if not _FORCE and not _TARGET:
        _fn = f"picks-{today}.json"
        if os.path.exists(_fn):
            try:
                _ex = json.load(open(_fn, encoding="utf-8-sig"))
                if _ex.get("picks") and _ex.get("generado_a") != "error":
                    print(f"  ⏭  {_fn} ya existe con {len(_ex['picks'])} picks — "
                          f"omito regeneración (usa FORCE=1 para forzar).")
                    sys.exit(0)
            except Exception:
                pass

    print(f"\n📅 Calendario de HOY ({today})...")
    mlb_sched = fetch_mlb_schedule()
    print(f"   MLB: {len(mlb_sched)} partidos")

    team_groups = []  # [(label, sport_key, games)]
    for sk in ["basketball_nba", "americanfootball_nfl"] + SOCCER_KEYS:
        label = LIGA_LABELS.get(sk, sk)
        games = fetch_espn_schedule(sk, label)
        if games:
            team_groups.append((label, sk, games))
    print(f"   Otras ligas con partidos hoy: {len(team_groups)}")

    print("\n📊 Estadistica ESPN (records, forma, pitchers) + logos...")
    active_keys = ["baseball_mlb"] + [sk for _, sk, _ in team_groups]
    espn_stats = {sk: fetch_espn_stats(sk) for sk in active_keys}

    print("\n🧠 Reporte de auto-aprendizaje (calibracion historica)...")
    learn_summary, lecciones = build_learning_report(".")
    print("  " + lecciones.replace("\n", "\n  "))
    with open("learning-summary.json", "w", encoding="utf-8") as f:
        json.dump(learn_summary, f, ensure_ascii=False, indent=2)

    context = build_context(mlb_sched, team_groups, espn_stats)
    context = lecciones + "\n\n" + context
    print("\n--- CONTEXTO (primeras 1500 chars) ---")
    print(context[:1500])
    print("...\n")

    print("🤖 Llamando Claude API con busqueda web (objetivo: 8-10 picks)...")
    try:
        picks_data = generate_picks(context)
        picks_data = apply_conservatism(picks_data)  # ancla EV + 1/partido + tope por liga
        n = len(picks_data.get("picks", []))
        print(f"\n✅ {n} picks (tras ancla de EV y diversificación):")
        for p in picks_data.get("picks", []):
            print(f"   ★{p.get('estrellas', 1)} {p.get('matchup')} — {p.get('pick')}  "
                  f"cuota:{p.get('cuota_bet365')}  EV+{p.get('ev_pct')}%")

        # MLB maximo 3 (mayor EV)
        MLB_MAX = 3
        mlb_picks = [p for p in picks_data.get("picks", [])
                     if str(p.get("sport_key", "")).startswith("baseball")]
        if len(mlb_picks) > MLB_MAX:
            keep_ids = {id(p) for p in sorted(mlb_picks, key=lambda x: x.get("ev_pct", 0), reverse=True)[:MLB_MAX]}
            dropped = [p for p in mlb_picks if id(p) not in keep_ids]
            picks_data["picks"] = [p for p in picks_data["picks"]
                                   if not str(p.get("sport_key", "")).startswith("baseball") or id(p) in keep_ids]
            for p in dropped:
                picks_data.setdefault("no_apostar", []).append({
                    "matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
                    "razon": f"Solo top-{MLB_MAX} MLB por EV (fuera, EV {p.get('ev_pct')}%)."})
            print(f"  ⚾ MLB recortado a {MLB_MAX}")

        # Tenis maximo 2
        TENNIS_MAX = 2
        ten_picks = [p for p in picks_data.get("picks", [])
                     if str(p.get("sport_key", "")).startswith("tennis")]
        if len(ten_picks) > TENNIS_MAX:
            keep_ids = {id(p) for p in sorted(ten_picks, key=lambda x: x.get("ev_pct", 0), reverse=True)[:TENNIS_MAX]}
            dropped = [p for p in ten_picks if id(p) not in keep_ids]
            picks_data["picks"] = [p for p in picks_data["picks"]
                                   if not str(p.get("sport_key", "")).startswith("tennis") or id(p) in keep_ids]
            for p in dropped:
                picks_data.setdefault("no_apostar", []).append({
                    "matchup": p.get("matchup", ""), "liga": p.get("liga", ""),
                    "razon": f"Maximo {TENNIS_MAX} picks de tenis por dia (fuera, EV {p.get('ev_pct')}%)."})
            print(f"  🎾 Tenis recortado a {TENNIS_MAX}")

        # Imagenes ESPN (logos futbol / fotos tenis)
        n_img = 0
        for p in picks_data.get("picks", []):
            sk = str(p.get("sport_key", ""))
            mm = _parse_matchup(p.get("matchup", ""))
            if not mm:
                continue
            if sk.startswith("soccer"):
                la, lh = _logo_for(mm[0]), _logo_for(mm[1])
                if la:
                    p["logo_away"] = la; n_img += 1
                if lh:
                    p["logo_home"] = lh; n_img += 1
            elif sk.startswith("tennis"):
                pa, ph = _tennis_photo(mm[0]), _tennis_photo(mm[1])
                if pa:
                    p["photo_away"] = pa; n_img += 1
                if ph:
                    p["photo_home"] = ph; n_img += 1
        if n_img:
            print(f"  🖼  {n_img} imagenes ESPN anadidas")

        # Que TODO partido real de hoy aparezca (pick o No Apostar)
        def _ya_listado(ga, gh):
            for lst in (picks_data.get("picks", []), picks_data.get("no_apostar", [])):
                for it in lst:
                    mm = _parse_matchup(it.get("matchup", ""))
                    if mm and _team_match(mm[0], ga) and _team_match(mm[1], gh):
                        return True
            return False
        all_real = [{"away": g["away"], "home": g["home"], "liga": "MLB"} for g in mlb_sched]
        for label, sk, games in team_groups:
            for g in games:
                all_real.append({"away": g["away"], "home": g["home"], "liga": label})
        n_na = 0
        for g in all_real:
            ga, gh = g["away"], g["home"]
            if not (ga and gh) or _ya_listado(ga, gh):
                continue
            picks_data.setdefault("no_apostar", []).append({
                "matchup": f"{ga} @ {gh}", "liga": g["liga"],
                "razon": "Sin ventaja (EV+) clara hoy o sin cuota fiable en la busqueda."})
            n_na += 1
        if n_na:
            print(f"  📋 {n_na} partidos sin pick anadidos a 'No Apostar'")

        n2 = len(picks_data.get("picks", []))
        if n2 == 0:
            print("\n⚠️  0 picks — NO se sobreescriben archivos existentes.")
        else:
            print("\n💾 Guardando archivos...")
            save_all(picks_data)
            print("\n🎉 Listo — GitHub Actions hara el push automatico.")

    except Exception as e:
        import traceback
        print(f"\n❌ Error: {e}")
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
