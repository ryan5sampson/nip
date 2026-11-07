"""Video ideas, a small file each in the workspace's Ideas folder. Footage Flow and Claude (through its Drive connector) both work on the same files."""
import re
import threading
from datetime import date

from nip import cli

FOLDER = cli.WORK / "Ideas"
STATUSES = ("Idea", "In progress", "Complete")
TEXT = ("idea", "notes", "link")
LISTS = ("bundles", "videos")  # a bundle's project folder, a video's folder in Videos
WRITE = threading.Lock()  # two adds at once would both take the next ID
HOW = """To add a video idea, create a new file in this folder. Footage Flow shows it the next time Ideas opens.

Give it any name ending in .json, and the content type application/json, with content like:
{"idea": "What the video is", "notes": "Optional details", "link": "Optional reference link", "status": "Idea"}

Status is Idea, In progress or Complete. Leave out anything else.

To change an idea, create a new file with the same "id" as the old one, containing all its fields. The newest file wins.
"""


def files():
    """Each idea and the file holding it. Claude can only add files, so a newer file with the same id replaces the old one."""
    FOLDER.mkdir(exist_ok=True)
    if not (FOLDER / "How to add ideas.txt").exists():
        (FOLDER / "How to add ideas.txt").write_text(HOW)
    found = {}
    for f in sorted(FOLDER.glob("[!.]*.json"), key=lambda f: f.stat().st_mtime):
        try:
            i = cli.load(f)
            title = str(i["idea"]).strip()
        except (OSError, ValueError, KeyError, TypeError) as e:  # one file Drive hasn't finished syncing mustn't hide the rest
            print(f"ideas: skipped {f.name}: {e}")
            continue
        key = str(i.get("id") or f.stem)
        if key in found:
            try:
                found[key][1].unlink()
            except OSError as e:
                print(f"ideas: couldn't remove {found[key][1].name}: {e}")
        found[key] = ({**i, "id": key, "idea": title, "notes": str(i.get("notes", "")), "link": str(i.get("link", "")), "added": str(i.get("added", "")),
                       "status": i.get("status") if i.get("status") in STATUSES else "Idea",
                       **{k: [str(x) for x in i.get(k) or []] for k in LISTS}}, f)
    return found


def read():
    return sorted((i for i, _ in files().values()), key=lambda i: i["id"])


def clean(body, new=False):
    """What the page sent, checked."""
    out = {k: str(body[k]).strip()[:2000] for k in TEXT if k in body}
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        out["status"] = body["status"]
    for k in LISTS:
        if k in body:
            if not isinstance(body[k], list):
                raise ValueError(f"{k} must be a list")
            out[k] = list(dict.fromkeys(str(x).strip() for x in body[k] if str(x).strip()))
    if (new or "idea" in out) and not out.get("idea"):
        raise ValueError("an idea needs a title")
    return out


def add(body):
    fields = clean(body, new=True)
    with WRITE:
        n = max((int(m[1]) for k in files() if (m := re.fullmatch(r"VI-(\d+)", k))), default=0) + 1
        idea = {"status": "Idea", **fields, "id": f"VI-{n:03d}", "added": str(date.today())}
        cli.save(FOLDER / f"{idea['id']} {cli.file_name(idea['idea'])[:60]}.json", idea)
    return idea["id"]


def update(idea_id, body):
    fields = clean(body)
    with WRITE:
        found = files().get(idea_id)
        if not found:
            raise ValueError("That idea isn’t there anymore. It may have been deleted.")
        cli.save(found[1], {**found[0], **fields})
    return idea_id


def delete(idea_id):
    with WRITE:
        found = files().get(idea_id)
        if not found:
            raise ValueError("That idea isn’t there anymore. It may have been deleted.")
        found[1].unlink()
