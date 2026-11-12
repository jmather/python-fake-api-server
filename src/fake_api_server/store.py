"""In-memory users. Idle users expire after two hours, swept every 15 minutes.

``last_active`` is unix epoch milliseconds. A user is purged when
``last_active < now - max_idle`` (the exact idle limit is still valid).
A successful authentication refreshes ``last_active``. A failed one does not.
"""

import base64
import threading
import time
import uuid
from collections.abc import Callable
from datetime import timedelta

from fake_api_server.matching import Endpoint, Playback, find_best

IdFactory = Callable[[], str]
Clock = Callable[[], int]


def system_now_ms() -> int:
    return time.time_ns() // 1_000_000


class User:
    def __init__(self, username: str, password: str, external_id: str, now_ms: int) -> None:
        self.username = username
        self.password = password
        self.external_id = external_id
        self.last_active = now_ms
        self.endpoints: dict[tuple, Endpoint] = {}

    @property
    def auth_token(self) -> str:
        raw = f"{self.username}:{self.password}".encode()
        return base64.b64encode(raw).decode("ascii")


class UserStore:
    def __init__(
        self,
        *,
        max_idle: timedelta = timedelta(hours=2),
        purge_interval: timedelta = timedelta(minutes=15),
        id_factory: IdFactory | None = None,
        clock: Clock | None = None,
        sweep: bool = True,
    ) -> None:
        self.max_idle = max_idle
        self.purge_interval = purge_interval
        self.id_factory = id_factory or (lambda: str(uuid.uuid4()))
        self.clock = clock or system_now_ms
        self.sweep_enabled = sweep
        self.users: dict[str, User] = {}
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def now_ms(self) -> int:
        return int(self.clock())

    def start(self) -> None:
        if not self.sweep_enabled:
            return
        with self.lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop.clear()
            self._thread = threading.Thread(
                target=self._loop,
                name="fakeapi-user-purge",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=1)

    def _loop(self) -> None:
        while not self._stop.is_set():
            if self._stop.wait(self.purge_interval.total_seconds()):
                return
            self.purge()

    def create(self, external_id: str) -> User:
        with self.lock:
            username = self.id_factory()
            while username in self.users:
                username = self.id_factory()
            password = self.id_factory()
            user = User(username, password, external_id, self.now_ms())
            self.users[username] = user
            return user

    def get(self, username: str) -> User | None:
        with self.lock:
            return self.users.get(username)

    def authenticate(self, username: str, password: str) -> User | None:
        """Return the user and refresh the idle timer when the password matches."""
        with self.lock:
            user = self.users.get(username)
            if user is None or user.password != password:
                return None
            user.last_active = self.now_ms()
            return user

    def record(self, user: User, endpoint: Endpoint) -> bool:
        with self.lock:
            if endpoint.identity in user.endpoints:
                return False
            user.endpoints[endpoint.identity] = endpoint
            return True

    def playback(
        self,
        user: User,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        query: dict[str, str],
        body: str,
        randbelow: Callable[[int], int],
    ) -> Playback | None:
        with self.lock:
            endpoint = find_best(
                user.endpoints.values(),
                method=method,
                path=path,
                headers=headers,
                query=query,
                body=body,
            )
            if endpoint is None:
                return None
            return endpoint.next_response(randbelow)

    def purge(self, now: int | None = None) -> int:
        current = self.now_ms() if now is None else now
        idle_ms = int(self.max_idle.total_seconds() * 1000)
        cutoff = current - idle_ms
        with self.lock:
            stale = [
                username
                for username, user in self.users.items()
                if user.last_active < cutoff
            ]
            for username in stale:
                del self.users[username]
            return len(stale)
