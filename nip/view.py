"""nip view: the local NIP app. Library, review and adjust takes, Claude edits, export. Also keeps ingesting the inbox."""
import json
import os
import re
import subprocess
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from nip import brief, cli
from nip.commands import kept_ranges

PAGE = Path(__file__).with_name("viewer.html")
LOCK = threading.Lock()  # one writer at a time for project.json and the .nip.json files
STATE = {"processing": 0}  # videos the background ingest is working on


def clips_of(proj):
    info = cli.load(proj / "project.json")
    out = []
    for stem in info["clips"]:
        c = cli.load(proj / f"{stem}.nip.json")
        out.append({"stem": stem, "source": c["source"], "duration": c["duration"],
                    "words": c["words"], "edit": cli.decisions(c)})
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
    return {"name": proj.name, "description": desc, "tags": tags, "videos": len(clips),
            "clips": names, "filmed": sum(c["duration"] for c in clips), "kept": sum(map(kept_seconds, clips)),
            "highlights": sum(len(c["edit"]["markers"]) for c in clips), "notes": len(notes),
            "stories": len(info.get("stories", [])), "modified": proj.stat().st_mtime,
            "search": " ".join([proj.name, desc or "", *tags, *names, *notes,
                                *(w["w"] for c in clips for w in c["words"])]).lower()}


def project_data(proj):
    info, clips = clips_of(proj)
    return {**info, "name": proj.name, "clips": clips,
            "description": info.get("my_description") or info["description"],
            "tags": info.get("my_tags", info["tags"]), "stories": info.get("stories", [])}


def clean_override(body, duration):
    """Validate a hand edit from the page; raises ValueError on anything unexpected."""
    out = {}
    if "keep" in body:
        if not isinstance(body["keep"], bool):
            raise ValueError("keep must be true or false")
        out["keep"] = body["keep"]
    for k in ("start", "end"):
        if k in body:
            v = body[k]
            if not isinstance(v, (int, float)) or not 0 <= v <= duration:
                raise ValueError(f"{k} must be a time inside the video")
            out[k] = round(float(v), 2)
    if "name" in body:
        if not isinstance(body["name"], str) or len(body["name"]) > 80:
            raise ValueError("name must be text, 80 characters at most")
        out["name"] = body["name"].strip()
    return out


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
                self.send_json({"inbox": str(cli.INBOX), "waiting": len(waiting), "processing": STATE["processing"],
                                "claude": bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))})
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
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
        except subprocess.CalledProcessError:
            self.send_json({"error": "ffmpeg couldn't read this video"}, 500)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser dropped a video range it no longer needs
        except Exception as e:  # answer the page instead of dropping the connection
            traceback.print_exc()
            self.send_json({"error": f"NIP hit an error: {e}"}, 500)

    def do_POST(self):
        try:
            p = self.parts()
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
                    at = body.get("at")
                    if not isinstance(at, (int, float)):
                        raise ValueError("at must be the take's original start time")
                    overrides = clip.setdefault("overrides", {})
                    key = str(round(float(at), 2))
                    if body.get("reset"):
                        overrides.pop(key, None)
                    else:
                        o = {**overrides.get(key, {}), **clean_override(body, clip["duration"])}
                        t = next((t for t in cli.decisions(clip)["takes"] if abs(t["at"] - at) < 0.01), None)
                        if t and o.get("start", t["start"]) >= o.get("end", t["end"]):
                            raise ValueError("a take must start before it ends")
                        overrides[key] = o
                    cli.save(f, clip)
                self.send_json(project_data(proj))
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
                self.send_json(project_data(proj))
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
                    self.send_json(project_data(proj))
                else:
                    raise ValueError("action must be export or delete")
            elif len(p) == 3 and p[:2] == ["api", "export"]:
                proj = self.project(p[2])
                cli.export(proj, bool(body.get("combined")))
                self.send_json({"done": str(cli.EXPORTS / proj.name)})
            elif p == ["api", "open"]:  # show a folder in Finder
                if body.get("what") == "inbox":
                    cli.INBOX.mkdir(parents=True, exist_ok=True)
                    target = cli.INBOX
                elif body.get("what") == "exports":
                    target = cli.EXPORTS / self.project(str(body.get("project", ""))).name
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    raise ValueError("what must be inbox or exports")
                subprocess.run(["open", str(target)], check=True)
                self.send_json({"opened": str(target)})
            else:
                raise FileNotFoundError
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:  # includes bad JSON
            self.send_json({"error": str(e)}, 400)
        except subprocess.CalledProcessError:
            self.send_json({"error": "ffmpeg failed; see the terminal running nip"}, 500)
        except Exception as e:  # answer the page instead of dropping the connection
            traceback.print_exc()
            self.send_json({"error": f"NIP hit an error: {e}"}, 500)


def watch_inbox():
    while True:
        try:
            STATE["processing"] = sum(1 for v in cli.INBOX.rglob("*") if v.suffix.lower() in cli.VIDEO)
            if STATE["processing"]:
                cli.ingest()
        except Exception as e:  # keep watching; a bad file shouldn't take the app down
            print(f"inbox: {e}")
        STATE["processing"] = 0
        time.sleep(10)


def serve(port=8765):
    for d in (cli.INBOX, cli.LIBRARY, cli.EXPORTS):
        d.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # this Mac only
    threading.Thread(target=watch_inbox, daemon=True).start()
    url = f"http://127.0.0.1:{port}/"
    print(f"NIP at {url}  (watching {cli.INBOX}; Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
