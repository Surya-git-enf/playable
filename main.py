# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import os
import json
import csv
import io
import traceback
import requests
import re
from bs4 import BeautifulSoup
import feedparser
# google generative ai is optional; we'll only call it if configured
try:
    import google.generativeai as genai
except Exception:
    genai = None
from dateutil import parser as dateparser
from typing import Optional, List, Dict

# optional supabase (if not installed, code runs without persistent history)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Global News Summarizer (Sheets + RSS + (optional) Gemini)")

@app.get("/")
def start():
    return {"message": "hi i am Nova , how can I help you?"}

# -------------------------
# CONFIG via ENV (Render)
# -------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()    # optional now
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SHEET_ID = os.getenv("SHEET_ID", "").strip()               # optional (public sheet CSV)
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()       # optional
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()       # optional
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))             # recent preference
EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()             # Comma-separated extra feeds

# Configure Gemini only if key present and library loaded
USE_GEMINI = False
if GEMINI_API_KEY and genai is not None:
    try:
        genai.configure(api_key=GEMINI_API_KEY)
        USE_GEMINI = True
        print("Gemini configured.")
    except Exception as e:
        print("Failed to configure Gemini:", e)
        USE_GEMINI = False
else:
    if not GEMINI_API_KEY:
        print("GEMINI_API_KEY not set — running without Gemini summarizer.")
    else:
        print("google.generativeai library not available — running without Gemini summarizer.")

# Initialize Supabase client if provided
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
        print("Supabase configured.")
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None
else:
    if SUPABASE_URL or SUPABASE_KEY:
        print("Supabase information provided but supabase library not available or incomplete; running without persistent DB.")

# -------------------------
# Default major global RSS feeds (expandable)
# -------------------------
DEFAULT_RSS_FEEDS = [
    # global general
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://rss.cnn.com/rss/edition.rss",
    "https://www.theguardian.com/world/rss",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.npr.org/1001/rss.xml",
    # space/science
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://www.sciencedaily.com/rss/top/science.xml",
    # tech
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    # business
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://feeds.reuters.com/reuters/businessNews",
    # specialty examples
    "https://www.spacepolicyonline.com/feeds/posts/default",
    "https://www.spaceflightnow.com/launch-feed/",
]

# merge extras
RSS_FEEDS = DEFAULT_RSS_FEEDS[:]
if EXTRA_RSS:
    # accept comma-separated list
    RSS_FEEDS = EXTRA_RSS.split(",") + RSS_FEEDS

# quick keyword -> category hints (can be extended)
KEYWORD_CATEGORY = {
    "nasa": "space",
    "space": "space",
    "spacex": "space",
    "jwst": "space",
    "comet": "space",
    "ai": "tech",
    "google": "tech",
    "apple": "tech",
    "markets": "business",
    "economy": "business",
    "covid": "world",
    "cricket": "sports",
    "football": "sports",
}

# -------------------------
# Request model
# -------------------------
class ChatReq(BaseModel):
    message: str
    user_email: Optional[str] = None
    prefer_recent: Optional[bool] = True

# -------------------------
# In-memory conversation store (fallback if Supabase not configured)
# -------------------------
conversations: Dict[str, Dict] = {}

# -------------------------
# Helpers: Sheets CSV fetch (public sheet)
# -------------------------
def sheet_csv_url(sheet_id: str, gid: str = "0") -> str:
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0") -> List[Dict]:
    if not sheet_id:
        return []
    try:
        url = sheet_csv_url(sheet_id, gid)
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        text = r.content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(text))
        rows = []
        for row in reader:
            normalized = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        traceback.print_exc()
        return []

# -------------------------
# Date parse helper
# -------------------------
def parse_date_safe(date_str: str):
    if not date_str:
        return None
    try:
        dt = dateparser.parse(date_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        try:
            # fallback to date-only parse
            return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None

# -------------------------
# Search Google Sheet rows for topic
# -------------------------
def search_sheet_for_topic(topic: str, rows: List[Dict], prefer_recent: bool = True) -> List[Dict]:
    topic_l = (topic or "").lower().strip()
    today = datetime.utcnow().date()
    recent_matches = []
    any_matches = []
    for r in rows:
        combined = " ".join([
            r.get("headline", ""),
            r.get("news", ""),
            r.get("categories", ""),
            r.get("link", ""),
            r.get("image_url", ""),
        ]).lower()
        if topic_l in combined:
            any_matches.append(r)
            d = parse_date_safe(r.get("date", "") or r.get("published", "") or "")
            if d and (today - d.date()).days <= DAYS_LIMIT:
                recent_matches.append(r)
    if prefer_recent and recent_matches:
        return recent_matches
    return any_matches

# -------------------------
# RSS search by category / feeds
# -------------------------
def map_keyword_to_category(topic: str) -> Optional[str]:
    for k, cat in KEYWORD_CATEGORY.items():
        if k in (topic or "").lower():
            return cat
    for cat in ["space", "tech", "business", "world", "sports"]:
        if cat in (topic or "").lower():
            return cat
    return None

def search_rss_for_topic(topic: str, max_items: int = 20) -> List[Dict]:
    topic_l = (topic or "").lower().strip()
    found = []
    cat = map_keyword_to_category(topic)
    feeds_to_check = []

    if cat:
        for url in RSS_FEEDS:
            if cat in url or any(word in url for word in [cat, "space", "tech", "reuters", "nasa", "space.com"]):
                feeds_to_check.append(url)
        feeds_to_check += [u for u in RSS_FEEDS if u not in feeds_to_check]
    else:
        feeds_to_check = RSS_FEEDS

    for feed_url in feeds_to_check:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:max_items]:
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                link = entry.get("link")
                tags = ""
                try:
                    if entry.get("tags"):
                        tags = " ".join([t.get("term", "") for t in entry.get("tags", [])])
                except Exception:
                    tags = ""
                if topic_l in title or topic_l in summary or topic_l in tags.lower():
                    found.append({
                        "headline": entry.get("title"),
                        "link": link,
                        "summary": entry.get("summary", ""),
                        "published": entry.get("published") or entry.get("updated") or None,
                        "source_feed": feed_url
                    })
        except Exception as e:
            print("RSS parse error for", feed_url, e)
    # sort by published date if present
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found

# -------------------------
# HTML article extractor (best-effort)
# -------------------------
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
            meta = soup.find("meta", {"name": "description"}) or soup.find("meta", {"property": "og:description"})
            if meta and meta.get("content"):
                content = meta.get("content")
        if not content:
            return None
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".", 1)[0] + "."
        return content
    except Exception as e:
        print("Article extraction error:", e)
        traceback.print_exc()
        return None

# -------------------------
# Summarizer: Gemini if available, else fallback snippet
# -------------------------
def summarize_article(article_text: str, headline: str, user_message: str) -> str:
    article_text = article_text or ""
    headline = headline or ""
    user_message = user_message or ""
    # If Gemini is configured, try to use it
    if USE_GEMINI and genai is not None:
        try:
            prompt = (
                "You are Nova — a friendly, concise AI news reporter.\n"
                "Summarize the article below in 2-4 short paragraphs with clear facts. "
                "Then produce one short tailored follow-up question the user might want next (1 sentence).\n\n"
                f"User message: {user_message}\n\n"
                f"Headline: {headline}\n\n"
                f"Article:\n{article_text}\n\n"
                "Summary:"
            )
            model = genai.GenerativeModel(MODEL_NAME)
            resp = model.generate_content(prompt, max_output_tokens=700)
            # resp may have .text or may be more complex depending on library version
            text = getattr(resp, "text", None) or (resp.get("candidates")[0].get("content") if isinstance(resp, dict) and resp.get("candidates") else None)
            if text:
                return text.strip()
            else:
                print("Gemini returned no text; falling back to snippet.")
        except Exception as e:
            print("Gemini error:", e)
            traceback.print_exc()
    # Fallback: provide a short snippet + friendly line
    snippet = article_text.strip()[:800]
    if len(article_text) > 800:
        # try to end on sentence
        try:
            snippet = snippet.rsplit(".", 1)[0] + "."
        except Exception:
            pass
    fallback = f"(Summary fallback) {snippet}\n\nWould you like more detail or the full article link?"
    return fallback

# -------------------------
# Conversation persistence (local + supabase)
# -------------------------
def save_local_conversation(email: str, conv_name: str, sender: str, text: str, meta: Optional[Dict] = None):
    if not email:
        return
    if email not in conversations:
        conversations[email] = {"last_conv": conv_name, "convs": {conv_name: []}}
    if conv_name not in conversations[email]["convs"]:
        conversations[email]["convs"][conv_name] = []
    conversations[email]["convs"][conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z", "meta": meta or {}})
    conversations[email]["last_conv"] = conv_name

def save_supabase_conversation(email: str, conv_name: str, sender: str, text: str, meta: Optional[Dict] = None) -> bool:
    if not supabase:
        return False
    try:
        # ensure user row exists (best-effort)
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)
        if not data:
            supabase.table("users").insert({"email": email, "chat_history": {}}).execute()
        # fetch current
        r2 = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        r2data = getattr(r2, "data", None) or (r2.get("data") if isinstance(r2, dict) else None)
        hist = (r2data.get("chat_history") if r2data else {}) or {}
        if isinstance(hist, str):
            try:
                hist = json.loads(hist)
            except Exception:
                hist = {}
        if not isinstance(hist, dict):
            hist = {}
        if conv_name not in hist:
            hist[conv_name] = []
        hist[conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z", "meta": meta or {}})
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("Supabase save error:", e)
        traceback.print_exc()
        return False

# -------------------------
# Read user preferences from chat_history (last N user messages)
# -------------------------
COMMON_TOPICS = [
    "moon", "red moon", "nasa", "space", "black hole", "blackhole", "spacex", "jwst",
    "moon eclipse", "eclipse", "cricket", "football", "ai", "google", "apple"
]

def get_user_preferences_from_history(email: Optional[str], limit_messages: int = 5) -> List[str]:
    if not email:
        return []
    messages = []
    hist = {}
    if supabase:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist = (data.get("chat_history") if data else {}) or {}
            if isinstance(hist, str):
                try:
                    hist = json.loads(hist)
                except Exception:
                    hist = {}
        except Exception:
            hist = {}
    else:
        hist = conversations.get(email, {}).get("convs", {}) if email in conversations else {}

    items = []
    if isinstance(hist, dict):
        for conv, msgs in hist.items():
            if isinstance(msgs, list):
                for m in msgs:
                    ts = m.get("ts")
                    items.append((ts, m))
    try:
        items.sort(key=lambda x: x[0] or "", reverse=True)
    except Exception:
        pass

    for _, m in items:
        sender = (m.get("sender") or "").lower()
        if sender and sender not in {"nova", "system"}:
            messages.append(m.get("text") or "")
        if len(messages) >= limit_messages:
            break

    prefs = []
    for msg in messages:
        t = (msg or "").lower()
        for ct in COMMON_TOPICS:
            if ct in t and ct not in prefs:
                prefs.append(ct)
        for k in KEYWORD_CATEGORY.keys():
            if k in t and k not in prefs:
                prefs.append(k)
        # fallback tokens
        tokens = re.findall(r"\b[a-z]{3,20}\b", t)
        for tk in tokens:
            if tk not in prefs and len(prefs) < 5:
                prefs.append(tk)
    cleaned = []
    for p in prefs:
        p_clean = p.strip()
        if p_clean and p_clean not in cleaned:
            cleaned.append(p_clean)
    return cleaned[:5]

# -------------------------
# Follow-up detection
# -------------------------
def is_affirmative_reply(text: str) -> bool:
    t = (text or "").strip().lower()
    return t in {"yes", "y", "yeah", "yep", "sure", "absolutely", "ok", "okay", "please", "tell me more", "more", "continue"}

# -------------------------
# Debug endpoint (no Gemini) - inspect what sheet/RSS returns
# -------------------------
@app.post("/chat_debug")
def chat_debug(req: ChatReq):
    try:
        print("DEBUG: /chat_debug received:", req.dict())
        email = (req.user_email or "").strip().lower() or None
        topic = (req.message or "").strip()
        if not topic:
            return {"error": "message required", "received": req.dict()}
        sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
        sheet_matches = []
        if sheet_rows:
            sheet_matches = search_sheet_for_topic(topic, sheet_rows, prefer_recent=bool(req.prefer_recent))
        rss_found = search_rss_for_topic(topic, max_items=10)
        return {
            "received": req.dict(),
            "sheet_matches_count": len(sheet_matches),
            "sheet_sample": sheet_matches[:3],
            "rss_found_count": len(rss_found),
            "rss_sample": rss_found[:3],
        }
    except Exception as e:
        print("ERROR in /chat_debug:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# -------------------------
# Main chat endpoint (robust - always returns fallback if summarizer fails)
# -------------------------
@app.post("/chat")
def chat(req: ChatReq):
    try:
        user_message = (req.message or "").strip()
        if not user_message:
            raise HTTPException(status_code=400, detail="message required")
        email = (req.user_email or "").strip().lower() or None
        prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)

        topic = re.sub(r"\b(latest|news|give me|show me|tell me|what's|is there|any)\b", "", user_message, flags=re.I).strip()
        if not topic:
            topic = user_message

        # Check if user replied 'yes' to continue previous article
        if email and is_affirmative_reply(user_message) and ((email in conversations and conversations[email].get("last_conv")) or supabase):
            last_conv = conversations.get(email, {}).get("last_conv") if email in conversations else None
            if not last_conv and supabase and email:
                try:
                    r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
                    data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
                    hist = data.get("chat_history", {}) if data else {}
                    if hist:
                        last_conv = list(hist.keys())[-1]
                except Exception:
                    last_conv = None
            if last_conv:
                # find last link from local conv or supabase
                msgs = conversations.get(email, {}).get("convs", {}).get(last_conv, []) if email in conversations else []
                last_link = None
                headline = None
                for m in reversed(msgs):
                    meta = m.get("meta") or {}
                    if meta.get("link"):
                        last_link = meta.get("link")
                        headline = meta.get("headline")
                        break
                if not last_link and supabase and email:
                    try:
                        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
                        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
                        hist = data.get("chat_history", {}) if data else {}
                        conv_msgs = hist.get(last_conv, [])
                        for m in reversed(conv_msgs):
                            if m.get("meta", {}).get("link"):
                                last_link = m["meta"]["link"]
                                headline = m["meta"].get("headline")
                                break
                    except Exception:
                        pass
                if not last_link:
                    return {"reply": "I couldn't find the previous article link to continue. Send a direct link or ask about a topic."}
                article_text = extract_article_text(last_link)
                if not article_text:
                    return {"reply": f"Couldn't fetch more details from {last_link}. Here's the link: {last_link}"}
                deeper_prompt = (
                    "You are Nova — now give a richer, deeper explanation about the article, "
                    "covering context, significance, and comparisons (if applicable). Keep it clear and factual."
                )
                combined_text = deeper_prompt + "\n\n" + article_text
                deep_summary = summarize_article(combined_text, headline or topic, user_message)
                if email:
                    save_local_conversation(email, last_conv, "nova", deep_summary, meta={"link": last_link, "headline": headline})
                    if supabase:
                        save_supabase_conversation(email, last_conv, "nova", deep_summary, meta={"link": last_link, "headline": headline})
                return {"reply": deep_summary, "link": last_link}

        # New behavior: consult user's chat history for topic preferences
        prefs = get_user_preferences_from_history(email) if email else []
        ordered_topics: List[str] = []
        for p in prefs:
            if p not in ordered_topics:
                ordered_topics.append(p)
        if topic and topic not in ordered_topics:
            ordered_topics.append(topic)
        if not ordered_topics:
            ordered_topics = [topic]

        # 1) Try Google Sheet first (prioritize preferences)
        sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
        articles: List[Dict] = []

        for t in ordered_topics:
            if len(articles) >= MAX_RESULTS:
                break
            if sheet_rows:
                matches = search_sheet_for_topic(t, sheet_rows, prefer_recent=prefer_recent)
                for r in matches:
                    if len(articles) >= MAX_RESULTS:
                        break
                    headline = r.get("headline") or r.get("title") or t
                    link = r.get("link") or ""
                    article_text = r.get("news") or r.get("summary") or ""
                    published = r.get("date") or None
                    if link and not article_text:
                        article_text = extract_article_text(link)
                    articles.append({"headline": headline, "link": link, "article_text": article_text, "published": published, "source": "sheet", "matched_topic": t})

        # 2) If not enough, search RSS prioritized by ordered_topics
        if len(articles) < MAX_RESULTS:
            for t in ordered_topics:
                if len(articles) >= MAX_RESULTS:
                    break
                rss_found = search_rss_for_topic(t, max_items=20)
                for item in rss_found:
                    if len(articles) >= MAX_RESULTS:
                        break
                    link = item.get("link")
                    headline = item.get("headline") or t
                    article_text = extract_article_text(link) or item.get("summary") or ""
                    articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss", "matched_topic": t})

        if not articles:
            friendly = ("I couldn't find articles for your request in the sheet or RSS feeds. "
                        "Try a different query or provide a link. "
                        f"I searched based on your recent chats: {', '.join(prefs)}") if prefs else "I couldn't find articles for your request in the sheet or RSS feeds. Try a different query or provide a link."
            return {"reply": friendly}

        # Summarize articles
        summaries = []
        for art in articles[:MAX_RESULTS]:
            art_text = art.get("article_text") or ""
            if not art_text:
                summaries.append({
                    "headline": art.get("headline"),
                    "link": art.get("link"),
                    "summary": f"❗️ No extractable text found at {art.get('link')}.",
                    "source": art.get("source"),
                    "matched_topic": art.get("matched_topic")
                })
                continue
            summary_text = summarize_article(art_text, art.get("headline", topic), user_message)
            # ensure fallback if summarizer gives odd values
            if not summary_text or str(summary_text).strip().lower() in {"", "none", "null"}:
                snippet = art_text[:800]
                try:
                    snippet = snippet.rsplit(".", 1)[0] + "."
                except Exception:
                    pass
                summary_text = f"(Fallback) {snippet}"
            summaries.append({
                "headline": art.get("headline"),
                "link": art.get("link"),
                "summary": summary_text,
                "source": art.get("source"),
                "published": art.get("published"),
                "matched_topic": art.get("matched_topic")
            })

        # Compose reply
        lead = "Hey — based on your request and recent chat history, here's what I found:\n\n"
        blocks = []
        for i, s in enumerate(summaries, start=1):
            topic_note = f" (matched: {s.get('matched_topic')})" if s.get('matched_topic') else ""
            block = f"{i}. {s.get('headline')}{topic_note}\n\n{s.get('summary')}\n\nLink: {s.get('link')}"
            blocks.append(block)
        combined_reply = lead + "\n\n---\n\n".join(blocks)

        # Save conversation (local + supabase)
        conv_name = topic or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        if email:
            save_local_conversation(email, conv_name, email, user_message, meta={"topic": topic, "prefs_used": prefs})
            save_local_conversation(email, conv_name, "nova", combined_reply, meta={"results": len(summaries)})
            if supabase:
                try:
                    save_supabase_conversation(email, conv_name, email, user_message, meta={"topic": topic, "prefs_used": prefs})
                    save_supabase_conversation(email, conv_name, "nova", combined_reply, meta={"results": len(summaries)})
                except Exception as e:
                    print("supabase save failed", e)
                    traceback.print_exc()

        followup_hint = "\n\nWould you like more detail on any of these (reply 'yes' or the number)?"
        return {"reply": combined_reply + followup_hint, "count": len(summaries), "conversation": conv_name}
    except Exception as e:
        print("ERROR in /chat:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# -------------------------
# health
# -------------------------
@app.get("/health")
def health():
    return {"status": "ok"}
                
