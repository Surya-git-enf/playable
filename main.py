# main.py
import os
import re
import json
import time
import uuid
import sqlite3
import traceback
from typing import Optional, List, Dict, Any, Tuple
from datetime import datetime, timezone, timedelta
from urllib.parse import urlparse

import requests
import feedparser
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Google/PaLM SDK (Gemini) — robust calls below
import google.generativeai as genai

# Optional supabase (best-effort)
try:
    from supabase import create_client as create_supabase_client
except Exception:
    create_supabase_client = None

# ---------------- CONFIG (env) ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SQLITE_DB = os.getenv("SQLITE_DB", "nova_app.db")
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
RSS_RATE_LIMIT_SECONDS = int(os.getenv("RSS_RATE_LIMIT_SECONDS", "60"))

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

# optional supabase client
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_supabase_client is not None:
    try:
        supabase = create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)

app = FastAPI(title="Nova — Intent-driven News Assistant (with local cache)")

# ---------------- DATABASE (sqlite) ----------------
def init_sqlite(path: str):
    conn = sqlite3.connect(path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
    CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        chat_history TEXT DEFAULT '[]',
        updated_at TEXT
    )
    """)
    cur.execute("""
    CREATE TABLE IF NOT EXISTS conversation_cache (
        conversation_name TEXT PRIMARY KEY,
        cache_json TEXT,
        updated_at TEXT
    )
    """)
    conn.commit()
    return conn

DB = init_sqlite(SQLITE_DB)

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
    cur.execute("INSERT INTO users (email, chat_history, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(email) DO UPDATE SET chat_history=excluded.chat_history, updated_at=excluded.updated_at",
                (email, json.dumps(history), now))
    DB.commit()
    # try supabase
    if supabase:
        try:
            supabase.table("users").upsert({"email": email, "chat_history": history}).execute()
        except Exception as e:
            print("supabase upsert error:", e)

def append_message_to_conversation(email: str, conv_name: str, sender: str, text: str):
    if not email:
        # use pseudo-anonymous local-only storage keyed by "anon"
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
    # last element normally is most recent conversation
    last = history[-1]
    if isinstance(last, dict):
        return list(last.keys())[0]
    return None

# conversation cache helpers
def set_conversation_cache(conv_name: str, payload: dict):
    cur = DB.cursor()
    now = datetime.utcnow().isoformat() + "Z"
    cur.execute("INSERT INTO conversation_cache (conversation_name, cache_json, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(conversation_name) DO UPDATE SET cache_json=excluded.cache_json, updated_at=excluded.updated_at",
                (conv_name, json.dumps(payload), now))
    DB.commit()

def get_conversation_cache(conv_name: str) -> Optional[dict]:
    cur = DB.cursor()
    cur.execute("SELECT cache_json FROM conversation_cache WHERE conversation_name = ?", (conv_name,))
    row = cur.fetchone()
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None

def clear_conversation_cache(conv_name: str):
    cur = DB.cursor()
    cur.execute("DELETE FROM conversation_cache WHERE conversation_name = ?", (conv_name,))
    DB.commit()

# ---------------- RSS feeds & rate limiter ----------------
DEFAULT_RSS_FEEDS = [
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://feeds.bbci.co.uk/news/world/rss.xml",
]
FEED_FETCH_TIMES: Dict[Tuple[str, str], float] = {}  # (user_email, feed_url) -> last_ts

CATEGORY_PATH_KEYWORDS = ["/tech", "/technology", "/space", "/nasa", "/science", "/business", "/sports", "/entertainment", "/world"]

# ---------------- UTIL: sheets fetch (csv export) ----------------
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0") -> List[dict]:
    if not sheet_id:
        return []
    try:
        r = requests.get(sheet_csv_url(sheet_id, gid), timeout=12)
        r.raise_for_status()
        text = r.content.decode("utf-8")
        import csv, io
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            rows.append({(k or "").strip().lower(): (v or "").strip() for k,v in row.items()})
        return rows
    except Exception as e:
        print("fetch_sheet_rows error:", e)
        return []

# ---------------- UTIL: article extraction ----------------
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
            # fallback: long paragraphs
            for p in soup.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 40:
                    texts.append(t)
        content = "\n\n".join(texts).strip()
        if not content:
            meta = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
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

# ---------------- MODEL helpers: robust genai calls ----------------
SYSTEM_PROMPT = """
You are NewsAssistant (Nova). Follow the workflow:
1) Decide if user asks for news (needs tools) or general chat.
2) If summarizing articles, be concise and chatty (1-3 short paragraphs) and finish with a single friendly question.
3) When asked to format results, return natural chat style (do not reveal internal tool details).
"""

def genai_call_text(prompt: str) -> Optional[str]:
    """
    Robust wrapper: try multiple SDK shapes and return string or None.
    """
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
        print("genai.Client() failed:", e)
    try:
        if hasattr(genai, "generate_content"):
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("genai.generate_content() failed:", e)
    return None

# chat call: use small context
def call_gemini_chat(user_message: str, conversation_name: Optional[str]=None, context_history: Optional[List[dict]]=None) -> str:
    ctx = ""
    try:
        if context_history:
            lines = []
            # include up to last 6 messages across latest conversation
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
    res = genai_call_text(prompt)
    if not res:
        # fallback short echo
        return f"Sorry — I couldn't reach the assistant. You said: {user_message[:200]}"
    return res

# intent detection: model decides news/chat/followup and returns topic
def call_gemini_intent(user_message: str, conversation_name: Optional[str]=None, context_history: Optional[List[dict]]=None) -> dict:
    ctx = ""
    try:
        if context_history:
            lines = []
            for conv in context_history[-6:]:
                if isinstance(conv, dict):
                    for k,msgs in conv.items():
                        for m in msgs[-4:]:
                            lines.append(f"{m.get('sender')}: {m.get('text')}")
            if lines:
                ctx = "\n\nPrevious:\n" + "\n".join(lines[-10:])
    except Exception:
        ctx = ""
    intent_prompt = (
        SYSTEM_PROMPT + "\n\n"
        "Task: Output ONLY a JSON object describing intent. Fields: {\"intent\":\"news\"|\"chat\"|\"followup\",\"topic\":\"...\",\"confidence\":0.0}\n"
        "Examples:\n"
        'User: "Latest NASA updates?" -> {"intent":"news","topic":"nasa","confidence":0.98}\n'
        'User: "Yes" -> {"intent":"followup","topic":"", "confidence":0.9}\n'
        'User: "Hey how are you?" -> {"intent":"chat","topic":"", "confidence":0.95}\n\n'
        f"Conversation: {conversation_name or ''}\n\nUser: {user_message}\n\n{ctx}\n\nRespond with the JSON only:"
    )
    text = genai_call_text(intent_prompt)
    if text:
        try:
            # extract JSON substring
            jstart = text.find("{")
            jend = text.rfind("}")
            if jstart != -1 and jend != -1 and jend > jstart:
                jtext = text[jstart:jend+1]
            else:
                jtext = text.strip()
            parsed = json.loads(jtext)
            intent = parsed.get("intent","").lower() if isinstance(parsed.get("intent",""), str) else "chat"
            topic = (parsed.get("topic") or "").strip()
            confidence = float(parsed.get("confidence") or 0.0)
            if intent not in ("news","chat","followup"):
                raise ValueError("bad intent")
            return {"intent": intent, "topic": topic, "confidence": confidence}
        except Exception as e:
            print("intent JSON parse failed:", e, "raw:", repr(text))
    # fallback heuristic
    low = user_message.lower().strip()
    if low in ("yes","y","yeah","yep","sure","continue","go on","ok"):
        return {"intent":"followup","topic":"", "confidence":0.6}
    if any(k in low for k in ["news","latest","update","summary","article","trailer","season","episode"]):
        # attempt to extract topic phrase
        t = extract_topic_from_message(user_message) if 'extract_topic_from_message' in globals() else extract_topic(user_message)
        return {"intent":"news","topic":t[:120], "confidence":0.5}
    return {"intent":"chat","topic":"", "confidence":0.5}

# ---------------- small helpers ----------------
def extract_topic(user_message: str) -> str:
    if not user_message:
        return "news"
    m = re.search(r"(?:about|on|regarding|about\s|on\s)([^?.!]+)", user_message, re.I)
    if m:
        topic = m.group(1).strip()
        topic = re.sub(r"\b(today|please|now|for my project|for my)\b","",topic,flags=re.I).strip()
        return topic[:140]
    cleaned = re.sub(r"\b(hey|hi|hello|please|can i|could you|i want|i'd like)\b","", user_message, flags=re.I).strip()
    return cleaned[:140] or "news"

def extract_topic_from_message(msg: str) -> str:
    return extract_topic(msg)

def sanitize_conv_name(name: str, max_words: int = 6) -> str:
    if not name:
        return f"chat_{uuid.uuid4().hex[:6]}"
    cleaned = re.sub(r"[^A-Za-z0-9 _-]", "", name).strip()
    words = cleaned.split()
    short = " ".join(words[:max_words])
    return short or f"chat_{uuid.uuid4().hex[:6]}"

# ---------------- RSS search that prefers path keywords ----------------
def search_rss_for_topic(topic: str, user_email: Optional[str]=None, max_items: int = 40, category: Optional[str]=None) -> Tuple[List[dict], List[str]]:
    topic_l = (topic or "").lower().strip()
    found = []
    feeds_checked = []
    feeds_to_check = []
    try:
        if category and category in CATEGORY_PATH_KEYWORDS:
            pass
        # add category-preferred feeds (simple approach)
        feeds_to_check = DEFAULT_RSS_FEEDS.copy()
        for feed_url in feeds_to_check:
            feeds_checked.append(feed_url)
            # rate-limit per user+feed
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
                            "published": entry.get("published") or entry.get("updated") or None,
                            "source_feed": feed_url
                        })
            except Exception as e:
                print("RSS parse error for", feed_url, e)
    except Exception as e:
        print("search_rss_for_topic error:", e)
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found, feeds_checked

# ---------------- Summarizer (system prompt prepended) ----------------
def summarize_article_with_gemini(article_text: str, headline: str, user_message: str) -> str:
    prompt = (
        SYSTEM_PROMPT + "\n\n"
        "You are Nova — a friendly, human-sounding AI news assistant.\n"
        "Summarize the article below in 2 short paragraphs (clear facts, conversational tone). "
        "End with one simple follow-up question to the user.\n\n"
        f"User message: {user_message}\n\nHeadline: {headline}\n\nArticle:\n{article_text}\n\nSummary:"
    )
    res = genai_call_text(prompt)
    if res:
        return res
    # fallback: extract first 2 paragraphs
    parts = article_text.split("\n\n")
    short = "\n\n".join(parts[:2]).strip()
    return short + "\n\n(Unable to generate full AI summary right now.)"

# ---------------- Intent-driven /chat endpoint ----------------
class ChatReq(BaseModel):
    user_email: Optional[str] = None
    message: str
    conversation_name: Optional[str] = None

@app.post("/chat")
def chat(req: ChatReq):
    """
    Main endpoint:
    1) call model intent detector (call_gemini_intent)
    2) route to tools (news) OR model chat (chat)
    3) store history and conversation cache to implement 'Yes' continuation
    """
    try:
        email = (req.user_email or "").strip().lower() or None
        user_message = (req.message or "").strip()
        conv_name_in = (req.conversation_name or "").strip() or None

        if not user_message:
            raise HTTPException(status_code=400, detail="message required")

        # determine conversation name (if provided use it, else try to make sensible one)
        if conv_name_in:
            conv_name = sanitize_conv_name(conv_name_in)
        else:
            # if user is a followup word like "yes" and has last conv, prefer last conv name
            if email and re.match(r"^(yes|yeah|yep|sure|continue|go on|ok)\b", user_message, flags=re.I):
                last = get_last_conversation(email)
                conv_name = last or sanitize_conv_name(user_message)
            else:
                conv_name = sanitize_conv_name(user_message)

        # ensure we have a row for the user
        if email:
            # create row if missing
            hist = fetch_user_history(email)
            if hist is None:
                upsert_user_history(email, [])

        # append user's message
        append_message_to_conversation(email or "anonymous", conv_name, email or "anonymous", user_message)

        # get small context history to pass to intent model
        context_hist = fetch_user_history(email or "anonymous") if email else None

        # get model's intent decision
        try:
            intent_result = call_gemini_intent(user_message, conversation_name=conv_name, context_history=context_hist)
        except Exception as e:
            print("call_gemini_intent error:", e)
            intent_result = {"intent":"chat","topic":"","confidence":0.0}

        intent = intent_result.get("intent", "chat")
        topic_from_model = (intent_result.get("topic") or "").strip()

        # ---------------- FOLLOWUP handling (Yes / continue)
        if intent == "followup":
            # If we have conversation cache for this conv, resume it
            cache = get_conversation_cache(conv_name)
            # if cache has 'last_list' (articles list) and user said yes -> expand first item details
            if cache and cache.get("last_list"):
                last_list = cache["last_list"]
                # If user provided "yes" without number -> expand first unseen item or give more details
                # We check optional numeric selection
                sel = None
                m = re.match(r"^\s*(\d+)\s*$", user_message)
                if m:
                    sel = int(m.group(1)) - 1
                if sel is None:
                    # choose first item that has not been expanded (or index 0)
                    idx = 0
                    for i,item in enumerate(last_list):
                        if not item.get("expanded"):
                            idx = i
                            break
                else:
                    idx = sel if 0 <= sel < len(last_list) else 0
                item = last_list[idx]
                # get full article text (if not present try extract)
                art_text = item.get("article_text")
                if not art_text and item.get("link"):
                    art_text = extract_article_text(item.get("link"))
                if not art_text:
                    # nothing to expand — ask model for generative expansion
                    follow_text = call_gemini_chat(f"Please give a short expansion for: {item.get('title')}", conversation_name=conv_name, context_history=context_hist)
                    append_message_to_conversation(email or "anonymous", conv_name, "Nova", follow_text)
                    # update nothing else
                    return {"reply": follow_text, "conversation": conv_name}
                # summarize / expand using summarizer
                expanded = summarize_article_with_gemini(art_text, item.get("title") or item.get("headline",""), user_message)
                # mark item expanded in cache
                item["expanded"] = True
                last_list[idx] = item
                cache["last_list"] = last_list
                set_conversation_cache(conv_name, cache)
                append_message_to_conversation(email or "anonymous", conv_name, "Nova", expanded)
                return {"reply": expanded, "conversation": conv_name}
            else:
                # no cache or nothing to continue — ask the model for next action
                ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
                append_message_to_conversation(email or "anonymous", conv_name, "Nova", ai_reply)
                return {"reply": ai_reply, "conversation": conv_name}

        # ---------------- NEWS flow (tools) ----------------
        if intent == "news":
            topic = topic_from_model or extract_topic_from_message(user_message) or user_message
            # 1) check Google Sheets cache
            articles = []
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

            # 2) if no cached -> RSS
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
                # no articles found -> generative fallback summary via chat model
                ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
                append_message_to_conversation(email or "anonymous", conv_name, "Nova", ai_reply)
                return {"reply": ai_reply, "conversation": conv_name, "count": 0}

            # 3) summarize each found article (short) and store in conversation cache for follow-ups
            short_summaries = []
            cache_list = []
            for art in articles:
                # generate a short summary (2 sentences)
                try:
                    if art.get("article_text"):
                        short = summarize_article_with_gemini(art["article_text"], art.get("title") or art.get("headline",""), user_message)
                    else:
                        short = art.get("article_text") or "(no extractable text)"
                except Exception as e:
                    print("summarize error:", e)
                    short = art.get("article_text") or art.get("summary") or "(summary failed)"
                short_summaries.append({"title": art.get("title"), "summary": short, "link": art.get("link"), "source": art.get("source")})
                cache_list.append({"title": art.get("title"), "link": art.get("link"), "article_text": art.get("article_text"), "expanded": False})
            # build friendly chat-style reply
            blocks = []
            for i, s in enumerate(short_summaries, start=1):
                blocks.append(f"{i}. {s['title']}\n\n{s['summary']}\n\nLink: {s['link']}")
            combined_reply = "Hey — I found these:\n\n" + "\n\n---\n\n".join(blocks) + ("\n\nReply with the number to read more, or say 'yes' to expand the first item. Do you want more?" )
            # store the list in conversation cache so follow-up 'yes' expands
            set_conversation_cache(conv_name, {"last_list": cache_list, "topic": topic, "fetched_at": datetime.utcnow().isoformat() + "Z"})
            # persist and return
            append_message_to_conversation(email or "anonymous", conv_name, "Nova", combined_reply)
            return {"reply": combined_reply, "conversation": conv_name, "count": len(short_summaries)}

        # ---------------- CHAT flow (non-news): forward to model ----------------
        ai_reply = call_gemini_chat(user_message, conversation_name=conv_name, context_history=context_hist)
        append_message_to_conversation(email or "anonymous", conv_name, "Nova", ai_reply)
        return {"reply": ai_reply, "conversation": conv_name}

    except Exception as e:
        print("Unhandled /chat error:", e)
        traceback.print_exc()
        return {"reply": "Sorry — unexpected error. Try again.", "conversation": conv_name_in or "chat_error"}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()}
    
