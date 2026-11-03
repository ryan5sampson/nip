# NIP

NIP preps footage for editing. Drop videos into a folder; NIP transcribes them, finds the takes and commands you spoke while filming, and exports cleaned-up clips ready for CapCut.

## Set up (Apple Silicon Mac)

```bash
brew install ffmpeg
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Homebrew's Python won't install packages globally, hence the venv. Run NIP as `.venv/bin/nip`, or activate the venv first.

The first transcription downloads the Whisper model (about 1.6 GB).

## Use

1. Put footage in `~/NIP/inbox/`. Loose files become one project named after today's date. A folder of files becomes one project named after the folder, so you can group several videos.
2. Run `nip ingest` once, or `nip watch` to keep checking the inbox.
3. Read what NIP found: `nip report`.
4. Export the kept takes as numbered clips: `nip export`. Add `--combined` for one joined video too.

Exports land in `~/NIP/exports/<project>/`. Originals stay untouched in `~/NIP/library/<project>/raw/`.

`report`, `reparse`, and `export` act on the newest project by default. Pass part of a folder name to pick another, for example `nip export linderball`.

To use a Google Drive folder as the inbox so you can upload from your phone, set `NIP_INBOX`. NIP moves files out of the inbox, so they're removed from Drive after ingest.

```bash
export NIP_INBOX="$HOME/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/NIP Inbox"
```

## Voice commands

Start every command with "NIP" and pause for a second after it, so NIP knows where the command ends and your speech begins.

| Say | What NIP does |
|---|---|
| "NIP, new clip" / "NIP, new clip curve throw" | Starts a new clip, optionally named. Its takes compete only with each other, and the best one is exported as `01_curve-throw_t3.mp4`. Footage right after it counts as take one. |
| "Take one", "Take two", … | Starts a take. Say it as its own phrase, with a short pause before and after, or as "NIP, take two". The last take in a run is kept. Without "new clip", starting again from the same or a lower number starts a new section; after "new clip", a repeated number is just another take. |
| "Scratch that" / "Scrap that" / "Reset" | Drops the current take. "Reset" needs a pause on both sides, or "NIP" before it. |
| "NIP, this is …" | Sets the project description. The project folder is renamed to match. |
| "NIP, tag …" | Adds a tag. |
| "NIP, best take" / "NIP, that's the best one" | Keeps this take instead of the last one in the clip. |
| "NIP, pause" … "NIP, resume" | Cuts everything between them. A pause with no resume cuts to the end of the take. |
| "NIP, cut the silence" | Shortens every pause longer than 1 second in this take. |
| "NIP, mark that" / "highlight" | Adds a marker. |
| "NIP, cut that" / "NIP, let me try that again" | Cuts back to the last pause of at least 1 second. |
| "NIP, …" anything else | Listed as not understood in the report, for now. |

NIP always cuts the command words themselves. Footage before the first "take" is dropped. Videos without any take or clip commands are kept whole.

## Tuning

Command rules live in `nip/commands.py`. After changing them, run `nip reparse` to re-apply them to the saved transcripts without transcribing again.
