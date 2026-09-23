from minking_desktop.device_id import device_fingerprint


def test_fingerprint_is_a_stable_hash(monkeypatch):
    import minking_desktop.device_id as device_id

    monkeypatch.setattr(device_id, "_material", lambda: "win\nsecret-guid\n00ff00ff")
    first = device_fingerprint()
    assert first == device_fingerprint()
    assert len(first) == 64
    assert "secret-guid" not in first
    assert "00ff00ff" not in first


def test_live_windows_fingerprint_ignores_nothing_but_stays_hashed():
    value = device_fingerprint()
    assert value == device_fingerprint()
    assert len(value) == 64
    int(value, 16)
