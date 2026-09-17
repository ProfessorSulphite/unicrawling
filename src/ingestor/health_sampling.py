"""
Pre-flight link health sampling (plan section 6.1).

Uploading a full link batch to NotebookLM costs a notebook slot and, downstream,
the university's whole query budget. When a university's links are mostly dead --
a moved site, an expired certificate, a bot wall -- that spend buys nothing and
is only discovered after the notebook has been provisioned and queried.

This module probes a small random sample first and returns a verdict, so the
caller can abandon the university before any of that is spent.

The probe is *injected* rather than imported. source_management owns the real
probe and also consumes this module, so importing it here would be circular; it
also means tests supply a plain function instead of monkeypatching a module
attribute, which is the failure mode that silently disarmed the pre-flight test
in C12.
"""
import asyncio
import logging
import math
import random
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence

from src.config import config

logger = logging.getLogger("Ingest.health")

# A probe takes a URL and answers "would this link survive ingestion?".
LinkProbe = Callable[[str], Awaitable[bool]]


@dataclass
class HealthReport:
    """
    Verdict on one university's link set, plus the evidence behind it.

    `results` is retained so the caller can reuse the sampled verdicts instead of
    re-probing those URLs during the full pre-flight pass -- the sample is drawn
    from the same list that pass will walk.
    """
    sampled: List[str] = field(default_factory=list)
    passed: List[str] = field(default_factory=list)
    failed: List[str] = field(default_factory=list)
    results: Dict[str, bool] = field(default_factory=dict)
    healthy: bool = True
    reason: str = ""
    skipped_check: bool = False

    @property
    def sample_size(self) -> int:
        return len(self.sampled)

    @property
    def pass_ratio(self) -> float:
        """Share of the sample that passed. An empty sample is vacuously healthy."""
        if not self.sampled:
            return 1.0
        return len(self.passed) / len(self.sampled)


def _asks_per_university() -> int:
    """
    The ask budget this university would have cost, under whichever plan is active.

    The staged plan's ceiling is `max_queries_per_university`, not
    `queries_per_university` -- that field now sizes only the legacy JSON suite,
    so quoting it here would understate the saving by half.
    """
    if getattr(config, "response_format", "text") == "text":
        return int(getattr(config, "max_queries_per_university", 14))
    return int(config.queries_per_university)


def resolve_sample_size(
    population: int,
    ratio: Optional[float] = None,
    minimum: Optional[int] = None,
) -> int:
    """
    How many links to probe out of `population`.

    `ceil(ratio * population)` floored at `minimum`, then clamped to the
    population itself -- a university with 3 links gets all 3 probed rather than
    the configured floor of 5, which would be impossible to satisfy.
    """
    if population <= 0:
        return 0
    ratio = config.health_check_sample_ratio if ratio is None else ratio
    minimum = config.health_check_min_sample if minimum is None else minimum
    proportional = math.ceil(max(0.0, ratio) * population)
    return max(1, min(population, max(minimum, proportional)))


def select_health_sample(
    records: Sequence[Dict[str, Any]],
    ratio: Optional[float] = None,
    minimum: Optional[int] = None,
    rng: Optional[random.Random] = None,
) -> List[Dict[str, Any]]:
    """
    Draw the random sample.

    Random rather than head-of-list on purpose: link lists arrive tier-ordered,
    so the first N are the highest-scoring pages and are systematically healthier
    than the batch as a whole. Sampling the head would clear a university whose
    long tail is entirely dead.

    Documents are excluded from the population entirely (C32). This sample
    decides whether a whole university is worth ingesting, and the question it
    is asking is whether the SITE serves pages to our HTTP client. A PDF on a
    file server that answers HEAD with 403 -- which is common, and which
    NotebookLM's own fetcher is unaffected by -- would answer "no" to a question
    it was never asked, and condemn every page on the site with it.
    """
    population = [r for r in records if not r.get("is_document")]
    if not population:
        population = list(records)

    size = resolve_sample_size(len(population), ratio=ratio, minimum=minimum)
    if size >= len(population):
        return list(population)
    chooser = rng or random
    return chooser.sample(population, size)


async def run_health_check(
    records: Sequence[Dict[str, Any]],
    probe: LinkProbe,
    ratio: Optional[float] = None,
    minimum: Optional[int] = None,
    min_pass_ratio: Optional[float] = None,
    concurrency: Optional[int] = None,
    rng: Optional[random.Random] = None,
    label: str = "",
    recheck_probe: Optional[LinkProbe] = None,
) -> HealthReport:
    """
    Probe a random sample of `records` and decide whether the batch is worth ingesting.

    Returns a HealthReport; raising is deliberately avoided so the caller keeps
    one return path and can record the verdict either way. A probe that itself
    raises counts as a failed link, not a failed check -- one unreachable host
    must not abort the sample.

    `recheck_probe` is a second, patient probe applied only to links the first
    pass failed, and only when the verdict would otherwise be "unhealthy". The
    stake here is the whole university: NUST scored 0/8 on 2026-09-05 and was
    dropped from the batch on a single round of 5-second probes. Re-probing is
    bounded (one extra pass over the failures alone) and cannot make a verdict
    worse -- it only ever promotes failures to passes.
    """
    report = HealthReport()

    if not records:
        report.reason = "no links to check"
        return report

    if not config.health_check_enabled:
        report.skipped_check = True
        report.reason = "health check disabled by config"
        return report

    sample = select_health_sample(records, ratio=ratio, minimum=minimum, rng=rng)
    report.sampled = [rec["url"] for rec in sample]

    sem = asyncio.Semaphore(concurrency or config.preflight_concurrency)

    async def _probe(url: str) -> bool:
        async with sem:
            try:
                return bool(await probe(url))
            except Exception as e:
                logger.debug(f"health probe raised for {url}: {e}")
                return False

    verdicts = await asyncio.gather(*(_probe(url) for url in report.sampled))
    for url, ok in zip(report.sampled, verdicts):
        report.results[url] = ok
        (report.passed if ok else report.failed).append(url)

    threshold = (
        config.health_check_min_pass_ratio if min_pass_ratio is None else min_pass_ratio
    )
    report.healthy = report.pass_ratio >= threshold

    # Second chance, spent only when the university is about to be abandoned.
    if not report.healthy and report.failed and config.health_check_recheck_failures:
        retry_probe = recheck_probe or probe

        async def _reprobe(url: str) -> bool:
            async with sem:
                try:
                    return bool(await retry_probe(url))
                except Exception as e:
                    logger.debug(f"health re-probe raised for {url}: {e}")
                    return False

        retried = list(report.failed)
        logger.info(
            f"{f'[{label}] ' if label else ''}Link health check failed on first pass "
            f"({report.pass_ratio:.0%}); re-probing {len(retried)} failed link(s) patiently."
        )
        second = await asyncio.gather(*(_reprobe(url) for url in retried))
        recovered = [url for url, ok in zip(retried, second) if ok]
        if recovered:
            for url in recovered:
                report.results[url] = True
                report.passed.append(url)
                report.failed.remove(url)
            report.healthy = report.pass_ratio >= threshold
            logger.info(
                f"{f'[{label}] ' if label else ''}Re-probe recovered "
                f"{len(recovered)}/{len(retried)} link(s)."
            )

    prefix = f"[{label}] " if label else ""
    report.reason = (
        f"{len(report.passed)}/{report.sample_size} sampled links reachable "
        f"({report.pass_ratio:.0%}), threshold {threshold:.0%}"
    )
    if report.healthy:
        logger.info(f"{prefix}Link health check passed: {report.reason}.")
    else:
        logger.warning(
            f"{prefix}Link health check FAILED: {report.reason}. "
            f"Skipping this university rather than spending a notebook and "
            f"up to {_asks_per_university()} queries on it."
        )
    return report
