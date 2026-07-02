"""ner-kg service — extracts entities and answers KG queries.

Exposes:
- POST /extract   — entity extraction from a text
- POST /kg/query  — small KG lookup
- GET  /metrics   — Prometheus text format

Honors Track — TODO implementations required. The starter wires the FastAPI
surface, instrumentation, and the request-id header echo; learners implement
the actual extraction + KG logic.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid

from fastapi import FastAPI, Request
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Histogram, generate_latest
from pydantic import BaseModel
from starlette.responses import Response

SERVICE = os.environ.get("SERVICE_NAME", "ner-kg")
app = FastAPI()

# Structured logger -- emits one JSON line per request that includes the
# X-Request-ID so cross-service logs can be joined by id (Task 4 in the
# learner guide). The handler ships configured at INFO; the middleware
# below writes one log line per response.
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(SERVICE)


REQUESTS = Counter(
    "service_requests_total", "Requests per endpoint", ["service", "endpoint", "status"]
)
LATENCY = Histogram(
    "service_request_latency_seconds",
    "Request latency by endpoint",
    ["service", "endpoint"],
)


@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    request_id = request.headers.get("x-request-id") or str(uuid.uuid4())
    start = time.perf_counter()
    response = await call_next(request)
    elapsed_ms = (time.perf_counter() - start) * 1000.0
    LATENCY.labels(SERVICE, request.url.path).observe(elapsed_ms / 1000.0)
    REQUESTS.labels(SERVICE, request.url.path, str(response.status_code)).inc()
    response.headers["x-request-id"] = request_id
    logger.info(json.dumps({
        "service": SERVICE,
        "request_id": request_id,
        "path": request.url.path,
        "status": response.status_code,
        "latency_ms": round(elapsed_ms, 3),
    }))
    return response


class ExtractIn(BaseModel):
    text: str


class KgQueryIn(BaseModel):
    cypher: str


# ---------------------------------------------------------------------------
# Rule-based NER (no external model dependency -> fast, deterministic, no
# cold-start cost inside a container). Two signal sources:
#  1. Regex gazetteers for common suffix/title patterns (ORG, PERSON, DATE).
#  2. A capitalized-token-run heuristic for anything else -> MISC.
# ---------------------------------------------------------------------------

_ORG_SUFFIXES = r"(?:Inc|Corp|Corporation|LLC|Ltd|Co|Company|Group|Labs|AI)\.?"
_TITLES = r"(?:Mr|Mrs|Ms|Dr|Prof)\."
_MONTHS = (
    "January|February|March|April|May|June|July|August|September|October|"
    "November|December"
)

_DATE_RE = re.compile(
    rf"\b(?:{_MONTHS})\s+\d{{1,2}},?\s+\d{{4}}\b|\b\d{{4}}-\d{{2}}-\d{{2}}\b|\b\d{{4}}\b"
)
_ORG_RE = re.compile(rf"\b([A-Z][\w&.]*(?:\s+[A-Z][\w&.]*)*\s+{_ORG_SUFFIXES})")
_PERSON_RE = re.compile(rf"\b{_TITLES}\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)")
_CAP_RUN_RE = re.compile(r"\b([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)*)\b")

_STOPWORDS_AT_START = {
    "The", "A", "An", "Who", "What", "Where", "When", "Why", "How", "Which",
    "Is", "Are", "Do", "Does",
}


def _extract_entities(text: str) -> list[dict[str, str]]:
    found: dict[str, str] = {}

    for m in _DATE_RE.finditer(text):
        found.setdefault(m.group(0).strip(), "DATE")

    for m in _ORG_RE.finditer(text):
        found.setdefault(m.group(1).strip(), "ORG")

    for m in _PERSON_RE.finditer(text):
        found.setdefault(m.group(1).strip(), "PERSON")

    for m in _CAP_RUN_RE.finditer(text):
        span = m.group(1).strip()
        if span in found:
            continue
        # Skip a capitalized word only because it starts the sentence.
        first_word = span.split()[0]
        if len(span.split()) == 1 and first_word in _STOPWORDS_AT_START:
            continue
        found[span] = "MISC"

    return [{"text": t, "label": lbl} for t, lbl in found.items()]


@app.post("/extract")
def extract(payload: ExtractIn) -> dict:
    entities = _extract_entities(payload.text)
    return {"entities": entities}


# ---------------------------------------------------------------------------
# Small in-memory knowledge graph used to answer /kg/query. Real assignments
# typically back this with Neo4j (see Module 9B); here we keep a toy graph so
# the service is self-contained and works without extra infra.
# ---------------------------------------------------------------------------

_KG: dict[str, list[dict[str, str]]] = {
    "OpenAI": [
        {"relation": "CEO", "target": "Sam Altman"},
        {"relation": "TYPE", "target": "AI research company"},
        {"relation": "HEADQUARTERED_IN", "target": "San Francisco"},
    ],
    "Anthropic": [
        {"relation": "CEO", "target": "Dario Amodei"},
        {"relation": "TYPE", "target": "AI safety company"},
        {"relation": "HEADQUARTERED_IN", "target": "San Francisco"},
    ],
    "Sam Altman": [
        {"relation": "CEO_OF", "target": "OpenAI"},
    ],
    "Dario Amodei": [
        {"relation": "CEO_OF", "target": "Anthropic"},
    ],
}

_NAME_IN_QUERY_RE = re.compile(r"['\"]([^'\"]+)['\"]|name\s*[:=]\s*([A-Za-z][\w\s]*)")


def _entity_from_cypher(cypher: str) -> str | None:
    m = _NAME_IN_QUERY_RE.search(cypher)
    if not m:
        return None
    return (m.group(1) or m.group(2) or "").strip()


@app.post("/kg/query")
def kg_query(payload: KgQueryIn) -> dict:
    entity = _entity_from_cypher(payload.cypher)
    if entity and entity in _KG:
        rows = [{"source": entity, **edge} for edge in _KG[entity]]
    else:
        rows = []
    return {"rows": rows}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)