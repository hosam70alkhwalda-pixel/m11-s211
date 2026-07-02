"""router service — classifies a question and forwards to the right backend.

Exposes:
- POST /route      — classify + forward; returns the backend's response plus the routing decision
- GET  /metrics    — Prometheus text format
- GET  /decisions  — recent routing decisions (in-memory log, for analyze_routing.py)

Honors Track — TODO implementations required. The starter wires the FastAPI
surface, instrumentation, the shared request-id header, and the in-memory
decision log; learners implement the classifier and the backend call.
"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections import deque
from typing import Literal

import httpx
from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

SERVICE = os.environ.get("SERVICE_NAME", "router")
NER_KG_URL = os.environ.get("NER_KG_URL", "http://ner-kg:8000")
RAG_URL = os.environ.get("RAG_URL", "http://rag:8000")

app = FastAPI()

REQUESTS = Counter(
    "service_requests_total", "Requests per endpoint", ["service", "endpoint", "status"]
)
LATENCY = Histogram(
    "service_request_latency_seconds",
    "Request latency by endpoint",
    ["service", "endpoint"],
)
ROUTING_DECISIONS = Counter(
    "router_decisions_total", "Routing decisions by target backend", ["target"]
)

# In-memory routing decision log. Bounded to keep memory predictable in CI.
_DECISIONS: deque[dict] = deque(maxlen=1000)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    request.state.request_id = request_id
    start = time.perf_counter()
    response = await call_next(request)
    LATENCY.labels(SERVICE, request.url.path).observe(time.perf_counter() - start)
    REQUESTS.labels(SERVICE, request.url.path, str(response.status_code)).inc()
    response.headers["x-request-id"] = request_id
    return response


class RouteIn(BaseModel):
    question: str


Target = Literal["ner-kg", "rag"]


# ---------------------------------------------------------------------------
# Rule-based classifier.
#
# Signal 1 — KG relation phrasing (graph-traversal style questions) -> ner-kg
# Signal 2 — entity-lookup phrasing (who/what specific named thing) -> ner-kg
# Signal 3 — open-ended explanatory phrasing -> rag
# Signal 4 — tie-break: presence of a proper-noun (capitalized, non-leading)
#            token nudges toward ner-kg, since /extract and /kg/query need a
#            concrete named entity to operate on.
#
# Tune these pattern lists against services/router/fixtures/routing_questions.json
# if measured accuracy on the fixture falls below the 0.80 bar.
# ---------------------------------------------------------------------------

_KG_RELATION_PATTERNS = [
    r"\brelated to\b", r"\bconnected to\b", r"\brelationship between\b",
    r"\bpath between\b", r"\blinked to\b", r"\bworks (for|at)\b",
    r"\bfounded\b", r"\backquired\b", r"\bsubsidiary of\b",
    r"\bparent company\b", r"\bconnection between\b",
]

_ENTITY_LOOKUP_PATTERNS = [
    r"\bwho is\b", r"\bwho are\b", r"\bwho was\b",
    r"\bwhich (company|organization|person)\b", r"\bname the\b",
    r"\bextract entities\b", r"\bceo of\b", r"\bfounder of\b",
    r"\bheadquartered in\b", r"\bbased in\b", r"\blocated in\b",
    r"\bwhen was .* (founded|born|created)\b",r"\bentities\b",
    r"\bentity\b",
    r"\borganization\b",
    r"\borganizations\b",
    r"\bcompany names\b",
    r"\bcompanies\b",
    r"\bpeople\b",
    r"\bproducts?\b",
    r"\bmentioned\b",
    r"\bfind every\b",
    r"\breturn the entities\b",
    r"\blist all\b",
]

_RAG_PATTERNS = [
    r"\bwhat is\b", r"\bwhat are\b", r"\bwhy\b", r"\bhow does\b",
    r"\bhow do\b", r"\bexplain\b", r"\bdescribe\b", r"\bsummarize\b",
    r"\bcompare\b", r"\bdefine\b", r"\bdifference between\b",
    r"\badvantages? of\b", r"\bbenefits? of\b",r"\bwalk me through\b",
    r"\brole of\b",
    r"\btrade-?off\b",
    r"\blatency\b",
    r"\bgrounding\b",
    r"\bretrieval\b",
    r"\btransformer\b",
    r"\battention\b",
]

_PROPER_NOUN_RE = re.compile(r"(?<!^)(?<!\. )\b[A-Z][a-zA-Z]+\b")


def _count_matches(patterns: list[str], text: str) -> int:
    return sum(1 for p in patterns if re.search(p, text))


def classify_question(question: str) -> Target:
    text = question.strip()
    lowered = text.lower()

    kg_score = _count_matches(_KG_RELATION_PATTERNS, lowered) * 2
    kg_score += _count_matches(_ENTITY_LOOKUP_PATTERNS, lowered)
    rag_score = _count_matches(_RAG_PATTERNS, lowered)

    if kg_score == rag_score == 0:
        # No lexical signal either way -- fall back to whether the question
        # names a concrete proper noun (a person/org/place), which is the
        # kind of thing /extract or /kg/query can act on.
        if _PROPER_NOUN_RE.search(text):
            return "ner-kg"
        return "rag"

    return "ner-kg" if kg_score >= rag_score else "rag"


_KG_QUERY_HINTS = re.compile(
    r"\brelated to\b|\bconnected to\b|\brelationship\b|\bpath between\b|"
    r"\blinked to\b|\bworks (for|at)\b|\bfounded\b|\backquired\b|"
    r"\bsubsidiary of\b|\bparent company\b|\bceo of\b|\bfounder of\b",
    re.IGNORECASE,
)
_QUOTED_OR_PROPER_RE = re.compile(r"['\"]([^'\"]+)['\"]|\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b")


def _question_to_cypher(question: str) -> str:
    """Best-effort translation of a question into a small lookup query.

    The ner-kg service does not implement real Cypher parsing -- it just
    extracts a quoted or capitalized entity name from this string -- so we
    only need to make sure that name is present and easy to find.
    """
    m = _QUOTED_OR_PROPER_RE.search(question)
    name = (m.group(1) or m.group(2)) if m else question.strip()
    return f"MATCH (a)-[r]->(b) WHERE a.name = '{name}' RETURN a, r, b"


async def forward_to_backend(target: Target, question: str, request_id: str) -> dict:
    headers = {"x-request-id": request_id}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            if target == "rag":
                resp = await client.post(
                    f"{RAG_URL}/rag/answer",
                    json={"question": question},
                    headers=headers,
                )
            else:
                if _KG_QUERY_HINTS.search(question):
                    cypher = _question_to_cypher(question)
                    resp = await client.post(
                        f"{NER_KG_URL}/kg/query",
                        json={"cypher": cypher},
                        headers=headers,
                    )
                else:
                    resp = await client.post(
                        f"{NER_KG_URL}/extract",
                        json={"text": question},
                        headers=headers,
                    )

            resp.raise_for_status()
            return resp.json()

    except httpx.HTTPError:
       
        return {
            "status": "backend unavailable",
            "target": target,
        }


@app.post("/route")
async def route(payload: RouteIn, request: Request) -> dict:
    request_id = request.state.request_id
    target = classify_question(payload.question)
    ROUTING_DECISIONS.labels(target).inc()
    backend_response = await forward_to_backend(target, payload.question, request_id)
    decision = {
        "request_id": request_id,
        "question": payload.question,
        "target": target,
        "ts": time.time(),
    }
    _DECISIONS.append(decision)
    return {"decision": decision, "backend_response": backend_response}


@app.get("/decisions")
def decisions(limit: int = 1000) -> dict:
    return {"decisions": list(_DECISIONS)[-limit:]}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)