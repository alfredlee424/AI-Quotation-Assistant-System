"""僅注入真正檢查子程序；第一張受檢表讀完後保持原唯讀交易。"""
import os

from engine import development_schema_inspection as live_schema


live_original_signature = live_schema._table_signature
live_stopped = False


def live_signature(connection, name):
    global live_stopped
    result = live_original_signature(connection, name)
    # 私有記憶體參考庫也呼叫此函式；必須排除它，不能誤稱受檢庫已讀取。
    if not live_stopped and connection.in_transaction and connection.isolation_level is None:
        live_stopped = True
        os.write(ready_fd, b"R" + os.getpid().to_bytes(8, "big"))
        if os.read(resume_fd, 1) != b"G":
            raise RuntimeError("test controller disconnected")
    return result


# 其他檢查模組尚未匯入，會取得這個只加屏障、不變更結果的函式。
live_schema._table_signature = live_signature
