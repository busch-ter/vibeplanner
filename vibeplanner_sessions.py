"""Sessões autenticadas do VibePlanner — armazenamento no servidor."""

import secrets
import time

SESSION_TTL = 8 * 60 * 60
_sessions = {}


def create_session(username: str) -> str:
    if not isinstance(username, str) or not username:
        raise ValueError("Identidade inválida.")

    now = time.time()

    for sid, data in list(_sessions.items()):
        if data["expires"] <= now:
            _sessions.pop(sid, None)

    sid = secrets.token_urlsafe(48)
    _sessions[sid] = {
        "username": username,
        "expires": now + SESSION_TTL,
    }
    return sid


def get_session(sid: str):
    if not isinstance(sid, str) or not sid:
        return None

    data = _sessions.get(sid)

    if data is None:
        return None

    if data["expires"] <= time.time():
        _sessions.pop(sid, None)
        return None

    return data["username"]


def revoke_session(sid: str):
    if isinstance(sid, str):
        _sessions.pop(sid, None)
