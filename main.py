import os
import traceback
import requests
import feedparser
from flask import Flask, request, jsonify
from bs4 import BeautifulSoup
from datetime import datetime, timedelta
from dateutil import parser as dateparser
import google.generativeai as genai

# === Configuration ===
app = Flask(__name__)
MODEL_NAME = "gemini-1.5-flash"

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY", "")
SHEET_ID = os.getenv("SHEET_ID", "")
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "")

genai.configure(api_key=GOOGLE_API_KEY)

# RSS Feeds to pull backup data if Google Sheet empty
RSS_FEEDS = [
    "https://www.nasa.gov/rss/dyn/breaking_news.rss",
    "https://rss.cnn.com/rss/edition_space.rss",
    "https://www.space.com/feeds/all",
]


# === Helper: extract readable text from HTML ===
def extract_article_text(url):
    try:
        resp = requests.get(url, timeout=10)
        soup = BeautifulSoup(resp.text, "html.parser")
        # Remove script/style
        for s in soup(["script", "style", "noscript"]):
            s.decompose()
        text = " ".join(soup.stripped_strings)
        return text[:5000]  # limit to avoid long prompts
    except Exception as e:
        print("Error extracting article:", e)
        return ""


# === Helper: summarize with Gemini ===
def summarize_article(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are Nova — a friendly AI news assistant.\n"
            "Summarize the article below in 2-4 short paragraphs with key facts, "
            "and end with one line suggesting what might come next or why it matters.\n\n"
            f"User asked: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        model = genai.GenerativeModel(MODEL_NAME)

        try:
            if hasattr(genai, "types"):
                cfg = None
                try:
                    cfg = genai.types.GenerateContentConfig(max_output_tokens=600)
                except Exception:
                    cfg = None
                if cfg:
                    resp = model.generate_content(prompt, config=cfg)
                else:
                    resp = model.generate_content(prompt)
            else:
                resp = model.generate_content(prompt)
        except TypeError:
            resp = model.generate_content(prompt)

        text = getattr(resp, "text", None)
        if not text:
            return "⚠️ Sorry — Gemini returned an empty summary."
        return text.strip()
    except Exception as e:
        traceback.print_exc()
        return f"⚠️ Sorry — error while generating summary: {e}"


# === Helper: fetch data from Google Sheet ===
def fetch_sheet_data():
    try:
        if not SHEET_ID:
            return []

        url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/gviz/tq?tqx=out:csv"
        resp = requests.get(url, timeout=10)
        if resp.status_code != 200:
            print("Google Sheet fetch error:", resp.text)
            return []

        import csv
        from io import StringIO

        data = []
        f = StringIO(resp.text)
        reader = csv.DictReader(f)
        for row in reader:
            data.append({
                "headline": row.get("headline") or "",
                "news": row.get("news") or "",
                "categories": row.get("categories") or "",
                "link": row.get("link") or "",
                "date": row.get("date") or "",
            })
        return data
    except Exception as e:
        print("Error reading sheet:", e)
        return []


# === Helper: fetch fallback news from RSS feeds ===
def fetch_rss_news():
    articles = []
    for feed_url in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                published = None
                try:
                    published = dateparser.parse(entry.get("published", ""))
                except Exception:
                    published = datetime.utcnow()

                articles.append({
                    "headline": entry.title,
                    "link": entry.link,
                    "published": published,
                    "summary": entry.get("summary", ""),
                })
        except Exception as e:
            print("RSS fetch error:", e)
    return articles


# === Main route ===
@app.route("/", methods=["GET", "POST"])
def home():
    return jsonify({
        "message": "🛰️ Nova News API is running!",
        "usage": "POST {'message': 'latest NASA news'} to /news"
    })


@app.route("/news", methods=["POST"])
def get_news():
    try:
        user_input = request.json.get("message", "").strip().lower()
        if not user_input:
            return jsonify({"reply": "⚠️ Please include a 'message' in JSON body."})

        keywords = [word for word in user_input.split() if word.isalpha()]

        # Try Google Sheets first
        sheet_data = fetch_sheet_data()
        matched = []
        for row in sheet_data:
            text = f"{row['headline']} {row['news']} {row['categories']}".lower()
            if any(k in text for k in keywords):
                matched.append(row)

        if not matched:
            # Fallback: RSS feed
            rss_data = fetch_rss_news()
            for item in rss_data:
                if any(k in item["headline"].lower() or k in item.get("summary", "").lower() for k in keywords):
                    matched.append({
                        "headline": item["headline"],
                        "link": item["link"],
                        "news": item.get("summary", ""),
                        "date": item.get("published", ""),
                        "categories": "rss",
                    })

        if not matched:
            return jsonify({"reply": f"⚠️ No news found for '{user_input}'. Try again later."})

        response_list = []
        for item in matched[:3]:
            article_text = extract_article_text(item["link"])
            summary = summarize_article(article_text or item["news"], item["headline"], user_input)
            response_list.append({
                "headline": item["headline"],
                "link": item["link"],
                "summary": summary,
            })

        reply = "\n\n---\n\n".join([
            f"**{i+1}. {n['headline']}**\n\n{n['summary']}\n\n🔗 {n['link']}"
            for i, n in enumerate(response_list)
        ])

        return jsonify({
            "reply": reply,
            "count": len(response_list),
            "conversation": user_input
        })

    except Exception as e:
        traceback.print_exc()
        return jsonify({"reply": f"⚠️ Server error: {e}"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
