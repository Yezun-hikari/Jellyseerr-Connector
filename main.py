import os
from typing import List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import httpx

app = FastAPI()

import json

# Configuration from environment variables
JELLYSEERR_URL = os.getenv("JELLYSEERR_URL", "http://localhost:5055")
JELLYSEERR_API_KEY = os.getenv("JELLYSEERR_API_KEY", "")
ANIWORLD_URL = os.getenv("ANIWORLD_URL", "http://aniworld-downloader:8080")
ANIWORLD_USERNAME = os.getenv("ANIWORLD_USERNAME", "")
ANIWORLD_PASSWORD = os.getenv("ANIWORLD_PASSWORD", "")

# Template setup
templates = Jinja2Templates(directory="templates")

# Helper to map types
TYPE_MAP = {
    "movie": "Movie",
    "tv": "Serie"
}

SETTINGS_FILE = os.getenv("SETTINGS_FILE", "settings.json")

def load_settings() -> Dict[str, Any]:
    if os.path.exists(SETTINGS_FILE):
        try:
            with open(SETTINGS_FILE, "r") as f:
                return json.load(f)
        except Exception:
            pass
    return {}

def save_settings(settings: Dict[str, Any]):
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=4)

# Jellyseerr request statuses
# 1 = PENDING, 2 = APPROVED, 3 = DECLINED
STATUS_APPROVED = 2

async def get_jellyseerr_details(client: httpx.AsyncClient, media_type: str, tmdb_id: int, headers: dict) -> dict:
    endpoint = "movie" if media_type == "movie" else "tv"
    try:
        detail_res = await client.get(
            f"{JELLYSEERR_URL.rstrip('/')}/api/v1/{endpoint}/{tmdb_id}",
            headers=headers,
            timeout=5.0
        )
        if detail_res.status_code == 200:
            return detail_res.json()
    except Exception:
        pass
    return {}

class AniWorldClient:
    def __init__(self):
        self.url = ANIWORLD_URL.rstrip('/')
        self.username = ANIWORLD_USERNAME
        self.password = ANIWORLD_PASSWORD
        self.client = httpx.AsyncClient(timeout=15.0, follow_redirects=True)
        self.logged_in = False

    async def login(self):
        if not self.username or not self.password:
            # If no credentials, we just hope it works without auth
            self.logged_in = True
            return True

        try:
            # 1. GET login page to retrieve CSRF token
            import re
            login_page_res = await self.client.get(f"{self.url}/login")
            if login_page_res.status_code != 200:
                print(f"Could not load login page: {login_page_res.status_code}")
                return False

            csrf_match = re.search(r'name="csrf_token"\s+value="([^"]+)"', login_page_res.text)
            csrf_token = csrf_match.group(1) if csrf_match else ""

            # 2. POST login data with CSRF token
            res = await self.client.post(
                f"{self.url}/login",
                data={
                    "username": self.username,
                    "password": self.password,
                    "csrf_token": csrf_token
                }
            )
            # If successful, we should have a session cookie now
            if res.status_code == 200:
                self.logged_in = True
                return True
        except Exception as e:
            print(f"Login failed: {e}")
        return False

    async def request(self, method, path, **kwargs):
        if not self.logged_in:
            await self.login()

        full_url = f"{self.url}/{path.lstrip('/')}"
        res = await self.client.request(method, full_url, **kwargs)

        # If we get a 401, try to log in again and retry once
        if res.status_code == 401 and self.username and self.password:
            if await self.login():
                res = await self.client.request(method, full_url, **kwargs)

        return res

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.client.aclose()

async def check_is_anime(client: httpx.AsyncClient, tmdb_id: int, headers: dict, details: dict = None) -> bool:
    # First check keywords for 'anime' tag
    try:
        keyword_res = await client.get(
            f"{JELLYSEERR_URL.rstrip('/')}/api/v1/tv/{tmdb_id}/keywords",
            headers=headers,
            timeout=5.0
        )
        if keyword_res.status_code == 200:
            keywords = keyword_res.json().get("keywords", [])
            if any(k.get("name", "").lower() == "anime" for k in keywords):
                return True
    except Exception:
        pass

    # If tag not found, fallback to Animation genre if details provided
    if details:
        genres = details.get("genres", [])
        if any(g.get("name", "").lower() == "animation" for g in genres):
            return True

    return False

async def fetch_users() -> List[Dict[str, Any]]:
    headers = {
        "X-Api-Key": JELLYSEERR_API_KEY
    }
    async with httpx.AsyncClient() as client:
        response = await client.get(
            f"{JELLYSEERR_URL.rstrip('/')}/api/v1/user",
            headers=headers,
            timeout=10.0
        )
        if response.status_code == 200:
            return response.json()
    return []

async def fetch_approved_requests() -> List[Dict[str, Any]]:
    all_requests = []
    skip = 0
    take = 100

    headers = {
        "X-Api-Key": JELLYSEERR_API_KEY
    }

    async with httpx.AsyncClient() as client:
        while True:
            response = await client.get(
                f"{JELLYSEERR_URL.rstrip('/')}/api/v1/request",
                params={"take": take, "skip": skip, "filter": "approved"},
                headers=headers,
                timeout=10.0
            )

            if response.status_code != 200:
                raise Exception(f"Jellyseerr API error: {response.status_code} - {response.text}")

            data = response.json()
            requests = data.get("results", [])

            if not requests:
                break

            for req in requests:
                if req.get("status") == STATUS_APPROVED:
                    media = req.get("media", {})
                    media_type = req.get("type") or media.get("mediaType")
                    tmdb_id = media.get("tmdbId")

                    title = "Unknown Title"
                    is_anime = False

                    if tmdb_id:
                        details = await get_jellyseerr_details(client, media_type, tmdb_id, headers)
                        if details:
                            title = details.get("title") or details.get("name") or details.get("originalName")
                            if media_type == "tv":
                                is_anime = await check_is_anime(client, tmdb_id, headers, details)

                    if not title or title == "Unknown Title":
                        if req.get("media"):
                            title = req.get("media").get("title") or req.get("media").get("name")
                        if not title or title == "Unknown Title":
                            title = req.get("title") or "Request ID: " + str(req.get("id"))

                    requested_by = "Unknown"
                    user = req.get("requestedBy")
                    if user:
                        requested_by = user.get("displayName") or user.get("username") or user.get("email") or "Unknown"

                    seasons_display = ""
                    if media_type == "tv":
                        requested_seasons = req.get("seasons", [])
                        requested_season_numbers = sorted([s.get("seasonNumber") for s in requested_seasons if s.get("seasonNumber") is not None])
                        available_seasons = media.get("seasons", [])
                        available_season_numbers = [s.get("seasonNumber") for s in available_seasons if s.get("seasonNumber") is not None and s.get("seasonNumber") > 0]

                        is_all_seasons = False
                        if requested_season_numbers and available_season_numbers:
                            is_all_seasons = all(sn in requested_season_numbers for sn in available_season_numbers)

                        if is_all_seasons:
                            seasons_display = "Seasons: All"
                        elif requested_season_numbers:
                            seasons_display = "Seasons: " + ", ".join(map(str, requested_season_numbers))

                    all_requests.append({
                        "id": req.get("id"),
                        "title": title,
                        "type": TYPE_MAP.get(media_type, media_type),
                        "raw_type": media_type,
                        "status": "Approved",
                        "requested_by": requested_by,
                        "seasons": seasons_display,
                        "is_anime": is_anime
                    })

            if len(requests) < take:
                break
            skip += take

    return all_requests

@app.post("/api/download/{request_id}")
async def trigger_download(request_id: int):
    headers = {
        "X-Api-Key": JELLYSEERR_API_KEY
    }

    async with httpx.AsyncClient() as js_client, AniWorldClient() as aw_client:
        # 1. Get request details from Jellyseerr
        req_res = await js_client.get(
            f"{JELLYSEERR_URL.rstrip('/')}/api/v1/request/{request_id}",
            headers=headers,
            timeout=10.0
        )
        if req_res.status_code != 200:
            return {"error": f"Failed to fetch request {request_id} from Jellyseerr"}

        req_data = req_res.json()
        media = req_data.get("media", {})
        user = req_data.get("requestedBy")
        user_id = str(user.get("id")) if user else None

        media_type = req_data.get("type") or media.get("mediaType")
        tmdb_id = media.get("tmdbId")

        if media_type != "tv":
            return {"error": "Only TV series are supported for download at the moment"}

        # 2. Get full details to determine title and genres
        title = "Unknown Title"
        is_anime = False
        if tmdb_id:
            details = await get_jellyseerr_details(js_client, media_type, tmdb_id, headers)
            if details:
                title = details.get("name") or details.get("originalName")
                is_anime = await check_is_anime(js_client, tmdb_id, headers, details)

        if title == "Unknown Title":
            title = media.get("name") or req_data.get("title") or f"Request {request_id}"

        # 3. Search on AniWorld/S.to
        site = "aniworld" if is_anime else "sto"

        # 3.1 Get default language for user
        settings = load_settings()
        user_settings = settings.get(user_id, {}) if user_id else {}
        if site == "aniworld":
            default_lang = user_settings.get("aniworld", "German Dub")
        else:
            default_lang = user_settings.get("serienstream", "German Dub")

        search_res = await aw_client.request(
            "POST",
            "/api/search",
            json={"keyword": title, "site": site}
        )
        if search_res.status_code != 200:
            return {"error": f"Failed to search on {site} (Status: {search_res.status_code})"}

        search_data = search_res.json()
        results = search_data.get("results", [])
        if not results:
            return {"error": f"No results found for '{title}' on {site}"}

        # Take the first result
        series_url = results[0].get("url")
        series_title = results[0].get("title")

        # 4. Get requested seasons
        requested_seasons = [s.get("seasonNumber") for s in req_data.get("seasons", [])]

        # 5. Fetch available seasons from AniWorld-Downloader
        seasons_res = await aw_client.request(
            "GET",
            "/api/seasons",
            params={"url": series_url}
        )
        if seasons_res.status_code != 200:
            return {"error": f"Failed to fetch seasons from downloader (Status: {seasons_res.status_code})"}

        available_seasons = seasons_res.json().get("seasons", [])

        all_episode_urls = []
        for s_num in requested_seasons:
            # Find matching season URL
            season_match = next((s for s in available_seasons if s.get("season_number") == s_num), None)
            if season_match:
                # Fetch episodes for this season
                ep_res = await aw_client.request(
                    "GET",
                    "/api/episodes",
                    params={"url": season_match.get("url")}
                )
                if ep_res.status_code == 200:
                    ep_data = ep_res.json()
                    all_episode_urls.extend([e.get("url") for e in ep_data.get("episodes", [])])

        if not all_episode_urls:
            return {"error": "No episodes found for the requested seasons"}

        # 6. Trigger download
        download_res = await aw_client.request(
            "POST",
            "/api/download",
            json={
                "episodes": all_episode_urls,
                "title": series_title,
                "series_url": series_url,
                "language": default_lang
            }
        )

        if download_res.status_code != 200:
            return {"error": f"Failed to trigger download in AniWorld-Downloader (Status: {download_res.status_code})"}

        return {"message": "Download started", "queue_id": download_res.json().get("queue_id")}

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        requests_data = await fetch_approved_requests()
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "requests": requests_data,
                "error": None
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="index.html",
            context={
                "requests": [],
                "error": str(e)
            }
        )

@app.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    try:
        users = await fetch_users()
        settings = load_settings()
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "users": users,
                "settings": settings,
                "error": None
            }
        )
    except Exception as e:
        return templates.TemplateResponse(
            request=request,
            name="settings.html",
            context={
                "users": [],
                "settings": {},
                "error": str(e)
            }
        )

@app.post("/api/settings")
async def update_settings(settings: Dict[str, Any]):
    save_settings(settings)
    return {"ok": True}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
