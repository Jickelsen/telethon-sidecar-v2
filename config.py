
from pydantic import BaseModel, Field
import os

class Settings(BaseModel):
    api_id: int = Field(default_factory=lambda: int(os.getenv("API_ID", "0")))
    api_hash: str = os.getenv("API_HASH", "")
    session_name: str = os.getenv("SESSION_NAME", "f_session")
    session_dir: str = os.getenv("SESSION_DIR", "/data/session")
    bot_username: str = os.getenv("BOT_USERNAME", "@a_bot")
    auth_token: str = os.getenv("AUTH_TOKEN", "change-me")

    # Timeouts (seconds)
    connect_timeout: int = int(os.getenv("CONNECT_TIMEOUT", "20"))
    read_timeout: int = int(os.getenv("READ_TIMEOUT", "20"))
    overall_timeout: int = int(os.getenv("OVERALL_TIMEOUT", "60"))
    wait_after_send: int = int(os.getenv("WAIT_AFTER_SEND", "12"))

settings = Settings()
