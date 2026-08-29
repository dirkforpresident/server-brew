#!/usr/bin/env python3
"""call2issi — Rufzeichen -> feste, diallbare FreeTetra-FM-ISSI.

Ein FM-Repeater hat von sich aus keine ISSI. Damit die TETRA-Seite den
virtuellen FM-Teilnehmer erreichen kann, braucht er eine numerische ISSI, die
    - stabil ist (gleiches Call -> immer dieselbe Nummer, ohne Datenbank),
    - von jedem TETRA-Funkgeraet waehlbar ist (24 bit, max 16.777.215),
    - nicht mit echten DMR-/TETRA-Teilnehmern kollidiert.

Reservierter FM-Block:  10.000.000 .. 16.777.215  (ueber allen DMR-IDs, in 24 bit)

Deutsche Repeater-Calls (D[Buchstabe]0[2-3 Buchstaben]) haben eine feste
Struktur mit nur ~5,1 Mio moeglichen Kombinationen. Die packen wir per
Stellenwert-Kodierung direkt in eine Zahl -> ECHTE 1:1-Abbildung, garantiert
kollisionsfrei (zwei verschiedene Calls -> zwei verschiedene ISSIs).

Alles andere (OE, HB9, Sonderformen) landet per crc32 in einem separaten
Fallback-Unterblock. Dort sind Kollisionen rechnerisch extrem selten und werden
serverseitig live erkannt und gemeldet.
"""
import re
import sys
from zlib import crc32

BLOCK_BASE     = 10_000_000          # Start des FM-Blocks
GERMAN_SPAN    = 26 * 10 * 27**3     # 5.117.580 Kombinationen (Index 0..5.117.579)
FALLBACK_BASE  = 15_200_000          # separater Unterblock fuer Nicht-DE / Sonderformen
FALLBACK_SPAN  = 1_500_000           # 15.200.000 .. 16.699.999  (< 16.777.215)

_DE = re.compile(r"^D([A-Z])0([A-Z]{2,3})$")


def normalize(call: str) -> str:
    """Basis-Rufzeichen: Grossbuchstaben, ohne SSID/Zusatz (/P, -7, ...)."""
    call = call.strip().upper()
    call = re.split(r"[/\-]", call, maxsplit=1)[0]   # DO0RAM/P -> DO0RAM
    return call


def call2issi(call: str) -> int:
    """Rufzeichen -> feste FM-ISSI im reservierten Block."""
    c = normalize(call)
    m = _DE.match(c)
    if m:
        letter2 = ord(c[1]) - ord("A")         # 0..25
        digit   = int(c[2])                    # immer 0 bei Repeatern, 0..9 erlaubt
        suffix  = m.group(2)                    # 2-3 Buchstaben
        scode = 0
        for i in range(3):                      # 3 Slots, base-27: leer=0, A=1..Z=26
            v = (ord(suffix[i]) - ord("A") + 1) if i < len(suffix) else 0
            scode = scode * 27 + v
        idx = (letter2 * 10 + digit) * 27**3 + scode
        return BLOCK_BASE + idx                 # 10.000.000 .. 15.117.579
    # Fallback: alles, was nicht dem DE-Repeater-Muster entspricht
    return FALLBACK_BASE + (crc32(c.encode()) % FALLBACK_SPAN)


def is_structural(call: str) -> bool:
    """True, wenn das Call sauber (kollisionsfrei) strukturell kodiert wird."""
    return _DE.match(normalize(call)) is not None


if __name__ == "__main__":
    if len(sys.argv) > 1:
        for call in sys.argv[1:]:
            kind = "struktur" if is_structural(call) else "hash-fallback"
            print(f"{normalize(call):8s} -> {call2issi(call):>8d}  ({kind})")
    else:
        print("Aufruf: call2issi.py <RUFZEICHEN> [RUFZEICHEN ...]")
        sys.exit(1)
