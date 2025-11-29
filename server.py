import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import re
import uuid 
import psycopg2 # ADDED: PostgreSQL driver
import sys

# Initialize a thread pool executor globally (Offloads slow AI tasks)
executor = ThreadPoolExecutor(max_workers=5) 

# 1. Load Environment Variables
load_dotenv()

# 2. Initialize Flask App
app = Flask(__name__)

# --- CONFIGURATION ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") 
TELEGRAM_BOT_TOKEN_FOR_COMMANDS = os.getenv("TELEGRAM_BOT_TOKEN_FOR_COMMANDS")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL") # NEW: PostgreSQL connection string
MODEL_NAME = 'gemini-2.5-pro' 
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# --- SYSTEM SETUP ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    print("❌ WARNING: GOOGLE_API_KEY not found. AI features will fail.")

if not DATABASE_URL:
    print("❌ CRITICAL ERROR: DATABASE_URL not set. Database functions will fail.")
    # Exit if critical environment variables are missing on startup
    # sys.exit(1) 

# --- DATABASE CONNECTION HELPER (POSTGRES) ---
def get_db_connection():
    """Establishes a connection to the PostgreSQL database."""
    try:
        # Connect using the single DATABASE_URL environment variable
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        raise ConnectionError("Failed to connect to PostgreSQL database.") from e

# --- 📦 DATABASE SETUP (POSTGRES FUNCTIONS) ---

def init_db():
    """Creates the tables in the PostgreSQL database."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # 1. updates (Standup Logs)
        c.execute('''
            CREATE TABLE IF NOT EXISTS updates (
                id SERIAL PRIMARY KEY,
                author TEXT,
                summary TEXT,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 2. webhooks (Secure Maintenance/Secret Keys)
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhooks (
                secret_key TEXT PRIMARY KEY,
                chat_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        conn.close()
        print("✅ PostgreSQL tables initialized!")
    except ConnectionError:
        print("⚠️ Database initialization skipped due to connection failure.")
    except Exception as e:
        print(f"❌ Error during DB initialization: {e}")

def save_to_db(author, summary):
    """Saves the standup to the logs table."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # %s is the placeholder syntax for psycopg2/Postgres
        c.execute("INSERT INTO updates (author, summary, timestamp) VALUES (%s, %s, NOW())", 
                  (author, summary)) 
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"❌ Error saving to updates table: {e}")

def save_webhook_config(chat_id, secret_key):
    """Saves the secret key/chat ID mapping to the webhooks table (Postgres UPSERT)."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Use ON CONFLICT (UPSERT) for Postgres
        c.execute("""
            INSERT INTO webhooks (secret_key, chat_id) 
            VALUES (%s, %s)
            ON CONFLICT (chat_id) 
            DO UPDATE SET secret_key = EXCLUDED.secret_key
        """, (secret_key, chat_id)) 
        conn.commit()
        conn.close()
        print(f"🔑 Saved new webhook config for chat {chat_id}.")
    except Exception as e:
        print(f"❌ Error saving webhook config: {e}")

def get_chat_id_from_secret(secret_key):
    """Retrieves the chat ID from the webhooks table."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT chat_id FROM webhooks WHERE secret_key = %s", (secret_key,))
        result = c.fetchone()
        conn.close()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ Error retrieving chat ID: {e}")
        return None

# --- HELPER FUNCTIONS (Rest of the functions are omitted for brevity, they are unchanged) ---
def parse_tag(text, tag_name):
    # ...
    start_tag = f"<{tag_name}>"
    end_tag = f"</{tag_name}>"
    start = text.find(start_tag)
    end = text.find(end_tag)
    if start != -1 and end != -1:
        return text[start + len(start_tag):end].strip()
    return None

def escape_markdown_v2(text):
    # ...
    escape_chars = r'[]()~`>#+-=|{}.!' 
    text = re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)
    return text

def send_simple_message(token, chat_id, text):
    # ...
    if not token or not chat_id: return
    try:
        url = TELEGRAM_API_URL.format(token=token)
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "Markdown" 
        }
        r = requests.post(url, json=payload)
        
        if r.status_code != 200:
             print(f"❌ Telegram Reply FAILED. Status: {r.status_code}. Response: {r.text}")
    except Exception as e:
        print(f"❌ Error sending Telegram reply: {e}")

# --- AI & TELEGRAM FUNCTIONS (Unchanged from previous versions) ---
def generate_ai_analysis(commit_data, files_changed):
    # ... (function body remains the same)
    commit_msg = commit_data.get('message', 'No message.')
    input_text = f"COMMIT MESSAGE: {commit_msg}\nFILES CHANGED: {', '.join(files_changed)}"

    prompt = f"""
    You are an AI Code Reviewer and Agile Assistant. 
    Analyze the following commit data and generate a structured standup update.
    
    COMMIT DATA: {input_text}

    OUTPUT FORMAT:
    
    <STANDUP_UPDATE>
    * **Review Status:** (e.g., ✅ LGTM, ⚠️ Needs attention, ❌ Critical Issues. Choose one based on the nature of the change.)
    * **Summary of Work:** (1-2 bullet points explaining the feature/bug fix.)
    * **Technical Context:** (List the main files/libraries touched.)
    * **Potential Risks/Blockers:** (1-2 bullet points on potential issues. If none, write 'None.')
    </STANDUP_UPDATE>
    """
    try:
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"AI Analysis Failed: {e}"

def send_to_telegram(text, author, target_bot_token, target_chat_id):
    # ... (function body remains the same)
    if not target_bot_token or not target_chat_id: return
    try:
        header = f"🚀 *GitSync Standup: {escape_markdown_v2(author)}*"
        escaped_summary = escape_markdown_v2(text)
        escaped_summary = escaped_summary.replace("\\*", "*").replace("\\_", "_")
        message_text = f"{header}\n\n{escaped_summary}"

        payload = {
            "chat_id": target_chat_id,
            "text": message_text,
            "parse_mode": "MarkdownV2"
        }
        
        url = TELEGRAM_API_URL.format(token=target_bot_token)
        r = requests.post(url, json=payload)
        
        if r.status_code != 200:
             print(f"❌ Telegram Delivery FAILED. Status: {r.status_code}. Response: {r.text}")
    except Exception as e:
        print(f"❌ Error sending to Telegram: {e}")

# --- ⚙️ BACKGROUND EXECUTION FUNCTION (Unchanged) ---
def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    # ... (function body remains the same)
    all_standup_updates = []
    
    for commit in data.get('commits', []):
        commit_id = commit['id']
        files_changed = commit['added'] + commit['removed'] + commit['modified']
        
        ai_response = generate_ai_analysis(commit, files_changed)
        standup_summary = parse_tag(ai_response, "STANDUP_UPDATE")
        
        if standup_summary:
            standup_entry = f"*Commit ID:* `{commit_id[:7]}`\n\n{standup_summary}"
            all_standup_updates.append(standup_entry)
            save_to_db(author_name, standup_summary) 
        else:
            print(f"Skipping commit {commit_id}: Failed to parse AI STANDUP_UPDATE.")
            
    if all_standup_updates:
        full_report = f"Daily Report for *{author_name}* (Total {len(all_standup_updates)} Commits):\n\n" + "\n---\n".join(all_standup_updates)
        send_to_telegram(full_report, author_name, target_bot_token, target_chat_id)
        
    print(f"✅ BACKGROUND TASK COMPLETE for {author_name}")

# --- ROUTES (Logic unchanged, now relies on Postgres functions) ---

@app.route('/', methods=['GET'])
def home():
    return redirect('/dashboard')

@app.route('/webhook', methods=['POST'])
def git_webhook():
    # ...
    data = request.json
    secret_key = request.args.get('secret_key') 
    target_chat_id = request.args.get('chat_id') 

    validated_chat_id = get_chat_id_from_secret(secret_key) 

    if not secret_key or not target_chat_id or validated_chat_id != target_chat_id:
        print(f"❌ Webhook authentication failed. Secret Key: {secret_key}, Target Chat: {target_chat_id}")
        return jsonify({
            "status": "error", 
            "message": "Webhook authentication failed. Invalid secret_key or chat_id."
        }), 401 

    target_bot_token = TELEGRAM_BOT_TOKEN_FOR_COMMANDS 

    if data and 'pusher' in data and 'commits' in data:
        author_name = data['pusher']['name']
        executor.submit(
            process_standup_task,
            target_bot_token,
            target_chat_id,
            author_name,
            data
        )
        
    return jsonify({"status": "processing_in_background", "message": "Webhook accepted by secure processor"}), 200

@app.route('/telegram_commands', methods=['POST'])
def telegram_commands():
    # ...
    update = request.json
    
    BOT_TOKEN = TELEGRAM_BOT_TOKEN_FOR_COMMANDS
    APP_BASE_URL_USED = APP_BASE_URL

    if not BOT_TOKEN or not APP_BASE_URL_USED:
        return jsonify({"status": "error", "message": "Bot not configured"}), 500


    if 'message' in update:
        message = update['message']
        message_text = message.get('text', '')
        chat_id = message['chat']['id']
        
        if message_text.startswith('/gitsync'):
            
            secret_key = str(uuid.uuid4())
            save_webhook_config(chat_id, secret_key) 
            
            webhook_url = f"{APP_BASE_URL_USED}/webhook?secret_key={secret_key}&chat_id={chat_id}"
            
            response_text = f"""
👋 **GitSync Setup Guide**

*1. Copy your unique Webhook URL (The token is hidden!):*
`{webhook_url}`

*2. Paste this URL into your GitHub repository settings under Webhooks.*
(Content Type: `application/json`, Events: `Just the push event`.)

I will now start analyzing your commits!
"""
            send_simple_message(BOT_TOKEN, chat_id, response_text)
            
        elif message_text.startswith('/dashboard'):
            
            dashboard_url = f"{APP_BASE_URL_USED}/dashboard"
            
            response_text = f"""
📊 **GitSync Team Analytics Dashboard**

View the historical performance and standup log for the entire team here:
[Open Dashboard]({dashboard_url})

_Note: This is a public URL. Please share responsibly._
"""
            send_simple_message(BOT_TOKEN, chat_id, response_text)

    return jsonify({"status": "ok"}), 200


# --- 📊 ADVANCED DASHBOARD SECTION ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    """Reads logs from the PostgreSQL database."""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        
        # Use simple select statements for Postgres
        c.execute("SELECT author, summary, timestamp FROM updates ORDER BY id DESC")
        all_updates = c.fetchall()
        
        c.execute("SELECT author, COUNT(*) FROM updates GROUP BY author")
        stats = c.fetchall()
        conn.close()
    except Exception as e:
        # Handle case where DB is unavailable
        return f"<h1>Database Connection Error</h1><p>Could not connect to PostgreSQL: {e}</p>", 500


    # --- TIME SORTING LOGIC ---
    now_utc = datetime.utcnow()
    # Note: Psycopg2 returns datetime objects, so direct comparison is possible,
    # but we will stick to string comparison for simplicity with the dashboard HTML.
    today_str = now_utc.strftime('%Y-%m-%d')
    yesterday_str = (now_utc - timedelta(days=1)).strftime('%Y-%m-%d')
    
    today_logs = []
    yesterday_logs = []
    week_logs = []
    
    for u in all_updates:
        # u[2] is now a datetime object from psycopg2
        db_date_str = u[2].strftime('%Y-%m-%d')
            
        # Format the timestamp for display
        display_timestamp = u[2].strftime('%Y-%m-%d %H:%M:%S UTC')
        summary_html = u[1].replace('\n', '<br>')
            
        item_html = f'<div class="update-item"><div class="meta">👤 {u[0]} | 🕒 {display_timestamp}</div><pre>{summary_html}</pre></div>'
        
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

# Initialize DBs immediately
init_db()

if __name__ == '__main__':
    # Use Gunicorn in Azure for production, but Flask's built-in server for local debug
    app.run(port=5000, debug=True)