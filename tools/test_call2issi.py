#!/usr/bin/env python3
"""Regressions-/Paritaets-Test fuer call2issi.

Diese Vektoren muessen identisch von der C++-Spiegelung im Modul
(svxlink-module-tetrabrew/ModuleTetraBrew.cpp, Funktion call2issi) geliefert
werden. Aendert sich hier etwas, MUSS die C++-Seite mitgezogen werden.
"""
import itertools
import string
from call2issi import call2issi, is_structural, BLOCK_BASE, GERMAN_SPAN, FALLBACK_BASE

# Feste Vektoren (mit der C++-Spiegelung gegengeprueft).
VECTORS = {
    "DO0RAM":   12768782,
    "DB0WL":    10213921,
    "DM0ABC":   12362746,
    "DB0XX":    10214974,
    "DO0RAM/P": 12768782,   # SSID/Zusatz wird abgeschnitten
    "OE5XYZ":   15775855,   # Hash-Fallback (nicht-DE)
    "HB9AW":    16288783,   # Hash-Fallback
}


def test_vectors():
    for call, want in VECTORS.items():
        got = call2issi(call)
        assert got == want, f"{call}: {got} != {want}"


def test_german_calls_collision_free():
    """Alle deutschen Repeater-Calls (D?0 + 2-3 Buchstaben) -> keine Kollision."""
    seen = {}
    for l2 in string.ascii_uppercase:
        for n in (2, 3):
            for suf in itertools.product(string.ascii_uppercase, repeat=n):
                call = "D" + l2 + "0" + "".join(suf)
                i = call2issi(call)
                assert is_structural(call)
                assert BLOCK_BASE <= i <= 16_777_215
                assert i < FALLBACK_BASE, f"{call} laeuft in den Fallback-Block"
                assert i not in seen, f"Kollision: {call} und {seen[i]} -> {i}"
                seen[i] = call
    # 26 * (26^2 + 26^3) moegliche DE-Repeater-Calls, alle kollisionsfrei
    assert len(seen) == 26 * (26**2 + 26**3) == 474552


if __name__ == "__main__":
    test_vectors()
    print(f"OK: {len(VECTORS)} feste Vektoren stimmen.")
    seen = {}
    for l2 in string.ascii_uppercase:
        for n in (2, 3):
            for suf in itertools.product(string.ascii_uppercase, repeat=n):
                call = "D" + l2 + "0" + "".join(suf)
                i = call2issi(call)
                assert i not in seen, f"Kollision {call}/{seen[i]}"
                seen[i] = call
    print(f"OK: {len(seen)} deutsche Repeater-Calls, 0 Kollisionen, "
          f"Bereich {min(seen)}..{max(seen)} (< {FALLBACK_BASE}).")
