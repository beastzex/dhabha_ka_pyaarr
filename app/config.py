"""Application configuration using Pydantic Settings."""

import os
from typing import Optional
from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration settings for Dhaba Triage Service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    GEMINI_API_KEY: Optional[str] = Field(default=None)
    OPENAI_API_KEY: Optional[str] = Field(default=None)
    TRIAGE_MODE: str = Field(default="replay")
    DB_PATH: str = Field(default="triage_state.db")
    CONFIDENCE_THRESHOLD: float = Field(default=0.75, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def determine_default_mode(self) -> "Settings":
        # If no LLM API key is available, default triage mode strictly to "replay"
        if not self.GEMINI_API_KEY and not self.OPENAI_API_KEY:
            if not self.TRIAGE_MODE or self.TRIAGE_MODE == "live":
                self.TRIAGE_MODE = "replay"
        return self


settings = Settings()
