"""rag service — answers questions over a small grounded corpus.

Exposes:
- POST /rag/answer — RAG answer endpoint
- GET  /metrics    — Prometheus text format

Honors Track — TODO implementations required.
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

SERVICE = os.environ.get("SERVICE_NAME", "rag")
app = FastAPI()

# Structured logger -- emits one JSON line per request that includes the
# X-Request-ID so cross-service logs can be joined by id (Task 4 in the
# learner guide).
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


class AnswerIn(BaseModel):
    question: str


# ---------------------------------------------------------------------------
# Small grounded corpus. NOTE: if your repo already ships a corpus fixture
# (e.g. services/rag/fixtures/corpus.json or data/corpus.json) from an
# earlier Module 10/11 lab, load it here instead of this inline list so the
# answers stay consistent with what the rest of the course expects:
#
#   with open(os.path.join(os.path.dirname(__file__), "corpus.json")) as f:
#       CORPUS = json.load(f)
# ---------------------------------------------------------------------------

CORPUS: list[dict[str, str]] = [
    {
        "id": "doc-fastapi",
        "text": (
            "FastAPI is a Python web framework for building APIs. It uses "
            "type hints and Pydantic models for request validation and "
            "generates interactive OpenAPI documentation automatically."
        ),
    },
    {
        "id": "doc-docker-compose",
        "text": (
            "Docker Compose defines multi-container applications in a single "
            "YAML file. Services can be placed on a shared network, given "
            "healthchecks, and started together with docker compose up."
        ),
    },
    {
        "id": "doc-prometheus",
        "text": (
            "Prometheus scrapes metrics exposed by services in the OpenMetrics "
            "text format. Common metric types are counters, gauges, and "
            "histograms, which support latency percentile calculations."
        ),
    },
    {
        "id": "doc-observability",
        "text": (
            "Distributed observability combines metrics, structured logs, and "
            "a correlation id propagated across service boundaries so a "
            "single request can be traced end to end."
        ),
    },
    {
        "id": "doc-microservices",
        "text": (
            "Splitting a monolith into microservices increases deployment "
            "flexibility and fault isolation, but adds operational cost such "
            "as network calls, service discovery, and distributed tracing."
        ),
    },
    {
        "id": "doc-knowledge-graph",
        "text": (
            "A knowledge graph stores entities as nodes and relationships as "
            "edges, and is commonly queried with a graph query language such "
            "as Cypher in systems like Neo4j."
        ),
    },
]

_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "of", "to", "in", "on",
    "for", "and", "or", "what", "which", "who", "how", "does", "do", "with",
    "this", "that", "it", "its", "be", "as", "by", "at", "from",
}

DECLINE = "I don't have enough grounded information to answer that."
_MIN_OVERLAP_SCORE = 1


def _tokenize(text: str) -> set[str]:
    words = re.findall(r"[a-zA-Z0-9]+", text.lower())
    return {w for w in words if w not in _STOPWORDS}


def _retrieve(question: str) -> tuple[dict[str, str] | None, int]:
    q_tokens = _tokenize(question)
    best_doc, best_score = None, 0
    for doc in CORPUS:
        overlap = len(q_tokens & _tokenize(doc["text"]))
        if overlap > best_score:
            best_doc, best_score = doc, overlap
    return best_doc, best_score


@app.post("/rag/answer")
def answer(payload: AnswerIn) -> dict:
    doc, score = _retrieve(payload.question)
    if doc is None or score < _MIN_OVERLAP_SCORE:
        return {"answer": DECLINE, "sources": []}
    return {"answer": doc["text"], "sources": [doc["id"]]}


@app.get("/metrics")
def metrics() -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)