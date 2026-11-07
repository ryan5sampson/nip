"""nip view: the local NIP app. Inbox, review and adjust takes, Claude edits, the clip library. Also keeps ingesting the inbox."""
import copy
import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from nip import brief, cli, ideas
from nip.commands import kept_ranges, respell, tiers, timeline, unmarked

PAGE = Path(__file__).with_name("viewer.html")
LOCK = threading.Lock()  # one writer at a time for project.json and the .nip.json files
STATE = {"transcribing": 0}  # videos the background ingest is working on
SCAN = {"thread": None}  # the library scan, one at a time
NO_DRIVE = "Google Drive isn’t answering, so nothing was deleted. Try again in a moment."
SAVES, JOBS = [], queue.Queue()  # footage being completed, newest last, kept until dismissed; and what's left to do, one at a time
DOWNLOADS = Path.home() / "Downloads"
PICK_FOLDER = 'POSIX path of (choose folder with prompt "Choose your shared Footage Flow folder in Google Drive")'
PICK = """set picked to choose file of type {"public.movie"} with prompt "Add videos to Footage Flow" with multiple selections allowed
set out to ""
repeat with f in picked
  set out to out & POSIX path of f & linefeed
end repeat
out"""


def picked():
    r = subprocess.run(["osascript", "-e", PICK], capture_output=True, text=True)  # Cancel exits 1 with nothing picked
    return [v for v in map(Path, r.stdout.splitlines()) if v.suffix.lower() in cli.VIDEO and v.is_file()]


def pick_videos():
    """The macOS file picker; the videos picked move into the inbox."""
    return [cli.move_free(v, cli.INBOX).name for v in picked()]


def upload(handler, name):
    """A video dropped on the page, streamed into the inbox. A browser can't move the original, so the same file in
    Downloads (same name and size) goes to the Trash."""
    if Path(name).name != name or Path(name).suffix.lower() not in cli.VIDEO:
        raise ValueError("only .mov, .mp4 and .m4v videos can be added")
    size = int(handler.headers.get("Content-Length", 0))
    cli.INBOX.mkdir(parents=True, exist_ok=True)
    part = cli.INBOX / f".{name}.{time.time_ns()}.part"  # hidden, and not a video suffix, so the inbox waits until it's whole
    try:
        with part.open("wb") as f:
            left = size
            while left > 0 and (chunk := handler.rfile.read(min(1 << 20, left))):
                f.write(chunk)
                left -= len(chunk)
        if part.stat().st_size != size:
            raise ValueError(f"{name} didn't finish uploading; try again")
        dest = cli.move_free(part, cli.INBOX, name)
    finally:
        part.unlink(missing_ok=True)
    original = DOWNLOADS / name
    if original.is_file() and original.stat().st_size == size:
        cli.move_free(original, cli.TRASH)
    return dest.name


def clips_of(proj):
    info = cli.load(proj / "project.json")
    out, spellings = [], cli.vocabulary()[1]
    for stem in info["clips"]:
        c = cli.load(proj / f"{stem}.nip.json")
        c["words"] = respell(c["words"], spellings)  # at read time: saved transcripts stay as Whisper heard them
        e = cli.decisions(c)
        segments = timeline(e, c["duration"], c["words"])
        if cli.stage_of(info) == "completed":  # only the kept clips and what's said in them; what was cut is gone
            segments = [s for s in segments if not s["gap"]]
            c["words"] = [w for w in c["words"] if any(s["a"] <= (w["start"] + w["end"]) / 2 < s["b"] for s in segments)]
        out.append({"stem": stem, "source": c["source"], "duration": c["duration"],
                    "words": c["words"], "edit": e, "unmarked": unmarked(e), "segments": segments,
                    "edit_notes": c.get("edit_notes", []), "can_undo": bool(c.get("undo_notes")),
                    "undo": len(c.get("undo", [])), "redo": len(c.get("redo", []))})
    return info, out


def kept_seconds(clip):
    e = clip["edit"]
    return sum(b - a for t in e["takes"] if t["keep"] for a, b in kept_ranges(t, e["cuts"]))


def summary(proj):
    """One library card: what the project is, how much footage, and text to search."""
    info, clips = clips_of(proj)
    desc, tags = info.get("my_description") or info["description"], info.get("my_tags", info["tags"])
    notes = [n["text"] for c in clips for n in c["edit"].get("notes", [])]
    names = sorted({t["name"] for c in clips for t in c["edit"]["takes"] if t.get("name")})
    labels = [l["text"] for c in clips for l in c["edit"]["labels"]]
    ts = [tiers(c["edit"], c["duration"]) for c in clips]
    return {"name": proj.name, "description": desc, "named": bool(info.get("my_description")), "tags": tags, "videos": len(clips),
            "clips": names, "filmed": sum(c["duration"] for c in clips), "kept": sum(map(kept_seconds, clips)),
            "notes": len(notes),
            "stories": len(info.get("stories", [])), "modified": proj.stat().st_mtime,
            "tiers": {k: sum(len(t[k]) for t in ts) for k in ("keep", "unmarked", "cut")},
            "said": " ".join(w["w"] for c in clips for w in c["words"])[:brief.ASSIST_SAID], "stage": cli.stage_of(info), "unsaved": bool(info.get("unsaved")),
            "unsorted": sum(c["edit"].get("reusable") is None for c in clips),  # videos not sorted by voice while filming
            "stems": info["clips"], "edits": [s["title"] for s in info.get("stories", [])],
            "media": [[c["source"], s["a"], s["b"] - s["a"]] for c in clips for s in c["segments"]],
            "search": " ".join([proj.name, desc or "", *tags, *names, *notes, *labels,
                                *(w["w"] for c in clips for w in c["words"])]).lower()}


def project_data(proj):
    info, clips = clips_of(proj)
    return {**info, "name": proj.name, "clips": clips, "stage": cli.stage_of(info),
            "description": info.get("my_description") or info["description"],
            "tags": info.get("my_tags", info["tags"]), "stories": info.get("stories", [])}


def edited(proj, clips=True):
    """The project after an edit by hand or by NIP; the first edit moves Inbox footage to Processing. Completed footage stays
    in Completed, marked unsaved until its new clips replace the old ones in the library. clips=False: this edit left them alone."""
    with LOCK:
        info = cli.load(proj / "project.json")
        if cli.stage_of(info) == "inbox":
            cli.set_stage(proj, "processing")
        elif clips and info.get("finalized"):
            cli.save(proj / "project.json", {**info, "unsaved": True})
    return project_data(proj)


def settle(proj, how):
    """Mark every stretch nobody marked yet as cut or keep (how), so the edit is fully decided."""
    if how not in ("cut", "keep"):
        return
    layer = "my_cuts" if how == "cut" else "my_keeps"
    with LOCK:
        for stem in cli.load(proj / "project.json")["clips"]:
            f = proj / f"{stem}.nip.json"
            clip = cli.load(f)
            clip.setdefault(layer, []).extend({"start": a, "end": b} for a, b in unmarked(cli.decisions(clip)))
            cli.save(f, clip)


def apply_notes(f, ids):
    """Send these edit notes to Claude, then apply its checked operations. Claude is asked outside the lock."""
    clip = cli.load(f)
    notes = [n for n in clip.get("edit_notes", []) if n["id"] in ids]
    if not notes:
        raise ValueError("there are no notes to apply")
    try:
        plan = brief.plan_notes(clip, notes)
    except RuntimeError as e:
        raise ValueError(str(e))
    with LOCK:
        clip = cli.load(f)
        clip["undo_notes"] = {"ids": ids, **{k: copy.deepcopy(clip.get(k)) for k in cli.HAND}}
        cli.remember(clip)
        errors = brief.apply_ops(clip, plan["ops"])
        said = {r.get("id"): r for r in plan["notes"] if isinstance(r, dict)}
        for n in clip.get("edit_notes", []):
            if n["id"] in ids:
                r, errs = said.get(n["id"], {}), errors.get(n["id"], [])
                n["status"] = "applied" if r.get("done") and not errs else "failed"
                n["result"] = " ".join([str(r.get("summary") or "NIP didn't report on this note.")]
                                       + [f"Couldn't apply: {e}." for e in errs])
        cli.save(f, clip)


def scan_library():
    """Bring the local index up to date, reading only clips that changed."""
    old = cli.load(cli.INDEX) if cli.INDEX.exists() else {}
    if not cli.FINAL.exists():
        return
    new = {}
    for f in sorted(f for s in cli.SHELVES for f in (cli.FINAL / s).glob("[!.]*/_info/*.json")):
        key = str(f.relative_to(cli.FINAL))
        try:  # one unreadable clip, like a file Drive hasn't finished syncing, mustn't hide the rest
            when = f.stat().st_mtime
            if old.get(key, {}).get("mtime") == when:
                new[key] = old[key]
                continue
            m = cli.load(f)
            new[key] = {**{k: v for k, v in m.items() if k != "words"}, "mtime": when, "stem": f.stem, "folder": f.parent.parent.name, "used": f.parent.parent.parent.name == "Used",
                        "text": " ".join(w["w"] for w in m["words"]), "file": f.with_suffix(Path(m["source"]).suffix).name}
        except (OSError, ValueError, KeyError) as e:
            print(f"library: skipped {key}: {e}")
    if new != old:
        cli.save(cli.INDEX, new)


def final_clips():
    """Every finalized clip, newest bundle first; what the Library searches. A stalled Drive blocks forever inside the
    system, so the scan runs on the side and the last index answers if it's slow, offline or waiting on a permission."""
    if not (SCAN["thread"] and SCAN["thread"].is_alive()):
        SCAN["thread"] = threading.Thread(target=scan_library, daemon=True)
        SCAN["thread"].start()
    SCAN["thread"].join(4)
    return sorted((cli.load(cli.INDEX) if cli.INDEX.exists() else {}).values(), key=lambda c: c["project"], reverse=True)


def library_answers():
    """False if the library's folder doesn't answer within a few seconds."""
    t = threading.Thread(target=lambda: cli.FINAL.mkdir(parents=True, exist_ok=True) or list(cli.FINAL.iterdir()), daemon=True)
    t.start()
    t.join(8)
    return not t.is_alive()


def final_file(folder, name, info=False):
    """A finalized clip, or from _info its thumbnail or preview."""
    f = cli.library_path(folder) / ("_info" if info else "") / name
    if not f.is_file():
        raise FileNotFoundError
    return f


def saving(name):
    return any(j["project"] == name and j["state"] in ("waiting", "saving") for j in SAVES)


def cards():
    """Every project's card. One that can't be read is left out, not the whole library."""
    out = []
    for d in cli.LIBRARY.iterdir() if cli.LIBRARY.exists() else []:
        if (d / "project.json").exists():
            try:
                out.append(summary(d))
            except (OSError, ValueError, KeyError) as e:
                print(f"library: skipped {d.name}: {e}")
    return sorted(out, key=lambda s: s["modified"], reverse=True)


def clean_tags(tags):
    if not isinstance(tags, list) or not all(isinstance(t, str) and len(t) <= 40 for t in tags):
        raise ValueError("tags must be a list of short texts")
    return [t.strip().lower() for t in tags if t.strip()]


def clean_sort(sort):
    """How Mark Complete was told to sort the bundle; None to leave the clips' labels as they are."""
    if sort is None:
        return None
    if not isinstance(sort, dict) or sort.get("mode") not in ("talking", "mixed", "action", "custom"):
        raise ValueError("sort must say talking, mixed, action or custom")
    text = sort.get("text", "")
    if sort["mode"] == "custom" and not (isinstance(text, str) and 0 < len(text.strip()) <= 2000):
        raise ValueError("say how to sort these clips")
    return {"mode": sort["mode"], "tags": clean_tags(sort.get("tags", [])), "text": text}


def why(e):
    if isinstance(e, FileNotFoundError) and e.filename:
        return f"Footage Flow can't find {Path(e.filename).name}."
    return "ffmpeg couldn't cut this footage." if isinstance(e, subprocess.CalledProcessError) else str(e) or type(e).__name__


def save_footage():
    """Complete footage in the background, so the app stays usable. New footage sits in Processing until this succeeds, and
    footage being updated in Completed drops back to Processing if it fails; either way the library stays as it was."""
    while True:
        job, work, failed = JOBS.get()
        job["state"] = "saving"
        try:
            if not library_answers():
                raise ValueError("Google Drive isn’t answering. If your Mac asked to let Footage Flow access Google Drive, allow it, then try again.")
            job["saved"] = work()
            job["state"] = "done"
        except Exception as e:
            traceback.print_exc()
            failed()
            job.update(state="failed", error=why(e))


threading.Thread(target=save_footage, daemon=True).start()


def video_job(key, title, work):
    """Rendering and writing captions takes a minute, so it runs in the background, in the same corner panel as saves."""
    job = {"project": f"video:{key}", "title": title, "state": "waiting", "step": "Waiting its turn", "part": 0, "video": True}
    SAVES[:] = [j for j in SAVES if j["project"] != job["project"]] + [job]
    JOBS.put((job, lambda: work(lambda step, part: job.update(step=step, part=part)), lambda: None))
    return job


def video_thumb(p):
    """A frame from a finished video, kept on this Mac so nothing extra lands in the Drive folder."""
    f = cli.video_file(p)
    out = cli.HOME / "video-thumbs" / f"{hashlib.md5(f'{f}{f.stat().st_mtime}'.encode()).hexdigest()}.jpg"
    if not out.exists():
        out.parent.mkdir(exist_ok=True)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", "1", "-i", str(f), "-frames:v", "1", "-vf", "scale=360:-2", str(out)], check=True)
    return out


def caption_fields(body):
    """What the review screen saves, checked."""
    caps = {k: body.get(k) for k in ("instagram", "tiktok")}
    title, sources = body.get("title"), body.get("sources", [])
    if not isinstance(title, str) or not title.strip() or not all(isinstance(c, str) and len(c) <= 4000 for c in caps.values()):
        raise ValueError("a video needs a title, and captions of up to 4000 characters")
    if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources):
        raise ValueError("sources must be a list of bundles")
    return title.strip(), caps, sources


def thumbnail(proj):
    """A small frame from the first kept take, made once and cached in the project folder."""
    f = proj / "thumb.jpg"
    if not f.exists():
        _, clips = clips_of(proj)
        c = clips[0]
        t = next(((s["a"] + s["b"]) / 2 for s in c["segments"] if not s["gap"]), 0)
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{t:.2f}",
                        "-i", str(proj / "raw" / c["source"]), "-frames:v", "1", "-vf", "scale=360:-2",
                        str(f)], check=True)
    return f


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet; errors still raise
        pass

    def parts(self):
        host, origin = self.headers.get("Host", ""), self.headers.get("Origin")
        if host.split(":")[0] not in ("127.0.0.1", "localhost") or origin not in (None, f"http://{host}"):
            raise ValueError("this request didn't come from Footage Flow")  # any website can send a POST to this Mac; only this page may
        parts = [unquote(p) for p in self.path.split("?")[0].strip("/").split("/")]
        if any(p in (".", "..") or "/" in p or "\\" in p for p in parts):
            raise FileNotFoundError
        return parts

    def project(self, name):
        proj = cli.LIBRARY / name
        if not name or not (proj / "project.json").exists():
            raise FileNotFoundError
        return proj

    def clip_file(self, proj, stem):
        if stem not in cli.load(proj / "project.json")["clips"]:
            raise FileNotFoundError
        return proj / f"{stem}.nip.json"

    def send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, f, kind):
        size = f.stat().st_size
        start, end = 0, size - 1
        m = re.fullmatch(r"bytes=(\d*)-(\d*)", self.headers.get("Range", ""))
        if m and (m[1] or m[2]):  # browsers fetch video in byte ranges to play and seek
            start, end = (int(m[1]), int(m[2] or end)) if m[1] else (max(0, size - int(m[2])), end)
            end = min(end, size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
            self.send_response(206)
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        else:
            self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        with f.open("rb") as fh:
            fh.seek(start)
            left = end - start + 1
            while left > 0 and (chunk := fh.read(min(1 << 20, left))):
                self.wfile.write(chunk)
                left -= len(chunk)

    def do_GET(self):
        try:
            p = self.parts()
            if p == [""]:
                self.send_file(PAGE, "text/html; charset=utf-8")
            elif p == ["api", "status"]:
                self.send_json({"inbox": str(cli.DRIVE_INBOX or cli.INBOX), "transcribing": STATE["transcribing"], "fetching": (cli.HOME / ".fetching").exists(), "setup": not cli.CONFIGURED,
                                "unreadable": sum(1 for f in cli.UNREADABLE.iterdir() if not f.name.startswith(".")) if cli.UNREADABLE.exists() else 0,
                                "claude": "plan" if shutil.which("claude") else
                                "api" if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") else ""})
            elif p == ["api", "saves"]:
                self.send_json(SAVES)
            elif p == ["api", "projects"]:
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering. Check that it’s running on this computer, then try again.")
                self.send_json(cards())
            elif len(p) == 3 and p[:2] == ["api", "project"]:
                self.send_json(project_data(self.project(p[2])))
            elif len(p) == 2 and p[0] == "thumb":
                self.send_file(thumbnail(self.project(p[1])), "image/jpeg")
            elif p == ["api", "clips"]:
                self.send_json(final_clips())
            elif p == ["api", "videos"]:
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so your videos can’t be listed. Try again in a moment.")
                self.send_json(cli.videos())
            elif p == ["api", "ideas"]:
                self.send_json({"ideas": ideas.read(), "statuses": ideas.STATUSES})
            elif len(p) == 3 and p[0] in ("video", "videothumb"):
                f = cli.video_path(f"{p[1]}/{p[2]}")
                self.send_file(video_thumb(f), "image/jpeg") if p[0] == "videothumb" else self.send_file(cli.video_file(f), "video/mp4")
            elif len(p) == 3 and p[0] == "final":
                f = final_file(p[1], p[2], info=True)
                self.send_file(f, "image/jpeg" if f.suffix == ".jpg" else "video/mp4")
            elif len(p) == 3 and p[0] == "media":
                f = self.project(p[1]) / "raw" / p[2]
                if not f.is_file():
                    raise FileNotFoundError
                self.send_file(f, "video/mp4")  # .mov too: Chrome only plays it labelled mp4
            else:
                raise FileNotFoundError
        except FileNotFoundError as e:  # a program NIP runs (ffmpeg, claude) that's missing names itself
            self.send_json({"error": f"Footage Flow can't find {e.filename}; is it installed?" if e.filename else "not found"},
                           500 if e.filename else 404)
        except subprocess.CalledProcessError:
            self.send_json({"error": "ffmpeg couldn't read this video"}, 500)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser dropped a video range it no longer needs
        except Exception as e:  # answer the page instead of dropping the connection
            traceback.print_exc()
            self.send_json({"error": f"Footage Flow hit an error: {e}"}, 500)

    def do_POST(self):
        try:
            p = self.parts()
            if self.command == "PUT":  # PUT, not POST: other websites can't send it to this Mac without asking first
                if len(p) != 3 or p[:2] != ["api", "upload"]:
                    raise FileNotFoundError
                return self.send_json({"added": upload(self, p[2])})
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
            if not isinstance(body, dict):
                raise ValueError("expected a JSON object")
            if len(p) >= 3 and p[1] != "saves" and saving(p[2]):  # its edits are being cut right now, so a change would be lost
                raise ValueError("It’s being saved right now; try again when it’s done")
            if len(p) == 4 and p[:2] == ["api", "override"]:
                proj = self.project(p[2])
                f = self.clip_file(proj, p[3])
                with LOCK:
                    clip = cli.load(f)
                    cli.remember(clip)
                    if body.get("reset"):
                        clip.get("overrides", {}).pop(str(round(float(body.get("at", -1)), 2)), None)
                    else:
                        cli.override_take(clip, body.get("at"), body)
                    cli.save(f, clip)
                self.send_json(edited(proj))
            elif len(p) == 4 and p[:2] == ["api", "notes"]:  # second-round edit notes for Claude
                proj = self.project(p[2])
                f = self.clip_file(proj, p[3])
                act = body.get("action")
                if act == "add":
                    text, t = body.get("text"), body.get("t")
                    if not isinstance(text, str) or not text.strip() or len(text) > 500:
                        raise ValueError("write a note of up to 500 characters")
                    with LOCK:
                        clip = cli.load(f)
                        if t is not None and (not isinstance(t, (int, float)) or not 0 <= t <= clip["duration"]):
                            raise ValueError("a note needs a time inside the video, or none for the whole video")
                        notes = clip.setdefault("edit_notes", [])
                        note = {"id": max((n["id"] for n in notes), default=0) + 1, "t": None if t is None else round(float(t), 2),
                                "text": text.strip(), "status": "pending"}
                        notes.append(note)
                        cli.save(f, clip)
                    if body.get("apply"):
                        apply_notes(f, [note["id"]])
                elif act == "apply":
                    apply_notes(f, [n["id"] for n in cli.load(f).get("edit_notes", []) if n["status"] != "applied"])
                elif act == "delete":
                    with LOCK:
                        clip = cli.load(f)
                        clip["edit_notes"] = [n for n in clip.get("edit_notes", []) if n["id"] != body.get("id")]
                        cli.save(f, clip)
                elif act == "undo":  # put the hand edits back to before the last batch of notes
                    with LOCK:
                        clip = cli.load(f)
                        before = clip.pop("undo_notes", None)
                        if not before:
                            raise ValueError("nothing to undo")
                        for k in cli.HAND:
                            if before.get(k) is None:
                                clip.pop(k, None)
                            else:
                                clip[k] = before[k]
                        for n in clip.get("edit_notes", []):
                            if n["id"] in before["ids"]:
                                n["status"], n["result"] = "pending", "Undone."
                        cli.save(f, clip)
                else:
                    raise ValueError("action must be add, apply, delete or undo")
                self.send_json(edited(proj, act in ("apply", "undo") or bool(body.get("apply"))))
            elif len(p) == 4 and p[:2] == ["api", "cuts"]:  # restore a voice cut, add or remove your own, or reset
                proj = self.project(p[2])
                f = self.clip_file(proj, p[3])
                with LOCK:
                    clip = cli.load(f)
                    act, start = body.get("action"), body.get("start")
                    if act in ("restore", "uncut", "unkeep", "merge", "reset") and cli.stage_of(cli.load(proj / "project.json")) == "completed":
                        raise ValueError("Completed footage can only be trimmed further. Move it back to Processing to bring back what was cut.")
                    if act not in ("undo", "redo"):
                        cli.remember(clip)
                    if act in ("undo", "redo"):  # step back or forward through this video's hand edits
                        cli.step(clip, act == "undo")
                    elif act == "restore":
                        cli.restore_cut(clip, start)
                    elif act == "cut":
                        cli.add_cut(clip, start, body.get("end"))
                    elif act == "keep":
                        cli.add_cut(clip, start, body.get("end"), "my_keeps")
                    elif act == "merge":  # one clip from start to end, whatever was between
                        cli.merge(clip, start, body.get("end"), body.get("text") or "")
                    elif act == "split":  # divide the footage at a point, neither cut nor keep
                        if not isinstance(start, (int, float)) or not 0 < start < clip["duration"]:
                            raise ValueError("a split needs a time inside the video")
                        clip["my_splits"] = sorted(set(clip.get("my_splits", [])) | {round(float(start), 2)})
                    elif act == "unsplit":
                        clip["my_splits"] = [t for t in clip.get("my_splits", []) if not isinstance(start, (int, float)) or abs(t - start) >= 0.01]
                    elif act == "label":
                        cli.set_label(clip, start, body.get("end"), body.get("text"))
                        if body["text"].strip() and cli.kept_ranges({"start": start, "end": body["end"]}, cli.decisions(clip)["keeps"]):
                            cli.add_cut(clip, start, body["end"], "my_keeps")  # a clip worth naming is one to keep
                    elif act in ("uncut", "unkeep"):
                        k = "my_cuts" if act == "uncut" else "my_keeps"
                        clip[k] = [c for c in clip.get(k, []) if not isinstance(start, (int, float)) or abs(c["start"] - start) >= 0.01]
                    elif act == "reset":  # back to exactly what the voice commands decided
                        for k in cli.HAND:
                            clip.pop(k, None)
                    else:
                        raise ValueError("action must be restore, cut, uncut, keep, unkeep, split, unsplit, merge, label, reset, undo or redo")
                    cli.save(f, clip)
                self.send_json(edited(proj))
            elif len(p) == 3 and p[:2] == ["api", "project"]:  # hand-set description and tags, kept through reparse
                proj = self.project(p[2])
                with LOCK:
                    info = cli.load(proj / "project.json")
                    if "description" in body:
                        if not isinstance(body["description"], str) or len(body["description"]) > 200:
                            raise ValueError("description must be text, 200 characters at most")
                        info["my_description"] = body["description"].strip()
                    if "tags" in body:
                        info["my_tags"] = clean_tags(body["tags"])
                    cli.save(proj / "project.json", info)
                self.send_json(project_data(proj))
            elif len(p) == 3 and p[:2] == ["api", "brief"]:
                proj = self.project(p[2])
                text = body.get("brief")
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise ValueError("write a brief of up to 2000 characters")
                _, clips = clips_of(proj)
                try:
                    story = brief.ask(text.strip(), clips)
                except RuntimeError as e:
                    raise ValueError(str(e))
                with LOCK:
                    info = cli.load(proj / "project.json")
                    stories = info.setdefault("stories", [])
                    story["id"] = max((s["id"] for s in stories), default=0) + 1
                    story["created"] = time.time()
                    stories.append(story)
                    cli.save(proj / "project.json", info)
                self.send_json(edited(proj, False))
            elif len(p) == 4 and p[:2] == ["api", "story"]:
                proj = self.project(p[2])
                info = cli.load(proj / "project.json")
                story = next((s for s in info.get("stories", []) if str(s["id"]) == p[3]), None)
                if story is None:
                    raise FileNotFoundError
                if body.get("action") == "export":
                    self.send_json({"done": str(cli.export_story(proj, story))})
                elif body.get("action") == "delete":
                    with LOCK:
                        info = cli.load(proj / "project.json")
                        info["stories"] = [s for s in info["stories"] if s["id"] != story["id"]]
                        cli.save(proj / "project.json", info)
                    self.send_json(edited(proj, False))
                else:
                    raise ValueError("action must be export or delete")
            elif len(p) == 3 and p[:2] == ["api", "stage"]:  # the user moves footage to any stage
                proj = self.project(p[2])
                if body.get("stage") not in cli.STAGES:
                    raise ValueError(f"stage must be one of {', '.join(cli.STAGES)}")
                if body["stage"] == "completed":  # cuts the footage down: each kept clip becomes its own file in the library
                    sort = clean_sort(body.get("sort"))
                    settle(proj, body.get("unmarked"))
                    with LOCK:
                        if not cli.load(proj / "project.json").get("finalized"):  # an update stays in Completed while it saves
                            cli.set_stage(proj, "processing")
                        info = cli.load(proj / "project.json")
                    job = {"project": proj.name, "title": info.get("my_description") or info.get("description") or proj.name,
                           "state": "waiting", "step": "Waiting its turn", "part": 0}
                    SAVES[:] = [j for j in SAVES if j["project"] != proj.name] + [job]

                    def failed():
                        with LOCK:
                            cli.set_stage(proj, "processing")
                    JOBS.put((job, lambda: cli.finalize(proj, lambda step, part: job.update(step=step, part=part), sort), failed))
                    self.send_json(job)
                else:
                    with LOCK:
                        cli.set_stage(proj, body["stage"])
                    self.send_json(project_data(proj))
            elif len(p) == 4 and p[:2] == ["api", "clip"]:  # a finalized clip's own tags
                f = cli.library_path(p[2]) / "_info" / f"{p[3]}.json"
                if not f.is_file():
                    raise FileNotFoundError
                tags = clean_tags(body.get("tags"))
                if body.get("reusable") not in (True, False, None):
                    raise ValueError("reusable must be true, false or null")
                with LOCK:
                    m = cli.load(f)
                    cli.save(f, {**m, "tags": tags, "reusable": body.get("reusable", m.get("reusable"))})
                self.send_json({"tags": tags})
            elif len(p) == 3 and p[:2] == ["api", "used"]:  # move a completed bundle's folder between Unused and Used
                proj = self.project(p[2])
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so nothing moved. Try again in a moment.")
                cli.set_used(cli.load(proj / "project.json").get("library_folder", proj.name), bool(body.get("used")))
                self.send_json({})
            elif len(p) == 3 and p[:2] == ["api", "saves"]:  # dismiss a finished one
                SAVES[:] = [j for j in SAVES if j["project"] != p[2] or j["state"] in ("waiting", "saving")]
                self.send_json({})
            elif p == ["api", "nip"]:  # the home screen's NIP: answers from the whole library, and says what to do
                text, history = body.get("message"), body.get("history", [])
                if not isinstance(text, str) or not text.strip() or len(text) > 2000:
                    raise ValueError("write a message of up to 2000 characters")
                if not isinstance(history, list) or not all(isinstance(m, dict) for m in history):
                    raise ValueError("history must be a list of messages")
                try:
                    self.send_json(brief.assist(text.strip(), history[-10:], str(body.get("view", "")), cards(), final_clips(), cli.videos() if library_answers() else []))
                except RuntimeError as e:
                    raise ValueError(str(e))
            elif p == ["api", "handoff"]:  # library clips copied to a folder of their own, to make a video with
                picks = body.get("clips")
                if not isinstance(picks, list) or not all(isinstance(c, dict) for c in picks):
                    raise ValueError("send clips (a list) and name")
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so nothing was exported. Try again in a moment.")
                files = [final_file(Path(str(c.get("folder"))).name, Path(str(c.get("file"))).name) for c in picks]
                self.send_json({"folder": cli.handoff(str(body.get("name", "")), files).name})
            elif len(p) == 3 and p[:2] == ["api", "clear"]:
                proj = self.project(p[2])
                if not library_answers():
                    raise ValueError(NO_DRIVE)
                with LOCK:
                    cli.clear(proj)
                self.send_json({})
            elif p == ["api", "sort-bundle"]:  # a bundle already in the library: only its labels change, no clip is cut or copied
                sort = clean_sort(body.get("sort") or "")
                if any(j["state"] in ("waiting", "saving") for j in SAVES):  # a save moves bundle folders
                    raise ValueError("Wait for the save in the corner to finish, then sort.")
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so nothing was sorted. Try again in a moment.")
                infos = cli.library_path(Path(str(body.get("folder"))).name) / "_info"
                if not infos.is_dir():
                    raise FileNotFoundError
                cli.sort_clips(infos, sort)
                self.send_json({})
            elif p == ["api", "used-bundle"]:  # filing a bundle after saving a video made from it, its project here or not
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so nothing moved. Try again in a moment.")
                cli.set_used(Path(str(body.get("folder"))).name, bool(body.get("used")))
                self.send_json({})
            elif p == ["api", "videos", "from-edit"]:  # an edit made here, rendered and captioned
                proj = self.project(str(body.get("project", "")))
                info = cli.load(proj / "project.json")
                story = next((s for s in info.get("stories", []) if s["id"] == body.get("story")), None)
                if story is None:
                    raise FileNotFoundError

                def work(progress):
                    progress("Rendering", .1)
                    p = cli.new_draft(story["title"], cli.export_story(proj, story), [info["library_folder"]] if info.get("library_folder") else [], move=True)
                    return cli.caption_video(p, "", progress)
                self.send_json(video_job(f"{proj.name}/{story['id']}", story["title"], work))
            elif p == ["api", "videos", "add"]:  # finished in CapCut, and given to Footage Flow to caption
                jobs = []
                for v in picked():
                    d = cli.new_draft(v.stem, v)
                    jobs.append(video_job(d.name, v.stem, lambda progress, d=d: cli.caption_video(d, "", progress)))
                self.send_json({"added": len(jobs)})
            elif p == ["api", "videos", "caption"]:  # draft or redraft one video's captions in its folder
                v = cli.video_path(body.get("id"))
                note = str(body.get("note", ""))[:500]
                self.send_json(video_job(v.name, v.stem, lambda progress: cli.caption_video(v, note, progress)))
            elif p == ["api", "videos", "caption-missing"]:
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering. Try again in a moment.")
                todo = [v for v in cli.videos() if v["shelf"] == "Unposted" and not v["captions"]]
                for v in todo:
                    f = cli.video_path(v["id"])
                    video_job(f.name, v["title"], lambda progress, f=f: cli.caption_video(f, "", progress))
                self.send_json({"added": len(todo)})
            elif p == ["api", "videos", "redraft"]:  # new wording for the review screen; nothing is saved
                v = cli.video_path(body.get("id"))
                info = cli.video_info(v)
                caps = {k: str(body.get(k, "")) for k in ("instagram", "tiktok")}
                try:
                    self.send_json(brief.captions(str(body.get("title") or v.name), info.get("said", ""), info.get("sources", []), str(body.get("note", ""))[:500], caps))
                except RuntimeError as e:
                    raise ValueError(str(e))
            elif p == ["api", "videos", "save"]:
                if not library_answers():
                    raise ValueError("Google Drive isn’t answering, so nothing was saved. Try again in a moment.")
                title, caps, sources = caption_fields(body)
                self.send_json({"id": cli.save_video(body.get("id"), title, caps, sources)})
            elif p == ["api", "videos", "move"]:
                self.send_json({"id": cli.move_video(body.get("id"), body.get("shelf"))})
            elif p == ["api", "videos", "delete"]:
                if not library_answers():
                    raise ValueError(NO_DRIVE)
                cli.delete_video(body.get("id"))
                self.send_json({})
            elif p == ["api", "ideas", "add"]:
                self.send_json({"id": ideas.add(body)})
            elif p == ["api", "ideas", "delete"]:
                ideas.delete(str(body.get("id", "")))
                self.send_json({})
            elif p == ["api", "ideas", "update"]:
                self.send_json({"id": ideas.update(str(body.get("id", "")), body)})
            elif p == ["api", "style", "improve"]:  # a proposal for the user to read; saving it is a separate step
                try:
                    self.send_json({**brief.improve_style(), "current": cli.STYLE.read_text() if cli.STYLE.exists() else ""})
                except RuntimeError as e:
                    raise ValueError(str(e))
            elif p == ["api", "style"]:
                text = body.get("guide")
                if not isinstance(text, str) or not text.strip():
                    raise ValueError("the style guide can't be empty")
                cli.STYLE.write_text(text)
                self.send_json({})
            elif p == ["api", "clear-bundle"]:  # a bundle in the library whose project is already gone
                if not library_answers():
                    raise ValueError(NO_DRIVE)
                cli.delete_bundle(Path(str(body.get("folder"))).name)
                self.send_json({})
            elif p == ["api", "workspace"]:  # first run: point this computer at the shared folder, then start over from it
                r = subprocess.run(["osascript", "-e", PICK_FOLDER], capture_output=True, text=True)  # Cancel exits 1 with nothing picked
                if not r.stdout.strip():
                    return self.send_json({"chosen": False})
                cli.HOME.mkdir(parents=True, exist_ok=True)
                cli.save(cli.HOME / "settings.json", {**cli.SETTINGS, "workspace": r.stdout.strip()})
                self.send_json({"chosen": True})
                threading.Timer(.3, lambda: os.execve(sys.executable, sys.orig_argv, {**os.environ, "NIP_RESTARTED": "1"})).start()
            elif p == ["api", "bundle"]:  # several projects become one, named by the user
                names = body.get("projects")
                if not isinstance(names, list) or not isinstance(body.get("name"), str):
                    raise ValueError("send projects (a list) and name")
                with LOCK:
                    dest = cli.bundle([self.project(str(n)) for n in dict.fromkeys(names)], body["name"])
                self.send_json({"name": dest.name})
            elif p == ["api", "add"]:
                self.send_json({"added": pick_videos()})
            elif p == ["api", "reveal"]:  # select a finalized clip in Finder, to drag it into an editor
                subprocess.run(["open", "-R", str(final_file(Path(str(body.get("folder"))).name, Path(str(body.get("file"))).name))], check=True)
                self.send_json({})
            elif p == ["api", "open"]:  # show a folder in Finder
                if body.get("what") == "inbox":
                    target = cli.DRIVE_INBOX or cli.INBOX
                    target.mkdir(parents=True, exist_ok=True)
                elif body.get("what") == "vocabulary":
                    if not cli.VOCABULARY.exists():
                        cli.VOCABULARY.write_text(cli.VOCABULARY_START)
                    target = cli.VOCABULARY
                elif body.get("what") == "style" and cli.STYLE.exists():
                    target = cli.STYLE
                elif body.get("what") == "videos":
                    target = cli.VIDEOS
                elif body.get("what") == "unreadable" and cli.UNREADABLE.exists():
                    target = cli.UNREADABLE
                elif body.get("what") == "finalized":
                    proj = self.project(str(body.get("project", "")))
                    target = cli.library_path(cli.load(proj / "project.json").get("library_folder", proj.name))
                    if not target.exists():
                        raise FileNotFoundError
                elif body.get("what") == "handoff":
                    target = cli.HANDOFFS / Path(str(body.get("folder"))).name
                    if not target.exists():
                        raise FileNotFoundError
                elif body.get("what") == "exports":
                    target = cli.EXPORTS / self.project(str(body.get("project", ""))).name
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    raise ValueError("what must be inbox, vocabulary, unreadable, finalized, handoff or exports")
                subprocess.run(["open", str(target)], check=True)
                self.send_json({"opened": str(target)})
            else:
                raise FileNotFoundError
        except FileNotFoundError as e:  # a program NIP runs (ffmpeg, claude) that's missing names itself
            self.send_json({"error": f"Footage Flow can't find {e.filename}; is it installed?" if e.filename else "not found"},
                           500 if e.filename else 404)
        except ValueError as e:  # includes bad JSON
            self.send_json({"error": str(e)}, 400)
        except subprocess.CalledProcessError:
            self.send_json({"error": "ffmpeg failed; see the terminal running nip"}, 500)
        except Exception as e:  # answer the page instead of dropping the connection
            traceback.print_exc()
            self.send_json({"error": f"Footage Flow hit an error: {e}"}, 500)

    do_PUT = do_POST


def watch_drive():
    """Its own thread: a stalled Drive blocks forever inside the system, and the local inbox has to keep working."""
    while cli.DRIVE_INBOX:
        try:
            cli.fetch()
        except Exception as e:  # a file that didn't copy stays on Drive and is tried again
            print(f"drive inbox: {e}")
        time.sleep(10)


def watch_inbox():
    while True:
        try:
            STATE["transcribing"] = sum(1 for v in cli.INBOX.rglob("*") if v.suffix.lower() in cli.VIDEO)
            if STATE["transcribing"]:
                cli.ingest()
        except Exception as e:  # keep watching; a bad file shouldn't take the app down
            print(f"inbox: {e}")
        STATE["transcribing"] = 0
        time.sleep(10)


def serve(port=8765):
    cli.INBOX.mkdir(parents=True, exist_ok=True)
    if cli.WORK.is_dir():  # with Drive not running, making it would only confuse it
        for d in (cli.LIBRARY, cli.EXPORTS):
            d.mkdir(exist_ok=True)
    cli.rescue()
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # this Mac only
    threading.Thread(target=watch_inbox, daemon=True).start()
    threading.Thread(target=watch_drive, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Footage Flow at {url}  (watching {cli.INBOX}; Ctrl+C to stop)")
    if "NIP_RESTARTED" not in os.environ:  # the page is already open, waiting for this
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
