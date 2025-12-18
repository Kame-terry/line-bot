#!/bin/bash
# 啟動 Line Bot 應用程式

# 檢查是否安裝了 python3
if ! command -v python3 &> /dev/null; then
    echo "錯誤: 未找到 python3。請先安裝 Python。"
    exit 1
fi

# 嘗試執行應用程式
echo "正在啟動應用程式..."
python3 app.py
