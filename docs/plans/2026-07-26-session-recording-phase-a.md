# Session Recording and Ingestion, Phase A: Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. Each task ends with a senior-engineer review subagent auditing the diff before the next task starts.

**Goal:** A clinician can bring a session recording, video, transcript, or document into Debrief; it is distilled once into a compact digest; the digest becomes labelled session evidence in the existing debrief flow. No source present means byte-identical behavior to today.

**Architecture:** Four new modules (`recstore.py` storage, `sources.py` ingestion and classification, `ladder.py` map-reduce digest, `jobs.py` background queue), one shared extraction module carved out of `compiler.py`, additive changes to `stt.py`, `extract.py`, `pipeline.py`, `records.py`, `settings_store.py`, `vault.py`, `app.py`, and the vanilla-JS frontend. Companion spec: `docs/specs/2026-07-25-session-recording-and-ingestion.md`. This plan implements spec section 14 Phase A only.

**Tech Stack:** Python 3.13, FastAPI, pytest (marker `live` excluded for the fast suite), vanilla JS frontend, ffmpeg, LM Studio `gemma-4-12b-it-qat`.

---

## Answers to the spec's open questions (section 16)

1. **textutil under a worker thread: safe.** `compiler.py` converts docx with `textutil` (compiler.py:98) and pdf with `pdftotext` (compiler.py:123), both plain `subprocess.run` with a temp file, no GUI or main-thread dependency. Subprocess spawning is thread-safe. The readers move to a new shared module `debrief/doctext.py` so `sources.py` never imports the template compiler.
2. **Parakeet timestamps: real.** `parakeet_mlx.AlignedResult.sentences` carries per-sentence `start`/`end` in seconds (alignment.py in the installed package). The current adapter discards them (`result.text.strip()`, stt.py:154). A new `transcribe_timed()` exposes them. mlx-whisper returns `segments` with start/end too. No chunk-boundary estimation needed.
3. **Source panel placement: a fourth tab on the client record.** The client record view (`renderClientRecord`, app.js:2023) has tabs Sessions | Profile | Documents (app.js:2055-2061). "Recordings" becomes the fourth tab. The debrief record screen additionally gets a small "use session evidence" selector when the chosen client has a ready digest.
4. **Trash: files only today, small contained fix.** `records.trash()` hard-gates on `is_file()` (records.py:582). Restore and sweep already handle directory buckets (`shutil.rmtree`, records.py:642 and 697), and `os.replace` moves directories fine. Task 2 relaxes the gate with directory guard rails.

## Non-negotiables (from the build brief)

- The six rules in spec section 6 each get a named test (rules 1 to 5 are non-live; rule 6 is the consent gate test; rule 7 is the no-debrief draft rule, phase A scope is its API shape only).
- `run_debrief()` with no source behaves exactly as today, protected by a regression test.
- Tests never touch `DebriefVault/`; every fixture repoints the vault at `tmp_path`.
- 413 existing non-live tests stay green, no new warnings.
- No em dashes anywhere: code, comments, UI copy, docs.
- New modules read `config.VAULT_DIR` at call time (never `from .config import VAULT_DIR`), so existing fixture patterns keep working and new fixtures stay one-line.

## File structure

| File | Status | Responsibility |
|---|---|---|
| `debrief/doctext.py` | new | pdf/docx/md/txt to text, moved out of compiler.py. Owns `DocumentReadError`. |
| `debrief/recstore.py` | new | `Recordings/` tree: paths, atomic rung writes, meta.json, listing, deletes, retention sweep. |
| `debrief/sources.py` | new | `Source` dataclass, ingest file/text, classification heuristics, ffmpeg audio extraction, vtt/srt parsing, consent check. |
| `debrief/ladder.py` | new | Rung orchestration and the map-reduce digest. Owns `DIGEST_SCHEMA`, digest markdown rendering, rules 2 and 3 enforcement. |
| `debrief/jobs.py` | new | In-process priority worker, json persistence, interactive hold, resume, visible failures. |
| `debrief/compiler.py` | modify | Delegates document reading to doctext, public behavior unchanged. |
| `debrief/stt.py` | modify | `transcribe_timed()`, `correct_long_transcript()`. |
| `debrief/extract.py` | modify | Optional `digest` parameter rendered as labelled session evidence. |
| `debrief/pipeline.py` | modify | `source_id` threaded through `transcribe_and_extract` and `run_debrief`; attribution post-filter. |
| `debrief/vault.py` | modify | `set_recording_consent()`, frontmatter-only. `write_session_note` passes source_* frontmatter through meta (verify current behavior, add if needed). |
| `debrief/records.py` | modify | `trash()` accepts directories with guard rails; trash listing labels folders. |
| `debrief/settings_store.py` | modify | `recording_retention_days` default plus validation. |
| `app.py` | modify | Source routes, jobs route, consent route, debrief source_id, lifespan sweep + job resume. |
| `static/app.js`, `static/style.css` | modify | Recordings tab, confirm prompt, job progress, review evidence markers, wizard retention card. |
| `tests/test_doctext.py`, `tests/test_recstore.py`, `tests/test_sources.py`, `tests/test_stt_long.py`, `tests/test_ladder.py`, `tests/test_jobs.py`, `tests/test_extract_digest.py`, `tests/test_pipeline_source.py`, `tests/test_sources_api.py`, `tests/test_rules.py` | new | Per-module suites plus the six-rule suite. |
| `tests/fixtures/sources/` | new | Otter verbatim txt, Otter summary txt, Granola md, Zoom vtt, srt, plain txt, clinician notes md. |

Storage layout, spec section 7, is authoritative. Rung map used throughout:

```
rung 0: transcript-raw.md (+ source.<ext> audio, original container)
rung 1: transcript.md
rung 2: digest.json + digest.md
always: meta.json (Source provenance + consent_ref + retention bookkeeping)
```

---

### Task 1: `doctext.py` (carve-out) and `recstore.py` (storage)

**Files:**
- Create: `debrief/doctext.py`, `tests/test_doctext.py`
- Create: `debrief/recstore.py`, `tests/test_recstore.py`
- Modify: `debrief/compiler.py` (delegate readers, keep public API)

- [ ] **Step 1: Write failing tests for doctext.** Move-behavior tests: `test_md_txt_decode`, `test_docx_via_textutil` (monkeypatch subprocess.run), `test_pdf_absent_pdftotext_returns_unsupported`, `test_unknown_ext_raises_valueerror`, `test_cap_at_max_chars`, `test_reader_error_raises_document_read_error`. Also `test_compiler_still_extracts` exercising `compiler.extract_document_text` end to end on md bytes, and `test_compiler_wraps_reader_error` asserting a doctext failure surfaces as `CompilerError` (the API layer catches that type today).
- [ ] **Step 2: Run, verify failure** (`.venv/bin/pytest tests/test_doctext.py -q`).
- [ ] **Step 3: Implement.** `doctext.py` receives `_ALLOWED_DOC_EXTS`, `MAX_DOC_CHARS`, `_ext_of`, `_as_bytes`, `_cap`, `_docx_to_text`, `_pdf_to_text`, `extract_document_text` moved verbatim from compiler.py, with `CompilerError` replaced by a new `DocumentReadError(RuntimeError)`. compiler.py keeps thin delegates:

```python
# compiler.py after the carve-out
from . import doctext
_ALLOWED_DOC_EXTS = doctext._ALLOWED_DOC_EXTS   # existing importers keep working
MAX_DOC_CHARS = doctext.MAX_DOC_CHARS

def extract_document_text(path_or_bytes, filename: str) -> dict:
    try:
        return doctext.extract_document_text(path_or_bytes, filename)
    except doctext.DocumentReadError as exc:
        raise CompilerError(str(exc)) from exc
```

- [ ] **Step 4: Green + full suite** (`.venv/bin/pytest -m "not live" -q`), zero regressions.
- [ ] **Step 5: Write failing tests for recstore.** Fixture pattern (new modules read config at call time, so one setattr only):

```python
@pytest.fixture()
def scratch_vault(tmp_path, monkeypatch):
    vault_dir = tmp_path / "vault"
    monkeypatch.setattr(config, "VAULT_DIR", vault_dir)
    monkeypatch.setattr(vault_mod, "VAULT_DIR", vault_dir)
    vault_mod.ensure_vault()
    return vault_dir
```

Tests: `test_source_dir_layout` (`<vault>/Recordings/C-0001/2026-07-25-1430/`), `test_write_rung_atomic` (no partial file on simulated failure; tmp then `os.replace`), `test_rungs_present_map`, `test_write_meta_and_list_sources_roundtrip`, `test_list_sources_filters_by_client`, `test_delete_audio_keeps_rungs_1_2`, `test_sweep_retention_deletes_only_expired_audio` (retention days from settings, meta `captured_at` decides age, rungs 1 and 2 and meta survive), `test_sweep_retention_zero_days_keeps_everything`, `test_delete_client_sources_removes_client_tree_via_trash`, `test_source_dir_rejects_traversal` (client_id or source_id containing `/`, `..`, or absolute paths raises ValueError).
- [ ] **Step 6: Run, verify failure.**
- [ ] **Step 7: Implement recstore.** Shape:

```python
RUNG_FILES = {0: "transcript-raw.md", 1: "transcript.md", 2: "digest.json"}

def recordings_root() -> Path:            # call-time, test-friendly
    return config.VAULT_DIR / "Recordings"

def source_dir(client_id: str, source_id: str) -> Path      # validates both ids
def write_rung(client_id, source_id, rung: int, content) -> Path  # atomic, digest also renders digest.md
def rungs_present(client_id, source_id) -> set[int]
def write_meta(client_id, source_id, meta: dict) -> Path
def read_meta(client_id, source_id) -> dict | None
def list_sources(client_id=None) -> list[dict]              # meta + rungs, newest first
def delete_audio(client_id, source_id) -> bool              # removes source.* audio only
def delete_source(client_id, source_id) -> bool             # whole dir via records.trash
def delete_client_sources(client_id) -> int
def sweep_retention(now: datetime) -> int
```

Atomic writes reuse `vault._atomic_write` for text and a tmp+`os.replace` twin for bytes. `write_rung(2, digest_dict)` writes `digest.json` and `digest.md` (rendered by ladder, injected as a callable or rendered by the caller; keep recstore dumb: caller passes both strings). Id validation: `re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", x)` and reject `..`.
- [ ] **Step 8: Green + full suite.**
- [ ] **Step 9: Commit** (`Shared document reader plus the Recordings storage layer`).
- [ ] **Step 10: Reviewer subagent audits the Task 1 diff.** Fix findings, commit.

### Task 2: directory-capable trash

**Files:**
- Modify: `debrief/records.py` (trash/restore/list), `tests/test_records.py` (extend)

- [ ] **Step 1: Failing tests.** `test_trash_directory_roundtrip` (trash a dir with three files, restore, contents identical), `test_trash_directory_refuses_vault_root_trash_and_settings_and_private`, `test_trash_refuses_client_root_dir`, `test_list_trash_labels_directory_with_file_count`, `test_sweep_purges_directory_buckets` (already-true behavior, pin it).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** In `records.trash()` replace the `is_file()` gate: allow `target.is_dir()` when the directory is not the vault root, not `_Trash`, not `_Settings`, not `Private`, not a `Clients/<id>` root. The `.md`/`.pdf` worksheet-pairing branch is skipped for directories. `list_trash()` entries gain `"kind": "folder" | "file"` and `"file_count"` for folders. Restore path needs no change (`os.replace` renames directories; collision suffix branch already works with empty suffix).
- [ ] **Step 4: Green + full suite. Commit** (`Trash can hold a folder, for recording sources`).
- [ ] **Step 5: Reviewer subagent audits. Fix, commit.**

### Task 3: `sources.py` ingestion and classification

**Files:**
- Create: `debrief/sources.py`, `tests/test_sources.py`, `tests/fixtures/sources/*`
- Modify: `debrief/vault.py` (`set_recording_consent`, `has_recording_consent`)

- [ ] **Step 1: Fixtures.** Seven small fixture files: `otter-verbatim.txt` (Speaker-and-timestamp lines), `otter-summary.txt` (Otter's summary header structure), `granola-note.md` (headed AI meeting summary, no timestamps), `zoom.vtt` (WEBVTT with speaker-prefixed cues), `plain.srt`, `plain-transcript.txt` (Therapist:/Client: turns, no timestamps), `clinician-notes.md`. All fictional.
- [ ] **Step 2: Failing tests.** Classification: `test_classify_zoom_vtt_verbatim`, `test_classify_srt_verbatim`, `test_classify_otter_verbatim_high_confidence`, `test_classify_otter_summary_not_verbatim`, `test_classify_granola_not_verbatim`, `test_classify_plain_speaker_text_verbatim`, `test_classify_clinician_notes_low_confidence_prompts` (needs_confirmation True). Parsing: `test_parse_vtt_timestamps_and_speakers`, `test_parse_srt_timestamps`, `test_parse_plain_text_no_timestamps`. Ingest: `test_ingest_transcript_file_writes_rung0_and_meta`, `test_ingest_text_pasted`, `test_ingest_audio_stores_original_container`, `test_ingest_video_extracts_audio_discards_video` (monkeypatch `_run_ffmpeg`), `test_ingest_doc_enters_rung2_path` (clinician notes: `entry_rung == 2`, doc text stored as source-text.md, never transcript-raw.md so a non-verbatim doc can never be mistaken for a rung 0 transcript later), `test_ingest_requires_consent` (rule 6: no dated flag, `ConsentError`), `test_consent_helpers_roundtrip` (set, check, date recorded; body untouched).
- [ ] **Step 3: Run, verify failure.**
- [ ] **Step 4: Implement.** The `Source` frozen dataclass exactly as spec section 5.2, plus `to_meta()`/`from_meta()` json round-trip. Classification is layered: extension and header sniffing first (`WEBVTT`, srt cue pattern, audio/video extensions are never ambiguous, confidence 1.0), then content heuristics for txt/md/pdf/docx (Otter speaker-timestamp line density, summary-header detection, Granola structure). Return `(kind, verbatim, produced_by, confidence)`; the ingest wrapper sets `needs_confirmation = confidence < 0.8`. `extract_audio()` and video-audio extraction shell out to ffmpeg (`-ar 16000 -ac 1` mono wav for STT; video to m4a audio for storage) with timeouts, stderr captured into raised `SourceError`. `vault.set_recording_consent(client_id, granted: bool)` rewrites profile frontmatter only (read, set `recording_consent` and `recording_consent_date`, write body back byte-identical); `vault.has_recording_consent(client_id) -> bool` checks flag True and a parseable date. Ingest refuses without consent (`ConsentError`), stores meta with `consent_ref = "recording_consent " + date`.
- [ ] **Step 5: Green + full suite. Commit** (`Sources come in classified, with provenance and a consent gate`).
- [ ] **Step 6: Reviewer subagent audits. Fix, commit.**

### Task 4: `stt.py` long-transcript correction and timed transcription

**Files:**
- Modify: `debrief/stt.py`
- Create: `tests/test_stt_long.py`

- [ ] **Step 1: Failing tests.** `test_windowing_sentence_boundaries` (a 3000-word synthetic transcript splits into windows of roughly 1200 words, each starting and ending on sentence boundaries, consecutive windows share the two-sentence context overlap), `test_single_window_short_text_one_call`, `test_stitching_reassembles_without_duplication` (stub llm.chat echoes input; output equals input), `test_failed_window_degrades_to_uncorrected` (stub raises on window 2 of 3; result carries corrected 1 and 3, raw 2, no exception), `test_progress_callback_counts_windows`, `test_correct_transcript_unchanged_for_short_debriefs` (existing function untouched), `test_transcribe_timed_parakeet_sentences` (fake engine returns an object shaped like AlignedResult; segments carry start/end seconds), `test_transcribe_timed_fallback_single_segment`.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Windowing: split into sentences (regex on `(?<=[.!?])\s+`, fallback hard-split for wall-of-text at 1500 words), pack disjoint sentence ranges of about 1200 words. Each window's llm call carries the same layered correction system prompt (`_build_correction_system`) plus, for windows after the first, the previous window's last two sentences as read-only context:

```python
user = (
    "CONTEXT (already corrected, do not repeat in your output):\n"
    + context_tail
    + "\n\nCORRECT ONLY THE FOLLOWING TEXT:\n" + window_text
) if context_tail else window_text
```

`max_tokens` sized to the window (about 2200 for 1200 words). Stitch is deterministic: corrected windows are disjoint, join with a space. A window failure (exception or empty return) keeps that window's raw text. `transcribe_timed(wav_path, engine_id=None) -> tuple[str, list[dict]]`: parakeet path maps `result.sentences` to `{"start", "end", "text"}`; whisper path maps `result["segments"]`; unknown shape falls back to one segment spanning the whole text with `start=0, end=None`.
- [ ] **Step 4: Green + full suite. Commit** (`Long transcripts correct in windows and keep their timestamps`).
- [ ] **Step 5: Reviewer subagent audits. Fix, commit.**

### Task 5: `ladder.py` digest map-reduce

**Files:**
- Create: `debrief/ladder.py`, `tests/test_ladder.py`, `prompts/digest_map.md`, `prompts/digest_reduce.md`

- [ ] **Step 1: Failing tests.** All with a stubbed `llm.chat`. `test_build_runs_missing_rungs_only` (rung 0 and 1 present on disk, only the digest call happens), `test_build_writes_rungs_as_checkpoints` (each rung on disk the moment it completes), `test_digest_map_reduce_shape` (fixture transcript, stub returns valid segment json, reduce returns DIGEST_SCHEMA shape, both digest.json and digest.md written), `test_digest_from_rung2_entry_source` (clinician notes: no rung 0 or 1 work, digest built from the doc text), `test_rule2_nonverbatim_source_never_yields_client_statements` (stub model tries to return quotes; ladder strips `client_statements` and `risk_excerpts` to empty for `verbatim=False` sources), `test_rule3_undiarized_statements_marked_low_confidence` (source `diarized=False` forces every `attribution_confidence` to "low"), `test_render_digest_md_readable_no_em_dash`, `test_load_digest_missing_returns_none`, `test_partial_map_failure_marks_segment_and_continues` (one map window fails, digest still builds, `segments` entry notes the gap).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** `DIGEST_SCHEMA` is the strict json-schema mirror of spec section 4.2's shape (all fields required, `additionalProperties: false`, matching llm.py's structured-output style). Map: rung 1 text re-windowed (reuse the Task 4 window packer, target 1500 words), each window one constrained call returning `{"topic", "summary", "quotes": [{"quote","start_min","attribution_confidence"}], "interventions": [...], "risk_language": [...]}` with the window's real start/end minutes computed from timed segments when present (else word-count proportion). Reduce: one constrained call over the serialized map outputs producing the digest; `source_id`, `duration_min`, `diarized` filled from meta, not the model. Post-model enforcement in code (never prompt-only): `verbatim=False` strips quote-bearing fields; `diarized=False` caps every confidence at "low"; every quote must be a substring (whitespace-normalized) of the rung 1 text or it is dropped (hallucination guard). `build(source, client_ctx, progress)` runs missing rungs in order: 0 (transcribe via `stt.transcribe_timed`, only when audio present), 1 (`correct_long_transcript`), 2 (map-reduce), writing each via recstore as it completes, calling `progress(stage_name, done, total)`.
- [ ] **Step 4: Green + full suite. Commit** (`The ladder distils a source into a grounded digest`).
- [ ] **Step 5: Reviewer subagent audits. Fix, commit.**

### Task 6: `jobs.py` background queue

**Files:**
- Create: `debrief/jobs.py`, `tests/test_jobs.py`

- [ ] **Step 1: Failing tests.** `test_submit_returns_id_and_persists_record` (json under `<vault>/.debrief_staging/jobs/`), `test_worker_runs_job_and_marks_done` (stub runner), `test_priority_orders_queue`, `test_interactive_hold_pauses_between_rungs` (runner yields between fake rungs via the `should_yield` callback; with hold set the second rung does not start until release), `test_failure_is_recorded_not_silent` (runner raises; job state `failed`, error text and completed rungs preserved on the record), `test_resume_requeues_interrupted_jobs` (a persisted `running` record plus `resume_pending()` requeues it as `queued`), `test_cancel_pending_job`, `test_list_jobs_filters_by_client`. All tests use a synchronous `run_pending_for_test()` drain or a started worker with generous `join` timeouts; no sleeps as assertions.
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Single daemon worker thread, started lazily on first `submit()` (keeps TestClient and pytest imports inert). `queue.PriorityQueue` of `(priority, seq, job_id)`. Job record json: `{id, kind, payload, priority, state, stage, progress, error, rungs_done, created_at, updated_at}`, atomic rewrite on every transition. One registered runner for kind `"ladder"`: loads Source from recstore meta, calls `ladder.build` with a progress callback that updates the record, checking `_interactive_hold` (a `threading.Event`) between rungs; when held, blocks on the event (with the job marked `waiting`), resuming at the same rung boundary. `interactive_hold()` is a context manager the debrief route wraps around its LLM-heavy phase 1. `resume_pending()` called from app lifespan requeues `queued`/`running` records as interrupted-then-resumed; ladder's build-missing-rungs semantics make resume free. Failures: state `failed`, `error` string, `rungs_done` intact; nothing deleted.
- [ ] **Step 4: Green + full suite. Commit** (`Background jobs queue, pause for the clinician, and fail loudly`).
- [ ] **Step 5: Reviewer subagent audits. Fix, commit.**

### Task 7: `extract()` digest evidence + pipeline `source_id` + the rules suite

**Files:**
- Modify: `debrief/extract.py`, `debrief/pipeline.py`, `debrief/vault.py` (note frontmatter passthrough if needed)
- Create: `tests/test_extract_digest.py`, `tests/test_pipeline_source.py`, `tests/test_rules.py`

- [ ] **Step 1: Failing tests, extract.** `test_no_digest_prompt_byte_identical` (golden: `_build_user_message` output with `digest=None` matches the exact current format, and `extract` passes the identical cached schema object), `test_digest_renders_labelled_evidence_block` (user message contains a SESSION EVIDENCE section, clearly separated from the debrief transcript, carrying the authority framing: debrief owns intent and priority, evidence owns fact and detail), `test_digest_schema_gains_conflicts_field_only_when_digest_present`, `test_low_confidence_quotes_render_unattributed` (rule 3 upstream half: a "low" statement appears as an unattributed observation, its quote text absent from the prompt).
- [ ] **Step 2: Failing tests, pipeline.** `test_run_debrief_no_source_regression`: monkeypatch stt and llm and vault the way `tests/test_pipeline*.py` already does, call `run_debrief(wav, client)` with no `source_id`, assert (a) `extract` was called with `digest=None`, (b) the plan dict has no new keys beyond `source` keys defaulting to absent, (c) note path and frontmatter match the pre-change golden. `test_run_debrief_with_ready_digest_attaches_evidence` (digest on disk, plan carries `digest_used: true`, note frontmatter gains `source_id`, `source_kind`, `source_verbatim`), `test_run_debrief_source_still_processing_proceeds_and_says_so` (job active: plan carries `source_status: "processing"`, no digest, never blocks), `test_rule3_filed_note_strips_low_confidence_matches` (a note `client_quotes` entry that whitespace-fuzzy-matches a low-confidence digest statement is removed and surfaced in `evidence_conflicts`).
- [ ] **Step 3: Failing tests, the rules suite (`tests/test_rules.py`).** One test per spec section 6 rule, named to be greppable:
  - `test_rule1_extract_never_sees_rung0_or_rung1`: run the full ingest-plus-debrief path with stubs; capture every `llm.chat` message; assert no message contains sentinel strings planted uniquely in the raw and corrected transcripts (the digest deliberately omits them), and assert `extract`'s transcript argument is the debrief, never the session transcript.
  - `test_rule2_nonverbatim_never_attributed`: end to end: ingest a Granola fixture, build digest with a stub model that emits quotes, assert digest carries none and the extract prompt carries none.
  - `test_rule3_low_confidence_never_a_filed_quote`: the pipeline strip test above, asserted at the filed-note level (note written to scratch vault contains no low-confidence quote text).
  - `test_rule4_raw_transcript_never_in_note`: file a note with a source attached; read the written markdown; assert the rung 0 sentinel is absent and the frontmatter `source_id` link is present.
  - `test_rule5_private_never_touched`: scratch vault with `Private/` seeded (file with sentinel content); recursive snapshot (path, mtime_ns, sha256) before; run ingest, ladder, jobs drain, debrief, deletes, retention sweep; snapshot after is identical. Plus a static assertion that none of `sources.py`, `recstore.py`, `ladder.py`, `jobs.py` contains the string `Private`.
  - `test_rule6_consent_gates_ingest` (dated flag absent: ingest and the upload route both refuse; flag true but date missing: refused).
  - `test_rule7_source_only_draft_has_no_actions`: build the plan from a digest with an empty debrief transcript path shape; phase A asserts the API contract: a plan whose transcript is empty and whose digest exists carries `actions == []` and a `no_debrief: true` marker for the UI copy.
- [ ] **Step 4: Run, verify failures.**
- [ ] **Step 5: Implement.** `extract(..., digest: dict | None = None)`: everything digest-related sits behind `if digest is not None`. Evidence rendering: a `_render_digest_evidence(digest)` helper producing the labelled block (segments as a timeline, themes, interventions observed, high-confidence client statements as quoted evidence, low-confidence ones re-phrased as unattributed observations, unresolved threads, practices). Schema: deep-copy the resolved schema and add `evidence_conflicts: {"type":"array","items":{"type":"string"}}` to required output only in the digest branch, never touching the cached objects. `transcribe_and_extract(wav_path, client_id, source_id=None)`: when given, `ladder.load_digest`; missing digest plus an active job means `source_status: "processing"`, missing digest and no job means `source_status: "missing"`; both proceed without evidence and say so in the plan. Post-extract: the rule 3 strip, populating `evidence_conflicts`. `run_debrief(..., source_id=None)` passes through. Note frontmatter: `session_meta` gains `source_id`, `source_kind`, `source_verbatim` when a digest was used; confirm `vault.write_session_note` writes unknown meta keys into frontmatter, add passthrough if it filters.
- [ ] **Step 6: Green + full suite. Commit** (`The debrief reads session evidence, and seven rules hold by test`).
- [ ] **Step 7: Reviewer subagent audits (this is the highest-stakes diff: regression risk to the hero flow). Fix, commit.**

### Task 8: API routes, consent, retention setting

**Files:**
- Modify: `app.py`, `debrief/settings_store.py`, `debrief/__main__.py` (nothing expected, verify), `tests/test_sources_api.py` (create), `tests/test_settings*.py` (extend)

- [ ] **Step 1: Failing tests** with the `TestClient` fixture pattern from `tests/test_records_api.py`. Routes: `test_upload_source_roundtrip` (multipart txt transcript: 200, `{source_id, kind, verbatim, needs_confirmation, job_id}`), `test_upload_streams_large_file_capped` (over-cap declared length: 413 with plain copy), `test_upload_without_consent_403`, `test_confirm_sets_provenance_and_queues`, `test_get_source_shows_rungs_and_job`, `test_list_sources_by_client`, `test_get_digest_404_before_built`, `test_delete_source_audio_only`, `test_delete_source_whole_via_trash`, `test_jobs_endpoint_lists_active_and_recent`, `test_debrief_accepts_source_id_form_field`, `test_consent_route_sets_dated_flag`. Settings: `test_retention_days_default_zero`, `test_retention_days_validation_rejects_negative_and_nonint`, `test_lifespan_runs_retention_sweep` (lifespan context on the TestClient triggers sweep; assert via monkeypatched counter).
- [ ] **Step 2: Run, verify failure.**
- [ ] **Step 3: Implement.** Routes per spec section 11, all blocking work through `run_in_threadpool`, following the bounded 64 KB chunk-read pattern of `/api/settings/import/upload` (app.py:1215) with a 2 GB cap streamed to a temp file, never fully in memory. Upload flow: stream, `sources.ingest_file`, then `jobs.submit("ladder", ...)` unless `needs_confirmation` (those queue on confirm). `POST /api/clients/{client_id}/recording-consent` wraps `vault.set_recording_consent`, with response copy stating plainly that obtaining and documenting consent is the clinician's responsibility (no jurisdiction talk, per spec section 8). `settings_store.DEFAULTS` gains `"recording_retention_days": 0`; `validate_patch` gains the explicit branch (int, `>= 0`, reject bools). Lifespan gains `recstore.sweep_retention(now)` and `jobs.resume_pending()`, both wrapped best-effort like the existing trash sweep.
- [ ] **Step 4: Green + full suite. Commit** (`Sources flow through the API with consent and retention`).
- [ ] **Step 5: Reviewer subagent audits. Fix, commit.**

### Task 9: Frontend

**Files:**
- Modify: `static/app.js`, `static/style.css`

- [ ] **Step 1: Recordings tab.** Fourth tab in `renderClientRecord` (app.js:2055-2061): `renderSourcesTab(d)` beside the three existing renderers. Contents, top to bottom: consent state card (flag and date shown; a single button records or withdraws dated consent, copy owns the responsibility line); upload zone reusing the docgrid drag-and-drop pattern (app.js:2154-2159) plus a paste-transcript textarea flow; one card per source showing provenance chips (kind, word-for-word or AI summary, produced by), rung progress with honest stage labels ("Transcribing", "Correcting vocabulary", "Building digest", "Ready", "Failed at <stage>, earlier steps kept"), and a two-item overflow menu (Delete audio only, Delete recording). Sources come from `GET /api/sources?client_id=`, jobs from `GET /api/jobs`; poll every 4 s only while this tab is visible and a job for this client is active (copy `startStatusPolling`, app.js:367, with its own timer handle cleared in `go()`).
- [ ] **Step 2: Confirmation prompt.** When an upload response has `needs_confirmation: true`, a dialog in the existing sheet pattern (focus trap, Escape): "Is this word for word, or an AI summary?" with exactly two buttons, posting `/api/sources/{id}/confirm` with `{verbatim, produced_by}`.
- [ ] **Step 3: Debrief with evidence.** On the record screen, when the selected client has a source with rung 2 ready: a small labelled checkbox row "Use the session recording from <date> as evidence". Checked sends `source_id` with `POST /api/debrief`. On the review screen: when the plan has `digest_used`, a visible "Session evidence used" marker near the transcript; when `source_status == "processing"`, the plain line "The session recording is still processing. This note was written from the debrief alone."; `evidence_conflicts` render as their own amber list titled "The recording and the debrief disagree", never auto-resolved. When the plan carries `no_debrief: true`, the review screen states no debrief was given, invites one, and shows zero actions.
- [ ] **Step 4: Wizard retention card.** Seventh card between features and permissions (insert at app.js:3508-3509): keep-until-deleted default versus auto-delete after N days, seeded in `ensureWizardState` (app.js:3569), included in `finishSetup`'s `postSettings` payload (app.js:3785). Renumber the permissions card, update the "Six quick things" copy to "Seven quick things" (app.js:3492), bump the `ent()` clamp (app.js:618). Mirror card in Settings after Card D (app.js:3076).
- [ ] **Step 5: Verify by hand against the running app** (Task 10 does the full pass). Commit (`The client record shows its recordings, honestly`).
- [ ] **Step 6: Reviewer subagent audits (XSS attention: every server string through the existing escaping helpers). Fix, commit.**

### Task 10: Full verification

- [ ] **Step 1:** `.venv/bin/pytest -m "not live" -q`: everything green, no new warnings (baseline has exactly one pre-existing StarletteDeprecationWarning from the fastapi package).
- [ ] **Step 2:** Real run: `DEBRIEF_VAULT_DIR=<scratch> uv run debrief`, then against `http://127.0.0.1:8377`: create consent on a mock client, upload a fixture transcript, watch the job progress honestly, confirm digest.json and digest.md appear in the scratch vault, run a debrief with the source attached (live model), see the evidence marker and note frontmatter link. Screenshot evidence for the morning report.
- [ ] **Step 3:** `eval/run_eval.py` if it exercises extract paths, confirm OVERALL PASS.
- [ ] **Step 4:** Update `HANDOFF.md` (state, test counts, what shipped, what is open) and this plan's checkboxes. Commit (`Phase A: session sources feed the debrief as evidence`).

---

## Execution notes for the implementing session

- Opus subagents implement, one task per subagent, TDD inside each task. A reviewer subagent audits every task's diff as a senior engineer before the next task starts (project precedent: this checkpoint has caught a real defect every time).
- Commits per task on `main`, matching the overnight-pass precedent in HANDOFF.md.
- Any fixture or scratch vault lives under `tmp_path` or the session scratchpad, never `DebriefVault/`.
- The one pre-existing warning is the fastapi TestClient deprecation; do not add suppressions for it and do not add new warnings.
- Spec section 15 scope exclusions hold: no embeddings, no live recording (that is Phase B), no diarization work, no video retention, no cloud calls.
