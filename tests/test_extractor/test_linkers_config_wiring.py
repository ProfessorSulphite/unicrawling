"""
Phase 1's knobs must be the ones Config publishes.

`semantic_threshold`, `max_crawl_pages` and `dynamic_link_ratio` were documented
in Config and in COMMANDS.md, and read by nothing. The orchestrator called
run_pipeline without passing a threshold or a page budget, so the linker CLI's
argparse defaults won: every batch ran at 0.45 and 15 pages regardless of what
config said. A live ITU crawl logged "78 links passed quality threshold (0.45);
0 retained as tier reserve" -- every link cleared it, so the threshold filtered
nothing and tier quotas were doing all of the selection.

These tests pin the wiring, not the values, so re-tuning a knob stays a config
change rather than a code change.
"""
import ast
import inspect
from pathlib import Path

import pytest

import src.config as config_module
from src.config import config
from src.extractor.linkers import runner as linkers_runner
from src.extractor.linkers.semantic_scoring import classify_and_score_links


# --------------------------------------------------------- the knobs are read --

def test_the_scorer_defaults_to_the_configured_threshold(monkeypatch):
    """None means the calibrated value, never a literal."""
    seen = {}

    class _FakeModel:
        def encode(self, texts, **kw):
            return texts

        def similarity(self, a, b):
            import torch

            return torch.tensor([[0.9] * len(b) for _ in a])

    monkeypatch.setattr(
        "src.extractor.linkers.semantic_scoring._get_embedding_model", lambda: _FakeModel()
    )
    monkeypatch.setattr(config, "semantic_threshold", 0.95)

    links = [{"text": "BS CS", "path_words": "bs cs", "href": "https://x/bs-cs", "raw_text": "BS CS"}]
    scored = classify_and_score_links(links)

    # 0.9 < the configured 0.95, so it is reserve rather than a pass. Under the
    # old literal default of 0.45 it would have passed.
    assert scored and scored[0]["passed_threshold"] is False


def test_an_explicit_threshold_still_wins(monkeypatch):
    class _FakeModel:
        def encode(self, texts, **kw):
            return texts

        def similarity(self, a, b):
            import torch

            return torch.tensor([[0.9] * len(b) for _ in a])

    monkeypatch.setattr(
        "src.extractor.linkers.semantic_scoring._get_embedding_model", lambda: _FakeModel()
    )
    monkeypatch.setattr(config, "semantic_threshold", 0.95)

    links = [{"text": "BS CS", "path_words": "bs cs", "href": "https://x/bs-cs", "raw_text": "BS CS"}]
    scored = classify_and_score_links(links, threshold=0.5)
    assert scored[0]["passed_threshold"] is True


async def test_run_pipeline_takes_its_page_budget_from_config(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "data_links_dir", tmp_path)
    monkeypatch.setattr(config, "max_crawl_pages", 42)
    seen = {}

    async def _record(start_url, max_pages=15):
        seen["max_pages"] = max_pages
        return [{"href": "https://x/bs-cs", "text": "BS CS", "title": "", "crawl_score": 1.0}]

    monkeypatch.setattr(linkers_runner, "crawl_site_links", _record)
    await linkers_runner.run_pipeline(
        url="https://x",
        output_links=str(tmp_path / "l.txt"),
        output_detailed=str(tmp_path / "d.txt"),
    )
    assert seen["max_pages"] == 42


async def test_the_selection_ratio_comes_from_config(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(config, "data_links_dir", tmp_path)
    monkeypatch.setattr(config, "dynamic_link_ratio", 0.10)

    async def _many(start_url, max_pages=15):
        return [
            {"href": f"https://x/bs-{i}", "text": f"BS {i}", "title": "", "crawl_score": 1.0}
            for i in range(100)
        ]

    async def _fake_score(links, **kw):
        return [
            {
                "href": l["href"],
                "text": l["text"],
                "raw_text": l["text"],
                "matched_keyword": "bs cs",
                "raw_similarity_score": 0.9,
                "weighted_score": 0.9,
                "category": "Tier 1: Admissions & Entry Requirements",
                "priority_tier_num": 1,
                "year_tag": "Current / Timeless",
                "passed_threshold": True,
            }
            for l in links
        ]

    monkeypatch.setattr(linkers_runner, "crawl_site_links", _many)
    monkeypatch.setattr(linkers_runner, "classify_and_score_links_async", _fake_score)
    with caplog.at_level("INFO"):
        await linkers_runner.run_pipeline(
            url="https://x",
            output_links=str(tmp_path / "l.txt"),
            output_detailed=str(tmp_path / "d.txt"),
        )
    assert "10% ratio" in caplog.text


# ---------------------------------------------------- the orchestrator passes --

def test_the_orchestrator_passes_both_knobs_to_phase_1():
    """
    Read from the source rather than executed: the call sits inside the network
    path, and what matters is that neither argument is left to a default again.
    """
    import src.orchestrator as orch

    source = Path(inspect.getfile(orch)).read_text(encoding="utf-8")
    call = next(
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and getattr(node.func, "id", None) == "run_link_extractor"
    )
    passed = {kw.arg for kw in call.keywords}
    assert {"threshold", "max_pages"} <= passed, (
        "Phase 1 was called without a threshold or page budget, so the linker "
        "CLI's argparse defaults silently replaced the configured values."
    )


def test_no_phase_1_knob_is_documented_in_config_and_read_by_nothing():
    """The three fields that were pure documentation until this change."""
    src_dir = Path(inspect.getfile(config_module)).parent
    body = "\n".join(
        p.read_text(encoding="utf-8")
        for p in src_dir.rglob("*.py")
        if p.name != "config.py"
    )
    for field in ("semantic_threshold", "max_crawl_pages", "dynamic_link_ratio"):
        assert f"config.{field}" in body, f"config.{field} is documented but read by nothing"
