"""Unit tests for the STT engine adapters.

Deterministic: no real model is ever loaded. The parakeet and whisper loaders
are monkeypatched, and the HF cache check is faked, so selection precedence,
the unknown-engine error, per-engine caching, delegation, and the cache-aware
offline decision are all exercised without network or weights.
"""

from __future__ import annotations

import pytest

from debrief import stt


@pytest.fixture(autouse=True)
def _clean_engine_cache():
    """Each test starts with an empty engine cache so singletons never bleed."""
    stt._engine_cache.clear()
    yield
    stt._engine_cache.clear()


# ---------------------------------------------------------------------------
# Selection precedence + unknown id
# ---------------------------------------------------------------------------


def test_explicit_arg_wins(monkeypatch):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {"stt_engine": "parakeet"})
    engine = stt.get_engine("mlx-whisper")
    assert engine.id == "mlx-whisper"


def test_settings_used_when_no_arg(monkeypatch):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {"stt_engine": "mlx-whisper"})
    engine = stt.get_engine()
    assert engine.id == "mlx-whisper"


def test_default_parakeet_when_settings_absent(monkeypatch):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    engine = stt.get_engine()
    assert engine.id == "parakeet"


def test_unknown_engine_raises_listing_valid_ids(monkeypatch):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    with pytest.raises(ValueError) as excinfo:
        stt.get_engine("banana")
    msg = str(excinfo.value)
    assert "banana" in msg
    assert "parakeet" in msg and "mlx-whisper" in msg


# ---------------------------------------------------------------------------
# Per-engine singleton cache
# ---------------------------------------------------------------------------


def test_per_engine_cache_returns_same_instance(monkeypatch):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    a = stt.get_engine("parakeet")
    b = stt.get_engine("parakeet")
    c = stt.get_engine("mlx-whisper")
    assert a is b
    assert a is not c


# ---------------------------------------------------------------------------
# Delegation with monkeypatched loaders
# ---------------------------------------------------------------------------


def test_transcribe_delegates_to_parakeet(monkeypatch, tmp_path):
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")

    class _FakeModel:
        def transcribe(self, path, chunk_duration=120.0):
            return type("R", (), {"text": "  hello parakeet  "})()

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    engine = stt.get_engine("parakeet")
    monkeypatch.setattr(engine, "_ensure_loaded", lambda: _FakeModel())
    assert stt.transcribe(str(wav), engine_id="parakeet") == "hello parakeet"


def test_transcribe_delegates_to_whisper(monkeypatch, tmp_path):
    wav = tmp_path / "b.wav"
    wav.write_bytes(b"RIFF")

    import sys
    import types

    fake = types.ModuleType("mlx_whisper")

    def _transcribe(path, path_or_hf_repo=None):
        assert path_or_hf_repo == stt._WHISPER_MODEL_ID
        return {"text": "  hello whisper  "}

    fake.transcribe = _transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    # Whisper does not seal offline until the first transcribe succeeds; fake the
    # cache decision so no env or network work happens.
    monkeypatch.setattr(stt, "_prepare_offline_for", lambda repo: None)
    monkeypatch.setattr(stt, "_seal_offline", lambda: None)

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    assert stt.transcribe(str(wav), engine_id="mlx-whisper") == "hello whisper"


def test_transcribe_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    with pytest.raises(FileNotFoundError):
        stt.transcribe(str(tmp_path / "nope.wav"), engine_id="parakeet")


# ---------------------------------------------------------------------------
# Cache-aware offline decision logic
# ---------------------------------------------------------------------------


def test_prepare_offline_sets_flags_when_cached(monkeypatch):
    monkeypatch.setattr(stt, "_USER_HF_OFFLINE", None)
    monkeypatch.setattr(stt, "_model_cached", lambda repo: True)
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    monkeypatch.delenv("TRANSFORMERS_OFFLINE", raising=False)
    stt._prepare_offline_for("some/repo")
    import os
    assert os.environ.get("HF_HUB_OFFLINE") == "1"
    assert os.environ.get("TRANSFORMERS_OFFLINE") == "1"


def test_prepare_offline_clears_flags_when_not_cached(monkeypatch):
    monkeypatch.setattr(stt, "_USER_HF_OFFLINE", None)
    monkeypatch.setattr(stt, "_model_cached", lambda repo: False)
    # A prior engine may have sealed offline; a not-cached first download must
    # clear it so the load can reach the network.
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    monkeypatch.setenv("TRANSFORMERS_OFFLINE", "1")
    stt._prepare_offline_for("some/repo")
    import os
    assert "HF_HUB_OFFLINE" not in os.environ
    assert "TRANSFORMERS_OFFLINE" not in os.environ


def test_explicit_user_offline_always_wins(monkeypatch):
    # A user who exported HF_HUB_OFFLINE before startup is never overridden.
    monkeypatch.setattr(stt, "_USER_HF_OFFLINE", "1")
    monkeypatch.setattr(stt, "_model_cached", lambda repo: False)
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    stt._prepare_offline_for("some/repo")
    import os
    assert os.environ.get("HF_HUB_OFFLINE") == "1"


def test_seal_offline_respects_user_choice(monkeypatch):
    monkeypatch.setattr(stt, "_USER_HF_OFFLINE", "0")
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    stt._seal_offline()
    import os
    assert "HF_HUB_OFFLINE" not in os.environ


def test_is_engine_model_cached_uses_scan(monkeypatch):
    monkeypatch.setattr(stt, "_model_cached", lambda repo: repo == stt._PARAKEET_MODEL_ID)
    assert stt.is_engine_model_cached("parakeet") is True
    assert stt.is_engine_model_cached("mlx-whisper") is False
    assert stt.is_engine_model_cached("banana") is False


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


def test_warm_engine_in_background_is_never_fatal(monkeypatch):
    """Warmup runs off-thread and swallows failures: a bad model must never
    stop the app from starting."""
    import debrief.stt as stt_mod

    def _boom(engine_id=None):
        raise RuntimeError("model exploded")

    monkeypatch.setattr(stt_mod, "get_engine", _boom)
    stt_mod.warm_engine_in_background()  # must not raise


def test_warm_engine_loads_when_supported(monkeypatch):
    """Engines exposing _ensure_loaded get warmed; others are skipped cleanly."""
    import threading

    import debrief.stt as stt_mod

    called = threading.Event()

    class _Warmable:
        def _ensure_loaded(self):
            called.set()

    monkeypatch.setattr(stt_mod, "get_engine", lambda engine_id=None: _Warmable())
    stt_mod.warm_engine_in_background()
    assert called.wait(timeout=5), "warmup thread did not load the engine"

    # An engine without _ensure_loaded (mlx-whisper) must not raise.
    class _NotWarmable:
        pass

    monkeypatch.setattr(stt_mod, "get_engine", lambda engine_id=None: _NotWarmable())
    stt_mod.warm_engine_in_background()
