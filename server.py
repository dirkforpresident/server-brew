#!/usr/bin/env python3
"""
FreeTetra — TETRA Brew Protocol Server.

Ein freier, offener BREW-Server: vernetzt TETRA-BlueStations, Eigenbau-TMO-Repeater
und (über ModuleTetraBrew) SvxLink-FM-Repeater zu einem gemeinsamen Sprach-Netz.

Protokoll: WebSocket + HTTP-Digest-Auth, binär Little-Endian, ACELP-Sprachframes.
Routing per Talkgroup (GSSI): wer auf eine GSSI affiliiert, hört deren Gruppenruf.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import struct
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import websockets
from websockets.http11 import Response
from websockets.datastructures import Headers

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("brew")

CONFIG_PATH = Path(__file__).parent / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Protokoll-Konstanten
# ---------------------------------------------------------------------------

CLASS_SUBSCRIBER = 0xF0
CLASS_CALL = 0xF1
CLASS_FRAME = 0xF2
CLASS_ERROR = 0xF3
CLASS_SERVICE = 0xF4

SUB_DEREGISTER = 0x00
SUB_REGISTER = 0x01
SUB_REREGISTER = 0x02
SUB_AFFILIATE = 0x08
SUB_DEAFFILIATE = 0x09

CALL_GROUP_TX = 0x02
CALL_GROUP_IDLE = 0x03
CALL_SHORT_TRANSFER = 0x0B

FRAME_TRAFFIC = 0x00
FRAME_SDS_TRANSFER = 0x01
FRAME_SDS_REPORT = 0x02

ERR_MALFORMED = 0x00

SVC_QUERY = 0x01
SVC_RESPONSE = 0x02

REALM = "brew"

# Offene Sessions (Digest-OK, aber WS-Upgrade noch nicht erfolgt) verfallen schnell:
# der echte Client verbindet sofort. Verhindert unbegrenztes Wachstum durch Clients,
# die nur den Handshake machen und nie upgraden.
PENDING_TTL = 30.0      # Sekunden bis eine offene Session verfällt
PENDING_MAX = 2048      # harte Obergrenze offener Sessions


# ---------------------------------------------------------------------------
# HTTP-Digest-Auth
# ---------------------------------------------------------------------------

def make_nonce() -> str:
    return hashlib.md5(os.urandom(32)).hexdigest()


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def parse_digest_header(header: str) -> dict:
    """`Authorization: Digest key=value, ...` -> dict."""
    parts = {}
    header = header.strip()
    if header.lower().startswith("digest "):
        header = header[7:]
    for part in header.split(","):
        part = part.strip()
        if "=" not in part:
            continue
        key, val = part.split("=", 1)
        parts[key.strip().lower()] = val.strip().strip('"')
    return parts


# ---------------------------------------------------------------------------
# Client-Zustand
# ---------------------------------------------------------------------------

@dataclass(eq=False)  # eq=False -> Identitäts-Hash, damit Client in Sets nutzbar ist
class Client:
    client_id: str          # RadioID / User-ID
    callsign: str
    session_id: str
    ws: object              # websockets-Verbindung
    issis: set = field(default_factory=set)        # registrierte ISSIs
    affiliations: dict = field(default_factory=dict)  # ISSI -> set(GSSIs)
    connected_at: float = 0.0                      # time.time() beim Verbinden (Verzeichnis)

    def __repr__(self):
        return f"Client({self.callsign}/{self.client_id}, session={self.session_id[:8]})"


# ---------------------------------------------------------------------------
# Server
# ---------------------------------------------------------------------------

class BrewServer:
    def __init__(self, config: dict):
        self.config = config
        self.users: dict = config.get("users", {})
        self.clients: dict[str, Client] = {}             # session_id -> Client
        self.pending_sessions: dict[str, tuple] = {}     # session_id -> (user_id, callsign)
        self.issi_registry: dict[int, Client] = {}       # ISSI -> Client
        self.gssi_affiliations: dict[int, set] = {}      # GSSI -> set(ISSIs)
        self.active_calls: dict[bytes, set] = {}         # call_uuid -> set(session_ids)
        self._last_nodes = None                          # letzter nodes.json-Inhalt (Dedup)

        # Open-Mode: jeder mit RadioID + Community-Passwort darf rein.
        self.open_mode: bool = config.get("open", False)
        self.open_password: str = config.get("open_password", "")
        # Optionale Sperrliste (RadioIDs/ISSIs als String) — Rausschmeißer.
        self.blocklist: set = {str(x) for x in config.get("blocklist", [])}

        # Echo-/Papagei-Test: Ruf auf eine dieser GSSIs wird aufgenommen und zurückgespielt.
        self.echo_gssis: set = {int(g) for g in config.get("echo_gssis", [])}
        self.echo_buffers: dict[bytes, list] = {}        # call_uuid -> [frames]
        self.echo_callers: dict[bytes, tuple] = {}       # call_uuid -> (src_issi, gssi)

        # Ein-BTS-/Testmodus: Gruppenruf an ALLE Clients (statt nur affiliierte).
        self.broadcast_groups: bool = config.get("broadcast_groups", False)

    # ------------------------------------------------------------------
    # Auth
    # ------------------------------------------------------------------

    def verify_digest(self, auth_header: str, method: str, uri: str) -> Optional[tuple]:
        """Digest prüfen. Rückgabe (user_id, callsign) oder None."""
        p = parse_digest_header(auth_header)
        username = p.get("username", "")
        if username in self.blocklist:
            log.info("Gesperrter User abgewiesen: %s", username)
            return None

        # Bekannter User -> sein Passwort; sonst im Open-Mode das Community-Passwort
        # (RadioID = Identität). Digest bleibt, weil Clients ein 401 erwarten.
        if username in self.users:
            password = self.users[username]["password"]
            callsign = self.users[username].get("callsign", username)
        elif self.open_mode:
            password = self.open_password
            callsign = username
        else:
            return None

        ha1 = _md5(f"{username}:{REALM}:{password}")
        ha2 = _md5(f"{method}:{uri}")
        expected = _md5(f"{ha1}:{p.get('nonce','')}:{p.get('nc','')}:"
                        f"{p.get('cnonce','')}:{p.get('qop','auth')}:{ha2}")
        # Konstant-zeit-Vergleich (kein Timing-Leak beim Auth-Check).
        if hmac.compare_digest(expected, p.get("response", "")):
            return (username, callsign)
        return None

    # ------------------------------------------------------------------
    # Teilnehmer-Verwaltung
    # ------------------------------------------------------------------

    def register_issi(self, client: Client, issi: int):
        old = self.issi_registry.get(issi)
        if old and old is not client:
            log.warning("ISSI %d wandert von %s zu %s", issi, old, client)
            old.issis.discard(issi)
        self.issi_registry[issi] = client
        client.issis.add(issi)
        log.info("REGISTER ISSI=%d auf %s", issi, client)

    def deregister_issi(self, client: Client, issi: int):
        if self.issi_registry.get(issi) is client:
            del self.issi_registry[issi]
        client.issis.discard(issi)
        for gssi, members in list(self.gssi_affiliations.items()):
            members.discard(issi)
            if not members:
                del self.gssi_affiliations[gssi]
        client.affiliations.pop(issi, None)
        log.info("DEREGISTER ISSI=%d von %s", issi, client)

    def affiliate(self, client: Client, issi: int, gssis: list):
        client.affiliations.setdefault(issi, set())
        for gssi in gssis:
            self.gssi_affiliations.setdefault(gssi, set()).add(issi)
            client.affiliations[issi].add(gssi)
        log.info("AFFILIATE ISSI=%d -> GSSIs=%s auf %s", issi, gssis, client)

    def deaffiliate(self, client: Client, issi: int, gssis: list):
        for gssi in gssis:
            if gssi in self.gssi_affiliations:
                self.gssi_affiliations[gssi].discard(issi)
                if not self.gssi_affiliations[gssi]:
                    del self.gssi_affiliations[gssi]
            if issi in client.affiliations:
                client.affiliations[issi].discard(gssi)
        log.info("DEAFFILIATE ISSI=%d von GSSIs=%s auf %s", issi, gssis, client)

    def cleanup_client(self, client: Client):
        for issi in list(client.issis):
            self.deregister_issi(client, issi)
        self.clients.pop(client.session_id, None)
        log.info("Client getrennt: %s", client)

    # ------------------------------------------------------------------
    # Routing
    # ------------------------------------------------------------------

    def targets_for_gssi(self, gssi: int, exclude: Client) -> set:
        """Alle Clients, die auf diese GSSI affiliiert sind (ohne Sender).
        broadcast_groups: zusätzlich alle verbundenen Clients (Ein-BTS-Test)."""
        targets = set()
        for issi in self.gssi_affiliations.get(gssi, set()):
            c = self.issi_registry.get(issi)
            if c and c is not exclude:
                targets.add(c)
        if self.broadcast_groups:
            targets.update(c for c in self.clients.values() if c is not exclude)
        return targets

    async def forward(self, data: bytes, targets: set, call_uuid: bytes = None):
        for c in targets:
            try:
                await c.ws.send(data)
            except Exception as e:
                log.error("Weiterleitung an %s fehlgeschlagen: %s", c, e)
        if call_uuid is not None:
            self.active_calls[call_uuid] = {c.session_id for c in targets}

    # ------------------------------------------------------------------
    # Nachrichten
    # ------------------------------------------------------------------

    async def handle_message(self, client: Client, data: bytes):
        if len(data) < 2:
            return
        cls, typ = data[0], data[1]
        try:
            if cls == CLASS_SUBSCRIBER:
                await self.handle_subscriber(client, typ, data)
            elif cls == CLASS_CALL:
                await self.handle_call(client, typ, data)
            elif cls == CLASS_FRAME:
                await self.handle_frame(client, typ, data)
            elif cls == CLASS_SERVICE:
                await self.handle_service(client, typ, data)
            else:
                log.warning("Unbekannte Klasse 0x%02x von %s", cls, client)
        except struct.error as e:
            log.error("Malformed 0x%02x/0x%02x von %s: %s", cls, typ, client, e)
            await client.ws.send(bytes([CLASS_ERROR, ERR_MALFORMED]))
        except Exception as e:
            log.error("Fehler bei 0x%02x/0x%02x von %s: %s", cls, typ, client, e)

    async def handle_subscriber(self, client: Client, typ: int, data: bytes):
        # class(1) type(1) issi(4) timestamp(8) nanos(4) gssi(4)...
        # (Standard-BREW: GSSIs direkt ab Offset 18; ModuleTetraBrew mit count-Praefix.)
        if len(data) < 18:
            raise struct.error("subscriber msg zu kurz")
        issi = struct.unpack_from("<I", data, 2)[0]

        if typ in (SUB_REGISTER, SUB_REREGISTER):
            self.register_issi(client, issi)
        elif typ == SUB_DEREGISTER:
            self.deregister_issi(client, issi)
        elif typ in (SUB_AFFILIATE, SUB_DEAFFILIATE):
            # GSSI-Liste ab Offset 18 bis Nachrichtenende (je 4 Byte, LE).
            # BlueStations senden die GSSIs direkt (kein count-Feld); unser
            # ModuleTetraBrew schiebt ein 2-Byte-count davor. Beide Varianten
            # tolerant lesen -> sonst wird die BlueStation nie affiliiert und
            # hoert FM->TETRA nicht (Server findet targets=0).
            rest = data[18:]
            if len(rest) % 4 == 2:                 # count-Praefix (ModuleTetraBrew)
                rest = rest[2:]
            if len(rest) % 4 != 0:                 # leere Liste = No-Op (nicht ablehnen)
                raise struct.error("affiliate msg abgeschnitten")
            gssis = [struct.unpack_from("<I", rest, i * 4)[0]
                     for i in range(len(rest) // 4)]
            if typ == SUB_AFFILIATE:
                self.affiliate(client, issi, gssis)
            else:
                self.deaffiliate(client, issi, gssis)
        else:
            log.warning("Unbekannter subscriber-Typ 0x%02x von %s", typ, client)

    async def handle_call(self, client: Client, typ: int, data: bytes):
        # class(1) type(1) uuid(16) payload...
        if len(data) < 18:
            raise struct.error("call msg zu kurz")
        call_uuid = data[2:18]

        if typ == CALL_GROUP_TX:
            if len(data) < 28:
                raise struct.error("GROUP_TX zu kurz")
            src_issi = struct.unpack_from("<I", data, 18)[0]
            dest_gssi = struct.unpack_from("<I", data, 22)[0]
            targets = self.targets_for_gssi(dest_gssi, exclude=client)
            log.info("GROUP_TX call=%s ISSI=%d->GSSI=%d targets=%d von %s",
                     call_uuid.hex()[:8], src_issi, dest_gssi, len(targets), client)
            await self.forward(data, targets, call_uuid)
            # Echo-Test: Aufnahme starten
            if dest_gssi in self.echo_gssis:
                self.echo_buffers[call_uuid] = []
                self.echo_callers[call_uuid] = (src_issi, dest_gssi)

        elif typ == CALL_GROUP_IDLE:
            sessions = self.active_calls.pop(call_uuid, set())
            targets = {self.clients[s] for s in sessions if s in self.clients}
            log.info("GROUP_IDLE call=%s targets=%d von %s", call_uuid.hex()[:8], len(targets), client)
            await self.forward(data, targets)
            # Echo-Test: Aufnahme zurückspielen
            frames = self.echo_buffers.pop(call_uuid, None)
            caller = self.echo_callers.pop(call_uuid, None)
            if frames and caller:
                asyncio.ensure_future(self.echo_playback(client, caller[1], frames))

        elif typ == CALL_SHORT_TRANSFER:
            # SDS-Einzelruf-Vorspann: an Ziel-ISSI weiterleiten
            if len(data) < 26:
                raise struct.error("SHORT_TRANSFER zu kurz")
            dest_issi = struct.unpack_from("<I", data, 22)[0]
            target = self.issi_registry.get(dest_issi)
            if target:
                await self.forward(data, {target}, call_uuid)
            else:
                log.warning("SHORT_TRANSFER Ziel-ISSI=%d nicht gefunden (von %s)", dest_issi, client)
        else:
            log.warning("Unbekannter call-Typ 0x%02x von %s", typ, client)

    async def handle_frame(self, client: Client, typ: int, data: bytes):
        # class(1) type(1) uuid(16) ...
        if len(data) < 18:
            raise struct.error("frame msg zu kurz")
        call_uuid = data[2:18]
        sessions = self.active_calls.get(call_uuid, set())
        targets = {self.clients[s] for s in sessions if s in self.clients}

        if typ == FRAME_TRAFFIC:
            await self.forward(data, targets)
            if call_uuid in self.echo_buffers:          # Echo-Test: Sprachframe merken
                self.echo_buffers[call_uuid].append(data[18:])
        elif typ in (FRAME_SDS_TRANSFER, FRAME_SDS_REPORT):
            await self.forward(data, targets)           # SDS unverändert weiterreichen
        else:
            log.warning("Unbekannter frame-Typ 0x%02x von %s", typ, client)

    async def handle_service(self, client: Client, typ: int, data: bytes):
        # SERVICE_QUERY: JSON-Liste von ISSIs -> Profile (registriert? Call/GSSIs)
        if typ != SVC_QUERY:
            log.warning("Unbekannter service-Typ 0x%02x von %s", typ, client)
            return
        payload = data[2:].rstrip(b"\x00")
        try:
            issi_list = json.loads(payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            log.error("Ungültige service-query von %s: %s", client, e)
            return
        profiles = {}
        for issi in issi_list:
            c = self.issi_registry.get(int(issi))
            profiles[str(issi)] = ({"registered": True, "callsign": c.callsign,
                                    "gssis": list(c.affiliations.get(int(issi), set()))}
                                   if c else {"registered": False})
        resp = bytes([CLASS_SERVICE, SVC_RESPONSE]) + json.dumps(profiles).encode("utf-8") + b"\x00"
        await client.ws.send(resp)

    # ------------------------------------------------------------------
    # Echo-/Papagei-Test
    # ------------------------------------------------------------------

    async def echo_playback(self, client: Client, dest_gssi: int, frames: list):
        """Aufgenommene Sprachframes als Echo auf derselben GSSI zurückspielen."""
        echo_uuid = uuid.uuid4().bytes
        log.info("ECHO: spiele %d Frames auf GSSI=%d", len(frames), dest_gssi)
        await asyncio.sleep(1.2)
        try:
            await client.ws.send(struct.pack("<BB16sIIBBH", CLASS_CALL, CALL_GROUP_TX,
                                             echo_uuid, 9999999, dest_gssi, 0, 0, 0))
            for f in frames:
                await client.ws.send(struct.pack("<BB16s", CLASS_FRAME, FRAME_TRAFFIC, echo_uuid) + f)
                await asyncio.sleep(0.056)   # ~60 ms TETRA-Framedauer
            await client.ws.send(struct.pack("<BB16sB", CLASS_CALL, CALL_GROUP_IDLE, echo_uuid, 0))
        except Exception as e:
            log.error("ECHO-Wiedergabe fehlgeschlagen: %s", e)

    # ------------------------------------------------------------------
    # HTTP-Digest-Handshake + WebSocket
    # ------------------------------------------------------------------

    def _prune_pending(self):
        """Abgelaufene offene Sessions (kein WS-Upgrade) entfernen."""
        now = time.monotonic()
        stale = [s for s, (_, _, ts) in self.pending_sessions.items() if now - ts > PENDING_TTL]
        for s in stale:
            self.pending_sessions.pop(s, None)

    def process_request(self, connection, request):
        """GET /brew/ -> Digest-Auth -> 200 mit Session-Pfad. /brew/<uuid> -> WS-Upgrade."""
        path = request.path
        if path.rstrip("/") != "/brew":
            return None  # /brew/<uuid> -> WS-Upgrade zulassen

        auth = request.headers.get("Authorization")
        if auth is None or self.verify_digest(auth, "GET", path) is None:
            headers = Headers()
            headers["WWW-Authenticate"] = (f'Digest realm="{REALM}", nonce="{make_nonce()}", '
                                           f'qop="auth", algorithm=MD5'
                                           + (', stale=true' if auth else ''))
            headers["Content-Type"] = "text/plain"
            return Response(401, "Unauthorized", headers, b"Authentication required")

        user_id, callsign = self.verify_digest(auth, "GET", path)
        self._prune_pending()
        if len(self.pending_sessions) >= PENDING_MAX:
            log.warning("Zu viele offene Sessions (%d) — weise ab.", len(self.pending_sessions))
            headers = Headers()
            headers["Content-Type"] = "text/plain"
            return Response(503, "Service Unavailable", headers, b"Too many pending sessions")
        session_id = str(uuid.uuid4())
        self.pending_sessions[session_id] = (user_id, callsign, time.monotonic())
        log.info("Session %s für %s (%s)", session_id[:8], callsign, user_id)
        body = f"/brew/{session_id}".encode()
        headers = Headers()
        headers["Content-Type"] = "text/plain"
        headers["Content-Length"] = str(len(body))
        return Response(200, "OK", headers, body)

    async def ws_handler(self, websocket):
        path = websocket.request.path
        if not path.startswith("/brew/"):
            await websocket.close(4001, "Invalid path")
            return
        session_id = path[6:].rstrip("/")
        pending = self.pending_sessions.pop(session_id, None)
        if pending is None:
            await websocket.close(4002, "Invalid or expired session")
            return
        user_id, callsign, ts = pending
        if time.monotonic() - ts > PENDING_TTL:
            await websocket.close(4002, "Session expired")
            return
        client = Client(user_id, callsign, session_id, websocket)
        client.connected_at = time.time()
        self.clients[session_id] = client
        log.info("WebSocket verbunden: %s", client)
        try:
            async for message in websocket:
                if isinstance(message, bytes):
                    await self.handle_message(client, message)
        except websockets.ConnectionClosed as e:
            log.info("Verbindung zu %s geschlossen (code=%s)", client, e.code)
        except Exception as e:
            log.error("Unerwarteter Fehler von %s: %s", client, e)
        finally:
            self.cleanup_client(client)

    # ------------------------------------------------------------------
    # Live-Knotenverzeichnis: eigenen Zustand nach web/nodes.json exportieren
    # (read-only, nginx liefert es statisch aus -> Verzeichnis-Webseite).
    # ------------------------------------------------------------------
    def nodes_snapshot(self):
        meta = self.config.get("node_meta", {})   # {"<radioid|call>": {name,type,qth}}
        by_id = {}
        for c in self.clients.values():
            tgs = sorted({g for gs in c.affiliations.values() for g in gs})
            m = meta.get(str(c.client_id)) or meta.get(c.callsign) or {}
            entry = {"call": m.get("name") or c.callsign, "id": str(c.client_id),
                     "tgs": tgs, "type": m.get("type", ""), "qth": m.get("qth", ""),
                     "since": int(c.connected_at)}
            prev = by_id.get(c.client_id)          # je Knoten die neueste Session
            if prev is None or entry["since"] >= prev["since"]:
                by_id[c.client_id] = entry
        nodes = sorted(by_id.values(), key=lambda e: (e["type"], e["call"]))
        return {"updated": int(time.time()), "count": len(nodes), "nodes": nodes}

    def write_nodes(self):
        try:
            web_dir = self.config.get("web_dir", "web")
            data = json.dumps(self.nodes_snapshot(), ensure_ascii=False, indent=1)
            if data == self._last_nodes:
                return
            self._last_nodes = data
            os.makedirs(web_dir, exist_ok=True)
            tmp = os.path.join(web_dir, ".nodes.json.tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(data)
            os.replace(tmp, os.path.join(web_dir, "nodes.json"))
        except Exception as e:
            log.error("write_nodes: %s", e)

    async def nodes_loop(self):
        while True:
            self.write_nodes()
            await asyncio.sleep(5)

    async def run(self):
        host = self.config["server"]["host"]
        port = self.config["server"]["port"]
        log.info("Starte FreeTetra-Brew-Server auf %s:%d (open=%s, %d User, Echo-GSSIs=%s)",
                 host, port, self.open_mode, len(self.users), self.echo_gssis or "-")
        async with websockets.serve(self.ws_handler, host, port,
                                    process_request=self.process_request,
                                    subprotocols=["brew"],
                                    ping_interval=30, ping_timeout=10):
            log.info("Server läuft.")
            asyncio.ensure_future(self.nodes_loop())   # Live-Verzeichnis nach web/nodes.json
            await asyncio.Future()  # für immer


def main():
    asyncio.run(BrewServer(load_config()).run())


if __name__ == "__main__":
    main()
