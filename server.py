from flask import Flask, request, jsonify

app = Flask(__name__)

# This is the endpoint GitHub will hit when code is committed
@app.route('/webhook', methods=['POST'])
def git_webhook():
    print("🔔 Webhook Triggered!")
    
    # Get the JSON data sent by GitHub
    data = request.json
    
    # For now, just print it to the console to prove it works
    # We will add AI logic here in Phase 3
    if data:
        print(f"Data received from repository.")
    
    return jsonify({"status": "success", "message": "Webhook received"}), 200

# Home route just to check if server is alive
@app.route('/', methods=['GET'])
def home():
    return "GitSync Bot Server is Running!"

if __name__ == '__main__':
    # Running on port 5000
    app.run(port=5000, debug=True)