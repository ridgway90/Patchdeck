import logging

import httpx

log = logging.getLogger("patchdeck")


async def discord(webhook, text):
    if not webhook:
        return
    # Discord rejects messages over 2000 characters.
    if len(text) > 1990:
        text = text[:1950].rsplit("\n", 1)[0] + "\n…and more"
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            r = await client.post(webhook, json={"content": text})
            r.raise_for_status()
    except Exception as e:
        log.warning("Discord notification failed: %s", e)
