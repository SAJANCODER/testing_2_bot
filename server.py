import google.generativeai as genai
from flask import Flask, request, jsonify, redirect, current_app
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
import json 
from maintenance import maintenance_middleware, register_admin_routes, is_maintenance_enabled 

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
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN") 
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
        current_app.logger.error(f"❌ DATABASE CONNECTION ERROR: {e}", exc_info=True)
        return None

# --- DATABASE INIT ---
def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        
        # 1. project_updates 
        c.execute('''
            CREATE TABLE IF NOT EXISTS project_updates (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                author TEXT,
                repo_name TEXT,
                branch_name TEXT,
                summary TEXT,
                files_changed INTEGER DEFAULT 0,
                insertions INTEGER DEFAULT 0,
                files_added INTEGER DEFAULT 0,
                files_modified INTEGER DEFAULT 0,
                files_removed INTEGER DEFAULT 0,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 2. webhooks 
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhooks (
                secret_key TEXT PRIMARY KEY,
                chat_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # 3. pending_commits
        c.execute('''
            CREATE TABLE IF NOT EXISTS pending_commits (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                author TEXT,
                repo_name TEXT,
                branch_name TEXT,
                commit_data TEXT, 
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # 4. AUTO-MIGRATION (Adding columns for stats)
        try:
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS insertions INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_added INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_modified INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_removed INTEGER DEFAULT 0;")
        except Exception as e:
            current_app.logger.warning(f"⚠️ Migration notice: {e}")

        conn.commit()
        current_app.logger.info("✅ PostgreSQL tables initialized & migrated!")
    except Exception as e:
        current_app.logger.error(f"❌ Error during DB initialization: {e}", exc_info=True)
    finally:
        conn.close()

# --- DATABASE OPERATIONS ---
def save_to_db(chat_id, author, repo_name, branch_name, summary, added, modified, removed, insertions):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        total_files = added + modified + removed
        
        c.execute("""
            INSERT INTO project_updates 
            (chat_id, author, repo_name, branch_name, summary, files_changed, files_added, files_modified, files_removed, insertions, timestamp) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (str(chat_id), author, repo_name, branch_name, summary, total_files, added, modified, removed, insertions)) 
        conn.commit()
    except Exception as e:
        current_app.logger.error(f"❌ Error saving to updates table: {e}")
    finally:
        conn.close()

def save_webhook_config(chat_id, secret_key):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO webhooks (secret_key, chat_id) 
            VALUES (%s, %s)
            ON CONFLICT (chat_id) 
            DO UPDATE SET secret_key = EXCLUDED.secret_key
        """, (secret_key, str(chat_id))) 
        conn.commit()
        current_app.logger.info(f"🔑 Saved/Updated webhook config for chat {chat_id}.")
    except Exception as e:
        current_app.logger.error(f"❌ Error saving webhook config: {e}")
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
        current_app.logger.error(f"❌ Error retrieving chat ID: {e}")
        return None
    finally:
        conn.close()

def get_secret_from_chat_id(chat_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT secret_key FROM webhooks WHERE chat_id = %s", (str(chat_id),))
        result = c.fetchone()
        return result[0] if result else None
    except Exception as e:
        current_app.logger.error(f"❌ Error retrieving secret key: {e}")
        return None
    finally:
        conn.close()

# ----------------- PENDING COMMITS HELPERS -----------------
def enqueue_pending_commit(chat_id, author, repo_full_name, branch_ref, commit_data_json):
    conn = get_db_connection()
    if not conn:
        current_app.logger.error("❌ DB unavailable, cannot enqueue pending commit")
        return
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO pending_commits (chat_id, author, repo_name, branch_name, commit_data)
            VALUES (%s, %s, %s, %s, %s)
        """, (str(chat_id), author, repo_full_name, branch_ref, commit_data_json))
        conn.commit()
        current_app.logger.info(f"🔄 Commit queued for {author} during maintenance.")
    except Exception as e:
        current_app.logger.error("❌ Pending commit enqueue error:", e)
    finally:
        conn.close()

def fetch_all_pending_commits(app, chat_id=None, limit=100):
    conn = get_db_connection()
    if not conn:
        return []
    try:
        c = conn.cursor()
        if chat_id:
            c.execute("""
                SELECT id, chat_id, author, repo_name, branch_name, commit_data
                FROM pending_commits
                WHERE chat_id=%s
                ORDER BY id ASC
                LIMIT %s
            """, (str(chat_id), limit))
        else:
            c.execute("""
                SELECT id, chat_id, author, repo_name, branch_name, commit_data
                FROM pending_commits
                ORDER BY id ASC
                LIMIT %s
            """, (limit,))
        rows = c.fetchall()
        return rows
    except Exception as e:
        app.logger.error(f"❌ Pending fetch error: {e}")
        return []
    finally:
        conn.close()

def delete_pending_commits_by_ids(ids):
    if not ids: return
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("DELETE FROM pending_commits WHERE id IN %s", (tuple(ids),))
        conn.commit()
        current_app.logger.info(f"🗑️ Deleted {len(ids)} pending commits.")
    except Exception as e:
        current_app.logger.error("❌ Pending delete error:", e)
    finally:
        conn.close()

# --- GITHUB API HELPER ---
def get_commit_stats(repo_full_name, commit_sha):
    if not GITHUB_TOKEN:
        return 0, 0, 0, 0, []
        
    url = f"https://api.github.com/repos/{repo_full_name}/commits/{commit_sha}"
    headers = {"Authorization": f"token {GITHUB_TOKEN}", "Accept": "application/vnd.github.v3+json"}
    
    try:
        r = requests.get(url, headers=headers)
        if r.status_code == 200:
            data = r.json()
            stats = data.get('stats', {})
            files = data.get('files', [])
            
            insertions = stats.get('additions', 0)
            
            added_count = len([f for f in files if f.get('status') == 'added'])
            removed_count = len([f for f in files if f.get('status') == 'removed'])
            modified_count = len([f for f in files if f.get('status') == 'modified'])
            
            return insertions, added_count, modified_count, removed_count, files
        else:
            current_app.logger.error(f"❌ GitHub API Error: {r.status_code} {r.text}")
    except Exception as e:
        current_app.logger.error(f"❌ Failed to fetch commit details: {e}")
    
    return 0, 0, 0, 0, []

# --- AI & TELEGRAM FUNCTIONS ---
def generate_ai_analysis(commit_msg, file_summary_text):
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
        current_app.logger.error(f"❌ Gemini Analysis Failed: {e}", exc_info=True)
        return f"AI Analysis Failed: {e}"

def send_to_telegram(text, author, repo, branch, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id: return
    try:
        clean_text = text.replace("```html", "").replace("```", "")
        clean_text = clean_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        clean_text = clean_text.replace("<ul>", "").replace("</ul>", "")
        clean_text = clean_text.replace("<ol>", "").replace("</ol>", "")
        clean_text = clean_text.replace("<li>", "• ").replace("</li>", "\n")
        clean_text = clean_text.replace("<p>", "").replace("</p>", "\n\n")
        
        now_utc = datetime.utcnow()
        IST_OFFSET = timedelta(hours=5, minutes=30)
        ist_time = now_utc + IST_OFFSET
        display_timestamp = ist_time.strftime('%I:%M %p')
        
        header = (
            f"👤 <b>{html.escape(author)}</b>\n"
            f"📂 <b>{html.escape(repo)}</b> (<code>{html.escape(branch)}</code>)\n"
            f"🕒 {display_timestamp}"
        )
        message_text = f"{header}\n\n{clean_text}"

        payload = {
            "chat_id": target_chat_id,
            "text": message_text,
            "parse_mode": "HTML" 
        }
        
        url = TELEGRAM_API_URL.format(token=target_bot_token)
        requests.post(url, json=payload)
        
        current_app.logger.info(f"✅ Message delivered to {target_chat_id}")
    except Exception as e:
        current_app.logger.error(f"❌ Error sending to Telegram: {e}", exc_info=True)


# --- PROCESSING LOGIC (Shared between Webhook and Flush) ---
def execute_commit_processing(chat_id, author_name, data):
    """Processes commits: gets stats, runs AI, saves to DB."""
    
    all_updates = []
    commits = data.get('commits', [])
    
    # Extract Repo info from payload
    repo_data = data.get('repository', {})
    repo_full_name = repo_data.get('full_name', 'Unknown/Repo') 
    repo_name = repo_data.get('name', 'Unknown Repo')
    org_name = repo_data.get('organization', {})
    if isinstance(org_name, dict): org_name = org_name.get('login', 'Unknown Org')
    
    display_repo_name = f"{org_name}/{repo_name}" if org_name not in ['Unknown Org', 'Unknown'] else repo_name
    branch_ref = data.get('ref', '')
    branch_name = branch_ref.split('/')[-1] if branch_ref else 'unknown'

    if not commits and 'head_commit' in data:
        commits = [data['head_commit']]
        
    for commit in commits:
        commit_id = commit.get('id', 'unknown')
        commit_sha = commit_id if len(commit_id) > 7 else commit_id 
        commit_msg = commit.get('message', '')
        
        # 1. Fetch Detailed Stats from GitHub (Insertions & File Counts)
        insertions, added_count, modified_count, removed_count, file_details = get_commit_stats(repo_full_name, commit_sha)
        
        # 2. Format File List for AI Input
        file_summary_list = [f"{f['filename']} (+{f['additions']})" for f in file_details]
        ai_input_summary = f"Commit message: {commit_msg}. Files: {len(file_details)}. Added lines: {insertions}. Touched files: {', '.join(file_summary_list)}"
        
        # 3. Generate AI Summary
        ai_response = generate_ai_analysis(commit_msg, ai_input_summary)
        summary_text = ai_response.replace("<b>Summary:</b>", "").strip()

        # 4. Save to DB 
        save_to_db(chat_id, author_name, display_repo_name, branch_name, summary_text, 
                   added_count, modified_count, removed_count, insertions)
        
        # 5. Prepare Output Message
        commit_output = f"<b>Commit:</b> <code>{commit_sha[:7]}</code>\n{summary_text}"
        all_updates.append(commit_output)
        
    return all_updates, display_repo_name, branch_name

def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    """Handles core processing or queues the task if maintenance is active."""
    
    # 1. Check Maintenance Mode
    if is_maintenance_enabled():
        current_app.logger.info(f"🛑 Maintenance ON. Queuing commit for {author_name}.")
        
        # Store the raw commit payload JSON string in the pending table
        enqueue_pending_commit(target_chat_id, author_name, 
                               data.get('repository', {}).get('full_name', 'Unknown'), 
                               data.get('ref', 'unknown'), 
                               json.dumps(data.get('commits', [])))
        return

    # 2. Execute Processing (Maintenance OFF)
    try:
        all_updates, display_repo_name, branch_name = execute_commit_processing(target_chat_id, author_name, data)
        
        if all_updates:
            final_report = "\n\n----------------\n\n".join(all_updates)
            send_to_telegram(final_report, author_name, display_repo_name, branch_name, target_bot_token, target_chat_id)
            
        current_app.logger.info(f"✅ BACKGROUND TASK COMPLETE for {author_name}")
    except Exception as e:
        current_app.logger.error(f"❌ CRITICAL ERROR in worker: {e}", exc_info=True)

# --- FLUSH CALLBACK (Called by admin routes) ---
def flush_pending_callback(app, chat_id=None):
    """Fetches all pending commits and sends them to Telegram."""
    
    pending_rows = fetch_all_pending_commits(app, chat_id=chat_id)
    ids_to_delete = []
    sent_count = 0
    failed_count = 0
    
    for row in pending_rows:
        try:
            # Row structure: id, chat_id, author, repo_name, branch_name, commit_data (JSON string)
            id_, chat_id, author, repo_name, branch_ref, commit_data_str = row
            
            # Reconstruct the commits payload structure required by execute_commit_processing
            commits_list = json.loads(commit_data_str)
            
            data_payload = {
                "commits": commits_list, 
                "repository": {"full_name": repo_name, "name": repo_name.split('/')[-1]}, 
                "ref": branch_ref
            }
            
            # Execute processing logic (AI analysis, DB save, Telegram send)
            all_updates, display_repo_name, branch_name = execute_commit_processing(chat_id, author, data_payload)

            if all_updates:
                final_report = "\n\n----------------\n\n".join(all_updates)
                send_to_telegram(final_report, author, display_repo_name, branch_name, TELEGRAM_BOT_TOKEN_FOR_COMMANDS, chat_id)
                ids_to_delete.append(id_)
                sent_count += 1
            else:
                ids_to_delete.append(id_) 
                
        except Exception as e:
            app.logger.error(f"❌ Failed to process pending commit ID {id_}: {e}")
            failed_count += 1
            
    # Clean up successfully processed/empty commits
    delete_pending_commits_by_ids(ids_to_delete)
    
    return sent_count, failed_count, "Flush successful."

# --- ROUTES ---

@app.route('/', methods=['GET'])
def home():
    return "GitSync Bot Active"

@app.route('/health', methods=['GET'])
def health_check():
    """Trivial non-blocking route for Render's health check."""
    return "OK", 200
    
@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    secret_key = request.args.get('secret_key') 
    target_chat_id = request.args.get('chat_id') 

    validated_chat_id = get_chat_id_from_secret(secret_key)

    if not secret_key or not target_chat_id or str(validated_chat_id) != str(target_chat_id):
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
        
        if message_text.startswith('/start'):
            guide_text = (
                "👋 <b>Welcome to GitSync!</b>\n\n"
                "Add me to your Telegram organization group to instantly generate a unique webhook for your team.\n\n"
                "Once added, run:\n"
                "🔹 <code>/gitsync</code> to get your webhook URL\n"
                "🔹 <code>/dashboard</code> to see your team's analytics"
            )
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": guide_text, "parse_mode": "HTML"})

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
            key = get_secret_from_chat_id(chat_id)
            if key:
                dashboard_url = f"{APP_BASE_URL}/dashboard?key={key}"
                response_text = f"📊 <b>Team Dashboard</b>\n\nView analytics here:\n<a href='{dashboard_url}'>Open Dashboard</a>"
            else:
                response_text = "❌ <b>Error:</b> Please run <code>/gitsync</code> first to set up your group."

            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})

    return jsonify({"status": "ok"}), 200

# --- 📊 DASHBOARD ROUTE ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    secret_key = request.args.get('key')
    if not secret_key: return "<h1>401 Unauthorized</h1><p>Access denied.</p>", 401
        
    target_chat_id = get_chat_id_from_secret(secret_key)
    if not target_chat_id: return "<h1>401 Unauthorized</h1><p>Invalid dashboard key.</p>", 401

    conn = get_db_connection()
    if not conn: return "Database Error", 500
    
    try:
        c = conn.cursor()
        
        # 1. Fetch Commit History
        c.execute("""
            SELECT author, summary, timestamp, repo_name, branch_name, insertions
            FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY id DESC
        """, (str(target_chat_id),))
        all_updates = c.fetchall()
        
        # 2. Fetch Stats (Grouped by author, summing up file changes)
        c.execute("""
            SELECT author, 
                   SUM(COALESCE(insertions, 0)) as insertions,
                   SUM(COALESCE(files_modified, 0)) as modified,
                   SUM(COALESCE(files_removed, 0)) as removed
            FROM project_updates 
            WHERE chat_id = %s 
            GROUP BY author
        """, (str(target_chat_id),))
        stats = c.fetchall()
        
        # 3. Fetch Organization Name (Most recent)
        c.execute("SELECT repo_name FROM project_updates WHERE chat_id = %s ORDER BY id DESC LIMIT 1", (str(target_chat_id),))
        repo_result = c.fetchone()
        org_title = repo_result[0] if repo_result else "Organization"

    except Exception as e:
        return f"Query Error: {e}", 500
    finally:
        conn.close()

    # Time sorting
    now_utc = datetime.utcnow()
    IST_OFFSET = timedelta(hours=5, minutes=30)
    today_str = (now_utc + IST_OFFSET).strftime('%Y-%m-%d')
    yesterday_str = (now_utc + IST_OFFSET - timedelta(days=1)).strftime('%Y-%m-%d')
    
    today_logs, yesterday_logs, week_logs = [], [], []
    
    for u in all_updates:
        db_utc_time = u[2]
        ist_time = db_utc_time + IST_OFFSET
        db_date_str = ist_time.strftime('%Y-%m-%d')
        display_timestamp = ist_time.strftime('%I:%M %p')
        
        repo = u[3] if u[3] else "Unknown"
        branch = u[4] if u[4] else "main"
        insertions = u[5] if u[5] is not None else 0
        summary_html = u[1].replace('\n', '<br>')
            
        item_html = (
            f'<div class="update-item">'
            f'<div class="meta">'
            f'👤 {u[0]} | 🕒 {display_timestamp}<br>'
            f'📂 {repo} (<code>{branch}</code>) | ➕ {insertions} lines'
            f'</div>'
            f'<pre>{summary_html}</pre>'
            f'</div>'
        )
        
        if db_date_str == today_str: today_logs.append(item_html)
        elif db_date_str == yesterday_str: yesterday_logs.append(item_html)
        else: week_logs.append(item_html)

    # Prepare Chart Data
    labels = [row[0] for row in stats]
    data_added = [row[1] for row in stats]
    data_modified = [row[2] for row in stats]
    data_removed = [row[3] for row in stats]

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{org_title} Analytics</title>
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
        <style>
            body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; padding: 20px; color: #333; }}
            .container {{ max-width: 1000px; margin: 0 auto; }}
            h1 {{ text-align: center; color: #2c3e50; margin-bottom: 10px; font-size: 1.8rem; }}
            .subtitle {{ text-align: center; color: #666; margin-bottom: 30px; font-size: 1rem; }}
            .card {{ background: white; padding: 25px; border-radius: 12px; box-shadow: 0 4px 12px rgba(0,0,0,0.05); margin-bottom: 25px; }}
            .section-header {{ background: #e9ecef; padding: 8px 15px; border-radius: 6px; margin: 15px 0 10px; font-weight: bold; color: #555; }}
            .update-item {{ background: #fff; border-left: 4px solid #007bff; padding: 15px; margin-bottom: 10px; border-radius: 4px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
            .meta {{ color: #888; font-size: 0.85em; margin-bottom: 8px; font-weight: 600; }}
            pre {{ white-space: pre-wrap; font-family: 'Segoe UI', sans-serif; color: #333; margin: 0; line-height: 1.5; }}
            .empty-msg {{ color: #999; font-style: italic; padding: 10px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <h1>🚀 GitSync Analytics</h1>
            <div class="subtitle">Organization/Repo: <b>{org_title}</b></div>
            
            <div class="card">
                <h2>🏆 Code Volume (Lines Added & File Changes)</h2>
                <div style="height: 300px;"><canvas id="activityChart"></canvas></div>
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
                data: {{ 
                    labels: {labels}, 
                    datasets: [
                        {{ label: 'Lines Added (Code Volume)', data: {data_added}, backgroundColor: '#2ecc71' }},
                        {{ label: 'Modified Files', data: {data_modified}, backgroundColor: '#f1c40f', stack: 'stack1' }},
                        {{ label: 'Deleted Files', data: {data_removed}, backgroundColor: '#e74c3c', stack: 'stack1' }}
                    ] 
                }},
                options: {{ 
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{ 
                        x: {{ stacked: true }},
                        y: {{ 
                            stacked: true, 
                            beginAtZero: true, 
                            ticks: {{ stepSize: 1 }} 
                        }} 
                    }},
                    plugins: {{
                        legend: {{ position: 'top' }},
                        tooltip: {{ mode: 'index', intersect: false }}
                    }}
                }}
            }});
        </script>
    </body>
    </html>
    """
    return html

# --- SAFE INIT ON STARTUP ---
with app.app_context():
    init_db()

# 3. Apply Admin and Maintenance Middleware
maintenance_middleware(app)
register_admin_routes(app, on_disable_flush_callback=flush_pending_callback)

if __name__ == '__main__':
    app.run(port=5000, debug=True)