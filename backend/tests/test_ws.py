"""Phase 6 done-gates (WEBSOCKET_PROTOCOL §7):
- two-worker test: expense created via worker B's REST port arrives on a socket
  held by worker A (Redis backplane, not process memory)
- gap/replay: reconnect with last_seq replays missed events
- Redis outage: outbox retains, client seq-gap recovery heals after restart
- protocol basics: bad auth 4401, unknown type → error not disconnect,
  membership revocation → close 4403

Spawns two real uvicorn workers against the test database.
"""

import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Iterator
from typing import Any
from uuid import uuid4

import httpx
import pytest
import websockets

PORT_A = 8151
PORT_B = 8152


@pytest.fixture(scope="module")
def workers(test_database: str) -> Iterator[tuple[str, str]]:
    env = {
        **os.environ,
        "DATABASE_URL": test_database,
        "REDIS_URL": "redis://localhost:6380/0",
    }
    procs = [
        subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port)],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for port in (PORT_A, PORT_B)
    ]
    base_a, base_b = (f"http://localhost:{p}/api/v1" for p in (PORT_A, PORT_B))
    for base in (base_a, base_b):
        for _ in range(50):
            try:
                if httpx.get(f"{base}/healthz", timeout=1).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            import time

            time.sleep(0.2)
        else:
            for p in procs:
                p.terminate()
            pytest.fail(f"worker at {base} never became healthy")
    yield base_a, base_b
    for p in procs:
        p.terminate()
        p.wait(timeout=5)


def _key() -> dict[str, str]:
    return {"Idempotency-Key": str(uuid4())}


async def _user(base: str, name: str) -> dict[str, str]:
    email = f"{name}-{uuid4().hex[:8]}@example.com"
    async with httpx.AsyncClient(base_url=base) as c:
        await c.post(
            "/auth/register",
            json={"email": email, "password": "hunter2!", "display_name": name},
            headers=_key(),
        )
        tokens = (
            await c.post(
                "/auth/login", json={"email": email, "password": "hunter2!"}, headers=_key()
            )
        ).json()
        me = (
            await c.get("/me", headers={"Authorization": f"Bearer {tokens['access_token']}"})
        ).json()
    return {"token": tokens["access_token"], "id": me["id"]}


async def _post(base: str, token: str, path: str, body: dict[str, Any] | None = None) -> Any:
    async with httpx.AsyncClient(base_url=base) as c:
        r = await c.post(
            path,
            json=body or {},
            headers={"Authorization": f"Bearer {token}", **_key()},
        )
        assert r.status_code < 400, f"{path}: {r.status_code} {r.text}"
        return r.json() if r.content else None


async def _connect(port: int, token: str) -> Any:
    ws = await websockets.connect(f"ws://localhost:{port}/api/v1/ws")
    await ws.send(json.dumps({"type": "auth", "token": token}))
    hello = json.loads(await asyncio.wait_for(ws.recv(), 5))
    assert hello["type"] == "auth.ok"
    return ws


async def _recv_type(ws: Any, ftype: str, wait: float = 5) -> dict[str, Any]:
    """Next frame of the given type, answering pings along the way."""
    while True:
        frame: dict[str, Any] = json.loads(await asyncio.wait_for(ws.recv(), wait))
        if frame["type"] == ftype:
            return frame
        if frame["type"] == "ping":
            await ws.send(json.dumps({"type": "pong"}))


async def _recv_events(ws: Any, n: int, wait: float = 10) -> list[dict[str, Any]]:
    """Collect n frames of type 'event', skipping pings/acks."""
    out: list[dict[str, Any]] = []
    while len(out) < n:
        frame = json.loads(await asyncio.wait_for(ws.recv(), wait))
        if frame["type"] == "event":
            out.append(frame)
        elif frame["type"] == "ping":
            await ws.send(json.dumps({"type": "pong"}))
    return out


async def _make_house(base: str, token: str) -> tuple[str, str]:
    house = await _post(base, token, "/houses", {"name": "WS House"})
    rule = await _post(
        base, token, f"/houses/{house['id']}/split-rules", {"name": "eq", "kind": "equal"}
    )
    return house["id"], rule["id"]


async def test_two_worker_fanout_and_replay(workers: tuple[str, str]) -> None:
    base_a, base_b = workers
    anna = await _user(base_b, "anna")
    house_id, rule_id = await _make_house(base_b, anna["token"])

    # socket on worker A, expense via worker B → must cross the backplane
    ws = await _connect(PORT_A, anna["token"])
    await ws.send(json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": 0}))
    await _recv_type(ws, "subscribed")

    await _post(
        base_b,
        anna["token"],
        f"/houses/{house_id}/expenses",
        {
            "description": "cross-worker",
            "category": "other",
            "amount_cents": 5000,
            "split_rule_id": rule_id,
        },
    )
    events = await _recv_events(ws, 2)
    assert {e["event_type"] for e in events} == {"expense.created", "balance.updated"}
    assert events[0]["house_id"] == house_id
    last_seq = max(e["seq"] for e in events)
    await ws.close()

    # gap/replay: miss an expense while disconnected, reconnect with last_seq
    await _post(
        base_b,
        anna["token"],
        f"/houses/{house_id}/expenses",
        {
            "description": "missed while offline",
            "category": "other",
            "amount_cents": 700,
            "split_rule_id": rule_id,
        },
    )
    ws = await _connect(PORT_A, anna["token"])
    await ws.send(
        json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": last_seq})
    )
    replayed = await _recv_events(ws, 2)
    assert [e["seq"] for e in replayed] == [last_seq + 1, last_seq + 2]
    assert replayed[0]["event_type"] == "expense.created"
    assert replayed[0]["payload"]["expense"]["description"] == "missed while offline"
    await ws.close()


async def test_redis_outage_client_recovers(workers: tuple[str, str]) -> None:
    base_a, base_b = workers
    anna = await _user(base_b, "outage")
    house_id, rule_id = await _make_house(base_b, anna["token"])

    ws = await _connect(PORT_A, anna["token"])
    await ws.send(json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": 0}))
    frame = await _recv_type(ws, "subscribed")
    last_seq = frame["current_seq"]

    await asyncio.to_thread(
        subprocess.run, ["docker", "stop", "splitledger-redis-1"], check=True, capture_output=True
    )
    try:
        # REST keeps working: outbox holds the events while Redis is down
        await _post(
            base_b,
            anna["token"],
            f"/houses/{house_id}/expenses",
            {
                "description": "written during outage",
                "category": "other",
                "amount_cents": 1200,
                "split_rule_id": rule_id,
            },
        )
    finally:
        await asyncio.to_thread(
            subprocess.run,
            ["docker", "start", "splitledger-redis-1"],
            check=True,
            capture_output=True,
        )

    # documented recovery path: client detects silence/gap and resubscribes
    await asyncio.sleep(3)  # relay backoff + consumer reconnect
    await ws.send(
        json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": last_seq})
    )
    events = await _recv_events(ws, 2, wait=20)
    assert events[0]["event_type"] == "expense.created"
    assert events[0]["payload"]["expense"]["description"] == "written during outage"
    await ws.close()


async def test_protocol_basics_and_eviction(workers: tuple[str, str]) -> None:
    base_a, base_b = workers

    # bad token → 4401
    ws = await websockets.connect(f"ws://localhost:{PORT_A}/api/v1/ws")
    await ws.send(json.dumps({"type": "auth", "token": "garbage"}))
    with pytest.raises(websockets.ConnectionClosed) as closed:
        await asyncio.wait_for(ws.recv(), 5)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4401

    # unknown message type → error frame, NOT a disconnect
    anna = await _user(base_b, "proto")
    house_id, rule_id = await _make_house(base_b, anna["token"])
    ws = await _connect(PORT_A, anna["token"])
    await ws.send(json.dumps({"type": "wat"}))
    frame = await _recv_type(ws, "error")
    assert frame == {"type": "error", "code": "UNSUPPORTED"}
    await ws.send(json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": None}))
    await _recv_type(ws, "subscribed")  # connection survived the unknown frame
    await ws.close()

    # eviction: ben joins, subscribes, then leaves — socket closed 4403
    ben = await _user(base_b, "ben")
    invite = await _post(base_b, anna["token"], f"/houses/{house_id}/invites")
    await _post(base_b, ben["token"], f"/invites/{invite['code']}/accept")
    ben_ws = await _connect(PORT_A, ben["token"])
    await ben_ws.send(
        json.dumps({"type": "subscribe", "house_id": house_id, "last_seq": None})
    )
    await _recv_type(ben_ws, "subscribed")

    await _post(base_b, ben["token"], f"/houses/{house_id}/members/{ben['id']}/leave")
    with pytest.raises(websockets.ConnectionClosed) as closed:
        while True:  # drain member.joined etc. until close
            await asyncio.wait_for(ben_ws.recv(), 10)
    assert closed.value.rcvd is not None and closed.value.rcvd.code == 4403
