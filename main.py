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

# Optional: Supabase client
try:
    from supabase import create_client as create_supabase_client
except Exception:
    create_supabase_client = None

# Optional: Google Sheets append via gspread (service account JSON path required)
try:
    import gspread
    from oauth2client.service_account import ServiceAccountCredentials
except Exception:
    gspread = None
    ServiceAccountCredentials = None

app = FastAPI(title="Nova — n8n-style News + Chat Assistant")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
GOOGLE_SA_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()  # path to JSON file (optional)
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()
CACHE_MAX_AGE_HOURS = int(os.getenv("CACHE_MAX_AGE_HOURS", "24"))  # sheet cache freshness window

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

# Supabase init (optional)
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_supabase_client is not None:
    try:
        supabase = create_supabase_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init failed:", e)
        supabase = None

# Google Sheets client init (optional)
gspread_client = None
if GOOGLE_SA_JSON and gspread is not None and ServiceAccountCredentials is not None:
    try:
        scope = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
        creds = ServiceAccountCredentials.from_json_keyfile_name(GOOGLE_SA_JSON, scope)
        gspread_client = gspread.authorize(creds)
    except Exception as e:
        print("gspread init error:", e)
        gspread_client = None

# ---------------- SYSTEM PROMPT ----------------
SYSTEM_PROMPT = """
You are NewsAssistant, a helpful, accurate, concise news agent. When a user asks for news, follow this flow:

1) IDENTIFY request: extract {topic_query}, {scope}, {timeframe}, {language}. Default => top 5 headlines last 24 hours, 2-sentence summaries.
2) CHECK CHAT HISTORY & PREFS: read Supabase 'user_chats' table chat_history to avoid duplicates and respect preferences.
3) LOOKUP CACHED NEWS in Google Sheets (sheet: news_cache). If fresh, return those first.
4) IF NOT FOUND: query RSS feeds (category-first, then fallback list). Fetch up to N=5, dedupe identical URLs.
5) RANK & FORMAT: newest first. For each produce: headline, 1-2 sentence summary, source, ISO date, url, short 'why this matters' if helpful.
6) WRITE-BACK: append new items to Google Sheets (skip if url exists).
7) RESPONSE RULES: concise, chat-style, include source+url, end with a single friendly question (e.g., "Want me to fetch the full article?").
8) PRIVACY & RATE LIMITS: don't expose credentials or raw feed URLs in replies; do not re-fetch the same feed more than once/minute per user.
"""

# ---------------- RATE LIMITER CONFIG ----------------
# Do not fetch the same feed more than once per user within RATE_LIMIT_SECONDS (in-memory).
RATE_LIMIT_SECONDS = 60
# mapping (user_email, feed_url) -> last_fetch_unix_seconds
FEED_FETCH_TIMES: Dict[Tuple[str, str], float] = {}

# ---------------- RSS and Category feeds (from your n8n blueprint) ----------------
DEFAULT_RSS_FEEDS = [
    # world & general
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://rss.cnn.com/rss/edition.rss",
    "https://www.theguardian.com/world/rss",
    "https://feeds.reuters.com/reuters/topNews",
    # tech
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.feedburner.com/TechCrunch/",
    "https://www.wired.com/feed/rss",
    # space / nasa
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://phys.org/rss-feed/space-news/",
    # business / finance
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    "https://feeds.reuters.com/reuters/businessNews",
]
# allow extra feeds from env var
if EXTRA_RSS:
    for e in EXTRA_RSS.split(","):
        e = e.strip()
        if e and e not in DEFAULT_RSS_FEEDS:
            DEFAULT_RSS_FEEDS.append(e)

CATEGORY_FEEDS = {
    "space": [
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "https://www.space.com/feeds/all",
        "https://phys.org/rss-feed/space-news/",
        "https://www.sciencedaily.com/rss/top/space_rss.xml",
    ],
    "tech": [
        "https://feeds.arstechnica.com/arstechnica/index",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.feedburner.com/TechCrunch/",
        "https://www.wired.com/feed/rss",
    ],
    "business": [
        "https://feeds.bbci.co.uk/news/business/rss.xml",
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "world": [
        "http://feeds.bbci.co.uk/news/rss.xml",
        "http://rss.cnn.com/rss/edition.rss",
        "https://feeds.reuters.com/reuters/topNews",
    ],
}

KEYWORD_CATEGORY = {
    "nasa": "space",
    "space": "space",
    "spacex": "space",
    "jwst": "space",
    "ai": "tech",
    "google": "tech",
    "apple": "tech",
    "tesla": "tech",
    "markets": "business",
    "business": "business",
    "world": "world",
}

# ---------------- MODELS / INPUT ----------------
class ChatReq(BaseModel):
    user_email: Optional[str] = None
    message: str
    conversation_name: Optional[str] = None

# ---------------- SIMPLE SQLITE (local fallback for storing user chats) ----------------
SQLITE_DB = os.getenv("SQLITE_DB", "nova_cache.db")
def init_db():
    conn = sqlite3.connect(SQLITE_DB, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS users (
        email TEXT PRIMARY KEY,
        chat_history TEXT DEFAULT '[]',
        created_at TEXT
    )""")
    conn.commit()
    return conn
DB = init_db()

def get_user_row_sqlite(email: str):
    cur = DB.cursor()
    cur.execute("SELECT email, chat_history FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    if not row:
        return None
    return {"email": row[0], "chat_history": json.loads(row[1] or "[]")}

def ensure_user_row_sqlite(email: str):
    if not email:
        return False
    if get_user_row_sqlite(email) is not None:
        return True
    cur = DB.cursor()
    cur.execute("INSERT INTO users (email, chat_history, created_at) VALUES (?, ?, ?)", (email, "[]", datetime.utcnow().isoformat()+"Z"))
    DB.commit()
    return True

def fetch_sqlite_chat_history(email: str):
    row = get_user_row_sqlite(email)
    if not row:
        return []
    return row.get("chat_history", []) or []

def save_sqlite_chat_history(email: str, hist: List[dict]):
    cur = DB.cursor()
    cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(hist), email))
    DB.commit()
    return True

def append_message_to_conversation(email: str, conv_name: str, sender: str, text: str):
    ensure_user_row_sqlite(email)
    hist = fetch_sqlite_chat_history(email) or []
    idx = -1
    for i, obj in enumerate(hist):
        if isinstance(obj, dict) and conv_name in obj:
            idx = i
            break
    msg = {"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()+"Z"}
    if idx >= 0:
        hist[idx][conv_name].append(msg)
    else:
        hist.append({conv_name: [msg]})
    save_sqlite_chat_history(email, hist)
    return True

def supabase_get_user(email: str):
    if not supabase:
        return None
    try:
        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
        return data
    except Exception as e:
        print("supabase get error:", e)
        return None

def supabase_upsert_user(email: str):
    if not supabase:
        return False
    try:
        hist = fetch_sqlite_chat_history(email)
        supabase.table("users").upsert({"email": email, "chat_history": hist}).execute()
        return True
    except Exception as e:
        print("supabase upsert error:", e)
        return False

# ---------------- Google Sheet helpers (read-only via CSV export; append optional via gspread) ----------------
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0") -> List[dict]:
    """Return list of rows (dict). Works even without GCP creds by using public/csv export or if sheet is shared."""
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
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        # don't crash — return empty
        return []

def append_row_to_sheet(sheet_id: str, values: List[str], sheet_gname: str = None) -> bool:
    """
    Append row using gspread if available and service account path was provided.
    values should be a list matching the sheet columns.
    """
    if not gspread_client or not sheet_id:
        return False
    try:
        sh = gspread_client.open_by_key(sheet_id)
        if sheet_gname:
            ws = sh.worksheet(sheet_gname)
        else:
            ws = sh.get_worksheet(0)
        ws.append_row(values, value_input_option="USER_ENTERED")
        return True
    except Exception as e:
        print("append_row_to_sheet error:", e)
        return False

# ---------------- RSS Search / Extraction ----------------
CATEGORY_PATH_KEYWORDS = ["/tech", "/technology", "/space", "/nasa", "/science", "/business", "/sports", "/entertainment", "/world"]

def search_rss_for_topic(topic: str, user_email: Optional[str] = None, max_items=30, category: Optional[str]=None) -> Tuple[List[dict], List[str]]:
    """Search the configured feeds and return (found_items, feeds_checked). Items are dicts with headline, link, summary, published, source_feed.
       This function enforces per-user per-feed rate limit using FEED_FETCH_TIMES."""
    topic_l = (topic or "").lower().strip()
    found = []
    feeds_checked = []
    feeds_to_check = []
    try:
        if category and category in CATEGORY_FEEDS:
            feeds_to_check += CATEGORY_FEEDS[category]
        # always add global defaults
        for u in DEFAULT_RSS_FEEDS:
            if u not in feeds_to_check:
                feeds_to_check.append(u)
        # track checked feeds
        for feed_url in feeds_to_check:
            feeds_checked.append(feed_url)
            # rate-limit: skip if this user recently fetched this feed
            if user_email:
                key = (user_email, feed_url)
                last = FEED_FETCH_TIMES.get(key)
                now_ts = time.time()
                if last and (now_ts - last) < RATE_LIMIT_SECONDS:
                    # skip fetching this feed to respect rate limit
                    # (we still note it in feeds_checked but do not parse it)
                    continue
                # otherwise mark as fetched now
                FEED_FETCH_TIMES[key] = now_ts
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:max_items]:
                    title = (entry.get("title") or "").lower()
                    summary = (entry.get("summary") or "").lower()
                    link = entry.get("link") or ""
                    tags = " ".join([t.get('term','') for t in entry.get('tags', [])]) if entry.get('tags') else ""
                    link_l = (link or "").lower()
                    match_by_content = (topic_l and (topic_l in title or topic_l in summary or topic_l in tags.lower()))
                    match_by_path = any(k in link_l for k in CATEGORY_PATH_KEYWORDS)
                    if (topic_l and match_by_content) or (not topic_l and match_by_path) or (topic_l and match_by_path):
                        found.append({
                            "headline": entry.get("title"),
                            "link": link,
                            "summary": entry.get("summary", ""),
                            "published": entry.get("published") or entry.get("updated") or None,
                            "source_feed": feed_url
                        })
            except Exception as e:
                print("RSS parse error for", feed_url, e)
    except Exception as e:
        print("search_rss_for_topic error", e)
    # sort by published if possible
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found, feeds_checked

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
            # fallback: pick paragraphs that look long enough
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
        return None

# ---------------- Gemini summarization helpers (SYSTEM_PROMPT prepended) ----------------
def summarize_article_with_gemini(article_text: str, headline: str, user_message: str) -> str:
    # Build the model prompt by prepending the system prompt (workflow) + user context
    prompt = SYSTEM_PROMPT + "\n\n" + (
        "User message: " + (user_message or "") + "\n\n"
        "Headline: " + (headline or "") + "\n\n"
        "Article:\n" + (article_text or "") + "\n\n"
        "Now provide: a 1-2 sentence summary (conversational), source and ISO date, and one friendly follow-up question."
    )
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
        print("genai.Client() call failed:", e)
    try:
        if hasattr(genai, "generate_content"):
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("genai.generate_content() call failed:", e)
    # fallback extractive
    paragraphs = article_text.split("\n\n")
    short = "\n\n".join(paragraphs[:2]).strip()
    return f"(extract) {short}\n\nWant me to open the link for more?"

# ---------------- Topic heuristics ----------------
def extract_topic_from_message(msg: str) -> str:
    if not msg:
        return "news"
    m = re.search(r"(?:about|on|regarding|regarding:|regarding\s)([^?.!]+)", msg, re.I)
    if not m:
        m = re.search(r"(?:news|latest|updates?)\s(?:about\s)?([^?.!]+)", msg, re.I)
    if m:
        topic = m.group(1).strip()
        topic = re.sub(r"\b(today|please|now|for my project|for my|for)\b", "", topic, flags=re.I).strip()
        return topic[:120].strip()
    cleaned = re.sub(r"\b(hey|hi|hello|buddy|bro|please|can i|could you|i want|i'd like|i want to)\b", "", msg, flags=re.I).strip()
    if cleaned and len(cleaned) < 140:
        return cleaned
    return "news"

def map_keyword_to_category(topic: str) -> Optional[str]:
    for k, cat in KEYWORD_CATEGORY.items():
        if k in topic.lower():
            return cat
    for cat in ["space", "tech", "business", "world", "sports", "entertainment"]:
        if cat in topic.lower():
            return cat
    return None

# ---------------- MAIN /chat endpoint implementing n8n flow ----------------
@app.post("/chat")
def chat(req: ChatReq):
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")
    email = (req.user_email or "").strip().lower() or None
    conv_name_in = (req.conversation_name or "").strip() or None

    # prepare conversation name
    if conv_name_in:
        conv_name = conv_name_in
    else:
        # simple short string name generated from the user's message
        base_name = re.sub(r'[^A-Za-z0-9 _-]', '', extract_topic_from_message(user_message)).strip()
        conv_name = base_name or f"chat_{uuid.uuid4().hex[:6]}"
    if email:
        ensure_user_row_sqlite(email)
        append_message_to_conversation(email, conv_name, email, user_message)

    # check greeting
    if re.match(r"^\s*(hi|hello|hey|hiya|yo|sup)\b", user_message, flags=re.I):
        reply = "Hello 👋 how are you? I'm Nova — your news + chat buddy. Ask me for the latest on any topic (e.g., 'news about NASA')."
        if email:
            append_message_to_conversation(email, conv_name, "Nova", reply)
            if supabase:
                supabase_upsert_user(email)
        return {"reply": reply, "conversation": conv_name}

    # decide if user asked for news
    low = user_message.lower()
    news_keywords = ["news", "latest", "update", "updates", "breaking", "what's new", "any news", "tell me about"]
    wants_news = any(k in low for k in news_keywords) or ("about " in low) or ("latest on" in low) or ("latest" in low and len(low) < 60)

    if not wants_news:
        # normal buddy-style response
        reply = f"Nice — {user_message}\n\nI'm here to chat like a buddy. Want me to look up news, summarize something, or save this conversation with a short name?"
        if email:
            append_message_to_conversation(email, conv_name, "Nova", reply)
            if supabase:
                supabase_upsert_user(email)
        return {"reply": reply, "conversation": conv_name}

    # --- User wants news: follow the n8n flow ---
    topic = extract_topic_from_message(user_message)
    inferred_category = map_keyword_to_category(topic)

    # 1) Fetch chat history to avoid duplicates (supabase or sqlite)
    user_hist = fetch_sqlite_chat_history(email) if email else []
    sup_row = supabase_get_user(email) if supabase and email else None
    if sup_row and isinstance(sup_row, dict) and sup_row.get("chat_history"):
        try:
            save_sqlite_chat_history(email, sup_row.get("chat_history"))
            user_hist = sup_row.get("chat_history")
        except Exception:
            pass

    # 2) Check Google Sheets cache for this topic (if configured)
    sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
    cached_matches = []
    cutoff_dt = datetime.utcnow() - timedelta(hours=CACHE_MAX_AGE_HOURS)
    try:
        for r in sheet_rows:
            cat = r.get("category", "")
            title = r.get("title", "")
            summary = r.get("summary", "")
            link = r.get("link", "")
            published = r.get("published_date") or r.get("published") or ""
            fetched = r.get("fetched_at") or ""
            if topic.lower() in (title + " " + summary + " " + cat).lower() or (inferred_category and inferred_category == cat):
                fresh = False
                if fetched:
                    try:
                        fdt = dateparser.parse(fetched)
                        if fdt.tzinfo is None:
                            fdt = fdt.replace(tzinfo=timezone.utc)
                        if fdt >= cutoff_dt:
                            fresh = True
                    except Exception:
                        fresh = True
                else:
                    if published:
                        try:
                            pdt = dateparser.parse(published)
                            if pdt.tzinfo is None:
                                pdt = pdt.replace(tzinfo=timezone.utc)
                            if pdt >= cutoff_dt:
                                fresh = True
                        except Exception:
                            fresh = True
                if fresh:
                    cached_matches.append({"title": title, "summary": summary, "link": link, "source": r.get("source", ""), "published": published})
    except Exception as e:
        print("sheet cache parse err", e)

    # If we have fresh cached results, return up to MAX_RESULTS
    if cached_matches:
        top = cached_matches[:MAX_RESULTS]
        parts = []
        for i, it in enumerate(top, start=1):
            parts.append(f"{i}. {it['title']}\n\n{it['summary']}\n\nSource: {it.get('source') or 'unknown'} | Link: {it.get('link')}\n")
        chatty = f"Hey — I looked in your cached news and found these for *{topic}*:\n\n" + "\n".join(parts) + "\nWould you like the full article text for any item?"
        if email:
            append_message_to_conversation(email, conv_name, "Nova", chatty)
            if supabase:
                supabase_upsert_user(email)
        return {"reply": chatty, "count": len(top), "conversation": conv_name}

    # 3) No fresh cached news -> fetch from RSS (preferring category feeds)
    rss_items, feeds_checked = search_rss_for_topic(topic, user_email=email, max_items=40, category=inferred_category)
    # dedupe by link/title
    seen = set()
    deduped = []
    for it in rss_items:
        link = it.get("link") or ""
        title = it.get("headline") or ""
        key = (link or title).strip()
        if key and key not in seen:
            seen.add(key)
            deduped.append(it)
    # take top MAX_RESULTS
    results = deduped[:MAX_RESULTS]

    # For each found result, try to extract article text then summarize
    summaries = []
    new_rows_for_sheet = []
    for it in results:
        headline = it.get("headline") or ""
        link = it.get("link") or ""
        article_text = extract_article_text(link) or it.get("summary") or ""
        published = it.get("published") or ""
        source_feed = it.get("source_feed") or ""
        if article_text:
            summary_text = summarize_article_with_gemini(article_text, headline, user_message)
        else:
            summary_text = it.get("summary") or "(no extractable text) " + (link or "")
        summaries.append({"headline": headline, "summary": summary_text, "link": link, "published": published, "source": source_feed})
        fetched_at = datetime.utcnow().isoformat()+"Z"
        new_rows_for_sheet.append([email or "anonymous", topic, headline, summary_text[:800], link, published, fetched_at, source_feed])

    # 4) Write-back: append new items to Google Sheet (if available) and update Supabase chat_history
    append_ok = False
    if gspread_client and SHEET_ID and new_rows_for_sheet:
        try:
            for row in new_rows_for_sheet:
                append_row_to_sheet(SHEET_ID, row)
            append_ok = True
        except Exception as e:
            print("sheet append failed:", e)
            append_ok = False

    # Update supabase chat history with the reply text (so we avoid repeating)
    domains = []
    for f in feeds_checked:
        try:
            d = urlparse(f).netloc.replace("www.", "")
            if d and d not in domains:
                domains.append(d)
        except Exception:
            pass
    domains_display = ", ".join(domains[:5]) if domains else "multiple sources"

    # format final friendly reply
    if summaries:
        parts = []
        for i, s in enumerate(summaries, start=1):
            parts.append(f"{i}. {s['headline']}\n\n{s['summary']}\n\nLink: {s['link']}\n")
        combined_reply = f"Hey! I checked {domains_display} and found these for *{topic}*:\n\n" + "\n".join(parts) + "\nDo you want the full article for any item (reply with the number)?"
    else:
        combined_reply = f"Hey — I couldn't find anything fresh about *{topic}* in the feeds I checked. Want me to broaden the search?"

    if email:
        append_message_to_conversation(email, conv_name, "Nova", combined_reply)
        if supabase:
            supabase_upsert_user(email)

    return {"reply": combined_reply, "count": len(summaries), "conversation": conv_name, "sheet_append_ok": append_ok}

# ---------------- simple health ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()+"Z"}
    
