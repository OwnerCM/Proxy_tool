# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     test_check.py
   Description :   helper/check.py 单元测试
   Author :        JHao
   date：          2026/6/15
-------------------------------------------------
     Change Activity:
                     2026/06/15:
-------------------------------------------------
"""
__author__ = 'JHao'

import pytest
from unittest.mock import patch, MagicMock, PropertyMock
from datetime import datetime

from helper.proxy import Proxy
from helper.check import DoValidator, _ThreadChecker


class TestDoValidator:
    """DoValidator.validator 测试"""

    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_http_pass_https_pass(self, mock_pv_cls, mock_conf_cls):
        """HTTP 通过 + HTTPS 通过 -> https=True, fail_count 不变"""
        mock_pv = MagicMock()
        mock_pv.http_validator = [MagicMock(return_value=True)]
        mock_pv.https_validator = [MagicMock(return_value=True)]
        mock_pv_cls.http_validator = mock_pv.http_validator
        mock_pv_cls.https_validator = mock_pv.https_validator

        mock_conf = MagicMock()
        mock_conf.proxyRegion = False
        mock_conf_cls.return_value = mock_conf

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.fail_count = 0

        # Patch DoValidator.conf at class level
        with patch.object(DoValidator, "conf", mock_conf):
            result = DoValidator.validator(proxy, "use")

        assert result.https is True
        assert result.last_status is True
        assert result.check_count == 1
        assert result.fail_count == 0

    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_http_pass_https_fail(self, mock_pv_cls, mock_conf_cls):
        """HTTP 通过 + HTTPS 失败 -> https=False"""
        mock_pv_cls.http_validator = [MagicMock(return_value=True)]
        mock_pv_cls.https_validator = [MagicMock(return_value=False)]

        mock_conf = MagicMock()
        mock_conf.proxyRegion = False

        proxy = Proxy("1.2.3.4:8080", source="test")

        with patch.object(DoValidator, "conf", mock_conf):
            result = DoValidator.validator(proxy, "use")

        assert result.https is False
        assert result.last_status is True

    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_http_fail(self, mock_pv_cls, mock_conf_cls):
        """HTTP 失败 -> fail_count += 1, last_status=False"""
        mock_pv_cls.http_validator = [MagicMock(return_value=False)]

        mock_conf = MagicMock()
        mock_conf.proxyRegion = False

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.fail_count = 0

        with patch.object(DoValidator, "conf", mock_conf):
            result = DoValidator.validator(proxy, "use")

        assert result.last_status is False
        assert result.fail_count == 1

    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_fail_count_decrement(self, mock_pv_cls, mock_conf_cls):
        """HTTP 通过 + fail_count > 0 -> fail_count -= 1"""
        mock_pv_cls.http_validator = [MagicMock(return_value=True)]
        mock_pv_cls.https_validator = [MagicMock(return_value=True)]

        mock_conf = MagicMock()
        mock_conf.proxyRegion = False

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.fail_count = 3

        with patch.object(DoValidator, "conf", mock_conf):
            result = DoValidator.validator(proxy, "use")

        assert result.fail_count == 2

    @patch("helper.check.DoValidator.regionGetter", return_value="US")
    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_raw_sets_region(self, mock_pv_cls, mock_conf_cls, mock_region):
        """work_type='raw' + proxyRegion=True -> regionGetter 被调用"""
        mock_pv_cls.http_validator = [MagicMock(return_value=True)]
        mock_pv_cls.https_validator = [MagicMock(return_value=True)]

        mock_conf = MagicMock()
        mock_conf.proxyRegion = True

        proxy = Proxy("1.2.3.4:8080", source="test")

        with patch.object(DoValidator, "conf", mock_conf):
            result = DoValidator.validator(proxy, "raw")

        assert result.region == "US"
        mock_region.assert_called_once_with(proxy)

    @patch("helper.check.DoValidator.regionGetter")
    @patch("helper.check.ConfigHandler")
    @patch("helper.check.ProxyValidator")
    def test_validator_use_skips_region(self, mock_pv_cls, mock_conf_cls, mock_region):
        """work_type='use' -> 不调用 regionGetter"""
        mock_pv_cls.http_validator = [MagicMock(return_value=True)]
        mock_pv_cls.https_validator = [MagicMock(return_value=True)]

        mock_conf = MagicMock()
        mock_conf.proxyRegion = True

        proxy = Proxy("1.2.3.4:8080", source="test")

        with patch.object(DoValidator, "conf", mock_conf):
            DoValidator.validator(proxy, "use")

        mock_region.assert_not_called()


class TestRegionGetter:
    """DoValidator.regionGetter 测试"""

    @patch("helper.check.WebRequest")
    def test_success_returns_country_code(self, mock_wr_cls):
        """正常返回 -> country_code"""
        mock_wr = MagicMock()
        mock_wr.get.return_value.json = {"country_code": "CN"}
        mock_wr_cls.return_value = mock_wr

        proxy = Proxy("1.2.3.4:8080")
        result = DoValidator.regionGetter(proxy)
        assert result == "CN"

    @patch("helper.check.WebRequest")
    def test_exception_returns_error(self, mock_wr_cls):
        """异常 -> 'error'"""
        mock_wr = MagicMock()
        mock_wr.get.side_effect = Exception("timeout")
        mock_wr_cls.return_value = mock_wr

        proxy = Proxy("1.2.3.4:8080")
        result = DoValidator.regionGetter(proxy)
        assert result == "error"


def _make_checker(work_type, proxy_handler, conf=None):
    """构造手动注入依赖的 _ThreadChecker（绕过 Thread.__init__）"""
    checker = _ThreadChecker.__new__(_ThreadChecker)
    # 手动初始化 Thread 所需的状态
    checker._initialized = True
    checker._name = "test_thread"
    checker._target = None
    checker._args = ()
    checker._kwargs = {}
    checker._daemonic = False
    checker._ident = None
    checker._tstate_lock = None
    checker._started = MagicMock()
    checker._is_stopped = False
    checker._block = MagicMock()
    checker._waiters = []
    checker._stderr = None
    # 注入依赖
    checker.proxy_handler = proxy_handler
    checker.log = MagicMock()
    checker.work_type = work_type
    checker.conf = conf or MagicMock()
    return checker


class TestThreadCheckerIfRaw:
    """_ThreadChecker.__ifRaw 测试"""

    def test_ifraw_new_proxy_gets_put(self):
        """last_status=True, exists=False -> put"""
        mock_ph = MagicMock()
        mock_ph.exists.return_value = False

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = True

        checker = _make_checker("raw", mock_ph)
        checker._ThreadChecker__ifRaw(proxy)
        mock_ph.put.assert_called_once_with(proxy)

    def test_ifraw_existing_proxy_skipped(self):
        """last_status=True, exists=True -> 不 put"""
        mock_ph = MagicMock()
        mock_ph.exists.return_value = True

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = True

        checker = _make_checker("raw", mock_ph)
        checker._ThreadChecker__ifRaw(proxy)
        mock_ph.put.assert_not_called()

    def test_ifraw_failed_proxy_not_put(self):
        """last_status=False -> 不 put"""
        mock_ph = MagicMock()

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = False

        checker = _make_checker("raw", mock_ph)
        checker._ThreadChecker__ifRaw(proxy)
        mock_ph.put.assert_not_called()


class TestThreadCheckerIfUse:
    """_ThreadChecker.__ifUse 测试"""

    def test_ifuse_pass_gets_put(self):
        """last_status=True -> put"""
        mock_ph = MagicMock()

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = True

        checker = _make_checker("use", mock_ph)
        checker._ThreadChecker__ifUse(proxy)
        mock_ph.put.assert_called_once_with(proxy)

    def test_ifuse_fail_exceeds_max_deleted(self):
        """fail_count > maxFailCount -> delete"""
        mock_ph = MagicMock()
        mock_conf = MagicMock()
        mock_conf.maxFailCount = 3

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = False
        proxy.fail_count = 5

        checker = _make_checker("use", mock_ph, mock_conf)
        checker._ThreadChecker__ifUse(proxy)
        mock_ph.delete.assert_called_once_with(proxy)
        mock_ph.put.assert_not_called()

    def test_ifuse_fail_below_max_kept(self):
        """fail_count <= maxFailCount -> put"""
        mock_ph = MagicMock()
        mock_conf = MagicMock()
        mock_conf.maxFailCount = 3

        proxy = Proxy("1.2.3.4:8080", source="test")
        proxy.last_status = False
        proxy.fail_count = 2

        checker = _make_checker("use", mock_ph, mock_conf)
        checker._ThreadChecker__ifUse(proxy)
        mock_ph.put.assert_called_once_with(proxy)
        mock_ph.delete.assert_not_called()
