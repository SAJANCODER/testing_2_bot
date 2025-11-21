# GitSync Standup Bot for Zoho Cliq

GitSync automates daily standups by analyzing GitHub commits using Gemini AI and posting summaries to Zoho Cliq. It also features a live Team Velocity Dashboard.

## 🔥 Features
* **Automated Standups:** Webhook triggers from GitHub Push.
* **AI Summaries:** Uses Google Gemini 2.5 Pro to format commit logs into readable updates.
* **Team Analytics:** Visual dashboard showing commit velocity.
* **Native Integration:** Works via Zoho Cliq Slash Command `/dashboard`.

## 🛠️ Tech Stack
* **Backend:** Python (Flask)
* **AI:** Google Gemini API
* **Database:** SQLite
* **Frontend:** HTML5 + Chart.js (Embedded in Cliq)

## 🚀 How to Run
1.  Clone the repoe stand_up bot.
2.  Install dependencies: `pip install -r requirements.txt`
3.  Set up `.env` with `GOOGLE_API_KEY` and `CLIQ_WEBHOOK_URL`.
4.  Run `python server.py`.