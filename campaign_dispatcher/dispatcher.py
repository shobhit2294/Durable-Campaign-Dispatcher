"""Durable campaign dispatcher backed by a small SQLite state machine."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import uvicorn
from cryptography.fernet import Fernet
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from campaign_dispatcher.scheduling import IST, is_calling_time, priority

ROOT = Path(__file__).resolve().parents[1]
PROVIDER_FIXTURES = ROOT / "provider" / "fixtures"
LOG = logging.getLogger("dispatcher")
UTC = timezone.utc


def parse_time(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=IST)


class Store:
    """SQLite persistence. Every outbound call is reserved before network I/O."""

    def __init__(self, path: Path, config: dict[str, Any], encryption_key: str):
        self.path = path
        self.config = config
        self.campaigns = {row["campaign_id"]: row for row in config["campaigns"]}
        self.tenants = {row["tenant_id"]: row for row in config["tenants"]}
        self.fernet = Fernet(encryption_key.encode())
        self.fingerprint_key = hashlib.sha256(encryption_key.encode()).digest()
        self.db = sqlite3.connect(path, timeout=30, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self._schema()
        self._load_leads()

    def _schema(self) -> None:
        self.db.executescript("""
        CREATE TABLE IF NOT EXISTS leads (
            campaign_id TEXT NOT NULL, lead_id TEXT NOT NULL, phone TEXT NOT NULL, phone_key TEXT NOT NULL,
            attempts INTEGER NOT NULL DEFAULT 0, last_status TEXT NOT NULL DEFAULT '',
            last_disposition TEXT NOT NULL DEFAULT '', callback_at TEXT, retry_at TEXT,
            final_state TEXT, PRIMARY KEY (campaign_id, lead_id)
        );
        CREATE TABLE IF NOT EXISTS calls (
            request_id TEXT PRIMARY KEY, campaign_id TEXT NOT NULL, lead_id TEXT NOT NULL,
            phone TEXT NOT NULL, phone_key TEXT NOT NULL, status TEXT NOT NULL, accepted INTEGER NOT NULL DEFAULT 0,
            call_sid TEXT, attempt_no INTEGER NOT NULL, not_before TEXT,
            provider_status TEXT, disposition TEXT, callback_at TEXT, ended_at TEXT,
            updated_at TEXT NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_unresolved_call_per_phone
          ON calls(phone_key) WHERE status IN ('prepared','submitting','unknown','active');
        CREATE TABLE IF NOT EXISTS dnd_phones (phone_key TEXT PRIMARY KEY, seen_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS webhook_events (request_id TEXT PRIMARY KEY, received_at TEXT NOT NULL);
        """)

    def _load_leads(self) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            for campaign in self.campaigns.values():
                file_path = PROVIDER_FIXTURES / campaign["leads_file"]
                with file_path.open(newline="", encoding="utf-8") as source:
                    for row in csv.DictReader(source):
                        self.db.execute(
                            "INSERT OR IGNORE INTO leads(campaign_id, lead_id, phone, phone_key) VALUES (?, ?, ?, ?)",
                            (campaign["campaign_id"], row["lead_id"], self._encrypt(row["phone"]),
                             self._phone_key(row["phone"])),
                        )
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def _encrypt(self, phone: str) -> str:
        return self.fernet.encrypt(phone.encode()).decode()

    def _decrypt(self, encrypted: str) -> str:
        return self.fernet.decrypt(encrypted.encode()).decode()

    def _phone_key(self, phone: str) -> str:
        return hmac.new(self.fingerprint_key, phone.encode(), hashlib.sha256).hexdigest()

    def transaction(self):
        return self.db

    def unresolved(self) -> list[sqlite3.Row]:
        return list(self.db.execute("SELECT * FROM calls WHERE status IN ('prepared','submitting','unknown','active')"))

    def _mark_accepted(self, call: sqlite3.Row, payload: dict[str, Any]) -> None:
        current = self.db.execute("SELECT * FROM calls WHERE request_id=?", (call["request_id"],)).fetchone()
        if not current["accepted"]:
            self.db.execute("UPDATE calls SET accepted=1 WHERE request_id=?", (current["request_id"],))
            self.db.execute(
                "UPDATE leads SET attempts=attempts+1, callback_at=NULL, retry_at=NULL WHERE campaign_id=? AND lead_id=?",
                (current["campaign_id"], current["lead_id"]),
            )
        if current["status"] != "ended":
            self.db.execute(
                "UPDATE calls SET status='active', call_sid=COALESCE(?,call_sid), updated_at=? WHERE request_id=?",
                (payload.get("call_sid"), datetime.now(UTC).isoformat(), current["request_id"]),
            )

    def accept(self, request_id: str, payload: dict[str, Any]) -> None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            call = self.db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if call:
                self._mark_accepted(call, payload)
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def record_result(self, result: dict[str, Any]) -> None:
        request_id = result["request_id"]
        self.db.execute("BEGIN IMMEDIATE")
        try:
            if self.db.execute("SELECT 1 FROM webhook_events WHERE request_id=?", (request_id,)).fetchone():
                self.db.commit()
                return
            call = self.db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            if not call:
                # A webhook can beat the response, but our intent is always persisted first.
                raise ValueError(f"webhook for unknown request_id {request_id}")
            self._mark_accepted(call, {"call_sid": result.get("call_sid")})
            self.db.execute("""UPDATE calls SET status='ended', call_sid=?, provider_status=?, disposition=?,
                callback_at=?, ended_at=?, updated_at=? WHERE request_id=?""",
                (result.get("call_sid"), result.get("status"), result.get("disposition") or "",
                 result.get("callback_at"), result.get("ended_at"), datetime.now(UTC).isoformat(), request_id))
            campaign_id, lead_id = call["campaign_id"], call["lead_id"]
            status = result["status"]
            disposition = result.get("disposition") or ""
            callback_at = result.get("callback_at") if disposition == "callback_requested" else None
            retry_at = None
            if status in ("no_answer", "busy", "failed"):
                minutes = self.config["retry_policy"][f"{status}_minutes"]
                retry_at = (parse_time(result["ended_at"]) + timedelta(minutes=minutes)).isoformat()
            self.db.execute("""UPDATE leads SET last_status=?, last_disposition=?, callback_at=?, retry_at=?
                WHERE campaign_id=? AND lead_id=?""",
                (status, disposition, callback_at, retry_at, campaign_id, lead_id))
            if disposition == "dnd":
                self.db.execute("INSERT OR IGNORE INTO dnd_phones(phone_key,seen_at) VALUES (?,?)",
                                 (self._phone_key(result["phone"]), result["ended_at"]))
            self.db.execute("INSERT INTO webhook_events(request_id,received_at) VALUES (?,?)",
                             (request_id, datetime.now(UTC).isoformat()))
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def finalize_phone_blocks(self) -> None:
        # DND applies across campaigns; completed and exhausted states retain precedence.
        for lead in self.db.execute("SELECT * FROM leads WHERE final_state IS NULL").fetchall():
            if lead["last_disposition"] in ("interested", "not_interested", "wrong_number", "dnd"):
                continue
            if not self.db.execute("SELECT 1 FROM dnd_phones WHERE phone_key=?", (lead["phone_key"],)).fetchone():
                continue
            if self.db.execute("SELECT 1 FROM calls WHERE campaign_id=? AND lead_id=? AND status IN ('prepared','submitting','unknown','active')",
                               (lead["campaign_id"], lead["lead_id"])).fetchone():
                continue
            max_attempts = self.campaigns[lead["campaign_id"]]["max_attempts"]
            if lead["attempts"] < max_attempts:
                self.db.execute("UPDATE leads SET final_state='dnd_blocked' WHERE campaign_id=? AND lead_id=?",
                                 (lead["campaign_id"], lead["lead_id"]))

    def refresh_final_states(self) -> None:
        self.finalize_phone_blocks()
        for lead in self.db.execute("SELECT * FROM leads WHERE final_state IS NULL").fetchall():
            if self.db.execute("SELECT 1 FROM calls WHERE campaign_id=? AND lead_id=? AND status IN ('prepared','submitting','unknown','active')",
                               (lead["campaign_id"], lead["lead_id"])).fetchone():
                continue
            if lead["last_disposition"] in ("interested", "not_interested", "wrong_number", "dnd"):
                state = "completed"
            elif lead["attempts"] >= self.campaigns[lead["campaign_id"]]["max_attempts"]:
                state = "exhausted"
            else:
                continue
            self.db.execute("UPDATE leads SET final_state=? WHERE campaign_id=? AND lead_id=?",
                             (state, lead["campaign_id"], lead["lead_id"]))

    def reserve_next(self, now: datetime) -> sqlite3.Row | None:
        self.db.execute("BEGIN IMMEDIATE")
        try:
            self.refresh_final_states()
            unresolved = self.unresolved()
            occupied_phones = {row["phone_key"] for row in unresolved}
            occupied_tenants: dict[str, int] = {}
            for row in unresolved:
                tenant_id = self.campaigns[row["campaign_id"]]["tenant_id"]
                occupied_tenants[tenant_id] = occupied_tenants.get(tenant_id, 0) + 1
            available: list[tuple[tuple[int, datetime, str, str], sqlite3.Row]] = []
            for lead in self.db.execute("SELECT * FROM leads WHERE final_state IS NULL").fetchall():
                if lead["phone_key"] in occupied_phones:
                    continue
                if self.db.execute("SELECT 1 FROM calls WHERE campaign_id=? AND lead_id=? AND status IN ('prepared','submitting','unknown','active')",
                                   (lead["campaign_id"], lead["lead_id"])).fetchone():
                    continue
                campaign = self.campaigns[lead["campaign_id"]]
                waiting_transient = self.db.execute(
                    "SELECT not_before FROM calls WHERE campaign_id=? AND lead_id=? AND status='transient' ORDER BY attempt_no DESC LIMIT 1",
                    (lead["campaign_id"], lead["lead_id"]),
                ).fetchone()
                if waiting_transient and waiting_transient["not_before"] and parse_time(waiting_transient["not_before"]) > now:
                    continue
                tenant_id = campaign["tenant_id"]
                if occupied_tenants.get(tenant_id, 0) >= self.tenants[tenant_id]["max_concurrent_calls"]:
                    continue
                if self.db.execute("SELECT 1 FROM dnd_phones WHERE phone_key=?", (lead["phone_key"],)).fetchone():
                    continue
                if lead["attempts"] >= campaign["max_attempts"]:
                    continue
                if lead["callback_at"] and parse_time(lead["callback_at"]) > now:
                    continue
                if lead["retry_at"] and parse_time(lead["retry_at"]) > now:
                    continue
                available.append((priority(dict(lead), now), lead))
            selected = min(available, key=lambda item: item[0])[1] if available else None
            if selected is None:
                self.db.commit()
                return None
            campaign = self.campaigns[selected["campaign_id"]]
            # Keep a minute of headroom for request transit while the provider clock advances.
            if not is_calling_time(now + timedelta(seconds=60), campaign, self.config["regulatory_window"]):
                self.db.commit()
                return None
            attempt_no = selected["attempts"] + 1
            request_id = f"{selected['campaign_id']}:{selected['lead_id']}:{attempt_no}"
            self.db.execute("""INSERT OR IGNORE INTO calls(request_id,campaign_id,lead_id,phone,phone_key,status,attempt_no,updated_at)
                VALUES (?,?,?,?,?,?,?,?)""",
                (request_id, selected["campaign_id"], selected["lead_id"], selected["phone"], selected["phone_key"], "prepared",
                 attempt_no, datetime.now(UTC).isoformat()))
            call = self.db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
            self.db.commit()
            return call
        except BaseException:
            self.db.rollback()
            raise

    def update_call_state(self, request_id: str, status: str, **values: Any) -> None:
        allowed = {"call_sid", "not_before"}
        update_fields = ["status=?", "updated_at=?"]
        params: list[Any] = [status, datetime.now(UTC).isoformat()]
        for key, value in values.items():
            if key not in allowed:
                raise ValueError(f"unsupported call field {key}")
            update_fields.append(f"{key}=?")
            params.append(value)
        params.append(request_id)
        # A webhook can arrive before a slow/ambiguous POST response. Never regress an ended call.
        self.db.execute(f"UPDATE calls SET {', '.join(update_fields)} WHERE request_id=? AND status!='ended'", params)

    def all_done(self) -> bool:
        self.refresh_final_states()
        pending = self.db.execute("SELECT COUNT(*) FROM leads WHERE final_state IS NULL").fetchone()[0]
        active = self.db.execute("SELECT COUNT(*) FROM calls WHERE status IN ('prepared','submitting','unknown','active')").fetchone()[0]
        return pending == 0 and active == 0

    def progress(self) -> dict[str, Any]:
        counts = {row["final_state"] or "pending": row["n"] for row in self.db.execute(
            "SELECT final_state, COUNT(*) AS n FROM leads GROUP BY final_state")}
        return {"leads": counts, "active_calls": len(self.unresolved())}

    def write_output(self, path: Path) -> None:
        self.refresh_final_states()
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.db.execute("SELECT * FROM leads ORDER BY campaign_id,lead_id").fetchall()
        with path.open("w", newline="", encoding="utf-8") as target:
            writer = csv.writer(target)
            writer.writerow(("campaign_id", "lead_id", "final_state", "attempts", "last_status", "last_disposition"))
            for row in rows:
                writer.writerow((row["campaign_id"], row["lead_id"], row["final_state"], row["attempts"],
                                 row["last_status"], row["last_disposition"]))


class SimulatedClock:
    def __init__(self) -> None:
        self.sim_now = parse_time(json.loads((PROVIDER_FIXTURES / "campaigns.json").read_text())["sim_start"])
        self.speed = 600.0
        self.synced_at = time.monotonic()

    def now(self) -> datetime:
        return self.sim_now + timedelta(seconds=(time.monotonic() - self.synced_at) * self.speed)

    async def sync(self, client: httpx.AsyncClient, base_url: str) -> None:
        response = await client.get(f"{base_url}/v1/clock")
        response.raise_for_status()
        data = response.json()
        self.sim_now = parse_time(data["sim_now"])
        self.speed = float(data["speed"])
        self.synced_at = time.monotonic()


async def resolve_unfinished(store: Store, client: httpx.AsyncClient, base_url: str) -> None:
    for call in store.unresolved():
        if call["status"] in ("prepared", "submitting", "unknown"):
            try:
                response = await client.get(f"{base_url}/v1/calls", params={"request_id": call["request_id"]})
                if response.status_code == 200:
                    payload = response.json()
                    store.accept(call["request_id"], payload)
                    if payload.get("state") == "ended" and payload.get("result"):
                        store.record_result(payload["result"])
                elif response.status_code == 404:
                    await place_call(store, client, base_url, call["request_id"])
            except httpx.HTTPError:
                LOG.warning("Could not reconcile request %s; will retry", call["request_id"])
        elif call["status"] == "active" and call["call_sid"]:
            try:
                response = await client.get(f"{base_url}/v1/calls/{call['call_sid']}")
                if response.status_code == 200:
                    payload = response.json()
                    if payload.get("state") == "ended" and payload.get("result"):
                        store.record_result(payload["result"])
            except httpx.HTTPError:
                LOG.warning("Could not poll call %s; will retry", call["call_sid"])


async def place_call(store: Store, client: httpx.AsyncClient, base_url: str, request_id: str) -> None:
    call = store.db.execute("SELECT * FROM calls WHERE request_id=?", (request_id,)).fetchone()
    if not call:
        return
    if call["status"] == "error":
        raise RuntimeError(f"Previous provider attempt failed for {request_id}; inspect the provider error above.")
    campaign = store.campaigns[call["campaign_id"]]
    try:
        clock_response = await client.get(f"{base_url}/v1/clock")
        clock_response.raise_for_status()
        provider_now = parse_time(clock_response.json()["sim_now"])
    except httpx.HTTPError:
        # Without the provider clock, do not risk an out-of-window call.
        return
    if not is_calling_time(provider_now + timedelta(seconds=60), campaign, store.config["regulatory_window"]):
        # The same durable request ID will be selected again when a window opens.
        store.update_call_state(request_id, "deferred")
        return
    store.update_call_state(request_id, "submitting")
    body = {"request_id": request_id, "campaign_id": call["campaign_id"], "lead_id": call["lead_id"],
            "phone": store._decrypt(call["phone"]),
            "callback_url": os.getenv("CALLBACK_URL", "http://127.0.0.1:8200/webhooks/telephony")}
    try:
        response = await client.post(f"{base_url}/v1/calls", json=body, timeout=3.0)
    except httpx.HTTPError:
        store.update_call_state(request_id, "unknown")
        return
    if response.status_code in (200, 201):
        store.accept(request_id, response.json())
    elif response.status_code == 503:
        store.update_call_state(request_id, "transient", not_before=(provider_now + timedelta(minutes=1)).isoformat())
    elif response.status_code == 504:
        store.update_call_state(request_id, "unknown")
    else:
        detail = response.text[:300]
        store.update_call_state(request_id, "error")
        LOG.error("Provider rejected %s with %s: %s", request_id, response.status_code, detail)
        raise RuntimeError(f"Provider rejected {request_id} with HTTP {response.status_code}.")


async def dispatch_loop(app: FastAPI) -> None:
    store: Store = app.state.store
    client: httpx.AsyncClient = app.state.client
    clock: SimulatedClock = app.state.clock
    base_url: str = app.state.base_url
    last_log = 0.0
    try:
        while True:
            await clock.sync(client, base_url)
            await resolve_unfinished(store, client, base_url)
            # Transient 503s are retried with the same idempotency key after a simulated minute.
            now = clock.now()
            transient = store.db.execute("SELECT * FROM calls WHERE status='transient'").fetchall()
            for call in transient:
                if not call["not_before"] or parse_time(call["not_before"]) <= now:
                    store.db.execute("UPDATE calls SET status='prepared' WHERE request_id=?", (call["request_id"],))
                    await place_call(store, client, base_url, call["request_id"])
            call = store.reserve_next(now)
            if call:
                await place_call(store, client, base_url, call["request_id"])
            store.refresh_final_states()
            if time.monotonic() - last_log >= 10:
                LOG.info("Dispatcher progress: %s", json.dumps(store.progress(), sort_keys=True))
                last_log = time.monotonic()
            if store.all_done():
                output = Path(os.getenv("FINAL_STATE_PATH", str(ROOT / "final_state.csv")))
                store.write_output(output)
                LOG.info("All leads final; wrote %s", output)
                if app.state.server:
                    app.state.server.should_exit = True
                return
            await asyncio.sleep(0.12)
    except asyncio.CancelledError:
        raise
    except Exception:
        LOG.exception("Dispatcher loop stopped after an unexpected error")
        if app.state.server:
            app.state.server.should_exit = True
        raise


def create_app() -> FastAPI:
    config = json.loads((PROVIDER_FIXTURES / "campaigns.json").read_text(encoding="utf-8"))
    db_path = Path(os.getenv("DISPATCHER_DB", str(ROOT / "dispatcher.sqlite")))
    encryption_key = os.getenv("DISPATCHER_FERNET_KEY")
    if not encryption_key:
        raise RuntimeError("Set DISPATCHER_FERNET_KEY before starting; use Fernet.generate_key() and keep it private.")
    store = Store(db_path, config, encryption_key)
    base_url = os.getenv("TELEPHONY_URL", "http://127.0.0.1:8099").rstrip("/")
    clock = SimulatedClock()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.client = httpx.AsyncClient(timeout=2.0)
        app.state.dispatch_task = asyncio.create_task(dispatch_loop(app))
        try:
            yield
        finally:
            app.state.dispatch_task.cancel()
            try:
                await app.state.dispatch_task
            except asyncio.CancelledError:
                pass
            await app.state.client.aclose()
            store.db.close()

    app = FastAPI(title="Durable Campaign Dispatcher", lifespan=lifespan)
    app.state.store = store
    app.state.base_url = base_url
    app.state.clock = clock
    app.state.server = None

    @app.post("/webhooks/telephony")
    async def telephony_webhook(request: Request):
        raw = await request.body()
        signature = request.headers.get("X-AU-Signature", "")
        expected = hmac.new(config["webhook_secret"].encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=401, detail="invalid webhook signature")
        try:
            result = json.loads(raw)
            if result.get("event") != "call.completed":
                raise ValueError("unsupported event")
            store.record_result(result)
        except (KeyError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("Rejected malformed webhook: %s", error)
            raise HTTPException(status_code=400, detail="invalid webhook") from error
        return JSONResponse({"ok": True})

    @app.get("/status")
    async def status():
        return {"progress": store.progress(), "sim_now": clock.now().isoformat()}

    return app


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host="0.0.0.0", port=int(os.getenv("PORT", "8200")), log_level="info"))
    app.state.server = server
    server.run()


if __name__ == "__main__":
    main()
