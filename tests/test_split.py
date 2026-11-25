"""v4.01 分账号下载的纯逻辑测试。

背景：单账号被 PikPak 按连接数静默限流，2026-11-23 实测 2.28 GB 只有 42 KB/s。
要做的是「同一份资源转存到多个账号 → 按文件拆开并行下」，因此必须保证三件事：
  1) 拆分**互斥且覆盖全集**（同一个文件不能被两个账号同时写）；
  2) 分配按字节均衡（按文件数均衡会让某个账号早早跑完、另一个还差一大半）；
  3) 只有各账号看到的文件集合完全一致才允许开始，否则分给 A 的文件 B 没有。

这里只覆盖可测的纯函数，不碰网络与 rclone。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_split", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def files(*pairs):
    return [{"Path": p, "Size": s} for p, s in pairs]


class SplitSrcTest(unittest.TestCase):
    def test_src_with_and_without_sub(self):
        self.assertEqual(m.split_src("a", "x/y"), "a:x/y")
        self.assertEqual(m.split_src("a", ""), "a:")
        self.assertEqual(m.split_src("a", "/"), "a:")


class SplitFilterTest(unittest.TestCase):
    def test_no_filter_keeps_all(self):
        rows = files(("a.mp4", 1), ("b.mkv", 2))
        self.assertEqual(m.split_apply_filter(rows, {}), rows)

    def test_min_max_size(self):
        rows = files(("small.mp4", 10), ("mid.mp4", 100), ("big.mp4", 1000))
        out = m.split_apply_filter(rows, {"minSize": "100"} )
        self.assertEqual([x["Path"] for x in out], ["mid.mp4", "big.mp4"])
        out = m.split_apply_filter(rows, {"maxSize": "100"})
        self.assertEqual([x["Path"] for x in out], ["small.mp4", "mid.mp4"])

    def test_skip_empty_uses_one_byte_floor(self):
        rows = files(("empty.mp4", 0), ("ok.mp4", 1))
        out = m.split_apply_filter(rows, {"skipEmpty": True})
        self.assertEqual([x["Path"] for x in out], ["ok.mp4"])

    def test_include_extension_is_case_insensitive_and_basename_scoped(self):
        rows = files(("a.MP4", 1), ("dir/b.mp4", 1), ("c.mkv", 1))
        out = m.split_apply_filter(rows, {"incExt": ["*.mp4"]})
        self.assertEqual([x["Path"] for x in out], ["a.MP4", "dir/b.mp4"])

    def test_exclude_extension_and_keyword(self):
        rows = files(("a.mp4", 1), ("b.srt", 1), ("c.mp4", 1), ("Sample-x.mp4", 1))
        out = m.split_apply_filter(rows, {"excExt": ["*.srt"], "excKw": ["*sample*"]})
        self.assertEqual([x["Path"] for x in out], ["a.mp4", "c.mp4"])

    def test_size_units(self):
        rows = files(("a.mp4", 1024), ("b.mp4", 2048))
        out = m.split_apply_filter(rows, {"minSize": "2k"})
        self.assertEqual([x["Path"] for x in out], ["b.mp4"])


class SplitAssignTest(unittest.TestCase):
    def test_balanced_by_bytes_not_by_count(self):
        rows = files(("big.mp4", 1000), ("a.mp4", 10), ("b.mp4", 10), ("c.mp4", 10))
        buckets, stats = m.split_assign(rows, ["p1", "p2"])
        # 1000 字节的先落 p1，剩下的小文件全给更空的 p2 —— 字节差 970，比「各 2 个」更均衡
        self.assertEqual(stats["p1"]["files"], 1)
        self.assertEqual(stats["p2"]["files"], 3)
        self.assertEqual(stats["p1"]["bytes"] + stats["p2"]["bytes"], 1030)
        self.assertEqual(len(buckets["p1"]) + len(buckets["p2"]), 4)

    def test_every_file_exactly_once(self):
        rows = files(("a.mp4", 5), ("b.mp4", 4), ("c.mp4", 3), ("d.mp4", 2), ("e.mp4", 1))
        buckets, _ = m.split_assign(rows, ["p1", "p2", "p3"])
        got = [p for v in buckets.values() for p in v]
        self.assertEqual(sorted(got), sorted(x["Path"] for x in rows))
        self.assertEqual(len(got), len(set(got)), "同一文件不能分给两个账号")

    def test_deterministic(self):
        rows = files(("a.mp4", 5), ("b.mp4", 5), ("c.mp4", 1))
        a, _ = m.split_assign(rows, ["p1", "p2"])
        b, _ = m.split_assign(rows, ["p1", "p2"])
        self.assertEqual(a, b, "同一批文件每次分配结果必须一致，否则对账会漂移")


class MakeSplitJobsTest(unittest.TestCase):
    """三条不变式：同一个 dest、fileList 互斥、并集等于全部文件。"""

    def _make(self):
        rows = files(("a.mp4", 10), ("b.mp4", 20), ("c.mp4", 30), ("d.mp4", 40))
        buckets, stats = m.split_assign(rows, ["p1", "p2"])
        jobs = m.make_split_jobs(["p1", "p2"], "dir", buckets, stats, "/data/Downloads/x")
        return rows, jobs

    def test_all_shards_share_one_dest(self):
        _rows, jobs = self._make()
        self.assertEqual({j["dest"] for j in jobs}, {"/data/Downloads/x"})

    def test_file_lists_disjoint_and_complete(self):
        rows, jobs = self._make()
        lists = [set(j["fileList"]) for j in jobs]
        self.assertFalse(lists[0] & lists[1], "两片不能有交集，否则两个账号抢写同一个文件")
        self.assertEqual(set().union(*lists), {x["Path"] for x in rows})

    def test_ids_and_split_meta(self):
        _rows, jobs = self._make()
        self.assertEqual(len({j["id"] for j in jobs}), len(jobs))
        self.assertEqual([j["split"]["index"] for j in jobs], [1, 2])
        self.assertTrue(all(j["split"]["of"] == 2 for j in jobs))
        self.assertEqual(sum(j["total"] for j in jobs), 100)

    def test_filter_text_only_no_rclone_filter(self):
        rows = files(("a.mp4", 10))
        buckets, stats = m.split_assign(rows, ["p1"])
        jobs = m.make_split_jobs(["p1"], "dir", buckets, stats, "/d", flt={"incExt": ["*.mp4"]})
        self.assertEqual(jobs[0]["filterText"], "仅 mp4")
        self.assertNotIn("filter", jobs[0], "分片任务不能再带 filter，否则 rclone 会二次过滤")


class SplitPeerRunningTest(unittest.TestCase):
    def test_same_group_peer_unlocks_concurrency(self):
        running = [{"state": "running", "split": {"group": 7, "index": 1}}]
        self.assertTrue(m.split_peer_running({"split": {"group": 7, "index": 2}}, running))

    def test_other_group_or_plain_job_does_not(self):
        running = [{"state": "running", "split": {"group": 8, "index": 1}}]
        self.assertFalse(m.split_peer_running({"split": {"group": 7}}, running))
        self.assertFalse(m.split_peer_running({"remote": "p1", "subpath": "x"}, running))

    def test_peer_must_be_running_not_queued(self):
        running = [{"state": "queued", "split": {"group": 7, "index": 1}}]
        self.assertFalse(m.split_peer_running({"split": {"group": 7, "index": 2}}, running))


class SplitScanTest(unittest.TestCase):
    def setUp(self):
        self._orig = m.split_list_files

    def tearDown(self):
        m.split_list_files = self._orig

    def _patch(self, table):
        def fake(remote, sub, proxy=False):
            v = table.get(remote)
            if isinstance(v, Exception):
                raise v
            if v is None:
                return []
            return files(*v)
        m.split_list_files = fake

    def test_consistent_sets_are_assignable(self):
        self._patch({"p1": [("a.mp4", 10), ("b.mp4", 20)],
                     "p2": [("b.mp4", 20), ("a.mp4", 10)]})
        out = m.split_scan(["p1", "p2"], "dir")
        self.assertTrue(out["consistent"])
        self.assertEqual(out["fileCount"], 2)
        self.assertEqual(out["bytes"], 30)
        self.assertEqual(sum(x["files"] for x in out["assign"]), 2)

    def test_bigger_size_counts_as_mismatch(self):
        # 同名但大小不同 = 不是同一份文件，绝不能当成「有」直接分
        self._patch({"p1": [("a.mp4", 10)], "p2": [("a.mp4", 11)]})
        out = m.split_scan(["p1", "p2"], "dir")
        self.assertFalse(out["consistent"])
        self.assertEqual(out["commonCount"], 0)   # 没有任何「路径+大小」完全相同项
        self.assertEqual(out["fileCount"], 2)     # v4.02 起 fileCount 用并集：两边各算一份

    def test_missing_file_reports_counts_and_blocks(self):
        self._patch({"p1": [("a.mp4", 10), ("b.mp4", 20)],
                     "p2": [("a.mp4", 10), ("c.mp4", 30)]})
        out = m.split_scan(["p1", "p2"], "dir")
        self.assertFalse(out["consistent"])
        self.assertEqual(out["fileCount"], 3)          # 并集：这份资源一共 a/b/c 三个文件
        self.assertEqual(out["commonCount"], 1)        # 只有 a.mp4 双方都有
        rows = {x["remote"]: x for x in out["accounts"]}
        self.assertEqual(rows["p1"]["missing"], 1)     # p1 没有 p2 的 c.mp4
        self.assertEqual(rows["p2"]["missing"], 1)     # p2 没有 p1 的 b.mp4
        self.assertTrue(out["warnings"])

    def test_empty_or_error_account_blocks(self):
        self._patch({"p1": [("a.mp4", 10)], "p2": [], "p3": RuntimeError("captcha")})
        out = m.split_scan(["p1", "p2", "p3"], "dir")
        self.assertFalse(out["consistent"])
        rows = {x["remote"]: x["state"] for x in out["accounts"]}
        self.assertEqual(rows["p2"], "empty")
        self.assertEqual(rows["p3"], "error")

    def test_unlistable_account_missing_is_whole_resource(self):
        """v4.02：列不到目录的账号不是「一个都不缺」，缺的是整份资源。

        旧逻辑对这类账号直接 `continue`，界面上显示成「0 个 · 0 KB 缺 0 / 多 0」，
        看上去像「这个账号什么都不缺」，与红色「集合不一致」自相矛盾。
        """
        self._patch({"p1": [("a.mp4", 10), ("b.mp4", 20)],
                     "p2": RuntimeError("error in ListJSON: directory not found")})
        out = m.split_scan(["p1", "p2"], "dir")
        rows = {x["remote"]: x for x in out["accounts"]}
        self.assertFalse(rows["p2"]["listed"])
        self.assertEqual(rows["p2"]["missing"], 2)
        self.assertEqual(rows["p2"]["extra"], 0)
        self.assertEqual(out["missingDir"], 1)

    def test_unlistable_account_produces_warning(self):
        """能看到的账号彼此完全一致、但有账号看不到目录时，warnings 不能是空的。

        否则界面只有「不一致」一句结论，用户不知道该去转存哪几个账号。
        """
        self._patch({"p1": [("a.mp4", 10)], "p2": [("a.mp4", 10)],
                     "p3": RuntimeError("directory not found")})
        out = m.split_scan(["p1", "p2", "p3"], "dir")
        self.assertFalse(out["consistent"])
        self.assertEqual(out["mismatched"], 0)         # 能看到的两个号完全一致
        self.assertEqual(out["missingDir"], 1)
        self.assertTrue(any("p3" in w and "看不到" in w for w in out["warnings"]))

    def test_filter_applies_before_splitting(self):
        self._patch({"p1": [("a.mp4", 10), ("b.srt", 1)],
                     "p2": [("a.mp4", 10), ("b.srt", 1)]})
        out = m.split_scan(["p1", "p2"], "dir", {"incExt": ["*.mp4"]})
        self.assertTrue(out["consistent"])
        self.assertEqual(out["fileCount"], 1)

    def test_single_account_is_rejected_by_caller_shape(self):
        # split_scan 自身不校验账号数（由 action 层拦），这里确认不会崩
        self._patch({"p1": [("a.mp4", 10)]})
        out = m.split_scan(["p1"], "dir")
        self.assertFalse(out["consistent"])
        self.assertEqual(out["assign"], [])


class ExpandSourceFileListTest(unittest.TestCase):
    """expand_source_files 必须按 fileList 精确裁剪，否则分片会互相抢同一个文件。"""

    def setUp(self):
        self._orig_rc = m.rc
        self._orig_log = m.log_event
        self.events = []

    def tearDown(self):
        m.rc = self._orig_rc
        m.log_event = self._orig_log

    def _patch(self, rows):
        m.rc = lambda method, **kv: {"list": rows}
        m.log_event = lambda job_id, event, **kw: self.events.append((event, kw))

    def test_only_listed_files_survive(self):
        self._patch([{"Path": "a.mp4", "Size": 1}, {"Path": "b.mp4", "Size": 2},
                     {"Path": "sub/c.mp4", "Size": 3}, {"Path": "d", "IsDir": True}])
        job = {"src": "p1:dir", "id": 7, "fileList": ["a.mp4", "sub/c.mp4"]}
        out = m.expand_source_files(job)
        self.assertEqual([x["Path"] for x in out], ["a.mp4", "sub/c.mp4"])

    def test_missing_listed_file_is_logged(self):
        self._patch([{"Path": "a.mp4", "Size": 1}])
        job = {"src": "p1:dir", "id": 7, "fileList": ["a.mp4", "gone.mp4"]}
        out = m.expand_source_files(job)
        self.assertEqual([x["Path"] for x in out], ["a.mp4"])
        self.assertEqual(len(self.events), 1)
        self.assertEqual(self.events[0][0], "split-missing")
        self.assertEqual(self.events[0][1]["count"], 1)

    def test_no_file_list_keeps_old_behavior(self):
        self._patch([{"Path": "a.mp4", "Size": 1}, {"Path": "b.mp4", "Size": 2},
                     {"Path": "d", "IsDir": True}])
        out = m.expand_source_files({"src": "p1:dir", "id": 7})
        self.assertEqual([x["Path"] for x in out], ["a.mp4", "b.mp4"])


if __name__ == "__main__":
    unittest.main()
