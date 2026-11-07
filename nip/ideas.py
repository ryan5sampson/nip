"""Video ideas, kept in a Google Sheet. The sheet is the source of truth: it's read fresh each time, and only changed cells are written."""
import re
import threading
from datetime import date
from pathlib import Path

from nip import cli

SHEET = cli.SETTINGS.get("ideas_sheet", "1XgWse8mgKu2nalas3a64xjxXudVN_hXGRIkGKbsBjeY")
KEY = Path(cli.SETTINGS.get("google_key") or cli.HOME / "google-key.json").expanduser()
URL = f"https://docs.google.com/spreadsheets/d/{SHEET}/edit"
API = f"https://sheets.googleapis.com/v4/spreadsheets/{SHEET}/values"
STATUSES = ("Idea", "In progress", "Complete")
COLUMNS = {"id": "ID", "idea": "Idea", "notes": "Notes", "link": "Reference link", "status": "Status",
           "bundles": "Footage bundles", "videos": "Videos", "added": "Date added"}
LISTS = ("bundles", "videos")  # one name per line in the cell: a bundle's project folder, a video's folder in Videos
WRITE = threading.Lock()  # two adds at once would both take the next ID
_session = []


def session():
    if not _session:
        if not KEY.exists():
            raise ValueError(f"Video ideas need a Google key file at {KEY}. The README's Video ideas section says how to make one.")
        from google.auth.transport.requests import AuthorizedSession  # here, not at the top: the rest of the app works without it
        from google.oauth2 import service_account
        _session.append(AuthorizedSession(service_account.Credentials.from_service_account_file(
            str(KEY), scopes=["https://www.googleapis.com/auth/spreadsheets"])))
    return _session[0]


def call(method, url, **kw):
    import requests
    from google.auth.exceptions import RefreshError, TransportError
    s = session()
    try:
        r = s.request(method, url, timeout=20, **kw)
    except RefreshError:
        raise ValueError(f"Google didn’t accept the key file at {KEY}. Make a new key for the service account and save it there.")
    except (requests.RequestException, TransportError):
        raise ValueError("Google Sheets isn’t answering. Check the internet connection and try again.")
    if r.status_code in (403, 404):
        raise ValueError(f"Footage Flow can’t open the ideas sheet. Share it with {s.credentials.service_account_email} as an editor.")
    if not r.ok:
        raise ValueError(f"Google Sheets said: {r.json().get('error', {}).get('message', r.reason)}")
    return r.json()


def parse(rows):
    """Sheet rows to ideas, matched by header so columns can be moved around in the sheet."""
    header = [h.strip() for h in rows[0]] if rows else []
    missing = [c for c in COLUMNS.values() if c not in header]
    if missing:
        raise ValueError(f"The ideas sheet's first row needs these columns: {', '.join(missing)}")
    col = {k: header.index(c) for k, c in COLUMNS.items()}
    ideas = []
    for n, row in enumerate(rows[1:], start=2):
        cell = {k: (row[i] if i < len(row) else "").strip() for k, i in col.items()}
        if cell["id"] or cell["idea"]:
            ideas.append({**cell, "row": n, "status": cell["status"] or "Idea",
                          **{k: [x.strip() for x in cell[k].splitlines() if x.strip()] for k in LISTS}})
    return ideas, col, len(header)


def read():
    return parse(call("GET", f"{API}/A1:Z").get("values", []))  # no tab name: the first tab, whatever it's called


def clean(body, new=False):
    """What the page sent, checked, as the cell values to write."""
    out = {}
    for k in ("idea", "notes", "link"):
        if k in body:
            out[k] = str(body[k]).strip()[:2000]
    if "status" in body:
        if body["status"] not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        out["status"] = body["status"]
    for k in LISTS:
        if k in body:
            if not isinstance(body[k], list):
                raise ValueError(f"{k} must be a list")
            out[k] = "\n".join(dict.fromkeys(str(x).strip() for x in body[k] if str(x).strip()))
    if (new or "idea" in out) and not out.get("idea"):
        raise ValueError("an idea needs a title")
    return out


def letter(i):
    return chr(ord("A") + i)  # the sheet is read as A:Z, so a column is one letter


def add(body):
    fields = clean(body, new=True)
    with WRITE:
        ideas, col, width = read()
        n = max((int(m[1]) for i in ideas if (m := re.fullmatch(r"VI-(\d+)", i["id"]))), default=0) + 1
        fields = {"status": "Idea", **fields, "id": f"VI-{n:03d}", "added": str(date.today())}
        row = [""] * width
        for k, v in fields.items():
            row[col[k]] = v
        # RAW: an idea starting with "=" stays text, not a formula
        call("POST", f"{API}/A1:Z:append", params={"valueInputOption": "RAW", "insertDataOption": "INSERT_ROWS"}, json={"values": [row]})
    return fields["id"]


def update(idea_id, body):
    fields = clean(body)
    with WRITE:
        ideas, col, _ = read()  # find it by ID now: the sheet may have been sorted since the page loaded
        idea = next((i for i in ideas if i["id"] == idea_id), None)
        if not idea:
            raise ValueError("That idea isn’t in the sheet anymore. It may have been deleted there.")
        if fields:
            call("POST", f"{API}:batchUpdate", json={"valueInputOption": "RAW", "data": [
                {"range": f"{letter(col[k])}{idea['row']}", "values": [[v]]} for k, v in fields.items()]})
    return idea_id
