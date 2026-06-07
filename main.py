"""
Sellora — AI Sales Bot for Zero1
Full-featured: lead CRM + smart funnel, interactive /post wizard,
4x/day channel scheduler, owner notifications, dual AI modes.
"""

import os, re, time, logging, traceback, psutil
from datetime import datetime
from dotenv import load_dotenv
from pytz import timezone
from groq import AsyncGroq
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters,
)

load_dotenv()

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("sellora")

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.environ.get("SELLORA_BOT_TOKEN") or os.environ["BOT_TOKEN"]
GROQ_API_KEY       = os.environ["GROQ_API_KEY"]
GROQ_MODEL         = "llama-3.3-70b-versatile"
OWNER_CHAT_ID      = int(os.environ.get("OWNER_ID", "6597203199"))
CHANNEL_USERNAME   = os.environ.get("CHANNEL_USERNAME", "")

TZ            = timezone("Asia/Tashkent")
START_TIME    = time.time()
autopost_on   = True          # toggled by /autopost on/off

# ─── Groq client ──────────────────────────────────────────────────────────────
groq_client = AsyncGroq(api_key=GROQ_API_KEY)

# ─── In-memory storage ────────────────────────────────────────────────────────
conversations : dict[int, list[dict]] = {}   # chat_id → messages
leads         : dict[int, dict]       = {}   # chat_id → lead profile
tasks         : list[dict]            = []   # owner task reminders
post_queue    : list[str]             = []   # custom posts queue
known_users   : set[int]              = set()

# State tracking
user_state    : dict[int, str]  = {}   # chat_id → state key
post_wizard   : dict[str, any]  = {}   # owner post-wizard data
channel_cfg   : dict            = {"username": CHANNEL_USERNAME}  # runtime channel

# Rotation counters
post_type_idx    = 0
service_idx      = 0
POST_TYPES       = ["service_ad", "tech_tip", "social_proof", "engagement"]
SERVICES         = ["Telegram Bot (basic, $150+)", "Telegram Bot (AI-powered, $400+)",
                    "Landing Page ($200+)", "Business Website ($500+)",
                    "AI Chatbot ($600+)", "Automation Tool ($300+)", "Web App ($800+)"]

# ─── States ───────────────────────────────────────────────────────────────────
S_NORMAL         = "normal"
S_LEAD_Q1        = "lead_q1"    # asked about service
S_LEAD_Q2        = "lead_q2"    # asked about type/budget/timeline
S_LEAD_Q3        = "lead_q3"    # asked for contact
S_POST_TYPE      = "post_type"
S_POST_DETAILS   = "post_details"
S_POST_CONFIRM   = "post_confirm"
S_CHANNEL_SETUP  = "channel_setup"

# ─── Keyword scoring ──────────────────────────────────────────────────────────
HOT_KW = [
    "kerak","нужно","need","want","хочу","order","buyurtma","заказ",
    "price","narx","цена","сколько","how much","qancha",
    "urgent","tez","срочно","asap","budget","byudjet","бюджет",
    "when can","qachon","когда","start","boshlash","начать",
    "contact","bog'lan","связаться",
]
WARM_KW = [
    "maybe","balki","возможно","thinking","o'ylayapman","думаю",
    "option","variant","compare","taqqosla","сравнить",
    "service","xizmat","услуга","interested","qiziqaman","интересует",
    "tell me","aytib","расскажи",
]
PHONE_RE    = re.compile(r'(\+?[0-9]{9,13})')
USERNAME_RE = re.compile(r'@([a-zA-Z][a-zA-Z0-9_]{3,})')

# ─── System prompts ───────────────────────────────────────────────────────────
CLIENT_PROMPT = """You are Sellora — the warm, smart AI sales assistant for Zero1, a software company.

Zero1 services:
1. Telegram Bot (basic) — $150+
2. Telegram Bot (AI-powered) — $400+
3. Landing Page — $200+
4. Full Business Website — $500+
5. AI Chatbot for Business — $600+
6. Automation Tool — $300+
7. Full Web App — $800+

Every project: 3-7 day delivery, modern design, mobile responsive, multilingual (EN/UZ/RU), free revisions, ongoing support.

Sales flow: understand their need → match service → explain benefits → share price → handle objections → close.
Closing CTA: https://zero1premium.netlify.app | @zero1_uz | @akobircs | +998774463446

Language rule: detect language (EN/RU/UZ) and reply in that language only. Never mix.
Tone: warm, confident, professional. Max 4-5 sentences unless explaining."""

OWNER_PROMPT = """You are Sellora — sharp AI assistant for Akobir, founder of Zero1.
Help with: lead analysis, message classification, reply suggestions, business decisions.
Zero1 builds: Telegram Bots ($150-400+), Websites ($200-500+), AI Chatbots ($600+), Tools ($300+), Web Apps ($800+).
Tone: direct, smart, like a co-founder. Reply in whatever language Akobir writes."""

CHANNEL_SYSTEM = """You are a professional content creator for Zero1, a software company in Uzbekistan.
Write engaging Telegram channel posts. Use emojis naturally. Max 180 words. Be persuasive and clear.
Always include CTA pointing to @zero1_uz at the end."""

# ─── Helpers ──────────────────────────────────────────────────────────────────
def is_owner(chat_id: int) -> bool:
    return chat_id == OWNER_CHAT_ID

def get_state(chat_id: int) -> str:
    return user_state.get(chat_id, S_NORMAL)

def set_state(chat_id: int, state: str):
    user_state[chat_id] = state

def get_history(chat_id: int) -> list[dict]:
    if chat_id not in conversations:
        conversations[chat_id] = []
    return conversations[chat_id]

def add_message(chat_id: int, role: str, content: str):
    h = get_history(chat_id)
    h.append({"role": role, "content": content})
    limit = 20 if is_owner(chat_id) else 12
    if len(h) > limit:
        conversations[chat_id] = h[2:]

def clear_history(chat_id: int):
    conversations[chat_id] = []

def get_channel() -> str:
    return channel_cfg["username"]

async def pin(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, msg_id: int):
    """Pin a message silently, skip if no permission."""
    try:
        await ctx.bot.pin_chat_message(chat_id=chat_id, message_id=msg_id,
                                       disable_notification=True)
    except Exception:
        pass

# ─── Lead helpers ─────────────────────────────────────────────────────────────
def init_lead(update: Update) -> dict:
    """Create or return a lead profile for this user."""
    u  = update.effective_user
    cid = update.effective_chat.id
    if cid not in leads:
        leads[cid] = {
            "chat_id": cid,
            "name": f"{u.first_name or ''} {u.last_name or ''}".strip() or "Unknown",
            "username": f"@{u.username}" if u.username else None,
            "phone": None,
            "language": "UZ",
            "service_needed": None,
            "project_type": None,
            "budget": None,
            "timeline": None,
            "first_message": None,
            "contact_given": False,
            "lead_score": "❄️ Cold",
            "status": "new",
            "first_seen": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "last_seen": datetime.now(TZ).strftime("%Y-%m-%d %H:%M"),
            "total_messages": 0,
            "notes": "",
        }
    else:
        leads[cid]["last_seen"] = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
        leads[cid]["total_messages"] += 1
    return leads[cid]

def score_lead(chat_id: int, text: str) -> str:
    """Calculate lead score based on keywords in text."""
    t = text.lower()
    hot_hits  = sum(1 for kw in HOT_KW  if kw in t)
    warm_hits = sum(1 for kw in WARM_KW if kw in t)
    phone_hit = bool(PHONE_RE.search(text))
    user_hit  = bool(USERNAME_RE.search(text))

    if hot_hits >= 2 or phone_hit or user_hit:
        return "🔥 Hot"
    if hot_hits == 1 or warm_hits >= 2:
        return "🌤 Warm"
    return "❄️ Cold"

def extract_contact(text: str) -> str | None:
    """Extract phone or @username from text."""
    u = USERNAME_RE.search(text)
    if u:
        return f"@{u.group(1)}"
    p = PHONE_RE.search(text)
    if p:
        return p.group(1)
    return None

# ─── Owner notifications ──────────────────────────────────────────────────────
async def notify_new_client(app: Application, lead: dict, first_msg: str):
    """Send a new-client alert to the owner."""
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🆕 YANGI MIJOZ\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Ism: {lead['name']}\n"
        f"🔗 Username: {lead['username'] or '—'}\n"
        f"🌐 Til: {lead['language']}\n"
        f"💬 Birinchi xabar: \"{first_msg[:80]}\"\n"
        f"🕐 Vaqt: {lead['first_seen']}\n"
        "📊 Holat: ❄️ Cold (yangi)\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👆 Suhbatni kuzating"
    )
    try:
        msg = await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
        await app.bot.pin_chat_message(chat_id=OWNER_CHAT_ID,
                                       message_id=msg.message_id,
                                       disable_notification=True)
    except Exception as e:
        logger.error(f"[notify_new_client] {e}")

async def notify_hot_lead(app: Application, lead: dict, last_msg: str):
    """Send a hot-lead alert to the owner."""
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "🔥 HOT LEAD ANIQLANDI!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Ism: {lead['name']}\n"
        f"🔗 Username: {lead['username'] or '—'}\n"
        f"📱 Kontakt: {lead['phone'] or lead['username'] or '—'}\n"
        f"🛠 Xizmat: {lead['service_needed'] or 'aniqlanmagan'}\n"
        f"💰 Byudjet: {lead['budget'] or 'noma lum'}\n"
        f"⚡️ Muddat: {lead['timeline'] or 'aniqlanmagan'}\n"
        f"💬 So'nggi xabar: \"{last_msg[:80]}\"\n"
        f"🕐 Vaqt: {lead['last_seen']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👉 HOZIR JAVOB BERING!"
    )
    try:
        msg = await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
        await app.bot.pin_chat_message(chat_id=OWNER_CHAT_ID,
                                       message_id=msg.message_id,
                                       disable_notification=True)
        logger.info(f"🎯 [HOT LEAD] {lead['name']} | {lead['service_needed']}")
    except Exception as e:
        logger.error(f"[notify_hot_lead] {e}")

async def notify_contact_received(app: Application, lead: dict):
    """Alert owner when a client shares their contact info."""
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "📱 KONTAKT OLINDI!\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"👤 Ism: {lead['name']}\n"
        f"🔗 Username: {lead['username'] or '—'}\n"
        f"📞 Telefon/Kontakt: {lead['phone']}\n"
        f"🛠 Xizmat: {lead['service_needed'] or '—'}\n"
        f"💰 Byudjet: {lead['budget'] or '—'}\n"
        f"⚡️ Muddat: {lead['timeline'] or '—'}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "✅ Bu lead tayyor — bog'laning!"
    )
    try:
        msg = await app.bot.send_message(chat_id=OWNER_CHAT_ID, text=text)
        await app.bot.pin_chat_message(chat_id=OWNER_CHAT_ID,
                                       message_id=msg.message_id,
                                       disable_notification=True)
    except Exception as e:
        logger.error(f"[notify_contact] {e}")

# ─── Groq helpers ─────────────────────────────────────────────────────────────
async def ask_groq(chat_id: int, user_text: str) -> str:
    """Conversational Groq call — uses history and correct mode."""
    add_message(chat_id, "user", user_text)
    system = OWNER_PROMPT if is_owner(chat_id) else CLIENT_PROMPT
    temp   = 0.5 if is_owner(chat_id) else 0.7
    msgs   = [{"role": "system", "content": system}] + get_history(chat_id)
    try:
        r = await groq_client.chat.completions.create(
            model=GROQ_MODEL, messages=msgs, temperature=temp, max_tokens=512)
        reply = r.choices[0].message.content.strip()
        add_message(chat_id, "assistant", reply)
        return reply
    except Exception as e:
        if conversations.get(chat_id):
            conversations[chat_id].pop()
        logger.error(f"[Groq] {e}\n{traceback.format_exc()}")
        return "Something went wrong, try again 🔄"

async def ask_groq_raw(prompt: str, system: str = "", temp: float = 0.5) -> str:
    """One-shot Groq call with no history."""
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    try:
        r = await groq_client.chat.completions.create(
            model=GROQ_MODEL, messages=msgs, temperature=temp, max_tokens=512)
        return r.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"[Groq raw] {e}\n{traceback.format_exc()}")
        return "Something went wrong, try again 🔄"

# ─── Channel post generation ──────────────────────────────────────────────────
POST_TEMPLATES = {
    "service_ad": lambda svc, lang: (
        f"Write a compelling Telegram channel post advertising this Zero1 service: {svc}. "
        f"Language: {lang}. Include price, key benefits, delivery time (3-7 days), and CTA to @zero1_uz. "
        "Use emojis. HTML format (<b>, <i> only). Max 150 words."
    ),
    "tech_tip": lambda svc, lang: (
        f"Write a useful tech tip post for Zero1's Telegram channel. "
        f"Topic: something related to {svc}. Language: {lang}. "
        "End with subtle Zero1 mention. HTML format. Max 150 words."
    ),
    "social_proof": lambda svc, lang: (
        f"Write a realistic client success story for Zero1. Service: {svc}. Language: {lang}. "
        "Include: client type, their problem, Zero1 solution, positive result with numbers. "
        "CTA to @zero1_uz at end. HTML format. Max 150 words."
    ),
    "engagement": lambda svc, lang: (
        f"Write an engaging question post for Zero1's channel. Related to: {svc}. Language: {lang}. "
        "Ask a question that businesses would relate to. Encourage comments. "
        "Subtle Zero1 CTA. HTML format. Max 150 words."
    ),
}

async def generate_channel_post(post_type: str, lang: str = "UZ", service: str = "") -> str:
    """Generate a channel post using Groq for the given type and language."""
    prompt = POST_TEMPLATES[post_type](service, lang)
    return await ask_groq_raw(prompt, system=CHANNEL_SYSTEM, temp=0.8)

async def auto_post_to_channel(app: Application, lang: str = "UZ"):
    """Scheduled job — post to channel at given time with correct language."""
    global post_type_idx, service_idx
    ch = get_channel()
    if not ch or not autopost_on:
        logger.info(f"[scheduler] skip — channel={ch!r} autopost={autopost_on}")
        return

    try:
        if post_queue:
            text = post_queue.pop(0)
            ptype = "queued"
        else:
            ptype   = POST_TYPES[post_type_idx % len(POST_TYPES)]
            service = SERVICES[service_idx % len(SERVICES)]
            post_type_idx += 1
            service_idx   += 1
            text = await generate_channel_post(ptype, lang, service)

        await app.bot.send_message(chat_id=ch, text=text, parse_mode="HTML")
        logger.info(f"[scheduler] ✅ Posted to {ch} | type={ptype} | lang={lang}")

        now = datetime.now(TZ).strftime("%H:%M")
        await app.bot.send_message(
            chat_id=OWNER_CHAT_ID,
            text=f"📤 Kanalga post yuborildi ✅\n🕐 {now} | 📊 Tur: {ptype} ({lang})"
        )
    except Exception as e:
        logger.error(f"[scheduler] ERROR: {e}\n{traceback.format_exc()}")

# ─── Lead funnel questions ─────────────────────────────────────────────────────
Q1 = (
    "Salom! Men Sellora — Zero1 AI yordamchisi 👋\n\n"
    "Sizga yaxshiroq yordam berish uchun:\n"
    "Qanday loyiha yoki xizmat kerak? 🤔"
)
Q2 = (
    "Ajoyib! Bir nechta savol:\n\n"
    "1️⃣ Bu loyiha biznes uchunmi yoki shaxsiy?\n"
    "2️⃣ Taxminiy byudjetingiz bor? (yo'q desangiz ham bo'ladi)\n"
    "3️⃣ Qachon kerak? (tez / 1 oy / aniq emas)"
)
Q3 = (
    "Zo'r! Siz bilan bog'lanish uchun:\n"
    "Telegram username yoki telefon raqamingizni\n"
    "yubora olasizmi? 📱"
)

# ─── Public commands ──────────────────────────────────────────────────────────
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/start — greet owner or open client lead funnel."""
    user    = update.effective_user
    chat_id = update.effective_chat.id
    known_users.add(chat_id)
    clear_history(chat_id)
    set_state(chat_id, S_NORMAL)

    if is_owner(chat_id):
        ch = get_channel()
        if not ch:
            set_state(chat_id, S_CHANNEL_SETUP)
            sent = await update.message.reply_text(
                "👋 Salom! Kanalingiz linkini yuboring.\n"
                "Masalan: @zero1_uz yoki https://t.me/zero1_uz"
            )
        else:
            sent = await update.message.reply_text(
                f"👋 Hey Akobir! Sellora tayyor.\n\n"
                f"📢 Kanal: {ch}\n"
                "Owner: /task /tasks /clear /status /post /schedule /channel\\_stats /ideas /summarize /autopost\n"
                "Leads: /leads /hot\\_leads /export\\_leads\n\n"
                "📨 Har qanday xabarni forward qiling — tahlil qilaman!"
            )
        await pin(context, chat_id, sent.message_id)
        return

    # New client — start lead funnel
    lead = init_lead(update)
    if lead["first_message"] is None:
        lead["first_message"] = "—"
        await notify_new_client(context.application, lead, "started bot")

    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    greeting = await ask_groq(chat_id,
        f"Greet new client {user.first_name} warmly as Sellora, introduce Zero1 briefly. "
        "Then ask what they need. Be warm and concise.")

    sent = await update.message.reply_text(greeting)
    await pin(context, chat_id, sent.message_id)

    # Move to Q1 after greeting
    set_state(chat_id, S_LEAD_Q1)
    q1_sent = await update.message.reply_text(Q1)
    await pin(context, chat_id, q1_sent.message_id)


async def cmd_services(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/services — Show Zero1 service list."""
    chat_id = update.effective_chat.id
    known_users.add(chat_id)
    sent = await update.message.reply_text(
        "🚀 *Zero1 — Services & Pricing*\n\n"
        "🤖 Telegram Bot (basic) — from *$150*\n"
        "🧠 Telegram Bot (AI-powered) — from *$400*\n"
        "🌐 Landing Page — from *$200*\n"
        "💻 Full Business Website — from *$500*\n"
        "💬 AI Chatbot for Business — from *$600*\n"
        "⚙️ Custom Automation Tool — from *$300*\n"
        "📱 Full Web App — from *$800+*\n\n"
        "✅ All projects: 3-7 day delivery, modern design, multilingual, free revisions\n\n"
        "💬 Tell me what you need!",
        parse_mode="Markdown",
    )
    await pin(context, chat_id, sent.message_id)


async def cmd_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/contact — Show Zero1 contact info."""
    chat_id = update.effective_chat.id
    sent = await update.message.reply_text(
        "📬 *Contact Zero1*\n\n"
        "🌐 https://zero1premium.netlify.app\n\n"
        "💼 @zero1\\_uz\n"
        "👤 @akobircs\n"
        "📞 +998774463446\n\n"
        "We respond fast — let's build! 🚀",
        parse_mode="Markdown",
    )
    await pin(context, chat_id, sent.message_id)


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/reset — Clear conversation history."""
    chat_id = update.effective_chat.id
    clear_history(chat_id)
    set_state(chat_id, S_NORMAL)
    sent = await update.message.reply_text("🔄 Cleared! Fresh start — what do you need?")
    await pin(context, chat_id, sent.message_id)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/help — Show commands."""
    chat_id = update.effective_chat.id
    if is_owner(chat_id):
        text = (
            "🤖 *Sellora — All Commands*\n\n"
            "👥 *Public*\n"
            "/start /services /contact /reset /help\n\n"
            "🔐 *Owner*\n"
            "/task `<text>` — Save task\n"
            "/tasks — View tasks\n"
            "/clear — Clear tasks\n"
            "/status — Bot stats\n"
            "/summarize — Summarize chat\n\n"
            "📢 *Channel*\n"
            "/post — Interactive post wizard\n"
            "/schedule `<text>` — Queue a post\n"
            "/autopost `on/off` — Toggle auto-posts\n"
            "/channel\\_stats — Channel info\n"
            "/ideas — Generate 5 post ideas\n\n"
            "🎯 *Leads*\n"
            "/leads — All leads by score\n"
            "/hot\\_leads — Hot leads only\n"
            "/lead `@user` — Full lead profile\n"
            "/lead\\_close `@user` — Mark closed ✅\n"
            "/lead\\_reject `@user` — Mark rejected ❌\n"
            "/lead\\_note `@user text` — Add note\n"
            "/export\\_leads — Export all leads\n\n"
            "📨 Forward any message for AI analysis"
        )
    else:
        text = (
            "🤖 *Sellora — Commands*\n\n"
            "/start — Start conversation\n"
            "/services — Services & prices\n"
            "/contact — Contact info\n"
            "/reset — Clear history\n"
            "/help — This menu\n\n"
            "Or just type your question! 💬"
        )
    sent = await update.message.reply_text(text, parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)


# ─── Owner task commands ──────────────────────────────────────────────────────
async def cmd_task(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not context.args:
        sent = await update.message.reply_text("Usage: /task Buy server domain")
        await pin(context, chat_id, sent.message_id)
        return
    t = " ".join(context.args)
    tasks.append({"text": t, "time": datetime.now(TZ).strftime("%d %b %H:%M")})
    sent = await update.message.reply_text(f"✅ Task saved: *{t}*", parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not tasks:
        sent = await update.message.reply_text("📋 No tasks. Use /task to add one.")
    else:
        lines = [f"{i+1}. {t['text']}  _({t['time']})_" for i, t in enumerate(tasks)]
        sent = await update.message.reply_text(
            f"📋 *Tasks ({len(tasks)}):*\n\n" + "\n".join(lines), parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

async def cmd_clear_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    n = len(tasks); tasks.clear()
    sent = await update.message.reply_text(f"🗑️ Cleared {n} task(s).")
    await pin(context, chat_id, sent.message_id)

async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    u = int(time.time() - START_TIME)
    h, r = divmod(u, 3600); m, s = divmod(r, 60)
    try:
        mem = f"{psutil.Process(os.getpid()).memory_info().rss/1024/1024:.1f} MB"
    except Exception:
        mem = "N/A"
    sent = await update.message.reply_text(
        f"📊 *Sellora Status*\n\n"
        f"⏱ Uptime: `{h}h {m}m {s}s`\n"
        f"👥 Users: `{len(known_users)}`\n"
        f"🎯 Leads: `{len(leads)}`\n"
        f"💬 Active chats: `{len([c for c in conversations.values() if c])}`\n"
        f"📋 Tasks: `{len(tasks)}`\n"
        f"📬 Queue: `{len(post_queue)}`\n"
        f"📢 Channel: `{get_channel() or 'not set'}`\n"
        f"⏰ Autopost: `{'ON' if autopost_on else 'OFF'}`\n"
        f"🧠 Mem: `{mem}`\n"
        f"🤖 Model: `{GROQ_MODEL}`",
        parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

async def cmd_summarize(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    msgs = [m["content"] for m in get_history(chat_id) if m["role"] == "user"][-10:]
    if not msgs:
        sent = await update.message.reply_text("No messages to summarize yet.")
        await pin(context, chat_id, sent.message_id)
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    s = await ask_groq_raw(f"Summarize in 3-5 bullets:\n" + "\n".join(f"- {m}" for m in msgs), temp=0.3)
    sent = await update.message.reply_text(f"📝 *Summary:*\n\n{s}", parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

# ─── Channel owner commands ───────────────────────────────────────────────────
async def cmd_autopost(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/autopost on|off — Toggle scheduled auto-posting."""
    global autopost_on
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if context.args and context.args[0].lower() in ("on", "off"):
        autopost_on = context.args[0].lower() == "on"
    status = "✅ ON" if autopost_on else "❌ OFF"
    sent = await update.message.reply_text(f"⏰ Auto-posting: {status}")
    await pin(context, chat_id, sent.message_id)

async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/post — Start interactive post wizard."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    ch = get_channel()
    if not ch:
        sent = await update.message.reply_text("⚠️ Channel not configured. Send /start first.")
        await pin(context, chat_id, sent.message_id)
        return
    set_state(chat_id, S_POST_TYPE)
    post_wizard.clear()
    sent = await update.message.reply_text(
        "📝 Nima haqida post yozmoqchisiz?\n\n"
        "1️⃣ Mahsulot reklama qilish\n"
        "2️⃣ Yangilik yoki xabar\n"
        "3️⃣ Aksiya yoki chegirma\n"
        "4️⃣ O'z matnimni yozaman\n\n"
        "Raqam yuboring 👇"
    )
    await pin(context, chat_id, sent.message_id)

async def cmd_schedule(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not context.args:
        sent = await update.message.reply_text(f"Usage: /schedule Your post text\nQueue: {len(post_queue)}")
    else:
        post_queue.append(" ".join(context.args))
        sent = await update.message.reply_text(f"📬 Queued! Position #{len(post_queue)}.")
    await pin(context, chat_id, sent.message_id)

async def cmd_channel_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    ch = get_channel()
    if not ch:
        sent = await update.message.reply_text("⚠️ Channel not set.")
        await pin(context, chat_id, sent.message_id)
        return
    try:
        info  = await context.bot.get_chat(ch)
        count = await context.bot.get_chat_member_count(ch)
        text  = (
            f"📢 *{info.title or ch}*\n\n"
            f"👥 Subscribers: `{count}`\n"
            f"📬 Queue: `{len(post_queue)}`\n"
            f"⏰ Autopost: `{'ON' if autopost_on else 'OFF'}`\n"
            f"🔄 Next type: `{POST_TYPES[post_type_idx % len(POST_TYPES)]}`"
        )
    except Exception as e:
        text = f"❌ Couldn't fetch stats: {e}\n\nMake sure bot is admin of {ch}."
    sent = await update.message.reply_text(text, parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    ideas = await ask_groq_raw(
        "Generate 5 creative Telegram channel post ideas for Zero1 "
        "(Telegram bots, websites, AI chatbots, automation). "
        "Each: type + 1-line hook + brief description. Numbered list.",
        temp=0.85
    )
    sent = await update.message.reply_text(f"💡 *5 Post Ideas:*\n\n{ideas}", parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

# ─── Lead commands ────────────────────────────────────────────────────────────
def _find_lead_by_username(username: str) -> dict | None:
    u = username.lstrip("@").lower()
    for lead in leads.values():
        if lead["username"] and lead["username"].lstrip("@").lower() == u:
            return lead
    return None

def _lead_score_emoji(score: str) -> str:
    if "Hot" in score: return "🔥"
    if "Warm" in score: return "🌤"
    return "❄️"

async def cmd_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/leads — All leads sorted by score."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not leads:
        sent = await update.message.reply_text("📊 No leads yet.")
        await pin(context, chat_id, sent.message_id)
        return

    hot  = [l for l in leads.values() if "Hot"  in l["lead_score"]]
    warm = [l for l in leads.values() if "Warm" in l["lead_score"]]
    cold = [l for l in leads.values() if "Cold" in l["lead_score"]]

    lines = [f"━━━━━━━━━━━━━━━━━━━━━━━━━━━",
             f"📊 LEADLAR RO'YXATI (jami: {len(leads)})",
             f"━━━━━━━━━━━━━━━━━━━━━━━━━━━"]

    def fmt(lst, emoji):
        out = [f"{emoji} ({len(lst)} ta):"]
        for i, l in enumerate(lst, 1):
            out.append(
                f"{i}. {l['name']} {l['username'] or ''} — "
                f"{l['service_needed'] or '?'} — {l['last_seen']}"
                + (f"\n   📱 {l['phone']}" if l['phone'] else "")
            )
        return out

    if hot:  lines += fmt(hot,  "🔥 HOT")
    if warm: lines += fmt(warm, "🌤 WARM")
    if cold: lines += fmt(cold, "❄️ COLD")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━━━━━━")

    sent = await update.message.reply_text("\n".join(lines))
    await pin(context, chat_id, sent.message_id)

async def cmd_hot_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/hot_leads — Show only hot leads."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    hot = [l for l in leads.values() if "Hot" in l["lead_score"]]
    if not hot:
        sent = await update.message.reply_text("🔥 No hot leads yet.")
        await pin(context, chat_id, sent.message_id)
        return
    lines = ["🔥 HOT LEADS\n━━━━━━━━━━━━━━━━━━━━━━━━━━━"]
    for l in hot:
        lines.append(
            f"👤 {l['name']} {l['username'] or ''}\n"
            f"🛠 {l['service_needed'] or '?'} | 💰 {l['budget'] or '?'} | ⚡️ {l['timeline'] or '?'}\n"
            f"📱 {l['phone'] or '—'} | 📅 {l['last_seen']}\n"
        )
    sent = await update.message.reply_text("\n".join(lines))
    await pin(context, chat_id, sent.message_id)

async def cmd_lead(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lead @username — Full lead profile."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not context.args:
        sent = await update.message.reply_text("Usage: /lead @username")
        await pin(context, chat_id, sent.message_id)
        return
    lead = _find_lead_by_username(context.args[0])
    if not lead:
        sent = await update.message.reply_text(f"❌ Lead not found: {context.args[0]}")
        await pin(context, chat_id, sent.message_id)
        return
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    summary = await ask_groq_raw(
        f"In 2 sentences, analyze this lead:\n{lead}\nGive a quick sales assessment.", temp=0.4)
    text = (
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        "👤 LEAD PROFILI\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Ism: {lead['name']}\n"
        f"Username: {lead['username'] or '—'}\n"
        f"Telefon: {lead['phone'] or '—'}\n"
        f"Til: {lead['language']}\n"
        f"Xizmat: {lead['service_needed'] or '—'}\n"
        f"Loyiha: {lead['project_type'] or '—'}\n"
        f"Byudjet: {lead['budget'] or '—'}\n"
        f"Muddat: {lead['timeline'] or '—'}\n"
        f"Score: {lead['lead_score']}\n"
        f"Holat: {lead['status']}\n"
        f"Birinchi xabar: {lead['first_seen']}\n"
        f"So'nggi faollik: {lead['last_seen']}\n"
        f"Xabarlar soni: {lead['total_messages']}\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"AI tahlil: {summary}"
    )
    sent = await update.message.reply_text(text)
    await pin(context, chat_id, sent.message_id)

async def cmd_lead_close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lead_close @username — Mark lead as closed/won."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    lead = _find_lead_by_username(context.args[0] if context.args else "")
    if not lead:
        sent = await update.message.reply_text("❌ Lead not found.")
    else:
        lead["status"] = "closed"
        sent = await update.message.reply_text(f"✅ {lead['name']} marked as CLOSED!")
    await pin(context, chat_id, sent.message_id)

async def cmd_lead_reject(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lead_reject @username — Mark lead as rejected."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    lead = _find_lead_by_username(context.args[0] if context.args else "")
    if not lead:
        sent = await update.message.reply_text("❌ Lead not found.")
    else:
        lead["status"] = "rejected"
        sent = await update.message.reply_text(f"❌ {lead['name']} marked as REJECTED.")
    await pin(context, chat_id, sent.message_id)

async def cmd_lead_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/lead_note @username text — Add note to lead."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not context.args or len(context.args) < 2:
        sent = await update.message.reply_text("Usage: /lead_note @username note text")
        await pin(context, chat_id, sent.message_id)
        return
    lead = _find_lead_by_username(context.args[0])
    if not lead:
        sent = await update.message.reply_text("❌ Lead not found.")
    else:
        note = " ".join(context.args[1:])
        lead["notes"] = (lead["notes"] + f"\n[{datetime.now(TZ).strftime('%d %b %H:%M')}] {note}").strip()
        sent = await update.message.reply_text(f"📝 Note added to {lead['name']}.")
    await pin(context, chat_id, sent.message_id)

async def cmd_export_leads(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/export_leads — Export all leads as full text report."""
    chat_id = update.effective_chat.id
    if not is_owner(chat_id): return
    if not leads:
        sent = await update.message.reply_text("No leads to export.")
        await pin(context, chat_id, sent.message_id)
        return
    now  = datetime.now(TZ).strftime("%Y-%m-%d %H:%M")
    lines = [f"📊 LEADS EXPORT — {now}", f"Total: {len(leads)}\n"]
    for l in sorted(leads.values(), key=lambda x: (
        0 if "Hot" in x["lead_score"] else 1 if "Warm" in x["lead_score"] else 2
    )):
        lines.append(
            f"{l['lead_score']} {l['name']} {l['username'] or ''}\n"
            f"  Phone: {l['phone'] or '—'} | Service: {l['service_needed'] or '—'}\n"
            f"  Budget: {l['budget'] or '—'} | Timeline: {l['timeline'] or '—'}\n"
            f"  Status: {l['status']} | Msgs: {l['total_messages']}\n"
            f"  First: {l['first_seen']} | Last: {l['last_seen']}\n"
            + (f"  Notes: {l['notes']}\n" if l['notes'] else "")
        )
    # Split into chunks if too long
    full = "\n".join(lines)
    for i in range(0, len(full), 4000):
        sent = await update.message.reply_text(f"```\n{full[i:i+4000]}\n```", parse_mode="Markdown")
    await pin(context, chat_id, sent.message_id)

# ─── Forwarded message analysis ───────────────────────────────────────────────
async def handle_forwarded(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Classify and suggest replies for forwarded messages (owner only)."""
    text = update.message.text or update.message.caption or ""
    if not text:
        sent = await update.message.reply_text("⚠️ No text in forwarded message.")
        await pin(context, update.effective_chat.id, sent.message_id)
        return
    prompt = (
        f"Analyze this forwarded message:\n\"\"\"{text}\"\"\"\n\n"
        "Reply in this exact format:\n"
        "**Classification:** [🎯 Lead | ⚠️ Complaint | 🗑️ Spam | 💬 General]\n"
        "**Summary:** One sentence.\n"
        "**Reply options:**\n1. ...\n2. ...\n3. ...\n"
        "**Service match (if Lead):** [Zero1 service or N/A]\n\n"
        "Reply in the same language as the message."
    )
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")
    analysis = await ask_groq_raw(prompt, temp=0.4)
    if "🎯" in analysis or "Lead" in analysis:
        logger.info(f"🎯 [LEAD via forward] {text[:80]}")
    sent = await update.message.reply_text(f"📋 *Analysis:*\n\n{analysis}", parse_mode="Markdown")
    await pin(context, update.effective_chat.id, sent.message_id)

# ─── Main message handler ─────────────────────────────────────────────────────
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Route all text messages through state machine."""
    if not update.message or not update.message.text:
        return

    chat_id = update.effective_chat.id
    text    = update.message.text.strip()
    state   = get_state(chat_id)
    known_users.add(chat_id)
    logger.info(f"[msg] {'owner' if is_owner(chat_id) else 'client'}={chat_id} state={state} | {text[:60]!r}")

    # ── Owner: channel setup ──────────────────────────────────────────────────
    if is_owner(chat_id) and state == S_CHANNEL_SETUP:
        ch = text.strip()
        if not ch.startswith("@"):
            # Extract @handle from URL
            m = re.search(r't\.me/([a-zA-Z0-9_]+)', ch)
            ch = f"@{m.group(1)}" if m else ch if ch.startswith("@") else f"@{ch}"
        channel_cfg["username"] = ch
        set_state(chat_id, S_NORMAL)
        sent = await update.message.reply_text(
            f"✅ Kanal ulandi: {ch}\n"
            "Endi har kuni avtomatik post yuboraman! 🚀\n\n"
            "Post vaqtlari (Toshkent): 09:00 🇺🇿 | 13:00 🇷🇺 | 18:00 🇺🇿 | 21:00 🇬🇧"
        )
        await pin(context, chat_id, sent.message_id)
        return

    # ── Owner: forwarded messages ─────────────────────────────────────────────
    if is_owner(chat_id) and update.message.forward_date:
        await handle_forwarded(update, context)
        return

    # ── Owner: /post wizard steps ─────────────────────────────────────────────
    if is_owner(chat_id) and state == S_POST_TYPE:
        choice = text.strip()
        type_map = {
            "1": ("service_ad",    "Qaysi mahsulot? (masalan: Telegram bot, Landing page)"),
            "2": ("news",          "Yangilik matni nima?"),
            "3": ("promo",         "Aksiya tafsilotlari nima?"),
            "4": ("custom",        "Matnni yuboring, chiroyli qilib joylashtiramiz."),
        }
        if choice in type_map:
            post_wizard["type"] = type_map[choice][0]
            set_state(chat_id, S_POST_DETAILS)
            sent = await update.message.reply_text(type_map[choice][1])
            await pin(context, chat_id, sent.message_id)
        else:
            sent = await update.message.reply_text("1, 2, 3 yoki 4 yuboring 👇")
            await pin(context, chat_id, sent.message_id)
        return

    if is_owner(chat_id) and state == S_POST_DETAILS:
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        ptype = post_wizard.get("type", "custom")
        if ptype == "custom":
            post_wizard["preview"] = text
        else:
            prompt = (
                f"Write a Telegram channel post for Zero1. Type: {ptype}. "
                f"Details: {text}. Language: Russian. "
                "Use HTML formatting, emojis, max 150 words. CTA to @zero1_uz at end."
            )
            post_wizard["preview"] = await ask_groq_raw(prompt, system=CHANNEL_SYSTEM, temp=0.8)
        set_state(chat_id, S_POST_CONFIRM)
        sent = await update.message.reply_text(
            f"👀 Preview:\n─────────────────\n{post_wizard['preview']}\n─────────────────\n"
            "✅ Yuborishga rozimisiz? (ha / yo'q)"
        )
        await pin(context, chat_id, sent.message_id)
        return

    if is_owner(chat_id) and state == S_POST_CONFIRM:
        set_state(chat_id, S_NORMAL)
        ch = get_channel()
        if text.lower() in ("ha", "yes", "да", "✅"):
            try:
                await context.bot.send_message(chat_id=ch, text=post_wizard["preview"], parse_mode="HTML")
                sent = await update.message.reply_text(f"✅ Posted to {ch}!")
            except Exception as e:
                sent = await update.message.reply_text(f"❌ Failed: {e}")
        else:
            sent = await update.message.reply_text("❌ Bekor qilindi.")
        post_wizard.clear()
        await pin(context, chat_id, sent.message_id)
        return

    # ── Owner: normal AI chat ─────────────────────────────────────────────────
    if is_owner(chat_id):
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await ask_groq(chat_id, text)
        sent  = await update.message.reply_text(reply)
        await pin(context, chat_id, sent.message_id)
        return

    # ── Client: lead funnel ───────────────────────────────────────────────────
    lead = init_lead(update)
    lead["total_messages"] += 1

    # Update score and check for upgrade
    old_score   = lead["lead_score"]
    new_score   = score_lead(chat_id, text)
    if new_score != "❄️ Cold":
        lead["lead_score"] = new_score
    if old_score != "🔥 Hot" and new_score == "🔥 Hot":
        await notify_hot_lead(context.application, lead, text)

    # Extract contact if present
    contact = extract_contact(text)
    if contact and not lead["contact_given"]:
        lead["phone"] = contact
        lead["contact_given"] = True
        await notify_contact_received(context.application, lead)

    if state == S_LEAD_Q1:
        lead["service_needed"] = text[:100]
        if lead["first_message"] in (None, "—"):
            lead["first_message"] = text[:100]
        set_state(chat_id, S_LEAD_Q2)
        sent = await update.message.reply_text(Q2)
        await pin(context, chat_id, sent.message_id)
        return

    if state == S_LEAD_Q2:
        # Parse budget/timeline hints from free-form answer
        tl = text.lower()
        if any(w in tl for w in ["tez","urgent","срочно","asap"]):
            lead["timeline"] = "urgent"
        elif any(w in tl for w in ["oy","month","месяц"]):
            lead["timeline"] = "1 month"
        else:
            lead["timeline"] = "unclear"
        if any(w in tl for w in ["biznes","business","бизнес"]):
            lead["project_type"] = "business"
        elif any(w in tl for w in ["shaxsiy","personal","личный"]):
            lead["project_type"] = "personal"
        nums = re.findall(r'\d+', text)
        if nums:
            lead["budget"] = f"~${nums[0]}"
        set_state(chat_id, S_LEAD_Q3)
        sent = await update.message.reply_text(Q3)
        await pin(context, chat_id, sent.message_id)
        return

    if state == S_LEAD_Q3:
        if contact:
            lead["phone"] = contact
            lead["contact_given"] = True
        set_state(chat_id, S_NORMAL)
        await context.bot.send_chat_action(chat_id=chat_id, action="typing")
        reply = await ask_groq(chat_id, text)
        sent  = await update.message.reply_text(reply)
        await pin(context, chat_id, sent.message_id)
        return

    # Normal AI conversation
    await context.bot.send_chat_action(chat_id=chat_id, action="typing")
    reply = await ask_groq(chat_id, text)
    sent  = await update.message.reply_text(reply)
    await pin(context, chat_id, sent.message_id)

# ─── Unknown command ──────────────────────────────────────────────────────────
async def handle_unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.effective_chat.id
    msg = "Unknown command." if is_owner(chat_id) else "Type /help to see what I can do 😊"
    sent = await update.message.reply_text(msg)
    await pin(context, chat_id, sent.message_id)

# ─── Error handler ────────────────────────────────────────────────────────────
async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.error(f"[error] {context.error}\n{traceback.format_exc()}")

# ─── Main ─────────────────────────────────────────────────────────────────────
def main():
    logger.info("🤖 Sellora starting up...")
    logger.info(f"👤 Owner: {OWNER_CHAT_ID} | 📢 Channel: {CHANNEL_USERNAME or 'not set'}")

    scheduler = AsyncIOScheduler()

    async def post_init(app: Application):
        """Start scheduler after event loop is running."""
        # 4x/day posts: 09:00 UZ | 13:00 RU | 18:00 UZ | 21:00 EN
        for hour, lang in [(9, "UZ"), (13, "RU"), (18, "UZ"), (21, "EN")]:
            scheduler.add_job(
                auto_post_to_channel, "cron",
                hour=hour, minute=0, timezone=TZ,
                args=[app, lang], id=f"post_{hour}"
            )
        scheduler.start()
        logger.info("⏰ Scheduler started — posts at 09:00/13:00/18:00/21:00 Tashkent")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Public
    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("services",     cmd_services))
    app.add_handler(CommandHandler("contact",      cmd_contact))
    app.add_handler(CommandHandler("reset",        cmd_reset))
    app.add_handler(CommandHandler("help",         cmd_help))
    # Owner — tasks
    app.add_handler(CommandHandler("task",         cmd_task))
    app.add_handler(CommandHandler("tasks",        cmd_tasks))
    app.add_handler(CommandHandler("clear",        cmd_clear_tasks))
    app.add_handler(CommandHandler("status",       cmd_status))
    app.add_handler(CommandHandler("summarize",    cmd_summarize))
    # Owner — channel
    app.add_handler(CommandHandler("post",         cmd_post))
    app.add_handler(CommandHandler("schedule",     cmd_schedule))
    app.add_handler(CommandHandler("autopost",     cmd_autopost))
    app.add_handler(CommandHandler("channel_stats",cmd_channel_stats))
    app.add_handler(CommandHandler("ideas",        cmd_ideas))
    # Owner — leads
    app.add_handler(CommandHandler("leads",        cmd_leads))
    app.add_handler(CommandHandler("hot_leads",    cmd_hot_leads))
    app.add_handler(CommandHandler("lead",         cmd_lead))
    app.add_handler(CommandHandler("lead_close",   cmd_lead_close))
    app.add_handler(CommandHandler("lead_reject",  cmd_lead_reject))
    app.add_handler(CommandHandler("lead_note",    cmd_lead_note))
    app.add_handler(CommandHandler("export_leads", cmd_export_leads))
    # Messages
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.COMMAND, handle_unknown))
    app.add_error_handler(error_handler)

    logger.info("✅ Sellora is live!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
