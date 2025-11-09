# main.py
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from datetime import datetime
import os, json, sqlite3, traceback
from typing import Optional, List, Dict, Any

# optional supabase (if installed)
try:
    from supabase import create_client
except Exception:
    create_client = None

app = FastAPI(title="Nova — Conversations (list / edit / delete)")

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
        # .select("chat_history").eq("email", email).maybe_single() returns wrapper
        res = supabase.table("users").select("chat_history").eq("email", email).execute()
        # res.data is list of rows
        if res and getattr(res, "data", None):
            if isinstance(res.data, list) and len(res.data) > 0:
                h = res.data[0].get("chat_history", []) or []
                # ensure list of strings
                return h
        return []
    except Exception as e:
        print("supabase_get_history error:", e)
        return None

def supabase_upsert_history(email: str, history: List[str]) -> bool:
    if not supabase:
        return False
    try:
        # upsert the row with chat_history array-of-strings
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
                # ensure it's a list
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
    """
    GET /list_chats?email=...
    Returns: {"conversations": ["nasa news", "general_2025...", ...]}
    """
    if not email:
        raise HTTPException(status_code=400, detail="email query param required")
    email = _normalize_email(email)
    # read history
    hist = read_chat_history(email) or []
    names = extract_conversation_names(hist)
    return {"conversations": names, "count": len(names)}

@app.post("/get_chat")
def get_chat(req: GetChatReq):
    """
    POST /get_chat
    body: {"email":"surya@gmail.com", "conversation_name":"nasa news"}
    Returns: {"chat_history": [...]} (full array), and also returns the matched conversation object under 'conversation' for convenience
    """
    email = _normalize_email(req.email)
    conv = (req.conversation_name or "").strip()
    if not email or not conv:
        raise HTTPException(status_code=400, detail="email and conversation_name required")
    hist = read_chat_history(email) or []
    idx = find_history_index_by_name(hist, conv)
    matched = None
    if idx >= 0:
        matched = hist[idx]
    # Return full chat_history (so frontend can update list) and matched element for immediate parsing
    return {"chat_history": hist, "conversation": matched}

@app.post("/rename_chat")
def rename_chat(req: RenameReq):
    """
    POST /rename_chat
    body: {"user_email":"surya@gmail.com", "old_name":"nasa news", "new_name":"nasa_updates"}
    """
    email = _normalize_email(req.user_email)
    old = (req.old_name or "").strip()
    new = (req.new_name or "").strip()
    if not email or not old or not new:
        raise HTTPException(status_code=400, detail="user_email, old_name, new_name required")
    hist = read_chat_history(email) or []
    changed = False
    # We will rename matching elements in-place (preserve order). For each element where old key exists, we replace key.
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
                # unchanged element — append original string (preserve type string)
                if isinstance(el, str):
                    new_hist.append(el)
                else:
                    new_hist.append(json.dumps(parsed, separators=(",", ":"), ensure_ascii=False))
        except Exception:
            # if parse fails, preserve original raw element
            new_hist.append(el)
    if changed:
        ok = write_chat_history(email, new_hist)
        if not ok:
            raise HTTPException(status_code=500, detail="failed to save renamed history")
        return {"ok": True, "message": f"Renamed '{old}' to '{new}'", "chat_history": new_hist}
    return {"ok": False, "message": f"Conversation '{old}' not found", "chat_history": hist}

@app.post("/delete_chat")
def delete_chat(req: DeleteReq):
    """
    POST /delete_chat
    body: {"user_email":"surya@gmail.com", "conversation_name":"nasa news"}
    """
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
                continue  # skip this element (delete)
            # preserve original string form
            if isinstance(el, str):
                new_hist.append(el)
            else:
                new_hist.append(json.dumps(parsed, separators=(",", ":"), ensure_ascii=False))
        except Exception:
            # on parse error, keep the element
            new_hist.append(el)
    if removed:
        ok = write_chat_history(email, new_hist)
        if not ok:
            raise HTTPException(status_code=500, detail="failed to persist deletion")
        return {"ok": True, "message": f"Deleted conversation '{conv}'", "chat_history": new_hist}
    return {"ok": False, "message": f"Conversation '{conv}' not found", "chat_history": hist}

@app.post("/append_chat")
def append_chat(req: AppendReq):
    """
    Optional helper endpoint to append a compact JSON string element to the chat_history array
    body: { user_email, conversation_name, element_json_string }
    element_json_string must be a JSON string representing the conversation element, e.g.
    "{\"nasa news\":{\"messages\":[{\"Surya\":\"hi\"},{\"Nova\":\"reply\"}]}}"
    This endpoint will append the provided string as a new element (not merging).
    """
    email = _normalize_email(req.user_email)
    conv = (req.conversation_name or "").strip()
    elstr = (req.element_json_string or "").strip()
    if not email or not conv or not elstr:
        raise HTTPException(status_code=400, detail="user_email, conversation_name, element_json_string required")
    # validate JSON and that it contains conv as a top-level key
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
