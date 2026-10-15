"""Grounding checks for evidence, script, and the rendered video."""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from devvideo.clone import CloneError, acquire, parse_github
from devvideo.evidence import extract
from devvideo.pipeline import run
from devvideo.script import ScriptError, compose_grounded, validate_storyboard
from devvideo.tts import fit_to_duration, probe_duration, speakable


SAMPLE = ROOT / "examples" / "frame_index"


class CloneTests(unittest.TestCase):
    def test_parse_github_url_and_short_form(self):
        self.assertEqual(
            parse_github("https://github.com/openai/CLIP"),
            ("https://github.com/openai/CLIP.git", "openai-CLIP"),
        )
        self.assertEqual(parse_github("openai/CLIP.git"), ("https://github.com/openai/CLIP.git", "openai-CLIP"))
        self.assertIsNone(parse_github("https://example.com/openai/CLIP"))

    def test_local_checkout_is_not_cloned(self):
        with tempfile.TemporaryDirectory() as tmp:
            ref = acquire(str(SAMPLE), Path(tmp))
        self.assertEqual(ref.path, SAMPLE.resolve())
        self.assertFalse(ref.cloned)

    def test_missing_path_is_rejected(self):
        with self.assertRaises(CloneError):
            acquire("not-a-repo", Path("."))


class EvidenceTests(unittest.TestCase):
    def test_sample_repo_retrieves_readme_api_and_results(self):
        cards = extract(SAMPLE)
        symbols = {card.symbol for card in cards}
        self.assertIn("index_video", symbols)
        self.assertIn("search", symbols)
        blob = "\n".join(card.text for card in cards)
        self.assertIn("12 timestamps", blob)
        self.assertTrue(any(card.kind == "example" and "index_video" in card.text for card in cards))
        self.assertTrue(any(card.kind == "result" and "12 timestamps" in card.text for card in cards))
        ids = [card.id for card in cards]
        self.assertEqual(ids, [f"E{i}" for i in range(1, len(cards) + 1)])


class ScriptTests(unittest.TestCase):
    def setUp(self):
        self.cards = extract(SAMPLE)
        self.by_id = {card.id: card for card in self.cards}

    def test_grounded_script_cites_retrieved_evidence_only(self):
        board = compose_grounded(self.cards, "frame_index", max_seconds=58)
        self.assertEqual([beat.section for beat in board.beats], ["title", "problem", "api", "how", "results"])
        self.assertLessEqual(sum(beat.duration_s for beat in board.beats), 58)
        api = next(beat for beat in board.beats if beat.section == "api")
        self.assertIn("index_video", api.narration)
        self.assertIn("search", api.narration)
        usage = next(beat for beat in board.beats if beat.section == "how")
        self.assertEqual(usage.headline, "How to use it")
        cited = "\n".join(self.by_id[item].text for item in usage.evidence_ids)
        self.assertIn("index_video(", cited)
        self.assertIn("search(", cited)
        results = next(beat for beat in board.beats if beat.section == "results")
        self.assertIn("12", results.narration)
        for beat in board.beats:
            for evidence_id in beat.evidence_ids:
                self.assertIn(evidence_id, self.by_id)

    def test_invented_metric_is_rejected(self):
        board = compose_grounded(self.cards, "frame_index")
        board.beats[-1].narration = "The index reaches 99.9% recall@5 on the smoke set."
        errors = validate_storyboard(board, self.by_id)
        self.assertTrue(any("99.9%" in error for error in errors))

    def test_unknown_evidence_id_is_rejected(self):
        board = compose_grounded(self.cards, "frame_index")
        board.beats[0].evidence_ids = ["E999"]
        errors = validate_storyboard(board, self.by_id)
        self.assertTrue(any("E999" in error for error in errors))


class RenderTests(unittest.TestCase):
    def test_pipeline_writes_a_video_under_one_minute(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            result = run(str(SAMPLE), out, llm="off", tts="off", max_seconds=58)
            self.assertTrue(result["video"].is_file())
            self.assertLessEqual(result["duration_s"], 58)
            script = json.loads((out / "script.json").read_text(encoding="utf-8"))
            self.assertEqual(script["generator"], "grounded")
            probe = subprocess.run(
                [
                    "ffprobe",
                    "-v",
                    "error",
                    "-show_entries",
                    "format=duration",
                    "-of",
                    "csv=p=0",
                    str(result["video"]),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
            duration = float(probe.stdout.strip())
            self.assertGreater(duration, 20)
            self.assertLess(duration, 60)
            frame = next((out / "frames").glob("beat-01-*.png"))
            self.assertGreater(frame.stat().st_size, 5_000)


class SpeechTests(unittest.TestCase):
    def test_speakable_softens_paths_and_units(self):
        spoken = speakable(
            "Use Engine. Signatures are retrieved from jev_visual/engine.py. 678.6 total ms, 5.89 decisions/s."
        )
        self.assertIn("jev visual engine dot py", spoken)
        self.assertIn("milliseconds", spoken)
        self.assertIn("per second", spoken)
        self.assertNotIn("_", spoken)
        self.assertNotIn(".py", spoken)

    def test_short_clip_is_padded_to_the_beat(self):
        if shutil.which("ffmpeg") is None:
            self.skipTest("ffmpeg is not installed")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            tone = folder / "tone.wav"
            fitted = folder / "fitted.wav"
            subprocess.run(
                [
                    "ffmpeg",
                    "-y",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "sine=frequency=440:duration=0.35",
                    str(tone),
                ],
                check=True,
            )
            fit_to_duration(tone, fitted, 1.2)
            self.assertAlmostEqual(probe_duration(fitted), 1.2, delta=0.08)


if __name__ == "__main__":
    unittest.main()
