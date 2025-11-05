# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import os, json, csv, io, traceback, requests, re
from bs4 import BeautifulSoup
import feedparser
import google.generativeai as genai
from dateutil import parser as dateparser
from typing import Optional, Any

# optional supabase (if not installed, code runs without persistent history)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Global News Summarizer (Sheets + RSS + Gemini)")

@app.get("/")
def start():
    return {"message": "hi i am Nova , how can I help you? 🗞️"}

# ---
# CONFIG via ENV (Render)
# ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")            # required
MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
SHEET_ID = os.getenv("SHEET_ID", "").strip()           # optional (public sheet CSV)
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()   # optional
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()   # optional
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "3"))
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))         # recent preference
EXTRA_RSS = os.getenv("RSS_FEEDS", "").strip()

if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in Render environment variables.")

# Initialize Gemini
genai.configure(api_key=GEMINI_API_KEY)

# Initialize Supabase client if provided
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None

# Default major global RSS feeds (expandable)
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
    RSS_FEEDS = EXTRA_RSS.split(",") + RSS_FEEDS

# quick keyword -> category hints (can be extended)
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

# Request model
class ChatReq(BaseModel):
    message: str
    user_email: Optional[str] = None
    prefer_recent: Optional[bool] = True

# In-memory conversation store (fallback if Supabase not configured)
conversations: dict = {}

# -------- Helpers: Sheets CSV fetch (public sheet) -----------
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

# -------- Date parse helper -----------
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

# -------- Search Google Sheet rows for topic -----------
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
        if d and (today - d.date()).days <= DAYS_LIMIT and topic_l in combined:
            recent_matches.append(r)
    if prefer_recent and recent_matches:
        return recent_matches
    return any_matches

# -------- RSS search by category / feeds -----------
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
        # mapping to category first for prioritized feeds
        cat = map_keyword_to_category(topic)
        feeds_to_check = []
        if cat:
            # search feeds that likely match category first
            for url in RSS_FEEDS:
                if cat in url or any(word in url for word in [cat, "space", "tech", "reuters", "nasa", "space.com"]):
                    feeds_to_check.append(url)
            # then include all
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
                        tags = " ".join([t.get("term","") for t in entry.get("tags",[])]) if entry.get("tags") else ""
                    except Exception:
                        tags = ""
                    if topic_l in title or topic_l in summary or topic_l in tags:
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
    # sort by published date if present
    def score_item(it):
        try:
            return dateparser.parse(it.get("published")) if it.get("published") else datetime.min
        except Exception:
            return datetime.min
    found.sort(key=score_item, reverse=True)
    return found

# -------- HTML article extractor (best-effort) -----------
def extract_article_text(url: str, max_chars: int = 15000):
    if not url:
        return None
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"}
        r = requests.get(url, timeout=12, headers=headers)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")

        # prefer <article>, otherwise long <p> tags
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
            # fallback to meta
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

# -------- Gemini summarizer - returns summary + short follow-up question -----------
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

# -------- Conversation persistence helpers -----------
def save_local_conversation(email: str, conv_name: str, sender: str, text: str, meta: Optional[dict] = None):
    if not email:
        return
    if email not in conversations:
        conversations[email] = {"last_conv": conv_name, "convs": {conv_name: []}}
    if conv_name not in conversations[email]["convs"]:
        conversations[email]["convs"][conv_name] = []
    conversations[email]["convs"][conv_name].append({"sender": sender, "text": text, "ts": datetime.utcnow().isoformat()+"Z", "meta": meta or {}})
    conversations[email]["last_conv"] = conv_name

def save_supabase_conversation(email: str, conv_name: str, sender: str, text: str, meta: Optional[dict] = None):
    if not supabase:
        return False
    try:
        # fetch existing user row
        res = supabase.table("users").select("email, chat_history").eq("email", email).execute()
        data = getattr(res, "data", None) or (res.get("data") if isinstance(res, dict) else None)
        if not data:
            # insert user with empty chat_history
            supabase.table("users").insert({"email": email, "chat_history": []}).execute()

        # fetch current
        r2 = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        hist = (getattr(r2, "data", None) or r2.get("data") if isinstance(r2, dict) else {}).get("chat_history", []) or []
        # Normalize hist to list-of-items format
        if isinstance(hist, dict):
            # convert dict into list entries
            hist_list = []
            for k, v in hist.items():
                hist_list.append({k: v})
            hist = hist_list
        if not isinstance(hist, list):
            hist = []

        # append or update
        new_entry = {conv_name: {sender: text, "meta": meta or {}, "ts": datetime.utcnow().isoformat()+"Z"}}
        hist.append(new_entry)
        supabase.table("users").update({"chat_history": hist}).eq("email", email).execute()
        return True
    except Exception as e:
        print("Supabase save error:", e)
        traceback.print_exc()
        return False

# -------- Parse last N chat_history items from supabase user row (best-effort) -----------
def fetch_last_chat_history_topics(email: str, limit: int = 5):
    """
    Returns a list of text snippets (user messages + Nova replies) from last `limit` history items.
    Handles a few shapes of chat_history JSON stored in Supabase.
    """
    snippets = []
    if not supabase or not email:
        # fallback to local memory
        try:
            convs = conversations.get(email, {}).get("convs", {})
            # flatten last messages across last conv
            last_conv = conversations.get(email, {}).get("last_conv")
            if last_conv:
                msgs = convs.get(last_conv, [])[-limit:]
                for m in msgs:
                    snippets.append(m.get("text") or "")
            return snippets
        except Exception:
            return snippets

    try:
        r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
        data = getattr(r, "data", None) or (r.get("data") if isinstance(r, dict) else None)
        hist = data.get("chat_history", []) if data else []
        # Several possible shapes:
        # 1) list of entries like [{ "conv1": {"user":"...","Nova":"..."} }, ...]
        # 2) dict of conv_name->list or dict
        # 3) simple list of messages strings
        if isinstance(hist, dict):
            # convert to list of {conv: {...}}
            hist_list = []
            for k, v in hist.items():
                hist_list.append({k: v})
            hist = hist_list
        if isinstance(hist, list):
            # reverse iterate last entries
            for item in reversed(hist[-limit:]):
                if isinstance(item, dict):
                    # take inner values
                    for conv_name, conv_val in item.items():
                        if isinstance(conv_val, dict):
                            # collect user and Nova text if keys exist
                            if "user" in conv_val:
                                snippets.append(conv_val.get("user",""))
                            # Nova could be under "Nova" or stored as messages
                            if "Nova" in conv_val:
                                snippets.append(conv_val.get("Nova",""))
                            # if conv_val itself is list of messages
                            if isinstance(conv_val, list):
                                for inner in conv_val[-3:]:
                                    if isinstance(inner, dict):
                                        snippets.append(inner.get("text",""))
                        elif isinstance(conv_val, list):
                            for m in conv_val[-3:]:
                                if isinstance(m, dict):
                                    snippets.append(m.get("text",""))
                        else:
                            # fallback: stringify
                            snippets.append(str(conv_val))
                else:
                    snippets.append(str(item))
        else:
            # fallback: stringify
            snippets.append(str(hist))
        # trim and clean
        snippets = [s for s in snippets if s and len(s.strip())>0][:limit]
        return snippets
    except Exception as e:
        print("fetch_last_chat_history_topics error:", e)
        traceback.print_exc()
        return snippets

# -------- Simple keyword extractor from snippets -----------
def extract_priorities_from_history(snippets):
    """
    Given text snippets from previous chats, produce a prioritized list of keywords/topics.
    We'll be conservative: look for known KEYWORD_CATEGORY keys, nouns like 'moon', 'nasa', etc.
    """
    priorities = []
    joined = " ".join(snippets).lower()
    # check known keywords first
    for k in KEYWORD_CATEGORY.keys():
        if k in joined and k not in priorities:
            priorities.append(k)
    # some simple heuristics for single words
    extras = ["moon", "eclipse", "red moon", "supermoon", "launch", "jwst", "black hole", "mars", "nasa", "spacex"]
    for e in extras:
        if e in joined and e not in priorities:
            priorities.append(e)
    # fallback: take top words (naive)
    if not priorities:
        words = re.findall(r"\b[a-z]{3,20}\b", joined)
        freq = {}
        for w in words:
            freq[w] = freq.get(w,0)+1
        common = sorted(freq.items(), key=lambda x: x[1], reverse=True)[:5]
        for w,_ in common:
            if w not in priorities and len(priorities) < 5:
                priorities.append(w)
    return priorities

# -------- Follow-up detection -----------
def is_affirmative_reply(text: str):
    t = text.strip().lower()
    return t in {"yes","y","yeah","yep","sure","absolutely","ok","okay","please","tell me more","more","continue"}

# -------- Main chat endpoint -----------
@app.post("/chat")
def chat(req: ChatReq):
    user_message = (req.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")
    email = (req.user_email or "").strip().lower() or None
    prefer_recent = True if req.prefer_recent is None else bool(req.prefer_recent)

    # Clean a little (remove assistant invocation word variants)
    topic = re.sub(r"\b(nova|nova,|nova:|latest|news|give me|show me|tell me|what's|is there)\b", "", user_message, flags=re.I).strip()
    if not topic:
        topic = user_message.strip()

    # If user is answering follow-up "yes", try to continue last conversation / last link
    if email and is_affirmative_reply(user_message) and ((email in conversations and conversations[email].get("last_conv")) or supabase):
        # try to find last conv and last saved link
        last_conv = conversations.get(email, {}).get("last_conv") if email in conversations else None
        if not last_conv and supabase and email:
            try:
                r = supabase.table("users").select("chat_history").eq("email", email).single().execute()
                data = getattr(r, "data", None) or r.get("data") if isinstance(r, dict) else None
                hist = data.get("chat_history", {}) if data else {}
                if hist:
                    # try to pick last conv name
                    if isinstance(hist, dict):
                        last_conv = list(hist.keys())[-1] if hist else None
                    elif isinstance(hist, list) and len(hist) > 0:
                        # last element may be {convname: {...}}
                        last_conv = list(hist[-1].keys())[0] if isinstance(hist[-1], dict) else None
            except Exception:
                last_conv = None

        last_link = None
        headline = None
        # scan local convs
        if email in conversations and last_conv:
            msgs = conversations[email].get("convs", {}).get(last_conv, [])
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
                hist = data.get("chat_history", {}) if data else {}
                # find last link in history items
                if isinstance(hist, list):
                    for item in reversed(hist):
                        if isinstance(item, dict):
                            for conv_name, conv_val in item.items():
                                if isinstance(conv_val, dict):
                                    # may contain meta
                                    mlink = conv_val.get("meta", {}).get("link")
                                    if mlink:
                                        last_link = mlink
                                        headline = conv_val.get("meta", {}).get("headline")
                                        break
                                elif isinstance(conv_val, list):
                                    for msg in reversed(conv_val):
                                        if isinstance(msg, dict) and msg.get("meta", {}).get("link"):
                                            last_link = msg["meta"]["link"]
                                            headline = msg["meta"].get("headline")
                                            break
                        if last_link:
                            break
                elif isinstance(hist, dict):
                    # dict of convs
                    for conv_name, conv_val in hist.items():
                        if isinstance(conv_val, list):
                            for msg in reversed(conv_val):
                                if isinstance(msg, dict) and msg.get("meta", {}).get("link"):
                                    last_link = msg["meta"]["link"]
                                    headline = msg["meta"].get("headline")
                                    break
                            if last_link:
                                break
            except Exception:
                pass

        if not last_link:
            return {"reply": "I couldn't find the previous article link to continue. Send a direct link or ask about a topic (for example: 'latest NASA launches')."}
        article_text = extract_article_text(last_link)
        if not article_text:
            return {"reply": f"Couldn't fetch more details from {last_link}. Here's the link: {last_link}"}
        # deeper summary (longer)
        deeper_prompt = (
            "You are Nova — now give a richer, deeper explanation about the article, "
            "covering context, significance, and comparisons (if applicable). Keep it clear and factual."
        )
        combined_text = deeper_prompt + "\n\n" + article_text
        deep_summary = summarize_article(combined_text, headline or topic, user_message)
        # save
        conv_name = last_conv or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
        if email:
            save_local_conversation(email, conv_name, "nova", deep_summary, meta={"link": last_link, "headline": headline})
            if supabase:
                save_supabase_conversation(email, conv_name, "nova", deep_summary, meta={"link": last_link, "headline": headline})
        return {"reply": deep_summary, "link": last_link}

    # MAIN flow: use chat history to bias search results
    # 1) Fetch last chat snippets (if any)
    snippets = fetch_last_chat_history_topics(email, limit=5) if email else []
    priorities = extract_priorities_from_history(snippets) if snippets else []
    # Put user typed topic first
    search_topics = []
    if topic:
        search_topics.append(topic)
    # Add priorities next (dedupe)
    for p in priorities:
        if p not in search_topics:
            search_topics.append(p)
    # fallback small default topic if none
    if not search_topics:
        search_topics = [topic or "news"]

    # Try Google Sheet first
    sheet_rows = fetch_sheet_rows(SHEET_ID, SHEET_GID) if SHEET_ID else []
    articles = []

    # helper to add article while avoiding duplicates
    def add_article_if_new(art):
        for a in articles:
            if a.get("link") and art.get("link") and a.get("link") == art.get("link"):
                return False
        articles.append(art)
        return True

    # search through prioritized topics
    for t in search_topics:
        if len(articles) >= MAX_RESULTS:
            break
        if sheet_rows:
            sheet_matches = search_sheet_for_topic(t, sheet_rows, prefer_recent=prefer_recent)
            for r in sheet_matches:
                if len(articles) >= MAX_RESULTS:
                    break
                headline = r.get("headline") or r.get("title") or t
                link = r.get("link") or ""
                article_text = r.get("news") or r.get("summary") or ""
                published = r.get("date") or None
                if link and not article_text:
                    article_text = extract_article_text(link)
                art = {"headline": headline, "link": link, "article_text": article_text, "published": published, "source": "sheet"}
                add_article_if_new(art)

    # then RSS search for any remaining slots
    if len(articles) < MAX_RESULTS:
        for t in search_topics:
            if len(articles) >= MAX_RESULTS:
                break
            rss_found = search_rss_for_topic(t, max_items=20)
            for item in rss_found:
                if len(articles) >= MAX_RESULTS:
                    break
                link = item.get("link")
                headline = item.get("headline") or t
                article_text = extract_article_text(link) or item.get("summary") or ""
                art = {"headline": headline, "link": link, "article_text": article_text, "published": item.get("published"), "source": "rss"}
                add_article_if_new(art)

    # If no articles found, be chatty and helpful
    if not articles:
        hint = ""
        if snippets:
            hint = f" I checked your recent preferences like: {', '.join(snippets[:3])}. "
        return {
            "reply": f"Sorry — I couldn't find articles for '{topic}'.{hint}Try a different query (e.g., 'latest NASA launches', 'red moon eclipse') or paste a link and I'll summarize it for you.",
            "count": 0
        }

    # Summarize articles with Gemini
    summaries = []
    for art in articles[:MAX_RESULTS]:
        if not art.get("article_text"):
            summaries.append({
                "headline": art.get("headline"),
                "link": art.get("link"),
                "summary": f"❗️ No extractable text found at {art.get('link')}.",
                "source": art.get("source"),
                "published": art.get("published")
            })
            continue
        summary_text = summarize_article(art["article_text"], art.get("headline", topic), user_message)
        summaries.append({
            "headline": art.get("headline"),
            "link": art.get("link"),
            "summary": summary_text,
            "source": art.get("source"),
            "published": art.get("published")
        })

    # Compose numbered human-friendly reply (chatty)
    blocks = []
    for i, s in enumerate(summaries, start=1):
        pub = f" (published: {s.get('published')})" if s.get('published') else ""
        block = f"{i}. {s.get('headline')}{pub}\n\n{s.get('summary')}\n\n🔗 {s.get('link')}"
        blocks.append(block)
    combined_reply = "\n\n---\n\n".join(blocks)

    # Save conversation (local + supabase) with metadata including topic and priorities
    conv_name = topic or f"conv_{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}"
    # Save user's message (incoming)
    if email:
        save_local_conversation(email, conv_name, email, user_message, meta={"topic": topic, "priorities": priorities})
        if supabase:
            try:
                save_supabase_conversation(email, conv_name, email, user_message, meta={"topic": topic, "priorities": priorities})
            except Exception as e:
                print("supabase save failed (user msg)", e)
    # Save Nova reply
    if email:
        save_local_conversation(email, conv_name, "nova", combined_reply, meta={"results": len(summaries), "priorities": priorities})
        if supabase:
            try:
                save_supabase_conversation(email, conv_name, "nova", combined_reply, meta={"results": len(summaries), "priorities": priorities})
            except Exception as e:
                print("supabase save failed (nova reply)", e)

    followup_hint = "\n\nWould you like more detail on any of these (reply 'yes' or the number)?"

    return {
        "reply": f"Hey — here are the top {len(summaries)} results I found based on your message and recent chat history:\n\n{combined_reply}{followup_hint}",
        "count": len(summaries),
        "conversation": conv_name
        }
        
