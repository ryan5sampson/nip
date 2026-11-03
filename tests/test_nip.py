import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nip.commands import build_edit, kept_ranges


def script(text):
    """Fake word timestamps: words take 0.3s with 0.05s gaps; "/" is a 1.2s pause."""
    words, t = [], 0.0
    for tok in text.split():
        if tok == "/":
            t += 1.2
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

    def test_cut_that_removes_previous_sentence(self):
        words, dur = script("good sentence / bad sentence NIP cut that / more good")
        e = build_edit(words, dur)
        pieces = kept_ranges(e["takes"][0], e["cuts"])
        spoken = lambda a, b: [w["w"] for w in words if w["start"] >= a and w["end"] <= b]
        kept = [w for a, b in pieces for w in spoken(a, b)]
        self.assertEqual(kept, ["good", "sentence", "more", "good"])

    def test_command_words_are_cut(self):
        words, dur = script("/ Take one. / hello")
        e = build_edit(words, dur)
        kept = kept_ranges(e["takes"][-1], e["cuts"])
        self.assertTrue(all(a >= words[1]["end"] for a, _ in kept))


class Export(unittest.TestCase):
    def test_ingest_and_export_end_to_end(self):
        import nip.cli as cli
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.multiple(cli, INBOX=d / "inbox", LIBRARY=d / "lib", EXPORTS=d / "out"):
                for p in (cli.INBOX, cli.LIBRARY, cli.EXPORTS):
                    p.mkdir()
                for name in ("a", "b"):
                    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=15:s=320x240:r=30",
                                    "-f", "lavfi", "-i", "sine=d=15", "-shortest", str(cli.INBOX / f"{name}.mp4")],
                                   check=True)
                words, _ = script("NIP this is demo day / / Take one / / bad / / / Take two / / good")
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
                self.assertFalse(any(cli.INBOX.iterdir()))


if __name__ == "__main__":
    unittest.main()
