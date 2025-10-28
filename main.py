from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
import requests
import os
from supabase import create_client, Client
from dotenv import load_dotenv

load_dotenv()
app = FastAPI(title="AI Game Builder 🎮")

# === Supabase setup ===
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

# === Gemini setup ===
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-pro:generateContent?key={GEMINI_API_KEY}"

# === Unreal mock automation link ===
UNREAL_AUTOMATION_URL = os.getenv("UNREAL_AUTOMATION_URL", "http://localhost:8081/build")

# -------- Data model ----------
class GamePrompt(BaseModel):
    user_id: str
    prompt: str | None = None
    game_script: str | None = None
    build_type: str = "webgl"  # webgl or apk

# -------- Main route ----------
@app.post("/game")
def create_game(data: GamePrompt):
    try:
        # 1️⃣ Generate Unreal script from Gemini if only prompt is provided
        if data.game_script is None and data.prompt:
            gemini_payload = {
                "contents": [{"parts": [{"text": f"Create Unreal Engine 5 Python script for: {data.prompt}"}]}]
            }
            gemini_resp = requests.post(GEMINI_URL, json=gemini_payload)
            data.game_script = gemini_resp.json()["candidates"][0]["content"]["parts"][0]["text"]

        # 2️⃣ Send script to Unreal automation mock
        unreal_payload = {
            "script": data.game_script,
            "build_type": data.build_type
        }
        unreal_resp = requests.post(UNREAL_AUTOMATION_URL, json=unreal_payload)
        if unreal_resp.status_code != 200:
            raise HTTPException(status_code=500, detail="Unreal build failed")

        unreal_data = unreal_resp.json()
        webgl_url = unreal_data.get("webgl_url")
        apk_url = unreal_data.get("apk_url")

        # 3️⃣ Store build details in Supabase
        supabase.table("games").insert({
            "user_id": data.user_id,
            "prompt": data.prompt,
            "script": data.game_script,
            "webgl_url": webgl_url,
            "apk_url": apk_url
        }).execute()

        # 4️⃣ Return URLs
        return {
            "status": "success",
            "message": "🎮 Game built successfully!",
            "preview_url": webgl_url,
            "download_links": {
                "webgl": webgl_url,
                "apk": apk_url
            }
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
