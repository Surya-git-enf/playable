# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import os, json, csv, io, traceback, requests, re
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from dateutil import parser as dateparser

# Optional Supabase client
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Sheets + RSS News Summarizer (Gemini)")

# -------------------------
# CONFIG (from Render env)
# -------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in Render environment variables.")

SHEET_ID = os.getenv("SHEET_ID", "").strip()       # optional
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))     # used for 'recent' preference
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))   # how many articles to summarize

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None

# initialize Gemini client
genai.configure(api_key=GEMINI_API_KEY)

# -------------------------
# Category -> RSS feeds mapping
# extend this map to add more categories/feeds
# -------------------------
CATEGORY_FEEDS = {
    "space": [
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "https://www.space.com/feeds/all",
        "https://www.spacex.com/feeds"
    ],
    "tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index"
    ],
    "business": [
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        "https://feeds.reuters.com/reuters/businessNews"
    ],
    "world": [
        "https://feeds.reuters.com/Reuters/worldNews",
        "https://rss.cnn.com/rss/cnn_world.rss"
    ],
    "sports": [
        "https://www.espn.com/espn/rss/news"
    ],
}

# quick keyword -> category hints
KEYWORD_CATEGORY = {
    "nasa": "space",
    "space": "space",
    "rocket": "space",
    "spacex": "space",
    "tech": "tech",
    "ai": "tech",
    "google": "tech",
    "apple": "tech",
    "business": "business",
    "finance": "business",
    "market": "business",
    "world": "world",
    "news": "world",
    "football": "sports",
    "cricket": "sports",
    "tennis": "sports",
}

# -------------------------
# Request model
# -------------------------
class ChatReq(BaseModel):
    message: str
    user_email: str | None = None   # optional; used only if supabase configured
    prefer_recent: bool | None = True   # if true prefer recent (< DAYS_LIMIT) but not required

# -------------------------
# Utilities: Sheets CSV fetch
# -------------------------
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"):
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
            # normalize keys to lowercase
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        traceback.print_exc()
        return []

# -------------------------
# Date parser (handles ISO with timezone)
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
            return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except Exception:
            return None

# -------------------------
# Sheet search: prefer recent but fallback to any match
# -------------------------
def search_sheet_for_topic(topic: str, rows, prefer_recent=True):
    topic_l = topic.lower().strip()
    today = datetime.utcnow().date()
    recent_matches = []
    any_matches = []

    for r in rows:
        combined = " ".join([
            r.get("headline",""),
            r.get("news",""),
            r.get("categories",""),
            r.get("link",""),
            r.get("image_url",""),
        ]).lower()
        if topic_l in combined:
            any_matches.append(r)
            date_str = r.get("date") or r.get("published") or ""
            d = parse_date_safe(date_str)
            if d:
                if (today - d.date()).days <= DAYS_LIMIT:
                    recent_matches.append(r)
            else:
                # treat rows without parseable date as potential matches (lower priority)
                any_matches.append(r)

    if prefer_recent and recent_matches:
        return recent_matches
    if any_matches:
        return any_matches
    return []

# -------------------------
# RSS search
# -------------------------
def map_keyword_to_category(topic: str):
    topic_l = topic.lower()
    for k, cat in KEYWORD_CATEGORY.items():
        if k in topic_l:
            return cat
    # fallback: check if any category name appears
    for cat in CATEGORY_FEEDS.keys():
        if cat in topic_l:
            return cat
    return None

def search_rss_category_for_topic(category: str, topic: str, max_items=10):
    feeds = CATEGORY_FEEDS.get(category, [])
    found = []
    topic_l = topic.lower().strip()
    for feed_url in feeds:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries[:max_items]:
                title = (entry.get("title") or "").lower()
                summary = (entry.get("summary") or "").lower()
                link = entry.get("link")
                published = None
                try:
                    published = parse_date_safe(entry.get("published") or entry.get("updated") or "")
                except Exception:
                    published = None
                if topic_l in title or topic_l in summary:
                    found.append({
                        "headline": entry.get("title"),
                        "link": link,
                        "summary": entry.get("summary",""),
                        "published": published.isoformat() if published else None
                    })
        except Exception as e:
            print("RSS parse error for", feed_url, e)
    # sort by published desc if available
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found

# -------------------------
# HTML article extractor
# -------------------------
def extract_article_text(url: str, max_chars: int = 12000):
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
            # fallback: gather paragraphs that look long
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
        print("Article extraction error:", e)
        traceback.print_exc()
        return None

# -------------------------
# Gemini summarizer (one article)
# -------------------------
def summarize_article_with_gemini(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are Nova — a concise, professional news reporter.\n"
            "Summarize the article below in 2-4 short paragraphs, focusing on the key facts. "
            "Then provide a short, tailored follow-up suggestion the user might want next (1 sentence).\n\n"
            f"User message: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt, max_output_tokens=512)
        text = getattr(resp, "text", None)
        if not text:
            print("Gemini returned empty response:", resp)
            return "⚠️ Sorry — Gemini returned no summary."
        return text.strip()
    except Exception as e:
        print("Gemini error:", e)
        traceback.print_exc()
        return "⚠️ Sorry — error while generating summary."

# -------------------------
# Supabase chat history helpers (optional)
# -------------------------
def append_chat_history(email: str, conv_name: str, sender: str, text: str, meta: dict | None = None):
    if not supabase:
        return False
    try:
        # ensure row exists
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        existing = None
        if isinstance(res, dict):
            existing = res.get("data")
        else:
            existing = getattr(res, "data", None)
        if not existing:
            supabase.table("users").insert({"email": email, "chat_history": {}}).execute()
        # fetch current history
        r2 = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        hist = {}
        if isinstance(r2, dict):
            hist = r2.get("data", {}).get("chat_history", {}) or {}
        else:
            hist = getattr(r2, "data", {}).get("chat_history", {}) or {}
        if conv_name not in hist:
            hist[conv_name] = []
        entry = {"sender": sender, "text": text, "ts": datetime.utcnow().isoformat() + "Z"}
        if meta:
            entry["meta"] = meta
        hist[conv_name].append(entry)
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("Supabase append error:", e)
        return False

# -------------------------
# Endpoint: /chat
# -------------------------
@app.post("/chat")
def chat(req: ChatReq):
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")

    user_email = (req.user_email or "").strip().lower() or None
    prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)
    topic = re.sub(r"\b(latest|news)\b", "", user_message, flags=re.I).strip()
    if not topic:
        topic = user_message

    # 1) Try Google Sheet (if configured)
    sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
    sheet_matches = []
    if sheet_rows:
        sheet_matches = search_sheet_for_topic(topic, sheet_rows, prefer_recent=prefer_recent)

    articles = []  # list of dicts: {headline, link, source, article_text, published}

    # Use sheet rows first (prefer)
    if sheet_matches:
        for r in sheet_matches[:MAX_RESULTS]:
            headline = r.get("headline") or r.get("title") or topic
            link = r.get("link") or ""
            article_text = r.get("news") or r.get("summary") or ""
            published = r.get("date") or None
            # if link exists and no article_text, try extracting
            if link and not article_text:
                article_text = extract_article_text(link)
            if article_text:
                articles.append({
                    "headline": headline,
                    "link": link,
                    "article_text": article_text,
                    "published": published,
                    "source": "sheet"
                })
            else:
                # if no article text, still include link so user can be informed
                articles.append({
                    "headline": headline,
                    "link": link,
                    "article_text": None,
                    "published": published,
                    "source": "sheet"
                })

    # 2) If still not enough articles, map topic to category and search RSS
    if len(articles) < MAX_RESULTS:
        cat = map_keyword_to_category(topic) or None
        # if no mapping, attempt searching all categories
        categories_to_check = [cat] if cat else list(CATEGORY_FEEDS.keys())
        for c in categories_to_check:
            found = search_rss_category_for_topic(c, topic, max_items=10)
            for item in found:
                if len(articles) >= MAX_RESULTS:
                    break
                link = item.get("link")
                headline = item.get("headline") or topic
                # try to extract article text
                text = extract_article_text(link)
                articles.append({
                    "headline": headline,
                    "link": link,
                    "article_text": text or item.get("summary") or "",
                    "published": item.get("published"),
                    "source": f"rss:{c}"
                })
            if len(articles) >= MAX_RESULTS:
                break

    if not articles:
        return {"reply": f"Sorry — no items found in Google Sheet or RSS for '{topic}'. Try a different query."}

    # 3) Summarize each article with Gemini (only those with article_text)
    summaries = []
    for art in articles[:MAX_RESULTS]:
        if not art.get("article_text"):
            summaries.append({
                "headline": art.get("headline"),
                "link": art.get("link"),
                "summary": f"❗️ No article text available for this link: {art.get('link')}"
            })
            continue
        summary_text = summarize_article_with_gemini(art["article_text"], art.get("headline", topic), user_message)
        summaries.append({
            "headline": art.get("headline"),
            "link": art.get("link"),
            "summary": summary_text,
            "source": art.get("source"),
            "published": art.get("published")
        })

    # 4) Combine reply (presented as multiple blocks)
    combined = []
    for s in summaries:
        block = f"📰 {s.get('headline')}\n\n{s.get('summary')}\n\n🔗 {s.get('link')}"
        combined.append(block)
    combined_reply = "\n\n---\n\n".join(combined)

    # 5) Persist conversation to Supabase if configured & user provided
    conv_name = topic or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    if user_email and supabase:
        try:
            append_chat_history(user_email, conv_name, user_email, user_message, meta={"topic": topic})
            append_chat_history(user_email, conv_name, "nova", combined_reply, meta={"results": len(summaries)})
        except Exception as e:
            print("Supabase store error:", e)

    return {
        "reply": combined_reply,
        "count": len(summaries),
        "conversation": conv_name
    }

# health
@app.get("/health")
def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat() + "Z"}
