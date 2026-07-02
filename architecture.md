# Architecture Write-up: Monolith vs. Microservices

## Overview

This project decomposes a single monolithic backend into three independent FastAPI services: **ner-kg**, **rag**, and **router**. Instead of having one application responsible for every task, each service focuses on a specific responsibility. The router acts as the entry point for client requests, classifies each question, generates a shared request identifier, and forwards the request to the appropriate backend service.

## What the Three-Service Split Provides

Splitting the application into three services improves modularity and separation of concerns. The **ner-kg** service is responsible only for entity extraction and knowledge graph queries, while the **rag** service focuses exclusively on retrieval-augmented generation. The **router** contains the routing logic and determines which backend should process each request. Because each service has a single responsibility, the codebase becomes easier to understand, maintain, and extend.

Another important advantage is independent scalability. If the RAG service receives significantly more traffic than the NER/KG service, it can be scaled separately without increasing resources for the entire application. Likewise, updates to one service can often be deployed without modifying the others, reducing deployment risk.

The distributed design also improves observability. Each service exposes its own `/metrics` endpoint, allowing Prometheus to collect service-specific metrics. In addition, the router generates a unique `X-Request-ID` that is propagated to downstream services. This correlation identifier makes it possible to trace a single request across multiple services, simplifying debugging and performance analysis.

## Costs of the Microservice Architecture

Although microservices provide flexibility, they also introduce additional complexity. Communication between services now occurs over the network instead of through local function calls. This increases latency and creates additional failure points, since one unavailable service can affect the overall system.

Deployment is also more complicated than with a monolithic application. Multiple containers must be configured, health checks must be maintained, environment variables must be managed correctly, and Docker Compose is required to orchestrate the services. Logging and monitoring become more challenging because information is distributed across multiple applications rather than being available in a single process.

Testing also becomes more involved. Individual services must be tested independently, while integration tests are required to verify that the router correctly communicates with the backend services and that request identifiers are propagated throughout the system.

## Monolith vs. Microservices for This Stack

A monolithic architecture would be simpler for a project of this size because all functionality would exist in a single application. Development, deployment, and debugging would generally require less operational effort.

However, this project demonstrates why a microservice architecture is valuable for larger AI systems. Different components often have different workloads, dependencies, and scaling requirements. Separating routing, knowledge graph operations, and retrieval-augmented generation makes the system more flexible, easier to evolve, and better suited for future expansion.

## Conclusion

For this project, decomposing the backend into the **router**, **ner-kg**, and **rag** services provides clear separation of responsibilities, better scalability, and improved observability through distributed metrics and request correlation. These benefits come at the cost of increased operational complexity, additional network communication, and more sophisticated deployment and testing. Overall, while a monolithic architecture remains appropriate for small applications, the microservice architecture offers significant long-term advantages for distributed AI systems such as the one implemented in this assignment.