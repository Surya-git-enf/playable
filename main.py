from fastapi import FastAPI, HTTPException
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

# ---------- App ----------
app = FastAPI(title="Nova — Chat History Manager")

# ---------- Models ----------
class EmailReq(BaseModel):
    email: str

class ChatReq(BaseModel):
    email: str
    conversation_name: str

class AppendReq(BaseModel):
    email: str
    conversation_name: str
    sender: str
    message: str

class RenameReq(BaseModel):
    email: str
    old_name: str
    new_name: str

class DeleteReq(BaseModel):
    email: str
    conversation_name: str

# ---------- Helpers ----------
def get_history(email: str) -> List[dict]:
    """Fetch chat_history JSONB[] for given user."""
    try:
        res = supabase.table("users").select("chat_history").eq("email", email).execute()
        data = res.data[0]["chat_history"] if res.data else []
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

# ---------- Endpoints ----------

@app.get("/")
def root():
    return {"message": "Nova Supabase Chat History API 💬"}

@app.post("/list_chats")
def list_chats(req: EmailReq):
    history = get_history(req.email)
    names = [list(conv.keys())[0] for conv in history if isinstance(conv, dict)]
    return {"conversations": names, "count": len(names)}

@app.post("/get_chat")
def get_chat(req: ChatReq):
    history = get_history(req.email)
    for conv in history:
        if req.conversation_name in conv:
            return {req.conversation_name: conv[req.conversation_name]}
    raise HTTPException(status_code=404, detail="Conversation not found")

@app.post("/append_message")
def append_message(req: AppendReq):
    """Append message to a specific conversation."""
    history = get_history(req.email)
    found = False
    for conv in history:
        if req.conversation_name in conv:
            conv[req.conversation_name]["messages"].append({req.sender: req.message})
            found = True
            break
    if not found:
        # Create new conversation if it doesn’t exist
        new_conv = {req.conversation_name: {"messages": [{req.sender: req.message}]}}
        history.append(new_conv)
    save_history(req.email, history)
    return {"ok": True, "message": "Message appended successfully", "chat_history": history}

@app.post("/rename_chat")
def rename_chat(req: RenameReq):
    history = get_history(req.email)
    renamed = False
    for i, conv in enumerate(history):
        if req.old_name in conv:
            history[i] = {req.new_name: conv[req.old_name]}
            renamed = True
            break
    if not renamed:
        raise HTTPException(status_code=404, detail="Conversation not found")
    save_history(req.email, history)
    return {"ok": True, "message": f"Renamed '{req.old_name}' to '{req.new_name}'", "chat_history": history}

@app.post("/delete_chat")
def delete_chat(req: DeleteReq):
    history = get_history(req.email)
    new_hist = [conv for conv in history if req.conversation_name not in conv]
    if len(new_hist) == len(history):
        raise HTTPException(status_code=404, detail="Conversation not found")
    save_history(req.email, new_hist)
    return {"ok": True, "message": f"Deleted '{req.conversation_name}'", "chat_history": new_hist}

@app.get("/health")
def health():
    return {"ok": True, "time": datetime.utcnow().isoformat() + "Z"}
