from fastapi import FastAPI, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
import os, json, feedparser, asyncio
import google.auth
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
import google.generativeai as genai

app = FastAPI()

# --- Environment Variables ---
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")
GOOGLE_SHEETS_TOKEN = os.getenv("GOOGLE_SHEETS_TOKEN")  # Token JSON as text
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")

# --- Configure Gemini ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# --- RSS Feeds ---
RSS_FEEDS = [
    "https://www.gadgets360.com/rss/feeds",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "http://www.chinadaily.com.cn/rss/china_rss.xml",
    "https://www.space.com/feeds.xml",
    "https://www.nasa.gov/feeds/iotd-feed/",
]

# --- Google Sheets Setup ---
def get_sheets_service():
    creds_dict = json.loads(GOOGLE_SHEETS_TOKEN)
    creds = Credentials.from_authorized_user_info(creds_dict)
    return build("sheets", "v4", credentials=creds)

def read_sheet():
    service = get_sheets_service()
    sheet = service.spreadsheets()
    result = sheet.values().get(spreadsheetId=GOOGLE_SHEETS_ID, range="Sheet1!A2:F").execute()
    rows = result.get("values", [])
    return rows

def append_to_sheet(row):
    service = get_sheets_service()
    sheet = service.spreadsheets()
    sheet.values().append(
        spreadsheetId=GOOGLE_SHEETS_ID,
        range="Sheet1!A2",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [row]},
    ).execute()

# --- Pydantic Model ---
class Msg(BaseModel):
    message: str

# --- Utility to find latest news ---
def find_recent_news(topic: str, rows):
    today = datetime.now()
    for row in rows:
        if len(row) < 6:
            continue
        headline, news, category, link, image, date_str = row
        try:
            date = datetime.strptime(date_str, "%Y-%m-%d")
            if topic.lower() in category.lower() and (today - date).days <= 2:
                return {"headline": headline, "news": news, "link": link, "image": image}
        except:
            continue
    return None

# --- RSS Fetcher ---
def fetch_rss_news(topic: str):
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:10]:
            if topic.lower() in entry.title.lower() or topic.lower() in entry.summary.lower():
                return {
                    "headline": entry.title,
                    "news": entry.summary,
                    "link": entry.link,
                    "image": getattr(entry, "media_content", [{}])[0].get("url", ""),
                }
    return None

# --- AI Response ---
async def ai_respond(prompt: str):
    response = model.generate_content(prompt)
    return response.text.strip()

# --- Main Route ---
@app.post("/chat")
async def chat(msg: Msg):
    user_message = msg.message.strip()
    rows = read_sheet()
    topic = user_message.replace("latest", "").replace("news", "").strip()

    # Check Google Sheets
    news_data = find_recent_news(topic, rows)

    if not news_data:
        # Fetch from RSS if not in sheets
        news_data = fetch_rss_news(topic)
        if news_data:
            append_to_sheet([
                news_data["headline"],
                news_data["news"],
                topic,
                news_data["link"],
                news_data["image"],
                datetime.now().strftime("%Y-%m-%d"),
            ])

    if news_data:
        reporter_prompt = (
            f"You are a smart news reporter. Summarize this news in a friendly way and end with "
            f"'Do you want to know anything more about this topic?' 🗞️\n\n"
            f"Headline: {news_data['headline']}\n"
            f"Details: {news_data['news']}\n"
            f"Link: {news_data['link']}"
        )
        ai_reply = await ai_respond(reporter_prompt)
        return {"reply": ai_reply, "headline": news_data["headline"], "link": news_data["link"]}
    else:
        fallback = await ai_respond(
            f"User asked for {topic} news, but none found. Kindly respond politely."
        )
        return {"reply": fallback}

@app.get("/")
async def root():
    return {"status": "Gemini News Agent is Running ✅"}
