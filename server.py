import google.generativeai as genai
from flask import Flask, request, jsonify, redirect
from dotenv import load_dotenv
import os
import requests
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import re
import uuid
import psycopg2
import sys
import html
import traceback
import pytz
import json
from collections import defaultdict
import random  # Added missing import

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

# Set timezone
IST = pytz.timezone('Asia/Kolkata')

# --- DATABASE CONNECTION ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

# --- DATABASE INIT (AUTO-MIGRATION) ---
def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()

        # 1. Create base table
        c.execute('''
            CREATE TABLE IF NOT EXISTS project_updates (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                author TEXT,
                repo_name TEXT,
                branch_name TEXT,
                summary TEXT,
                files_changed INTEGER DEFAULT 0,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # 2. AUTO-MIGRATION: Add new columns if they don't exist
        try:
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS repo_name TEXT;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS branch_name TEXT;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_added INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_modified INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_removed INTEGER DEFAULT 0;")
        except Exception as e:
            print(f"⚠️ Migration notice: {e}")

        # 3. Webhooks table
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhooks (
                secret_key TEXT PRIMARY KEY,
                chat_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        print("✅ PostgreSQL tables initialized & migrated!")
    except Exception as e:
        print(f"❌ Error during DB initialization: {e}")
    finally:
        conn.close()

# --- DATABASE OPERATIONS ---
def save_to_db(chat_id, author, repo_name, branch_name, summary, added, modified, removed):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        # Calculate total files changed for legacy compatibility
        total_files = added + modified + removed

        c.execute("""
            INSERT INTO project_updates 
            (chat_id, author, repo_name, branch_name, summary, files_changed, files_added, files_modified, files_removed, timestamp) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (str(chat_id), author, repo_name, branch_name, summary, total_files, added, modified, removed))
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

def get_secret_from_chat_id(chat_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT secret_key FROM webhooks WHERE chat_id = %s", (str(chat_id),))
        result = c.fetchone()
        return result[0] if result else None
    except Exception as e:
        print(f"❌ Error retrieving secret key: {e}")
        return None
    finally:
        conn.close()

# --- AI & TELEGRAM FUNCTIONS ---
def generate_ai_analysis(commit_data, files_changed):
    commit_msg = commit_data.get('message', 'No message.')
    input_text = f"COMMIT MESSAGE: {commit_msg}\nFILES CHANGED: {', '.join(files_changed)}"

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

def send_to_telegram(text, author, repo, branch, target_bot_token, target_chat_id):
    if not target_bot_token or not target_chat_id: return
    try:
        # CLEANUP
        clean_text = text.replace("```html", "").replace("```", "")
        clean_text = clean_text.replace("<br>", "\n").replace("<br/>", "\n").replace("<br />", "\n")
        clean_text = clean_text.replace("<ul>", "").replace("</ul>", "")
        clean_text = clean_text.replace("<ol>", "").replace("</ol>", "")
        clean_text = clean_text.replace("<li>", "• ").replace("</li>", "\n")
        clean_text = clean_text.replace("<p>", "").replace("</p>", "\n\n")

        now_utc = datetime.utcnow()
        ist_time = now_utc.astimezone(IST)
        display_timestamp = ist_time.strftime('%I:%M %p')
        
        # Header with Repo and Branch
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
        r = requests.post(url, json=payload)
        
        if r.status_code != 200:
             print(f"❌ Telegram Delivery FAILED. Status: {r.status_code}. Response: {r.text}")
    except Exception as e:
        print(f"❌ Error sending to Telegram: {e}")

# --- WEBHOOK PROCESSOR ---
def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    try:
        all_updates = []
        commits = data.get('commits', [])

        repo_name = data.get('repository', {}).get('name', 'Unknown Repo')
        org_name = data.get('repository', {}).get('organization', 'Unknown Org')
        if isinstance(org_name, dict): org_name = org_name.get('login', 'Unknown')
        
        display_repo_name = f"{org_name}/{repo_name}" if org_name != 'Unknown' and org_name != 'Unknown Org' else repo_name
        branch_ref = data.get('ref', '')
        branch_name = branch_ref.split('/')[-1] if branch_ref else 'unknown'

        if not commits and 'head_commit' in data:
            commits = [data['head_commit']]
            
        for commit in commits:
            # CALCULATE FILE STATS
            added_list = commit.get('added', [])
            removed_list = commit.get('removed', [])
            modified_list = commit.get('modified', [])
            
            added_count = len(added_list)
            removed_count = len(removed_list)
            modified_count = len(modified_list)
            
            files_list = added_list + removed_list + modified_list
            ai_response = generate_ai_analysis(commit, files_list)
            
            summary = ai_response.strip()
            commit_id = commit.get('id', 'unknown')[:7]
            all_updates.append(f"<b>Commit:</b> <code>{commit_id}</code>\n{summary}")
            
            # Save detailed stats to DB
            save_to_db(target_chat_id, author_name, display_repo_name, branch_name, summary, added_count, modified_count, removed_count)
                
        if all_updates:
            final_report = "\n\n----------------\n\n".join(all_updates)
            send_to_telegram(final_report, author_name, display_repo_name, branch_name, target_bot_token, target_chat_id)
            
        print(f"✅ BACKGROUND TASK COMPLETE for {author_name}")
    except Exception as e:
        print(f"❌ CRITICAL TASK ERROR: {e}")
        traceback.print_exc()

# --- HELPER FUNCTIONS FOR DASHBOARD ---
def get_date_boundaries():
    """Get today, yesterday, and week start boundaries in UTC"""
    now_ist = datetime.now(IST)
    
    # Start of today in IST
    today_start_ist = IST.localize(datetime(now_ist.year, now_ist.month, now_ist.day, 0, 0, 0))
    # Convert to UTC
    today_start_utc = today_start_ist.astimezone(pytz.UTC)
    
    # Start of yesterday
    yesterday_start_utc = today_start_utc - timedelta(days=1)
    
    # Start of week (Monday)
    week_start_utc = today_start_utc - timedelta(days=now_ist.weekday())
    
    return today_start_utc, yesterday_start_utc, week_start_utc, now_ist

def calculate_performance_score(today_data, week_data, now_ist):
    """Calculate team performance score (0-100)"""
    score = 0
    
    # 1. Today's activity (0-40 points)
    today_changes = today_data['files_changed']
    score += min(today_changes * 2, 40)
    
    # 2. Team participation (0-30 points)
    active_devs = len(today_data['active_developers'])
    total_devs = len(week_data)
    if total_devs > 0:
        participation = (active_devs / total_devs) * 30
        score += participation
    
    # 3. Consistency check (Mon-Fri only)
    weekday = now_ist.weekday()
    if weekday < 5:  # Monday to Friday
        if today_changes > 0:
            score += 15  # Good work on a weekday
        else:
            score -= 10  # No activity on a weekday
    
    # 4. Multi-repo activity (0-10 points)
    score += min(len(today_data.get('repos', set())) * 2, 10)
    
    # 5. Code quality bonus (based on commit diversity)
    if today_data['files_added'] > today_data['files_removed']:
        score += 5  # More additions than deletions is good
    
    return max(0, min(score, 100))

def generate_motivation_message(today_data, yesterday_data, leaderboard, now_ist):
    """Generate motivational message based on performance"""
    weekday = now_ist.weekday()
    today_changes = today_data['files_changed']
    
    # Weekend message
    if weekday >= 5:
        return {
            'title': 'Weekend Mode',
            'message': "🏖️ Perfect time for passion projects and exploration!"
        }
    
    # Determine message based on performance
    if today_changes >= 20:
        level = 'high'
    elif today_changes >= 10:
        level = 'medium'
    elif today_changes > 0:
        level = 'low'
    else:
        level = 'none'
    
    messages = {
        'high': [
            "🚀 Incredible momentum! You're crushing it!",
            "🔥 Unstoppable force! Keep up this amazing pace!",
            "🌟 Stellar performance! The team is on fire!",
            "💫 Breaking records! This is your best day yet!"
        ],
        'medium': [
            "💪 Solid progress! Every commit counts!",
            "📈 Good velocity! Consistency leads to success!",
            "🎯 On target! Keep up the great work!",
            "⚡ Making steady progress! Teamwork makes the dream work!"
        ],
        'low': [
            "🔋 Good start! Ready for the next challenge?",
            "💡 Every line of code matters! Keep building!",
            "🏗️ Foundation laid! Time to build upwards!",
            "🌱 Planting seeds for future growth!"
        ],
        'none': [
            "🎪 The stage is set! What will you create today?",
            "⚙️ Ready to build? The canvas is blank!",
            "🚀 Launch sequence initiated! Time to code!",
            "💡 New day, new opportunities!"
        ]
    }
    
    return {
        'title': random.choice(['Daily Sprint', 'Team Momentum', 'Progress Pulse']),
        'message': random.choice(messages[level])
    }

# --- ROUTES ---
@app.route('/', methods=['GET'])
def home():
    return "GitSync Bot Active"

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
        
        if message_text.startswith('/start'):
            guide_text = (
                "👋 <b>Welcome to GitSync!</b>\n\n"
                "Add me to your Telegram organization group to instantly generate a unique webhook for your team.\n\n"
                "I'll handle everything automatically — just drop me in, and your dashboard comes alive.\n\n"
                "Tap →Add(User_Name:<code>@QubreaSyncBot</code>)→ Done.\n\n"
                "Let's get your team synced in seconds.\n\n"
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
                "1. Copy your unique Webhook URL (The token is hidden!):\n\n"
                f"<code>{webhook_url}</code>\n\n"
                "2. Paste this URL into your GitHub repository settings under Webhooks.\n"
                "(Content Type: <code>application/json</code>, Events: <code>Just the push event</code>.)\n\n"
                "Once added, run:\n"
                "🔹 <code>/dashboard</code> to see your team's analytics\n\n"
                "I will now start analyzing your commits!"
            )
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})
            
        elif message_text.startswith('/dashboard'):
            key = get_secret_from_chat_id(chat_id)
            if key:
                dashboard_url = f"{APP_BASE_URL}/dashboard?key={key}"
                response_text = f"📊 <b>Team Dashboard</b>\n\nYour team's performance graph is waiting!\nOpen the dashboard and See what your team shipped today \n<a href='{dashboard_url}'>Open Dashboard</a>"
            else:
                response_text = "❌ <b>Error:</b> Please run <code>/gitsync</code> first to set up your group."

            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), 
                          json={"chat_id": chat_id, "text": response_text, "parse_mode": "HTML"})

    return jsonify({"status": "ok"}), 200

# --- 📊 FIXED DASHBOARD ROUTE ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    secret_key = request.args.get('key')
    if not secret_key:
        return "<h1>401 Unauthorized</h1><p>Access denied.</p>", 401
    
    target_chat_id = get_chat_id_from_secret(secret_key)
    if not target_chat_id:
        return "<h1>401 Unauthorized</h1><p>Invalid dashboard key.</p>", 401
    
    conn = get_db_connection()
    if not conn: 
        return """
        <html>
        <head><title>Database Error</title></head>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h1>⚠️ Database Connection Error</h1>
            <p>Unable to connect to the database. Please try again later.</p>
        </body>
        </html>
        """, 500
    
    try:
        c = conn.cursor()
        
        # Get date boundaries
        today_start_utc, yesterday_start_utc, week_start_utc, now_ist = get_date_boundaries()
        
        # 1. Get organization name
        c.execute("""
            SELECT repo_name FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY timestamp DESC LIMIT 1
        """, (str(target_chat_id),))
        repo_result = c.fetchone()
        org_title = repo_result[0] if repo_result and repo_result[0] else "Development Team"
        
        # 2. Get all developers in this group
        c.execute("""
            SELECT DISTINCT author FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY author
        """, (str(target_chat_id),))
        developers = [row[0] for row in c.fetchall()]
        total_developers = len(developers)
        
        # 3. Fetch all commits for this group
        c.execute("""
            SELECT author, files_added, files_modified, files_removed, 
                   timestamp, repo_name, summary
            FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY timestamp DESC
        """, (str(target_chat_id),))
        all_commits = c.fetchall()
        
        # Initialize data structures
        daily_stats = {i: {'added': 0, 'modified': 0, 'removed': 0, 'commits': 0} 
                      for i in range(7)}
        
        today_data = {
            'total_commits': 0,
            'files_added': 0,
            'files_modified': 0,
            'files_removed': 0,
            'files_changed': 0,
            'active_developers': set(),
            'repos': set()
        }
        
        yesterday_data = {
            'files_changed': 0,
            'developers': set()
        }
        
        week_data = {dev: {
            'commits': 0,
            'added': 0,
            'modified': 0,
            'removed': 0,
            'score': 0,
            'activity_days': set()
        } for dev in developers}
        
        recent_activities = []
        
        # Process all commits
        for commit in all_commits:
            author = commit[0]
            added = commit[1] or 0
            modified = commit[2] or 0
            removed = commit[3] or 0
            timestamp = commit[4]  # This is timezone-aware UTC from PostgreSQL
            repo = commit[5]
            summary = commit[6]
            
            # Ensure timestamp is timezone-aware
            if timestamp.tzinfo is None:
                timestamp = pytz.utc.localize(timestamp)
            
            # Convert to IST for display
            commit_time_ist = timestamp.astimezone(IST)
            commit_date_ist = commit_time_ist.date()
            days_ago = (now_ist.date() - commit_date_ist).days
            
            # Calculate score (weighted: added > modified > removed)
            score = (added * 3) + (modified * 2) + (removed * 1)
            
            # Today's commits (compare in UTC)
            if timestamp >= today_start_utc:
                today_data['total_commits'] += 1
                today_data['files_added'] += added
                today_data['files_modified'] += modified
                today_data['files_removed'] += removed
                today_data['files_changed'] += (added + modified + removed)
                today_data['active_developers'].add(author)
                today_data['repos'].add(repo)
                
                # Add to recent activities
                if len(recent_activities) < 5:
                    activity_types = [
                        {'icon': 'fas fa-plus-circle', 'color': '#2ecc71', 'title': 'Files Added'},
                        {'icon': 'fas fa-edit', 'color': '#f1c40f', 'title': 'Files Modified'},
                        {'icon': 'fas fa-minus-circle', 'color': '#e74c3c', 'title': 'Files Removed'},
                        {'icon': 'fas fa-code', 'color': '#3498db', 'title': 'Code Commit'},
                        {'icon': 'fas fa-bug', 'color': '#9b59b6', 'title': 'Bug Fix'}
                    ]
                    activity = random.choice(activity_types)
                    
                    recent_activities.append({
                        'title': f"{author} committed",
                        'description': f"{added} added, {modified} modified in {repo}" if added or modified else f"Cleaned up {removed} files",
                        'icon': activity['icon'],
                        'color': activity['color'],
                        'time': commit_time_ist.strftime('%I:%M %p')
                    })
            
            # Yesterday's commits
            elif yesterday_start_utc <= timestamp < today_start_utc:
                yesterday_data['files_changed'] += (added + modified + removed)
                yesterday_data['developers'].add(author)
            
            # This week's commits (for leaderboard)
            if timestamp >= week_start_utc:
                week_data[author]['commits'] += 1
                week_data[author]['added'] += added
                week_data[author]['modified'] += modified
                week_data[author]['removed'] += removed
                week_data[author]['score'] += score
                week_data[author]['activity_days'].add(commit_date_ist.weekday())
            
            # Last 7 days for chart
            if 0 <= days_ago < 7:
                idx = 6 - days_ago
                daily_stats[idx]['added'] += added
                daily_stats[idx]['modified'] += modified
                daily_stats[idx]['removed'] += removed
                daily_stats[idx]['commits'] += 1
        
        # Calculate statistics
        active_developers = len(today_data['active_developers'])
        
        # Calculate changes from yesterday
        yesterday_changes = yesterday_data['files_changed']
        today_changes = today_data['files_changed']
        
        if yesterday_changes > 0:
            change_percent = ((today_changes - yesterday_changes) / yesterday_changes) * 100
        else:
            change_percent = 100 if today_changes > 0 else 0
        
        # Calculate performance score
        performance_score = calculate_performance_score(today_data, week_data, now_ist)
        
        # Create leaderboard
        leaderboard = []
        for dev, stats in week_data.items():
            if stats['score'] > 0:
                # Calculate consistency bonus (extra points for working multiple days)
                consistency_bonus = len(stats['activity_days']) * 5
                
                leaderboard.append({
                    'name': dev,
                    'commits': stats['commits'],
                    'files_changed': stats['added'] + stats['modified'] + stats['removed'],
                    'score': stats['score'] + consistency_bonus,
                    'consistency': len(stats['activity_days'])
                })
        
        # Sort leaderboard
        leaderboard.sort(key=lambda x: x['score'], reverse=True)
        leaderboard = leaderboard[:10]
        
        # Calculate progress percentages
        target_per_day = 10  # Target files changed per day
        today_progress = min(round((today_changes / target_per_day) * 100), 100) if target_per_day > 0 else 0
        
        # Weekly progress (target for 5 working days)
        week_files = sum(daily_stats[i]['added'] + daily_stats[i]['modified'] + daily_stats[i]['removed'] 
                        for i in range(7))
        target_per_week = target_per_day * 5
        week_progress = min(round((week_files / target_per_week) * 100), 100) if target_per_week > 0 else 0
        
        # Sprint progress (simulated based on weekly performance)
        sprint_progress = min(week_progress + random.randint(10, 30), 100)
        
        # Generate motivation message
        motivation = generate_motivation_message(today_data, yesterday_data, leaderboard, now_ist)
        
        # Top performer message
        top_performer = leaderboard[0] if leaderboard else None
        top_performer_message = f"{top_performer['name']} is leading with {int(top_performer['score'])} points!" if top_performer else "No activity yet this week!"
        
        # Prepare chart data
        chart_labels = []
        for i in range(6, -1, -1):
            date = now_ist - timedelta(days=i)
            chart_labels.append(date.strftime('%a'))
        
        # Prepare template data
        template_data = {
            'org_title': org_title,
            'total_members': total_developers,
            'current_date': now_ist.strftime('%B %d, %Y'),
            'week_number': now_ist.isocalendar()[1],
            'today_stats': {
                'total_commits': today_data['total_commits'],
                'files_changed': today_changes,
                'active_developers': active_developers,
                'change_percentage': round(change_percent, 1),
                'active_percentage': round((active_developers / max(total_developers, 1)) * 100),
                'velocity_score': round(performance_score, 1),
                'velocity_change': 5 if today_changes > yesterday_changes else -5,
                'repos': len(today_data['repos'])
            },
            'daily_stats': {
                'labels': chart_labels,
                'added': [daily_stats[i]['added'] for i in range(7)],
                'modified': [daily_stats[i]['modified'] for i in range(7)],
                'removed': [daily_stats[i]['removed'] for i in range(7)],
                'commits': [daily_stats[i]['commits'] for i in range(7)]
            },
            'leaderboard': leaderboard,
            'today_progress': today_progress,
            'week_progress': week_progress,
            'sprint_progress': sprint_progress,
            'recent_activities': recent_activities,
            'motivation_title': motivation['title'],
            'motivation_message': motivation['message'],
            'top_performer_message': top_performer_message
        }
        
        conn.close()
        
        # Render template
        from flask import render_template_string
        try:
            with open('templates/dashboard.html', 'r', encoding='utf-8') as f:
                template = f.read()
            return render_template_string(template, **template_data)
        except FileNotFoundError:
            # Fallback template if dashboard.html is missing
            return """
            <html>
            <head><title>Dashboard - {}</title></head>
            <body style="font-family: Arial, sans-serif; padding: 20px;">
                <h1>📊 {} Dashboard</h1>
                <p>Today's Progress: {}%</p>
                <p>Active Developers: {}/{} ({:.1f}%)</p>
                <p>Performance Score: {:.1f}/100</p>
                <hr>
                <p>Full dashboard template is being loaded...</p>
            </body>
            </html>
            """.format(
                org_title, org_title, today_progress,
                active_developers, total_developers,
                round((active_developers / max(total_developers, 1)) * 100, 1),
                performance_score
            )
        
    except Exception as e:
        print(f"❌ Dashboard Error: {e}")
        traceback.print_exc()
        conn.close()
        return """
        <html>
        <head><title>Dashboard Error</title></head>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h1>⚠️ Dashboard Error</h1>
            <p>Error: {}</p>
            <p>Please try again or contact support if the issue persists.</p>
        </body>
        </html>
        """.format(str(e)), 500

# Add health check endpoint
@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "GitSync Bot",
        "timestamp": datetime.now(timezone.utc).isoformat()
    })

# Add test endpoint
@app.route('/test-db', methods=['GET'])
def test_db():
    conn = get_db_connection()
    if conn:
        conn.close()
        return jsonify({"database": "connected"})
    return jsonify({"database": "disconnected"}), 500

# --- SAFE INIT ON STARTUP ---
with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=True)