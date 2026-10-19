"""
agent.py: a small AI agent that answers questions like
    "suggest a 4.5+ rated restaurant near me that's open now"
by calling the `search_places` tool on our MCP server (maps_server.py).

How it works, in four steps:
  1. Start the MCP server and connect to it (MCP client).
  2. Ask the server which tools it has, and describe them to the model.
  3. Send your question to the model. If the model asks to use a tool,
     run that tool on the MCP server and give the model the result.
  4. Repeat step 3 until the model writes a final answer.

Run it:
    python agent.py
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from openai import AsyncOpenAI

HERE = Path(__file__).parent
load_dotenv(HERE / ".env")

MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# Safety limit: the most tool calls the model may make for one question.
MAX_STEPS = 5

# The system prompt sets the model's job and rules. It is sent with every request.
SYSTEM_PROMPT = """You are a helpful local guide. You find places using the search_places tool.

Rules:
- Always use the tool to find places. Never make up places, ratings or addresses.
- If the user says "near me" or names no place, leave `location` empty.
- Turn the user's words into tool arguments, e.g. "4.5 or above" -> min_rating=4.5,
  "open now" / "right now" -> open_now=true.
- Answer with a short numbered list: name, rating (review count), address, and the Maps link.
- If nothing matches, say so and suggest relaxing a filter (lower rating, ignore open now)."""


def mcp_tools_to_openai(mcp_tools) -> list[dict]:
    """Translate MCP tool descriptions into the format OpenAI expects.

    MCP and OpenAI describe tools almost the same way (name, description,
    JSON schema for the arguments), just with different wrapping.
    """
    return [
        {
            "type": "function",
            "function": {
                "name": tool.name,
                "description": tool.description or "",
                "parameters": tool.input_schema,
            },
        }
        for tool in mcp_tools
    ]


async def answer(question: str, session: ClientSession, llm: AsyncOpenAI, tools: list[dict]) -> str:
    """Run the agent loop for one question and return the final answer."""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]

    for _ in range(MAX_STEPS):
        # Ask the model what to do next. It either answers or requests tool calls.
        response = await llm.chat.completions.create(model=MODEL, messages=messages, tools=tools)
        reply = response.choices[0].message

        # No tool calls means the model has its final answer.
        if not reply.tool_calls:
            return reply.content

        # Keep the model's tool request in the conversation history.
        messages.append({
            "role": "assistant",
            "content": reply.content,
            "tool_calls": [
                {"id": c.id, "type": "function",
                 "function": {"name": c.function.name, "arguments": c.function.arguments}}
                for c in reply.tool_calls
            ],
        })

        # Run each requested tool on the MCP server.
        for call in reply.tool_calls:
            args = json.loads(call.function.arguments or "{}")
            print(f"  -> calling {call.function.name}({args})")

            result = await session.call_tool(call.function.name, args)
            result_text = "\n".join(c.text for c in result.content if hasattr(c, "text"))

            # Give the result back to the model, tagged with the call it answers.
            messages.append({"role": "tool", "tool_call_id": call.id, "content": result_text})

    return "Sorry, I couldn't finish that within the step limit."


async def main() -> None:
    if not os.getenv("OPENAI_API_KEY"):
        sys.exit("OPENAI_API_KEY is not set. Copy .env.example to .env and fill it in.")

    # Step 1: start maps_server.py as a subprocess and connect to it over stdio.
    server = StdioServerParameters(command=sys.executable, args=[str(HERE / "maps_server.py")])

    async with stdio_client(server) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            # Step 2: discover the server's tools.
            listed = await session.list_tools()
            tools = mcp_tools_to_openai(listed.tools)
            print(f"Connected to MCP server. Tools: {[t.name for t in listed.tools]}")
            print("Ask me about places (or type 'quit').\n")

            llm = AsyncOpenAI()  # reads OPENAI_API_KEY from the environment

            # Steps 3 and 4: one question at a time.
            while True:
                question = input("You: ").strip()
                if question.lower() in {"quit", "exit", "q"}:
                    break
                if not question:
                    continue
                print(f"\nAgent: {await answer(question, session, llm, tools)}\n")


if __name__ == "__main__":
    asyncio.run(main())
