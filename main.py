from fastapi import FastAPI, HTTPException from pydantic import BaseModel from datetime import datetime, timezone import os, json, csv, io, traceback, requests, re, uuid, sqlite3 from bs4 import BeautifulSoup import feedparser import google.generativeai as genai from dateutil import parser as dateparser from typing import Optional

Optional supabase client if present

try: from supabase import create_client except Exception: create_client = None

app = FastAPI(title="Nova — Friendly News + Chat Assistant")

---------------- CONFIG ----------------

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY") MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash") SHEET_ID = os.getenv("SHEET_ID", "").strip() SHEET_GID = os.getenv("SHEET_GID", "0").strip() SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip() SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip() MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3")) DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2")) EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip() SQLITE_DB = os.getenv("SQLITE_DB", "nova_cache.db")

if not GEMINI_API_KEY: raise RuntimeError("Please set GEMINI_API_KEY in environment variables.")

configure genai

genai.configure(api_key=GEMINI_API_KEY)

init supabase if provided

supabase = None if SUPABASE_URL and SUPABASE_KEY and create_client is not None: try: supabase = create_client(SUPABASE_URL, SUPABASE_KEY) except Exception as e: print("Supabase init error:", e) supabase = None

---------------- RSS FEEDS (expanded) ----------------

DEFAULT_RSS_FEEDS = [ # global "http://feeds.bbci.co.uk/news/rss.xml", "http://rss.cnn.com/rss/edition.rss", "https://www.theguardian.com/world/rss", "https://feeds.reuters.com/reuters/topNews", "https://feeds.npr.org/1001/rss.xml", # tech "https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml", "https://feeds.arstechnica.com/arstechnica/index", # business "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "https://feeds.reuters.com/reuters/businessNews", # space / science "https://www.nasa.gov/rss/dyn/breaking_news.rss", "https://www.space.com/feeds/all", "https://www.sciencedaily.com/rss/top/science.xml", # entertainment / streaming "https://www.hollywoodreporter.com/feed/", "https://www.variety.com/rss2.0.xml", "https://www.rollingstone.com/music/music-news/feed/", "https://www.empireonline.com/feeds/all/rss/", # Netflix specific and entertainment news "https://about.netflix.com/en/newsroom/rss", "https://www.netflix.com/browse/genre/rss", # culture / tv "https://www.vulture.com/rss/index.xml", "https://ew.com/tv/feed/", # sports "https://www.espn.com/espn/rss/news", # regional "https://timesofindia.indiatimes.com/rssfeedstopstories.cms", "https://www.aljazeera.com/xml/rss/all.xml", # space policy / launches "https://www.spacepolicyonline.com/feeds/posts/default", "https://www.spaceflightnow.com/launch-feed/", ]

category -> extra feeds map (priority)

CATEGORY_FEEDS = { "space": [ "https://www.nasa.gov/rss/dyn/breaking_news.rss", "https://www.space.com/feeds/all", "https://www.spaceflightnow.com/launch-feed/", ], "tech": [ "https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml", "https://feeds.arstechnica.com/arstechnica/index", ], "business": [ "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "https://feeds.reuters.com/reuters/businessNews", ], "entertainment": [ "https://www.hollywoodreporter.com/feed/", "https://www.variety.com/rss2.0.xml", "https://www.rollingstone.com/music/music-news/feed/", "https://www.empireonline.com/feeds/all/rss/", "https://about.netflix.com/en/newsroom/rss", "https://www.vulture.com/rss/index.xml", "https://ew.com/tv/feed/", ], "sports": [ "https://www.espn.com/espn/rss/news", ], "world": [ "http://feeds.bbci.co.uk/news/rss.xml", "https://www.theguardian.com/world/rss", "https://www.aljazeera.com/xml/rss/all.xml", ] }

RSS_FEEDS = (EXTRA_RSS.split(",") if EXTRA_RSS else []) + DEFAULT_RSS_FEEDS

quick keyword -> category

KEYWORD_CATEGORY = { "nasa": "space", "space": "space", "spacex": "space", "jwst": "space", "comet": "space", "moon": "space", "red moon": "space", "ai": "tech", "google": "tech", "apple": "tech", "markets": "business", "economy": "business", "covid": "world", "cricket": "sports", "football": "sports", "netflix": "entertainment", "stranger things": "entertainment", "strangerthings": "entertainment", }

---------------- MODELS / INPUT ----------------

class ChatReq(BaseModel): message: str user_email: Optional[str] = None prefer_recent: Optional[bool] = True

class GetChatReq(BaseModel): user_email: str conversation_name: str

class AppendReq(BaseModel): user_email: str conversation_name: str sender: str text: str

---------------- SIMPLE SQLITE DB ----------------

def init_db(): conn = sqlite3.connect(SQLITE_DB, check_same_thread=False) cur = conn.cursor() cur.execute("""CREATE TABLE IF NOT EXISTS users ( email TEXT PRIMARY KEY, chat_history TEXT DEFAULT '[]', created_at TEXT )""") conn.commit() return conn

DB = init_db()

def get_user_row_sqlite(email: str): cur = DB.cursor() cur.execute("SELECT email, chat_history FROM users WHERE email = ?", (email,)) row = cur.fetchone() if not row: return None return {"email": row[0], "chat_history": json.loads(row[1] or "[]")}

def ensure_user_row_sqlite(email: str): if not email: return False if get_user_row_sqlite(email) is not None: return True cur = DB.cursor() cur.execute("INSERT INTO users (email, chat_history, created_at) VALUES (?, ?, ?)", (email, "[]", datetime.utcnow().isoformat()+"Z")) DB.commit() return True

def fetch_sqlite_chat_history(email: str): row = get_user_row_sqlite(email) if not row: return [] return row.get("chat_history", []) or []

def save_sqlite_chat_history_append(email: str, conv_obj): ensure_user_row_sqlite(email) hist = fetch_sqlite_chat_history(email) or [] hist.append(conv_obj) cur = DB.cursor() cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(hist), email)) DB.commit() return True

def update_sqlite_conversation(email: str, conv_name: str, add_pairs: dict): ensure_user_row_sqlite(email) hist = fetch_sqlite_chat_history(email) or [] found = False for i, obj in enumerate(hist): if isinstance(obj, dict) and conv_name in obj: existing = obj.get(conv_name) or {} merged = existing.copy() merged.update(add_pairs) hist[i] = {conv_name: merged} found = True break if not found: hist.append({conv_name: add_pairs}) cur = DB.cursor() cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(hist), email)) DB.commit() return True

---------------- UTILITIES ----------------

def sheet_csv_url(sheet_id: str, gid: str = "0"): return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"): if not sheet_id: return [] try: url = sheet_csv_url(sheet_id, gid) r = requests.get(url, timeout=12) r.raise_for_status() text = r.content.decode("utf-8") reader = csv.DictReader(io.StringIO(text)) rows = [] for row in reader: normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() } rows.append(normalized) return rows except Exception as e: print("Sheet fetch error:", e) traceback.print_exc() return []

def parse_date_safe(date_str: str): if not date_str: return None try: dt = dateparser.parse(date_str) if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc) return dt.astimezone(timezone.utc) except Exception: try: return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc) except Exception: return None

def map_keyword_to_category(topic: str): for k, cat in KEYWORD_CATEGORY.items(): if k in topic.lower(): return cat for cat in ["space","tech","business","world","sports","entertainment"]: if cat in topic.lower(): return cat return None

def search_rss_for_topic(topic: str, max_items=30, category: Optional[str]=None): topic_l = topic.lower().strip() found = [] try: # build prioritized feed list: category-specific first, then global list feeds_to_check = [] if category and category in CATEGORY_FEEDS: feeds_to_check += CATEGORY_FEEDS[category] # add all feeds but avoid duplicates for u in RSS_FEEDS: if u not in feeds_to_check: feeds_to_check.append(u)

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
            # don't crash if one feed is bad
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

def extract_article_text(url: str, max_chars: int = 15000): if not url: return None try: headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"} r = requests.get(url, timeout=12, headers=headers) r.raise_for_status() soup = BeautifulSoup(r.text, "html.parser") article = soup.find("article") texts = [] if article: for p in article.find_all("p"): t = p.get_text(strip=True) if t: texts.append(t) else: for p in soup.find_all("p"): t = p.get_text(strip=True) if t and len(t) > 40: texts.append(t) content = "

".join(texts).strip() if not content: meta = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"}) if meta and meta.get("content"): content = meta.get("content") if not content: return None if len(content) > max_chars: content = content[:max_chars].rsplit(".", 1)[0] + "." return content except Exception as e: print("Article extraction error:", e) traceback.print_exc() return None

---------------- Gemini robust summarizer (no unsupported kwargs) ----------------

def summarize_article(article_text: str, headline: str, user_message: str): try: prompt = ( "You are Nova — a friendly, concise AI news reporter. " "Summarize the article below in 2-4 short paragraphs with clear facts. " "Then produce one short tailored follow-up question the user might want next (1 sentence).

" f"User message: {user_message}

" f"Headline: {headline}

" f"Article: {article_text}

" "Summary:" )

# 1) try Client() style
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

    # 2) older GenerativeModel style
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

    # 3) fallback: genai.generate_content function
    try:
        if hasattr(genai, "generate_content"):
            resp = genai.generate_content(model=MODEL_NAME, prompt=prompt)
            text = getattr(resp, "text", None)
            if text:
                return text.strip()
    except Exception as e:
        print("genai.generate_content() call failed:", e)

    # final fallback: short extractive summary (first 2 paragraphs)
    paragraphs = article_text.split("

") short = "

".join(paragraphs[:2]).strip() fallback = (f"⚠️ Couldn't get a GenAI summary (SDK mismatch or quota). " f"Here is an extract:

{short}

Would you like me to try again or open the original link?") return fallback except Exception as e: print("Gemini summarizer final error:", e) traceback.print_exc() return "⚠️ Sorry — error while generating summary: Gemini unavailable or SDK mismatch."

---------------- Conversation helpers ----------------

def is_affirmative_reply(text: str): t = (text or "").strip().lower() return t in {"yes","y","yeah","yep","sure","absolutely","ok","okay","please","tell me more","more","continue"}

decide if message requires news fetch or just chat

NEWS_INTENT_KEYWORDS = ["news","update","updates","latest","breaking","any updates","is there any","report","reports","announce","released","release","new"] GREETINGS_RE = re.compile(r"(hi|hello|hey|hiya|greetings|glad to meet|nice to meet|pleased to meet)", re.I)

def decide_need_news(user_message: str): """Return (need_news: bool, category: Optional[str], topic_text: str) Heuristics: - If greeting-only (matches GREETINGS_RE and short) => no news - If contains explicit news intent keywords => yes - If message mentions a keyword mapped to category => yes, use that category - Otherwise default to chat (no news) but offer to search if user asks """ msg = (user_message or "").strip() if not msg: return False, None, "" # if short greeting and likely not asking anything if len(msg) < 60 and GREETINGS_RE.search(msg) and not any(k in msg.lower() for k in NEWS_INTENT_KEYWORDS): return False, None, msg low = msg.lower() # if explicit news intent for k in NEWS_INTENT_KEYWORDS: if k in low: cat = map_keyword_to_category(low) return True, cat, msg # if mentions known keyword (e.g., 'stranger things') cat = map_keyword_to_category(low) if cat: return True, cat, msg # otherwise, default to chat return False, None, msg

---------------- MAIN /chat endpoint ----------------

@app.post("/chat") def chat(req: ChatReq): user_message = (req.message or "").strip() if not user_message: raise HTTPException(status_code=400, detail="message required") email = (req.user_email or "").strip().lower() or None prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)

need_news, category, topic = decide_need_news(user_message)

# fetch history for personalization (sqlite primary, supabase fallback)
history_list = fetch_sqlite_chat_history(email) if email else []
if not history_list and supabase and email:
    try:
        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
        hist = data.get("chat_history", []) if data else []
        if isinstance(hist, list):
            history_list = hist
    except Exception:
        pass

# If no news needed -> chatbot reply (use Gemini only for conversational replies if desired)
if not need_news:
    reply = None
    # simple conversational handling: if user says 'help' or asks for suggestions, we respond
    low = user_message.lower()
    if any(g in low for g in ("help","suggest","what can you do","how can you")):
        reply = ("Hey 👋 I'm Nova — your friendly news buddy and assistant. I can:

" "• fetch the latest news on topics you ask about " "• summarize articles " "• store and show past conversations " "• suggest related topics and alerts

" "Ask me something or say 'news about <topic>' to get started! 🚀") else: # default friendly echo + suggestion reply = f"Hey 👋 nice to meet you! I heard: "{user_message}". What's on your mind — want news, suggestions, or a quick chat?"

# save conversation stub
    if email:
        conv_name = f"chat_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        save_sqlite_chat_history_append(email, {conv_name: {email: user_message, "Nova": reply}})
        if supabase:
            try:
                supabase.table("users").upsert({"email": email, "chat_history": fetch_sqlite_chat_history(email)}).execute()
            except Exception:
                pass
    return {"reply": reply, "conversation": conv_name if email else None}

# need_news == True: proceed to fetch news using category-aware feeds
# build prioritized topic and feeds
topic_text = topic
cat = category

# try sheet first
sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
articles = []

def add_matches_for_topic(t, cat=None):
    nonlocal articles
    # sheet
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
    # rss (category prioritized)
    if len(articles) < MAX_RESULTS:
        rss_found = search_rss_for_topic(t, max_items=30, category=cat)
        for item in rss_found:
            if len(articles) >= MAX_RESULTS:
                break
            link = item.get("link")
            headline = item.get("headline") or t
            article_text = extract_article_text(link) or item.get("summary") or ""
            articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

add_matches_for_topic(topic_text, cat)

# fallback: broad google news RSS search
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

if not articles:
    reply = (f"Hey 👋 I couldn't find recent news about *{topic_text}* in my feeds right now. "
             "Would you like me to search wider sources (Reddit, blogs) or set a watch/alert?")
    # save
    if email:
        conv_name = f"nova_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
        save_sqlite_chat_history_append(email, {conv_name: {email: user_message, "Nova": reply}})
    return {"reply": reply, "count": 0}

# summarize found articles
summaries = []
for art in articles[:MAX_RESULTS]:
    if not art.get("article_text"):
        summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": f"❗️ Couldn't extract text from the link. Link: {art.get('link')}", "source": art.get("source")})
        continue
    summary_text = summarize_article(art["article_text"], art.get("headline", topic_text), user_message)
    summaries.append({"headline": art.get("headline": art.get("headline"), "link": art.get("link"), "summary": f"❗️ Couldn't extract text from the link. Link: {art.get('link')}", "source": art.get("source")})
            continue
        summary_text = summarize_article(art["article_text"], art.get("headline", topic_text), user_message)
        summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": summary_text, "source": art.get("source"), "published": art.get("published")})

    # build chatty reply
    blocks = []
    for i, s in enumerate(summaries, start=1):
        blocks.append(f"{i}. {s.get('headline')}

{s.get('summary')}

Link: {s.get('link')}")
    combined_reply = "

---

".join(blocks)
    suggestions = ("

Suggestions: 
"
                   "• Reply with the article number to dive deeper (e.g. '1').
"
                   "• Reply 'watch' to create a simple watch/alert for this topic.
"
                   "• Reply 'more' to search wider sources (Reddit, blogs).
")
    chatty = f"Yo — here's what I found about *{topic_text}*:

{combined_reply}

{suggestions}
— Nova ✌️"

    # save conversation
    conv_name = f"nova_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"
    if email:
        save_sqlite_chat_history_append(email, {conv_name: {email: user_message, "Nova": chatty}})
        if supabase:
            try:
                supabase.table("users").upsert({"email": email, "chat_history": fetch_sqlite_chat_history(email)}).execute()
            except Exception:
                pass

    return {"reply": chatty, "count": len(summaries), "conversation": conv_name}

# ---------------- get a single conversation ----------------
@app.post("/get_chat")
def get_chat(req: GetChatReq):
    email = (req.user_email or "").strip().lower()
    conv_name = (req.conversation_name or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="user_email required")
    if not conv_name:
        raise HTTPException(status_code=400, detail="conversation_name required")

    # try sqlite
    hist = fetch_sqlite_chat_history(email)
    for obj in hist:
        if isinstance(obj, dict) and conv_name in obj:
            return {"found": True, "conversation": {conv_name: obj[conv_name]}}

    # fallback to supabase if configured
    if supabase:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist_s = data.get("chat_history", []) if data else []
            for obj in hist_s:
                if isinstance(obj, dict) and conv_name in obj:
                    return {"found": True, "conversation": {conv_name: obj[conv_name]}}
        except Exception as e:
            print("get_chat supabase error:", e)

    return {"found": False, "conversation": None, "message": f"Conversation '{conv_name}' not found for {email}."}

# ---------------- append to conversation explicitly ----------------
@app.post("/append_chat")
def append_chat(req: AppendReq):
    email = (req.user_email or "").strip().lower()
    conv = (req.conversation_name or "").strip()
    sender = (req.sender or "").strip()
    text = (req.text or "").strip()
    if not email or not conv or not sender or not text:
        raise HTTPException(status_code=400, detail="email, conversation_name, sender, text required")

    update_sqlite_conversation(email, conv, {sender: text})
    if supabase:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist = data.get("chat_history", []) if data else []
            sqlite_hist = fetch_sqlite_chat_history(email)
            merged = sqlite_hist if sqlite_hist else hist
            supabase.table("users").upsert({"email": email, "chat_history": merged}).execute()
        except Exception as e:
            print("append_chat supabase error:", e)
    return {"ok": True, "message": "Appended message to conversation."}

# ---------------- list conversation names for an email ----------------
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

# ---------------- delete / rename helpers & endpoints ----------------
def delete_sqlite_conversation(email: str, conv_name: str):
    hist = fetch_sqlite_chat_history(email) or []
    new_hist = []
    removed = False
    for obj in hist:
        if isinstance(obj, dict) and conv_name in obj:
            removed = True
            continue
        new_hist.append(obj)
    cur = DB.cursor()
    cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(new_hist), email))
    DB.commit()
    return removed


def rename_sqlite_conversation(email: str, old_name: str, new_name: str):
    hist = fetch_sqlite_chat_history(email) or []
    found = False
    for i, obj in enumerate(hist):
        if isinstance(obj, dict) and old_name in obj:
            hist[i] = {new_name: obj[old_name]}
            found = True
            break
    if found:
        cur = DB.cursor()
        cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(hist), email))
        DB.commit()
    return found

class DeleteReq(BaseModel):
    user_email: str
    conversation_name: str

class RenameReq(BaseModel):
    user_email: str
    old_name: str
    new_name: str

@app.post("/delete_chat")
def delete_chat(req: DeleteReq):
    email = (req.user_email or "").strip().lower()
    conv = (req.conversation_name or "").strip()
    if not email or not conv:
        raise HTTPException(status_code=400, detail="user_email and conversation_name required")

    removed = delete_sqlite_conversation(email, conv)
    if supabase:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist = data.get("chat_history", []) if data else []
            if isinstance(hist, list):
                new_hist = [obj for obj in hist if not (isinstance(obj, dict) and conv in obj)]
                supabase.table("users").update({"chat_history": new_hist}).eq("email", email).execute()
                removed = removed or (len(new_hist) < len(hist))
        except Exception as e:
            print("delete_chat supabase error:", e)

    if removed:
        return {"ok": True, "message": f"Conversation '{conv}' deleted for {email}."}
    return {"ok": False, "message": f"Conversation '{conv}' not found for {email}."}

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
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
            hist = data.get("chat_history", []) if data else []
            if isinstance(hist, list):
                for i, obj in enumerate(hist):
                    if isinstance(obj, dict) and old in obj:
                        hist[i] = {new: obj[old]}
                        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
                        renamed = True
                        break
        except Exception as e:
            print("rename_chat supabase error:", e)

    if renamed:
        return {"ok": True, "message": f"Conversation renamed from '{old}' to '{new}' for {email}."}
    return {"ok": False, "message": f"Conversation '{old}' not found for {email}."}

# ---------------- health ----------------
@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()+"Z"}

# ---------------- end of file ----------------
