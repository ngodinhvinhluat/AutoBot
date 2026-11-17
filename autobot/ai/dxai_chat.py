"""1 lượt hội thoại với agent trên DxAI (POST /api/v1/aiPlayground/chat), cắt câu trả lời thành mệnh đề để TTS đọc ngay.

Cấu hình trong .env: DXAI_API_KEY (bắt buộc), DXAI_BASE_URL, DXAI_TENANT, DXAI_AGENT_CODE.
Khóa API chỉ cần scope `ai.run`; khóa đã gắn với workspace nên không cần header X-Workspace-Id.
"""
import asyncio
import os
import time
import uuid
from typing import Any, Awaitable, Callable, Dict, List

import requests

from autobot.conversation.clauses import split_into_speakable_clauses
from autobot.core.env import load_env

DEFAULT_BASE_URL = "https://demo.indus.nois.vn"
DEFAULT_TENANT = "nois"
DEFAULT_AGENT_CODE = "autobot-assistant"
REQUEST_TIMEOUT_SECONDS = 30
MAX_HISTORY_TURNS = 8  # mỗi lượt = 1 câu hỏi + 1 câu trả lời


def get_dxai_api_key() -> str:
    """Đọc nóng DXAI_API_KEY từ .env (rỗng = không dùng DxAI)."""
    load_env()
    return os.environ.get("DXAI_API_KEY", "").strip()


def _settings() -> Dict[str, str]:
    return {
        "base_url": os.environ.get("DXAI_BASE_URL", DEFAULT_BASE_URL).strip().rstrip("/"),
        "tenant": os.environ.get("DXAI_TENANT", DEFAULT_TENANT).strip(),
        "agent": os.environ.get("DXAI_AGENT_CODE", DEFAULT_AGENT_CODE).strip(),
    }


class DxAIChatTurn:
    """Ngữ cảnh nằm trong session_state["dxai_messages"] (gắn theo thiết bị), cùng giao diện với GeminiChatTurn."""

    def __init__(self, api_key: str, session_state: Dict[str, Any], user_text: str) -> None:
        self.api_key = api_key
        self.cfg = _settings()
        self.history: List[Dict[str, str]] = session_state.setdefault("dxai_messages", [])
        self.session_id = session_state.setdefault("dxai_session_id", str(uuid.uuid4()))
        self.user_text = user_text
        self.reply_texts: List[str] = []
        self.t0 = time.perf_counter()
        self.elapsed = 0.0

    def _call(self) -> str:
        resp = requests.post(
            f"{self.cfg['base_url']}/api/v1/aiPlayground/chat",
            headers={
                "X-API-KEY": self.api_key,
                "X-Tenant-Id": self.cfg["tenant"],
                "Content-Type": "application/json; charset=utf-8",
            },
            json={
                "sessionId": self.session_id,
                "agentCode": self.cfg["agent"],
                "messages": [*self.history, {"role": "user", "content": self.user_text}],
            },
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        resp.raise_for_status()
        data = resp.json()
        return (data.get("content") or data.get("message") or "").strip()

    async def stream_clauses(self, emit: Callable[[str], Awaitable[None]]) -> None:
        text = await asyncio.to_thread(self._call)
        self.elapsed = time.perf_counter() - self.t0
        for clause in split_into_speakable_clauses(text):
            self.reply_texts.append(clause)
            await emit(clause)

    def describe(self) -> str:
        return (
            f"DxAI agent {self.cfg['agent']} @ {self.cfg['base_url']}: xong sau {self.elapsed:.2f}s | "
            f"Ngữ cảnh: {len(self.history) // 2} lượt trước"
        )

    def save_to_history(self) -> None:
        if not self.reply_texts:
            return
        self.history.append({"role": "user", "content": self.user_text})
        self.history.append({"role": "assistant", "content": " ".join(self.reply_texts)})
        max_items = MAX_HISTORY_TURNS * 2
        if len(self.history) > max_items:
            del self.history[: len(self.history) - max_items]
