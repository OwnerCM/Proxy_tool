# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     fetch.py
   Description :   代理采集
   Author :        JHao
   date：          2019/8/6
-------------------------------------------------
   Change Activity:
                   2019/08/06: 多线程采集
                   2026/05/31: 重构为动态加载 fetcher 插件
-------------------------------------------------
"""
__author__ = 'JHao'

import os
import sys
import importlib
from queue import Queue, Empty
from threading import Thread, Lock

from helper.proxy import Proxy
from helper.check import DoValidator
from handler.logHandler import LogHandler
from handler.configHandler import ConfigHandler
from fetcher.baseFetcher import BaseFetcher

_logger = LogHandler("fetch")

# 模块缓存: {module_name: (mtime, module)}
_module_cache = {}


def _get_sources_dir():
    return os.path.join(
        os.path.dirname(os.path.abspath(__file__)), '..', 'fetcher', 'sources')


def _load_module(module_name, filepath):
    """加载或 reload 模块，仅在文件 mtime 变化时 reload"""
    global _module_cache
    mtime = os.path.getmtime(filepath)
    cached = _module_cache.get(module_name)
    if cached and cached[0] == mtime:
        return cached[1]
    try:
        if module_name in sys.modules:
            module = importlib.reload(sys.modules[module_name])
        else:
            module = importlib.import_module(module_name)
        _module_cache[module_name] = (mtime, module)
        return module
    except Exception as e:
        _logger.warning("ProxyFetch : load %s error - %s" % (module_name, e))
        return None


def _discover_fetchers(exclude_list):
    """
    自动扫描 sources/ 目录，返回所有 enabled=True 且不在黑名单中的 fetcher 类列表。
    仅在文件 mtime 变化时重新加载模块，支持运行时热更新。
    """
    global _module_cache
    sources_dir = _get_sources_dir()
    fetcher_classes = []
    seen_modules = set()

    for filename in os.listdir(sources_dir):
        if not filename.endswith('.py') or filename.startswith('_'):
            continue
        module_name = "fetcher.sources.%s" % filename[:-3]
        seen_modules.add(module_name)
        filepath = os.path.join(sources_dir, filename)
        module = _load_module(module_name, filepath)
        if module is None:
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name, None)
            if (attr and isinstance(attr, type)
                    and issubclass(attr, BaseFetcher)
                    and attr is not BaseFetcher
                    and attr.name
                    and attr.enabled
                    and attr.__name__ not in exclude_list):
                fetcher_classes.append(attr)

    # 清理已删除文件的缓存
    for name in list(_module_cache):
        if name not in seen_modules:
            del _module_cache[name]

    return sorted(fetcher_classes, key=lambda c: c.name)


class _ThreadFetcher(Thread):
    """从队列里逐个取采集源来跑, 直到队列取空。

    上游是"一个采集源一个线程, 然后全部同时 start", 24 个源就意味着每
    PROXY_FETCH_INTERVAL 分钟出现一次 24 路并发 HTTP 下载 + lxml 解析的爆发,
    这是 CPU 尖刺的主要来源。改成固定数量的 worker 从队列取任务后, 瞬时并发被
    压到 PROXY_FETCH_THREADS, 线程数也从 24 降到 5(顺带省掉 19 个线程栈)。
    一轮总耗时会略长, 但采集本来就不需要抢时间。
    """

    def __init__(self, task_queue, proxy_dict, dict_lock):
        Thread.__init__(self)
        self.task_queue = task_queue
        self.proxy_dict = proxy_dict
        self.dict_lock = dict_lock
        self.log = LogHandler("fetcher")

    def run(self):
        while True:
            try:
                fetcher_class = self.task_queue.get_nowait()
            except Empty:
                return
            self.__fetch_one(fetcher_class)

    def __fetch_one(self, fetcher_class):
        fetcher_name = fetcher_class.name
        self.log.info("ProxyFetch - {func}: start".format(func=fetcher_name))
        try:
            for proxy in fetcher_class().fetch():
                self.log.info('ProxyFetch - %s: %s ok' % (fetcher_name, proxy.ljust(23)))
                proxy = proxy.strip()
                # "先判断再写入"在多线程下是竞态(上游同样有, 后果只是丢掉一次来源
                # 标注), 这里加锁消掉它 —— 锁的粒度极小, 无实际竞争开销
                with self.dict_lock:
                    if proxy in self.proxy_dict:
                        self.proxy_dict[proxy].add_source(fetcher_name)
                    else:
                        self.proxy_dict[proxy] = Proxy(proxy, source=fetcher_name)
        except Exception as e:
            self.log.error("ProxyFetch - {func}: error".format(func=fetcher_name))
            self.log.error(str(e))


class Fetcher(object):
    name = "fetcher"

    def __init__(self):
        self.log = LogHandler(self.name)
        self.conf = ConfigHandler()

    def run(self):
        """
        fetch proxy with fetcher plugins
        :return:
        """
        proxy_dict = dict()
        dict_lock = Lock()
        thread_list = list()
        self.log.info("ProxyFetch : start")

        exclude_list = self.conf.fetcherExclude
        fetcher_classes = _discover_fetchers(exclude_list)
        self.log.info("ProxyFetch : active fetchers [%s]" % ", ".join(c.name for c in fetcher_classes))

        task_queue = Queue()
        for fetcher_class in fetcher_classes:
            task_queue.put(fetcher_class)

        # worker 数不超过采集源数, 避免起一堆立刻退出的空线程
        worker_count = max(1, min(self.conf.proxyFetchThreads, len(fetcher_classes)))
        self.log.info("ProxyFetch : %s fetchers with %s threads"
                      % (len(fetcher_classes), worker_count))

        for _ in range(worker_count):
            thread_list.append(_ThreadFetcher(task_queue, proxy_dict, dict_lock))

        for thread in thread_list:
            thread.setDaemon(True)
            thread.start()

        for thread in thread_list:
            thread.join()

        self.log.info("ProxyFetch - all complete!")
        for _ in proxy_dict.values():
            if DoValidator.preValidator(_.proxy):
                yield _
