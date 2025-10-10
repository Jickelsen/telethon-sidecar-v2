
import asyncio
import re
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.functions.contacts import ResolvePhoneRequest

from config import settings
import os

app = FastAPI(title="Telethon Sidecar", version="1.0.0")

# --- Auth dependency ---
async def require_token(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != settings.auth_token:
        raise HTTPException(status_code=403, detail="Invalid token")

# --- Telethon client ---
client: Optional[TelegramClient] = None

async def get_client() -> TelegramClient:
    global client
    if client is None:
        os.makedirs(settings.session_dir, exist_ok=True)
        session_path = os.path.join(settings.session_dir, settings.session_name)
        client = TelegramClient(
            session=session_path,
            api_id=settings.api_id,
            api_hash=settings.api_hash,
            connection_retries=5,
            auto_reconnect=True,
            request_retries=5,
            timeout=settings.read_timeout,
        )
        await client.connect()
        if not await client.is_user_authorized():
            raise HTTPException(status_code=401, detail="Session not authorized. Initialize session first.")
    return client

@app.on_event("shutdown")
async def on_shutdown():
    global client
    if client:
        await client.disconnect()

@app.get("/health")
async def health():
    return {"status": "ok"}

# --- Models ---
class ResolvePhoneBody(BaseModel):
    phone: str

class SendBotBody(BaseModel):
    bot_username: Optional[str] = None
    text: str
    wait_seconds: Optional[int] = None

class SearchViaBotBody(BaseModel):
    phone: str
    bot_username: Optional[str] = None
    message_template: str = "{phone}"
    wait_seconds: Optional[int] = None

# --- Helpers ---
PHONE_RE = re.compile(r"[^0-9+]")

def norm_phone(p: str) -> str:
    p = p.strip()
    if not p:
        return p
    p = PHONE_RE.sub("", p)
    if p and not p.startswith("+"):
        return p
    return p

# --- Endpoints ---
@app.post("/resolve_phone", dependencies=[Depends(require_token)])
async def resolve_phone(body: ResolvePhoneBody):
    cl = await get_client()
    phone = norm_phone(body.phone)
    try:
        res = await cl(ResolvePhoneRequest(phone=phone))
        user = res.users[0] if res.users else None
        if not user:
            raise HTTPException(status_code=404, detail="No user found for phone")
        return {
            "id": user.id,
            "username": getattr(user, "username", None),
            "first_name": getattr(user, "first_name", None),
            "last_name": getattr(user, "last_name", None),
            "phone": phone,
        }
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")

@app.post("/bot/send", dependencies=[Depends(require_token)])
async def bot_send(body: SendBotBody):
    cl = await get_client()

    bot = (body.bot_username or settings.bot_username or "").strip()

    # --- Validate username early ---
    if not bot or not re.fullmatch(r"[a-zA-Z][\w\d]{3,30}[a-zA-Z\d]", bot.lstrip("@")):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bot username '{bot}'. It must match [a-zA-Z][\\w\\d]{{3,30}}[a-zA-Z\\d]."
        )

    bot = bot.lstrip("@")

    wait = body.wait_seconds or settings.wait_after_send

    try:
        await cl.send_message(entity=bot, message=body.text)
        if wait <= 0:
            return {"sent": True, "reply": None}
        evt = await cl.wait_for(events.NewMessage(chats=bot), timeout=wait)
        reply_text = evt.message.message if evt and evt.message else None
        return {"sent": True, "reply": reply_text}
    except asyncio.TimeoutError:
        return {"sent": True, "reply": None, "error": "timeout"}
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")

@app.post("/search_phone_via_bot", dependencies=[Depends(require_token)])
async def search_phone_via_bot(body: SearchViaBotBody):
    cl = await get_client()

    bot = (body.bot_username or settings.bot_username or "").strip()

    # --- Validate username early ---
    if not bot or not re.fullmatch(r"[a-zA-Z][\w\d]{3,30}[a-zA-Z\d]", bot.lstrip("@")):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid bot username '{bot}'. It must match [a-zA-Z][\\w\\d]{{3,30}}[a-zA-Z\\d]."
        )

    bot = bot.lstrip("@")

    wait = body.wait_seconds or settings.wait_after_send

    # Try to resolve phone (best-effort)
    try:
        await cl(ResolvePhoneRequest(phone=norm_phone(body.phone)))
    except Exception:
        pass

    text = body.message_template.format(phone=norm_phone(body.phone))
    try:
        await cl.send_message(entity=bot, message=text)
        evt = await cl.wait_for(events.NewMessage(chats=bot), timeout=wait)
        reply_text = evt.message.message if evt and evt.message else None
        return {"ok": True, "query": text, "reply": reply_text}
    except asyncio.TimeoutError:
        return {"ok": False, "query": text, "reply": None, "error": "timeout"}
    except FloodWaitError as e:
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")
