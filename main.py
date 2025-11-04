from fastapi import FastAPI
from pydantic import BaseModel
import requests, os, json
from datetime import datetime, timedelta
from dateutil import parser as dateparser
import google.generativeai as genai
from supabase import create_client

app = FastAPI()

# ====== ENVIRONMENT VARIABLES ======
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
SHEET_ID = os.getenv("SHEET_ID")
SHEET_GID = os.getenv("SHEET_GID", "0")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# ====== INITIALIZE CLIENTS ======
genai.configure(api_key=GEMINI_API_KEY)
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

# ====== GOOGLE SHEET FETCH ======
def read_google_sheet():
    """Reads CSV export of the public Google Sheet."""
    csv_url = f"https://docs.google.com/spreadsheets/d/{SHEET_ID}/export?format=csv&gid={SHEET_GID}"
    res = requests.get(csv_url)
    if res.status_code != 200:
        raise Exception("Could not read Google Sheet. Make sure it’s public.")
    lines = [x.split(",") for x in res.text.splitlines()]
    header, rows = lines[0], lines[1:]
    news_items = [dict(zip(header, row)) for row in rows if len(row) == len(header)]
    return news_items

# ====== INPUT MODEL ======
class Message(BaseModel):
    message: str
    email: str = "anonymous@email.com"

# ====== MAIN CHAT ENDPOINT ======
@app.post("/chat")
def chat(input: Message):
    try:
        query = input.message.lower().strip()
        news_items = read_google_sheet()

        recent_cutoff = datetime.now() - timedelta(days=2)
        matched_news, fallback_news = [], []

        for item in news_items:
            date_str = item.get("date", "")
            try:
                date_value = dateparser.parse(date_str, fuzzy=True)
            except Exception:
                date_value = None

            combined_text = " ".join([
                item.get("headline", ""),
                item.get("news", ""),
                item.get("categories", "")
            ]).lower()

            if any(word in combined_text for word in query.split()):
                if date_value and date_value >= recent_cutoff:
                    matched_news.append(item)
                else:
                    fallback_news.append(item)

        if not matched_news:
            matched_news = fallback_news

        if not matched_news:
            return {"reply": f"⚠️ No matching news found for '{query}'. Add more data to the sheet."}

        # Limit to top 3-5 results
        matched_news = matched_news[:5]

        # ====== Summarize all matches ======
        summaries = []
        model = genai.GenerativeModel("gemini-1.5-flash")

        for news in matched_news:
            content = news.get("news") or news.get("headline") or ""
            link = news.get("link", "")
            prompt = f"""
            You are Nova, a friendly AI news reporter 🗞️.
            Summarize this news naturally and clearly.
            End with a question related to this topic to engage the reader.
            News: {content}
            """
            try:
                response = model.generate_content(prompt)
                text = response.text.strip()
            except Exception as e:
                text = f"⚠️ Summary failed: {str(e)}"

            summaries.append({
                "headline": news.get("headline", ""),
                "summary": text,
                "link": link
            })

        # ====== Build combined reply ======
        combined_reply = "\n\n".join(
            [f"📰 {s['headline']}\n{s['summary']}\n🔗 {s['link']}" for s in summaries]
        )

        # ====== Save chat history ======
        try:
            email = input.email
            chat_record = {
                query: {"news": summaries, "timestamp": datetime.now().isoformat()}
            }
            supabase.table("users").upsert({
                "email": email,
                "chat_history": json.dumps(chat_record)
            }).execute()
        except Exception as e:
            print("⚠️ Supabase save failed:", e)

        return {
            "reply": combined_reply,
            "conversation": query,
            "count": len(summaries)
        }

    except Exception as e:
        return {"reply": f"⚠️ Error: {str(e)}"}
