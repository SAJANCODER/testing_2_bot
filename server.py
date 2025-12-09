# GitSync final server file (copy-paste)
import google.generativeai as genai
from flask import Flask, request, jsonify, redirect, render_template_string
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
import random
from cryptography.fernet import Fernet
from psycopg2.extras import RealDictCursor

# Initialize thread pool
executor = ThreadPoolExecutor(max_workers=5)

# Load env
load_dotenv()

app = Flask(__name__)

@app.route('/', methods=['GET'])
def home():
    # Simple health / landing for browsers and Render root
    return "GitSync Bot Active", 200

# --- CONFIG ---
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")
TELEGRAM_BOT_TOKEN_FOR_COMMANDS = os.getenv("TELEGRAM_BOT_TOKEN_FOR_COMMANDS")
APP_BASE_URL = os.getenv("APP_BASE_URL")
DATABASE_URL = os.getenv("DATABASE_URL")
MODEL_NAME = "gemini-1.5-flash"
TELEGRAM_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
BOT_USERNAME = os.getenv("BOT_USERNAME")
FERNET_KEY = os.getenv("FERNET_KEY")
fernet = Fernet(FERNET_KEY.encode()) if FERNET_KEY else None

if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

IST = pytz.timezone('Asia/Kolkata')

# --- DB ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        return conn
    except Exception as e:
        print("DB connection error:", e, file=sys.stderr)
        return None

def init_db():
    conn = get_db_connection()
    if not conn:
        print("❌ DB not available for init.")
        return
    try:
        c = conn.cursor()
        # project_updates
        c.execute('''
            CREATE TABLE IF NOT EXISTS project_updates (
                id SERIAL PRIMARY KEY,
                chat_id TEXT NOT NULL,
                author TEXT,
                repo_name TEXT,
                branch_name TEXT,
                summary TEXT,
                files_changed INTEGER DEFAULT 0,
                files_added INTEGER DEFAULT 0,
                files_modified INTEGER DEFAULT 0,
                files_removed INTEGER DEFAULT 0,
                lines_added INTEGER DEFAULT 0,
                lines_removed INTEGER DEFAULT 0,
                timestamp TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # webhooks
        c.execute('''
            CREATE TABLE IF NOT EXISTS webhooks (
                secret_key TEXT PRIMARY KEY,
                chat_id TEXT UNIQUE,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # tokens
        c.execute('''
            CREATE TABLE IF NOT EXISTS github_tokens (
                chat_id TEXT PRIMARY KEY,
                encrypted_token TEXT NOT NULL,
                created_by TEXT,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # pending requests
        c.execute('''
            CREATE TABLE IF NOT EXISTS pending_token_requests (
                request_id TEXT PRIMARY KEY,
                secret_key TEXT NOT NULL,
                user_id TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # processed commits
        c.execute('''
            CREATE TABLE IF NOT EXISTS processed_commits (
                commit_sha TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                repo_name TEXT,
                processed_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (commit_sha, chat_id)
            )
        ''')
        # pull requests & reviews & issues & ci
        c.execute('''
            CREATE TABLE IF NOT EXISTS pull_requests (
              id BIGINT PRIMARY KEY,
              chat_id TEXT,
              repo_name TEXT,
              number INTEGER,
              author TEXT,
              created_at TIMESTAMP WITH TIME ZONE,
              merged_at TIMESTAMP WITH TIME ZONE,
              closed_at TIMESTAMP WITH TIME ZONE,
              state TEXT,
              additions INTEGER DEFAULT 0,
              deletions INTEGER DEFAULT 0,
              changed_files INTEGER DEFAULT 0
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS pr_reviews (
              id BIGINT PRIMARY KEY,
              pr_id BIGINT REFERENCES pull_requests(id),
              reviewer TEXT,
              state TEXT,
              submitted_at TIMESTAMP WITH TIME ZONE
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS issues_closed (
              id BIGINT PRIMARY KEY,
              repo_name TEXT,
              number INTEGER,
              author TEXT,
              closed_by TEXT,
              created_at TIMESTAMP WITH TIME ZONE,
              closed_at TIMESTAMP WITH TIME ZONE,
              labels TEXT[]
            )
        ''')
        c.execute('''
            CREATE TABLE IF NOT EXISTS ci_results (
              id BIGINT PRIMARY KEY,
              pr_id BIGINT REFERENCES pull_requests(id),
              status TEXT,
              started_at TIMESTAMP WITH TIME ZONE,
              finished_at TIMESTAMP WITH TIME ZONE
            )
        ''')
        c.execute("CREATE INDEX IF NOT EXISTS idx_updates_chat_time ON project_updates (chat_id, timestamp DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_webhooks_secret ON webhooks (secret_key)")
        conn.commit()
        print("✅ DB initialized.")
    except Exception as e:
        print("init_db error:", e)
        traceback.print_exc()
    finally:
        conn.close()

# --- DB helpers (tokens/pending/processed) ---
def save_to_db(chat_id, author, repo_name, branch_name, summary, added, modified, removed, lines_added=0, lines_removed=0):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        total_files = added + modified + removed
        c.execute("""
            INSERT INTO project_updates 
            (chat_id, author, repo_name, branch_name, summary, files_changed, files_added, files_modified, files_removed, lines_added, lines_removed, timestamp) 
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NOW())
        """, (str(chat_id), author, repo_name, branch_name, summary, total_files, added, modified, removed, lines_added, lines_removed))
        conn.commit()
    except Exception as e:
        print("save_to_db error:", e)
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
            ON CONFLICT (chat_id) DO UPDATE SET secret_key = EXCLUDED.secret_key
        """, (secret_key, str(chat_id)))
        conn.commit()
    except Exception as e:
        print("save_webhook_config error:", e)
    finally:
        conn.close()

def get_chat_id_from_secret(secret_key):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT chat_id FROM webhooks WHERE secret_key = %s", (secret_key,))
        r = c.fetchone()
        return r[0] if r else None
    except Exception as e:
        print("get_chat_id_from_secret error:", e)
        return None
    finally:
        conn.close()

def get_secret_from_chat_id(chat_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT secret_key FROM webhooks WHERE chat_id = %s", (str(chat_id),))
        r = c.fetchone()
        return r[0] if r else None
    except Exception as e:
        print("get_secret_from_chat_id error:", e)
        return None
    finally:
        conn.close()

# tokens
def save_encrypted_token_for_chat(chat_id, plaintext_token, created_by=None):
    if not fernet:
        print("FERNET_KEY missing")
        return False
    enc = fernet.encrypt(plaintext_token.encode()).decode()
    conn = get_db_connection()
    if not conn: return False
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO github_tokens (chat_id, encrypted_token, created_by)
            VALUES (%s, %s, %s)
            ON CONFLICT (chat_id) DO UPDATE SET encrypted_token = EXCLUDED.encrypted_token, created_by = EXCLUDED.created_by, created_at = NOW()
        """, (str(chat_id), enc, created_by))
        conn.commit()
        return True
    except Exception as e:
        print("save_encrypted_token error:", e)
        return False
    finally:
        conn.close()

def get_decrypted_token_for_chat(chat_id):
    if not fernet:
        return None
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT encrypted_token FROM github_tokens WHERE chat_id = %s", (str(chat_id),))
        r = c.fetchone()
        if not r: return None
        enc = r[0]
        try:
            return fernet.decrypt(enc.encode()).decode()
        except Exception as e:
            print("decrypt token error:", e)
            return None
    finally:
        conn.close()

def get_token_creator_for_chat(chat_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("SELECT created_by FROM github_tokens WHERE chat_id = %s", (str(chat_id),))
        r = c.fetchone()
        return r[0] if r else None
    finally:
        conn.close()

def remove_token_for_chat(chat_id):
    conn = get_db_connection()
    if not conn: return False
    try:
        c = conn.cursor()
        c.execute("DELETE FROM github_tokens WHERE chat_id = %s", (str(chat_id),))
        conn.commit()
        return True
    except Exception as e:
        print("remove_token error:", e)
        return False
    finally:
        conn.close()

def create_pending_request(secret_key, user_id, chat_id):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        request_uuid = str(uuid.uuid4())
        c.execute("""
            INSERT INTO pending_token_requests (request_id, secret_key, user_id, chat_id, created_at)
            VALUES (%s, %s, %s, %s, NOW())
        """, (request_uuid, secret_key, str(user_id), str(chat_id)))
        conn.commit()
        return request_uuid
    except Exception as e:
        print("create_pending_request error:", e)
        return None
    finally:
        conn.close()

def get_pending_request_by_user(user_id, expiry_minutes=15):
    conn = get_db_connection()
    if not conn: return None
    try:
        c = conn.cursor()
        c.execute("""
            SELECT secret_key, chat_id, created_at FROM pending_token_requests
            WHERE user_id = %s
            ORDER BY created_at DESC LIMIT 1
        """, (str(user_id),))
        r = c.fetchone()
        if not r:
            return None
        secret_key, chat_id, created_at = r
        age = (datetime.utcnow().replace(tzinfo=pytz.UTC) - created_at).total_seconds()
        if age > expiry_minutes * 60:
            c.execute("DELETE FROM pending_token_requests WHERE user_id = %s", (str(user_id),))
            conn.commit()
            return None
        return {'secret_key': secret_key, 'chat_id': chat_id}
    except Exception as e:
        print("get_pending_request error:", e)
        return None
    finally:
        conn.close()

def clear_pending_request_by_user(user_id):
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("DELETE FROM pending_token_requests WHERE user_id = %s", (str(user_id),))
        conn.commit()
    finally:
        conn.close()

# processed commits helpers
def is_commit_processed(conn, sha, chat_id):
    with conn.cursor() as c:
        c.execute("SELECT 1 FROM processed_commits WHERE commit_sha=%s AND chat_id=%s", (sha, str(chat_id)))
        return c.fetchone() is not None

def mark_commit_processed(conn, sha, chat_id, repo):
    with conn.cursor() as c:
        c.execute("INSERT INTO processed_commits (commit_sha, chat_id, repo_name) VALUES (%s, %s, %s) ON CONFLICT DO NOTHING", (sha, str(chat_id), repo))
    conn.commit()

# --- AI & TELEGRAM ---
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
        ist_time = now_utc.astimezone(IST)
        display_timestamp = ist_time.strftime('%I:%M %p')
        header = (
            f"👤 <b>{html.escape(author)}</b>\n"
            f"📂 <b>{html.escape(repo)}</b> (<code>{html.escape(branch)}</code>)\n"
            f"🕒 {display_timestamp}"
        )
        message_text = f"{header}\n\n{clean_text}"
        payload = {"chat_id": target_chat_id, "text": message_text, "parse_mode": "HTML"}
        url = TELEGRAM_API_URL.format(token=target_bot_token)
        r = requests.post(url, json=payload)
        if r.status_code != 200:
            print("Telegram send failed:", r.status_code, r.text)
    except Exception as e:
        print("send_to_telegram error:", e)

# --- GitHub helpers ---
def validate_github_token(token):
    try:
        r = requests.get("https://api.github.com/user", headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}, timeout=8)
        if r.status_code == 200:
            return r.json()
        else:
            print("token validate failed:", r.status_code, r.text)
            return None
    except Exception as e:
        print("validate_github_token error:", e)
        return None

def try_compare_api_with_chat_token(owner, repo, before, after, chat_id):
    token = get_decrypted_token_for_chat(chat_id)
    if not token:
        return None, "no-token"
    url = f"https://api.github.com/repos/{owner}/{repo}/compare/{before}...{after}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"}
    try:
        r = requests.get(url, headers=headers, timeout=12)
        if r.status_code == 200:
            return r.json(), None
        elif r.status_code in (401, 403):
            return None, f"auth-failed-{r.status_code}"
        else:
            print("compare returned", r.status_code, r.text)
            return None, f"error-{r.status_code}"
    except Exception as e:
        print("compare error:", e)
        return None, "exception"

def mark_token_invalid(chat_id, reason=None):
    creator = get_token_creator_for_chat(chat_id)
    removed = remove_token_for_chat(chat_id)
    group_msg = "⚠️ GitSync: The saved GitHub token for this group appears invalid or lacks required permissions. Exact per-file counts are now disabled until an admin reconfigures the token."
    if reason:
        group_msg += f"\n\nReason: {html.escape(reason)}"
    try:
        requests.post(TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN_FOR_COMMANDS),
                      json={"chat_id": chat_id, "text": group_msg, "parse_mode": "HTML"})
    except Exception as e:
        print("notify group failed:", e)
    # notify creator by name in group (best-effort)
    if creator:
        try:
            creator_msg = f"Hi {creator}, your saved GitHub token for this group appears invalid or revoked. Please reconfigure by clicking the secure setup link in the group (/gitsync)."
            requests.post(TELEGRAM_API_URL.format(token=TELEGRAM_BOT_TOKEN_FOR_COMMANDS),
                          json={"chat_id": chat_id, "text": creator_msg, "parse_mode": "HTML"})
        except Exception as e:
            print("notify creator failed:", e)
    return removed

# --- GitHub event handlers (store PRs/reviews/issues) ---
def handle_pull_request_event(data, target_chat):
    # insert/update pull_requests table
    pr = data.get('pull_request', {})
    pr_id = pr.get('id')
    number = pr.get('number')
    author = pr.get('user', {}).get('login')
    repo = data.get('repository', {}).get('full_name')
    state = pr.get('state')
    created_at = pr.get('created_at')
    merged_at = pr.get('merged_at')
    closed_at = pr.get('closed_at')
    additions = pr.get('additions', 0)
    deletions = pr.get('deletions', 0)
    changed_files = pr.get('changed_files', 0)
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO pull_requests (id, chat_id, repo_name, number, author, created_at, merged_at, closed_at, state, additions, deletions, changed_files)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE
              SET repo_name=EXCLUDED.repo_name, number=EXCLUDED.number, author=EXCLUDED.author,
                  created_at=EXCLUDED.created_at, merged_at=EXCLUDED.merged_at, closed_at=EXCLUDED.closed_at,
                  state=EXCLUDED.state, additions=EXCLUDED.additions, deletions=EXCLUDED.deletions, changed_files=EXCLUDED.changed_files
        """, (pr_id, str(target_chat), repo, number, author, created_at, merged_at, closed_at, state, additions, deletions, changed_files))
        conn.commit()
    except Exception as e:
        print("handle_pull_request error:", e)
    finally:
        conn.close()

def handle_pr_review_event(data, target_chat):
    review = data.get('review', {})
    pr = data.get('pull_request', {})
    pr_id = pr.get('id')
    reviewer = review.get('user', {}).get('login')
    state = review.get('state')
    submitted_at = review.get('submitted_at')
    review_id = review.get('id')
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO pr_reviews (id, pr_id, reviewer, state, submitted_at)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO UPDATE SET state = EXCLUDED.state, submitted_at = EXCLUDED.submitted_at
        """, (review_id, pr_id, reviewer, state, submitted_at))
        conn.commit()
    except Exception as e:
        print("handle_pr_review error:", e)
    finally:
        conn.close()

def handle_issues_event(data, target_chat):
    issue = data.get('issue', {})
    issue_id = issue.get('id')
    repo = data.get('repository', {}).get('full_name')
    number = issue.get('number')
    author = issue.get('user', {}).get('login')
    closed_by = issue.get('closed_by', {}).get('login') if issue.get('closed_by') else None
    created_at = issue.get('created_at')
    closed_at = issue.get('closed_at')
    labels = [l.get('name') for l in issue.get('labels', [])]
    conn = get_db_connection()
    if not conn: return
    try:
        c = conn.cursor()
        c.execute("""
            INSERT INTO issues_closed (id, repo_name, number, author, closed_by, created_at, closed_at, labels)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (id) DO UPDATE SET closed_by=EXCLUDED.closed_by, closed_at=EXCLUDED.closed_at
        """, (issue_id, repo, number, author, closed_by, created_at, closed_at, labels))
        conn.commit()
    except Exception as e:
        print("handle_issues_event error:", e)
    finally:
        conn.close()

# --- WEBHOOK ROUTE (single endpoint handles multiple event types) ---
@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    secret_key = request.args.get('secret_key')
    target_chat_id = request.args.get('chat_id')
    validated_chat_id = get_chat_id_from_secret(secret_key)
    if not secret_key or not target_chat_id or str(validated_chat_id) != str(target_chat_id):
        print("Auth failed:", target_chat_id, validated_chat_id)
        return jsonify({"status": "error", "message": "Invalid secret_key or chat_id."}), 401

    # Determine event type
    gh_event = request.headers.get('X-GitHub-Event', '').lower()
    # pick author nicely
    author_name = "Unknown"
    if 'pusher' in data:
        author_name = data['pusher'].get('name')
    elif 'sender' in data:
        author_name = data['sender'].get('login')

    # route events
    try:
        if gh_event == 'pull_request':
            handle_pull_request_event(data, target_chat_id)
            # enqueue summary to group (optional)
            pr = data.get('pull_request', {})
            title = pr.get('title','')
            number = pr.get('number')
            action = data.get('action')
            msg = f"🔀 Pull Request {action}: <b>#{number}</b> - {html.escape(title)}"
            send_to_telegram(msg, "GitSync", data.get('repository',{}).get('full_name',''), pr.get('head',{}).get('ref',''), TELEGRAM_BOT_TOKEN_FOR_COMMANDS, target_chat_id)
        elif gh_event == 'pull_request_review':
            handle_pr_review_event(data, target_chat_id)
            pr = data.get('pull_request', {})
            reviewer = data.get('review', {}).get('user',{}).get('login')
            state = data.get('review', {}).get('state')
            msg = f"🧐 PR Review by <b>{html.escape(reviewer or 'unknown')}</b>: <b>#{pr.get('number')}</b> — {state}"
            send_to_telegram(msg, "GitSync", data.get('repository',{}).get('full_name',''), pr.get('head',{}).get('ref',''), TELEGRAM_BOT_TOKEN_FOR_COMMANDS, target_chat_id)
        elif gh_event == 'issues':
            handle_issues_event(data, target_chat_id)
            issue = data.get('issue', {})
            action = data.get('action')
            msg = f"📌 Issue {action}: <b>#{issue.get('number')}</b> — {html.escape(issue.get('title',''))}"
            send_to_telegram(msg, "GitSync", data.get('repository',{}).get('full_name',''), '', TELEGRAM_BOT_TOKEN_FOR_COMMANDS, target_chat_id)
        else:
            # default: treat as push
            executor.submit(process_standup_task, TELEGRAM_BOT_TOKEN_FOR_COMMANDS, target_chat_id, author_name, data)
    except Exception as e:
        print("webhook dispatch error:", e)
        traceback.print_exc()

    return jsonify({"status": "processing", "message": "Accepted"}), 200

# --- STANDUP / PROCESSING (push handling) ---
def process_standup_task(target_bot_token, target_chat_id, author_name, data):
    try:
        all_updates = []
        commits = data.get('commits', [])
        repo_name = data.get('repository', {}).get('name', 'Unknown Repo')
        org_name = data.get('repository', {}).get('organization', 'Unknown Org')
        if isinstance(org_name, dict): org_name = org_name.get('login', 'Unknown')
        display_repo_name = f"{org_name}/{repo_name}" if org_name not in ('Unknown','Unknown Org') else repo_name
        branch_ref = data.get('ref', '')
        branch_name = branch_ref.split('/')[-1] if branch_ref else 'unknown'
        if not commits and 'head_commit' in data:
            commits = [data['head_commit']]

        owner = data.get('repository', {}).get('owner', {}) or {}
        owner_login = owner.get('login') or owner.get('name')
        before_sha = data.get('before')
        after_sha = data.get('after')

        compare_data = None
        compare_err = None
        if owner_login and repo_name and before_sha and after_sha:
            compare_data, compare_err = try_compare_api_with_chat_token(owner_login, repo_name, before_sha, after_sha, target_chat_id)

        if compare_data:
            files_info = compare_data.get('files', [])
            file_map = {f['filename']:(f.get('additions',0), f.get('deletions',0), f.get('status','modified')) for f in files_info}
            total_added = sum(v[0] for v in file_map.values())
            total_removed = sum(v[1] for v in file_map.values())
            total_modified = sum(1 for v in file_map.values() if v[2]=='modified')
            files_list = list(file_map.keys())
            head_commit = data.get('head_commit') or (commits[0] if commits else {})
            ai_response = generate_ai_analysis(head_commit or {}, files_list)
            summary = ai_response.strip()
            # Save summary + exact lines
            save_to_db(target_chat_id, author_name, display_repo_name, branch_name, summary, 0, total_modified, 0, lines_added=total_added, lines_removed=total_removed)
            lines_text = "\n".join([f"{k}: +{v[0]} / -{v[1]}" for k,v in file_map.items()])
            final_summary = f"<b>Push Summary (exact)</b>\n{summary}\n\n{lines_text}\n\n<b>Confidence:</b> exact"
            send_to_telegram(final_summary, author_name, display_repo_name, branch_name, target_bot_token, target_chat_id)
        else:
            confidence_tag = "estimated"
            if compare_err and compare_err.startswith("auth-failed"):
                try:
                    mark_token_invalid(target_chat_id, reason=compare_err)
                except Exception as e:
                    print("mark_token_invalid failed:", e)
                confidence_tag = "token-invalid"
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
                all_updates.append(f"<b>Commit:</b> <code>{commit_id}</code>\n{summary}\n\n<b>Confidence:</b> {confidence_tag}")
                save_to_db(target_chat_id, author_name, display_repo_name, branch_name, summary, added_count, modified_count, removed_count)
            if all_updates:
                final_report = "\n\n----------------\n\n".join(all_updates)
                send_to_telegram(final_report, author_name, display_repo_name, branch_name, target_bot_token, target_chat_id)
        print("Background task complete.")
    except Exception as e:
        print("process_standup_task error:", e)
        traceback.print_exc()

# --- TELEGRAM COMMANDS endpoint (handles /start, /gitsync, /dashboard, token paste) ---
@app.route('/telegram_commands', methods=['POST'])
def telegram_commands():
    update = request.json
    BOT_TOKEN = TELEGRAM_BOT_TOKEN_FOR_COMMANDS
    APP_BASE_URL_USED = APP_BASE_URL

    if 'message' in update:
        message = update['message']
        message_text = message.get('text', '')
        chat_id = message['chat']['id']

        # /start (supports deep-link payload)
        if message_text.startswith('/start'):
            parts = message_text.strip().split()
            if len(parts) > 1:
                secret_payload = parts[1].strip()
                target_chat = get_chat_id_from_secret(secret_payload)
                if not target_chat:
                    requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "❌ This setup link is invalid or expired.", "parse_mode":"HTML"})
                else:
                    create_pending_request(secret_payload, message['from']['id'], target_chat)
                    requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "🔒 Paste your GitHub PAT in this private chat. It will be stored encrypted and never shown. (Expires in 15 minutes)", "parse_mode":"HTML"})
            else:
                guide_text = (
                    "👋 <b>Welcome to GitSync!</b>\n\n"
                    "Add me to your Telegram organization group to instantly generate a unique webhook for your team.\n\n"
                    f"Tap →Add(User_Name:<code>@{BOT_USERNAME}</code>)→ Done.\n\n"
                    "Run:\n🔹 <code>/gitsync</code>\n🔹 <code>/dashboard</code>"
                )
                requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": guide_text, "parse_mode":"HTML"})
            return jsonify({"status":"ok"}), 200

        if message_text.startswith('/gitsync'):
            new_key = str(uuid.uuid4())
            save_webhook_config(chat_id, new_key)
            webhook_url = f"{APP_BASE_URL_USED}/webhook?secret_key={new_key}&chat_id={chat_id}"
            deep_link = f"https://t.me/{BOT_USERNAME}?start={new_key}"
            response_text = (
                "👋 <b>GitSync Setup Guide</b>\n\n"
                "1. Copy your unique Webhook URL:\n\n"
                f"<code>{webhook_url}</code>\n\n"
                "2. Paste in GitHub repo settings → Webhooks (push event).\n\n"
                f"3. To enable exact line counts for private repos, an admin should click: <a href=\"{deep_link}\">secure token setup (private DM)</a>\n\n"
                "Then run /dashboard."
            )
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": response_text, "parse_mode":"HTML", "disable_web_page_preview": True})
            return jsonify({"status":"ok"}), 200

        if message_text.startswith('/dashboard'):
            key = get_secret_from_chat_id(chat_id)
            if key:
                dashboard_url = f"{APP_BASE_URL}/dashboard?key={key}"
                response_text = f"📊 <b>Team Dashboard</b>\nOpen: <a href='{dashboard_url}'>Open Dashboard</a>"
            else:
                response_text = "❌ Run /gitsync first."
            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": response_text, "parse_mode":"HTML"})
            return jsonify({"status":"ok"}), 200

        # private chat flows: token paste & removal
        def looks_like_token(s):
            return bool(re.search(r'ghp_|gho_|github_pat_|ghs_|ghu_|ghr_', s)) or len(s.strip()) > 30

        if message['chat']['type'] == 'private':
            text = message_text.strip()
            if text.startswith('/remove_github_token'):
                pending = get_pending_request_by_user(message['from']['id'])
                if pending:
                    target_chat = pending['chat_id']
                    ok = remove_token_for_chat(target_chat)
                    clear_pending_request_by_user(message['from']['id'])
                    if ok:
                        requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "✅ Token removed.", "parse_mode":"HTML"})
                        requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": target_chat, "text": "⚠️ GitSync: Token removed. Exact counts disabled.", "parse_mode":"HTML"})
                    else:
                        requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "❌ Remove failed.", "parse_mode":"HTML"})
                else:
                    requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "⚠️ Click group setup link first.", "parse_mode":"HTML"})
                return jsonify({"status":"ok"}), 200

            if looks_like_token(text):
                pending = get_pending_request_by_user(message['from']['id'])
                if not pending:
                    requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "⚠️ No pending request found. Use group's setup link.", "parse_mode":"HTML"})
                else:
                    target_chat_id = pending['chat_id']
                    v = validate_github_token(text)
                    if v:
                        saved = save_encrypted_token_for_chat(target_chat_id, text, created_by=message.get('from',{}).get('username'))
                        clear_pending_request_by_user(message['from']['id'])
                        if saved:
                            group_msg = f"✅ GitHub token installed by <b>{html.escape(message.get('from',{}).get('username','admin'))}</b>. Exact per-file insertions/deletions enabled."
                            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": target_chat_id, "text": group_msg, "parse_mode":"HTML"})
                            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "✅ Token validated and saved securely.", "parse_mode":"HTML"})
                        else:
                            requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "❌ Save failed.", "parse_mode":"HTML"})
                    else:
                        requests.post(TELEGRAM_API_URL.format(token=BOT_TOKEN), json={"chat_id": chat_id, "text": "❌ Token validation failed. Ensure 'repo' permissions are present.", "parse_mode":"HTML"})
                return jsonify({"status":"ok"}), 200

    return jsonify({"status":"ok"}), 200

# --- Dashboard route (final metrics & corporate leaderboard) ---
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
        return "<h1>Database Error</h1><p>Unable to connect.</p>", 500
    
    try:
        c = conn.cursor()
        # date boundaries
        today_start_utc, yesterday_start_utc, week_start_utc, now_ist = get_date_boundaries()
        
        # org title
        c.execute("SELECT repo_name FROM project_updates WHERE chat_id = %s ORDER BY timestamp DESC LIMIT 1", (str(target_chat_id),))
        r = c.fetchone()
        org_title = r[0] if r and r[0] else "Development Team"

        # fetch distinct developers
        c.execute("SELECT DISTINCT author FROM project_updates WHERE chat_id = %s ORDER BY author", (str(target_chat_id),))
        developers = [row[0] for row in c.fetchall()]
        total_developers = len(developers)

        # -----------------------------
        # Practical, data-driven metrics
        # -----------------------------
        # 1) Today's stats (exact lines)
        c.execute("""
            SELECT
              COALESCE(SUM(lines_added),0) as lines_added,
              COALESCE(SUM(lines_removed),0) as lines_removed,
              COALESCE(SUM(files_added + files_modified + files_removed),0) as files_changed,
              COUNT(*) as commits_count,
              COUNT(DISTINCT author) as active_devs
            FROM project_updates
            WHERE chat_id = %s AND timestamp >= %s
        """, (str(target_chat_id), today_start_utc))
        today_row = c.fetchone()
        today_lines_added = int(today_row[0] or 0)
        today_lines_removed = int(today_row[1] or 0)
        today_files_changed = int(today_row[2] or 0)
        today_commits = int(today_row[3] or 0)
        today_active_devs = int(today_row[4] or 0)
        today_net_lines = today_lines_added - today_lines_removed

        # Calculate percentages for today
        today_active_percentage = round((today_active_devs / max(1, total_developers)) * 100, 1)
        today_change_percentage = 0
        # Calculate yesterday's stats for comparison
        c.execute("""
            SELECT
              COALESCE(SUM(lines_added + lines_removed),0) as total_changes,
              COALESCE(SUM(files_added + files_modified + files_removed),0) as files_changed
            FROM project_updates
            WHERE chat_id = %s AND timestamp >= %s AND timestamp < %s
        """, (str(target_chat_id), yesterday_start_utc, today_start_utc))
        yesterday_row = c.fetchone()
        yesterday_total = int(yesterday_row[0] or 0)
        yesterday_files = int(yesterday_row[1] or 0)
        
        today_total = today_lines_added + today_lines_removed
        if yesterday_total > 0:
            today_change_percentage = round(((today_total - yesterday_total) / yesterday_total) * 100, 1)
        elif today_total > 0:
            today_change_percentage = 100

        # 2) Week-to-date totals
        c.execute("""
            SELECT
              COALESCE(SUM(lines_added),0) as lines_added,
              COALESCE(SUM(lines_removed),0) as lines_removed,
              COUNT(*) as commits_count
            FROM project_updates
            WHERE chat_id = %s AND timestamp >= %s
        """, (str(target_chat_id), week_start_utc))
        week_row = c.fetchone()
        week_lines_added = int(week_row[0] or 0)
        week_lines_removed = int(week_row[1] or 0)
        week_commits = int(week_row[2] or 0)
        week_lines_changed = week_lines_added + week_lines_removed
        week_net_lines = week_lines_added - week_lines_removed

        # 3) Last 7 days daily breakdown
        daily_lines_added = []
        daily_lines_removed = []
        daily_files_modified = []
        labels = []
        for i in range(6, -1, -1):
            date_ist = now_ist - timedelta(days=i)
            labels.append(date_ist.strftime('%a'))
            day_start_utc = datetime(date_ist.year, date_ist.month, date_ist.day, 0,0,0, tzinfo=IST).astimezone(pytz.UTC)
            day_end_utc = day_start_utc + timedelta(days=1)
            c.execute("""
                SELECT COALESCE(SUM(lines_added),0), COALESCE(SUM(lines_removed),0), 
                       COALESCE(SUM(files_modified),0)
                FROM project_updates
                WHERE chat_id = %s AND timestamp >= %s AND timestamp < %s
            """, (str(target_chat_id), day_start_utc, day_end_utc))
            rr = c.fetchone()
            daily_lines_added.append(int(rr[0] or 0))
            daily_lines_removed.append(int(rr[1] or 0))
            daily_files_modified.append(int(rr[2] or 0))

        # 4) churn ratio
        churn_ratio = (week_lines_removed / (week_lines_added + week_lines_removed)) if (week_lines_added + week_lines_removed) > 0 else 0.0

        # 5) velocity
        velocity_today_per_dev = (today_net_lines / max(1, today_active_devs)) if today_active_devs > 0 else 0
        velocity_week_per_dev = (week_net_lines / max(1, len(developers))) if len(developers) > 0 else 0
        
        # Calculate velocity score (0-100)
        velocity_score = min(100, max(0, round(velocity_week_per_dev / 100 * 100, 0)))  # Normalized to 0-100
        velocity_change = 0  # Default for now

        # 6) Calculate progress percentages
        # Today's progress - based on commits vs average
        avg_daily_commits = week_commits / 7 if week_commits > 0 else 1
        today_progress = min(100, round((today_commits / avg_daily_commits) * 100, 0))
        
        # Weekly progress - based on week vs previous week
        prev_week_start_utc = week_start_utc - timedelta(weeks=1)
        c.execute("""
            SELECT COUNT(*) as commits_count
            FROM project_updates
            WHERE chat_id = %s AND timestamp >= %s AND timestamp < %s
        """, (str(target_chat_id), prev_week_start_utc, week_start_utc))
        prev_week_row = c.fetchone()
        prev_week_commits = int(prev_week_row[0] or 0) if prev_week_row else 0
        week_progress = min(100, round((week_commits / max(1, prev_week_commits)) * 100, 0)) if prev_week_commits > 0 else 100
        
        # Sprint progress (simplified - based on week completion)
        sprint_progress = min(100, round((now_ist.weekday() / 7) * 100, 0))

        # 7) Generate motivation messages
        motivation_messages = [
            "Great work team! Keep pushing those commits!",
            "Every line of code brings us closer to success!",
            "Teamwork makes the dream work! Keep collaborating!",
            "Innovation is happening - great job everyone!",
            "Your hard work is paying off. Keep it up!",
            "Quality code is being written. Excellent progress!",
            "The team is on fire today! 🔥"
        ]
        
        top_performer_messages = [
            "Leading the pack with exceptional contributions!",
            "Setting the standard for excellence this week!",
            "MVP material with outstanding performance!",
            "Consistently delivering top-tier work!",
            "A true rockstar of the development team!"
        ]

        motivation_title = random.choice(["🚀 Amazing Progress!", "⭐ Team Excellence", "💪 Outstanding Work"])
        motivation_message = random.choice(motivation_messages)
        top_performer_message = random.choice(top_performer_messages)

        # 8) Generate recent activities
        recent_activities = []
        c.execute("""
            SELECT author, repo_name, branch_name, summary, timestamp
            FROM project_updates
            WHERE chat_id = %s
            ORDER BY timestamp DESC
            LIMIT 10
        """, (str(target_chat_id),))
        
        activity_icons = ["fas fa-code", "fas fa-file-code", "fas fa-terminal", "fas fa-bug", "fas fa-check-circle"]
        activity_colors = ["#4361ee", "#4cc9f0", "#f72585", "#7209b7", "#3a0ca3"]
        
        for i, row in enumerate(c.fetchall()):
            activity = {
                'title': f"{row[0]} pushed to {row[1]}",
                'description': row[3][:50] + "..." if len(row[3]) > 50 else row[3],
                'time': row[4].astimezone(IST).strftime('%I:%M %p'),
                'icon': activity_icons[i % len(activity_icons)],
                'color': activity_colors[i % len(activity_colors)]
            }
            recent_activities.append(activity)

        # 9) Corporate leaderboard (composite using PRs, reviews, issues, speed, CI, cross-team)
        period_start = week_start_utc

        # merged PRs
        c.execute("""SELECT author, COUNT(*) AS merged_prs
                     FROM pull_requests
                     WHERE merged_at IS NOT NULL AND merged_at >= %s
                     GROUP BY author""", (period_start,))
        merged_rows = {r[0]: int(r[1]) for r in c.fetchall()}

        # reviews
        c.execute("""SELECT reviewer AS author, COUNT(*) AS reviews_done,
                    SUM(CASE WHEN r.state='APPROVED' THEN 1 ELSE 0 END) AS approvals
            FROM pr_reviews r
            JOIN pull_requests p ON r.pr_id = p.id
            WHERE r.submitted_at >= %s
            GROUP BY reviewer""", (period_start,))
        review_rows = {r[0]: {'reviews_done': int(r[1]), 'approvals': int(r[2])} for r in c.fetchall()}

        # issues closed
        c.execute("""SELECT closed_by AS author, COUNT(*) AS issues_closed,
                            SUM(CASE WHEN labels && ARRAY['bug'] THEN 1 ELSE 0 END) AS bugs_closed
                     FROM issues_closed
                     WHERE closed_at >= %s
                     GROUP BY closed_by""", (period_start,))
        issue_rows = {r[0]: {'issues_closed': int(r[1]), 'bugs_closed': int(r[2] or 0)} for r in c.fetchall()}

        # first review time per author (lower better)
        c.execute("""WITH first_review AS (
                       SELECT pr_id, MIN(submitted_at) AS first_review_at
                       FROM pr_reviews GROUP BY pr_id
                     )
                     SELECT p.author, AVG(EXTRACT(epoch FROM (fr.first_review_at - p.created_at))) AS avg_first_review_secs
                     FROM pull_requests p JOIN first_review fr ON fr.pr_id = p.id
                     WHERE p.created_at >= %s
                     GROUP BY p.author""", (period_start,))
        first_review_rows = {r[0]: float(r[1]) for r in c.fetchall()}

        # avg merge secs
        c.execute("""SELECT author, AVG(EXTRACT(epoch FROM (merged_at - created_at))) AS avg_merge_secs
                     FROM pull_requests
                     WHERE merged_at IS NOT NULL AND created_at >= %s
                     GROUP BY author""", (period_start,))
        merge_time_rows = {r[0]: float(r[1]) for r in c.fetchall()}

        # ci pass rates
        c.execute("""SELECT p.author,
                            SUM(CASE WHEN c.status='success' THEN 1 ELSE 0 END) AS passed,
                            COUNT(c.*) AS total
                     FROM pull_requests p
                     LEFT JOIN ci_results c ON c.pr_id = p.id
                     WHERE p.created_at >= %s
                     GROUP BY p.author""", (period_start,))
        ci_rows = {r[0]: {'passed': int(r[1] or 0), 'total': int(r[2] or 0)} for r in c.fetchall()}

        # cross-team reviews (reviewer != pr author)
        c.execute("""SELECT r.reviewer AS author, COUNT(*) AS cross_reviews
                     FROM pr_reviews r
                     JOIN pull_requests p ON r.pr_id = p.id
                     WHERE r.submitted_at >= %s AND r.reviewer <> p.author
                     GROUP BY r.reviewer""", (period_start,))
        cross_rows = {r[0]: int(r[1]) for r in c.fetchall()}

        # Also get commit stats for leaderboard
        c.execute("""SELECT author, COUNT(*) as commits, 
                            SUM(files_added + files_modified + files_removed) as files_changed
                     FROM project_updates
                     WHERE chat_id = %s AND timestamp >= %s
                     GROUP BY author""", (str(target_chat_id), period_start))
        commit_stats = {r[0]: {'commits': int(r[1] or 0), 'files_changed': int(r[2] or 0)} for r in c.fetchall()}

        authors = set(merged_rows) | set(review_rows) | set(issue_rows) | set(first_review_rows) | set(merge_time_rows) | set(ci_rows) | set(cross_rows) | set(commit_stats.keys())

        metrics = {}
        for a in authors:
            metrics[a] = {
                'merged_prs': merged_rows.get(a, 0),
                'reviews_done': review_rows.get(a, {}).get('reviews_done', 0),
                'approvals': review_rows.get(a, {}).get('approvals', 0),
                'issues_closed': issue_rows.get(a, {}).get('issues_closed', 0),
                'bugs_closed': issue_rows.get(a, {}).get('bugs_closed', 0),
                'avg_first_review_secs': first_review_rows.get(a, None),
                'avg_merge_secs': merge_time_rows.get(a, None),
                'ci_pass_rate': (ci_rows.get(a, {}).get('passed',0) / max(1, ci_rows.get(a, {}).get('total',0))) if ci_rows.get(a) else None,
                'cross_reviews': cross_rows.get(a, 0),
                'commits': commit_stats.get(a, {}).get('commits', 0),
                'files_changed': commit_stats.get(a, {}).get('files_changed', 0)
            }

        def normalize_map(vals):
            if not vals:
                return {}
            maxv = max(vals.values())
            if maxv == 0:
                return {k: 0.0 for k in vals}
            return {k: (v / maxv) for k,v in vals.items()}

        merged_map = {a: metrics[a]['merged_prs'] for a in authors}
        reviews_map = {a: metrics[a]['reviews_done'] for a in authors}
        issues_map = {a: metrics[a]['issues_closed'] for a in authors}
        cross_map = {a: metrics[a]['cross_reviews'] for a in authors}
        commits_map = {a: metrics[a]['commits'] for a in authors}
        files_map = {a: metrics[a]['files_changed'] for a in authors}
        first_review_map = {a: (metrics[a]['avg_first_review_secs'] if metrics[a]['avg_first_review_secs'] else None) for a in authors}
        merge_time_map = {a: (metrics[a]['avg_merge_secs'] if metrics[a]['avg_merge_secs'] else None) for a in authors}
        ci_map = {a: (metrics[a]['ci_pass_rate'] if metrics[a]['ci_pass_rate'] is not None else 0.0) for a in authors}

        n_merged = normalize_map(merged_map)
        n_reviews = normalize_map(reviews_map)
        n_issues = normalize_map(issues_map)
        n_cross = normalize_map(cross_map)
        n_commits = normalize_map(commits_map)
        n_files = normalize_map(files_map)
        n_ci = normalize_map(ci_map)

        def normalize_time_map(time_map):
            filtered = {k:v for k,v in time_map.items() if v is not None}
            if not filtered:
                return {k:0.0 for k in time_map}
            maxv = max(filtered.values())
            if maxv == 0:
                return {k:1.0 for k in filtered}
            scores = {}
            for k in time_map:
                v = time_map[k]
                if v is None:
                    scores[k] = 0.0
                else:
                    scores[k] = 1.0 - (v / maxv)
            return scores

        n_first_review = normalize_time_map(first_review_map)
        n_merge_time = normalize_time_map(merge_time_map)

        weights = {
            'merged_prs': 0.20,
            'reviews': 0.15,
            'issues': 0.10,
            'commits': 0.15,
            'files': 0.10,
            'first_review_speed': 0.08,
            'merge_speed': 0.07,
            'ci': 0.10,
            'cross_reviews': 0.05
        }

        leaderboard = []
        for a in sorted(authors):
            score = 0.0
            score += weights['merged_prs'] * n_merged.get(a, 0.0)
            score += weights['reviews'] * n_reviews.get(a, 0.0)
            score += weights['issues'] * n_issues.get(a, 0.0)
            score += weights['commits'] * n_commits.get(a, 0.0)
            score += weights['files'] * n_files.get(a, 0.0)
            score += weights['first_review_speed'] * n_first_review.get(a, 0.0)
            score += weights['merge_speed'] * n_merge_time.get(a, 0.0)
            score += weights['ci'] * n_ci.get(a, 0.0)
            score += weights['cross_reviews'] * n_cross.get(a, 0.0)

            leaderboard.append({
                'name': a,
                'score': round(score * 100, 2),
                'commits': metrics[a]['commits'],
                'files_changed': metrics[a]['files_changed'],
                'merged_prs': metrics[a]['merged_prs'],
                'reviews_done': metrics[a]['reviews_done'],
                'issues_closed': metrics[a]['issues_closed'],
                'ci_pass_rate': metrics[a].get('ci_pass_rate', None)
            })

        leaderboard.sort(key=lambda x: x['score'], reverse=True)

        # -- prepare template data
        template_data = {
            'org_title': org_title,
            'total_members': total_developers,
            'current_date': now_ist.strftime('%B %d, %Y'),
            'week_number': now_ist.isocalendar()[1],
            'today_stats': {
                'total_commits': today_commits,
                'files_changed': today_files_changed,
                'lines_added': today_lines_added,
                'lines_removed': today_lines_removed,
                'net_lines': today_net_lines,
                'commits': today_commits,
                'active_developers': today_active_devs,
                'active_percentage': today_active_percentage,
                'change_percentage': today_change_percentage,
                'velocity_per_dev': round(velocity_today_per_dev, 1),
                'velocity_score': velocity_score,
                'velocity_change': velocity_change,
                'confidence_exact': (today_lines_added + today_lines_removed + week_lines_changed) > 0
            },
            'daily_stats': {
                'labels': labels,
                'added': daily_lines_added,
                'removed': daily_lines_removed,
                'modified': daily_files_modified,
                'net': [daily_lines_added[i] - daily_lines_removed[i] for i in range(7)]
            },
            'leaderboard': leaderboard,
            'week_progress': {
                'lines_added': week_lines_added,
                'lines_removed': week_lines_removed,
                'net_lines': week_net_lines,
                'commits': week_commits,
                'lines_changed': week_lines_changed,
                'churn_ratio': round(churn_ratio, 3),
                'change_percentage': today_change_percentage  # Use today's change for now
            },
            'today_progress': today_progress,
            'week_progress_pct': week_progress,
            'sprint_progress': sprint_progress,
            'motivation_title': motivation_title,
            'motivation_message': motivation_message,
            'top_performer_message': top_performer_message,
            'recent_activities': recent_activities
        }

        conn.close()

        # Render the dashboard template from file if present
        try:
            with open('templates/dashboard.html', 'r', encoding='utf-8') as f:
                template = f.read()
            return render_template_string(template, **template_data)
        except FileNotFoundError:
            # minimal fallback
            return f"""
            <html><body style="font-family: Arial;">
            <h1>Dashboard - {html.escape(org_title)}</h1>
            <p>Today lines added: {template_data['today_stats']['lines_added']}</p>
            <p>Today lines removed: {template_data['today_stats']['lines_removed']}</p>
            <p>Leaderboard: {', '.join([x['name']+':'+str(x['score']) for x in leaderboard[:5]])}</p>
            </body></html>
            """, 200

    except Exception as e:
        print("dashboard error:", e)
        traceback.print_exc()
        conn.close()
        return "<h1>Dashboard Error</h1><p>See server logs.</p>", 500

# helpers
def get_date_boundaries():
    now_ist = datetime.now(IST)
    today_start_ist = IST.localize(datetime(now_ist.year, now_ist.month, now_ist.day, 0,0,0))
    today_start_utc = today_start_ist.astimezone(pytz.UTC)
    yesterday_start_utc = today_start_utc - timedelta(days=1)
    week_start_utc = today_start_utc - timedelta(days=now_ist.weekday())
    return today_start_utc, yesterday_start_utc, week_start_utc, now_ist

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status":"healthy","service":"GitSync Bot","timestamp": datetime.now(timezone.utc).isoformat()})

@app.route('/test-db', methods=['GET'])
def test_db():
    conn = get_db_connection()
    if conn:
        conn.close()
        return jsonify({"database": "connected"})
    return jsonify({"database": "disconnected"}), 500

with app.app_context():
    init_db()

if __name__ == '__main__':
    import os
    port = int(os.environ.get("PORT", 5000))
    # debug=False in production
    app.run(host='0.0.0.0', port=port, debug=False)