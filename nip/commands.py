"""Turn a word-timestamped transcript into edit decisions. Pure functions, no I/O."""
import re

WAKE = {"nip", "nipp", "knip", "nips", "nyp"}
NUMBERS = {"one": 1, "won": 1, "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
           "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
PAUSE = 0.25        # gap (s) that makes "reset" / "take two" a standalone command, not normal speech
NIP_WINDOW = 3.0    # instruction must start within this many seconds of the wake word
NIP_GAP = 1.0       # a silence this long ends the instruction
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
        if t in WAKE:
            j = i
            while (j + 1 < len(words) and words[j + 1]["start"] - words[j]["end"] < NIP_GAP
                   and words[j + 1]["start"] <= words[i]["end"] + NIP_WINDOW):
                j += 1
            text = " ".join(toks[i + 1:j + 1])
            events.append({"kind": "nip", "start": words[i]["start"], "end": words[j]["end"], "text": text})
        elif t == "take" and number(nxt) and isolated(words, i, i + 1):
            j = i + 1
            events.append({"kind": "take", "n": number(nxt), "start": words[i]["start"], "end": words[j]["end"]})
        elif t == "scratch" and nxt == "that":
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

    Takes: a "take N" with N <= the previous take number starts a new group (a new section).
    In each group the last take is kept unless "NIP best take" picked another.
    "Scratch that" / "reset" drops the current take and starts the next one in the same group.
    Footage before the first take marker is dropped if the clip has takes, else kept (e.g. gameplay).
    """
    events = find_events(words)
    edit = {"takes": [], "cuts": [], "markers": [], "description": None, "tags": [], "pending": []}
    takes = edit["takes"]
    cur = {"start": 0.0, "group": 0, "take": 0, "keep": True, "why": "no take markers"}
    last_n = 0  # last spoken take number; "scratch that, take two" must not start a new group

    def close(at):
        cur["end"] = at
        spoken = any(cur["start"] <= w["start"] < at for w in words)
        if at - cur["start"] >= 3 or (spoken and at - cur["start"] > 0.5):  # skip pauses between commands
            takes.append(cur)

    for e in events:
        edit["cuts"].append({"start": max(0.0, e["start"] - PAD_BEFORE),
                             "end": min(duration, e["end"] + PAD_AFTER), "why": e["kind"]})
        if e["kind"] == "take":
            new_group = e["n"] <= last_n or cur["group"] == 0
            last_n = e["n"]
            close(e["start"])
            cur = {"start": e["end"], "group": cur["group"] + new_group, "take": e["n"], "keep": False, "why": ""}
        elif e["kind"] == "reset":
            cur["why"] = "scratched"
            close(e["start"])
            cur = {"start": e["end"], "group": max(cur["group"], 1), "take": cur["take"] + 1, "keep": False, "why": ""}
        else:
            text = e["text"]
            if "take" in text and re.search(r"\b(best|keep|keeper|good)\b", text):
                cur["why"] = "picked as best"
            elif re.search(r"\b(cut|delete|remove|scrap)\b", text) and "that" in text:
                edit["cuts"].append({"start": last_pause_before(words, e["start"], cur["start"]),
                                     "end": e["start"], "why": "nip cut that"})
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
