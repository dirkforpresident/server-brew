# Mesh — mehrere FreeTetra-Server koppeln

Du kannst FreeTetra **allein** betreiben (Standard) oder **mehrere Server koppeln**,
sodass Talkgroups über Servergrenzen hinweg zusammenlaufen. Es gibt **keine zentrale
Stelle**: zwei Betreiber einigen sich auf einen **Link-Namen** und ein **gemeinsames
Geheimnis**, fertig. Ohne Geheimnis kann sich niemand mit dir meshen.

## Standalone (Default)
Nichts zu tun. Ohne `"mesh"`-Block (oder mit leerer `peers`-Liste) läuft der Server
exakt wie bisher — nur lokale Clients.

## Prinzip
- Jeder Server **besitzt eine TG-Vorwahl** (`local_prefix`), z.B. `262` → ihm gehören
  alle TGs, die mit 262 anfangen (2621, 26299, …). Wie Telefon-Vorwahlen.
- Ein Ruf auf einer fremden TG geht automatisch zum **Besitzer** dieser Vorwahl
  (Longest-Prefix-Match). Nicht beanspruchte TGs bleiben lokal.
- **Loop-sicher** ohne Pfadlisten: nur der Besitzer verteilt an interessierte Peers.

## Zwei Server koppeln
Server **A** (besitzt `262`) und Server **B** (besitzt `263`).
Beide bekommen denselben **Link-Namen** (`AB`) und dasselbe **Secret**.

**config.json auf A:**
```json
"mesh": {
  "local_prefix": "262",
  "peers": [
    { "name": "AB", "secret": "EIN-LANGES-GEHEIMNIS", "prefix": "263",
      "url": "wss://server-b.example/trunk" }
  ]
}
```

**config.json auf B:**
```json
"mesh": {
  "local_prefix": "263",
  "peers": [
    { "name": "AB", "secret": "EIN-LANGES-GEHEIMNIS", "prefix": "262",
      "url": "wss://server-a.example/trunk" }
  ]
}
```

- `name` + `secret` **müssen auf beiden Seiten gleich** sein.
- `prefix` = die Vorwahl der **Gegenseite**.
- `url` = wie man die Gegenseite erreicht (`wss://…/trunk`, via nginx auf den WS-Port).

Das war's. Beide bauen die Verbindung auf; nur **eine Seite braucht einen offenen
Port** (die andere wählt raus, NAT-tauglich). Mehr Server = einfach weitere Peers
eintragen.

## Welche TGs will ich? (optional)
- **Global** — nur bestimmte TGs überhaupt meshen:
  `"tgs": ["262*", "263*", "91"]` im `mesh`-Block (fehlt/`null` = alle).
- **Pro Peer** — von *diesem* Peer nur bestimmte TGs:
  `"tgs": ["262*"]` in der Peer-Zeile.
- Muster: exakt (`"8"`) oder Prefix (`"262*"`).

## nginx
Der Trunk läuft über denselben WebSocket-Port wie die Clients, Pfad `/trunk`.
In der nginx-Site einfach zusätzlich weiterreichen:
```nginx
location /trunk { proxy_pass http://127.0.0.1:8443; proxy_set_header Upgrade $http_upgrade; proxy_set_header Connection "upgrade"; }
```

## Sicherheit
- Meshen geht **nur** mit passendem `secret` (HMAC-geprüftes Hello, Zeitfenster 120 s).
- Läuft über `wss://` (TLS via nginx). Wer dein Secret nicht kennt, kommt nicht rein.

## Was (noch) nicht über das Mesh geht
- **SDS/Text-Nachrichten** zwischen Servern (aktuell nur lokal). Sprache + Talkgroups
  laufen voll über das Mesh.
