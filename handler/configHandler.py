# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     configHandler
   Description :
   Author :        JHao
   date：          2020/6/22
-------------------------------------------------
   Change Activity:
                   2020/6/22:
-------------------------------------------------
"""
__author__ = 'JHao'

import os
import setting
from util.singleton import Singleton
from util.lazyProperty import LazyProperty
from util.six import reload_six, withMetaclass


class ConfigHandler(withMetaclass(Singleton)):

    def __init__(self):
        pass

    @LazyProperty
    def serverHost(self):
        return os.environ.get("HOST", setting.HOST)

    @LazyProperty
    def serverPort(self):
        return os.environ.get("PORT", setting.PORT)

    @LazyProperty
    def dbConn(self):
        return os.getenv("DB_CONN", setting.DB_CONN)

    @LazyProperty
    def tableName(self):
        return os.getenv("TABLE_NAME", setting.TABLE_NAME)

    @property
    def fetcherExclude(self):
        reload_six(setting)
        return getattr(setting, 'PROXY_FETCHER_EXCLUDE', [])

    @LazyProperty
    def httpUrl(self):
        return os.getenv("HTTP_URL", setting.HTTP_URL)

    @LazyProperty
    def httpsUrl(self):
        return os.getenv("HTTPS_URL", setting.HTTPS_URL)

    @LazyProperty
    def verifyTimeout(self):
        return int(os.getenv("VERIFY_TIMEOUT", setting.VERIFY_TIMEOUT))

    # @LazyProperty
    # def proxyCheckCount(self):
    #     return int(os.getenv("PROXY_CHECK_COUNT", setting.PROXY_CHECK_COUNT))

    @LazyProperty
    def maxFailCount(self):
        return int(os.getenv("MAX_FAIL_COUNT", setting.MAX_FAIL_COUNT))

    # @LazyProperty
    # def maxFailRate(self):
    #     return int(os.getenv("MAX_FAIL_RATE", setting.MAX_FAIL_RATE))

    @LazyProperty
    def poolSizeMin(self):
        return int(os.getenv("POOL_SIZE_MIN", setting.POOL_SIZE_MIN))

    @LazyProperty
    def proxyCheckThreads(self):
        return int(os.getenv("PROXY_CHECK_THREADS",
                             getattr(setting, "PROXY_CHECK_THREADS", 5)))

    @LazyProperty
    def proxyCheckHttps(self):
        raw = os.getenv("PROXY_CHECK_HTTPS")
        if raw is None:
            return bool(getattr(setting, "PROXY_CHECK_HTTPS", True))
        return raw.strip().lower() not in ("0", "false", "no", "off", "")

    @LazyProperty
    def proxyFetchThreads(self):
        return int(os.getenv("PROXY_FETCH_THREADS",
                             getattr(setting, "PROXY_FETCH_THREADS", 5)))

    @LazyProperty
    def proxyFetchInterval(self):
        return int(os.getenv("PROXY_FETCH_INTERVAL",
                             getattr(setting, "PROXY_FETCH_INTERVAL", 30)))

    @LazyProperty
    def proxyCheckInterval(self):
        return int(os.getenv("PROXY_CHECK_INTERVAL",
                             getattr(setting, "PROXY_CHECK_INTERVAL", 15)))

    @LazyProperty
    def serverWorkers(self):
        return int(os.getenv("SERVER_WORKERS",
                             getattr(setting, "SERVER_WORKERS", 1)))

    @LazyProperty
    def logLevel(self):
        return os.getenv("LOG_LEVEL", getattr(setting, "LOG_LEVEL", "INFO")).upper()

    @LazyProperty
    def logToFile(self):
        raw = os.getenv("LOG_TO_FILE")
        if raw is None:
            return bool(getattr(setting, "LOG_TO_FILE", False))
        return raw.strip().lower() not in ("0", "false", "no", "off", "")

    @LazyProperty
    def proxyRegion(self):
        # 上游这里是 bool(os.getenv(...))，而 bool("0") 和 bool("false") 都是 True，
        # 意味着这个开关**用环境变量根本关不掉**，只能改 setting.py。
        # 本项目靠关掉它来省掉每个新代理一次 api.ip.sb 请求，所以必须能用 env 控制。
        raw = os.getenv("PROXY_REGION")
        if raw is None:
            return bool(setting.PROXY_REGION)
        return raw.strip().lower() not in ("0", "false", "no", "off", "")

    @LazyProperty
    def timezone(self):
        return os.getenv("TIMEZONE", setting.TIMEZONE)

