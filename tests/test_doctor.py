"""Tests for debrief.doctor. detect_servers and vault are mocked, never live."""

from __future__ import annotations

import debrief.doctor as doctor


def _servers(lm_up=True, lm_gemma="gemma-4-12b-it-qat", ol_up=False, ol_gemma=None):
    return [
        {
            "provider": "lmstudio",
            "base_url": "http://localhost:1234/v1",
            "reachable": lm_up,
            "models": [lm_gemma] if lm_gemma else [],
            "gemma_model": lm_gemma if lm_up else None,
        },
        {
            "provider": "ollama",
            "base_url": "http://localhost:11434",
            "reachable": ol_up,
            "models": [ol_gemma] if ol_gemma else [],
            "gemma_model": ol_gemma if ol_up else None,
        },
    ]


def _by_name(checks):
    return {c["name"]: c for c in checks}


def test_all_pass(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(doctor.models, "detect_servers", lambda: _servers())
    monkeypatch.setattr(
        doctor.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    checks = doctor.run_checks()
    by = _by_name(checks)
    assert by["Model server reachable"]["ok"] is True
    assert by["Gemma model loaded"]["ok"] is True
    assert by["Vault writable"]["ok"] is True
    assert by["ffmpeg on PATH"]["ok"] is True
    # No hard failure -> main() exits 0.
    hard_fail = [c for c in checks if c.get("hard") and not c["ok"]]
    assert hard_fail == []


def test_server_down_is_hard_fail(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(
        doctor.models, "detect_servers", lambda: _servers(lm_up=False)
    )
    monkeypatch.setattr(doctor.models, "pick_gemma", lambda: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    checks = doctor.run_checks()
    by = _by_name(checks)
    assert by["Model server reachable"]["ok"] is False
    assert by["Model server reachable"]["hard"] is True
    assert by["Gemma model loaded"]["ok"] is False
    assert by["Model server reachable"]["fix"]


def test_ffmpeg_missing_is_hard(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(doctor.models, "detect_servers", lambda: _servers())
    monkeypatch.setattr(
        doctor.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: None)

    by = _by_name(doctor.run_checks())
    assert by["ffmpeg on PATH"]["ok"] is False
    assert by["ffmpeg on PATH"]["hard"] is True


def test_pdf_is_soft_warning(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(doctor.models, "detect_servers", lambda: _servers())
    monkeypatch.setattr(
        doctor.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor, "_importable", lambda mod: False)

    by = _by_name(doctor.run_checks())
    pdf = by["PDF export (weasyprint + markdown)"]
    assert pdf["ok"] is False
    assert pdf["hard"] is False
    assert "uv sync --extra pdf" in pdf["fix"]


def test_stt_check_reflects_selected_engine(monkeypatch, tmp_path):
    # Point the vault at a temp dir and select whisper via the settings store.
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(doctor.models, "detect_servers", lambda: _servers())
    monkeypatch.setattr(
        doctor.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.settings_store, "load", lambda: {"stt_engine": "mlx-whisper"})
    monkeypatch.setattr(doctor, "_importable", lambda mod: True)
    # Not cached -> the check stays ok but adds the first-download hint.
    monkeypatch.setattr(doctor.stt, "is_engine_model_cached", lambda eng: False)

    by = _by_name(doctor.run_checks())
    stt_check = by["Speech-to-text (mlx-whisper)"]
    assert stt_check["ok"] is True
    assert stt_check["hard"] is False
    assert "mlx-whisper" in stt_check["detail"]
    assert "1.6 GB" in stt_check["detail"]


def test_stt_check_parakeet_default_no_download_hint(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(doctor.models, "detect_servers", lambda: _servers())
    monkeypatch.setattr(
        doctor.models,
        "pick_gemma",
        lambda: {"base_url": "http://localhost:1234/v1", "model": "gemma-4-12b-it-qat"},
    )
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(doctor.settings_store, "load", lambda: {"stt_engine": "parakeet"})
    monkeypatch.setattr(doctor, "_importable", lambda mod: True)
    monkeypatch.setattr(doctor.stt, "is_engine_model_cached", lambda eng: True)

    by = _by_name(doctor.run_checks())
    stt_check = by["Speech-to-text (parakeet-mlx)"]
    assert stt_check["ok"] is True
    assert "1.6 GB" not in stt_check["detail"]


def test_ollama_gemma_reported_when_lmstudio_lacks_it(monkeypatch, tmp_path):
    monkeypatch.setattr(doctor.config, "VAULT_DIR", tmp_path / "vault")
    monkeypatch.setattr(
        doctor.models,
        "detect_servers",
        lambda: _servers(lm_up=True, lm_gemma=None, ol_up=True, ol_gemma="gemma2:latest"),
    )
    monkeypatch.setattr(doctor.models, "pick_gemma", lambda: None)
    monkeypatch.setattr(doctor.shutil, "which", lambda name: f"/usr/bin/{name}")

    by = _by_name(doctor.run_checks())
    gemma = by["Gemma model loaded"]
    assert gemma["ok"] is False
    assert "Ollama" in gemma["detail"]
