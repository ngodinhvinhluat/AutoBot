"""Đường dẫn file/thư mục dùng chung. Dữ liệu chạy (log, settings, recordings) nằm ở thư mục gốc dự án."""
import os

PACKAGE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROJECT_ROOT = os.path.dirname(PACKAGE_DIR)

ENV_FILE = os.path.join(PROJECT_ROOT, ".env")
LOG_FILE_PATH = os.path.join(PROJECT_ROOT, "voice_session.log")
SETTINGS_FILE = os.path.join(PROJECT_ROOT, "settings.json")
RECORDINGS_DIR = os.path.join(PROJECT_ROOT, "recordings")
WEB_DIR = os.path.join(PACKAGE_DIR, "web")
