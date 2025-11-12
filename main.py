# app.py (FastAPI) — replace your current file with this
from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel
from datetime import datetime
from typing import Optional, List, Dict, Any
import json, traceback, os

# ---------- Supabase ----------
try:
    from supabase import create_client
except Exception:
    create_client = None

SUPABASE_URL = os.getenv("SUPABASE_URL", "").strip()
SUPABASE_KEY = os.getenv("SUPABASE_KEY", "").strip()

if not SUPABASE_URL or not SUPABASE_KEY or not create_client:
    raise RuntimeError("Supabase credentials missing in environment variables!")

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)

app = FastAPI(title="Nova — Chat History Manager (compatible endpoints)")

# ---------- Helpers ----------
def get_history_raw_row(email: str):
    """Return raw data row or None"""
    try:
        res = supabase.table("users").select("chat_history").eq("email", email).execute()
        if not res or not hasattr(res, "data") or not res.data:
            return None
        return res.data[0]
    except Exception as e:
        print("get_history_raw_row error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error fetching chat history")

def get_history(email: str) -> List[dict]:
    """Fetch chat_history JSONB[] for given user."""
    try:
        row = get_history_raw_row(email)
        if not row:
            return []
        data = row.get("chat_history") or []
        if not data:
            return []
        # Parse any stringified JSON
        parsed = []
        for d in data:
            if isinstance(d, str):
                try:
                    parsed.append(json.loads(d))
                except Exception:
                    continue
            elif isinstance(d, dict):
                parsed.append(d)
        return parsed
    except HTTPException:
        raise
    except Exception as e:
        print("get_history error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error fetching chat history")

def save_history(email: str, history: List[dict]):
    """Save updated chat_history to Supabase."""
    try:
        supabase.table("users").update({"chat_history": history}).eq("email", email).execute()
        return True
    except Exception as e:
        print("save_history error:", e)
        traceback.print_exc()
        raise HTTPException(status_code=500, detail="Error saving chat history")

def extract_email_from_payload(body: dict, query_email: Optional[str]=None) -> Optional[str]:
    if not body:
        return query_email
    return body.get('email') or body.get('user_email') or query_email

def merge_element_into_history(history: List[dict], element: dict) -> List[dict]:
    """
    element example: { "conv_name": { "messages":[ { "User": "hi" } ] } }
    If conv exists, append messages; else add new conv object.
    """
    if not isinstance(element, dict):
        return history
    # For each top-level key in element, merge its messages into history
    for conv_name, conv_obj in element.items():
        incoming_msgs = conv_obj.get("messages", []) if isinstance(conv_obj, dict) else []
        found = False
        for h in history:
            if conv_name in h:
                # ensure list exists
                if "messages" not in h[conv_name] or not isinstance(h[conv_name]["messages"], list):
                    h[conv_name]["messages"] = []
                # append each message (keep it as object)
                h[conv_name]["messages"].extend(incoming_msgs)
                found = True
                break
        if not found:
            # append as new conversation object
            history.append({ conv_name: { "messages": list(incoming_msgs) } })
    return history

# ---------- Endpoints ----------

@app.get("/")
def root():
    return {"message": "Nova Supabase Chat History API 💬"}

# LIST: support GET?email= and POST { email | user_email }
@app.get("/list_chats")
def list_chats_get(email: Optional[str] = None):
    if not email:
        raise HTTPException(status_code=400, detail="Missing email query param")
    history = get_history(email)
    names = [list(conv.keys())[0] for conv in history if isinstance(conv, dict)]
    return {"conversations": names[::-1], "count": len(names), "chat_history": history}

@app.post("/list_chats")
async def list_chats_post(request: Request):
    body = await request.json()
    email = extract_email_from_payload(body)
    if not email:
        raise HTTPException(status_code=400, detail="Missing email")
    history = get_history(email)
    names = [list(conv.keys())[0] for conv in history if isinstance(conv, dict)]
    return {"conversations": names[::-1], "count": len(names), "chat_history": history}

# GET conversation (POST body: { email|user_email, conversation_name })
@app.post("/get_chat")
async def get_chat(request: Request):
    body = await request.json()
    email = extract_email_from_payload(body)
    conv_name = body.get("conversation_name") or body.get("conversationName") or body.get("conversation") or body.get("name")
    if not email or not conv_name:
        raise HTTPException(status_code=400, detail="Missing email or conversation_name")
    history = get_history(email)
    for conv in history:
        if conv_name in conv:
            return {conv_name: conv[conv_name]}
    raise HTTPException(status_code=404, detail="Conversation not found")

# APPEND: accept either "element_json_string" (frontend) or simple fields (sender/message)
@app.post("/append_chat")
async def append_chat(request: Request):
    """
    Accepts:
    - { user_email | email, conversation_name, element_json_string } (frontend default)
    OR
    - { email, conversation_name, sender, message } (older format)
    """
    body = await request.json()
    email = extract_email_from_payload(body)
    conv_name = body.get("conversation_name") or body.get("conversationName") or body.get("conversation")
    if not email or not conv_name:
        raise HTTPException(status_code=400, detail="Missing email or conversation_name")

    history = get_history(email)
    # Path A: element_json_string (stringified JSON)
    if "element_json_string" in body and body["element_json_string"]:
        try:
            element = json.loads(body["element_json_string"])
            history = merge_element_into_history(history, element)
            save_history(email, history)
            return {"ok": True, "message": "Appended (element_json_string)", "chat_history": history}
        except Exception as e:
            print("append_chat parse element error", e)
            traceback.print_exc()
            raise HTTPException(status_code=400, detail="Invalid element_json_string")
    # Path B: sender/message form
    sender = body.get("sender") or body.get("from") or body.get("user")
    message = body.get("message") or body.get("text") or body.get("content")
    if sender is None or message is None:
        raise HTTPException(status_code=400, detail="Missing message or sender")
    # build element and merge
    element = { conv_name: { "messages": [ { sender: message } ] } }
    history = merge_element_into_history(history, element)
    save_history(email, history)
    return {"ok": True, "message": "Appended (sender/message)", "chat_history": history}

# For compatibility: keep /append_message as well
@app.post("/append_message")
async def append_message(request: Request):
    body = await request.json()
    # this is the older "AppendReq" shape (email, conversation_name, sender, message)
    return await append_chat(request)

# RENAME: accepts user_email or email
@app.post("/rename_chat")
async def rename_chat(request: Request):
    body = await request.json()
    email = extract_email_from_payload(body)
    old_name = body.get("old_name") or body.get("oldName")
    new_name = body.get("new_name") or body.get("newName")
    if not email or not old_name or not new_name:
        raise HTTPException(status_code=400, detail="Missing parameters")
    history = get_history(email)
    renamed = False
    for i, conv in enumerate(history):
        if old_name in conv:
            # preserve messages
            history[i] = { new_name: conv[old_name] }
            renamed = True
            break
    if not renamed:
        raise HTTPException(status_code=404, detail="Conversation not found")
    save_history(email, history)
    return {"ok": True, "message": f"Renamed '{old_name}' to '{new_name}'", "chat_history": history}

# DELETE: accepts user_email or email
@app.post("/delete_chat")
async def delete_chat(request: Request):
    body = await request.json()
    email = extract_email_from_payload(body)
    conv_name = body.get("conversation_name") or body.get("conversationName") or body.get("conversation")
    if not email or not conv_name:
        raise HTTPException(status_code=400, detail="Missing email or conversation_name")
    history = get_history(email)
    new_hist = [conv for conv in history if conv_name not in conv]
    if len(new_hist) == len(history):
        raise HTTPException(status_code=404, detail="Conversation not found")
    save_history(email, new_hist)
    return {"ok": True, "message": f"Deleted '{conv_name}'", "chat_history": new_hist}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
