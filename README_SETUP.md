# 環境設定說明

看起來您的系統尚未安裝 `pip`，這導致無法自動安裝 Python 套件。

## 1. 安裝 pip

如果您使用的是 Ubuntu/Debian Linux (WSL)，請執行：

```bash
sudo apt-get update
sudo apt-get install python3-pip
```

## 2. 安裝專案依賴

安裝 pip 後，請執行以下指令安裝此專案需要的套件：

```bash
pip install flask line-bot-sdk openai python-dotenv
```

## 3. 設定環境變數

請確認您的 `.env` 檔案中包含以下內容（請填入您的實際金鑰）：

```
LINE_CHANNEL_SECRET=您的Secret
LINE_CHANNEL_ACCESS_TOKEN=您的AccessToken
OPENAI_API_KEY=您的OpenAIKey
```

## 4. 啟動程式

完成上述步驟後，您可以執行：

```bash
bash run.sh
```
或者直接：
```bash
python3 app.py
```
