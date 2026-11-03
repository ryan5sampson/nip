"""nip view: a local page to review and adjust the edit decisions before export."""
import json
import re
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote

from nip import cli

PAGE = Path(__file__).with_name("viewer.html")
LOCK = threading.Lock()  # one writer at a time for the .nip.json files


def project_data(proj):
    info = cli.load(proj / "project.json")
    clips = []
    for stem in info["clips"]:
        c = cli.load(proj / f"{stem}.nip.json")
        clips.append({"stem": stem, "source": c["source"], "duration": c["duration"],
                      "words": c["words"], "edit": cli.decisions(c)})
    return {**info, "name": proj.name, "clips": clips}


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
            elif p == ["api", "projects"]:
                projs = sorted((d for d in cli.LIBRARY.iterdir() if (d / "project.json").exists()),
                               key=lambda d: d.stat().st_mtime, reverse=True)
                self.send_json([d.name for d in projs])
            elif len(p) == 3 and p[:2] == ["api", "project"]:
                self.send_json(project_data(self.project(p[2])))
            elif len(p) == 3 and p[0] == "media":
                f = self.project(p[1]) / "raw" / p[2]
                if not f.is_file():
                    raise FileNotFoundError
                self.send_file(f, "video/mp4")  # .mov too: Chrome only plays it labelled mp4
            else:
                raise FileNotFoundError
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass  # the browser dropped a video range it no longer needs

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
            elif len(p) == 3 and p[:2] == ["api", "export"]:
                proj = self.project(p[2])
                cli.export(proj, bool(body.get("combined")))
                self.send_json({"done": str(cli.EXPORTS / proj.name)})
            else:
                raise FileNotFoundError
        except FileNotFoundError:
            self.send_json({"error": "not found"}, 404)
        except ValueError as e:  # includes bad JSON
            self.send_json({"error": str(e)}, 400)


def serve(port=8765):
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)  # this Mac only
    url = f"http://127.0.0.1:{port}/"
    print(f"NIP viewer at {url}  (Ctrl+C to stop)")
    webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
