# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timezone
import os, json, csv, io, traceback, requests, re, uuid
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from dateutil import parser as dateparser

# optional supabase client (if not installed code still runs)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Global News Summarizer (Sheets + RSS + Gemini)")

@app.get("/")
def root():
    return {"message": "hi i am Nova , how can I help you?"}

# --- config via ENV ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SHEET_ID = os.getenv("SHEET_ID", "").strip()
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))
EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

genai.configure(api_key=GEMINI_API_KEY)

# init supabase client if provided
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None

# default RSS feeds (expand as you like)
DEFAULT_RSS_FEEDS = [
    "http://feeds.bbci.co.uk/news/rss.xml",
    "http://rss.cnn.com/rss/edition.rss",
    "https://www.theguardian.com/world/rss",
    "https://feeds.reuters.com/reuters/topNews",
    "https://feeds.npr.org/1001/rss.xml",
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://www.space.com/feeds/all",
    "https://www.sciencedaily.com/rss/top/science.xml",
    "https://techcrunch.com/feed/",
    "https://www.theverge.com/rss/index.xml",
    "https://feeds.arstechnica.com/arstechnica/index",
    "https://feeds.a.dj.com/rss/RSSWorldNews.xml",
    "https://feeds.reuters.com/reuters/businessNews",
    "https://www.spacepolicyonline.com/feeds/posts/default",
    "https://www.spaceflightnow.com/launch-feed/",
]

RSS_FEEDS = DEFAULT_RSS_FEEDS[:]
if EXTRA_RSS:
    RSS_FEEDS = EXTRA_RSS.split(",") + RSS_FEEDS

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
}

# --- request models ---
class ChatReq(BaseModel):
    message: str
    user_email: str | None = None
    prefer_recent: bool | None = True

class GetChatReq(BaseModel):
    user_email: str
    conversation_name: str

# in-memory fallback conversation store
# structure: conversations[email]["convs"][conv_name] = [ {"sender":..,"text":..,"ts":..,"meta":{}} ...]
conversations = {}

# ---------- Helpers: sheets ----------
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

# ---------- date helper ----------
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

# ---------- sheet search ----------
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
        d = parse_date_safe(r.get("date","") or r.get("published","") or "")
        if d and (today - d.date()).days <= DAYS_LIMIT:
            recent_matches.append(r)
    if prefer_recent and recent_matches:
        return recent_matches
    return any_matches

# ---------- rss search ----------
def map_keyword_to_category(topic: str):
    for k, cat in KEYWORD_CATEGORY.items():
        if k in topic.lower():
            return cat
    for cat in ["space","tech","business","world","sports"]:
        if cat in topic.lower():
            return cat
    return None

def search_rss_for_topic(topic: str, max_items=20):
    topic_l = topic.lower().strip()
    found = []
    try:
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

    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found

# ---------- article extraction ----------
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

# ---------- Gemini summarizer ----------
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
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt, max_output_tokens=700)
        text = getattr(resp, "text", None)
        if not text:
            print("Gemini returned empty response:", resp)
            return "⚠️ Sorry — no summary available from Gemini."
        return text.strip()
    except Exception as e:
        print("Gemini error:", e)
        traceback.print_exc()
        return f"⚠️ Sorry — error while generating summary: {e}"

# ---------- local conversation persistence ----------
def save_local_conversation(email: str, conv_name: str, sender: str, text: str, meta: dict | None = None):
    if not email:
        return
    if email not in conversations:
        conversations[email] = {"last_conv": conv_name, "convs": {conv_name: []}}
    if conv_name not in conversations[email]["convs"]:
        conversations[email]["convs"][conv_name] = []
    conversations[email]["convs"][conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()+"Z", "meta": meta or {}})
    conversations[email]["last_conv"] = conv_name

# ---------- Supabase helpers (JSONB array) ----------
def get_user_row(email: str):
    """Return user's row dict or None"""
    if not supabase or not email:
        return None
    try:
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)
        if not data:
            return None
        # res.data might be a list or single object depending on client wrapper
        if isinstance(data, list):
            if len(data) == 0:
                return None
            return data[0]
        return data
    except Exception as e:
        print("Supabase get_user_row error:", e)
        return None

def ensure_user_row(email: str):
    """Ensure a user row exists; create if missing. Return True/False."""
    if not supabase or not email:
        return False
    try:
        if not get_user_row(email):
            supabase.table("users").insert({"email": email, "chat_history": []}).execute()
        return True
    except Exception as e:
        print("ensure_user_row error:", e)
        return False

def fetch_supabase_chat_history(email: str):
    """
    Return chat_history as Python list (JSONB array) or [].
    Expected shape: [ { "conv_name": { ... } }, ... ]
    """
    row = get_user_row(email)
    if not row:
        return []
    hist = row.get("chat_history", []) or []
    # ensure list
    if isinstance(hist, dict):
        # convert dict to list of single item for backward compatibility
        return [hist]
    if not isinstance(hist, list):
        return []
    return hist

def save_supabase_chat_history_append(email: str, conv_obj):
    """
    Append conv_obj (e.g. { conv_name: {user:..., 'Nova': ...} }) to chat_history array.
    Replaces entire chat_history column with new array (supabase client handles JSON->jsonb).
    """
    if not supabase or not email:
        return False
    try:
        ensure_user_row(email)
        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
        hist = data.get("chat_history", []) if data else []
        if not isinstance(hist, list):
            hist = []
        hist.append(conv_obj)
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("Supabase save error:", e)
        return False

def update_supabase_conversation(email: str, conv_name: str, add_pairs: dict):
    """
    Update a conversation inside chat_history array by name.
    - If conversation exists, merge append new pairs in order (user->Nova->user->Nova...).
    - If not exists, append a new conversation object.
    add_pairs example: {"surya@example.com": "new message", "Nova": "reply"}
    Returns True/False.
    """
    if not supabase or not email:
        return False
    try:
        ensure_user_row(email)
        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
        hist = data.get("chat_history", []) if data else []
        if not isinstance(hist, list):
            hist = []
        # find index
        found = False
        for i, obj in enumerate(hist):
            if isinstance(obj, dict) and conv_name in obj:
                # merge by appending keys in order; keep existing as dict but preserve insertion order by creating new dict
                existing = obj.get(conv_name, {}) or {}
                merged = existing.copy()
                merged.update(add_pairs)
                hist[i] = {conv_name: merged}
                found = True
                break
        if not found:
            hist.append({conv_name: add_pairs})
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("update_supabase_conversation error:", e)
        return False

# ---------- utilities ----------
def is_affirmative_reply(text: str):
    t = text.strip().lower()
    return t in {"yes","y","yeah","yep","sure","absolutely","ok","okay","please","tell me more","more","continue"}

def parse_preferences_from_history(hist_list):
    """
    Return prioritized topic keywords from last N items in chat_history JSONB array.
    hist_list expected as described above.
    """
    if not hist_list:
        return []
    topics = []
    for item in reversed(hist_list[-5:]):
        if not isinstance(item, dict):
            continue
        for conv_name, conv_body in item.items():
            if not isinstance(conv_body, dict):
                continue
            for k, v in conv_body.items():
                if k.lower() == "nova":
                    continue
                if isinstance(v, str):
                    text = v.lower()
                    for kw in KEYWORD_CATEGORY.keys():
                        if kw in text and kw not in topics:
                            topics.append(kw)
                    if "moon" in text and "moon" not in topics:
                        topics.append("moon")
            # also try conv_name text for keywords
            for kw in KEYWORD_CATEGORY.keys():
                if kw in conv_name.lower() and kw not in topics:
                    topics.append(kw)
    return topics

# ---------- main chat endpoint ----------
@app.post("/chat")
def chat(req: ChatReq):
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")
    email = (req.user_email or "").strip().lower() or None
    prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)

    topic = re.sub(r"\b(latest|news|give me|show me|tell me|what's|any)\b", "", user_message, flags=re.I).strip()
    if not topic:
        topic = user_message.strip()

    # if user is answering yes to followup, attempt to continue last conv
    if email and is_affirmative_reply(user_message) and ((email in conversations and conversations[email].get("last_conv")) or supabase):
        last_conv = conversations.get(email, {}).get("last_conv") if email in conversations else None
        if not last_conv and supabase:
            # try to fetch last entry from supabase chat_history
            hist = fetch_supabase_chat_history(email)
            if hist:
                last_conv = list(hist[-1].keys())[0] if isinstance(hist[-1], dict) else None
        if last_conv:
            # attempt to find a link in local or supabase saved meta (best-effort)
            last_link = None
            headline = None
            msgs = conversations.get(email, {}).get("convs", {}).get(last_conv, []) if email in conversations else []
            for m in reversed(msgs):
                meta = m.get("meta") or {}
                if meta.get("link"):
                    last_link = meta.get("link")
                    headline = meta.get("headline")
                    break
            if not last_link and supabase:
                # look inside supabase's last conversation object for any 'meta' or 'link' keys
                hist = fetch_supabase_chat_history(email)
                if hist:
                    last_obj = hist[-1] if isinstance(hist[-1], dict) else {}
                    body = list(last_obj.values())[0] if last_obj else {}
                    last_link = body.get("meta_link") or body.get("link")
                    headline = body.get("headline")
            if not last_link:
                return {"reply": "I couldn't find the previous article link to continue. Send a direct link or ask about a topic."}
            article_text = extract_article_text(last_link)
            if not article_text:
                return {"reply": f"Couldn't fetch more details from {last_link}. Here's the link: {last_link}"}
            deeper_prompt = ("You are Nova — now give a richer, deeper explanation about the article, "
                             "covering context, significance, and comparisons (if applicable). Keep it clear and factual.")
            combined_text = deeper_prompt + "\n\n" + article_text
            deep_summary = summarize_article(combined_text, headline or topic, user_message)
            # save
            conv_name = last_conv
            if email:
                save_local_conversation(email, conv_name, "nova", deep_summary, meta={"link": last_link, "headline": headline})
                if supabase:
                    try:
                        update_supabase_conversation(email, conv_name, {"Nova": deep_summary})
                    except Exception:
                        pass
            return {"reply": deep_summary, "link": last_link}

    # build prioritized topics from message and history
    history_list = fetch_supabase_chat_history(email) if email else []
    history_topics = parse_preferences_from_history(history_list)

    prioritized = []
    explicit = topic.lower()
    if explicit and explicit not in ("latest", "news"):
        prioritized.append(explicit)
    for ht in history_topics:
        if ht not in prioritized:
            prioritized.append(ht)
    if not prioritized:
        prioritized = [topic]

    sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
    articles = []

    def add_matches_for_topic(t):
        nonlocal articles
        if sheet_rows:
           sheet_matches = search_sheet_for_topic(t, sheet_rows, prefer_recent=prefer_recent)
            for r in sheet_matches:
                if len(articles) >= MAX_RESULTS:
                    return
                headline = r.get("headline") or r.get("title") or t
                link = r.get("link") or ""
                article_text = r.get("news") or r.get("summary") or ""
                published = r.get("date") or None
                if link and not article_text:
                    article_text = extract_article_text(link)
                articles.append({"headline": headline, "link": link, "article_text": article_text, "published": published, "source": "sheet"})
        if len(articles) < MAX_RESULTS:
            rss_found = search_rss_for_topic(t, max_items=20)
            for item in rss_found:
                if len(articles) >= MAX_RESULTS:
                    break
                link = item.get("link")
                headline = item.get("headline") or t
                article_text = extract_article_text(link) or item.get("summary") or ""
                articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

    for t in prioritized:
        if len(articles) >= MAX_RESULTS:
            break
        add_matches_for_topic(t)

    if not articles:
        rss_found = search_rss_for_topic(topic, max_items=30)
        for item in rss_found[:MAX_RESULTS]:
            link = item.get("link")
            headline = item.get("headline") or topic
            article_text = extract_article_text(link) or item.get("summary") or ""
            articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

    if not articles:
        return {"reply": f"Sorry — I couldn't find articles for '{topic}' in the sheet or RSS feeds. Try a different query or provide a link."}

    summaries = []
    for art in articles[:MAX_RESULTS]:
        if not art.get("article_text"):
            summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": f"❗️ No extractable text found at {art.get('link')}.", "source": art.get("source")})
            continue
        summary_text = summarize_article(art["article_text"], art.get("headline", topic), user_message)
        summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": summary_text, "source": art.get("source"), "published": art.get("published")})

    blocks = []
    for i, s in enumerate(summaries, start=1):
        block = f"{i}. {s.get('headline')}\n\n{s.get('summary')}\n\nLink: {s.get('link')}"
        blocks.append(block)
    combined_reply = "\n\n---\n\n".join(blocks)
    followup_hint = "\n\nWould you like more detail on any of these (reply 'yes' or the number)?"
    conv_name = f"nova_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"

    # Save local + supabase
    if email:
        save_local_conversation(email, conv_name, email, user_message, meta={"topic": topic})
        save_local_conversation(email, conv_name, "nova", combined_reply + followup_hint, meta={"results": len(summaries)})
    if email and supabase:
        try:
            user_key = email
            conv_obj = {conv_name: {user_key: user_message, "Nova": combined_reply + followup_hint}}
            save_supabase_chat_history_append(email, conv_obj)
        except Exception as e:
            print("Failed to append to supabase chat_history:", e)

    return {"reply": combined_reply + followup_hint, "count": len(summaries), "conversation": conv_name}

# ---------- new endpoint: get single conversation ----------
@app.post("/get_chat")
def get_chat(req: GetChatReq):
    email = (req.user_email or "").strip().lower()
    conv_name = (req.conversation_name or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="user_email required")
    if not conv_name:
        raise HTTPException(status_code=400, detail="conversation_name required")

    # 1) Try supabase
    if supabase:
        try:
            hist = fetch_supabase_chat_history(email)
            # hist is list of objects like [{conv_name: {...}}, ...]
            for obj in hist:
                if isinstance(obj, dict) and conv_name in obj:
                    return {"found": True, "conversation": {conv_name: obj[conv_name]}}
            return {"found": False, "conversation": None, "message": f"Conversation '{conv_name}' not found for {email}."}
        except Exception as e:
            print("get_chat supabase error:", e)
            # fall back to local

    # 2) Fallback to in-memory store
    if email in conversations:
        convs = conversations[email].get("convs", {})
        if conv_name in convs:
            return {"found": True, "conversation": {conv_name: convs[conv_name]}}
        # sometimes convs are stored as single list of {sender,text,...} (older shape)
        return {"found": False, "conversation": None, "message": f"Conversation '{conv_name}' not found in local store for {email}."}

    return {"found": False, "conversation": None, "message": "No conversation history found for this email."}

# ---------- optional: endpoint to append a message to a conversation explicitly ----------
class AppendReq(BaseModel):
    user_email: str
    conversation_name: str
    sender: str
    text: str

@app.post("/append_chat")
def append_chat(req: AppendReq):
    email = (req.user_email or "").strip().lower()
    conv = (req.conversation_name or "").strip()
    sender = (req.sender or "").strip()
    text = (req.text or "").strip()
    if not email or not conv or not sender or not text:
        raise HTTPException(status_code=400, detail="email, conversation_name, sender, text required")

    ts = datetime.utcnow().isoformat() + "Z"
    # local save
    save_local_conversation(email, conv, sender, text, meta={})
    # supabase update
    if supabase:
        try:
            # create a small dict: { sender: text } and merge into conv
            update_supabase_conversation(email, conv, {sender: text})
        except Exception as e:
            print("append_chat supabase error:", e)
    return {"ok": True, "message": "Appended message to conversation."}

# End of file
