import os
import re
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

schedule_data = defaultdict(lambda: defaultdict(lambda: defaultdict(dict)))
MIN_PLAYERS = 4

def get_current_month_key():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"

def parse_dates(text):
    dates = []
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    # 格式一：「6月 3/13/17」或「6月3/13/17」- 指定月份後列日期
    cn_month_match = re.search(r'(\d{1,2})月\s*(\d{1,2}(?:[/、,]\d{1,2})*)', text)
    if cn_month_match:
        month = int(cn_month_match.group(1))
        day_part = cn_month_match.group(2)
        days = re.findall(r'\d{1,2}', day_part)
        for d in days:
            try:
                datetime(current_year, month, int(d))
                dates.append(f"{month}/{int(d)}")
            except ValueError:
                pass
        return list(dict.fromkeys(dates))

    # 格式二：「6/3、6/13」每個日期都帶月份
    slash_pairs = re.findall(r'(\d{1,2})/(\d{1,2})', text)
    for m, d in slash_pairs:
        try:
            datetime(current_year, int(m), int(d))
            dates.append(f"{int(m)}/{int(d)}")
        except ValueError:
            pass
    if dates:
        return list(dict.fromkeys(dates))

    # 格式三：純數字，補當月月份
    pure_nums = re.findall(r'(?<!\d)(\d{1,2})(?!\d)', text)
    for d in pure_nums:
        day = int(d)
        if 1 <= day <= 31:
            try:
                datetime(current_year, current_month, day)
                dates.append(f"{current_month}/{day}")
            except ValueError:
                pass

    return list(dict.fromkeys(dates))

def get_user_display_name(user_id, group_id=None):
    try:
        with ApiClient(configuration) as api_client:
            api = MessagingApi(api_client)
            if group_id:
                profile = api.get_group_member_profile(group_id, user_id)
            else:
                profile = api.get_profile(user_id)
            return profile.display_name
    except Exception:
        return f"用戶_{user_id[-4:]}"

def build_summary_message(group_id):
    month_key = get_current_month_key()
    data = schedule_data[group_id][month_key]
    if not data:
