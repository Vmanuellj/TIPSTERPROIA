#!/usr/bin/env python3
"""
TIPSTER PRO IA — Generador de Picks Diarios
Llama a Claude API con metodología EV+, genera picks JSON y actualiza picks-data.js
"""
import anthropic
import json
import os
from datetime import datetime, timezone, timedelta

# Zona horaria Ciudad de México (CT = UTC-6)
CT = timezone(timedelta(hours=-6))
today = datetime.now(CT).strftime("%Y-%m-%d")
weekday_es = ["Lunes","Martes","Miércoles","Jueves","Viernes","Sábado","Domingo"]
dt = datetime.now(CT)
fecha_display = f"{weekday_es[dt.weekday()]} {dt.day} de {['Enero','Febrero','Marzo','Abril','Mayo','Junio','Julio','Agosto','Septiembre','Octubre','Noviembre','Diciembre'][dt.month-1]} {dt.year}"

PROMPT = f"""Eres un tipster profesional y analista cuantitativo de apuestas deportivas. Hoy es {today}.

Tu única misión: encontrar valor esperado positivo (+EV) real. Casa principal: Winpot (MXN). Referencia: Bet365.
Ligas prioritarias: MLB y NBA. También Liga MX, La Liga o Premier según jornada.

METODOLOGÍA OBLIGATORIA por pick:
1. Convierte momio a probabilidad implícita y réstale el margen de la casa (~4.5%)
2. Estima TU probabilidad propia con base en datos reales (forma, pitchers probables, lesiones, head-to-head, xERA, FIP, etc.)
3. EV% = (prob_propia × cuota_decimal) - 1. Solo incluye el pick si EV > 0
4. Stake con Kelly fraccional (1/4 Kelly). Exposición total de sesión 10-15%
5. Para parlays: calcula EV ya con boost Winpot aplicado (~15-40%). Mínimo cuota 1.20 por pata.
6. Props clavados en el promedio estacional = sin edge → no incluir
7. Si no hay edge real hoy, manda partidos a NO APOSTAR y explica por qué

Devuelve ÚNICAMENTE el JSON a continuación, sin texto adicional, sin markdown, sin explicaciones fuera del JSON:

{{
  "fecha": "{today}",
  "fecha_display": "{fecha_display}",
  "bankroll": {{
    "exposicion_total": "X.X u",
    "max_por_juego": "X.X u",
    "nota": "resumen de estrategia del día"
  }},
  "picks": [
    {{
      "liga": "MLB",
      "matchup": "Equipo A vs Equipo B",
      "hora": "HH:MM CT",
      "pick": "descripción del pick (ej: Over 8.5 Runs, Yankees ML, Nola Over 4.5 Ks)",
      "cuota_winpot": 1.95,
      "cuota_bet365": 1.90,
      "prob_implicita": 52.6,
      "prob_propia": 58.0,
      "ev_pct": 4.2,
      "prob_acierto": 58,
      "estrellas": 3,
      "stake": "1.0 u",
      "razonamiento": "2-3 líneas con datos concretos: stats recientes, tendencias, situación del partido"
    }}
  ],
  "no_apostar": [
    {{
      "matchup": "Equipo A vs Equipo B",
      "liga": "NBA",
      "razon": "Explicación concreta de por qué no tiene edge"
    }}
  ],
  "parlay_sugerido": {{
    "patas": ["pick1 corto", "pick2 corto"],
    "cuota_total": 3.80,
    "cuota_con_boost": 4.37,
    "ev_pct": 2.1,
    "stake": "0.5 u",
    "nota": "boost X% Winpot aplicado, correlación: ..."
  }}
}}

Genera 4-6 picks con edge real. Si hay pocos juegos con valor, menos picks es mejor. Prioriza calidad sobre cantidad.
"""

def generate_picks():
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY no está configurada")

    client = anthropic.Anthropic(api_key=api_key)

    print(f"[{today}] Consultando Claude API para picks del día...")

    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4096,
        messages=[{"role": "user", "content": PROMPT}]
    )

    raw = message.content[0].text.strip()

    # Limpiar posibles bloques markdown
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
    raw = raw.strip()

    # Parsear y validar
    data = json.loads(raw)
    assert "picks" in data, "Falta campo 'picks'"
    assert "bankroll" in data, "Falta campo 'bankroll'"

    print(f"✅ {len(data['picks'])} picks generados")
    for p in data["picks"]:
        print(f"   {p['estrellas']}★ {p['matchup']} — {p['pick']} | EV +{p['ev_pct']}% | {p['stake']}")

    return data

def write_picks_js(data):
    js_content = f"// Auto-generado por TIPSTER PRO IA el {today}\n"
    js_content += f"window.PICKS_DATA = {json.dumps(data, ensure_ascii=False, indent=2)};\n"

    with open("picks-data.js", "w", encoding="utf-8") as f:
        f.write(js_content)
    print(f"✅ picks-data.js actualizado")

    # Escribir picks-FECHA.json (archivo diario)
    json_filename = f"picks-{today}.json"
    with open(json_filename, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ {json_filename} escrito")

    # Escribir latest.json (siempre apunta a los picks más recientes)
    with open("latest.json", "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    print(f"✅ latest.json actualizado")

if __name__ == "__main__":
    try:
        data = generate_picks()
        write_picks_js(data)
    except Exception as e:
        print(f"❌ Error: {e}")
        # Fallback: picks vacíos para no romper la web
        fallback = {
            "fecha": today,
            "fecha_display": fecha_display,
            "bankroll": {"exposicion_total": "0 u", "max_por_juego": "0 u", "nota": "Error al generar picks hoy"},
            "picks": [],
            "no_apostar": [{"matchup": "Error de generación", "liga": "—", "razon": str(e)}],
            "parlay_sugerido": {"patas": [], "cuota_total": 0, "cuota_con_boost": 0, "ev_pct": 0, "stake": "0 u", "nota": ""}
        }
        write_picks_js(fallback)
        raise
