"""1 lượt hội thoại với Gemini: stream câu trả lời, cắt thành mệnh đề đọc được ngay, lưu lại ngữ cảnh."""
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional

from autobot.ai.gemini_client import get_gemini_client
from autobot.config.store import CFG
from autobot.conversation.clauses import extract_ready_clause


class GeminiChatTurn:
    """
    Ngữ cảnh hội thoại nằm trong session_state["gemini_contents"] (gắn theo thiết bị).
    stream_clauses() có thể bị hủy giữa chừng (timeout) -> phần đã nói vẫn giữ trong reply_texts để lưu ngữ cảnh.
    """

    def __init__(self, api_key: str, session_state: Dict[str, Any], user_text: str) -> None:
        from google.genai import types

        self._types = types
        self.api_key = api_key
        self.history: List[Any] = session_state.setdefault("gemini_contents", [])
        self.user_content = types.Content(role="user", parts=[types.Part(text=user_text)])
        self.reply_texts: List[str] = []
        self.first_text: Optional[float] = None
        self.chunks = 0
        self.t0 = time.perf_counter()

    async def stream_clauses(self, emit: Callable[[str], Awaitable[None]]) -> None:
        types = self._types
        client = get_gemini_client(self.api_key)
        config = types.GenerateContentConfig(
            system_instruction=CFG["system_instruction"],
            thinking_config=types.ThinkingConfig(thinking_level=CFG["gemini_thinking"]),
        )
        stream = await client.aio.models.generate_content_stream(
            model=CFG["gemini_model"], contents=[*self.history, self.user_content], config=config,
        )
        buffer = ""
        async for chunk in stream:
            piece = ""
            for cand in chunk.candidates or []:
                for part in (cand.content.parts if cand.content else None) or []:
                    if part.text and not part.thought:
                        piece += part.text
            if not piece:
                continue
            self.chunks += 1
            if self.first_text is None:
                self.first_text = time.perf_counter() - self.t0
            buffer += piece
            while True:
                # Mệnh đề sau dài tối đa ~bằng tổng phần đã nói (tối thiểu 40 ký tự): VieNeu tổng hợp nhanh hơn
                # thời gian thực một chút (~0.7-0.8x), nên mệnh đề kế tiếp kịp xong trước khi loa phát hết.
                emitted_any = bool(self.reply_texts)
                spoken_chars = sum(len(t) for t in self.reply_texts)
                clause, buffer = extract_ready_clause(
                    buffer, first=not emitted_any, max_chars=max(40, spoken_chars) if emitted_any else 0,
                )
                if not clause:
                    break
                self.reply_texts.append(clause)
                await emit(clause)
        if buffer.strip():
            self.reply_texts.append(buffer.strip())
            await emit(buffer.strip())

    def describe(self) -> str:
        ft = self.first_text
        return (
            f"{CFG['gemini_model']} (suy nghĩ: {CFG['gemini_thinking']}): chữ đầu sau "
            f"{(f'{ft:.2f}s' if ft is not None else '-')}, xong sau {time.perf_counter() - self.t0:.2f}s, "
            f"{self.chunks} mẩu | Ngữ cảnh: {len(self.history) // 2} lượt trước"
        )

    def save_to_history(self) -> None:
        """Lưu lượt này vào ngữ cảnh (kể cả khi bị cắt giữa chừng, giữ phần AI đã nói) và cắt bớt lượt cũ"""
        if not self.reply_texts:
            return
        types = self._types
        self.history.append(self.user_content)
        self.history.append(types.Content(role="model", parts=[types.Part(text=" ".join(self.reply_texts))]))
        max_items = int(CFG["history_turns"]) * 2
        if len(self.history) > max_items:
            del self.history[: len(self.history) - max_items]
