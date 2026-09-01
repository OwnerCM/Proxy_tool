# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     LogHandler.py
   Description :  日志操作模块
   Author :       JHao
   date：          2017/3/6
-------------------------------------------------
   Change Activity:
                   2017/03/06: log handler
                   2017/09/21: 屏幕输出/文件输出 可选(默认屏幕和文件均输出)
                   2020/07/13: Windows下TimedRotatingFileHandler线程不安全, 不再使用
-------------------------------------------------
"""
__author__ = 'JHao'

import os
import logging
import platform

from logging.handlers import TimedRotatingFileHandler

# 日志级别
CRITICAL = 50
FATAL = CRITICAL
ERROR = 40
WARNING = 30
WARN = WARNING
INFO = 20
DEBUG = 10
NOTSET = 0

_LEVELS = {"CRITICAL": CRITICAL, "FATAL": FATAL, "ERROR": ERROR,
           "WARNING": WARNING, "WARN": WARN, "INFO": INFO,
           "DEBUG": DEBUG, "NOTSET": NOTSET}

CURRENT_PATH = os.path.dirname(os.path.abspath(__file__))
ROOT_PATH = os.path.join(CURRENT_PATH, os.pardir)
LOG_PATH = os.path.join(ROOT_PATH, 'log')


def _conf():
    # 延迟导入：configHandler 会 import setting，而 setting 不依赖本模块，
    # 但放在模块顶层导入会与 handler 包的初始化顺序打结
    from handler.configHandler import ConfigHandler
    return ConfigHandler()


def _ensure_log_dir():
    if not os.path.exists(LOG_PATH):
        try:
            os.mkdir(LOG_PATH)
        except (FileExistsError, OSError):
            pass


class LogHandler(logging.Logger):
    """
    LogHandler
    """

    def __init__(self, name, level=None, stream=True, file=None):
        # 级别与是否写文件改为可配（上游写死 DEBUG + 同时写文件）。
        # 校验器对每个代理都会打好几行日志，DEBUG 级 + 双份输出在池子大时
        # 是一笔可观的格式化与 I/O 开销；容器里 stdout 已被 docker 收集，
        # 再写一份文件只是重复。见 setting.py 的 LOG_LEVEL / LOG_TO_FILE。
        if level is None:
            level = _LEVELS.get(_conf().logLevel, INFO)
        if file is None:
            file = _conf().logToFile
        self.name = name
        self.level = level
        logging.Logger.__init__(self, self.name, level=level)
        if stream:
            self.__setStreamHandler__()
        if file:
            if platform.system() != "Windows":
                self.__setFileHandler__()

    def __setFileHandler__(self, level=None):
        """
        set file handler
        :param level:
        :return:
        """
        # 只在真要写文件时才创建目录（上游在 import 时无条件创建）
        _ensure_log_dir()
        file_name = os.path.join(LOG_PATH, '{name}.log'.format(name=self.name))
        # 设置日志回滚, 保存在log目录, 一天保存一个文件, 保留15天
        file_handler = TimedRotatingFileHandler(filename=file_name, when='D', interval=1, backupCount=15)
        file_handler.suffix = '%Y%m%d.log'
        if not level:
            file_handler.setLevel(self.level)
        else:
            file_handler.setLevel(level)
        formatter = logging.Formatter('%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s')

        file_handler.setFormatter(formatter)
        self.file_handler = file_handler
        self.addHandler(file_handler)

    def __setStreamHandler__(self, level=None):
        """
        set stream handler
        :param level:
        :return:
        """
        stream_handler = logging.StreamHandler()
        formatter = logging.Formatter('%(asctime)s %(filename)s[line:%(lineno)d] %(levelname)s %(message)s')
        stream_handler.setFormatter(formatter)
        if not level:
            stream_handler.setLevel(self.level)
        else:
            stream_handler.setLevel(level)
        self.addHandler(stream_handler)


if __name__ == '__main__':
    log = LogHandler('test')
    log.info('this is a test msg')
