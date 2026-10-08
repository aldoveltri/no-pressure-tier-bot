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

def categoria(mio_elo: int, avversario_elo: int) -> str:
    diff = avversario_elo - mio_elo
    if diff <= -100: return "molto_basso"
    if diff <= -35:  return "basso"
    if diff >= 100:  return "molto_alto"
    if diff >= 35:   return "alto"
    return "normale"

def calcola(elo_vincitore: int, elo_perdente: int, partite_perse: int):
    """partite_perse = partite vinte dal perdente (0, 1 o 2)."""
    i = partite_perse
    guadagno = WIN[categoria(elo_vincitore, elo_perdente)][i]
    perdita = LOSS[categoria(elo_perdente, elo_vincitore)][i]
    perdita = max(perdita, -elo_perdente)   # l'elo non scende sotto 0
    return guadagno, perdita

if __name__ == "__main__":
    print(calcola(250, 250, 0))   # atteso (15, -9)
    print(calcola(100, 250, 0))   # atteso (90, -36)