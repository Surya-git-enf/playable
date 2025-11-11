# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import os, json, sqlite3, traceback
from typing import Optional, List, Dict, Any

# HTTP requests for proxying
import requests

# CORS
from fastapi.middleware.cors import CORSMiddleware

# optional supabase (if installed)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Conversations (list / edit / delete / proxy)")

# Allow CORS from frontend (adjust origins for production)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # change to specific origins in production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------------- CONFIG ----------------
SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()
SQLITE_DB = os.getenv("SQLITE_DB", "nova_cache.db")

# initialize supabase client if possible
supabase = None
if SUPABASE_URL and SUPABASE_KEY and create_client is not None:
    try:
        supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    except Exception as e:
        print("Supabase init error:", e)
        supabase = None

# ---------------- SQLITE fallback ----------------
def init_db(path=SQLITE_DB):
    conn = sqlite3.connect(path, check_same_thread=False)
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS users (
            email TEXT PRIMARY KEY,
            chat_history TEXT DEFAULT '[]',
            created_at TEXT
        )
    """)
    conn.commit()
    return conn

DB = init_db()

# ---------------- Pydantic models ----------------
class GetChatReq(BaseModel):
    email: str
    conversation_name: str

class RenameReq(BaseModel):
    user_email: str
    old_name: str
    new_name: str

class DeleteReq(BaseModel):
    user_email: str
    conversation_name: str

class AppendReq(BaseModel):
    user_email: str
    conversation_name: str
    element_json_string: str  # must be a compact JSON string like "{\"nasa news\":{...}}"

# For proxy endpoint
class ProxyReq(BaseModel):
    link: str
    method: str
    body: Optional[Dict[str, Any]] = {}

# For send_message endpoint (sends to n8n webhook/chat)
class SendMsgReq(BaseModel):
    user_email: str
    message: str
    conversation_name: Optional[str] = ""  # empty string if new

# ---------------- Helpers ----------------
def _normalize_email(e: Optional[str]) -> str:
    return (e or "").strip().lower()

# SQLite helpers (store chat_history as JSON string of array-of-strings)
def sqlite_get_row(email: str) -> Optional[Dict[str, Any]]:
    cur = DB.cursor()
    cur.execute("SELECT email, chat_history FROM users WHERE email = ?", (email,))
    row = cur.fetchone()
    if not row:
        return None
    try:
        hist = json.loads(row[1] or "[]")
    except Exception:
        hist = []
    return {"email": row[0], "chat_history": hist}

def sqlite_ensure_row(email: str):
    if not email:
        return False
    if sqlite_get_row(email) is not None:
        return True
    cur = DB.cursor()
    cur.execute("INSERT INTO users (email, chat_history, created_at) VALUES (?, ?, ?)",
                (email, json.dumps([]), datetime.utcnow().isoformat()+"Z"))
    DB.commit()
    return True

def sqlite_save_history(email: str, history: List[str]):
    cur = DB.cursor()
    cur.execute("UPDATE users SET chat_history = ? WHERE email = ?", (json.dumps(history), email))
    DB.commit()

# Supabase helpers (assume table 'users' with columns 'email' and 'chat_history')
def supabase_get_history(email: str) -> Optional[List[str]]:
    if not supabase:
        return None
    try:
        res = supabase.table("users").select("chat_history").eq("email", email).execute()
        if res and getattr(res, "data", None):
            if isinstance(res.data, list) and len(res.data) > 0:
                h = res.data[0].get("chat_history", []) or []
                return h
        return []
    except Exception as e:
        print("supabase_get_history error:", e)
        return None

def supabase_upsert_history(email: str, history: List[str]) -> bool:
    if not supabase:
        return False
    try:
        supabase.table("users").upsert({"email": email, "chat_history": history}).execute()
        return True
    except Exception as e:
        print("supabase_upsert_history error:", e)
        return False

# Unified read/write helpers (prefer supabase, fall back to sqlite)
def read_chat_history(email: str) -> List[str]:
    email = _normalize_email(email)
    # try supabase first
    if supabase:
        try:
            h = supabase_get_history(email)
            if h is not None:
                return h if isinstance(h, list) else []
        except Exception:
            pass
    # fallback to sqlite
    sqlite_ensure_row(email)
    row = sqlite_get_row(email)
    return row.get("chat_history", []) if row else []

def write_chat_history(email: str, history: List[str]) -> bool:
    email = _normalize_email(email)
    ok = True
    # try supabase upsert if available
    if supabase:
        try:
            ok = supabase_upsert_history(email, history)
        except Exception as e:
            print("write supabase failed:", e)
            ok = False
    # always ensure sqlite is updated so frontend devs can inspect local DB
    try:
        sqlite_ensure_row(email)
        sqlite_save_history(email, history)
    except Exception as e:
        print("write sqlite failed:", e)
        ok = False
    return ok

# helper to extract conversation names from a chat_history array-of-strings
def extract_conversation_names(chat_history: List[str]) -> List[str]:
    names = []
    for el in chat_history:
        try:
            if isinstance(el, str):
                parsed = json.loads(el)
            elif isinstance(el, dict):
                parsed = el
            else:
                continue
            # pick first key
            if isinstance(parsed, dict):
                for k in parsed.keys():
                    names.append(str(k))
                    break
        except Exception:
            continue
    return names

# find index of element whose parsed object contains conv_name key (exact match)
def find_history_index_by_name(chat_history: List[str], conv_name: str) -> int:
    for i, el in enumerate(chat_history):
        try:
            parsed = json.loads(el) if isinstance(el, str) else el
            if isinstance(parsed, dict) and conv_name in parsed:
                return i
        except Exception:
            continue
    return -1

# safe rename within a single JSON-string element: replace key while preserving messages object
def rename_in_element_string(el_str: str, old_name: str, new_name: str) -> str:
    try:
        parsed = json.loads(el_str)
        if old_name in parsed:
            parsed[new_name] = parsed.pop(old_name)
            return json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
    except Exception:
        pass
    return el_str

# ---------------- Endpoints ----------------

@app.get("/list_chats")
def list_chats(email: Optional[str] = None):
    if not email:
        raise HTTPException(status_code=400, detail="email query param required")
    email = _normalize_email(email)
    hist = read_chat_history(email) or []
    names = extract_conversation_names(hist)
    return {"conversations": names, "count": len(names)}

@app.post("/get_chat")
def get_chat(req: GetChatReq):
    email = _normalize_email(req.email)
    conv = (req.conversation_name or "").strip()
    if not email or not conv:
        raise HTTPException(status_code=400, detail="email and conversation_name required")
    hist = read_chat_history(email) or []
    idx = find_history_index_by_name(hist, conv)
    matched = None
    if idx >= 0:
        matched = hist[idx]
    return {"chat_history": hist, "conversation": matched}

@app.post("/rename_chat")
def rename_chat(req: RenameReq):
    email = _normalize_email(req.user_email)
    old = (req.old_name or "").strip()
    new = (req.new_name or "").strip()
    if not email or not old or not new:
        raise HTTPException(status_code=400, detail="user_email, old_name, new_name required")
    hist = read_chat_history(email) or []
    changed = False
    new_hist = []
    for el in hist:
        try:
            if isinstance(el, str):
                parsed = json.loads(el)
            elif isinstance(el, dict):
                parsed = el
            else:
                new_hist.append(el)
                continue
            if isinstance(parsed, dict) and old in parsed:
                parsed[new] = parsed.pop(old)
                new_el = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
                new_hist.append(new_el)
                changed = True
            else:
                if isinstance(el, str):
                    new_hist.append(el)
                else:
                    new_hist.append(json.dumps(parsed, separators=(",", ":"), ensure_ascii=False))
        except Exception:
            new_hist.append(el)
    if changed:
        ok = write_chat_history(email, new_hist)
        if not ok:
            raise HTTPException(status_code=500, detail="failed to save renamed history")
        return {"ok": True, "message": f"Renamed '{old}' to '{new}'", "chat_history": new_hist}
    return {"ok": False, "message": f"Conversation '{old}' not found", "chat_history": hist}

@app.post("/delete_chat")
def delete_chat(req: DeleteReq):
    email = _normalize_email(req.user_email)
    conv = (req.conversation_name or "").strip()
    if not email or not conv:
        raise HTTPException(status_code=400, detail="user_email and conversation_name required")
    hist = read_chat_history(email) or []
    new_hist = []
    removed = False
    for el in hist:
        try:
            parsed = json.loads(el) if isinstance(el, str) else el
            if isinstance(parsed, dict) and conv in parsed:
                removed = True
                continue
            if isinstance(el, str):
                new_hist.append(el)
            else:
                new_hist.append(json.dumps(parsed, separators=(",", ":"), ensure_ascii=False))
        except Exception:
            new_hist.append(el)
    if removed:
        ok = write_chat_history(email, new_hist)
        if not ok:
            raise HTTPException(status_code=500, detail="failed to persist deletion")
        return {"ok": True, "message": f"Deleted conversation '{conv}'", "chat_history": new_hist}
    return {"ok": False, "message": f"Conversation '{conv}' not found", "chat_history": hist}

@app.post("/append_chat")
def append_chat(req: AppendReq):
    email = _normalize_email(req.user_email)
    conv = (req.conversation_name or "").strip()
    elstr = (req.element_json_string or "").strip()
    if not email or not conv or not elstr:
        raise HTTPException(status_code=400, detail="user_email, conversation_name, element_json_string required")
    try:
        parsed = json.loads(elstr)
        if not isinstance(parsed, dict) or conv not in parsed:
            raise HTTPException(status_code=400, detail="element_json_string must parse to an object and contain the conversation_name as top-level key")
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"element_json_string is not valid JSON: {e}")
    hist = read_chat_history(email) or []
    hist.append(elstr)
    ok = write_chat_history(email, hist)
    if not ok:
        raise HTTPException(status_code=500, detail="failed to persist appended element")
    return {"ok": True, "message": "Appended element", "chat_history": hist}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat()+"Z"}

# ---------------- New endpoints (proxy + send_message) ----------------

# Proxy: forward arbitrary request through the n8n proxy webhook/request
N8N_PROXY_URL = "https://n8n-8ush.onrender.com/webhook/request"
@app.post("/proxy_request")
def proxy_request(req: ProxyReq):
    """
    Forward a request through the provided n8n proxy endpoint.
    Expects JSON:
    {
      "link": "https://playable-36ab.onrender.com/(endpoint)",
      "method": "GET|POST|PUT|DELETE",
      "body": { ... }
    }
    This endpoint will POST to https://n8n-8ush.onrender.com/webhook/request
    with the same shape in its JSON body, and return the proxy response.
    """
    try:
        payload = {
            "link": req.link,
            "method": (req.method or "GET").upper(),
            "body": req.body or {}
        }
        # forward via n8n proxy
        r = requests.post(N8N_PROXY_URL, json=payload, timeout=30)
        # try to return JSON if possible
        try:
            return {"ok": True, "status_code": r.status_code, "response": r.json()}
        except Exception:
            return {"ok": True, "status_code": r.status_code, "text": r.text}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))

# Send message: directly send a message payload to n8n webhook chat URL
N8N_CHAT_URL = "https://n8n-8ush.onrender.com/webhook/chat"
@app.post("/send_message")
def send_message(req: SendMsgReq):
    """
    Send a message to the external n8n webhook/chat.
    Body:
    {"user_email":"...","message":"...","conversation_name":""}
    conversation_name can be empty string when it's a 'new' conversation.
    """
    email = _normalize_email(req.user_email)
    if not email:
        raise HTTPException(status_code=400, detail="user_email required")
    payload = {
        "userEmail": email,
        "message": req.message or "",
        # pass empty string if new conversation (as requested)
        "conversationName": req.conversation_name or ""
    }
    try:
        r = requests.post(N8N_CHAT_URL, json=payload, timeout=30)
        try:
            return {"ok": True, "status_code": r.status_code, "response": r.json()}
        except Exception:
            return {"ok": True, "status_code": r.status_code, "text": r.text}
    except Exception as e:
        traceback.print_exc()
        raise HTTPException(status_code=500, detail=str(e))
