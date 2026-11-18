# AutoBot server (FastAPI + uvicorn), chạy CPU. Build: docker build -t autobot:dev .
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONUTF8=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=Asia/Ho_Chi_Minh \
    PORT=8000 \
    AUTOBOT_DATA_DIR=/data \
    HF_HOME=/data/cache/huggingface \
    VIENEU_LOCAL_DIR=/data/cache/vieneu_local

# flac: SpeechRecognition (Google STT) đổi WAV -> FLAC; libsndfile1/libgomp1: soundfile, onnxruntime; tzdata: giờ trong log
RUN apt-get update \
 && apt-get install -y --no-install-recommends flac libsndfile1 libgomp1 tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Cài thư viện trước để tận dụng cache layer khi chỉ sửa code
COPY requirements.txt .
# numpy<2.4: từ 2.4 NumPy đòi CPU x86-64-v2 (SSE4.2/POPCNT). Node k3s là VM "Common KVM processor" (kvm64) không có -> crash lúc import
RUN pip install -r requirements.txt "numpy<2.4"

COPY server.py docker-entrypoint.sh ./
COPY autobot ./autobot
# Cài đặt mẫu, chỉ chép vào /data/settings.json khi volume chưa có
COPY settings.json ./settings.default.json

RUN useradd --uid 10001 --create-home autobot \
 && mkdir -p /data && chown autobot:autobot /data \
 && chmod +x docker-entrypoint.sh
USER 10001

VOLUME ["/data"]
EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request,os; urllib.request.urlopen(f'http://127.0.0.1:{os.environ.get(\"PORT\",\"8000\")}/api/state', timeout=4)"

ENTRYPOINT ["./docker-entrypoint.sh"]
CMD ["python", "server.py"]
