"""Command line entry point: `cas serve | worker | migrate | bootstrap | demo`."""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cas", description="Coordinated Agent Systems reference implementation")
    sub = parser.add_subparsers(dest="cmd", required=True)
    serve = sub.add_parser("serve", help="run the API and workspace UI")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--no-worker", action="store_true", help="do not run the worker loop inside this process")
    sub.add_parser("worker", help="run the scheduler, reconciler, validation and outbox workers")
    sub.add_parser("migrate", help="apply database migrations")
    boot = sub.add_parser("bootstrap", help="create a tenant and its first admin; prints the admin credential once")
    boot.add_argument("--tenant", required=True)
    boot.add_argument("--name", default=None)
    boot.add_argument("--admin-name", default="Administrator")
    demo = sub.add_parser("demo", help="create a demo tenant and run the three-agent reference scenario")
    demo.add_argument("--tenant", default=None)
    args = parser.parse_args(argv)
    logging.basicConfig(level=os.environ.get("CAS_LOG_LEVEL", "INFO"), format="%(asctime)s %(name)s %(levelname)s %(message)s")

    from cas.persistence import db

    if args.cmd == "serve":
        import uvicorn
        os.environ["CAS_EMBEDDED_WORKER"] = "0" if args.no_worker else "1"
        uvicorn.run("cas.api.app:app", host=args.host, port=args.port, log_level="info")
        return 0
    if args.cmd == "migrate":
        print("applied:", ", ".join(db.migrate()) or "nothing to apply")
        return 0
    if args.cmd == "worker":
        from cas.workers.runner import run_forever
        db.migrate()
        run_forever()
        return 0
    if args.cmd == "bootstrap":
        from cas.service import principals
        db.migrate()
        with db.transaction() as tx:
            out = principals.bootstrap_tenant(tx, args.tenant, args.name or args.tenant, args.admin_name)
        print(json.dumps({"tenant_id": out["tenant_id"], "admin_id": out["admin"]["id"],
                          "admin_credential": out["token"]}, indent=2))
        print("Store this credential now; it is not shown again.", file=sys.stderr)
        return 0
    if args.cmd == "demo":
        from cas.demo import run_demo
        db.migrate()
        run_demo(args.tenant)
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
