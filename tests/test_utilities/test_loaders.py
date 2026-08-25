"""Tests for src.utilities.loaders.load_dotenv (extracted from config.py in C4)."""
import os

import pytest

from src.utilities.loaders import load_dotenv


def test_missing_file_is_a_noop(tmp_path):
    """A project without a .env must not raise -- the pipeline runs on real env vars."""
    before = dict(os.environ)
    load_dotenv(tmp_path / "does_not_exist.env")
    assert dict(os.environ) == before


def test_sets_keys_from_file(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text("UNICRAWL_TEST_KEY=hello\n", encoding="utf-8")
    monkeypatch.delenv("UNICRAWL_TEST_KEY", raising=False)

    load_dotenv(env)

    assert os.environ["UNICRAWL_TEST_KEY"] == "hello"


def test_existing_environment_wins(tmp_path, monkeypatch):
    """An explicitly exported key must never be clobbered by a stale .env file."""
    env = tmp_path / ".env"
    env.write_text("UNICRAWL_TEST_KEY=from_file\n", encoding="utf-8")
    monkeypatch.setenv("UNICRAWL_TEST_KEY", "from_shell")

    load_dotenv(env)

    assert os.environ["UNICRAWL_TEST_KEY"] == "from_shell"


@pytest.mark.parametrize(
    "line,expected",
    [
        ("UNICRAWL_TEST_KEY='single'", "single"),
        ('UNICRAWL_TEST_KEY="double"', "double"),
        ("UNICRAWL_TEST_KEY=  padded  ", "padded"),
        ("UNICRAWL_TEST_KEY=", ""),
        ("UNICRAWL_TEST_KEY=a=b=c", "a=b=c"),
    ],
)
def test_value_parsing(tmp_path, monkeypatch, line, expected):
    """Quotes and surrounding whitespace are stripped; only the first '=' splits."""
    env = tmp_path / ".env"
    env.write_text(line + "\n", encoding="utf-8")
    monkeypatch.delenv("UNICRAWL_TEST_KEY", raising=False)

    load_dotenv(env)

    assert os.environ["UNICRAWL_TEST_KEY"] == expected


def test_comments_blanks_and_malformed_lines_are_skipped(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text(
        "\n"
        "# UNICRAWL_COMMENTED=nope\n"
        "   \n"
        "THIS_LINE_HAS_NO_EQUALS_SIGN\n"
        "UNICRAWL_TEST_KEY=survived\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("UNICRAWL_TEST_KEY", raising=False)
    monkeypatch.delenv("UNICRAWL_COMMENTED", raising=False)

    load_dotenv(env)

    assert os.environ["UNICRAWL_TEST_KEY"] == "survived"
    assert "UNICRAWL_COMMENTED" not in os.environ
    assert "THIS_LINE_HAS_NO_EQUALS_SIGN" not in os.environ


def test_unreadable_file_is_swallowed(tmp_path, monkeypatch):
    """A directory where a file is expected raises OSError on read; must not propagate."""
    bogus = tmp_path / "env_dir"
    bogus.mkdir()
    load_dotenv(bogus)  # must not raise


def test_config_no_longer_defines_the_loader():
    """C4 moved the loader out of config.py; guard against it creeping back."""
    import src.config as cfg

    assert not hasattr(cfg, "_load_dotenv")
    assert cfg.load_dotenv is load_dotenv
