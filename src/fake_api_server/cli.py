"""Console entry point. Listens on PORT, or 3000 when PORT is unset."""

import argparse
import os

import uvicorn

from fake_api_server.app import create_app


def resolve_port(argv: list[str] | None = None, environ: dict[str, str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="fake-api-server")
    parser.add_argument(
        "--port",
        type=int,
        default=None,
        help="TCP port. Overrides PORT. Default: PORT or 3000.",
    )
    args = parser.parse_args(argv)
    if args.port is not None:
        return args.port
    env = os.environ if environ is None else environ
    raw = env.get("PORT", "3000")
    return int(raw)


def main(argv: list[str] | None = None) -> None:
    port = resolve_port(argv)
    app = create_app()
    print(f"Listening at http://127.0.0.1:{port}", flush=True)
    uvicorn.run(app, host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
