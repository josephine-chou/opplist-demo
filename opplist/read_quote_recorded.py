"""Read quote on recorded answers.

Read quote sends a vendor's reply to a language model and gets the rate form
back as JSON. This edition ships no key and calls nothing: with setting
read_quote_recorded on (the demo turns it on), the request that would have
gone out is answered from a recording instead, and everything after the
call - the answer's check, the rules that turn it into drafts and warnings,
the cost and the monthly cap - runs as it does on a real answer.

A recording is one JSON file in fixtures/read_quote/:

    {"about":  "one line on the invented mail it belongs to",
     "match":  ["words", "that are all", "in the text sent"],
     "usage":  {"input_tokens": 9000, "output_tokens": 700, ...},
     "answer": { ...the rate form, as the model would write it... }}

Every mail and every answer here is invented. A reading whose text matches
no recording gets an error status with a sentence saying so, never a guess.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import paths

FOLDER_NAME = "read_quote"
#: what stands in the request where the key would
NO_KEY = "recorded-answers"
NO_RECORDING = "no recorded answer fits this mail (this edition reads recorded answers only)"
_USAGE = {"input_tokens": 9000, "output_tokens": 700, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}


def folder() -> Path:
    return paths.FIXTURES_DIR / FOLDER_NAME


def recordings(where=None) -> list:
    """Every recording, by file name; one that cannot be read is left out."""
    out = []
    for file in sorted((Path(where) if where else folder()).glob("*.json")):
        try:
            doc = json.loads(file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(doc, dict) and isinstance(doc.get("answer"), dict) and doc.get("match"):
            out.append(dict(doc, name=file.name))
    return out


def text_of(body: dict) -> str:
    """The words of the request: every text block of its message."""
    parts = []
    for message in (body or {}).get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        for block in content or []:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text") or "")
    return "\n".join(parts)


def find(text: str, where=None) -> dict | None:
    """The recording whose `match` words are all in the text (case and
    spacing aside); the one with the most words when several fit."""
    squashed = " ".join((text or "").casefold().split())
    best = None
    for doc in recordings(where):
        words = [" ".join(str(w).casefold().split()) for w in doc["match"]]
        if words and all(w in squashed for w in words) and (best is None or len(words) > len(best["match"])):
            best = doc
    return best


def transport(url, headers, body, timeout):
    """What read_quote.call hands the request to instead of the network:
    (status, the answer in the API's envelope)."""
    doc = find(text_of(body))
    if doc is None:
        return 400, {"error": {"type": "invalid_request_error", "message": NO_RECORDING}}
    return 200, {"content": [{"type": "text", "text": json.dumps(doc["answer"], ensure_ascii=False)}],
                 "stop_reason": "end_turn", "usage": dict(_USAGE, **(doc.get("usage") or {})),
                 "model": (body or {}).get("model"), "recording": doc["name"]}
