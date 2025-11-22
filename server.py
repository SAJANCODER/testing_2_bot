import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
import sqlite3
from datetime import datetime

# 1. Load Environment Variables
load_dotenv()

# 2. Initialize Flask App
app = Flask(__name__)

# --- CONFIGURATION ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") 
# This is the Default Channel (Fallback)
DEFAULT_CLIQ_WEBHOOK_URL = os.getenv("CLIQ_WEBHOOK_URL")

# --- SYSTEM SETUP ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
    model = genai.GenerativeModel('gemini-2.5-pro')
else:
    print("❌ WARNING: GOOGLE_API_KEY not found.")

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
    """Saves the standup to the database."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO updates (author, summary) VALUES (?, ?)", (author, summary))
    conn.commit()
    conn.close()
    print(f"💾 Saved entry for {author} to database.")

# --- AI FUNCTION ---
def generate_standup_summary(commits):
    prompt = f"""
    You are an Agile Scrum Assistant. 
    Analyze these git commit messages and convert them into a daily standup update.
    
    COMMITS:
    {commits}
    
    OUTPUT FORMAT:
    * **Completed:** (Summarize the work done in 1-2 clear bullet points)
    * **Technical Context:** (Briefly mention libraries/files touched if obvious)
    * **Potential Blockers:** (If the commits mention 'fix', 'error', or 'debug', note that a bug was resolved. Otherwise say 'None')
    
    Keep it concise, professional, and ready to post to a team chat.
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI summary: {e}"

# --- CLIQ SEND FUNCTION (UPDATED FOR DYNAMIC ROUTING) ---
def send_to_cliq(text, author, target_webhook_url):
    """
    Sends the formatted AI summary to the SPECIFIED Zoho Cliq URL.
    """
    if not target_webhook_url:
        print("❌ Error: No Webhook URL provided.")
        return

    try:
        payload = {
            "text": f"### 🚀 GitSync Standup: {author}",
            "bot": { "name": "GitSync Bot", "image": "https://cdn-icons-png.flaticon.com/512/4712/4712109.png" },
            "card": { "title": f"Daily Update: {author}", "theme": "modern-inline" },
            "slides": [ { "type": "text", "data": text } ]
        }
        
        # Send to the DYNAMIC URL passed to this function
        r = requests.post(target_webhook_url, json=payload)
        
        if r.status_code == 200 or r.status_code == 204:
            print(f"📨 Sent to Cliq Channel: Success (Status {r.status_code})")
        else:
            print(f"❌ Cliq Error: {r.status_code} - {r.text}")
            
    except Exception as e:
        print(f"❌ Error sending to Cliq: {e}")

# --- ROUTES ---

@app.route('/', methods=['GET'])
def home():
    print("👋 Zoho checked the root connection. Redirecting...")
    return redirect('/dashboard')

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    
    # --- 🔀 DYNAMIC ROUTING LOGIC ---
    # Check if the URL has ?channel=NAME&token=KEY
    # --- 🔀 DYNAMIC ROUTING LOGIC (FIXED) ---
    # --- 🔀 DYNAMIC ROUTING LOGIC (UPDATED) ---
    dynamic_channel = request.args.get('channel')
    dynamic_token = request.args.get('token')
    dynamic_oid = request.args.get('oid') # <--- NEW: Get the Org ID
    
    # Decide which URL to use
    if dynamic_channel and dynamic_token:
        print(f"🔀 Routing to Custom Team Channel: {dynamic_channel}")
        
        # Use the provided OID, or fallback to yours if missing (Safety net)
        # Replace '906264961' below with your own ID as the default backup
        company_id = dynamic_oid if dynamic_oid else "906264961"
        
        target_url = f"https://cliq.zoho.com/company/{company_id}/api/v2/channelsbyname/{dynamic_channel}/message?zapikey={dynamic_token}"
    else:
        print("⬇️ Using Default Server Channel")
        target_url = DEFAULT_CLIQ_WEBHOOK_URL

    # Process the Commit
    if data and 'commits' in data:
        author_name = data['pusher']['name']
        commit_messages = [commit['message'] for commit in data['commits']]
        full_raw_update = "\n".join(commit_messages)

        print(f"\n🔄 Processing commits from {author_name}...")
        
        # 1. Generate AI Summary
        ai_summary = generate_standup_summary(full_raw_update)
        
        # 2. Send to the Determined Target URL
        send_to_cliq(ai_summary, author_name, target_url)
        
        # 3. Save to Global Analytics DB
        save_to_db(author_name, ai_summary)
        
    return jsonify({"status": "success"}), 200

@app.route('/dashboard', methods=['GET'])
def dashboard():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    # Get Recent Standups
    c.execute("SELECT author, summary, timestamp FROM updates ORDER BY id DESC LIMIT 10")
    recent_updates = c.fetchall()
    
    # Get Stats
    c.execute("SELECT author, COUNT(*) FROM updates GROUP BY author")
    stats = c.fetchall()
    conn.close()

    chart_labels = [row[0] for row in stats]
    chart_data = [row[1] for row in stats]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>GitSync Team Analytics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: sans-serif; background: #f4f6f8; padding: 20px; }}
            .container {{ max-width: 900px; margin: 0 auto; }}
            .card {{ background: white; padding: 20px; border-radius: 10px; box-shadow: 0 2px 5px rgba(0,0,0,0.1); margin-bottom: 20px; }}
            h2 {{ color: #333; border-bottom: 2px solid #eee; padding-bottom: 10px; }}
            .update-item {{ background: #fafafa; border-left: 4px solid #007bff; padding: 10px; margin-bottom: 10px; }}
            .meta {{ color: #666; font-size: 0.85em; margin-bottom: 5px; font-weight: bold; }}
            pre {{ white-space: pre-wrap; font-family: sans-serif; color: #444; margin: 0; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 GitSync Analytics Dashboard</h1>
            <div class="card">
                <h2>🏆 Team Velocity (Commits by Author)</h2>
                <canvas id="activityChart" height="100"></canvas>
            </div>
            <div class="card">
                <h2>📅 Recent Standups</h2>
                {''.join([f'<div class="update-item"><div class="meta">👤 {u[0]} | 🕒 {u[2]}</div><pre>{u[1]}</pre></div>' for u in recent_updates])}
            </div>
        </div>
        <script>
            const ctx = document.getElementById('activityChart').getContext('2d');
            new Chart(ctx, {{
                type: 'bar',
                data: {{
                    labels: {chart_labels},
                    datasets: [{{
                        label: '# of Standups',
                        data: {chart_data},
                        backgroundColor: 'rgba(54, 162, 235, 0.6)',
                        borderColor: 'rgba(54, 162, 235, 1)',
                        borderWidth: 1
                    }}]
                }},
                options: {{ scales: {{ y: {{ beginAtZero: true }} }} }}
            }});
        </script>
    </body>
    </html>
    """
    return html

# 🔴 CRITICAL FOR RENDER: Initialize DB immediately when file loads
init_db()

if __name__ == '__main__':
    app.run(port=5000, debug=True)