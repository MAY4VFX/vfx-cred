import asyncio
import logging
import os
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import httpx


logger = logging.getLogger(__name__)

APIFY_TOKEN = (
    os.getenv("APIFY_TOKEN")
    or os.getenv("APIFY_API_TOKEN")
    or os.getenv("HARVESTAPI_TOKEN")
)
APIFY_ACTOR_ID = os.getenv(
    "APIFY_LINKEDIN_ACTOR_ID", "harvestapi~linkedin-profile-search-by-name"
)
PROFILE_MODE = os.getenv("APIFY_LINKEDIN_PROFILE_MODE", "Full")
MAX_ITEMS = int(os.getenv("APIFY_LINKEDIN_MAX_ITEMS", "5"))
MAX_PAGES = int(os.getenv("APIFY_LINKEDIN_MAX_PAGES", "1"))
CONCURRENCY = int(os.getenv("APIFY_LINKEDIN_CONCURRENCY", "3"))
TIMEOUT = float(os.getenv("APIFY_LINKEDIN_TIMEOUT", "45"))

_SEMAPHORE = asyncio.Semaphore(CONCURRENCY)
_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}


def _cache_key(name: str, job: str, tmdb_person_id: Optional[str]) -> str:
    normalized_name = name.strip().lower()
    normalized_job = job.strip().lower()
    person_part = tmdb_person_id or ""
    return "|".join([person_part, normalized_name, normalized_job])


def _split_name(full_name: str) -> Tuple[str, str]:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0], ""
    return parts[0], " ".join(parts[1:])


def _tokenize_job(job: str) -> List[str]:
    return [
        token
        for token in re.split(r"[^a-z0-9]+", job.lower())
        if len(token) > 2
    ]


def _aggregate_candidate_text(candidate: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for key in ("headline", "summary", "about"):
        value = candidate.get(key)
        if isinstance(value, str):
            chunks.append(value)
    current_positions = candidate.get("currentPosition") or []
    for pos in current_positions:
        if isinstance(pos, dict):
            for key in ("position", "title", "companyName"):
                value = pos.get(key)
                if isinstance(value, str):
                    chunks.append(value)
    experience = candidate.get("experience") or []
    for pos in experience:
        if isinstance(pos, dict):
            for key in ("position", "title", "companyName"):
                value = pos.get(key)
                if isinstance(value, str):
                    chunks.append(value)
    return " ".join(chunks).lower()


def _score_candidate(candidate: Dict[str, Any], job_tokens: List[str]) -> float:
    if not job_tokens:
        return 0.0
    text = _aggregate_candidate_text(candidate)
    if not text:
        return 0.0
    matches = sum(1 for token in job_tokens if token in text)
    if matches == 0:
        return 0.0
    return matches / len(job_tokens)


async def _run_actor(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    url = (
        f"https://api.apify.com/v2/acts/{APIFY_ACTOR_ID}/run-sync-get-dataset-items"
    )
    params = {"token": APIFY_TOKEN}
    async with _SEMAPHORE:
        async with httpx.AsyncClient(timeout=TIMEOUT) as client:
            response = await client.post(url, params=params, json=payload)
            response.raise_for_status()
            data = response.json()
            if isinstance(data, dict):
                # Actor sometimes wraps results in {items: [...]}
                items = data.get("items")
                if isinstance(items, list):
                    return items
                return []
            if isinstance(data, list):
                return data
            return []


async def find_linkedin_profile(
    name: str,
    job: str,
    tmdb_person_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not APIFY_TOKEN:
        logger.debug("APIFY token not configured, skipping LinkedIn lookup")
        return None

    cache_key = _cache_key(name, job, tmdb_person_id)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    first_name, last_name = _split_name(name)
    if not first_name:
        _CACHE[cache_key] = None
        return None

    payload: Dict[str, Any] = {
        "profileScraperMode": PROFILE_MODE,
        "firstName": first_name,
        "lastName": last_name,
        "maxItems": MAX_ITEMS,
        "maxPages": MAX_PAGES,
    }

    try:
        candidates = await _run_actor(payload)
    except httpx.HTTPError as exc:
        logger.warning("LinkedIn lookup failed for %s: %s", name, exc)
        _CACHE[cache_key] = None
        return None

    job_tokens = _tokenize_job(job)
    best_candidate: Optional[Dict[str, Any]] = None
    best_score = 0.0

    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        score = _score_candidate(candidate, job_tokens)
        if score > best_score:
            best_candidate = candidate
            best_score = score

    if best_candidate and best_candidate.get("linkedinUrl"):
        result = {
            "url": best_candidate.get("linkedinUrl"),
            "profile_name": " ".join(
                filter(
                    None,
                    [
                        best_candidate.get("firstName"),
                        best_candidate.get("lastName"),
                    ],
                )
            ).strip()
            or None,
            "headline": best_candidate.get("headline"),
            "confidence": round(best_score, 2) if best_score else None,
        }
        _CACHE[cache_key] = result
        return result

    _CACHE[cache_key] = None
    return None


async def enrich_crew_with_linkedin(crew_members: Iterable[Any]) -> None:
    crew_list: List[Any] = list(crew_members)
    if not crew_list or not APIFY_TOKEN:
        return

    tasks = [
        asyncio.create_task(
            find_linkedin_profile(
                member.name,
                member.job,
                getattr(member, "tmdb_person_id", None),
            )
        )
        for member in crew_list
    ]

    results = await asyncio.gather(*tasks, return_exceptions=True)

    for member, result in zip(crew_list, results):
        if isinstance(result, Exception):
            logger.warning("LinkedIn lookup error for %s: %s", member.name, result)
            continue
        if not result:
            continue
        member.linkedin_url = result.get("url")
        member.linkedin_profile_name = result.get("profile_name")
        member.linkedin_headline = result.get("headline")
        member.linkedin_confidence = result.get("confidence")
