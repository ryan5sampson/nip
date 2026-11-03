"""NIP command line: ingest, watch, reparse, report, list, export."""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from nip.commands import build_edit, kept_ranges

HOME = Path(os.environ.get("NIP_HOME", Path.home() / "NIP")).expanduser()
INBOX = Path(os.environ.get("NIP_INBOX", HOME / "inbox")).expanduser()
LIBRARY, EXPORTS = HOME / "library", HOME / "exports"
VIDEO = {".mov", ".mp4", ".m4v"}
MODEL = "mlx-community/whisper-large-v3-turbo"
PROMPT = "NIP. Take one. Take two. Scratch that. Reset. NIP, this is"


def load(p):
    return json.loads(p.read_text())


def save(p, data):
    p.write_text(json.dumps(data, indent=1))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    fmt = json.loads(out)["format"]
    return float(fmt["duration"]), fmt.get("tags", {}).get("creation_time", "")


def transcribe(path):
    try:
        import mlx_whisper
    except ImportError:
        sys.exit("mlx-whisper isn't installed. It needs an Apple Silicon Mac: pip install -e .")
    r = mlx_whisper.transcribe(str(path), path_or_hf_repo=MODEL, word_timestamps=True,
                               initial_prompt=PROMPT, condition_on_previous_text=False)
    return [{"w": w["word"].strip(), "start": round(w["start"], 2), "end": round(w["end"], 2)}
            for s in r["segments"] for w in s.get("words", [])]


def settled(p):
    # AirDrop and Drive sync write files gradually; wait until a file stops changing.
    return time.time() - p.stat().st_mtime > 10


def ingest():
    batches = {}  # project folder name -> videos
    for p in sorted(INBOX.iterdir()):
        if p.is_dir():
            vids = [v for v in sorted(p.iterdir()) if v.suffix.lower() in VIDEO]
            if vids and all(settled(v) for v in vids):
                batches[p.name] = vids
        elif p.suffix.lower() in VIDEO and settled(p):
            batches.setdefault(str(date.today()), []).append(p)
    for name, vids in batches.items():
        process(new_project(name), vids)
        if vids[0].parent != INBOX and not any(vids[0].parent.iterdir()):
            vids[0].parent.rmdir()


def new_project(name):
    proj = free_path(name)
    (proj / "raw").mkdir(parents=True)
    return proj


def free_path(name):
    p, n = LIBRARY / name, 2
    while p.exists():
        p, n = LIBRARY / f"{name}-{n}", n + 1
    return p


def process(proj, vids):
    info = {"description": None, "tags": [], "clips": []}
    clips = []
    for v in vids:
        dest = proj / "raw" / v.name
        shutil.move(v, dest)
        duration, created = probe(dest)
        clips.append((created or dest.name, dest, duration))
    for _, dest, duration in sorted(clips):  # filming order
        print(f"transcribing {dest.name} ({duration / 60:.1f} min)")
        clip = {"source": dest.name, "duration": duration, "words": transcribe(dest)}
        clip["edit"] = build_edit(clip["words"], duration)
        save(proj / f"{dest.stem}.nip.json", clip)
        info["clips"].append(dest.stem)
        info["description"] = info["description"] or clip["edit"]["description"]
        info["tags"] += clip["edit"]["tags"]
    save(proj / "project.json", info)
    if info["description"]:
        proj = proj.rename(free_path(f"{proj.name}_{slug(info['description'])}"))
    report(proj)


def reparse(proj):
    """Re-run command detection on saved transcripts (after tuning commands.py), no re-transcribing."""
    info = load(proj / "project.json")
    info["description"], info["tags"] = None, []
    for stem in info["clips"]:
        p = proj / f"{stem}.nip.json"
        clip = load(p)
        clip["edit"] = build_edit(clip["words"], clip["duration"])
        save(p, clip)
        info["description"] = info["description"] or clip["edit"]["description"]
        info["tags"] += clip["edit"]["tags"]
    save(proj / "project.json", info)
    report(proj)


def mmss(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


def report(proj):
    info = load(proj / "project.json")
    print(f"\n{proj.name}\n  {info['description'] or '(no description)'}  tags: {info['tags']}")
    for stem in info["clips"]:
        e = load(proj / f"{stem}.nip.json")["edit"]
        print(f"\n  {stem}")
        for t in e["takes"]:
            label = f"take {t['take']}" if t["group"] else "untaken"
            mark = "KEEP" if t["keep"] else "drop"
            where = f'"{t["name"]}"' if t.get("name") else f"section {t['group']}"
            print(f"    {mark}  {mmss(t['start'])}-{mmss(t['end'])}  {where} {label} ({t['why']})")
        for m in e["markers"]:
            print(f"    mark  {mmss(m['t'])}  {m['label']}")
        for c in e["cuts"]:
            if c["why"] == "nip cut that":
                print(f"    cut   {mmss(c['start'])}-{mmss(c['end'])}  (nip cut that)")
        for n in e["pending"]:
            print(f"    ????  {mmss(n['t'])}  \"{n['text']}\" (not understood yet)")


def encoder():
    if sys.platform == "darwin":  # hardware HEVC keeps iPhone 10-bit/HDR footage intact and is fast
        return ["-c:v", "hevc_videotoolbox", "-q:v", "65", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def cut(src, start, pieces, out):
    """Encode the given (start, end) pieces of src, in order, into one file."""
    parts = []
    for i, (a, b) in enumerate(pieces):
        a, b = a - start, b - start
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
    graph = "".join(parts) + "".join(f"[v{i}][a{i}]" for i in range(len(pieces)))
    graph += f"concat=n={len(pieces)}:v=1:a=1[v][a]"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{start:.3f}", "-to", f"{pieces[-1][1]:.3f}",
                    "-i", str(src), "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    *encoder(), "-c:a", "aac", "-b:a", "192k", str(out)], check=True)


def export(proj, combined):
    info = load(proj / "project.json")
    out = EXPORTS / proj.name
    out.mkdir(parents=True, exist_ok=True)
    files, n = [], 1
    for stem in info["clips"]:
        clip = load(proj / f"{stem}.nip.json")
        for t in clip["edit"]["takes"]:
            pieces = kept_ranges(t, clip["edit"]["cuts"]) if t["keep"] else []
            if not pieces:
                continue
            if t.get("name"):
                name = f"{n:02d}_{slug(t['name'])}_t{t['take']}.mp4"
            else:
                name = f"{n:02d}_{stem}" + (f"_s{t['group']}t{t['take']}" if t["group"] else "") + ".mp4"
            print(f"exporting {name}")
            cut(proj / "raw" / clip["source"], pieces[0][0], pieces, out / name)
            files.append(out / name)
            n += 1
    if combined and files:
        # shortcut: stream-copy concat assumes all clips share resolution/fps; re-encode if mixing cameras
        lst = out / "list.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for f in files))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(out / f"{proj.name}.mp4")], check=True)
        lst.unlink()
    print(f"done: {out}")


def find(name):
    hits = sorted(p for p in LIBRARY.glob(f"*{name}*") if (p / "project.json").exists()) if name else []
    if not hits:
        hits = sorted((p for p in LIBRARY.iterdir() if (p / "project.json").exists()),
                      key=lambda p: p.stat().st_mtime)
    if not hits:
        sys.exit("no projects yet")
    return hits[-1]


def main():
    ap = argparse.ArgumentParser(prog="nip")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="process everything waiting in the inbox once")
    sub.add_parser("watch", help="keep checking the inbox")
    sub.add_parser("list", help="list projects")
    for c in ("report", "reparse", "export"):
        p = sub.add_parser(c)
        p.add_argument("project", nargs="?", default="", help="part of the folder name (default: newest)")
        if c == "export":
            p.add_argument("--combined", action="store_true", help="also join everything into one video")
    a = ap.parse_args()
    for d in (INBOX, LIBRARY, EXPORTS):
        d.mkdir(parents=True, exist_ok=True)

    if a.cmd == "ingest":
        ingest()
    elif a.cmd == "watch":
        print(f"watching {INBOX}")
        while True:
            ingest()
            time.sleep(10)
    elif a.cmd == "list":
        for p in sorted(LIBRARY.iterdir()):
            if (p / "project.json").exists():
                i = load(p / "project.json")
                print(f"{p.name:50} {len(i['clips'])} clips  {', '.join(i['tags'])}")
    else:
        proj = find(a.project)
        {"report": report, "reparse": reparse}.get(a.cmd, lambda p: export(p, a.combined))(proj)


if __name__ == "__main__":
    main()
