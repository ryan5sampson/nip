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

from nip.commands import apply_cut_edits, apply_overrides, build_edit, kept_ranges

HOME = Path(os.environ.get("NIP_HOME", Path.home() / "NIP")).expanduser()
INBOX = Path(os.environ.get("NIP_INBOX", HOME / "inbox")).expanduser()
LIBRARY, EXPORTS = HOME / "library", HOME / "exports"
UNREADABLE = HOME / "unreadable"  # inbox files ffmpeg can't read are set aside here
VIDEO = {".mov", ".mp4", ".m4v"}
MODEL = "mlx-community/whisper-large-v3-turbo"
PROMPT = "NIP. Take one. Take two. Scratch that. Reset. NIP, this is"
TRASH = Path.home() / ".Trash"
STAGES = ("inbox", "processing", "processed", "exported")  # footage moves through these; any can be deleted to the Trash
SHORT_SIDE = 1080  # Reels, TikTok and Shorts all play 1080x1920; more is wasted on them


def load(p):
    return json.loads(p.read_text())


def save(p, data):
    p.write_text(json.dumps(data, indent=1))


def decisions(clip):
    """Voice decisions with the user's hand edits on top (all kept through reparse)."""
    e = apply_overrides(clip["edit"], clip.get("overrides", {}))
    e["labels"] = clip.get("labels", [])  # descriptions the user gave stretches of footage; Reset keeps them
    return apply_cut_edits(e, clip.get("restored_cuts", []), clip.get("my_cuts", []), clip.get("my_keeps", []))


HAND = ("overrides", "restored_cuts", "my_cuts", "my_keeps")  # the user's hand edits on a clip, on top of the voice edit


def clean_override(body, duration):
    """Validate a hand edit to a take; raises ValueError on anything unexpected."""
    out = {}
    if "keep" in body:
        if not isinstance(body["keep"], bool):
            raise ValueError("keep must be true or false")
        out["keep"] = body["keep"]
    for k in ("start", "end"):
        if k in body:
            v = body[k]
            if not isinstance(v, (int, float)) or isinstance(v, bool) or not 0 <= v <= duration:
                raise ValueError(f"{k} must be a time inside the video")
            out[k] = round(float(v), 2)
    if "name" in body:
        if not isinstance(body["name"], str) or len(body["name"]) > 80:
            raise ValueError("name must be text, 80 characters at most")
        out["name"] = body["name"].strip()
    return out


def override_take(clip, at, body):
    """Change one take (found by its original start, `at`) with the validated fields in body."""
    if not isinstance(at, (int, float)) or isinstance(at, bool):
        raise ValueError("at must be the take's original start time")
    t = next((t for t in decisions(clip)["takes"] if abs(t["at"] - at) < 0.01), None)
    if t is None:
        raise ValueError(f"no take starts at {at}")
    overrides = clip.setdefault("overrides", {})
    key = str(round(float(t["at"]), 2))
    o = {**overrides.get(key, {}), **clean_override(body, clip["duration"])}
    if o.get("start", t["start"]) >= o.get("end", t["end"]):
        raise ValueError("a take must start before it ends")
    overrides[key] = o


def add_cut(clip, start, end, layer="my_cuts"):  # layer "my_keeps" marks it keep instead
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end))
    if not (ok and 0 <= start < end <= clip["duration"]):
        raise ValueError("a range must end after it starts, inside the video")
    clip.setdefault(layer, []).append({"start": round(float(start), 2), "end": round(float(end), 2)})


def set_label(clip, start, end, text):
    """Describe a stretch of footage; replaces any label already on it, and empty text removes it."""
    ok = all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end))
    if not (ok and 0 <= start < end <= clip["duration"]) or not isinstance(text, str) or len(text) > 200:
        raise ValueError("a label needs a range inside the video and text of 200 characters at most")
    clip["labels"] = [l for l in clip.get("labels", []) if not start <= (l["start"] + l["end"]) / 2 <= end]
    if text.strip():
        clip["labels"].append({"start": round(float(start), 2), "end": round(float(end), 2), "text": text.strip()})


def restore_cut(clip, start):
    if not any(isinstance(start, (int, float)) and abs(c["start"] - start) < 0.01 for c in clip["edit"]["cuts"]):
        raise ValueError(f"no voice cut starts at {start}")
    clip.setdefault("restored_cuts", []).append(round(float(start), 2))


def slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40]


def probe(path):
    out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", str(path)],
                         capture_output=True, text=True, check=True).stdout
    fmt = json.loads(out)["format"]
    return float(fmt["duration"]), fmt.get("tags", {}).get("creation_time", "")


def readable(path):
    try:
        return probe(path)[0] > 0
    except (subprocess.CalledProcessError, KeyError, ValueError):
        return False


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
        elif p.suffix.lower() in VIDEO and settled(p):  # each loose video is its own project until bundled
            batches[f"{date.today()}_{slug(p.stem)}"] = [p]
    for name, vids in batches.items():
        if bad := [v for v in vids if not readable(v)]:  # e.g. an AirDrop that never finished; the rest still process
            UNREADABLE.mkdir(parents=True, exist_ok=True)
            for v in bad:
                dest = UNREADABLE / v.name
                shutil.move(v, dest.with_stem(f"{dest.stem} {time.time_ns()}") if dest.exists() else dest)
                print(f"inbox: {v.name} isn't a video ffmpeg can read; moved to {UNREADABLE}")
            continue
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
    info = {"description": None, "tags": [], "clips": [], "stage": "inbox"}
    clips = []
    for v in vids:
        dest = proj / "raw" / v.name
        shutil.move(v, dest)
        duration, created = probe(dest)
        clips.append((created or dest.name, dest, duration))
    for created, dest, duration in sorted(clips):  # filming order
        print(f"transcribing {dest.name} ({duration / 60:.1f} min)")
        clip = {"source": dest.name, "duration": duration, "created": created, "words": transcribe(dest)}
        clip["edit"] = build_edit(clip["words"], duration)
        save(proj / f"{dest.stem}.nip.json", clip)
        info["clips"].append(dest.stem)
        info["description"] = info["description"] or clip["edit"]["description"]
        info["tags"] += clip["edit"]["tags"]
    save(proj / "project.json", info)
    if info["description"]:
        proj = proj.rename(free_path(f"{proj.name}_{slug(info['description'])}"))
    report(proj)


def bundle(projs, title):
    """Move several projects' videos, edits and Claude edits into one new project named after title."""
    infos = [load(p / "project.json") for p in projs]
    stems = [s for i in infos for s in i["clips"]]
    if len(projs) < 2 or len(set(stems)) < len(stems) or not slug(title):
        raise ValueError("bundle two or more projects with different video names, and give the bundle a name")
    stages = {stage_of(p, i) for p, i in zip(projs, infos)}
    if stages - {"inbox", "processing"}:
        raise ValueError("only footage in the inbox or processing can be bundled")

    def filmed(p, stem):  # clips from before NIP stored "created" are probed for it
        c = load(p / f"{stem}.nip.json")
        return c.get("created") or probe(p / "raw" / c["source"])[1] or c["source"]
    order = sorted((filmed(p, s), s) for p, i in zip(projs, infos) for s in i["clips"])
    dest = new_project(f"{min(p.name[:10] for p in projs)}_{slug(title)}")
    info = {"description": next((i["description"] for i in infos if i["description"]), None), "my_description": title.strip(),
            "tags": sorted({t for i in infos for t in i["tags"]}),
            "my_tags": sorted({t for i in infos for t in i.get("my_tags", i["tags"])}), "clips": [s for _, s in order],
            "stories": [], "stage": "processing" if "processing" in stages else "inbox"}
    for p, i in zip(projs, infos):
        for s in i.get("stories", []):
            info["stories"].append({**s, "id": len(info["stories"]) + 1})
        for stem in i["clips"]:
            clip = load(p / f"{stem}.nip.json")
            shutil.move(p / "raw" / clip["source"], dest / "raw" / clip["source"])
            shutil.move(p / f"{stem}.nip.json", dest / f"{stem}.nip.json")
    save(dest / "project.json", info)
    for p in projs:  # only what NIP made is left; rmdir refuses anything else, so nothing unknown is deleted
        (p / "project.json").unlink()
        (p / "thumb.jpg").unlink(missing_ok=True)
        (p / "raw").rmdir()
        p.rmdir()
    return dest


def stage_of(proj, info):
    return info.get("stage") or ("exported" if (EXPORTS / proj.name).exists() else "inbox")  # projects from before stages


def set_stage(proj, stage):
    info = load(proj / "project.json")
    info["stage"] = stage
    save(proj / "project.json", info)


def clear(proj):
    """Move a project's footage and edits to the Trash, to free space; its exports stay."""
    return move_free(proj, TRASH)


def move_free(src, folder, name=None):
    """Move src into folder as name (default: its own name), numbering it rather than overwriting anything there."""
    name = Path(name or src.name)
    dest, n = folder / name, 2
    while dest.exists():
        dest, n = folder / f"{name.stem} {n}{name.suffix}", n + 1
    folder.mkdir(parents=True, exist_ok=True)
    shutil.move(src, dest)
    return dest


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
    desc, tags = info.get("my_description") or info["description"], info.get("my_tags", info["tags"])
    print(f"\n{proj.name}\n  {desc or '(no description)'}  tags: {tags}")
    for stem in info["clips"]:
        e = decisions(load(proj / f"{stem}.nip.json"))
        print(f"\n  {stem}")
        for t in e["takes"]:
            label = f"take {t['take']}" if t["group"] else "untaken"
            mark = "KEEP" if t["keep"] else "drop"
            where = f'"{t["name"]}"' if t.get("name") else f"section {t['group']}"
            print(f"    {mark}  {mmss(t['start'])}-{mmss(t['end'])}  {where} {label} ({t['why']}{', edited' if t.get('edited') else ''})")
        for m in e["markers"]:
            span = f"{mmss(m['t'])}-{mmss(m['end'])}" if "end" in m else mmss(m["t"])
            print(f"    mark  {span}  {m['label']}")
        for n in e.get("notes", []):
            print(f"    note  {mmss(n['t'])}  {n['text']}")
        for c in e["cuts"]:
            if c["why"] in ("nip cut that", "nip cut"):
                print(f"    cut   {mmss(c['start'])}-{mmss(c['end'])}  ({c['why']})")
        for n in e["pending"]:
            print(f"    ????  {mmss(n['t'])}  \"{n['text']}\" (not understood yet)")


def encoder():
    # an editing copy for CapCut, which makes the final ~10 Mbps platform encode, so leave headroom above that
    if sys.platform == "darwin":  # hardware HEVC keeps iPhone 10-bit/HDR footage intact and is fast
        return ["-c:v", "hevc_videotoolbox", "-b:v", "16M", "-tag:v", "hvc1"]
    return ["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"]


def cut(src, start, pieces, out):
    """Encode the given (start, end) pieces of src, in order, into one file."""
    parts = []
    for i, (a, b) in enumerate(pieces):
        a, b = a - start, b - start
        parts.append(f"[0:v]trim={a:.3f}:{b:.3f},setpts=PTS-STARTPTS[v{i}];"
                     f"[0:a]atrim={a:.3f}:{b:.3f},asetpts=PTS-STARTPTS[a{i}];")
    graph = "".join(parts) + "".join(f"[v{i}][a{i}]" for i in range(len(pieces)))
    graph += f"concat=n={len(pieces)}:v=1:a=1[vc][a];"
    s = SHORT_SIDE  # scale the short side down to s, whichever way the footage is held; never upscale
    graph += f"[vc]scale=w='if(lt(iw,ih),min({s},iw),-2)':h='if(lt(iw,ih),-2,min({s},ih))'[v]"
    hw = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []
    subprocess.run(["ffmpeg", "-v", "error", "-y", *hw, "-ss", f"{start:.3f}", "-to", f"{pieces[-1][1]:.3f}",
                    "-i", str(src), "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    *encoder(), "-c:a", "aac", "-b:a", "192k", str(out)], check=True)


def export(proj, combined):
    info = load(proj / "project.json")
    out = EXPORTS / proj.name
    out.mkdir(parents=True, exist_ok=True)
    files, n = [], 1
    for stem in info["clips"]:
        clip = load(proj / f"{stem}.nip.json")
        e = decisions(clip)
        for t in e["takes"]:
            pieces = kept_ranges(t, e["cuts"]) if t["keep"] else []
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
    set_stage(proj, "exported")
    print(f"done: {out}")


def export_story(proj, story):
    """Export a Claude edit: its pieces, in order, as one video."""
    out = EXPORTS / proj.name
    out.mkdir(parents=True, exist_ok=True)
    srcs = {stem: proj / "raw" / load(proj / f"{stem}.nip.json")["source"] for stem in load(proj / "project.json")["clips"]}
    runs = []  # consecutive pieces from one video going forward in time encode in one pass
    for p in story["pieces"]:
        if runs and runs[-1][0] == p["stem"] and p["start"] >= runs[-1][1][-1][1]:
            runs[-1][1].append((p["start"], p["end"]))
        else:
            runs.append((p["stem"], [(p["start"], p["end"])]))
    final = out / f"story_{slug(story['title']) or 'edit'}.mp4"
    parts = [out / f".part{i}.mp4" for i in range(len(runs))] if len(runs) > 1 else [final]
    for (stem, pieces), part in zip(runs, parts):
        cut(srcs[stem], pieces[0][0], pieces, part)
    if len(runs) > 1:  # same encoder settings for every part, so stream-copy joining is safe
        lst = out / ".list.txt"
        lst.write_text("".join(f"file '{f.name}'\n" for f in parts))
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i", str(lst),
                        "-c", "copy", str(final)], check=True)
        for f in [lst, *parts]:
            f.unlink()
    set_stage(proj, "exported")
    print(f"done: {final}")
    return final


def find(name):
    if name and (LIBRARY / name / "project.json").exists():  # exact name beats "newest containing it"
        return LIBRARY / name
    hits = sorted(p for p in LIBRARY.glob(f"*{name}*") if (p / "project.json").exists()) if name else []
    if not hits:
        hits = sorted((p for p in LIBRARY.iterdir() if (p / "project.json").exists()),
                      key=lambda p: p.stat().st_mtime)
    if not hits:
        sys.exit("no projects yet")
    return hits[-1]


def main():
    ap = argparse.ArgumentParser(prog="nip")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("ingest", help="process everything waiting in the inbox once")
    sub.add_parser("watch", help="keep checking the inbox")
    sub.add_parser("list", help="list projects")
    sub.add_parser("view", help="open Footage Flow in your browser and keep processing the inbox (the default)")
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
    elif a.cmd in ("view", None):
        from nip.view import serve  # here, not at the top: view imports cli
        serve()
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
