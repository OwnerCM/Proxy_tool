# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     proxyScheduler
   Description :
   Author :        JHao
   date：          2019/8/5
-------------------------------------------------
   Change Activity:
                   2019/08/05: proxyScheduler
                   2021/02/23: runProxyCheck时,剩余代理少于POOL_SIZE_MIN时执行抓取
-------------------------------------------------
"""
__author__ = 'JHao'

from apscheduler.schedulers.blocking import BlockingScheduler

from util.six import Queue
from helper.fetch import Fetcher
from helper.check import Checker
from handler.logHandler import LogHandler
from handler.proxyHandler import ProxyHandler
from handler.configHandler import ConfigHandler


def __runProxyFetch():
    proxy_queue = Queue()
    proxy_fetcher = Fetcher()

    for proxy in proxy_fetcher.run():
        proxy_queue.put(proxy)

    Checker("raw", proxy_queue)


def __runProxyCheck():
    proxy_handler = ProxyHandler()
    proxy_queue = Queue()
    if proxy_handler.db.getCount().get("total", 0) < proxy_handler.conf.poolSizeMin:
        __runProxyFetch()
    for proxy in proxy_handler.getAll():
        proxy_queue.put(proxy)
    Checker("use", proxy_queue)


def runScheduler():
    __runProxyFetch()

    timezone = ConfigHandler().timezone
    scheduler_log = LogHandler("scheduler")
    scheduler = BlockingScheduler(logger=scheduler_log, timezone=timezone)

    conf = ConfigHandler()
    scheduler.add_job(__runProxyFetch, 'interval', minutes=conf.proxyFetchInterval,
                      id="proxy_fetch", name="proxy采集")
    scheduler.add_job(__runProxyCheck, 'interval', minutes=conf.proxyCheckInterval,
                      id="proxy_check", name="proxy检查")

    # 只有两个 job，20 个执行线程没有意义（上游写死 20）
    executors = {'default': {'type': 'threadpool', 'max_workers': 2}}
    # 上游这里是 coalesce=False + max_instances=10，是本系统 CPU 飙高的主要放大器：
    # 一轮校验只要没在间隔内跑完，APScheduler 就再起一个实例，最多叠到 10 层，
    # 而每一层又会开 PROXY_CHECK_THREADS 个线程去做 TLS 握手。
    # 改成 max_instances=1 后跑不完就跳过本轮；coalesce=True 让积压的多次触发
    # 合并为一次。跳过时 APScheduler 会打一条 warning，正好是"跟不上"的信号。
    job_defaults = {
        'coalesce': True,
        'max_instances': 1,
    }
    # 上游还声明了 processpool 执行器（ProcessPoolExecutor(max_workers=5)），
    # 但没有任何 job 指定用它 —— 死配置，已删除。

    scheduler.configure(executors=executors, job_defaults=job_defaults, timezone=timezone)

    scheduler.start()


if __name__ == '__main__':
    runScheduler()
