import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import re
import uuid 
import psycopg2
import sys
import html 
import traceback

# Initialize thread pool
executor = ThreadPoolExecutor(max_workers=5) 

# 1. Load Environment Variables
load_dotenv()

# 2. Initialize Flask App
app = Flask(__name__)

# --- CONFIGURATION ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") 
TELEGRAM_BOT_TOKEN_FOR_COMMANDS = os.getenv("TELEGRAM_BOT_TOKEN_FOR_COMMANDS")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = 'gemini-2.5-pro' 
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# --- SYSTEM SETUP ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# --- DATABASE CONNECTION ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- DATABASE INIT ---
def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS updates (
                id SERIAL PRIMARY KEY,
                author TEXT,
                summary TEXT,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhooks (
                secret_key TEXT PRIMARY KEY,
                chat_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        print("✅ PostgreSQL tables initialized!")
    except Exception as e:
        print(f"❌ Error during DB initialization: {e}")
    finally:
        conn.close()

# --- DATABASE OPERATIONS ---
def save_to_db(author, summary):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("INSERT INTO updates (author, summary, timestamp) VALUES (%s, %s, NOW())", 
                  (author, summary)) 
        conn.commit()
    except Exception as e:
        print(f"❌ Error saving to updates table: {e}")
    finally:
        conn.close()

def save_webhook_config(chat_id, secret_key):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        # Robust UPSERT for Postgres
        c.execute("""
            INSERT INTO webhooks (secret_key, chat_id) 
            VALUES (%s, %s)
            ON CONFLICT (chat_id) 
            DO UPDATE SET secret_key = EXCLUDED.secret_key
        """, (secret_key, str(chat_id))) 
        conn.commit()
        print(f"🔑 Saved/Updated webhook config for chat {chat_id}.")
    except Exception as e:
        print(f"❌ Error saving webhook config: {e}")
    finally:
        conn.close()

def get_chat_id_from_secret(secret_key):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT chat_id FROM webhooks WHERE secret_key = %s", (secret_key,))
        result = c.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ Error retrieving chat ID: {e}")
        return None
    finally:
        conn.close()

# --- AI & TELEGRAM FUNCTIONS ---
def generate_ai_analysis(commit_data, files_changed):
    commit_msg = commit_data.get('message', 'No message.')
    input_text = f"COMMIT MESSAGE: {commit_msg}\nFILES CHANGED: {', '.join(files_changed)}"

    # STRICT PROMPT: Forbids list tags and asks for text bullets
    prompt = f"""
    You are an AI Code Reviewer. Analyze this commit data.
    COMMIT DATA: {input_text}

    INSTRUCTIONS:
    1. Return valid HTML ONLY.
    2. Telegram does NOT support <ul>, <ol>, or <li> tags. DO NOT USE THEM.
    3. Use the text character "•" for bullet points.
    4. Use <br> or newlines for line breaks.
    5. Use <b> for bold, <i> for italic, <code> for code.

    OUTPUT FORMAT:
    <b>Review Status:</b> [Status]
    <b>Summary:</b> [One sentence summary]
    <b>Technical Context:</b> [List files using • bullet points]
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI Analysis Failed: {e}"

def send_to_telegram(text, author, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id: return
    try:
        # CLEANUP: Remove markdown code blocks
        clean_text = text.replace("```html", "").replace("```", "")
        
        # CRITICAL FIX: Sanitize HTML for Telegram
        # 1. Replace <br> with newlines
        clean_text = clean_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        
        # 2. Strip unsupported list tags if AI ignored instructions
        clean_text = clean_text.replace("<ul>", "").replace("</ul>", "")
        clean_text = clean_text.replace("<ol>", "").replace("</ol>", "")
        clean_text = clean_text.replace("<li>", "• ").replace("</li>", "\n")
        
        # 3. Strip paragraph tags
        clean_text = clean_text.replace("<p>", "").replace("</p>", "\n\n")
        
        # Time calculation for header (IST)
        now_utc = datetime.utcnow()
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_time = now_utc + IST_OFFSET
        display_timestamp = ist_time.strftime('%Y-%m-%d %I:%M %p')
        
        header = f"👤 <b>{html.escape(author)}</b> | 🕒 {display_timestamp}"
        message_text = f"{header}\n\n{clean_text}"

        payload = {
            "chat_id": target_chat_id,
            "text": message_text,
            "parse_mode": "HTML" 
        }
        
        url = TELEGRAM_API_URL.format(token=target_bot_token)
        r = requests.post(url, json=payload)
        
        if r.status_code != 200:
             print(f"❌ Telegram Delivery FAILED. Status: {r.status_code}. Response: {r.text}")
        else:
             print(f"✅ Message delivered to {target_chat_id}")
    except Exception as e:
        print(f"❌ Error sending to Telegram: {e}")

# --- WEBHOOK PROCESSOR ---
def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    try:
        all_updates = []
        commits = data.get('commits', [])
        
        # Handle case where commits might be empty or single head_commit
        if not commits and 'head_commit' in data:
            commits = [data['head_commit']]
            
        for commit in commits:
            # Safe get for files
            files_changed = commit.get('added', []) + commit.get('removed', []) + commit.get('modified', [])
            ai_response = generate_ai_analysis(commit, files_changed)
            
            summary = ai_response.strip()
            commit_id = commit.get('id', 'unknown')[:7]
            all_updates.append(f"<b>Commit:</b> <code>{commit_id}</code>\n{summary}")
            
            save_to_db(author_name, summary)
                
        if all_updates:
            final_report = "\n\n----------------\n\n".join(all_updates)
            send_to_telegram(final_report, author_name, target_bot_token, target_chat_id)
            
        print(f"✅ BACKGROUND TASK COMPLETE for {author_name}")
    except Exception as e:
        print(f"❌ CRITICAL TASK ERROR: {e}")
        traceback.print_exc()

# --- ROUTES ---

@app.route('/', methods=['GET'])
def home():
    return redirect('/dashboard')

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    secret_key = request.args.get('secret_key') 
    target_chat_id = request.args.get('chat_id') 

    validated_chat_id = get_chat_id_from_secret(secret_key)

    if not secret_key or not target_chat_id or str(validated_chat_id) != str(target_chat_id):
        print(f"❌ Auth Failed. URL Chat: {target_chat_id} vs DB Chat: {validated_chat_id}")
        return jsonify({"status": "error", "message": "Invalid secret_key or chat_id."}), 401 

    author_name = "Unknown"
    if 'pusher' in data:
        author_name = data['pusher']['name']
    elif 'sender' in data:
        author_name = data['sender']['login']

    executor.submit(process_standup_task, TELEGRAM_BOT_TOKEN_FOR_COMMANDS, target_chat_id, author_name, data)
        
    return jsonify({"status": "processing", "message": "Accepted"}), 200

@app.route('/telegram_commands', methods=['POST'])
def telegram_commands():
    update = request.json
    
    BOT_TOKEN = TELEGRAM_BOT_TOKEN_FOR_COMMANDS
    APP_BASE_URL_USED = APP_BASE_URL

    if 'message' in update:
        message = update['message']
        message_text = message.get('text', '')
        chat_id = message['chat']['id']
        
        # --- MODIFICATION START: /start command handler ---
        if message_text.startswith('/start'):
            guide_text = (
                "👋 <b>Welcome to GitSync!</b>\n\n"
                "Add me to your Telegram organization group so that I can generate a webhook according to your organization, "
                "and I can guide you further.\n\n"
                "After adding me in your Telegram group, just provide:\n"
                "🔹 <code>/gitsync</code> to get your unique webhook URL\n"
                "🔹 <code>/dashboard</code> for tracking performance"
            )
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": guide_text, "parse_mode": "HTML"})
        # --- MODIFICATION END ---

        elif message_text.startswith('/gitsync'):
            new_key = str(uuid.uuid4())
            save_webhook_config(chat_id, new_key)
            webhook_url = f"{APP_BASE_URL_USED}/webhook?secret_key={new_key}&chat_id={chat_id}"
            
            response_text = (
                "👋 <b>GitSync Setup Guide</b>\n\n"
                "1. Copy your unique Webhook URL (The token is hidden!):\n"
                f"<code>{webhook_url}</code>\n\n"
                "2. Paste this URL into your GitHub repository settings under Webhooks.\n"
                "(Content Type: <code>application/json</code>, Events: <code>Just the push event</code>.)\n\n"
                "I will now start analyzing your commits!"
            )
            
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})
            
        elif message_text.startswith('/dashboard'):
            dashboard_url = f"{APP_BASE_URL_USED}/dashboard"
            response_text = f"📊 <b>Team Dashboard</b>\n\nView analytics here:\n<a href='{dashboard_url}'>Open Dashboard</a>"
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})

    return jsonify({"status": "ok"}), 200

# --- 📊 DASHBOARD ROUTE ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    conn = get_db_connection()
    if not conn: return "Database Error", 500
    
    try:
        c = conn.cursor()
        c.execute("SELECT author, summary, timestamp FROM updates ORDER BY id DESC")
        all_updates = c.fetchall()
        
        c.execute("SELECT author, COUNT(*) FROM updates GROUP BY author")
        stats = c.fetchall()
    except Exception as e:
        return f"Query Error: {e}", 500
    finally:
        conn.close()

    # --- TIMEZONE FIX (Convert UTC to IST) ---
    now_utc = datetime.utcnow()
    IST_OFFSET = timedelta(hours=5, minutes=30)
    
    today_str = (now_utc + IST_OFFSET).strftime('%Y-%m-%d')
    yesterday_str = (now_utc + IST_OFFSET - timedelta(days=1)).strftime('%Y-%m-%d')
    
    today_logs = []
    yesterday_logs = []
    week_logs = []
    
    for u in all_updates:
        db_utc_time = u[2]
        ist_time = db_utc_time + IST_OFFSET
        
        db_date_str = ist_time.strftime('%Y-%m-%d')
        display_timestamp = ist_time.strftime('%Y-%m-%d %I:%M %p')
        
        summary_html = u[1].replace('\n', '<br>')
            
        item_html = f'<div class="update-item"><div class="meta">👤 {u[0]} | 🕒 {display_timestamp}</div><pre>{summary_html}</pre></div>'
        
        if db_date_str == today_str:
            today_logs.append(item_html)
        elif db_date_str == yesterday_str:
            yesterday_logs.append(item_html)
        else:
            week_logs.append(item_html)

    chart_labels = [row[0] for row in stats]
    chart_data = [row[1] for row in stats]
    
    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>GitSync Team Analytics</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 20px; color: #333; }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #2c3e50; margin-bottom: 30px; }}
            .card {{ background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-bottom: 25px; }}
            .section-header {{ background: #e9ecef; padding: 8px 15px; border-radius: 6px; margin: 15px 0 10px; font-weight: bold; color: #555; }}
            .update-item {{ background: #fff; border-left: 4px solid #007bff; padding: 15px; margin-bottom: 10px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .meta {{ color: #888; font-size: 0.85em; margin-bottom: 8px; font-weight: 600; text-transform: uppercase; }}
            pre {{ white-space: pre-wrap; font-family: 'Segoe UI', sans-serif; color: #333; margin: 0; line-height: 1.5; }}
            .empty-msg {{ color: #999; font-style: italic; padding: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 GitSync Analytics</h1>
            <div class="card">
                <h2>🏆 Team Velocity</h2>
                <div style="height: 200px;"><canvas id="activityChart"></canvas></div>
            </div>
            <div class="card">
                <h2>📅 Activity Timeline (IST)</h2>
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
                data: {{ labels: {chart_labels}, datasets: [{{ label: 'Contributions', data: {chart_data}, backgroundColor: '#36a2eb', borderRadius: 5 }}] }},
                options: {{ maintainAspectRatio: false, scales: {{ y: {{ beginAtZero: true, ticks: {{ stepSize: 1 }} }} }} }}
            }});
        </script>
    </body>
    </html>
    """
    return html

# --- SAFE INIT ON STARTUP ---
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(port=5000, debug=True)