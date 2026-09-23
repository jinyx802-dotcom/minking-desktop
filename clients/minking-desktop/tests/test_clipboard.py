from __future__ import annotations

from minking_desktop.clipboard import copy_text


class _Result:
    returncode = 0


def test_copy_text_macos_uses_pbcopy(monkeypatch):
    monkeypatch.setattr("minking_desktop.clipboard._is_windows", lambda: False)
    monkeypatch.setattr("minking_desktop.clipboard._is_macos", lambda: True)
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append((list(cmd), kwargs.get("input")))
        return _Result()

    monkeypatch.setattr("minking_desktop.clipboard.subprocess.run", fake_run)
    copy_text("sk-ts-hello")
    assert calls == [(["pbcopy"], b"sk-ts-hello")]
