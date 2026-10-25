"""python -m pricecomparison [serve | demo | passwd]"""

from __future__ import annotations

import argparse
import getpass
import logging
import os
import socket
from pathlib import Path


def main() -> None:
    ap = argparse.ArgumentParser(prog="pricecomparison")
    ap.add_argument("--data", default=os.environ.get("PC_DATA_DIR", "data"), help="data directory")
    ap.add_argument("--log-file", default=os.environ.get("PC_LOG_FILE", ""),
                    help="write the log to this file (for running as a Windows scheduled task, which has no console)")
    sub = ap.add_subparsers(dest="cmd")
    sv = sub.add_parser("serve", help="run the panel (default)")
    sv.add_argument("--host", default=os.environ.get("PC_HOST", "0.0.0.0"))
    sv.add_argument("--port", type=int, default=int(os.environ.get("PC_PORT", "8090")))
    sv.add_argument("--trusted-proxy", default=os.environ.get("PC_TRUSTED_PROXY", "127.0.0.1"),
                    help="IP of the reverse proxy in front of the panel (comma separated); its X-Forwarded-Proto "
                         "is believed, so the login cookie is marked Secure behind HTTPS")
    dm = sub.add_parser("demo", help="run with sample products and simulated shops")
    dm.add_argument("--host", default="127.0.0.1")
    dm.add_argument("--port", type=int, default=8090)
    pw = sub.add_parser("passwd", help="create an account or set its password (an admin, unless --role user)")
    pw.add_argument("--username", default="admin")
    pw.add_argument("--password", help="non-interactive (for install scripts)")
    pw.add_argument("--role", choices=["admin", "user"], default="admin")
    args = ap.parse_args()

    if args.cmd == "passwd":
        from .auth import Auth

        a = Auth(Path(args.data) / "auth.json")
        if args.password:
            p1 = args.password
            if len(p1) < 6:
                raise SystemExit("password must be at least 6 characters")
        else:
            p1 = getpass.getpass("New password: ")
            if len(p1) < 6 or p1 != getpass.getpass("Repeat: "):
                raise SystemExit("passwords differ or shorter than 6 characters")
        a.set_password(args.username, p1, args.role)
        print("password updated")
        return

    import uvicorn

    from .app import create_app

    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    if args.log_file:
        from logging.handlers import RotatingFileHandler

        Path(args.log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(args.log_file, maxBytes=5_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter(fmt))
        logging.basicConfig(level=logging.INFO, handlers=[handler])
    else:
        logging.basicConfig(level=logging.INFO, format=fmt)
    log = logging.getLogger("pricecomparison")
    host = getattr(args, "host", os.environ.get("PC_HOST", "0.0.0.0"))
    port = getattr(args, "port", int(os.environ.get("PC_PORT", "8090")))
    probe = socket.socket()
    try:
        # SO_REUSEADDR lets Windows bind a port another program is listening
        # on, which would hide exactly the clash this check is for
        if os.name != "nt":
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        probe.bind((host, port))
    except OSError:
        log.error("port %s is already in use on %s", port, host)
        raise SystemExit(
            f"port {port} is already in use on {host} - pick another one with --port\n"
            f"پورت {port} اشغال است؛ با --port یک پورت دیگر بدهید"
        ) from None
    finally:
        probe.close()
    log.info("serving on %s:%s, data in %s", host, port, os.path.abspath(args.data))

    demo = args.cmd == "demo"
    data = args.data if not demo else os.path.join(args.data, "demo")
    tls = {}
    if os.environ.get("PC_SSL_CERT") and os.environ.get("PC_SSL_KEY"):
        tls = {"ssl_certfile": os.environ["PC_SSL_CERT"], "ssl_keyfile": os.environ["PC_SSL_KEY"]}
    tls["forwarded_allow_ips"] = getattr(args, "trusted_proxy", "127.0.0.1")
    if args.log_file:
        # let uvicorn's own messages reach the log file instead of a console that is not there
        for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
            logging.getLogger(name).setLevel(logging.WARNING)
        uvicorn.run(create_app(data, demo=demo), host=host, port=port, proxy_headers=True, log_config=None, **tls)
    else:
        uvicorn.run(create_app(data, demo=demo), host=host, port=port, proxy_headers=True, log_level="warning", **tls)


if __name__ == "__main__":
    main()
