"""阶段 1 回归测试：jobs / resetsettings 响应必须带 proxyScope 字段。

背景（2026-11-26 完整性审计发现）：前端 app.js:1128 在 jobs 轮询响应里读
d.proxyScope（v3.77 全局代理模式指示），app.js:3073 在 resetsettings 响应里
也读，但后端只有 settings / savesettings 分支返回该字段，jobs 与
resetsettings 的响应里缺失——属于「后端没给、前端在等」的字段断链。
本测试直接调 api() 真分支，锁住这两个响应的键，防止重构时再丢。
"""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("dc_proxyscope", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class ProxyScopeHarness(unittest.TestCase):
    """数据目录换到临时目录，避免 resetsettings 真写项目 settings.json。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self._saved = {k: getattr(m, k) for k in
                       ("DATA_DIR", "SETTINGS_FILE", "JOBS_FILE", "rc")}
        m.DATA_DIR = root
        m.SETTINGS_FILE = os.path.join(root, "settings.json")
        m.JOBS_FILE = os.path.join(root, "jobs.json")
        # jobs 分支会跑 job_live / storages，全部经由 rc：fake 掉防网络
        m.rc = lambda method, **kv: {}

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(m, k, v)
        with m.LOCK:
            m.JOBS_SNAPSHOT = None
        m.JOBS_SNAP.clear()
        self.tmp.cleanup()

    def test_resetsettings_response_has_proxyscope(self):
        """resetsettings 响应必须带 proxyScope（前端恢复默认后会同步代理模式指示）。"""
        out = m.api("resetsettings", {})
        self.assertTrue(out.get("ok"), out)
        self.assertIn("proxyScope", out, "resetsettings 响应丢了 proxyScope 字段")
        self.assertIn("mode", out["proxyScope"])

    def test_jobs_response_has_proxyscope(self):
        """jobs 轮询响应必须带 proxyScope（前端 app.js v3.77 的同步逻辑依赖它）。"""
        m._write_json(m.JOBS_FILE, [])
        m.JOBS_SNAP.clear()                      # 绕开 1.2s 快照，走真实构造
        out = m.api("jobs", {})
        self.assertIn("jobs", out)
        self.assertIn("proxyScope", out, "jobs 响应丢了 proxyScope 字段")
        self.assertIn("mode", out["proxyScope"])


if __name__ == "__main__":
    unittest.main()
