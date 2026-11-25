"""v4.15 孤儿传输清理的纯逻辑测试。

背景：逐文件下载是「一个文件一个异步 job」，worker 只 stop 自己手上那一个。
job/stop 失败被吞掉时，工作线程会重试同一文件 → 旧 job 继续传、新 job 又起，
同一个文件被并行重下几份（实测同一个视频在 3 个组里同时传），面板就出现
「进行中 0 / 任务出错，却一直有速度」。
修法：所有异步 job 打上任务级 _group，停止/出错/删记录/重启巡检时按组清掉。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_orphan", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class GroupNameTest(unittest.TestCase):
    def test_group_name(self):
        self.assertEqual(m.job_group_of("1795451685452"), "cloudshuttle/1795451685452")
        self.assertTrue(m.job_group_of(7).startswith(m.JOB_GROUP_PREFIX))


class StopGroupTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self._orig_rc = m.rc

    def tearDown(self):
        m.rc = self._orig_rc

    def test_uses_stopgroup_first(self):
        def fake_rc(cmd, **kw):
            self.calls.append((cmd, kw.get("group"), kw.get("jobid")))
            if cmd == "job/stopgroup":
                return {}
            raise AssertionError("不该走到兜底分支")
        m.rc = fake_rc
        self.assertTrue(m.stop_job_group("abc"))

    def test_falls_back_to_job_list_when_stopgroup_missing(self):
        """老版 rclone 没有 job/stopgroup（返回 Didn't find key / 不支持）：逐个 job/stop。"""
        def fake_rc(cmd, **kw):
            self.calls.append((cmd, kw.get("group"), kw.get("jobid")))
            if cmd == "job/stopgroup":
                raise RuntimeError("unknown method job/stopgroup")
            if cmd == "job/list":
                return {"jobs": [{"id": 1, "group": "cloudshuttle/abc"},
                                 {"id": 2, "group": "cloudshuttle/abc"},
                                 {"id": 3, "group": "cloudshuttle/other"}]}
            if cmd == "job/stop":
                return {}
            raise AssertionError(cmd)
        m.rc = fake_rc
        self.assertTrue(m.stop_job_group("abc"))
        stopped = [c for c in self.calls if c[0] == "job/stop"]
        self.assertEqual(sorted(c[2] for c in stopped), [1, 2])   # 别的任务别动




class SweepTest(unittest.TestCase):
    def setUp(self):
        self._orig = (m.rc, m.load_jobs)
        self.calls = []
        self.transferring = []
        self.unfinished = []
        self.proxy_down = True

    def tearDown(self):
        m.rc, m.load_jobs = self._orig

    def fake_rc(self, cmd, **kw):
        if kw.get("_proxy") and self.proxy_down:
            raise RuntimeError("proxy daemon down")
        self.calls.append((cmd, kw.get("group"), kw.get("_proxy")))
        if cmd == "core/stats":
            return {"transferring": [{"group": g} for g in self.transferring]}
        if cmd == "job/list":
            return {"jobs": [{"id": i, "group": g} for i, g in enumerate(self.unfinished)]}
        if cmd == "job/stopgroup":
            return {}
        raise AssertionError(cmd)

    def test_kills_unowned_groups_only(self):
        m.rc = self.fake_rc
        self.transferring = ["cloudshuttle/111", "cloudshuttle/222", "cloudshuttle/333"]
        m.load_jobs = lambda: [{"id": "111", "state": "running"},
                               {"id": "222", "state": "error"},
                               {"id": "333", "state": "done"}]
        killed = m.sweep_orphan_groups()
        stopped = [g for c, g, _ in self.calls if c == "job/stopgroup"]
        self.assertEqual(sorted(stopped), ["cloudshuttle/222", "cloudshuttle/333"])
        self.assertEqual(killed, 2)      # 111 在跑，不能停

    def test_no_active_groups_is_noop(self):
        m.rc = self.fake_rc
        m.load_jobs = lambda: []
        self.assertEqual(m.sweep_orphan_groups(), 0)

    def test_kills_untagged_leftovers_when_nothing_running(self):
        """升级到 v4.15 之前启动的老 job 没有 cloudshuttle/ 前缀；没有在跑任务时一并清掉。"""
        m.rc = self.fake_rc
        self.unfinished = ["job/217848", "job/217859"]
        m.load_jobs = lambda: [{"id": "1", "state": "done"}]
        killed = m.sweep_orphan_groups()
        stopped = [g for c, g, _ in self.calls if c == "job/stopgroup"]
        self.assertEqual(sorted(stopped), ["job/217848", "job/217859"])
        self.assertEqual(killed, 2)

    def test_running_task_keeps_untagged_jobs_alone(self):
        """还有任务在跑时不能乱停无标签组，可能正是那个任务升级前的 job。"""
        m.rc = self.fake_rc
        self.unfinished = ["job/217848"]
        m.load_jobs = lambda: [{"id": "1", "state": "running"}]
        m.sweep_orphan_groups()
        self.assertEqual([g for c, g, _ in self.calls if c == "job/stopgroup"], [])

    def test_proxy_daemon_also_swept(self):
        self.proxy_down = False
        m.rc = self.fake_rc
        self.transferring = ["cloudshuttle/9"]
        m.load_jobs = lambda: []
        m.sweep_orphan_groups()
        proxy_stops = [g for c, g, p in self.calls if c == "job/stopgroup" and p]
        self.assertEqual(proxy_stops, ["cloudshuttle/9"])


if __name__ == "__main__":
    unittest.main()

class ActiveSpeedTest(unittest.TestCase):
    def test_sums_transferring(self):
        d = {"transferring": [{"speed": 100000}, {"speed": 250000.5}]}
        self.assertAlmostEqual(m.active_speed(d), 350000.5, places=1)

    def test_zero_when_nothing_transferring(self):
        self.assertEqual(m.active_speed({"transferring": []}), 0.0)
        self.assertEqual(m.active_speed({}), 0.0)

    def test_missing_speed_counts_as_zero(self):
        d = {"transferring": [{"speed": None}, {"speed": 1024}, {}]}
        self.assertEqual(m.active_speed(d), 1024.0)
