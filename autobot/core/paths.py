"""Đường dẫn file/thư mục dùng chung.
Dữ liệu chạy (log, settings, recordings) mặc định nằm ở thư mục gốc dự án.
Đặt AUTOBOT_DATA_DIR (VD: /data khi chạy container/k3s) để gom hết vào 1 thư mục có gắn volume.
"""
import os

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)
DATA_DIR = os.environ.get("AUTOBOT_DATA_DIR", "").strip() or PROJECT_ROOT

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
LOG_FILE_PATH = os.path.join(DATA_DIR, "voice_session.log")
SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
RECORDINGS_DIR = os.path.join(DATA_DIR, "recordings")
WEB_DIR = os.path.join(PACKAGE_DIR, "web")
