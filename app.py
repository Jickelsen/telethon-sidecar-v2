import asyncio
import os
import re
import logging
from typing import Optional

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.functions.contacts import ResolvePhoneRequest

from config import settings

# -----------------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------------
LOG_FORMAT = (
    "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
)
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("telethon-sidecar")

# -----------------------------------------------------------------------------
# App init
# -----------------------------------------------------------------------------
app = FastAPI(title="Telethon Sidecar", version="1.2.0")

# -----------------------------------------------------------------------------
# Auth dependency
# -----------------------------------------------------------------------------
async def require_token(authorization: Optional[str] = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing Bearer token")
    token = authorization.split(" ", 1)[1].strip()
    if token != settings.auth_token:
        raise HTTPException(status_code=403, detail="Invalid token")

# -----------------------------------------------------------------------------
# Telethon client setup
# -----------------------------------------------------------------------------
client: Optional[TelegramClient] = None

async def get_client() -> TelegramClient:
    """Get or initialize a connected TelegramClient, reconnect if needed."""
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

    # Ensure connected
    if not client.is_connected():
        try:
            await client.connect()
            logger.info("Telethon client reconnected")
        except Exception as e:
            logger.exception(f"Failed to reconnect Telethon: {e}")
            raise HTTPException(status_code=500, detail="Failed to reconnect Telegram client")

    # Ensure authorized
    if not await client.is_user_authorized():
        raise HTTPException(
            status_code=401,
            detail="Session not authorized. Run init_session.py first.",
        )

    return client

@app.on_event("shutdown")
async def on_shutdown():
    global client
    if client:
        await client.disconnect()
        logger.info("Telethon client disconnected")

# -----------------------------------------------------------------------------
# Pydantic models
# -----------------------------------------------------------------------------
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

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
PHONE_RE = re.compile(r"[^0-9+]")

def norm_phone(p: str) -> str:
    p = p.strip()
    if not p:
        return p
    p = PHONE_RE.sub("", p)
    if p and not p.startswith("+"):
        return "+" + p
    return p

def validate_bot_username(bot: str):
    """Normalize and validate a Telegram bot username."""
    if not bot:
        raise HTTPException(status_code=400, detail="Missing bot_username")

    clean = bot.strip().lstrip("@")

    # Telegram bot usernames must be 5–32 chars and end with 'bot' (case-insensitive)
    if not (5 <= len(clean) <= 32 and clean.lower().endswith("bot")):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid bot username '{bot}'. "
                "Telegram bot usernames must be 5–32 characters and end with 'bot'."
            ),
        )

    return clean

async def send_and_wait_reply(client, bot: str, text: str, timeout: int = 10):
    """
    Send a message to a bot and wait for its next reply within timeout seconds.
    Returns the reply text or None on timeout.
    """
    reply_text = None
    waiter = asyncio.Event()

    @client.on(events.NewMessage(chats=bot))
    async def handler(event):
        nonlocal reply_text
        reply_text = event.message.message
        logger.info("Bot reply received", extra={"bot": bot, "reply": reply_text})
        try:
            client.remove_event_handler(handler, events.NewMessage(chats=bot))
        except Exception:
            pass
        waiter.set()

    logger.info("Sending message", extra={"bot": bot, "text": text})
    await client.send_message(bot, text)
    try:
        await asyncio.wait_for(waiter.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        logger.warning("No reply within timeout", extra={"bot": bot, "timeout": timeout})
    finally:
        try:
            client.remove_event_handler(handler, events.NewMessage(chats=bot))
        except Exception:
            pass

    return reply_text

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(
        f"HTTP {request.method} {request.url.path}",
        extra={"client": request.client.host}
    )
    try:
        response = await call_next(request)
        logger.info(
            f"{request.method} {request.url.path} -> {response.status_code}",
            extra={"client": request.client.host}
        )
        return response
    except Exception as e:
        logger.exception(f"Unhandled error on {request.url.path}: {e}")
        raise

@app.get("/health")
async def health():
    return {"status": "ok"}

@app.post("/resolve_phone", dependencies=[Depends(require_token)])
async def resolve_phone(body: ResolvePhoneBody):
    cl = await get_client()
    phone = norm_phone(body.phone)
    logger.info("Resolving phone", extra={"phone": phone})
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
        logger.warning("Flood wait", extra={"seconds": e.seconds})
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")

@app.post("/bot/send", dependencies=[Depends(require_token)])
async def bot_send(body: SendBotBody):
    cl = await get_client()
    bot = validate_bot_username(body.bot_username or settings.bot_username)
    wait = body.wait_seconds or settings.wait_after_send
    try:
        reply_text = await send_and_wait_reply(cl, bot, body.text, wait)
        return {"sent": True, "reply": reply_text}
    except FloodWaitError as e:
        logger.warning("Flood wait", extra={"seconds": e.seconds})
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")
    except Exception as e:
        logger.exception("Error in /bot/send", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search_phone_via_bot", dependencies=[Depends(require_token)])
async def search_phone_via_bot(body: SearchViaBotBody):
    cl = await get_client()
    bot = validate_bot_username(body.bot_username or settings.bot_username)
    wait = body.wait_seconds or settings.wait_after_send
    phone = norm_phone(body.phone)
    text = body.message_template.format(phone=phone)
    logger.info("search_phone_via_bot", extra={"bot": bot, "phone": phone, "msg_text": text})
    # Try to resolve phone (optional)
    try:
        await cl(ResolvePhoneRequest(phone=phone))
    except Exception:
        pass

    try:
        reply_text = await send_and_wait_reply(cl, bot, text, wait)
        logger.info("Bot interaction complete", extra={"bot": bot, "reply": reply_text})
        return {"ok": True, "query": text, "reply": reply_text}
    except asyncio.TimeoutError:
        logger.warning("Timeout waiting for bot reply", extra={"bot": bot, "timeout": wait})
        return {"ok": False, "query": text, "reply": None, "error": "timeout"}
    except FloodWaitError as e:
        logger.warning("Flood wait", extra={"seconds": e.seconds})
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")
    except Exception as e:
        logger.exception("Error in /search_phone_via_bot", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

