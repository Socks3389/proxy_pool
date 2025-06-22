# -*- coding: utf-8 -*-
"""
-------------------------------------------------
   File Name：     ProxyHandler.py
   Description :
   Author :       JHao
   date：          2016/12/3
-------------------------------------------------
   Change Activity:
                   2016/12/03:
                   2020/05/26: 区分http和https
-------------------------------------------------
"""
__author__ = 'JHao'

from helper.proxy import Proxy
from db.dbClient import DbClient
from handler.configHandler import ConfigHandler
from handler.logHandler import LogHandler


class ProxyHandler(object):
    """ Proxy CRUD operator"""

    def __init__(self):
        self.log = LogHandler("proxy_handler")  # 添加日志记录器
        self.conf = ConfigHandler()
        self.db = DbClient(self.conf.dbConn)
        self.db.changeTable(self.conf.tableName)

    def get(self, https=False):
        """
        return a proxy
        Args:
            https: True/False
        Returns:
        """
        proxy = self.db.get(https)
        if proxy:
            self.log.info(f"Fetched proxy: {proxy}")
        else:
            self.log.warning("No proxy fetched from database.")
        return Proxy.createFromJson(proxy) if proxy else None

    def pop(self, https):
        """
        return and delete a useful proxy
        :return:
        """
        proxy = self.db.pop(https)
        if proxy:
            return Proxy.createFromJson(proxy)
        return None

    def put(self, proxy):
        """
        put proxy into use proxy
        :return:
        """
        self.db.put(proxy)

    def delete(self, proxy):
        """
        delete useful proxy
        :param proxy:
        :return:
        """
        return self.db.delete(proxy.proxy)

    def getAll(self, https=None):
        """
        get all proxy from pool as Proxy list
        :param https: None -> all, True -> only https, False -> only http
        :return:
        """
        proxies = self.db.getAll(https)
        self.log.info(f"Fetched {len(proxies)} proxies from database with https={https}.")
        return [Proxy.createFromJson(_) for _ in proxies]

    def exists(self, proxy):
        """
        check proxy exists
        :param proxy:
        :return:
        """
        return self.db.exists(proxy.proxy)

    def getCount(self):
        """
        return raw_proxy and use_proxy count
        :return:
        """
        count_data = self.db.getCount()
        self.log.info(f"Proxy count: {count_data}")
        return count_data
