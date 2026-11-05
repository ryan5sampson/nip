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


STOCK = {"thank you", "thanks for watching", "thank you for watching", "thank you so much for watching",
         "you", "bye", "please subscribe", "subscribe"}  # what Whisper says over silence


def made_up(segment):
    """True if one Whisper segment looks invented from silence or noise: real speech averages 0.85+ word confidence,
    made-up words under 0.1, and stock phrases like "Thank you." come out around 0.5. Voice commands always stay."""
    if not segment or any(norm(w["w"]) in WAKE for w in segment):
        return False
    p = sum(w.get("p", 1.0) for w in segment) / len(segment)
    return p < 0.45 or (" ".join(norm(w["w"]) for w in segment) in STOCK and p < 0.8)


def drop_loops(words):
    """Split words into (kept, loops): a phrase of up to 6 words said 3+ times in a row (a single word 5+ times),
    the loop Whisper falls into over silence. Saying something twice, like a retake, stays."""
    toks = [norm(w["w"]) for w in words]
    gone, i = set(), 0
    while i < len(toks):
        run = 0
        for n in range(1, 7):
            reps = 1
            while toks[i + reps * n:i + (reps + 1) * n] == toks[i:i + n]:
                reps += 1
            if any(toks[i:i + n]) and reps >= (5 if n == 1 else 3):
                run = max(run, reps * n)
        gone.update(range(i, i + run))
        i += run or 1
    return [w for k, w in enumerate(words) if k not in gone], [w for k, w in enumerate(words) if k in gone]


def respell(words, spellings):
    """Put words in the user's spelling. spellings maps a phrase, lowercased with spaces and punctuation gone, to how
    it's written; a phrase said as several words becomes one word spanning them, so "Linder ball" is "Linderball"."""
    out, i = [], 0
    while i < len(words):
        for n in range(min(4, len(words) - i), 0, -1):  # longest first: "bamboo lab" before "bamboo"
            right = spellings.get("".join(norm(w["w"]) for w in words[i:i + n]))
            if right:
                last = words[i + n - 1]
                out.append({**words[i], "w": right + re.search(r"\W*$", last["w"])[0], "end": last["end"]})
                i += n
                break
        else:
            out.append(words[i])
            i += 1
    return out


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
    since = 0.0  # end of the last NIP command: "NIP cut", "NIP keep" and "tag that" act on footage since then

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
        prev, since = since, e["end"]
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
            if not text or text == "split":  # a bare "NIP" (or "NIP split") is a divider to refer back to
                edit["markers"].append({"t": e["end"], "label": "divider"})
            elif last and number(last[1]):
                a = max(cur["start"], e["start"] - number(last[1]))
                if re.match(r"(cut|delete|remove)\b", text):  # "cut the last 5 seconds"
                    edit["cuts"].append({"start": a, "end": e["start"], "why": "nip cut"})
                else:
                    edit["markers"].append({"t": a, "end": e["start"], "label": text})
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
            elif re.search(r"\bagain\b", text):  # a retry: only the line just said goes
                edit["cuts"].append({"start": last_pause_before(words, e["start"], cur["start"]),
                                     "end": e["start"], "why": "nip cut that"})
            elif re.match(r"(cut|delete|remove)\b", text):  # "NIP cut (that / to the last nip)"
                edit["cuts"].append({"start": max(prev, cur["start"]), "end": e["start"], "why": "nip cut"})
            elif re.match(r"keep\b", text):  # "NIP keep (that)": since the last NIP is worth keeping
                edit["markers"].append({"t": max(prev, cur["start"]), "end": e["start"], "label": "keep"})
            elif text.startswith("that was"):  # a note on the stretch just filmed
                edit["notes"].append({"t": e["start"], "text": text})
            elif text.startswith("this is"):
                edit["description"] = text[len("this is"):].strip()
            elif text.startswith("tag that"):  # "tag that as a good clip" highlights since the last NIP
                tag = re.sub(r"^tag that( as)?( an?| the)?\b", "", text).strip()
                edit["markers"].append({"t": max(prev, cur["start"]), "end": e["start"], "label": tag or "tagged"})
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


def unmarked(edit):
    """Kept footage the user hasn't marked keep: the pieces of kept takes outside every cut and keep."""
    keeps = [{"start": k["start"], "end": k["end"]} for k in edit["keeps"]]
    out = [p for t in edit["takes"] if t["keep"] for p in kept_ranges(t, edit["cuts"] + keeps)]
    return sorted(p for p in out if p[1] - p[0] >= 0.5)  # slivers between marks aren't worth asking about


def kept_pieces(edit):
    """Everything not cut, as (start, end) pieces in time order."""
    merged = []
    for a, b in sorted(p for t in edit["takes"] if t["keep"] for p in kept_ranges(t, edit["cuts"])):
        if merged and a <= merged[-1][1] + 0.01:
            merged[-1] = (merged[-1][0], max(merged[-1][1], b))
        else:
            merged.append((a, b))
    return merged


def silences(words, a, b):
    """The stretches of a..b with no speech, SILENCE seconds or longer."""
    out, t = [], a
    for w in words:
        if w["end"] > a and w["start"] < b:
            if w["start"] - t >= SILENCE:
                out.append([t, w["start"]])
            t = max(t, w["end"])
    return out + [[t, b]] if b - t >= SILENCE else out


def timeline(edit, duration, words=()):
    """The clips the user sees, in order: kept footage (keep or not marked) and the cut stretches between. Splits and
    the edges of names divide them, so named neighbours never merge. Each lists its silences."""
    out, t, loose = [], 0.0, unmarked(edit)
    for a, b in kept_pieces(edit):
        if a - t > 0.05:
            out.append({"gap": True, "keep": False, "a": t, "b": a})
        x = a  # inside a kept piece: unmarked stretches, and keep between them
        for ua, ub in (p for p in loose if p[0] < b and p[1] > a):
            if ua - x > 0.05:
                out.append({"gap": False, "keep": True, "a": x, "b": ua})
            out.append({"gap": False, "keep": False, "a": max(ua, a), "b": min(ub, b)})
            x = min(ub, b)
        if b - x > 0.05:
            out.append({"gap": False, "keep": True, "a": x, "b": b})
        t = b
    if duration - t > 0.05:
        out.append({"gap": True, "keep": False, "a": t, "b": duration})
    at = [s["t"] for s in edit["splits"]] + [t for x in edit["labels"] for t in (x["start"], x["end"])]
    clips = []
    for s in out:
        pts = [] if s["gap"] else sorted({t for t in at if s["a"] + 0.05 < t < s["b"] - 0.05})
        clips += [{**s, "a": a, "b": b, "silences": silences(words, a, b)}
                  for a, b in zip([s["a"], *pts], [*pts, s["b"]])]
    return clips


def clip_name(s, edit):
    """The name the user gave this clip: the closest-fitting one around it, or ""."""
    return min((x for x in edit["labels"] if x["start"] <= s["a"] + 0.05 and s["b"] <= x["end"] + 0.05),
               key=lambda x: x["end"] - x["start"], default={}).get("text", "")


def tiers(edit, duration):
    """The three tiers as (start, end) lists: cut (folded away), unmarked, and keep."""
    merged, loose = kept_pieces(edit), unmarked(edit)
    keep = [p for a, b in merged for p in kept_ranges({"start": a, "end": b}, [{"start": x, "end": y} for x, y in loose])
            if p[1] - p[0] >= 0.5]
    cut = kept_ranges({"start": 0.0, "end": duration}, [{"start": a, "end": b} for a, b in merged])
    return {"cut": cut, "unmarked": loose, "keep": keep}


def apply_overrides(edit, overrides):
    """Hand edits from the viewer on top of the voice decisions.

    overrides maps a take's original start (as a string) to changed fields: keep, start, end, name.
    Matching by original start, not list position, keeps them through `nip reparse`.
    """
    takes = [dict(t) for t in edit["takes"]]
    starts = [t["start"] for t in takes]
    for t, s in zip(takes, starts):
        t["at"] = s
    for at, o in overrides.items():
        i = min(range(len(takes)), key=lambda i: abs(starts[i] - float(at)), default=None)
        if i is None or abs(starts[i] - float(at)) > 1.0:  # take is gone after a rule change; ignore
            continue
        for k in ("keep", "start", "end"):
            if k in o:
                takes[i][k] = o[k]
        if "name" in o:  # a name belongs to the whole clip
            for t in takes:
                if t["group"] == takes[i]["group"]:
                    t["name"] = o["name"] or None
        takes[i]["edited"] = True
    return {**edit, "takes": takes}


def apply_cut_edits(edit, restored, added, kept=(), brought=()):
    """Hand changes to the cuts: restore voice cuts (matched by start time) and add the user's own.

    brought is footage the user brought back by range: it beats any cut or dropped take, so part of a cut can come back.
    Also lists the keeps: voice highlights ("NIP keep", "last 10 seconds", "tag that") and the user's own.
    """
    cuts = [c for c in edit["cuts"] if not any(abs(c["start"] - r) < 0.01 for r in restored)]
    cuts += [{"start": a["start"], "end": a["end"], "why": "cut by you"} for a in added]
    keeps = [{"start": m["t"], "end": m["end"], "why": m["label"]} for m in edit["markers"] if "end" in m]
    keeps += [{"start": k["start"], "end": k["end"], "why": "kept by you"} for k in kept]
    takes, back = edit["takes"], []
    for r in sorted(brought, key=lambda r: r["start"]):  # overlapping ranges joined
        if back and r["start"] <= back[-1]["end"]:
            back[-1]["end"] = max(back[-1]["end"], r["end"])
        else:
            back.append(dict(r))
    if back:
        def uncut(c):  # orig: a shortened cut is still restored by the start it was made with
            left = kept_ranges(c, back)
            return [c] if left == [(c["start"], c["end"])] else [{**c, "start": a, "end": b, "orig": c.get("orig", c["start"])} for a, b in left]
        cuts = [p for c in cuts for p in uncut(c)]
        on = [t for t in takes if t["keep"]]
        takes = sorted(takes + [{"start": a, "end": b, "at": a, "keep": True, "why": "brought back by you", "group": 0, "take": 0,
                                 "name": None, "mine": True} for r in back for a, b in kept_ranges(r, on)], key=lambda t: t["start"])
    return {**edit, "takes": takes, "cuts": cuts, "keeps": keeps}
