from __future__ import annotations

import pytest

from graphnav.config import Config, ContextConfig
from graphnav.graph_query import RankedFile
from graphnav.runner import _read_file, build_prompt


class TestReadFile:
    def test_reads_file_content(self, tmp_path):
        f = tmp_path / "hello.py"
        f.write_text("print('hello')\n")
        assert _read_file(str(f), 1000) == "print('hello')\n"

    def test_returns_placeholder_for_missing_file(self, tmp_path):
        result = _read_file(str(tmp_path / "ghost.py"), 1000)
        assert result == "[FILE NOT FOUND ON DISK]"

    def test_truncates_at_max_chars(self, tmp_path):
        f = tmp_path / "big.py"
        f.write_text("a" * 200)
        result = _read_file(str(f), 100)
        assert len(result) <= 100 + len("\n[... truncated ...]")
        assert "[... truncated ...]" in result

    def test_truncates_at_newline_boundary(self, tmp_path):
        f = tmp_path / "lines.py"
        f.write_text("line1\nline2\nline3\nline4\n")
        result = _read_file(str(f), 12)
        assert "[... truncated ...]" in result
        assert result.startswith("line1\nline2")

    def test_no_truncation_when_under_limit(self, tmp_path):
        f = tmp_path / "small.py"
        content = "x = 1\n"
        f.write_text(content)
        result = _read_file(str(f), 10000)
        assert result == content
        assert "[... truncated ...]" not in result

    def test_empty_file(self, tmp_path):
        f = tmp_path / "empty.py"
        f.write_text("")
        assert _read_file(str(f), 1000) == ""

    def test_unicode_replacement_on_bad_bytes(self, tmp_path):
        f = tmp_path / "bad.py"
        f.write_bytes(b"good\xff\xfe bad")
        result = _read_file(str(f), 1000)
        assert "good" in result


class TestBuildPrompt:
    def _cfg(self, show_scores=False, max_file_chars=8000) -> Config:
        cfg = Config()
        cfg.context = ContextConfig(show_scores=show_scores, max_file_chars=max_file_chars)
        return cfg

    def test_no_ranked_files_prompt_only(self, tmp_path):
        cfg = self._cfg()
        result = build_prompt("my task", [], cfg, str(tmp_path))
        assert "USER TASK:\nmy task" in result
        assert "FILE" not in result

    def test_ranked_file_content_injected(self, tmp_path):
        src = tmp_path / "foo.py"
        src.write_text("x = 1\n")
        ranked = [RankedFile(source_file="foo.py", score=1.5)]
        cfg = self._cfg()
        result = build_prompt("do something", ranked, cfg, str(tmp_path))
        assert "=== FILE: foo.py ===" in result
        assert "x = 1" in result
        assert "USER TASK:\ndo something" in result

    def test_score_shown_when_enabled(self, tmp_path):
        src = tmp_path / "bar.py"
        src.write_text("y = 2\n")
        ranked = [RankedFile(source_file="bar.py", score=3.14)]
        cfg = self._cfg(show_scores=True)
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        assert "3.14" in result

    def test_score_hidden_when_disabled(self, tmp_path):
        src = tmp_path / "bar.py"
        src.write_text("y = 2\n")
        ranked = [RankedFile(source_file="bar.py", score=3.14)]
        cfg = self._cfg(show_scores=False)
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        assert "3.14" not in result

    def test_multiple_files_all_injected(self, tmp_path):
        for name in ("a.py", "b.py", "c.py"):
            (tmp_path / name).write_text(f"# {name}\n")
        ranked = [RankedFile(f, 1.0) for f in ("a.py", "b.py", "c.py")]
        cfg = self._cfg()
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        for name in ("a.py", "b.py", "c.py"):
            assert f"=== FILE: {name} ===" in result

    def test_context_header_and_footer_present(self, tmp_path):
        (tmp_path / "f.py").write_text("pass\n")
        ranked = [RankedFile("f.py", 1.0)]
        cfg = self._cfg()
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        assert "selected as most relevant" in result
        assert "=== END OF CONTEXT ===" in result

    def test_missing_file_shows_placeholder(self, tmp_path):
        ranked = [RankedFile("ghost.py", 1.0)]
        cfg = self._cfg()
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        assert "[FILE NOT FOUND ON DISK]" in result

    def test_file_content_truncated_at_max_chars(self, tmp_path):
        big = tmp_path / "big.py"
        big.write_text("x" * 10000)
        ranked = [RankedFile("big.py", 1.0)]
        cfg = self._cfg(max_file_chars=100)
        result = build_prompt("task", ranked, cfg, str(tmp_path))
        assert "[... truncated ...]" in result

    def test_prompt_ends_with_user_task(self, tmp_path):
        cfg = self._cfg()
        result = build_prompt("final task", [], cfg, str(tmp_path))
        assert result.strip().endswith("final task")
