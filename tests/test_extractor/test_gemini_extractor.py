"""
Gemini extraction engine: the two consolidated passes, targeted resume, and how
failures are reported rather than invented.
"""
from unittest.mock import AsyncMock, patch

import pytest

from src.extractor.crawlers import gemini_extractor
from src.extractor.crawlers.gemini_extractor import (
    _as_dict,
    _loads_lenient,
    extract_with_gemini_engine,
    query_gemini_block,
)
from src.extractor.crawlers.query_schemas import QUERY_SUITE, IdentityResponse, ProgramsResponse
from src.utilities.gemini_client import GeminiQuotaError
from src.utilities.schema import (
    ContactInfo,
    DegreeLevel,
    FacultyItem,
    KeyLinks,
    MainInfo,
    ProgramItem,
    UniversityPayload,
)

LINKS = [{"url": "https://example.edu/programs", "tier": 1, "selected": True}]


def _programs_response() -> ProgramsResponse:
    return ProgramsResponse(
        bachelors=[
            ProgramItem(
                name="BS Computer Science",
                degree_level=DegreeLevel.BACHELORS,
                tuition_fee="PKR 150,000 per semester",
                currency="PKR",
            )
        ],
        masters=[ProgramItem(name="MS Data Science", degree_level=DegreeLevel.MASTERS)],
        phd=[],
        diploma=[],
    )


def _identity_response() -> IdentityResponse:
    return IdentityResponse(
        main_info=MainInfo(
            name="Example University",
            website="https://example.edu",
            description="A university.",
            key_links=KeyLinks(application_portal_url="https://apply.example.edu"),
        ),
        contact=ContactInfo(official_email="admissions@example.edu"),
        faculties=[FacultyItem(faculty_name="Faculty of Computing", departments=["CS"])],
    )


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Corpus fetching and post-processing never touch the network in tests."""
    monkeypatch.setattr(
        gemini_extractor,
        "fetch_corpus_text_for_links",
        AsyncMock(return_value={"https://example.edu/programs": "BS Computer Science, 4 years."}),
    )
    monkeypatch.setattr(gemini_extractor, "free_search_find_portal", AsyncMock(return_value=None))
    monkeypatch.setattr(gemini_extractor, "is_typesafe_available", lambda: False)
    monkeypatch.setattr(gemini_extractor, "normalize_tuition_batch", AsyncMock(side_effect=lambda p, **_: p))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def test_loads_lenient_strips_markdown_fences():
    assert _loads_lenient('```json\n{"a": 1}\n```') == {"a": 1}


def test_loads_lenient_returns_empty_on_unrepairable_text():
    assert _loads_lenient("not json at all") == {}


def test_as_dict_accepts_models_dicts_and_text():
    assert _as_dict(ContactInfo(official_email="x@y.z"))["official_email"] == "x@y.z"
    assert _as_dict({"a": 1}) == {"a": 1}
    assert _as_dict('{"a": 1}') == {"a": 1}
    assert _as_dict(None) == {}


# ---------------------------------------------------------------------------
# Full engine
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_extract_with_gemini_engine_builds_a_full_payload():
    """Two consolidated passes produce one validated payload -- and only two calls."""
    responses = [_programs_response(), _identity_response()]
    call = AsyncMock(side_effect=responses)

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, report = await extract_with_gemini_engine(
            links_list=LINKS,
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
        )

    assert isinstance(payload, UniversityPayload)
    assert call.await_count == 2, "a full extraction is two requests, not six"
    assert [p.name for p in payload.programs.bachelors] == ["BS Computer Science"]
    assert [p.name for p in payload.programs.masters] == ["MS Data Science"]
    assert payload.programs.phd == []
    assert payload.contact.official_email == "admissions@example.edu"
    assert payload.faculties[0].faculty_name == "Faculty of Computing"
    assert report.ok
    assert payload.failed_query_blocks == []
    # All six blocks are accounted for by the two passes.
    assert set(report.succeeded) == {
        "bachelors", "masters", "phd", "diploma", "main_info_contact", "faculties",
    }


@pytest.mark.asyncio
async def test_engine_accepts_raw_json_text_from_the_model():
    """Schema enforcement is best-effort; raw JSON text still yields a payload."""
    programs_json = '{"bachelors": [{"name": "BSc Physics", "degree_level": "bachelors"}]}'
    identity_json = (
        '{"main_info": {"name": "Example University", "website": "https://example.edu",'
        ' "description": "d", "key_links": {}}, "contact": {"official_email": "a@b.c"},'
        ' "faculties": []}'
    )
    call = AsyncMock(side_effect=[programs_json, identity_json])

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, report = await extract_with_gemini_engine(
            links_list=LINKS,
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
        )

    assert [p.name for p in payload.programs.bachelors] == ["BSc Physics"]
    assert payload.contact.official_email == "a@b.c"
    assert report.ok


@pytest.mark.asyncio
async def test_quota_error_propagates_so_the_orchestrator_can_fall_back():
    call = AsyncMock(side_effect=GeminiQuotaError("daily quota spent"))

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        with pytest.raises(GeminiQuotaError, match="daily quota spent"):
            await extract_with_gemini_engine(
                links_list=LINKS,
                uni_name="Example University",
                uni_slug="example",
                uni_domain="example.edu",
            )


@pytest.mark.asyncio
async def test_a_failed_pass_is_reported_not_invented():
    """A pass that errors leaves its blocks empty and named in the report."""
    call = AsyncMock(side_effect=[RuntimeError("upstream 500"), _identity_response()])

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, report = await extract_with_gemini_engine(
            links_list=LINKS,
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
        )

    assert not report.ok
    assert set(report.failed) == {"bachelors", "masters", "phd", "diploma"}
    assert payload.programs.bachelors == []
    assert payload.failed_query_blocks == ["bachelors", "diploma", "masters", "phd"]
    # The identity pass still landed.
    assert payload.contact.official_email == "admissions@example.edu"


@pytest.mark.asyncio
async def test_empty_corpus_still_yields_an_identity_payload(monkeypatch):
    monkeypatch.setattr(gemini_extractor, "fetch_corpus_text_for_links", AsyncMock(return_value={}))
    call = AsyncMock(side_effect=[ProgramsResponse(), _identity_response()])

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, _ = await extract_with_gemini_engine(
            links_list=[],
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
        )

    assert payload.main_info.name == "Example University"


@pytest.mark.asyncio
async def test_missing_portal_is_filled_by_free_search(monkeypatch):
    monkeypatch.setattr(
        gemini_extractor, "free_search_find_portal", AsyncMock(return_value="https://apply.example.edu/found")
    )
    identity = _identity_response()
    identity.main_info.key_links = KeyLinks()
    call = AsyncMock(side_effect=[ProgramsResponse(), identity])

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, _ = await extract_with_gemini_engine(
            links_list=LINKS,
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
        )

    assert payload.main_info.key_links.application_portal_url == "https://apply.example.edu/found"
    assert payload.main_info.exa_enriched is True


# ---------------------------------------------------------------------------
# Smart resume
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_one_failed_block_is_re_asked_on_its_own():
    """A single outstanding block skips the consolidated passes."""
    call = AsyncMock(return_value=[FacultyItem(faculty_name="Faculty of Law")])

    with patch.object(gemini_extractor, "gemini_generate_json", call):
        payload, report = await extract_with_gemini_engine(
            links_list=LINKS,
            uni_name="Example University",
            uni_slug="example",
            uni_domain="example.edu",
            failed_blocks=["faculties"],
            accumulated_results={"bachelors": [ProgramItem(name="BS CS", degree_level=DegreeLevel.BACHELORS)]},
        )

    assert call.await_count == 1
    assert [f.faculty_name for f in payload.faculties] == ["Faculty of Law"]
    # Previously-extracted blocks were carried through, not re-queried.
    assert [p.name for p in payload.programs.bachelors] == ["BS CS"]


@pytest.mark.asyncio
async def test_query_gemini_block_unwraps_an_items_object():
    spec = next(s for s in QUERY_SUITE if s.key == "faculties")
    payload = {"items": [{"faculty_name": "Faculty of Science", "departments": ["Physics"]}]}

    with patch.object(gemini_extractor, "gemini_generate_json", AsyncMock(return_value=payload)):
        items = await query_gemini_block(spec, "corpus", "Example University", "example.edu")

    assert [f.faculty_name for f in items] == ["Faculty of Science"]


@pytest.mark.asyncio
async def test_query_gemini_block_accepts_a_bare_list():
    spec = next(s for s in QUERY_SUITE if s.key == "bachelors")
    raw = [{"name": "BS Maths", "degree_level": "bachelors"}, "not-a-program"]

    with patch.object(gemini_extractor, "gemini_generate_json", AsyncMock(return_value=raw)):
        items = await query_gemini_block(spec, "corpus", "Example University", "example.edu")

    assert [p.name for p in items] == ["BS Maths"]


@pytest.mark.asyncio
async def test_gemini_reuses_the_deepseek_corpus_fetching():
    """The two engines share one page-fetching implementation."""
    from src.extractor.crawlers import deepseek_extractor

    assert gemini_extractor._build_combined_context is deepseek_extractor._build_combined_context
    # fetch_corpus_text_for_links is monkeypatched by the autouse fixture, so it is
    # checked against the module source rather than the live attribute.
    import inspect
    source = inspect.getsource(gemini_extractor)
    assert "from src.extractor.crawlers.deepseek_extractor import" in source
    assert "fetch_corpus_text_for_links" in source
