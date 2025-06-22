import pymysql
import json
from handler.logHandler import LogHandler
from datetime import datetime


class MysqlClient:
    """
    MySQL client
    """

    def __init__(self, host, port, username, password, db):
        self.log = LogHandler("mysql_client")
        self.conn = pymysql.connect(
            host=host,
            port=port,
            user=username,
            password=password,
            database=db,
            charset="utf8mb4"
        )
        self.cursor = self.conn.cursor()
        self.table_name = ""

    def __ensure_connection(self):
        """
        确保数据库连接有效，如果失效则重新连接
        """
        try:
            self.conn.ping(reconnect=True)
        except Exception as e:
            self.log.error(f"MySQL connection lost. Reconnecting... Error: {str(e)}")
            self.conn = pymysql.connect(
                host=self.conn.host,
                port=self.conn.port,
                user=self.conn.user,
                password=self.conn.password,
                database=self.conn.db.decode(),
                charset="utf8mb4"
            )
            self.cursor = self.conn.cursor()

    def get(self, https):
        """
        随机返回一个代理
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"SELECT * FROM {self.table_name} WHERE https=%s ORDER BY RAND() LIMIT 1"
        self.cursor.execute(query, (https,))
        result = self.cursor.fetchone()
        if result:
            self.log.info(f"Query result: {result}")
            # 将 datetime 类型转换为字符串
            result_dict = {desc[0]: (value.isoformat() if isinstance(value, datetime) else value)
                           for desc, value in zip(self.cursor.description, result)}
            return json.dumps(result_dict)
        self.log.warning("No result fetched from database.")
        return None

    def put(self, proxy_obj):
        """
        插入一个代理
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"INSERT INTO {self.table_name} (proxy, https, fail_count, region, anonymous, source, check_count, last_status, last_time) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)"
        self.cursor.execute(query, (
            proxy_obj.proxy, proxy_obj.https, proxy_obj.fail_count, proxy_obj.region,
            proxy_obj.anonymous, proxy_obj.source, proxy_obj.check_count,
            proxy_obj.last_status, proxy_obj.last_time
        ))
        self.conn.commit()

    def pop(self, https):
        """
        弹出一个代理
        """
        self.__ensure_connection()  # 确保连接有效
        proxy = self.get(https)
        if proxy:
            self.delete(json.loads(proxy)["proxy"])
        return proxy

    def delete(self, proxy_str):
        """
        删除指定代理
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"DELETE FROM {self.table_name} WHERE proxy=%s"
        self.cursor.execute(query, (proxy_str,))
        self.conn.commit()

    def exists(self, proxy_str):
        """
        检查代理是否存在
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"SELECT 1 FROM {self.table_name} WHERE proxy=%s"
        self.cursor.execute(query, (proxy_str,))
        return self.cursor.fetchone() is not None

    def update(self, proxy_obj):
        """
        更新代理信息
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"UPDATE {self.table_name} SET https=%s, fail_count=%s, region=%s, anonymous=%s, source=%s, check_count=%s, last_status=%s, last_time=%s WHERE proxy=%s"
        self.cursor.execute(query, (
            proxy_obj.https, proxy_obj.fail_count, proxy_obj.region,
            proxy_obj.anonymous, proxy_obj.source, proxy_obj.check_count,
            proxy_obj.last_status, proxy_obj.last_time, proxy_obj.proxy
        ))
        self.conn.commit()

    def getAll(self, https=None):
        """
        获取所有代理
        """
        self.__ensure_connection()  # 确保连接有效
        if https is None:
            query = f"SELECT * FROM {self.table_name}"
            self.cursor.execute(query)
        else:
            query = f"SELECT * FROM {self.table_name} WHERE https=%s"
            self.cursor.execute(query, (https,))
        results = self.cursor.fetchall()
        self.log.info(f"Fetched {len(results)} rows from table {self.table_name}.")
        return [
            json.dumps(
                {desc[0]: (value.isoformat() if isinstance(value, datetime) else value)
                 for desc, value in zip(self.cursor.description, row)}
            )
            for row in results
        ]

    def clear(self):
        """
        清空所有代理
        """
        self.__ensure_connection()  # 确保连接有效
        query = f"TRUNCATE TABLE {self.table_name}"
        self.cursor.execute(query)
        self.conn.commit()

    def getCount(self):
        """
        获取代理数量
        """
        self.__ensure_connection()  # 确保连接有效
        if not self.table_name:
            raise ValueError("Table name is not set. Please call changeTable() to set the table name.")
        
        query_total = f"SELECT COUNT(*) FROM {self.table_name}"
        self.cursor.execute(query_total)
        total = self.cursor.fetchone()[0]

        query_https = f"SELECT COUNT(*) FROM {self.table_name} WHERE https=1"
        self.cursor.execute(query_https)
        https_count = self.cursor.fetchone()[0]

        return {"total": total, "https": https_count}

    def __ensure_table_exists(self):
        """
        确保表存在，如果不存在则自动创建
        """
        self.__ensure_connection()  # 确保连接有效
        create_table_query = f"""
        CREATE TABLE IF NOT EXISTS {self.table_name} (
            id INT AUTO_INCREMENT PRIMARY KEY,
            proxy VARCHAR(191) NOT NULL UNIQUE,
            https BOOLEAN NOT NULL,
            fail_count INT DEFAULT 0,
            region VARCHAR(255),
            anonymous VARCHAR(255),
            source VARCHAR(255),
            check_count INT DEFAULT 0,
            last_status BOOLEAN,
            last_time DATETIME
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 ROW_FORMAT=DYNAMIC;
        """
        self.cursor.execute(create_table_query)
        self.conn.commit()

    def changeTable(self, name):
        """
        切换操作表
        """
        if not name:
            raise ValueError("Table name cannot be empty.")
        self.table_name = name
        self.__ensure_table_exists()  # 确保表存在

    def test(self):
        """
        测试数据库连接
        """
        try:
            if not self.table_name:
                self.log.info("Table name is not set. Setting default table name to 'use_proxy'.")
                self.changeTable("use_proxy")  # 设置默认表名
            self.getCount()
        except Exception as e:
            self.log.error(f"MySQL connection error: {str(e)}", exc_info=True)
            return e
