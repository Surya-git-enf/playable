from fastapi import FastAPI
from pydantic import BaseModel
from fastapi.responses import JSONResponse, HTMLResponse

app = FastAPI()

# In-memory game storage
GAMES = {}

class ScriptInput(BaseModel):
    script: str

@app.post("/build")
async def build_game(data: ScriptInput):
    game_name = data.script.replace(" ", "_").lower()[:25]

    html_content = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <title>{data.script}</title>
        <style>
            body {{
                margin: 0;
                height: 100vh;
                display: flex;
                align-items: center;
                justify-content: center;
                background: linear-gradient(135deg, #1a1a1a, #444);
                color: white;
                font-family: Arial;
            }}
            .shape {{
                width: 100px;
                height: 100px;
                background: {'blue' if 'blue' in data.script.lower() else 'red'};
                border-radius: {'50%' if 'circle' in data.script.lower() else '0'};
                animation: spin {'1s' if 'fast' in data.script.lower() else '3s'} linear infinite;
            }}
            @keyframes spin {{
                0% {{ transform: rotate(0deg); }}
                100% {{ transform: rotate(360deg); }}
            }}
        </style>
    </head>
    <body>
        <div>
            <h2>🎮 Game Preview</h2>
            <div class="shape"></div>
            <p>{data.script}</p>
        </div>
    </body>
    </html>
    """

    # Store the game in memory
    GAMES[game_name] = html_content

    # Generate links
    webgl_url = f"/preview/{game_name}"
    apk_url = f"/download/{game_name}.apk"

    return JSONResponse({
        "webgl_url": webgl_url,
        "apk_url": apk_url
    })

@app.get("/preview/{name}")
async def preview_game(name: str):
    if name not in GAMES:
        return HTMLResponse("<h2>❌ Game not found!</h2>", status_code=404)
    return HTMLResponse(GAMES[name])

@app.get("/download/{apk_name}")
async def download_apk(apk_name: str):
    return JSONResponse({
        "message": f"✅ APK for {apk_name} generated (placeholder).",
        "download_hint": "Real Unreal .apk build integration coming soon."
    })
