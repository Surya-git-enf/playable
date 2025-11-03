# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import os
import json
import requests
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from supabase import create_client

app = FastAPI(title="Nova — Gemini News Agent (Supabase chat history)")

# -------------------------
# Environment config
# -------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY environment variable.")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise RuntimeError("Please set SUPABASE_URL and SUPABASE_KEY environment variables.")

# configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")

# supabase client
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# RSS fallback sources (used if no sheet hit)
RSS_FEEDS = [
    "https://www.gadgets360.com/rss/feeds",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "http://www.chinadaily.com.cn/rss/china_rss.xml",
    "https://www.space.com/feeds.xml",
    "https://www.nasa.gov/feeds/iotd-feed/",
]

# -------------------------
# System prompt (dynamic follow-ups)
# -------------------------
SYSTEM_PROMPT = (
    "You are Nova 🪶 — a friendly, concise AI news reporter and assistant. "
    "When given an article, summarize it like a professional journalist (2-4 short paragraphs). "
    "Then predict one or two helpful follow-up actions/questions the user might want (e.g., 'Would you like links to full articles?', "
    "'Shall I save this in your reading list?', 'Do you want a short TL;DR?'). "
    "Do not always end with the exact same sentence; tailor the question(s) to the article and user intent."
)

# -------------------------
# Request model
# -------------------------
class Msg(BaseModel):
    user_email: str
    message: str
    conversation_name: str | None = None   # optional: use or create
    # if message is "latest nasa news" we parse topic; if it's a link we use the link


# -------------------------
# Utility helpers
# -------------------------
def safe_now_iso():
    return datetime.utcnow().isoformat() + "Z"

def extract_text_from_url(url: str, max_chars: int = 3000) -> str | None:
    """
    Fetches the URL and extracts main textual content using BeautifulSoup best-effort.
    Truncates to max_chars to avoid huge prompts.
    """
    try:
        resp = requests.get(url, timeout=12)
        resp.raise_for_status()
        html = resp.text
        soup = BeautifulSoup(html, "html.parser")

        # Try article tag first
        article = soup.find("article")
        texts = []
        if article:
            for p in article.find_all("p"):
                texts.append(p.get_text(strip=True))
        else:
            # fallback: collect paragraphs but avoid nav/footer
            for p in soup.find_all("p"):
                text = p.get_text(strip=True)
                if len(text) > 20:  # skip tiny bits
                    texts.append(text)

        content = "\n\n".join(texts).strip()
        if not content:
            # fallback: meta description
            desc = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
            if desc and desc.get("content"):
                content = desc.get("content")

        if not content:
            return None

        # truncate gracefully
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".", 1)[0] + "."
        return content
    except Exception as e:
        print("extract_text_from_url error:", e)
        return None

def find_from_rss_by_topic(topic: str):
    topic = topic.lower().strip()
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries[:8]:
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                if topic in title or topic in summary:
                    return {
                        "headline": entry.get("title"),
                        "link": entry.get("link"),
                        "summary": entry.get("summary", ""),
                        "published": entry.get("published", "")
                    }
        except Exception as e:
            print("RSS error for", url, e)
    return None

# -------------------------
# Supabase helpers (users table with chat_history JSONB)
# -------------------------
def ensure_user_row(email: str):
    """
    Make sure a row exists in 'users' table with the given email.
    Expected schema: users(email TEXT PRIMARY KEY, chat_history JSONB)
    """
    try:
        resp = supabase.table("users").select("email, chat_history").eq("email", email).single().execute()
        if resp.status_code in (200, 201) and resp.data:
            return resp.data
        # create
        created = supabase.table("users").insert({"email": email, "chat_history": {}}).execute()
        return created.data[0] if created.data else None
    except Exception as e:
        print("Supabase ensure_user_row error:", e)
        return None

def get_chat_history(email: str) -> dict:
    try:
        row = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        if row and row.data:
            return row.data.get("chat_history") or {}
        return {}
    except Exception as e:
        print("Supabase get_chat_history error:", e)
        return {}

def save_chat_history(email: str, chat_history: dict) -> bool:
    try:
        res = supabase.table("users").update({"chat_history": chat_history}).eq("email", email).execute()
        return res.status_code in (200, 201)
    except Exception as e:
        print("Supabase save_chat_history error:", e)
        return False

def append_message_to_conversation(email: str, conv_name: str, sender: str, text: str):
    """
    Append a message object to chat_history[conv_name] = [ {sender, text, ts}, ... ]
    If conversation not exists, create it.
    """
    try:
        ensure_user_row(email)
        history = get_chat_history(email)
        if not isinstance(history, dict):
            history = {}
        if conv_name not in history:
            history[conv_name] = []
        entry = {"sender": sender, "text": text, "ts": safe_now_iso()}
        history[conv_name].append(entry)
        ok = save_chat_history(email, history)
        return ok
    except Exception as e:
        print("append_message_to_conversation error:", e)
        return False

# -------------------------
# Gemini call (safe wrapper)
# -------------------------
def call_gemini_summarize(article_text: str, headline: str, user_message: str) -> str:
    """
    Send a summarization prompt to Gemini and return the generated text.
    """
    try:
        # Build a prompt that clearly instructs summarization + follow-up predictions
        prompt = (
            f"{SYSTEM_PROMPT}\n\n"
            f"User message: {user_message}\n\n"
            f"Article headline: {headline}\n\n"
            f"Article content:\n{article_text}\n\n"
            f"Instructions: Summarize the article concisely (2-4 short paragraphs). "
            f"Then predict one or two helpful follow-up actions/questions tailored to the user. "
            f"Return only the summary and follow-up suggestions (no JSON)."
        )
        # Use the generative model
        model = genai.GenerativeModel(MODEL_NAME)
        response = model.generate_content(prompt, max_output_tokens=512)
        text = getattr(response, "text", None)
        if not text:
            # Try alternative fields (defensive)
            j = response.__dict__ if hasattr(response, "__dict__") else {}
            text = str(j)
        return text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "⚠️ Sorry — I couldn't generate a summary right now. Try again in a moment."

# -------------------------
# Main endpoint
# -------------------------
@app.post("/chat")
def chat(msg: Msg):
    """
    Workflow:
    - use user_email (ensure exists in supabase)
    - determine if message contains a direct link -> prioritize summarizing that link
    - else try to look up recent news (via RSS fallback)
    - fetch article text from link, summarize via Gemini
    - store messages in Supabase chat_history under a conversation name
    """
    try:
        email = msg.user_email.strip().lower()
        if "@" not in email:
            raise HTTPException(status_code=400, detail="Invalid user_email")

        # ensure user exists
        ensure_user_row(email)

        user_msg = msg.message.strip()
        conv_name = msg.conversation_name

        # If message looks like a URL, prioritize it
        link = None
        if user_msg.startswith("http://") or user_msg.startswith("https://"):
            link = user_msg
        else:
            # Try to interpret "latest X news" -> use RSS fallback to find an article
            # Determine topic heuristically: take last two words if message contains 'latest' or 'news'
            topic = user_msg.lower().replace("latest", "").replace("news", "").strip()
            if topic:
                rss_item = find_from_rss_by_topic(topic)
                if rss_item:
                    link = rss_item.get("link")

        if not link:
            # nothing found
            reply_text = "Sorry, I couldn't find a link for that topic. Can you send a direct article link or be more specific?"
            # Save user message
            conv_to_use = conv_name or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
            append_message_to_conversation(email, conv_to_use, email, user_msg)
            append_message_to_conversation(email, conv_to_use, "nova", reply_text)
            return {"reply": reply_text, "conversation": conv_to_use}

        # fetch article content
        article_text = extract_text_from_url(link, max_chars=6000)
        if not article_text:
            # fallback: if we couldn't parse full text, try to pass summary or title from RSS
            feed_try = feedparser.parse(link)
            fallback_summary = (feed_try.entries[0].get("summary") if feed_try.entries else "") if feed_try else ""
            article_text = fallback_summary or "No readable article text found."

        # choose conversation name if not provided: use headline or timestamp
        # try to get headline from link via feedparser or page title
        headline = None
        try:
            parsed = feedparser.parse(link)
            if parsed and parsed.entries:
                headline = parsed.entries[0].get("title")
        except Exception:
            headline = None
        if not headline:
            # try to fetch page title
            try:
                r = requests.get(link, timeout=8)
                soup = BeautifulSoup(r.text, "html.parser")
                t = soup.title.string if soup.title else None
                headline = t
            except Exception:
                headline = "article"

        conv_to_use = conv_name or headline or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"

        # Save the user's request in chat history
        append_message_to_conversation(email, conv_to_use, email, user_msg)

        # Generate summary using Gemini (article_text truncated)
        # Limit article_text length in case it's very large (we already truncated in extractor)
        if len(article_text) > 15000:
            article_text = article_text[:15000]

        ai_reply = call_gemini_summarize(article_text, headline, user_msg)

        # Save Nova's reply to Supabase
        append_message_to_conversation(email, conv_to_use, "nova", ai_reply)

        return {"reply": ai_reply, "headline": headline, "link": link, "conversation": conv_to_use}

    except HTTPException as he:
        raise he
    except Exception as e:
        print("Chat endpoint error:", e)
        return {"error": "Internal error", "details": str(e)}

# -------------------------
# Health
# -------------------------
@app.get("/")
def root():
    return {"status": "Nova running", "time": safe_now_iso()}
