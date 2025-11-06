from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from sqlalchemy import create_engine, Column, Integer, String, JSON
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
import asyncio
import aiohttp
import feedparser
import json

# -----------------------------------
# Database setup
# -----------------------------------
DATABASE_URL = "postgresql://postgres:password@localhost:5432/nova_chat"

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(bind=engine)
Base = declarative_base()


class User(Base):
    __tablename__ = "users"
    id = Column(Integer, primary_key=True, index=True)
    email = Column(String, unique=True)
    chat_history = Column(JSON, default=[])


Base.metadata.create_all(bind=engine)

# -----------------------------------
# FastAPI setup
# -----------------------------------
app = FastAPI(title="Nova AI Chat Agent")

@app.get("/")
def home():
    return{"message":"hlo , i am Nova how can I help you 🙏 "}

# -----------------------------------
# Pydantic models
# -----------------------------------
class ChatRequest(BaseModel):
    user_email: str
    conversation_name: str = ""
    message: str
    agent: str


class GetChatRequest(BaseModel):
    email: str
    conversation_name: str


class RenameChatRequest(BaseModel):
    email: str
    old_name: str
    new_name: str


class DeleteChatRequest(BaseModel):
    email: str
    conversation_name: str


# -----------------------------------
# Helper Functions
# -----------------------------------
async def fetch_rss_async(url, timeout=8):
    """Fetch RSS feed asynchronously with timeout"""
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=timeout) as response:
                text = await response.text()
                feed = feedparser.parse(text)
                return feed.entries[:5]
    except Exception:
        return []


async def get_relevant_articles_async(message: str):
    """Select RSS source dynamically based on message topic"""
    rss_sources = {
        "space": "https://www.nasa.gov/rss/dyn/breaking_news.rss",
        "technology": "https://feeds.feedburner.com/TechCrunch/",
        "entertainment": "https://www.hollywoodreporter.com/t/feed/",
        "netflix": "https://www.whats-on-netflix.com/feed/",
        "sports": "https://www.espn.com/espn/rss/news",
        "politics": "https://feeds.a.dj.com/rss/RSSPolitics.xml",
        "science": "https://www.sciencenews.org/feed",
        "movies": "https://www.empireonline.com/feeds/all/",
        "business": "https://www.businessinsider.in/rssfeeds/2147477983.cms",
    }

    topic = None
    for key in rss_sources:
        if key in message.lower():
            topic = key
            break

    if not topic:
        return None

    entries = await fetch_rss_async(rss_sources[topic])
    return entries


def summarize_message(message: str) -> str:
    """Simple summarizer for new conversation names"""
    words = message.split()
    if len(words) <= 3:
        return message.title()
    return " ".join(words[:3]).title()


def get_username(email: str) -> str:
    return email.split("@")[0]


# -----------------------------------
# Routes
# -----------------------------------
@app.post("/chat")
async def chat_with_nova(data: ChatRequest):
    db = SessionLocal()
    user = db.query(User).filter_by(email=data.user_email).first()
    if not user:
        # auto create user if not exists
        user = User(email=data.user_email, chat_history=[])
        db.add(user)
        db.commit()
        db.refresh(user)

    articles = await get_relevant_articles_async(data.message)

    if articles:
        reply = f"Here are the latest updates, {data.agent} 🤖:\n\n"
        for a in articles:
            reply += f"📰 {a.title}\n🔗 {a.link}\n\n"
    else:
        reply = f"Hey {get_username(data.user_email)} 👋, glad to chat with you!"

    convo_name = data.conversation_name.strip() or summarize_message(data.message)
    chat_history = user.chat_history or []

    found = False
    for convo in chat_history:
        if convo_name in convo:
            convo[convo_name][get_username(data.user_email)] = data.message
            convo[convo_name][data.agent] = reply
            found = True
            break

    if not found:
        chat_history.append({
            convo_name: {
                get_username(data.user_email): data.message,
                data.agent: reply
            }
        })

    user.chat_history = chat_history
    db.commit()

    return {
        "conversation_name": convo_name,
        "reply": reply,
        "chat_history": chat_history
    }


@app.post("/get_chat")
def get_chat(data: GetChatRequest):
    db = SessionLocal()
    user = db.query(User).filter_by(email=data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for convo in user.chat_history:
        if data.conversation_name in convo:
            return convo

    raise HTTPException(status_code=404, detail="Conversation not found")


@app.post("/rename_chat")
def rename_chat(data: RenameChatRequest):
    db = SessionLocal()
    user = db.query(User).filter_by(email=data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    for convo in user.chat_history:
        if data.old_name in convo:
            convo[data.new_name] = convo.pop(data.old_name)
            db.commit()
            return {"message": "Chat renamed successfully", "chat_history": user.chat_history}

    raise HTTPException(status_code=404, detail="Old chat name not found")


@app.post("/delete_chat")
def delete_chat(data: DeleteChatRequest):
    db = SessionLocal()
    user = db.query(User).filter_by(email=data.email).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    updated = [c for c in user.chat_history if data.conversation_name not in c]
    user.chat_history = updated
    db.commit()

    return {"message": "Chat deleted successfully", "chat_history": user.chat_history}
