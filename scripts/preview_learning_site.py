#!/usr/bin/env python3
"""Build and serve one fresh local preview of the learning site."""

from __future__ import annotations

import argparse
import functools
import http.server
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


DEFAULT_PORT = 8000


class PreviewError(RuntimeError):
    """A local preview step failed."""


def project_root(path: Path) -> Path:
    root = path.expanduser().resolve()
    if not root.is_dir():
        raise PreviewError(f"project folder does not exist: {root}")
    if not (root / "pyproject.toml").is_file() or not (root / "docs" / "conf.py").is_file():
        raise PreviewError(
            "run this command from a learning project that contains "
            "pyproject.toml and docs/conf.py"
        )
    return root


def clean_build_directory(root: Path) -> Path:
    docs = (root / "docs").resolve()
    build = (docs / "_build").resolve()
    if build != docs / "_build":
        raise PreviewError("refusing to remove an unexpected build directory")
    if build.exists():
        shutil.rmtree(build)
    return build


def build_site(root: Path) -> Path:
    build = clean_build_directory(root)
    output = build / "html"
    command = [
        sys.executable,
        "-m",
        "sphinx",
        "-b",
        "html",
        "docs",
        str(output),
    ]
    print("Preparing a fresh local preview...", flush=True)
    result = subprocess.run(command, cwd=root, check=False)
    if result.returncode:
        raise PreviewError(
            f"the site build failed with exit code {result.returncode}; "
            "review the message above"
        )
    if not (output / "index.html").is_file():
        raise PreviewError("the build finished without creating docs/_build/html/index.html")
    return output


def create_server(
    output: Path,
    port: int,
) -> http.server.ThreadingHTTPServer:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler,
        directory=str(output),
    )
    try:
        return http.server.ThreadingHTTPServer(("127.0.0.1", port), handler)
    except OSError:
        if port == 0:
            raise
        print(f"Port {port} is already in use; choosing another local port.")
        return http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)


def execute(args: argparse.Namespace) -> int:
    root = project_root(Path(args.repo))
    output = build_site(root)
    if args.build_only:
        print(f"READY Fresh preview files: {output}")
        return 0

    server = create_server(output, args.port)
    port = server.server_address[1]
    url = f"http://127.0.0.1:{port}"
    print(f"\nREADY Preview: {url}")
    print("Press Ctrl+C to stop.")
    if not args.no_open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nPreview stopped.")
    finally:
        server.server_close()
    return 0


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="Build and open one fresh local preview of the learning site."
    )
    result.add_argument(
        "--repo",
        default=".",
        help="learning project folder (default: current folder)",
    )
    result.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"preferred local port (default: {DEFAULT_PORT})",
    )
    result.add_argument(
        "--no-open",
        action="store_true",
        help="print the preview URL without opening a browser",
    )
    result.add_argument(
        "--build-only",
        action="store_true",
        help="create fresh preview files without starting a server",
    )
    return result


def main() -> int:
    try:
        return execute(parser().parse_args())
    except (PreviewError, OSError) as exc:
        print(f"PREVIEW STOPPED: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
