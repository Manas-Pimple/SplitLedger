"""Prometheus metrics. Phase 5 creates the pipeline metrics; Phase 11 completes
the OBSERVABILITY.md §1 set and exposes /metrics on an internal port."""

import os

from prometheus_client import Counter, Gauge, Histogram

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
