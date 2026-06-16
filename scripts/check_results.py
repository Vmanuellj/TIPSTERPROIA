#!/usr/bin/env python3
"""
TIPSTER PRO IA — Verificador automático de resultados
Corre justo antes de generate_picks.py para evaluar los picks del día anterior.
Escribe results-YYYY-MM-DD.json en la raíz del repo.
"""
import json, os, re, requests
from datetime import datetime, timezone, timedelta

ODDS_KEY = os.environ.get("ODDS_API_KEY", "")

# Zona CDMX
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
CT = timezone(timedelta(hours=CDMX_OFFSET))
dt_now = datetime.now(CT)

# Fecha de ayer (los picks que queremos verificar)
yesterday = (dt_now - timedelta(days=1)).strftime("%Y-%m-%d")

# ── Matching de equipos ───────────────────────────────────────────────────────
# Palabras de ciudad que NO identifican un equipo por sí solas
CITY_WORDS = {
    "LOS","ANGELES","NEW","YORK","SAN","SAN FRANCISCO","DIEGO","JOSE",
    "KANSAS","CITY","RED","WHITE","BLUE","FC","CF","DE","THE","UNITED",
    "TAMPA","BAY","GOLDEN","STATE","NEW","ORLEANS","CITY"
}

def _significant_words(name: str) -> list:
    """Palabras significativas del nombre (quita artículos y palabras de ciudad comunes)."""
    return [w for w in re.split(r'\W+', name.upper()) if len(w) > 2 and w not in CITY_WORDS]

def _team_match(a: str, b: str) -> bool:
    a, b = a.upper().strip(), b.upper().strip()
    if a == b:
        return True
    # Subcadena exacta (ej: "SPAIN" en "SPAIN NATIONAL")
    if a in b or b in a:
        return True
    wa = [w for w in re.split(r'\W+', a) if len(w) > 2]
    wb = [w for w in re.split(r'\W+', b) if len(w) > 2]
    # El ÚLTIMO token (apodo del equipo) debe coincidir
    # Ej: "Dodgers" != "Angels" aunque compartan "Los Angeles"
    if wa and wb and wa[-1] == wb[-1]:
        return True
    # Palabras significativas (sin ciudad): al menos 1 debe coincidir
    sa = set(_significant_words(a))
    sb = set(_significant_words(b))
    if sa and sb and len(sa & sb) >= 1:
        return True
    return False

# ── Fetch scores de Odds API ──────────────────────────────────────────────────
def fetch_scores(sport_key: str) -> list:
    if not ODDS_KEY:
        return []
    try:
        r = requests.get(
            f"https://api.the-odds-api.com/v4/sports/{sport_key}/scores/",
            params={"apiKey": ODDS_KEY, "daysFrom": 2},
            timeout=15,
        )
        if not r.ok:
            print(f"  ⚠ scores API {sport_key}: {r.status_code}")
            return []
        games = r.json()
        # Solo juegos completados
        done = [g for g in games if g.get("completed")]
        print(f"  Scores {sport_key}: {len(done)} completados de {len(games)}")
        return done
    except Exception as e:
        print(f"  ⚠ scores {sport_key}: {e}")
        return []

def get_sport_key(liga: str) -> str:
    liga = liga.upper()
    if "MLB" in liga:
        return "baseball_mlb"
    if "NBA" in liga:
        return "basketball_nba"
    if "NFL" in liga:
        return "americanfootball_nfl"
    if "WORLD CUP" in liga or "COPA" in liga or "FIFA" in liga:
        return "soccer_fifa_world_cup"
    if "FUTBOL" in liga or "FÚTBOL" in liga or "SOCCER" in liga:
        return "soccer_epl"
    return "baseball_mlb"

# ── Buscar el partido en los scores ──────────────────────────────────────────
def find_game(pick: dict, scores: list) -> dict | None:
    matchup = pick.get("matchup", "")
    if " @ " in matchup:
        away_raw, home_raw = matchup.split(" @ ", 1)
    elif " VS " in matchup.upper():
        parts = re.split(r'\s+vs\s+', matchup, flags=re.IGNORECASE)
        away_raw, home_raw = parts[0].strip(), parts[1].strip()
    else:
        return None

    for g in scores:
        api_away = g.get("away_team", "")
        api_home = g.get("home_team", "")
        if _team_match(away_raw, api_away) and _team_match(home_raw, api_home):
            return g
        if _team_match(away_raw, api_home) and _team_match(home_raw, api_away):
            return g
    return None

def get_score(game: dict, team: str) -> int | None:
    for s in game.get("scores") or []:
        if _team_match(s.get("name", ""), team):
            try:
                return int(s.get("score", 0))
            except Exception:
                return None
    return None

# ── Evaluar resultado de un pick ─────────────────────────────────────────────
def evaluate_pick(pick: dict, game: dict) -> str:
    """Retorna 'win', 'loss', 'push', o 'pending'."""
    tipo     = (pick.get("tipo") or "").lower()
    pick_txt = (pick.get("pick") or "").upper()
    matchup  = pick.get("matchup", "")
    liga     = (pick.get("liga") or "").lower()

    if " @ " in matchup:
        away_raw, home_raw = matchup.split(" @ ", 1)
    elif " VS " in matchup.upper():
        parts = re.split(r'\s+vs\s+', matchup, flags=re.IGNORECASE)
        away_raw, home_raw = parts[0].strip(), parts[1].strip()
    else:
        return "pending"

    away_score = get_score(game, away_raw)
    home_score = get_score(game, home_raw)

    if away_score is None or home_score is None:
        return "pending"

    total = away_score + home_score

    # ── Moneyline / 1X2 ──────────────────────────────────────────────────────
    if "moneyline" in tipo or "1x2" in tipo or tipo == "ml":
        is_soccer = any(k in liga.lower() for k in ("soccer","futbol","fútbol","copa","world cup","fifa","football"))
        if "DRAW" in pick_txt or "EMPATE" in pick_txt:
            return "win" if away_score == home_score else "loss"
        if any(w in pick_txt for w in [w.upper() for w in re.split(r'\W+', away_raw) if len(w) > 2]):
            # Pick visitante
            if away_score > home_score:
                return "win"
            elif away_score == home_score:
                # En soccer el empate es LOSS para un pick de equipo ganador
                return "loss" if is_soccer else "push"
            else:
                return "loss"
        else:
            # Pick local
            if home_score > away_score:
                return "win"
            elif home_score == away_score:
                return "loss" if is_soccer else "push"
            else:
                return "loss"

    # ── Total (Over/Under) ───────────────────────────────────────────────────
    if "total" in tipo or "over" in pick_txt or "under" in pick_txt:
        m = re.search(r'(\d+\.?\d*)', pick_txt)
        if not m:
            return "pending"
        line = float(m.group(1))
        if "OVER" in pick_txt:
            if total > line:
                return "win"
            elif total == line:
                return "push"
            else:
                return "loss"
        else:  # UNDER
            if total < line:
                return "win"
            elif total == line:
                return "push"
            else:
                return "loss"

    # ── BTTS (Ambos Anotan) ──────────────────────────────────────────────────
    if "btts" in tipo or "ambos" in pick_txt or "both" in pick_txt:
        both_scored = away_score > 0 and home_score > 0
        if "NO" in pick_txt and "SI" not in pick_txt and "YES" not in pick_txt:
            return "win" if not both_scored else "loss"
        else:
            return "win" if both_scored else "loss"

    # ── Spread / Handicap ────────────────────────────────────────────────────
    if "spread" in tipo or "handicap" in tipo or "run line" in tipo:
        m = re.search(r'([+-]?\d+\.?\d*)', pick_txt)
        if not m:
            return "pending"
        line = float(m.group(1))
        # Determinar equipo
        pick_on_away = any(w in pick_txt for w in [w.upper() for w in re.split(r'\W+', away_raw) if len(w) > 2])
        if pick_on_away:
            margin = away_score - home_score + line
        else:
            margin = home_score - away_score + line
        if margin > 0:
            return "win"
        elif margin == 0:
            return "push"
        else:
            return "loss"

    return "pending"

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print(f"\n=== CHECK RESULTS para {yesterday} ===")

    picks_file = f"picks-{yesterday}.json"
    if not os.path.exists(picks_file):
        print(f"  No existe {picks_file} — nada que verificar.")
        return

    with open(picks_file, encoding="utf-8") as f:
        data = json.load(f)

    picks = data.get("picks", [])
    if not picks:
        print("  Sin picks en el archivo.")
        return

    # Cargar scores por deporte (cache para no repetir llamadas)
    scores_cache: dict[str, list] = {}
    def get_scores(liga):
        sk = get_sport_key(liga)
        if sk not in scores_cache:
            scores_cache[sk] = fetch_scores(sk)
        return scores_cache[sk]

    results = []
    wins = losses = pushes = pending = 0

    for pick in picks:
        liga   = pick.get("liga", "")
        scores = get_scores(liga)
        game   = find_game(pick, scores)

        if game is None:
            resultado = "pending"
            score_str = None
        else:
            resultado = evaluate_pick(pick, game)
            sc = game.get("scores") or []
            score_str = " - ".join(f"{s['name']} {s['score']}" for s in sc) if sc else None

        if resultado == "win":    wins    += 1
        elif resultado == "loss": losses  += 1
        elif resultado == "push": pushes  += 1
        else:                     pending += 1

        results.append({
            "matchup":   pick.get("matchup"),
            "pick":      pick.get("pick"),
            "tipo":      pick.get("tipo"),
            "liga":      liga,
            "cuota":     pick.get("cuota_bet365"),
            "stake":     pick.get("stake"),
            "resultado": resultado,
            "score":     score_str,
        })
        print(f"  [{resultado.upper():7}] {pick.get('matchup')} — {pick.get('pick')}"
              + (f" ({score_str})" if score_str else ""))

    # ROI estimado (solo picks resueltos)
    total_stake = 0.0
    profit = 0.0
    for r in results:
        try:
            stake_val = float(re.sub(r'[^\d.]', '', r.get("stake") or "0"))
        except Exception:
            stake_val = 0.0
        cuota = r.get("cuota") or 0.0
        if r["resultado"] == "win":
            profit += stake_val * (cuota - 1)
            total_stake += stake_val
        elif r["resultado"] == "loss":
            profit -= stake_val
            total_stake += stake_val
        elif r["resultado"] == "push":
            total_stake += stake_val

    roi = (profit / total_stake * 100) if total_stake > 0 else 0.0

    output = {
        "fecha":   yesterday,
        "resumen": {
            "total":   len(picks),
            "wins":    wins,
            "losses":  losses,
            "pushes":  pushes,
            "pending": pending,
            "roi_pct": round(roi, 2),
        },
        "picks": results,
    }

    out_file = f"results-{yesterday}.json"
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output, f, ensure_ascii=False, indent=2)

    print(f"\n  ✅ {wins}W / {losses}L / {pushes}P / {pending} pendientes | ROI estimado: {roi:+.1f}%")
    print(f"  Guardado: {out_file}")

if __name__ == "__main__":
    main()
