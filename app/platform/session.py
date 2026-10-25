"""Conversation state. In-memory here; the interface is what a Redis-backed store
would implement so any app instance can serve any conversation."""

import asyncio
import secrets
import time
from dataclasses import dataclass, field


@dataclass
class Session:
    session_id: str
    customer_id: str
    customer_name: str
    usecases: list[str]  # every workspace this conversation can use
    active: str = ""     # the workspace whose tools the model gets right now (the tab on screen)
    context_text: str = ""
    messages: list = field(default_factory=list)  # Claude message history, appended to, never edited
    cache: dict = field(default_factory=dict)  # per-conversation backend read cache
    actions: dict = field(default_factory=dict)  # action_id -> PendingAction
    created_at: float = field(default_factory=time.time)
    last_active: float = field(default_factory=time.time)
    turns: int = 0
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def __post_init__(self):
        self.active = self.active or self.usecases[0]


class SessionStore:
    def __init__(self, ttl_s: int = 3600):
        self.ttl_s = ttl_s
        self._sessions: dict[str, Session] = {}

    def create(self, customer_id: str, customer_name: str, usecases: list[str], active: str | None = None) -> Session:
        self._expire()
        session = Session(secrets.token_urlsafe(24), customer_id, customer_name, usecases, active or usecases[0])
        self._sessions[session.session_id] = session
        return session

    def get(self, session_id: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None or time.time() - session.last_active > self.ttl_s:
            self._sessions.pop(session_id, None)
            return None
        session.last_active = time.time()
        return session

    def clear(self):
        self._sessions.clear()

    def _expire(self):
        now = time.time()
        for sid in [s for s, v in self._sessions.items() if now - v.last_active > self.ttl_s]:
            self._sessions.pop(sid, None)

    def __len__(self):
        return len(self._sessions)
