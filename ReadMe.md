# 🚀 GitSync Standup Bot

### *AI-powered daily standups generated automatically from your GitHub commits.*

<p align="center">
  <img src="https://img.shields.io/badge/Python-3.10%2B-blue?logo=python" />
  <img src="https://img.shields.io/badge/Flask-Framework-black?logo=flask" />
  <img src="https://img.shields.io/badge/Google%20Gemini-2.5%20Pro-orange?logo=google" />
  <img src="https://img.shields.io/badge/PostgreSQL-Neon%20DB-336791?logo=postgresql" />
  <img src="https://img.shields.io/badge/Telegram-Bot-blue?logo=telegram" />
  <img src="https://img.shields.io/badge/Deploy-Render-46E3B7?logo=render" />
  <img src="https://img.shields.io/badge/License-MIT-green" />
</p>

---

## 📌 Overview

**GitSync Standup Bot** is an automated AI-powered standup assistant that turns your GitHub commits into concise, professional daily reports delivered directly to your Telegram group.

It listens for GitHub push events, uses **Google Gemini 2.5 Pro** to analyze code changes, and generates human-readable summaries. Perfect for teams who want automated tracking, clean summaries, and zero manual reporting.

---

## ✨ Features

- **🔗 Automated Tracking**: Listens for GitHub push events via Webhook.
- **🤖 AI Analysis**: Uses Google Gemini 2.5 Pro to analyze code changes and generate human-readable summaries.
- **🗄️ Persistent Storage**: Securely stores webhook configurations and logs using PostgreSQL (Neon DB).
- **🎨 Smart Formatting**: Delivers beautifully formatted HTML reports to Telegram.
- **📊 Team Dashboard**: Includes a live web dashboard (`/dashboard`) to view team velocity and commit history (auto-converted to IST).
- **🛡️ Secure Setup**: Uses unique, per-group secret keys to prevent unauthorized access.

---

## 🛠️ Tech Stack

- **Backend**: Python (Flask, Gunicorn)
- **Database**: PostgreSQL (via `psycopg2-binary`)
- **AI Model**: Google Gemini API (`google-generativeai`)
- **Hosting**: Render (Web Service) + Neon (Database)

---

## ⚙️ Setup Guide

### 1. Prerequisites

- **Telegram Bot**: Create a bot via [@BotFather](https://t.me/BotFather) and get the Token.
- **Gemini API Key**: Get a free API key from [Google AI Studio](https://aistudio.google.com/).
- **PostgreSQL Database**: A connection string (e.g., from Neon.tech Free Tier).

### 2. Deployment (Render)

1. **Fork/Clone** this repository.
2. Create a **New Web Service** on Render connected to your repo.
3. Set the **Build Command**: `pip install -r requirements.txt`
4. Set the **Start Command**: `gunicorn server:app`
5. Add the following **Environment Variables**:

| Variable | Description |
| :--- | :--- |
| `GOOGLE_API_KEY` | Your Google Gemini API Key. |
| `TELEGRAM_BOT_TOKEN_FOR_COMMANDS` | Your Telegram Bot Token. |
| `DATABASE_URL` | PostgreSQL Connection String (e.g., `postgres://user:pass@host/db`). |
| `APP_BASE_URL` | Your Render App URL (e.g., `https://your-app-e-.onrender.com`). |

### 3. Bot Activation

1. Add your bot to a Telegram Group.
2. Make the bot an **Administrator** (required to post messages).
3. Send the command `/gitsync` inside the group.
4. The bot will reply with a secure Webhook URL.
5. Go to your GitHub Repository -> **Settings** -> **Webhooks**.
6. Add a new webhook with:
    - **Payload URL**: (Paste the URL from the bot)
    - **Content type**: `application/json`
    - **Events**: Just the `push` event.

---

## 📊 Usage

- `/gitsync`: Generates a new secure webhook link for the current group.
- `/dashboard`: Returns a link to the public analytics dashboard showing commit history and team stats, each team personalized dashboard.
- `/start`: Guides you to operate the bot, add bot to your group and set up webhook.

---

## 📂 Project Structure

```
.
├── server.py           # Main application logic (Flask + AI + DB)
├── requirements.txt    # Python dependencies
├── Procfile            # Gunicorn startup configuration
└── README.md           # project documentation
```

---

## 🛡️ Security Note

> This project uses a Secret Key validation mechanism. Every time `/gitsync` is run, a new UUID is generated and linked specifically to that Telegram Group ID in the database. Webhooks without a matching key/ID will be rejected.