"""Real HTTP/worker integration in a copied code tree with synthetic prices.

The copy has no production evidence caches. A source-versioned sleep in its
kernel makes controller restart and cancellation timing deterministic; no test
knob or arbitrary worker command is added to the production API.
"""
from contextlib import closing
from datetime import datetime
import http.client
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid

import pandas as pd

from research_center.store import ROOT


class PortfolioWorkflowExecutionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.code = self.root / "code"
        for package in ("research_center", "strategy"):
            destination = self.code / package
            destination.mkdir(parents=True)
            for source in (ROOT / package).glob("*.py"):
                shutil.copyfile(source, destination / source.name)
        (self.code / "data").mkdir()
        for name in ("__init__.py", "joinquant_minute.py"):
            shutil.copyfile(ROOT / "data" / name, self.code / "data" / name)
        kernel = self.code / "research_center/portfolio_kernel.py"
        source = kernel.read_text()
        needle = '    canonical = _canonical_frames(normalized, frames, state["last_source_datetime"])'
        self.assertEqual(source.count(needle), 1)
        kernel.write_text(source.replace(needle, '    import time as _fixture_clock\n    _fixture_clock.sleep(2)\n' + needle))
        data = self.code / "闲鱼商品_A股1分钟数据_聚宽版/01_数据文件"
        data.mkdir(parents=True)
        rows = []
        for symbol, close in (("000001.XSHE", 10.0), ("000002.XSHE", 11.0)):
            for stamp in pd.date_range("2024-01-04 09:31", periods=3, freq="min"):
                rows.append(dict(time=stamp, code=symbol, open=close, high=close + .1, low=close - .1,
                                 close=close, volume=100000., money=close * 100000.))
        pd.DataFrame(rows).to_parquet(data / "stock_1m_2024-01_batch000.parquet", index=False)
        self.store = self.root / "state/research"
        self.server = None
        self.addCleanup(self.stop_server)
        self.start_server()
        self.addCleanup(self.cancel_all_active)

    def start_server(self):
        port_file = self.root / ("port-" + uuid.uuid4().hex)
        bootstrap = ("import sys;sys.path.insert(0,sys.argv[1]);"
            "from pathlib import Path;from research_center.store import RunStore;"
            "from research_center.http import make_server;"
            "server=make_server(RunStore(sys.argv[2]),0);"
            "Path(sys.argv[3]).write_text(str(server.server_port));server.serve_forever()")
        self.server = subprocess.Popen([sys.executable, "-I", "-c", bootstrap, str(self.code), str(self.store), str(port_file)],
            cwd=self.code, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            close_fds=True, start_new_session=True)
        deadline = time.monotonic() + 8
        while not port_file.exists() and self.server.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        self.assertTrue(port_file.exists(), "isolated HTTP service did not start")
        self.port = int(port_file.read_text())

    def stop_server(self):
        if self.server is not None and self.server.poll() is None:
            self.server.terminate()
            try:
                self.server.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.server.kill()
                self.server.wait(timeout=3)

    def request(self, method, path, value=None):
        with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)) as connection:
            connection.request(method, path, None if value is None else json.dumps(value), {"Content-Type": "application/json"})
            response = connection.getresponse()
            content = response.read()
            self.assertIn(response.status, (200, 202), (response.status, content[:200]))
            return json.loads(content)

    def submit(self):
        self.request_id = uuid.uuid4().hex
        self.spec = {"config": {"symbols": ["000001.SZSE", "000002.SZSE"], "start": "2024-01-04", "end": "2024-01-04"}}
        plan = self.request("POST", "/api/research/workflows/preview", {"kind": "portfolio", "spec": self.spec})
        self.spec["expected_source_version"] = plan["source_version"]
        payload = {"kind": "portfolio", "spec": self.spec, "request_id": self.request_id}
        job = self.request("POST", "/api/research/workflows", payload)
        self.job_id = job["job_id"]
        self.addCleanup(self.cancel_remaining)
        self.payload = payload
        return job

    def read_job(self):
        return self.request("GET", "/api/research/workflows/" + self.job_id)

    def wait_for(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            job = self.read_job()
            if predicate(job):
                return job
            time.sleep(.03)
        self.fail("isolated job did not reach expected state")

    def cancel_remaining(self):
        # Cancellation goes through the owning supervisor. The copied worker
        # also has its independent OS deadline if this cleanup cannot connect.
        if self.server is not None and self.server.poll() is None:
            try:
                job = self.read_job()
                if job["status"] in {"queued", "running"}:
                    self.request("POST", "/api/research/workflows/" + self.job_id + "/cancel", {})
                    self.wait_for(lambda item: item["status"] not in {"queued", "running"})
            except (OSError, AssertionError):
                pass

    def cancel_all_active(self):
        if self.server is None or self.server.poll() is not None:
            return
        try:
            for job in self.request("GET", "/api/research/workflows")["jobs"]:
                if job["kind"] == "portfolio" and job["status"] in {"queued", "running"}:
                    self.job_id = job["job_id"]
                    self.cancel_remaining()
        except (OSError, AssertionError):
            pass

    def test_http_restart_preserves_worker_and_retry_identity_then_verifies_report(self):
        self.submit()
        self.wait_for(lambda item: item["status"] == "running")
        stopped_at = time.time()
        self.stop_server()
        self.start_server()
        final = self.wait_for(lambda item: item["status"] not in {"queued", "running"})
        self.assertEqual(final["status"], "completed", final.get("error"))
        self.assertGreater(datetime.fromisoformat(final["finished_at"]).timestamp(), stopped_at)
        retry = self.request("POST", "/api/research/workflows", self.payload)
        self.assertEqual(retry["job_id"], self.job_id)
        self.assertEqual(retry["status"], "completed")
        report = self.request("GET", "/api/research/portfolios/" + self.job_id)
        self.assertEqual(report["receipt"]["artifact_integrity"], "verified")
        self.assertEqual(report["receipt"]["source_snapshot_status"], "archived_and_verified")
        self.assertEqual(len(report["result"]["equity"]), 4)
        self.assertFalse(report["result"]["paper_admission"])
        self.assertEqual(report["result"]["data_readiness"]["status"], "unverified")
        self.assertEqual(len(self.request("GET", "/api/research/workflows")["jobs"]), 1)

    def test_running_cancellation_is_confirmed_and_no_financial_report_is_published(self):
        self.submit()
        self.wait_for(lambda item: item["status"] == "running")
        self.request("POST", "/api/research/workflows/" + self.job_id + "/cancel", {})
        final = self.wait_for(lambda item: item["status"] not in {"queued", "running"})
        self.assertEqual(final["status"], "cancelled", final.get("error"))
        self.assertTrue(final["receipt"]["execution"]["cleanup_confirmed"])
        report = self.request("GET", "/api/research/portfolios/" + self.job_id)
        self.assertEqual(report["receipt"]["status"], "cancelled")
        self.assertIsNone(report["result"])
        self.assertFalse((self.store.parent / "research_portfolios" / self.job_id / "result.json").exists())

    @unittest.skipUnless(shutil.which("node"), "Node is required for the real-API mock-DOM integration")
    def test_submission_page_script_uses_actual_preview_job_and_report_api(self):
        prefix = (ROOT / "tests/test_portfolio_submit.cjs").read_text().split("(async () => {", 1)[0]
        driver = r'''
(async()=>{
  const base=process.argv[2];
  const page=makePage((url,options)=>fetch(base+url,options));
  page.ids['portfolio-start'].value='2024-01-04';
  page.ids['portfolio-end'].value='2024-01-04';
  await page.ids['portfolio-submit-form'].fire('submit');
  const deadline=Date.now()+12000;
  while(page.ids['portfolio-preview-panel'].hidden&&Date.now()<deadline)await new Promise(resolve=>setTimeout(resolve,20));
  assert.equal(page.ids['portfolio-preview-panel'].hidden,false);
  await page.ids['portfolio-confirm-button'].fire('click');
  let saved;
  while(Date.now()<deadline){
    saved=JSON.parse(page.storage.get('quant-portfolio-pending-v1')||'{}');
    if(saved.job_id)break;
    await new Promise(resolve=>setTimeout(resolve,20));
  }
  assert.match(saved.job_id,/^[a-f0-9]{32}$/);
  while(!page.ids['portfolio-job-cases'].textContent.includes('打开已完成报告')&&Date.now()<deadline){
    if(!page.ids['portfolio-refresh-job'].disabled)await page.ids['portfolio-refresh-job'].fire('click');
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  assert.match(page.ids['portfolio-job-cases'].textContent,/打开已完成报告/);
  const report=await (await fetch(base+'/api/research/portfolios/'+saved.job_id)).json();
  assert.equal(report.receipt.artifact_integrity,'verified');
  assert.equal(report.receipt.source_snapshot_status,'archived_and_verified');
  assert.equal(report.result.paper_admission,false);
  console.log(JSON.stringify({scope:'actual HTTP and supervised workers with synthetic prices and mock DOM',job_id:saved.job_id,status:'completed',frames:report.receipt.frames,paper_admission:false}));
})().catch(error=>{console.error(error.stack);process.exitCode=1;});
'''
        path = self.root / "actual-submit.cjs"
        path.write_text(prefix + driver)
        completed = subprocess.run([shutil.which("node"), str(path), "http://127.0.0.1:" + str(self.port)],
                                   cwd=ROOT, capture_output=True, text=True, timeout=18)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        proof = json.loads(completed.stdout.strip().splitlines()[-1])
        self.assertEqual(proof["frames"], 3)
        self.assertFalse(proof["paper_admission"])


if __name__ == "__main__":
    unittest.main()
