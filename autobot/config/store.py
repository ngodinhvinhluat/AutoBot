"""
Kho cài đặt đang dùng (CFG): mặc định lấy từ schema, settings.json ghi đè, Dashboard đổi được lúc đang chạy.
Các module khác chỉ ĐỌC CFG[...] tại thời điểm dùng -> thay đổi áp dụng ngay, không cần khởi động lại.
"""
import json
import os
from typing import Any, Dict, List, Tuple

from autobot.config.defaults import VAD_SILENCE_SECONDS_V1
from autobot.config.prompts import SYSTEM_INSTRUCTION_V1
from autobot.config.schema import SETTINGS_INDEX, SETTINGS_SCHEMA
from autobot.core.logger import write_log
from autobot.core.paths import SETTINGS_FILE

SETTINGS_VERSION = 2

CFG: Dict[str, Any] = {s["key"]: s["default"] for s in SETTINGS_SCHEMA}


def _coerce_setting(spec: Dict[str, Any], value: Any) -> Any:
    """Ép kiểu + kiểm tra hợp lệ 1 giá trị cài đặt; lỗi -> ValueError"""
    t = spec["type"]
    if t == "bool":
        if isinstance(value, str):
            return value.strip().lower() in ("1", "true", "on", "yes")
        return bool(value)
    if t == "range":
        v = float(value)
        v = min(max(v, spec["min"]), spec["max"])
        return int(round(v)) if float(spec["step"]).is_integer() and float(spec["min"]).is_integer() else round(v, 3)
    if t == "select":
        allowed = [o[0] for o in spec["options"]]
        if value not in allowed:
            raise ValueError(f"'{value}' không nằm trong danh sách cho phép")
        return value
    v = str(value).strip()
    if not v:
        raise ValueError("không được để trống")
    return v


def save_settings() -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump({**CFG, "_version": SETTINGS_VERSION}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        write_log("SETTINGS", f"Lỗi lưu {SETTINGS_FILE}: {e}")


def load_settings() -> None:
    if not os.path.exists(SETTINGS_FILE):
        return
    try:
        with open(SETTINGS_FILE, encoding="utf-8") as f:
            saved = json.load(f)
        migrated: List[str] = []
        if int(saved.get("_version", 1)) < 2:
            # v1 -> v2: giá trị cũ còn đúng bằng mặc định cũ thì chuyển sang mặc định mới (giá trị đã tự chỉnh thì giữ)
            if saved.get("system_instruction") == SYSTEM_INSTRUCTION_V1:
                saved.pop("system_instruction")
                migrated.append("system_instruction")
            if saved.get("vad_silence_seconds") == VAD_SILENCE_SECONDS_V1:
                saved.pop("vad_silence_seconds")
                migrated.append("vad_silence_seconds")
        for k, v in saved.items():
            if k in SETTINGS_INDEX:
                try:
                    CFG[k] = _coerce_setting(SETTINGS_INDEX[k], v)
                except Exception:
                    pass
        if int(saved.get("_version", 1)) < SETTINGS_VERSION:
            save_settings()
            write_log("SETTINGS", f"Đã nâng cấp {SETTINGS_FILE} lên v{SETTINGS_VERSION} (đổi sang mặc định mới: {migrated or 'không'})")
    except Exception as e:
        write_log("SETTINGS", f"Lỗi đọc {SETTINGS_FILE}: {e} -> dùng mặc định")


def update_settings(changes: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, str]]:
    """Áp dụng thay đổi từ Dashboard. Trả về (các giá trị đã đổi, lỗi theo key)"""
    applied: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for k, v in changes.items():
        spec = SETTINGS_INDEX.get(k)
        if spec is None:
            errors[k] = "không có cài đặt này"
            continue
        try:
            nv = _coerce_setting(spec, v)
        except Exception as e:
            errors[k] = str(e)
            continue
        if CFG.get(k) != nv:
            CFG[k] = nv
            applied[k] = nv
    if applied:
        save_settings()
        shown = {k: (v if not isinstance(v, str) or len(v) <= 40 else v[:40] + "...") for k, v in applied.items()}
        write_log("SETTINGS", f"⚙️ Đã cập nhật: {shown}")
    return applied, errors


def reset_settings() -> None:
    """Khôi phục toàn bộ cài đặt về mặc định và lưu lại"""
    for s in SETTINGS_SCHEMA:
        CFG[s["key"]] = s["default"]
    save_settings()
    write_log("SETTINGS", "↺ Đã khôi phục toàn bộ cài đặt mặc định")
