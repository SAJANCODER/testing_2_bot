import google.generativeai as genai
from flask import Flask, request, jsonify
from dotenv import load_dotenv
import os
import requests
load_dotenv()
app = Flask(__name__)

# --- CONFIGURATION ---
# Paste your Gemini API Key here
GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY") 
CLIQ_WEBHOOK_URL = os.getenv("CLIQ_WEBHOOK_URL")


# --- SYSTEM SETUP ---
# Configure Gemini with the 2.5 Pro model you have access to
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel('gemini-2.5-pro')

def generate_standup_summary(commits):
    """
    Sends raw commit logs to Gemini to get a clean summary.
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
    
    Keep it concise, professional, and ready to post to a team chat.
    """
    try:
        response = model.generate_content(prompt)
        return response.text
    except Exception as e:
        return f"Error generating AI summary: {e}"

def send_to_cliq(text, author):
    """
    Sends the formatted AI summary to Zoho Cliq.
    """
    try:
        # Construct the message card for Cliq
        payload = {
            "text": f"### 🚀 GitSync Standup: {author}",
            "bot": {
                "name": "GitSync Bot",
                "image": "https://cdn-icons-png.flaticon.com/512/4712/4712109.png" 
            },
            "card": {
                "title": f"Daily Update: {author}",
                "theme": "modern-inline"
            },
            "slides": [
                {
                    "type": "text",
                    "data": text
                }
            ]
        }
        
        # Send the POST request
        r = requests.post(CLIQ_WEBHOOK_URL, json=payload)
        
        if r.status_code == 200 or r.status_code == 204:
            print(f"📨 Sent to Cliq: Success (Status {r.status_code})")
        else:
            print(f"❌ Cliq Error: {r.status_code} - {r.text}")
            
    except Exception as e:
        print(f"❌ Error sending to Cliq: {e}")

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    
    # Check if the data is a Push event
    if 'commits' in data:
        author_name = data['pusher']['name']
        commit_messages = [commit['message'] for commit in data['commits']]
        full_raw_update = "\n".join(commit_messages)

        print(f"\n🔄 Processing commits from {author_name}...")
        
        # Step 1: Generate AI Summary
        ai_summary = generate_standup_summary(full_raw_update)
        
        print("---------------------------------")
        print(f"🤖 GENERATED STANDUP FOR {author_name.upper()}:")
        print(ai_summary)
        print("---------------------------------")
        
        # Step 2: Send to Zoho Cliq
        send_to_cliq(ai_summary, author_name)
        
    return jsonify({"status": "success"}), 200

if __name__ == '__main__':
    # Run on standard Flask port 5000
    app.run(port=5000, debug=True)