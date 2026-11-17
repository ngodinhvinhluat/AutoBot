"""Nhận dạng giọng nói qua Google Speech Recognition (thư viện SpeechRecognition)."""
import io

import speech_recognition as sr

from autobot.config.store import CFG
from autobot.core.logger import write_log


def speech_to_text(wav_bytes: bytes) -> str:
    """Chuyển đổi WAV bytes thành văn bản (ngôn ngữ theo cài đặt stt_language). Không nghe ra chữ -> chuỗi rỗng"""
    recognizer = sr.Recognizer()
    with sr.AudioFile(io.BytesIO(wav_bytes)) as source:
        audio_data = recognizer.record(source)
        try:
            return recognizer.recognize_google(audio_data, language=CFG["stt_language"])
        except sr.UnknownValueError:
            return ""
        except Exception as e:
            write_log("STT_ERROR", f"Lỗi nhận diện giọng nói: {e}")
            return ""
