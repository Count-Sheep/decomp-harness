import json
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest

import m2c_remote as remote
import m2c_remote_worker as worker_module


class RemoteM2CTests(unittest.TestCase):
    def tearDown(self):
        remote.configure(None)

    def test_executor_specs_are_weighted_and_strict(self):
        specs = remote.configure(["local=2", "http://127.0.0.1:8766=5"])
        self.assertEqual(remote.slots(), 7)
        self.assertEqual(specs[1]["slots"], 5)
        with self.assertRaisesRegex(ValueError, "Executor"):
            remote.parse_specs(["worker"])

    def test_remote_worker_verifies_tool_and_job_and_caches_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            tool = root / "m2c"
            (tool / "m2c").mkdir(parents=True)
            (tool / "m2c_pycparser").mkdir()
            (tool / "m2c/options.py").write_text("VALUE = 1\n")
            (tool / "m2c_pycparser/parser.py").write_text("VALUE = 1\n")
            (tool / "m2c.py").write_text(
                "import sys\nprint('int f(void) { return 1; }')\n")
            subprocess.run(["git", "init", "-q"], cwd=tool, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tool, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tool, check=True)
            subprocess.run(["git", "add", "."], cwd=tool, check=True)
            subprocess.run(["git", "commit", "-qm", "test"], cwd=tool, check=True)
            worker = worker_module.Worker(tool, root / "cache")
            server = worker_module.ThreadingHTTPServer(("127.0.0.1", 0), worker_module.Handler)
            server.worker = worker
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            url = f"http://127.0.0.1:{server.server_port}"
            remote.configure([f"{url}=2"])
            health = remote.verify_executors(worker.fingerprint)
            self.assertEqual(health[0]["worker"], worker.identity)
            with self.assertRaisesRegex(RuntimeError, "wrong tool fingerprint"):
                remote.verify_executors("not-the-installed-tool")
            payload = remote.job_payload("f", b".fn f, global\nblr\n.endfn f\n", "int f(void);",
                {"typed": True, "infer_unknowns": True, "explicit_fields": True,
                 "deterministic_vars": True, "declaration_mode": "used-context"},
                worker.fingerprint, 10)
            first = remote.remote_execute(url, payload, 10)
            second = remote.remote_execute(url, payload, 10)
            self.assertEqual(first["stdout"].strip(), "int f(void) { return 1; }")
            self.assertFalse(first["cache_hit"])
            self.assertTrue(second["cache_hit"])
            self.assertEqual(second["cache_origin_worker"], worker.identity)
            self.assertEqual(first["job_sha256"], payload["job_sha256"])

    def test_local_executor_retains_receipt(self):
        remote.configure(["local=1"])
        result = remote.execute({"timeout": 1}, lambda: {
            "returncode": 0, "stdout": "draft", "stderr": ""})
        self.assertEqual(result["execution"], {"executor": "local"})


if __name__ == "__main__":
    unittest.main()
