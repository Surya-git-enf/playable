from fastapi import FastAPI, Request
from pydantic import BaseModel
from datetime import datetime, timedelta
import os, json, feedparser, asyncio
import google.generativeai as genai

app = FastAPI()

# --- ENVIRONMENT VARIABLES ---
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GOOGLE_SHEETS_API_KEY = os.getenv("GOOGLE_SHEETS_API_KEY")  # optional
GOOGLE_SHEETS_ID = os.getenv("GOOGLE_SHEETS_ID")

# --- CONFIGURE GEMINI ---
genai.configure(api_key=GEMINI_API_KEY)
model = genai.GenerativeModel("gemini-1.5-flash")

# --- SYSTEM PROMPT (personality & behavior) ---
SYSTEM_PROMPT = """
You are Nova 🪶, a friendly AI news reporter who reads data from Google Sheets and RSS feeds.
You summarize news clearly, add context, and end every report with:
"Would you like to know more about this topic?" 🗞️
Keep replies engaging and concise.
"""

# --- In-memory chat memory ---
memory = {}

# --- RSS FEEDS ---
RSS_FEEDS = [
    "https://www.gadgets360.com/rss/feeds",
    "https://feeds.content.dowjones.io/public/rss/RSSWorldNews",
    "https://feeds.content.dowjones.io/public/rss/WSJcomUSBusiness",
    "https://feeds.content.dowjones.io/public/rss/RSSUSnews",
    "http://www.chinadaily.com.cn/rss/china_rss.xml",
    "https://www.space.com/feeds.xml",
    "https://www.nasa.gov/feeds/iotd-feed/",
]

# --- Pydantic model ---
class Msg(BaseModel):
    user_id: str
    message: str

# --- RSS Fetcher ---
def fetch_rss_news(topic: str):
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:8]:
            if topic.lower() in entry.title.lower() or topic.lower() in entry.summary.lower():
                return {
                    "headline": entry.title,
                    "news": entry.summary,
                    "link": entry.link,
                    "image": getattr(entry, "media_content", [{}])[0].get("url", ""),
                    "date": getattr(entry, "published", str(datetime.now().date()))
                }
    return None

# --- Fake local "sheet cache" (for free-tier use) ---
sheet_data = []  # You can persist this to file or DB if needed

# --- Search cache (like Google Sheets lookup) ---
def find_recent_news(topic):
    today = datetime.now()
    for row in sheet_data:
        if topic.lower() in row["category"].lower() and (today - row["date"]).days <= 2:
            return row
    return None

# --- Save to cache (like Sheets append) ---
def save_news(news_data, topic):
    sheet_data.append({
        "headline": news_data["headline"],
        "news": news_data["news"],
        "category": topic,
        "link": news_data["link"],
        "image": news_data["image"],
        "date": datetime.now()
    })

# --- AI Response ---
async def ai_respond(prompt: str, user_id: str):
    if user_id not in memory:
        memory[user_id] = []

    # combine context + new message
    context = "\n".join([m for m in memory[user_id][-6:]])
    final_prompt = f"{SYSTEM_PROMPT}\n\nPrevious:\n{context}\n\nUser: {prompt}"

    response = model.generate_content(final_prompt)
    text = response.text.strip()

    memory[user_id].append(f"User: {prompt}")
    memory[user_id].append(f"Nova: {text}")
    return text

# --- MAIN CHAT ENDPOINT ---
@app.post("/chat")
async def chat(msg: Msg):
    user_id = msg.user_id
    message = msg.message.strip()

    topic = message.replace("latest", "").replace("news", "").strip()
    news_data = find_recent_news(topic)

    if not news_data:
        # Try fetching fresh from RSS
        news_data = fetch_rss_news(topic)
        if news_data:
            save_news(news_data, topic)

    if news_data:
        ai_prompt = (
            f"Summarize this news engagingly and conversationally, as a reporter.\n\n"
            f"Headline: {news_data['headline']}\n"
            f"Details: {news_data['news']}\n"
            f"Link: {news_data['link']}\n"
            f"Then end with 'Would you like to know more about this topic?'"
        )
        ai_reply = await ai_respond(ai_prompt, user_id)
        return {
            "reply": ai_reply,
            "headline": news_data["headline"],
            "link": news_data["link"]
        }
    else:
        fallback = await ai_respond(
            f"User asked for {topic} news but it's unavailable. Respond politely and suggest another category.",
            user_id
        )
        return {"reply": fallback}

# --- Root endpoint ---
@app.get("/")
async def root():
    return {"status": "Nova (Gemini News Agent) is running ✅"}
