"""Asyncio TCP/TLS listener speaking the CoT streaming protocol."""

from __future__ import annotations

import asyncio
import logging
import ssl
from typing import Optional

from .cot import CotStreamParser, build_pong, parse_event
from .hub import Hub, TakSession

log = logging.getLogger("takcore.tak")


def cn_from_peercert(cert: Optional[dict]) -> Optional[str]:
    """Pull the commonName out of a stdlib getpeercert() dict, or None."""
    if not cert:
        return None
    for rdn in cert.get("subject", ()):
        for key, value in rdn:
            if key == "commonName":
                return value
    return None


async def _handle(hub: Hub, reader: asyncio.StreamReader,
                  writer: asyncio.StreamWriter) -> None:
    peername = writer.get_extra_info("peername")
    peer = f"{peername[0]}:{peername[1]}" if peername else "?"
    session = TakSession(writer, peer)
    ssl_object = writer.get_extra_info("ssl_object")
    if ssl_object is not None:
        session.cn = cn_from_peercert(ssl_object.getpeercert())
    hub.register(session)
    parser = CotStreamParser()
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            for raw in parser.feed(data):
                ev = parse_event(raw)
                if ev is None:
                    continue
                if ev.is_ping:
                    session.send(build_pong())
                    continue
                hub.publish(ev, session)
            await writer.drain()
    except (ConnectionResetError, asyncio.IncompleteReadError, TimeoutError):
        pass
    except Exception:  # noqa: BLE001
        log.exception("session %d error", session.id)
    finally:
        hub.unregister(session)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:  # noqa: BLE001
            pass


def _tls_context(certfile: str, keyfile: str, cafile: Optional[str],
                 require_client_cert: bool) -> ssl.SSLContext:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    # Cap at TLS 1.2: the TAK ecosystem (TAK Server, ATAK/iTAK) standardises on
    # 1.2, and some ATAK builds fail the handshake against a TLS 1.3-only server.
    ctx.maximum_version = ssl.TLSVersion.TLSv1_2
    # Present leaf + CA so clients that don't have the CA cached can still
    # build the trust path (matches the enrollment listener).
    chain = certfile
    if cafile:
        from .enrollment import build_chain_file
        chain = build_chain_file(certfile, cafile)
    ctx.load_cert_chain(chain, keyfile)
    if cafile:
        ctx.load_verify_locations(cafile)
    ctx.verify_mode = (
        ssl.CERT_REQUIRED if require_client_cert else ssl.CERT_NONE
    )
    return ctx


async def start_tak_listeners(hub: Hub, tcp_port: int, tls_port: int,
                              certfile: Optional[str], keyfile: Optional[str],
                              cafile: Optional[str],
                              require_client_cert: bool) -> list:
    """Start the plain-TCP listener and, when certs exist, the TLS listener."""
    servers = []

    async def handler(r: asyncio.StreamReader, w: asyncio.StreamWriter) -> None:
        await _handle(hub, r, w)

    if tcp_port:
        srv = await asyncio.start_server(handler, host="0.0.0.0", port=tcp_port)
        servers.append(srv)
        log.info("CoT TCP listener on :%d", tcp_port)

    if tls_port and certfile and keyfile:
        ctx = _tls_context(certfile, keyfile, cafile, require_client_cert)

        # Accept plain, then upgrade to TLS manually so the handshake result
        # (success OR failure) is always logged. asyncio's start_server(ssl=)
        # silently swallows handshake failures, hiding client trust/cert/cipher
        # problems — which made ATAK's SSL failures impossible to diagnose.
        async def tls_handler(r: asyncio.StreamReader,
                              w: asyncio.StreamWriter) -> None:
            peername = w.get_extra_info("peername")
            peer = f"{peername[0]}:{peername[1]}" if peername else "?"
            loop = asyncio.get_running_loop()
            transport = w.transport
            protocol = transport.get_protocol()
            try:
                tls_transport = await loop.start_tls(
                    transport, protocol, ctx, server_side=True,
                    ssl_handshake_timeout=15)
            except Exception as e:  # noqa: BLE001
                log.warning("CoT TLS(%d) handshake FAILED from %s: %s: %s",
                            tls_port, peer, type(e).__name__, e)
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
                return
            w._transport = tls_transport  # route writes over the encrypted link
            log.info("CoT TLS(%d) handshake OK from %s", tls_port, peer)
            await _handle(hub, r, w)

        srv = await asyncio.start_server(tls_handler, host="0.0.0.0",
                                         port=tls_port)
        servers.append(srv)
        log.info("CoT TLS listener on :%d (client certs %s)", tls_port,
                 "required" if require_client_cert else "optional")
    elif tls_port:
        log.warning("TLS port %d configured but no certificates found - "
                    "TLS listener disabled (run scripts/make-certs.sh)", tls_port)

    return servers
