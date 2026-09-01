# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     setting.py
   Description :   配置文件
   Author :        JHao
   date：          2019/2/15
-------------------------------------------------
   Change Activity:
                   2019/2/15:
-------------------------------------------------
"""

BANNER = r"""
****************************************************************
*** ______  ********************* ______ *********** _  ********
*** | ___ \_ ******************** | ___ \ ********* | | ********
*** | |_/ / \__ __   __  _ __   _ | |_/ /___ * ___  | | ********
*** |  __/|  _// _ \ \ \/ /| | | ||  __// _ \ / _ \ | | ********
*** | |   | | | (_) | >  < \ |_| || |  | (_) | (_) || |___  ****
*** \_|   |_|  \___/ /_/\_\ \__  |\_|   \___/ \___/ \_____/ ****
****                       __ / /                          *****
************************* /___ / *******************************
*************************       ********************************
****************************************************************
"""

VERSION = "2.4.0"

# ############### server config ###############
HOST = "0.0.0.0"

PORT = 5010

# ############### database config ###################
# db connection uri
# example:
#      Redis: redis://:password@ip:port/db
#      Ssdb:  ssdb://:password@ip:port
DB_CONN = 'redis://:pwdstring@127.0.0.1:6379/0'

# proxy table name
TABLE_NAME = 'use_proxy'


# ###### config the proxy fetch function ######
# 自动扫描 fetcher/sources/ 目录，加载所有 enabled=True 的 fetcher
# 如需临时禁用某个 fetcher，在下方黑名单中添加类名（不改源文件）
PROXY_FETCHER_EXCLUDE = []

# ############# proxy validator #################
# 代理验证目标网站
HTTP_URL = "http://httpbin.org"

HTTPS_URL = "https://www.qq.com"

# 代理验证时超时时间
# 上游默认 10s。调小能显著降低 CPU：死代理占用线程的时间减半,
# 单轮校验更容易在调度间隔内跑完, 不会堆叠成任务积压
VERIFY_TIMEOUT = 5

# 近PROXY_CHECK_COUNT次校验中允许的最大失败次数,超过则剔除代理
MAX_FAIL_COUNT = 0

# 近PROXY_CHECK_COUNT次校验中允许的最大失败率,超过则剔除代理
# MAX_FAIL_RATE = 0.1

# proxyCheck时代理数量少于POOL_SIZE_MIN触发抓取
POOL_SIZE_MIN = 20

# ############# 资源占用相关 #################
# 以下几项决定了这个系统的 CPU 占用。默认值偏"省电"——代理池的实时性通常不重要,
# 晚几分钟发现一个代理失效没有影响, 但每一轮校验都是实打实的 CPU
# (每个代理要做一次 HTTP 请求 + 一次 HTTPS 的 TLS 握手)。
#
# 想让池子更新更快就调小间隔/调大线程数, 反之调大间隔/调小线程数。
# 判断依据看日志: 若 "ProxyCheck ... complete" 迟迟不出现, 说明一轮没跑完。

# 校验线程数(上游写死 20)。同时进行的 TLS 握手数量, 是 CPU 的主要来源
PROXY_CHECK_THREADS = 5

# 采集间隔(分钟): 拉取所有代理源并校验新代理。上游默认 5
PROXY_FETCH_INTERVAL = 30

# 复检间隔(分钟): 全量重新校验池中已有代理。上游默认 2
PROXY_CHECK_INTERVAL = 15

# API 的 gunicorn worker 数(上游写死 4)。这个 API 只供人工查看和外部调用,
# 展示层与网关都是直读 Redis 的, 不经过它, 所以 1 个够用
SERVER_WORKERS = 1

# 日志级别: DEBUG/INFO/WARNING/ERROR。上游默认 DEBUG, 每个代理都会打好几行
LOG_LEVEL = "INFO"

# 是否额外写日志文件。容器里 stdout 已被 docker 收集, 再写文件只是重复 I/O
LOG_TO_FILE = False

# ############# proxy attributes #################
# 是否启用代理地域属性。
# 上游默认 True, 实现是给**每个**新代理发一次 https://api.ip.sb/geoip/ 请求,
# 只为拿一个国家代码。本项目关掉它: 展示层(web/web.py)在 region 为空时会用
# 容器内的离线库 web/data/ipdb.bin 解析, 粒度完全相同(都只到国家),
# 但不需要任何网络请求。开着纯属浪费 CPU 和外部 API 配额。
PROXY_REGION = False

# ############# scheduler config #################

# Set the timezone for the scheduler forcely (optional)
# If it is running on a VM, and
#   "ValueError: Timezone offset does not match system offset"
#   was raised during scheduling.
# Please uncomment the following line and set a timezone for the scheduler.
# Otherwise it will detect the timezone from the system automatically.

TIMEZONE = "Asia/Shanghai"
