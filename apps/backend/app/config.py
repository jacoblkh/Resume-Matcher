"""Application configuration using pydantic-settings."""

import json
import os
import re
from pathlib import Path
from typing import Any, Literal
from cryptography.fernet import Fernet
from pydantic_settings import BaseSettings, SettingsConfigDict


# Path to config file for API key persistence
CONFIG_FILE_PATH = Path(__file__).parent.parent / "data" / "config.json"

# Encryption key for securing API keys
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
if ENCRYPTION_KEY is None:
    raise ValueError("ENCRYPTION_KEY environment variable must be set.")

fernet = Fernet(ENCRYPTION_KEY.encode())


def load_config_file() -> dict[str, Any]:
    """Load configuration from config.json file.

    Returns:
        Dictionary with configuration values, empty dict if file doesn't exist.
    """
    if CONFIG_FILE_PATH.exists():
        try:
            return json.loads(CONFIG_FILE_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def save_config_file(config: dict[str, Any]) -> None:
    """Save configuration to config.json file.

    Args:
        config: Dictionary with configuration values to save.
    """
    # Ensure data directory exists
    CONFIG_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE_PATH.write_text(json.dumps(config, indent=2))


def encrypt_api_keys(api_keys: dict[str, str]) -> dict[str, str]:
    """Encrypt API keys using Fernet encryption.

    Args:
        api_keys: Dictionary with provider names as keys and API keys as values.

    Returns:
        Dictionary with encrypted API keys.
    """
    return {provider: fernet.encrypt(api_key.encode()).decode() for provider, api_key in api_keys.items()}


def decrypt_api_keys(encrypted_api_keys: dict[str, str]) -> dict[str, str]:
    """Decrypt API keys using Fernet encryption.

    Args:
        encrypted_api_keys: Dictionary with provider names as keys and encrypted API keys as values.

    Returns:
        Dictionary with decrypted API keys.
    """
    return {provider: fernet.decrypt(api_key.encode()).decode() for provider, api_key in encrypted_api_keys.items()}


def validate_api_key(api_key: str) -> bool:
    """Validate the API key format.

    Args:
        api_key: The API key to validate.

    Returns:
        True if valid, False otherwise.
    """
    # Example validation: length check and regex pattern
    return bool(re.match(r'^[A-Za-z0-9_-]{32,}$', api_key)) and len(api_key) <= 64


def get_api_keys_from_config() -> dict[str, str]:
    """Get API keys from config file.

    Returns:
        Dictionary with provider names as keys and API keys as values.
    """
    config = load_config_file()
    encrypted_keys = config.get("api_keys", {})
    return decrypt_api_keys(encrypted_keys)


def save_api_keys_to_config(api_keys: dict[str, str]) -> None:
    """Save API keys to config file.

    Args:
        api_keys: Dictionary with provider names as keys and API keys as values.
    """
    for provider, api_key in api_keys.items():
        if not validate_api_key(api_key):
            raise ValueError(f"Invalid API key for provider {provider}.")
    
    config = load_config_file()
    encrypted_keys = encrypt_api_keys(api_keys)
    config["api_keys"] = encrypted_keys
    save_config_file(config)


def delete_api_key_from_config(provider: str) -> None:
    """Delete a specific API key from config file.

    Args:
        provider: The provider name to delete.
    """
    config = load_config_file()
    if "api_keys" in config and provider in config["api_keys"]:
        del config["api_keys"][provider]
        save_config_file(config)


def clear_all_api_keys() -> None:
    """Clear all API keys from config file."""
    config = load_config_file()
    # Clear plural dict
    config["api_keys"] = {}
    # Clear singular top-level key (legacy support)
    config["api_key"] = ""
    save_config_file(config)


def _get_llm_api_key_with_fallback() -> str:
    """Get LLM API key with fallback to config file.

    Priority: Environment variable > config.json > empty string
    """
    # First check environment variable
    env_key = os.environ.get("LLM_API_KEY", "")
    if env_key and validate_api_key(env_key):
        return env_key

    # Fallback to config file based on provider
    config_keys = get_api_keys_from_config()
    provider = os.environ.get("LLM_PROVIDER", "openai")

    # Map provider to config key
    provider_map = {
        "openai": "openai",
        "anthropic": "anthropic",
        "gemini": "google",
        "openrouter": "openrouter",
        "deepseek": "deepseek",
        "ollama": "ollama",
    }

    config_provider = provider_map.get(provider, provider)
    return config_keys.get(config_provider, "")


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # LLM Configuration
    llm_provider: Literal[
        "openai", "anthropic", "openrouter", "gemini", "deepseek", "ollama"
    ] = "openai"
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str = ""
    llm_api_base: str | None = None  # For Ollama or custom endpoints

    # Server Configuration
    host: str = "0.0.0.0"
    port: int = 8000
    frontend_base_url: str = "http://localhost:3000"

    # CORS Configuration
    cors_origins: list[str] = [
        "http://localhost:3000",
        "http://127.0.0.1:3000",
    ]

    # Paths
    data_dir: Path = Path(__file__).parent.parent / "data"

    @property
    def db_path(self) -> Path:
        """Path to TinyDB database file."""
        return self.data_dir / "database.json"

    @property
    def config_path(self) -> Path:
        """Path to config storage file."""
        return self.data_dir / "config.json"

    def get_effective_api_key(self) -> str:
        """Get the effective API key with config file fallback.

        Priority: Environment/settings value > config.json > empty string
        """
        if self.llm_api_key and validate_api_key(self.llm_api_key):
            return self.llm_api_key
        return _get_llm_api_key_with_fallback()


settings = Settings()