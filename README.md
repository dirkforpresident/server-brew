# FreeTetra — freier TETRA-BREW-Server

Ein kleiner, offener **BREW-Server**, der TETRA-Stationen (BlueStations, Eigenbau-TMO-Repeater) und —
über [**ModuleTetraBrew**](https://github.com/do1xx/svxlink-module-tetrabrew) — auch
**SvxLink-FM-Repeater** zu einem gemeinsamen Sprach-Netz verbindet. Mehrere Server lassen sich
dezentral **koppeln** (Mesh, siehe [MESH.md](MESH.md)). Reines `python3` (asyncio + websockets),
eine Datei, keine Datenbank.

## Mitmachen oder selbst hosten

- **Einfach testen / mitmachen:** verbinde dich mit dem öffentlichen Server
  **`wss://freetetra.de/brew/`** — Username = deine **RadioID** (TETRA-Stationen) bzw. dein
  **Rufzeichen** (FM-Repeater über das Modul), Passwort `freetetra`.
- **Eigenen Node hosten:** dieses Repo. Jede Region/Gruppe kann ihren **eigenen** FreeTetra-Server
  fahren — so entstehen viele kleine Netze statt eines zentralen.

## Wie es funktioniert

- Clients (BlueStations, Gateways, das FM-Modul) verbinden per **WebSocket** + **HTTP-Digest-Auth**.
- Routing per **Talkgroup (GSSI)**: wer auf eine GSSI affiliiert, hört deren Gruppenruf. Sprache läuft
  als **ACELP**-Frames durch — TETRA-Qualität, kein Transcoding.
- **Open-Mode**: jeder mit gültigem Login + Community-Passwort darf rein. Der Login-Name *ist* die
  Identität — eine **RadioID** (TETRA-Stationen) oder ein **Rufzeichen** (FM-Repeater über das Modul).
  Alternativ eine feste User-Liste. Eine **Sperrliste** wirft einzelne wieder raus.
- **Echo-Test**: ein Ruf auf eine `echo_gssis`-Talkgroup wird aufgenommen und zurückgespielt (Audio-Check).

## Was er kann — und was nicht

Der Kern ist ein **Gruppenruf- und SDS-Router mit Presence**, kein vollständiges TETRA-SwMI.

**Kann:**

- ISSI **Register / Reregister / Deregister**
- **Affiliation** an Talkgroups (GSSI), auch mehrere GSSIs je ISSI
- **Gruppenruf** (GROUP_TX → GROUP_IDLE), ACELP-Sprache an alle affiliierten Teilnehmer
- **SDS**: Gruppen-SDS und Einzel-ISSI (SHORT_TRANSFER), Transfer + Report — unverändert weitergereicht
- **Presence-Query** (SERVICE): ist ISSI registriert? welches Callsign? welche GSSIs?
- Auth: feste User-Liste, Open-Mode (RadioID/Rufzeichen + Passwort), Blocklist
- Echo-/Papagei-Test, Broadcast-Modus (Ein-Stationen-Test: alle hören alles)
- **Mesh**: mehrere Server koppeln — Talkgroups über Servergrenzen, Prefix-Ownership,
  loop-sicher, nur mit gemeinsamem Secret ([MESH.md](MESH.md))
- **Live-Verzeichnis** der verbundenen Knoten (`nodes.json`/-Webseite) mit Status, **ISSI**
  und Talkgroup. Rufzeichen kommt aus dem Login-Namen (FM-Repeater) bzw. wird automatisch aus
  der RadioID-Datenbank aufgelöst (TETRA-Stationen)

**Kann nicht (teils bewusst weggelassen):**

- **Keine Einzel-/Privatgespräche** (Punkt-zu-Punkt-Voice) — nur Gruppenruf. SHORT_TRANSFER ist der SDS-Vorspann, kein Voice-Einzelruf.
- **Kein Late-Entry** — die Zielmenge wird bei GROUP_TX fixiert; wer danach affiliiert, hört den laufenden Ruf nicht mehr.
- **Keine Priorität / Notruf / Preemption**
- **Kein SDS Store-and-Forward** — Ziel-ISSI offline → Nachricht wird verworfen (keine spätere Zustellung)
- **SDS läuft (noch) nicht über das Mesh** — Text/SDS bleibt lokal am Server; Sprache + Talkgroups meshen
- **Keine Persistenz** — keine Datenbank, kein QSO-/Nachrichten-Log (nur Laufzeit-Logs)
- **Kein Transcoding** — ACELP wird 1:1 durchgereicht; FM↔TETRA-Wandlung macht das Modul, nicht der Server
- **Kein echtes SwMI/ISI** — keine TETRA-Call-Control-Signalisierung, keine Mobility, kein Interworking zu echten TETRA-Netzen
- **Keine serverseitige GSSI-Zugangskontrolle** — jeder registrierte Client darf jede Talkgroup nutzen; die ACL liegt beim Client/Modul (`GSSI_ALLOW`)
- **Keine SDS-Semantik** — LIP/GPS-Position, Status-Codes o.ä. werden nicht interpretiert, nur Bytes weitergereicht

## Voraussetzungen

- `python3.10+` mit `websockets` (`pip install websockets`)
- ein Reverse-Proxy (nginx o.ä.) als **TLS-Terminierung** für `wss://` — der Server selbst spricht
  plaintext `ws://` auf localhost.

## Starten

```bash
cp config.example.json config.json     # anpassen (siehe unten)
pip install websockets
python3 server.py                       # oder brew-server.service als systemd-Unit
```

Der Server lauscht auf `127.0.0.1:8443`. Davor der Reverse-Proxy für `wss://<deine-domain>/brew/`:

```nginx
# im http { } Block:
map $http_upgrade $connection_upgrade { default upgrade; '' close; }

# im server { } (mit TLS-Cert):
root /opt/brew/web;                 # Landingpage + Verzeichnis (nodes.html)

location /brew/  { proxy_pass http://127.0.0.1:8443; proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host; proxy_read_timeout 86400; }

location /trunk  { proxy_pass http://127.0.0.1:8443; proxy_http_version 1.1;   # Mesh: Server-Trunk
    proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host; proxy_read_timeout 86400; }

location /       { try_files $uri $uri/ =404; }
```

## Konfiguration (`config.json`)

```json
{
  "server":          { "host": "127.0.0.1", "port": 8443 },
  "open":            true,            // jeder mit RadioID/Rufzeichen + open_password darf rein
  "open_password":   "freetetra",
  "users":           {},             // optional feste User: "<Login>": {"password":"…","callsign":"<CALL>"}
  "blocklist":       [],             // gesperrte Logins/ISSIs (rausschmeißen)
  "echo_gssis":      [9],            // Talkgroups mit Echo-Test
  "broadcast_groups": false,         // nur Ein-Stationen-Test: true = alle hören alles
  "node_meta":       {},             // Klartext fürs Verzeichnis, Key = Login-Name: "<RadioID|CALL>": {"name":"…","type":"FM|DMO|TMO","qth":"…"}
  "mesh":            {}              // leer = Standalone. Mesh-Beispiel siehe MESH.md
}
```

`config.json` enthält Passwörter/Secrets → **nicht committen** (ist in `.gitignore`).

## FM-Repeater anbinden

FM-Relais kommen über das Modul rein → siehe
[**svxlink-module-tetrabrew**](https://github.com/do1xx/svxlink-module-tetrabrew).

Ein FM-Repeater hat von sich aus keine ISSI. Das Modul macht ihn zu **einem virtuellen
TETRA-Teilnehmer** mit einer festen ISSI, die aus dem **Rufzeichen** abgeleitet wird
([`tools/call2issi.py`](tools/call2issi.py) — deutsche Repeater-Calls kollisionsfrei, sonst
Hash-Fallback, immer im diallbaren FM-Block `10.000.000–16.777.215`). Diese ISSI steht im
Verzeichnis, damit die TETRA-Seite den Repeater per SDS/Einzelruf erreichen kann. Die Logik ist
identisch im Modul (C++) hinterlegt — beide müssen dieselbe Zahl liefern.

---

Freies Community-Projekt. Nutzung auf eigene Verantwortung, haltet euch ans AFU-Recht. — DO1XX
