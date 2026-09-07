"""Entry point: python -m takcore

Configuration via environment variables (all optional):

    TAK_TCP_PORT   plain-TCP CoT port          (default 0, disabled; set to 8087 to enable plain-TCP for an isolated lab)
    TAK_TLS_PORT   TLS CoT port                (default 8089, 0 disables)
    HTTP_PORT      web UI / API port           (default 8080)
    DB_PATH        SQLite file                 (default ./takcore.db)
    WEB_DIR        static web UI directory     (default ../web relative to repo)
    CERT_FILE      server TLS certificate      (default certs/server.pem)
    KEY_FILE       server TLS private key      (default certs/server.key)
    CA_FILE        CA cert for client certs    (default certs/ca.pem)
    REQUIRE_CLIENT_CERT  "1" to enforce mTLS   (default on; "0" to allow optional certs)
    LOG_LEVEL      DEBUG/INFO/WARNING          (default INFO)
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys

from . import auth as authmod
from .auth import SessionManager
from .ca import CAError, CertificateAuthority
from .enrollment import EnrollmentService, start_enrollment_server
from .hub import Hub
from .mediamtx import MediaMTX, StreamRegistry
from .store import Store
from .taklistener import start_tak_listeners
from .webserver import start_web_server


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


async def main() -> None:
    logging.basicConfig(
        level=_env("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("takcore")

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    default_web = os.path.join(os.path.dirname(repo_root), "web")

    tcp_port = int(_env("TAK_TCP_PORT", "0"))
    tls_port = int(_env("TAK_TLS_PORT", "8089"))
    http_port = int(_env("HTTP_PORT", "8080"))
    db_path = _env("DB_PATH", "takcore.db")
    web_dir = _env("WEB_DIR", default_web)
    cert_file = _env("CERT_FILE", os.path.join("certs", "server.pem"))
    key_file = _env("KEY_FILE", os.path.join("certs", "server.key"))
    ca_file = _env("CA_FILE", os.path.join("certs", "ca.pem"))
    require_cc = _env("REQUIRE_CLIENT_CERT", "1") == "1"

    mtx = MediaMTX(_env("MEDIAMTX_API", "http://127.0.0.1:9997"))
    store = Store(db_path)
    stream_secret = os.environ.get(
        "STREAM_TOKEN_SECRET", secrets.token_hex(32)).encode()
    registry = StreamRegistry(store, mtx,
                              publish_token=os.environ.get("PUBLISH_TOKEN"),
                              token_secret=stream_secret)
    hub = Hub(store, registry)
    hub.loop = asyncio.get_running_loop()

    if mtx.reachable():
        n = registry.sync()
        log.info("MediaMTX reachable, %d proxy path(s) synced", n)
    else:
        log.info("MediaMTX not reachable at %s (video features idle until "
                 "it comes up)", mtx.api_url)

    certs_ok = os.path.isfile(cert_file) and os.path.isfile(key_file)
    servers = await start_tak_listeners(
        hub,
        tcp_port=tcp_port,
        tls_port=tls_port if certs_ok else 0,
        certfile=cert_file if certs_ok else None,
        keyfile=key_file if certs_ok else None,
        cafile=ca_file if os.path.isfile(ca_file) else None,
        require_client_cert=require_cc,
    )
    if tls_port and not certs_ok:
        log.warning("TLS disabled: %s / %s not found "
                    "(run scripts/make-certs.sh)", cert_file, key_file)

    # -- certificate enrollment (Phase 3) --
    enroll_port = int(_env("ENROLL_PORT", "8446"))
    certs_dir = os.path.dirname(cert_file) or "certs"
    ca = CertificateAuthority(certs_dir)
    enroll_svc = None
    enroll_httpd = None
    if enroll_port and certs_ok:
        try:
            ca.ensure()
            enroll_svc = EnrollmentService(store, ca)
            enroll_httpd = start_enrollment_server(
                enroll_svc, enroll_port, cert_file, key_file,
                cafile=ca.ca_cert)
        except CAError as e:
            log.error("enrollment disabled: %s", e)
    elif enroll_port:
        log.warning("enrollment disabled: no TLS certs "
                    "(run scripts/make-certs.sh)")

    # -- web auth (Phase 4) --
    auth_enabled = _env("AUTH_ENABLED", "1") == "1"
    sessions = SessionManager()
    login_throttle = authmod.LoginThrottle()
    if auth_enabled:
        admin_user, admin_pw = authmod.default_admin()
        if authmod.must_refuse_start(auth_enabled, admin_pw,
                                     store.user_count()):
            log.error("AUTH_ENABLED=1 but no users exist and ADMIN_PASSWORD is "
                      "unset. Refusing to start with a default credential. Set "
                      "ADMIN_PASSWORD (see .env.example) and restart.")
            store.close()
            sys.exit(1)
        if admin_pw:
            store.upsert_user(admin_user, authmod.hash_password(admin_pw),
                              "admin")
            log.info("admin user '%s' password set from ADMIN_PASSWORD",
                     admin_user)
        log.info("web auth ENABLED (roles: viewer/operator/admin)")
    else:
        log.warning("web auth DISABLED - the UI/API are open on this network")

    httpd = start_web_server(hub, store, http_port, web_dir, registry,
                             enroll_svc, sessions, auth_enabled, login_throttle)
    log.info("takcore up - TAK devices connect on tcp:%s%s, browser at "
             "http://localhost:%d", tcp_port,
             f" / tls:{tls_port}" if certs_ok and tls_port else "",
             http_port)

    retention_days = float(_env("POSITION_RETENTION_DAYS", "7"))

    async def _prune_loop() -> None:
        while True:
            await asyncio.sleep(6 * 3600)  # every 6 hours
            try:
                n = await asyncio.get_running_loop().run_in_executor(
                    None, store.prune_positions, retention_days)
                if n:
                    log.info("pruned %d position rows older than %.0f days",
                             n, retention_days)
            except Exception:  # noqa: BLE001
                log.exception("position prune failed")

    prune_task = (asyncio.create_task(_prune_loop())
                  if retention_days > 0 else None)

    stop = asyncio.Event()

    def _shutdown(*_args) -> None:
        stop.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _shutdown)
        except NotImplementedError:  # pragma: no cover (Windows)
            signal.signal(sig, _shutdown)

    await stop.wait()
    log.info("shutting down")
    for srv in servers:
        srv.close()
        await srv.wait_closed()
    if prune_task is not None:
        prune_task.cancel()
    httpd.shutdown()
    if enroll_httpd:
        enroll_httpd.shutdown()
    store.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
