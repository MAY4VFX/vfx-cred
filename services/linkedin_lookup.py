from __future__ import annotations

import asyncio
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

try:  # pragma: no cover - внешняя зависимость
    from linkdapi import AsyncLinkdAPI
    import httpx
except ImportError:  # pragma: no cover - опциональная зависимость
    AsyncLinkdAPI = None  # type: ignore[assignment]
    httpx = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

LINKDAPI_API_KEY = os.getenv("LINKDAPI_API_KEY")
LINKDAPI_MAX_RESULTS = int(os.getenv("LINKDAPI_MAX_RESULTS", "3"))
LINKDAPI_CONCURRENCY = max(1, int(os.getenv("LINKDAPI_CONCURRENCY", "1")))
LINKDAPI_REQUEST_INTERVAL = max(0.0, float(os.getenv("LINKDAPI_REQUEST_INTERVAL", "0.5")))
LINKDAPI_MAX_RETRIES = max(1, int(os.getenv("LINKDAPI_MAX_RETRIES", "3")))
LINKDAPI_RETRY_DELAY = max(0.1, float(os.getenv("LINKDAPI_RETRY_DELAY", "1.0")))
LINKDAPI_TIMEOUT = max(1.0, float(os.getenv("LINKDAPI_TIMEOUT", "30.0")))

_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}
_SEMAPHORE = asyncio.Semaphore(LINKDAPI_CONCURRENCY)
_CLIENT: Optional[AsyncLinkdAPI] = None
_CLIENT_INITIALIZED = False
_CLIENT_LOCK = asyncio.Lock()
_THROTTLE_LOCK = asyncio.Lock()
_LAST_REQUEST_TS = 0.0


def _cache_key(name: str, job: str, tmdb_person_id: Optional[str]) -> str:
    normalized_name = name.strip().lower()
    normalized_job = job.strip().lower()
    person_part = tmdb_person_id or ""
    return "|".join([person_part, normalized_name, normalized_job])


def _split_name(full_name: str) -> Dict[str, Optional[str]]:
    parts = [part for part in re.split(r"\s+", full_name.strip()) if part]
    if not parts:
        return {"first": None, "last": None}
    if len(parts) == 1:
        return {"first": parts[0], "last": None}
    return {"first": parts[0], "last": " ".join(parts[1:])}


def _tokenize_job(job: str) -> List[str]:
    normalized = (job or "").lower()
    return [token for token in re.split(r"[^a-z0-9]+", normalized) if len(token) > 2]


def _safe_get_list(container: Any, keys: Sequence[str]) -> List[Any]:
    if not isinstance(container, dict):
        return []
    for key in keys:
        value = container.get(key)
        if isinstance(value, list):
            return value
        if isinstance(value, dict):
            # иногда LinkdAPI возвращает вложенные структуры вида {"items": [...], ...}
            nested = _safe_get_list(value, keys)
            if nested:
                return nested
    return []


async def _get_client() -> Optional[AsyncLinkdAPI]:
    global _CLIENT, _CLIENT_INITIALIZED
    if AsyncLinkdAPI is None:
        if not _CLIENT_INITIALIZED:
            logger.warning("Библиотека linkdapi не установлена, LinkedIn-обогащение отключено")
            _CLIENT_INITIALIZED = True
        return None

    if not LINKDAPI_API_KEY:
        if not _CLIENT_INITIALIZED:
            logger.warning("Переменная окружения LINKDAPI_API_KEY не задана — пропускаем LinkedIn-обогащение")
            _CLIENT_INITIALIZED = True
        return None

    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            logger.debug("LinkdAPI клиент уже инициализирован")
            return _CLIENT
        if _CLIENT_INITIALIZED and _CLIENT is None:
            logger.debug("LinkdAPI клиент ранее был отключён")
            return None
        try:
            logger.info("Инициализация LinkdAPI клиента...")

            # Create httpx client explicitly without proxy (env vars will be ignored if we override limits)
            # LinkdAPI doesn't work properly with SOCKS proxy environment variables
            if httpx is not None:
                # Create transport with no proxy by using limits to force direct connection
                http_client = httpx.AsyncClient(
                    timeout=LINKDAPI_TIMEOUT,
                    limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
                    verify=False,  # Skip SSL verification since we trust the API host
                )
                # Patch the client to not use environment proxies
                http_client._mounts = {}  # Clear any proxy mounts

                _CLIENT = AsyncLinkdAPI(
                    LINKDAPI_API_KEY,
                    client=http_client,
                    timeout=LINKDAPI_TIMEOUT,
                    max_retries=LINKDAPI_MAX_RETRIES,
                    retry_delay=LINKDAPI_RETRY_DELAY,
                )
            else:
                _CLIENT = AsyncLinkdAPI(
                    LINKDAPI_API_KEY,
                    timeout=LINKDAPI_TIMEOUT,
                    max_retries=LINKDAPI_MAX_RETRIES,
                    retry_delay=LINKDAPI_RETRY_DELAY,
                )
            logger.info("LinkdAPI клиент успешно инициализирован")

        except Exception as exc:  # pragma: no cover - внешняя зависимость
            logger.warning("Не удалось инициализировать LinkdAPI-клиент: %s", exc)
            _CLIENT = None
        _CLIENT_INITIALIZED = True
        return _CLIENT


async def _throttled_call(method, *args, **kwargs):
    global _LAST_REQUEST_TS
    if LINKDAPI_REQUEST_INTERVAL <= 0:
        return await method(*args, **kwargs)

    async with _THROTTLE_LOCK:
        now = time.monotonic()
        wait_for = LINKDAPI_REQUEST_INTERVAL - (now - _LAST_REQUEST_TS)
        if wait_for > 0:
            await asyncio.sleep(wait_for)
        result = await method(*args, **kwargs)
        _LAST_REQUEST_TS = time.monotonic()
        return result


def _candidate_text(candidate: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("headline", "occupation", "summary", "title", "description"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    location = candidate.get("location") or candidate.get("locationName")
    if isinstance(location, str):
        parts.append(location)
    return " ".join(parts).lower()


def _profile_text(profile: Dict[str, Any]) -> str:
    parts: List[str] = []
    for key in ("headline", "summary", "about", "industryName", "industry", "fullName"):
        value = profile.get(key)
        if isinstance(value, str) and value:
            parts.append(value)
    experience = profile.get("experience") or profile.get("positions")
    if isinstance(experience, dict):
        experience = experience.get("positions") or experience.get("items")
    if isinstance(experience, list):
        for item in experience:
            if isinstance(item, dict):
                for key in ("title", "companyName", "description"):
                    value = item.get(key)
                    if isinstance(value, str) and value:
                        parts.append(value)
    return " ".join(parts).lower()


def _score_text(text: str, tokens: Sequence[str]) -> float:
    if not text or not tokens:
        return 0.0
    matches = sum(1 for token in tokens if token in text)
    if matches == 0:
        return 0.0
    return matches / len(tokens)


def _extract_public_identifier(candidate: Dict[str, Any]) -> Optional[str]:
    for key in ("publicIdentifier", "public_identifier", "username", "profileId", "id"):
        value = candidate.get(key)
        if isinstance(value, str) and value:
            return value.strip()
    profile_url = candidate.get("profileUrl") or candidate.get("url")
    if isinstance(profile_url, str) and "linkedin.com/in/" in profile_url:
        slug = profile_url.split("linkedin.com/in/")[-1]
        slug = slug.split("?")[0].strip("/")
        if slug:
            return slug
    return None


def _candidate_name(candidate: Dict[str, Any]) -> Optional[str]:
    full_name = candidate.get("fullName") or candidate.get("name")
    if isinstance(full_name, str) and full_name:
        return full_name
    first = candidate.get("firstName")
    last = candidate.get("lastName")
    parts = [part for part in (first, last) if isinstance(part, str) and part]
    return " ".join(parts) if parts else None


async def _lookup_profile(client: AsyncLinkdAPI, name: str, job: str) -> Optional[Dict[str, Any]]:
    name_parts = _split_name(name)
    search_kwargs: Dict[str, Any] = {
        "keyword": f"{name} {job}".strip(),
        "first_name": name_parts["first"],
        "last_name": name_parts["last"],
        "title": job or None,
    }

    # удалить пустые значения
    search_kwargs = {k: v for k, v in search_kwargs.items() if v}

    try:
        search_response = await _throttled_call(client.search_people, **search_kwargs)
    except Exception as exc:  # pragma: no cover - внешняя зависимость
        logger.warning("LinkdAPI: ошибка поиска для %s — %s", name, exc)
        return None

    candidates: List[Dict[str, Any]] = []
    if isinstance(search_response, dict):
        data = search_response.get("data")
        if isinstance(data, list):
            candidates = data
        elif isinstance(data, dict):
            candidates = _safe_get_list(data, ("profiles", "items", "elements", "results"))
    elif isinstance(search_response, list):
        candidates = search_response

    if not candidates:
        return None

    job_tokens = _tokenize_job(job)
    best_candidate: Optional[Dict[str, Any]] = None
    best_score = 0.0

    for candidate in candidates[:LINKDAPI_MAX_RESULTS]:
        if not isinstance(candidate, dict):
            continue
        score = _score_text(_candidate_text(candidate), job_tokens)
        if score > best_score or best_candidate is None:
            best_candidate = candidate
            best_score = score

    if best_candidate is None:
        return None

    public_id = _extract_public_identifier(best_candidate)
    profile_details: Optional[Dict[str, Any]] = None

    if public_id:
        try:
            overview_response = await _throttled_call(client.get_profile_overview, public_id)
        except Exception as exc:  # pragma: no cover - внешняя зависимость
            logger.debug("LinkdAPI: не удалось получить профиль %s: %s", public_id, exc)
        else:
            if isinstance(overview_response, dict):
                profile_details = overview_response.get("data") if isinstance(overview_response.get("data"), dict) else None
                if profile_details:
                    detail_score = _score_text(_profile_text(profile_details), job_tokens)
                    if detail_score > best_score:
                        best_score = detail_score

    url = f"https://www.linkedin.com/in/{public_id}" if public_id else None
    headline = None
    if profile_details and isinstance(profile_details.get("headline"), str):
        headline = profile_details.get("headline")
    elif isinstance(best_candidate.get("headline"), str):
        headline = best_candidate.get("headline")

    profile_name = None
    if profile_details and isinstance(profile_details.get("fullName"), str):
        profile_name = profile_details.get("fullName")
    else:
        profile_name = _candidate_name(best_candidate)

    confidence = round(best_score, 2) if best_score > 0 else None

    return {
        "url": url,
        "profile_name": profile_name,
        "headline": headline,
        "confidence": confidence,
    }


async def find_linkedin_profile(
    name: str,
    job: str,
    tmdb_person_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    cache_key = _cache_key(name, job, tmdb_person_id)
    if cache_key in _CACHE:
        return _CACHE[cache_key]

    client = await _get_client()
    if client is None:
        _CACHE[cache_key] = None
        return None

    async with _SEMAPHORE:
        result = await _lookup_profile(client, name, job)

    _CACHE[cache_key] = result
    return result


async def enrich_crew_with_linkedin(crew_members: Iterable[Any]) -> None:
    crew_list: List[Any] = list(crew_members)
    if not crew_list:
        return

    client = await _get_client()
    if client is None:
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
            logger.warning("Ошибка LinkdAPI для %s: %s", member.name, result)
            continue
        if not result:
            continue
        member.linkedin_url = result.get("url")
        member.linkedin_profile_name = result.get("profile_name")
        member.linkedin_headline = result.get("headline")
        member.linkedin_confidence = result.get("confidence")
