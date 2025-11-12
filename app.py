import os
import re
from typing import List, Dict, Optional
from io import BytesIO
import asyncio
import logging

# Configure logging to show INFO and DEBUG messages
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import pandas as pd
from dotenv import load_dotenv

from services.linkedin_lookup import enrich_crew_with_linkedin, find_linkedin_profile

logger = logging.getLogger(__name__)

# Suppress SSL warnings when using proxies
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Setup custom SSL context for HTTPS through SOCKS5 proxy
import ssl
from requests.adapters import HTTPAdapter
from urllib3.util.ssl_ import create_urllib3_context

class SSLAdapter(HTTPAdapter):
    """Custom adapter to handle SSL verification issues through SOCKS5 proxy"""
    def init_poolmanager(self, *args, **kwargs):
        ctx = create_urllib3_context(ssl_version=ssl.PROTOCOL_TLS_CLIENT)
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        kwargs['ssl_context'] = ctx
        return super().init_poolmanager(*args, **kwargs)

load_dotenv()

app = FastAPI(title="VFX Credits Filter Service")

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Configuration
TMDB_API_KEY = os.getenv("TMDB_API_KEY", "")
TMDB_BASE_URL = os.getenv("TMDB_BASE_URL", "https://api.themoviedb.org/3")

# Proxy configuration for bypassing geo-blocking
# Use socks5h:// to resolve DNS through SOCKS5 proxy
PROXIES = {}
if os.getenv("HTTP_PROXY"):
    proxy_url = os.getenv("HTTP_PROXY")
    # Convert socks5:// to socks5h:// for DNS resolution through proxy
    proxy_url = proxy_url.replace("socks5://", "socks5h://")
    PROXIES["http"] = proxy_url
if os.getenv("HTTPS_PROXY"):
    proxy_url = os.getenv("HTTPS_PROXY")
    # Convert socks5:// to socks5h:// for DNS resolution through proxy
    proxy_url = proxy_url.replace("socks5://", "socks5h://")
    PROXIES["https"] = proxy_url
# Support both uppercase and lowercase
if os.getenv("http_proxy") and not PROXIES.get("http"):
    proxy_url = os.getenv("http_proxy")
    proxy_url = proxy_url.replace("socks5://", "socks5h://")
    PROXIES["http"] = proxy_url
if os.getenv("https_proxy") and not PROXIES.get("https"):
    proxy_url = os.getenv("https_proxy")
    proxy_url = proxy_url.replace("socks5://", "socks5h://")
    PROXIES["https"] = proxy_url

# Create session with SSL adapter for SOCKS5 proxy support
def get_session_with_ssl_adapter():
    """Create a requests session with custom SSL adapter for SOCKS5 proxy"""
    session = requests.Session()
    session.mount('https://', SSLAdapter())
    session.mount('http://', SSLAdapter())
    return session

# ============================================================================
# VFX FILTERING CONFIGURATION - CUSTOMIZE THIS!
# ============================================================================

# Priority 1: Departments to ALWAYS include (all jobs from these departments)
VFX_DEPARTMENTS = {
    "Visual Effects"  # Always include all roles from this department
}

# Priority 2: Specific jobs to include (from any department)
VFX_SPECIFIC_JOBS = {
    # Visual Effects roles
    "Visual Effects Supervisor",
    "Visual Effects Producer",
    "Special Effects Supervisor",
    "Animation Supervisor",
    "Compositing Supervisor",
    "Character Designer",

    # Effects and technical
    "Special Effects Technician",
    "Special Effects Manager",
    "Special Effects Makeup Artist",
    "Executive Visual Effects Producer",

    # Art and Design
    "Production Design",
    "Set Designer",
    "Concept Artist",
    "Prop Designer",
    "Set Decoration",

    # Camera and Lighting
    "Director of Photography",
    "Gaffer",

    # Sound and Music
    "Sound Designer",
    "Sound Effects Editor",
    "Music Editor",
    "Original Music Composer"
}

# Priority 3: Keywords to match in job titles (if not in above lists)
# NOTE: "supervisor" and "producer" are NOT included as they are too generic
# Use VFX_SPECIFIC_JOBS for specific VFX supervisor/producer roles
VFX_KEYWORDS = [
    "vfx",
    "visual effects",
    "animator",
    "composit",
    "effects",
    "digital",
    "cg",
    "3d",
    "tracking",
    "rendering",
    "fx"
]

# Departments to EXCLUDE (even if jobs match VFX keywords)
EXCLUDE_DEPARTMENTS = {
    "Production"  # Exclude general production roles like Producer, Executive Producer
}


class MovieRequest(BaseModel):
    imdb_id: Optional[str] = None
    title: Optional[str] = None


class CrewMember(BaseModel):
    name: str
    job: str
    department: str
    movie_title: str
    imdb_id: str
    tmdb_person_id: Optional[str] = None
    linkedin_url: Optional[str] = None
    linkedin_profile_name: Optional[str] = None
    linkedin_headline: Optional[str] = None
    linkedin_confidence: Optional[float] = None


def extract_imdb_id(url_or_id: str) -> Optional[str]:
    """Extract IMDB ID from URL or return ID if already formatted"""
    if not url_or_id:
        return None

    # Match IMDB ID pattern (tt followed by digits)
    match = re.search(r'(tt\d+)', str(url_or_id))
    if match:
        return match.group(1)
    return None


def is_vfx_job(job: str, department: str) -> bool:
    """Check if job should be included in VFX crew results

    Filtering priority:
    1. If department is in VFX_DEPARTMENTS → include ALL jobs from it
    2. If department is in EXCLUDE_DEPARTMENTS → exclude (even if job matches)
    3. If job is in VFX_SPECIFIC_JOBS → include
    4. If job contains VFX_KEYWORDS → include
    5. Otherwise → exclude
    """
    # Priority 1: Always include from VFX departments
    if department in VFX_DEPARTMENTS:
        return True

    # Priority 2: Always exclude from these departments
    if department in EXCLUDE_DEPARTMENTS:
        return False

    # Priority 3: Check if this specific job is listed
    if job in VFX_SPECIFIC_JOBS:
        return True

    # Priority 4: Check if job contains VFX keywords
    text = f"{job} {department}".lower()
    if any(keyword in text for keyword in VFX_KEYWORDS):
        return True

    return False


def get_tmdb_id_from_imdb(imdb_id: str) -> Optional[Dict]:
    """Convert IMDB ID to TMDb ID, returns dict with id, type, and episode info if applicable"""
    try:
        session = get_session_with_ssl_adapter()
        url = f"{TMDB_BASE_URL}/find/{imdb_id}"
        params = {
            "api_key": TMDB_API_KEY,
            "external_source": "imdb_id"
        }
        logger.info(f"Looking up IMDB ID {imdb_id} in TMDb...")
        response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
        response.raise_for_status()
        data = response.json()

        logger.info(f"TMDb response keys: {list(data.keys())}")
        logger.info(f"  movie_results: {len(data.get('movie_results', []))} items")
        logger.info(f"  tv_episode_results: {len(data.get('tv_episode_results', []))} items")
        logger.info(f"  tv_results: {len(data.get('tv_results', []))} items")

        # Check movie results
        if data.get("movie_results"):
            tmdb_id = str(data["movie_results"][0]["id"])
            logger.info(f"Found as MOVIE: {tmdb_id}")
            return {"id": tmdb_id, "type": "movie"}

        # Check TV episode results (PRIORITY - check before TV series)
        if data.get("tv_episode_results"):
            episode = data["tv_episode_results"][0]
            logger.info(f"Found as TV EPISODE: S{episode['season_number']}E{episode['episode_number']} of show {episode['show_id']}")
            return {
                "id": str(episode["show_id"]),
                "type": "tv_episode",
                "season_number": episode["season_number"],
                "episode_number": episode["episode_number"],
                "episode_name": episode.get("name")
            }

        # Also check TV series results
        if data.get("tv_results"):
            tmdb_id = str(data["tv_results"][0]["id"])
            logger.info(f"Found as TV SERIES: {tmdb_id}")
            return {"id": tmdb_id, "type": "tv"}

        logger.warning(f"No results found in TMDb for IMDB ID {imdb_id}")
        return None
    except Exception as e:
        logger.error(f"Error converting IMDB ID {imdb_id}: {e}", exc_info=True)
        return None


def get_episode_credits(show_id: str, season_number: int, episode_number: int) -> Optional[Dict]:
    """Get credits for a specific TV episode"""
    try:
        session = get_session_with_ssl_adapter()
        url = f"{TMDB_BASE_URL}/tv/{show_id}/season/{season_number}/episode/{episode_number}/credits"
        params = {"api_key": TMDB_API_KEY}
        logger.info(f"Fetching episode credits: S{season_number:02d}E{episode_number:02d}")
        response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error getting episode credits for TV show {show_id} S{season_number}E{episode_number}: {e}")
        return None


def get_movie_credits(tmdb_id: str, media_type: str = "movie") -> Optional[Dict]:
    """Get credits from TMDb (supports both movies and TV shows)"""
    try:
        session = get_session_with_ssl_adapter()
        url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}/credits"
        params = {"api_key": TMDB_API_KEY}
        response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        logger.error(f"Error getting {media_type} credits for TMDb ID {tmdb_id}: {e}")
        return None


def get_movie_details(tmdb_id: str, media_type: str = "movie") -> Optional[Dict]:
    """Get details from TMDb (supports both movies and TV shows)"""
    try:
        session = get_session_with_ssl_adapter()
        url = f"{TMDB_BASE_URL}/{media_type}/{tmdb_id}"
        params = {"api_key": TMDB_API_KEY}
        response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
        response.raise_for_status()
        return response.json()
    except Exception as e:
        print(f"Error getting {media_type} details for TMDb ID {tmdb_id}: {e}")
        return None


def filter_vfx_crew(credits: Dict, movie_title: str, imdb_id: str, apply_filter: bool = False) -> List[CrewMember]:
    """
    Get crew members from credits

    Args:
        credits: Credits data from TMDb API
        movie_title: Title of the movie/show
        imdb_id: IMDB ID
        apply_filter: If True, apply configured VFX filtering. If False, return ALL crew members.
    """
    crew = []

    if not credits or "crew" not in credits:
        return crew

    for member in credits["crew"]:
        job = member.get("job", "")
        department = member.get("department", "")
        name = member.get("name", "")
        tmdb_person_id = member.get("id")
        if tmdb_person_id is not None:
            tmdb_person_id = str(tmdb_person_id)

        # If filtering is disabled (default), include everyone
        if not apply_filter or is_vfx_job(job, department):
            crew.append(CrewMember(
                name=name,
                job=job,
                department=department,
                movie_title=movie_title,
                imdb_id=imdb_id,
                tmdb_person_id=tmdb_person_id
            ))

    return crew


@app.get("/", response_class=HTMLResponse)
async def read_root():
    """Serve the main HTML page"""
    try:
        with open("static/index.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return """
        <html>
            <body>
                <h1>VFX Credits Filter Service</h1>
                <p>Please ensure static/index.html exists</p>
                <p>API Documentation: <a href="/docs">/docs</a></p>
                <p><a href="/crew-browser">Go to Crew Browser →</a></p>
            </body>
        </html>
        """


@app.get("/crew-browser", response_class=HTMLResponse)
async def crew_browser():
    """Serve the crew browser page with interactive filters"""
    try:
        with open("static/crew-browser.html", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        return """
        <html>
            <body>
                <h1>Crew Browser</h1>
                <p>Please ensure static/crew-browser.html exists</p>
            </body>
        </html>
        """


@app.post("/api/upload-csv")
async def upload_csv(file: UploadFile = File(...)):
    """Upload CSV/Excel file with movie data"""
    try:
        content = await file.read()

        # Try to read as CSV or Excel
        try:
            if file.filename.endswith('.xlsx'):
                df = pd.read_excel(BytesIO(content))
            else:
                df = pd.read_csv(BytesIO(content))
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Error reading file: {str(e)}")

        # Process movies
        all_vfx_crew_models: List[CrewMember] = []
        processed_movies = []

        for _, row in df.iterrows():
            # Try to find IMDB ID or title in the row
            imdb_id = None
            title = None

            # Check common column names
            for col in df.columns:
                col_lower = col.lower()
                if 'imdb' in col_lower or 'url' in col_lower or 'link' in col_lower:
                    imdb_id = extract_imdb_id(str(row[col]))
                if 'title' in col_lower or 'name' in col_lower or 'film' in col_lower or 'movie' in col_lower:
                    if pd.notna(row[col]):
                        title = str(row[col])

            if not imdb_id and not title:
                continue

            # Get TMDb ID
            tmdb_info = None
            if imdb_id:
                tmdb_info = get_tmdb_id_from_imdb(imdb_id)

            if not tmdb_info:
                processed_movies.append({
                    "title": title or "Unknown",
                    "imdb_id": imdb_id or "N/A",
                    "status": "not_found",
                    "vfx_crew_count": 0
                })
                continue

            # Get movie/TV details and credits
            media_type = tmdb_info.get("type", "movie")
            tmdb_id = tmdb_info.get("id")
            movie_details = get_movie_details(tmdb_id, media_type)
            credits = get_movie_credits(tmdb_id, media_type)

            if not credits:
                processed_movies.append({
                    "title": title or movie_details.get("title", "Unknown"),
                    "imdb_id": imdb_id or "N/A",
                    "status": "no_credits",
                    "vfx_crew_count": 0
                })
                continue

            # Handle both movies (title) and TV shows (name)
            movie_title = movie_details.get("title") or movie_details.get("name") or title or "Unknown"
            vfx_crew = filter_vfx_crew(credits, movie_title, imdb_id or "N/A")

            all_vfx_crew_models.extend(vfx_crew)

            processed_movies.append({
                "title": movie_title,
                "imdb_id": imdb_id or "N/A",
                "status": "success",
                "vfx_crew_count": len(vfx_crew)
            })

        # LinkedIn enrichment is now done on-demand via /api/linkedin-lookup endpoint
        # to avoid blocking the response and burning API tokens during file upload
        # Users can click the "LinkedIn для всех" button to load LinkedIn profiles

        all_vfx_crew = [member.dict() for member in all_vfx_crew_models]

        return {
            "success": True,
            "processed_movies": processed_movies,
            "vfx_crew": all_vfx_crew,
            "total_vfx_crew": len(all_vfx_crew_models)
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error processing file: {str(e)}")


@app.post("/api/search-movie")
async def search_movie(movie: MovieRequest):
    """Search for a single movie by IMDB ID or title"""
    try:
        tmdb_info = None
        media_type = "movie"

        if movie.imdb_id:
            imdb_id = extract_imdb_id(movie.imdb_id)
            if imdb_id:
                tmdb_info = get_tmdb_id_from_imdb(imdb_id)

        if not tmdb_info and movie.title:
            # Search by title
            session = get_session_with_ssl_adapter()
            url = f"{TMDB_BASE_URL}/search/movie"
            params = {
                "api_key": TMDB_API_KEY,
                "query": movie.title
            }
            response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
            response.raise_for_status()
            results = response.json().get("results", [])

            if results:
                tmdb_info = {"id": str(results[0]["id"]), "type": "movie"}

        if not tmdb_info:
            raise HTTPException(status_code=404, detail="Movie not found")

        # Extract ID and type
        tmdb_id = tmdb_info.get("id")
        media_type = tmdb_info.get("type", "movie")

        # Get movie/TV details
        # For episodes, we still need series details but will fetch episode-specific credits
        if media_type == "tv_episode":
            movie_details = get_movie_details(tmdb_id, "tv")
            season_number = tmdb_info.get("season_number")
            episode_number = tmdb_info.get("episode_number")
            episode_name = tmdb_info.get("episode_name", "")
            logger.info(f"Fetching episode-specific credits for S{season_number}E{episode_number}: {episode_name}")
            credits = get_episode_credits(tmdb_id, season_number, episode_number)
        else:
            movie_details = get_movie_details(tmdb_id, media_type)
            credits = get_movie_credits(tmdb_id, media_type)

        if not credits:
            raise HTTPException(status_code=404, detail="Credits not found")

        # Handle both movies (title) and TV shows (name)
        movie_title = movie_details.get("title") or movie_details.get("name") or movie.title or "Unknown"
        vfx_crew = filter_vfx_crew(credits, movie_title, movie.imdb_id or "N/A")

        # LinkedIn enrichment is now done on-demand via /api/linkedin-lookup endpoint
        # to avoid burning API tokens during initial movie load

        vfx_crew_payload = [member.dict() for member in vfx_crew]

        return {
            "success": True,
            "movie": {
                "title": movie_title,
                "imdb_id": movie.imdb_id or "N/A",
                "tmdb_id": tmdb_id,
                "overview": movie_details.get("overview", ""),
                "release_date": movie_details.get("release_date") or movie_details.get("first_air_date", "")
            },
            "vfx_crew": vfx_crew_payload,
            "total_vfx_crew": len(vfx_crew_payload)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching movie: {str(e)}")


@app.post("/api/linkedin-lookup")
async def lookup_linkedin(name: str, job: str):
    """
    Lookup LinkedIn profile for a specific person
    Returns LinkedIn URL, profile name, headline, and confidence score
    """
    try:
        logger.info(f"LinkedIn lookup endpoint called: name={name}, job={job}")

        if not name or not name.strip():
            raise HTTPException(status_code=400, detail="Name is required")
        if not job or not job.strip():
            raise HTTPException(status_code=400, detail="Job is required")

        logger.info(f"Starting find_linkedin_profile for {name.strip()}")
        profile = await find_linkedin_profile(name.strip(), job.strip())
        logger.info(f"find_linkedin_profile returned: {profile}")

        if profile:
            return {
                "success": True,
                "name": name,
                "job": job,
                "linkedin_url": profile.get("url"),
                "linkedin_profile_name": profile.get("profile_name"),
                "linkedin_headline": profile.get("headline"),
                "linkedin_confidence": profile.get("confidence")
            }
        else:
            return {
                "success": False,
                "name": name,
                "job": job,
                "message": "No LinkedIn profile found"
            }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LinkedIn lookup error for {name}: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Error looking up LinkedIn: {str(e)}")


@app.post("/api/export")
async def export_data(vfx_crew: List[Dict]):
    """Export VFX crew data to Excel"""
    try:
        if not vfx_crew:
            raise HTTPException(status_code=400, detail="No data to export")

        df = pd.DataFrame(vfx_crew)

        # Create Excel file in memory
        output = BytesIO()
        with pd.ExcelWriter(output, engine='openpyxl') as writer:
            df.to_excel(writer, index=False, sheet_name='VFX Crew')

        output.seek(0)

        return StreamingResponse(
            output,
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": "attachment; filename=vfx_crew_export.xlsx"}
        )

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error exporting data: {str(e)}")


@app.post("/api/get-all-crew")
async def get_all_crew(movie: MovieRequest):
    """Get all crew members WITHOUT filtering - for understanding TMDb structure"""
    try:
        tmdb_info = None

        if movie.imdb_id:
            imdb_id = extract_imdb_id(movie.imdb_id)
            if imdb_id:
                tmdb_info = get_tmdb_id_from_imdb(imdb_id)

        if not tmdb_info:
            raise HTTPException(status_code=404, detail="Movie not found")

        tmdb_id = tmdb_info.get("id")
        media_type = tmdb_info.get("type", "movie")

        credits = get_movie_credits(tmdb_id, media_type)

        if not credits or "crew" not in credits:
            raise HTTPException(status_code=404, detail="Credits not found")

        # Group by department and job
        hierarchy = {}
        for member in credits["crew"]:
            dept = member.get("department", "Unknown")
            job = member.get("job", "Unknown")

            if dept not in hierarchy:
                hierarchy[dept] = {}
            if job not in hierarchy[dept]:
                hierarchy[dept][job] = []

            hierarchy[dept][job].append(member.get("name", "Unknown"))

        return {
            "success": True,
            "total_crew": len(credits["crew"]),
            "hierarchy": hierarchy
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.post("/api/collect-jobs-stats")
async def collect_jobs_stats(movies: List[MovieRequest]):
    """Collect all unique jobs/departments from multiple movies to build a dictionary"""
    try:
        all_jobs = {}  # {department: set of jobs}
        processed = 0
        errors = 0

        for movie in movies:
            try:
                tmdb_info = None

                if movie.imdb_id:
                    imdb_id = extract_imdb_id(movie.imdb_id)
                    if imdb_id:
                        tmdb_info = get_tmdb_id_from_imdb(imdb_id)

                if not tmdb_info:
                    errors += 1
                    continue

                tmdb_id = tmdb_info.get("id")
                media_type = tmdb_info.get("type", "movie")
                credits = get_movie_credits(tmdb_id, media_type)

                if not credits or "crew" not in credits:
                    errors += 1
                    continue

                # Collect all unique jobs
                for member in credits["crew"]:
                    dept = member.get("department", "Unknown")
                    job = member.get("job", "Unknown")

                    if dept not in all_jobs:
                        all_jobs[dept] = set()
                    all_jobs[dept].add(job)

                processed += 1

            except Exception as e:
                errors += 1
                print(f"Error processing movie: {e}")
                continue

        # Convert sets to lists for JSON serialization
        result = {dept: sorted(list(jobs)) for dept, jobs in all_jobs.items()}

        return {
            "success": True,
            "processed": processed,
            "errors": errors,
            "unique_jobs_by_department": result,
            "total_unique_departments": len(result),
            "total_unique_jobs": sum(len(jobs) for jobs in result.values())
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error: {str(e)}")


@app.get("/api/health")
async def health_check():
    """Health check endpoint"""
    return {
        "status": "healthy",
        "tmdb_api_configured": bool(TMDB_API_KEY)
    }


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host=os.getenv("HOST", "0.0.0.0"),
        port=int(os.getenv("PORT", 8000))
    )
