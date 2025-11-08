import os, re, json, traceback
from datetime import datetime
from typing import Optional, List
import feedparser, requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import google.generativeai as genai

# ============================================================
# CONFIGURATION
# ============================================================
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_GID = os.getenv("SHEET_GID", "0")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-2.0-flash")
MAX_RESULTS = 3
SYSTEM_PROMPT = "You are Nova — a friendly conversational AI that can chat and also fetch and summarize news articles."
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))
app = FastAPI(title="Nova News Assistant")

# ============================================================
# SUPABASE
# ============================================================
supabase: Optional[Client] = None
if SUPABASE_URL and SUPABASE_KEY:
    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ============================================================
# SCHEMAS
# ============================================================
class ChatReq(BaseModel):
    user_email: Optional[str] = None
    message: str
    conversation_name: Optional[str] = None

# ============================================================
# UTILITIES
# ============================================================
def sanitize_conv_name(name: str) -> str:
    s = re.sub(r'["\']', "", name)
    s = re.sub(r"[^A-Za-z0-9 _-]", "", s)
    return s.strip()[:60] or "conversation"

# ============================================================
# SUPABASE HELPERS
# ============================================================
def fetch_user_history(email: str):
    if not supabase:
        return None
    try:
        res = supabase.table("user_chats").select("chat_history").eq("email", email).execute()
        if res.data:
            return res.data[0].get("chat_history", [])
    except Exception as e:
        print("fetch_user_history error:", e)
    return []

def upsert_user_history(email: str, history: list):
    if not supabase:
        return
    try:
        supabase.table("user_chats").upsert({"email": email, "chat_history": history}).execute()
    except Exception as e:
        print("upsert_user_history error:", e)

def append_message_to_conversation(email: str, conv: str, sender: str, text: str):
    if not supabase:
        return
    try:
        data = fetch_user_history(email) or []
        entry = {"conversation": conv, "sender": sender, "text": text, "time": datetime.utcnow().isoformat() + "Z"}
        data.append(entry)
        upsert_user_history(email, data)
    except Exception as e:
        print("append_message_to_conversation error:", e)

# ============================================================
# GEMINI HELPER
# ============================================================
def genai_call(prompt: str) -> Optional[str]:
    try:
        if hasattr(genai, "Client"):
            client = genai.Client()
            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            if hasattr(resp, "text") and resp.text:
                return resp.text.strip()
        else:
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            if hasattr(resp, "text") and resp.text:
                return resp.text.strip()
    except Exception as e:
        print("❌ Gemini error:", e)
        traceback.print_exc()
    print("⚠️ Gemini API returned no text.")
    return None

def call_gemini_chat(user_message: str, conversation_name: str, context_history=None):
    ctx = ""
    if context_history:
        lines = [f"{c['sender']}: {c['text']}" for c in context_history[-5:]]
        ctx = "\n".join(lines)
    prompt = f"{SYSTEM_PROMPT}\n\nConversation: {conversation_name}\n{ctx}\n\nUser: {user_message}\nNova:"
    reply = genai_call(prompt)
    return reply or "Sorry, I couldn't generate a reply."

# ============================================================
# CACHE for FOLLOWUPS
# ============================================================
CONV_CACHE = {}

def set_conversation_cache(name, data):
    CONV_CACHE[name] = data

def get_conversation_cache(name):
    return CONV_CACHE.get(name)

# ============================================================
# TOOLS (RSS + ARTICLE EXTRACTION)
# ============================================================
def fetch_sheet_rows(sheet_id, gid="0"):
    try:
        url = f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"
        rows = []
        for line in requests.get(url, timeout=8).text.splitlines()[1:]:
            parts = line.split(",")
            if len(parts) >= 4:
                rows.append({"title": parts[0], "summary": parts[1], "link": parts[2], "category": parts[3]})
        return rows
    except Exception as e:
        print("fetch_sheet_rows error:", e)
    return []

def search_rss_for_topic(topic, max_items=15):
    feeds = [
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "https://www.theverge.com/rss/index.xml",
        "https://www.wired.com/feed/rss",
        "https://feeds.feedburner.com/TechCrunch/",
        "https://www.space.com/feeds/all",
        "https://rss.nytimes.com/services/xml/rss/nyt/World.xml"
    ]
    items = []
    for url in feeds:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                if topic.lower() in (entry.title + entry.get("summary", "")).lower():
                    items.append({
                        "headline": entry.title,
                        "summary": entry.get("summary", ""),
                        "link": entry.link,
                        "source_feed": url
                    })
                    if len(items) >= max_items:
                        return items
        except Exception as e:
            print("RSS error:", e)
    return items

def extract_article_text(url: str):
    try:
        html = requests.get(url, timeout=8).text
        soup = BeautifulSoup(html, "html.parser")
        paragraphs = [p.get_text() for p in soup.find_all("p")]
        return "\n".join(paragraphs[:12])
    except Exception as e:
        print("extract_article_text error:", e)
    return None

def summarize_article_with_gemini(article_text, title, user_message):
    prompt = f"{SYSTEM_PROMPT}\nSummarize this for user message: '{user_message}'\n\nTitle: {title}\n\n{article_text[:4000]}"
    return genai_call(prompt) or "(No summary)"

# ============================================================
# INTENT DETECTION (MODEL DECIDES)
# ============================================================
def call_gemini_intent(user_message: str, conversation_name: Optional[str]=None, context_history=None) -> dict:
    ctx = ""
    if context_history:
        lines = [f"{c['sender']}: {c['text']}" for c in context_history[-5:]]
        ctx = "\n".join(lines)
    prompt = f"""
You are Nova, an assistant that decides if a user wants news or just chatting.
Return JSON only: {{"intent":"news"|"chat"|"followup","topic":"string","confidence":0.0}}
Examples:
User: "latest NASA news" -> {{"intent":"news","topic":"nasa","confidence":0.9}}
User: "hello" -> {{"intent":"chat","topic":"","confidence":0.9}}
User: "yes" -> {{"intent":"followup","topic":"","confidence":0.8}}

Conversation: {conversation_name}
{ctx}
User: {user_message}
Answer with JSON only:
"""
    text = genai_call(prompt)
    try:
        j = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
        return {"intent": j.get("intent", "chat"), "topic": j.get("topic", ""), "confidence": j.get("confidence", 0)}
    except Exception as e:
        print("intent parse error:", e, "raw:", text)
        return {"intent": "chat", "topic": "", "confidence": 0}

# ============================================================
# MAIN /CHAT ENDPOINT
# ============================================================
@app.post("/chat")
def chat(req: ChatReq):
    try:
        email = req.user_email or "anonymous"
        msg = req.message.strip()
        conv_name = sanitize_conv_name(req.conversation_name or msg)

        append_message_to_conversation(email, conv_name, "User", msg)
        history = fetch_user_history(email)

        intent = call_gemini_intent(msg, conversation_name=conv_name, context_history=history)
        kind, topic = intent["intent"], intent["topic"]

        # Followup -> expand from cache
        if kind == "followup":
            cache = get_conversation_cache(conv_name)
            if cache and cache.get("last_list"):
                art = cache["last_list"][0]
                full = summarize_article_with_gemini(art.get("article_text") or "", art.get("title"), msg)
                append_message_to_conversation(email, conv_name, "Nova", full)
                return {"reply": full, "conversation": conv_name}
            else:
                r = call_gemini_chat(msg, conv_name, history)
                append_message_to_conversation(email, conv_name, "Nova", r)
                return {"reply": r, "conversation": conv_name}

        # News intent
        if kind == "news":
            topic = topic or msg
            rows = fetch_sheet_rows(SHEET_ID, SHEET_GID)
            found = [r for r in rows if topic.lower() in (r["title"]+r["summary"]+r["category"]).lower()]
            if not found:
                found = search_rss_for_topic(topic, max_items=MAX_RESULTS)

            if not found:
                r = call_gemini_chat(msg, conv_name, history)
                append_message_to_conversation(email, conv_name, "Nova", r)
                return {"reply": r, "conversation": conv_name}

            summaries, cache_list = [], []
            for f in found[:MAX_RESULTS]:
                art_text = f.get("summary") or extract_article_text(f.get("link"))
                short = summarize_article_with_gemini(art_text or "", f.get("title"), msg)
                summaries.append(f"{f['title']}\n{short}\n🔗 {f.get('link')}")
                cache_list.append({"title": f.get("title"), "link": f.get("link"), "article_text": art_text})
            reply = "Here’s what I found 🗞️:\n\n" + "\n\n---\n\n".join(summaries) + "\n\nWant more details on one?"
            set_conversation_cache(conv_name, {"last_list": cache_list})
            append_message_to_conversation(email, conv_name, "Nova", reply)
            return {"reply": reply, "conversation": conv_name}

        # Chat intent
        r = call_gemini_chat(msg, conv_name, history)
        append_message_to_conversation(email, conv_name, "Nova", r)
        return {"reply": r, "conversation": conv_name}

    except Exception as e:
        print("Unhandled error:", e)
        traceback.print_exc()
        return {"reply": "Sorry — internal error.", "conversation": "error"}
