import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
import sqlite3
from datetime import datetime, timedelta

# 1. Load Environment Variables
load_dotenv()

# 2. Initialize Flask App
app = Flask(__name__)

# --- CONFIGURATION ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") 
DEFAULT_CLIQ_WEBHOOK_URL = os.getenv("CLIQ_WEBHOOK_URL")

# --- SYSTEM SETUP ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-pro')

# --- 📦 DATABASE SETUP ---
DB_NAME = "standups.db"

def init_db():
    """Creates the database table if it doesn't exist."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute('''
        CREATE TABLE IF NOT EXISTS updates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            author TEXT,
            summary TEXT,
            timestamp DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    conn.commit()
    conn.close()
    print("✅ Database initialized!")

def save_to_db(author, summary):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    # Store pure python datetime object handling
    c.execute("INSERT INTO updates (author, summary) VALUES (?, ?)", (author, summary))
    conn.commit()
    conn.close()
    print(f"💾 Saved entry for {author} to database.")

# --- AI & CLIQ FUNCTIONS ---
def generate_standup_summary(commits):
    prompt = f"""
    You are an Agile Scrum Assistant. 
    Analyze these git commit messages and convert them into a daily standup update.
    COMMITS: {commits}
    OUTPUT FORMAT:
    * **Completed:** (Summarize work in 1-2 bullet points)
    * **Technical Context:** (Files/Libraries touched)
    * **Potential Blockers:** (Bugs fixed or None)
    Keep it concise.
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI summary: {e}"

def send_to_cliq(text, author, target_webhook_url):
    if not target_webhook_url: return
    try:
        payload = {
            "text": f"### 🚀 GitSync Standup: {author}",
            "bot": { "name": "GitSync Bot", "image": "https://cdn-icons-png.flaticon.com/512/4712/4712109.png" },
            "card": { "title": f"Daily Update: {author}", "theme": "modern-inline" },
            "slides": [ { "type": "text", "data": text } ]
        }
        requests.post(target_webhook_url, json=payload)
    except Exception as e:
        print(f"❌ Error sending to Cliq: {e}")

# --- ROUTES ---
@app.route('/', methods=['GET'])
def home():
    return redirect('/dashboard')

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    
    # Dynamic Routing
    dynamic_channel = request.args.get('channel')
    dynamic_token = request.args.get('token')
    dynamic_oid = request.args.get('oid')
    
    if dynamic_channel and dynamic_token:
        company_id = dynamic_oid if dynamic_oid else "906264961"
        target_url = f"https://cliq.zoho.com/company/{company_id}/api/v2/channelsbyname/{dynamic_channel}/message?zapikey={dynamic_token}"
    else:
        target_url = DEFAULT_CLIQ_WEBHOOK_URL

    if data and 'commits' in data:
        author_name = data['pusher']['name']
        commit_messages = [commit['message'] for commit in data['commits']]
        full_raw_update = "\n".join(commit_messages)

        print(f"\n🔄 Processing commits from {author_name}...")
        ai_summary = generate_standup_summary(full_raw_update)
        send_to_cliq(ai_summary, author_name, target_url)
        save_to_db(author_name, ai_summary)
        
    return jsonify({"status": "success"}), 200

# --- 📊 ADVANCED DASHBOARD SECTION ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # 1. Get All Data to sort in Python
    c.execute("SELECT author, summary, timestamp FROM updates ORDER BY id DESC")
    all_updates = c.fetchall()
    
    # 2. Get Chart Stats
    c.execute("SELECT author, COUNT(*) FROM updates GROUP BY author")
    stats = c.fetchall()
    conn.close()

    # --- TIME SORTING LOGIC ---
    today_logs = []
    yesterday_logs = []
    week_logs = []
    
    now = datetime.utcnow()
    today_str = now.strftime('%Y-%m-%d')
    yesterday_str = (now - timedelta(days=1)).strftime('%Y-%m-%d')
    
    for u in all_updates:
        # u[2] is timestamp string "2023-11-22 10:00:00"
        db_date_str = u[2].split(' ')[0] # Get just YYYY-MM-DD
        
        item_html = f'<div class="update-item"><div class="meta">👤 {u[0]} | 🕒 {u[2]}</div><pre>{u[1]}</pre></div>'
        
        if db_date_str == today_str:
            today_logs.append(item_html)
        elif db_date_str == yesterday_str:
            yesterday_logs.append(item_html)
        else:
            week_logs.append(item_html)

    # Chart Data
    chart_labels = [row[0] for row in stats]
    chart_data = [row[1] for row in stats]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>GitSync Team Analytics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 20px; color: #333; }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #2c3e50; margin-bottom: 30px; }}
            .card {{ background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-bottom: 25px; }}
            h2 {{ font-size: 1.2em; border-bottom: 2px solid #eee; padding-bottom: 10px; margin-top: 0; color: #444; }}
            .section-header {{ background: #e9ecef; padding: 8px 15px; border-radius: 6px; margin: 15px 0 10px; font-weight: bold; color: #555; }}
            .update-item {{ background: #fff; border-left: 4px solid #007bff; padding: 15px; margin-bottom: 10px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .meta {{ color: #888; font-size: 0.85em; margin-bottom: 8px; font-weight: 600; text-transform: uppercase; letter-spacing: 0.5px; }}
            pre {{ white-space: pre-wrap; font-family: 'Segoe UI', sans-serif; color: #333; margin: 0; line-height: 1.5; }}
            .empty-msg {{ color: #999; font-style: italic; padding: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 GitSync Analytics</h1>
            
            <div class="card">
                <h2>🏆 Team Velocity</h2>
                <div style="height: 200px;">
                    <canvas id="activityChart"></canvas>
                </div>
            </div>

            <div class="card">
                <h2>📅 Activity Timeline</h2>
                
                <div class="section-header">🔥 Today</div>
                { "".join(today_logs) if today_logs else "<div class='empty-msg'>No updates yet today.</div>" }
                
                <div class="section-header">⏪ Yesterday</div>
                { "".join(yesterday_logs) if yesterday_logs else "<div class='empty-msg'>No updates yesterday.</div>" }
                
                <div class="section-header">📂 Past History</div>
                { "".join(week_logs) if week_logs else "<div class='empty-msg'>No older history.</div>" }
            </div>
        </div>

        <script>
            const ctx = document.getElementById('activityChart').getContext('2d');
            new Chart(ctx, {{
                type: 'bar',
                data: {{
                    labels: {chart_labels},
                    datasets: [{{
                        label: 'Contributions',
                        data: {chart_data},
                        backgroundColor: '#36a2eb',
                        borderRadius: 5
                    }}]
                }},
                options: {{ 
                    maintainAspectRatio: false,
                    scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }} 
                }}
            }});
        </script>
    </body>
    </html>
    """
    return html

# Initialize DB immediately for Render
init_db()

if __name__ == '__main__':
    app.run(port=5000, debug=True)