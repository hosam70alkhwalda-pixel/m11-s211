# Multi-Service Routing Analysis Report

## Routing Accuracy

Routing accuracy on the held-out fixture: **1.000** (15/15 correct).

## Per-Service Metrics

| Service | Total Requests | Error Rate | p95 Latency (s) |
|---|---:|---:|---:|
| router | 18 | 0.00% | 0.0750 |
| ner-kg | 12 | 0.00% | 0.0050 |
| rag | 12 | 0.00% | 0.0050 |

## Routing Pattern

- `ner-kg`: 8/15 requests (53.3%)
- `rag`: 7/15 requests (46.7%)

Observed pattern: routing decisions are reasonably balanced across backends.

## Cross-Service Correlation

Each inbound request is assigned a `request_id` (uuid4) by the router's `request_id_middleware` and forwarded to the chosen backend as the `X-Request-ID` header. Both `ner-kg` and `rag` echo the same header back on their response and include it in every structured log line they emit for that request, so filtering `docker compose logs` on a single id yields a continuous trace across all three services. Example ids captured in this run: 2414ef65-235a-49c3-9a0b-8331ffebeba2, 8de38797-24d6-4297-9ba2-630539658196, 162715a7-7c82-4989-a7dd-ff7e78f44d30.
