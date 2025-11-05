from fastapi import FastAPI, HTTPException from pydantic import BaseModel from datetime import datetime, timedelta, timezone import os, json, csv, io, traceback, requests, re, uuid from bs4 import BeautifulSoup import feedparser import google.generativeai as genai from dateutil import parser as dateparser

optional supabase (if not installed, code runs without persistent history)

try: from supabase import create_client except Exception: create_client = None

app = FastAPI(title="Nova — Global News Summarizer (Sheets + RSS + Gemini)")

@app.get("/") def start(): return {"message":"hi i am Nova , how can I help you?"}

---

CONFIG via ENV (Render)

---

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")            # required MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash") SHEET_ID = os.getenv("SHEET_ID", "").strip()           # optional (public sheet CSV) SHEET_GID = os.getenv("SHEET_GID", "0").strip() SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()   # optional SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()   # optional MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3")) DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))         # recent preference EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()

if not GEMINI_API_KEY: raise RuntimeError("Please set GEMINI_API_KEY in Render environment variables.")

Initialize Gemini

genai.configure(api_key=GEMINI_API_KEY)

Initialize Supabase client if provided

supabase = None if SUPABASE_URL and SUPABASE_KEY and create_client is not None: try: supabase = create_client(SUPABASE_URL, SUPABASE_KEY) except Exception as e: print("Supabase init error:", e) supabase = None

---

DEFAULT RSS FEEDS

---

DEFAULT_RSS_FEEDS = [ # global general "http://feeds.bbci.co.uk/news/rss.xml", "http://rss.cnn.com/rss/edition.rss", "https://www.theguardian.com/world/rss", "https://feeds.reuters.com/reuters/topNews", "https://feeds.npr.org/1001/rss.xml", # space/science "https://www.nasa.gov/rss/dyn/breaking_news.rss", "https://www.space.com/feeds/all", "https://www.sciencedaily.com/rss/top/science.xml", # tech "https://techcrunch.com/feed/", "https://www.theverge.com/rss/index.xml", "https://feeds.arstechnica.com/arstechnica/index", # business "https://feeds.a.dj.com/rss/RSSWorldNews.xml", "https://feeds.reuters.com/reuters/businessNews", # specialty examples "https://www.spacepolicyonline.com/feeds/posts/default", "https://www.spaceflightnow.com/launch-feed/", ]

RSS_FEEDS = DEFAULT_RSS_FEEDS[:] if EXTRA_RSS: # allow users to override/add with comma-separated list RSS_FEEDS = EXTRA_RSS.split(",") + RSS_FEEDS

quick keyword -> category hints (can be extended)

KEYWORD_CATEGORY = { "nasa": "space", "space": "space", "spacex": "space", "jwst": "space", "comet": "space", "moon": "space", "red moon": "space", "ai": "tech", "google": "tech", "apple": "tech", "markets": "business", "economy": "business", "covid": "world", "cricket": "sports", "football": "sports", }

---

Request model

---

class ChatReq(BaseModel): message: str user_email: str | None = None prefer_recent: bool | None = True

In-memory conversation store (fallback if Supabase not configured)

conversations = {}

---

Helpers: Sheets CSV fetch (public sheet)

---

def sheet_csv_url(sheet_id: str, gid: str = "0"): return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"): if not sheet_id: return [] try: url = sheet_csv_url(sheet_id, gid) r = requests.get(url, timeout=12) r.raise_for_status() text = r.content.decode("utf-8") reader = csv.DictReader(io.StringIO(text)) rows = [] for row in reader: normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() } rows.append(normalized) return rows except Exception as e: print("Sheet fetch error:", e) traceback.print_exc() return []

---

Date parse helper

---

def parse_date_safe(date_str: str): if not date_str: return None try: dt = dateparser.parse(date_str) if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc) return dt.astimezone(timezone.utc) except Exception: try: return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").replace(tzinfo=timezone.utc) except Exception: return None

---

Search Google Sheet rows for topic

---

def search_sheet_for_topic(topic: str, rows, prefer_recent=True): topic_l = topic.lower().strip() today = datetime.utcnow().date() recent_matches = [] any_matches = [] for r in rows: combined = " ".join([ r.get("headline",""), r.get("news",""), r.get("categories",""), r.get("link",""), r.get("image_url",""), ]).lower() if topic_l in combined: any_matches.append(r) d = parse_date_safe(r.get("date","") or r.get("published","") or "") if d and (today - d.date()).days <= DAYS_LIMIT: recent_matches.append(r) if prefer_recent and recent_matches: return recent_matches return any_matches

---

RSS search by category / feeds

---

def map_keyword_to_category(topic: str): for k, cat in KEYWORD_CATEGORY.items(): if k in topic.lower(): return cat for cat in ["space","tech","business","world","sports"]: if cat in topic.lower(): return cat return None

def search_rss_for_topic(topic: str, max_items=20): topic_l = topic.lower().strip() found = [] try: cat = map_keyword_to_category(topic) feeds_to_check = [] if cat: # prioritise feeds that mention category keywords for url in RSS_FEEDS: if cat in url or any(word in url for word in [cat, "space", "tech", "reuters", "nasa", "space.com"]): feeds_to_check.append(url) feeds_to_check += [u for u in RSS_FEEDS if u not in feeds_to_check] else: feeds_to_check = RSS_FEEDS

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

---

HTML article extractor (best-effort)

---

def extract_article_text(url: str, max_chars: int = 15000): if not url: return None try: headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"} r = requests.get(url, timeout=12, headers=headers) r.raise_for_status() soup = BeautifulSoup(r.text, "html.parser") article = soup.find("article") texts = [] if article: for p in article.find_all("p"): t = p.get_text(strip=True) if t: texts.append(t) else: for p in soup.find_all("p"): t = p.get_text(strip=True) if t and len(t) > 40: texts.append(t) content = "\n\n".join(texts).strip() if not content: meta = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"}) if meta and meta.get("content"): content = meta.get("content") if not content: return None if len(content) > max_chars: content = content[:max_chars].rsplit(".", 1)[0] + "." return content except Exception as e: print("Article extraction error:", e) traceback.print_exc() return None

---

Gemini summarizer - returns summary + short follow-up question

---

def summarize_article(article_text: str, headline: str, user_message: str): try: prompt = ( "You are Nova — a friendly, concise AI news reporter.\n" "Summarize the article below in 2-4 short paragraphs with clear facts. " "Then produce one short tailored follow-up question the user might want next (1 sentence).\n\n" f"User message: {user_message}\n\n" f"Headline: {headline}\n\n" f"Article:\n{article_text}\n\n" "Summary:" ) model = genai.GenerativeModel(MODEL_NAME) resp = model.generate_content(prompt, max_output_tokens=700) text = getattr(resp, "text", None) if not text: print("Gemini returned empty response:", resp) return "⚠️ Sorry — no summary available from Gemini." return text.strip() except Exception as e: print("Gemini error:", e) traceback.print_exc() return f"⚠️ Sorry — error while generating summary: {e}"

---

Conversation persistence (local + supabase helpers)

---

def save_local_conversation(email: str, conv_name: str, sender: str, text: str, meta: dict | None = None): if not email: return if email not in conversations: conversations[email] = {"last_conv": conv_name, "convs": {conv_name: []}} if conv_name not in conversations[email]["convs"]: conversations[email]["convs"][conv_name] = [] conversations[email]["convs"][conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()+"Z", "meta": meta or {}}) conversations[email]["last_conv"] = conv_name

def fetch_supabase_chat_history(email: str): """Return chat_history as a Python list (JSONB) or []""" if not supabase or not email: return [] try: r = supabase.table("users").select("chat_history").eq("email", email).single().execute() data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None) hist = data.get("chat_history", []) if data else [] # ensure list if isinstance(hist, dict): # older shape: convert to list return [hist] if not isinstance(hist, list): return [] return hist except Exception as e: print("Supabase fetch error:", e) return []

def save_supabase_chat_history_append(email: str, conv_obj): """Append conv_obj to user's chat_history JSONB array (best-effort).""" if not supabase or not email: return False try: # fetch existing r = supabase.table("users").select("chat_history").eq("email", email).single().execute() data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None) hist = data.get("chat_history", []) if data else [] if not isinstance(hist, list): hist = [] hist.append(conv_obj) supabase.table("users").update({"chat_history": hist}).eq("email", email).execute() return True except Exception as e: print("Supabase save error:", e) return False

---

Follow-up detection

---

def is_affirmative_reply(text: str): t = text.strip().lower() return t in {"yes","y","yeah","yep","sure","absolutely","ok","okay","please","tell me more","more","continue"}

---

Parse preferences/topics from chat history

---

def parse_preferences_from_history(hist_list): """Given the chat_history list (JSONB array), return a list of prioritized topic keywords from last N items.""" if not hist_list: return [] topics = [] # look at last up to 5 chat objects for item in reversed(hist_list[-5:]): # item is expected to be like {"conv_name": {"user@example.com": "...", "Nova": "..."}} if not isinstance(item, dict): continue for conv_name, conv_body in item.items(): if not isinstance(conv_body, dict): continue # take user's message(s) - prefer the first non-Nova key for k, v in conv_body.items(): if k.lower() == "nova": continue if isinstance(v, str): text = v.lower() # find any keyword matches from KEYWORD_CATEGORY for kw in KEYWORD_CATEGORY.keys(): if kw in text and kw not in topics: topics.append(kw) # also simple nouns: moon, red moon if "moon" in text and "moon" not in topics: topics.append("moon") return topics

---

Main chat endpoint (improved behavior)

---

@app.post("/chat") def chat(req: ChatReq): user_message = (req.message or "").strip() if not user_message: raise HTTPException(status_code=400, detail="message required") email = (req.user_email or "").strip().lower() or None prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)

# clean topic extraction (keep simple)
topic = re.sub(r"\b(latest|news|give me|show me|tell me|what's|any)\b", "", user_message, flags=re.I).strip()
if not topic:
    topic = user_message.strip()

# If user is answering an earlier follow-up (yes), try to continue last conv
if email and is_affirmative_reply(user_message) and ((email in conversations and conversations[email].get("last_conv")) or supabase):
    # reuse existing behavior to continue last linked article if any
    last_conv = conversations.get(email, {}).get("last_conv") if email in conversations else None
    if not last_conv and supabase and email:
        try:
            r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
            data = getattr(r, "data", None) or r.get("data") if isinstance(r, dict) else None
            hist = data.get("chat_history", {}) if data else {}
            if hist:
                # hist might be list, pick last element's key
                if isinstance(hist, list) and hist:
                    last_conv = list(hist[-1].keys())[0]
        except Exception:
            last_conv = None
    if last_conv:
        # find last link in local convs
        msgs = conversations.get(email, {}).get("convs", {}).get(last_conv, []) if email in conversations else []
        last_link = None
        headline = None
        for m in reversed(msgs):
            meta = m.get("meta") or {}
            if meta.get("link"):
                last_link = meta.get("link")
                headline = meta.get("headline")
                break
        # supabase fallback
        if not last_link and supabase and email:
            try:
                r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
                data = getattr(r, "data", None) or r.get("data") if isinstance(r, dict) else None
                hist = data.get("chat_history", []) if data else []
                if isinstance(hist, list) and hist:
                    conv_msgs = hist[-1]
                    # conv_msgs expected as {conv_name: {user:..., "Nova":...}}
                    body = list(conv_msgs.values())[0] if isinstance(conv_msgs, dict) else {}
                    # attempt to find link in latest Nova message or meta
                    # This is a best-effort fallback; structure may vary
                    last_link = body.get("meta_link") or body.get("link")
                    headline = body.get("headline")
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

        # save
        conv_name = last_conv
        if email:
            save_local_conversation(email, conv_name, "nova", deep_summary, meta={"link": last_link, "headline": headline})
            if supabase:
                try:
                    save_supabase_conversation(email, conv_name, "nova", deep_summary, meta={"link": last_link, "headline": headline})
                except Exception:
                    pass
        return {"reply": deep_summary, "link": last_link}

# Parse user & history preferences to build prioritized topic list
history_topics = []
history_list = fetch_supabase_chat_history(email) if email else []
history_topics = parse_preferences_from_history(history_list)

# Build prioritized topics: explicit in user message first, then history
prioritized = []
# extract quoted phrase or explicit nouns from topic
explicit = topic.lower()
if explicit and explicit not in ("latest","news"):
    prioritized.append(explicit)
for ht in history_topics:
    if ht not in prioritized:
        prioritized.append(ht)

# always include fallback generic topic
if not prioritized:
    prioritized = [topic]

# Try Google Sheet first
sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []

articles = []

# helper to add matches for a topic
def add_matches_for_topic(t):
    nonlocal articles
    # search sheet
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
    # if still need, search RSS
    if len(articles) < MAX_RESULTS:
        rss_found = search_rss_for_topic(t, max_items=20)
        for item in rss_found:
            if len(articles) >= MAX_RESULTS:
                break
            link = item.get("link")
            headline = item.get("headline") or t
            article_text = extract_article_text(link) or item.get("summary") or ""
            articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

# iterate prioritized topics until we fill MAX_RESULTS
for t in prioritized:
    if len(articles) >= MAX_RESULTS:
        break
    add_matches_for_topic(t)

# if still empty, try searching the raw topic once
if not articles:
    rss_found = search_rss_for_topic(topic, max_items=30)
    for item in rss_found[:MAX_RESULTS]:
        link = item.get("link")
        headline = item.get("headline") or topic
        article_text = extract_article_text(link) or item.get("summary") or ""
        articles.append({"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"})

if not articles:
    return {"reply": f"Sorry — I couldn't find articles for '{topic}' in the sheet or RSS feeds. Try a different query or provide a link."}

# Summarize articles
summaries = []
for art in articles[:MAX_RESULTS]:
    if not art.get("article_text"):
        summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": f"❗️ No extractable text found at {art.get('link')}.", "source": art.get("source")})
        continue
    summary_text = summarize_article(art["article_text"], art.get("headline", topic), user_message)
    summaries.append({"headline": art.get("headline"), "link": art.get("link"), "summary": summary_text, "source": art.get("source"), "published": art.get("published")})

# Compose numbered chatty reply (human-friendly)
blocks = []
for i, s in enumerate(summaries, start=1):
    block = f"{i}. {s.get('headline')}\n\n{s.get('summary')}\n\nLink: {s.get('link')}"
    blocks.append(block)
combined_reply = "\n\n---\n\n".join(blocks)

# Add a short personalized follow-up based on history topics
followup_hint = "\n\nWould you like more detail on any of these (reply 'yes' or the number)?"
# build conversation name and save
conv_name = f"nova_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}_{uuid.uuid4().hex[:6]}"

# Save local
if email:
    save_local_conversation(email, conv_name, email, user_message, meta={"topic": topic})
    save_local_conversation(email, conv_name, "nova", combined_reply + followup_hint, meta={"results": len(summaries)})

# Saveto supabase chat_history as JSONB array (append conv object)
    if email and supabase:
        try:
            # structure: [{"conv_name_unique": {"user_email_or_name": "user message", "Nova": "reply"}}, ...]
            user_key = email
            conv_obj = {conv_name: {user_key: user_message, "Nova": combined_reply + followup_hint}}
            save_supabase_chat_history_append(email, conv_obj)
        except Exception as e:
            print("Failed to append to supabase chat_history:", e)

    return {"reply": combined_reply + followup_hint, "count": len(summaries), "conversation": conv_name}


# ---
# Helper to reuse old supabase saving function signature from previous code
# ---

def save_supabase_conversation(email: str, conv_name: str, sender: str, text: str, meta: dict | None = None):
    if not supabase:
        return False
    try:
        # ensure user row
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)
        if not data:
            supabase.table("users").insert({"email": email, "chat_history": []}).execute()
        # fetch current
        r2 = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        hist = (getattr(r2, "data", None) or r2.get("data") if isinstance(r2, dict) else {}).get("chat_history", []) or []
        # append new message(s) into last conv object for compatibility
        # Here we add an entry with conv_name wrapper so both old and new flows are supported
        conv_obj = {conv_name: {sender: text, "meta": meta or {}}}
        if not isinstance(hist, list):
            hist = []
        hist.append(conv_obj)
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("Supabase save error:", e)
        return False
