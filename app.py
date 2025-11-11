import os
import re
import asyncio
from typing import List, Dict, Optional
from io import BytesIO

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import StreamingResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
import requests
import pandas as pd
from dotenv import load_dotenv

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

# VFX-related job titles to filter
VFX_JOBS = [
    "vfx",
    "visual effects",
    "supervisor",
    "producer",
    "coordinator",
    "composit",
    "animator",
    "cg",
    "3d",
    "effects",
    "digital",
    "matte painter",
    "rotoscop",
    "tracking",
    "lighting",
    "rendering",
    "fx"
]


class MovieRequest(BaseModel):
    imdb_id: Optional[str] = None
    title: Optional[str] = None


class CrewMember(BaseModel):
    name: str
    job: str
    department: str
    movie_title: str
    imdb_id: str


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
    """Check if job/department is VFX-related"""
    text = f"{job} {department}".lower()
    return any(vfx_keyword in text for vfx_keyword in VFX_JOBS)


def get_tmdb_id_from_imdb(imdb_id: str) -> Optional[Dict]:
    """Convert IMDB ID to TMDb ID, returns dict with id and type (movie/tv)"""
    try:
        session = get_session_with_ssl_adapter()
        url = f"{TMDB_BASE_URL}/find/{imdb_id}"
        params = {
            "api_key": TMDB_API_KEY,
            "external_source": "imdb_id"
        }
        response = session.get(url, params=params, proxies=PROXIES if PROXIES else None, timeout=10)
        response.raise_for_status()
        data = response.json()

        # Check movie results
        if data.get("movie_results"):
            tmdb_id = str(data["movie_results"][0]["id"])
            return {"id": tmdb_id, "type": "movie"}

        # Also check TV results
        if data.get("tv_results"):
            tmdb_id = str(data["tv_results"][0]["id"])
            return {"id": tmdb_id, "type": "tv"}

        return None
    except Exception as e:
        print(f"Error converting IMDB ID {imdb_id}: {e}")
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
        print(f"Error getting {media_type} credits for TMDb ID {tmdb_id}: {e}")
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


def filter_vfx_crew(credits: Dict, movie_title: str, imdb_id: str) -> List[CrewMember]:
    """Filter VFX crew members from credits"""
    vfx_crew = []

    if not credits or "crew" not in credits:
        return vfx_crew

    for member in credits["crew"]:
        job = member.get("job", "")
        department = member.get("department", "")
        name = member.get("name", "")

        # Only include VFX and Visual Effects department members
        # Exclude general production roles like Producer, Executive Producer, etc.
        if department and department.lower() == "visual effects":
            vfx_crew.append(CrewMember(
                name=name,
                job=job,
                department=department,
                movie_title=movie_title,
                imdb_id=imdb_id
            ))
        elif is_vfx_job(job, department) and department and department.lower() != "production":
            # For other departments, apply VFX keyword filter but exclude Production
            vfx_crew.append(CrewMember(
                name=name,
                job=job,
                department=department,
                movie_title=movie_title,
                imdb_id=imdb_id
            ))

    return vfx_crew


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
        all_vfx_crew = []
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

            all_vfx_crew.extend([member.dict() for member in vfx_crew])

            processed_movies.append({
                "title": movie_title,
                "imdb_id": imdb_id or "N/A",
                "status": "success",
                "vfx_crew_count": len(vfx_crew)
            })

        return {
            "success": True,
            "processed_movies": processed_movies,
            "vfx_crew": all_vfx_crew,
            "total_vfx_crew": len(all_vfx_crew)
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

        # Get movie/TV details and credits
        movie_details = get_movie_details(tmdb_id, media_type)
        credits = get_movie_credits(tmdb_id, media_type)

        if not credits:
            raise HTTPException(status_code=404, detail="Credits not found")

        # Handle both movies (title) and TV shows (name)
        movie_title = movie_details.get("title") or movie_details.get("name") or movie.title or "Unknown"
        vfx_crew = filter_vfx_crew(credits, movie_title, movie.imdb_id or "N/A")

        return {
            "success": True,
            "movie": {
                "title": movie_title,
                "imdb_id": movie.imdb_id or "N/A",
                "tmdb_id": tmdb_id,
                "overview": movie_details.get("overview", ""),
                "release_date": movie_details.get("release_date") or movie_details.get("first_air_date", "")
            },
            "vfx_crew": [member.dict() for member in vfx_crew],
            "total_vfx_crew": len(vfx_crew)
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error searching movie: {str(e)}")


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
