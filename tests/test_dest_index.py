#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.91：目标目录索引失败不再静默当空 + 龟速文件让位到第二轮。

覆盖：
- 索引首次失败、重试后成功 → 正常返回快照；
- 目录不存在 → 合法空集合（新任务首次下载）；
- 连续失败 → 显式抛错，任务标 error，绝不把已下文件当缺失重上；
- 连续龟速 → 取消当前传输并返回 stalled；allow_defer=False 时不再让位。

运行：python -m unittest discover -s tests -v
"""
import importlib.util
import os
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


class ListRc:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, **kv):
        for k in ("_timeout", "_strict", "_proxy"):
            kv.pop(k, None)
        self.calls.append((method, dict(kv)))
        if method == "operations/list":
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r
        return {}


class DestIndexTest(unittest.TestCase):
    def setUp(self):
        self.old_rc = m.rc
        self.old_delays = m.INDEX_RETRY_DELAYS
        m.INDEX_RETRY_DELAYS = (0.01, 0.01)

    def tearDown(self):
        m.rc = self.old_rc
        m.INDEX_RETRY_DELAYS = self.old_delays

    def idx(self, fake):
        m.rc = fake
        return m._dest_index({"dest": "/data/d", "proxy": False})

    def test_retry_then_success(self):
        fake = ListRc([RuntimeError("connection reset by peer"),
                       {"list": [{"Path": "a.mp4", "Size": 10, "IsDir": False},
                                 {"Path": "sub", "Size": 0, "IsDir": True}]}])
        got = self.idx(fake)
        self.assertEqual(got, {"a.mp4": 10})
        self.assertEqual(len(fake.calls), 2)

    def test_missing_dir_is_empty_ok(self):
        fake = ListRc([RuntimeError("rc operations/list: directory not found")])
        self.assertEqual(self.idx(fake), {})

    def test_persistent_failure_raises(self):
        fake = ListRc([RuntimeError("i/o timeout")] * 3)
        with self.assertRaises(RuntimeError) as cm:
            self.idx(fake)
        self.assertIn("目标目录索引失败", str(cm.exception))

    def test_error_field_in_body_raises(self):
        fake = ListRc([{"error": "context deadline exceeded"}] * 3)
        with self.assertRaises(RuntimeError):
            self.idx(fake)


class StallTest(unittest.TestCase):
    def setUp(self):
        self.old = (m.rc, m.COPY_POLL_INTERVAL, m.STALL_SECONDS, m.STALL_MIN_SPEED)
        m.COPY_POLL_INTERVAL = 0.01
        m.STALL_SECONDS = 0.05
        m.STALL_MIN_SPEED = 1024
        self.job = {"id": 7, "src": "pk:dir", "dest": "/data/dir", "mode": "copy", "proxy": False}
        self.entry = {"src": "big.mp4", "dst": "big.mp4", "size": 1000}
        self.settings = dict(m.DEFAULTS, fileRetryMax=0)
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[7] = {"stop": threading.Event()}

    def tearDown(self):
        m.rc, m.COPY_POLL_INTERVAL, m.STALL_SECONDS, m.STALL_MIN_SPEED = self.old
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS.pop(7, None)

    def _fake(self):
        calls = []
        state = {"n": 0}

        def fake(method, **kv):
            for k in ("_timeout", "_strict", "_proxy"):
                kv.pop(k, None)
            calls.append(method)
            if method == "operations/copyfile":
                return {"jobid": 9}
            if method == "job/status":
                state["n"] += 1
                return {"finished": False} if state["n"] < 60 else {"finished": True, "success": True}
            if method == "job/stop":
                return {}
            raise RuntimeError("unexpected " + method)

        peer = type("Peer", (), {"calls": calls})()
        return fake, peer

    def test_stall_cancels_and_reports(self):
        fake, peer = self._fake()
        m.rc = fake
        st, err = m.transfer_one(self.job, self.entry, self.settings, {}, set(),
                                 speed_of=lambda name: 200, allow_defer=True)
        self.assertEqual(st, "stalled")
        self.assertIn("让位", err)
        self.assertIn("job/stop", peer.calls)

    def test_no_defer_still_completes(self):
        fake, _ = self._fake()
        m.rc = fake
        st, err = m.transfer_one(self.job, self.entry, self.settings, {}, set(),
                                 speed_of=lambda name: 200, allow_defer=False)
        self.assertEqual((st, err), ("done", ""))

    def test_all_peers_slow_never_defers(self):
        # 账号整体限速（最快的一条也只有 300 B/s）：让位没有意义，应该老实下完
        fake, peer = self._fake()
        m.rc = fake
        st, err = m.transfer_one(self.job, self.entry, self.settings, {}, set(),
                                 speed_of=lambda name: 200, allow_defer=True,
                                 peer_speed_of=lambda: 300)
        self.assertEqual((st, err), ("done", ""))
        self.assertNotIn("job/stop", peer.calls)


if __name__ == "__main__":
    unittest.main()
