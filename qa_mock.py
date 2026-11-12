"""Loopback game double. All input, events and state are simulated."""

import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit
from uuid import UUID, uuid4

from qa_nodes import compile_node

MAPPINGS = {"W": "IA_Move", "SpaceBar": "IA_Jump", "LeftShift": "IA_Sprint"}
MOCK_CATALOG = {"MOVE_FORWARD": "IA_Move", "JUMP": "IA_Jump", "SPRINT": "IA_Sprint"}
MAX_BODY = 8192
LEASE_MS = 750
INPUT_OWNERS = ("SCRIPTED", "DECISIONS", "MANUAL")


class Rejected(ValueError):
    pass


def read_json(handler):
    if handler.headers.get_content_type() != "application/json":
        raise Rejected("JSON_REQUIRED")
    size = int(handler.headers.get("Content-Length", "0"))
    if not 0 < size <= MAX_BODY:
        raise Rejected("INVALID_BODY_SIZE")
    handler.connection.settimeout(3)
    value = json.loads(handler.rfile.read(size))
    if not isinstance(value, dict):
        raise Rejected("OBJECT_REQUIRED")
    return value


def reply(handler, status, value):
    data = json.dumps(value, ensure_ascii=False).encode()
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(data)))
    handler.end_headers()
    handler.wfile.write(data)


def validate_step(keys, hold_ms, mappings=None):
    mappings = MAPPINGS if mappings is None else mappings
    if (not isinstance(keys, list) or not 1 <= len(keys) <= len(mappings)
            or any(not isinstance(key, str) or key not in mappings for key in keys)
            or len(set(keys)) != len(keys)):
        raise Rejected("UNMAPPED_OR_DUPLICATE_KEY")
    if type(hold_ms) is not int or not 20 <= hold_ms <= 2000:
        raise Rejected("HOLD_MS_OUT_OF_RANGE")


class MockGame:
    def __init__(self):
        self.lock = threading.Lock()
        self.session_id = None
        self.world = 0
        self.active = None
        self.held = {}
        self.commands = {}
        self.events = []
        self.position = 0
        self.jumps = 0
        self.mapping_revision = 1
        self.mappings = MAPPINGS.copy()
        self.lease_lost = False
        self.owner = "MANUAL"
        self.owner_revision = 0

    def update_mappings(self, mappings):
        with self.lock:
            if mappings != self.mappings:
                self.finish(cancel=True)
                self.mappings = mappings.copy()
                self.mapping_revision += 1

    def dispatch(self, method, path, query, body):
        with self.lock:
            self.finish()
            if method == "GET" and path == "/health":
                return {"mode": "MOCK", "ready": True, "build": "mock-v1"}
            if method == "GET" and path == "/capabilities":
                return {"mode": "MOCK", "schema_version": 1, "max_hold_ms": 2000, "lease_ms": LEASE_MS}
            if method == "GET" and path == "/mappings":
                return {"mapping_revision": self.mapping_revision, "keys": self.mappings, "catalog": MOCK_CATALOG}
            if method == "POST" and path == "/session":
                owner = body.get("input_owner", "MANUAL")
                if type(body.get("schema_version")) is not int or body["schema_version"] != 1:
                    raise Rejected("SCHEMA_MISMATCH")
                if owner not in INPUT_OWNERS:
                    raise Rejected("INVALID_INPUT_OWNER")
                if self.session_id and not self.lease_lost:
                    raise Rejected("SESSION_BUSY")
                self.session_id = uuid4().hex
                self.world += 1
                self.commands.clear()
                self.events.clear()
                self.position = self.jumps = 0
                self.lease_lost = False
                self.owner = owner
                self.owner_revision = 1
                return self.context()
            if not self.session_id:
                raise Rejected("NO_SESSION")
            if method == "GET" and path == "/observations":
                return {**self.context(), "position": self.position, "jumps": self.jumps,
                        "held_keys": list(self.held),
                        "observation_seq": len(self.events), "lease_lost": self.lease_lost,
                        "player_exists": True, "on_ground": True}
            if method == "GET" and path == "/events":
                after = int(query.get("after_seq", ["0"])[0])
                if after < 0:
                    raise Rejected("INVALID_CURSOR")
                return {"events": self.events[after:]}
            if method == "GET" and path.startswith("/commands/"):
                record = self.commands.get(path.rsplit("/", 1)[1])
                if record:
                    return {"status": record[1]}
                raise Rejected("UNKNOWN_REQUEST")
            if method != "POST":
                raise Rejected("UNKNOWN_ROUTE")
            self.validate_context(body)
            if path == "/session/owner":
                owner = body.get("next_owner")
                if owner not in INPUT_OWNERS:
                    raise Rejected("INVALID_INPUT_OWNER")
                if self.lease_lost:
                    raise Rejected("LEASE_EXPIRED")
                if owner != self.owner:
                    self.finish(cancel=True)
                    if self.held:
                        raise Rejected("KEY_RELEASE_FAILED")
                    self.owner = owner
                    self.owner_revision += 1
                    self.event(body, "L1", "InputOwnerChanged", owner)
                return self.context()
            if path == "/input/release-all" or path == "/session/stop":
                self.finish(cancel=True)
                if path == "/session/stop":
                    self.session_id = None
                return {"held_keys": []}
            if path == "/session/heartbeat":
                if self.lease_lost:
                    raise Rejected("LEASE_EXPIRED")
                self.lease_until = time.monotonic() + LEASE_MS / 1000
                return {"lease_ms": LEASE_MS}
            if path != "/commands":
                raise Rejected("UNKNOWN_ROUTE")
            request_id = body["request_id"]
            prior = self.commands.get(request_id)
            if prior:
                if body != prior[0]:
                    raise Rejected("IDEMPOTENCY_CONFLICT")
                return {"status": prior[1]}
            if self.lease_lost:
                raise Rejected("LEASE_EXPIRED")
            if "expected_observation_seq" in body:
                seq = body["expected_observation_seq"]
                if type(seq) is not int or seq != len(self.events):
                    raise Rejected("STALE_OBSERVATION")
            if self.active:
                raise Rejected("INPUT_BUSY")
            if len(self.commands) >= 1000:
                raise Rejected("SESSION_COMMAND_LIMIT")
            if "steps" in body:
                if "keys" in body or "hold_ms" in body:
                    raise Rejected("AMBIGUOUS_COMMAND_INPUT")
                try:
                    duration, transitions = compile_node(body["steps"], self.mappings, MOCK_CATALOG)
                except ValueError as error:
                    raise Rejected(str(error)) from None
            else:
                keys, duration = body.get("keys"), body.get("hold_ms")
                validate_step(keys, duration, self.mappings)
                transitions = [(0, "down", key) for key in keys]
                transitions += [(duration, "up", key) for key in keys]
            if duration > body["deadline_ms"]:
                raise Rejected("DEADLINE_TOO_SHORT")
            transitions.append((duration, "end", None))
            started = time.monotonic()
            self.lease_until = started + LEASE_MS / 1000
            self.active = (started, body, iter(transitions))
            self.pending = next(self.active[2])
            self.elapsed_ms = 0
            self.commands[request_id] = (body, "running")
            self.finish()
            if self.active:
                worker = threading.Thread(target=self.run_timeline, args=(body,), daemon=True)
                worker.start()
            return {"status": self.commands[request_id][1]}

    def context(self):
        return {"schema_version": 1, "session_id": self.session_id,
                "world_generation": self.world, "expected_mapping_revision": self.mapping_revision,
                "input_owner": self.owner, "owner_revision": self.owner_revision}

    def validate_context(self, body):
        for field, code in zip(self.context(), (
                "SCHEMA_MISMATCH", "SESSION_MISMATCH", "STALE_WORLD", "MAPPING_CHANGED",
                "INPUT_OWNER_MISMATCH", "STALE_INPUT_OWNER")):
            expected = self.context()[field]
            if type(body.get(field)) is not type(expected) or body[field] != expected:
                raise Rejected(code)
        try:
            UUID(body.get("request_id", ""))
        except (ValueError, TypeError, AttributeError):
            raise Rejected("INVALID_REQUEST_ID") from None
        deadline = body.get("deadline_ms")
        if type(deadline) is not int or not 1 <= deadline <= 3000:
            raise Rejected("INVALID_DEADLINE")

    def event(self, body, layer, kind, value):
        self.events.append({"seq": len(self.events) + 1, "request_id": body["request_id"],
                            "layer": layer, "kind": kind, "value": value})

    def run_timeline(self, body):
        while True:
            with self.lock:
                if not self.active or self.active[1] is not body:
                    return
                self.finish()
                if not self.active:
                    return
                due = min(self.active[0] + self.pending[0] / 1000, self.lease_until)
            time.sleep(max(0, due - time.monotonic()))

    def finish(self, cancel=False):
        if not self.active:
            return
        started, body, remaining = self.active
        now = time.monotonic()
        expired = now >= self.lease_until
        if cancel or expired:
            for key in list(self.held):
                self.release(body, key, cancelled=True)
            self.active = None
            self.commands[body["request_id"]] = (body, "expired" if expired else "cancelled")
            self.lease_lost |= expired
            if expired:
                self.event(body, "L1", "LeaseExpired", None)
            return
        elapsed = (now - started) * 1000
        while self.pending[0] <= elapsed:
            at, kind, key = self.pending
            delta = at - self.elapsed_ms
            self.elapsed_ms = at
            if delta:
                held_actions = {self.mappings[key] for key in self.held}
                if "IA_Move" in held_actions:
                    self.position += delta * (2 if "IA_Sprint" in held_actions else 1)
                    self.event(body, "L3", "PositionChanged", self.position)
            if kind == "end":
                self.active = None
                self.commands[body["request_id"]] = (body, "completed")
                return
            if kind == "down":
                self.held[key] = None
                self.event(body, "L1", "KeyDown", key)
                self.event(body, "L2", "Triggered", self.mappings[key])
            else:
                self.release(body, key)
            self.pending = next(remaining)

    def release(self, body, key, cancelled=False):
        del self.held[key]
        self.event(body, "L1", "KeyUp", key)
        self.event(body, "L2", "Completed", self.mappings[key])
        if self.mappings[key] == "IA_Jump" and not cancelled:
            self.jumps += 1
            self.event(body, "L3", "JumpCountChanged", self.jumps)


def make_server(game=None):
    game = MockGame() if game is None else game
    token = secrets.token_urlsafe(32)
    session_token = None
    auth_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def handle_request(self):
            nonlocal session_token
            url = urlsplit(self.path)
            if not url.path.startswith("/qa/v1/"):
                return reply(self, 404, {"error": "UNKNOWN_ROUTE"})
            path = url.path[6:]
            start = self.command == "POST" and path == "/session"
            try:
                with auth_lock:
                    expected = token if start or session_token is None else session_token
                    if not secrets.compare_digest(self.headers.get("Authorization", "").encode(),
                                                  f"Bearer {expected}".encode()):
                        return reply(self, 401, {"error": "UNAUTHORIZED"})
                    body = read_json(self) if self.command == "POST" else {}
                    value = game.dispatch(self.command, path, parse_qs(url.query), body)
                    if start:
                        session_token = secrets.token_urlsafe(32)
                        value["session_token"] = session_token
                    elif self.command == "POST" and path == "/session/stop":
                        session_token = None
            except (ValueError, TimeoutError) as error:
                code = str(error) if isinstance(error, Rejected) else "BAD_REQUEST"
                return reply(self, 400, {"error": code})
            reply(self, 200, value)

        do_GET = do_POST = handle_request

    return ThreadingHTTPServer(("127.0.0.1", 0), Handler), token
