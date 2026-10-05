"""獨立開發入口；直接啟動預設停用。可信 caller 可呼叫 main(reader)。"""
from utils.development_snapshot_ui import render


def main(reader=None):
    import streamlit as st
    render(st, reader)


if __name__ == "__main__":
    main()
