"""User store: credentials, idle expiry, and the purge sweep."""

from datetime import timedelta

from fake_api_server.store import UserStore


def test_authenticate_and_collision():
    values = iter(["taken", "taken", "user-b", "pass-b"])
    store = UserStore(id_factory=lambda: next(values))
    store.users["taken"] = object()
    user = store.create("external")
    assert user.username == "user-b"
    assert user.password == "pass-b"
    assert user.external_id == "external"
    assert store.authenticate("user-b", "nope") is None
    assert store.authenticate("missing", "pass-b") is None
    assert store.authenticate("user-b", "pass-b") is user


def test_purge_drops_idle_users_and_keeps_fresh_ones():
    store = UserStore(max_idle=timedelta(seconds=10), purge_interval=timedelta(hours=1))
    expired = store.create("old")
    fresh = store.create("new")
    expired.last_active = store.now_ms() - 11_000
    assert store.purge() == 1
    assert store.get(expired.username) is None
    assert store.get(fresh.username) is fresh


def test_exact_idle_limit_is_still_valid():
    store = UserStore(max_idle=timedelta(hours=2))
    user = store.create("boundary")
    user.last_active = 1_700_000_000_000
    assert store.purge(1_700_000_000_000 + 7_200_000) == 0
    assert store.purge(1_700_000_000_000 + 7_200_000 + 1) == 1


def test_failed_auth_does_not_refresh_and_success_does():
    store = UserStore()
    user = store.create("ext")
    user.last_active = 10
    assert store.authenticate(user.username, user.password + "x") is None
    assert user.last_active == 10
    assert store.authenticate(user.username, user.password) is user
    assert user.last_active > 10


def test_purge_thread_stops():
    store = UserStore(purge_interval=timedelta(hours=1))
    store.start()
    store.start()
    assert store._thread is not None
    assert store._thread.is_alive()
    store.stop()
    assert store._thread is not None
    assert not store._thread.is_alive()
