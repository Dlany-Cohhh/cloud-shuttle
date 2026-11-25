"""v3.97 一致性校验的两处修正的纯逻辑测试。

背景（2026-11-23 实测，会员号 159 个文件）：
  · 「一致性校验」把 10 个按安全命名截断过的文件同时报成「本地缺失 10 + 本地多余 10」，
    其实云端 159 / 本地 159、总字节 241,953,030,215 完全相同 → ok=false 是误报；
  · 首探 `lsjson --stat` 偶发卡住 60 秒（重跑同命令 1.7 秒返回），直接报「云端连接失败」。

这里只覆盖可测的纯函数：改名映射构造、按字节数配对、路径归一化。
"""
import importlib.util
import os
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_verify", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class PairRenamesBySizeTest(unittest.TestCase):
    def _it(self, path, status, csize=None, lsize=None):
        return {"path": path, "status": status, "cloudSize": csize, "localSize": lsize, "note": ""}

    def test_unique_size_pair_becomes_renamed(self):
        items = [self._it("long_cloud_name.mp4", "missing", 4096),
                 self._it("long_clou~abc123.mp4", "extra", None, 4096)]
        self.assertEqual(m.pair_renames_by_size(items), 1)
        self.assertEqual(items[0]["status"], "renamed")
        self.assertEqual(items[0]["renameSource"], "size")
        self.assertEqual(items[0]["localPath"], "long_clou~abc123.mp4")
        self.assertEqual(items[0]["localSize"], 4096)
        self.assertTrue(items[1].get("_merged"), "本地那条应被标记合并，避免重复展示")

    def test_ambiguous_size_not_paired(self):
        # 同一大小 2 个缺失 + 1 个多余：无法确定谁是谁，保持原状
        items = [self._it("a.mp4", "missing", 100), self._it("b.mp4", "missing", 100),
                 self._it("c.mp4", "extra", None, 100)]
        self.assertEqual(m.pair_renames_by_size(items), 0)

    def test_two_extras_one_missing_not_paired(self):
        items = [self._it("a.mp4", "missing", 100),
                 self._it("b.mp4", "extra", None, 100), self._it("c.mp4", "extra", None, 100)]
        self.assertEqual(m.pair_renames_by_size(items), 0)

    def test_different_sizes_not_paired(self):
        items = [self._it("a.mp4", "missing", 100), self._it("b.mp4", "extra", None, 101)]
        self.assertEqual(m.pair_renames_by_size(items), 0)
        self.assertEqual(items[0]["status"], "missing")

    def test_none_size_never_paired(self):
        items = [self._it("a.mp4", "missing", None), self._it("b.mp4", "extra", None, None)]
        self.assertEqual(m.pair_renames_by_size(items), 0)

    def test_multiple_independent_pairs(self):
        items = [self._it("a1", "missing", 10), self._it("a2", "missing", 20),
                 self._it("b1", "extra", None, 10), self._it("b2", "extra", None, 20)]
        self.assertEqual(m.pair_renames_by_size(items), 2)
        self.assertEqual([i["status"] for i in items],
                         ["renamed", "renamed", "extra", "extra"])
        self.assertTrue(items[2]["_merged"] and items[3]["_merged"])

    def test_match_and_size_untouched(self):
        items = [self._it("x", "match", 10, 10), self._it("y", "size", 10, 9)]
        self.assertEqual(m.pair_renames_by_size(items), 0)
        self.assertEqual([i["status"] for i in items], ["match", "size"])


class RenameMapForTest(unittest.TestCase):
    """rename_map_for 读的是安全命名清单，这里用假清单替换掉 IO。"""

    def setUp(self):
        self.old_vr = m._local_volume_root
        self.old_map = m.load_name_map
        m._local_volume_root = lambda _p: "/vol2/download"
        self.seen_vol = []

        def fake_load(vol):
            self.seen_vol.append(vol)
            return {"entries": {
                "会员号:Pack From Shared/绿帽学院/超长的中文文件名.mp4":
                    {"safe": "超长的中文文件名~a1b2c3.mp4"},
                "会员号:Pack From Shared/绿帽学院/正常名.mp4": {"safe": "正常名.mp4"},
                "会员号:其他目录/无关.mp4": {"safe": "无关~zz.mp4"},
                "别的号:Pack From Shared/绿帽学院/别人的.mp4": {"safe": "别人的~qq.mp4"},
            }}
        m.load_name_map = fake_load

    def tearDown(self):
        m._local_volume_root, m.load_name_map = self.old_vr, self.old_map

    def test_maps_truncated_name(self):
        r = m.rename_map_for("会员号", "Pack From Shared/绿帽学院", "/vol2/download/Downloads/绿帽学院")
        self.assertEqual(r.get("超长的中文文件名.mp4"), "超长的中文文件名~a1b2c3.mp4")

    def test_volume_dict_passed_to_loader(self):
        # _name_map_path 只认 {"console": <卷根>}，传字符串会静默拿不到映射（v3.97 修的就是这个）
        m.rename_map_for("会员号", "Pack From Shared/绿帽学院", "/vol2/download/Downloads/绿帽学院")
        self.assertEqual(self.seen_vol[0], {"console": "/vol2/download"})

    def test_identity_name_not_included(self):
        r = m.rename_map_for("会员号", "Pack From Shared/绿帽学院", "/vol2/download/Downloads/绿帽学院")
        self.assertNotIn("正常名.mp4", r)

    def test_other_dir_excluded(self):
        r = m.rename_map_for("会员号", "Pack From Shared/绿帽学院", "/vol2/download/Downloads/绿帽学院")
        self.assertNotIn("无关.mp4", r)

    def test_other_account_excluded(self):
        r = m.rename_map_for("会员号", "Pack From Shared/绿帽学院", "/vol2/download/Downloads/绿帽学院")
        self.assertNotIn("别人的.mp4", r)

    def test_broken_map_returns_empty(self):
        m.load_name_map = lambda _v: None
        self.assertEqual(m.rename_map_for("会员号", "x", "/vol2/download"), {})


class VerifyRenameSourceTest(unittest.TestCase):
    """v3.99：renamed 必须区分「清单确认」和「仅按字节数推断」。"""

    def test_size_pair_source_is_recorded(self):
        items = [{"path": "a.mp4", "status": "missing", "cloudSize": 10, "localSize": None, "note": ""},
                 {"path": "b.mp4", "status": "extra", "cloudSize": None, "localSize": 10, "note": ""}]
        m.pair_renames_by_size(items)
        self.assertEqual(items[0]["renameSource"], "size")
        self.assertIn("未列入安全命名清单", items[0]["note"])

    def test_counts_split_by_evidence(self):
        items = [
            {"status": "renamed", "renameSource": "name-map"},
            {"status": "renamed", "renameSource": "size"},
            {"status": "renamed", "renameSource": "size"},
            {"status": "match"},
        ]
        self.assertEqual(m.verify_rename_counts(items), {"byMap": 1, "bySize": 2})

    def test_counts_treat_missing_source_as_map(self):
        # 老结果/外部数据没有 renameSource 时按清单确认处理，不误报为待确认
        items = [{"status": "renamed"}, {"status": "missing"}]
        self.assertEqual(m.verify_rename_counts(items), {"byMap": 1, "bySize": 0})


class VerifyCompareRenameSourceTest(unittest.TestCase):
    """verify_compare 端到端：清单确认 + 字节数推断同时出现时的汇总。"""

    def setUp(self):
        self.old = (m.verify_cloud, m.verify_local, m.rename_map_for)
        m.verify_cloud = lambda remote, path: {
            "error": "", "attempts": 1, "truncated": False,
            "scope": "dir", "target": "acc:dir",
            "files": {"长名.mp4": 100, "普通.mp4": 200, "历史.mp4": 300},
            "targets": {"长名.mp4": "acc:dir/长名.mp4", "普通.mp4": "acc:dir/普通.mp4",
                        "历史.mp4": "acc:dir/历史.mp4"},
        }
        m.verify_local = lambda rel: (
            {"长名~abc.mp4": {"size": 100, "abs": "/tmp/长名~abc.mp4"},
             "历史~old.mp4": {"size": 300, "abs": "/tmp/历史~old.mp4"}},
            "dir", "/tmp/dir", False, "")
        m.rename_map_for = lambda remote, cloud_path, local_root: {"长名.mp4": "长名~abc.mp4"}

    def tearDown(self):
        m.verify_cloud, m.verify_local, m.rename_map_for = self.old

    def test_summary_and_items_keep_both_sources(self):
        r = m.verify_compare("acc", "dir", "dir", "quick", 6)
        self.assertFalse(r["error"])
        s = r["summary"]
        self.assertEqual(s["renamed"], 2)
        self.assertEqual(s["renamedByMap"], 1)
        self.assertEqual(s["renamedBySize"], 1)
        self.assertEqual(s["missing"], 1)
        src = {it["path"]: it.get("renameSource") for it in r["items"]}
        self.assertEqual(src["长名.mp4"], "name-map")
        self.assertEqual(src["历史.mp4"], "size")
        self.assertTrue(any("仅按字节数" in w for w in r["warnings"]))

    def test_inferred_only_still_ok(self):
        # 只有 size 推断时仍按 v3.97 口径视为一致（ok=True），但不掩盖待确认项
        m.verify_cloud = lambda remote, path: {
            "error": "", "attempts": 1, "truncated": False,
            "scope": "dir", "target": "acc:dir",
            "files": {"历史.mp4": 300},
            "targets": {"历史.mp4": "acc:dir/历史.mp4"},
        }
        m.verify_local = lambda rel: (
            {"历史~old.mp4": {"size": 300, "abs": "/tmp/历史~old.mp4"}},
            "dir", "/tmp/dir", False, "")
        r = m.verify_compare("acc", "dir", "dir", "quick", 6)
        self.assertTrue(r["ok"])
        self.assertEqual(r["summary"]["renamedBySize"], 1)
        self.assertEqual(r["summary"]["renamedByMap"], 0)


if __name__ == "__main__":
    unittest.main()
