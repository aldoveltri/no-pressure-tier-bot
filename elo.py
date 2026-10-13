# Points for each match, based on the elo difference with the opponent.
# Each tuple is indexed by the games won by the LOSER (0, 1, 2...).

# ----- first to 3 (Normal mode, Bedwars Crossmap, The Bridge) -----
WIN = {
    "molto_basso": (1, 1, 1),
    "basso":       (3, 2, 1),
    "normale":     (15, 10, 5),
    "alto":        (45, 30, 15),
    "molto_alto":  (90, 60, 30),
}
LOSS = {
    "molto_basso": (-36, -24, -12),
    "basso":       (-18, -12, -6),
    "normale":     (-9, -6, -3),
    "alto":        (-3, -2, -1),
    "molto_alto":  (-1, -1, -1),
}

# ----- first to 5 (Classic 1v1) -----
WIN5 = {
    "molto_basso": (1, 1, 1, 1, 1),
    "basso":       (5, 5, 3, 3, 2),
    "normale":     (25, 25, 17, 17, 8),
    "alto":        (75, 75, 50, 50, 25),
    "molto_alto":  (150, 150, 100, 100, 50),
}
LOSS5 = {
    "molto_basso": (-60, -60, -40, -40, -20),
    "basso":       (-30, -30, -20, -20, -10),
    "normale":     (-15, -15, -10, -10, -5),
    "alto":        (-5, -5, -5, -5, -5),
    "molto_alto":  (-1, -1, -1, -1, -1),
}

SYSTEMS = {"ft3": (WIN, LOSS), "ft5": (WIN5, LOSS5)}
FIRST_TO = {"ft3": 3, "ft5": 5}


def categoria(mio_elo: int, avversario_elo: int) -> str:
    diff = avversario_elo - mio_elo
    if diff <= -100: return "molto_basso"
    if diff <= -35:  return "basso"
    if diff >= 100:  return "molto_alto"
    if diff >= 35:   return "alto"
    return "normale"


def calcola(elo_vincitore: int, elo_perdente: int, partite_perse: int, system: str = "ft3"):
    """partite_perse = games won by the loser (0, 1, 2... up to first-to minus 1).
    Returns (winner's change, loser's change)."""
    win, loss = SYSTEMS[system]
    i = partite_perse
    guadagno = win[categoria(elo_vincitore, elo_perdente)][i]
    perdita = loss[categoria(elo_perdente, elo_vincitore)][i]
    perdita = max(perdita, -elo_perdente)   # elo never goes below 0
    return guadagno, perdita


if __name__ == "__main__":
    print(calcola(250, 250, 0))           # expected (15, -9)
    print(calcola(100, 250, 0))           # expected (90, -36)
    print(calcola(250, 250, 0, "ft5"))    # expected (25, -15)
    print(calcola(100, 250, 4, "ft5"))    # expected (50, -1)