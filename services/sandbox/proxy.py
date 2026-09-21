"""Per-call egress proxy — the only route out of a skill container.

A runner container that needs the network is attached to an **internal** Docker
network, which has no route to the outside. The single thing it can reach is
this proxy, and the proxy will only connect onwards to hosts the *called
function* declared. Enforcement therefore sits outside the guest process: a
skill cannot widen its own allowlist by opening a raw socket, because there is
nowhere for that socket to go.

Scoping is per execution, not per skill: the sandbox registers a random token
with that call's allowlist, passes it to the container as proxy credentials,
and revokes it when the container exits. Two functions of the same skill with
different allowlists never share an egress policy, and a leaked token is
useless the moment the call ends.

Speaks the two forms a client actually uses: ``CONNECT host:443`` for HTTPS and
absolute-form ``GET http://host/path`` for plain HTTP.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import secrets
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Ports a skill may reach. Anything else is refused before a socket is opened.
ALLOWED_PORTS = {80, 443}

#: Guard against a proxied transfer being used to stream gigabytes into the
#: container (and thus into the answer).
MAX_TRANSFER_BYTES = 8 * 1024 * 1024


@dataclass
class _Grant:
    allowed_domains: frozenset[str]
    label: str  # "skill.function", for logging only


class EgressProxy:
    """HTTP proxy with a per-token domain allowlist."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8888) -> None:
        self.host = host
        self.port = port
        self._grants: dict[str, _Grant] = {}
        self._server: asyncio.AbstractServer | None = None

    # ── grant lifecycle ──────────────────────────────────────────────

    def grant(self, allowed_domains: list[str], label: str) -> str:
        """Register an allowlist for one execution; returns its token."""
        token = secrets.token_urlsafe(24)
        self._grants[token] = _Grant(
            allowed_domains=frozenset(d.lower().strip() for d in allowed_domains if d),
            label=label,
        )
        return token

    def revoke(self, token: str) -> None:
        self._grants.pop(token, None)

    @property
    def active_grants(self) -> int:
        return len(self._grants)

    # ── server ───────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self.host, self.port
        )
        logger.info("Egress proxy listening on %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # ── request handling ─────────────────────────────────────────────

    def _authorise(self, headers: dict[str, str], host: str) -> tuple[bool, str]:
        raw = headers.get("proxy-authorization", "")
        if not raw.lower().startswith("basic "):
            return False, "Proxy-Authentifizierung fehlt"
        try:
            decoded = base64.b64decode(raw[6:].strip()).decode("utf-8", "replace")
        except (ValueError, UnicodeDecodeError):
            return False, "Ungültige Proxy-Credentials"
        token = decoded.split(":", 1)[0]
        grant = self._grants.get(token)
        if grant is None:
            return False, "Unbekanntes oder abgelaufenes Egress-Token"
        # Exact host match only — the allowlist is a list of hosts, not a
        # suffix rule, so "evil-api.open-meteo.com.attacker.tld" cannot pass by
        # ending in an allowed name.
        if host.lower() not in grant.allowed_domains:
            logger.warning(
                "Egress denied for %s: %s not in %s",
                grant.label, host, sorted(grant.allowed_domains),
            )
            return False, f"Host {host} steht nicht auf der Allowlist"
        return True, ""

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=15)
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError,
                asyncio.TimeoutError, ConnectionError):
            writer.close()
            return

        lines = head.decode("latin-1").split("\r\n")
        request_line = lines[0] if lines else ""
        headers = {}
        for line in lines[1:]:
            if ":" in line:
                key, _, value = line.partition(":")
                headers[key.strip().lower()] = value.strip()

        parts = request_line.split()
        if len(parts) < 3:
            await self._deny(writer, 400, "Ungültige Proxy-Anfrage")
            return

        method, target = parts[0].upper(), parts[1]
        try:
            if method == "CONNECT":
                await self._handle_connect(reader, writer, target, headers)
            else:
                await self._handle_absolute(reader, writer, target, headers, head)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Proxy error for %s %s: %s", method, target, exc)
            try:
                writer.close()
            except Exception:  # noqa: BLE001
                pass

    async def _deny(self, writer: asyncio.StreamWriter, status: int, reason: str) -> None:
        body = reason.encode("utf-8")
        writer.write(
            f"HTTP/1.1 {status} Forbidden\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Content-Type: text/plain; charset=utf-8\r\n"
            "Proxy-Connection: close\r\n\r\n".encode("latin-1")
            + body
        )
        try:
            await writer.drain()
        except ConnectionError:
            pass
        writer.close()

    async def _handle_connect(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        headers: dict[str, str],
    ) -> None:
        host, _, port_raw = target.rpartition(":")
        port = int(port_raw) if port_raw.isdigit() else 443
        if not host:
            host, port = target, 443
        if port not in ALLOWED_PORTS:
            await self._deny(writer, 403, f"Port {port} ist nicht erlaubt")
            return
        ok, reason = self._authorise(headers, host)
        if not ok:
            await self._deny(writer, 403, reason)
            return

        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            await self._deny(writer, 502, f"Verbindung zu {host} fehlgeschlagen: {exc}")
            return

        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _handle_absolute(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        target: str,
        headers: dict[str, str],
        head: bytes,
    ) -> None:
        """Plain-HTTP proxying: forward the request verbatim to the origin."""
        if not target.lower().startswith("http://"):
            await self._deny(writer, 400, "Nur http:// und CONNECT werden unterstützt")
            return
        authority = target[7:].split("/", 1)[0]
        host, _, port_raw = authority.partition(":")
        port = int(port_raw) if port_raw.isdigit() else 80
        if port not in ALLOWED_PORTS:
            await self._deny(writer, 403, f"Port {port} ist nicht erlaubt")
            return
        ok, reason = self._authorise(headers, host)
        if not ok:
            await self._deny(writer, 403, reason)
            return

        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as exc:
            await self._deny(writer, 502, f"Verbindung zu {host} fehlgeschlagen: {exc}")
            return

        # Rewrite the absolute request line to origin form and drop the hop-by-hop
        # credentials so they are never forwarded to the target host.
        lines = head.decode("latin-1").split("\r\n")
        method, _, rest = lines[0].partition(" ")
        path = "/" + target[7:].split("/", 1)[1] if "/" in target[7:] else "/"
        version = rest.rsplit(" ", 1)[-1] or "HTTP/1.1"
        rebuilt = [f"{method} {path} {version}"]
        for line in lines[1:]:
            lowered = line.lower()
            if lowered.startswith(("proxy-authorization:", "proxy-connection:")):
                continue
            rebuilt.append(line)
        up_writer.write("\r\n".join(rebuilt).encode("latin-1"))
        await up_writer.drain()
        await self._pipe(reader, writer, up_reader, up_writer)

    async def _pipe(
        self,
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        up_reader: asyncio.StreamReader,
        up_writer: asyncio.StreamWriter,
    ) -> None:
        async def copy(src: asyncio.StreamReader, dst: asyncio.StreamWriter) -> None:
            transferred = 0
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    transferred += len(chunk)
                    if transferred > MAX_TRANSFER_BYTES:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (ConnectionError, asyncio.IncompleteReadError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        await asyncio.gather(
            copy(client_reader, up_writer),
            copy(up_reader, client_writer),
            return_exceptions=True,
        )
