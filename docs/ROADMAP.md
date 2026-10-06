# Roadmap

Where the song features are going. The core playlist tooling (sync, query,
reorder, clean, Watch Later import) is described in [`SPEC.md`](SPEC.md).

## Stage A — Last.fm similarity ✅

`byp match` / `byp similar` / `byp discover` (see the README). Similarity is
collaborative: "listeners who like X also like Y", from Last.fm's
`track.getSimilar`. Strong on cultural/scene links across genres; weak on
obscure tracks (slowed/sped-up edits, small uploaders), where Last.fm has no
data and the commands return nothing.

## Stage B — audio-embedding similarity

"Sounds like" similarity (tempo, timbre, energy, mood) from the audio itself,
to cover what Stage A can't: tracks Last.fm doesn't know, and "more with this
vibe" across artists nobody links together.

**Pipeline**

1. **Download** each live song's audio with yt-dlp.
2. **Decode/resample** with ffmpeg to the rate and channel layout the chosen
   model expects.
3. **Embed** with a pretrained music model; keep one vector per song in a
   `track_embeddings(video_id, model, vector BLOB, embedded_at)` table.
4. **Delete the audio** immediately — only vectors are stored.
5. **Query**: cosine nearest neighbours. At ~800 songs, brute force in numpy is
   instant; no vector index or pgvector needed.
6. **Combine** with Stage A: rank by Last.fm when it has data, fall back to (or
   blend in) audio similarity when it doesn't.

**Cost.** ~800 songs × ~3.5 min ≈ 2–3 GB of audio streamed through, never kept.
First run is a one-off batch of a few hours on a laptop; afterwards only newly
added songs. The real costs are not compute: downloading audio from YouTube is
against its Terms of Service (grey area for personal use), and it means
maintaining an ML pipeline rather than an API call.

**Design notes from the thesis** (`proyecto-grado/tesis`, the OpenFING lecture
RAG pipeline, which used yt-dlp + ffmpeg + Whisper):

- Reuse its yt-dlp Python-API options
  (`backend/services/downloader/core/downloader_service.py:130-152`):
  `format: bestaudio/best`, an `FFmpegExtractAudio` postprocessor,
  `retries`/`fragment_retries: 3`, `continuedl`.
- Reuse the per-item try/except so one failed video doesn't abort the batch —
  but record the failure (like `import_failures`) instead of swallowing it.
- Reuse the scratch-space pattern from `scripts/download_transcrive.py:53-80`:
  resample into `/dev/shm`, process, then delete both the download and the
  temp WAV.
- Change the resample target: it used 16 kHz mono (right for Whisper); music
  models typically want 44.1/48 kHz — set it per model.
- Add what it didn't need (it mostly fetched university-hosted MP4s):
  `download_archive` keyed by video id rather than a filename-glob skip check,
  `sleep_interval`/`sleep_requests` throttling, and `cookiesfrombrowser`
  (already used by `import-watch-later`) for age-gated videos.
- Optionally embed a 30–60 s excerpt instead of the whole track to cut download
  and compute time.

**Open decisions** (when Stage B starts): the model — e.g. CLAP or Essentia's
Discogs-EffNet — after checking its license and expected input; whether to
embed excerpts or full tracks; whether the heavy deps (torch/essentia) live in
an optional dependency group.

## Stage C — the "play" wrapper

A front end over A/B: "play songs similar to this one", "play everything by
X". Likely output: a queue of watch URLs, or a temporary YouTube playlist
(`playlistItems.insert` is 50 quota units per song, so a ~20-song queue costs
~1,000 units).
