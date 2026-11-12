"""
Server-side token storage.

Access and refresh tokens never leave the server. The browser's session cookie
only holds a random session id, which keeps the cookie tiny (well under the 4 KB
limit) and keeps the tokens out of reach of anyone who can read the cookie.

The store is in memory and per process: it is lost on restart (users simply log
in again) and is not shared between multiple worker processes. If you run more
than one worker, swap this class for a shared backend such as Redis.
"""

import secrets
import threading
import time


def expires_at_from(token_data, now=None):
    """Absolute expiry time (epoch seconds) for a token response."""
    now = time.time() if now is None else now
    expires_in = float(token_data.get('expires_in', 3600))
    # Adobe IMS reports expires_in in milliseconds; handle seconds too
    if expires_in > 10_000_000:
        expires_in /= 1000.0
    return now + expires_in


class SessionEntry:
    """Tokens for one logged-in browser session.

    `lock` serialises refreshes so that parallel requests (e.g. a page full of
    thumbnails) trigger a single refresh instead of racing each other.
    """

    def __init__(self, token_data):
        self.lock = threading.Lock()
        self.last_used = time.time()
        self.access_token = token_data['access_token']
        self.refresh_token = token_data.get('refresh_token')
        self.expires_at = expires_at_from(token_data)

    def needs_refresh(self, margin=0):
        return time.time() >= self.expires_at - margin

    def update(self, token_data):
        """Apply a refresh response, keeping the old refresh token if none is returned."""
        access_token = token_data['access_token']  # raises before anything is changed
        self.access_token = access_token
        self.refresh_token = token_data.get('refresh_token') or self.refresh_token
        self.expires_at = expires_at_from(token_data)


class TokenStore:
    def __init__(self, idle_ttl=14 * 24 * 3600):
        self._entries = {}
        self._lock = threading.Lock()
        self._idle_ttl = idle_ttl

    def create(self, token_data):
        """Store a new login and return its random session id."""
        entry = SessionEntry(token_data)
        sid = secrets.token_urlsafe(32)
        with self._lock:
            self._purge_locked(entry.last_used)
            self._entries[sid] = entry
        return sid

    def get(self, sid):
        if not sid:
            return None
        with self._lock:
            entry = self._entries.get(sid)
            if entry is not None:
                entry.last_used = time.time()
            return entry

    def delete(self, sid):
        if not sid:
            return
        with self._lock:
            self._entries.pop(sid, None)

    def _purge_locked(self, now):
        cutoff = now - self._idle_ttl
        stale = [s for s, e in self._entries.items() if e.last_used < cutoff]
        for sid in stale:
            del self._entries[sid]
