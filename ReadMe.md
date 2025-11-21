# 🚀 GitSync Standups  
**Automated AI-powered Standups from Git Commits + Team Velocity Analytics**

GitSync Standups converts GitHub/GitLab commits into **smart AI-generated standup updates** and posts them automatically into **Zoho Cliq channels**.  
It also includes **blocker detection**, **PR insights**, and **team velocity dashboards, intensive**.

This project is built for the **Zoho Cliq Hackathon/Contest**.

---

## ✨ Features

### 🔄 Automated Standups
- Triggered automatically by GitHub/GitLab webhooks  
- Summarizes commits for each developer  
- Groups activity into:  
  - **Summary**  
  - **Next steps**  
  - **Blockers (auto-detected + AI inferred)**  

### 🤖 AI-Powered Processing
- AI generates concise, human-like standup updates  
- Detects blockers using:
  - Commit patterns  
  - PR delays  
  - CI failures  
  - Keyword-based heuristics  

### 📊 Team Velocity Analytics
- Tracks daily team activity  
- Commits/day visualization  
- PR lead time  
- Blocker trends  
- Can be integrated into Grafana / Metabase  

### 💬 Zoho Cliq Integration
- Posts formatted summary directly to chosen channel  
- Works via **Cliq Incoming Webhook** or full **Zoho Cliq Bot**  

---

## 🛠️ Architecture

GitHub/GitLab
↓ (Webhook)
Node.js Webhook Receiver (Express)
↓
Commit Parser + Metadata Enrichment
↓
AI Standup Generator (LLM)
↓
Zoho Cliq Bot/Webhook → Team Channel
↓
Analytics Database → Velocity Dashboard