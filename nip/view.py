"""nip view: the local NIP app. Library, review and adjust takes, Claude edits, export. Also keeps ingesting the inbox."""
import copy
import json
import os
import re
import shutil
import subprocess
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from nip import brief, cli
from nip.commands import kept_ranges, tiers, unmarked

PAGE = Path(__file__).with_name("viewer.html")
LOCK = threading.Lock()  # one writer at a time for project.json and the .nip.json files
STATE = {"transcribing": 0}  # videos the background ingest is working on
DOWNLOADS = Path.home() / "Downloads"
PICK = """set picked to choose file of type {"public.movie"} with prompt "Add videos to Footage Flow" with multiple selections allowed
set out to ""
repeat with f in picked
  set out to out & POSIX path of f & linefeed
end repeat
out"""


def pick_videos():
    """The macOS file picker; the videos picked move into the inbox."""
    r = subprocess.run(["osascript", "-e", PICK], capture_output=True, text=True)  # Cancel exits 1 with nothing picked
    vids = [Path(l) for l in r.stdout.splitlines() if Path(l).suffix.lower() in cli.VIDEO]
    return [cli.move_free(v, cli.INBOX).name for v in vids if v.is_file()]


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
    out = []
    for stem in info["clips"]:
        c = cli.load(proj / f"{stem}.nip.json")
        e = cli.decisions(c)
        out.append({"stem": stem, "source": c["source"], "duration": c["duration"],
                    "words": c["words"], "edit": e, "unmarked": unmarked(e),
                    "edit_notes": c.get("edit_notes", []), "can_undo": bool(c.get("undo_notes"))})
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
            "highlights": sum(len(c["edit"]["markers"]) for c in clips), "notes": len(notes),
            "stories": len(info.get("stories", [])), "modified": proj.stat().st_mtime,
            "tiers": {k: sum(len(t[k]) for t in ts) for k in ("keep", "unmarked", "cut")},
            "stage": cli.stage_of(proj, info), "media": [[c["source"], c["duration"]] for c in clips],
            "search": " ".join([proj.name, desc or "", *tags, *names, *notes, *labels,
                                *(w["w"] for c in clips for w in c["words"])]).lower()}


def project_data(proj):
    info, clips = clips_of(proj)
    return {**info, "name": proj.name, "clips": clips, "stage": cli.stage_of(proj, info),
            "description": info.get("my_description") or info["description"],
            "tags": info.get("my_tags", info["tags"]), "stories": info.get("stories", [])}


def edited(proj):
    """The project after an edit by hand or by NIP; the first edit moves Inbox footage to Processing."""
    with LOCK:
        if cli.stage_of(proj, cli.load(proj / "project.json")) == "inbox":
            cli.set_stage(proj, "processing")
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
        errors = brief.apply_ops(clip, plan["ops"])
        said = {r.get("id"): r for r in plan["notes"] if isinstance(r, dict)}
        for n in clip.get("edit_notes", []):
            if n["id"] in ids:
                r, errs = said.get(n["id"], {}), errors.get(n["id"], [])
                n["status"] = "applied" if r.get("done") and not errs else "failed"
                n["result"] = " ".join([str(r.get("summary") or "NIP didn't report on this note.")]
                                       + [f"Couldn't apply: {e}." for e in errs])
        cli.save(f, clip)


def thumbnail(proj):
    """A small frame from the first kept take, made once and cached in the project folder."""
    f = proj / "thumb.jpg"
    if not f.exists():
        _, clips = clips_of(proj)
        c = clips[0]
        t = next((t["start"] for t in c["edit"]["takes"] if t["keep"]), 0) + 1
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{min(t, c['duration'] - 0.1):.2f}",
                        "-i", str(proj / "raw" / c["source"]), "-frames:v", "1", "-vf", "scale=360:-2",
                        str(f)], check=True)
    return f


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):  # keep the terminal quiet; errors still raise
        pass

    def parts(self):
        parts = [unquote(p) for p in self.path.split("?")[0].strip("/").split("/")]
        if any(p in (".", "..") or "/" in p or "\\" in p for p in parts):
            raise FileNotFoundError
        return parts

    def project(self, name):
        proj = cli.LIBRARY / name
        if not name or not (proj / "project.json").exists():
            raise FileNotFoundError
        return proj

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
                waiting = [v for v in cli.INBOX.rglob("*") if v.suffix.lower() in cli.VIDEO] if cli.INBOX.exists() else []
                self.send_json({"inbox": str(cli.INBOX), "waiting": len(waiting), "transcribing": STATE["transcribing"],
                                "unreadable": sum(1 for f in cli.UNREADABLE.iterdir() if not f.name.startswith(".")) if cli.UNREADABLE.exists() else 0,
                                "claude": "plan" if shutil.which("claude") else
                                "api" if os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN") else ""})
            elif p == ["api", "projects"]:
                projs = [d for d in cli.LIBRARY.iterdir() if (d / "project.json").exists()] if cli.LIBRARY.exists() else []
                self.send_json(sorted((summary(d) for d in projs), key=lambda s: s["modified"], reverse=True))
            elif len(p) == 3 and p[:2] == ["api", "project"]:
                self.send_json(project_data(self.project(p[2])))
            elif len(p) == 2 and p[0] == "thumb":
                self.send_file(thumbnail(self.project(p[1])), "image/jpeg")
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
            if len(p) == 4 and p[:2] == ["api", "override"]:
                proj = self.project(p[2])
                if p[3] not in cli.load(proj / "project.json")["clips"]:
                    raise FileNotFoundError
                f = proj / f"{p[3]}.nip.json"
                with LOCK:
                    clip = cli.load(f)
                    if body.get("reset"):
                        clip.get("overrides", {}).pop(str(round(float(body.get("at", -1)), 2)), None)
                    else:
                        cli.override_take(clip, body.get("at"), body)
                    cli.save(f, clip)
                self.send_json(edited(proj))
            elif len(p) == 4 and p[:2] == ["api", "notes"]:  # second-round edit notes for Claude
                proj = self.project(p[2])
                if p[3] not in cli.load(proj / "project.json")["clips"]:
                    raise FileNotFoundError
                f = proj / f"{p[3]}.nip.json"
                act = body.get("action")
                if act == "add":
                    text, t = body.get("text"), body.get("t")
                    if not isinstance(text, str) or not text.strip() or len(text) > 500:
                        raise ValueError("write a note of up to 500 characters")
                    with LOCK:
                        clip = cli.load(f)
                        if not isinstance(t, (int, float)) or not 0 <= t <= clip["duration"]:
                            raise ValueError("a note needs a time inside the video")
                        notes = clip.setdefault("edit_notes", [])
                        note = {"id": max((n["id"] for n in notes), default=0) + 1, "t": round(float(t), 2),
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
                self.send_json(edited(proj))
            elif len(p) == 4 and p[:2] == ["api", "cuts"]:  # restore a voice cut, add or remove your own, or reset
                proj = self.project(p[2])
                if p[3] not in cli.load(proj / "project.json")["clips"]:
                    raise FileNotFoundError
                f = proj / f"{p[3]}.nip.json"
                with LOCK:
                    clip = cli.load(f)
                    act, start = body.get("action"), body.get("start")
                    if act == "restore":
                        cli.restore_cut(clip, start)
                    elif act == "cut":
                        cli.add_cut(clip, start, body.get("end"))
                    elif act == "keep":
                        cli.add_cut(clip, start, body.get("end"), "my_keeps")
                    elif act == "split":  # divide the footage at a point, neither cut nor keep
                        if not isinstance(start, (int, float)) or not 0 < start < clip["duration"]:
                            raise ValueError("a split needs a time inside the video")
                        clip["my_splits"] = sorted(set(clip.get("my_splits", [])) | {round(float(start), 2)})
                    elif act == "unsplit":
                        clip["my_splits"] = [t for t in clip.get("my_splits", []) if not isinstance(start, (int, float)) or abs(t - start) >= 0.01]
                    elif act == "label":
                        cli.set_label(clip, start, body.get("end"), body.get("text"))
                    elif act in ("uncut", "unkeep"):
                        k = "my_cuts" if act == "uncut" else "my_keeps"
                        clip[k] = [c for c in clip.get(k, []) if not isinstance(start, (int, float)) or abs(c["start"] - start) >= 0.01]
                    elif act == "reset":  # back to exactly what the voice commands decided
                        for k in cli.HAND:
                            clip.pop(k, None)
                    else:
                        raise ValueError("action must be restore, cut, uncut, keep, unkeep, split, unsplit, label or reset")
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
                        tags = body["tags"]
                        if not isinstance(tags, list) or not all(isinstance(t, str) and len(t) <= 40 for t in tags):
                            raise ValueError("tags must be a list of short texts")
                        info["my_tags"] = [t.strip().lower() for t in tags if t.strip()]
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
                self.send_json(edited(proj))
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
                    self.send_json(edited(proj))
                else:
                    raise ValueError("action must be export or delete")
            elif len(p) == 3 and p[:2] == ["api", "export"]:
                proj = self.project(p[2])
                settle(proj, body.get("unmarked"))
                cli.export(proj, bool(body.get("combined")))
                self.send_json({"done": str(cli.EXPORTS / proj.name)})
            elif len(p) == 3 and p[:2] == ["api", "stage"]:  # the user moves footage to any stage
                proj = self.project(p[2])
                if body.get("stage") not in cli.STAGES:
                    raise ValueError(f"stage must be one of {', '.join(cli.STAGES)}")
                settle(proj, body.get("unmarked"))
                with LOCK:
                    cli.set_stage(proj, body["stage"])
                self.send_json(project_data(proj))
            elif len(p) == 3 and p[:2] == ["api", "clear"]:
                with LOCK:
                    self.send_json({"trash": str(cli.clear(self.project(p[2])))})
            elif p == ["api", "bundle"]:  # several projects become one, named by the user
                names = body.get("projects")
                if not isinstance(names, list) or not isinstance(body.get("name"), str):
                    raise ValueError("send projects (a list) and name")
                with LOCK:
                    dest = cli.bundle([self.project(str(n)) for n in dict.fromkeys(names)], body["name"])
                self.send_json({"name": dest.name})
            elif p == ["api", "add"]:
                self.send_json({"added": pick_videos()})
            elif p == ["api", "open"]:  # show a folder in Finder
                if body.get("what") == "inbox":
                    cli.INBOX.mkdir(parents=True, exist_ok=True)
                    target = cli.INBOX
                elif body.get("what") == "unreadable" and cli.UNREADABLE.exists():
                    target = cli.UNREADABLE
                elif body.get("what") == "exports":
                    target = cli.EXPORTS / self.project(str(body.get("project", ""))).name
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    raise ValueError("what must be inbox or exports")
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
    for d in (cli.INBOX, cli.LIBRARY, cli.EXPORTS):
        d.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # this Mac only
    threading.Thread(target=watch_inbox, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"Footage Flow at {url}  (watching {cli.INBOX}; Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
