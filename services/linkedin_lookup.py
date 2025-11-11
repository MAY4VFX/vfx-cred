from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, Iterable, List, Optional

try:
    from linkedin_api import Linkedin
except ImportError:  # pragma: no cover - optional dependency
    Linkedin = None  # type: ignore[assignment]


logger = logging.getLogger(__name__)

LINKEDIN_USERNAME = os.getenv("LINKEDIN_USERNAME")
LINKEDIN_PASSWORD = os.getenv("LINKEDIN_PASSWORD")
LINKEDIN_COOKIES_PATH = os.getenv("LINKEDIN_COOKIES_PATH")
LINKEDIN_COOKIES_JSON = os.getenv("LINKEDIN_COOKIES_JSON")
LINKEDIN_PROXY = os.getenv("LINKEDIN_PROXY")
LINKEDIN_HTTP_PROXY = os.getenv("LINKEDIN_HTTP_PROXY")
LINKEDIN_HTTPS_PROXY = os.getenv("LINKEDIN_HTTPS_PROXY")
LINKEDIN_MAX_RESULTS = int(os.getenv("LINKEDIN_MAX_RESULTS", "3"))
LINKEDIN_REQUEST_INTERVAL = float(os.getenv("LINKEDIN_REQUEST_INTERVAL", "1.5"))
LINKEDIN_CONCURRENCY = max(1, int(os.getenv("LINKEDIN_CONCURRENCY", "1")))

_SEMAPHORE = asyncio.Semaphore(LINKEDIN_CONCURRENCY)
_CACHE: Dict[str, Optional[Dict[str, Any]]] = {}
_CLIENT: Optional[Linkedin] = None
_CLIENT_INITIALIZED = False
_CLIENT_LOCK = asyncio.Lock()
_THREAD_LOCK = threading.Lock()
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
    return [
        token
        for token in re.split(r"[^a-z0-9]+", normalized)
        if len(token) > 2
    ]


def _load_cookies() -> Optional[Dict[str, Any]]:
    if LINKEDIN_COOKIES_JSON:
        try:
            return json.loads(LINKEDIN_COOKIES_JSON)
        except json.JSONDecodeError:
            logger.warning("Не удалось распарсить LINKEDIN_COOKIES_JSON")
    if LINKEDIN_COOKIES_PATH:
        try:
            with open(LINKEDIN_COOKIES_PATH, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except FileNotFoundError:
            logger.warning("Файл с cookie LinkedIn не найден: %s", LINKEDIN_COOKIES_PATH)
        except json.JSONDecodeError:
            logger.warning("Не удалось распарсить cookie из файла %s", LINKEDIN_COOKIES_PATH)
    return None


def _build_proxies() -> Dict[str, str]:
    proxies: Dict[str, str] = {}
    if LINKEDIN_PROXY:
        proxies["http"] = LINKEDIN_PROXY
        proxies["https"] = LINKEDIN_PROXY
    if LINKEDIN_HTTP_PROXY:
        proxies["http"] = LINKEDIN_HTTP_PROXY
    if LINKEDIN_HTTPS_PROXY:
        proxies["https"] = LINKEDIN_HTTPS_PROXY
    return proxies


def _extract_localized(value: Any) -> Optional[str]:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        localized = value.get("localized")
        if isinstance(localized, dict) and localized:
            for preferred_key in ("ru_RU", "en_US"):
                if preferred_key in localized:
                    return localized[preferred_key]
            return next(iter(localized.values()))
        if "text" in value and isinstance(value["text"], str):
            return value["text"]
    return None


def _collect_profile_text(profile: Dict[str, Any]) -> str:
    chunks: List[str] = []
    for key in ("headline", "summary", "industryName"):
        value = _extract_localized(profile.get(key))
        if value:
            chunks.append(value)
    experience = profile.get("experience") or []
    for position in experience:
        if isinstance(position, dict):
            for key in ("title", "description", "companyName", "companyTitle"):
                value = position.get(key)
                if isinstance(value, dict):
                    value = _extract_localized(value)
                if isinstance(value, str) and value:
                    chunks.append(value)
    return " ".join(chunks).lower()


def _score_profile(profile: Dict[str, Any], job_tokens: List[str]) -> float:
    if not job_tokens:
        return 0.0
    text = _collect_profile_text(profile)
    if not text:
        return 0.0
    matches = sum(1 for token in job_tokens if token in text)
    if matches == 0:
        return 0.0
    return matches / len(job_tokens)


def _profile_name(profile: Dict[str, Any]) -> Optional[str]:
    first = _extract_localized(profile.get("firstName"))
    last = _extract_localized(profile.get("lastName"))
    name_parts = [part for part in (first, last) if part]
    return " ".join(name_parts) if name_parts else None


def _create_client() -> Optional[Linkedin]:
    if Linkedin is None:
        logger.warning("Библиотека linkedin-api не установлена, обогащение отключено")
        return None

    cookies = _load_cookies()
    username = LINKEDIN_USERNAME or ""
    password = LINKEDIN_PASSWORD or ""
    proxies = _build_proxies()

    # If we have cookies, use them. Otherwise require username/password
    if not cookies and (not username or not password):
        logger.warning("Не заданы учетные данные LinkedIn — пропускаем обогащение")
        return None

    try:
        logger.debug(f"Initializing LinkedIn client with username={username if not cookies else 'cookies'}, proxies={bool(proxies)}")

        # If cookies are provided, use only cookies (no password needed)
        # If cookies are not provided, use username/password to authenticate
        if cookies:
            # Use cookies only - disable authentication since we have valid cookies
            logger.info(f"Initializing LinkedIn client with cookies-only mode")
            client = Linkedin(
                "", "",  # Empty username/password when using cookies
                authenticate=False,  # Skip authentication if cookies are provided
                cookies=cookies,
                proxies=proxies if proxies else {},
            )
            logger.info("LinkedIn client successfully initialized with cookies")
            return client
        else:
            # Use username/password authentication
            logger.info(f"Initializing LinkedIn client with username/password authentication")
            return Linkedin(
                username,
                password,
                proxies=proxies if proxies else {},
            )
    except Exception as exc:  # pragma: no cover - внешняя зависимость
        logger.warning(f"Ошибка инициализации клиента LinkedIn: {type(exc).__name__}: {exc}")
        import traceback
        logger.debug(f"LinkedIn init traceback: {traceback.format_exc()}")
        return None


async def _get_client() -> Optional[Linkedin]:
    global _CLIENT, _CLIENT_INITIALIZED
    async with _CLIENT_LOCK:
        if _CLIENT is not None:
            return _CLIENT
        if _CLIENT_INITIALIZED and _CLIENT is None:
            return None
        _CLIENT = await asyncio.to_thread(_create_client)
        _CLIENT_INITIALIZED = True
        return _CLIENT


def _call_linkedin(func, *args, **kwargs):
    global _LAST_REQUEST_TS
    with _THREAD_LOCK:
        now = time.monotonic()
        wait_for = LINKEDIN_REQUEST_INTERVAL - (now - _LAST_REQUEST_TS)
        if wait_for > 0:
            time.sleep(wait_for)
        result = func(*args, **kwargs)
        _LAST_REQUEST_TS = time.monotonic()
        # Debug: log raw response
        logger.debug(f"LinkedIn API response type: {type(result)}, value: {result}")
        return result


def _lookup_profile_sync(client: Linkedin, name: str, job: str) -> Optional[Dict[str, Any]]:
    name_parts = _split_name(name)
    if not name_parts["first"] and not name_parts["last"]:
        return None

    search_kwargs: Dict[str, Any] = {
        "keywords": f"{name} {job}".strip(),
        "include_private_profiles": False,
        "limit": LINKEDIN_MAX_RESULTS,
    }
    if name_parts["first"]:
        search_kwargs["keyword_first_name"] = name_parts["first"]
    if name_parts["last"]:
        search_kwargs["keyword_last_name"] = name_parts["last"]
    if job:
        search_kwargs["keyword_title"] = job

    try:
        candidates = _call_linkedin(client.search_people, **search_kwargs)
    except Exception as exc:  # pragma: no cover - внешняя зависимость
        logger.warning("Поиск LinkedIn для %s завершился с ошибкой: %s", name, exc)
        logger.debug(f"LinkedIn search error details: {type(exc).__name__}: {exc}", exc_info=True)
        return None

    if not candidates:
        return None

    job_tokens = _tokenize_job(job)
    best_candidate: Optional[Dict[str, Any]] = None
    best_profile: Optional[Dict[str, Any]] = None
    best_score = 0.0

    for candidate in candidates:
        public_id = candidate.get("public_id")
        if not public_id:
            continue
        try:
            profile = _call_linkedin(client.get_profile, public_id=public_id)
        except Exception as exc:  # pragma: no cover - внешняя зависимость
            logger.debug("Не удалось получить профиль %s: %s", public_id, exc)
            continue
        if not isinstance(profile, dict) or not profile:
            continue
        score = _score_profile(profile, job_tokens)
        if score > best_score or best_candidate is None:
            best_candidate = candidate
            best_profile = profile
            best_score = score

    if best_candidate and best_profile:
        public_id = best_candidate.get("public_id")
        url = f"https://www.linkedin.com/in/{public_id}" if public_id else None
        headline = _extract_localized(best_profile.get("headline"))
        confidence = round(best_score, 2) if best_score > 0 else None
        return {
            "url": url,
            "profile_name": _profile_name(best_profile),
            "headline": headline,
            "confidence": confidence,
        }

    return None


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
        result = await asyncio.to_thread(_lookup_profile_sync, client, name, job)

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
            logger.warning("Ошибка LinkedIn для %s: %s", member.name, result)
            continue
        if not result:
            continue
        member.linkedin_url = result.get("url")
        member.linkedin_profile_name = result.get("profile_name")
        member.linkedin_headline = result.get("headline")
        member.linkedin_confidence = result.get("confidence")
