# FreeTetra — freier TETRA-BREW-Server

Ein kleiner, offener **BREW-Server**, der TETRA-Stationen (BlueStations, Eigenbau-TMO-Repeater) und —
über [**ModuleTetraBrew**](https://github.com/do1xx/svxlink-module-tetrabrew) — auch
**SvxLink-FM-Repeater** zu einem gemeinsamen Sprach-Netz verbindet. Reines `python3` (asyncio +
websockets), ~380 Zeilen, keine Datenbank.

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

⚠️ `config.json` enthält Passwörter → **nicht committen** (ist in `.gitignore`).

## FM-Repeater anbinden

FM-Relais kommen über das Modul rein → siehe
[**svxlink-module-tetrabrew**](https://github.com/do1xx/svxlink-module-tetrabrew).

---

Freies Community-Projekt. Nutzung auf eigene Verantwortung, haltet euch ans AFU-Recht. — DO1XX
