"""
Tests for the Config dataclass (reorganised in C10).

The per-field comment rule is a documented requirement of refactoring_plan.md
section 2 ("No variable should be left uncommented"), so it is enforced by a test
rather than left to review discipline.
"""
import ast
import dataclasses
import inspect
from pathlib import Path

import pytest

import src.config as config_module
from src.config import Config, config


def _field_comment_map():
    """Map each dataclass field to the trailing comment on its definition, if any."""
    source = Path(inspect.getfile(config_module)).read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source)

    cls = next(n for n in ast.walk(tree)
               if isinstance(n, ast.ClassDef) and n.name == "Config")

    out = {}
    for node in cls.body:
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        # A field may span several lines (field(default_factory=...)); the inline
        # comment then lives on the closing line.
        end = node.end_lineno or node.lineno
        text = lines[end - 1]
        comment = text.split("#", 1)[1].strip() if "#" in text else ""
        out[node.target.id] = comment
    return out


def test_every_field_is_documented():
    """Each Config field must carry an inline comment explaining what changing it does."""
    comments = _field_comment_map()
    declared = set(Config.__dataclass_fields__)

    assert set(comments) == declared, (
        "AST walk missed fields: " f"{declared ^ set(comments)}"
    )
    undocumented = sorted(name for name, c in comments.items() if not c)
    assert not undocumented, f"Config fields missing an inline comment: {undocumented}"


def test_comments_are_substantive_not_placeholders():
    """A bare '#' or a one-word restatement of the name is not documentation."""
    too_short = sorted(
        name for name, c in _field_comment_map().items() if len(c) < 15
    )
    assert not too_short, f"Config comments too short to be useful: {too_short}"


def test_dead_source_directory_fields_are_gone():
    """
    C10 removed the abandoned src_*_dir fields. They used the extraction/ingestion/
    inspection names the plan explicitly rejected, and nothing ever read them.
    """
    for dead in (
        "src_dir", "src_utils_dir", "src_extraction_dir", "extraction_linkers_dir",
        "extraction_payloaders_dir", "extraction_normalizers_dir",
        "src_ingestion_dir", "src_inspection_dir",
    ):
        assert dead not in Config.__dataclass_fields__, f"{dead} should have been removed"


def test_output_paths_point_at_all_uni_outputs():
    """Finding 13: the ledger pointed at a deleted file in the old outputs root."""
    assert config.output_jsonl_path.parent == config.outputs_all_uni_outputs_dir
    assert config.output_master_json_path.parent == config.outputs_all_uni_outputs_dir
    assert config.output_jsonl_path.suffix == ".jsonl"
    assert config.output_master_json_path.suffix == ".json"
    assert config.output_jsonl_path.stem == config.output_master_json_path.stem


def test_ensure_directories_creates_every_workspace_path(tmp_path):
    """ensure_directories must cover every directory the pipeline writes into."""
    overrides = {
        name: tmp_path / Path(getattr(config, name)).relative_to(config.base_dir)
        for name in Config.__dataclass_fields__
        if name.endswith("_dir") and name != "base_dir"
    }
    cfg = dataclasses.replace(config, base_dir=tmp_path, **overrides)

    cfg.ensure_directories()

    for name in overrides:
        assert getattr(cfg, name).is_dir(), f"{name} was not created"


def test_ensure_directories_is_idempotent(tmp_path):
    cfg = dataclasses.replace(config, base_dir=tmp_path,
                              loggings_dir=tmp_path / "loggings")
    cfg.ensure_directories()
    cfg.ensure_directories()  # must not raise on an existing tree


def test_health_check_knobs_exist_for_c13():
    """Plan section 6.6: sampling must be tunable without code changes."""
    for name in ("health_check_enabled", "health_check_sample_ratio",
                 "health_check_min_sample", "health_check_min_pass_ratio"):
        assert name in Config.__dataclass_fields__
    assert 0 < config.health_check_sample_ratio <= 1
    assert config.health_check_min_sample >= 1
    assert 0 < config.health_check_min_pass_ratio <= 1


def test_query_budget_is_internally_consistent():
    """
    reserve_queries() claims queries_per_university up front, so the daily budget
    must admit at least one university. C17 raises the suite to 6 queries.
    """
    assert config.queries_per_university >= 1
    assert config.daily_query_budget >= config.queries_per_university


def test_tier_quotas_sum_to_the_requested_total():
    for total in (0, 1, 7, 100, 150):
        quotas = config.tier_quotas(total)
        assert sum(quotas.values()) == total, f"tier quotas lost links at total={total}"
        assert set(quotas) == set(config.tier_quota_shares)


def test_tier_quota_shares_are_a_partition():
    assert pytest.approx(sum(config.tier_quota_shares.values()), abs=1e-9) == 1.0


def test_embedding_fields_are_a_real_contract_not_a_literal():
    """
    Until C14 these fields were dead: only the vector exporter deleted in C11b read
    them, and they disagreed with what the scorer actually loaded -- config said
    bge-*base* (768-dim) / batch 32 while the scorer hardcoded bge-*small* (384-dim)
    / batch 64. The scorer now reads config, so the values are enforced for real in
    tests/test_extractor/test_linkers_semantic_scoring.py. This only guards the
    shape, so a bad value cannot reach a model load.
    """
    assert config.embedding_model_name and "/" in config.embedding_model_name
    assert isinstance(config.embedding_batch_size, int) and config.embedding_batch_size > 0


def test_no_vector_store_credentials_remain():
    """Supabase is the only destination; Qdrant and Pinecone keys are gone."""
    for dead in ("qdrant_url", "qdrant_api_key", "qdrant_collection_name",
                 "qdrant_upsert_batch_size", "pinecone_api_key", "pinecone_index_name"):
        assert dead not in Config.__dataclass_fields__, f"{dead} should have been removed"
