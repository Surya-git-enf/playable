# 🎮 AI Game Builder (Prompt → Unreal → Playable Build)

## 💡 Overview
This FastAPI app takes a user prompt, generates an Unreal Engine script using Gemini AI, sends it to a mock Unreal automation server, stores output in Supabase, and returns WebGL/APK URLs.

---

## 🚀 Deploy on Render
1. Fork or upload this repo to GitHub.
2. Go to [Render.com](https://render.com) → New Web Service.
3. Connect your repo.
4. Add environment variables:
   - GEMINI_API_KEY
   - SUPABASE_URL
   - SUPABASE_KEY
   - UNREAL_AUTOMATION_URL
5. Start command:
   ```bash
   uvicorn main:app --host 0.0.0.0 --port 8080
