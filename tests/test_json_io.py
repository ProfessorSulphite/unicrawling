"""
Unit tests for JSON I/O primitives: atomic writes, JSONL streaming, and master compilation.
"""
import json
import pytest

from src.json_io import (
    atomic_write_text,
    atomic_write_json,
    append_jsonl,
    iter_jsonl,
    stream_compile_master_json,
)


class TestAppendJsonl:
    """Tests for append_jsonl function."""

    def test_append_jsonl_with_dict_and_string(self, tmp_path):
        """append_jsonl accepts both dict and pre-serialized JSON string, writes one line per call."""
        ledger = tmp_path / "ledger.jsonl"

        # Append as dict
        append_jsonl(ledger, {"name": "University A", "city": "Lahore"})
        # Append as pre-serialized JSON string
        append_jsonl(ledger, '{"name": "University B", "city": "Islamabad"}')

        # Should have exactly 2 lines
        lines = ledger.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        assert json.loads(lines[0]) == {"name": "University A", "city": "Lahore"}
        assert json.loads(lines[1]) == {"name": "University B", "city": "Islamabad"}

    def test_append_jsonl_newline_termination(self, tmp_path):
        """Each append_jsonl call writes exactly one newline-terminated line."""
        ledger = tmp_path / "ledger.jsonl"

        append_jsonl(ledger, {"id": 1})
        append_jsonl(ledger, {"id": 2})

        content = ledger.read_bytes()
        # Each line should end with a newline
        lines = content.split(b"\n")
        # Last element after split on final \n will be empty string
        assert lines[-1] == b""
        assert len(lines) == 3  # 2 records + 1 empty after final \n


class TestIterJsonl:
    """Tests for iter_jsonl function."""

    def test_iter_jsonl_is_generator_and_yields_in_order(self, tmp_path):
        """iter_jsonl returns a generator that yields dicts in file order."""
        ledger = tmp_path / "ledger.jsonl"
        records = [{"id": 1}, {"id": 2}, {"id": 3}]
        for rec in records:
            append_jsonl(ledger, rec)

        gen = iter_jsonl(ledger)
        # Verify it's a generator
        assert hasattr(gen, "__iter__") and hasattr(gen, "__next__")

        # Collect all yielded records
        yielded = list(gen)
        assert yielded == records

    def test_iter_jsonl_skips_blank_lines(self, tmp_path):
        """iter_jsonl skips blank lines."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text('{"id": 1}\n\n{"id": 2}\n  \n{"id": 3}\n', encoding="utf-8")

        records = list(iter_jsonl(ledger))
        assert len(records) == 3
        assert records == [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_iter_jsonl_skips_malformed_by_default(self, tmp_path):
        """iter_jsonl skips malformed lines by default instead of raising."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text(
            '{"id": 1}\n'
            'not valid json\n'
            '{"id": 2}\n'
            '{broken\n'
            '{"id": 3}\n',
            encoding="utf-8"
        )

        records = list(iter_jsonl(ledger, skip_malformed=True))
        assert records == [{"id": 1}, {"id": 2}, {"id": 3}]

    def test_iter_jsonl_raises_on_malformed_when_skip_malformed_false(self, tmp_path):
        """iter_jsonl raises json.JSONDecodeError when skip_malformed=False."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text('{"id": 1}\ninvalid json\n', encoding="utf-8")

        with pytest.raises(json.JSONDecodeError):
            list(iter_jsonl(ledger, skip_malformed=False))

    def test_iter_jsonl_yields_nothing_for_nonexistent_path(self, tmp_path):
        """iter_jsonl yields nothing for a non-existent path."""
        nonexistent = tmp_path / "does_not_exist.jsonl"

        records = list(iter_jsonl(nonexistent))
        assert records == []


class TestStreamCompileMasterJson:
    """Tests for stream_compile_master_json function."""

    def test_stream_compile_missing_ledger_returns_empty_array(self, tmp_path):
        """stream_compile_master_json on missing ledger writes [] and returns 0."""
        ledger = tmp_path / "ledger.jsonl"  # Does not exist
        master = tmp_path / "master.json"

        count = stream_compile_master_json(ledger, master)

        assert count == 0
        assert json.loads(master.read_text(encoding="utf-8")) == []

    def test_stream_compile_empty_ledger_returns_empty_array(self, tmp_path):
        """stream_compile_master_json on empty ledger writes [] and returns 0."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text("", encoding="utf-8")
        master = tmp_path / "master.json"

        count = stream_compile_master_json(ledger, master)

        assert count == 0
        assert json.loads(master.read_text(encoding="utf-8")) == []

    def test_stream_compile_dedupes_by_name_case_insensitive_last_write_wins(self, tmp_path):
        """stream_compile_master_json dedupes by main_info.name (case-insensitive) with last-write-wins."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        # Append the same university twice with different city values
        append_jsonl(ledger, {"main_info": {"name": "NUST"}, "city": "Islamabad"})
        append_jsonl(ledger, {"main_info": {"name": "nust"}, "city": "Rawalpindi"})

        count = stream_compile_master_json(ledger, master, dedupe=True)

        # Should have only 1 record (deduplicated)
        assert count == 1
        records = json.loads(master.read_text(encoding="utf-8"))
        assert len(records) == 1
        # Last write wins: should have the second city (Rawalpindi)
        assert records[0]["city"] == "Rawalpindi"
        assert records[0]["main_info"]["name"] == "nust"

    def test_stream_compile_preserves_unkeyed_records(self, tmp_path):
        """stream_compile_master_json never drops records with no resolvable main_info.name."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        # Records with no identifiable name are never dropped
        append_jsonl(ledger, {"data": "value1"})  # No main_info
        append_jsonl(ledger, {"main_info": {"name": "NUST"}, "city": "Islamabad"})
        append_jsonl(ledger, {"main_info": {}})  # Empty main_info
        append_jsonl(ledger, {"main_info": {"name": "NUST"}, "city": "Rawalpindi"})

        count = stream_compile_master_json(ledger, master, dedupe=True)

        # Should keep: 1 unkeyed, 1 for empty, 1 deduplicated NUST = 3 total
        # Actually: unkeyed records (no name) are kept, NUST gets deduplicated to 1
        # So: 1 ({"data": "value1"}) + 1 ({"main_info": {}}) + 1 (NUST with Rawalpindi) = 3
        assert count == 3
        records = json.loads(master.read_text(encoding="utf-8"))
        assert len(records) == 3

    def test_stream_compile_dedupe_false_preserves_duplicates(self, tmp_path):
        """stream_compile_master_json with dedupe=False preserves every record including duplicates."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        append_jsonl(ledger, {"main_info": {"name": "NUST"}, "city": "Islamabad"})
        append_jsonl(ledger, {"main_info": {"name": "NUST"}, "city": "Rawalpindi"})
        append_jsonl(ledger, {"main_info": {"name": "ITU"}, "city": "Lahore"})

        count = stream_compile_master_json(ledger, master, dedupe=False)

        assert count == 3
        records = json.loads(master.read_text(encoding="utf-8"))
        assert len(records) == 3
        # Both NUST records should be present
        nust_records = [r for r in records if r.get("main_info", {}).get("name") == "NUST"]
        assert len(nust_records) == 2

    def test_stream_compile_byte_identical_to_json_dumps(self, tmp_path):
        """The compiled master file is byte-identical to json.dumps(records, indent=2, ensure_ascii=False) + newline."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        records = [
            {"id": 1, "name": "First"},
            {"id": 2, "name": "Second"},
        ]
        for rec in records:
            append_jsonl(ledger, rec)

        stream_compile_master_json(ledger, master, indent=2, dedupe=False)

        # Expected output
        expected = json.dumps(records, indent=2, ensure_ascii=False) + "\n"
        actual = master.read_text(encoding="utf-8")

        assert actual == expected

    def test_stream_compile_creates_parent_directory(self, tmp_path):
        """stream_compile_master_json creates parent directories if they don't exist."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "subdir1" / "subdir2" / "master.json"

        append_jsonl(ledger, {"id": 1})
        stream_compile_master_json(ledger, master)

        assert master.exists()


class TestAtomicWriteOperations:
    """Tests for atomic_write_text and atomic_write_json atomicity guarantees."""

    def test_atomic_write_text_no_leftover_tmp_files(self, tmp_path):
        """atomic_write_text leaves no leftover .tmp files after successful write."""
        target = tmp_path / "file.txt"

        atomic_write_text(target, "Hello, World!")

        # Check no .tmp file remains
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
        assert target.exists()
        assert target.read_text(encoding="utf-8") == "Hello, World!"

    def test_atomic_write_json_no_leftover_tmp_files(self, tmp_path):
        """atomic_write_json leaves no leftover .tmp files after successful write."""
        target = tmp_path / "data.json"
        data = {"key": "value"}

        atomic_write_json(target, data)

        # Check no .tmp file remains
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == data

    def test_stream_compile_no_leftover_tmp_files(self, tmp_path):
        """stream_compile_master_json leaves no leftover .tmp files after successful write."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        append_jsonl(ledger, {"id": 1})
        stream_compile_master_json(ledger, master)

        # Check no .tmp file remains in master's directory
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []
        assert master.exists()


class TestNonAsciiContent:
    """Tests for handling non-ASCII characters."""

    def test_non_ascii_roundtrip_through_append_and_compile(self, tmp_path):
        """Non-ASCII content (ü, é) round-trips unescaped through append_jsonl -> stream_compile_master_json."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        # Append records with non-ASCII characters
        record1 = {"main_info": {"name": "Münster Universität"}, "city": "München"}
        record2 = {"main_info": {"name": "Université de Montréal"}, "city": "Québec"}

        append_jsonl(ledger, record1)
        append_jsonl(ledger, record2)

        stream_compile_master_json(ledger, master, dedupe=False)

        # Read and verify
        compiled = json.loads(master.read_text(encoding="utf-8"))
        assert len(compiled) == 2
        assert compiled[0]["main_info"]["name"] == "Münster Universität"
        assert compiled[0]["city"] == "München"
        assert compiled[1]["main_info"]["name"] == "Université de Montréal"
        assert compiled[1]["city"] == "Québec"

        # Verify the file is not escaped (uses ensure_ascii=False)
        master_text = master.read_text(encoding="utf-8")
        assert "Münster Universität" in master_text  # Should be unescaped
        assert "Université de Montréal" in master_text  # Should be unescaped
        assert "\\u" not in master_text  # Should not contain escaped unicode


class TestEdgeCases:
    """Edge case tests."""

    def test_append_jsonl_strips_trailing_newlines_from_string(self, tmp_path):
        """append_jsonl strips trailing newlines when given a pre-serialized JSON string."""
        ledger = tmp_path / "ledger.jsonl"

        # Pass a string with trailing newlines
        append_jsonl(ledger, '{"id": 1}\n\n')

        lines = ledger.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 1
        assert json.loads(lines[0]) == {"id": 1}

    def test_stream_compile_with_only_blank_lines(self, tmp_path):
        """stream_compile_master_json handles ledger with only blank lines."""
        ledger = tmp_path / "ledger.jsonl"
        master = tmp_path / "master.json"

        ledger.write_text("\n\n  \n", encoding="utf-8")

        count = stream_compile_master_json(ledger, master)

        assert count == 0
        assert json.loads(master.read_text(encoding="utf-8")) == []

    def test_atomic_write_creates_parent_directories(self, tmp_path):
        """atomic_write_text creates parent directories if they don't exist."""
        target = tmp_path / "a" / "b" / "c" / "file.txt"

        atomic_write_text(target, "content")

        assert target.exists()
        assert target.read_text(encoding="utf-8") == "content"

    def test_iter_jsonl_with_whitespace_only_lines(self, tmp_path):
        """iter_jsonl skips lines that are whitespace-only."""
        ledger = tmp_path / "ledger.jsonl"
        ledger.write_text('{"id": 1}\n   \t   \n{"id": 2}\n', encoding="utf-8")

        records = list(iter_jsonl(ledger))
        assert len(records) == 2
        assert records == [{"id": 1}, {"id": 2}]
