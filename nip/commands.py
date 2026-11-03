"""Turn a word-timestamped transcript into edit decisions. Pure functions, no I/O."""
import re

WAKE = {"nip", "nipp", "knip", "nips", "nyp"}
SCRATCH = {"scratch", "scrap"}
NUMBERS = {"one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
           "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
PAUSE = 0.25        # gap (s) that makes "reset" / "take two" a standalone command, not normal speech
NIP_WINDOW = 8.0    # instruction must start within this many seconds of the wake word
NIP_GAP = 1.5       # a silence this long ends the instruction (users pause ~2s after a command)
SILENCE = 1.0       # "NIP, cut the silence" shortens gaps longer than this
PAD_BEFORE, PAD_AFTER = 0.1, 0.3


def norm(w):
    return re.sub(r"[^a-z0-9]", "", w.lower())


def number(tok):
    return int(tok) if tok.isdigit() else NUMBERS.get(tok)


def isolated(words, i, j):
    """True if words[i..j] has a pause (or the clip edge) on both sides."""
    before = i == 0 or words[i]["start"] - words[i - 1]["end"] >= PAUSE
    after = j == len(words) - 1 or words[j + 1]["start"] - words[j]["end"] >= PAUSE
    return before and after


def find_events(words):
    toks = [norm(w["w"]) for w in words]
    events, i = [], 0
    while i < len(words):
        t, nxt = toks[i], toks[i + 1] if i + 1 < len(toks) else ""
        a, b = (toks + ["", ""])[i + 1:i + 3]
        if t in WAKE and a == "take" and number(b):  # "NIP, take two": the wake word stands in for the pause
            j = i + 2
            events.append({"kind": "take", "n": number(b), "start": words[i]["start"], "end": words[j]["end"]})
        elif t in WAKE and (a in SCRATCH and b == "that" or a == "reset"):
            j = i + 1 + (a != "reset")
            events.append({"kind": "reset", "start": words[i]["start"], "end": words[j]["end"]})
        elif t in WAKE and a == "resume":  # speech right after "resume" belongs to the take
            j = i + 1
            events.append({"kind": "nip", "start": words[i]["start"], "end": words[j]["end"], "text": "resume"})
        elif t in WAKE:
            j = i
            # ends at a pause or at Whisper's sentence end ("NIP." itself doesn't count)
            while (j + 1 < len(words) and words[j + 1]["start"] - words[j]["end"] < NIP_GAP
                   and (j == i or not words[j]["w"].endswith((".", "?", "!")))
                   and words[j + 1]["start"] <= words[i]["end"] + NIP_WINDOW):
                j += 1
            text = " ".join(toks[i + 1:j + 1])
            events.append({"kind": "nip", "start": words[i]["start"], "end": words[j]["end"], "text": text})
        elif t == "take" and number(nxt) and isolated(words, i, i + 1):
            j = i + 1
            events.append({"kind": "take", "n": number(nxt), "start": words[i]["start"], "end": words[j]["end"]})
        elif t in SCRATCH and nxt == "that":
            j = i + 1
            events.append({"kind": "reset", "start": words[i]["start"], "end": words[j]["end"]})
        elif t == "reset" and isolated(words, i, i):
            j = i
            events.append({"kind": "reset", "start": words[i]["start"], "end": words[j]["end"]})
        else:
            i += 1
            continue
        i = j + 1
    return events


def last_pause_before(words, t, floor):
    """Start of the sentence that ends at t: the last >=1s silence before t, but not before floor."""
    for k in range(len(words) - 1, 0, -1):
        if words[k]["start"] < t and words[k]["start"] - words[k - 1]["end"] >= 1.0:
            return max(words[k]["start"], floor)
    return floor


def build_edit(words, duration):
    """Return the edit decisions for one clip.

    Clips: "NIP, new clip [name]" starts a new group; the footage right after it is take 1.
    Takes: a "take N" with N <= the previous take number starts a new group (a new section),
    except after "new clip", where only "new clip" starts one (a repeated number is just a retake).
    In each group the last take is kept unless "NIP best take" picked another.
    "Scratch that" / "reset" drops the current take and starts the next one in the same group.
    Footage before the first take marker is dropped if the clip has takes, else kept (e.g. gameplay).
    """
    events = find_events(words)
    edit = {"takes": [], "cuts": [], "markers": [], "notes": [], "description": None, "tags": [], "pending": []}
    takes = edit["takes"]
    cur = {"start": 0.0, "group": 0, "take": 0, "keep": True, "why": "no take markers", "name": None}
    last_n = 0  # last spoken take number; "scratch that, take two" must not start a new group
    clips = False  # "new clip" was used, so take numbers no longer start groups

    def close(at):
        cur["end"] = at
        if cur.get("paused") is not None:  # paused and never resumed: the rest of the take goes
            edit["cuts"].append({"start": cur["paused"], "end": at, "why": "paused"})
        if cur.get("trim"):
            inside = [w for w in words if cur["start"] <= w["start"] and w["end"] <= at]
            for a, b in zip([cur["start"]] + [w["end"] for w in inside], [w["start"] for w in inside] + [at]):
                if b - a > SILENCE:
                    edit["cuts"].append({"start": a + PAD_AFTER, "end": b - PAD_BEFORE, "why": "silence"})
        spoken = any(cur["start"] <= w["start"] < at for w in words)
        if at - cur["start"] >= 3 or (spoken and at - cur["start"] > 0.5):  # skip pauses between commands
            takes.append(cur)

    for e in events:
        # pad the command cut, but never into a neighbouring word ("NIP resume as a way")
        prev_end = max((w["end"] for w in words if w["end"] <= e["start"]), default=0.0)
        next_start = min((w["start"] for w in words if w["start"] >= e["end"]), default=duration)
        edit["cuts"].append({"start": max(prev_end, e["start"] - PAD_BEFORE),
                             "end": min(next_start, e["end"] + PAD_AFTER), "why": e["kind"]})
        if e["kind"] == "take":
            new_group = cur["group"] == 0 or (e["n"] <= last_n and not clips)
            last_n = e["n"]
            close(e["start"])
            cur = {"start": e["end"], "group": cur["group"] + new_group, "take": e["n"], "keep": False, "why": "",
                   "name": None if new_group else cur["name"]}
        elif e["kind"] == "reset":
            cur["why"] = "scratched"
            close(e["start"])
            cur = {"start": e["end"], "group": max(cur["group"], 1), "take": cur["take"] + 1, "keep": False, "why": "",
                   "name": cur["name"]}
        else:
            text = e["text"]
            clip = re.match(r"(new clip|clip(?:ped)? (\w+))\b(.*)", text)  # "new clip x", "clip two x"
            last = re.search(r"\blast (\w+) seconds?\b", text)
            if last and number(last[1]):
                edit["markers"].append({"t": max(cur["start"], e["start"] - number(last[1])), "end": e["start"],
                                        "label": text})
            elif clip and (clip[2] is None or number(clip[2])):
                close(e["start"])
                name = re.sub(r"^(called|named)\b", "", clip[3].strip()).strip()
                cur = {"start": e["end"], "group": cur["group"] + 1, "take": 1, "keep": False, "why": "",
                       "name": name or None}
                last_n, clips = 0, True
            elif text.startswith("pause") or "silence" in text:
                if text.startswith("pause"):
                    cur["paused"] = e["start"]
                if "silence" in text:
                    cur["trim"] = True
            elif text == "resume":
                if cur.get("paused") is not None:
                    edit["cuts"].append({"start": cur.pop("paused"), "end": e["start"], "why": "paused"})
            elif re.search(r"\bbest\b", text) or ("take" in text and re.search(r"\b(keep|keeper|good)\b", text)):
                cur["why"] = "picked as best"
            elif re.search(r"\bcut everything\b", text):
                edit["cuts"].append({"start": cur["start"], "end": e["start"], "why": "cut everything"})
            elif re.search(r"\b(cut|delete|remove|scrap)\b", text) and "that" in text or re.search(r"\bagain\b", text):
                edit["cuts"].append({"start": last_pause_before(words, e["start"], cur["start"]),
                                     "end": e["start"], "why": "nip cut that"})
            elif text.startswith("that was"):  # a note on the stretch just filmed
                edit["notes"].append({"t": e["start"], "text": text})
            elif text.startswith("this is"):
                edit["description"] = text[len("this is"):].strip()
            elif text.startswith("tag"):
                edit["tags"].append(text[3:].strip())
            elif re.search(r"\b(mark|highlight|flag|clip)\b", text):
                edit["markers"].append({"t": e["start"], "label": text})
            elif text:
                edit["pending"].append({"t": e["start"], "text": text})
    close(duration)

    for g in {t["group"] for t in takes}:
        group = [t for t in takes if t["group"] == g]
        if g == 0:
            for t in group:
                t["keep"] = len(takes) == len(group)
                t["why"] = "no take markers" if t["keep"] else "before first take"
            continue
        picked = [t for t in group if t["why"] == "picked as best"]
        best = picked[-1] if picked else next((t for t in reversed(group) if t["why"] != "scratched"), None)
        for t in group:
            t["keep"] = t is best
            t["why"] = t["why"] or ("last take" if t is best else "earlier take")
    return edit


def kept_ranges(take, cuts):
    """The (start, end) pieces of a take that survive the cuts."""
    pieces = [(take["start"], take["end"])]
    for c in cuts:
        pieces = [p for a, b in pieces
                  for p in ((a, min(b, c["start"])), (max(a, c["end"]), b)) if p[1] - p[0] > 0.05]
    return pieces
