import os
import sqlite3
import time
import threading
import requests
import json
import logging
import uuid
import secrets
import string
from datetime import datetime, timedelta
from flask import Flask, render_template, request, make_response, redirect, url_for, g
from dotenv import load_dotenv
import numpy as np
from apscheduler.schedulers.background import BackgroundScheduler

# Load environment variables
load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "momentum_fc_secret_key")

# Vercel / Serverless Compatibility
IS_VERCEL = os.environ.get("VERCEL") == "1"
if IS_VERCEL:
    DATABASE = "/tmp/momentum_fc.db"
else:
    DATABASE = "momentum_fc.db"

BASE_URL = "https://api.football-data.org/v4"
# Top Competitions IDs: PL (2021), PD (2014), CL (2001), BL1 (2002), SA (2019), FL1 (2015)
TOP_COMPETITIONS = "2021,2014,2001,2002,2019,2015"

# State tracking
api_status = {"status": "Unknown", "last_check": None, "error": None}

# Database setup
def get_db():
    db = getattr(g, '_database', None)
    if db is None:
        db = g._database = sqlite3.connect(DATABASE, timeout=10)
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, '_database', None)
    if db is not None:
        db.close()

def init_db():
    logging.basicConfig(level=logging.INFO)
    with app.app_context():
        db = get_db()
        cursor = db.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS matches (
                id INTEGER PRIMARY KEY,
                name TEXT,
                home_team TEXT,
                away_team TEXT,
                status TEXT,
                utc_date TEXT,
                score_home INTEGER,
                score_away INTEGER,
                last_updated TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                match_id INTEGER,
                timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                minute INTEGER,
                score_home INTEGER,
                score_away INTEGER,
                pressure_index REAL,
                FOREIGN KEY (match_id) REFERENCES matches (id)
            )
        ''')
        # Prediction System Tables
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                referral_code TEXT UNIQUE NOT NULL,
                referred_by_code TEXT,
                points INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS predictions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                match_id INTEGER,
                predicted_outcome TEXT, -- 'HOME', 'DRAW', 'AWAY'
                predicted_home_goals INTEGER,
                predicted_away_goals INTEGER,
                points_awarded INTEGER DEFAULT 0,
                status TEXT DEFAULT 'PENDING',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(user_id, match_id),
                FOREIGN KEY (user_id) REFERENCES users (id)
            )
        ''')
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS referrals (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                referrer_user_id INTEGER,
                referred_user_id INTEGER,
                bonus_points INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (referrer_user_id) REFERENCES users (id),
                FOREIGN KEY (referred_user_id) REFERENCES users (id)
            )
        ''')
        db.commit()

def generate_referral_code():
    return ''.join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))

# Momentum calculation logic
def calculate_pressure_index(snapshot_data, previous_snapshots):
    minute = snapshot_data.get('minute', 0)
    score_home = snapshot_data.get('score_home', 0)
    score_away = snapshot_data.get('score_away', 0)
    
    base_pressure = (minute / 90) * 20 
    score_diff = score_home - score_away
    score_impact = np.clip(score_diff * 30, -60, 60)
    
    trend_impact = 0
    if previous_snapshots:
        last = previous_snapshots[0]
        if score_home > last['score_home']:
            trend_impact += 40
        if score_away > last['score_away']:
            trend_impact -= 40
            
    pressure = base_pressure + score_impact + trend_impact
    return float(np.clip(pressure, -100, 100))

def get_forecast(snapshots):
    if len(snapshots) < 3:
        return {"level": "Inconclusive", "probability": 0, "explanation": "Insufficient data for trend analysis."}
    
    y = [s['pressure_index'] for s in snapshots[-10:]]
    x = list(range(len(y)))
    
    try:
        slope, intercept = np.polyfit(x, y, 1)
        variance = np.var(y)
    except Exception as e:
        logging.warning(f"Error calculating forecast: {e}")
        return {"level": "Moderate", "probability": 50, "explanation": "Calculating..."}
    
    volatility_penalty = min(variance / 10, 20)
    probability = min(max(50 + (slope * 10) - volatility_penalty, 0), 100)
    
    if slope > 1:
        level = "High"
    elif slope > -1:
        level = "Moderate"
    else:
        level = "Low"
        
    explanation = f"Slope: {slope:.2f}, Var: {variance:.1f}. "
    explanation += "Upward trend." if slope > 0 else "Downward pressure."
        
    return {"level": level, "probability": int(probability), "explanation": explanation}

def generate_svg_chart(snapshots, width=600, height=200, padding=20, color="#a855f7"):
    if not snapshots: return ""
    points = []
    for i, s in enumerate(snapshots):
        x = padding + (i * (width - 2 * padding) / (max(len(snapshots) - 1, 1)))
        y = height / 2 - (s['pressure_index'] * (height / 2 - padding) / 100)
        points.append(f"{x},{y}")
    polyline = " ".join(points)
    return f'''<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
        <defs>
            <linearGradient id="grad-{color.replace('#','')}" x1="0%" y1="0%" x2="100%" y2="0%">
                <stop offset="0%" style="stop-color:{color};stop-opacity:0.2" />
                <stop offset="100%" style="stop-color:{color};stop-opacity:1" />
            </linearGradient>
        </defs>
        <line x1="{padding}" y1="{height/2}" x2="{width-padding}" y2="{height/2}" stroke="rgba(255,255,255,0.1)" stroke-dasharray="4"/>
        <polyline points="{polyline}" fill="none" stroke="url(#grad-{color.replace('#','')})" stroke-width="3" stroke-linejoin="round" />
    </svg>'''

def generate_sparkline(snapshots):
    # Mini SVG for match cards
    return generate_svg_chart(snapshots, width=120, height=40, padding=5, color="#22c55e")

def poll_live_matches():
    global api_status
    api_status["last_check"] = str(datetime.now())
    load_dotenv(override=True)
    current_key = os.getenv("FOOTBALL_DATA_API_KEY")
    is_demo = not current_key or "your_api_key_here" in current_key
    
    if is_demo:
        api_status["status"] = "Demo Mode"
        api_status["error"] = "Using synthetic data. Add a real API key in .env to see live matches."
        matches = [
            {'id': 1001, 'homeTeam': {'name': 'Demo United'}, 'awayTeam': {'name': 'Mock City'}, 'status': 'LIVE', 
             'score': {'fullTime': {'home': 2, 'away': 1}}, 'utcDate': str(datetime.now())},
            {'id': 1002, 'homeTeam': {'name': 'Synthetic FC'}, 'awayTeam': {'name': 'Silicon Real'}, 'status': 'LIVE', 
             'score': {'fullTime': {'home': 0, 'away': 0}}, 'utcDate': str(datetime.now())}
        ]
    else:
        try:
            headers = {'X-Auth-Token': current_key}
            # Filter matches by top competitions
            response = requests.get(f"{BASE_URL}/matches", headers=headers, params={'status': 'LIVE', 'competitions': TOP_COMPETITIONS}, timeout=10)
            if response.status_code != 200:
                api_status["status"] = "API Error"
                api_status["error"] = f"Error {response.status_code}: {response.json().get('message', 'Unknown')}"
                return
            api_status["status"] = "Healthy"
            api_status["error"] = None
            matches = response.json().get('matches', [])
        except Exception as e:
            api_status["status"] = "Connection Error"
            api_status["error"] = f"Failed to connect to API: {str(e)}"
            logging.error(f"API Connection Error: {e}")
            return

    with app.app_context():
        db = get_db()
        
        # Define top competitions
        top_comp_ids = [int(i.strip()) for i in TOP_COMPETITIONS.split(',')]
        
        # Filter matches early to ensure cleanup works for non-top leagues too
        matches = [m for m in matches if isinstance(m, dict) and isinstance(m.get('competition'), dict) and m['competition'].get('id') in top_comp_ids]
        
        # Track which IDs we saw in this poll (now filtered)
        seen_ids = [m['id'] for m in matches]
        
        # Mark matches that were previously active but are not in the current response as FINISHED
        active_statuses = "('LIVE', 'IN_PLAY', 'TIMED', 'STARTING')"
        if seen_ids:
            placeholders = ','.join(['?'] * len(seen_ids))
            db.execute(f"UPDATE matches SET status='FINISHED' WHERE status IN {active_statuses} AND id NOT IN ({placeholders})", seen_ids)
        else:
            db.execute(f"UPDATE matches SET status='FINISHED' WHERE status IN {active_statuses}")

        for m in matches:
                
            match_id = m['id']
            home_name = m['homeTeam']['name']
            away_name = m['awayTeam']['name']
            name = f"{home_name} vs {away_name}"
            status = m['status']
            score_data = m.get('score', {}).get('fullTime', {})
            score_home = score_data.get('home') if score_data.get('home') is not None else 0
            score_away = score_data.get('away') if score_data.get('away') is not None else 0
            
            db.execute('''
                INSERT INTO matches (id, name, home_team, away_team, status, utc_date, score_home, score_away)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET status=excluded.status, score_home=excluded.score_home, score_away=excluded.score_away, last_updated=CURRENT_TIMESTAMP
            ''', (match_id, name, home_name, away_name, status, m['utcDate'], score_home, score_away))
            
            cursor = db.execute('SELECT * FROM snapshots WHERE match_id = ? ORDER BY timestamp DESC LIMIT 20', (match_id,))
            prev_snapshots = cursor.fetchall()
            
            # Calculate dynamic minute
            try:
                # API format usually ends with Z, but let's be safe
                utc_val = m.get('utcDate')
                if not isinstance(utc_val, str):
                    utc_val = str(utc_val) if utc_val else ""
                
                utc_date_str = utc_val.replace('Z', '+00:00')
                if not utc_date_str:
                    raise ValueError("Empty UTC date")
                
                match_start = datetime.fromisoformat(utc_date_str)
                now_utc = datetime.now(match_start.tzinfo)
                minute = int((now_utc - match_start).total_seconds() / 60)
                # Cap and adjust for break/extra time roughly
                minute = max(1, min(minute, 120))
            except Exception as e:
                logging.warning(f"Could not calculate minute for match {match_id}: {e}")
                minute = 45 # Fallback
                
            p_index = calculate_pressure_index({'minute': minute, 'score_home': score_home, 'score_away': score_away}, prev_snapshots)
            
            db.execute('INSERT INTO snapshots (match_id, minute, score_home, score_away, pressure_index) VALUES (?, ?, ?, ?, ?)',
                       (match_id, minute, score_home, score_away, p_index))
            db.execute('DELETE FROM snapshots WHERE id IN (SELECT id FROM snapshots WHERE match_id=? ORDER BY timestamp DESC LIMIT -1 OFFSET 2000)', (match_id,))
        db.commit()
    
    # Auto-scoring of predictions
    score_predictions()

def score_predictions():
    with app.app_context():
        db = get_db()
        # Find pending predictions for finished matches
        preds = db.execute('''
            SELECT p.*, m.score_home as actual_h, m.score_away as actual_a, m.status as match_status 
            FROM predictions p
            JOIN matches m ON p.match_id = m.id
            WHERE p.status = 'PENDING' AND m.status = 'FINISHED'
        ''').fetchall()
        
        for p in preds:
            points = 0
            actual_h = p['actual_h']
            actual_a = p['actual_a']
            pred_h = p['predicted_home_goals']
            pred_a = p['predicted_away_goals']
            pred_outcome = p['predicted_outcome']
            
            actual_outcome = 'DRAW'
            if actual_h > actual_a: actual_outcome = 'HOME'
            elif actual_a > actual_h: actual_outcome = 'AWAY'
            
            if pred_outcome == actual_outcome:
                points += 120 if actual_outcome == 'DRAW' else 100
                
            if pred_h == actual_h and pred_a == actual_a:
                points += 200 # Exact score bonus
                
            db.execute('UPDATE predictions SET points_awarded = ?, status = "SCORED" WHERE id = ?', (points, p['id']))
            db.execute('UPDATE users SET points = points + ? WHERE id = ?', (points, p['user_id']))
        
        db.commit()

# Scheduler (Disabled on Vercel/Serverless)
if not IS_VERCEL:
    scheduler = BackgroundScheduler()
    scheduler.add_job(func=poll_live_matches, trigger="interval", seconds=60)
    scheduler.start()
else:
    # On Vercel, we might want to poll once per process start or handle it differently
    # For now, let's just ensure DB exists
    with app.app_context():
        init_db()

# Routes
@app.route('/')
def home():
    theme = request.cookies.get('theme', 'dark')
    username = request.cookies.get('username')
    return render_template('home.html', theme=theme, api_status=api_status, username=username)

@app.route('/ref/<referral_code>')
def referral_entry(referral_code):
    resp = make_response(redirect(url_for('predict_page')))
    resp.set_cookie('referred_by', referral_code, max_age=3600) # 1 hour to register
    return resp

@app.route('/predict', methods=['GET', 'POST'])
def predict_page():
    theme = request.cookies.get('theme', 'dark')
    username = request.cookies.get('username')
    db = get_db()
    
    if request.method == 'POST':
        if not username:
            candidate_username = request.form.get('username', '').strip()
            if len(candidate_username) < 3 or len(candidate_username) > 20:
                return "Username must be 3-20 characters", 400
            
            # Create user
            referred_by = request.cookies.get('referred_by')
            ref_code = generate_referral_code()
            
            try:
                db.execute('INSERT INTO users (username, referral_code, referred_by_code) VALUES (?, ?, ?)',
                           (candidate_username, ref_code, referred_by))
                db.commit()
                
                # If referred, we handle points later or here? Let's do it here for the referrer.
                if referred_by:
                    referrer = db.execute('SELECT id FROM users WHERE referral_code = ?', (referred_by,)).fetchone()
                    if referrer:
                        new_user = db.execute('SELECT id FROM users WHERE username = ?', (candidate_username,)).fetchone()
                        db.execute('UPDATE users SET points = points + 50 WHERE id = ?', (referrer['id'],))
                        db.execute('INSERT INTO referrals (referrer_user_id, referred_user_id, bonus_points) VALUES (?, ?, ?)',
                                   (referrer['id'], new_user['id'], 50))
                        db.commit()
            except sqlite3.IntegrityError:
                # User might already exist, just log them in if it's the same session?
                # For this MVP, let's just fetch them.
                pass
            
            resp = make_response(redirect(url_for('predict_page')))
            resp.set_cookie('username', candidate_username, max_age=31536000)
            return resp
        
        # Save prediction
        match_id = request.form.get('match_id')
        outcome = request.form.get('outcome') # 'HOME', 'DRAW', 'AWAY'
        h_goals = request.form.get('home_goals', 0)
        a_goals = request.form.get('away_goals', 0)
        
        user = db.execute('SELECT id FROM users WHERE username = ?', (username,)).fetchone()
        if not user: return redirect(url_for('predict_page'))
        
        try:
            db.execute('''
                INSERT INTO predictions (user_id, match_id, predicted_outcome, predicted_home_goals, predicted_away_goals)
                VALUES (?, ?, ?, ?, ?)
            ''', (user['id'], match_id, outcome, h_goals, a_goals))
            db.commit()
        except sqlite3.IntegrityError:
            return "You already predicted this match!", 400
            
        return redirect(url_for('predict_page'))

    # GET: Show form or listing
    if not username:
        return render_template('predict_reg.html', theme=theme)
    
    user_data = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
    
    # Matches available for prediction: LIVE or SCHEDULED (Upcoming)
    matches_raw = db.execute('SELECT * FROM matches WHERE status IN ("LIVE", "IN_PLAY", "PAUSED", "SCHEDULED", "TIMED") ORDER BY utc_date ASC').fetchall()
    
    # Filter out matches already predicted
    user_preds = db.execute('SELECT match_id FROM predictions WHERE user_id = ?', (user_data['id'],)).fetchall()
    pred_ids = [p['match_id'] for p in user_preds]
    
    available_matches = []
    for m in matches_raw:
        if m['id'] not in pred_ids:
            available_matches.append(dict(m))
            
    ref_link = f"{request.url_root.rstrip('/')}/ref/{user_data['referral_code']}"
    
    return render_template('predict_list.html', theme=theme, matches=available_matches, user=user_data, ref_link=ref_link)

@app.route('/leaderboard')
def leaderboard():
    theme = request.cookies.get('theme', 'dark')
    username = request.cookies.get('username')
    db = get_db()
    
    top_users = db.execute('SELECT username, points FROM users ORDER BY points DESC LIMIT 50').fetchall()
    
    return render_template('leaderboard.html', theme=theme, users=top_users, current_username=username)
@app.route('/live')
def live_matches():
    theme = request.cookies.get('theme', 'dark')
    followed = request.cookies.get('followed_matches', '').split(',')
    db = get_db()
    matches_raw = db.execute('SELECT * FROM matches WHERE status IN ("LIVE", "IN_PLAY", "PAUSED") ORDER BY last_updated DESC').fetchall()
    
    matches = []
    featured_match = None
    for i, m in enumerate(matches_raw):
        # m is a sqlite3.Row, convert to dict for item assignment
        match_dict = dict(m)
        snapshots_raw = db.execute('SELECT * FROM snapshots WHERE match_id=? ORDER BY timestamp ASC', (match_dict['id'],)).fetchall()
        snapshots = [dict(s) for s in snapshots_raw]
        
        match_dict['sparkline'] = generate_sparkline(snapshots)
        match_dict['is_followed'] = str(match_dict['id']) in followed
        
        # Calculate time since last update
        last_upd = datetime.strptime(m['last_updated'], "%Y-%m-%d %H:%M:%S")
        match_dict['seconds_ago'] = int((datetime.now() - last_upd).total_seconds())
        
        if i == 0: featured_match = match_dict
        matches.append(match_dict)
        
    return render_template('live.html', matches=matches, featured_match=featured_match, theme=theme)

@app.route('/predict/<int:match_id>')
def predict(match_id):
    theme = request.cookies.get('theme', 'dark')
    db = get_db()
    match = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if not match: return "Match not found", 404
    
    snapshots = db.execute('SELECT * FROM snapshots WHERE match_id=? ORDER BY timestamp ASC', (match_id,)).fetchall()
    
    # Simple heuristic predictor
    # Home win prob = 33% + (pressure/2)
    # Away win prob = 33% - (pressure/2)
    current_pressure = 0
    if snapshots:
        last_s = snapshots[-1]
        if isinstance(last_s, dict):
            current_pressure = last_s.get('pressure_index', 0)
        else:
            current_pressure = last_s['pressure_index']
    home_prob = 33 + (current_pressure * 0.3)
    away_prob = 33 - (current_pressure * 0.3)
    draw_prob = 100 - home_prob - away_prob
    
    # Normalize
    total = home_prob + away_prob + draw_prob
    home_prob = int((home_prob / total) * 100)
    away_prob = int((away_prob / total) * 100)
    draw_prob = 100 - home_prob - away_prob
    
    analysis = {
        "home": home_prob,
        "away": away_prob,
        "draw": draw_prob,
        "explanation": "Based on current momentum slope and pressure intensity."
    }
    
    return render_template('predict.html', match=match, analysis=analysis, theme=theme)

@app.route('/toggle-follow/<int:match_id>')
def toggle_follow(match_id):
    followed_raw = request.cookies.get('followed_matches', '')
    followed = [f for f in followed_raw.split(',') if f]
    
    if str(match_id) in followed:
        followed.remove(str(match_id))
    else:
        followed.append(str(match_id))
        
    resp = make_response(redirect(request.referrer or url_for('live_matches')))
    resp.set_cookie('followed_matches', ','.join(followed), max_age=31536000)
    return resp

@app.route('/match/<int:match_id>')
def match_dashboard(match_id):
    theme = request.cookies.get('theme', 'dark')
    followed = request.cookies.get('followed_matches', '').split(',')
    db = get_db()
    match = db.execute('SELECT * FROM matches WHERE id = ?', (match_id,)).fetchone()
    if not match: return "Match not found", 404
    
    is_followed = str(match_id) in followed
    snapshots_raw = db.execute('SELECT * FROM snapshots WHERE match_id=? ORDER BY timestamp ASC', (match_id,)).fetchall()
    snapshots = [dict(s) for s in snapshots_raw]
    latest_snapshots = snapshots[-12:]
    latest_snapshots.reverse()
    chart_svg = generate_svg_chart(snapshots)
    forecast = get_forecast(snapshots)
    
    # Pressure at latest snapshot
    current_pressure = 0
    if snapshots:
        last_s = snapshots[-1]
        if isinstance(last_s, dict):
            current_pressure = last_s.get('pressure_index', 0)
        else:
            current_pressure = last_s['pressure_index']
    events = [
        {"time": "85'", "event": "Substitution", "desc": "Fresh legs for the final push."},
        {"time": "72'", "event": "Yellow Card", "desc": "High intensity foul."},
        {"time": "45'", "event": "Half Time", "desc": "Teams regrouping."}
    ]
    
    return render_template('match.html', match=match, snapshots=latest_snapshots, 
                           chart_svg=chart_svg, forecast=forecast, 
                           is_followed=is_followed, events=events, theme=theme)

@app.route('/upcoming')
def upcoming_matches():
    theme = request.cookies.get('theme', 'dark')
    current_key = os.getenv("FOOTBALL_DATA_API_KEY")
    if not current_key or "your_api_key_here" in current_key:
        return render_template('upcoming.html', matches=[], theme=theme, note="API Key required for upcoming matches.")
        
    try:
        headers = {'X-Auth-Token': current_key}
        response = requests.get(f"{BASE_URL}/matches", headers=headers, params={
            'dateFrom': datetime.now().strftime('%Y-%m-%d'), 
            'dateTo': (datetime.now() + timedelta(days=7)).strftime('%Y-%m-%d'), 
            'status': 'SCHEDULED',
            'competitions': TOP_COMPETITIONS
        })
        matches = response.json().get('matches', [])
    except Exception as e:
        logging.error(f"Error fetching upcoming matches: {e}")
        matches = []
    return render_template('upcoming.html', matches=matches, theme=theme)


@app.route('/about')
def about():
    theme = request.cookies.get('theme', 'dark')
    return render_template('about.html', theme=theme)

@app.route('/set_theme', methods=['POST'])
def set_theme():
    theme = request.form.get('theme', 'dark')
    resp = make_response(redirect(request.referrer or url_for('home')))
    resp.set_cookie('theme', theme, max_age=31536000)
    return resp

if __name__ == '__main__':
    init_db()
    poll_live_matches()
    app.run(host='0.0.0.0', port=int(os.getenv("PORT", 5000)), debug=os.getenv("DEBUG", "False") == "True")
