"""Look up the current digest of an image tag in its registry, without pulling it.

Uses a HEAD request on the manifest (Docker Hub doesn't count HEAD requests against its
pull rate limit) and the registry's anonymous token flow, so it works for public images on
Docker Hub, GHCR, lscr.io, Quay and most other registries. With a login for a registry
(Patchdeck Settings → Docker) the token request is authenticated, which raises Docker Hub's
rate limit and allows private images.
"""
import asyncio
import re

import httpx

ACCEPT = ", ".join([
    "application/vnd.oci.image.index.v1+json",
    "application/vnd.docker.distribution.manifest.list.v2+json",
    "application/vnd.docker.distribution.manifest.v2+json",
    "application/vnd.oci.image.manifest.v1+json",
])
CHALLENGE = re.compile(r'(\w+)="([^"]*)"')


def parse_ref(ref):
    """'nginx' -> ('registry-1.docker.io', 'library/nginx', 'latest'). None for digest-pinned refs."""
    if "@" in ref:
        return None
    name, tag = ref, "latest"
    if ":" in ref.rsplit("/", 1)[-1]:
        name, tag = ref.rsplit(":", 1)
    parts = name.split("/")
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
        registry, repo = parts[0], "/".join(parts[1:])
    else:
        registry, repo = "registry-1.docker.io", name
    if registry in ("docker.io", "index.docker.io", "registry-1.docker.io"):
        registry = "registry-1.docker.io"
        if "/" not in repo:
            repo = f"library/{repo}"
    return registry, repo, tag


def normalise_registry(name):
    """'docker.io', 'hub.docker.com', '' -> 'registry-1.docker.io'; other hosts lowercased."""
    name = (name or "").strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
    if name in ("", "docker.io", "index.docker.io", "registry-1.docker.io", "hub.docker.com", "docker hub"):
        return "registry-1.docker.io"
    return name


def _base(registry):
    insecure = registry.startswith(("localhost", "127.0.0.1"))
    return f"{'http' if insecure else 'https'}://{registry}"


class Registry:
    """One instance per check run: caches tokens and digests so shared images are looked up once."""

    def __init__(self, concurrency=6, logins=None):
        # logins: {normalised registry: (username, password or access token)}
        self.logins = logins or {}
        self.tokens = {}
        self.cache = {}
        self.sem = asyncio.Semaphore(concurrency)
        self.client = httpx.AsyncClient(timeout=20, follow_redirects=True,
                                        headers={"User-Agent": "patchdeck"})

    async def close(self):
        await self.client.aclose()

    async def _token(self, header, registry, repo):
        login = self.logins.get(registry)
        if header.lower().startswith("basic"):
            if not login:
                raise RuntimeError("registry needs a login (add one in Settings → Docker)")
            self.tokens[(registry, repo)] = ("basic", login)
            return self.tokens[(registry, repo)]
        if not header.lower().startswith("bearer"):
            raise RuntimeError("registry needs a login")
        params = dict(CHALLENGE.findall(header))
        realm = params.pop("realm", None)
        if not realm:
            raise RuntimeError("registry sent no token address")
        params.setdefault("scope", f"repository:{repo}:pull")
        r = await self.client.get(realm, params=params, auth=login)
        if 400 <= r.status_code < 500 and r.status_code != 429:
            raise RuntimeError("registry login rejected" if login else
                               "private image (add a registry login in Settings → Docker)")
        r.raise_for_status()
        data = r.json()
        token = data.get("token") or data.get("access_token")
        if not token:
            raise RuntimeError("registry gave no token")
        self.tokens[(registry, repo)] = token
        return token

    def _auth(self, token, headers):
        if isinstance(token, tuple):  # registry uses Basic auth directly
            headers.pop("Authorization", None)
            return httpx.BasicAuth(*token[1])
        headers["Authorization"] = f"Bearer {token}"
        return None

    async def _lookup(self, ref):
        parsed = parse_ref(ref)
        if not parsed:
            return None, "pinned to a digest"
        registry, repo, tag = parsed
        url = f"{_base(registry)}/v2/{repo}/manifests/{tag}"
        headers = {"Accept": ACCEPT}
        token = self.tokens.get((registry, repo))
        auth = None
        for attempt in range(2):
            if token:
                auth = self._auth(token, headers)
            r = await self.client.head(url, headers=headers, auth=auth)
            if r.status_code == 401 and attempt == 0:
                token = await self._token(r.headers.get("www-authenticate", ""), registry, repo)
                continue
            break
        if r.status_code == 404:
            return None, "tag not found in registry"
        if r.status_code == 429:
            return None, ("registry rate limit reached" if registry in self.logins else
                          "registry rate limit reached (a Docker Hub login raises it: Settings → Docker)"
                          if registry == "registry-1.docker.io" else "registry rate limit reached")
        if r.status_code >= 400:
            return None, f"registry answered {r.status_code}"
        digest = r.headers.get("docker-content-digest")
        if not digest:  # a few registries only send it on GET
            r = await self.client.get(url, headers=headers, auth=auth)
            digest = r.headers.get("docker-content-digest")
        return (digest, None) if digest else (None, "registry sent no digest")

    async def digest(self, ref):
        """Returns (digest, None) or (None, reason)."""
        if ref not in self.cache:
            self.cache[ref] = asyncio.ensure_future(self._guarded(ref))
        return await self.cache[ref]

    async def _guarded(self, ref):
        async with self.sem:
            try:
                return await self._lookup(ref)
            except httpx.HTTPError as e:
                return None, f"couldn't reach registry ({e.__class__.__name__})"
            except Exception as e:
                return None, str(e)


async def annotate(stacks, registry):
    """Fill in status for each image in each stack: update / current / unknown / local."""
    async def one(img):
        if img.get("missing"):
            img.update(status="unknown", note="image not pulled yet")
            return
        if not img.get("digests"):
            img.update(status="local", note="built locally; Patchdeck can't check it")
            return
        remote, why = await registry.digest(img["ref"])
        if not remote:
            img.update(status="unknown", note=why)
        elif remote in img["digests"]:
            img.update(status="current", note=None)
        else:
            img.update(status="update", note=None, remote=remote)

    await asyncio.gather(*(one(i) for s in stacks for i in s["images"]))
    for s in stacks:
        s["updates"] = sum(1 for i in s["images"] if i["status"] == "update")


async def test_login(registry, username, secret):
    """Check a login by fetching a pull token. Returns (ok, message)."""
    registry = normalise_registry(registry)
    reg = Registry(logins={registry: (username, secret)})
    try:
        if registry == "registry-1.docker.io":
            r = await reg.client.get("https://auth.docker.io/token",
                                     params={"service": "registry.docker.io", "scope": "repository:library/alpine:pull"},
                                     auth=(username, secret))
            if 400 <= r.status_code < 500:
                try:
                    why = r.json().get("details") or ""
                except ValueError:
                    why = ""
                return False, f"Docker Hub rejected the login{': ' + why if why else ''}"
            r.raise_for_status()
            token = r.json().get("token")
            head = await reg.client.head("https://registry-1.docker.io/v2/library/alpine/manifests/latest",
                                         headers={"Accept": ACCEPT, "Authorization": f"Bearer {token}"})
            left = (head.headers.get("ratelimit-remaining") or "").split(";")[0]
            limit = (head.headers.get("ratelimit-limit") or "").split(";w=")
            if left and len(limit) == 2:
                hours = int(limit[1]) // 3600
                return True, f"Logged in. {left} of {limit[0]} lookups left in the current {hours}-hour window."
            return True, "Logged in."
        r = await reg.client.get(f"{_base(registry)}/v2/")
        if r.status_code == 200:
            return True, "This registry doesn't need a login."
        header = r.headers.get("www-authenticate", "")
        if header.lower().startswith("basic"):
            r = await reg.client.get(f"{_base(registry)}/v2/", auth=(username, secret))
            return (r.status_code == 200), ("Logged in." if r.status_code == 200 else "Login rejected")
        params = dict(CHALLENGE.findall(header))
        realm = params.pop("realm", None)
        if not realm:
            return False, f"Unexpected answer from the registry ({r.status_code})"
        r = await reg.client.get(realm, params=params, auth=(username, secret))
        return (r.status_code == 200), ("Logged in." if r.status_code == 200 else "Login rejected")
    except httpx.HTTPError as e:
        return False, f"Couldn't reach the registry ({e.__class__.__name__})"
    finally:
        await reg.close()
