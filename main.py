# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone, timedelta
import os, json, csv, io, traceback, requests, re, uuid, sqlite3, time
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from dateutil import parser as dateparser
from typing import Optional, List, Dict, Any, Tuple
from urllib.parse import urlparse

# Optional Supabase
try:
    from supabase import create_client as create_supabase_client
except Exception:
    create_supabase_client = None

app = FastAPI(title="Nova — Smart News Chatbot")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")
MAX_RESULTS = 3
RATE_LIMIT_SECONDS = 60

if not GEMINI_API_KEY:
    raise RuntimeError("Set GEMINI_API_KEY in environment")

genai.configure(api_key=GEMINI_API_KEY)

supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_supabase_client:
    try:
        supabase = create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init failed:", e)

DB_PATH = "nova_cache.db"

def init_db():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    c = conn.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        chat_history TEXT DEFAULT '[]'
    )""")
    conn.commit()
    return conn

DB = init_db()

def get_user(email):
    cur = DB.cursor()
    cur.execute("SELECT chat_history FROM users WHERE email=?", (email,))
    r = cur.fetchone()
    if r:
        return json.loads(r[0])
    return []

def save_user(email, data):
    cur = DB.cursor()
    cur.execute("INSERT OR REPLACE INTO users (email, chat_history) VALUES (?,?)",
                (email, json.dumps(data)))
    DB.commit()

def supabase_sync(email):
    if not supabase:
        return
    try:
        data = get_user(email)
        supabase.table("users").upsert({"email": email, "chat_history": data}).execute()
    except Exception as e:
        print("supabase sync error:", e)

# ---------------- Prompt ----------------
SYSTEM_PROMPT = """
You are Nova, a helpful, accurate, friendly AI News Assistant.
Follow this workflow:
1️⃣ Identify user intent (topic, scope, timeframe)
2️⃣ Check Supabase chat history to avoid duplicates
3️⃣ Get cached news or fetch via RSS feeds
4️⃣ Summarize news naturally (2 sentences + friendly question)
5️⃣ End each reply with a short, conversational question.
Never say "Here are the results for...". Speak like a human.
"""

# ---------------- Helper ----------------
def extract_topic(msg: str):
    if not msg:
        return "news"
    m = re.search(r"(?:about|on|regarding)\s(.+)", msg, re.I)
    if m:
        return m.group(1).strip()
    msg = re.sub(r"[^A-Za-z0-9 ]", "", msg)
    msg = msg.strip()
    return msg if msg else "news"

def make_conv_name_from_message(msg: str):
    topic = extract_topic(msg)
    name = re.sub(r"[^A-Za-z0-9 _-]", "", topic).strip()
    return name[:40] or f"chat_{uuid.uuid4().hex[:6]}"

def append_message(email, conv, sender, text):
    history = get_user(email)
    found = False
    for obj in history:
        if conv in obj:
            obj[conv].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()})
            found = True
            break
    if not found:
        history.append({conv: [{"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()}]})
    save_user(email, history)
    supabase_sync(email)

def get_last_conversation(email):
    history = get_user(email)
    if not history:
        return None
    last = history[-1]
    if isinstance(last, dict):
        return list(last.keys())[0]
    return None

# ---------------- RSS Fetch ----------------
RSS_FEEDS = [
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.bbci.co.uk/news/world/rss.xml"
]
FEED_FETCH_TIMES = {}

def fetch_rss(topic, user_email):
    found = []
    now = time.time()
    for feed_url in RSS_FEEDS:
        last = FEED_FETCH_TIMES.get((user_email, feed_url))
        if last and (now - last) < RATE_LIMIT_SECONDS:
            continue
        FEED_FETCH_TIMES[(user_email, feed_url)] = now
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:10]:
                if topic.lower() in entry.title.lower() or topic.lower() in entry.get("summary", "").lower():
                    found.append({
                        "title": entry.title,
                        "summary": entry.get("summary", ""),
                        "link": entry.link
                    })
        except Exception as e:
            print("RSS error:", e)
    return found[:MAX_RESULTS]

def summarize(text, headline, user_msg):
    prompt = f"{SYSTEM_PROMPT}\nUser message: {user_msg}\nHeadline: {headline}\nArticle: {text}\n\nGenerate summary:"
    try:
        resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
        if hasattr(resp, "text") and resp.text:
            return resp.text.strip()
        return text[:200] + "..."
    except Exception as e:
        print("summ error:", e)
        return text[:200] + "..."

# ---------------- Request Model ----------------
class ChatReq(BaseModel):
    user_email: str
    message: str
    conversation_name: Optional[str] = None

# ---------------- Endpoint ----------------
@app.post("/chat")
def chat(req: ChatReq):
    email = req.user_email.strip().lower()
    msg = req.message.strip()
    conv_name = req.conversation_name

    if not msg:
        raise HTTPException(status_code=400, detail="Message required")

    # handle greeting
    if re.match(r"^(hi|hello|hey)\b", msg, re.I):
        conv = conv_name or make_conv_name_from_message(msg)
        reply = "Hello 👋 how are you? I'm Nova — your friendly AI news assistant. Want to know what's new today?"
        append_message(email, conv, email, msg)
        append_message(email, conv, "Nova", reply)
        return {"reply": reply, "conversation": conv}

    # if no conversation name provided
    if not conv_name:
        # detect if this is a "yes"/"continue" type follow-up
        if re.match(r"^(yes|yeah|yep|continue|go on|sure)\b", msg, re.I):
            last_conv = get_last_conversation(email)
            conv_name = last_conv or make_conv_name_from_message(msg)
        else:
            conv_name = make_conv_name_from_message(msg)

    append_message(email, conv_name, email, msg)

    # detect news intent
    if any(k in msg.lower() for k in ["news", "update", "latest", "about"]):
        topic = extract_topic(msg)
        articles = fetch_rss(topic, email)
        if not articles:
            reply = f"I couldn't find anything fresh about {topic}. Want me to broaden the search?"
            append_message(email, conv_name, "Nova", reply)
            return {"reply": reply, "conversation": conv_name}
        summaries = []
        for art in articles:
            text = summarize(art["summary"], art["title"], msg)
            summaries.append(f"📰 *{art['title']}*\n{text}\n🔗 {art['link']}")
        reply = "\n\n".join(summaries) + "\n\nWould you like me to summarize one in detail?"
        append_message(email, conv_name, "Nova", reply)
        return {"reply": reply, "conversation": conv_name}

    # casual chat fallback
    reply = f"Got it — {msg}. Want me to check latest news or something else?"
    append_message(email, conv_name, "Nova", reply)
    return {"reply": reply, "conversation": conv_name}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}
