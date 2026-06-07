# Sellora — AI Sales Bot for Zero1

Smart Telegram sales bot powered by Groq AI. Handles clients 24/7, posts to your channel automatically every 6 hours, and gives the owner powerful management tools.

## Setup

### 1. Set your secrets/environment variables
In Replit Secrets (or a `.env` file locally):
```
SELLORA_BOT_TOKEN=your_sellora_bot_token
GROQ_API_KEY=your_groq_api_key
OWNER_ID=your_personal_telegram_chat_id
CHANNEL_USERNAME=@your_channel_username
```

- **SELLORA_BOT_TOKEN** — get from @BotFather → /newbot
- **GROQ_API_KEY** — get from https://console.groq.com
- **OWNER_ID** — message @userinfobot on Telegram to get your numeric ID
- **CHANNEL_USERNAME** — your channel like `@zero1_uz`

### 2. Add bot as channel admin
Telegram channel → Settings → Administrators → Add Admin → select your bot.
Give permissions: **Post Messages** + **Pin Messages**

### 3. Install dependencies
```
pip install -r requirements.txt
```

### 4. Run
```
python main.py
```

## Publishing Checklist
- ✅ Bot token from @BotFather
- ✅ Groq API key from console.groq.com
- ✅ Bot added as channel admin (post + pin permissions)
- ✅ All 4 env vars configured
- ✅ `pip install -r requirements.txt` done
- ✅ `python main.py` running

## Commands

### Public (clients)
| Command | Description |
|---|---|
| /start | Welcome message, opens sales flow |
| /services | All Zero1 services with prices |
| /contact | Website + Telegram + phone |
| /reset | Clear conversation history |
| /help | Show all commands |

### Owner Only (your chat ID only, clients cannot use)
| Command | Description |
|---|---|
| /task `<text>` | Save a quick task reminder |
| /tasks | View all saved tasks |
| /clear | Clear all tasks |
| /status | Uptime, users, memory, queue stats |
| /post `<text>` | Post immediately to channel |
| /schedule `<text>` | Add post to queue (sent next cycle) |
| /channel_stats | Channel subscriber count + info |
| /ideas | Generate 5 AI post ideas |
| /summarize | AI summary of your last messages |

### Forwarded Messages (owner only)
Forward any message to Sellora → instant AI analysis:
- 🎯 Lead / ⚠️ Complaint / 🗑️ Spam / 💬 General classification
- One-line summary
- 3 ready-to-send reply options
- Matching Zero1 service if it's a lead

## Channel Auto-Posting
Bot posts to your channel every 6 hours automatically. Posts rotate through 6 types:
1. Service highlight
2. Tech tip
3. Client case study
4. Engagement question
5. Behind the scenes
6. AI tip for business

Use `/schedule` to queue custom posts — they're sent before AI-generated ones.
