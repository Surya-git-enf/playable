# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta
import os, csv, io, traceback, requests
from bs4 import BeautifulSoup
import google.generativeai as genai

app = FastAPI(title="Nova — Google Sheets only summarizer")

# ----------------------------
# Environment-configured values (set these on Render)
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in Render environment variables.")

SHEET_ID = os.getenv("SHEET_ID", "").strip()   # required
SHEET_GID = os.getenv("SHEET_GID", "0").strip()

MODEL_NAME = os.getenv("GEMINI_MODEL", "gemini-1.5-flash")
# ----------------------------
# Init Gemini
# ----------------------------
genai.configure(api_key=GEMINI_API_KEY)

# ----------------------------
# Request model
# ----------------------------
class ChatReq(BaseModel):
    message: str

# ----------------------------
# Helpers: Sheet CSV -> rows
# ----------------------------
def sheet_csv_url(sheet_id: str, gid: str = "0"):
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}/export?format=csv&gid={gid}"

def fetch_sheet_rows(sheet_id: str, gid: str = "0"):
    if not sheet_id:
        return []
    url = sheet_csv_url(sheet_id, gid)
    try:
        r = requests.get(url, timeout=12)
        r.raise_for_status()
        s = r.content.decode("utf-8")
        reader = csv.DictReader(io.StringIO(s))
        rows = []
        for row in reader:
            # normalize keys to lowercase to be forgiving
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        traceback.print_exc()
        return []

# ----------------------------
# Find recent (<=days_limit) sheet row matching topic
# Expected sheet columns (any order): headline, news, categories, link, image_url, date (YYYY-MM-DD)
# ----------------------------
def find_recent_sheet_news(topic: str, rows, days_limit: int = 2):
    topic_l = (topic or "").lower().strip()
    today = datetime.utcnow().date()
    for r in rows:
        date_str = r.get("date") or r.get("published") or ""
        try:
            d = datetime.strptime(date_str.strip(), "%Y-%m-%d").date() if date_str else None
        except Exception:
            d = None
        if d and (today - d).days <= days_limit:
            headline = r.get("headline", "") or ""
            categories = r.get("categories", r.get("category", "")) or ""
            if (topic_l and (topic_l in headline.lower() or topic_l in categories.lower())):
                return {
                    "headline": headline,
                    "news": r.get("news") or r.get("summary") or "",
                    "link": r.get("link") or "",
                    "image_url": r.get("image_url") or r.get("image") or ""
                }
    return None

# ----------------------------
# Optional: extract article text from link (only when sheet row lacks `news`)
# ----------------------------
def extract_article_text(url: str, max_chars: int = 8000):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"}
        resp = requests.get(url, timeout=12, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        article = soup.find("article")
        paragraphs = []
        if article:
            for p in article.find_all("p"):
                t = p.get_text(strip=True)
                if t:
                    paragraphs.append(t)
        else:
            for p in soup.find_all("p"):
                t = p.get_text(strip=True)
                if t and len(t) > 30:
                    paragraphs.append(t)
        content = "\n\n".join(paragraphs).strip()
        if not content:
            # fallback to meta description
            desc = soup.find("meta", {"name":"description"}) or soup.find("meta", {"property":"og:description"})
            if desc and desc.get("content"):
                content = desc.get("content")
        if not content:
            return None
        if len(content) > max_chars:
            content = content[:max_chars].rsplit(".", 1)[0] + "."
        return content
    except Exception as e:
        print("Article extraction error:", e)
        traceback.print_exc()
        return None

# ----------------------------
# Gemini summarizer wrapper (safe)
# ----------------------------
def call_gemini_summarize(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are a professional news reporter. Summarize the article below in 2-4 short paragraphs. "
            "Then offer one tailored follow-up suggestion the user might want next (e.g., 'Would you like a shorter TL;DR?'). "
            "Be natural and concise.\n\n"
            f"User request: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt, max_output_tokens=512)
        text = getattr(resp, "text", None)
        if not text:
            # defensive: return clear error text for easier logs
            print("Gemini returned empty. resp repr:", repr(resp))
            return "⚠️ Sorry — Gemini returned no summary. Check API key and quota."
        return text.strip()
    except Exception as e:
        print("Gemini call error:", e)
        traceback.print_exc()
        return "⚠️ Sorry — an error occurred while generating the summary."

# ----------------------------
# Main endpoint (SHEET ONLY — no RSS)
# ----------------------------
@app.post("/chat")
def chat(req: ChatReq):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # derive topic (strip helper words)
    topic = message.lower().replace("latest", "").replace("news", "").strip()
    if not topic:
        topic = message.lower().strip()

    # 1) load sheet rows (public sheet CSV)
    rows = fetch_sheet_rows(SHEET_ID, SHEET_GID)
    if not rows:
        # clearly explain in response, so user knows why
        return {"reply": "⚠️ Could not read the Google Sheet. Ensure the sheet is public (Anyone with link can view) and SHEET_ID / SHEET_GID are set in Render env."}

    # 2) find recent row in sheet
    sheet_hit = find_recent_sheet_news(topic, rows, days_limit=2)
    if not sheet_hit:
        return {"reply": f"Sorry — no recent ({2} days) items found in the Google Sheet for '{topic}'. Please add the news to the sheet or try a different query."}

    headline = sheet_hit.get("headline") or topic
    article_text = sheet_hit.get("news") or ""
    link = sheet_hit.get("link") or ""

    # 3) if sheet has no 'news' text but has a link, try to extract article text (still not RSS)
    if not article_text and link:
        article_text = extract_article_text(link)

    if not article_text:
        return {"reply": f"Found an item in the sheet ('{headline}') but it doesn't contain article text and I couldn't fetch it from the link. Please add article text to the sheet's 'news' column or include a readable link."}

    # 4) Summarize via Gemini
    summary = call_gemini_summarize(article_text, headline, message)
    return {"reply": summary, "headline": headline, "link": link}

# health
@app.get("/")
def root():
    return {"status": "ok", "note": "Sheets-only summarizer running"}
