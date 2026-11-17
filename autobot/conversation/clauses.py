"""Chia nhỏ câu trả lời thành mệnh đề (clause chunking) để đọc cuốn chiếu: AI stream tới đâu, đọc tới đó."""
import re
from typing import List, Optional

CLAUSE_SPLIT_PATTERN = re.compile(r"([.!?;\n]+|,\s+)")


def split_into_speakable_clauses(text: str, min_chars: int = 16) -> List[str]:
    """Tách văn bản thành các mệnh đề ngắn theo dấu câu để đọc cuốn chiếu từng phần"""
    parts = CLAUSE_SPLIT_PATTERN.split(text)
    clauses: List[str] = []
    current = ""
    for part in parts:
        current += part
        if CLAUSE_SPLIT_PATTERN.fullmatch(part) and len(current.strip()) >= min_chars:
            clauses.append(current.strip())
            current = ""
    if current.strip():
        if clauses and len(current.strip()) < 8:
            clauses[-1] = f"{clauses[-1]} {current.strip()}"
        else:
            clauses.append(current.strip())
    return [c for c in clauses if c]


def extract_ready_clause(buffer: str, first: bool = False, max_chars: int = 0) -> tuple[Optional[str], str]:
    """
    Kiểm tra bộ đệm văn bản đang stream từ Gemini:
    - Gặp dấu kết câu (. ! ? ; \\n) và độ dài >= 10 ký tự -> tách ngay để đọc luôn.
    - Gặp dấu phẩy (, ) và độ dài >= 22 ký tự -> tách ngay mệnh đề đầu để đọc trước.
    - first=True (mệnh đề ĐẦU của câu trả lời): ngưỡng thấp hơn (6 / 12 ký tự) để mệnh đề đầu ngắn,
      VieNeu tổng hợp nhanh -> loa cất tiếng sớm; các mệnh đề sau tổng hợp song song trong lúc loa đang phát.
    - max_chars > 0: câu dài hơn mức này thì ưu tiên cắt ở dấu phẩy (gần max_chars nhất) để mệnh đề kế tiếp
      tổng hợp xong trước khi loa phát hết phần đã có (tránh khoảng lặng giữa câu). Không có dấu phẩy thì giữ cả câu.
    """
    min_strong, min_comma = (6, 12) if first else (10, 22)
    # Dấu ngắt câu mạnh đầu tiên đạt ngưỡng độ dài
    strong_end = next((m.end() for m in re.finditer(r"[.!?;\n]+\s*", buffer) if m.end() >= min_strong), None)
    if strong_end is not None and (not max_chars or strong_end <= max_chars):
        return buffer[:strong_end].strip(), buffer[strong_end:]

    # Câu chưa kết thúc (hoặc quá dài) -> cắt ở dấu phẩy
    commas = [m.end() for m in re.finditer(r",\s+", buffer)
              if m.end() >= min_comma and (strong_end is None or m.end() < strong_end)]
    if max_chars:
        within = [c for c in commas if c <= max_chars]
        cut = within[-1] if within else (commas[0] if commas else None)
    else:
        cut = commas[0] if commas else None
    if cut is not None:
        return buffer[:cut].strip(), buffer[cut:]
    if strong_end is not None:
        return buffer[:strong_end].strip(), buffer[strong_end:]
    return None, buffer
