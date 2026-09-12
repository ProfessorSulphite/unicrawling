"""
Proportional tier quota allocation and the semantic threshold it is tuned against.

Moved out of tests/test_pipeline.py in C14. Every test here is a regression test
for a defect that was actually present in the shipped code, not a smoke test.
"""
from src.config import Config
from src.extractor.linkers.semantic_scoring import allocate_proportional_tier_quotas


def test_semantic_threshold_is_calibrated_for_prefixed_bge():
    """
    The inherited 0.45 was a dead knob: with the BGE query prefix and L2-normalised
    embeddings, a live 473-link crawl scored min 0.639, so every link cleared it and
    the threshold filtered nothing.
    """
    assert Config().semantic_threshold > 0.60
# =============================================================================
# Proportional tier quotas
# =============================================================================

def _mk(tier, score):
    return {"priority_tier_num": tier, "weighted_score": score,
            "href": f"https://x/{tier}-{score}", "category": f"Tier {tier}"}


def test_tier_quotas_guarantee_faculty_and_contact_sources():
    """
    scored[:max_links] after a tier-major sort let Tier 1 consume the whole budget,
    so the faculties (Q5) and contact (Q1) queries ran against a notebook that
    contained no faculty or contact pages at all.
    """
    links = ([_mk(1, 0.9 - i * 0.001) for i in range(200)]
             + [_mk(2, 0.8 - i * 0.001) for i in range(100)]
             + [_mk(3, 0.7 - i * 0.001) for i in range(50)]
             + [_mk(4, 0.6 - i * 0.001) for i in range(50)])
    selected = allocate_proportional_tier_quotas(links, total_cap=60)
    tiers = [i["priority_tier_num"] for i in selected]
    assert len(selected) == 60
    for t in (1, 2, 3, 4):
        assert tiers.count(t) > 0, f"tier {t} was starved"
    # Derived from config, not transcribed from it. These numbers used to be
    # written out as literals (27/18/9/6), which pinned one particular setting of
    # tier_quota_shares rather than the allocation behaviour, and turned any
    # retuning of the shares into a test failure with nothing wrong behind it.
    expected = Config().tier_quotas(60)
    for tier, quota in expected.items():
        assert tiers.count(tier) == quota, f"tier {tier} got {tiers.count(tier)}, not {quota}"


def test_tier_quotas_redistribute_unfilled_budget():
    """A small site must still fill its budget rather than under-ingesting."""
    links = [_mk(1, 0.9 - i * 0.001) for i in range(100)] + [_mk(3, 0.5)]
    selected = allocate_proportional_tier_quotas(links, total_cap=60)
    assert len(selected) == 60


def test_tier_reserve_prevents_threshold_starving_a_tier():
    """
    A global threshold tuned for the dense programme tier starved the sparse
    faculties tier on a live NUST run (4 sources against a quota of 9), degrading
    the faculties query. A tier fills its quota from its own sub-threshold reserve
    before any cross-tier redistribution.
    """
    links = [dict(_mk(1, 0.9 - i * 0.001), passed_threshold=True) for i in range(200)]
    links += [dict(_mk(2, 0.85 - i * 0.001), passed_threshold=True) for i in range(60)]
    links += [dict(_mk(3, 0.80), passed_threshold=True) for _ in range(4)]
    links += [dict(_mk(3, 0.62 - i * 0.001), passed_threshold=False) for i in range(20)]
    links += [dict(_mk(4, 0.75), passed_threshold=True) for _ in range(10)]

    selected = allocate_proportional_tier_quotas(links, total_cap=60)
    tiers = [i["priority_tier_num"] for i in selected]
    tier3_quota = Config().tier_quotas(60)[3]
    assert tier3_quota > 4, "the fixture only tests backfill if the quota exceeds the 4 passing links"
    assert tiers.count(3) == tier3_quota, "tier 3 must backfill from its own reserve"
    # The 4 above-threshold tier-3 links must all be chosen ahead of the reserve.
    t3 = [i for i in selected if i["priority_tier_num"] == 3]
    assert sum(1 for i in t3 if i["passed_threshold"]) == 4


def test_tier_quotas_prefer_passing_links_over_reserve():
    links = [dict(_mk(1, 0.70), passed_threshold=False) for _ in range(50)]
    links += [dict(_mk(1, 0.69), passed_threshold=True) for _ in range(50)]
    selected = allocate_proportional_tier_quotas(links, total_cap=20)
    assert all(i["passed_threshold"] for i in selected), \
        "an above-threshold link must outrank a higher-scoring reserve link"


def test_tier_quotas_handle_undersized_input():
    selected = allocate_proportional_tier_quotas([_mk(1, 0.9), _mk(2, 0.8)], total_cap=60)
    assert len(selected) == 2
