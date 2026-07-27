# Session recording and source ingestion

Design spec, 2026-07-25. Feature: Debrief can record the session itself, or ingest a recording, video, transcript, or document produced elsewhere, and use that material as context when writing the note.

Reader: an implementation session with this repo in context. Companion to `ARCHITECTURE.md`, which this extends.

---

## 1. The idea in one paragraph

Today a clinician records a short debrief and Debrief writes a note from it. This feature adds a second, optional input: the session itself, either recorded in the app or brought in as a file. The session material is distilled once into a compact digest, and that digest is handed to the existing extract step alongside the debrief. The clinician still debriefs, still reviews, still approves. Nothing about the existing flow changes when no session material is present.

## 2. Two inputs, two different jobs

This distinction is load-bearing. Do not let the two collapse into one.

| | Session source | The debrief |
|---|---|---|
| Knows | What was **said**. Exact words, the full arc, the thread at minute forty. | What it **meant** and what to **do**. |
| Supplies | Detail, verbatim quotes, chronology, things already forgotten | Clinical judgment, priorities, actions, what to carry forward |
| Required | No | **Yes. Still the hero moment.** |

`extract()` receives both when both exist. With no source it behaves exactly as it does today.

**When they disagree on a fact, surface the conflict on the review screen. Never silently pick one.**

## 3. The constraint that shapes the whole design

One local model, `gemma-4-12b-it-qat`, at 64k context.

A 50-minute session is roughly 6,000 to 9,000 spoken words, or 10k to 20k tokens once two speakers, crosstalk, and filler are counted. That technically fits in 64k. It should still never be sent to the note-writing call, for two reasons:

1. **Quality.** A 12b model given 15k tokens of unstructured two-speaker dialogue writes a worse note than the same model given a focused 90-second debrief. Signal drowns. There is no larger-model escape hatch in a local-first app.
2. **Repetition cost.** Every later call that wants session context would pay the full price again.

Therefore: **distil once, read the distillate.** In engineering terms the digest is a materialized view over the transcript. The technique is hierarchical (map-reduce) summarization.

**Consumption rule, enforced in code, not by convention: `extract()` reads the digest. It never reads rung 0 or rung 1.**

## 4. The ladder

| Rung | Artifact | Produced by | Regenerated |
|---|---|---|---|
| **0** | Source audio plus raw transcript | Existing `stt.transcribe` (parakeet already chunks at 120s) | Never |
| **1** | Corrected transcript | `stt.correct_long_transcript`, new, chunked | On dictionary or vocab change |
| **2** | **Session digest**, structured | `ladder.build_digest`, map-reduce over rung 1 | On demand |
| → | Durable facts and themes | Existing `vault.update_profile` | Merged, never replaced |

Rung 0 exists so the clinician can verify anything and so the ladder can be rebuilt when models improve. It is an archive, not a working input.

**Rungs are written to disk as they complete, which makes the ladder its own checkpoint.** Resuming an interrupted job means building the next missing rung, not starting over. No separate progress bookkeeping.

Embeddings and semantic retrieval are **out of scope**. The app has grep-based search via `/api/search`, there is no vector store, and adding one for a single-user local app is unjustified until session volume proves otherwise.

### 4.1 Rung 1 matters more than it looks

Speech to text mangles clinical vocabulary, which is exactly why `correct_transcript` already exists. Over a full session the existing implementation is inadequate in two ways: `max_tokens=1500` truncates, and one call cannot hold a long transcript alongside the layered correction prompt.

New function, same contract and same prompt layers:

```python
# debrief/stt.py
def correct_long_transcript(
    text: str,
    client_ctx: dict | None = None,
    framework: str | None = None,
    profession: str | None = None,
    dictionary: str | None = None,
    progress: Callable[[int, int], None] | None = None,
) -> str
```

Splits on sentence boundaries into disjoint windows of roughly 1,200 words, corrects each window with the existing layered system prompt, and joins the corrected windows. Each window after the first carries the previous window's last two sentences as read-only context (marked "do not repeat in your output"), which orients the correction without an overlap to de-duplicate at the stitch; disjoint windows make silent duplication or loss at a seam structurally impossible. Windows are independent, so a single failed window degrades to its uncorrected text rather than failing the run. `correct_transcript` stays as-is for short debriefs.

As built, the window budget counts characters as well as words, so a pasted transcript in a language without spaces still splits into real windows with a real output budget.

### 4.2 The digest is where quality lives or dies

Map stage: each rung 1 window produces a segment summary, candidate verbatim quotes with timestamps, interventions observed, and any risk language. Reduce stage: one call over all segment outputs produces the digest.

Budget check: 12 windows at roughly 600 output tokens is about 7k tokens into the reduce call, comfortably inside 64k. The digest itself lands around 1,200 to 2,000 tokens, which is what `extract()` then carries.

```json
{
  "source_id": "...",
  "duration_min": 52,
  "diarized": true,
  "segments": [
    {"start_min": 0, "end_min": 8, "topic": "...", "summary": "..."}
  ],
  "themes": ["..."],
  "interventions_observed": [
    {"name": "...", "client_response": "...", "start_min": 14}
  ],
  "client_statements": [
    {"quote": "...", "start_min": 12, "attribution_confidence": "high | low"}
  ],
  "risk_language_present": false,
  "risk_excerpts": [],
  "practices_or_homework": ["..."],
  "unresolved_threads": ["..."],
  "notable_shifts": ["..."]
}
```

Notes on specific fields:

- `client_statements` are the grounding target. The existing `EXTRACT_SCHEMA` already carries `client_quotes`; the digest is where they can now come from actual speech rather than the clinician's recollection.
- `risk_language_present` is a **flag for human attention, never a clinical determination.** It surfaces excerpts on the review screen. It does not populate the note's risk block and does not decide anything. The clinician's debrief and judgment remain the source for risk assessment, per existing key decision 4.
- `attribution_confidence` drives rule 6.3 below.

## 5. Sources and provenance

### 5.1 What can come in

| Input | Handling | Entry rung | Verbatim |
|---|---|---|---|
| Live in-app recording (phase B) | Chunks assembled, then normal audio path | 0 | Yes |
| Audio file (m4a, mp3, wav, aac) | ffmpeg normalize to 16k mono wav, then `stt.transcribe` | 0 | Yes |
| Video file (mp4, mov, webm) | ffmpeg extracts the audio track, then as audio. Video is not retained. | 0 | Yes |
| Transcript file (vtt, srt, txt) | Parse, keep timestamps and speaker labels, skip STT | 0 | Yes |
| Pasted transcript text | As above without timestamps | 0 | Yes |
| AI meeting-tool export (Otter, Granola, Fireflies, Fathom) as pdf, docx, md | **Classify first**, see 5.2 | 0 or 2 | Depends |
| Clinician's own written notes (pdf, docx, md, txt) | Treated as clinician observation | 2 | **No** |

ffmpeg is already a machine dependency and is present. Text extraction for pdf, docx, md, and txt already exists in `debrief/compiler.py` (`_ALLOWED_DOC_EXTS`, and the docx and pdf readers below it). **Reuse it. Do not write a second extractor.** If those readers need to move to a shared module so `sources.py` can call them without importing the template compiler, move them; do not duplicate them.

### 5.2 Provenance, and the clinical-record bug it prevents

An Otter export may be a verbatim transcript, Otter's AI summary, or both in one file. A Granola note is almost entirely AI-generated interpretation.

**If a third-party AI summary is ingested as verbatim, the system will eventually place a paraphrased or hallucinated sentence into a clinical record as the client's own quote.** In a clinical record that is not a quality problem, it is a records-integrity failure that survives in the file forever.

Every source therefore carries provenance:

```python
# debrief/sources.py
@dataclass(frozen=True)
class Source:
    id: str                 # e.g. "2026-07-25-1430"
    client_id: str
    kind: str               # live_recording | uploaded_audio | uploaded_video
                            # | transcript_file | pasted_transcript
                            # | third_party_summary | clinician_notes
    verbatim: bool
    produced_by: str        # debrief | zoom | otter | granola | clinician | unknown
    entry_rung: int         # 0 or 2
    captured_at: datetime
    duration_min: int | None
    diarized: bool
    partial: bool           # true when a live recording was interrupted
    audio_path: Path | None
    consent_ref: str | None # the dated consent recorded on the client profile
```

Classification is heuristic and then confirmed. Detect the common export headers and structures, guess, and **ask the clinician to confirm with one tap when confidence is low.** A single "Is this word for word, or an AI summary?" removes the entire failure class for the cost of one question.

## 6. Rules that must hold

1. **`extract()` never sees rung 0 or rung 1.** Enforce with a test, not a comment.
2. **A non-verbatim source may never produce a `client_statements` entry or any attributed quote.** It informs the digest's summary fields only.
3. **A low-confidence attribution may never become a verbatim quote in a filed note.** Mark it unattributed instead. An unattributed observation is recoverable. A misattributed quote is not.
4. **The raw transcript never enters the session note.** The note links to its source. The clinical record stays the clinical record.
5. **Recordings and digests never touch `Private/`.** Existing key decision 6 (45 CFR 164.501 psychotherapy-notes separation) applies to this material at least as strongly as to notes. Session material is arguably closer to psychotherapy notes than to the clinical record, so keep the separation absolute and do not let a digest, transcript, or excerpt be written into or read from `Private/`.
6. **The record button is gated on dated recording consent** for that client. Section 8.
7. **A recording without a debrief can produce a draft note but no actions.** The review screen says plainly that no debrief was given and invites one. Actions require intent, and only the clinician supplies intent.

## 7. Storage layout

New tree at the vault root, mirroring `Clients/`.

```
DebriefVault/
  Recordings/
    C-0001/
      2026-07-25-1430/
        source.m4a           rung 0 audio, original container, absent if not retained
        transcript-raw.md     rung 0
        transcript.md         rung 1, corrected
        digest.json           rung 2, the machine artifact code reads
        digest.md             rung 2, rendered human view, generated from digest.json
        meta.json             Source provenance, consent reference, retention date
```

Rationale for a separate root tree rather than living inside `Clients/C-0001/`: media is large (a 50-minute m4a is roughly 25MB, and a working clinician generates gigabytes a year), the clinical text folders stay lean and greppable, and the entire media tree is one directory to exclude from cloud sync or delete wholesale.

`digest.json` is the single source of truth; `digest.md` is a rendering of it. This is a deliberate, narrow exception to the plain-markdown vault value, scoped to `Recordings/` and never to the clinical record. The clinician still gets a readable markdown view of everything.

Writes are atomic (tmp plus rename) to match `vault.write_session_note`.

The session note gains a frontmatter link to its source, so the record points at its evidence:

```yaml
source_id: 2026-07-25-1430
source_kind: uploaded_audio
source_verbatim: true
```

## 8. Consent and retention

**Consent.** A dated flag on the client profile, in `_Profile.md` frontmatter, consistent with the existing profile pattern:

```yaml
recording_consent: true
recording_consent_date: 2026-07-25
```

- The in-app record button and the import path are both gated on an active flag for that client.
- Copy at the point of recording states plainly that the clinician is responsible for obtaining and documenting consent.
- **Do not attempt to determine jurisdiction, and do not offer legal advice.** Per existing claims-language rules, describe what the app does and let the clinician and their counsel draw conclusions. Recording law varies by state and this app takes no position on it.

**Retention.** Audio is kept until the clinician deletes it, per the product decision. Because that default accumulates sensitive material indefinitely, it must be a conscious choice rather than a discovery:

- A retention card in the setup wizard, alongside the existing six, presenting keep-until-deleted (default), or auto-delete audio after a chosen number of days.
- `settings_store` gains `recording_retention_days` (0 means keep).
- A retention sweep runs at launch, deletes expired audio, and **leaves rungs 1 and 2 intact.** Losing the audio must never lose the note's evidence trail.
- Per-source and per-client delete, both reachable from the records UI, both routed through the existing trash with its 30-day retention.
- Deleting a client cascades into `Recordings/`. This is the deletion path most likely to be missed.

## 9. The job queue

One local model server means LLM work must serialize. That constraint produces a genuinely good behaviour rather than a compromise:

> Session ends. Processing starts in the background. The clinician records their debrief. The debrief **preempts** the queue because a human is waiting. By the time the note is on screen for review, the digest is ready.

```python
# debrief/jobs.py
def submit(kind: str, payload: dict, priority: int = 5) -> str   # returns job_id
def status(job_id: str) -> dict     # state, current rung, progress, error
def list_jobs(client_id: str | None = None) -> list[dict]
def cancel(job_id: str) -> bool
```

- In-process worker thread with a priority queue. **No Redis, no Celery.** This is a single-user local app and a dependency here would be indefensible.
- Interactive work (a debrief) runs at higher priority than background work (source processing) and preempts at the next rung boundary, not mid-call.
- Job records persist as json under `DebriefVault/.debrief_staging/jobs/` so a restart can report what was interrupted.
- Resume rebuilds from the first missing rung, per section 4.
- Rough budget on an M-series Mac for a 50-minute session: transcription 1 to 3 minutes, rung 1 correction 4 to 7 minutes, digest 4 to 7 minutes. Call it 10 to 17 minutes of background work, invisible when it starts as the session ends.
- **Failures are visible.** Existing failure mode in this codebase: errors were cleared one line after being set, so failures were silent. A failed or partial job must surface in the UI with what failed and what survived, and must not discard rungs already built.

## 10. Module contract

Additions in the style of `ARCHITECTURE.md`.

```python
# debrief/sources.py            NEW. Get material in, classify it, normalize it.
def ingest_file(path: Path, client_id: str, declared_kind: str | None = None) -> Source
def ingest_text(text: str, client_id: str, kind: str) -> Source
def classify(path: Path, text: str) -> tuple[str, bool, str, float]
    # -> (kind, verbatim, produced_by, confidence). Low confidence prompts the clinician.
def extract_audio(media_path: Path) -> Path      # ffmpeg, video or audio -> 16k mono wav
def parse_transcript(path: Path) -> tuple[str, list[dict]]   # text, timed speaker turns

# debrief/ladder.py             NEW. Distil a Source into a digest.
def build(source: Source, client_ctx: dict, progress=None) -> dict   # runs missing rungs, returns digest
def build_digest(corrected_text: str, source: Source, client_ctx: dict) -> dict  # map-reduce
def load_digest(client_id: str, source_id: str) -> dict | None
def render_digest_md(digest: dict) -> str

# debrief/recstore.py           NEW. Where the files live.
def source_dir(client_id: str, source_id: str) -> Path
def write_rung(client_id: str, source_id: str, rung: int, content) -> Path   # atomic
def rungs_present(client_id: str, source_id: str) -> set[int]
def list_sources(client_id: str | None = None) -> list[Source]
def delete_audio(client_id: str, source_id: str) -> bool
def delete_source(client_id: str, source_id: str) -> bool     # via trash
def sweep_retention(now: datetime) -> int                     # returns audio files removed

# debrief/jobs.py               NEW. See section 9.

# debrief/stt.py                CHANGED
def correct_long_transcript(...) -> str          # see 4.1

# debrief/extract.py            CHANGED
def extract(transcript: str, client_ctx: dict, framework: str, now: datetime,
            digest: dict | None = None) -> dict
    # digest, when present, is rendered into the prompt as session evidence, clearly
    # labelled as such and distinguished from the clinician's debrief. Prompt guidance:
    # the debrief is authoritative on intent and priority, the digest on fact and detail.

# debrief/pipeline.py           CHANGED
def run_debrief(wav_path: str, client_id: str, execute: bool = True,
                verify: bool = True, source_id: str | None = None) -> dict
    # Loads the digest when source_id is given and the digest exists. When the source is
    # still processing, either wait on the job or proceed without it and say so in the
    # result. Never block silently.

# debrief/vault.py              CHANGED
    # write_session_note gains source linkage in frontmatter (section 7)
```

## 11. Server contract

```
POST   /api/sources/upload          multipart: file + client_id [+ declared_kind]
                                    -> {source_id, kind, verbatim, needs_confirmation, job_id}
POST   /api/sources/{id}/confirm    json: {verbatim: bool, produced_by: str}
GET    /api/sources/{id}            -> Source + rungs present + job status
GET    /api/sources?client_id=      -> list
GET    /api/sources/{id}/digest     -> digest json
DELETE /api/sources/{id}            [?audio_only=true]
POST   /api/debrief                 gains optional source_id
GET    /api/jobs                    -> active and recent jobs

# Phase B only
POST   /api/recording/start         json: {client_id} -> {source_id}. Rejects without consent.
POST   /api/recording/chunk         multipart: source_id + sequence + blob
POST   /api/recording/stop          json: {source_id} -> assembles, queues the ladder
```

## 12. Frontend

**Phase A**

- A source panel on the client record: upload, drop, or paste. Shows each source with its provenance, rung progress, and a delete control.
- The confirmation prompt when classification confidence is low. One question, two buttons.
- Job progress that is honest about stage ("transcribing", "correcting vocabulary", "building digest") rather than a fake spinner.
- On the debrief review screen, when a digest was used: a clear marker on which parts drew on session evidence, and any conflicts between digest and debrief surfaced rather than resolved silently.

**Phase B**

The current recorder accumulates blobs in `App.chunks` and posts one blob on stop. That is correct for 90 seconds and wrong for 50 minutes. Long recording needs:

- `mediaRecorder.start(timeslice)` with each chunk POSTed as it arrives, so a crash loses at most one timeslice.
- A persistent, unmissable recording indicator with running duration. A clinician who forgets they are recording is the worst outcome this feature can produce.
- Pause and resume. Clients ask for it. Paused audio must be genuinely absent, not merely marked.
- Recovery on reload: an interrupted recording is offered back, clearly labelled partial.
- An auto-stop prompt after a configurable ceiling, so a forgotten recording does not capture the next client.
- **Note for the implementer:** the capture setup block (getUserMedia, mime negotiation, `App.chunks = []`, the MediaRecorder wiring) is duplicated verbatim between `toggleAssistantRecord` (`static/app.js:1622`) and `toggleRecord` (`static/app.js:1785`). Only the surrounding UI chrome differs. Extract one recorder helper before adding long-recording behaviour, or this complexity gets built twice and drifts.

## 13. Tests

Repo convention: pytest, `-m "not live"` for the fast suite, live tests separate. 413 non-live tests currently pass. Follow it.

Non-live:

- `correct_long_transcript` windowing: boundaries, overlap stitching, a failed window degrading to uncorrected text rather than failing the run.
- Digest map-reduce against a fixture transcript, with a stubbed llm.
- `classify()` across fixture exports: Otter verbatim, Otter summary, Granola note, Zoom vtt, plain text, clinician notes.
- **Rule enforcement, one test each for rules 1, 2, 3, 4, and 5 in section 6.** These are the tests that matter most. Rule 5 in particular: assert no path writes to or reads from `Private/`.
- Transcript parsing: vtt and srt timestamps and speaker labels.
- `recstore` atomicity, rung presence, retention sweep preserving rungs 1 and 2, client-delete cascade.
- Job queue: priority preemption at rung boundaries, resume from partial rungs, failure surfacing.
- Consent gate rejects recording start and import without a dated flag.
- `run_debrief` with no source behaves byte-identically to today. This is the regression that protects the hero flow.

Live:

- End to end on a short real recording: ingest, ladder, digest, debrief, note.
- ffmpeg audio extraction from a real video file.

**Never write to `DebriefVault/` during tests.** Point `DEBRIEF_VAULT_DIR` at a scratch dir, per existing practice.

## 14. Build order

**Phase A: ingestion and the ladder.**
`sources.py`, `recstore.py`, `ladder.py`, `jobs.py`, `correct_long_transcript`, the `extract()` digest parameter, the API routes, the source panel, consent flag, retention setting and sweep.

**Phase B: live in-app recording.**
Recorder consolidation, chunked upload, consent-gated record button, pause and resume, partial recovery, long-session UI.

Phase A first, deliberately. It exercises every piece of downstream machinery with none of the real-time engineering risk, and it proves the digest actually improves notes before a live client session depends on it. If the digest turns out not to improve notes, that is much cheaper to discover in phase A.

## 15. Explicitly out of scope

- **Embeddings and semantic retrieval.** No vector store, grep search already exists, unjustified at this scale.
- **Real-time transcription during a session.** Capture live, transcribe after. Materially harder and nothing here needs it.
- **Diarization beyond what the STT engine provides.** Single-microphone in-person audio is the hard case and parakeet does not diarize. Where speakers cannot be separated, mark statements unattributed rather than guessing, per rule 3. Revisit only if attribution proves necessary in practice.
- **Video retention.** Audio is extracted, video is discarded.
- **Cloud transcription.** Everything stays local, per the project's core claim.
- **The digest replacing the note.** It is evidence for the note. The clinician still reviews and approves.
- **Risk determination from session audio.** The digest flags language for human attention. It never assesses.

## 16. Open questions for the implementation session

1. Does `compiler.py`'s docx and pdf text extraction shell out to macOS `textutil`, and does that survive being called from a background worker thread? Check before reusing.
2. Does parakeet's chunked transcribe expose per-chunk timestamps? The digest's `start_min` fields and rung 5-style chaptering depend on it. If not, derive offsets from chunk boundaries.
3. Where should the source panel live in the existing records UI: on the client record, on the session timeline, or both?
4. Does the existing trash implementation handle directories, or only files? `delete_source` removes a directory.
