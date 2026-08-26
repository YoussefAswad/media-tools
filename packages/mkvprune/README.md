# mkvprune

Interactively prune audio and subtitle tracks from a Matroska file, normalize
non-passthrough **multi-channel** audio to Dolby Digital (AC-3), and rewrite the file
**without touching the video stream or any container metadata**.

The tool is built for 4K UHD remuxes with Dolby Vision. The overriding rule is that the
video stream and all DV / HDR10 / HDR10+ metadata survive bit-exact:

- The video is **never** re-encoded and **never** passed through a filter graph.
- The final mux is done with **`mkvmerge`**, not ffmpeg — ffmpeg's Matroska muxer is
  unreliable for round-tripping the Dolby Vision configuration record. FFmpeg is used
  **only** to encode individual audio tracks into standalone intermediate files.
- Chapters, attachments (fonts/cover art), global & per-track tags, track names, language
  tags (including IETF/BCP-47), delays, and the video track UID/codec/resolution/frame
  rate are all preserved.

## Requirements

External programs (must be on `PATH`; checked at startup with an actionable error):

- `mkvmerge`, `mkvpropedit` (from **mkvtoolnix**)
- `ffmpeg`, `ffprobe` (from **ffmpeg**)

Python **3.11+**. No third-party Python runtime dependencies. **No network access** is used.

## Install

```bash
python -m venv .venv && . .venv/bin/activate
pip install -e ".[dev]"    # drop [dev] if you don't want pytest/ruff
```

This installs the `mkvprune` console command.

## Usage

```
mkvprune [OPTIONS] INPUT           # a .mkv/.mp4/.m4v/.mov file, or a directory to batch

  --scratch-dir DIR   Directory for the temp mux and audio encodes (default: INPUT's dir)
  --audio IDS         Non-interactive audio selection (same syntax as the prompt)
  --subs IDS          Non-interactive subtitle selection
  --add-sub FILE      Add an external subtitle file; language is derived from the filename
                      and the first added sub becomes the default subtitle. Repeatable.
  -y, --yes           Skip the final confirmation
  --dry-run           Print the exact mkvmerge/ffmpeg commands and change nothing
  --keep-original     Write INPUT.pruned.mkv alongside the source instead of replacing it
  --keep-temp         Do not clean up intermediates on success or failure
  --deep-verify       Full decode pass during verification (slow)
  --ac3-bitrate RATE  Flat AC-3 bitrate override (default: channel-based ladder)
  --eac3-bitrate RATE Reserved (see Assumptions); >6ch takes the AC-3 compat path
  --group-by MODE     Batch: how strictly two files count as the same layout.
                      'name' (default) compares normalized track names; 'structure' ignores
  --no-recursive      Batch: only scan the top level of the directory
  --no-progress       Disable progress bars
  -v, --verbose
```

**Input:** a `.mkv`, `.mp4`, `.m4v`, or `.mov` file, or a **directory** (see
[Batch mode](#batch-mode-a-directory-of-files)). **Output is always `.mkv`.** For an mkv source
the file is replaced in place (atomically); for a non-mkv source, `INPUT.mkv` is written and
the original is removed after a successful, verified write (use `--keep-original` to keep it).

### Batch mode (a directory of files)

Point mkvprune at a directory and it prunes every supported file underneath it (recursively;
`--no-recursive` limits the scan to the top level), audio conversion included:

```
mkvprune "/media/Shows/Band of Brothers"
```

Track IDs are **not** stable across files. One episode with an extra subtitle track shifts
every ID after it, so replaying one file's ID selection against another would silently keep
the wrong tracks. Instead, files are grouped by a **layout signature** — each track's kind,
codec, channel count, language, flags, and (by default) its name, whitespace- and
case-normalized — and you are prompted **once per group**. Answer `skip` at a prompt to leave
a whole group untouched.

Track names drift cosmetically between episodes ("for Audio commentary…" vs "Audio
commentary…"), which splits groups for no real reason. When two groups differ *only* by
names, mkvprune says so and `--group-by structure` merges them:

```
3 track layout(s) found:
  Group 1: 8 file(s) — 1 video, 2 audio, 3 subtitle
  Group 2: 1 file(s) — 1 video, 2 audio, 4 subtitle
  Group 3: 1 file(s) — 1 video, 2 audio, 3 subtitle

  note: group(s) 1 and 3 differ only in track names.
        --group-by structure would merge them into one prompt.
```

Files are processed one at a time, and **the batch stops at the first failure**: a
verification failure usually means an assumption is wrong, and continuing would repeat it on
every remaining file. Finished files stay finished, the failing file's original is untouched
with its temp kept for inspection, and the summary names everything left unprocessed.

Two guardrails preserve the fail-loud contract:

- `--audio`/`--subs` are accepted only when the directory holds exactly **one** layout — an
  ID means something different in each group.
- `--add-sub` is rejected for a directory: one subtitle file cannot be right for every input.

### Selection syntax

At the prompt (or via `--audio`/`--subs`):

- IDs: `1,3,4` · ranges: `1-3` · mixed: `1,3-5,8`
- `all` · `none` · empty input → `all` · `q` → abort (exit 130), no changes

IDs are **mkvmerge track IDs** as shown in the table. Bad input re-prompts (it never
crashes). At least one audio track is required unless you type `none` explicitly; subtitles
may be empty. If **both** `--audio` and `--subs` are given, the run is fully non-interactive
(no confirmation prompt).

### Examples

```bash
# Interactive: choose tracks, see the conversion plan, confirm
mkvprune movie.mkv

# Preview the exact commands without changing anything
mkvprune --dry-run movie.mkv

# Non-interactive: keep audio 1 & 3, keep English subs (id 4), replace in place
mkvprune -y --audio 1,3 --subs 4 movie.mkv

# Keep the original; write movie.pruned.mkv; encode intermediates to a fast scratch disk
mkvprune --keep-original --scratch-dir /mnt/nvme/scratch movie.mkv

# Convert an mp4 to a pruned mkv (source mp4 removed after verification)
mkvprune -y --audio all --subs none movie.mp4

# Add an external subtitle as the default track (language derived from the filename)
mkvprune -y --audio all --subs none --add-sub "Movie.spa.forced.srt" movie.mkv
```

### External subtitles (`--add-sub`)

`--add-sub FILE` muxes an external subtitle file into the output and makes it the **default**
subtitle (clearing the default flag on every other subtitle). The language is derived from
the filename the way MKVToolNix does — the last recognized ISO-639 code among the
dot/underscore/dash-separated tokens:

- `Movie.en.srt` → `en` · `Movie.spa.srt` → `spa` · `Show.S01E01.de.sup` → `de`
- a `forced` / `foreign` token sets the forced flag; an `sdh` / `cc` / `hi` token sets the
  hearing-impaired flag (e.g. `Movie.spa.forced.srt` → Spanish, forced)
- if no language token is recognized, the track is tagged `und` (with a warning)

The flag is repeatable; only the **first** added subtitle is made default. Added subtitle
files are treated as inputs — they are never deleted or moved.

### Progress

Progress bars for the audio encodes and the final mux are shown on an interactive terminal.
They are automatically disabled when stderr is not a TTY, under `--verbose` (which prints the
commands instead), during `--dry-run`, or with `--no-progress`.

## Track identification

Two probes are reconciled: `mkvmerge -J` is the source of truth for the **mkvmerge track
IDs** shown in the table (the IDs `--audio-tracks` / `--subtitle-tracks` accept), and
`ffprobe` supplies audio detail and DV/HDR side data. ffprobe stream indices and mkvmerge
track IDs are not interchangeable, so tracks are matched by ordinal within the file's real
stream order, with a codec-family cross-check that fails loudly rather than guessing.

Streams that are not tracks are excluded from that ordering: ffprobe `attachment` and `data`
streams, and **embedded cover art** — which ffmpeg models as a single-frame video stream
flagged `attached_pic`, but mkvmerge (correctly) reports as an attachment. This is what makes
MP4/M4V/MOV inputs with embedded artwork line up; the artwork itself is preserved, as an MKV
attachment.

## Codec policy

Classification is done **strictly on the probe's codec identity** (mkvmerge `codec_id` /
ffprobe `codec_name`) — never on the human-readable track title. A track *named* "Dolby
Digital 5.1" that is actually FLAC is still treated as FLAC.

### Passthrough (never re-encoded)

| Codec | Notes |
|-------|-------|
| AC-3 | Dolby Digital |
| E-AC-3 | Dolby Digital Plus, incl. Atmos/JOC |
| TrueHD | incl. Atmos |
| DTS (all variants) | DTS, DTS-ES, DTS-HD HRA, DTS-HD MA, DTS:X |
| MLP (non-TrueHD) | passthrough — see Assumptions |

### Convert / keep

Only **multi-channel (> 2 channel)** non-passthrough tracks are re-encoded. Mono and stereo
tracks in any codec (FLAC, AAC, Opus, …) are **kept as-is** — transcoding stereo to AC-3
wastes bits for no compatibility gain (see Assumptions).

| Source (non-passthrough) | Action |
|--------------------------|--------|
| ≤ 2 channels (mono/stereo) | **keep as-is** (no re-encode) |
| 3–6 channels | **→ AC-3** (channel-based bitrate ladder) |
| > 6 channels (7.1+) | **keep original + add a downmixed 5.1 AC-3 compatibility track** |
| unknown channel count | keep as-is (don't guess), with a warning |

Channel count and layout are preserved for the ≤6-channel conversions (no up/downmix).
Sample rates unsupported by AC-3 (e.g. 96/192 kHz on some FLAC/LPCM tracks) are resampled to
48 kHz with a warning. A converted track **replaces** its original; a compatibility downmix
is **added** alongside the untouched original and is never marked default.

### Bitrate ladder (default)

| Layout | AC-3 bitrate |
|--------|--------------|
| 1.0 | 224k |
| 2.0 | 448k *(only relevant with `--ac3-bitrate`; stereo is kept, not converted)* |
| 5.1 (and 3–6 ch) | 640k (AC-3 maximum) |

`--ac3-bitrate RATE` overrides the ladder with a flat value for every AC-3 conversion
(capped at 640k, the AC-3 maximum). The 5.1 compatibility downmix uses 640k.

### Default-track flag resolution

Applied independently to audio and subtitles, after selection:

1. If the source default of that type survived → keep it.
2. Otherwise the first kept **English** track (`en` / `eng` / `en-*`).
3. Otherwise the first kept track.
4. No kept tracks of that type → no-op.

Exactly one default per type is written; it is explicitly cleared on all others. **`forced`
flags are never modified** — they are carried through as-is. Language, track name, delay
(`--sync`), and all flags are carried onto converted tracks by mkvmerge.

## Verification (before any replacement)

Every check must pass; on any failure the temp file is left in place, the **original is left
completely untouched**, the failing check is printed with both values, and the exit code is
non-zero.

1. `mkvmerge` exit ≤ 1 and `mkvmerge -J` re-parses the output.
2. Track count and per-type composition match the plan.
3. Container duration within ±0.5% **and** ±1s of the source.
4. Video codec ID, resolution, frame rate, and (where reported) frame count identical.
5. **DV/HDR parity:** DOVI configuration side data (`dv_profile`, `dv_level`,
   `rpu_present_flag`, `el_present_flag`, `bl_present_flag`,
   `dv_bl_signal_compatibility_id`), mastering-display & content-light side data, colour
   identification (primaries/transfer/matrix/range), and mkvmerge video block-addition /
   colour properties are all identical.
6. Chapter count and attachment count match.
7. Exactly one default flag per track type, on the intended track (checked positionally
   against the `--track-order`).
8. Output size within a sane band (guards against truncation).
9. Container integrity: `ffmpeg -c copy -f null -` (cheap) by default; `--deep-verify` runs
   the full decode `ffmpeg -f null -` instead.

## Safety

- The final placement is atomic: `os.replace()` on the same filesystem, or copy-into-dest +
  `fsync` + `os.replace()` across filesystems. The destination path is never left partially
  written.
- A pre-existing `.tmp.mkv` is refused, not clobbered. Free scratch space is checked before
  starting.
- `SIGINT`/`SIGTERM` kill child processes and remove intermediates. **The source is never
  left deleted or truncated** under any signal or exception.
- Source mtime and permissions are preserved on the output (best-effort).

## Assumptions (the spec's open questions, with the choices made)

1. **7.1+ conversion (encoders cap at 6 channels).** *Chosen: keep the original untouched
   and add a downmixed 5.1 AC-3 compatibility track, with a prominent warning.* Note: the
   installed ffmpeg silently auto-downmixes 7.1→5.1 for `ac3`/`eac3` (it does **not** error),
   so mkvprune detects >6-channel tracks itself and performs an **explicit** downmix rather
   than trusting the encoder — otherwise you'd get a 5.1 track mislabeled as 7.1.
2. **Bitrate.** *Chosen: channel-based ladder* (1.0→224k, 2.0→448k, 5.1→640k), overridable
   with a flat `--ac3-bitrate`.
3. **Stereo/mono.** *Chosen: keep as-is.* Only multi-channel (>2ch) non-passthrough tracks
   are re-encoded.
4. **MLP (non-TrueHD).** *Chosen: passthrough.*
5. **"classify by real codec, not by name."** The parenthetical "(not Dolby or DTS)" in the
   original brief is read as *classify by the real codec reported by the probe, not by the
   track title.*
6. **DTS-HD MA / TrueHD core extraction** (adding a compatibility AC-3 by extracting the
   core) — **out of scope**; those tracks are passed through untouched.
7. **Batch mode** over a directory/glob — **out of scope for v1.**
8. **mp4/mov input → mkv output** is supported (added on request). Output is always mkv;
   a non-mkv source is removed after a successful, verified write unless `--keep-original`.

### A note on testing DV bit-exactness

True Dolby Vision bit-exactness (§0) can only be exercised on a real DV remux, which cannot
be synthesized with ffmpeg `lavfi`. The verification logic is unit-covered with recorded
side-data structures and exercised end-to-end on generated HDR10 fixtures (mastering
display + colour identification), and the video is copied 1:1 by mkvmerge so its bitstream —
including the DV RPU and configuration record — is never modified.

## Development

```bash
pytest        # tiny fixture MKVs are generated at test time via ffmpeg; no media committed
ruff check .
```

## License

MIT
