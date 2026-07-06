# ⚡ TIPSTER PRO IA

Web app de picks deportivos diarios con metodología +EV real.

## Setup en 5 minutos

### 1. Subir a GitHub
```bash
git init
git add .
git commit -m "🎯 Tipster Pro IA init"
git remote add origin https://github.com/TU_USUARIO/tipster-pro-ia.git
git push -u origin main
```

### 2. Agregar API Key en GitHub
1. Ve a tu repo → **Settings** → **Secrets and variables** → **Actions**
2. Click **New repository secret**
3. Nombre: `ANTHROPIC_API_KEY`
4. Valor: tu API key de Anthropic (https://console.anthropic.com)

### 3. Conectar Netlify a GitHub
1. Ve a [app.netlify.com](https://app.netlify.com)
2. **Add new site** → **Import from Git** → GitHub
3. Selecciona este repo
4. Build command: (vacío)
5. Publish directory: `.`
6. Deploy

### 4. Listo
- Cada mañana a las **5:00 AM CT**, GitHub Actions llama a Claude API, genera los picks y hace push automático
- Netlify detecta el push y despliega en segundos
- Abre la URL en tu cel y agrégala a la pantalla de inicio

## Archivos
- `index.html` — La web app (UI completa)
- `picks-data.js` — Picks del día (auto-generado cada mañana)
- `generate_picks.py` — Script que llama a Claude API
- `.github/workflows/daily-picks.yml` — GitHub Action (5 AM CT diario)

## Ejecutar manualmente
En GitHub → **Actions** → **Generar Picks Diarios** → **Run workflow**

## Metodología de EV (cuota mínima con valor)

Cada pick se valida contra el **mercado sharp (Pinnacle)** vía The Odds API:

1. Se toma la línea de Pinnacle y se le quita el vig → **probabilidad justa**.
2. De ahí sale la **cuota mínima con valor** = `1 / prob_justa`. Es el precio que
   tu casa debe **superar** para que la apuesta tenga EV positivo real.
3. Se compara la mejor cuota disponible en el mercado (line-shopping) contra esa
   cuota mínima. Si no le gana por al menos ~3%, el pick se descarta a *No Apostar*.

Como **PlayDoIt / Winpot** (casas mexicanas) no están en ninguna API pública, la
app no puede leer sus cuotas: en su lugar te muestra la **cuota mínima** para que
tú la compares con lo que ofrece tu casa. Si tu cuota ≥ cuota mínima → hay valor.

### Verificación manual opcional (oddschecker)
Antes de apostar, puedes contrastar el mercado amplio en
[oddschecker.com](https://www.oddschecker.com). No lista casas mexicanas, pero el
mercado internacional que sí muestra suele mover en línea con PlayDoIt/Winpot, así
que sirve como segundo chequeo de que la cuota mínima calculada es coherente.
