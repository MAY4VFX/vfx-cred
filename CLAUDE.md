# CLAUDE.md
### Dokploy Server
- **Host**: 192.168.2.140
- **SSH Access**: `ssh -o StrictHostKeyChecking=no root@192.168.2.140`
- **Auto-deploy**: Enabled - пуш в правильную ветку автоматически запускает деплой
- **Dokploy API Key**: XdVofMdOfAlneojMFpBWplFeYWbxFzcUpuPBlQLYuBxmfWmjARKNyXwDEnsgMrZc
This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

VFX Credits Filter Service is a FastAPI-based web application that filters VFX crew members from movie credits. It integrates with TMDb API to fetch movie credits and intelligently identifies VFX-related professionals.

**Tech Stack:**
- Backend: FastAPI 0.104.1 + Uvicorn
- Frontend: Vanilla JavaScript + HTML5 + CSS3
- Database Integration: TMDb API
- Data Processing: Pandas + OpenPyXL (CSV/Excel)
- Containerization: Docker + Docker Compose

## Development Commands

### Local Development (without Docker)

```bash
# Setup
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Development with hot-reload
uvicorn app:app --host 0.0.0.0 --port 8000 --reload

# Or run directly
python app.py
```

### Docker Development

```bash
# Build and start
docker-compose up -d

# View logs
docker-compose logs -f

# Rebuild after code changes
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Stop
docker-compose down
```

### Health Check

```bash
curl http://localhost:8000/api/health
```

## Project Structure

```
app.py                 # FastAPI application (main entry point)
requirements.txt       # Python dependencies
static/index.html      # Web UI (Vanilla JS + CSS)
Dockerfile            # Container configuration
docker-compose.yml    # Multi-container orchestration
.env.example          # Environment template
README.md             # Russian documentation
logs/                 # Application logs directory
```

## Architecture

### Component Interaction

```
Web UI (index.html)
    ↓
FastAPI Application (app.py)
    ├→ File Upload & Processing (CSV/Excel parsing)
    ├→ IMDB ↔ TMDb ID Conversion
    ├→ TMDb API Integration (credits, movie details)
    └→ VFX Crew Filtering & Export
    ↓
Response (JSON or Excel)
```

### Key Processing Pipeline

1. **File Upload** → CSV/Excel parsing via Pandas
2. **IMDB ID Extraction** → Extract from URL or direct ID
3. **TMDb Conversion** → Convert IMDB ID to TMDb ID via TMDb API
4. **Credit Fetching** → Get full movie credits from TMDb
5. **VFX Filtering** → Filter crew by VFX-related keywords and departments
6. **Export** → Return JSON response or Excel file

## API Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/` | GET | Serve HTML UI |
| `/api/health` | GET | Service health check |
| `/api/upload-csv` | POST | Upload and process CSV/Excel files |
| `/api/search-movie` | POST | Search single movie by IMDB ID |
| `/api/export` | POST | Export results to Excel |

### API Response Format

```json
{
  "success": true,
  "vfx_crew": [
    {
      "name": "...",
      "character": "...",
      "department": "Visual Effects",
      "job": "VFX Supervisor"
    }
  ],
  "total_vfx_crew": 25
}
```

## Environment Configuration

Create `.env` from `.env.example`:

```env
TMDB_API_KEY=your_tmdb_api_key_here
TMDB_BASE_URL=https://api.themoviedb.org/3
HOST=0.0.0.0
PORT=8000
```

Obtain TMDb API key from: https://www.themoviedb.org/settings/api

## VFX Filtering Logic

The service identifies VFX crew by matching against:

**Keywords:** vfx, visual effects, supervisor, producer, coordinator, compositor, animator, cg, 3d, effects, digital, matte painter, rotoscoping, tracking, lighting, rendering, fx

**Departments:** Visual Effects, Animation

**Function:** `is_vfx_job()` in app.py

## Key Functions

| Function | Purpose |
|----------|---------|
| `extract_imdb_id()` | Extract IMDB ID from URL or string |
| `is_vfx_job()` | Check if job/department is VFX-related |
| `get_tmdb_id_from_imdb()` | Convert IMDB ID to TMDb ID |
| `get_movie_credits()` | Fetch credits from TMDb API |
| `get_movie_details()` | Fetch movie metadata (title, release date) |
| `filter_vfx_crew()` | Extract VFX crew from full credits list |

## Web UI Features

**File:** `static/index.html`

- Two modes: Bulk CSV upload and Single movie search
- Drag-and-drop file upload
- Real-time results table with filtering
- Excel export functionality
- Responsive design (gradient purple/blue theme)
- No external dependencies (Vanilla JS)

## Deployment

### Docker (Recommended)

```bash
docker-compose up -d
```

Service runs on `0.0.0.0:8000` with automatic health checks and restart policy.

### Production Considerations

- Use environment variables for sensitive data (API keys)
- Configure reverse proxy (Nginx) with SSL for `cred.ai-vfx.com`
- Set up log rotation for `./logs` directory
- Monitor service health via `/api/health` endpoint

## Common Development Tasks

### Add New VFX Keywords

Edit `is_vfx_job()` function in app.py to add keywords to the VFX filtering logic.

### Test with Sample Data

Use a CSV with structure:
```
Movie Title, IMDB ID
The Matrix, tt0133093
Inception, tt1375666
```

Upload via `/api/upload-csv` endpoint or web UI.

### Debug TMDb API Issues

Check environment variables and API key validity. Logs are written to `./logs` directory.

## Git Workflow

- Main branch: `main`
- Branch naming: Follow existing PR pattern (`claude/feature-name`)
- Commits are automatically tracked with git hooks

## Notes

- Python version: 3.11+ (specified in Dockerfile)
- No package-lock equivalent needed (pip handles requirements.txt)
- Database: External TMDb API integration only (no local DB)
- Frontend: Single HTML file - no build process needed
