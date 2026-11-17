"""Bộ hội thoại theo ngữ cảnh nội bộ (rule-based) - chỉ dùng khi không có / lỗi kết nối Gemini API."""
from typing import Any, Dict, List, Optional


def local_contextual_fallback(user_text: str, session_state: Dict[str, Any]) -> str:
    history: List[Dict[str, str]] = session_state.setdefault("history", [])
    user_name: Optional[str] = session_state.get("user_name")
    text_lower = user_text.lower()

    if ("tên gì" in text_lower or "tên là gì" in text_lower) and (
        "tôi" in text_lower or "mình" in text_lower or "nhớ" in text_lower or "anh" in text_lower
    ):
        if user_name:
            reply = f"Bạn vừa giới thiệu tên bạn là {user_name} mà, mình vẫn nhớ rất rõ!"
        else:
            reply = "Bạn chưa giới thiệu tên cho mình biết. Bạn tên là gì nhỉ?"
        history.append({"user": user_text, "ai": reply})
        return reply

    for marker in ["tên tôi là ", "tôi tên là ", "mình tên là ", "anh tên là ", "em tên là "]:
        if marker in text_lower and "gì" not in text_lower and "không" not in text_lower:
            idx = text_lower.find(marker) + len(marker)
            extracted = user_text[idx:].strip().split()[0].strip(",.!?")
            if extracted:
                user_name = extracted.capitalize()
                session_state["user_name"] = user_name
                reply = f"Chào {user_name} nhé! Rất vui được làm quen với {user_name}. Hôm nay bạn muốn trò chuyện về chủ đề gì?"
                history.append({"user": user_text, "ai": reply})
                return reply

    if "làm được gì" in text_lower or "có thể làm gì" in text_lower or "bạn là ai" in text_lower:
        prefix = f"{user_name} ơi, " if user_name else ""
        reply = (
            f"{prefix}mình là trợ lý giọng nói hai chiều của bạn. Mình có thể lắng nghe bạn nói qua Mic, "
            f"ghi nhớ tên và nội dung cuộc trò chuyện của chúng mình, rồi trả lời lại qua tai nghe."
        )
    elif "xin chào" in text_lower or "chào bạn" in text_lower or "alo" in text_lower or "nghe rõ" in text_lower:
        greeting_name = f" {user_name}" if user_name else ""
        reply = f"Chào{greeting_name}! Mình nghe bạn rất rõ qua tai nghe rồi, bạn nói tiếp đi nhé."
    else:
        turn_count = len(history) + 1
        prefix = f"{user_name} ơi, " if user_name else ""
        reply = f"{prefix}mình đã nghe rõ câu thứ {turn_count} của bạn: {user_text}."

    history.append({"user": user_text, "ai": reply})
    return reply
