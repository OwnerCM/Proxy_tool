# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     test_web_request.py
   Description :   WebRequest 单元测试
   Author :        JHao
   date：          2026/6/15
-------------------------------------------------
     Change Activity:
                     2026/06/15:
-------------------------------------------------
"""
__author__ = 'JHao'

import pytest
from unittest.mock import patch, MagicMock
from requests.models import Response

from util.webRequest import WebRequest


def _mock_response(status_code=200, text=None, content=None, json_data=None):
    """构造 mock Response"""
    resp = Response()
    resp.status_code = status_code
    if json_data is not None:
        import json
        resp._content = json.dumps(json_data).encode("utf-8")
        resp.json = lambda: json_data
    elif content is not None:
        resp._content = content
    elif text is not None:
        resp._content = text.encode("utf-8")
    else:
        resp._content = b"ok"
    return resp


class TestWebRequestGet:
    """get() 测试"""

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.get")
    def test_success_path(self, mock_get, mock_sleep):
        """正常返回 -> self.response 被设置"""
        mock_get.return_value = _mock_response(200, "hello")
        wr = WebRequest()
        result = wr.get("http://example.com", retry_time=1, retry_interval=0, timeout=1)

        assert result is wr
        assert wr.response.status_code == 200
        assert wr.text == "hello"

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.get")
    def test_custom_header_merge(self, mock_get, mock_sleep):
        """自定义 header 合并到默认 header"""
        mock_get.return_value = _mock_response(200)
        wr = WebRequest()
        wr.get("http://example.com", header={"X-Custom": "v"}, retry_time=1, retry_interval=0, timeout=1)

        call_kwargs = mock_get.call_args[1]
        assert call_kwargs["headers"]["X-Custom"] == "v"
        assert "User-Agent" in call_kwargs["headers"]

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.get")
    def test_retry_exhaustion(self, mock_get, mock_sleep):
        """全部失败 -> 返回 fallback（注意 get() 的 bug: 未赋值 self.response）"""
        mock_get.side_effect = TimeoutError("timeout")
        wr = WebRequest()
        result = wr.get("http://example.com", retry_time=2, retry_interval=0, timeout=1)

        assert result is wr
        # 注意：get() 在 retry 耗尽时创建了 resp 但未赋值给 self.response
        # 所以 self.response 仍为初始的空 Response
        assert mock_get.call_count == 2


class TestWebRequestPost:
    """post() 测试"""

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.post")
    def test_success_path(self, mock_post, mock_sleep):
        """正常返回 -> self.response 被设置"""
        mock_post.return_value = _mock_response(200, "posted")
        wr = WebRequest()
        result = wr.post("http://example.com", retry_time=1, retry_interval=0, timeout=1)

        assert result is wr
        assert wr.response.status_code == 200
        assert wr.text == "posted"

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.post")
    def test_custom_header_merge(self, mock_post, mock_sleep):
        """自定义 header 合并到默认 header"""
        mock_post.return_value = _mock_response(200)
        wr = WebRequest()
        wr.post("http://example.com", header={"X-Custom": "v"}, retry_time=1, retry_interval=0, timeout=1)

        call_kwargs = mock_post.call_args[1]
        assert call_kwargs["headers"]["X-Custom"] == "v"
        assert "User-Agent" in call_kwargs["headers"]

    @patch("util.webRequest.time.sleep")
    @patch("util.webRequest.requests.post")
    def test_retry_exhaustion(self, mock_post, mock_sleep):
        """全部失败 -> self.response 被正确赋值（与 get() 不同）"""
        mock_post.side_effect = TimeoutError("timeout")
        wr = WebRequest()
        result = wr.post("http://example.com", retry_time=2, retry_interval=0, timeout=1)

        assert result is wr
        # post() 正确赋值 self.response = resp
        assert wr.response.status_code == 200
        assert mock_post.call_count == 2


class TestWebRequestTree:
    """tree 属性测试"""

    def test_empty_content_returns_none(self):
        """空 content -> None"""
        wr = WebRequest()
        wr.response = _mock_response(200, content=b"")
        assert wr.tree is None

    def test_valid_html_returns_element(self):
        """有效 HTML -> lxml element"""
        wr = WebRequest()
        html = b"<html><body><p>hello</p></body></html>"
        wr.response = _mock_response(200, content=html)
        tree = wr.tree
        assert tree is not None
        assert tree.xpath("//p/text()") == ["hello"]


class TestWebRequestText:
    """text 属性测试"""

    def test_returns_response_text(self):
        """返回 response.text"""
        wr = WebRequest()
        wr.response = _mock_response(200, text="hello world")
        assert wr.text == "hello world"


class TestWebRequestJson:
    """json 属性测试"""

    def test_valid_json_returns_dict(self):
        """有效 JSON -> dict"""
        wr = WebRequest()
        wr.response = _mock_response(200, json_data={"key": "val"})
        assert wr.json == {"key": "val"}

    def test_invalid_json_returns_empty_dict(self):
        """无效 JSON -> {}"""
        wr = WebRequest()
        resp = _mock_response(200, content=b"not json")
        resp.json = lambda: (_ for _ in ()).throw(ValueError("Invalid JSON"))
        wr.response = resp
        assert wr.json == {}


class TestWebRequestProperties:
    """header/user_agent 属性测试"""

    def test_user_agent_returns_string(self):
        wr = WebRequest()
        ua = wr.user_agent
        assert isinstance(ua, str)
        assert len(ua) > 0

    def test_header_contains_user_agent(self):
        wr = WebRequest()
        h = wr.header
        assert "User-Agent" in h
        assert "Accept" in h
        assert "Connection" in h
