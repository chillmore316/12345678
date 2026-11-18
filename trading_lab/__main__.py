from __future__ import annotations

import argparse
import threading
import webbrowser

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(description="Trading Lab 本地历史训练工作台")
    parser.add_argument("--host", default="127.0.0.1", choices=("127.0.0.1", "localhost", "::1"))
    parser.add_argument("--port", type=int, default=8090)
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port 须在 1 到 65535 之间")
    url_host = f"[{args.host}]" if args.host == "::1" else args.host
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open_new_tab(f"http://{url_host}:{args.port}/")).start()
    print(f"Trading Lab 仅监听本机： http://{url_host}:{args.port}/")
    uvicorn.run(create_app(), host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
