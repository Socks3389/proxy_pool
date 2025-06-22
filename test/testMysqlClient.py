from db.dbClient import DbClient
from helper.proxy import Proxy


def testMysqlClient():
    try:
        uri = "mysql://username:password@127.0.0.1:3306/test_db"
        db = DbClient(uri)
        db.changeTable("use_proxy")
        proxy = Proxy.createFromJson('{"proxy": "118.190.79.36:8090", "https": false, "fail_count": 0, "region": "", "anonymous": "", "source": "freeProxy14", "check_count": 4, "last_status": true, "last_time": "2021-05-26 10:58:04"}')

        print("put: ", db.put(proxy))
        print("get: ", db.get(https=None))
        print("exists: ", db.exists("118.190.79.36:8090"))
        print("getAll: ", db.getAll(https=None))
        print("clear: ", db.clear())
        print("getCount: ", db.getCount())
    except Exception as e:
        print(f"Error during MySQL client test: {e}")


if __name__ == '__main__':
    testMysqlClient()
