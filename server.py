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
import urllib.request
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
CLASS_TRUNK = 0xF5          # Server-zu-Server (Mesh): Steuernachrichten
TRUNK_HELLO = 0x01         # {name, nonce, ts, mac, prefix}
TRUNK_INTEREST = 0x02      # {tgs:[...]} — welche TGs der Peer hören will

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


@dataclass(eq=False)
class Peer:
    """Ein anderer FreeTetra-Server im Mesh (Trunk-Verbindung)."""
    name: str
    secret: str
    prefix: str = ""                 # TGs mit dieser Vorwahl "gehören" dem Peer
    url: str = ""                    # wss://…/trunk zum Rauswählen ("" = nur eingehend)
    tgs: list = None                 # optionaler TG-Filter (Muster), None = alles
    ws: object = None                # aktive Trunk-Verbindung (None = offline)
    interest: set = field(default_factory=set)   # GSSIs, die der Peer hören will
    connected_at: float = 0.0

    def connected(self) -> bool:
        return self.ws is not None

    def __repr__(self):
        return f"Peer({self.name}{'/'+self.prefix if self.prefix else ''})"


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
        self._meta_mtime = None                          # mtime der config.json (node_meta live-reload)
        self._idcache_path = config.get("radioid_cache", "radioid_cache.json")
        try:
            self._id_names = json.load(open(self._idcache_path))   # "<baseid>" -> callsign
        except Exception:
            self._id_names = {}
        self._id_pending = set()                         # laufende/fehlgeschlagene Lookups

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

        # --- Mesh (optional): mehrere FreeTetra-Server koppeln. Ohne "mesh" =
        # Standalone, exakt wie bisher. Peering nur mit passendem Shared Secret. ---
        mesh = config.get("mesh") or {}
        self.local_prefix: str = str(mesh.get("local_prefix", "") or "")   # meine TG-Vorwahl(en), z.B. "262"
        self.mesh_tgs = mesh.get("tgs")            # global: welche TGs will ICH ueberhaupt (None = alle)
        self.peers: dict[str, Peer] = {}
        for pc in mesh.get("peers", []) or []:
            p = Peer(name=str(pc["name"]), secret=str(pc["secret"]),
                     prefix=str(pc.get("prefix", "") or ""), url=str(pc.get("url", "") or ""),
                     tgs=pc.get("tgs"))
            self.peers[p.name] = p
        self.mesh_enabled = bool(self.peers)
        self.active_call_peers: dict[bytes, set] = {}   # call_uuid -> set(Peer) fuer Trunk-Frames
        self._interest_dirty = False                    # lokale Affiliationen geaendert -> Peers neu melden

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
            if old.callsign != client.callsign:
                log.warning("ISSI-KOLLISION %d: '%s' und '%s' beanspruchen dieselbe ISSI "
                            "— einer muss SRC_ISSI manuell setzen", issi, old.callsign, client.callsign)
            else:
                log.info("ISSI %d wandert von %s zu %s (Reconnect)", issi, old, client)
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
        if self.mesh_enabled:
            self._interest_dirty = True

    def affiliate(self, client: Client, issi: int, gssis: list):
        client.affiliations.setdefault(issi, set())
        for gssi in gssis:
            self.gssi_affiliations.setdefault(gssi, set()).add(issi)
            client.affiliations[issi].add(gssi)
        log.info("AFFILIATE ISSI=%d -> GSSIs=%s auf %s", issi, gssis, client)
        if self.mesh_enabled:
            self._interest_dirty = True

    def deaffiliate(self, client: Client, issi: int, gssis: list):
        for gssi in gssis:
            if gssi in self.gssi_affiliations:
                self.gssi_affiliations[gssi].discard(issi)
                if not self.gssi_affiliations[gssi]:
                    del self.gssi_affiliations[gssi]
            if issi in client.affiliations:
                client.affiliations[issi].discard(gssi)
        log.info("DEAFFILIATE ISSI=%d von GSSIs=%s auf %s", issi, gssis, client)
        if self.mesh_enabled:
            self._interest_dirty = True

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
            if self.mesh_enabled:                       # Mesh: an Owner/interessierte Peers
                peers = self.trunk_targets(dest_gssi, src_peer=None)
                for p in peers:
                    await self.trunk_send(p, data)
                self.active_call_peers[call_uuid] = peers
            # Echo-Test: Aufnahme starten
            if dest_gssi in self.echo_gssis:
                self.echo_buffers[call_uuid] = []
                self.echo_callers[call_uuid] = (src_issi, dest_gssi)

        elif typ == CALL_GROUP_IDLE:
            sessions = self.active_calls.pop(call_uuid, set())
            targets = {self.clients[s] for s in sessions if s in self.clients}
            log.info("GROUP_IDLE call=%s targets=%d von %s", call_uuid.hex()[:8], len(targets), client)
            await self.forward(data, targets)
            if self.mesh_enabled:
                for p in self.active_call_peers.pop(call_uuid, set()):
                    await self.trunk_send(p, data)
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
            if self.mesh_enabled:                       # Mesh: Sprachframe an dieselben Peers
                for p in self.active_call_peers.get(call_uuid, set()):
                    await self.trunk_send(p, data)
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
        if path.rstrip("/") == "/trunk":            # Mesh: eingehender Server-Trunk
            await self.handle_trunk_inbound(websocket)
            return
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
    # Mesh: mehrere FreeTetra-Server koppeln (Trunk + Prefix-Ownership).
    # Alles hinter self.mesh_enabled -> ohne Peers exakt Standalone-Verhalten.
    # ------------------------------------------------------------------
    @staticmethod
    def _tg_match(patterns, gssi) -> bool:
        """None = alles erlaubt. Sonst Liste aus exakt ('8') oder Prefix ('262*')."""
        if not patterns:
            return True
        s = str(gssi)
        for pat in patterns:
            pat = str(pat)
            if pat.endswith("*"):
                if s.startswith(pat[:-1]):
                    return True
            elif s == pat:
                return True
        return False

    @staticmethod
    def _prefix_match(prefix: str, gssi) -> bool:
        return bool(prefix) and str(gssi).startswith(prefix)

    def _mesh_wants(self, gssi) -> bool:
        """Nimmt DIESER Server an dieser TG ueberhaupt teil (globaler Filter)?"""
        return self._tg_match(self.mesh_tgs, gssi)

    def owner_of(self, gssi):
        """Longest-Prefix-Match: 'local', der Owner-Peer, oder None (keine Vorwahl
        besitzt die TG -> bleibt lokal, wird NICHT gemesht; loop-sicher)."""
        owner, best = None, -1
        if self._prefix_match(self.local_prefix, gssi):
            owner, best = "local", len(self.local_prefix)
        for p in self.peers.values():
            if self._prefix_match(p.prefix, gssi) and len(p.prefix) > best:
                owner, best = p, len(p.prefix)
        return owner

    def trunk_targets(self, gssi, src_peer):
        """Peers, an die ein Ruf auf dieser GSSI geht (loop-sicher via Owner-Relay)."""
        if not self.mesh_enabled or not self._mesh_wants(gssi):
            return set()
        owner = self.owner_of(gssi)
        if owner is None:                 # keine Vorwahl besitzt die TG -> nicht meshen
            return set()
        out = set()
        if owner == "local":
            # Ich bin Owner -> an ALLE interessierten Peers faechern (ausser Quelle).
            for p in self.peers.values():
                if p is src_peer or not p.connected():
                    continue
                if self._tg_match(p.tgs, gssi) and (gssi in p.interest or self._prefix_match(p.prefix, gssi)):
                    out.add(p)
        elif owner is not src_peer and owner.connected() and self._tg_match(owner.tgs, gssi):
            # Owner ist ein Peer -> nur dorthin (Uplink).
            out.add(owner)
        return out

    def _local_interest(self) -> set:
        return {g for g, m in self.gssi_affiliations.items() if m and self._mesh_wants(g)}

    async def trunk_send(self, peer, data: bytes):
        if peer.ws is None:
            return
        try:
            await peer.ws.send(data)
        except Exception as e:
            log.warning("Trunk-Send an %s fehlgeschlagen: %s", peer, e)

    async def trunk_send_ctrl(self, peer, sub: int, obj: dict):
        await self.trunk_send(peer, bytes([CLASS_TRUNK, sub]) + json.dumps(obj).encode())

    def _hello_bytes(self, peer) -> bytes:
        nonce = os.urandom(8).hex()
        ts = int(time.time())
        mac = hmac.new(peer.secret.encode(), f"{peer.name}:{nonce}:{ts}".encode(),
                       hashlib.sha256).hexdigest()
        obj = {"name": peer.name, "nonce": nonce, "ts": ts, "mac": mac, "prefix": self.local_prefix}
        return bytes([CLASS_TRUNK, TRUNK_HELLO]) + json.dumps(obj).encode()

    def _verify_hello(self, data: bytes):
        """Gueltiges Hello -> zugehoeriger Peer, sonst None. Schuetzt vor Fremd-Meshing."""
        if len(data) < 2 or data[0] != CLASS_TRUNK or data[1] != TRUNK_HELLO:
            return None
        try:
            obj = json.loads(data[2:])
        except Exception:
            return None
        peer = self.peers.get(str(obj.get("name", "")))
        if peer is None:
            return None
        try:
            if abs(int(time.time()) - int(obj.get("ts", 0))) > 120:
                return None
        except Exception:
            return None
        mac = hmac.new(peer.secret.encode(),
                       f"{peer.name}:{obj.get('nonce','')}:{obj.get('ts','')}".encode(),
                       hashlib.sha256).hexdigest()
        if not hmac.compare_digest(mac, str(obj.get("mac", ""))):
            return None
        if not peer.prefix and obj.get("prefix"):      # Vorwahl aus Hello, falls nicht konfiguriert
            peer.prefix = str(obj["prefix"])
        return peer

    async def handle_trunk_message(self, peer, data: bytes):
        if len(data) < 2:
            return
        if data[0] == CLASS_TRUNK:
            if data[1] == TRUNK_INTEREST:
                try:
                    obj = json.loads(data[2:])
                    peer.interest = {int(x) for x in obj.get("tgs", [])}
                except Exception:
                    pass
            elif data[1] == TRUNK_HELLO:
                try:
                    obj = json.loads(data[2:])
                    if not peer.prefix and obj.get("prefix"):
                        peer.prefix = str(obj["prefix"])
                except Exception:
                    pass
            return
        await self.handle_trunk_brew(peer, data)      # BREW-Frame vom Peer -> routen

    async def handle_trunk_brew(self, peer, data: bytes):
        """Ruf/Frame von einem Peer: lokal zustellen + loop-sicher weiterreichen."""
        cls, typ = data[0], data[1]
        if cls == CLASS_CALL and typ == CALL_GROUP_TX and len(data) >= 28:
            uuid = data[2:18]
            gssi = struct.unpack_from("<I", data, 22)[0]
            local = self.targets_for_gssi(gssi, exclude=None)
            await self.forward(data, local, uuid)
            peers = self.trunk_targets(gssi, src_peer=peer)
            for p in peers:
                await self.trunk_send(p, data)
            self.active_call_peers[uuid] = peers
        elif cls == CLASS_CALL and typ == CALL_GROUP_IDLE and len(data) >= 18:
            uuid = data[2:18]
            sessions = self.active_calls.pop(uuid, set())
            await self.forward(data, {self.clients[s] for s in sessions if s in self.clients})
            for p in self.active_call_peers.pop(uuid, set()):
                if p is not peer:
                    await self.trunk_send(p, data)
        elif cls == CLASS_FRAME and len(data) >= 18:
            uuid = data[2:18]
            sessions = self.active_calls.get(uuid, set())
            await self.forward(data, {self.clients[s] for s in sessions if s in self.clients})
            for p in self.active_call_peers.get(uuid, set()):
                if p is not peer:
                    await self.trunk_send(p, data)

    async def _trunk_run(self, peer, ws, inbound: bool):
        """Attach + Empfangsschleife fuer eine authentifizierte Trunk-Verbindung."""
        if peer.ws is not None:
            log.info("Trunk %s bereits verbunden -> Duplikat verworfen", peer)
            await ws.close()
            return
        peer.ws = ws
        peer.connected_at = time.time()
        log.info("Trunk %s VERBUNDEN (%s)", peer, "inbound" if inbound else "outbound")
        try:
            await self.trunk_send_ctrl(peer, TRUNK_INTEREST, {"tgs": sorted(self._local_interest())})
            async for msg in ws:
                if isinstance(msg, bytes):
                    await self.handle_trunk_message(peer, msg)
        except Exception as e:
            log.info("Trunk %s Fehler: %s", peer, e)
        finally:
            if peer.ws is ws:
                peer.ws = None
                peer.interest = set()
            log.info("Trunk %s getrennt", peer)

    async def handle_trunk_inbound(self, ws):
        """Eingehende Trunk-Verbindung: erst Hello verifizieren, dann laufen lassen."""
        try:
            first = await asyncio.wait_for(ws.recv(), timeout=10)
        except Exception:
            await ws.close()
            return
        peer = self._verify_hello(first if isinstance(first, bytes) else b"")
        if peer is None:
            log.warning("Trunk-Inbound abgewiesen (Hello ungueltig/unbekannt)")
            await ws.close(4003, "auth")
            return
        await ws.send(self._hello_bytes(peer))       # zurueckgruessen
        await self._trunk_run(peer, ws, inbound=True)

    async def trunk_dial(self, peer):
        """Ausgehende Trunk-Verbindung mit Reconnect-Backoff."""
        backoff = 5
        while True:
            try:
                async with websockets.connect(peer.url, subprotocols=["brew"],
                                               ping_interval=30, ping_timeout=10) as ws:
                    await ws.send(self._hello_bytes(peer))   # Secret nachweisen
                    backoff = 5
                    await self._trunk_run(peer, ws, inbound=False)
            except Exception as e:
                log.info("Trunk-Dial %s: %s", peer, e)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)

    async def mesh_loop(self):
        """Bei geaenderten lokalen Affiliationen die Interest-Liste an Peers melden."""
        while True:
            await asyncio.sleep(3)
            if not self._interest_dirty:
                continue
            self._interest_dirty = False
            snap = {"tgs": sorted(self._local_interest())}
            for p in self.peers.values():
                if p.connected():
                    await self.trunk_send_ctrl(p, TRUNK_INTEREST, snap)

    # ------------------------------------------------------------------
    # Live-Knotenverzeichnis: eigenen Zustand nach web/nodes.json exportieren
    # (read-only, nginx liefert es statisch aus -> Verzeichnis-Webseite).
    # ------------------------------------------------------------------
    def nodes_snapshot(self):
        # node_meta = bekannte Knoten (Roster): werden IMMER gelistet, auch offline.
        meta = self.config.get("node_meta", {})    # {"<radioid>": {name,type,qth}}
        # Live-Zustand je client_id (neueste Session gewinnt).
        live = {}
        for c in self.clients.values():
            cid = str(c.client_id)
            issis = sorted(c.issis)
            rec = {"tgs": sorted({g for gs in c.affiliations.values() for g in gs}),
                   "since": int(c.connected_at), "auth": c.callsign,
                   # genau eine ISSI (FM-Repeater) -> Waehl-Nummer zeigen;
                   # mehrere (BlueStation/Gateway) -> keine einzelne ISSI
                   "issi": issis[0] if len(issis) == 1 else 0}
            if cid not in live or rec["since"] >= live[cid]["since"]:
                live[cid] = rec
        nodes = []
        for cid in set(meta.keys()) | set(live.keys()):
            m = meta.get(cid, {}); lv = live.get(cid)
            online = lv is not None
            tgs = lv["tgs"] if online else []
            status = "offline" if not online else ("active" if tgs else "standby")
            name = m.get("name") or self.resolve_name(cid) or (lv["auth"] if lv else cid)
            nodes.append({"call": name, "id": cid, "type": m.get("type", ""),
                          "qth": m.get("qth", ""), "online": online, "status": status,
                          "issi": lv["issi"] if online else 0,
                          "tgs": tgs, "since": lv["since"] if online else 0})
        nodes.sort(key=lambda e: (not e["online"], e["type"], e["call"]))
        return {"updated": int(time.time()),
                "count": sum(1 for n in nodes if n["online"]),
                "total": len(nodes), "nodes": nodes}

    @staticmethod
    def _base_id(cid):
        # RadioID + laufende SSID-Nummer -> Basis-ID (DMR = 7 Stellen).
        s = "".join(ch for ch in str(cid) if ch.isdigit())
        return s[:7] if len(s) > 7 else s

    def resolve_name(self, cid):
        # Callsign aus dem RadioID-Cache; unbekannte IDs im Hintergrund nachladen.
        # Nicht-numerischer Login = ist bereits ein Rufzeichen (CALL=) -> kein radioid.net.
        if not str(cid).isdigit():
            return None
        base = self._base_id(cid)
        if base in self._id_names:
            return self._id_names[base] or None
        if base and base not in self._id_pending:
            self._id_pending.add(base)
            asyncio.ensure_future(self._radioid_fetch(base))
        return None

    async def _radioid_fetch(self, base):
        # radioid.net: erst User-, dann Repeater-Endpoint. Blockierendes urllib im
        # Executor -> Event-Loop bleibt frei. Ergebnis (auch leer) wird gecacht.
        def _q(kind):
            url = f"https://radioid.net/api/dmr/{kind}/?id={base}"
            try:
                with urllib.request.urlopen(url, timeout=8) as r:
                    res = json.loads(r.read().decode()).get("results") or []
                    return (res[0].get("callsign") or "").strip() if res else ""
            except Exception:
                return None
        loop = asyncio.get_event_loop()
        call = await loop.run_in_executor(None, _q, "user")
        if not call:
            call = await loop.run_in_executor(None, _q, "repeater")
        self._id_names[base] = call or ""      # "" = nachgeschlagen, nichts gefunden
        try:
            json.dump(self._id_names, open(self._idcache_path, "w"), ensure_ascii=False, indent=1)
        except Exception as e:
            log.error("radioid cache: %s", e)
        if call:
            log.info("RadioID %s -> %s", base, call)
        self._last_nodes = None                # Snapshot neu schreiben (Name kam dazu)

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

    def reload_node_meta(self):
        # node_meta (Verzeichnis-Roster: name/type/qth) live aus der config.json
        # nachladen, wenn sie sich geaendert hat — kosmetische Aenderungen (Badges,
        # QTH) brauchen dann KEINEN Server-Neustart (der Clients trennen wuerde).
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime == self._meta_mtime:
                return
            self._meta_mtime = mtime
            with open(CONFIG_PATH) as f:
                meta = json.load(f).get("node_meta", {})
            if meta != self.config.get("node_meta"):
                self.config["node_meta"] = meta
                self._last_nodes = None            # Snapshot neu schreiben
                log.info("node_meta neu geladen (%d Eintraege)", len(meta))
        except Exception as e:
            log.error("reload_node_meta: %s", e)

    async def nodes_loop(self):
        while True:
            self.reload_node_meta()
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
            if self.mesh_enabled:
                log.info("Mesh aktiv: local_prefix=%r, %d Peer(s)", self.local_prefix, len(self.peers))
                asyncio.ensure_future(self.mesh_loop())
                for p in self.peers.values():
                    if p.url:                          # nur Peers mit URL rauswaehlen (andere kommen rein)
                        asyncio.ensure_future(self.trunk_dial(p))
            await asyncio.Future()  # für immer


def main():
    asyncio.run(BrewServer(load_config()).run())


if __name__ == "__main__":
    main()
