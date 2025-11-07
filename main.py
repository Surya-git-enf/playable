# main.py
"""
Nova — Simple intent-driven chat + news assistant.

Features:
- Gemini model for chat & intent detection (uses google.generativeai)
- RSS feeds + Google Sheets (CSV export) for news fetching & caching
- SQLite local history + conversation cache; optional Supabase upsert
- "Yes" follow-ups resume last news list and expand first/unread item
"""

import os
import re
import json
import time
import uuid
import sqlite3
import traceback
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Google Generative AI SDK
import google.generativeai as genai

# Optional Supabase SDK
try:
    from supabase import create_client as create_supabase_client
except Exception:
    create_supabase_client = None

# ---------------- CONFIG (env) ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()         # optional: public or shared sheet for CSV export
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SQLITE_DB = os.getenv("SQLITE_DB", "nova_simple.db")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
RSS_RATE_LIMIT_SECONDS = int(os.getenv("RSS_RATE_LIMIT_SECONDS", "60"))

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

# Supabase client (optional)
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_supabase_client is not None:
    try:
        supabase = create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)

app = FastAPI(title="Nova (simple)")

# ---------------- SQLite (history + conversation cache) ----------------
def init_db(path: str):
    conn = sqlite3.connect(path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        chat_history TEXT DEFAULT '[]',
        updated_at TEXT
    )""")
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversation_cache (
        conversation_name TEXT PRIMARY KEY,
        cache_json TEXT,
        updated_at TEXT
    )""")
    conn.commit()
    return conn

DB = init_db(SQLITE_DB)

def fetch_user_history(email: str) -> List[dict]:
    cur = DB.cursor()
    cur.execute("SELECT chat_history FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    if not row:
        return []
    try:
        return json.loads(row[0] or "[]")
    except Exception:
        return []

def upsert_user_history(email: str, history: List[dict]):
    now = datetime.utcnow().isoformat() + "Z"
    cur = DB.cursor()
    cur.execute(
        "INSERT INTO users (email, chat_history, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(email) DO UPDATE SET chat_history=excluded.chat_history, updated_at=excluded.updated_at",
        (email, json.dumps(history), now)
    )
    DB.commit()
    # best-effort Supabase upsert
    if supabase:
        try:
            supabase.table("users").upsert({"email": email, "chat_history": history}).execute()
        except Exception as e:
            print("supabase upsert error:", e)

def append_message(email: str, conv_name: str, sender: str, text: str):
    if not email:
        email = "anonymous"
    history = fetch_user_history(email) or []
    found = False
    for obj in history:
        if isinstance(obj, dict) and conv_name in obj:
            obj[conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z"})
            found = True
            break
    if not found:
        history.append({conv_name: [{"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z"}]})
    upsert_user_history(email, history)

def get_last_conversation(email: str) -> Optional[str]:
    history = fetch_user_history(email)
    if not history:
        return None
    last = history[-1]
    if isinstance(last, dict):
        return list(last.keys())[0]
    return None

def set_conv_cache(conv_name: str, payload: dict):
    cur = DB.cursor()
    now = datetime.utcnow().isoformat() + "Z"
    cur.execute(
        "INSERT INTO conversation_cache (conversation_name, cache_json, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(conversation_name) DO UPDATE SET cache_json=excluded.cache_json, updated_at=excluded.updated_at",
        (conv_name, json.dumps(payload), now)
    )
    DB.commit()

def get_conv_cache(conv_name: str) -> Optional[dict]:
    cur = DB.cursor()
    cur.execute("SELECT cache_json FROM conversation_cache WHERE conversation_name = ?", (conv_name,))
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def clear_conv_cache(conv_name: str):
    cur = DB.cursor()
    cur.execute("DELETE FROM conversation_cache WHERE conversation_name = ?", (conv_name,))
    DB.commit()

# ---------------- RSS feeds and rate limiter ----------------
DEFAULT_RSS_FEEDS = [
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]
FEED_FETCH_TIMES: Dict[Tuple[str, str], float] = {}

CATEGORY_PATH_KEYWORDS = ["/tech", "/technology", "/space", "/nasa", "/science", "/business", "/sports", "/entertainment", "/world"]

# ---------------- Utilities: Google Sheets CSV fetch ----------------
def sheet_csv_url(sheet_id: str, gid: str = "0") -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0") -> List[dict]:
    if not sheet_id:
        return []
    try:
        r = requests.get(sheet_csv_url(sheet_id, gid), timeout=12)
        r.raise_for_status()
        import csv, io
        reader = csv.DictReader(io.StringIO(r.content.decode("utf-8")))
        rows = [{(k or "").strip().lower(): (v or "").strip() for k,v in row.items()} for row in reader]
        return rows
    except Exception as e:
        print("fetch_sheet_rows error:", e)
        return []

# ---------------- Utilities: extract article text ----------------
def extract_article_text(url: str, max_chars: int = 15000) -> Optional[str]:
    if not url:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"}
        r = requests.get(url, timeout=12, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        article = soup.find("article")
        texts = []
        if article:
            for p in article.find_all("p"):
                t = p.get_text(strip=True)
                if t:
                    texts.append(t)
        else:
            for p in soup.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 40:
                    texts.append(t)
        content = "\n\n".join(texts).strip()
        if not content:
            meta = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"})
            if meta and meta.get("content"):
                content = meta.get("content")
        if not content:
            return None
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".", 1)[0] + "."
        return content
    except Exception as e:
        print("extract_article_text error:", e)
        return None

# ---------------- Generative API wrappers ----------------
SYSTEM_PROMPT = (
    "You are Nova, a friendly concise news assistant. When user asks for news, prefer fetching latest articles and summarizing. "
    "When user chats, reply naturally. Keep replies short and helpful."
)

def genai_call(prompt: str) -> Optional[str]:
    try:
        if hasattr(genai, "Client"):
            client = genai.Client()
            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            text = getattr(resp, "text", None)
            if not text and hasattr(resp, "output") and isinstance(resp.output, (list, tuple)) and resp.output:
                text = getattr(resp.output[0], "content", None) or getattr(resp.output[0], "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("genai.Client() error:", e)
    try:
        if hasattr(genai, "generate_content"):
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("genai.generate_content() error:", e)
    return None

def call_gemini_chat(user_message: str, conversation_name: Optional[str] = None, context_history: Optional[List[dict]] = None) -> str:
    ctx = ""
    try:
        if context_history:
            lines = []
            for conv in context_history[-6:]:
                if isinstance(conv, dict):
                    for k, msgs in conv.items():
                        for m in msgs[-4:]:
                            lines.append(f"{m.get('sender')}: {m.get('text')}")
            if lines:
                ctx = "\n\nPrevious:\n" + "\n".join(lines[-12:])
    except Exception:
        ctx = ""
    prompt = SYSTEM_PROMPT + "\n\n" + f"Conversation: {conversation_name or ''}\n\nUser: {user_message}\n\n{ctx}\n\nAssistant:"
    out = genai_call(prompt)
    if not out:
        return f"Sorry — I couldn't reach the assistant. You said: {user_message[:200]}"
    return out

def call_gemini_intent(user_message: str, conversation_name: Optional[str] = None, context_history: Optional[List[dict]] = None) -> dict:
    ctx = ""
    try:
        if context_history:
            lines = []
            for conv in context_history[-6:]:
                if isinstance(conv, dict):
                    for k, msgs in conv.items():
                        for m in msgs[-4:]:
                            lines.append(f"{m.get('sender')}: {m.get('text')}")
            if lines:
                ctx = "\n\nPrevious:\n" + "\n".join(lines[-10:])
    except Exception:
        ctx = ""
    intent_prompt = (
        SYSTEM_PROMPT + "\n\n"
        "Task: Return ONLY JSON: {\"intent\":\"news\"|\"chat\"|\"followup\", \"topic\":\"<short>\", \"confidence\":0.0}\n"
        "Examples:\nUser: 'Latest NASA updates' -> {\"intent\":\"news\",\"topic\":\"nasa\",\"confidence\":0.98}\n"
        "User: 'Yes' -> {\"intent\":\"followup\",\"topic\":\"\",\"confidence\":0.9}\n"
        "User: 'How are you?' -> {\"intent\":\"chat\",\"topic\":\"\",\"confidence\":0.95}\n\n"
        f"Conversation: {conversation_name or ''}\n\nUser: {user_message}\n\n{ctx}\n\nRespond with JSON only:"
    )
    text = genai_call(intent_prompt)
    if text:
        try:
            jstart = text.find("{")
            jend = text.rfind("}")
            if jstart != -1 and jend != -1 and jend > jstart:
                jtext = text[jstart:jend+1]
            else:
                jtext = text.strip()
            parsed = json.loads(jtext)
            intent = parsed.get("intent","chat").lower()
            topic = (parsed.get("topic") or "").strip()
            confidence = float(parsed.get("confidence") or 0.0)
            if intent not in ("news","chat","followup"):
                raise ValueError("bad intent")
            return {"intent": intent, "topic": topic, "confidence": confidence}
        except Exception as e:
            print("intent parse error:", e, "raw:", repr(text))
    # fallback heuristic
    low = (user_message or "").lower().strip()
    if low in ("yes","y","yeah","yep","sure","continue","go on","ok"):
        return {"intent":"followup","topic":"", "confidence":0.6}
    if any(k in low for k in ["news","latest","update","summary","article","trailer","season","episode"]):
        # quick topic extraction
        t = extract_topic_simple(user_message)
        return {"intent":"news","topic":t[:120], "confidence":0.5}
    return {"intent":"chat","topic":"", "confidence":0.5}

def summarize_article_with_gemini(article_text: str, headline: str, user_message: str) -> str:
    prompt = (
        SYSTEM_PROMPT + "\n\nYou are Nova — summarize the article in 1-3 short paragraphs conversationally and end with one simple question.\n\n"
        f"User message: {user_message}\nHeadline: {headline}\n\nArticle:\n{article_text}\n\nSummary:"
    )
    out = genai_call(prompt)
    if out:
        return out
    # simple extractive fallback
    paras = article_text.split("\n\n")
    return (paras[0] if paras else article_text)[:800] + "\n\n(Short summary - could not call model.)"

# ---------------- topic helpers ----------------
def extract_topic_simple(msg: str) -> str:
    if not msg:
        return "news"
    m = re.search(r"(?:about|on|regarding)\s(.+)", msg, re.I)
    if m:
        return m.group(1).strip()
    cleaned = re.sub(r"[^A-Za-z0-9 ]", "", msg).strip()
    return cleaned[:140] or "news"

def sanitize_conv_name(s: str, max_words: int = 6) -> str:
    if not s:
        return f"chat_{uuid.uuid4().hex[:6]}"
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", s).strip()
    words = cleaned.split()
    return " ".join(words[:max_words]) or f"chat_{uuid.uuid4().hex[:6]}"

# ---------------- RSS search ----------------
def search_rss_for_topic(topic: str, user_email: Optional[str] = None, max_items: int = 40) -> Tuple[List[dict], List[str]]:
    topic_l = (topic or "").lower().strip()
    found = []
    feeds_checked = []
    feeds_to_check = DEFAULT_RSS_FEEDS.copy()
    for feed_url in feeds_to_check:
        feeds_checked.append(feed_url)
        # rate-limit per (user, feed)
        if user_email:
            key = (user_email, feed_url)
            last = FEED_FETCH_TIMES.get(key)
            now_ts = time.time()
            if last and (now_ts - last) < RSS_RATE_LIMIT_SECONDS:
                continue
            FEED_FETCH_TIMES[key] = now_ts
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:max_items]:
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                link = entry.get("link") or ""
                link_l = link.lower()
                match_by_content = (topic_l and (topic_l in title or topic_l in summary))
                match_by_path = any(k in link_l for k in CATEGORY_PATH_KEYWORDS)
                if (topic_l and match_by_content) or (not topic_l and match_by_path) or (topic_l and match_by_path):
                    found.append({
                        "headline": entry.get("title"),
                        "link": link,
                        "summary": entry.get("summary",""),
                        "published": entry.get("published") or entry.get("updated"),
                        "source_feed": feed_url
                    })
        except Exception as e:
            print("rss parse error:", feed_url, e)
    # sort by published
    def score(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score, reverse=True)
    return found, feeds_checked

# ---------------- FastAPI models ----------------
class ChatReq(BaseModel):
    user_email: Optional[str] = None
    message: str
    conversation_name: Optional[str] = None

# ---------------- /chat endpoint ----------------
@app.post("/chat")
def chat(req: ChatReq):
    """
    Main flow:
    - Append user message to history (SQLite + optional Supabase)
    - Ask model to decide intent (news/chat/followup)
    - If news: check Google Sheets cache -> RSS -> extract -> summarize -> store conversation cache (last_list)
    - If followup: resume conversation cache (expand item)
    - If chat: forward to Gemini chat model
    """
    try:
        email = (req.user_email or "").strip().lower() or None
        user_message = (req.message or "").strip()
        conv_in = (req.conversation_name or "").strip() or None

        if not user_message:
            raise HTTPException(status_code=400, detail="message required")

        # determine conversation name
        if conv_in:
            conv_name = sanitize_conv_name(conv_in)
        else:
            # if user says yes and has a last conversation, reuse it
            if email and re.match(r"^(yes|yeah|yep|sure|continue|go on|ok)\b", user_message, flags=re.I):
                last_conv = get_last_conversation(email)
                conv_name = last_conv or sanitize_conv_name(user_message)
            else:
                conv_name = sanitize_conv_name(user_message)

        # ensure user row exists
        if email:
            hist = fetch_user_history(email)
            if hist is None:
                upsert_user_history(email, [])

        # append user message immediately
        append_message(email or "anonymous", conv_name, email or "anonymous", user_message)

        # get small context for intent model
        context_hist = fetch_user_history(email or "anonymous") if email else None

        # intent detection by model (preferred)
        try:
            intent_res = call_gemini_intent(user_message, conversation_name=conv_name, context_history=context_hist)
        except Exception as e:
            print("intent call failed:", e)
            intent_res = {"intent":"chat", "topic":"", "confidence":0.0}

        intent = intent_res.get("intent", "chat")
        topic_from_model = (intent_res.get("topic") or "").strip()

        # -------- FOLLOWUP handling -------
        if intent == "followup":
            cache = get_conv_cache(conv_name)
            if cache and cache.get("last_list"):
                last_list = cache["last_list"]
                # check if user gave a number
                m = re.match(r"^\s*(\d+)\s*$", user_message)
                if m:
                    idx = max(0, int(m.group(1)) - 1)
                else:
                    # pick first unexpanded or first
                    idx = 0
                    for i,item in enumerate(last_list):
                        if not item.get("expanded"):
                            idx = i
                            break
                item = last_list[idx]
                art_text = item.get("article_text")
                if not art_text and item.get("link"):
                    art_text = extract_article_text(item.get("link"))
                if not art_text:
                    # fallback: ask model to expand based on title
                    ai_reply = call_gemini_chat(f"Please expand the following headline in a short paragraph: {item.get('title')}", conversation_name=conv_name, context_history=context_hist)
                else:
                    ai_reply = summarize_article_with_gemini(art_text, item.get("title") or item.get("headline",""), user_message)
                # mark expanded
                last_list[idx]["expanded"] = True
                cache["last_list"] = last_list
                set_conv_cache(conv_name, cache)
                append_message(email or "anonymous", conv_name, "Nova", ai_reply)
                return {"reply": ai_reply, "conversation": conv_name}
            else:
                # nothing cached: ask model what to do next (chat)
                ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
                append_message(email or "anonymous", conv_name, "Nova", ai_reply)
                return {"reply": ai_reply, "conversation": conv_name}

        # -------- NEWS flow (tools) -------
        if intent == "news":
            topic = topic_from_model or extract_topic_simple(user_message) or user_message
            articles = []

            # 1) Try Google Sheets cache if configured
            try:
                sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
                for r in sheet_rows:
                    title = r.get("title","")
                    summary = r.get("summary","")
                    link = r.get("link","")
                    cat = r.get("category","")
                    if topic.lower() in (title + " " + summary + " " + cat).lower():
                        articles.append({"title": title, "link": link, "article_text": summary, "source": r.get("source")})
                        if len(articles) >= MAX_RESULTS:
                            break
            except Exception as e:
                print("sheet cache error:", e)

            # 2) If no cache -> RSS search
            if not articles:
                try:
                    rss_found, feeds_checked = search_rss_for_topic(topic, user_email=email or "anonymous", max_items=40)
                    for item in rss_found[:MAX_RESULTS]:
                        link = item.get("link")
                        headline = item.get("headline") or topic
                        art_text = extract_article_text(link) or item.get("summary") or ""
                        articles.append({"title": headline, "link": link, "article_text": art_text, "source": item.get("source_feed")})
                except Exception as e:
                    print("rss fetch error:", e)

            if not articles:
                # fallback to generative topic summary
                ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
                append_message(email or "anonymous", conv_name, "Nova", ai_reply)
                return {"reply": ai_reply, "conversation": conv_name, "count": 0}

            # summarize found articles briefly and build conversation cache for followups
            short_blocks = []
            cache_list = []
            for art in articles:
                try:
                    if art.get("article_text"):
                        short = summarize_article_with_gemini(art["article_text"], art.get("title") or art.get("headline",""), user_message)
                    else:
                        short = art.get("article_text") or "(no extractable text)"
                except Exception as e:
                    print("summarize error:", e)
                    short = art.get("article_text") or art.get("summary") or "(summary failed)"
                short_blocks.append({"title": art.get("title"), "summary": short, "link": art.get("link"), "source": art.get("source")})
                cache_list.append({"title": art.get("title"), "link": art.get("link"), "article_text": art.get("article_text"), "expanded": False})

            # prepare friendly reply
            lines = []
            for i, s in enumerate(short_blocks, start=1):
                lines.append(f"{i}. {s['title']}\n\n{s['summary']}\n\nLink: {s['link']}")
            combined = "Hey — I found these:\n\n" + "\n\n---\n\n".join(lines) + "\n\nReply with a number to read more, or say 'yes' to expand the first item. Do you want more?"
            set_conv_cache(conv_name, {"last_list": cache_list, "topic": topic, "fetched_at": datetime.utcnow().isoformat() + "Z"})
            append_message(email or "anonymous", conv_name, "Nova", combined)
            return {"reply": combined, "conversation": conv_name, "count": len(short_blocks)}

        # -------- CHAT flow (default) -------
        ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
        append_message(email or "anonymous", conv_name, "Nova", ai_reply)
        return {"reply": ai_reply, "conversation": conv_name}

    except Exception as e:
        print("Unhandled /chat error:", e)
        traceback.print_exc()
        return {"reply": "Sorry — unexpected error. Try again.", "conversation": (req.conversation_name or "chat_error")}

# ---------------- health ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}
    
