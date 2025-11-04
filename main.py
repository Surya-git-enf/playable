# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime, timedelta, timezone
import os, csv, io, traceback, requests, re
from bs4 import BeautifulSoup
import google.generativeai as genai
from dateutil import parser as date_parser

app = FastAPI(title="Nova — Sheets-only summarizer (robust date + fallback)")

# ----------------------------
# Environment-configured values
# ----------------------------
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
if not GEMINI_API_KEY:
    raise RuntimeError("Please set GEMINI_API_KEY in environment variables on Render.")

SHEET_ID = os.getenv("SHEET_ID", "").strip()   # required
SHEET_GID = os.getenv("SHEET_GID", "0").strip()
# Number of days to consider "recent" (default 2). If no recent match, code will fallback to all rows.
DAYS_LIMIT = int(os.getenv("DAYS_LIMIT", "2"))
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
            normalized = { (k or "").strip().lower(): (v or "").strip() for k, v in row.items() }
            rows.append(normalized)
        return rows
    except Exception as e:
        print("Sheet fetch error:", e)
        traceback.print_exc()
        return []

# ----------------------------
# Parse dates robustly (handles ISO with timezone)
# ----------------------------
def parse_date_safe(date_str: str):
    if not date_str:
        return None
    try:
        dt = date_parser.parse(date_str)
        # Normalize to UTC date
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).date()
    except Exception:
        # Try simple YYYY-MM-DD
        try:
            return datetime.strptime(date_str.split("T")[0], "%Y-%m-%d").date()
        except Exception:
            return None

# ----------------------------
# Find recent sheet news (strict recent filter)
# ----------------------------
def find_recent_sheet_news(topic: str, rows, days_limit: int = 2):
    topic_l = (topic or "").lower().strip()
    today_utc = datetime.utcnow().date()
    for r in rows:
        date_str = r.get("date") or r.get("published") or r.get("pubdate") or ""
        d = parse_date_safe(date_str)
        if not d:
            continue
        # within days_limit
        try:
            if (today_utc - d).days <= days_limit:
                # check matches in headline/categories/news/link/image_url
                combined = " ".join([
                    r.get("headline",""),
                    r.get("categories","") or r.get("category",""),
                    r.get("news",""),
                    r.get("link",""),
                    r.get("image_url","") or r.get("image","")
                ]).lower()
                if topic_l and topic_l in combined:
                    return {
                        "headline": r.get("headline",""),
                        "news": r.get("news",""),
                        "link": r.get("link",""),
                        "image_url": r.get("image_url","") or r.get("image",""),
                        "date": date_str
                    }
        except Exception:
            continue
    return None

# ----------------------------
# Fallback: search all rows ignoring date, prefer best match
# ----------------------------
def find_best_match_any_date(topic: str, rows):
    topic_l = (topic or "").lower().strip()
    if not topic_l:
        return None
    # Score rows by number of occurrences of topic in key fields, prefer recent if date exists
    best = None
    best_score = -1
    best_date = None
    for r in rows:
        combined_fields = [
            r.get("headline",""),
            r.get("categories","") or r.get("category",""),
            r.get("news",""),
            r.get("link",""),
            r.get("image_url","") or r.get("image","")
        ]
        combined = " ".join([c for c in combined_fields if c]).lower()
        score = combined.count(topic_l)
        if score == 0:
            # small bonus if the topic appears as substring in headline
            if topic_l in (r.get("headline","").lower()):
                score += 1
        if score > 0:
            d = parse_date_safe(r.get("date","") or r.get("published","") or "")
            # prefer rows with both higher score and more recent date
            date_score = 0
            if d:
                days_old = (datetime.utcnow().date() - d).days
                date_score = max(0, 30 - days_old)  # prefer more recent within 30d window
            combined_score = score * 10 + date_score
            if combined_score > best_score:
                best_score = combined_score
                best = r
                best_date = r.get("date","")
    if best:
        return {
            "headline": best.get("headline",""),
            "news": best.get("news",""),
            "link": best.get("link",""),
            "image_url": best.get("image_url","") or best.get("image",""),
            "date": best_date
        }
    return None

# ----------------------------
# Extract article text from link (if needed)
# ----------------------------
def extract_article_text(url: str, max_chars: int = 8000):
    try:
        headers = {"User-Agent": "Mozilla/5.0 (compatible; NovaBot/1.0)"}
        resp = requests.get(url, timeout=12, headers=headers)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
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
                if t and len(t) > 30:
                    texts.append(t)
        content = "\n\n".join(texts).strip()
        if not content:
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
# Gemini summarizer wrapper
# ----------------------------
def call_gemini_summarize(article_text: str, headline: str, user_message: str):
    try:
        prompt = (
            "You are Nova, a helpful news reporter. Summarize the article below in 2-4 short paragraphs, "
            "in clear, factual language. Then propose one concise follow-up action the user might want next (tailored to the article). "
            "Keep it natural.\n\n"
            f"User request: {user_message}\n\n"
            f"Headline: {headline}\n\n"
            f"Article:\n{article_text}\n\n"
            "Summary:"
        )
        model = genai.GenerativeModel(MODEL_NAME)
        resp = model.generate_content(prompt, max_output_tokens=512)
        text = getattr(resp, "text", None)
        if not text:
            print("Gemini returned empty response:", resp)
            return "⚠️ Sorry — Gemini returned no summary. Check your API key/quota."
        return text.strip()
    except Exception as e:
        print("Gemini call error:", e)
        traceback.print_exc()
        return "⚠️ Sorry — an error occurred while generating the summary."

# ----------------------------
# Main endpoint (SHEET ONLY, with fallback search)
# ----------------------------
@app.post("/chat")
def chat(req: ChatReq):
    message = (req.message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="message required")

    # derive topic (strip helper words)
    topic = re.sub(r"\b(latest|news)\b", "", message, flags=re.I).strip()
    if not topic:
        topic = message.strip()

    # Load sheet rows
    rows = fetch_sheet_rows(SHEET_ID, SHEET_GID)
    if not rows:
        return {"reply": "⚠️ Could not read the Google Sheet. Ensure the sheet is public (Anyone with link can view) and SHEET_ID / SHEET_GID are set in Render env."}

    # 1) Strict recent search
    sheet_hit = find_recent_sheet_news(topic, rows, days_limit=DAYS_LIMIT)

    # 2) If not found, fallback to best match in any date
    if not sheet_hit:
        sheet_hit = find_best_match_any_date(topic, rows)
        if sheet_hit:
            # flag it's an older match (optional) — we just proceed
            print("Fallback to older match:", sheet_hit.get("headline"), "date:", sheet_hit.get("date"))

    if not sheet_hit:
        return {"reply": f"Sorry — no items found in the Google Sheet for '{topic}'. Please add the news or try a different query."}

    headline = sheet_hit.get("headline") or topic
    article_text = sheet_hit.get("news") or ""
    link = sheet_hit.get("link") or ""

    # If sheet lacks 'news' text but has a link, try to fetch it
    if not article_text and link:
        article_text = extract_article_text(link)

    if not article_text:
        return {"reply": f"Found a sheet row ('{headline}') but it lacks article text and I couldn't fetch the page. Please add article text to the 'news' column or provide a readable link."}

    # Summarize with Gemini
    summary = call_gemini_summarize(article_text, headline, message)
    return {"reply": summary, "headline": headline, "link": link, "date": sheet_hit.get("date")}

# health
@app.get("/")
def root():
    return {"status": "ok", "note": f"DaysLimit={DAYS_LIMIT}"}
