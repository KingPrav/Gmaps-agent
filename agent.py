"""
agent.py: a LangGraph agent that finds places through our MCP server (maps_server.py).

Instead of one loop where the model decides everything, the work is split into
steps (nodes). Some nodes use the model, some are plain Python:

    understand -> search -> (no results? relax -> search again) -> rank -> answer
         |                                                                   ^
         +---- follow-up about the last results (no new search needed) -----+

Run it:
    python agent.py              # chat
    python agent.py --graph      # print the graph as a Mermaid diagram
"""

import asyncio
import json
import operator
import os
import sys
from pathlib import Path
from typing import Annotated, TypedDict

from dotenv import load_dotenv
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI
from pydantic import BaseModel, Field

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
MAX_RETRIES = 2  # how many times "relax" may loosen the filters


# ---------------------------------------------------------------------------
# 1. STATE: the shared data every node reads from and writes to.
# ---------------------------------------------------------------------------
class State(TypedDict, total=False):
    question: str                                   # the user's latest message
    history: Annotated[list[dict], operator.add]    # chat so far (new entries are appended)
    filters: dict                                   # search arguments, e.g. {"query": "cafe", ...}
    needs_search: bool                              # False for follow-ups about the last results
    places: list[dict]                              # results from the MCP server
    error: str                                      # tool error message, if any
    retries: int                                    # how many times we've relaxed the filters
    notes: list[str]                                # what we relaxed, so the answer can say so
    answer: str                                     # the final reply


# What the "understand" node asks the model to fill in (structured output).
class SearchPlan(BaseModel):
    needs_search: bool = Field(description="False only if the user is asking about places already listed")
    query: str = Field(description='What to look for, e.g. "coffee shop", "laundromat". Empty if no search.')
    location: str = Field(description='Where to search, e.g. "Hoboken, NJ". Empty for "near me".')
    min_rating: float = Field(description="Minimum rating 0-5. 0 if the user didn't ask.")
    open_now: bool = Field(description="True if the user wants places open right now.")
    max_results: int = Field(description="How many places to show. Default 5, max 10.")


UNDERSTAND_PROMPT = """You turn a user's request into a Google Maps search plan.
Use the conversation so far to resolve follow-ups:
- "same but in Hoboken" -> reuse the previous filters, change location.
- "which of those has the most reviews?" -> needs_search=false (answer from the last results).
Previous search filters: {filters}"""

ANSWER_PROMPT = """You are a helpful local guide. Answer using ONLY the places provided (never invent any).
Format: a short numbered list with name, rating (review count), whether it's open, address, and Maps link.
If some filters were relaxed, say so in one short sentence first.
If there are no places, say so and suggest a different search."""


# ---------------------------------------------------------------------------
# 2. NODES: each one is a function that takes the state and returns updates.
# ---------------------------------------------------------------------------
def build_graph(session: ClientSession, llm: AsyncOpenAI):
    """The nodes need the MCP session and the OpenAI client, so we build them here."""

    async def understand(state: State) -> dict:
        """[model] Turn the question into structured search filters."""
        messages = [{"role": "system", "content": UNDERSTAND_PROMPT.format(filters=state.get("filters"))}]
        messages += state.get("history", [])[-6:]  # recent turns, for follow-ups
        messages.append({"role": "user", "content": state["question"]})

        response = await llm.chat.completions.parse(model=MODEL, messages=messages, response_format=SearchPlan)
        plan = response.choices[0].message.parsed

        if not plan.needs_search and state.get("places"):
            print("  [understand] follow-up about the previous results, no new search")
            return {"needs_search": False}

        filters = plan.model_dump(exclude={"needs_search"})
        filters["max_results"] = max(1, min(filters["max_results"], 10))
        print(f"  [understand] {filters}")
        return {"needs_search": True, "filters": filters, "retries": 0, "notes": [], "error": ""}

    async def search(state: State) -> dict:
        """[MCP tool] Call search_places on the MCP server."""
        result = await session.call_tool("search_places", state["filters"])
        data = json.loads(result.content[0].text)
        if "error" in data:
            print(f"  [search] error: {data['error']}")
            return {"places": [], "error": data["error"]}
        print(f"  [search] {data['count']} result(s) for '{data['searched_for']}'")
        return {"places": data["places"], "error": ""}

    def relax(state: State) -> dict:
        """[code] Loosen one filter per retry: first lower the rating by 0.5,
        then drop 'open now', then lower the rating again."""
        f = dict(state["filters"])
        notes = list(state.get("notes", []))
        already_lowered = any("rating" in n for n in notes)
        if f["min_rating"] > 0 and not already_lowered:
            f["min_rating"] = max(0, f["min_rating"] - 0.5)
            notes.append(f"lowered the minimum rating to {f['min_rating']}")
        elif f["open_now"]:
            f["open_now"] = False
            notes.append("included places that may be closed right now")
        elif f["min_rating"] > 0:
            f["min_rating"] = max(0, f["min_rating"] - 0.5)
            notes.append(f"lowered the minimum rating to {f['min_rating']}")
        else:
            notes.append("broadened the search")  # nothing left to loosen; search once more
        print(f"  [relax] {notes[-1]}")
        return {"filters": f, "notes": notes, "retries": state.get("retries", 0) + 1}

    def rank(state: State) -> dict:
        """[code] Sort by a rating that accounts for review count (a 5.0 from 3 reviews
        shouldn't beat a 4.7 from 2,000). This is a 'Bayesian average'."""
        prior_rating, prior_weight = 4.0, 50

        def score(p: dict) -> float:
            r, n = p.get("rating") or 0, p.get("reviews") or 0
            return (r * n + prior_rating * prior_weight) / (n + prior_weight)

        places = sorted(state["places"], key=score, reverse=True)
        print(f"  [rank] top: {[p['name'] for p in places[:3]]}")
        return {"places": places}

    async def answer(state: State) -> dict:
        """[model] Write the final reply from the ranked places."""
        context = {"places": state.get("places", []), "relaxed_filters": state.get("notes", []),
                   "tool_error": state.get("error") or None}
        messages = [{"role": "system", "content": ANSWER_PROMPT}]
        messages += state.get("history", [])[-6:]
        messages.append({"role": "user", "content": f"{state['question']}\n\nData:\n{json.dumps(context)}"})

        response = await llm.chat.completions.create(model=MODEL, messages=messages)
        text = response.choices[0].message.content
        new_history = [{"role": "user", "content": state["question"]}, {"role": "assistant", "content": text}]
        return {"answer": text, "history": new_history}

    # -----------------------------------------------------------------------
    # 3. EDGES: the arrows. Conditional edges are functions that pick the next node.
    # -----------------------------------------------------------------------
    def after_understand(state: State) -> str:
        return "search" if state["needs_search"] else "answer"

    def after_search(state: State) -> str:
        if state.get("error"):
            return "answer"                         # report the problem
        if not state["places"] and state.get("retries", 0) < MAX_RETRIES:
            return "relax"                          # nothing found: loosen and retry
        return "rank"

    graph = StateGraph(State)
    graph.add_node("understand", understand)
    graph.add_node("search", search)
    graph.add_node("relax", relax)
    graph.add_node("rank", rank)
    graph.add_node("answer", answer)

    graph.add_edge(START, "understand")
    graph.add_conditional_edges("understand", after_understand, ["search", "answer"])
    graph.add_conditional_edges("search", after_search, ["relax", "rank", "answer"])
    graph.add_edge("relax", "search")               # the retry loop
    graph.add_edge("rank", "answer")
    graph.add_edge("answer", END)

    # The checkpointer saves the state after every step, per conversation "thread".
    # That's what lets follow-up questions see the previous results.
    return graph.compile(checkpointer=InMemorySaver())


# ---------------------------------------------------------------------------
# 4. MAIN: connect to the MCP server, then chat.
# ---------------------------------------------------------------------------
async def main() -> None:
    if "--graph" in sys.argv:
        print(build_graph(None, None).get_graph().draw_mermaid())
        return
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")

    server = StdioServerParameters(command=sys.executable, args=[str(HERE / "maps_server.py")])
    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            print(f"Connected to MCP server. Tools: {[t.name for t in tools.tools]}")

            app = build_graph(session, AsyncOpenAI())
            config = {"configurable": {"thread_id": "chat-1"}}  # one conversation = one thread
            print("Ask me about places (or type 'quit').\n")

            while True:
                question = input("You: ").strip()
                if question.lower() in {"quit", "exit", "q"}:
                    break
                if not question:
                    continue
                try:
                    result = await app.ainvoke({"question": question}, config)
                    print(f"\nAgent: {result['answer']}\n")
                except Exception as e:  # keep chatting even if one question fails
                    print(f"\nAgent: Sorry, something went wrong: {type(e).__name__}: {e}\n")


if __name__ == "__main__":
    asyncio.run(main())
