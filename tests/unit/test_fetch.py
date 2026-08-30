# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     test_fetch.py
   Description :   helper/fetch.py 单元测试
   Author :        JHao
   date：          2026/6/15
-------------------------------------------------
     Change Activity:
                     2026/06/15:
-------------------------------------------------
"""
__author__ = 'JHao'

import os
import sys
import pytest
from unittest.mock import patch, MagicMock

import helper.fetch as fetch_mod
from helper.fetch import _get_sources_dir, _load_module, _discover_fetchers, _ThreadFetcher
from helper.proxy import Proxy
from fetcher.baseFetcher import BaseFetcher


class TestGetSourcesDir:

    def test_returns_correct_path(self):
        """返回 fetcher/sources/ 目录路径"""
        path = _get_sources_dir()
        assert path.endswith(os.path.join("fetcher", "sources"))
        assert os.path.isdir(path)


class TestLoadModule:

    def setup_method(self):
        """每个测试前清空缓存"""
        fetch_mod._module_cache.clear()

    def test_fresh_load(self):
        """缓存为空 -> importlib.import_module"""
        # 使用一个已知存在的模块
        filepath = os.path.join(_get_sources_dir(), "kuaidaili.py")
        result = _load_module("fetcher.sources.kuaidaili", filepath)
        assert result is not None
        assert "fetcher.sources.kuaidaili" in fetch_mod._module_cache

    def test_cache_hit(self):
        """mtime 不变 -> 返回缓存"""
        filepath = os.path.join(_get_sources_dir(), "kuaidaili.py")
        first = _load_module("fetcher.sources.kuaidaili", filepath)
        second = _load_module("fetcher.sources.kuaidaili", filepath)
        assert first is second

    def test_cache_miss_reload(self):
        """mtime 变化 -> importlib.reload"""
        filepath = os.path.join(_get_sources_dir(), "kuaidaili.py")
        first = _load_module("fetcher.sources.kuaidaili", filepath)
        # 模拟 mtime 变化
        fetch_mod._module_cache["fetcher.sources.kuaidaili"] = (0, first)
        second = _load_module("fetcher.sources.kuaidaili", filepath)
        assert second is not None

    @patch("helper.fetch.os.path.getmtime", return_value=0)
    @patch("helper.fetch.importlib")
    def test_import_exception_returns_none(self, mock_importlib, mock_mtime):
        """import 失败 -> 返回 None"""
        mock_importlib.import_module.side_effect = ImportError("not found")
        # 确保模块不在 sys.modules 中，避免走 reload 分支
        mock_importlib.reload.side_effect = ImportError("not found")
        saved = sys.modules.pop("fetcher.sources.nonexistent", None)
        try:
            result = _load_module("fetcher.sources.nonexistent", "/fake/path.py")
            assert result is None
        finally:
            if saved is not None:
                sys.modules["fetcher.sources.nonexistent"] = saved


class TestDiscoverFetchers:

    def setup_method(self):
        fetch_mod._module_cache.clear()

    def test_filters_enabled_only(self):
        """enabled=False 的 fetcher 被排除"""
        # 使用真实扫描，检查结果中所有 fetcher 都是 enabled=True
        fetchers = _discover_fetchers([])
        for f in fetchers:
            assert f.enabled is True

    def test_filters_exclude_list(self):
        """exclude_list 中的被排除"""
        all_fetchers = _discover_fetchers([])
        if not all_fetchers:
            pytest.skip("No fetchers available")
        first_name = all_fetchers[0].__name__
        filtered = _discover_fetchers([first_name])
        filtered_names = [f.__name__ for f in filtered]
        assert first_name not in filtered_names

    def test_returns_sorted_by_name(self):
        """返回结果按 name 排序"""
        fetchers = _discover_fetchers([])
        names = [f.name for f in fetchers]
        assert names == sorted(names)

    def test_prunes_stale_cache(self):
        """已删除文件的缓存被清理"""
        fetch_mod._module_cache["fetcher.sources.deleted_module"] = (0, MagicMock())
        _discover_fetchers([])
        assert "fetcher.sources.deleted_module" not in fetch_mod._module_cache


class TestThreadFetcher:

    def test_collects_proxies(self):
        """fetcher.fetch() yield 代理 -> proxy_dict 有值"""
        mock_cls = MagicMock()
        mock_cls.name = "test_fetcher"
        mock_cls.return_value.fetch.return_value = ["1.2.3.4:8080", "5.6.7.8:443"]

        proxy_dict = {}
        thread = _ThreadFetcher(mock_cls, proxy_dict)
        thread.run()

        assert "1.2.3.4:8080" in proxy_dict
        assert "5.6.7.8:443" in proxy_dict
        assert isinstance(proxy_dict["1.2.3.4:8080"], Proxy)

    def test_merges_duplicate_sources(self):
        """同一代理出现两次 -> add_source 被调用"""
        mock_cls = MagicMock()
        mock_cls.name = "test_fetcher"
        mock_cls.return_value.fetch.return_value = ["1.2.3.4:8080", "1.2.3.4:8080"]

        proxy_dict = {}
        thread = _ThreadFetcher(mock_cls, proxy_dict)
        thread.run()

        assert "1.2.3.4:8080" in proxy_dict
        # source 应该包含两次 "test_fetcher"（add_source 去重，但只出现一次）
        assert "test_fetcher" in proxy_dict["1.2.3.4:8080"].source
