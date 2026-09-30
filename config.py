import os
from typing import List, Optional
from dotenv import load_dotenv
from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Load environment variables from .env file
load_dotenv()

class Settings(BaseSettings):
    # App Settings
    HOST: str = Field(default="127.0.0.1")
    PORT: int = Field(default=8000)
    DEBUG: bool = Field(default=False)
    LOG_LEVEL: str = Field(default="INFO")
    MAX_SCRAPE_WORKERS: int = Field(default=4)
    MAX_EMAIL_RATE_DELAY: float = Field(default=1.2)

    # Gemini API Settings
    # Supports both GEMINI_API_KEYS (comma separated) and GEMINI_API_KEY (single)
    GEMINI_API_KEYS: Optional[SecretStr] = None
    GEMINI_API_KEY: Optional[SecretStr] = None

    # SMTP Server Settings
    SMTP_HOST: Optional[str] = None
    SMTP_PORT: Optional[int] = None
    SMTP_USER: Optional[str] = None
    SMTP_PASSWORD: Optional[SecretStr] = None
    SMTP_FROM_EMAIL: Optional[str] = None
    SMTP_FROM_NAME: Optional[str] = None

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore"
    )

    def get_gemini_keys(self) -> List[str]:
        """Returns all configured Gemini API keys as a list of strings."""
        keys = []
        
        if self.GEMINI_API_KEYS:
            keys_str = self.GEMINI_API_KEYS.get_secret_value()
            keys.extend([k.strip() for k in keys_str.split(",") if k.strip()])
            
        if not keys and self.GEMINI_API_KEY:
            single_key = self.GEMINI_API_KEY.get_secret_value()
            if single_key.strip():
                keys.append(single_key.strip())
                
        return keys

    @property
    def is_gemini_configured(self) -> bool:
        """Returns True if at least one Gemini API key is configured."""
        return len(self.get_gemini_keys()) > 0

    @property
    def is_smtp_configured(self) -> bool:
        """Returns True if all required SMTP settings are present."""
        return bool(
            self.SMTP_HOST
            and self.SMTP_PORT
            and self.SMTP_USER
            and self.SMTP_PASSWORD
            and self.SMTP_FROM_EMAIL
        )


try:
    settings = Settings()
except Exception as e:
    import sys
    print(f"Error loading configuration: {e}", file=sys.stderr)
    sys.exit(1)
