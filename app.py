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
    AudioMessageContent,
    ImageMessageContent
)
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload
from bs4 import BeautifulSoup
from apify_client import ApifyClient

app = Flask(__name__)

# 取得環境變數
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
openai_api_key = os.getenv('OPENAI_API_KEY')
notion_token = os.getenv('NOTION_API_TOKEN')
notion_database_id = os.getenv('NOTION_DATABASE_ID')
allowed_user_id = os.getenv('ALLOWED_USER_ID')
apify_api_token = os.getenv('APIFY_API_TOKEN')

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
# 初始化 Apify Client
apify_client = ApifyClient(apify_api_token) if apify_api_token else None

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

def save_to_notion_enhanced(text, ai_title, ai_summary, user_id, type_name="語音筆記", url=None):
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

    properties = {
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
                    "name": type_name
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

    if url:
        properties["url"] = {
            "url": url
        }

    data = {
        "parent": {"database_id": notion_database_id},
        "properties": properties
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

def fetch_url_content(url):
    """爬取網頁內容並回傳純文字"""
    try:
        # 判斷是否為 Facebook
        if "facebook.com" in url or "fb.watch" in url:
            if not apify_client:
                return "錯誤：未設定 Apify API Token，無法爬取 Facebook。"
            app.logger.info(f"Using Apify for Facebook URL: {url}")
            
            # 使用 apify/facebook-posts-scraper
            run_input = {
                "startUrls": [{"url": url}],
                "resultsLimit": 1,
            }
            # 改用 Actor 名稱呼叫
            run = apify_client.actor("apify/facebook-posts-scraper").call(run_input=run_input)
            
            # 取得結果
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if dataset_items:
                post = dataset_items[0]
                text = post.get("text") or post.get("postText") or ""
                # 如果有 comments 也可以抓，這裡先只抓內文
                return text[:8000]
            else:
                return "Apify 未能抓取到內容，可能是權限或貼文不存在。"

        # 判斷是否為 Threads
        elif "threads.net" in url:
            if not apify_client:
                return "錯誤：未設定 Apify API Token，無法爬取 Threads。"
            app.logger.info(f"Using Apify for Threads URL: {url}")
            
            # 使用 apify/threads-scraper
            run_input = {
                "startUrls": [url],
                "maxPostCount": 1,
            }
            # 改用 Actor 名稱呼叫
            run = apify_client.actor("apify/threads-scraper").call(run_input=run_input)
            
            dataset_items = apify_client.dataset(run["defaultDatasetId"]).list_items().items
            if dataset_items:
                thread = dataset_items[0]
                text = thread.get("thread_items", [{}])[0].get("post", {}).get("caption", {}).get("text", "")
                if not text:
                     text = thread.get("text") or "" # 嘗試其他可能的欄位
                return text[:8000]
            else:
                 return "Apify 未能抓取到內容。"

        # 一般網頁爬取
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        
        # 使用 BeautifulSoup 解析 HTML
        soup = BeautifulSoup(response.text, 'html.parser')
        
        # 移除 script, style 等不相關標籤
        for script in soup(["script", "style", "nav", "footer", "iframe"]):
            script.extract()
            
        # 取得純文字
        text = soup.get_text()
        
        # 清理多餘空白
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = '\n'.join(chunk for chunk in chunks if chunk)
        
        # 限制回傳長度，避免 Token 爆量 (取前 8000 字)
        return text[:8000]
    except Exception as e:
        app.logger.error(f"Error fetching URL {url}: {e}")
        return None

@handler.add(MessageEvent, message=TextMessageContent)

def handle_message(event):

    user_id = event.source.user_id

    if allowed_user_id and user_id != allowed_user_id:

        # 非白名單使用者，不回應或回應無權限

        return



    text = event.message.text.strip()



    with ApiClient(configuration) as api_client:

        line_bot_api = MessagingApi(api_client)



        if text.startswith("/a"):

            # 處理文字摘要請求

            content_to_summarize = text[2:].strip()

            if not content_to_summarize:

                line_bot_api.reply_message(

                    ReplyMessageRequest(

                        reply_token=event.reply_token,

                        messages=[TextMessage(text="請在 /a 後面加上要摘要的文字。")]

                    )

                )

                return



            try:

                # 產生標題與摘要

                ai_title, ai_summary = get_ai_title_and_summary(content_to_summarize)

                

                # 儲存到 Notion

                notion_status = ""

                record_time = ""

                if notion_token and notion_database_id and "your_" not in notion_token:

                    success, time_str = save_to_notion_enhanced(

                        content_to_summarize, 

                        ai_title, 

                        ai_summary, 

                        user_id, 

                        type_name="文字摘要"

                    )

                    if success:

                        notion_status = "\n\n(已儲存摘要至 Notion)"

                        record_time = time_str

                    else:

                        notion_status = "\n\n(Notion 儲存失敗)"

                

                # 回覆使用者

                if not record_time:

                    tz = timezone(timedelta(hours=8))

                    record_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")



                reply_msg = f"【{ai_title}】\n\n{ai_summary}\n\n---\n原始文字：{content_to_summarize[:50]}...\n\n時間：{record_time}{notion_status}"

                

                line_bot_api.reply_message(

                    ReplyMessageRequest(

                        reply_token=event.reply_token,

                        messages=[TextMessage(text=reply_msg)]

                    )

                )

            except Exception as e:

                app.logger.error(f"Error processing text summary: {e}")

                line_bot_api.reply_message(

                    ReplyMessageRequest(

                        reply_token=event.reply_token,

                        messages=[TextMessage(text="抱歉，摘要處理失敗。")]

                    )

                )



        elif text.startswith("http://") or text.startswith("https://"):

            # 處理網址摘要

            url = text

            try:

                # 1. 辨別類型

                type_name = "網頁摘要"

                if "facebook.com" in url or "fb.watch" in url:

                    type_name = "fb"

                elif "threads.net" in url:

                    type_name = "threads"



                # 2. 爬取網頁內容

                web_content = fetch_url_content(url)

                

                if not web_content:

                    line_bot_api.reply_message(

                        ReplyMessageRequest(

                            reply_token=event.reply_token,

                            messages=[TextMessage(text="無法讀取網頁內容，可能是網站有防護或連結無效。")]

                        )

                    )

                    return



                # 3. 產生標題與摘要

                ai_title, ai_summary = get_ai_title_and_summary(web_content)

                

                # 4. 儲存到 Notion (包含 URL 與類型)

                notion_status = ""

                record_time = ""

                if notion_token and notion_database_id and "your_" not in notion_token:

                    success, time_str = save_to_notion_enhanced(

                        web_content, 

                        ai_title, 

                        ai_summary, 

                        user_id, 

                        type_name=type_name,

                        url=url

                    )

                    if success:

                        notion_status = "\n\n(已儲存摘要至 Notion)"

                        record_time = time_str

                    else:

                        notion_status = "\n\n(Notion 儲存失敗)"

                

                # 5. 回覆使用者

                if not record_time:

                    tz = timezone(timedelta(hours=8))

                    record_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")



                reply_msg = f"【{ai_title}】({type_name})\n\n{ai_summary}\n\n---\n來源：{url}\n\n時間：{record_time}{notion_status}"

                

                line_bot_api.reply_message(

                    ReplyMessageRequest(

                        reply_token=event.reply_token,

                        messages=[TextMessage(text=reply_msg)]

                    )

                )

                

            except Exception as e:

                app.logger.error(f"Error processing URL summary: {e}")

                line_bot_api.reply_message(

                    ReplyMessageRequest(

                        reply_token=event.reply_token,

                        messages=[TextMessage(text="抱歉，網頁摘要處理失敗。")]

                    )

                )



        else:

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

def upload_to_drive(file_path, original_filename):
    """上傳檔案至 Google Drive"""
    drive_folder_id = os.getenv('GOOGLE_DRIVE_FOLDER_ID')
    token_file = os.getenv('GOOGLE_OAUTH_TOKEN', 'token.json')
    credential_file = os.getenv('GOOGLE_OAUTH_CREDENTIALS', 'credentials.json')

    if not drive_folder_id:
        app.logger.error("GOOGLE_DRIVE_FOLDER_ID is not set.")
        return None

    creds = None
    SCOPES = ['https://www.googleapis.com/auth/drive.file']

    if os.path.exists(token_file):
        creds = Credentials.from_authorized_user_file(token_file, SCOPES)
    
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                with open(token_file, 'w') as token:
                    token.write(creds.to_json())
            except Exception as e:
                app.logger.error(f"Error refreshing token: {e}")
                return None
        else:
            app.logger.error("Token is invalid or missing. Please run auth_google.py locally.")
            return None

    try:
        service = build('drive', 'v3', credentials=creds)
        
        file_metadata = {
            'name': original_filename,
            'parents': [drive_folder_id]
        }
        media = MediaFileUpload(file_path, resumable=True)
        
        file = service.files().create(
            body=file_metadata,
            media_body=media,
            fields='id, webViewLink'
        ).execute()
        
        return file.get('webViewLink')
        
    except Exception as e:
        app.logger.error(f"Error uploading to Drive: {e}")
        return None

import base64

def encode_image(image_path):
    with open(image_path, "rb") as image_file:
        return base64.b64encode(image_file.read()).decode('utf-8')

@handler.add(MessageEvent, message=ImageMessageContent)
def handle_image_message(event):
    user_id = event.source.user_id
    if allowed_user_id and user_id != allowed_user_id:
        return

    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_blob_api = MessagingApiBlob(api_client)

        try:
            # 取得圖片內容
            message_content = line_bot_blob_api.get_message_content(message_id=event.message.id)
            
            # 暫存圖片
            with tempfile.NamedTemporaryFile(delete=False, suffix='.jpg') as tf:
                tf.write(message_content)
                temp_file_path = tf.name

            # 上傳至 Google Drive
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"line_image_{timestamp}.jpg"
            drive_link = upload_to_drive(temp_file_path, filename)
            
            if drive_link:
                # 使用 GPT-4o 辨識圖片內容
                try:
                    base64_image = encode_image(temp_file_path)
                    response = openai_client.chat.completions.create(
                        model="gpt-4o",
                        messages=[
                            {
                                "role": "user",
                                "content": [
                                    {"type": "text", "text": "請描述這張圖片的內容，並為它下一個精簡的標題(15字內)。格式範例：\n標題：[標題]\n內容：[詳細描述]"},
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/jpeg;base64,{base64_image}"
                                        },
                                    },
                                ],
                            }
                        ],
                        max_tokens=500,
                    )
                    ai_response = response.choices[0].message.content
                    
                    # 解析回應
                    ai_title = "圖片筆記"
                    ai_summary = ai_response
                    
                    if "標題：" in ai_response and "內容：" in ai_response:
                        parts = ai_response.split("內容：")
                        ai_title = parts[0].replace("標題：", "").strip()
                        ai_summary = parts[1].strip()
                    
                except Exception as ai_e:
                    app.logger.error(f"Error in AI vision processing: {ai_e}")
                    ai_title = "圖片筆記"
                    ai_summary = f"無法辨識圖片內容。Drive 連結: {drive_link}"

                # 儲存至 Notion
                notion_status = ""
                record_time = ""
                if notion_token and notion_database_id and "your_" not in notion_token:
                    success, time_str = save_to_notion_enhanced(
                        f"圖片連結: {drive_link}\n\nAI 描述: {ai_summary}", 
                        ai_title, 
                        ai_summary, 
                        user_id, 
                        type_name="圖片",
                        url=drive_link
                    )
                    if success:
                        notion_status = "\n\n(已記錄至 Notion)"
                        record_time = time_str
                    else:
                        notion_status = "\n\n(Notion 儲存失敗)"
                else:
                    tz = timezone(timedelta(hours=8))
                    record_time = datetime.now(tz).strftime("%Y-%m-%d %H:%M:%S")

                reply_msg = f"【{ai_title}】\n\n{ai_summary}\n\n---\n連結：{drive_link}\n時間：{record_time}{notion_status}"
            else:
                reply_msg = "圖片上傳失敗，請檢查後端日誌或確認授權狀態。"

            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text=reply_msg)]
                )
            )

        except Exception as e:
            app.logger.error(f"Error processing image: {e}")
            line_bot_api.reply_message(
                ReplyMessageRequest(
                    reply_token=event.reply_token,
                    messages=[TextMessage(text="抱歉，圖片處理失敗。")]
                )
            )
        finally:
            if os.path.exists(temp_file_path):
                os.remove(temp_file_path)

if __name__ == "__main__":
    # 本地測試時使用 8000 port
    app.run(host="0.0.0.0", port=8000, debug=True)