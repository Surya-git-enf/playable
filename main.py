# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import requests, csv, io, re
import feedparser
from bs4 import BeautifulSoup
import google.generativeai as genai
import os
app = FastAPI(title="Nova Simple — Single-input News Summarizer")

# -----------------------------
# EDIT THESE VALUES BEFORE RUN
# -----------------------------
GEMINI_API_KEY = os.getenv("GOOGLE_API_KEY")  # <-- replace with your key
SHEET_ID = "1ahwKkDMSm_o-T17xp4CMe7M1tzR6XgRSz0UHcTiFEzE"  # your sheet id (from the link you provided)
SHEET_GID = "1"  # usually 0 for first sheet; change if needed

# RSS feeds fallback (you can add/remove)
RSS_FEEDS = [
    "https://www.gadgets360.com/rss/feeds",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "http://www.chinadaily.com.cn/rss/china_rss.xml",
    "https://www.space.com/feeds.xml",
    "https://www.nasa.gov/feeds/iotd-feed/"
]

# Gemini model name (change if you want)
MODEL_NAME = "gemini-1.5-flash"

# Configure Gemini
genai.configure(api_key=GEMINI_API_KEY)
MODEL = genai.GenerativeModel(MODEL_NAME)

# ---- Request model ----
class SingleMsg(BaseModel):
    message: str

# ---- Helpers ----
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"):
    """
    Fetch CSV-export of the sheet and return list of rows (dicts).
    Expected columns (order): headline, news, categories, link, image_url, date (YYYY-MM-DD)
    """
    url = sheet_csv_url(sheet_id, gid)
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        s = r.content.decode("utf-8")
        reader = csv.reader(io.StringIO(s))
        rows = list(reader)
        # If header exists, skip it. We try to detect header by first row not being a date in col 5.
        data = []
        for i, row in enumerate(rows):
            if len(row) < 6:
                # pad if needed
                row += [""] * (6 - len(row))
            # If first row seems like header, skip it automatically
            if i == 0 and re.search(r"headline|news|category|date", " ".join(row), re.I):
                continue
            data.append({
                "headline": row[0],
                "news": row[1],
                "categories": row[2],
                "link": row[3],
                "image_url": row[4],
                "date": row[5]
            })
        return data
    except Exception as e:
        print("Sheet fetch error:", e)
        return []

def find_recent_sheet_news(topic: str, rows, days_limit: int = 2):
    topic_l = topic.lower().strip()
    today = datetime.utcnow().date()
    matches = []
    for r in rows:
        try:
            d = datetime.strptime(r.get("date","").strip(), "%Y-%m-%d").date()
        except:
            # if date missing/invalid, skip
            continue
        if (today - d).days <= days_limit:
            if topic_l in (r.get("categories","") or "").lower() or topic_l in (r.get("headline","") or "").lower():
                matches.append(r)
    # prefer newest first
    return matches[0] if matches else None

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
                        "summary": entry.get("summary",""),
                        "published": entry.get("published","")
                    }
        except Exception as e:
            print("RSS error for", url, e)
    return None

def extract_article_text(url: str, max_chars: int = 8000):
    try:
        resp = requests.get(url, timeout=12, headers={"User-Agent":"Mozilla/5.0"})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Try article tag first
        article = soup.find("article")
        texts = []
        if article:
            for p in article.find_all("p"):
                t = p.get_text(strip=True)
                if t:
                    texts.append(t)
        else:
            # fall back to large paragraphs
            for p in soup.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 30:
                    texts.append(t)
        content = "\n\n".join(texts).strip()
        if not content:
            # fallback to meta description
            meta = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"})
            if meta and meta.get("content"):
                content = meta.get("content")
        if not content:
            return None
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".",1)[0] + "."
        return content
    except Exception as e:
        print("Article fetch error:", e)
        return None

def summarize_with_gemini(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are a professional news reporter. Summarize the article below in a concise, readable style (2-4 short paragraphs). "
            "Tailor a follow-up suggestion or prediction of what the user might want next, but do not always repeat the same question.\n\n"
            f"User request: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        resp = MODEL.generate_content(prompt, max_output_tokens=512)
        text = getattr(resp, "text", None)
        if not text:
            # defensive fallback
            return "⚠️ Sorry — couldn't produce a summary right now."
        return text.strip()
    except Exception as e:
        print("Gemini error:", e)
        return "⚠️ Sorry — an error occurred while generating the summary."

# ---- Endpoint ----
@app.post("/chat")
def chat_single(msg: SingleMsg):
    user_message = (msg.message or "").strip()
    if not user_message:
        raise HTTPException(status_code=400, detail="Message is required")

    # determine topic: remove words like "latest" and "news"
    topic = user_message.lower().replace("latest", "").replace("news", "").strip()
    if not topic:
        topic = user_message.lower().strip()

    # 1) Try public Google Sheet
    rows = fetch_sheet_rows(SHEET_ID, SHEET_GID)
    sheet_hit = find_recent_sheet_news(topic, rows, days_limit=2)
    link = None
    headline = None
    article_text = None

    if sheet_hit:
        link = sheet_hit.get("link") or ""
        headline = sheet_hit.get("headline") or ""
        # If the sheet has a 'news' column with article text, use it; otherwise fetch from link
        article_text = sheet_hit.get("news") or None

    # 2) If no sheet hit or no article_text, try RSS
    if (not sheet_hit) or (not article_text):
        rss_item = search_rss_for_topic(topic)
        if rss_item:
            link = rss_item.get("link")
            headline = rss_item.get("headline") or headline or ""
            # prefer summary from rss if available
            article_text = rss_item.get("summary") or article_text

    # 3) If we have a link but no article_text, fetch page and extract
    if link and not article_text:
        article_text = extract_article_text(link)

    if not article_text:
        return {"reply": f"Sorry, I couldn't find a recent article for '{topic}'."}

    # 4) Summarize with Gemini
    summary = summarize_with_gemini(article_text, headline or topic, user_message)

    # Return only summary
    return {"reply": summary}

# ---- Health ----
@app.get("/")
def root():
    return {"status": "ok", "note": "single-input Nova summarizer running"}
