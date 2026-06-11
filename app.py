import os
import re
import json
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', 'YOUR_CHANNEL_ACCESS_TOKEN')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', 'YOUR_CHANNEL_SECRET')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN)
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

    slash_multi = re.findall(r'(\d{1,2})/(\d{1,2}(?:/\d{1,2})*)', text)
    for match in slash_multi:
        month = int(match[0])
        days = [int(d) for d in match[1].split('/')]
