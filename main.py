# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import os, json, csv, io, traceback, requests, re, uuid, sqlite3
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from dateutil import parser as dateparser
from typing import Optional, List, Dict, Any

# optional supabase (if installed)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Friendly News + Chat Assistant")

# ---------------- CONFIG ----------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))
EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()
SQLITE_DB = os.getenv("SQLITE_DB", "nova_cache.db")

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

# configure genai
genai.configure(api_key=GEMINI_API_KEY)

# supabase client if provided
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None

# ---------------- RSS FEEDS (expanded) ----------------
DEFAULT_RSS_FEEDS = [
    # global
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://rss.cnn.com/rss/edition.rss",
    "https://www.theguardian.com/world/rss",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.npr.org/1001/rss.xml",
    # tech
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    # business
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://feeds.reuters.com/reuters/businessNews",
    # space / science
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://www.sciencedaily.com/rss/top/science.xml",
    # entertainment / streaming
    "https://www.hollywoodreporter.com/feed/",
    "https://www.variety.com/rss2.0.xml",
    "https://www.rollingstone.com/music/music-news/feed/",
    "https://www.empireonline.com/feeds/all/rss/",
    # Netflix / streaming specifics
    "https://about.netflix.com/en/newsroom/rss",
    # culture / tv
    "https://www.vulture.com/rss/index.xml",
    "https://ew.com/tv/feed/",
    # sports
    "https://www.espn.com/espn/rss/news",
    # regional
    "https://timesofindia.indiatimes.com/rssfeedstopstories.cms",
    "https://www.aljazeera.com/xml/rss/all.xml",
    # space policy / launches
    "https://www.spacepolicyonline.com/feeds/posts/default",
    "https://www.spaceflightnow.com/launch-feed/",
]

CATEGORY_FEEDS = {
    "space": [
        "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "https://www.space.com/feeds/all",
        "https://www.spaceflightnow.com/launch-feed/",
    ],
    "tech": [
        "https://techcrunch.com/feed/",
        "https://www.theverge.com/rss/index.xml",
        "https://feeds.arstechnica.com/arstechnica/index",
    ],
    "business": [
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
        "https://feeds.reuters.com/reuters/businessNews",
    ],
    "entertainment": [
        "https://www.hollywoodreporter.com/feed/",
        "https://www.variety.com/rss2.0.xml",
        "https://www.rollingstone.com/music/music-news/feed/",
        "https://www.empireonline.com/feeds/all/rss/",
        "https://about.netflix.com/en/newsroom/rss",
        "https://www.vulture.com/rss/index.xml",
        "https://ew.com/tv/feed/",
    ],
    "sports": ["https://www.espn.com/espn/rss/news"],
    "world": [
        "http://feeds.bbci.co.uk/news/rss.xml",
        "https://www.theguardian.com/world/rss",
        "https://www.aljazeera.com/xml/rss/all.xml",
    ],
}

RSS_FEEDS = (EXTRA_RSS.split(",") if EXTRA_RSS else []) + DEFAULT_RSS_FEEDS

# quick keyword -> category
KEYWORD_CATEGORY = {
    "nasa": "space",
    "space": "space",
    "spacex": "space",
    "jwst": "space",
    "comet": "space",
    "moon": "space",
    "red moon": "space",
    "ai": "tech",
    "google": "tech",
    "apple": "tech",
    "markets": "business",
    "economy": "business",
    "covid": "world",
    "cricket": "sports",
    "football": "sports",
    "netflix": "entertainment",
    "stranger things": "entertainment",
    "strangerthings": "entertainment",
}

# ---------------- MODELS / INPUT ----------------
class ChatReq(BaseModel):
    user_email: Optional[str] = None
    message: str
    conversation_name: Optional[str] = None

class GetChatReq(BaseModel):
    email: str
    conversation_name: str

class RenameReq(BaseModel):
    user_email: str
    old_name: str
    new_name: str

class DeleteReq(BaseModel):
    user_email: str
    conversation_name: str

class AppendReq(BaseModel):
    user_email: str
    conversation_name: str
    sender: str
    text: str

# ---------------- SIMPLE SQLITE DB ----------------
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

def append_new_conversation(email: str, conv_name: str, messages: List[dict]):
    ensure_user_row_sqlite(email)
    hist = fetch_sqlite_chat_history(email) or []
    hist.append({conv_name: messages})
    save_sqlite_chat_history(email, hist)
    return True

def find_conversation_index(hist: List[dict], conv_name: str) -> int:
    for i, obj in enumerate(hist):
        if isinstance(obj, dict) and conv_name in obj:
            return i
    return -1

def append_message_to_conversation(email: str, conv_name: str, sender: str, text: str):
    ensure_user_row_sqlite(email)
    hist = fetch_sqlite_chat_history(email) or []
    idx = find_conversation_index(hist, conv_name)
    msg = {"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()+"Z"}
    if idx >= 0:
        hist[idx][conv_name].append(msg)
    else:
        hist.append({conv_name: [msg]})
    save_sqlite_chat_history(email, hist)
    return True

def rename_sqlite_conversation(email: str, old_name: str, new_name: str):
    hist = fetch_sqlite_chat_history(email) or []
    idx = find_conversation_index(hist, old_name)
    if idx < 0:
        return False
    hist[idx] = {new_name: hist[idx][old_name]}
    save_sqlite_chat_history(email, hist)
    return True

def delete_sqlite_conversation(email: str, conv_name: str):
    hist = fetch_sqlite_chat_history(email) or []
    new_hist = [obj for obj in hist if not (isinstance(obj, dict) and conv_name in obj)]
    changed = len(new_hist) < len(hist)
    if changed:
        save_sqlite_chat_history(email, new_hist)
    return changed

# ---------------- UTILITIES: sheets / rss / article extract ----------------
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
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        traceback.print_exc()
        return []

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

def map_keyword_to_category(topic: str):
    for k, cat in KEYWORD_CATEGORY.items():
        if k in topic.lower():
            return cat
    for cat in ["space","tech","business","world","sports","entertainment"]:
        if cat in topic.lower():
            return cat
    return None

def search_rss_for_topic(topic: str, max_items=30, category: Optional[str]=None):
    topic_l = topic.lower().strip()
    found = []
    try:
        feeds_to_check: List[str] = []
        if category and category in CATEGORY_FEEDS:
            feeds_to_check += CATEGORY_FEEDS[category]
        for u in RSS_FEEDS:
            if u not in feeds_to_check:
                feeds_to_check.append(u)
        for feed_url in feeds_to_check:
            try:
                feed = feedparser.parse(feed_url)
                for entry in feed.entries[:max_items]:
                    title = (entry.get("title") or "").lower()
                    summary = (entry.get("summary") or "").lower()
                    link = entry.get("link")
                    tags = " ".join([t.get('term','') for t in entry.get('tags', [])]) if entry.get('tags') else ""
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
    except Exception as e:
        print("search_rss_for_topic error", e)
    # sort by published if possible
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found

def extract_article_text(url: str, max_chars: int = 15000):
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
        print("Article extraction error:", e)
        traceback.print_exc()
        return None

# ---------------- Gemini robust summarizer ----------------
def summarize_article(article_text: str, headline: str, user_message: str):
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
        # try multiple SDK shapes (robust)
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
            if hasattr(genai, "GenerativeModel"):
                model = genai.GenerativeModel(MODEL_NAME)
                for call_args in ( (prompt,), {"prompt": prompt}, {"contents": prompt} ):
                    try:
                        resp = model.generate_content(*call_args) if isinstance(call_args, tuple) else model.generate_content(**call_args)
                        text = getattr(resp, "text", None)
                        if text:
                            return text.strip()
                    except Exception:
                        continue
        except Exception as e:
            print("GenerativeModel() call failed:", e)
        try:
            if hasattr(genai, "generate_content"):
                resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
                text = getattr(resp, "text", None)
                if text:
                    return text.strip()
        except Exception as e:
            print("genai.generate_content() call failed:", e)
        # fallback extractive snippet
        paragraphs = article_text.split("\n\n")
        short = "\n\n".join(paragraphs[:2]).strip()
        fallback = (f"⚠️ Couldn't get a GenAI summary (SDK mismatch or quota). "
                    f"Here is an extract:\n\n{short}\n\nWould you like me to try again or open the original link?")
        return fallback
    except Exception as e:
        print("Gemini summarizer final error:", e)
        traceback.print_exc()
        return "⚠️ Sorry — error while generating summary: Gemini unavailable."

# ---------------- fallback topic summarizer (news not found) ----------------
def summarize_topic_fallback(topic: str, user_message: str):
    prompt = (
        "You are Nova — a friendly, concise AI news assistant.\n"
        "The user asked about this topic and I couldn't find direct articles in feeds.\n"
        "Provide a short chatty but factual summary (2-4 short paragraphs) of what is known about the topic right now, "
        "and one short question or suggestion the user might want next.\n\n"
        f"User message: {user_message}\n\n"
        f"Topic: {topic}\n\n"
        "Summary:"
    )
    try:
        if hasattr(genai, "Client"):
            client = genai.Client()
            resp = client.models.generate_content(model=MODEL_NAME, contents=prompt)
            text = getattr(resp, "text", None)
            if not text and hasattr(resp, "output") and isinstance(resp.output, (list,tuple)) and resp.output:
                text = getattr(resp.output[0], "content", None) or getattr(resp.output[0], "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("fallback genai.Client failed:", e)
    try:
        if hasattr(genai, "GenerativeModel"):
            model = genai.GenerativeModel(MODEL_NAME)
            for call_args in ( (prompt,), {"prompt": prompt}, {"contents": prompt} ):
                try:
                    resp = model.generate_content(*call_args) if isinstance(call_args, tuple) else model.generate_content(**call_args)
                    text = getattr(resp, "text", None)
                    if text:
                        return text.strip()
                except Exception:
                    continue
    except Exception as e:
        print("fallback GenerativeModel failed:", e)
    try:
        if hasattr(genai, "generate_content"):
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("fallback genai.generate_content failed:", e)
    return (f"Hey — I couldn't find direct articles in my feeds, but here's a short summary of *{topic}* "
            "based on general knowledge. I can keep watching and alert you to updates if you'd like.")

# ---------------- intent heuristics ----------------
NEWS_INTENT_KEYWORDS = ["news","update","updates","latest","breaking","any updates","is there any","report","reports","announce","released","release","new","update?"]
GREETINGS_RE = re.compile(r"\b(hi|hello|hey|hiya|greetings|glad to meet|nice to meet|pleased to meet)\b", re.I)

def decide_need_news(user_message: str):
    msg = (user_message or "").strip()
    if not msg:
        return False, None, ""
    # greeting-only and short -> no news
    if len(msg) < 60 and GREETINGS_RE.search(msg) and not any(k in msg.lower() for k in NEWS_INTENT_KEYWORDS):
        return False, None, msg
    low = msg.lower()
    for k in NEWS_INTENT_KEYWORDS:
        if k in low:
            cat = map_keyword_to_category(low)
            return True, cat, msg
    cat = map_keyword_to_category(low)
    if cat:
        return True, cat, msg
    return False, None, msg

# ---------------- MAIN /chat endpoint ----------------
@app.post("/chat")
def chat(req: ChatReq):
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")
    email = (req.user_email or "").strip().lower() or None
    conv_name_in = (req.conversation_name or "").strip() or None

    # decide news intent
    need_news, category, topic_text = decide_need_news(user_message)

    # If conversation name provided -> append to that conversation (create if missing)
    # If no conversation name provided -> create a new conversation for this chat (auto name)
    if email:
        ensure_user_row_sqlite(email)

    # Prepare conversation name
    if conv_name_in:
        conv_name = conv_name_in
    else:
        # auto-generate name from topic if news, else generic chat name
        if need_news:
            # safe conv name: topic + timestamp
            base = re.sub(r"[^\w\s-]", "", topic_text)[:60].strip()
            conv_name = f"{base or 'topic'}_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        else:
            conv_name = f"chat_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"

    # Append user's message to conversation
    if email:
        append_message_to_conversation(email, conv_name, email, user_message)

    # If not news, reply conversationally
    if not need_news:
        # simple help or friendly reply
        low = user_message.lower()
        if any(g in low for g in ("help","suggest","what can you do","how can you")):
            reply = ("Hey 👋 I'm Nova — your friendly assistant. I can fetch news, summarize articles, save conversations, "
                     "rename/delete chats, and set up simple watches. Ask 'news about <topic>' to get news.")
        else:
            reply = f"Hey 👋 I heard: \"{user_message}\" — what would you like me to do? News, summary, or chat?"

        if email:
            append_message_to_conversation(email, conv_name, "Nova", reply)
        return {"reply": reply, "conversation": conv_name}

    # need_news: search sheet + category-prioritized RSS
    sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
    articles = []

    def add_matches_for_topic(t, cat=None):
        nonlocal articles
        # (1) sheet matches
        if sheet_rows:
            try:
                for r in sheet_rows:
                    combined = " ".join([r.get("headline",""), r.get("news",""), r.get("categories",""), r.get("link","")]).lower()
                    if t.lower() in combined:
                        headline = r.get("headline") or r.get("title") or t
                        link = r.get("link") or ""
                        article_text = r.get("news") or r.get("summary") or ""
                        published = r.get("date") or None
                        if link and not article_text:
                            article_text = extract_article_text(link)
                        articles.append({"headline": headline, "link": link, "article_text": article_text, "published": published, "source": "sheet"})
                        if len(articles) >= MAX_RESULTS:
                            return
            except Exception as e:
                print("sheet search err", e)
        # (2) rss (category prioritized)
        if len(articles) < MAX_RESULTS:
            rss_found = search_rss_for_topic(t, max_items=30, category=cat)
            for item in rss_found:
                if len(articles) >= MAX_RESULTS:
                    break
                link = item.get("link")
                headline = item.get("headline") or t
                article_text = extract_article_text(link) or item.get("summary") or ""
                articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

    add_matches_for_topic(topic_text, category)

    # fallback: google news rss
    if not articles:
        try:
            q = requests.utils.quote(topic_text)
            gurl = f"https://news.google.com/rss/search?q={q}"
            feed = feedparser.parse(gurl)
            for entry in feed.entries[:MAX_RESULTS]:
                headline = entry.get("title")
                link = entry.get("link")
                article_text = extract_article_text(link) or entry.get("summary") or ""
                articles.append({"headline": headline, "link": link, "article_text": article_text, "published": entry.get("published"), "source": "googlenews"})
        except Exception:
            pass

    # If still none, produce a generative fallback summary (user should always get a reply)
    if not articles:
        summary_text = summarize_topic_fallback(topic_text, user_message)
        chatty = f"Yo — here's a quick summary about *{topic_text}*:\n\n{summary_text}\n\n— Nova ✌️"
        if email:
            append_message_to_conversation(email, conv_name, "Nova", chatty)
        return {"reply": chatty, "count": 0, "conversation": conv_name}

    # Summarize found articles
    summaries = []
    for art in articles[:MAX_RESULTS]:
        if not art.get("article_text"):
            summaries.append({
                "headline": art.get("headline"),
                "link": art.get("link"),
                "summary": f"❗️ Couldn't extract text from the link. Link: {art.get('link')}",
                "source": art.get("source")
            })
            continue
        summary_text = summarize_article(art["article_text"], art.get("headline", topic_text), user_message)
        summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": summary_text, "source": art.get("source"), "published": art.get("published")})

    # Build chatty reply
    blocks = []
    for i, s in enumerate(summaries, start=1):
        blocks.append(f"{i}. {s.get('headline')}\n\n{s.get('summary')}\n\nLink: {s.get('link')}")
    combined_reply = "\n\n---\n\n".join(blocks)
    suggestions = ("\n\nSuggestions: \n"
                   "• Reply with the article number to dive deeper (e.g. '1').\n"
                   "• Reply 'watch' to create a simple watch/alert for this topic.\n"
                   "• Reply 'more' to search wider sources (Reddit, blogs).\n")
    chatty = f"Yo — here's what I found about *{topic_text}*:\n\n{combined_reply}\n\n{suggestions}\n— Nova ✌️"

    # Append Nova reply to conversation
    if email:
        append_message_to_conversation(email, conv_name, "Nova", chatty)

    return {"reply": chatty, "count": len(summaries), "conversation": conv_name}

# ---------------- get a single conversation ----------------
@app.post("/get_chat")
def get_chat(req: GetChatReq):
    email = (req.email or "").strip().lower()
    conv_name = (req.conversation_name or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="email required")
    if not conv_name:
        raise HTTPException(status_code=400, detail="conversation_name required")
    hist = fetch_sqlite_chat_history(email)
    for obj in hist:
        if isinstance(obj, dict) and conv_name in obj:
            return {conv_name: obj[conv_name]}
    # fallback supabase
    if supabase:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist_s = data.get("chat_history", []) if data else []
            for obj in hist_s:
                if isinstance(obj, dict) and conv_name in obj:
                    return {conv_name: obj[conv_name]}
        except Exception as e:
            print("get_chat supabase error:", e)
    raise HTTPException(status_code=404, detail=f"Conversation '{conv_name}' not found for {email}.")

# ---------------- append explicit ----------------
@app.post("/append_chat")
def append_chat(req: AppendReq):
    email = (req.user_email or "").strip().lower()
    conv = (req.conversation_name or "").strip()
    sender = (req.sender or "").strip()
    text = (req.text or "").strip()
    if not email or not conv or not sender or not text:
        raise HTTPException(status_code=400, detail="email, conversation_name, sender, text required")
    append_message_to_conversation(email, conv, sender, text)
    if supabase:
        try:
            supabase.table("users").upsert({"email": email, "chat_history": fetch_sqlite_chat_history(email)}).execute()
        except Exception as e:
            print("append supabase error:", e)
    return {"ok": True, "message": "Appended message to conversation."}

# ---------------- list chats ----------------
@app.get("/list_chats")
def list_chats(email: Optional[str] = None):
    if not email:
        raise HTTPException(status_code=400, detail="email query param required")
    email = email.strip().lower()
    hist = fetch_sqlite_chat_history(email)
    names = []
    for obj in hist:
        if isinstance(obj, dict):
            names.extend(list(obj.keys()))
    return {"count": len(names), "conversations": names}

# ---------------- rename ----------------
@app.post("/rename_chat")
def rename_chat(req: RenameReq):
    email = (req.user_email or "").strip().lower()
    old = (req.old_name or "").strip()
    new = (req.new_name or "").strip()
    if not email or not old or not new:
        raise HTTPException(status_code=400, detail="user_email, old_name, new_name required")
    renamed = rename_sqlite_conversation(email, old, new)
    if supabase:
        try:
            hist = fetch_sqlite_chat_history(email)
            supabase.table("users").upsert({"email": email, "chat_history": hist}).execute()
        except Exception as e:
            print("rename supabase error:", e)
    if renamed:
        return {"ok": True, "message": f"Conversation renamed from '{old}' to '{new}' for {email}."}
    return {"ok": False, "message": f"Conversation '{old}' not found for {email}."}

# ---------------- delete ----------------
@app.post("/delete_chat")
def delete_chat(req: DeleteReq):
    email = (req.user_email or "").strip().lower()
    conv = (req.conversation_name or "").strip()
    if not email or not conv:
        raise HTTPException(status_code=400, detail="user_email and conversation_name required")
    removed = delete_sqlite_conversation(email, conv)
    if supabase:
        try:
            hist = fetch_sqlite_chat_history(email)
            supabase.table("users").upsert({"email": email, "chat_history": hist}).execute()
        except Exception as e:
            print("delete supabase error:", e)
    if removed:
        return {"ok": True, "message": f"Conversation '{conv}' deleted for {email}."}
    return {"ok": False, "message": f"Conversation '{conv}' not found for {email}."}

# ---------------- health ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()+"Z"}
        
