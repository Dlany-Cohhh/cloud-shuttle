#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.93：RC 传输层与孤儿分片清理的单元测试。

- RC 走 HTTP 时，rclone 的「非 200 + JSON 体」也要能解析成 dict（job not found 是 500）；
- 正常 200 体解析；
- 孤儿分片只清超过 24h 的，正在传的（mtime 新）和非 .partial 文件都不动。
"""
import http.server
import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class _Stub(http.server.BaseHTTPRequestHandler):
    code = 200
    body = b"{}"

    def do_POST(self):                       # noqa: N802
        _Stub.last_path = self.path
        self.send_response(_Stub.code)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(_Stub.body)

    def log_message(self, *a):
        pass


def _serve():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Stub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class RcHttpTest(unittest.TestCase):
    def test_200_body_parsed(self):
        srv = _serve()
        try:
            _Stub.code, _Stub.body = 200, json.dumps({"jobid": 7, "finished": False}).encode()
            d = m._rc_http_call(srv.server_port, "job/status", {"jobid": 7}, 5)
            self.assertEqual(d["jobid"], 7)
            self.assertTrue(_Stub.last_path.endswith("/job/status"))
        finally:
            srv.shutdown()

    def test_500_json_body_parsed(self):
        """job not found：rclone 返回 500 + {"error":...}，不能当成传输异常。"""
        srv = _serve()
        try:
            _Stub.code, _Stub.body = 500, json.dumps({"error": "job not found"}).encode()
            d = m._rc_http_call(srv.server_port, "job/status", {"jobid": 1}, 5)
            self.assertEqual(d.get("error"), "job not found")
        finally:
            srv.shutdown()

    def test_garbage_body_raises(self):
        srv = _serve()
        try:
            _Stub.code, _Stub.body = 200, b"<html>not json</html>"
            with self.assertRaises(RuntimeError):
                m._rc_http_call(srv.server_port, "core/stats", {}, 5)
        finally:
            srv.shutdown()


class WaitWorkerTest(unittest.TestCase):
    def test_unknown_job_returns_immediately(self):
        self.assertEqual(m.wait_worker_exit(987654321, 0.1), (0.0, False))

    def test_dead_thread_is_reported_as_exited(self):
        th = threading.Thread(target=lambda: None)
        th.start()
        th.join()
        with m.SAFE_JOBS_LOCK:
            m.SAFE_THREADS[555] = th
        try:
            self.assertEqual(m.wait_worker_exit(555, 0.1)[1], False)
        finally:
            with m.SAFE_JOBS_LOCK:
                m.SAFE_THREADS.pop(555, None)


@unittest.skipIf(os.name != "posix", "chmod 语义只在 POSIX 上有意义")
class FixPermissionTest(unittest.TestCase):
    def test_zero_mode_entries_fixed(self):
        with tempfile.TemporaryDirectory() as d:
            sub = os.path.join(d, "sub")
            os.mkdir(sub)
            f = os.path.join(sub, "a.mp4")
            with open(f, "wb") as fp:
                fp.write(b"x")
            os.chmod(sub, 0o000)
            os.chmod(f, 0o000)
            nd, nf = m.fix_permissions(d)
            self.assertGreaterEqual(nd, 1)
            self.assertGreaterEqual(nf, 1)
            self.assertEqual(oct(os.stat(sub).st_mode & 0o777), "0o755")
            self.assertEqual(oct(os.stat(f).st_mode & 0o777), "0o644")


class OrphanPartialTest(unittest.TestCase):
    def test_only_old_partials_removed(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.path.join(d, "a.mp4.ab12cd.partial")
            with open(old, "wb") as f:
                f.write(b"x" * 100)
            past = time.time() - 25 * 3600
            os.utime(old, (past, past))

            fresh = os.path.join(d, "b.mp4.ef34.partial")
            with open(fresh, "wb") as f:
                f.write(b"y" * 10)

            normal = os.path.join(d, "c.mp4")
            with open(normal, "wb") as f:
                f.write(b"z" * 5)

            n, freed = m.cleanup_orphan_partials(d)
            self.assertEqual(n, 1)
            self.assertEqual(freed, 100)
            self.assertFalse(os.path.exists(old))
            self.assertTrue(os.path.exists(fresh))
            self.assertTrue(os.path.exists(normal))

    def test_missing_dir_is_noop(self):
        self.assertEqual(m.cleanup_orphan_partials("/no/such/dir"), (0, 0))

    def test_empty_dest_is_noop(self):
        self.assertEqual(m.cleanup_orphan_partials(""), (0, 0))


if __name__ == "__main__":
    unittest.main()
