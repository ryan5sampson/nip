"""NIP command line: ingest, watch, reparse, retranscribe, report, list, finalize."""
import argparse
import copy
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

from nip.commands import apply_cut_edits, apply_overrides, build_edit, clip_name, drop_loops, kept_pieces, kept_ranges, made_up, norm, respell, timeline, unmarked

HOME = Path(os.environ.get("NIP_HOME", Path.home() / "NIP")).expanduser()
INBOX = Path(os.environ.get("NIP_INBOX", HOME / "inbox")).expanduser()
LIBRARY, EXPORTS = HOME / "library", HOME / "exports"
SETTINGS = json.loads((HOME / "settings.json").read_text()) if (HOME / "settings.json").exists() else {}
FINAL = Path(os.environ.get("NIP_FINAL") or SETTINGS.get("library") or HOME / "finalized").expanduser()  # kept clips, as their own files
SHELVES = ("Unused", "Used")  # the library's two folders; a bundle's folder sits in one, moved by the app or by hand in Drive
HANDOFFS = FINAL.parent / "Handoffs"  # clips picked from the library for a video, a folder each
INDEX = HOME / "clips-index.json"  # every clip's info, kept here so search works with the library offline
SNAP = 0.5  # a sliver of cut this short between a kept range and the footage beside it isn't meant to be there
VOCABULARY = HOME / "vocabulary.txt"
VOCABULARY_START = """# Words Footage Flow should spell your way. One per line.
# A word on its own helps the transcription hear it, and fixes its spelling and capitals.
# "what it hears = how it's spelled" fixes a mistake. Changes show as soon as you reload the page.

Linderball
Linder
Linderball.com
Bambu Lab
NIP

linear ball = Linderball
linderball dot com = Linderball.com
bamboo labs = Bambu Lab
bamboo lab = Bambu Lab
bamboo left = Bambu Lab
bamboo = Bambu
"""
UNREADABLE = HOME / "unreadable"  # inbox files ffmpeg can't read are set aside here
VIDEO = {".mov", ".mp4", ".m4v"}
MODEL = "mlx-community/whisper-large-v3-turbo"
PROMPT = "NIP. Take one. Take two. Scratch that. Reset. NIP, this is"
TRASH = Path.home() / ".Trash"
STAGES = ("inbox", "processing", "completed")  # footage moves through these; any can be deleted to the Trash. Completed means its clips are in the library
SHORT_SIDE = 1080  # Reels, TikTok and Shorts all play 1080x1920; more is wasted on them
HW = ["-hwaccel", "videotoolbox"] if sys.platform == "darwin" else []  # 4K HDR in software took minutes a clip, not seconds


def load(p):
    return json.loads(p.read_text())


def save(p, data):
    tmp = p.with_name(f".{p.name}.{time.time_ns()}")  # swapped in whole: a crash can't leave half a file, and nobody reads one
    tmp.write_text(json.dumps(data, indent=1))
    os.replace(tmp, p)


def decisions(clip):
    """Voice decisions with the user's hand edits on top (all kept through reparse)."""
    e = apply_overrides(clip["edit"], clip.get("overrides", {}))
    e["labels"] = clip.get("labels", [])  # descriptions the user gave stretches of footage; Reset keeps them
    merged = clip.get("my_merges", [])
    e["splits"] = sorted([{"t": m["t"], "why": "divider"} for m in e["markers"] if m.get("label") == "divider"
                          and not any(abs(m["t"] - t) < 0.01 for t in merged)]
                         + [{"t": t, "why": "split by you"} for t in clip.get("my_splits", [])], key=lambda s: s["t"])
    return apply_cut_edits(e, clip.get("restored_cuts", []), clip.get("my_cuts", []), clip.get("my_keeps", []), clip.get("my_restores", []))


HAND = ("overrides", "restored_cuts", "my_cuts", "my_keeps", "my_splits", "my_restores", "my_merges")  # the user's hand edits on a clip, on top of the voice edit
UNDOABLE = HAND + ("labels",)


def remember(clip):
    """Before a hand edit: save the clip's edits for Undo (the last 100), and forget what Redo had."""
    clip["undo"] = (clip.get("undo", []) + [{k: copy.deepcopy(clip[k]) for k in UNDOABLE if k in clip}])[-100:]
    clip["redo"] = []


def step(clip, back):
    """Undo (back) or redo the last hand edit on this clip."""
    src, dst = ("undo", "redo") if back else ("redo", "undo")
    if not clip.get(src):
        raise ValueError(f"nothing to {src}")
    state = clip[src].pop()
    clip[dst] = clip.get(dst, []) + [{k: copy.deepcopy(clip[k]) for k in UNDOABLE if k in clip}]
    for k in UNDOABLE:
        clip.pop(k, None)
    clip.update(state)


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


def is_range(clip, start, end):
    return all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (start, end)) and 0 <= start < end <= clip["duration"]


def add_cut(clip, start, end, layer="my_cuts"):  # layer "my_keeps" marks it keep instead
    if not is_range(clip, start, end):
        raise ValueError("a range must end after it starts, inside the video")
    if layer == "my_keeps":
        start, end = snap(clip, start, end)
    r = {"start": round(float(start), 2), "end": round(float(end), 2)}
    clip.setdefault(layer, []).append(r)
    if layer == "my_keeps":  # keeping footage brings it back if it was cut
        clip.setdefault("my_restores", []).append(dict(r))
    elif layer == "my_cuts" and clip.get("my_restores"):  # and the newest mark wins: cutting it takes it out again
        clip["my_restores"] = [{"start": a, "end": b} for x in clip["my_restores"] for a, b in kept_ranges(x, [r])]


def snap(clip, a, b):
    """Stretch a range that ends in cut footage over a sliver left between it and the footage beside it. Selections stop
    at words and cuts rarely do, so "up to the clip" falls just short. A whole word in the sliver stays cut: it wasn't selected."""
    pieces = kept_pieces(decisions(clip))
    cut_at = lambda t: not any(p - 0.01 <= t <= q + 0.01 for p, q in pieces)
    whole_word = lambda x, y: any(x <= w["start"] and w["end"] <= y for w in clip["words"])
    start, end = a, b
    for p, q in pieces:
        if cut_at(a) and 0 < a - q < SNAP and not whole_word(q, a):
            start = q
        if cut_at(b) and 0 < p - b < SNAP and not whole_word(b, p):
            end = p
    return start, end


def merge(clip, start, end, name=""):
    """Make start..end one clip: bring back anything cut inside it and remove the splits and spoken dividers between.
    It's keep if any of it was, and takes name."""
    spans = [s for s in timeline(decisions(clip), clip["duration"]) if not s["gap"] and s["a"] < end - 0.05 and s["b"] > start + 0.05]
    add_cut(clip, start, end, "my_keeps" if any(s["keep"] for s in spans) else "my_restores")
    inside = lambda t: start + 0.05 < t < end - 0.05
    clip["my_merges"] = clip.get("my_merges", []) + [s["t"] for s in decisions(clip)["splits"] if s["why"] == "divider" and inside(s["t"])]
    clip["my_splits"] = [t for t in clip.get("my_splits", []) if not inside(t)]
    if name:
        set_label(clip, start, end, name)


def set_label(clip, start, end, text):
    """Describe a stretch of footage; replaces any label already on it, and empty text removes it."""
    if not is_range(clip, start, end) or not isinstance(text, str) or len(text) > 200:
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


def vocabulary():
    """The user's words: (how each is written, for Whisper's prompt; the spellings for respell)."""
    written, spellings = [], {}
    for line in (VOCABULARY.read_text() if VOCABULARY.exists() else VOCABULARY_START).splitlines():
        heard, _, right = line.split("#")[0].rpartition("=")
        if right := right.strip():
            written += [right] if right not in written else []
            spellings |= {norm(s): right for s in (heard, right) if norm(s)}
    return written, spellings


def readable(path):
    try:
        return probe(path)[0] > 0
    except (subprocess.CalledProcessError, KeyError, ValueError):
        return False


def transcribe(path):
    try:
        import mlx_whisper
    except ImportError:
        raise RuntimeError("mlx-whisper isn't installed. It needs an Apple Silicon Mac: pip install -e .")  # not sys.exit: it would silently end the app's inbox thread
    r = mlx_whisper.transcribe(str(path), path_or_hf_repo=MODEL, word_timestamps=True,  # the prompt biases it to the user's words
                               initial_prompt=f"{', '.join(vocabulary()[0])}. {PROMPT}", condition_on_previous_text=False)
    words, noise = [], []
    for s in r["segments"]:
        seg = [{"w": w["word"].strip(), "start": round(w["start"], 2), "end": round(w["end"], 2), "p": round(w["probability"], 2)}
               for w in s.get("words", [])]
        (noise if made_up(seg) else words).extend(seg)
    words, loops = drop_loops(words)
    return words, sorted(noise + loops, key=lambda w: w["start"])  # (speech, what Whisper made up from silence or noise)


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


def rescue():
    """Footage whose transcribing was interrupted (the app quit, or Whisper crashed) has no project.json, so nothing lists it. It goes back to the inbox."""
    for p in LIBRARY.iterdir():
        if (p / "raw").is_dir() and not (p / "project.json").exists():
            for v in (p / "raw").iterdir():
                if v.suffix.lower() in VIDEO:
                    move_free(v, INBOX)
            shutil.rmtree(p)  # only what NIP made is left


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
        words, noise = transcribe(dest)
        clip = {"source": dest.name, "duration": duration, "created": created, "words": words, "noise": noise}
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
    stages = {stage_of(i) for i in infos}
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


def stage_of(info):
    stage = info.get("stage", "inbox")
    if stage in STAGES:
        return stage
    return "completed" if info.get("finalized") else "processing"  # the old Processed and Exported, from before Complete saved the clips


def set_stage(proj, stage):
    """Move footage out of Completed, or between the stages before it; only finalize() completes it."""
    info = load(proj / "project.json")
    info["stage"] = stage
    info.pop("finalized", None)  # its clips in the library may no longer match
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
        clip["words"], loops = drop_loops(clip["words"])
        clip["noise"] = sorted(clip.get("noise", []) + loops, key=lambda w: w["start"])
        clip["edit"] = build_edit(clip["words"], clip["duration"])
        save(p, clip)
        info["description"] = info["description"] or clip["edit"]["description"]
        info["tags"] += clip["edit"]["tags"]
    save(proj / "project.json", info)
    report(proj)


def retranscribe(proj):
    """Transcribe a project's videos again (with the made-up-words check) and redo the voice edit; hand edits stay."""
    for stem in load(proj / "project.json")["clips"]:
        p = proj / f"{stem}.nip.json"
        clip = load(p)
        shutil.copy(p, p.with_suffix(".json.bak"))  # the old transcript, in case
        print(f"transcribing {clip['source']}")
        clip["words"], clip["noise"] = transcribe(proj / "raw" / clip["source"])
        save(p, clip)
    reparse(proj)


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
    subprocess.run(["ffmpeg", "-v", "error", "-y", *HW, "-ss", f"{start:.3f}", "-to", f"{pieces[-1][1]:.3f}",
                    "-i", str(src), "-filter_complex", graph, "-map", "[v]", "-map", "[a]",
                    *encoder(), "-c:a", "aac", "-b:a", "192k", str(out)], check=True)


def copy_clip(src, a, b, out):
    """Copy a..b of src to its own file without re-encoding, then check it. Stream copy measured the same size as a
    re-encode, with no quality loss, and keeps the footage's location and date."""
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{a:.3f}", "-i", str(src), "-t", f"{b - a:.3f}", "-map", "0:v:0", "-map", "0:a:0?",
                    "-c", "copy", "-map_metadata", "0", "-movflags", "use_metadata_tags", str(out)], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-xerror", "-i", str(out), "-map", "0", "-c", "copy", "-f", "null", "-"], check=True)
    if probe(out)[0] < b - a - 0.2:
        raise ValueError(f"{out.name} came out shorter than the clip")


def preview(clip, name):
    """A thumbnail and a small video, so browsing never has to fetch the full clip from Drive (measured about 4% of its size)."""
    side = "scale='if(lt(iw,ih),{0},-2)':'if(lt(iw,ih),-2,{0})'"
    run = ["ffmpeg", "-v", "error", "-y", *HW, "-i", str(clip)]
    subprocess.run(run + ["-vf", side.format(540), "-c:v", *(["h264_videotoolbox", "-b:v", "1500k"] if sys.platform == "darwin" else ["libx264", "-crf", "28"]),
                          "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart", str(name.with_suffix(".preview.mp4"))], check=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", f"{probe(clip)[0] / 2:.2f}", "-i", str(clip), "-frames:v", "1",
                    "-vf", side.format(360), str(name.with_suffix(".jpg"))], check=True)


def file_name(text):
    return re.sub(r'[^\w ,.()&+-]', '', text).strip(' .')[:80]


def library_path(folder):
    """Where a bundle's folder is in the library, or would go."""
    return next((FINAL / s / folder for s in SHELVES if (FINAL / s / folder).exists()), FINAL / SHELVES[0] / folder)


def set_used(folder, used):
    src, dest = library_path(folder), FINAL / SHELVES[used] / folder
    if not src.exists():
        raise ValueError(f"“{folder}” isn't in the library anymore")
    if src != dest:
        dest.parent.mkdir(parents=True, exist_ok=True)
        src.rename(dest)


def library_folder(proj, info):
    """The bundle's name, then the date it was filmed. Unnamed footage keeps its project name; a same-named bundle gets a number, never another's folder."""
    name = file_name(info.get("my_description") or info.get("description") or "")
    base = f"{name} ({proj.name[:10]})" if name else proj.name
    folder, n = base, 2
    while library_path(folder).exists() and folder != info.get("library_folder", proj.name):
        folder, n = f"{base} {n}", n + 1
    return folder


def finalize(proj, progress=lambda step, part: None):
    """Complete a project: write each kept clip of a fully decided project as its own file. _info beside them holds each clip's .json (name, tags,
    transcript with times from the start of the clip), thumbnail and preview, so editors just see the clips. It's built and
    checked locally and copied over whole, so Drive never uploads half a result. The originals aren't touched.
    progress gets what's happening and how much of it is done, from 0 to 1."""
    info = load(proj / "project.json")
    clips = [load(proj / f"{stem}.nip.json") for stem in info["clips"]]
    left = sum(len(unmarked(decisions(c))) for c in clips)
    if left:
        raise ValueError(f"{left} stretches aren't marked cut or keep yet. Open it and press Mark Complete to settle them.")
    for s in SHELVES:
        (FINAL / s).mkdir(parents=True, exist_ok=True)
    folder = library_folder(proj, info)
    out, tmp, old = HOME / ".finalizing" / proj.name, FINAL / f".{proj.name}.building", library_path(info.get("library_folder", proj.name))
    dest = old.parent / folder  # completing it again leaves it on the same shelf
    shutil.rmtree(out, ignore_errors=True)
    (out / "_info").mkdir(parents=True)
    spellings, n, skipped = vocabulary()[1], 0, 0
    total = sum(not s["gap"] for c in clips for s in timeline(decisions(c), c["duration"]))
    before = [load(f) for f in old.glob("_info/*.json")]  # tags added to clips outlive finalizing again
    try:
        for c in clips:
            e, source = decisions(c), Path(c["source"])
            words = respell(c["words"], spellings)
            for s in timeline(e, c["duration"]):
                name = clip_name(s, e)
                if s["gap"]:
                    continue
                if s["b"] - s["a"] < 0.5 and not name:  # a sliver between marks, not a clip anyone chose
                    skipped += 1
                    continue
                n += 1
                progress(f"Cutting clip {n} of {total}", (n - 1) / total / 2)
                was = next((o for o in before if o["source"] == source.name
                            and min(o["source_end"], s["b"]) - max(o["source_start"], s["a"]) > (s["b"] - s["a"]) / 2), None)
                f = out / f"{n:02d} {file_name(name or source.stem)}{source.suffix}"
                copy_clip(proj / "raw" / source, s["a"], s["b"], f)
                preview(f, out / "_info" / f.name)
                save((out / "_info" / f.name).with_suffix(".json"), {
                    "name": name, "tags": was["tags"] if was else info.get("my_tags", info["tags"]), "bundle": info.get("my_description") or info["description"],
                    "project": proj.name, "source": source.name, "source_start": s["a"], "source_end": s["b"],
                    "duration": round(s["b"] - s["a"], 2), "filmed": c.get("created", ""),
                    "words": [{**w, "start": round(max(w["start"] - s["a"], 0), 2), "end": round(w["end"] - s["a"], 2)}
                              for w in words if s["a"] <= (w["start"] + w["end"]) / 2 < s["b"]]})
        if not n:
            raise ValueError("nothing is kept in this project")
        shutil.rmtree(tmp, ignore_errors=True)
        size, copied = sum(f.stat().st_size for f in out.rglob("*") if f.is_file()), 0

        def copy(src, dst):
            nonlocal copied
            progress("Saving to the library", .5 + copied / size / 2)
            copied += Path(src).stat().st_size
            return shutil.copy2(src, dst)
        shutil.copytree(out, tmp, copy_function=copy)
    except Exception:  # never leave half a result
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    finally:
        shutil.rmtree(out, ignore_errors=True)
    spare = FINAL / f".{proj.name}.old"  # the old clips stay until the new ones are in place
    shutil.rmtree(spare, ignore_errors=True)
    if old.exists():
        old.rename(spare)
    try:
        tmp.rename(dest)
    except OSError:
        if spare.exists():
            spare.rename(old)
        raise
    if spare.exists():  # on Drive, deleting goes to Drive's own trash; moving to this Mac's would download it all first
        shutil.rmtree(spare) if "CloudStorage" in str(FINAL) else move_free(spare, TRASH, old.name)
    info["finalized"], info["stage"], info["library_folder"] = {"at": time.time(), "folder": str(dest), "clips": n}, "completed", folder
    save(proj / "project.json", info)
    print(f"finalized {n} clips to {dest}" + (f" ({skipped} slivers under half a second left out)" if skipped else ""))
    return {"folder": str(dest), "clips": n, "skipped": skipped, "bytes": sum(f.stat().st_size for f in dest.iterdir() if f.is_file())}


def handoff(name, files):
    """Copy chosen library clips, numbered in the order picked, into a folder of their own for one video. It's built
    beside its final name, so nobody working from the folder sees half of it."""
    name = file_name(name)
    if not name or not files:
        raise ValueError("name the video and pick at least one clip")
    tmp = HANDOFFS / f".{name}.building"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True)
    try:
        for i, f in enumerate(files, 1):
            shutil.copyfile(f, tmp / f"{i:02d} {re.sub(r'^[0-9]+ ', '', f.name)}")  # not copy2: Drive's own file ids must not come along
        return move_free(tmp, HANDOFFS, name)
    except Exception:  # never leave half a result
        shutil.rmtree(tmp, ignore_errors=True)
        raise


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
    for c in ("report", "reparse", "retranscribe", "finalize"):
        p = sub.add_parser(c)
        p.add_argument("project", nargs="?", default="", help="part of the folder name (default: newest)")
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
        {"report": report, "reparse": reparse, "retranscribe": retranscribe, "finalize": finalize}[a.cmd](proj)


def run():
    try:
        main()
    except RuntimeError as e:
        sys.exit(str(e))


if __name__ == "__main__":
    run()
