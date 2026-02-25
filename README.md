# Momentum FC ⚽📈

A real-time football momentum tracker powered by Python and server-side rendering. **Zero JavaScript required.**

## 🚀 Features
- **Deterministic Pressure Index:** Normalized [-100, +100] metric computed server-side.
- **Momentum Forecast:** Next 10-minute play prediction using slope analysis and volatility penalties.
- **Server-Side SVGs:** Line charts generated dynamically for every match.
- **Modern Glassmorphism UI:** Premium dark/light themes with neon accents.
- **No-JS Live Updates:** Uses HTML `<meta>` refresh for a seamless real-time feel.

## 🛠️ Setup Instructions

1. **Clone/Extract** the project folder.
2. **Install Dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
3. **Configure Environment:**
   - Copy `.env.example` to `.env`.
   - Add your [football-data.org](https://www.football-data.org/) API key to `FOOTBALL_DATA_API_KEY`.
4. **Run the App:**
   ```bash
   python app.py
   ```
5. **Access:** Open `http://localhost:5000` in your browser.

## 🧠 Core Architecture

### Polling Worker
A background thread in `app.py` polls the API every 60 seconds. It captures match snapshots and stores them in a SQLite database. Snapshot retention is capped at 2000 rows per match.

### Momentum Algorithm
The **Pressure Index** is calculated by analyzing:
- Score changes (immediate spikes)
- Match minute progression (intensity creep)
- Recent event transitions

### Forecast Engine
Linear regression is applied to the last N snapshots to calculate a **slope**. The forecast level (Low/Moderate/High) is determined by the slope, while the probability is adjusted by a **volatility penalty** (variance).

## ⚠️ Transparent Limitations
- **Data Source:** Limited by the free tier of football-data.org.
- **Derived Metrics:** Some stats are labeled as "Derived (proxy)" because the free API tier does not provide deep technical stats like xG or dangerous attacks.
- **Refresh rate:** Minimum refresh is 30 seconds to respect API rate limits.
