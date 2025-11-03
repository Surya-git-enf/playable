
# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import os, json, csv, io, re, traceback
import requests, feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai

# Optional: Supabase persistence
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Gemini News Summarizer (env vars + optional Supabase)")

# ----------------------------
# Environment-configured values
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables on Render.")

SHEET_ID = os.getenv("SHEET_ID", "").strip()          # optional
SHEET_GID = os.getenv("SHEET_GID", "0").strip()       # optional, default 0

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()  # optional
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()  # optional

# RSS fallback list (can be extended via editing)
RSS_FEEDS = [
    "https://www.nasa.gov/feeds/nasalive.xml",        # NASA feeds (different endpoints exist)
    "https://www.nasa.gov/feeds/iotd-feed/",
    "https://www.space.com/feeds/all",
    "https://www.gadgets360.com/rss/feeds",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://techcrunch.com/feed/"
]

# Gemini model name (change if needed)
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Initialize Supabase client if credentials provided and lib available
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase client init error:", e)
        supabase = None

# ----------------------------
# Request model
# ----------------------------
class ChatReq(BaseModel):
    message: str
    user_email: str | None = None   # optional — if provided, history will be saved (requires Supabase vars)

# ----------------------------
# Utility helpers
# ----------------------------
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"):
    """
    Returns list of dict rows from public Google Sheet CSV export.
    Expected columns (any order): headline, news, categories, link, image_url, date (YYYY-MM-DD)
    If sheet_id is empty, returns [].
    """
    if not sheet_id:
        return []
    url = sheet_csv_url(sheet_id, gid)
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        s = r.content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(s))
        rows = []
        for row in reader:
            # normalize keys to lowercase
            normalized = {k.strip().lower(): (v or "").strip() for k, v in row.items()}
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        return []

def find_recent_sheet_news(topic: str, rows, days_limit: int = 2):
    """
    Finds first recent row (≤ days_limit) where topic appears in categories or headline.
    """
    topic_l = topic.lower().strip()
    today = datetime.utcnow().date()
    for r in rows:
        # try several common date keys
        date_str = r.get("date") or r.get("published") or r.get("pubdate") or ""
        try:
            d = datetime.strptime(date_str.strip(), "%Y-%m-%d").date() if date_str else None
        except Exception:
            d = None
        if d and (today - d).days <= days_limit:
            headline = r.get("headline", "")
            categories = r.get("categories", r.get("category", ""))
            if topic_l in headline.lower() or topic_l in categories.lower():
                return {
                    "headline": headline,
                    "news": r.get("news") or r.get("summary") or "",
                    "link": r.get("link") or "",
                    "image_url": r.get("image_url") or r.get("image") or ""
                }
    return None

def search_rss_for_topic(topic: str):
    topic_l = topic.lower().strip()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:10]:
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                if topic_l in title or topic_l in summary:
                    return {
                        "headline": entry.get("title"),
                        "link": entry.get("link"),
                        "summary": entry.get("summary", ""),
                        "published": entry.get("published", "")
                    }
        except Exception as e:
            print("RSS error for", url, e)
    return None

def extract_article_text(url: str, max_chars: int = 8000):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"}
        resp = requests.get(url, timeout=12, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Try <article> first
        article = soup.find("article")
        texts = []
        if article:
            for p in article.find_all("p"):
                t = p.get_text(strip=True)
                if t:
                    texts.append(t)
        else:
            # fallback: collect visible paragraphs
            for p in soup.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 30:
                    texts.append(t)

        content = "\n\n".join(texts).strip()
        if not content:
            # fallback to meta description
            desc = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
            if desc and desc.get("content"):
                content = desc.get("content")

        if not content:
            return None

        # truncate to avoid huge prompt
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".", 1)[0] + "."
        return content
    except Exception as e:
        print("Article extraction error:", e)
        return None

# ----------------------------
# Supabase chat history helpers
# ----------------------------
def ensure_user_row(email: str):
    """Ensure a user row exists in 'users' table with default chat_history {}."""
    if supabase is None:
        return None
    try:
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        # supabase-py returns a dict with data key
        data = getattr(res, "data", None) or res.get("data") if isinstance(res, dict) else None
        if data:
            return data[0]
        # create if not exists
        create = supabase.table("users").insert({"email": email, "chat_history": {}}).execute()
        created = getattr(create, "data", None) or create.get("data") if isinstance(create, dict) else None
        return created[0] if created else None
    except Exception as e:
        print("ensure_user_row error:", e)
        return None

def get_chat_history(email: str):
    if supabase is None:
        return {}
    try:
        res = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(res, "data", None) or res.get("data") if isinstance(res, dict) else None
        if data:
            return data.get("chat_history", {}) or {}
        return {}
    except Exception as e:
        print("get_chat_history error:", e)
        return {}

def save_chat_history(email: str, chat_history: dict):
    if supabase is None:
        return False
    try:
        res = supabase.table("users").update({"chat_history": chat_history}).eq("email", email).execute()
        status = getattr(res, "status_code", None) or res.get("status_code") if isinstance(res, dict) else None
        # If supabase client doesn't give status_code, check data
        data = getattr(res, "data", None) or res.get("data") if isinstance(res, dict) else None
        return True if data is not None else False
    except Exception as e:
        print("save_chat_history error:", e)
        return False

def append_message_to_conversation(email: str, conv_name: str, sender: str, text: str, meta: dict | None = None):
    try:
        if supabase is None:
            return False
        ensure_user_row(email)
        history = get_chat_history(email) or {}
        if conv_name not in history:
            history[conv_name] = []
        entry = {"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z"}
        if meta:
            entry["meta"] = meta
        history[conv_name].append(entry)
        return save_chat_history(email, history)
    except Exception as e:
        print("append_message_to_conversation error:", e)
        return False

# ----------------------------
# Gemini summarization wrapper
# ----------------------------
def call_gemini_summarize(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are Nova, a friendly and professional news reporter. "
            "Given the article content below, produce a concise, well-structured summary (2-4 short paragraphs). "
            "After the summary, suggest one or two helpful follow-up actions/questions tailored to what the user might want next. "
            "Do not output JSON — only natural text.\n\n"
            f"User message: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt, max_output_tokens=512)
        text = getattr(resp, "text", None)
        if not text:
            # attempt to extract other fields defensively
            try:
                j = resp.__dict__
                text = str(j)
            except Exception:
                text = None
        if not text:
            return "⚠️ Sorry — an error occurred while generating the summary."
        return text.strip()
    except Exception as e:
        print("Gemini call error:", e)
        traceback.print_exc()
        return "⚠️ Sorry — an error occurred while generating the summary."

# ----------------------------
# Small helper to detect "continue" replies like 'yes', 'more'
# ----------------------------
def is_follow_up_yes(message: str):
    return message.strip().lower() in {"yes", "y", "more", "tell me more", "ok", "sure", "continue", "please"}

# ----------------------------
# Main endpoint
# ----------------------------
@app.post("/chat")
def chat(req: ChatReq):
    """POST /chat
    Body: { "message": "...", "user_email": "optional@example.com" }
    """
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message is required")

    user_email = (req.user_email or "").strip().lower() or None

    try:
        # If user wants to continue previous conversation (yes/more) and user_email + supabase present,
        # try to find last conversation and continue.
        if user_email and supabase and is_follow_up_yes(user_message):
            history = get_chat_history(user_email)
            # pick last conversation and last nova entry that contains a link in meta
            if isinstance(history, dict) and history:
                last_conv = list(history.keys())[-1]
                conv_msgs = history.get(last_conv, [])
                # search backwards for last message with meta.link
                last_link = None
                last_headline = None
                for msg in reversed(conv_msgs):
                    meta = msg.get("meta") or {}
                    if meta.get("link"):
                        last_link = meta.get("link")
                        last_headline = meta.get("headline") or last_headline
                        break
                if last_link:
                    article_text = extract_article_text(last_link)
                    if not article_text:
                        return {"reply": "Sorry — I couldn't retrieve more details from the previous article link."}
                    summary = call_gemini_summarize(article_text, last_headline or "the article", user_message)
                    append_message_to_conversation(user_email, last_conv, "nova", summary, meta={"link": last_link, "headline": last_headline})
                    return {"reply": summary, "headline": last_headline, "link": last_link, "conversation": last_conv}

        # Determine topic or direct URL
        link = None
        headline = None
        article_text = None

        # If message is a URL, use it directly
        if user_message.startswith("http://") or user_message.startswith("https://"):
            link = user_message
        else:
            # derive topic: strip 'latest' and 'news'
            topic = re.sub(r"\b(latest|news)\b", "", user_message, flags=re.I).strip()
            if not topic:
                topic = user_message

            # 1) try Google Sheet (if provided)
            rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
            sheet_hit = find_recent_sheet_news(topic, rows, days_limit=2)
            if sheet_hit:
                link = sheet_hit.get("link") or ""
                headline = sheet_hit.get("headline") or ""
                article_text = sheet_hit.get("news") or None

            # 2) if no sheet hit or no article_text, try RSS
            if not link or not article_text:
                rss_item = search_rss_for_topic(topic)
                if rss_item:
                    link = rss_item.get("link") or link
                    headline = rss_item.get("headline") or headline
                    if not article_text:
                        article_text = rss_item.get("summary") or None

        # if we have a link but no article text yet, extract it
        if link and not article_text:
            article_text = extract_article_text(link)

        if not article_text:
            return {"reply": f"Sorry, I couldn't find a recent article for '{user_message}'. Try a different phrase or send a direct link."}

        # Summarize with Gemini
        summary = call_gemini_summarize(article_text, headline or user_message, user_message)

        # Persist to Supabase chat_history if possible
        conv_name = (headline or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}")
        if user_email and supabase:
            append_message_to_conversation(user_email, conv_name, user_email, user_message, meta={"link": link, "headline": headline})
            append_message_to_conversation(user_email, conv_name, "nova", summary, meta={"link": link, "headline": headline})

        # Return response
        return {"reply": summary, "headline": headline, "link": link, "conversation": conv_name}

    except Exception as e:
        print("Chat endpoint error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Internal error occurred. Check server logs for details.")

# Health
@app.get("/")
def root():
    return {"status": "Nova running", "time": datetime.utcnow().isoformat() + "Z"}
