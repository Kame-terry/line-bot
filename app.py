# /// script
# requires-python = ">=3.12"
# dependencies = [
#     "flask",
#     "line-bot-sdk",
# ]
# ///

import sys
import os
import tempfile
import json
import requests
from datetime import datetime
from pathlib import Path

from flask import Flask, request, abort
from openai import OpenAI
from dotenv import load_dotenv

from linebot.v3 import (
    WebhookHandler
)

# 載入 .env 檔案中的環境變數
load_dotenv()

from linebot.v3.exceptions import (
    InvalidSignatureError
)
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    MessagingApiBlob,
    ReplyMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import (
    MessageEvent,
    TextMessageContent,
    AudioMessageContent
)

app = Flask(__name__)

# 取得環境變數
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
openai_api_key = os.getenv('OPENAI_API_KEY')
notion_token = os.getenv('NOTION_API_TOKEN')
notion_database_id = os.getenv('NOTION_DATABASE_ID')

if channel_secret is None:
    print('Error: LINE_CHANNEL_SECRET is not defined in environment variables.')
    sys.exit(1)
if channel_access_token is None:
    print('Error: LINE_CHANNEL_ACCESS_TOKEN is not defined in environment variables.')
    sys.exit(1)
if openai_api_key is None:
    print('Error: OPENAI_API_KEY is not defined in environment variables.')
    sys.exit(1)

handler = WebhookHandler(channel_secret)
configuration = Configuration(access_token=channel_access_token)
openai_client = OpenAI(api_key=openai_api_key)

def save_to_notion(text):
    if not notion_token or not notion_database_id or "your_" in notion_token:
        app.logger.error("Notion configurations are missing or invalid.")
        return False

    headers = {
        "Authorization": "Bearer " + notion_token,
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    # 標題只取前 30 個字
    title = text[:30] + "..." if len(text) > 30 else text

    data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "name": {
                "title": [
                    {
                        "text": {
                            "content": title
                        }
                    }
                ]
            }
        },
        "children": [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [
                        {
                            "type": "text",
                            "text": {
                                "content": text
                            }
                        }
                    ]
                }
            }
        ]
    }

    try:
        app.logger.info(f"Attempting to save to Notion DB: {notion_database_id}")
        response = requests.post("https://api.notion.com/v1/pages", headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            app.logger.info("Successfully saved to Notion.")
            return True
        else:
            app.logger.error(f"Failed to save to Notion. Status: {response.status_code}, Response: {response.text}")
            return False
    except Exception as e:
        app.logger.error(f"Error saving to Notion: {e}")
        return False

@app.route("/", methods=['GET'])
def index():
    return "Hello, LINE Bot is running!"

@app.route("/callback", methods=['POST'])
def callback():
    # get X-Line-Signature header value
    signature = request.headers['X-Line-Signature']

    # get request body as text
    body = request.get_data(as_text=True)
    app.logger.info("Request body: " + body)

    # handle webhook body
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.info("Invalid signature. Please check your channel access token/channel secret.")
        abort(400)

    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        # 回覆一樣的訊息 (Echo)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=event.message.text)]
            )
        )

@handler.add(MessageEvent, message=AudioMessageContent)
def handle_audio_message(event):
    with ApiClient(configuration) as api_client:
        line_bot_blob_api = MessagingApiBlob(api_client)
        line_bot_api = MessagingApi(api_client)
        
        # 取得音訊內容
        message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        
        # 儲存到暫存檔
        with tempfile.NamedTemporaryFile(delete=False, suffix='.m4a') as tf:
            tf.write(message_content)
            temp_file_path = tf.name

        try:
            # 使用 OpenAI Whisper 轉錄
            with open(temp_file_path, "rb") as audio_file:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
            
            # 處理轉錄文字
            reply_text = transcript if isinstance(transcript, str) else transcript.text
            
            # 儲存到 Notion
            notion_status = ""
            if notion_token and notion_database_id and "your_" not in notion_token:
                success = save_to_notion(reply_text)
                if success:
                    notion_status = "\n\n(已儲存至 Notion)"
                else:
                    notion_status = "\n\n(Notion 儲存失敗，請檢查後台日誌)"
            
            # 回覆轉錄文字 + 狀態
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_text + notion_status)]
                )
            )
        except Exception as e:
            app.logger.error(f"Error processing audio: {e}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="抱歉，語音處理失敗。")]
                )
            )
        finally:
            # 清理暫存檔
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

if __name__ == "__main__":
    # 本地測試時使用 8000 port
    app.run(host="0.0.0.0", port=8000, debug=True)
