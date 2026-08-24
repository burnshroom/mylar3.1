"""
Metron Issue Snapshot Cache (Phase C4.5).

Thread-safe, in-memory, bounded TTL cache for normalized Metron issue snapshots.
Operates with zero network calls, zero SQLite tables/files, and zero background threads.
"""

import time
import threading
from collections import OrderedDict

from mylar import logger
from mylar.extensions.providers.metron.issue_service import validate_comicvine_issue_id

DEFAULT_CACHE_TTL = 3600  # 1 hour
DEFAULT_MAX_ENTRIES = 500


class MetronIssueCache:
    """
    Thread-safe, bounded LRU + TTL cache for normalized Metron issue snapshots.
    """

    def __init__(self, default_ttl=DEFAULT_CACHE_TTL, max_entries=DEFAULT_MAX_ENTRIES, time_func=time.time):
        """
        Initialize Metron issue snapshot cache.

        :param default_ttl: Time-to-live in seconds (default 3600)
        :param max_entries: Maximum number of cached items before LRU eviction (default 500)
        :param time_func: Callable returning current epoch time in seconds (for test injection)
        """
        self.default_ttl = int(default_ttl)
        self.max_entries = int(max_entries)
        self.time_func = time_func
        self._store = OrderedDict()
        self._lock = threading.RLock()

    def get(self, comicvine_issue_id):
        """
        Retrieve a cached normalized issue snapshot if present and not expired.

        :param comicvine_issue_id: Positive integer or valid digit string
        :return: Normalized snapshot dictionary, or None if missing or expired
        """
        try:
            cv_id = validate_comicvine_issue_id(comicvine_issue_id)
        except Exception:
            return None

        with self._lock:
            if cv_id not in self._store:
                return None

            entry = self._store[cv_id]
            now = self.time_func()

            if entry['expires_at'] <= now:
                logger.fdebug(f"[METRON-CACHE] Cache expired for ComicVine IssueID {cv_id}")
                del self._store[cv_id]
                return None

            # Mark as most recently used
            self._store.move_to_end(cv_id)
            logger.fdebug(f"[METRON-CACHE] Cache hit for ComicVine IssueID {cv_id}")
            return entry['snapshot']

    def set(self, comicvine_issue_id, snapshot, ttl=None):
        """
        Store a normalized issue snapshot with TTL expiration.
        Enforces maximum capacity via LRU eviction.

        :param comicvine_issue_id: Positive integer or valid digit string
        :param snapshot: Normalized issue snapshot dictionary
        :param ttl: Optional custom TTL in seconds
        :return: None
        """
        if not isinstance(snapshot, dict) or not snapshot:
            logger.fdebug("[METRON-CACHE] Refusing to cache non-dictionary or empty snapshot")
            return

        try:
            cv_id = validate_comicvine_issue_id(comicvine_issue_id)
        except Exception:
            logger.fdebug(f"[METRON-CACHE] Refusing to cache invalid ComicVine IssueID: {comicvine_issue_id}")
            return

        effective_ttl = int(ttl) if ttl is not None and int(ttl) > 0 else self.default_ttl
        now = self.time_func()
        expires_at = now + effective_ttl

        with self._lock:
            if cv_id in self._store:
                self._store[cv_id] = {
                    'snapshot': snapshot,
                    'expires_at': expires_at
                }
                self._store.move_to_end(cv_id)
                logger.fdebug(f"[METRON-CACHE] Updated cached snapshot for ComicVine IssueID {cv_id}")
                return

            # Check capacity and evict oldest if necessary
            if len(self._store) >= self.max_entries:
                # Evict least recently used (first item in OrderedDict)
                evicted_id, _ = self._store.popitem(last=False)
                logger.fdebug(f"[METRON-CACHE] Evicted oldest entry (ComicVine IssueID {evicted_id}) due to capacity limit")

            self._store[cv_id] = {
                'snapshot': snapshot,
                'expires_at': expires_at
            }
            logger.fdebug(f"[METRON-CACHE] Cached snapshot for ComicVine IssueID {cv_id} (TTL: {effective_ttl}s)")

    def clear(self):
        """Clear all cached entries."""
        with self._lock:
            self._store.clear()
            logger.fdebug("[METRON-CACHE] Cache cleared")

    def __len__(self):
        with self._lock:
            return len(self._store)

    @property
    def size(self):
        with self._lock:
            return len(self._store)


# Process-local singleton instance
_GLOBAL_METRON_CACHE = MetronIssueCache()


def get_metron_cache():
    """Return the process-local MetronIssueCache singleton."""
    return _GLOBAL_METRON_CACHE
