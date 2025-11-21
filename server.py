from flask import Flask, request, jsonify

app = Flask(__name__)

@app.route('/webhook', methods=['POST'])
def git_webhook():
    data = request.json
    
    # 1. Check if this is a Push event (it has 'commits')
    if 'commits' in data:
        # Extract simple details
        author_name = data['pusher']['name']
        repo_name = data['repository']['name']
        
        # Get all commit messages in this push
        commit_messages = [commit['message'] for commit in data['commits']]
        full_update = "\n".join(commit_messages)

        print("---------------------------------")
        print(f"👤 Author: {author_name}")
        print(f"📂 Repo: {repo_name}")
        print(f"📝 Updates: {full_update}")
        print("---------------------------------")
        
        # TODO: Send 'full_update' to AI here later
        
    return jsonify({"status": "success"}), 200

@app.route('/', methods=['GET'])
def home():
    return "GitSync Bot Server is Running!"

if __name__ == '__main__':
    app.run(port=5000, debug=True)