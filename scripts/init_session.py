
import os
from telethon import TelegramClient

api_id = int(os.getenv("API_ID", "0"))
api_hash = os.getenv("API_HASH", "")
session_name = os.getenv("SESSION_NAME", "f_session")
session_dir = os.getenv("SESSION_DIR", "/data/session")

os.makedirs(session_dir, exist_ok=True)
session_path = os.path.join(session_dir, session_name)

client = TelegramClient(session_path, api_id, api_hash)

async def main():
    print("Starting Telethon login flow...")
    await client.start()
    me = await client.get_me()
    print(f"Session authorized for: {getattr(me, 'username', None) or me.id}")
    print("Done. Session files persisted.")

with client:
    client.loop.run_until_complete(main())
