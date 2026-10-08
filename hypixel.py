import os

import aiohttp


class HypixelError(RuntimeError):
    pass


class NoApiKey(HypixelError):
    pass


class KeyRejected(HypixelError):
    pass


def normalize_handle(handle: str) -> str:
    """Lowercases a Discord handle and drops the old '#0' suffix."""
    h = handle.strip().lower()
    if h.endswith("#0") or h.endswith("#0000"):
        h = h.split("#")[0]
    return h


async def get_hypixel_discord(uuid: str):
    """Returns (found, discord_handle).
    found is False if Hypixel has no data for this UUID; discord_handle is None
    if the player hasn't set a Discord name on Hypixel."""
    key = os.getenv("HYPIXEL_API_KEY")
    if not key:
        raise NoApiKey()

    url = f"https://api.hypixel.net/v2/player?uuid={uuid}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, headers={"API-Key": key}) as r:
            if r.status == 429:
                raise HypixelError("Rate limited by Hypixel.")
            if r.status in (401, 403):
                raise KeyRejected()
            if r.status != 200:
                raise HypixelError(f"Hypixel answered with status {r.status}.")
            data = await r.json()

    player = data.get("player")
    if not player:
        return False, None
    links = (player.get("socialMedia") or {}).get("links") or {}
    handle = links.get("DISCORD")
    return True, (handle.strip() if handle else None)