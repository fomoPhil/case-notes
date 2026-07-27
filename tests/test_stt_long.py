"""Unit tests for windowed long-transcript correction and timed transcription.

Deterministic and fully offline. debrief.stt.llm.chat is always stubbed, so no
LM Studio is needed and no real model is ever called. The windowing tests reach
into the private packer directly, so they do not depend on the llm at all.

Two behaviours are pinned here because the hero flow depends on them:
  - a single failed window degrades to its raw text instead of failing the run
  - correct_transcript (the short-debrief path) is untouched: one call, 1500 tokens
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from debrief import stt


# ---------------------------------------------------------------------------
# Synthetic transcript helpers
# ---------------------------------------------------------------------------


def _sentence(idx: int, words: int = 100) -> str:
    """One synthetic sentence of exactly `words` words, ending in a period.

    Carries two markers: "alpha" (which correction stubs rewrite to "ALPHA", so
    corrected text is distinguishable from raw) and "s<idx>" (unique per
    sentence, so duplication and ordering are checkable).
    """
    body = ["alpha", f"s{idx}"] + ["filler"] * (words - 2)
    return " ".join(body) + "."


def _transcript(n_sentences: int, words: int = 100) -> str:
    """n synthetic sentences joined by single spaces."""
    return " ".join(_sentence(i, words) for i in range(n_sentences))


def _echo_body(user_content: str) -> str:
    """The window body from a correction user message, CONTEXT block stripped."""
    marker = "CORRECT ONLY THE FOLLOWING TEXT:\n"
    return user_content.split(marker)[-1]


@pytest.fixture(autouse=True)
def _clean_engine_cache():
    """Each test starts with an empty engine cache so singletons never bleed."""
    stt._engine_cache.clear()
    yield
    stt._engine_cache.clear()


@pytest.fixture
def _no_llm(monkeypatch):
    """Any llm.chat call is a test failure unless a test installs its own stub."""

    def _boom(*args, **kwargs):
        raise AssertionError("llm.chat must not be called")

    monkeypatch.setattr(stt.llm, "chat", _boom)


# ---------------------------------------------------------------------------
# Window packing (no llm involved)
# ---------------------------------------------------------------------------


def test_windowing_sentence_boundaries():
    """~3000 words pack into <= 1200-word windows that tile the sentence list."""
    text = _transcript(30)  # 30 x 100 words
    sentences = stt._split_sentences(text)
    assert len(sentences) == 30

    windows = stt._pack_windows(sentences)
    assert len(windows) > 1

    # Disjoint, contiguous, and covering: the ranges tile [0, len(sentences)).
    assert windows[0][0] == 0
    assert windows[-1][1] == len(sentences)
    for (_, prev_end), (next_start, _) in zip(windows, windows[1:]):
        assert prev_end == next_start

    for start, end in windows:
        assert start < end, "a window must hold at least one sentence"
        window_words = sum(len(sentences[i].split()) for i in range(start, end))
        assert window_words <= stt._WINDOW_WORDS

    # Every boundary lands on a sentence boundary: each window is whole sentences.
    rebuilt = " ".join(
        " ".join(sentences[start:end]) for start, end in windows
    )
    assert rebuilt == " ".join(sentences)


def test_windowing_wall_of_text_hard_split():
    """4000 words with zero punctuation still split, and nothing exceeds the cap."""
    text = " ".join(["word"] * 4000)
    sentences = stt._split_sentences(text)
    windows = stt._pack_windows(sentences)

    assert len(windows) > 1, "a wall of text must not become one monster window"
    for start, end in windows:
        window_words = sum(len(sentences[i].split()) for i in range(start, end))
        assert window_words <= stt._MAX_SENTENCE_WORDS

    # Nothing is lost by the hard split.
    assert sum(len(s.split()) for s in sentences) == 4000


def test_pack_windows_empty_input_has_no_windows():
    assert stt._pack_windows([]) == []


def test_window_max_tokens_scales_and_caps():
    """Sizing is generous per word so a corrected window is never truncated."""
    small = stt._window_max_tokens(" ".join(["word"] * 10))
    full = stt._window_max_tokens(" ".join(["word"] * 1200))
    assert small >= 2 * 10
    assert full == stt._MAX_WINDOW_TOKENS == 2600
    # Even an oversized hard-split window stays at the cap, never above it.
    assert stt._window_max_tokens(" ".join(["word"] * 1500)) == 2600


# ---------------------------------------------------------------------------
# correct_long_transcript
# ---------------------------------------------------------------------------


def test_single_window_one_call(monkeypatch):
    """Short text that fits one window: one call, and no CONTEXT block."""
    calls: list[dict] = []

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        calls.append({"messages": messages, "max_tokens": max_tokens})
        return "corrected short text"

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    out = stt.correct_long_transcript(
        "The client discussed sertraline. She felt calmer.",
        profession="therapy",
        dictionary="",
    )
    assert out == "corrected short text"
    assert len(calls) == 1
    user = calls[0]["messages"][1]["content"]
    assert "CONTEXT" not in user
    assert user == "The client discussed sertraline. She felt calmer."


def test_context_overlap_present(monkeypatch):
    """Window 2 carries window 1's last two CORRECTED sentences as context."""
    calls: list[dict] = []

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        calls.append({"messages": messages})
        return _echo_body(messages[1]["content"]).replace("alpha", "ALPHA")

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    text = _transcript(24)  # exactly two 12-sentence windows
    stt.correct_long_transcript(text, profession="therapy", dictionary="")

    assert len(calls) == 2
    first_user = calls[0]["messages"][1]["content"]
    second_user = calls[1]["messages"][1]["content"]

    assert "CONTEXT" not in first_user
    assert "CONTEXT (already corrected, do not repeat it in your output):" in second_user
    assert "CORRECT ONLY THE FOLLOWING TEXT:" in second_user

    corrected_one = first_user.replace("alpha", "ALPHA")
    tail = " ".join(stt._split_sentences(corrected_one)[-2:])
    assert tail in second_user
    # The context came from the CORRECTED output, not the raw text.
    assert "ALPHA s10" in second_user and "ALPHA s11" in second_user


def test_stitching_no_duplication(monkeypatch):
    """An echoing stub round-trips the transcript with no sentence repeated."""

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        return _echo_body(messages[1]["content"])

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    text = _transcript(30)
    out = stt.correct_long_transcript(text, profession="therapy", dictionary="")

    assert out == text
    for i in range(30):
        assert out.count(f"alpha s{i} ") == 1


def test_failed_window_degrades(monkeypatch):
    """A window whose call raises falls back to its raw text; the run survives."""
    seen: list[int] = []

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        seen.append(1)
        if len(seen) == 2:
            raise RuntimeError("model fell over")
        return _echo_body(messages[1]["content"]).replace("alpha", "ALPHA")

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    text = _transcript(36)  # exactly three 12-sentence windows
    out = stt.correct_long_transcript(text, profession="therapy", dictionary="")

    assert len(seen) == 3
    for i in list(range(0, 12)) + list(range(24, 36)):
        assert f"ALPHA s{i} " in out, f"window sentence {i} lost its correction"
    for i in range(12, 24):
        assert f"alpha s{i} " in out, f"failed window sentence {i} lost its raw text"
    # No apology note and no leaked scaffolding in the clinical text.
    assert "CONTEXT" not in out
    assert "CORRECT ONLY" not in out
    assert "fell over" not in out


def test_empty_window_result_degrades_to_raw(monkeypatch):
    """An empty or whitespace-only answer degrades exactly like a raised error."""

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        return "   "

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    text = _transcript(4)
    out = stt.correct_long_transcript(text, profession="therapy", dictionary="")
    assert out == text


def test_progress_counts(monkeypatch):
    """progress(done, total) fires once per window, in order."""

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        return _echo_body(messages[1]["content"])

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    ticks: list[tuple[int, int]] = []
    stt.correct_long_transcript(
        _transcript(36),
        profession="therapy",
        dictionary="",
        progress=lambda done, total: ticks.append((done, total)),
    )
    assert ticks == [(1, 3), (2, 3), (3, 3)]


def test_empty_input_unchanged(_no_llm):
    assert stt.correct_long_transcript("") == ""
    assert stt.correct_long_transcript("   ") == "   "
    assert stt.correct_long_transcript(None) is None


def test_long_correction_uses_same_layered_system_prompt(monkeypatch):
    """Same prompt layers as correct_transcript, built once for the whole run."""
    systems: list[str] = []

    def fake_chat(messages, max_tokens=1500, temperature=0.0):
        systems.append(messages[0]["content"])
        return _echo_body(messages[1]["content"])

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    stt.correct_long_transcript(
        _transcript(24),
        {"name": "Bob Smith", "framework": "CBT"},
        "EMDR",
        profession="therapy",
        dictionary="",
    )
    assert len(systems) == 2
    assert systems[0] == systems[1], "the system prompt must not vary per window"
    assert "Bob Smith" in systems[0]
    assert "This clinician practices EMDR" in systems[0]


# ---------------------------------------------------------------------------
# correct_transcript stays exactly as it was (protects the hero flow)
# ---------------------------------------------------------------------------


def test_correct_transcript_unchanged(monkeypatch):
    """The short path is still one call at max_tokens=1500 with the raw text."""
    calls: list[dict] = []

    def fake_chat(messages, max_tokens=None, temperature=None):
        calls.append(
            {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
        )
        return "corrected"

    monkeypatch.setattr(stt.llm, "chat", fake_chat)
    out = stt.correct_transcript("raw text", profession="therapy", dictionary="")
    assert out == "corrected"
    assert len(calls) == 1
    assert calls[0]["max_tokens"] == 1500
    assert calls[0]["temperature"] == 0.0
    assert calls[0]["messages"][1]["content"] == "raw text"
    assert "CONTEXT" not in calls[0]["messages"][1]["content"]


# ---------------------------------------------------------------------------
# transcribe_timed
# ---------------------------------------------------------------------------


# Stand-ins for parakeet_mlx.alignment.AlignedSentence / AlignedResult, so the
# test never imports the real engine package.
@dataclass
class _FakeSentence:
    text: str
    start: float
    end: float


@dataclass
class _FakeResult:
    text: str
    sentences: list = field(default_factory=list)


def test_transcribe_timed_parakeet_segments(monkeypatch, tmp_path):
    """Parakeet sentences map to segments in ONE pass over the audio."""
    wav = tmp_path / "a.wav"
    wav.write_bytes(b"RIFF")

    passes: list[dict] = []

    class _FakeModel:
        def transcribe(self, path, chunk_duration=120.0):
            passes.append({"path": path, "chunk_duration": chunk_duration})
            return _FakeResult(
                text="Hello there. Second bit.",
                sentences=[
                    _FakeSentence(text="Hello there.", start=0.0, end=1.5),
                    _FakeSentence(text=" Second bit.", start=1.5, end=3.25),
                ],
            )

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    engine = stt.get_engine("parakeet")
    monkeypatch.setattr(engine, "_ensure_loaded", lambda: _FakeModel())

    text, segments = stt.transcribe_timed(str(wav), engine_id="parakeet")

    assert len(passes) == 1, "timestamps must not cost a second transcription"
    assert passes[0]["chunk_duration"] == 120.0
    assert text == "Hello there. Second bit."
    assert segments == [
        {"start": 0.0, "end": 1.5, "text": "Hello there."},
        {"start": 1.5, "end": 3.25, "text": "Second bit."},
    ]
    assert " ".join(s["text"] for s in segments) == text


def test_transcribe_timed_whisper_segments(monkeypatch, tmp_path):
    """Whisper's dict result maps start/end/text the same way."""
    wav = tmp_path / "b.wav"
    wav.write_bytes(b"RIFF")

    import sys
    import types

    calls: list[str] = []
    fake = types.ModuleType("mlx_whisper")

    def _transcribe(path, path_or_hf_repo=None):
        calls.append(path)
        return {
            "text": " one two ",
            "segments": [
                {"start": 0.0, "end": 2.0, "text": " one"},
                {"start": 2.0, "end": 4.0, "text": " two"},
            ],
        }

    fake.transcribe = _transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", fake)
    monkeypatch.setattr(stt, "_prepare_offline_for", lambda repo: None)
    monkeypatch.setattr(stt, "_seal_offline", lambda: None)
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})

    text, segments = stt.transcribe_timed(str(wav), engine_id="mlx-whisper")
    assert len(calls) == 1
    assert text == "one two"
    assert segments == [
        {"start": 0.0, "end": 2.0, "text": "one"},
        {"start": 2.0, "end": 4.0, "text": "two"},
    ]


def test_transcribe_timed_fallback_single_segment(monkeypatch, tmp_path):
    """An engine with no timed support returns one spanning segment."""
    wav = tmp_path / "c.wav"
    wav.write_bytes(b"RIFF")

    class _NoTimedEngine:
        id = "parakeet"

        def transcribe(self, wav_path):
            return "  the whole transcript  "

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    stt._engine_cache["parakeet"] = _NoTimedEngine()

    text, segments = stt.transcribe_timed(str(wav), engine_id="parakeet")
    assert text == "the whole transcript"
    assert segments == [{"start": 0.0, "end": None, "text": "the whole transcript"}]


def test_transcribe_timed_unusable_result_shape_falls_back(monkeypatch, tmp_path):
    """A result with no usable sentences still yields one spanning segment."""
    wav = tmp_path / "d.wav"
    wav.write_bytes(b"RIFF")

    class _FakeModel:
        def transcribe(self, path, chunk_duration=120.0):
            return _FakeResult(text="only text, no timings", sentences=[])

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    engine = stt.get_engine("parakeet")
    monkeypatch.setattr(engine, "_ensure_loaded", lambda: _FakeModel())

    text, segments = stt.transcribe_timed(str(wav), engine_id="parakeet")
    assert text == "only text, no timings"
    assert segments == [{"start": 0.0, "end": None, "text": "only text, no timings"}]


def test_transcribe_timed_missing_file_raises(monkeypatch, tmp_path):
    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    with pytest.raises(FileNotFoundError):
        stt.transcribe_timed(str(tmp_path / "nope.wav"), engine_id="parakeet")


def test_transcribe_still_returns_plain_text(monkeypatch, tmp_path):
    """transcribe() keeps its old shape: a stripped string, nothing else."""
    wav = tmp_path / "e.wav"
    wav.write_bytes(b"RIFF")

    class _FakeModel:
        def transcribe(self, path, chunk_duration=120.0):
            return _FakeResult(
                text="  hello parakeet  ",
                sentences=[_FakeSentence(text="hello parakeet", start=0.0, end=1.0)],
            )

    monkeypatch.setattr(stt.settings_store, "load", lambda: {})
    engine = stt.get_engine("parakeet")
    monkeypatch.setattr(engine, "_ensure_loaded", lambda: _FakeModel())
    assert stt.transcribe(str(wav), engine_id="parakeet") == "hello parakeet"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
