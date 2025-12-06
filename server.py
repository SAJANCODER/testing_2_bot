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
import pytz
import json
from collections import defaultdict

executor = ThreadPoolExecutor(max_workers=5)

load_dotenv()

app = Flask(__name__)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
TELEGRAM_BOT_TOKEN_FOR_COMMANDS = os.getenv("TELEGRAM_BOT_TOKEN_FOR_COMMANDS")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = 'gemini-2.5-pro'
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print(f"❌ DATABASE CONNECTION ERROR: {e}", file=sys.stderr)
        return None

def init_db():
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()

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

        try:
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS repo_name TEXT;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS branch_name TEXT;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_added INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_modified INTEGER DEFAULT 0;")
            c.execute("ALTER TABLE project_updates ADD COLUMN IF NOT EXISTS files_removed INTEGER DEFAULT 0;")
        except Exception as e:
            print(f"⚠️ Migration notice: {e}")

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

def save_to_db(chat_id, author, repo_name, branch_name, summary, added, modified, removed):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
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
        r = requests.post(url, json=payload)

        if r.status_code != 200:
             print(f"❌ Telegram Delivery FAILED. Status: {r.status_code}. Response: {r.text}")
    except Exception as e:
        print(f"❌ Error sending to Telegram: {e}")

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

            save_to_db(target_chat_id, author_name, display_repo_name, branch_name, summary, added_count, modified_count, removed_count)

        if all_updates:
            final_report = "\n\n----------------\n\n".join(all_updates)
            send_to_telegram(final_report, author_name, display_repo_name, branch_name, target_bot_token, target_chat_id)

        print(f"✅ BACKGROUND TASK COMPLETE for {author_name}")
    except Exception as e:
        print(f"❌ CRITICAL TASK ERROR: {e}")
        traceback.print_exc()

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



# --- 📊 ENHANCED DASHBOARD ROUTE ---
@app.route('/dashboard', methods=['GET'])
def dashboard():
    secret_key = request.args.get('key')
    if not secret_key:
        return "<h1>401 Unauthorized</h1><p>Access denied.</p>", 401
    
    target_chat_id = get_chat_id_from_secret(secret_key)
    if not target_chat_id:
        return "<h1>401 Unauthorized</h1><p>Invalid dashboard key.</p>", 401
    
    conn = get_db_connection()
    if not conn: return "Database Error", 500
    
    try:
        c = conn.cursor()
        
        # Get current date info
        now_utc = datetime.utcnow()
        IST_OFFSET = timedelta(hours=5, minutes=30)
        now_ist = now_utc + IST_OFFSET
        
        today_start = (now_ist.replace(hour=0, minute=0, second=0, microsecond=0) - IST_OFFSET)
        yesterday_start = today_start - timedelta(days=1)
        week_start = today_start - timedelta(days=now_ist.weekday())
        
        # 1. Fetch all commits for this chat
        c.execute("""
            SELECT author, files_added, files_modified, files_removed, 
                   timestamp, repo_name, summary
            FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY timestamp DESC
        """, (str(target_chat_id),))
        all_commits = c.fetchall()
        
        # 2. Get organization name
        c.execute("""
            SELECT repo_name FROM project_updates 
            WHERE chat_id = %s 
            ORDER BY id DESC LIMIT 1
        """, (str(target_chat_id),))
        repo_result = c.fetchone()
        org_title = repo_result[0] if repo_result and repo_result[0] else "Development Team"
        
        # 3. Calculate metrics
        today_stats = {
            'total_commits': 0,
            'files_added': 0,
            'files_modified': 0,
            'files_removed': 0,
            'files_changed': 0,
            'active_developers': set(),
            'developers': []
        }
        
        yesterday_stats = {
            'total_commits': 0,
            'files_changed': 0,
            'developers': set()
        }
        
        week_stats = defaultdict(lambda: {
            'commits': 0,
            'added': 0,
            'modified': 0,
            'removed': 0,
            'score': 0
        })
        
        daily_stats = {
            'labels': [],
            'added': [],
            'modified': [],
            'removed': []
        }
        
        # Initialize last 7 days
        for i in range(6, -1, -1):
            date = now_ist - timedelta(days=i)
            daily_stats['labels'].append(date.strftime('%a'))
            daily_stats['added'].append(0)
            daily_stats['modified'].append(0)
            daily_stats['removed'].append(0)
        
        recent_activities = []
        developer_scores = defaultdict(int)
        
        for commit in all_commits:
            author = commit[0]
            added = commit[1] or 0
            modified = commit[2] or 0
            removed = commit[3] or 0
            timestamp = commit[4]
            repo = commit[5]
            summary = commit[6]
            
            # Convert timestamp to IST
            commit_time_ist = timestamp + IST_OFFSET
            commit_date_str = commit_time_ist.strftime('%Y-%m-%d')
            day_of_week = commit_time_ist.weekday()
            
            # Calculate score for leaderboard (weighted: added > modified > removed)
            score = (added * 3) + (modified * 2) + (removed * 1)
            developer_scores[author] += score
            
            # Today's commits
            if timestamp >= today_start:
                today_stats['total_commits'] += 1
                today_stats['files_added'] += added
                today_stats['files_modified'] += modified
                today_stats['files_removed'] += removed
                today_stats['files_changed'] += (added + modified + removed)
                today_stats['active_developers'].add(author)
                
                # Add to recent activities
                if len(recent_activities) < 10:
                    activity_types = [
                        {'icon': 'fas fa-plus-circle', 'color': '#2ecc71', 'title': 'Files Added'},
                        {'icon': 'fas fa-edit', 'color': '#f1c40f', 'title': 'Files Modified'},
                        {'icon': 'fas fa-minus-circle', 'color': '#e74c3c', 'title': 'Files Removed'},
                        {'icon': 'fas fa-code', 'color': '#3498db', 'title': 'Code Commit'},
                        {'icon': 'fas fa-bug', 'color': '#9b59b6', 'title': 'Bug Fix'}
                    ]
                    import random
                    activity = random.choice(activity_types)
                    
                    recent_activities.append({
                        'title': f"{author} {activity['title'].lower()}",
                        'description': f"{added} added, {modified} modified in {repo}" if added or modified else f"Cleaned up {removed} files",
                        'icon': activity['icon'],
                        'color': activity['color'],
                        'time': commit_time_ist.strftime('%I:%M %p')
                    })
            
            # Yesterday's commits
            elif yesterday_start <= timestamp < today_start:
                yesterday_stats['total_commits'] += 1
                yesterday_stats['files_changed'] += (added + modified + removed)
                yesterday_stats['developers'].add(author)
            
            # This week's commits (for leaderboard)
            if timestamp >= week_start:
                week_stats[author]['commits'] += 1
                week_stats[author]['added'] += added
                week_stats[author]['modified'] += modified
                week_stats[author]['removed'] += removed
                week_stats[author]['score'] += score
            
            # Fill daily stats for chart (last 7 days)
            days_ago = (now_ist.date() - commit_time_ist.date()).days
            if 0 <= days_ago <= 6:
                idx = 6 - days_ago
                daily_stats['added'][idx] += added
                daily_stats['modified'][idx] += modified
                daily_stats['removed'][idx] += removed
        
        # Calculate percentages and changes
        today_stats['active_developers'] = len(today_stats['active_developers'])
        total_developers = len(set([c[0] for c in all_commits]))
        today_stats['active_percentage'] = round((today_stats['active_developers'] / max(total_developers, 1)) * 100)
        
        # Calculate velocity score (0-100)
        velocity_base = min(today_stats['files_changed'] * 2, 100)
        team_factor = min((today_stats['active_developers'] / max(total_developers, 1)) * 50, 50)
        today_stats['velocity_score'] = min(velocity_base + team_factor, 100)
        
        # Calculate changes from yesterday
        yesterday_files = yesterday_stats['files_changed']
        if yesterday_files > 0:
            change = ((today_stats['files_changed'] - yesterday_files) / yesterday_files) * 100
            today_stats['change_percentage'] = round(change, 1)
            today_stats['files_change_percentage'] = round(change, 1)
        else:
            today_stats['change_percentage'] = 100 if today_stats['files_changed'] > 0 else 0
            today_stats['files_change_percentage'] = 100 if today_stats['files_changed'] > 0 else 0
        
        # Velocity change (placeholder - would compare with previous day)
        today_stats['velocity_change'] = 5  # Positive by default
        
        # Create leaderboard
        leaderboard = []
        for author, stats in week_stats.items():
            leaderboard.append({
                'name': author,
                'commits': stats['commits'],
                'files_changed': stats['added'] + stats['modified'] + stats['removed'],
                'score': stats['score']
            })
        
        # Sort leaderboard by score
        leaderboard.sort(key=lambda x: x['score'], reverse=True)
        leaderboard = leaderboard[:10]  # Top 10
        
        # Calculate progress percentages
        target_per_day = 10  # Target files changed per day
        today_progress = min(round((today_stats['files_changed'] / target_per_day) * 100), 100)
        
        target_per_week = target_per_day * 5  # Weekly target
        week_files = sum(daily_stats['added']) + sum(daily_stats['modified']) + sum(daily_stats['removed'])
        week_progress = min(round((week_files / target_per_week) * 100), 100)
        
        # Sprint progress (simulated)
        sprint_progress = min(week_progress + 20, 100)
        
        # Motivation messages
        motivation_messages = [
            "🚀 You're on fire! Keep up the momentum!",
            "🔥 Amazing work today! The team is crushing it!",
            "💪 Strong performance! Let's keep pushing forward!",
            "🌟 Stellar contributions! You're making great progress!",
            "⚡ Lightning fast development! Impressive velocity!",
            "🎯 Right on target! Team coordination is excellent!"
        ]
        
        top_performer_messages = [
            "is leading the charge with exceptional contributions!",
            "is setting the pace with outstanding commits!",
            "is dominating the leaderboard this week!",
            "is showing everyone how it's done!",
            "is the MVP of the week with consistent performance!"
        ]
        
        
        motivation_title = random.choice(["Team Momentum", "Performance Peak", "Development Sprint"])
        motivation_message = random.choice(motivation_messages)
        top_performer_message = random.choice(top_performer_messages)
        
        # Prepare template data
        template_data = {
            'org_title': org_title,
            'total_members': total_developers,
            'current_date': now_ist.strftime('%B %d, %Y'),
            'week_number': now_ist.isocalendar()[1],
            'today_stats': today_stats,
            'yesterday_stats': yesterday_stats,
            'daily_stats': daily_stats,
            'leaderboard': leaderboard,
            'today_progress': today_progress,
            'week_progress': week_progress,
            'sprint_progress': sprint_progress,
            'recent_activities': recent_activities,
            'motivation_title': motivation_title,
            'motivation_message': motivation_message,
            'top_performer_message': top_performer_message
        }
        
        # Render template
        from flask import render_template_string
        with open('templates/dashboard.html', 'r') as f:
            template = f.read()
        
        return render_template_string(template, **template_data)
        
    except Exception as e:
        print(f"❌ Dashboard Error: {e}")
        traceback.print_exc()
        return f"Dashboard Error: {e}", 500
    finally:
        conn.close()

with app.app_context():
    init_db()

if __name__ == '__main__':
    app.run(port=5000, debug=True)
