"""One local entry point; reuses only a verified research service."""
from __future__ import annotations
import argparse
import errno
import json
from pathlib import Path
import socket
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from .health import SERVICE_ID
from .store import ROOT


def probe(port=8060):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(f'http://127.0.0.1:{port}/api/research/health', timeout=2) as response:
            data = json.loads(response.read(65536))
        return isinstance(data, dict) and data.get('service') == SERVICE_ID and data.get('mode') == 'RESEARCH_ONLY'
    except (OSError, ValueError):
        return False


def port_free(port):
    # On Darwin SO_REUSEADDR permits a specific bind beside a wildcard
    # listener. A TCP handshake detects that listener before the TIME_WAIT
    # reuse check; only an explicit refusal is evidence of no listener.
    with socket.socket() as listener:
        listener.settimeout(.3)
        try:
            connected = listener.connect_ex(('127.0.0.1', port))
        except OSError:
            return False
        if connected != errno.ECONNREFUSED:
            return False
    with socket.socket() as handle:
        # Match HTTPServer's reuse policy: TIME_WAIT is not an active listener.
        handle.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            handle.bind(('127.0.0.1', port))
            return True
        except OSError:
            return False


def launch(port=8060, open_page=True, stop_requested=lambda: False):
    url = f'http://127.0.0.1:{port}/research'
    if probe(port):
        if open_page:
            webbrowser.open(url)
        print('已连接本机研究中心：' + url, flush=True)
        return 0
    if not port_free(port):
        raise RuntimeError(f'端口 {port} 已被其他程序占用；请关闭占用程序或选择其他端口。不会终止未知进程。')
    child = subprocess.Popen([sys.executable, '-m', 'research_center.cli', 'serve', '--port', str(port)], cwd=ROOT)
    try:
        for _ in range(40):
            if stop_requested():
                raise KeyboardInterrupt
            if probe(port):
                if open_page:
                    webbrowser.open(url)
                print('研究中心已启动；保留此窗口，Ctrl+C 停止服务。', flush=True)
                while child.poll() is None:
                    if stop_requested():
                        raise KeyboardInterrupt
                    try:
                        child.wait(timeout=.2)
                    except subprocess.TimeoutExpired:
                        pass
                return child.returncode
            if child.poll() is not None:
                raise RuntimeError('研究服务未能启动，请查看上方错误。')
            time.sleep(.2)
        raise RuntimeError('研究服务启动超时，请查看本地错误后重试。')
    finally:
        if child.poll() is None:
            child.terminate()
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


def main():
    parser = argparse.ArgumentParser(description='启动本地中文量化工作台')
    parser.add_argument('--port', type=int, default=8060)
    parser.add_argument('--check', action='store_true', help='仅检查已运行的工作台，不启动或打开网页')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('端口须在1至65535之间')
    if args.check:
        available = probe(args.port)
        print(json.dumps({'service': SERVICE_ID, 'reachable': available, 'port': args.port}, ensure_ascii=False))
        return 0 if available else 1
    stopping = False
    def stop_owned_server(_signum, _frame):
        # Do not interrupt Popen before its return value is assigned. The
        # owned child is cleaned up after creation even if a signal arrives
        # inside that call; repeated signals cannot interrupt the cleanup.
        nonlocal stopping
        stopping = True
    signals = [signal.SIGTERM, signal.SIGINT]
    if hasattr(signal, 'SIGHUP'):
        signals.append(signal.SIGHUP)
    previous = {signum: signal.getsignal(signum) for signum in signals}
    try:
        for signum in signals:
            signal.signal(signum, stop_owned_server)
        try:
            return launch(args.port, stop_requested=lambda: stopping)
        except KeyboardInterrupt:
            print('\n启动窗口已关闭；历史记录保留。')
            return 0
        except (OSError, RuntimeError) as exc:
            print(str(exc), file=sys.stderr)
            return 1
    finally:
        for signum, handler in previous.items():
            signal.signal(signum, handler)


if __name__ == '__main__':
    raise SystemExit(main())
