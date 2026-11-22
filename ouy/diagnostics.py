"""Model and task failures retained for inspection without request credentials."""
from __future__ import annotations

import re
import traceback
from typing import Any


def redact(text: str) -> str:
    text = re.sub(r"(?i)\bBearer\s+[^\s\"',;}]+", "Bearer [redacted]", text)
    text = re.sub(r"\bsk-[A-Za-z0-9_-]+", "[redacted]", text)
    text = re.sub(
        r"(?i)([\"']?(?:api[_-]?key|access[_-]?key|access[_-]?token|refresh[_-]?token|id[_-]?token|"
        r"ok-access-(?:key|passphrase|sign)|authorization|cookie|set-cookie|password|passphrase|client[_-]?secret|secret[_-]?key)[\"']?\s*[:=]\s*)"
        r"(?:\"[^\"]*\"|'[^']*'|[^\s&,;}]+)",
        r"\1[redacted]", text,
    )
    return re.sub(r"(https?://)[^/\s:@]+:[^/\s@]+@", r"\1[redacted]@", text)


def exception_detail(exc: BaseException) -> dict[str, Any]:
    """Keep messages, causal exceptions and call sites; never serialize requests or locals."""
    chain = []
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(chain) < 5:
        seen.add(id(current))
        message = redact(str(current)) or type(current).__name__
        row: dict[str, Any] = {"type": type(current).__name__, "message": message[:24000]}
        if len(message) > 24000:
            row["truncated"] = True
        for key in ("status_code", "model_name", "request_id"):
            value = getattr(current, key, None)
            if isinstance(value, (str, int)):
                row[key] = redact(str(value))
        chain.append(row)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    # Traceback frames contain file/line/function only, not source code or local variables.
    frames = traceback.extract_tb(exc.__traceback__)[-30:]
    return {**chain[0], "causes": chain[1:], "frames": [
        {"file": f.filename, "line": f.lineno, "function": f.name} for f in frames
    ]}
