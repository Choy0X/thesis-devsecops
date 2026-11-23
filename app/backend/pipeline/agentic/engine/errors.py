"""What can be wrong with an agent session, as a sentence for the person, with the HTTP status a route answers with (mirrors ``ChatStoreError``).

They live here, below both the store and ``agent_sessions.py``, so that the store can raise them without importing the module that uses it. ``agent_sessions`` re-exports
``AgentSessionError`` and ``NotFound`` under their old names: they are the same classes."""
from __future__ import annotations


class AgentSessionError(ValueError):
    def __init__(self, message: str, status: int = 400, code: str = "bad_request"):
        super().__init__(message)
        self.status, self.code = status, code


class NotFound(AgentSessionError):
    def __init__(self, message: str = "There is no such agent session."):
        super().__init__(message, 404, "not_found")


class SessionTooLarge(AgentSessionError):
    def __init__(self):
        super().__init__("This session has grown too large to keep. Start a new one; this one stays as it is.", 413, "too_large")


class SessionExists(AgentSessionError):
    def __init__(self):
        super().__init__("A session with this id already exists.", 409, "exists")
