import json
from unittest.mock import AsyncMock, patch
import pytest

from src.extractor.crawlers.deepseek_extractor import (
    _clean_html_to_markdown_summary,
    _build_combined_context,
    query_deepseek_block,
    extract_with_deepseek_engine,
)
from src.extractor.crawlers.notebook_querying import QUERY_SUITE, Q1Payload
from src.utilities.schema import DegreeLevel, UniversityPayload, ProgramItem


def test_clean_html_to_markdown_summary():
    """Verify HTML stripping removes script, style, nav, and preserves content."""
    sample_html = """
    <html>
    <head><style>.ad { color: red; }</style></head>
    <body>
        <nav><a href="/home">Home</a></nav>
        <h1>Computer Science Department</h1>
        <p>BS Computer Science requires 130 credit hours.</p>
        <script>console.log("tracking");</script>
        <footer>Copyright 2026</footer>
    </body>
    </html>
    """
    cleaned = _clean_html_to_markdown_summary(sample_html)
    assert "Computer Science Department" in cleaned
    assert "BS Computer Science requires 130 credit hours." in cleaned
    assert "tracking" not in cleaned
    assert "Copyright 2026" not in cleaned


def test_build_combined_context():
    """Verify source URLs and text chunks are formatted cleanly."""
    corpus = {
        "https://mit.edu/cs": "MIT Computer Science program description.",
        "https://mit.edu/admissions": "Application deadline is January 15.",
    }
    context = _build_combined_context(corpus)
    assert "--- SOURCE URL: https://mit.edu/cs ---" in context
    assert "MIT Computer Science program description." in context
    assert "--- SOURCE URL: https://mit.edu/admissions ---" in context


@pytest.mark.asyncio
async def test_query_deepseek_block_single():
    """Verify single-block querying (main_info_contact) against mocked DeepSeek API."""
    spec = next(s for s in QUERY_SUITE if s.key == "main_info_contact")
    mock_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "main_info": {
                            "name": "MIT",
                            "website": "https://mit.edu",
                            "type": "private",
                            "description": "Top research university.",
                            "key_links": {
                                "application_portal_url": "https://admissions.mit.edu/apply"
                            },
                        },
                        "contact": {
                            "official_email": "admissions@mit.edu",
                            "phone_numbers": ["+1-617-253-1000"],
                        },
                    })
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json = lambda: mock_response

        res = await query_deepseek_block(spec, "Corpus text...", "MIT", "mit.edu")
        assert isinstance(res, Q1Payload)
        assert res.main_info.name == "MIT"
        assert res.main_info.website == "https://mit.edu"
        assert res.main_info.key_links.application_portal_url == "https://admissions.mit.edu/apply"
        assert res.contact.official_email == "admissions@mit.edu"


@pytest.mark.asyncio
async def test_extract_with_deepseek_engine_mock():
    """Verify end-to-end payload extraction with DeepSeek engine."""
    mock_links = [
        {"url": "https://mit.edu/cs", "selected": True, "tier": 1},
        {"url": "https://mit.edu/apply", "selected": True, "tier": 2},
    ]

    mock_q1 = Q1Payload(
        main_info={
            "name": "Massachusetts Institute of Technology",
            "website": "https://mit.edu",
            "type": "private",
            "description": "Leading science institute.",
            "key_links": {"application_portal_url": "https://mit.edu/apply"},
        },
        contact={"official_email": "info@mit.edu", "phone_numbers": []},
    )

    mock_bachelors = [
        ProgramItem(
            name="BS in Computer Science and Engineering",
            degree_level=DegreeLevel.BACHELORS,
            tuition_fee="$62,150 per year",
            duration="4 Years",
        )
    ]

    async def mock_query_block(spec, corpus, name, domain):
        if spec.key == "main_info_contact":
            return mock_q1
        elif spec.key == "bachelors":
            return mock_bachelors
        return []

    with patch("src.extractor.crawlers.deepseek_extractor.fetch_corpus_text_for_links", new_callable=AsyncMock) as mock_fetch, \
         patch("src.extractor.crawlers.deepseek_extractor.query_deepseek_block", side_effect=mock_query_block), \
         patch("src.extractor.crawlers.deepseek_extractor.is_typesafe_available", return_value=False):
        mock_fetch.return_value = {"https://mit.edu/cs": "Computer Science degree details."}

        payload, report = await extract_with_deepseek_engine(
            links_list=mock_links,
            uni_name="Massachusetts Institute of Technology",
            uni_slug="mit",
            uni_domain="mit.edu",
        )

        assert isinstance(payload, UniversityPayload)
        assert payload.main_info.name == "Massachusetts Institute of Technology"
        assert len(payload.programs.bachelors) == 1
        assert payload.programs.bachelors[0].name == "BS in Computer Science and Engineering"
        assert payload.programs.bachelors[0].tuition_fee_normalized is not None
        assert payload.programs.bachelors[0].tuition_fee_normalized.amount == 62150.0
        assert payload.intake_year == "2026"
        assert payload.data_version == 1
        assert report.ok is True


@pytest.mark.asyncio
async def test_query_deepseek_block_items_generic_unwrapping():
    """Verify that List[ProgramItem] and List[FacultyItem] models are correctly unwrapped."""
    masters_spec = next(s for s in QUERY_SUITE if s.key == "masters")
    mock_response = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({
                        "items": [
                            {
                                "name": "Master of Engineering in Aerospace Engineering",
                                "degree_level": "masters",
                                "duration": "2 semesters",
                            }
                        ]
                    })
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json = lambda: mock_response

        res = await query_deepseek_block(masters_spec, "Corpus...", "Cornell", "cornell.edu")
        assert isinstance(res, list)
        assert len(res) == 1
        assert isinstance(res[0], ProgramItem)
        assert res[0].name == "Master of Engineering in Aerospace Engineering"


@pytest.mark.asyncio
async def test_query_deepseek_block_markdown_and_payload():
    """Verify markdown fences are stripped and payload sets thinking disabled and max_tokens 8192."""
    faculties_spec = next(s for s in QUERY_SUITE if s.key == "faculties")
    fenced_json = "```json\n" + json.dumps({"items": [{"faculty_name": "Faculty of Engineering"}]}) + "\n```"
    mock_response = {
        "choices": [
            {
                "message": {
                    "content": fenced_json
                }
            }
        ]
    }

    with patch("httpx.AsyncClient.post", new_callable=AsyncMock) as mock_post:
        mock_post.return_value.status_code = 200
        mock_post.return_value.json = lambda: mock_response

        res = await query_deepseek_block(faculties_spec, "Corpus...", "Cornell", "cornell.edu")
        assert len(res) == 1
        assert res[0].faculty_name == "Faculty of Engineering"

        # Verify post payload arguments
        _, kwargs = mock_post.call_args
        sent_payload = kwargs.get("json", {})
        assert sent_payload.get("thinking") == {"type": "disabled"}
        assert sent_payload.get("max_tokens") == 8192
        assert sent_payload.get("response_format") == {"type": "json_object"}

