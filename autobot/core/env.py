"""Nạp biến môi trường từ file .env ở thư mục gốc dự án (nếu có cài python-dotenv)."""
from autobot.core.paths import ENV_FILE


def load_env() -> None:
    try:
        from dotenv import load_dotenv
        load_dotenv(ENV_FILE, override=True)
    except ImportError:
        pass
