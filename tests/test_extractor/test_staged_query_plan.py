"""
The staged query plan: roster-first chunking, budgeting, merging and arbitration.

These replace what a live run would have told us, because a live run costs real
NotebookLM quota against a 75-query rolling window. The client is faked; the
plan logic, the budget arithmetic and the merge rules are real.
"""

import asyncio

import pytest

from src.config import config
from src.extractor.crawlers import runner as crawl_runner
from src.extractor.crawlers.notebook_querying import (
    ExtractionReport,
    build_detail_specs,
    build_gapfill_spec,
    build_identity_spec,
    build_roster_spec,
    match_roster_name,
    normalize_program_name,
    plan_query_budget,
)
from src.utilities.schema import ProgramItem


def _program(name, level="bachelors", **kw):
    return ProgramItem(name=name, degree_level=level, **kw)


# ---------------------------------------------------------------------------
# Budgeting
# ---------------------------------------------------------------------------

class TestQueryBudget:
    def test_ask_count_follows_from_roster_size_and_chunk_size(self):
        chunk, asks = plan_query_budget(17)
        assert chunk == config.program_detail_chunk_size
        assert asks == 4                      # ceil(17 / 5)

    def test_a_tight_budget_raises_the_chunk_size_rather_than_dropping_programmes(self):
        """
        The direction of this trade is the point.

        A dropped programme is invisible downstream -- it looks exactly like a
        university that does not offer it. A larger chunk merely risks a big
        response, which announces itself and can be retried.
        """
        chunk, asks = plan_query_budget(60, budget=4)
        assert asks <= 4
        assert chunk > config.program_detail_chunk_size
        assert chunk * asks >= 60

    def test_chunk_size_is_clamped_so_a_doomed_ask_is_never_issued(self):
        """
        Beyond the ceiling the response is large enough to be the original
        problem again. The honest outcome is fewer programmes described, and the
        caller saying so, rather than an ask that predictably blows the ceiling.
        """
        chunk, asks = plan_query_budget(500, budget=1)
        assert chunk == config.max_program_detail_chunk_size
        assert asks > 1

    def test_an_empty_roster_needs_no_detail_asks(self):
        _, asks = plan_query_budget(0)
        assert asks == 0


# ---------------------------------------------------------------------------
# Stage construction
# ---------------------------------------------------------------------------

class TestStageConstruction:
    def test_detail_chunks_never_straddle_a_degree_level(self):
        """One LEVEL per ask; the model is never asked to switch taxonomy."""
        roster = (
            [_program(f"BS {i}", "bachelors") for i in range(7)]
            + [_program(f"MS {i}", "masters") for i in range(3)]
        )
        specs = build_detail_specs(roster, chunk_size=5)
        assert len(specs) == 3               # 5 + 2 bachelors, 3 masters
        assert [s.key.split(":")[1] for s in specs] == ["bachelors", "bachelors", "masters"]

    def test_every_roster_programme_appears_in_exactly_one_chunk(self):
        roster = [_program(f"BS {i}") for i in range(13)]
        specs = build_detail_specs(roster, chunk_size=5)
        mentioned = [p.name for p in roster if any(f"- {p.name}\n" in s.prompt + "\n" for s in specs)]
        assert len(mentioned) == 13

        # And no programme is described twice: duplicated detail records merge
        # into one row, so a duplicate is silent rather than visible.
        counts = {
            p.name: sum(s.prompt.count(f"- {p.name}\n") for s in specs)
            for p in roster
        }
        assert set(counts.values()) == {1}

    def test_the_roster_prompt_asks_for_no_detail_fields(self):
        """
        The roster's bound is that it emits four short fields per programme.
        A description or a fee leaking into it removes that bound entirely.
        """
        prompt = build_roster_spec().prompt
        for forbidden in ("TUITION:", "DESCRIPTION:", "APP_FEE:", "CAREERS:"):
            assert forbidden not in prompt

    def test_the_gapfill_prompt_permits_a_university_wide_figure(self):
        """
        Models decline per-programme questions when the source states the fact
        once, institution-wide -- which is the normal way it is published, and
        the reason the field read 0% rather than 'mostly answered'.
        """
        prompt = build_gapfill_spec(["BS CS", "MS DS"]).prompt
        assert "university-wide" in prompt.lower()
        assert "- BS CS" in prompt

    def test_identity_is_a_single_record_over_every_tier(self):
        spec = build_identity_spec()
        assert spec.single is True
        assert set(spec.tiers) == {1, 2, 3, 4}

    def test_the_identity_stage_forbids_rankings(self):
        """
        Rankings come from the registry, never from the model: a numeric world
        rank is the most confidently hallucinated field in the whole payload.
        The prompt has to SAY so -- silence is not suppression.
        """
        prompt = build_identity_spec().prompt.lower()
        assert "do not state any numeric ranking" in prompt
        # And no key exists for one to be written into.
        assert "rank:" not in prompt


# ---------------------------------------------------------------------------
# Name matching
# ---------------------------------------------------------------------------

class TestNameMatching:
    def test_exact_match_ignores_case_and_punctuation(self):
        assert match_roster_name("bs computer science.", ["BS Computer Science"]) == \
            "BS Computer Science"

    def test_a_parenthetical_suffix_does_not_break_the_match(self):
        assert match_roster_name("BS Computer Science (BSCS)", ["BS Computer Science"]) == \
            "BS Computer Science"

    def test_a_different_programme_is_not_matched(self):
        """
        A WRONG match writes one programme's fees onto another, and is invisible.
        An orphan is visible in the report. The fallback is deliberately strict
        for exactly that asymmetry.
        """
        assert match_roster_name("MS Physics", ["BS Computer Science"]) is None

    def test_sibling_programmes_at_different_levels_never_collide(self):
        assert match_roster_name("MS Computer Science", ["BS Computer Science"]) != \
            "BS Computer Science"

    def test_empty_name_matches_nothing(self):
        assert match_roster_name("", ["BS Computer Science"]) is None

    def test_normalisation_is_stable(self):
        assert normalize_program_name("  BS   Computer  Science  ") == "bs computer science"


# ---------------------------------------------------------------------------
# Merging
# ---------------------------------------------------------------------------

class TestRosterMerge:
    def test_detail_fills_blank_roster_fields(self):
        report = ExtractionReport()
        roster = [_program("BS Computer Science")]
        details = [_program("BS Computer Science", tuition_fee="1,416,000", currency="PKR")]

        merged = crawl_runner.merge_detail_into_roster(roster, details, report)
        assert len(merged) == 1
        assert merged[0].tuition_fee == "1,416,000"
        assert merged[0].currency == "PKR"

    def test_an_answered_field_is_never_overwritten(self):
        """
        A later gap-fill ask must not replace a real per-programme fee with a
        university-wide one. First answer wins; the merge only ever fills.
        """
        report = ExtractionReport()
        roster = [_program("BS CS", application_fee="5,000")]
        gapfill = [_program("BS CS", application_fee="2,000")]

        merged = crawl_runner.merge_detail_into_roster(roster, gapfill, report, label="gapfill")
        assert merged[0].application_fee == "5,000"

    def test_the_roster_owns_identity(self):
        """A detail ask cannot rename a programme or move it to another level."""
        report = ExtractionReport()
        roster = [_program("BS Computer Science", "bachelors")]
        details = [_program("BS Computer Science", "masters", duration="2 Years")]

        merged = crawl_runner.merge_detail_into_roster(roster, details, report)
        assert merged[0].degree_level.value == "bachelors"
        assert merged[0].duration == "2 Years"

    def test_an_orphan_is_kept_and_reported(self):
        report = ExtractionReport()
        roster = [_program("BS Computer Science")]
        details = [_program("BS Nursing")]

        merged = crawl_runner.merge_detail_into_roster(roster, details, report)
        assert {p.name for p in merged} == {"BS Computer Science", "BS Nursing"}
        assert any("BS Nursing" in note for note in report.anomalies)

    def test_no_details_leaves_the_roster_untouched(self):
        report = ExtractionReport()
        roster = [_program("BS CS")]
        assert crawl_runner.merge_detail_into_roster(roster, [], report) == roster


class TestCrossLevelArbitration:
    def test_the_programme_name_decides(self):
        report = ExtractionReport()
        programs = [
            _program("MS Data Science", "diploma"),
            _program("MS Data Science", "masters"),
        ]
        out = crawl_runner.arbitrate_cross_level_duplicates(programs, report)
        assert len(out) == 1
        assert out[0].degree_level.value == "masters"
        assert report.anomalies

    def test_entry_level_doctorates_stay_bachelors(self):
        """MBBS and DPT read as doctorates and are bachelors-level entry."""
        report = ExtractionReport()
        programs = [_program("MBBS", "phd"), _program("MBBS", "bachelors")]
        out = crawl_runner.arbitrate_cross_level_duplicates(programs, report)
        assert out[0].degree_level.value == "bachelors"

    def test_an_unreadable_name_keeps_the_first_and_says_so(self):
        """Guessing a level would fabricate a fact a student acts on."""
        report = ExtractionReport()
        programs = [_program("Programme X", "masters"), _program("Programme X", "phd")]
        out = crawl_runner.arbitrate_cross_level_duplicates(programs, report)
        assert len(out) == 1
        assert out[0].degree_level.value == "masters"
        assert any("unreadable" in n for n in report.anomalies)

    def test_a_plain_duplicate_collapses_without_an_anomaly(self):
        report = ExtractionReport()
        programs = [_program("BS CS"), _program("BS CS")]
        out = crawl_runner.arbitrate_cross_level_duplicates(programs, report)
        assert len(out) == 1
        assert not report.anomalies

    def test_order_is_preserved(self):
        report = ExtractionReport()
        programs = [_program("A"), _program("B"), _program("C")]
        out = crawl_runner.arbitrate_cross_level_duplicates(programs, report)
        assert [p.name for p in out] == ["A", "B", "C"]


class TestGapDetection:
    def test_a_programme_missing_either_field_is_a_gap(self):
        programs = [
            _program("complete", application_fee="2,000", application_deadlines=["2026-08-15"]),
            _program("no fee", application_deadlines=["2026-08-15"]),
            _program("no deadline", application_fee="2,000"),
        ]
        assert crawl_runner.programs_missing_fee_or_deadline(programs) == ["no fee", "no deadline"]

    def test_bucketing_splits_by_level(self):
        block = crawl_runner.bucket_by_level([
            _program("a", "bachelors"), _program("b", "masters"), _program("c", "masters"),
        ])
        assert len(block.bachelors) == 1
        assert len(block.masters) == 2
        assert block.phd == []


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

class TestExtractionReport:
    def test_query_index_is_continuous_across_stages(self):
        """
        Each stage used to get a fresh report, so `query_index` restarted at 1
        per block and the audit could not order the asks of one run.
        """
        parent = ExtractionReport()
        parent.queries_used = 3
        sub = ExtractionReport(index_offset=parent.index_offset + parent.queries_used)
        assert sub.index_offset == 3

    def test_merge_accumulates_anomalies_as_well_as_counts(self):
        parent, sub = ExtractionReport(), ExtractionReport()
        sub.queries_used = 2
        sub.note("something worth seeing")
        parent.merge(sub)
        assert parent.queries_used == 2
        assert parent.anomalies == ["something worth seeing"]

    def test_anomalies_do_not_make_a_report_not_ok(self):
        """An anomaly is neither success nor failure; only `failed` gates the run."""
        report = ExtractionReport()
        report.note("an orphan record")
        assert report.ok is True


class TestAnswerAccounting:
    """
    A parse failure and a transport failure are charged differently, and the
    text path has to agree with the JSON path about which is which.
    """

    def test_a_validation_failure_is_an_extraction_error_not_a_transport_error(self):
        """
        By the time validation runs, the ask has already been counted. A raw
        ValidationError escaping here falls through to the transport-error
        branch, which charges the ask a SECOND time, logs it with the wrong
        status, and clears the very text the repair prompt exists to quote.

        Caught by simulating a university whose identity answer named no URLs:
        3 asks were issued and 6 were billed.
        """
        from src.extractor.crawlers.json_repairing import ExtractionError
        from src.extractor.crawlers.notebook_querying import build_identity_spec, parse_answer

        report = ExtractionReport()
        # Parses cleanly as a record, but has no NAME -- MainInfo requires one.
        bad = "@@RECORD\nCITY: Lahore\n@@END"

        with pytest.raises(ExtractionError):
            parse_answer(bad, build_identity_spec(), report)

    def test_one_unreadable_row_does_not_cost_the_other_twenty(self):
        """
        List answers validate per record. A whole-list validation would let a
        single mis-levelled programme fail every other programme with it.
        """
        from src.extractor.crawlers.notebook_querying import build_roster_spec, parse_answer

        report = ExtractionReport()
        text = (
            "@@RECORD\nNAME: BS Computer Science\nLEVEL: bachelors\n@@END\n"
            "@@RECORD\nNAME: Something Odd\nLEVEL: postgraduate-ish\n@@END\n"
            "@@RECORD\nNAME: MS Data Science\nLEVEL: masters\n@@END\n"
        )
        items = parse_answer(text, build_roster_spec(), report)

        assert [p.name for p in items] == ["BS Computer Science", "MS Data Science"]
        assert any("Something Odd" in note for note in report.anomalies)

    def test_an_answer_where_nothing_validates_is_a_failure(self):
        """Leniency is not silence: zero usable records is still an error."""
        from src.extractor.crawlers.json_repairing import ExtractionError
        from src.extractor.crawlers.notebook_querying import build_roster_spec, parse_answer

        report = ExtractionReport()
        with pytest.raises(ExtractionError):
            parse_answer("@@RECORD\nNAME: X\nLEVEL: nonsense\n@@END", build_roster_spec(), report)


class TestQuotaRefusal:
    """
    What happens when the roster arrives and the quota will not pay for it.

    This is a new failure mode the staged plan introduces: the detail asks are
    reserved *after* three asks have already run and a notebook is already
    ingested, so "no budget" can now land mid-university. The behaviour has to
    be defined rather than discovered on a batch night.
    """

    def _client(self, roster_text):
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()

        async def _ask(notebook_id, question, source_ids=None, conversation_id=None):
            if "core identity" in question:
                return MagicMock(answer=(
                    "@@RECORD\nNAME: Uni\nWEBSITE: https://u.edu\n"
                    "DESCRIPTION: d\n@@END"
                ))
            if "faculty and school" in question:
                return MagicMock(answer="@@RECORD\nFACULTY: F\n@@END")
            if "This is an inventory, not a description" in question:
                return MagicMock(answer=roster_text)
            raise AssertionError(f"an ask was issued that the quota refused: {question[:60]}")

        client.chat.ask = AsyncMock(side_effect=_ask)
        return client

    async def test_a_refused_top_up_keeps_the_roster_and_issues_nothing(self, monkeypatch):
        """
        Spending budget the ledger has already refused is the exact failure the
        up-front reservation exists to prevent, just moved later in the run.
        The programmes survive, named and levelled but undescribed, and the
        report says so -- so the university can be re-run rather than shipped as
        though it had no fees.
        """
        monkeypatch.setattr(
            crawl_runner, "exa_find_application_portal", _async_none()
        )

        roster = "".join(
            f"@@RECORD\nNAME: BS Programme {i}\nLEVEL: bachelors\n@@END\n" for i in range(8)
        )
        report = ExtractionReport()

        payload, report = await crawl_runner.extract_university_payload(
            self._client(roster), "nb", "Uni", "u.edu",
            report=report, uni_slug="uni", reserve_more=lambda n: 0,
        )

        # Three asks: identity, faculties, roster. No detail asks were issued --
        # the fake client raises if one is, so this is enforced, not asserted.
        assert report.queries_used == 3
        assert len(payload.programs.bachelors) == 8
        assert all(p.tuition_fee is None for p in payload.programs.bachelors)
        assert any("granted no detail asks" in n for n in report.anomalies)


def _async_none():
    from unittest.mock import AsyncMock
    return AsyncMock(return_value=None)


class TestConversationIsolation:
    """
    Every ask must be a question, not a follow-up turn (C33).

    The SDK extends the notebook's most-recent conversation whenever `ask()` is
    called without a `conversation_id`, so the staged plan's nine asks were nine
    turns of one conversation. On ITU's `s_2` run the roster -- turn four,
    behind a 52 MB aborted turn -- returned 7 bachelors programmes and no
    masters or PhD from a corpus holding 5 MS and 2 PhD programme pages.

    The staged plan made this worse than the suite it replaced: more asks, each
    carrying more accumulated context.
    """

    def _client(self, outcomes):
        from unittest.mock import AsyncMock, MagicMock

        client = MagicMock()
        client.deleted = []
        client.chat = MagicMock()

        async def _ask(notebook_id, question, source_ids=None, conversation_id=None):
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            res = MagicMock()
            res.answer = outcome
            res.conversation_id = f"conv-{len(client.deleted) + 1}"
            return res

        async def _delete(notebook_id, conversation_id):
            client.deleted.append(conversation_id)
            return True

        async def _get_conv(notebook_id):
            return "conv-orphan"

        client.chat.ask = AsyncMock(side_effect=_ask)
        client.chat.delete_conversation = AsyncMock(side_effect=_delete)
        client.chat.get_conversation_id = AsyncMock(side_effect=_get_conv)
        return client

    async def test_a_successful_ask_clears_its_conversation(self):
        from src.extractor.crawlers.notebook_querying import _ask

        client = self._client(["an answer"])
        answer = await _ask(client, "nb-1", "a question")

        assert answer == "an answer"
        assert client.deleted == ["conv-1"], "the next ask would arrive as a follow-up"

    async def test_an_oversized_ask_clears_the_turn_it_left_behind(self):
        """
        The client aborts the read at 52 MB, but the server still recorded a
        turn -- and on `s_2` the two asks that followed an aborted one returned
        the two SHORTEST answers of the nine. Clearing it is what makes the
        single re-ask worth making.
        """
        from notebooklm.exceptions import RPCResponseTooLargeError

        from src.extractor.crawlers.notebook_querying import _ask

        client = self._client([RPCResponseTooLargeError("response exceeded")])
        with pytest.raises(RPCResponseTooLargeError):
            await _ask(client, "nb-1", "a question")

        # No result to read an id from, so it is looked up.
        assert client.deleted == ["conv-orphan"]

    async def test_a_timed_out_ask_also_clears(self):
        from src.extractor.crawlers.notebook_querying import QueryTimeoutError, _ask

        client = self._client([])
        client.chat.ask.side_effect = asyncio.TimeoutError()

        with pytest.raises(QueryTimeoutError):
            await _ask(client, "nb-1", "a question")
        assert client.deleted == ["conv-orphan"]

    async def test_an_explicit_conversation_id_is_never_cleared(self):
        """A deliberate follow-up is the one case where continuity is the point."""
        from src.extractor.crawlers.notebook_querying import _ask

        client = self._client(["an answer"])
        await _ask(client, "nb-1", "a question", conversation_id="conv-mine")
        assert client.deleted == []

    async def test_cleanup_failure_never_costs_a_good_answer(self):
        """
        A conversation that will not delete is a quality problem for the NEXT
        ask, not a reason to discard an answer already in hand.
        """
        from unittest.mock import AsyncMock

        from src.extractor.crawlers.notebook_querying import _ask

        client = self._client(["an answer"])
        client.chat.delete_conversation = AsyncMock(side_effect=RuntimeError("nope"))

        assert await _ask(client, "nb-1", "a question") == "an answer"

    async def test_isolation_can_be_turned_off_for_comparison(self, monkeypatch):
        from src.extractor.crawlers.notebook_querying import _ask

        monkeypatch.setattr(config, "isolate_query_conversations", False)
        client = self._client(["an answer"])
        await _ask(client, "nb-1", "a question")
        assert client.deleted == []

    async def test_every_stage_of_a_real_plan_gets_a_clean_conversation(self):
        """
        The end-to-end property: N asks, N conversations, none inherited.
        """
        roster = "@@RECORD\nNAME: BS CS\nLEVEL: bachelors\n@@END\n"
        answers = [
            "@@RECORD\nNAME: U\nWEBSITE: https://u.edu\nDESCRIPTION: d\n@@END",
            "@@RECORD\nFACULTY: F\n@@END",
            roster,
            "@@RECORD\nNAME: BS CS\nLEVEL: bachelors\nTUITION: 1\nAPP_FEE: 2\n"
            "DEADLINES: 2026-08-01\nDESCRIPTION: d\n@@END",
        ]
        client = self._client(answers)
        crawl_runner.exa_find_application_portal = _async_none()

        report = ExtractionReport()
        await crawl_runner.extract_university_payload(
            client, "nb-1", "U", "u.edu", report=report, uni_slug="u",
        )

        assert report.queries_used == len(client.deleted), (
            "an ask ran on a conversation a previous ask had already used"
        )
        assert len(set(client.deleted)) == len(client.deleted), "a conversation was reused"


class TestRosterContainment:
    """
    The roster ask streamed past 52 MB on BOTH attempts of run `s_3` -- ~50
    million characters for an answer whose correct form is about 1.5 KB, twice,
    deterministically. That is runaway repetition, not a large answer.

    Run `s_2` had looked better only because a polluted conversation made the
    model terse: 7 bachelors and no masters or PhD from a corpus holding 5 MS
    and 2 PhD pages. The short answer was the symptom, not the success.
    """

    def test_the_roster_reads_tier_1_only(self):
        """
        Tier 2 is fee schedules, test patterns and sample papers -- 15 further
        sources on ITU that cannot name a programme the Tier-1 set does not, and
        every one of them is more repetitive corpus to loop over.
        """
        from src.extractor.crawlers.notebook_querying import build_roster_specs
        assert all(s.tiers == (1,) for s in build_roster_specs())

    def test_detail_still_reads_the_wider_set(self):
        """A fee page can describe a programme its own page does not."""
        from src.extractor.crawlers.notebook_querying import build_detail_specs
        specs = build_detail_specs([_program("BS CS")])
        assert specs[0].tiers == (1, 2)

    def test_the_roster_prompt_has_no_open_ended_exhaustiveness_clause(self):
        """
        The removed phrases, each an invitation to keep generating:
        "EVERY ... at every level", "named anywhere in the sources", and
        "Completeness matters more than detail here".
        """
        from src.extractor.crawlers.notebook_querying import build_roster_specs
        prompt = build_roster_specs()[0].prompt
        for banned in ("anywhere in the sources", "Completeness matters more",
                       "List EVERY", "Repeat the whole"):
            assert banned not in prompt, f"{banned!r} is back in the roster prompt"

    def test_the_roster_prompt_states_a_cap_and_a_stop(self):
        from src.extractor.crawlers.notebook_querying import build_roster_specs
        prompt = build_roster_specs()[0].prompt
        assert f"AT MOST {config.roster_max_items} blocks" in prompt
        assert "then stop" in prompt
        assert "exactly once" in prompt

    def test_the_split_fallback_covers_every_degree_level(self, monkeypatch):
        """
        Splitting does NOT reintroduce the partition problem the design exists
        to avoid. These are DISCOVERY asks over the same source set, so no
        programme can fall between them -- only the question is narrowed, and
        the four levels are exhaustive over DegreeLevel by construction.
        """
        from src.utilities.schema import DegreeLevel
        from src.extractor.crawlers.notebook_querying import build_roster_specs

        monkeypatch.setattr(config, "roster_split_by_level", True)
        specs = build_roster_specs()

        assert len(specs) == 4
        keys = {s.key.split(":")[1] for s in specs}
        assert keys == {lvl.value for lvl in DegreeLevel}

    def test_each_split_ask_pins_its_own_level(self, monkeypatch):
        """The level is stated as a fact, so the model never has to choose it."""
        from src.extractor.crawlers.notebook_querying import build_roster_specs

        monkeypatch.setattr(config, "roster_split_by_level", True)
        for spec in build_roster_specs():
            level = spec.key.split(":")[1]
            assert f"LEVEL is {level} for every record" in spec.prompt

    async def test_split_roster_answers_merge_into_one_roster(self, monkeypatch):
        """
        Downstream must not be able to tell which shape produced the roster.
        """
        from unittest.mock import AsyncMock, MagicMock

        monkeypatch.setattr(config, "roster_split_by_level", True)
        monkeypatch.setattr(crawl_runner, "exa_find_application_portal", _async_none())

        by_level = {
            "bachelors": "@@RECORD\nNAME: BS CS\nLEVEL: bachelors\n@@END\n",
            "masters": "@@RECORD\nNAME: MS DS\nLEVEL: masters\n@@END\n",
            "phd": "@@RECORD\nNAME: PhD CS\nLEVEL: phd\n@@END\n",
            "diploma": "",
        }

        client = MagicMock()
        client.chat = MagicMock()

        async def _ask(notebook_id, question, source_ids=None, conversation_id=None):
            answer = ""
            if "core identity" in question:
                answer = ("@@RECORD\nNAME: U\nWEBSITE: https://u.edu\n"
                          "DESCRIPTION: d\n@@END")
            elif "faculty and school" in question:
                answer = "@@RECORD\nFACULTY: F\n@@END"
            elif "This is an inventory, not a description" in question:
                for level, body in by_level.items():
                    if f"LEVEL is {level} for every record" in question:
                        answer = body or "@@RECORD\n@@END"
                        break
            elif "For EACH of the following" in question:
                answer = "@@RECORD\n@@END"
            return MagicMock(answer=answer, conversation_id="c")

        client.chat.ask = AsyncMock(side_effect=_ask)
        client.chat.delete_conversation = AsyncMock(return_value=True)
        client.chat.get_conversation_id = AsyncMock(return_value=None)

        report = ExtractionReport()
        payload, report = await crawl_runner.extract_university_payload(
            client, "nb", "U", "u.edu", report=report, uni_slug="u",
        )
        p = payload.programs
        assert [x.name for x in p.bachelors] == ["BS CS"]
        assert [x.name for x in p.masters] == ["MS DS"]
        assert [x.name for x in p.phd] == ["PhD CS"]
        assert p.diploma == []
