#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""v3.83 长文件名安全命名：单元测试 + 用假 rclone 跑的目录任务集成测试。

运行：python -m unittest discover -s tests -v
不需要 Docker / rclone / 网络；所有外部调用都用内存里的 FakeRclone 替代。
"""
import importlib.util
import os
import tempfile
import threading
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
spec = importlib.util.spec_from_file_location("console", os.path.join(ROOT, "download-console.py"))
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

LONG_CN = "这是一个非常非常长的文件名" * 30 + ".mp4"     # 远超 255 字节的 UTF-8 中文名


class NamingTest(unittest.TestCase):
    def test_truncate_utf8_never_splits_char(self):
        s = "汉字" * 50
        out = m.truncate_utf8(s, 100)
        self.assertLessEqual(len(out.encode("utf-8")), 100)
        self.assertEqual(out.encode("utf-8"), out.encode("utf-8").decode("utf-8").encode("utf-8"))
        self.assertTrue(s.startswith(out))

    def test_truncate_utf8_short_unchanged(self):
        self.assertEqual(m.truncate_utf8("abc.mp4", 100), "abc.mp4")
        self.assertEqual(m.truncate_utf8("", 10), "")

    def test_clean_segment_illegal_chars(self):
        self.assertEqual(m.clean_segment('a<b>:c/d\\e|f?g*h"i.mp4'), "a_b__c_d_e_f_g_h_i.mp4")

    def test_clean_segment_trailing_space_dot(self):
        self.assertEqual(m.clean_segment("name.mp4 .  "), "name.mp4")
        self.assertEqual(m.clean_segment("   .  "), "unnamed")

    def test_clean_segment_control_chars(self):
        self.assertEqual(m.clean_segment("a\x00b\x1fc.mp4"), "a_b_c.mp4")

    def test_clean_segment_device_names(self):
        self.assertEqual(m.clean_segment("CON"), "_CON")
        self.assertEqual(m.clean_segment("com1.mp4"), "_com1.mp4")
        self.assertEqual(m.clean_segment("LPT9.txt"), "_LPT9.txt")
        self.assertEqual(m.clean_segment("CONSOLE.mp4"), "CONSOLE.mp4")

    def test_safe_basename_normal_unchanged(self):
        self.assertEqual(m.safe_basename("电影.2024.1080p.mp4", "pk", "dir/电影.2024.1080p.mp4"),
                         "电影.2024.1080p.mp4")

    def test_safe_basename_long_hashed_and_bounded(self):
        out = m.safe_basename(LONG_CN, "pk", "dir/" + LONG_CN)
        self.assertLessEqual(len(out.encode("utf-8")), 190)
        self.assertTrue(out.endswith(".mp4"))
        self.assertIn("~", out)

    def test_safe_basename_deterministic(self):
        a = m.safe_basename(LONG_CN, "pk", "dir/" + LONG_CN)
        b = m.safe_basename(LONG_CN, "pk", "dir/" + LONG_CN)
        self.assertEqual(a, b)

    def test_safe_basename_diff_dirs_diff_hash(self):
        a = m.safe_basename(LONG_CN, "pk", "d1/" + LONG_CN)
        b = m.safe_basename(LONG_CN, "pk", "d2/" + LONG_CN)
        self.assertNotEqual(a, b)

    def test_safe_basename_diff_remote_diff_hash(self):
        a = m.safe_basename(LONG_CN, "pk1", "d/" + LONG_CN)
        b = m.safe_basename(LONG_CN, "pk2", "d/" + LONG_CN)
        self.assertNotEqual(a, b)

    def test_safe_basename_device_name_no_hash(self):
        self.assertEqual(m.safe_basename("CON.mp4", "pk", "d/CON.mp4"), "_CON.mp4")

    def test_safe_basename_illegal_char_gets_hash(self):
        out = m.safe_basename("a:b.mp4", "pk", "d/a:b.mp4")
        self.assertTrue(out.startswith("a_b"))
        self.assertIn("~", out)
        self.assertTrue(out.endswith(".mp4"))

    def test_safe_basename_long_extension_truncated(self):
        name = "video." + "x" * 80
        out = m.safe_basename(name, "pk", "d/" + name)
        self.assertLessEqual(len(out.encode("utf-8")), 200)

    def test_safe_relpath_cleans_each_segment(self):
        out = m.safe_relpath("a:b/c*d/e.mp4", "pk")
        self.assertNotIn(":", out)
        self.assertNotIn("*", out)
        self.assertTrue(out.endswith("e.mp4"))

    def test_safe_relpath_total_cap_keeps_filename(self):
        rel = "/".join(["长目录名" * 20] * 6 + [LONG_CN])
        out = m.safe_relpath(rel, "pk", max_total=240)
        self.assertTrue(out.endswith(".mp4"))
        self.assertLessEqual(len(out.encode("utf-8")), 300)
        self.assertEqual(out, m.safe_relpath(rel, "pk", max_total=240))

    def test_safe_relpath_ignores_parent_refs(self):
        self.assertNotIn("..", m.safe_relpath("../a/./b.mp4", "pk").split("/"))

    def test_build_name_map_case_conflict(self):
        files = [{"Path": "A.mp4", "Size": 1}, {"Path": "a.mp4", "Size": 2}]
        ents = m.build_name_map(files, "pk", "d")
        dsts = [e["dst"] for e in ents]
        self.assertEqual(len({d.casefold() for d in dsts}), 2)
        self.assertIn("A.mp4", dsts)
        self.assertTrue(any("-2" in d for d in dsts))

    def test_build_name_map_truncation_collision(self):
        # 两个超长名前缀相同，只靠截断会撞名；哈希 + 序号必须把它们分开
        a, b = LONG_CN + "A.mp4", LONG_CN + "B.mp4"
        ents = m.build_name_map([{"Path": a, "Size": 1}, {"Path": b, "Size": 1}], "pk", "d")
        self.assertEqual(len({e["dst"] for e in ents}), 2)

    def test_build_name_map_stable_order(self):
        files = [{"Path": "b.mp4", "Size": 1}, {"Path": "a.mp4", "Size": 1}]
        e1 = m.build_name_map(files, "pk", "d")
        e2 = m.build_name_map(list(reversed(files)), "pk", "d")
        self.assertEqual([e["dst"] for e in e1], [e["dst"] for e in e2])
        self.assertTrue(all(e["status"] == "pending" for e in e1))

    def test_build_name_map_skips_dirs(self):
        ents = m.build_name_map([{"Path": "sub", "IsDir": True}, {"Path": "sub/a.mp4", "Size": 3}], "pk", "d")
        self.assertEqual(len(ents), 1)
        self.assertEqual(ents[0]["size"], 3)


class ErrorClassifyTest(unittest.TestCase):
    def test_file_fatal_keywords(self):
        for msg in ("file name too long", "Filename Too Long", "path too long",
                    "文件名或扩展名太长", "路径太长"):
            kind, _ = m.classify_error(msg)
            self.assertEqual(kind, "FILE_FATAL", msg)

    def test_invalid_argument_needs_name_context(self):
        self.assertEqual(m.classify_error("invalid argument", context="a/b.mp4")[0], "FILE_FATAL")
        self.assertNotEqual(m.classify_error("invalid argument", context="")[0], "FILE_FATAL")

    def test_account_fatal(self):
        kind, _ = m.classify_error("daily_download_size_exceeded")
        self.assertEqual(kind, "ACCOUNT_FATAL")
        kind, _ = m.classify_error("invalid_grant")
        self.assertEqual(kind, "ACCOUNT_FATAL")

    def test_disk_fatal(self):
        self.assertEqual(m.classify_error("no space left on device")[0], "DISK_FATAL")
        self.assertEqual(m.classify_error("permission denied")[0], "DISK_FATAL")

    def test_transient(self):
        self.assertEqual(m.classify_error("i/o timeout")[0], "TRANSIENT")
        self.assertEqual(m.classify_error("connection reset by peer")[0], "TRANSIENT")

    def test_unknown(self):
        self.assertEqual(m.classify_error("something odd happened")[0], "UNKNOWN")

    def test_fatal_job_error_covers_long_name(self):
        # 老任务（非 v2）也要能脱离 running，不再永远「下载中」
        st = m._fatal_job_error("failed to copy: file name too long")
        self.assertIsNotNone(st)
        self.assertEqual(st[0], "error")
        self.assertIsNone(m._fatal_job_error("i/o timeout"))

    def test_error_masking(self):
        token = "os." + "a" * 60
        _, friendly = m.classify_error("file name too long " + token)
        self.assertNotIn(token, friendly)


class SettingsTest(unittest.TestCase):
    def test_defaults_present(self):
        s = m._norm_settings({})
        self.assertTrue(s["safeNaming"])
        self.assertEqual(s["safeNameMaxBytes"], 180)
        self.assertEqual(s["safeNameHashLen"], 12)
        self.assertEqual(s["safePathMaxBytes"], 240)
        self.assertEqual(s["stuckWatchdogMin"], 10)
        self.assertEqual(s["fileRetryMax"], 3)

    def test_clamps_and_bool(self):
        s = m._norm_settings({"safeNaming": "false", "safeNameMaxBytes": 99999,
                              "fileRetryMax": -5, "stuckWatchdogMin": 0})
        self.assertFalse(s["safeNaming"])
        self.assertEqual(s["safeNameMaxBytes"], 240)
        self.assertEqual(s["fileRetryMax"], 0)
        self.assertEqual(s["stuckWatchdogMin"], 0)

    def test_advanced_job_config_untouched(self):
        cfg = m._job_config(m._norm_settings({}))
        self.assertNotIn("Retries", cfg)     # 高级模式默认关闭，行为同旧版


class Harness(unittest.TestCase):
    """把数据目录、存储卷、设置、rclone 全部换成内存 / 临时目录实现。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self._saved = {k: getattr(m, k) for k in
                       ("DATA_DIR", "JOBS_FILE", "SETTINGS_FILE", "JOB_FILES_DIR", "rc", "load_settings")}
        self._volpairs = m.STATE.get("volumePairs")
        m.DATA_DIR = root
        m.JOBS_FILE = os.path.join(root, "jobs.json")
        m.SETTINGS_FILE = os.path.join(root, "settings.json")
        m.JOB_FILES_DIR = os.path.join(root, "jobfiles")
        m.STATE["volumePairs"] = [{"rclone": "/data", "console": root, "host": ""}]
        m.NAME_MAPS.clear()
        m.NAME_MAP_DIRTY.clear()
        m.load_settings = lambda: dict(m.DEFAULTS, transfers=2, fileRetryMax=0, stuckWatchdogMin=10)

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(m, k, v)
        m.STATE["volumePairs"] = self._volpairs
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS.clear()
        m.NAME_MAPS.clear()
        m.NAME_MAP_DIRTY.clear()
        self.tmp.cleanup()

    def seed_job(self, job):
        m._write_json(m.JOBS_FILE, [job])


class ManifestTest(Harness):
    def vol(self):
        return m.STATE["volumePairs"][0]

    def test_record_and_reload(self):
        m.record_file_state(self.vol(), "pk", "d/" + LONG_CN, safe="short~abc.mp4",
                            size=123, status="done", _flush=True)
        path = m._name_map_path(self.vol())
        self.assertTrue(os.path.isfile(path))
        self.assertEqual(os.path.basename(path), ".cloudshuttle-name-map.json")
        m.NAME_MAPS.clear()
        d = m.load_name_map(self.vol())
        ent = d["entries"]["pk:d/" + LONG_CN]
        self.assertEqual(ent["safe"], "short~abc.mp4")
        self.assertEqual(ent["status"], "done")
        self.assertEqual(d["version"], 2)
        self.assertEqual(m.get_safe_path(self.vol(), "pk", "d/" + LONG_CN), "short~abc.mp4")

    def test_atomic_write_leaves_no_tmp(self):
        m.record_file_state(self.vol(), "pk", "a.mp4", safe="a.mp4", _flush=True)
        self.assertFalse(os.path.exists(m._name_map_path(self.vol()) + ".tmp"))

    def test_old_version_backed_up_not_migrated(self):
        path = m._name_map_path(self.vol())
        m._write_json(path, {"version": 1, "entries": {"x": 1}})
        m.NAME_MAPS.clear()
        d = m.load_name_map(self.vol())
        self.assertEqual(d["version"], 2)
        self.assertEqual(d["entries"], {})
        self.assertTrue(os.path.isfile(path + ".old"))

    def test_plan_roundtrip(self):
        m.save_file_plan(7, {"version": 2, "entries": [{"src": "a", "status": "done"}]})
        self.assertEqual(m.load_file_plan(7)["entries"][0]["src"], "a")
        self.assertIsNone(m.load_file_plan(999))


class FakeRclone:
    """内存版 rclone RC：源文件表 + 目标文件表，支持注入确定性失败。"""

    def __init__(self, src_files, fail_names=()):
        self.src = dict(src_files)
        self.dst = {}
        self.fail_names = set(fail_names)
        self.calls = []

    def copies(self):
        return [c for c in self.calls if c[0] == "operations/copyfile"]

    def __call__(self, method, **kv):
        for k in ("_timeout", "_strict", "_proxy"):
            kv.pop(k, None)
        self.calls.append((method, dict(kv)))
        if method == "operations/list":
            data = self.src if str(kv.get("fs", "")).startswith("pk:") else self.dst
            return {"list": [{"Path": p, "Name": p.rsplit("/", 1)[-1], "Size": sz, "IsDir": False}
                             for p, sz in sorted(data.items())]}
        if method == "operations/copyfile":
            rel = kv["srcRemote"]
            if rel in self.fail_names:
                raise RuntimeError("rc operations/copyfile: file name too long")
            if rel not in self.src:
                raise RuntimeError("rc operations/copyfile: object not found")
            self.dst[kv["dstRemote"]] = self.src[rel]
            return {}
        if method == "operations/mkdir":
            return {}
        if method == "operations/movefile":
            self.dst[kv["dstRemote"]] = self.dst.pop(kv["srcRemote"])
            return {}
        if method == "operations/deletefile":
            self.src.pop(kv["remote"], None)
            return {}
        if method == "operations/rmdirs":
            return {}
        raise RuntimeError("unexpected rc method " + method)


def make_dir_job(jid=1, mode="copy"):
    return {"id": jid, "jobid": None, "remote": "pk", "subpath": "dir", "src": "pk:dir",
            "dest": "/data/dir", "mode": mode, "naming": "v2", "proxy": False,
            "state": "running", "bytes": 0, "total": 0, "speed": 0, "eta": None,
            "error": "", "started": "11-20 15:00"}


class DirWorkerTest(Harness):
    def run_worker(self, job, fake):
        self.seed_job(job)
        m.rc = fake
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[job["id"]] = {"stop": threading.Event()}
        m._safe_dir_worker(job["id"])
        return m.load_jobs()[0]

    def test_long_names_all_downloaded(self):
        files = {LONG_CN: 100, "普通文件.mkv": 200, "sub/" + LONG_CN: 300}
        job = self.run_worker(make_dir_job(), FakeRclone(files))
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["filesTotal"], 3)
        self.assertEqual(job["filesDone"], 3)
        self.assertEqual(job["total"], 600)
        self.assertEqual(job["bytes"], 600)
        self.assertEqual(job["error"], "")
        self.assertIn("普通文件.mkv", m.rc.dst)          # 正常文件名原样保留
        safe = [p for p in m.rc.dst if "~" in p]
        self.assertEqual(len(safe), 2)                    # 两个超长名都走了安全名
        for p in safe:
            self.assertLessEqual(len(p.rsplit("/", 1)[-1].encode("utf-8")), 190)
        plan = m.load_file_plan(job["id"])
        self.assertTrue(all(e["status"] == "done" for e in plan["entries"]))
        vol = m.STATE["volumePairs"][0]
        self.assertEqual(len(m.load_name_map(vol)["entries"]), 3)

    def test_launch_before_job_saved_still_downloads(self):
        # start 的顺序是 launch_job → append → save_jobs，工作线程不能依赖启动时就能读到 jobs.json
        fake = FakeRclone({LONG_CN: 100})
        m.rc = fake
        job = make_dir_job(jid=77)              # 故意不落盘，模拟 launch 与 save 之间的窗口
        self.assertTrue(m._start_safe_dir_job(job, m.load_settings()))
        deadline = time.time() + 10
        while time.time() < deadline:
            with m.SAFE_JOBS_LOCK:
                if 77 not in m.SAFE_JOBS:
                    break
            time.sleep(0.05)
        with m.SAFE_JOBS_LOCK:
            self.assertNotIn(77, m.SAFE_JOBS)
        self.assertEqual(len(fake.dst), 1)
        self.assertIsNotNone(m.load_file_plan(77))

    def test_concurrent_mixed_existing(self):
        # 多线程下同时有「同大小跳过 / 冲突改名 / 新下载」，共享快照必须线程安全
        files = {"f%02d.mp4" % i: i + 1 for i in range(40)}
        fake = FakeRclone(files)
        for i in range(0, 40, 4):
            fake.dst["f%02d.mp4" % i] = i + 1
        for i in range(1, 40, 4):
            fake.dst["f%02d.mp4" % i] = 999999
        m.load_settings = lambda: dict(m.DEFAULTS, transfers=6, fileRetryMax=0)
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "done", job.get("error"))
        self.assertEqual(job["filesDone"], 40)
        self.assertEqual(job["filesFailed"], 0)
        self.assertEqual(job["error"], "")

    def test_single_failure_isolated(self):
        files = {LONG_CN: 100, "bad.mp4": 50, "good.mp4": 60}
        fake = FakeRclone(files, fail_names={"bad.mp4"})
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "done")
        self.assertEqual(job["filesDone"], 2)
        self.assertEqual(job["filesFailed"], 1)
        self.assertIn("仅重试失败项", job["error"])
        self.assertIn("good.mp4", fake.dst)
        self.assertNotIn("bad.mp4", fake.dst)

    def test_all_failed_becomes_error(self):
        fake = FakeRclone({"bad.mp4": 50}, fail_names={"bad.mp4"})
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "error")
        self.assertIn("全部", job["error"])

    def test_account_error_aborts_whole_job(self):
        fake = FakeRclone({"a.mp4": 1, "b.mp4": 2}, fail_names=set())
        orig = fake.__call__

        def wrapper(method, **kv):
            if method == "operations/copyfile":
                raise RuntimeError("daily_download_size_exceeded")
            return orig(method, **kv)
        m.rc = wrapper
        self.seed_job(make_dir_job())
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[1] = {"stop": threading.Event()}
        m._safe_dir_worker(1)
        job = m.load_jobs()[0]
        self.assertEqual(job["state"], "stopped")
        self.assertIn("额度", job["error"])

    def test_resume_skips_completed(self):
        files = {LONG_CN: 100, "a.mp4": 10, "b.mp4": 20}
        fake = FakeRclone(files)
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "done")
        n = len(fake.copies())
        job["state"] = "stopped"
        job = self.run_worker(job, fake)
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(fake.copies()), n)           # 没有重复下载任何文件

    def test_existing_same_size_skipped(self):
        fake = FakeRclone({"a.mp4": 10})
        fake.dst["a.mp4"] = 10
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "done")
        self.assertEqual(len(fake.copies()), 0)
        plan = m.load_file_plan(job["id"])
        self.assertEqual(plan["entries"][0]["status"], "skipped")

    def test_existing_size_conflict_renamed_not_overwritten(self):
        fake = FakeRclone({"a.mp4": 10})
        fake.dst["a.mp4"] = 999
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["state"], "done")
        self.assertEqual(fake.dst["a.mp4"], 10)
        self.assertTrue(any(".conflict-" in p and fake.dst[p] == 999 for p in fake.dst))

    def test_retry_failed_only(self):
        files = {"bad.mp4": 50, "good.mp4": 60}
        fake = FakeRclone(files, fail_names={"bad.mp4"})
        job = self.run_worker(make_dir_job(), fake)
        self.assertEqual(job["filesFailed"], 1)
        fake.fail_names.clear()
        out = m.api("jobretryfailed", {"id": job["id"]})
        self.assertTrue(out.get("ok"), out)
        self.assertEqual(out["count"], 1)
        deadline = time.time() + 20
        cur = m.load_jobs()[0]
        while time.time() < deadline:
            cur = m.load_jobs()[0]
            with m.SAFE_JOBS_LOCK:
                alive = cur["id"] in m.SAFE_JOBS
            if cur["state"] == "done" and not alive and cur.get("filesFailed") == 0:
                break
            time.sleep(0.1)
        cur = m.load_jobs()[0]
        self.assertEqual(cur["state"], "done", cur.get("error"))
        self.assertEqual(cur["filesFailed"], 0)
        self.assertIn("bad.mp4", fake.dst)
        self.assertEqual(len([c for c in fake.copies() if c[1]["srcRemote"] == "good.mp4"]), 1)

    def test_stop_requested_keeps_pending(self):
        files = {"f%d.mp4" % i: 1 for i in range(6)}
        started, release = threading.Event(), threading.Event()

        class BlockingFake(FakeRclone):
            def __call__(self, method, **kv):
                if method == "operations/copyfile":
                    started.set()
                    release.wait(10)         # 卡在第一个文件上，等主线程点「暂停」
                return super().__call__(method, **kv)

        fake = BlockingFake(files)
        self.seed_job(make_dir_job())
        m.rc = fake
        with m.SAFE_JOBS_LOCK:
            m.SAFE_JOBS[1] = {"stop": threading.Event()}
        t = threading.Thread(target=m._safe_dir_worker, args=(1,), daemon=True)
        t.start()
        self.assertTrue(started.wait(10))
        job = m.load_jobs()[0]
        m.stop_job_entry(job)                # 与 stop action 同一条路径：置事件 + 写 stopped
        m.save_jobs([job])
        release.set()
        t.join(15)
        self.assertFalse(t.is_alive())
        job = m.load_jobs()[0]
        self.assertEqual(job["state"], "stopped")
        self.assertLess(len(fake.copies()), len(files))
        plan = m.load_file_plan(1)
        self.assertTrue(any(e["status"] == "pending" for e in plan["entries"]))


class SingleFileTest(Harness):
    def test_launch_uses_safe_leaf(self):
        captured = {}

        def fake_rc(method, **kv):
            captured.update(kv)
            captured["method"] = method
            return {"jobid": 42}
        m.rc = fake_rc
        job = {"id": 5, "remote": "pk", "subpath": "d/" + LONG_CN, "src": "pk:d/" + LONG_CN,
               "src_dir": "pk:d", "dst_dir": "/data", "dest": "/data", "leaf": LONG_CN,
               "safe_leaf": m.safe_basename(LONG_CN, "pk", "d/" + LONG_CN),
               "single_file": True, "naming": "v2", "mode": "copy", "proxy": False,
               "state": "queued", "error": ""}
        self.assertTrue(m.launch_job(job, m.load_settings()))
        self.assertEqual(captured["srcRemote"], LONG_CN)           # 源端原名
        self.assertEqual(captured["dstRemote"], job["safe_leaf"])  # 目标端安全名
        self.assertNotEqual(captured["dstRemote"], LONG_CN)
        self.assertEqual(job["curfile"], LONG_CN)                  # 界面显示原名
        self.assertEqual(job["jobid"], 42)

    def test_legacy_job_unchanged(self):
        captured = {}

        def fake_rc(method, **kv):
            captured.update(kv)
            return {"jobid": 43}
        m.rc = fake_rc
        job = {"id": 6, "remote": "pk", "src_dir": "pk:d", "dst_dir": "/data", "dest": "/data",
               "leaf": LONG_CN, "single_file": True, "mode": "copy", "proxy": False,
               "state": "queued", "error": ""}
        self.assertTrue(m.launch_job(job, m.load_settings()))
        self.assertEqual(captured["dstRemote"], LONG_CN)           # 没有 naming=v2 → 旧行为


class WatchdogTest(Harness):
    def test_stuck_v2_job_terminated(self):
        j = {"id": 1, "naming": "v2", "state": "running", "bytes": 0, "speed": 0, "files": 0,
             "_progBytes": 0, "_progAt": time.time() - 3600}
        m._stuck_watchdog(j, {"stuckWatchdogMin": 10})
        self.assertEqual(j["state"], "error")
        self.assertIn("疑似卡住", j["error"])

    def test_active_transfer_not_killed(self):
        j = {"id": 1, "naming": "v2", "state": "running", "bytes": 0, "speed": 0, "files": 1,
             "_progBytes": 0, "_progAt": time.time() - 3600}
        m._stuck_watchdog(j, {"stuckWatchdogMin": 10})
        self.assertEqual(j["state"], "running")

    def test_legacy_job_not_touched(self):
        j = {"id": 1, "state": "running", "bytes": 0, "speed": 0, "files": 0,
             "_progBytes": 0, "_progAt": time.time() - 3600}
        m._stuck_watchdog(j, {"stuckWatchdogMin": 10})
        self.assertEqual(j["state"], "running")

    def test_disabled(self):
        j = {"id": 1, "naming": "v2", "state": "running", "bytes": 0, "speed": 0, "files": 0,
             "_progBytes": 0, "_progAt": time.time() - 3600}
        m._stuck_watchdog(j, {"stuckWatchdogMin": 0})
        self.assertEqual(j["state"], "running")

    def test_progress_resets_timer(self):
        j = {"id": 1, "naming": "v2", "state": "running", "bytes": 100, "speed": 0, "files": 0,
             "_progBytes": 0, "_progAt": time.time() - 3600}
        m._stuck_watchdog(j, {"stuckWatchdogMin": 10})
        self.assertEqual(j["state"], "running")
        self.assertEqual(j["_progBytes"], 100)


class RestartDetectTest(Harness):
    def test_v2_dir_job_marked_stopped_after_restart(self):
        j = make_dir_job()
        m.job_live(j)                       # SAFE_JOBS 里没有它 = 控制台重启过
        self.assertEqual(j["state"], "stopped")
        self.assertIn("控制台重启过", j["error"])

    def test_v2_single_terminal_writes_manifest(self):
        j = {"id": 2, "naming": "v2", "single_file": True, "state": "done", "remote": "pk",
             "subpath": "d/" + LONG_CN, "leaf": LONG_CN, "safe_leaf": "x~abc.mp4",
             "dest": "/data", "bytes": 123, "error": ""}
        m.job_live(j)
        vol = m.STATE["volumePairs"][0]
        ent = m.load_name_map(vol)["entries"]["pk:d/" + LONG_CN]
        self.assertEqual(ent["status"], "done")
        self.assertEqual(ent["safe"], "x~abc.mp4")


if __name__ == "__main__":
    unittest.main(verbosity=2)
