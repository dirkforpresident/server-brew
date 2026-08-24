# FreeTetra — BREW-Server

Freier TETRA-BREW-Server. Reines python3 (asyncio + `websockets`), eine Datei (`server.py`), keine DB.

## Aufbau
- **HTTP-Digest-Auth** (`process_request`) → Session-UUID → **WebSocket-Upgrade** (`/brew/<uuid>`).
- Binäres BREW-Protokoll (Little-Endian): SUBSCRIBER (Register/Affiliate), CALL (Group-TX/Idle,
  Short-Transfer), FRAME (Traffic/SDS), SERVICE (Query).
- Routing per **GSSI/Talkgroup** (`targets_for_gssi`). Sprache = ACELP-Frames, unverändert weitergereicht.
- **Open-Mode** (`verify_digest`): jeder mit RadioID + `open_password`; alternativ feste `users`; `blocklist` sperrt.
- **Echo-Test** auf `echo_gssis` (aufnehmen → zurückspielen).

## Konfig / Betrieb
- `config.json` (aus `config.example.json`) — **nicht** committen (Passwörter, `.gitignore`).
- Server bindet localhost, `wss://` via Reverse-Proxy (nginx) davor. Siehe `README.md`.
- systemd: `brew-server.service`.

## Konventionen
- Änderungen klein halten, Protokoll-Semantik nicht brechen (Clients: BlueStations + ModuleTetraBrew).
- Kein zusätzlicher Ballast (SDS-Bridges, Bots etc. gehören in eigene Tools, nicht in den Kern-Server).
