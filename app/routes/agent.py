"""POST /api/agent/chat - bounded AI interaction layer (Phase 2, MVP).

Design contract (see docs/PHASE_2_AGENTIC_AI_DESIGN.md and the R21/R26
guardrails): this endpoint lets a user ask natural-language questions about
decisions, data freshness, and order chains. The model may ONLY call the
read-only tools registered in app.agent.tools - it never computes a new
ranking, never writes to the database, and never bypasses the human
approve/reject/override workflow. If OPENAI_API_KEY is not configured, the
endpoint returns a clear 503 rather than silently degrading, so the gap
between "designed" and "connected" is always visible.
"""
from __future__ import annotations

import json
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import get_db
from app.dependencies import get_current_user, CurrentUser
from app.agent.tools import TOOL_SPECS, call_tool

router = APIRouter(prefix="/api/agent", tags=["Agent"])

logger = logging.getLogger(__name__)

# Bounded loop guardrail (R20/R21): the agent cannot spiral into an
# unbounded tool-call loop or run indefinitely.
MAX_TOOL_CALLS = 6

SYSTEM_PROMPT = (
    "You are the BOM Optimizer assistant. You help production planners "
    "understand decisions, data freshness, and order relationships that "
    "the deterministic rule engine already computed and stored.\n\n"
    "Hard rules you must always follow:\n"
    "1. You NEVER invent numbers, scores, or rankings. Only state facts "
    "returned by a tool call.\n"
    "2. You NEVER approve, reject, override, or adopt a decision yourself - "
    "you may only describe what the current status is and tell the user to "
    "use the Decisions page to act.\n"
    "3. You NEVER claim to have re-run or re-optimized anything. If asked "
    "to recompute, tell the user to use the Production Persona / Analyze "
    "BOM pages, which run the deterministic engine.\n"
    "4. If a tool returns an error (e.g. decision not found), say so plainly "
    "instead of guessing.\n"
    "5. Be concise. Use bullet points for lists of decisions or components."
)


def _build_llm_client(settings):
    """Return (client, model_name, model_label) for the configured provider.

    One OpenAI-compatible path: OPENAI_BASE_URL empty means api.openai.com;
    set it for on-prem / Azure / any OpenAI-compatible host. Returns
    (None, "", "") when no key is configured.
    """
    if not settings.OPENAI_API_KEY:
        return None, "", ""

    from openai import AsyncOpenAI

    return (
        AsyncOpenAI(
            api_key=settings.OPENAI_API_KEY,
            base_url=settings.OPENAI_BASE_URL or None,
        ),
        settings.OPENAI_MODEL,
        f"openai/{settings.OPENAI_MODEL}",
    )


class ChatMessage(BaseModel):
    role: str = Field(description="'user' or 'assistant'")
    content: str


class ChatRequest(BaseModel):
    messages: list[ChatMessage] = Field(
        description="Conversation so far, oldest first. The system prompt is added automatically."
    )


class ChatResponse(BaseModel):
    reply: str
    tool_calls_made: list[str]
    model_used: str


@router.post("/chat", response_model=ChatResponse)
async def agent_chat(
    request: ChatRequest,
    db: AsyncSession = Depends(get_db),
    user: CurrentUser = Depends(get_current_user),
):
    settings = get_settings()

    client, model_name, model_label = _build_llm_client(settings)
    if client is None:
        raise HTTPException(
            status_code=503,
            detail=(
                "AI interaction layer is not configured. Set OPENAI_API_KEY in "
                "api/.env (plus OPENAI_BASE_URL if not using api.openai.com) "
                "and restart the API - see docs/OPENAI_SDK_SETUP_GUIDE.md."
            ),
        )

    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend({"role": m.role, "content": m.content} for m in request.messages)

    tool_calls_made: list[str] = []

    for _ in range(MAX_TOOL_CALLS):
        try:
            response = await client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=TOOL_SPECS,
                tool_choice="auto",
                temperature=0,
                seed=12345,
                max_tokens=700,
            )
        except Exception as exc:
            # A configured provider that fails must be visible — 502 with the
            # real error (e.g. quota, auth, unreachable), never a fake answer.
            logger.error("Agent chat LLM call failed: %s", exc)
            raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
        choice = response.choices[0]
        messages.append(choice.message.model_dump(exclude_none=True))

        if not choice.message.tool_calls:
            return ChatResponse(
                reply=choice.message.content or "",
                tool_calls_made=tool_calls_made,
                model_used=model_label,
            )

        for tc in choice.message.tool_calls:
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {}
            result = await call_tool(tc.function.name, args, db)
            tool_calls_made.append(tc.function.name)
            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,
                "content": json.dumps(result, default=str),
            })

    # Bounded-loop guardrail tripped: force a final answer without more tools.
    try:
        final = await client.chat.completions.create(
            model=model_name,
            messages=messages,
            temperature=0,
            seed=12345,
            max_tokens=500,
        )
    except Exception as exc:
        logger.error("Agent final LLM call failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"LLM call failed: {exc}")
    return ChatResponse(
        reply=final.choices[0].message.content or "I was unable to complete that within the tool-call limit.",
        tool_calls_made=tool_calls_made,
        model_used=model_label,
    )


@router.get("/tools")
async def list_tools(user: CurrentUser = Depends(get_current_user)):
    """Expose the registered tool schemas so the UI/lead can verify what the agent can and cannot do."""
    return {
        "tool_count": len(TOOL_SPECS),
        "tools": [
            {"name": t["function"]["name"], "description": t["function"]["description"]}
            for t in TOOL_SPECS
        ],
        "guardrails": [
            "Read-only: no tool can approve/reject/override/adopt or write any table.",
            f"Bounded loop: max {MAX_TOOL_CALLS} tool-call rounds per chat turn.",
            "No key configured -> 503, never a silent fake answer.",
        ],
    }
