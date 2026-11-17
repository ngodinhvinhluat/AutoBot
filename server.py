"""
Điểm khởi chạy server AutoBot. Toàn bộ mã nguồn nằm trong package `autobot/`, chia theo tính năng.

Chạy:  python server.py
Hoặc:  uvicorn server:app --host 0.0.0.0 --port 8000
"""
from autobot.main import app, run

__all__ = ["app"]

if __name__ == "__main__":
    run()
