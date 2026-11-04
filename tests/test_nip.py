import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from nip.commands import apply_cut_edits, apply_overrides, build_edit, kept_ranges, unmarked


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
        self.assertEqual((e["markers"][2]["t"], e["markers"][2]["end"]), (words[14]["end"], words[17]["start"]))
        self.assertEqual(e["markers"][3]["t"], words[-4]["end"])  # "NIP keep" starts after the previous NIP command
        self.assertEqual(e["pending"], [])
        # three tiers: what's neither cut nor marked keep is unmarked
        left = unmarked(apply_cut_edits(e, [], [], [{"start": 0.0, "end": words[1]["end"]}]))
        said = [[w["w"] for w in words if a <= w["start"] and w["end"] <= b] for a, b in left]
        self.assertEqual([x for x in said if x], [["good", "stuff"]])  # the rest is the test's silent pauses

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
                post("/api/cuts/p/v", {"action": "reset"})
                clip = cli.load(lib / "p" / "v.nip.json")
                self.assertEqual((cli.decisions(clip)["cuts"], clip.get("overrides")), (clip["edit"]["cuts"], None))
                # describe a clip; Reset leaves descriptions alone; the library card counts the tiers
                post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": "the hello"})
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/cuts/p/v", {"action": "label", "start": 1.3, "end": 4.4, "text": "x" * 201})
                post("/api/cuts/p/v", {"action": "reset"})
                card = json.loads(get("/api/projects").read())[0]
                self.assertEqual((card["tiers"], card["stage"]), ({"keep": 0, "unmarked": 1, "cut": 1}, "inbox"))
                self.assertIn("the hello", card["search"])
                # three tiers: mark the unmarked footage keep by hand; export can settle the rest as cut
                post("/api/cuts/p/v", {"action": "cut", "start": 3.0, "end": 3.2})  # splits "hello" in two
                left = json.loads(get("/api/project/p").read())["clips"][0]["unmarked"]
                self.assertEqual(len(left), 2)
                post("/api/cuts/p/v", {"action": "keep", "start": left[0][0], "end": left[0][1]})
                self.assertEqual(json.loads(get("/api/project/p").read())["clips"][0]["unmarked"], left[1:])
                with mock.patch.object(cli, "export") as export:
                    post("/api/export/p", {"unmarked": "cut"})
                export.assert_called_once()
                with mock.patch.object(cli, "export", side_effect=FileNotFoundError(2, "No such file", "ffmpeg")), \
                        self.assertRaises(urllib.error.HTTPError) as cm:
                    post("/api/export/p", {})
                self.assertEqual((cm.exception.code, json.loads(cm.exception.read())["error"]),
                                 (500, "Footage Flow can't find ffmpeg; is it installed?"))  # not a bare "not found"
                self.assertEqual(json.loads(get("/api/project/p").read())["clips"][0]["unmarked"], [])
                # stages: opened, then marked complete; only inbox or exported footage can be deleted, to the Trash
                post("/api/cuts/p/v", {"action": "uncut", "start": left[1][0]})
                self.assertEqual(json.loads(post("/api/stage/p", {"stage": "processing"}).read())["stage"], "processing")
                data = json.loads(post("/api/stage/p", {"stage": "processed", "unmarked": "keep"}).read())
                self.assertEqual((data["stage"], data["clips"][0]["unmarked"]), ("processed", []))
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/stage/p", {"stage": "exported"})
                with self.assertRaises(urllib.error.HTTPError):
                    post("/api/clear/p", {})
                self.assertEqual(json.loads(get("/api/projects").read())[0]["media"], [["v.mov", 4.4]])  # the card scrubs these
                cli.set_stage(lib / "p", "inbox")
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
                projs = sorted(cli.LIBRARY.iterdir())  # each loose video is its own project
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
                cli.export(proj, combined=True)
                outs = sorted(p.name for p in (cli.EXPORTS / proj.name).iterdir())
                self.assertEqual(outs, ["01_b_s1t2.mp4", "02_a_s1t2.mp4", f"{proj.name}.mp4"])
                self.assertEqual(json.loads((proj / "project.json").read_text())["stage"], "exported")
                dur, _ = cli.probe(cli.EXPORTS / proj.name / f"{proj.name}.mp4")
                self.assertGreater(dur, 1)
                size = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                                       "stream=width,height", "-of", "csv=p=0", str(cli.EXPORTS / proj.name / outs[0])],
                                      capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(size, "1080,1920")  # portrait source scaled to the social size
                self.assertFalse(any(cli.INBOX.iterdir()))
                story = {"title": "Best bits", "pieces": [{"stem": "b", "start": 1.0, "end": 2.0},
                                                          {"stem": "a", "start": 3.0, "end": 4.5}]}
                dur, _ = cli.probe(cli.export_story(proj, story))
                self.assertAlmostEqual(dur, 2.5, delta=0.2)  # two videos joined in Claude's order
                self.assertEqual(sorted(f.name for f in (cli.EXPORTS / proj.name).glob(".*")), [])


if __name__ == "__main__":
    unittest.main()
