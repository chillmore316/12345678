"""Real cold starts on temporary loopback ports; browser opening is intercepted."""
import os
from pathlib import Path
import signal
import socket
import subprocess
import sys
import time
import unittest
from research_center.launcher import probe

ROOT=Path(__file__).resolve().parents[1]
SCRIPT="""
from unittest.mock import patch
from research_center.launcher import main
with patch('research_center.launcher.webbrowser.open', return_value=True):
    raise SystemExit(main())
"""


class ResearchLauncherLifecycleTests(unittest.TestCase):
    def port(self):
        with socket.socket() as handle:
            handle.bind(('127.0.0.1',0))
            return handle.getsockname()[1]

    def start(self, port):
        owner=subprocess.Popen([sys.executable,'-c',SCRIPT,'--port',str(port)],cwd=ROOT,
                               stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
        self.addCleanup(self.cleanup,owner)
        limit=time.monotonic()+15
        while time.monotonic()<limit:
            if probe(port):return owner
            if owner.poll() is not None:
                self.fail('launcher exited during cold start: '+owner.communicate()[0])
            time.sleep(.05)
        self.fail('cold-start deadline expired')

    @staticmethod
    def cleanup(owner):
        try:os.killpg(owner.pid,signal.SIGTERM)
        except ProcessLookupError:pass
        try:owner.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(owner.pid,signal.SIGKILL);owner.communicate(timeout=5)

    def stopped(self,port):
        limit=time.monotonic()+6
        while time.monotonic()<limit:
            if not probe(port):return True
            time.sleep(.05)
        return False

    def test_cold_start_repeat_open_ctrl_c_and_restart(self):
        port=self.port();owner=self.start(port)
        second=subprocess.run([sys.executable,'-c',SCRIPT,'--port',str(port)],cwd=ROOT,
                              capture_output=True,text=True,timeout=10)
        self.assertEqual(second.returncode,0,second.stdout+second.stderr)
        self.assertIsNone(owner.poll());self.assertTrue(probe(port))
        owner.send_signal(signal.SIGINT);owner.communicate(timeout=10)
        self.assertEqual(owner.returncode,0)
        self.assertTrue(self.stopped(port),'owner exited but its server kept listening')
        restarted=self.start(port)
        restarted.send_signal(signal.SIGINT);restarted.communicate(timeout=10)
        self.assertEqual(restarted.returncode,0);self.assertTrue(self.stopped(port))

    @unittest.skipUnless(hasattr(signal,'SIGHUP'),'requires POSIX signals')
    def test_termination_and_terminal_hangup_close_owned_server(self):
        for signum in (signal.SIGTERM,signal.SIGHUP):
            with self.subTest(signal=signum):
                port=self.port();owner=self.start(port)
                owner.send_signal(signum);owner.communicate(timeout=10)
                self.assertTrue(self.stopped(port),'launcher termination left an orphan server')
                self.assertEqual(owner.returncode,0)

    def test_unknown_active_listener_is_not_reused_or_stopped(self):
        import http.server,threading,urllib.request
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                data=b'other-local-service'
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def log_message(self,*args):pass
        for address in ('127.0.0.1','0.0.0.0'):
            with self.subTest(address=address):
                # No SO_REUSEADDR fixture: reproduces Darwin wildcard bind.
                class ExclusiveServer(http.server.ThreadingHTTPServer):
                    allow_reuse_address=False
                server=ExclusiveServer((address,0),Handler)
                thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                try:
                    port=server.server_port
                    result=subprocess.run([sys.executable,'-c',SCRIPT,'--port',str(port)],cwd=ROOT,capture_output=True,text=True,timeout=10)
                    self.assertEqual(result.returncode,1)
                    self.assertIn('占用',result.stderr)
                    opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
                    with opener.open(f'http://127.0.0.1:{port}/',timeout=2) as response:
                        self.assertEqual(response.read(),b'other-local-service')
                finally:
                    server.shutdown();server.server_close();thread.join()

    def test_signals_during_process_creation_leave_no_service(self):
        injected = """
import os, signal, subprocess
from unittest.mock import patch
from research_center.launcher import main
real_popen = subprocess.Popen
def spawn_then_signal(*args, **kwargs):
    child = real_popen(*args, **kwargs)
    os.kill(os.getpid(), SIGNAL)
    return child
with patch('research_center.launcher.subprocess.Popen', side_effect=spawn_then_signal):
    raise SystemExit(main())
"""
        for signum in (signal.SIGINT,signal.SIGTERM,signal.SIGHUP):
            with self.subTest(signal=signum):
                port=self.port()
                owner=subprocess.Popen([sys.executable,'-c',injected.replace('SIGNAL',str(int(signum))),
                                        '--port',str(port)],cwd=ROOT,stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT,text=True,start_new_session=True)
                self.addCleanup(self.cleanup,owner)
                output=owner.communicate(timeout=15)[0]
                self.assertEqual(owner.returncode,0,output)
                self.assertTrue(self.stopped(port))
                restarted=self.start(port)
                restarted.send_signal(signal.SIGINT);restarted.communicate(timeout=10)
                self.assertTrue(self.stopped(port))
