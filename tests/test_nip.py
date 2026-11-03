import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nip.commands import apply_overrides, build_edit, kept_ranges


def script(text):
    """Fake word timestamps: words take 0.3s with 0.05s gaps; "/" is a 2s pause."""
    words, t = [], 0.0
    for tok in text.split():
        if tok == "/":
            t += 2.0
            continue
        words.append({"w": tok, "start": round(t, 2), "end": round(t + 0.3, 2)})
        t += 0.35
    return words, t + 1


def summary(text):
    words, dur = script(text)
    e = build_edit(words, dur)
    return [(t["group"], t["take"], t["keep"]) for t in e["takes"]], e


class Commands(unittest.TestCase):
    def test_no_takes_keeps_everything(self):
        takes, e = summary("just some gameplay footage here")
        self.assertEqual(takes, [(0, 0, True)])
        self.assertEqual(e["cuts"], [])

    def test_last_take_wins_and_preroll_dropped(self):
        takes, _ = summary("setting up / Take one. / hello world / Take two. / hello world again")
        self.assertEqual(takes, [(0, 0, False), (1, 1, False), (1, 2, True)])

    def test_scratch_that_take_two_stays_in_same_section(self):
        takes, _ = summary("/ Take one. / hello oops / scratch that, / Take two. / hello world / Take one. / next part")
        self.assertEqual(takes, [(1, 1, False), (1, 2, True), (2, 1, True)])

    def test_reset_without_numbers(self):
        takes, _ = summary("first try / reset / second try works")
        self.assertEqual([k for *_, k in takes], [False, True])

    def test_take_in_normal_speech_is_ignored(self):
        takes, _ = summary("you take two steps forward and reset the board")
        self.assertEqual(takes, [(0, 0, True)])

    def test_best_take_overrides_last(self):
        takes, _ = summary("/ Take one. / great line / NIP best take / Take two. / worse line")
        self.assertEqual(takes, [(1, 1, True), (1, 2, False)])

    def test_nip_description_tag_mark_pending(self):
        _, e = summary("NIP, this is playing Linderball with a big group / "
                       "gameplay / NIP tag funny / more / NIP mark that / NIP make it louder / end")
        self.assertEqual(e["description"], "playing linderball with a big group")
        self.assertEqual(e["tags"], ["funny"])
        self.assertEqual(len(e["markers"]), 1)
        self.assertEqual(e["pending"][0]["text"], "make it louder")

    def test_nip_instruction_stops_at_pause(self):
        _, e = summary("NIP, this is a test / this is narration")
        self.assertEqual(e["description"], "a test")

    def test_instruction_stops_at_sentence_end(self):
        _, e = summary("NIP. Pause. more words / NIP, this is a test. Not part of it")
        self.assertEqual(e["description"], "a test")
        self.assertEqual(e["pending"], [])

    def test_numbered_clips_highlight_note_cut_everything(self):
        words, dur = script("NIP, this is couch day. / setup chatter / NIP, cut everything up to here / "
                            "NIP, clip one, tossing on the couch. / talk talk / NIP, that was plant talk / "
                            "NIP, clip two, bouncing off the table. I'm sorry / slip / NIP, clipped the last 10 seconds")
        e = build_edit(words, dur)
        self.assertEqual(e["description"], "couch day")
        self.assertEqual([(t["name"], t["keep"]) for t in e["takes"]],
                         [(None, False), ("tossing on the couch", True), ("bouncing off the table", True)])
        self.assertEqual(e["notes"][0]["text"], "that was plant talk")
        self.assertIn("cut everything", [c["why"] for c in e["cuts"]])
        hl = e["markers"][0]
        self.assertEqual((hl["t"], hl["end"]), (e["takes"][2]["start"], words[-6]["start"]))  # clamped to clip start

    def test_cut_that_removes_previous_sentence(self):
        words, dur = script("good sentence / bad sentence NIP cut that / more good")
        e = build_edit(words, dur)
        pieces = kept_ranges(e["takes"][0], e["cuts"])
        spoken = lambda a, b: [w["w"] for w in words if w["start"] >= a and w["end"] <= b]
        kept = [w for a, b in pieces for w in spoken(a, b)]
        self.assertEqual(kept, ["good", "sentence", "more", "good"])

    def test_nip_prefixed_takes_scrap_and_best(self):
        takes, _ = summary("NIP take one / hello I'm NIP scrap that / NIP take two / good / "
                           "NIP take three / worse / NIP tag that as the best one / NIP take one / next")
        self.assertEqual(takes, [(1, 1, False), (1, 2, False), (1, 3, True), (2, 1, True)])

    def test_new_clip_groups_takes_by_name(self):
        _, e = summary("intro / NIP new clip curve throw / throwing / NIP take two / throw / "
                       "NIP take two / again / NIP take three / last / NIP best one / "
                       "NIP new clip / just one try")
        self.assertEqual([(t["name"], t["take"], t["keep"]) for t in e["takes"]],
                         [(None, 0, False), ("curve throw", 1, False), ("curve throw", 2, False),
                          ("curve throw", 2, False), ("curve throw", 3, True), (None, 1, True)])

    def test_try_again_cuts_back_to_last_pause(self):
        words, dur = script("good line / bad line / NIP let me try that again / good line two")
        e = build_edit(words, dur)
        pieces = kept_ranges(e["takes"][0], e["cuts"])
        kept = [w["w"] for a, b in pieces for w in words if w["start"] >= a and w["end"] <= b]
        self.assertEqual(kept, ["good", "line", "good", "line", "two"])

    def test_pause_resume_and_cut_silence(self):
        words, dur = script("NIP take one / NIP cut the silence / one / / / two NIP pause / waiting / "
                            "NIP resume three / / / NIP pause and cut the silence at the end / gone")
        e = build_edit(words, dur)
        pieces = kept_ranges(e["takes"][-1], e["cuts"])
        kept = [w["w"] for a, b in pieces for w in words if w["start"] >= a and w["end"] <= b]
        self.assertEqual(kept, ["one", "two", "three"])
        self.assertLess(sum(b - a for a, b in pieces), 3)  # the 3.6s gap after "one" was shortened
        self.assertEqual(e["pending"], [])

    def test_command_words_are_cut(self):
        words, dur = script("/ Take one. / hello")
        e = build_edit(words, dur)
        kept = kept_ranges(e["takes"][-1], e["cuts"])
        self.assertTrue(all(a >= words[1]["end"] for a, _ in kept))


class Overrides(unittest.TestCase):
    def test_hand_edits_match_by_original_start(self):
        _, e = summary("NIP new clip intro / one / NIP take two / two")
        a, b = e["takes"]
        out = apply_overrides(e, {str(a["start"]): {"keep": True, "end": a["end"] - 0.5, "name": "hello"},
                                  "999": {"keep": False}})  # 999: a take that no longer exists
        self.assertEqual([(t["keep"], t["name"], t.get("edited", False)) for t in out["takes"]],
                         [(True, "hello", True), (True, "hello", False)])
        self.assertEqual(out["takes"][0]["at"], a["start"])
        self.assertFalse(e["takes"][0]["keep"])  # the voice decisions are left as they were


class Viewer(unittest.TestCase):
    def test_serves_ranges_and_rejects_bad_requests(self):
        import threading
        import urllib.error
        import urllib.request
        import nip.cli as cli
        import nip.view as view
        with tempfile.TemporaryDirectory() as d:
            lib = Path(d) / "lib"
            (lib / "p" / "raw").mkdir(parents=True)
            (lib / "p" / "raw" / "v.mov").write_bytes(bytes(range(100)))
            words, dur = script("NIP take one / hello")
            cli.save(lib / "p" / "project.json", {"description": None, "tags": [], "clips": ["v"]})
            cli.save(lib / "p" / "v.nip.json", {"source": "v.mov", "duration": dur, "words": words,
                                                 "edit": build_edit(words, dur)})
            with mock.patch.object(cli, "LIBRARY", lib):
                server = view.ThreadingHTTPServer(("127.0.0.1", 0), view.Handler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                url = f"http://127.0.0.1:{server.server_port}"

                def get(path, **headers):
                    return urllib.request.urlopen(urllib.request.Request(url + path, headers=headers))

                def post(path, body):
                    return urllib.request.urlopen(urllib.request.Request(
                        url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}))

                r = get("/media/p/v.mov", Range="bytes=10-19")
                self.assertEqual((r.status, r.read()), (206, bytes(range(10, 20))))
                for bad in ("/media/p/..%2Fproject.json", "/media/../p/raw/v.mov", "/api/project/nope"):
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        get(bad)
                    self.assertEqual(cm.exception.code, 404)
                take = json.loads(get("/api/project/p").read())["clips"][0]["edit"]["takes"][-1]
                with self.assertRaises(urllib.error.HTTPError) as cm:
                    post("/api/override/p/v", {"at": take["at"], "start": take["end"] + 1})
                self.assertEqual(cm.exception.code, 400)
                post("/api/override/p/v", {"at": take["at"], "keep": False})
                self.assertFalse(cli.decisions(cli.load(lib / "p" / "v.nip.json"))["takes"][-1]["keep"])
                server.shutdown()


class Export(unittest.TestCase):
    def test_ingest_and_export_end_to_end(self):
        import nip.cli as cli
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.multiple(cli, INBOX=d / "inbox", LIBRARY=d / "lib", EXPORTS=d / "out"):
                for p in (cli.INBOX, cli.LIBRARY, cli.EXPORTS):
                    p.mkdir()
                for name in ("a", "b"):
                    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=15:s=1440x2560:r=30",
                                    "-f", "lavfi", "-i", "sine=d=15", "-shortest", str(cli.INBOX / f"{name}.mp4")],
                                   check=True)
                words, _ = script("NIP this is demo day / Take one / bad / Take two / good")
                with mock.patch.object(cli, "transcribe", return_value=words), \
                        mock.patch.object(cli, "settled", return_value=True):
                    cli.ingest()
                proj = cli.find("")
                self.assertTrue(proj.name.endswith("_demo-day"))
                self.assertEqual(json.loads((proj / "project.json").read_text())["clips"], ["a", "b"])
                cli.export(proj, combined=True)
                outs = sorted(p.name for p in (cli.EXPORTS / proj.name).iterdir())
                self.assertEqual(outs, ["01_a_s1t2.mp4", "02_b_s1t2.mp4", f"{proj.name}.mp4"])
                dur, _ = cli.probe(cli.EXPORTS / proj.name / f"{proj.name}.mp4")
                self.assertGreater(dur, 1)
                size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "csv=p=0", str(cli.EXPORTS / proj.name / outs[0])],
                                      capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(size, "1080,1920")  # portrait source scaled to the social size
                self.assertFalse(any(cli.INBOX.iterdir()))


if __name__ == "__main__":
    unittest.main()
