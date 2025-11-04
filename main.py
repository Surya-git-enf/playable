import os
import traceback
import requests
import feedparser
from bs4 import BeautifulSoup
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from datetime import datetime, timedelta
from dateutil import parser as dateparser
import google.generativeai as genai

# === Config ===
app = FastAPI(title="Nova News API", version="1.0")
MODEL_NAME = "gemini-1.5-flash"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
SHEET_ID = os.getenv("SHEET_ID", "")

genai.configure(api_key=GOOGLE_API_KEY)

RSS_FEEDS = [
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://rss.cnn.com/rss/edition_space.rss",
    "https://www.space.com/feeds/all",
]


# === Extract readable text ===
def extract_article_text(url):
    try:
        r = requests.get(url, timeout=10)
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        text = " ".join(soup.stripped_strings)
        return text[:6000]
    except Exception as e:
        print("❌ HTML extraction failed:", e)
        return ""


# === Summarize using Gemini ===
def summarize_article(article_text, headline, user_query):
    try:
        prompt = f"""
You are Nova — a helpful space news assistant. 
Summarize the article below in 3 short paragraphs with facts, mission names, discoveries, or data.
End with a 1-line insight (why it matters or what's next).

User asked: {user_query}
Headline: {headline}

Article:
{article_text}

Summary:
"""
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt)
        text = getattr(resp, "text", None)
        if not text:
            return "⚠️ Gemini returned no summary."
        return text.strip()
    except Exception as e:
        traceback.print_exc()
        return f"⚠️ Error while summarizing: {e}"


# === Fetch from Google Sheet ===
def fetch_google_sheet():
    if not SHEET_ID:
        return []

    try:
        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print("Google Sheet fetch error:", resp.text)
            return []
        import csv
        from io import StringIO
        data = []
        reader = csv.DictReader(StringIO(resp.text))
        for row in reader:
            data.append({
                "headline": row.get("headline", ""),
                "news": row.get("news", ""),
                "categories": row.get("categories", ""),
                "link": row.get("link", ""),
                "date": row.get("date", ""),
            })
        return data
    except Exception as e:
        print("❌ Sheet read error:", e)
        return []


# === Fetch RSS News ===
def fetch_rss_news():
    articles = []
    for url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                pub = entry.get("published", "")
                try:
                    pub_date = dateparser.parse(pub)
                except Exception:
                    pub_date = datetime.utcnow()
                articles.append({
                    "headline": entry.title,
                    "link": entry.link,
                    "summary": entry.get("summary", ""),
                    "date": pub_date,
                })
        except Exception as e:
            print("❌ RSS parse error:", e)
    return articles


# === Root Route ===
@app.get("/")
async def home():
    return {"message": "🛰️ Nova FastAPI backend active!", "usage": "POST /news with {'message': 'latest nasa news'}"}


# === Main Logic Route ===
@app.post("/news")
async def get_news(request: Request):
    try:
        body = await request.json()
        query = body.get("message", "").strip().lower()
        if not query:
            return JSONResponse({"reply": "⚠️ Missing 'message' in body."})

        keywords = [k for k in query.split() if k.isalpha()]

        # Try from Google Sheet first
        rows = fetch_google_sheet()
        matched = []
        for r in rows:
            text = f"{r['headline']} {r['news']} {r['categories']}".lower()
            if any(k in text for k in keywords):
                matched.append(r)

        # If no match, pull from RSS
        if not matched:
            rss_items = fetch_rss_news()
            for item in rss_items:
                if any(k in item["headline"].lower() or k in item["summary"].lower() for k in keywords):
                    matched.append({
                        "headline": item["headline"],
                        "news": item.get("summary", ""),
                        "link": item["link"],
                        "date": item["date"],
                        "categories": "rss"
                    })

        if not matched:
            return JSONResponse({"reply": f"⚠️ No news found for '{query}'."})

        results = []
        for m in matched[:3]:
            article_text = extract_article_text(m["link"])
            summary = summarize_article(article_text or m["news"], m["headline"], query)
            results.append({
                "headline": m["headline"],
                "summary": summary,
                "link": m["link"]
            })

        reply_text = "\n\n---\n\n".join([
            f"**{i+1}. {r['headline']}**\n\n{r['summary']}\n\n🔗 {r['link']}"
            for i, r in enumerate(results)
        ])

        return JSONResponse({
            "reply": reply_text,
            "count": len(results),
            "conversation": query
        })

    except Exception as e:
        traceback.print_exc()
        return JSONResponse({"reply": f"⚠️ Server error: {e}"})


# === Run locally (Render auto starts via gunicorn) ===
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000, reload=True)
