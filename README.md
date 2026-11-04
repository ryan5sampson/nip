# Footage Flow

Footage Flow preps footage for editing. Drop videos into a folder; Footage Flow transcribes them, and NIP, its assistant, finds the takes and commands you spoke while filming and helps you cut the footage down. It exports cleaned-up clips ready for CapCut.

NIP runs on Claude. The code, the `nip` command and the `~/NIP` folder keep the short name.

## Set up (Apple Silicon Mac)

```bash
brew install ffmpeg
```

```bash
python3 -m venv .venv && .venv/bin/pip install -e .
```

Homebrew's Python won't install packages globally, hence the venv. Run Footage Flow as `.venv/bin/nip`, or activate the venv first.

The first transcription downloads the Whisper model (about 1.6 GB).

## Use

Run `nip` (same as `nip view`). It opens Footage Flow in your browser and keeps processing the inbox while it runs.

For a double-click app, make one on your Desktop once:

```bash
osacompile -o ~/Desktop/"Footage Flow.app" -e 'do shell script quoted form of "'"$PWD"'/open-nip.sh"'
```

Opening it starts Footage Flow in the background, restarting it if it's already running so the latest code loads, and opens it in your browser after about 10 seconds. You can also bookmark `http://127.0.0.1:8765/` and refresh it while Footage Flow runs. Its output goes to `~/NIP/nip.log`.

1. **Drop footage in the inbox** (`~/NIP/inbox/`, or click *Open inbox* in the app). Each loose video becomes its own project. A folder of files becomes one project named after the folder. New footage shows up in the library within a minute or so.
2. **Inbox and library.** Every project is a card with a thumbnail, description, filmed and kept length, and its clip counts: kept (green), not marked yet (white) and cut (red). Footage moves through four stages, each with its own list in the sidebar: **Inbox** (new; opening it to look, or naming it, doesn't move it), **Processing** (your first edit in it moves it here, or click **Start Processing**), **Processed** (you clicked **Mark Complete** after going through every clip) and **Exported**. Drag a card onto any stage in the sidebar to move it there, back to the Inbox too. The app opens on the Inbox. In the Inbox or Processing, drag a card onto another to bundle them into one project and name the bundle; the videos are ordered by when they were filmed. Add footage with **Add Videos…** in the sidebar (a Finder window; the videos you pick move into the inbox), or drop videos anywhere on the window (they're copied in, and a file with the same name and size in Downloads goes to the Trash). Move the mouse across a card's picture to scrub through its footage without opening it. Its pencil button names it (bundled or not), and its text button shows everything said in it, to tell clips apart without sound. The trash button on any card moves its footage and edits to the Trash straight away (drag the folder back into `~/NIP/library` to undo); exports stay. An exported project's **Clear** button does the same. Search finds anything in a project: description, tags, clip names, clip descriptions, notes, and every word said. Click a tag to filter.
3. **Review the pre-edit.** A project opens on the cut version your voice commands made. The timeline shows only kept footage; everything removed (earlier and scratched takes, footage before the first take, the command words, pauses, silences, "cut everything up to here") is folded into a small bar showing its length. Playback plays the edit and skips those bars. Click a bar to open it: it plays through, and lists why each part was cut, with **Keep** or **Restore** to bring it back. Click a clip on the timeline, or press <kbd>N</kbd> with the playhead on it, to name it ("curve throw in slow motion"); naming a clip marks it keep. You can also play, keep or cut it there. <kbd>Shift</kbd>+<kbd>N</kbd> writes a note for NIP. Footage has three tiers: **cut** (red, folded away), **keep** (green: "NIP, keep", highlights, and what you mark), and **not marked** (plain). For a second pass, go through the plain footage: tap <kbd>C</kbd> or <kbd>G</kbd> to cut or keep the whole clip under the playhead (<kbd>G</kbd> on your own keep unmarks it); hold it to play and cut or keep just what plays while held; or <kbd>Shift</kbd>+<kbd>C</kbd> / <kbd>Shift</kbd>+<kbd>G</kbd> to cut or keep from the start of the clip up to the playhead. When you mark a project complete or export it with footage still unmarked, NIP says how many stretches in how many clips and how much footage, and asks whether to cut or keep it. Also set a take's in and out with <kbd>I</kbd>/<kbd>O</kbd>, keep or drop takes with <kbd>K</kbd>, rename clips, and fix the description and tags. <kbd>Space</kbd> plays, <kbd>←</kbd>/<kbd>→</kbd> skip 2s (<kbd>Shift</kbd> for 10s). Your changes sit on top of the voice edit, so `nip reparse` keeps them; *Info → Reset My Changes* goes back to the voice edit.
4. **Leave edit notes.** Wherever something should change, pause and type (or press the mic and say) what you want, like "cut the stumble" or "bring back the last line". <kbd>N</kbd> jumps to the note box. Each note is pinned to that moment. Go through the whole video, then hit **Apply Notes** in the Notes tab: NIP reads the notes with the transcript and your current edit, and sends back edit operations (cut, restore a cut, keep or drop a take, move a take's in or out, rename a clip). Footage Flow checks each one before applying it. Each note then shows what changed, or why it couldn't be done. Switch on **Apply instantly** to have each note done as soon as you add it. **Undo Last Apply** reverts the last batch. Speech uses your browser's built-in recognition; in Chrome that audio goes to Google.
5. **Edit with NIP** for talking-heavy footage. Write a brief like "give me a 30 second video where I explain why Linderballs are great". NIP reads the transcript and your clip descriptions, picks lines and described clips, and saves the result as an edit you can preview and export as one video. It never sees the video, only what was said and how you described it.

   NIP runs on Claude through [Claude Code](https://claude.com/claude-code), so they count against your Claude plan's usage, not a separate bill. Sign Claude Code in once:

   ```bash
   claude
   ```

   Then type `/login` and follow the browser steps. If Claude Code isn't installed, Footage Flow falls back to the Anthropic API (set `ANTHROPIC_API_KEY`; billed per use in the Anthropic Console).

6. **Mark Complete, then Export.** When you've gone through every clip, **Mark Complete** moves the project to Processed (click it again to reopen). **Export** makes the kept takes into numbered clips, or an edit by NIP into one video, and moves the project to Exported. *Show exports* opens the folder.

The terminal commands still work: `nip ingest` once, `nip watch` to keep checking, `nip report`, `nip reparse`, and `nip export` (add `--combined` for one joined video too).

Exports land in `~/NIP/exports/<project>/`. Originals stay untouched in `~/NIP/library/<project>/raw/`.

Exports are sized for Instagram Reels, TikTok and YouTube Shorts: 1080×1920 for vertical footage (the short side is scaled to 1080, never up), the original frame rate, and HEVC at 16 Mbps with iPhone HDR kept. That's an editing copy for CapCut, about a fifth the size of iPhone 4K. In CapCut, export at 1080p with the source frame rate. Each platform re-encodes what you upload.

`report`, `reparse`, and `export` act on the newest project by default. Pass part of a folder name to pick another, for example `nip export linderball`.

To use a Google Drive folder as the inbox so you can upload from your phone, set `NIP_INBOX`. Footage Flow moves files out of the inbox, so they're removed from Drive after ingest.

```bash
export NIP_INBOX="$HOME/Library/CloudStorage/GoogleDrive-you@gmail.com/My Drive/NIP Inbox"
```

## Voice commands

Start every command with "NIP" and pause for a second or two after it, so NIP knows where the command ends and your speech begins.

| Say | What NIP does |
|---|---|
| "NIP, new clip" / "NIP, clip two, curve throw" | Starts a new clip, optionally named. Its takes compete only with each other, and the best one is exported as `01_curve-throw_t3.mp4`. Footage right after it counts as take one. |
| "Take one", "Take two", … | Starts a take. Say it as its own phrase, with a short pause before and after, or as "NIP, take two". The last take in a run is kept. Without "new clip", starting again from the same or a lower number starts a new section; after "new clip", a repeated number is just another take. |
| "Scratch that" / "Scrap that" / "Reset" | Drops the current take. "Reset" needs a pause on both sides, or "NIP" before it. |
| "NIP, this is …" | Sets the project description. The project folder is renamed to match. |
| "NIP, tag …" | Adds a tag. |
| "NIP, best take" / "NIP, that's the best one" | Keeps this take instead of the last one in the clip. |
| "NIP, pause" … "NIP, resume" | Cuts everything between them. A pause with no resume cuts to the end of the take. |
| "NIP, cut the silence" | Shortens every pause longer than 1 second in this take. |
| "NIP, mark that" / "highlight" | Adds a marker. |
| "NIP, cut" / "NIP, cut that" | Cuts everything since the last NIP command (or the start of the take). |
| "NIP, keep" / "NIP, keep that" | Marks everything since the last NIP command keep (green). |
| "NIP, let me try that again" | Cuts back to the last pause of at least 1 second. |
| "NIP, cut everything up to here" | Cuts from the start of the current clip or take up to here. |
| "NIP" (nothing after), or "NIP split" | A divider: a reference point for "NIP, cut", "NIP, keep" and "tag that". It also splits the footage there into two clips in the editor, neither cut nor kept. In the editor, **Split** (S) does the same by hand. |
| "NIP, tag that as …" | Like "NIP, keep", with a label. |
| "NIP, clip the last 10 seconds" / "NIP, cut the last 5 seconds" | Marks those seconds keep, or cuts them. |
| "NIP, that was …" | Adds a note on what you just filmed to the report. |
| "NIP, …" anything else | Listed as not understood in the report, for now. |

NIP always cuts the command words themselves. Footage before the first "take" is dropped. Videos without any take or clip commands are kept whole.

## Tuning

Command rules live in `nip/commands.py`. After changing them, run `nip reparse` to re-apply them to the saved transcripts without transcribing again.
