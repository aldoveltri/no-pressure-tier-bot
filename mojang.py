import re
import time
import aiohttp

CACHE_TTL = 6 * 60 * 60  # 6 hours
_cache: dict[str, tuple[float, tuple[str, str]]] = {}

IGN_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


async def get_uuid(ign: str):
    """Returns (uuid, correct_ign) or None if the username doesn't exist.
    Raises RuntimeError if Mojang doesn't answer properly."""
    if not IGN_RE.match(ign):
        return None

    key = ign.lower()
    now = time.time()
    if key in _cache and now - _cache[key][0] < CACHE_TTL:
        return _cache[key][1]

    url = f"https://api.mojang.com/users/profiles/minecraft/{ign}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as r:
            if r.status == 200:
                data = await r.json()
                result = (data["id"], data["name"])
                _cache[key] = (now, result)
                return result
            if r.status in (204, 404):
                return None
            raise RuntimeError(f"Mojang answered with status {r.status}")


async def get_name(uuid: str):
    """Current username of a UUID, or None if the profile doesn't exist.
    Raises RuntimeError if Mojang is limiting requests or not answering."""
    url = f"https://sessionserver.mojang.com/session/minecraft/profile/{uuid}"
    timeout = aiohttp.ClientTimeout(total=10)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url) as r:
            if r.status == 200:
                data = await r.json()
                return data["name"]
            if r.status in (204, 404):
                return None
            raise RuntimeError(f"Mojang answered with status {r.status}")