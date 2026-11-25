"""v4.00：静态资源缓存策略的纯逻辑测试。

背景：静态资源原来靠 mtime+size 的 ETag 协商缓存，HTML 也参与协商。
升级时若 HTML 命中 304，页面仍引用旧的 `/static/app.js`，必须 Ctrl+F5。
现在 HTML 不缓存，且 HTML 里的 app.js/app.css 带内容指纹参数。
"""
import importlib.util
import os
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(os.path.dirname(HERE), "download-console.py")

_spec = importlib.util.spec_from_file_location("console_under_test_cache", APP)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


class AssetVersionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_dir = m.STATIC_DIR
        m.STATIC_DIR = self.tmp.name
        m._STATIC_VER_CACHE.update(key=None, value="")
        with open(os.path.join(self.tmp.name, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log('v1');")
        with open(os.path.join(self.tmp.name, "app.css"), "w", encoding="utf-8") as f:
            f.write("body{}")

    def tearDown(self):
        m.STATIC_DIR = self.old_dir
        m._STATIC_VER_CACHE.update(key=None, value="")
        self.tmp.cleanup()

    def test_fingerprint_is_stable(self):
        self.assertEqual(m.static_asset_version(), m.static_asset_version())

    def test_fingerprint_changes_when_js_changes(self):
        before = m.static_asset_version()
        with open(os.path.join(self.tmp.name, "app.js"), "w", encoding="utf-8") as f:
            f.write("console.log('v2 with more bytes');")
        self.assertNotEqual(m.static_asset_version(), before)

    def test_fingerprint_changes_when_only_mtime_changes(self):
        # 同大小也要能区分：指纹键包含 mtime_ns，不只是 size
        path = os.path.join(self.tmp.name, "app.css")
        before = m.static_asset_version()
        st = os.stat(path)
        os.utime(path, ns=(st.st_mtime_ns + 1_000_000_000, st.st_mtime_ns + 1_000_000_000))
        self.assertNotEqual(m.static_asset_version(), before)

    def test_missing_asset_still_returns_a_fingerprint(self):
        os.remove(os.path.join(self.tmp.name, "app.js"))
        self.assertTrue(m.static_asset_version())


class HtmlRenderTest(unittest.TestCase):
    def test_placeholder_replaced_with_fingerprint(self):
        old = m.static_asset_version
        m.static_asset_version = lambda: "deadbeef"
        try:
            raw = b'<script src="/static/app.js?v=__CACHE_VER__"></script>'
            out = m.render_static_html(raw)
            self.assertIn(b"?v=deadbeef", out)
            self.assertNotIn(b"__CACHE_VER__", out)
        finally:
            m.static_asset_version = old

    def test_html_without_placeholder_untouched(self):
        raw = b"<html>plain</html>"
        self.assertEqual(m.render_static_html(raw), raw)


class CacheControlTest(unittest.TestCase):
    def test_html_never_cached(self):
        self.assertIn("no-store", m.static_cache_control(".html"))

    def test_app_assets_revalidate(self):
        self.assertEqual(m.static_cache_control(".js"), "no-cache")
        self.assertEqual(m.static_cache_control(".css"), "no-cache")

    def test_other_assets_cached_by_day(self):
        self.assertEqual(m.static_cache_control(".png"), "max-age=86400")


if __name__ == "__main__":
    unittest.main()
