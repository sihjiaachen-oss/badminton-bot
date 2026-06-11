import os
import re
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, abort
import linebot.v3.messaging
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

    slash_multi = re.findall(r'(\d{1,2})/(\d{1,2}(?:/\d{1,2})*)', text)
    for match in slash_multi:
        month = int(match[0])
        days = [int(d) for d in match[1].split('/')]
        for day in days:
            try:
                datetime(current_year, month, day)
                dates.append(f"{month}/{day}")
            except ValueError:
                pass

    if not dates:
        slash_pairs = re.findall(r'(\d{1,2})/(\d{1,2})', text)
        for m, d in slash_pairs:
            try:
                datetime(current_year, int(m), int(d))
                dates.append(f"{int(m)}/{int(d)}")
            except ValueError:
                pass

    if not dates:
        cn_dates = re.findall(r'(\d{1,2})月(\d{1,2})日?', text)
        for m, d in cn_dates:
            try:
                datetime(current_year, int(m), int(d))
                dates.append(f"{int(m)}/{int(d)}")
            except ValueError:
                pass

    if not dates:
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
        return "📊 目前還沒有人登記日期喔！"

    current_month = datetime.now().month

    def sort_key(d):
        p = d.split('/')
        return (int(p[0]), int(p[1]))

    sorted_dates = sorted(data.keys(), key=sort_key)
    full_team, short_team = [], []

    for date_str in sorted_dates:
        players = data[date_str]
        count = len(players)
        names = list(players.values())
        if count >= MIN_PLAYERS:
            full_team.append((date_str, count, names))
        else:
            short_team.append((date_str, count, names))

    lines = [f"🏸 {current_month}月 羽球出團統計\n"]
    if full_team:
        lines.append("✅ 可以開團的日期：")
        for date_str, count, names in full_team:
            lines.append(f"  📅 {date_str}（{count}人）")
            lines.append(f"     👥 {' / '.join(names)}")
        lines.append("")
    if short_team:
        lines.append("⚠️ 人數不足的日期：")
        for date_str, count, names in short_team:
            need = MIN_PLAYERS - count
            lines.append(f"  📅 {date_str}（{count}人，還差 {need} 人）")
            lines.append(f"     👥 {' / '.join(names)}")
    return "\n".join(lines)

def reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    if event.source.type != 'group':
        reply(event.reply_token,
              "🏸 羽球出團小幫手\n\n請將我加入羽球群組使用！\n\n群組內指令：\n• 以下日期我可以 6/4/7 → 登記可打日期\n• 查詢出團 → 查看目前統計\n• 取消日期 6/4 → 取消登記\n• 重置本月 → 清除所有資料")
        return

    group_id = event.source.group_id
    user_id = event.source.user_id
    text = event.message.text.strip()
    display_name = get_user_display_name(user_id, group_id)
    month_key = get_current_month_key()

    if "以下日期我可以" in text:
        idx = text.find("以下日期我可以")
        date_part = text[idx + len("以下日期我可以"):]
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式如：以下日期我可以 6/4/7")
            return
        for d in dates:
            schedule_data[group_id][month_key][d][user_id] = display_name
        msg = f"✅ 已記錄 {display_name} 在 {'、'.join(dates)} 可以打球！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    elif text in ["查詢出團", "出團統計", "羽球統計", "統計"]:
        reply(event.reply_token, build_summary_message(group_id))

    elif text.startswith("取消日期"):
        date_part = text.replace("取消日期", "").strip()
        dates = parse_dates(date_part)
        removed
