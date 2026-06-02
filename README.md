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
