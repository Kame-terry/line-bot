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
from datetime import datetime, timedelta, timezone
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
allowed_user_id = os.getenv('ALLOWED_USER_ID')

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

def get_ai_title_and_summary(text):
    try:
        # 第一步：生成標題
        title_resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "請為這段文字產生一個精簡的標題（10-15字），不要包含標點符號或'標題'二字。"},
                {"role": "user", "content": text}
            ]
        )
        title = title_resp.choices[0].message.content.strip()

        # 第二步：生成摘要
        summary_resp = openai_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "請為這段文字產生條列式的重點摘要。"},
                {"role": "user", "content": text}
            ]
        )
        summary = summary_resp.choices[0].message.content.strip()
        
        return title, summary
    except Exception as e:
        app.logger.error(f"Error in AI processing: {e}")
        return text[:20], "無法產生摘要"

def save_to_notion_enhanced(text, ai_title, ai_summary, user_id):
    if not notion_token or not notion_database_id or "your_" in notion_token:
        app.logger.error("Notion configurations are missing or invalid.")
        return False, None

    headers = {
        "Authorization": "Bearer " + notion_token,
        "Content-Type": "application/json",
        "Notion-Version": "2022-06-28"
    }

    # 設定台灣時間 UTC+8
    tz = timezone(timedelta(hours=8))
    # Notion Date 格式需要 ISO 8601 (例如 2023-10-27T10:00:00+08:00)
    current_time_iso = datetime.now(tz).isoformat()
    # 顯示用的時間字串 (給 LINE 回覆用)
    current_time_display = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

    data = {
        "parent": {"database_id": notion_database_id},
        "properties": {
            "name": {
                "title": [
                    {
                        "text": {
                            "content": ai_title
                        }
                    }
                ]
            },
            "類型": {
                "multi_select": [
                    {
                        "name": "語音筆記"
                    }
                ]
            },
            "user_id": {
                "rich_text": [
                    {
                        "text": {
                            "content": user_id
                        }
                    }
                ]
            },
            "摘要": {
                "rich_text": [
                    {
                        "text": {
                            "content": ai_summary[:2000] # Notion Text 限制 2000 字
                        }
                    }
                ]
            },
            "內容": {
                "rich_text": [
                    {
                        "text": {
                            "content": text[:2000] # Notion Text 限制 2000 字
                        }
                    }
                ]
            },
            "創建時間": {
                "date": {
                    "start": current_time_iso
                }
            }
        }
    }

    try:
        app.logger.info(f"Attempting to save enhanced note to Notion DB: {notion_database_id}")
        response = requests.post("https://api.notion.com/v1/pages", headers=headers, data=json.dumps(data))
        if response.status_code == 200:
            app.logger.info("Successfully saved to Notion.")
            return True, current_time_display
        else:
            app.logger.error(f"Failed to save to Notion. Status: {response.status_code}, Response: {response.text}")
            return False, current_time_display
    except Exception as e:
        app.logger.error(f"Error saving to Notion: {e}")
        return False, None

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
    user_id = event.source.user_id
    if allowed_user_id and user_id != allowed_user_id:
        # 非白名單使用者，不回應或回應無權限
        return

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
    user_id = event.source.user_id
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        
        # 檢查權限
        if allowed_user_id and user_id != allowed_user_id:
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="抱歉，您沒有權限使用此功能。")]
                )
            )
            return

        line_bot_blob_api = MessagingApiBlob(api_client)
        
        # 取得音訊內容
        message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
        
        # 儲存到暫存檔
        with tempfile.NamedTemporaryFile(delete=False, suffix='.m4a') as tf:
            tf.write(message_content)
            temp_file_path = tf.name

        try:
            # 1. 使用 OpenAI Whisper 轉錄
            with open(temp_file_path, "rb") as audio_file:
                transcript = openai_client.audio.transcriptions.create(
                    model="whisper-1",
                    file=audio_file,
                    response_format="text"
                )
            
            raw_text = transcript if isinstance(transcript, str) else transcript.text
            
            # 2. 使用 OpenAI 生成標題與摘要
            ai_title, ai_summary = get_ai_title_and_summary(raw_text)

            # 3. 儲存到 Notion
            notion_status = ""
            record_time = ""
            if notion_token and notion_database_id and "your_" not in notion_token:
                success, time_str = save_to_notion_enhanced(raw_text, ai_title, ai_summary, user_id)
                if success:
                    notion_status = "\n\n(已儲存摘要至 Notion)"
                    record_time = time_str
                else:
                    notion_status = "\n\n(Notion 儲存失敗)"
            
            # 4. 回覆使用者
            reply_msg = f"【{ai_title}】\n\n{ai_summary}\n\n---\n原始語音：{raw_text}\n\n時間：{record_time}{notion_status}"
            
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
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