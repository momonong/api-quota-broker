from concurrent.futures import ThreadPoolExecutor

import pytest
import quota_broker_ohttp as native

from quota_broker.encrypted_transport import KeyRing, RecipientKey, ReplayStore, TransportError

NOW = 1800000000


def test_replay_survives_restart_and_concurrent_acceptance(tmp_path):
    path = tmp_path / "replay.db"

    def accept(_):
        try:
            ReplayStore(path, clock=lambda: NOW).accept(b"wire", "a" * 32, NOW)
            return True
        except TransportError:
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(accept, range(8))) == 1
    assert accept(1) is False
    with pytest.raises(TransportError):
        ReplayStore(path, clock=lambda: NOW).accept(b"new wire", "a" * 32, NOW)


def test_replay_time_capacity_and_clock_rollback_are_fail_closed(tmp_path):
    path = tmp_path / "replay.db"
    store = ReplayStore(path, clock=lambda: NOW, capacity=1)
    for created in (NOW - 121, NOW + 121, True):
        with pytest.raises(TransportError):
            store.accept(b"wire", "a" * 32, created)
    store.accept(b"wire", "a" * 32, NOW)
    with pytest.raises(TransportError):
        store.accept(b"second", "b" * 32, NOW)
    with pytest.raises(TransportError):
        ReplayStore(path, clock=lambda: NOW - 1).accept(b"third", "c" * 32, NOW)
    ReplayStore(path, clock=lambda: NOW + 121, capacity=1).accept(b"fourth", "d" * 32, NOW + 121)


def test_daily_key_rotation_and_bounded_overlap():
    first = RecipientKey(1, b"a" * 32, NOW, NOW + 86400)
    second = RecipientKey(2, b"b" * 32, NOW + 86400, NOW + 172800)
    ring = KeyRing([first, second], clock=lambda: NOW + 86400 + 1199)
    old, _ = native.ClientRequest(native.Server(b"a" * 32, 1).config()).encapsulate(b"old")
    assert ring.decapsulate(old)[0] == b"old"
    assert ring.config()[0] == 2
    with pytest.raises(TransportError):
        KeyRing([first, second], clock=lambda: NOW + 86400 + 1200).decapsulate(old)
    with pytest.raises(TransportError):
        KeyRing([first], clock=lambda: NOW - 1).decapsulate(old)


def test_key_ids_cannot_collide_and_private_files_are_restricted(tmp_path):
    first = RecipientKey(1, b"a" * 32, NOW, NOW + 86400)
    with pytest.raises(TransportError):
        KeyRing([first, first])
    path = tmp_path / "keys.json"
    path.write_text("{}")
    path.chmod(0o644)
    with pytest.raises(TransportError):
        KeyRing.load(path)
    assert b"a" * 32 not in repr(first).encode()
