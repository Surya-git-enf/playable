from fastapi import FastAPI, Request
from pydantic import BaseModel
import requests, os, json, google.generativeai as genai
from datetime import datetime, timedelta
from dateutil import parser as dateparser
from supabase import create_client

app = FastAPI()

# Environment
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_GID = os.getenv("SHEET_GID", "0")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Setup clients
genai.configure(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# Google Sheets CSV
def read_google_sheet():
    csv_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    r = requests.get(csv_url)
    if r.status_code != 200:
        raise Exception("Sheet not readable, check public access.")
    lines = [x.split(",") for x in r.text.splitlines()]
    header, rows = lines[0], lines[1:]
    news_items = [dict(zip(header, row)) for row in rows if len(row) == len(header)]
    return news_items

class Message(BaseModel):
    message: str
    email: str = "anonymous@email.com"

@app.post("/chat")
def chat(input: Message):
    try:
        query = input.message.lower()
        news_items = read_google_sheet()

        # Filter news by date (last 2 days) & query match
        recent_cutoff = datetime.now() - timedelta(days=2)
        matched_news = []
        for item in news_items:
            try:
                date_value = dateparser.parse(item.get("date", ""), fuzzy=True)
                if date_value < recent_cutoff:
                    continue
            except Exception:
                continue

            combined_text = " ".join([
                item.get("headline", ""),
                item.get("news", ""),
                item.get("categories", "")
            ]).lower()
            if any(word in combined_text for word in query.split()):
                matched_news.append(item)

        if not matched_news:
            return {"reply": f"Sorry — no recent (2 days) items found for '{query}' in your Google Sheet."}

        top_item = matched_news[0]
        link = top_item.get("link", "")
        content = top_item.get("news", "") or top_item.get("headline", "")

        # Use Gemini to summarize and generate a dynamic question
        model = genai.GenerativeModel("gemini-1.5-flash")
        prompt = f"""
        You are a helpful AI news reporter.
        Summarize this news clearly and conversationally, like speaking to a friend.
        End with a follow-up question about the topic.
        News: {content}
        """

        response = model.generate_content(prompt)
        summary = response.text.strip()

        # Store conversation in Supabase
        email = input.email
        supabase.table("users").upsert({
            "email": email,
            "chat_history": json.dumps({
                query: {"headline": top_item.get("headline", ""), "reply": summary}
            })
        }).execute()

        return {
            "reply": summary,
            "headline": top_item.get("headline"),
            "link": link,
            "conversation": query
        }

    except Exception as e:
        return {"reply": f"⚠️ Error: {str(e)}"}
