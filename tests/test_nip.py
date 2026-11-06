import json
import shutil
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from nip import brief, cli
from nip.commands import apply_cut_edits, apply_overrides, build_edit, drop_loops, kept_ranges, made_up, respell, silences, timeline, unmarked


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

    def test_cut_that_removes_since_last_nip(self):
        words, dur = script("good sentence / NIP / bad sentence / still bad NIP cut that / more good")
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

    def test_bare_nip_divider_cut_and_tag(self):
        words, dur = script("keep this / NIP / bad stuff here / NIP cut to the last nip / "
                            "good stuff / NIP / great bit / NIP tag that as a good clip / end NIP keep")
        e = build_edit(words, dur)
        kept = [w["w"] for a, b in kept_ranges(e["takes"][0], e["cuts"]) for w in words
                if w["start"] >= a and w["end"] <= b]
        self.assertEqual(kept, ["keep", "this", "good", "stuff", "great", "bit", "end"])
        self.assertEqual([m["label"] for m in e["markers"]], ["divider", "divider", "good clip", "keep"])
        self.assertEqual([m["label"] for m in build_edit(*script("one / NIP split / two"))["markers"]], ["divider"])
        self.assertEqual((e["markers"][2]["t"], e["markers"][2]["end"]), (words[14]["end"], words[17]["start"]))
        self.assertEqual(e["markers"][3]["t"], words[-4]["end"])  # "NIP keep" starts after the previous NIP command
        self.assertEqual(e["pending"], [])
        # three tiers: what's neither cut nor marked keep is unmarked
        left = unmarked(apply_cut_edits(e, [], [], [{"start": 0.0, "end": words[1]["end"]}]))
        said = [[w["w"] for w in words if a <= w["start"] and w["end"] <= b] for a, b in left]
        self.assertEqual([x for x in said if x], [["good", "stuff"]])  # the rest is the test's silent pauses

    def test_made_up_words_from_silence_are_caught(self):
        seg = lambda text, p: [{"w": w, "start": 0, "end": 0, "p": p} for w in text.split()]
        self.assertTrue(made_up(seg("Wohnung take 30 plano runner", 0.0)))  # what Whisper said over silent B-roll
        self.assertTrue(made_up(seg("Thank you.", 0.52)))  # a stock phrase, said unclearly
        self.assertFalse(made_up(seg("Thank you.", 0.95)))  # said clearly, it stays
        self.assertFalse(made_up(seg("Hey, my name is Ryan.", 0.87)))
        self.assertFalse(made_up(seg("NIP cut", 0.2)))  # voice commands always stay
        words = lambda text: [{"w": w, "start": i, "end": i + 0.5} for i, w in enumerate(text.split())]
        said = lambda ws: " ".join(w["w"] for w in ws)
        kept, loops = drop_loops(words("you can see the " + "old " * 12 + "system"))
        self.assertEqual((said(kept), len(loops)), ("you can see the system", 12))
        kept, _ = drop_loops(words("in the room. " + "So, you can get the rest. " * 4 + "end"))
        self.assertEqual(said(kept), "in the room. end")
        retake = "In here I always have a... In here I always have a... bamboo"
        self.assertEqual(said(drop_loops(words(retake))[0]), retake)  # said twice stays
        self.assertEqual(said(drop_loops(words("no no no"))[0]), "no no no")

    def test_timeline_lists_clips_and_their_silences(self):
        import nip.brief as brief
        import nip.cli as cli
        w = lambda text, a, b: {"w": text, "start": a, "end": b}
        words = [w("hello", 1, 2), w("Keep.", 9, 10), w("So", 40, 41), w("then", 41, 42)]
        self.assertEqual(silences(words, 10, 45), [[10, 40], [42, 45]])  # the cut command before 10 doesn't count
        self.assertEqual(silences(words, 0.5, 1.5), [])  # under a second isn't marked
        take = {"start": 0, "end": 50, "keep": True, "at": 0, "group": 0, "take": 0, "why": ""}
        edit = {"takes": [take], "cuts": [{"start": 8, "end": 10, "why": "nip"}], "markers": [], "pending": []}
        clip = {"duration": 50.0, "words": words, "edit": edit, "my_keeps": [{"start": 0, "end": 8}]}
        e = cli.decisions(clip)
        got = [(s["gap"], s["keep"], s["a"], s["b"], s["silences"]) for s in timeline(e, 50.0, words)]
        self.assertEqual(got, [(False, True, 0, 8, [[0, 1], [2, 8]]), (True, False, 8, 10, [[8, 9]]),
                               (False, False, 10, 50, [[10, 40], [42, 50]])])
        said = brief.notes_prompt(clip, [{"id": 1, "t": None, "text": "cut long silences at the start of clips"}])
        self.assertIn('clip 10.00-50.00 unmarked name="" silences=10.00-40.00,42.00-50.00', said)
        self.assertIn("9.00-10.00 Keep. (cut)", said)  # NIP can't mistake a cut command for the clip's first word
        # two names side by side stay two clips
        clip["labels"] = [{"start": 10, "end": 20, "text": "a"}, {"start": 20, "end": 30, "text": "b"}]
        self.assertEqual([s["a"] for s in timeline(cli.decisions(clip), 50.0) if not s["gap"]], [0, 10, 20, 30])

    def test_keep_brings_back_cut_footage_and_merge_joins_clips(self):
        import nip.cli as cli
        words, _ = script("one two three")
        take = {"start": 2, "end": 50, "keep": True, "at": 2, "group": 1, "take": 1, "why": ""}
        dropped = {"start": 0, "end": 2, "keep": False, "at": 0, "group": 0, "take": 0, "why": "before"}
        edit = {"takes": [dropped, take], "cuts": [{"start": 10, "end": 20, "why": "nip cut"}],
                "markers": [{"t": 15, "label": "divider"}], "pending": []}
        clip = {"duration": 50.0, "words": [], "edit": edit}
        kept = lambda: [(s["a"], s["b"]) for s in timeline(cli.decisions(clip), 50.0) if not s["gap"]]
        self.assertEqual(kept(), [(2, 10), (20, 50)])
        cli.add_cut(clip, 12, 14, "my_keeps")  # part of a voice cut comes back
        self.assertEqual(kept(), [(2, 10), (12, 14), (20, 50)])
        self.assertEqual([(c["start"], c["end"], c["orig"]) for c in cli.decisions(clip)["cuts"]], [(10, 12, 10), (14, 20, 10)])
        cli.add_cut(clip, 12, 13)  # the newest mark wins: cut it again
        self.assertEqual(kept(), [(2, 10), (13, 14), (20, 50)])
        cli.add_cut(clip, 0.5, 1, "my_keeps")  # footage in a dropped take too
        self.assertEqual(kept()[0], (0.5, 1))
        self.assertEqual([t["keep"] for t in cli.decisions(clip)["takes"]].count(True), 2)
        # a selection stopping just short of the footage beside it closes the sliver: "run" straddled a cut's edge
        take2 = {**take, "start": 0, "at": 0}
        run = {"w": "run", "start": 9.5, "end": 10.16}
        sliver = {"duration": 50.0, "words": [run, {"w": "a", "start": 10.16, "end": 10.3}, {"w": "um", "start": 19.7, "end": 19.8}],
                  "edit": {**edit, "takes": [take2], "markers": []}}
        cli.add_cut(sliver, 10.16, 19.6, "my_keeps")
        pieces = lambda c: [(s["a"], s["b"]) for s in timeline(cli.decisions(c), 50.0) if not s["gap"]]
        self.assertEqual(pieces(sliver), [(0, 10), (10, 19.6), (20, 50)])  # no gap on the left; "um", a whole word, stays cut on the right
        real = {"duration": 50.0, "words": [], "edit": {**edit, "takes": [take2], "cuts": [{"start": 10, "end": 10.3, "why": "cut by you"}], "markers": []}}
        cli.add_cut(real, 10.4, 12, "my_keeps")  # keeping footage that wasn't cut leaves a real short cut alone
        self.assertEqual(pieces(real)[:2], [(0, 10), (10.3, 12)])
        # an old keep under a cut stays cut: only new marks bring footage back
        old = {"duration": 50.0, "words": [], "edit": edit, "my_keeps": [{"start": 25, "end": 35}], "my_cuts": [{"start": 28, "end": 30}]}
        self.assertIn((28, 30, "cut by you"), [(c["start"], c["end"], c["why"]) for c in cli.decisions(old)["cuts"]])
        # merge: the cut between comes back, the spoken divider inside goes, keep and name carry over
        clip = {"duration": 50.0, "words": [], "edit": edit, "labels": [{"start": 2, "end": 10, "text": "intro"}]}
        cli.add_cut(clip, 2, 10, "my_keeps")
        cli.merge(clip, 2, 50, "intro")
        e = cli.decisions(clip)
        got = [(s["a"], s["b"], s["keep"]) for s in timeline(e, 50.0) if not s["gap"]]
        self.assertEqual((got, e["splits"], e["labels"]), ([(2, 50, True)], [], [{"start": 2.0, "end": 50.0, "text": "intro"}]))
        # keeps side by side are one clip; a name still divides
        clip = {"duration": 50.0, "words": [], "edit": {**edit, "cuts": [], "markers": []}}
        cli.add_cut(clip, 2, 10, "my_keeps")
        cli.add_cut(clip, 10, 20, "my_keeps")
        self.assertEqual(kept()[0], (2, 20))
        clip["labels"] = [{"start": 2, "end": 10, "text": "a"}]
        self.assertEqual(kept()[:2], [(2, 10), (10, 20)])

    def test_vocabulary_fixes_spellings(self):
        import nip.cli as cli
        with tempfile.TemporaryDirectory() as d, mock.patch.object(cli, "VOCABULARY", Path(d) / "v.txt"):
            self.assertIn("Bambu Lab", cli.vocabulary()[0])  # the starting list, before there's a file
            cli.VOCABULARY.write_text("# a comment\nLinderball\nLinderball.com\nBambu Lab\nNIP\n\nbamboo lab = Bambu Lab # the printers\nbamboo = Bambu\nlinear ball = Linderball\n")
            written, spellings = cli.vocabulary()
        self.assertEqual(written, ["Linderball", "Linderball.com", "Bambu Lab", "NIP", "Bambu"])
        words = [{"w": w, "start": i, "end": i + 0.5} for i, w in enumerate(
            "the Linder ball, from linear ball at linderball.com on a bamboo lab. or Bamboo printer nip, cut".split())]
        fixed = respell(words, spellings)
        self.assertEqual(" ".join(w["w"] for w in fixed),
                         "the Linderball, from Linderball at Linderball.com on a Bambu Lab. or Bambu printer NIP, cut")
        self.assertEqual((fixed[1]["start"], fixed[1]["end"]), (1, 2.5))  # one word now spans both
        self.assertEqual(respell(fixed, spellings), fixed)  # already right stays right
        self.assertEqual(build_edit(fixed, 20)["cuts"], build_edit(words, 20)["cuts"])  # voice commands still work

    def test_command_words_are_cut(self):
        words, dur = script("/ Take one. / hello")
        e = build_edit(words, dur)
        kept = kept_ranges(e["takes"][-1], e["cuts"])
        self.assertTrue(all(a >= words[1]["end"] for a, _ in kept))


class Brief(unittest.TestCase):
    def clips(self):
        words, dur = script("NIP take one / Linderballs are great. They bounce / um / and you can play anywhere")
        return [{"stem": "v", "words": words, "duration": dur, "edit": build_edit(words, dur)}]

    def test_phrases_skip_commands_and_split_at_pauses(self):
        from nip.brief import phrases
        self.assertEqual([p["text"] for p in phrases(self.clips())],
                         ["Linderballs are great.", "They bounce", "um", "and you can play anywhere"])

    def test_labelled_clips_can_be_picked_whole(self):
        from nip.brief import phrases, pieces, prompt
        clips = self.clips()
        clips[0]["edit"]["labels"] = [{"start": 4.0, "end": 9.0, "text": "slow motion throw"}]
        phs = phrases(clips)
        self.assertEqual((phs[-1]["id"], phs[-1]["text"]), (5, "slow motion throw"))
        self.assertIn("[5] v 4.0s (5.0s) kept CLIP: slow motion throw", prompt("b", phs))
        self.assertEqual(pieces([5], phs, {"v": 20.0}), [{"stem": "v", "start": 3.9, "end": 9.25}])

    def test_ask_checks_claudes_picks(self):
        from types import SimpleNamespace
        from nip import brief
        calls = []

        def create(**kw):
            calls.append(kw)
            text = json.dumps({"title": "Why", "note": "n", "phrases": [4, 1, 1, 99]})  # dupe and unknown id
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        out = brief.ask("30 seconds on why they're great", self.clips(), client=client)
        self.assertEqual(calls[0]["model"], "claude-opus-5-5")
        self.assertIn("[1] v", calls[0]["messages"][0]["content"])
        phs = brief.phrases(self.clips())
        self.assertEqual([(p["start"], p["end"]) for p in out["pieces"]],
                         [(round(phs[3]["start"] - 0.1, 2), round(phs[3]["end"] + 0.25, 2)),
                          (round(phs[0]["start"] - 0.1, 2), round(phs[0]["end"] + 0.25, 2))])


class BriefPieces(unittest.TestCase):
    def test_overlapping_neighbours_merge_instead_of_repeating(self):
        from nip.brief import pieces
        phs = [{"id": 1, "stem": "v", "start": 10.0, "end": 20.0}, {"id": 2, "stem": "v", "start": 20.1, "end": 30.0},
               {"id": 3, "stem": "v", "start": 50.0, "end": 55.0}]
        self.assertEqual(pieces([1, 2, 3], phs, {"v": 60}),
                         [{"stem": "v", "start": 9.9, "end": 30.25}, {"stem": "v", "start": 49.9, "end": 55.25}])


class BriefViaClaudeCode(unittest.TestCase):
    def run_cli(self, stdout):
        from types import SimpleNamespace
        from nip import brief
        words, dur = script("NIP take one / Linderballs are great. They bounce")
        clips = [{"stem": "v", "words": words, "duration": dur, "edit": build_edit(words, dur)}]
        seen = {}

        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, env=kw["env"], input=kw["input"])
            return SimpleNamespace(stdout=stdout, stderr="")
        with mock.patch("nip.brief.shutil.which", return_value="/usr/local/bin/claude"), \
                mock.patch("nip.brief.subprocess.run", fake_run), \
                mock.patch.dict("os.environ", {"ANTHROPIC_API_KEY": "sk-test"}):
            return brief.ask("short one", clips), seen

    def test_uses_the_plan_not_the_api_key(self):
        out, seen = self.run_cli(json.dumps({"is_error": False, "result": "",
                                             "structured_output": {"title": "T", "note": "n", "phrases": [2]}}))
        self.assertEqual(seen["cmd"][:2], ["claude", "-p"])
        self.assertNotIn("ANTHROPIC_API_KEY", seen["env"])  # a key would move the bill to the API
        self.assertIn("[1] v", seen["input"])
        self.assertEqual(seen["cmd"][seen["cmd"].index("--model"):][:4], ["--model", "opus", "--effort", "medium"])
        self.assertEqual((out["title"], len(out["pieces"])), ("T", 1))

    def test_signed_out_gives_a_clear_message(self):
        with self.assertRaisesRegex(RuntimeError, "/login"):
            self.run_cli(json.dumps({"is_error": True, "result": "Failed to authenticate: OAuth session expired"}))


class EditNotes(unittest.TestCase):
    def clip(self):
        words, dur = script("NIP take one / hello there um I mean hi / NIP take two / good one")
        return {"duration": dur, "words": words, "edit": build_edit(words, dur)}

    def test_ops_are_checked_before_applying(self):
        import nip.cli as cli
        from nip.brief import apply_ops
        clip = self.clip()
        first, second = cli.decisions(clip)["takes"]
        blank = {"start": 0, "end": 0, "at": 0, "time": 0, "name": ""}
        ops = [{**blank, "note": 1, "op": "cut", "start": 3.0, "end": 3.5},
               {**blank, "note": 1, "op": "keep", "at": first["at"]},
               {**blank, "note": 2, "op": "rename", "at": second["at"], "name": "Intro"},
               {**blank, "note": 3, "op": "cut", "start": 5.0, "end": 4.0},        # backwards
               {**blank, "note": 3, "op": "drop", "at": 999.0},                    # no such take
               {**blank, "note": 3, "op": "restore_cut", "start": 1.234},          # no such cut
               {**blank, "note": 3, "op": "delete_everything"}]
        errors = apply_ops(clip, ops)
        self.assertEqual(sorted(errors), [3])
        self.assertEqual(len(errors[3]), 4)
        e = cli.decisions(clip)
        self.assertIn({"start": 3.0, "end": 3.5, "why": "cut by you"}, e["cuts"])
        self.assertEqual([(t["keep"], t["name"]) for t in e["takes"]], [(True, "Intro"), (True, "Intro")])

    def test_notes_use_sonnet_and_only_nearby_words_on_long_videos(self):
        from types import SimpleNamespace
        from nip import brief
        words, dur = script(" ".join(f"w{i} /" if i % 20 == 19 else f"w{i}" for i in range(700)))
        clip = {"duration": dur, "words": words, "edit": build_edit(words, dur)}
        calls = []

        def create(**kw):
            calls.append(kw)
            text = json.dumps({"ops": [], "notes": [{"id": 1, "done": False, "summary": "s"}]})
            return SimpleNamespace(stop_reason="end_turn", content=[SimpleNamespace(type="text", text=text)])
        client = SimpleNamespace(beta=SimpleNamespace(messages=SimpleNamespace(create=create)))
        brief.plan_notes(clip, [{"id": 1, "t": 100.0, "text": "cut"}], client=client)
        self.assertEqual((calls[0]["model"], calls[0]["output_config"]["effort"]), ("claude-sonnet-5-5", "low"))
        sent = [w for w in words if f' {w["w"]}\n' in calls[0]["messages"][0]["content"]]
        self.assertTrue(sent and all(70 <= w["start"] <= 130 for w in sent))  # 30s either side of the note

    def test_notes_prompt_has_words_takes_and_notes(self):
        from nip.brief import notes_prompt
        text = notes_prompt(self.clip(), [{"id": 7, "t": 4.2, "text": "cut the um"}])
        self.assertIn("[7] at 4.20s: cut the um", text)
        self.assertIn(" um\n", text)
        self.assertIn("take at=", text)


class CutEdits(unittest.TestCase):
    def test_restore_voice_cuts_and_add_your_own(self):
        _, e = summary("NIP take one / hello there / NIP take two / again")
        first = e["cuts"][0]["start"]
        out = apply_cut_edits(e, [first], [{"start": 5.0, "end": 6.0}])
        self.assertNotIn(first, [c["start"] for c in out["cuts"]])
        self.assertEqual(out["cuts"][-1], {"start": 5.0, "end": 6.0, "why": "cut by you"})
        self.assertEqual(len(out["cuts"]), len(e["cuts"]))


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

                # a video dropped on the page lands in the inbox; its twin in Downloads goes to the Trash
                put = lambda name, data: urllib.request.urlopen(urllib.request.Request(url + "/api/upload/" + name, data=data, method="PUT"))
                d2 = Path(d)
                with mock.patch.multiple(cli, INBOX=d2 / "inbox", TRASH=d2 / "bin"), mock.patch.object(view, "DOWNLOADS", d2 / "dl"):
                    (d2 / "dl").mkdir()
                    (d2 / "dl" / "c.mov").write_bytes(b"xyz")
                    put("c.mov", b"xyz")
                    put("c.mov", b"xy")
                    self.assertEqual(sorted(f.name for f in (d2 / "inbox").iterdir()), ["c 2.mov", "c.mov"])  # nothing overwritten, no .part left
                    self.assertEqual([f.name for f in (d2 / "bin").iterdir()], ["c.mov"])
                    for bad in ("notes.txt", "..%2Fx.mov"):
                        with self.assertRaises(urllib.error.HTTPError):
                            put(bad, b"x")
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
                self.assertEqual(json.loads(get("/api/project/p").read())["stage"], "inbox")  # opened, and a rejected edit: still Inbox
                self.assertEqual(json.loads(post("/api/override/p/v", {"at": take["at"], "keep": False}).read())["stage"], "processing")
                self.assertFalse(cli.decisions(cli.load(lib / "p" / "v.nip.json"))["takes"][-1]["keep"])
                cut = cli.decisions(cli.load(lib / "p" / "v.nip.json"))["cuts"][0]
                post("/api/cuts/p/v", {"action": "restore", "start": cut["start"]})
                post("/api/cuts/p/v", {"action": "cut", "start": 1.0, "end": 1.5})
                self.assertEqual([c["why"] for c in cli.decisions(cli.load(lib / "p" / "v.nip.json"))["cuts"]], ["cut by you"])
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/cuts/p/v", {"action": "cut", "start": 2.0, "end": 1.0})
                plan = {"ops": [{"note": 1, "op": "cut", "start": 1.0, "end": 1.2, "at": 0, "time": 0, "name": ""}],
                        "notes": [{"id": 1, "done": True, "summary": "Cut the pause."}]}
                with mock.patch("nip.brief.plan_notes", return_value=plan) as planner:
                    post("/api/notes/p/v", {"action": "add", "t": 1.5, "text": "cut the pause"})
                    data = json.loads(post("/api/notes/p/v", {"action": "apply"}).read())
                self.assertEqual(planner.call_args[0][1][0]["text"], "cut the pause")
                note = data["clips"][0]["edit_notes"][0]
                self.assertEqual((note["status"], note["result"]), ("applied", "Cut the pause."))
                self.assertTrue(data["clips"][0]["can_undo"])
                data = json.loads(post("/api/notes/p/v", {"action": "undo"}).read())
                self.assertEqual(data["clips"][0]["edit_notes"][0]["status"], "pending")
                self.assertNotIn({"start": 1.0, "end": 1.2, "why": "cut by you"}, data["clips"][0]["edit"]["cuts"])
                # a note with no time is about the whole video; NIP sees it marked general, with every word
                data = json.loads(post("/api/notes/p/v", {"action": "add", "t": None, "text": "tighten it"}).read())
                general = data["clips"][0]["edit_notes"][-1]
                self.assertEqual((general["t"], general["status"]), (None, "pending"))
                self.assertIn(f'[{general["id"]}] general: tighten it', view.brief.notes_prompt(cli.load(lib / "p" / "v.nip.json"), [general]))
                post("/api/notes/p/v", {"action": "delete", "id": general["id"]})
                # undo and redo step through this video's hand edits
                mine = lambda d: ([c["start"] for c in d["clips"][0]["edit"]["cuts"] if c["why"] == "cut by you"],
                                  [k["start"] for k in d["clips"][0]["edit"]["keeps"] if k["why"] == "kept by you"])
                base = mine(json.loads(get("/api/project/p").read()))
                post("/api/cuts/p/v", {"action": "cut", "start": 3.0, "end": 3.2})
                after = mine(json.loads(post("/api/cuts/p/v", {"action": "keep", "start": 3.5, "end": 3.9}).read()))
                self.assertEqual(mine(json.loads(post("/api/cuts/p/v", {"action": "undo"}).read())), (after[0], base[1]))
                self.assertEqual(mine(json.loads(post("/api/cuts/p/v", {"action": "undo"}).read())), base)
                d2 = json.loads(post("/api/cuts/p/v", {"action": "redo"}).read())
                self.assertEqual((mine(d2), d2["clips"][0]["redo"]), ((after[0], base[1]), 1))
                self.assertEqual(json.loads(post("/api/cuts/p/v", {"action": "uncut", "start": 3.0}).read())["clips"][0]["redo"], 0)  # a new edit ends redo
                for act in ("undo", "undo", "redo", "redo"):
                    post("/api/cuts/p/v", {"action": act})
                with self.assertRaises(urllib.error.HTTPError):  # nothing left to redo
                    post("/api/cuts/p/v", {"action": "redo"})
                # split: a point that divides the footage, neither cut nor keep; S again removes it; Reset clears it
                data = json.loads(post("/api/cuts/p/v", {"action": "split", "start": 2.5}).read())
                self.assertEqual(data["clips"][0]["edit"]["splits"], [{"t": 2.5, "why": "split by you"}])
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/cuts/p/v", {"action": "split", "start": 99})
                data = json.loads(post("/api/cuts/p/v", {"action": "unsplit", "start": 2.5}).read())
                self.assertEqual(data["clips"][0]["edit"]["splits"], [])
                post("/api/cuts/p/v", {"action": "split", "start": 2.5})
                post("/api/cuts/p/v", {"action": "reset"})
                clip = cli.load(lib / "p" / "v.nip.json")
                self.assertEqual((cli.decisions(clip)["cuts"], clip.get("overrides")), (clip["edit"]["cuts"], None))
                self.assertEqual(cli.decisions(clip)["splits"], [])
                # describe a clip; Reset leaves descriptions alone; the library card counts the tiers
                keeps = lambda d: [k for k in d["clips"][0]["edit"]["keeps"] if k["why"] == "kept by you"]
                self.assertEqual(keeps(json.loads(post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": ""}).read())), [])
                named = json.loads(post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": "the hello"}).read())
                self.assertEqual(keeps(named), [{"start": 1.3, "end": 4.4, "why": "kept by you"}])  # naming a clip keeps it, once
                self.assertEqual(len(keeps(json.loads(post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": "the hello"}).read()))), 1)
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": "x" * 201})
                post("/api/cuts/p/v", {"action": "reset"})
                card = json.loads(get("/api/projects").read())[0]
                self.assertEqual((card["tiers"], card["stage"]), ({"keep": 0, "unmarked": 1, "cut": 1}, "processing"))  # edited, so it left the Inbox
                self.assertIn("the hello", card["search"])
                # three tiers: mark the unmarked footage keep by hand; completing can settle the rest as cut
                post("/api/cuts/p/v", {"action": "cut", "start": 3.0, "end": 3.2})  # splits "hello" in two
                left = json.loads(get("/api/project/p").read())["clips"][0]["unmarked"]
                self.assertEqual(len(left), 2)
                post("/api/cuts/p/v", {"action": "keep", "start": left[0][0], "end": left[0][1]})
                self.assertEqual(json.loads(get("/api/project/p").read())["clips"][0]["unmarked"], left[1:])
                done = lambda **kw: mock.patch.object(cli, "finalize", **kw)

                def complete(body):  # it runs in the background: wait for the corner to say how it went
                    post("/api/stage/p", {"stage": "completed", **body})
                    for _ in range(200):
                        job = json.loads(get("/api/saves").read())[-1]
                        if job["state"] in ("done", "failed"):
                            return job
                        time.sleep(.05)
                    self.fail("still saving")
                with done(return_value={"clips": 1}) as fin, mock.patch("nip.view.library_answers", return_value=True):
                    self.assertEqual(complete({"unmarked": "cut"})["saved"], {"clips": 1})
                fin.assert_called_once()
                with done(side_effect=FileNotFoundError(2, "No such file", "ffmpeg")), mock.patch("nip.view.library_answers", return_value=True):
                    job = complete({})
                self.assertEqual((job["state"], job["error"]), ("failed", "Footage Flow can't find ffmpeg."))
                self.assertEqual(json.loads(get("/api/project/p").read())["stage"], "processing")  # failed, so it stays there
                with mock.patch("nip.view.library_answers", return_value=False):
                    self.assertIn("isn’t answering", complete({})["error"])
                post("/api/saves/p", {})
                self.assertEqual(json.loads(get("/api/saves").read()), [])
                self.assertEqual(json.loads(get("/api/project/p").read())["clips"][0]["unmarked"], [])
                # stages: opened, then completed; any of it can be deleted, to the Trash
                post("/api/cuts/p/v", {"action": "uncut", "start": left[1][0]})
                self.assertEqual(json.loads(post("/api/stage/p", {"stage": "processing"}).read())["stage"], "processing")
                with done(return_value={}), mock.patch("nip.view.library_answers", return_value=True):
                    complete({"unmarked": "keep"})
                self.assertEqual(json.loads(get("/api/project/p").read())["clips"][0]["unmarked"], [])
                # the old stages: saved clips make it Completed, otherwise it still has that step to do
                info = cli.load(lib / "p" / "project.json")
                cli.save(lib / "p" / "project.json", {**info, "stage": "exported"})
                self.assertEqual(json.loads(get("/api/project/p").read())["stage"], "processing")
                cli.save(lib / "p" / "project.json", {**info, "stage": "processed", "finalized": {"clips": 1}})
                self.assertEqual(json.loads(get("/api/project/p").read())["stage"], "completed")
                # Completed shows only what was kept; trimming it stays in Completed, unsaved until the new clips replace the old
                d3 = json.loads(get("/api/project/p").read())["clips"][0]
                kept = d3["segments"]
                self.assertTrue(kept and not any(s["gap"] for s in kept))
                self.assertTrue(all(any(s["a"] <= (w["start"] + w["end"]) / 2 < s["b"] for s in kept) for w in d3["words"]))
                self.assertLess(len(d3["words"]), len(cli.load(lib / "p" / "v.nip.json")["words"]))
                for act in ("restore", "uncut", "unkeep", "merge", "reset"):
                    with self.assertRaises(urllib.error.HTTPError) as cm:
                        post("/api/cuts/p/v", {"action": act, "start": 0, "end": 1})
                    self.assertIn("Processing", json.loads(cm.exception.read())["error"])
                post("/api/notes/p/v", {"action": "add", "t": None, "text": "later"})
                self.assertNotIn("unsaved", cli.load(lib / "p" / "project.json"))  # a note changes no clip
                trimmed = json.loads(post("/api/cuts/p/v", {"action": "cut", "start": kept[0]["a"], "end": kept[0]["a"] + .3}).read())
                self.assertEqual((trimmed["stage"], trimmed["unsaved"]), ("completed", True))
                self.assertTrue(json.loads(get("/api/projects").read())[0]["unsaved"])
                with done(return_value={}) as fin, mock.patch("nip.view.library_answers", return_value=True):
                    complete({"unmarked": "keep"})
                fin.assert_called_once()
                self.assertEqual(json.loads(get("/api/project/p").read())["stage"], "completed")  # never left Completed
                with done(side_effect=ValueError("no")), mock.patch("nip.view.library_answers", return_value=True):
                    complete({"unmarked": "keep"})
                info = cli.load(lib / "p" / "project.json")
                self.assertEqual((cli.stage_of(info), "finalized" in info, "unsaved" in info), ("processing", False, False))  # the panel says it's back there
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/stage/p", {"stage": "done"})
                self.assertEqual(json.loads(post("/api/stage/p", {"stage": "inbox"}).read())["stage"], "inbox")  # any stage, back too
                media = json.loads(get("/api/projects").read())[0]["media"]  # the card scrubs these
                self.assertEqual((media[0][:2], round(sum(m[2] for m in media), 2)), (["v.mov", 0], 4.4))
                cli.save(lib / "p" / "project.json", {**cli.load(lib / "p" / "project.json"), "my_description": "Pockets"})
                self.assertTrue(json.loads(get("/api/projects").read())[0]["named"])  # bundling onto it keeps this name, no prompt
                with mock.patch.object(cli, "TRASH", Path(d) / "trash"):
                    (Path(d) / "trash").mkdir()
                    post("/api/clear/p", {})
                    self.assertTrue((Path(d) / "trash" / "p" / "raw" / "v.mov").exists())
                self.assertFalse((lib / "p").exists())
                server.shutdown()


class Export(unittest.TestCase):
    def test_ingest_and_export_end_to_end(self):
        import nip.cli as cli
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            with mock.patch.multiple(cli, INBOX=d / "inbox", LIBRARY=d / "lib", EXPORTS=d / "out", UNREADABLE=d / "bad"):
                for p in (cli.INBOX, cli.LIBRARY, cli.EXPORTS):
                    p.mkdir()
                for name in ("a", "b"):
                    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=d=15:s=1440x2560:r=30",
                                    "-f", "lavfi", "-i", "sine=d=15", "-shortest", str(cli.INBOX / f"{name}.mp4")],
                                   check=True)
                (cli.INBOX / "airdrop.mov").write_bytes(b"bplist00")  # a placeholder, not a video: set aside, not blocking
                words, _ = script("NIP this is demo day / Take one / bad / Take two / good")
                with mock.patch.object(cli, "transcribe", return_value=(words, [])), \
                        mock.patch.object(cli, "settled", return_value=True):
                    cli.ingest()
                projs = sorted(cli.LIBRARY.iterdir())  # each loose video is its own project
                self.assertEqual([p.name for p in (d / "bad").iterdir()], ["airdrop.mov"])
                self.assertEqual([p.name[11:] for p in projs], ["a_demo-day", "b_demo-day"])
                with self.assertRaises(ValueError):
                    cli.bundle(projs[:1], "Demo day")
                for p, when in zip(projs, ("2026-11-02T10:00:00Z", "2026-11-02T09:00:00Z")):  # b was filmed first
                    clip = cli.load(next(p.glob("*.nip.json")))
                    cli.save(next(p.glob("*.nip.json")), {**clip, "created": when})
                proj = cli.bundle(projs, "Demo day")
                self.assertEqual([p.name for p in cli.LIBRARY.iterdir()], [proj.name])  # the old folders are gone
                self.assertTrue(proj.name.endswith("_demo-day"))
                info = json.loads((proj / "project.json").read_text())
                self.assertEqual((info["clips"], info["my_description"], info["stage"]), (["b", "a"], "Demo day", "inbox"))
                # finalize: nothing unmarked allowed; then one lossless file and one info file per kept clip
                with mock.patch.multiple(cli, FINAL=d / "final", HANDOFFS=d / "handoffs", TRASH=d / "bin", INDEX=d / "index.json", HOME=d):
                    with self.assertRaises(ValueError):
                        cli.finalize(proj)
                    for stem in ("a", "b"):
                        f = proj / f"{stem}.nip.json"
                        c = cli.load(f)
                        for x, y in unmarked(cli.decisions(c)):
                            cli.add_cut(c, x, y, "my_keeps")
                        cli.set_label(c, 9.4, 15.0, "the good/bit" if stem == "a" else "")
                        cli.save(f, c)
                    r = cli.finalize(proj)
                    folder = f"Demo day ({proj.name[:10]})"
                    got = sorted(f.name for f in (d / "final" / "Unused" / folder).iterdir())
                    self.assertEqual((r["clips"], got), (2, ["01 b.mp4", "02 the goodbit.mp4", "_info"]))  # editors just see the clips
                    info = d / "final" / "Unused" / folder / "_info"
                    self.assertEqual(sorted(f.name for f in info.iterdir() if "goodbit" in f.name),
                                     ["02 the goodbit.jpg", "02 the goodbit.json", "02 the goodbit.preview.mp4"])
                    self.assertFalse((d / ".finalizing" / proj.name).exists())
                    side = cli.load(info / "02 the goodbit.json")
                    self.assertEqual((side["name"], side["source"], side["bundle"]), ("the good/bit", "a.mp4", "Demo day"))
                    self.assertEqual(side["words"][0]["start"], round(words[-1]["start"] - side["source_start"], 2))  # times from the clip's start
                    self.assertGreater(cli.probe(d / "final" / "Unused" / folder / "02 the goodbit.mp4")[0], 5)
                    self.assertTrue((proj / "raw" / "a.mp4").exists())  # the original stays
                    self.assertEqual((cli.load(proj / "project.json")["finalized"]["clips"], cli.load(proj / "project.json")["stage"]), (2, "completed"))
                    cli.save(info / "02 the goodbit.json", {**cli.load(info / "02 the goodbit.json"), "tags": ["curve throw"]})
                    cli.finalize(proj)  # redoing it replaces the result; the old one goes to the Trash
                    self.assertEqual(cli.load(info / "02 the goodbit.json")["tags"], ["curve throw"])  # tags outlive it
                    # trimmed after completing: the shorter clip replaces the old one, and undo can't reach back past it
                    long = cli.probe(d / "final" / "Unused" / folder / "02 the goodbit.mp4")[0]
                    c = cli.load(proj / "a.nip.json")
                    cli.remember(c)
                    cli.add_cut(c, side["source_start"], side["source_start"] + 2)
                    cli.save(proj / "a.nip.json", c)
                    cli.finalize(proj)
                    self.assertLess(cli.probe(d / "final" / "Unused" / folder / "02 the goodbit.mp4")[0], long - 1)
                    self.assertEqual(sorted(f.name for f in (d / "final" / "Unused" / folder).iterdir()), got)
                    self.assertEqual(cli.load(proj / "a.nip.json").get("undo"), None)
                    # the Library: listed, served, tagged, and only from inside the finalized folder
                    import threading
                    import urllib.error
                    import urllib.parse
                    import urllib.request
                    import nip.view as view
                    server = view.ThreadingHTTPServer(("127.0.0.1", 0), view.Handler)
                    threading.Thread(target=server.serve_forever, daemon=True).start()
                    base = f"http://127.0.0.1:{server.server_port}"
                    get = lambda u: urllib.request.urlopen(base + u)
                    listed = json.loads(get("/api/clips").read())
                    self.assertEqual([(c["name"], c["file"]) for c in listed], [("", "01 b.mp4"), ("the good/bit", "02 the goodbit.mp4")])
                    self.assertNotIn("words", listed[0])
                    q = urllib.parse.quote
                    self.assertGreater(len(get(f"/final/{q(folder)}/{q('02 the goodbit.jpg')}").read()), 500)
                    self.assertGreater(len(get(f"/final/{q(folder)}/{q('02 the goodbit.preview.mp4')}").read()), 500)
                    post = lambda u, b: urllib.request.urlopen(urllib.request.Request(base + u, data=json.dumps(b).encode()))
                    self.assertEqual(json.loads(post(f"/api/clip/{q(folder)}/{q('02 the goodbit')}", {"tags": [" Curve ", ""]}).read())["tags"], ["curve"])
                    # picked clips: copied in the order picked, into a new folder, never over an old one
                    picks = {"name": "My/ video", "clips": [{"folder": folder, "file": "02 the goodbit.mp4"}, {"folder": folder, "file": "01 b.mp4"}]}
                    self.assertEqual(json.loads(post("/api/handoff", picks).read())["folder"], "My video")
                    self.assertEqual(json.loads(post("/api/handoff", picks).read())["folder"], "My video 2")
                    self.assertEqual(sorted(f.name for f in (d / "handoffs" / "My video").iterdir()), ["01 the goodbit.mp4", "02 b.mp4"])
                    self.assertEqual(sorted(f.name for f in (d / "handoffs").iterdir()), ["My video", "My video 2"])
                    for bad in ({"name": "x", "clips": []}, {"name": "/", "clips": picks["clips"]}, {"name": "x", "clips": "y"},
                                {"name": "x", "clips": [{"folder": folder, "file": "nope.mp4"}]}):
                        with self.assertRaises(urllib.error.HTTPError):
                            post("/api/handoff", bad)
                    # used: the folder moves, by the app or by hand, and clips still load from it
                    post(f"/api/used/{proj.name}", {"used": True})
                    self.assertTrue((d / "final" / "Used" / folder).is_dir())
                    self.assertTrue(all(c["used"] for c in json.loads(get("/api/clips").read())))
                    self.assertGreater(len(get(f"/final/{q(folder)}/{q('02 the goodbit.jpg')}").read()), 500)
                    (d / "final" / "Used" / folder).rename(d / "final" / "Unused" / folder)
                    self.assertFalse(any(c["used"] for c in json.loads(get("/api/clips").read())))
                    post(f"/api/used/{proj.name}", {"used": True})
                    with mock.patch.object(cli, "FINAL", d / "unplugged"):  # the library out of reach: search still works
                        self.assertEqual(len(json.loads(get("/api/clips").read())), 2)
                    for bad in (lambda: get(f"/final/{q(folder)}/..%2Fnope.mp4"), lambda: post("/api/clip/x/y", {"tags": []}),
                                lambda: post(f"/api/clip/{q(folder)}/{q('01 b')}", {"tags": "x"})):
                        with self.assertRaises(urllib.error.HTTPError):
                            bad()
                    server.shutdown()
                    self.assertEqual(sorted(f.name for f in (d / "bin").iterdir()), [folder, f"{folder} 2"])
                    # renamed, it moves to a folder of the new name with its tags; another bundle's folder is never taken
                    (d / "final" / "Unused" / "Reel (2026-01-01)").mkdir()
                    with mock.patch.object(cli, "library_folder", return_value="Reel (2026-01-01) 2"):
                        cli.finalize(proj)
                    self.assertFalse((d / "final" / "Used" / folder).exists())  # completing it again stays on its shelf
                    self.assertEqual(cli.load(d / "final" / "Used" / "Reel (2026-01-01) 2" / "_info" / "02 the goodbit.json")["tags"], ["curve"])
                    self.assertEqual(cli.library_folder(Path("2026-01-01_x"), {"my_description": "Reel."}), "Reel (2026-01-01) 3")
                    self.assertEqual(cli.library_folder(Path("2026-01-01_x"), {"my_description": "Reel", "library_folder": "Reel (2026-01-01)"}), "Reel (2026-01-01)")
                    self.assertEqual(cli.library_folder(Path("2026-01-02_x"), {"description": None}), "2026-01-02_x")
                    self.assertFalse(list((d / "final").glob(".*")))
                self.assertFalse(any(cli.INBOX.iterdir()))
                story = {"title": "Best bits", "pieces": [{"stem": "b", "start": 1.0, "end": 2.0},
                                                          {"stem": "a", "start": 3.0, "end": 4.5}]}
                final = cli.export_story(proj, story)
                dur, _ = cli.probe(final)
                self.assertAlmostEqual(dur, 2.5, delta=0.2)  # two videos joined in Claude's order
                size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "csv=p=0", str(final)],
                                      capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(size, "1080,1920")  # portrait source scaled to the social size
                self.assertEqual(sorted(f.name for f in (cli.EXPORTS / proj.name).glob(".*")), [])


class Robustness(unittest.TestCase):
    def serve(self, d):
        import threading
        import nip.view as view
        patch = mock.patch.multiple(cli, LIBRARY=d / "lib", INBOX=d / "inbox", FINAL=d / "final", HANDOFFS=d / "handoffs", INDEX=d / "index.json", TRASH=d / "bin", HOME=d)
        patch.start()
        self.addCleanup(patch.stop)
        server = view.ThreadingHTTPServer(("127.0.0.1", 0), view.Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.shutdown)
        return f"http://127.0.0.1:{server.server_port}", view

    def project(self, d, name="p", words=()):
        p = d / "lib" / name
        (p / "raw").mkdir(parents=True)
        (p / "raw" / "v.mov").write_bytes(b"x")
        cli.save(p / "project.json", {"description": "A video", "tags": [], "clips": ["v"], "stage": "processing"})
        cli.save(p / "v.nip.json", {"source": "v.mov", "duration": 4.0, "words": list(words), "edit": build_edit(list(words), 4.0)})
        return p

    def test_only_this_page_can_post(self):
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "lib").mkdir()
            base, _ = self.serve(d)
            self.project(d)
            ask = lambda **h: urllib.request.urlopen(urllib.request.Request(base + "/api/clear/p", data=b"{}", headers=h))
            for bad in ({"Origin": "https://evil.example"}, {"Origin": "null"}, {"Host": "evil.example"}):
                with self.assertRaises(urllib.error.HTTPError):
                    ask(**bad)
            self.assertTrue((d / "lib" / "p").exists())
            (d / "bin").mkdir()
            self.assertEqual(ask(Origin=base).status, 200)  # the page itself
            self.assertFalse((d / "lib" / "p").exists())

    def test_saving_footage_cant_be_changed(self):
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "lib").mkdir()
            base, view = self.serve(d)
            self.project(d)
            view.SAVES[:] = [{"project": "p", "state": "saving"}]
            self.addCleanup(view.SAVES.clear)
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(urllib.request.Request(base + "/api/cuts/p/v", data=json.dumps({"action": "cut", "start": 1, "end": 2}).encode()))
            self.assertIn("being saved", json.loads(cm.exception.read())["error"])

    def test_one_bad_project_or_clip_hides_nothing_else(self):
        import urllib.request
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "lib").mkdir()
            base, view = self.serve(d)
            self.project(d)
            self.project(d, "broken")
            (d / "lib" / "broken" / "v.nip.json").write_text("{half a fi")
            self.assertEqual([c["name"] for c in json.load(urllib.request.urlopen(base + "/api/projects"))], ["p"])
            for name, text in (("good", {"project": "p", "source": "v.mov", "tags": [], "words": []}), ("bad", None)):
                f = d / "final" / "Unused" / "b" / "_info" / f"{name}.json"
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_text(json.dumps(text) if text else "{half")
            view.scan_library()
            self.assertEqual([c["stem"] for c in cli.load(cli.INDEX).values()], ["good"])

    def test_saves_are_whole_and_lost_footage_returns_to_the_inbox(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            with mock.patch.multiple(cli, LIBRARY=d / "lib", INBOX=d / "inbox"):
                f = d / "a.json"
                cli.save(f, {"a": 1})
                with mock.patch("os.replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
                    cli.save(f, {"a": 2})
                self.assertEqual(cli.load(f), {"a": 1})  # a failed save leaves the last good one
                (d / "lib" / "stopped" / "raw").mkdir(parents=True)
                (d / "lib" / "stopped" / "raw" / "v.MOV").write_bytes(b"x")
                (d / "lib" / "stopped" / "v.nip.json").write_text("{}")
                (d / "lib" / "fine" / "raw").mkdir(parents=True)
                (d / "lib" / "fine" / "project.json").write_text("{}")
                cli.rescue()
                self.assertTrue((d / "inbox" / "v.MOV").exists())
                self.assertEqual([p.name for p in (d / "lib").iterdir()], ["fine"])

    def test_nip_on_the_home_screen_is_checked(self):
        projects = [{"name": "p", "description": "Printers", "stage": "completed", "filmed": 60, "kept": 30, "tags": ["farm"], "said": "my print farm"}]
        clips = [{"project": "p", "stem": "01 a", "name": "Throw", "bundle": "Printers", "duration": 3.0, "tags": [], "text": "watch this", "used": False}]
        answer = {"reply": "Found it.", "clips": [0, 7], "projects": ["p", "nope"], "actions": [
            {"action": "pick", "project": "", "clips": [0], "text": ""}, {"action": "mark_used", "project": "p", "clips": [], "text": ""},
            {"action": "mark_used", "project": "nope", "clips": [], "text": ""}, {"action": "delete", "project": "p", "clips": [], "text": ""},
            {"action": "move", "project": "p", "clips": [], "text": "completed"}, {"action": "tag", "project": "", "clips": [9], "text": "x"}]}
        with mock.patch.object(brief, "claude_json", return_value=answer) as ask:
            r = brief.assist("find my throw", [{"role": "you", "text": "hi"}], "Library", projects, clips)
        sent = ask.call_args.args[0]
        self.assertIn('[0] title="Throw"', sent)
        self.assertIn('said="my print farm"', sent)
        self.assertIn("you: hi", sent)
        self.assertEqual((r["clips"], r["projects"]), (["p/01 a"], ["p"]))  # only what exists
        self.assertEqual([a["action"] for a in r["actions"]], ["pick", "mark_used"])  # no delete, no skipping Mark Complete's rules
        self.assertEqual(r["actions"][0]["clips"], ["p/01 a"])
        self.assertIn("4 of that couldn't be done", r["reply"])

    def test_nip_route_checks_the_message(self):
        import urllib.error
        import urllib.request
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            (d / "lib").mkdir()
            base, _ = self.serve(d)
            self.project(d, words=[{"w": "hello", "start": 1.0, "end": 1.5}])
            post = lambda b: urllib.request.urlopen(urllib.request.Request(base + "/api/nip", data=json.dumps(b).encode()))
            for bad in ({"message": ""}, {"message": "x" * 2001}, {"message": "hi", "history": "no"}):
                with self.assertRaises(urllib.error.HTTPError):
                    post(bad)
            said = {"reply": "Hi.", "clips": [], "projects": [], "actions": []}
            with mock.patch.object(brief, "claude_json", return_value=said) as ask:
                self.assertEqual(json.load(post({"message": "hi", "view": "Inbox"}))["reply"], "Hi.")
            self.assertIn("hello", ask.call_args.args[0])

    def test_a_failed_swap_keeps_the_old_clips(self):
        with tempfile.TemporaryDirectory() as t:
            d = Path(t)
            with mock.patch.multiple(cli, LIBRARY=d / "lib", FINAL=d / "final", HOME=d):
                proj = self.project(d)
                c = cli.load(proj / "v.nip.json")
                cli.add_cut(c, 0.0, 4.0, "my_keeps")
                cli.save(proj / "v.nip.json", c)
                old = d / "final" / "Unused" / "p"
                old.mkdir(parents=True)
                (old / "01 old.mov").write_bytes(b"old")
                real = Path.rename

                def rename(self, target):
                    if self.name.endswith(".building"):
                        raise OSError("Drive said no")
                    return real(self, target)
                fake = lambda src, a, b, out: out.write_bytes(b"new")
                make = lambda clip, name: None
                with mock.patch.object(Path, "rename", rename), mock.patch.object(cli, "copy_clip", fake), mock.patch.object(cli, "preview", make), self.assertRaises(OSError):
                    cli.finalize(proj)
                self.assertTrue((old / "01 old.mov").exists())


class Page(unittest.TestCase):
    """The tests can't run the page, so a slip in its script blanks the app. Catch the two kinds that have."""
    page = (Path(__file__).parent.parent / "nip" / "viewer.html").read_text()

    def test_every_element_the_script_uses_exists(self):
        import re
        self.assertEqual(sorted(set(re.findall(r"\$\('#([\w-]+)'\)", self.page)) - set(re.findall(r'id="([\w-]+)"', self.page))), [])

    @unittest.skipUnless(shutil.which("osascript"), "needs macOS's JavaScript")
    def test_the_script_parses(self):
        src = self.page[self.page.rindex("<script>") + 8:self.page.rindex("</script>")]
        with tempfile.TemporaryDirectory() as t:
            f = Path(t) / "check.js"
            f.write_text(f"new Function({json.dumps(src)}); 'ok'")
            r = subprocess.run(["osascript", "-l", "JavaScript", str(f)], capture_output=True, text=True)
        self.assertEqual((r.stdout.strip(), r.stderr), ("ok", ""))


if __name__ == "__main__":
    unittest.main()
