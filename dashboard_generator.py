from datetime import datetime, timedelta
import json
import html as html_module

def calculate_progress_offset(percentage):
    circumference = 2 * 3.14159 * 60
    return circumference - (percentage / 100 * circumference)

def generate_dashboard_html(target_chat_id, conn):
    IST_OFFSET = timedelta(hours=5, minutes=30)
    now_utc = datetime.utcnow()
    now_ist = now_utc + IST_OFFSET

    today_str = now_ist.strftime('%Y-%m-%d')
    yesterday_str = (now_ist - timedelta(days=1)).strftime('%Y-%m-%d')
    week_ago_str = (now_ist - timedelta(days=7)).strftime('%Y-%m-%d')

    try:
        c = conn.cursor()

        c.execute("""
            SELECT author, summary, timestamp, repo_name, branch_name,
                   files_added, files_modified, files_removed
            FROM project_updates
            WHERE chat_id = %s
            ORDER BY timestamp DESC
        """, (str(target_chat_id),))
        all_updates = c.fetchall()

        c.execute("""
            SELECT repo_name
            FROM project_updates
            WHERE chat_id = %s
            ORDER BY timestamp DESC
            LIMIT 1
        """, (str(target_chat_id),))
        repo_result = c.fetchone()
        org_title = repo_result[0] if repo_result and repo_result[0] else "Your Organization"

    except Exception as e:
        print(f"❌ Dashboard query error: {e}")
        return f"<html><body><h1>Error loading dashboard: {e}</h1></body></html>"

    today_commits_list = []
    yesterday_commits_list = []
    week_commits_list = []

    today_logs_html = []
    yesterday_logs_html = []
    week_logs_html = []

    daily_data = {}
    author_weekly_stats = {}

    for update in all_updates:
        author = update[0]
        summary = update[1]
        db_utc_time = update[2]
        repo = update[3] if update[3] else "Unknown"
        branch = update[4] if update[4] else "main"
        files_added = update[5] if len(update) > 5 and update[5] else 0
        files_modified = update[6] if len(update) > 6 and update[6] else 0
        files_removed = update[7] if len(update) > 7 and update[7] else 0

        ist_time = db_utc_time + IST_OFFSET
        db_date_str = ist_time.strftime('%Y-%m-%d')
        display_timestamp = ist_time.strftime('%I:%M %p')
        display_date = ist_time.strftime('%b %d')

        summary_clean = summary.replace('<b>', '').replace('</b>', '').replace('<i>', '').replace('</i>', '')
        summary_clean = summary_clean.replace('<code>', '').replace('</code>', '').replace('<br>', '\n')
        summary_short = summary_clean[:200] + '...' if len(summary_clean) > 200 else summary_clean

        item_html = f'''
        <div class="update-item">
            <div class="update-meta">
                <span class="author">👤 {html_module.escape(author)}</span>
                <span>🕒 {display_timestamp}</span>
                <span>📂 {html_module.escape(repo)}</span>
                <span>🌿 {html_module.escape(branch)}</span>
            </div>
            <div class="update-summary">{html_module.escape(summary_short)}</div>
        </div>
        '''

        if db_date_str == today_str:
            today_commits_list.append(update)
            today_logs_html.append(item_html)
        elif db_date_str == yesterday_str:
            yesterday_commits_list.append(update)
            yesterday_logs_html.append(item_html)

        if db_date_str >= week_ago_str:
            week_commits_list.append(update)
            if db_date_str not in [today_str, yesterday_str]:
                week_logs_html.append(item_html)

            if display_date not in daily_data:
                daily_data[display_date] = {'added': 0, 'modified': 0, 'removed': 0}
            daily_data[display_date]['added'] += files_added
            daily_data[display_date]['modified'] += files_modified
            daily_data[display_date]['removed'] += files_removed

            if author not in author_weekly_stats:
                author_weekly_stats[author] = {
                    'commits': 0,
                    'files_added': 0,
                    'files_modified': 0,
                    'files_removed': 0,
                    'total_impact': 0
                }
            author_weekly_stats[author]['commits'] += 1
            author_weekly_stats[author]['files_added'] += files_added
            author_weekly_stats[author]['files_modified'] += files_modified
            author_weekly_stats[author]['files_removed'] += files_removed
            author_weekly_stats[author]['total_impact'] += files_added + files_modified + files_removed

    today_commits = len(today_commits_list)
    yesterday_commits = len(yesterday_commits_list)
    weekly_commits = len(week_commits_list)

    if yesterday_commits > 0:
        change_pct = ((today_commits - yesterday_commits) / yesterday_commits) * 100
        if change_pct > 0:
            today_change_text = f"↑ {abs(change_pct):.0f}% from yesterday"
            today_change_class = "positive"
        elif change_pct < 0:
            today_change_text = f"↓ {abs(change_pct):.0f}% from yesterday"
            today_change_class = "negative"
        else:
            today_change_text = "Same as yesterday"
            today_change_class = "neutral"
    else:
        today_change_text = "No data from yesterday"
        today_change_class = "neutral"

    prev_week_start = (now_ist - timedelta(days=14)).strftime('%Y-%m-%d')
    prev_week_end = week_ago_str

    try:
        c.execute("""
            SELECT COUNT(*)
            FROM project_updates
            WHERE chat_id = %s
            AND DATE(timestamp + INTERVAL '5 hours 30 minutes') >= %s
            AND DATE(timestamp + INTERVAL '5 hours 30 minutes') < %s
        """, (str(target_chat_id), prev_week_start, prev_week_end))
        prev_weekly_commits = c.fetchone()[0]
    except:
        prev_weekly_commits = 0

    if prev_weekly_commits > 0:
        week_change_pct = ((weekly_commits - prev_weekly_commits) / prev_weekly_commits) * 100
        if week_change_pct > 0:
            weekly_change_text = f"↑ {abs(week_change_pct):.0f}% from last week"
            weekly_change_class = "positive"
        elif week_change_pct < 0:
            weekly_change_text = f"↓ {abs(week_change_pct):.0f}% from last week"
            weekly_change_class = "negative"
        else:
            weekly_change_text = "Same as last week"
            weekly_change_class = "neutral"
    else:
        weekly_change_text = "First week of data"
        weekly_change_class = "neutral"

    active_contributors = len(author_weekly_stats)

    try:
        c.execute("""
            SELECT COALESCE(SUM(files_added), 0) + COALESCE(SUM(files_modified), 0) + COALESCE(SUM(files_removed), 0)
            FROM project_updates
            WHERE chat_id = %s
        """, (str(target_chat_id),))
        total_files_changed = c.fetchone()[0]
    except:
        total_files_changed = 0

    sorted_daily = sorted(daily_data.items(), key=lambda x: datetime.strptime(x[0], '%b %d').replace(year=now_ist.year))
    daily_labels = [day for day, _ in sorted_daily[-7:]]
    daily_added = [data['added'] for _, data in sorted_daily[-7:]]
    daily_modified = [data['modified'] for _, data in sorted_daily[-7:]]
    daily_removed = [data['removed'] for _, data in sorted_daily[-7:]]

    daily_chart_data = {
        'labels': daily_labels,
        'added': daily_added,
        'modified': daily_modified,
        'removed': daily_removed
    }

    today_target = 10
    today_progress = min(100, (today_commits / today_target) * 100) if today_target > 0 else 0
    today_progress_offset = calculate_progress_offset(today_progress)

    weekly_target = 50
    week_progress = min(100, (weekly_commits / weekly_target) * 100) if weekly_target > 0 else 0
    week_progress_offset = calculate_progress_offset(week_progress)

    quality_score = min(100, 85 + (active_contributors * 3))
    quality_progress_offset = calculate_progress_offset(quality_score)

    sorted_leaders = sorted(author_weekly_stats.items(), key=lambda x: x[1]['total_impact'], reverse=True)

    leaderboard_html = ""
    if sorted_leaders:
        for idx, (author, stats) in enumerate(sorted_leaders[:10], 1):
            rank_class = f"rank-{idx}" if idx <= 3 else ""
            avatar_letter = author[0].upper() if author else "?"

            leaderboard_html += f'''
            <div class="leaderboard-item {rank_class}">
                <div class="rank">#{idx}</div>
                <div class="avatar">{avatar_letter}</div>
                <div class="leader-info">
                    <div class="leader-name">{html_module.escape(author)}</div>
                    <div class="leader-commits">{stats['commits']} commits • {stats['files_added']} added • {stats['files_modified']} modified • {stats['files_removed']} removed</div>
                </div>
                <div class="leader-score">{stats['total_impact']}</div>
            </div>
            '''
    else:
        leaderboard_html = '<div class="empty-state"><p>No activity this week</p></div>'

    if not today_logs_html:
        today_logs_html = ['<div class="empty-state"><p>No commits today yet. Time to ship something great!</p></div>']
    if not yesterday_logs_html:
        yesterday_logs_html = ['<div class="empty-state"><p>No commits yesterday</p></div>']
    if not week_logs_html:
        week_logs_html = ['<div class="empty-state"><p>No earlier commits this week</p></div>']

    with open('dashboard_template.html', 'r') as f:
        template = f.read()

    html_output = template.replace('{{org_title}}', html_module.escape(org_title))
    html_output = html_output.replace('{{today_commits}}', str(today_commits))
    html_output = html_output.replace('{{active_contributors}}', str(active_contributors))
    html_output = html_output.replace('{{total_files_changed}}', str(total_files_changed))
    html_output = html_output.replace('{{weekly_commits}}', str(weekly_commits))
    html_output = html_output.replace('{{today_change_text}}', today_change_text)
    html_output = html_output.replace('{{today_change_class}}', today_change_class)
    html_output = html_output.replace('{{weekly_change_text}}', weekly_change_text)
    html_output = html_output.replace('{{weekly_change_class}}', weekly_change_class)
    html_output = html_output.replace('{{today_progress}}', str(int(today_progress)))
    html_output = html_output.replace('{{today_progress_offset}}', str(int(today_progress_offset)))
    html_output = html_output.replace('{{week_progress}}', str(int(week_progress)))
    html_output = html_output.replace('{{week_progress_offset}}', str(int(week_progress_offset)))
    html_output = html_output.replace('{{quality_score}}', str(int(quality_score)))
    html_output = html_output.replace('{{quality_progress_offset}}', str(int(quality_progress_offset)))
    html_output = html_output.replace('{{leaderboard_html}}', leaderboard_html)
    html_output = html_output.replace('{{daily_chart_data}}', json.dumps(daily_chart_data))
    html_output = html_output.replace('{{today_logs}}', '\n'.join(today_logs_html))
    html_output = html_output.replace('{{yesterday_logs}}', '\n'.join(yesterday_logs_html))
    html_output = html_output.replace('{{week_logs}}', '\n'.join(week_logs_html))

    return html_output
