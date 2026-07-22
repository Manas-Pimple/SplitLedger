"""Prometheus metrics. Phase 5 creates the pipeline metrics; Phase 11 completes
the OBSERVABILITY.md §1 set and exposes /metrics on an internal port."""

import os

from prometheus_client import Counter, Gauge, Histogram, make_asgi_app

WORKER = str(os.getpid())

OUTBOX_PENDING = Gauge(
    "outbox_pending", "Events awaiting relay to Redis — key pipeline health signal"
)
OUTBOX_PUBLISH_LATENCY = Histogram(
    "outbox_publish_latency_seconds", "Delay from event commit to Redis publish"
)
REDIS_PUBLISH_TOTAL = Counter(
    "redis_publish_total", "Events published to Redis", ["worker"]
)
REDIS_RECEIVE_TOTAL = Counter(
    "redis_receive_total", "Events received from Redis backplane", ["worker"]
)
WS_CONNECTIONS_ACTIVE = Gauge(
    "ws_connections_active", "Open WebSocket connections", ["worker"]
)
WS_MESSAGES_SENT_TOTAL = Counter(
    "ws_messages_sent_total", "Messages fanned out to sockets", ["event_type"]
)
WS_SEND_QUEUE_DROPPED_TOTAL = Counter(
    "ws_send_queue_dropped_total", "Slow-consumer disconnects (send queue overflow)"
)
HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total", "HTTP requests", ["method", "route", "status"]
)
HTTP_REQUEST_DURATION = Histogram(
    "http_request_duration_seconds", "HTTP request duration", ["route"]
)
SCHEDULER_TICK_DURATION = Histogram(
    "scheduler_tick_duration_seconds", "Scheduler sub-job duration", ["job"]
)
SCHEDULER_BILLS_GENERATED_TOTAL = Counter(
    "scheduler_bills_generated_total", "Recurring bill instances generated"
)
SCHEDULER_REMINDERS_SENT_TOTAL = Counter(
    "scheduler_reminders_sent_total", "Payment reminders sent"
)
DB_POOL_IN_USE = Gauge("db_pool_in_use", "SQLAlchemy connections checked out of the pool")
LEDGER_INVARIANT_VIOLATIONS_TOTAL = Counter(
    "ledger_invariant_violations_total",
    "DB-level zero-sum/share-sum trigger violations — must stay 0",
)

# Served on an internal-only port (see app.observability.serve_metrics_server),
# never through Caddy.
metrics_app = make_asgi_app()
