import asyncio
import os
import re
import logging
from typing import Optional, List, Dict

from fastapi import FastAPI, HTTPException, Header, Depends, Request
from pydantic import BaseModel
from telethon import TelegramClient, events
from telethon.errors.rpcerrorlist import FloodWaitError
from telethon.tl.functions.contacts import ResolvePhoneRequest

from config import settings

# -----------------------------------------------------------------------------
# Logging setup
# -----------------------------------------------------------------------------
LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
logging.basicConfig(level=logging.INFO, format=LOG_FORMAT)
logger = logging.getLogger("telethon-sidecar")

# -----------------------------------------------------------------------------
# App init
# -----------------------------------------------------------------------------
app = FastAPI(title="Telethon Sidecar", version="1.3.0")

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
            logger.info("Telethon client connected/reconnected")
        except Exception as e:
            logger.exception(f"Failed to connect Telethon: {e}")
            raise HTTPException(status_code=500, detail="Failed to connect Telegram client")

    # Ensure authorized
    if not await client.is_user_authorized():
        raise HTTPException(
            status_code=401,
            detail="Session not authorized. Run init_session.py first.",
        )

    return client

@app.on_event("startup")
async def start_heartbeat():
    async def heartbeat():
        global client
        while True:
            await asyncio.sleep(300)  # every 5 minutes
            if client and not client.is_connected():
                try:
                    await client.connect()
                    logger.info("Reconnected in heartbeat")
                except Exception as e:
                    logger.warning(f"Heartbeat reconnect failed: {e}")
    asyncio.create_task(heartbeat())

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
    # Collect-mode parameters
    wait_seconds: Optional[int] = None       # overall timeout (total wall time)
    idle_seconds: Optional[int] = None       # quiet period to stop after last msg
    max_messages: Optional[int] = None       # safety cap

class SearchViaBotBody(BaseModel):
    phone: str
    bot_username: Optional[str] = None
    message_template: str = "{phone}"
    # Collect-mode parameters
    wait_seconds: Optional[int] = None
    idle_seconds: Optional[int] = None
    max_messages: Optional[int] = None

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

def validate_bot_username(bot: str) -> str:
    """Normalize and validate a Telegram bot username."""
    if not bot:
        raise HTTPException(status_code=400, detail="Missing bot_username")

    clean = bot.strip().lstrip("@")
    # Telegram bot usernames must be 5–32 chars and end with 'bot' (case-insensitive).
    if not (5 <= len(clean) <= 32 and clean.lower().endswith("bot")):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid bot username '{bot}'. "
                "Telegram bot usernames must be 5–32 characters and end with 'bot'."
            ),
        )
    return clean  # Telethon accepts without @

async def send_and_collect_replies(
    client: TelegramClient,
    bot: str,
    text: str,
    overall_timeout: int = 20,
    idle_timeout: int = 5,
    max_messages: int = 20,
) -> List[Dict[str, str]]:
    """
    Send `text` to `bot` and collect all replies until:
      - no new messages arrive within `idle_timeout` seconds, OR
      - `overall_timeout` elapses, OR
      - `max_messages` collected.

    Returns: [{"text": "...", "date": "..."}]
    """
    messages: List[Dict[str, str]] = []
    queue: asyncio.Queue = asyncio.Queue()

    # Event handler to push incoming bot messages into a queue
    @client.on(events.NewMessage(chats=bot))
    async def handler(event):
        try:
            await queue.put(event)
        except Exception:
            # don't let handler crash
            pass

    # Resolve entity early for clear error if bot doesn't exist
    try:
        _ = await client.get_entity(bot)
    except Exception as e:
        logger.error("Bot lookup failed", extra={"bot": bot, "error": str(e)})
        try:
            client.remove_event_handler(handler, events.NewMessage(chats=bot))
        except Exception:
            pass
        raise HTTPException(status_code=400, detail=f"Cannot resolve bot username '{bot}'. Make sure it exists.")

    logger.info("Sending message", extra={"bot": bot, "msg_text": text})
    await client.send_message(bot, text)

    loop = asyncio.get_event_loop()
    deadline = loop.time() + max(1, overall_timeout)

    try:
        # Keep collecting until idle timeout or overall timeout or message cap
        while len(messages) < max_messages:
            remaining_overall = deadline - loop.time()
            if remaining_overall <= 0:
                break

            try:
                # Wait up to idle_timeout (but never beyond overall timeout)
                event = await asyncio.wait_for(queue.get(), timeout=min(idle_timeout, remaining_overall))
                msg_text = event.message.message if event and event.message else ""
                msg_date = event.message.date.isoformat() if event and event.message and event.message.date else None
                messages.append({"text": msg_text, "date": msg_date})
                logger.info("Bot reply received", extra={"bot": bot, "count": len(messages)})
                # After each message, loop again to see if more messages arrive within idle window
            except asyncio.TimeoutError:
                # Quiet period hit: stop collecting
                logger.info("Idle period reached; stopping collection", extra={"bot": bot, "idle": idle_timeout})
                break
    finally:
        # Always remove handler
        try:
            client.remove_event_handler(handler, events.NewMessage(chats=bot))
        except Exception:
            pass

    return messages

# -----------------------------------------------------------------------------
# Routes
# -----------------------------------------------------------------------------
@app.middleware("http")
async def log_requests(request: Request, call_next):
    logger.info(f"HTTP {request.method} {request.url.path}", extra={"client": request.client.host})
    try:
        response = await call_next(request)
        logger.info(f"{request.method} {request.url.path} -> {response.status_code}", extra={"client": request.client.host})
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

    overall = body.wait_seconds if body.wait_seconds is not None else settings.wait_after_send
    idle = body.idle_seconds if body.idle_seconds is not None else 5
    cap = body.max_messages if body.max_messages is not None else 20

    try:
        msgs = await send_and_collect_replies(cl, bot, body.text, overall_timeout=overall, idle_timeout=idle, max_messages=cap)
        reply = msgs[-1]["text"] if msgs else None
        return {"sent": True, "messages": msgs, "reply": reply}
    except FloodWaitError as e:
        logger.warning("Flood wait", extra={"seconds": e.seconds})
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in /bot/send", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/search_phone_via_bot", dependencies=[Depends(require_token)])
async def search_phone_via_bot(body: SearchViaBotBody):
    cl = await get_client()
    bot = validate_bot_username(body.bot_username or settings.bot_username)

    overall = body.wait_seconds if body.wait_seconds is not None else settings.wait_after_send
    idle = body.idle_seconds if body.idle_seconds is not None else 5
    cap = body.max_messages if body.max_messages is not None else 20

    phone = norm_phone(body.phone)
    text = body.message_template.format(phone=phone)

    logger.info("search_phone_via_bot", extra={"bot": bot, "phone": phone, "msg_text": text})

    # Optional: best-effort phone resolve to warm caches (ignore errors)
    try:
        await cl(ResolvePhoneRequest(phone=phone))
    except Exception:
        pass

    try:
        msgs = await send_and_collect_replies(cl, bot, text, overall_timeout=overall, idle_timeout=idle, max_messages=cap)
        reply = msgs[-1]["text"] if msgs else None
        logger.info("Bot interaction complete", extra={"bot": bot, "collected": len(msgs)})
        return {"ok": True, "query": text, "messages": msgs, "reply": reply}
    except asyncio.TimeoutError:
        logger.warning("Timeout waiting for bot reply", extra={"bot": bot, "overall": overall})
        return {"ok": False, "query": text, "messages": [], "reply": None, "error": "timeout"}
    except FloodWaitError as e:
        logger.warning("Flood wait", extra={"seconds": e.seconds})
        raise HTTPException(status_code=429, detail=f"Flood wait: {e.seconds}s")
    except HTTPException:
        raise
    except Exception as e:
        logger.exception("Error in /search_phone_via_bot", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail=str(e))
