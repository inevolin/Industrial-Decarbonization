import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


MODULE_PATH = Path(__file__).resolve().parents[1] / "daily_research.py"
SPEC = importlib.util.spec_from_file_location("daily_research", MODULE_PATH)
daily_research = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = daily_research
SPEC.loader.exec_module(daily_research)


class DailyResearchTests(unittest.TestCase):
    def test_is_allowed_repo_path_rejects_unsafe_values(self):
        self.assertTrue(daily_research.is_allowed_repo_path("README.md"))
        self.assertTrue(daily_research.is_allowed_repo_path("Articles/example.md"))
        self.assertFalse(daily_research.is_allowed_repo_path("../README.md"))
        self.assertFalse(daily_research.is_allowed_repo_path(".github/workflows/test.yml"))
        self.assertFalse(daily_research.is_allowed_repo_path("/tmp/test.md"))
        self.assertFalse(daily_research.is_allowed_repo_path("notes.txt"))

    def test_normalize_response_rejects_disallowed_files(self):
        with self.assertRaisesRegex(ValueError, "disallowed change path"):
            daily_research.normalize_response(
                {
                    "decision": "propose_pr",
                    "summary": "summary",
                    "pr_title": "title",
                    "pr_body": "body",
                    "changes": [
                        {
                            "path": ".github/workflows/pwn.yml",
                            "content": "bad",
                        }
                    ],
                    "sources": [],
                }
            )

    def test_dedupe_candidates_keeps_first_unique_item(self):
        items = [
            daily_research.CandidateItem("Title", "https://example.com/a", "A", "Today"),
            daily_research.CandidateItem("Title", "https://example.com/a", "A", "Today"),
            daily_research.CandidateItem("Other", "https://example.com/b", "B", "Today"),
        ]
        deduped = daily_research.dedupe_candidates(items)
        self.assertEqual(2, len(deduped))
        self.assertEqual("https://example.com/a", deduped[0].url)
        self.assertEqual("https://example.com/b", deduped[1].url)

    def test_apply_changes_writes_allowed_file(self):
        original_root = daily_research.REPO_ROOT
        with tempfile.TemporaryDirectory() as tmpdir:
            daily_research.REPO_ROOT = Path(tmpdir)
            daily_research.apply_changes(
                [{"path": "Articles/test-entry.md", "content": "# Test\n"}]
            )
            written = Path(tmpdir) / "Articles" / "test-entry.md"
            self.assertEqual("# Test\n", written.read_text(encoding="utf-8"))
        daily_research.REPO_ROOT = original_root


if __name__ == "__main__":
    unittest.main()
