# Gmaps Agent

A small AI agent that finds places on Google Maps through an MCP server.

```
You: suggest a 4.5 or above rated restaurant near me which is open
  -> calling search_places({'query': 'restaurant', 'min_rating': 4.5, 'open_now': True})

Agent: Here are some open restaurants rated 4.5+ near you:
1. ... ⭐ 4.7 (1,240 reviews), 123 Main St, https://maps.google.com/?cid=...
```

It works for any kind of place: restaurants, coffee shops, laundromats, gas stations, pharmacies, gyms and so on.

## How it works

```
 ┌──────────┐  question  ┌────────────┐  "which tools?" / call tool  ┌────────────────┐  HTTPS  ┌──────────────┐
 │   You    │ ─────────► │  agent.py  │ ───────────────────────────► │ maps_server.py │ ──────► │ Google Places│
 └──────────┘            │ (MCP client│ ◄─────────────────────────── │  (MCP server)  │ ◄────── │     API      │
                         │  + OpenAI) │        tool results          └────────────────┘         └──────────────┘
                         └────────────┘
```

- **`maps_server.py`** is an **MCP server**. It offers one tool, `search_places(query, location, min_rating, open_now, max_results)`, which calls the Google Places API and returns a tidy list. Only this file uses your Google key.
- **`agent.py`** is the **agent**. It starts the server, asks it which tools it has, and hands those tools to the model. When the model asks for a tool, `agent.py` runs it on the server and passes the result back. It repeats this until the model writes an answer.

The model decides *what* to search for. The code decides *how* to run the search, and double-checks the rating and open-now filters itself.

## Setup

### 1. Get API keys
- **OpenAI:** https://platform.openai.com/api-keys
- **Google Maps:** in [Google Cloud Console](https://console.cloud.google.com/), create a project with billing enabled, then:
  1. Enable **Places API (New)** under *APIs & Services → Library*.
  2. Create an API key under *APIs & Services → Credentials*.
  3. Recommended: restrict the key to the Places API (New) only.

### 2. Install
```bash
cd gmaps
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure
```bash
cp .env.example .env
```
Open `.env` and fill in `OPENAI_API_KEY`, `GOOGLE_MAPS_API_KEY`, and `MY_LOCATION` (your city or address, used for "near me"). `.env` is listed in `.gitignore`, so it's never committed.

## Run
```bash
python agent.py
```

Try asking:
- `suggest a 4.5 or above rated restaurant near me which is open`
- `any laundromat open right now?`
- `coffee shops in Hoboken with at least 4 stars`
- `24 hour pharmacy near me`
- `top 3 gyms near Journal Square`

Type `quit` to exit.

## Test the server on its own (optional)

The [MCP Inspector](https://github.com/modelcontextprotocol/inspector) lets you call the tool by hand, with no model involved:
```bash
npx @modelcontextprotocol/inspector .venv/bin/python maps_server.py
```

## Files

| File | Purpose |
|---|---|
| `maps_server.py` | MCP server exposing the `search_places` tool |
| `agent.py` | Agent: MCP client + OpenAI tool-calling loop |
| `requirements.txt` | Python dependencies (MCP SDK v2, OpenAI, httpx, python-dotenv) |
| `.env.example` | Template for API keys and settings |
| `.gitignore` | Keeps `.env` and local files out of git |

## Troubleshooting

| Problem | Fix |
|---|---|
| `Google Places API error 403 ... not been used ... or it is disabled` | Enable **Places API (New)** for your project (the older "Places API" isn't the same one). |
| `Google Places API error 400 ... API key not valid` | Check `GOOGLE_MAPS_API_KEY` in `.env`. |
| `OPENAI_API_KEY is not set` | Create `.env` from `.env.example`. |
| Results are from the wrong area | Set `MY_LOCATION` more precisely (an address works best), or name a place in your question. |
| `No module named 'mcp.server.fastmcp'` in other tutorials | This project uses MCP SDK v2, where `FastMCP` became `MCPServer`. |

## Ideas for next steps
- Add a second tool, such as `get_directions` (Routes API) for "how long to walk there?". `agent.py` picks up new tools automatically.
- Keep the conversation going between questions, so follow-ups like "which of those is closest?" work.
- Connect `maps_server.py` to an MCP client app such as Claude Desktop instead of `agent.py`.
