from fastapi import FastAPI
from pydantic import BaseModel
import feedparser
import datetime
import gspread
from google.oauth2.service_account import Credentials
import google.generativeai as genai
import os
import re

# -----------------------------
# 🔐 Setup
# -----------------------------
app = FastAPI()
genai.configure(api_key=os.getenv("GEMINI_API_KEY"))

# Google Sheets setup
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
creds = Credentials.from_service_account_file("service_account.json", scopes=SCOPES)
gc = gspread.authorize(creds)
SHEET_ID = "YOUR_SHEET_ID"  # e.g. 1abcXYZ123...
worksheet = gc.open_by_key(SHEET_ID).sheet1

# -----------------------------
# 🗞️ RSS Feeds
# -----------------------------
RSS_FEEDS = {
    "tech": [
        "https://feeds.feedburner.com/TechCrunch/",
        "https://www.theverge.com/rss/index.xml",
        "https://www.cnet.com/rss/news/"
    ],
    "space": [
        "https://www.nasa.gov/feeds/iotd-feed/",
        "https://www.space.com/feeds/all"
    ],
    "world": [
        "https://feeds.reuters.com/reuters/worldNews",
        "https://feeds.content.dowjones.io/public/rss/RSSWorldNews"
    ],
}

# -----------------------------
# 📩 Message Model
# -----------------------------
class Msg(BaseModel):
    message: str
    user_id: str = "default_user"  # Optional user context


# -----------------------------
# 🔍 Helper Functions
# -----------------------------
def clean_text(text):
    return re.sub(r"\s+", " ", text.strip())

def get_news_from_sheets(keyword):
    records = worksheet.get_all_records()
    today = datetime.datetime.now()
    recent_news = []
    for row in records:
        date_str = row.get("date")
        if not date_str:
            continue
        try:
            news_date = datetime.datetime.strptime(date_str, "%Y-%m-%d")
            if (today - news_date).days <= 2:
                if keyword.lower() in row.get("categories", "").lower() or keyword.lower() in row.get("headline", "").lower():
                    recent_news.append(row)
        except:
            continue
    return recent_news

def fetch_from_rss(keyword):
    news_data = []
    for topic, urls in RSS_FEEDS.items():
        if keyword in topic or keyword in ["latest", "news", "all"]:
            for url in urls:
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    news_data.append({
                        "headline": clean_text(entry.title),
                        "news": clean_text(entry.get("summary", "")),
                        "categories": topic,
                        "link": entry.link,
                        "image_url": entry.get("media_content", [{}])[0].get("url", ""),
                        "date": datetime.datetime.now().strftime("%Y-%m-%d")
                    })
    return news_data

def store_in_sheets(news_items):
    for n in news_items:
        worksheet.append_row([
            n["headline"],
            n["news"],
            n["categories"],
            n["link"],
            n["image_url"],
            n["date"]
        ])

def summarize_with_gemini(prompt):
    model = genai.GenerativeModel("gemini-1.5-flash")
    response = model.generate_content(prompt)
    return response.text


# -----------------------------
# 🤖 Chat Endpoint
# -----------------------------
@app.post("/chat")
def chat(message: Msg):
    user_msg = message.message.lower()

    # Try Sheets memory first
    found_news = get_news_from_sheets(user_msg)

    if not found_news:
        # Fetch new data from RSS
        new_data = fetch_from_rss(user_msg)
        if new_data:
            store_in_sheets(new_data)
            found_news = new_data

    if not found_news:
        return {"reply": "Sorry, I couldn’t find any relevant news at the moment."}

    # Prepare Gemini summary
    news_text = "\n".join([f"- {n['headline']} ({n['link']})" for n in found_news])
    prompt = f"""
    You are a friendly AI news reporter. 
    The user asked: "{user_msg}".
    Here is the latest news data:
    {news_text}

    Summarize like a news anchor, mention 2-3 headlines naturally, and end with:
    "Would you like to know more or ask a question about any of these stories?"
    """

    gemini_reply = summarize_with_gemini(prompt)
    return {"reply": gemini_reply}


# -----------------------------
# 🚀 Run Local
# -----------------------------
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=10000)
