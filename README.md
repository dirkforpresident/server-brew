# FreeTetra — freier TETRA-BREW-Server

Ein kleiner, offener **BREW-Server**, der TETRA-Stationen (BlueStations, Eigenbau-TMO-Repeater) und —
über [**ModuleTetraBrew**](https://github.com/do1xx/svxlink-module-tetrabrew) — auch
**SvxLink-FM-Repeater** zu einem gemeinsamen Sprach-Netz verbindet. Reines `python3` (asyncio +
websockets), ~500 Zeilen, keine Datenbank.

## Mitmachen oder selbst hosten

- **Einfach testen / mitmachen:** verbinde dich mit dem öffentlichen Server
  **`wss://freetetra.de/brew/`** — Username = deine **RadioID**, Passwort `freetetra`.
- **Eigenen Node hosten:** dieses Repo. Jede Region/Gruppe kann ihren **eigenen** FreeTetra-Server
  fahren — so entstehen viele kleine Netze statt eines zentralen.

## Wie es funktioniert

- Clients (BlueStations, Gateways, das FM-Modul) verbinden per **WebSocket** + **HTTP-Digest-Auth**.
- Routing per **Talkgroup (GSSI)**: wer auf eine GSSI affiliiert, hört deren Gruppenruf. Sprache läuft
  als **ACELP**-Frames durch — TETRA-Qualität, kein Transcoding.
- **Open-Mode**: jeder mit RadioID + Community-Passwort darf rein (die RadioID *ist* die Identität).
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
- Auth: feste User-Liste, Open-Mode (RadioID + Passwort), Blocklist
- Echo-/Papagei-Test, Broadcast-Modus (Ein-Stationen-Test: alle hören alles)

**Kann nicht (teils bewusst weggelassen):**

- **Keine Einzel-/Privatgespräche** (Punkt-zu-Punkt-Voice) — nur Gruppenruf. SHORT_TRANSFER ist der SDS-Vorspann, kein Voice-Einzelruf.
- **Kein Late-Entry** — die Zielmenge wird bei GROUP_TX fixiert; wer danach affiliiert, hört den laufenden Ruf nicht mehr.
- **Keine Priorität / Notruf / Preemption**
- **Kein SDS Store-and-Forward** — Ziel-ISSI offline → Nachricht wird verworfen (keine spätere Zustellung)
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
location /brew/ {
    proxy_pass http://127.0.0.1:8443;
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection $connection_upgrade;
    proxy_set_header Host $host;
    proxy_read_timeout 86400;
}
```

## Konfiguration (`config.json`)

```json
{
  "server":          { "host": "127.0.0.1", "port": 8443 },
  "open":            true,            // jeder mit RadioID + open_password darf rein
  "open_password":   "freetetra",
  "users":           {},             // optional feste User: "<RadioID>": {"password":"…","callsign":"<CALL>"}
  "blocklist":       [],             // gesperrte RadioIDs/ISSIs (rausschmeißen)
  "echo_gssis":      [9],            // Talkgroups mit Echo-Test
  "broadcast_groups": false          // nur Ein-Stationen-Test: true = alle hören alles
}
```

`config.json` enthält Passwörter → **nicht committen** (ist in `.gitignore`).

## FM-Repeater anbinden

FM-Relais kommen über das Modul rein → siehe
[**svxlink-module-tetrabrew**](https://github.com/do1xx/svxlink-module-tetrabrew).

---

Freies Community-Projekt. Nutzung auf eigene Verantwortung, haltet euch ans AFU-Recht. — DO1XX
