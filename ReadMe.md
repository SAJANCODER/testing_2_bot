🚀 GitSync Standup Bot

An automated AI-powered standup assistant that turns your GitHub commits into concise, professional daily reports delivered directly to your Telegram group.

✨ Features

Automated Tracking: Listens for GitHub push events via Webhook.

AI Analysis: Uses Google Gemini 2.5 Pro to analyze code changes and generate human-readable summaries.

Persistent Storage: Securely stores webhook configurations and logs using PostgreSQL (Neon DB).

Smart Formatting: Delivers beautifully formatted HTML reports to Telegram.

Team Dashboard: Includes a live web dashboard (/dashboard) to view team velocity and commit history (auto-converted to IST).

Secure Setup: Uses unique, per-group secret keys to prevent unauthorized access.

🛠️ Tech Stack

Backend: Python (Flask, Gunicorn)

Database: PostgreSQL (via psycopg2-binary)

AI Model: Google Gemini API (google-generativeai)

Hosting: Render (Web Service) + Neon (Database)

⚙️ Setup Guide

1. Prerequisites

Telegram Bot: Create a bot via @BotFather and get the Token.

Gemini API Key: Get a free API key from Google AI Studio.

PostgreSQL Database: A connection string (e.g., from Neon.tech Free Tier).

2. Deployment (Render)

Fork/Clone this repository.

Create a New Web Service on Render connected to your repo.

Set the Build Command: pip install -r requirements.txt

Set the Start Command: gunicorn server:app

Add the following Environment Variables:

Variable

Description

GOOGLE_API_KEY

Your Google Gemini API Key.

TELEGRAM_BOT_TOKEN_FOR_COMMANDS

Your Telegram Bot Token.

DATABASE_URL

PostgreSQL Connection String (e.g., postgres://user:pass@host/db).

APP_BASE_URL

Your Render App URL (e.g., https://your-app.onrender.com).

3. Bot Activation

Add your bot to a Telegram Group.

Make the bot an Administrator (required to post messages).

Send the command /gitsync inside the group.

The bot will reply with a secure Webhook URL.

Go to your GitHub Repository -> Settings -> Webhooks.

Add a new webhook with:

Payload URL: (Paste the URL from the bot)

Content type: application/json

Events: Just the push event.

📊 Usage

/gitsync: Generates a new secure webhook link for the current group.

/dashboard: Returns a link to the public analytics dashboard showing commit history and team stats.

📂 Project Structure

.
├── server.py           # Main application logic (Flask + AI + DB)
├── requirements.txt    # Python dependencies
├── Procfile            # Gunicorn startup configuration
└── README.md           # Project documentation


🛡️ Security Note

This project uses a Secret Key validation mechanism. Every time /gitsync is run, a new UUID is generated and linked specifically to that Telegram Group ID in the database. Webhooks without a matching key/ID