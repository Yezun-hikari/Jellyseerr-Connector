import os
from typing import List, Dict, Any
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
import httpx

app = FastAPI()

# Configuration from environment variables
JELLYSEERR_URL = os.getenv("JELLYSEERR_URL", "http://localhost:5055")
JELLYSEERR_API_KEY = os.getenv("JELLYSEERR_API_KEY", "")

# Template setup
templates = Jinja2Templates(directory="templates")

# Helper to map types
TYPE_MAP = {
    "movie": "Movie",
    "tv": "Serie"
}

# Jellyseerr request statuses
# 1 = PENDING, 2 = APPROVED, 3 = DECLINED
STATUS_APPROVED = 2

async def fetch_approved_requests() -> List[Dict[str, Any]]:
    all_requests = []
    skip = 0
    take = 100

    headers = {
        "X-Api-Key": JELLYSEERR_API_KEY
    }

    async with httpx.AsyncClient() as client:
        while True:
            # Jellyseerr API call to fetch requests
            # Documentation: https://api-docs.jellyseerr.dev/#/request/get_request
            # We filter for 'approved' (status=2) if the API supports it,
            # otherwise we filter manually.
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
                # Double check status just in case filter is not supported/reliable
                if req.get("status") == STATUS_APPROVED:
                    media = req.get("media", {})
                    media_type = req.get("type") or media.get("mediaType")

                    # Try to get title from the nested media or search results if available
                    # Jellyseerr requests usually have a 'media' object
                    title = "Unknown Title"
                    if req.get("media"):
                        # Sometimes titles are in different places depending on the type
                        # For brevity, we check common fields
                        title = req.get("media").get("title") or req.get("media").get("name")

                    if not title or title == "Unknown Title":
                        # For TV shows, the name is sometimes in 'tv_show_name' if available or just 'title'
                        title = req.get("title") or "Request ID: " + str(req.get("id"))

                    all_requests.append({
                        "id": req.get("id"),
                        "title": title,
                        "type": TYPE_MAP.get(media_type, media_type),
                        "status": "Approved"
                    })

            if len(requests) < take:
                break

            skip += take

    return all_requests

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    try:
        requests_data = await fetch_approved_requests()
        return templates.TemplateResponse("index.html", {
            "request": request,
            "requests": requests_data,
            "error": None
        })
    except Exception as e:
        return templates.TemplateResponse("index.html", {
            "request": request,
            "requests": [],
            "error": str(e)
        })

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
