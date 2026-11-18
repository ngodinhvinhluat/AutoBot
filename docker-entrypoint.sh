#!/bin/sh
set -e
DATA="${AUTOBOT_DATA_DIR:-/data}"
mkdir -p "$DATA/recordings" "$DATA/cache"
# Lần chạy đầu trên volume trống: lấy cài đặt mẫu đóng kèm image
if [ ! -f "$DATA/settings.json" ] && [ -f /app/settings.default.json ]; then
  cp /app/settings.default.json "$DATA/settings.json"
fi
exec "$@"
