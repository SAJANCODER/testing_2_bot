import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
import sqlite3
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
import re # For cleaning/parsing AI output

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
MODEL_NAME = 'gemini-2.5-pro' 
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

# --- SYSTEM SETUP ---
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)
else:
    print("❌ WARNING: GOOGLE_API_KEY not found. AI features will fail.")

if not TELEGRAM_BOT_TOKEN_FOR_COMMANDS or not APP_BASE_URL:
    print("❌ WARNING: TELEGRAM_BOT_TOKEN_FOR_COMMANDS or APP_BASE_URL is missing in .env. Command routes will fail.")

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
    """Saves the standup explicitly using UTC time."""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("INSERT INTO updates (author, summary, timestamp) VALUES (?, ?, ?)", 
              (author, summary, datetime.utcnow())) 
    conn.commit()
    conn.close()
    print(f"💾 Saved entry for {author} to database.")

# --- HELPER FUNCTIONS ---

def parse_tag(text, tag_name):
    """Simple parser to extract content between XML-style tags."""
    start_tag = f"<{tag_name}>"
    end_tag = f"</{tag_name}>"
    start = text.find(start_tag)
    end = text.find(end_tag)
    if start != -1 and end != -1:
        return text[start + len(start_tag):end].strip()
    return None

def escape_markdown_v2(text):
    """Escapes special characters for Telegram's MarkdownV2 format."""
    # Escape characters: _*[]()~`>#+-=|{}.!
    # We escape all except *, _, which are needed for formatting, 
    # and assume the AI output uses them correctly.
    escape_chars = r'[]()~`>#+-=|{}.!' 
    text = re.sub(f"([{re.escape(escape_chars)}])", r"\\\1", text)
    return text

def send_simple_message(token, chat_id, text):
    """Sends a basic, unformatted text reply via Telegram."""
    if not token or not chat_id: return

    try:
        url = TELEGRAM_API_URL.format(token=token)
        # Using HTML parse mode for simple replies to preserve basic Markdown 
        # without complex MarkdownV2 escaping issues for links/quotes.
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

# --- AI & TELEGRAM FUNCTIONS ---

def generate_ai_analysis(commit_data, files_changed):
    """
    Generates a structured review and standup based on commit metadata.
    """
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
    """Sends the summary to the determined Telegram chat using MarkdownV2."""
    if not target_bot_token or not target_chat_id: 
        print("❌ Telegram Delivery FAILED: Token or Chat ID is missing.")
        return
    try:
        # Format for Telegram MarkdownV2
        header = f"🚀 *GitSync Standup: {escape_markdown_v2(author)}*"
        
        # Escape the summary content itself
        escaped_summary = escape_markdown_v2(text)
        
        # Restore bolding/italic added by the AI (which use * and _)
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

# --- ⚙️ BACKGROUND EXECUTION FUNCTION ---
def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    """Handles AI analysis, posting, and saving for multiple commits."""
    all_standup_updates = []
    
    # Iterate through all commits in the push
    for commit in data.get('commits', []):
        commit_id = commit['id']
        
        # 1. Prepare files changed list
        files_changed = commit['added'] + commit['removed'] + commit['modified']
        
        # 2. Generate AI Analysis
        ai_response = generate_ai_analysis(commit, files_changed)
        
        # 3. Parse the Standup Update
        standup_summary = parse_tag(ai_response, "STANDUP_UPDATE")
        
        if standup_summary:
            # Add commit details to the summary for context
            # Use backticks for commit ID in the update
            standup_entry = f"*Commit ID:* `{commit_id[:7]}`\n\n{standup_summary}"
            all_standup_updates.append(standup_entry)
            
            # 4. Save to DB
            save_to_db(author_name, standup_summary)
        else:
            print(f"Skipping commit {commit_id}: Failed to parse AI STANDUP_UPDATE.")
            
    # 5. Send all aggregated summaries to Telegram
    if all_standup_updates:
        # Aggregate all updates into one report for Telegram
        full_report = f"Daily Report for *{author_name}* (Total {len(all_standup_updates)} Commits):\n\n" + "\n---\n".join(all_standup_updates)
        send_to_telegram(full_report, author_name, target_bot_token, target_chat_id)
        
    print(f"✅ BACKGROUND TASK COMPLETE for {author_name}")

# --- ROUTES ---

@app.route('/', methods=['GET'])
def home():
    return redirect('/dashboard')

@app.route('/webhook', methods=['POST'])
def git_webhook():
    """Handles POST requests from GitHub webhooks."""
    data = request.json
    
    # Dynamic Routing for Telegram
    target_bot_token = request.args.get('token')
    target_chat_id = request.args.get('chat_id')

    # Ensure required parameters are present for Telegram
    if not target_bot_token or not target_chat_id:
        return jsonify({
            "status": "error", 
            "message": "Missing 'token' or 'chat_id' query parameters for Telegram integration. Please check your GitHub webhook URL setup."
        }), 400

    if data and 'pusher' in data and 'commits' in data:
        author_name = data['pusher']['name']

        print(f"\n🔄 Processing {len(data['commits'])} commits from {author_name}...")
        
        # Submit the slow work to the thread pool and return IMMEDIATELY
        executor.submit(
            process_standup_task,
            target_bot_token,
            target_chat_id,
            author_name,
            data
        )
        
    # Must return 200 OK immediately to prevent GitHub timeout
    return jsonify({"status": "processing_in_background", "message": "Webhook accepted by Telegram processor"}), 200

@app.route('/telegram_commands', methods=['POST'])
def telegram_commands():
    """Handles commands like /gitsync and /dashboard sent via the Telegram Bot API."""
    update = request.json
    
    BOT_TOKEN = TELEGRAM_BOT_TOKEN_FOR_COMMANDS
    APP_BASE_URL_USED = APP_BASE_URL # Use the configured variable

    if not BOT_TOKEN or not APP_BASE_URL_USED:
        print("Telegram command handler is not configured correctly.")
        return jsonify({"status": "error", "message": "Bot not configured"}), 500


    if 'message' in update:
        message = update['message']
        message_text = message.get('text', '')
        chat_id = message['chat']['id']
        
        if message_text.startswith('/gitsync'):
            
            webhook_url = f"{APP_BASE_URL_USED}/webhook?token={BOT_TOKEN}&chat_id={chat_id}"
            
            response_text = f"""
👋 **GitSync Setup Guide**

*1. Copy your unique Webhook URL:*
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


# --- 📊 ADVANCED DASHBOARD SECTION (Remains the same) ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    c.execute("SELECT author, summary, timestamp FROM updates ORDER BY id DESC")
    all_updates = c.fetchall()
    
    c.execute("SELECT author, COUNT(*) FROM updates GROUP BY author")
    stats = c.fetchall()
    conn.close()

    # --- TIME SORTING LOGIC ---
    now_utc = datetime.utcnow()
    today_str = now_utc.strftime('%Y-%m-%d')
    yesterday_str = (now_utc - timedelta(days=1)).strftime('%Y-%m-%d')
    
    today_logs = []
    yesterday_logs = []
    week_logs = []
    
    for u in all_updates:
        try:
            db_date_str = u[2].split(' ')[0]
        except:
            db_date_str = ""
            
        summary_html = u[1].replace('\n', '<br>')
            
        item_html = f'<div class="update-item"><div class="meta">👤 {u[0]} | 🕒 {u[2]}</div><pre>{summary_html}</pre></div>'
        
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

# Initialize DB immediately
init_db()

if __name__ == '__main__':
    app.run(port=5000, debug=True)