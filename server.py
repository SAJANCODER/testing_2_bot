import google.generativeai as genai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
app = Flask(__name__)

# --- CONFIGURATION ---
# Paste your Gemini API Key here
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") # Loaded from .env file

# Configure Gemini
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-1.5-flash')

def generate_standup_summary(commits):
    """
    Sends commits to Gemini and gets a standup summary.
    """
    prompt = f"""
    You are an Agile Scrum Assistant. 
    Analyze these git commit messages and convert them into a daily standup update.
    
    COMMITS:
    {commits}
    
    OUTPUT FORMAT:
    * **Completed:** (Summarize the work done in 1-2 clear bullet points)
    * **Technical Context:** (Briefly mention libraries/files touched if obvious)
    * **Potential Blockers:** (If the commits mention 'fix', 'error', or 'debug', note that a bug was resolved. Otherwise say 'None')
    
    Keep it concise and professional.
    """
    
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI summary: {e}"

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    
    if 'commits' in data:
        author_name = data['pusher']['name']
        repo_name = data['repository']['name']
        commit_messages = [commit['message'] for commit in data['commits']]
        full_raw_update = "\n".join(commit_messages)

        print(f"\n🔄 Processing commits from {author_name}...")

        # --- AI MAGIC HAPPENS HERE ---
        ai_summary = generate_standup_summary(full_raw_update)
        
        print("---------------------------------")
        print(f"🤖 GENERATED STANDUP FOR {author_name.upper()}:")
        print(ai_summary)
        print("---------------------------------")
        
    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    app.run(port=5000, debug=True)