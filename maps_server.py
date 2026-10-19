"""
maps_server.py: an MCP server that gives an AI agent one tool, `search_places`.

The agent never talks to Google directly. It asks this server to run the tool,
the server calls the Google Places API, and it returns clean results.

Run it on its own (for testing with the MCP Inspector):
    python maps_server.py
Normally you won't run it yourself: agent.py starts it automatically.
"""

import os
from pathlib import Path

import httpx
from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer

# Load .env from this file's folder, so it works no matter where we're started from.
load_dotenv(Path(__file__).parent / ".env")

GOOGLE_MAPS_API_KEY = os.getenv("GOOGLE_MAPS_API_KEY")
MY_LOCATION = os.getenv("MY_LOCATION", "")

# Places API (New) "Text Search" endpoint: takes a plain-English query like
# "coffee shop near Jersey City" and returns matching places.
PLACES_URL = "https://places.googleapis.com/v1/places:searchText"

# Google only returns the fields we ask for (and bills by which fields we ask for).
FIELDS = ",".join([
    "places.displayName",
    "places.formattedAddress",
    "places.rating",
    "places.userRatingCount",
    "places.currentOpeningHours.openNow",
    "places.googleMapsUri",
])

# Create the server. The name is what MCP clients see when they connect.
mcp = MCPServer("gmaps")


# @mcp.tool() turns a normal Python function into an MCP tool.
# The MCP SDK reads the function name, the type hints and the docstring and
# turns them into the tool description the AI model sees. So the docstring
# isn't just a comment: it's the instructions the model reads.
@mcp.tool()
async def search_places(
    query: str,
    location: str = "",
    min_rating: float = 0.0,
    open_now: bool = False,
    max_results: int = 5,
) -> dict:
    """Search Google Maps for places of any kind: restaurants, coffee shops,
    laundromats, gas stations, pharmacies, gyms, and so on.

    Args:
        query: What to look for, e.g. "thai restaurant", "laundromat", "gas station".
        location: Where to search, e.g. "Hoboken, NJ". Leave empty to search
            near the user's default location ("near me").
        min_rating: Only return places rated at least this high (0 to 5).
        open_now: If true, only return places that are open right now.
        max_results: How many places to return (1 to 10).
    """
    if not GOOGLE_MAPS_API_KEY:
        return {"error": "GOOGLE_MAPS_API_KEY is not set. Add it to your .env file."}

    where = location.strip() or MY_LOCATION
    if not where:
        return {"error": "No location given and MY_LOCATION is not set in .env."}

    max_results = max(1, min(int(max_results), 10))

    # What we send to Google.
    body = {"textQuery": f"{query} near {where}", "pageSize": max_results}
    if min_rating > 0:
        body["minRating"] = min_rating
    if open_now:
        body["openNow"] = True

    headers = {
        "X-Goog-Api-Key": GOOGLE_MAPS_API_KEY,
        "X-Goog-FieldMask": FIELDS,
    }

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.post(PLACES_URL, json=body, headers=headers)

    if response.status_code != 200:
        # Pass Google's own error message back, so the problem is easy to spot
        # (wrong key, API not enabled, billing not set up...).
        message = response.json().get("error", {}).get("message", response.text)
        return {"error": f"Google Places API error {response.status_code}: {message}"}

    places = []
    for p in response.json().get("places", []):
        rating = p.get("rating")
        is_open = p.get("currentOpeningHours", {}).get("openNow")

        # Double-check the filters ourselves instead of trusting the API blindly.
        if min_rating > 0 and (rating is None or rating < min_rating):
            continue
        if open_now and is_open is not True:
            continue

        places.append({
            "name": p.get("displayName", {}).get("text"),
            "address": p.get("formattedAddress"),
            "rating": rating,
            "reviews": p.get("userRatingCount"),
            "open_now": is_open,
            "maps_link": p.get("googleMapsUri"),
        })

    return {"searched_for": body["textQuery"], "count": len(places), "places": places}


if __name__ == "__main__":
    # "stdio" means the server talks over standard input/output. The agent
    # starts this file as a subprocess and they exchange MCP messages through
    # its stdin/stdout. (So never print() to stdout in this file: it would
    # corrupt the messages.)
    mcp.run(transport="stdio")
