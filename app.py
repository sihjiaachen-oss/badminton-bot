import os
import re
import json
from datetime import datetime
from collections import defaultdict
from flask import Flask, request, abort, jsonify
import redis
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, PushMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
GROUP_ID = os.environ.get('LINE_GROUP_ID', '')
REDIS_URL = os.environ.get('REDIS_URL', '')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)

r = redis.from_url(REDIS_URL, decode_responses=True)

MIN_PLAYERS = 4
COURT_FEE_NORMAL = 550
COURT_FEE_DISCOUNT = 500

DISCOUNT_SLOTS = [
    (1200, 1400),
    (1800, 2000),
    (2000, 2200),
]

def redis_key_schedule(group_id, month_key):
    return f"schedule:{group_id}:{month_key}"

def redis_key_fee(group_id, month_key):
    return f"fee:{group_id}:{month_key}"

def get_schedule(group_id, month_key):
    raw = r.get(redis_key_schedule(group_id, month_key))
    return json.loads(raw) if raw else {}

def save_schedule(group_id, month_key, data):
    r.set(redis_key_schedule(group_id, month_key), json.dumps(data, ensure_ascii=False))

def get_fee(group_id, month_key):
    raw = r.get(redis_key_fee(group_id, month_key))
    return json.loads(raw) if raw else {}

def save_fee(group_id, month_key, data):
    r.set(redis_key_fee(group_id, month_key), json.dumps(data, ensure_ascii=False))

def get_current_month_key():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"

def is_discount_slot(date_str, start_fmt, end_fmt):
    now = datetime.now()
    month, day = date_str.split('/')
    try:
        dt = datetime(now.year, int(month), int(day))
    except ValueError:
        return False
    if dt.weekday() not in (5, 6):
        return False
    start_int = int(start_fmt.replace(':', ''))
    end_int = int(end_fmt.replace(':', ''))
    for slot_start, slot_end in DISCOUNT_SLOTS:
        if start_int == slot_start and end_int == slot_end:
            return True
    return False

def parse_dates(text):
    dates = []
    now = datetime.now()
    current_year = now.year
    current_month = now.month

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

    slash_days = re.findall(r'\d{1,2}', text)
    for d in slash_days:
        day = int(d)
        if 1 <= day <= 31:
            try:
                datetime(current_year, current_month, day)
                dates.append(f"{current_month}/{day}")
            except ValueError:
                pass

    return list(dict.fromkeys(dates))

def parse_proxy_command(text, keyword):
    """
    解析代登記/代更正/代取消指令
    支援格式：
    代登記 小王 3/13/17
    代登記小王 3/13/17
    代登記 小王3/13/17
    代登記小王3/13/17
    邏輯：去掉關鍵字後，找到第一個數字出現的位置，前面是名字，後面是日期
    """
    rest = text[len(keyword):].strip()
    if not rest:
        return None, None

    # 找到第一個數字的位置，之前是名字，之後是日期
    match = re.search(r'\d', rest)
    if not match:
        return None, None

    first_digit_pos = match.start()
    proxy_name = rest[:first_digit_pos].strip()
    date_part = rest[first_digit_pos:]

    if not proxy_name:
        return None, None

    dates = parse_dates(date_part)
    return proxy_name, dates

def parse_time_range(time_str):
    match = re.match(r'(\d{4})-(\d{4})', time_str.strip())
    if not match:
        return None, None, None
    start_str = match.group(1)
    end_str = match.group(2)
    start_h, start_m = int(start_str[:2]), int(start_str[2:])
    end_h, end_m = int(end_str[:2]), int(end_str[2:])
    start_total = start_h * 60 + start_m
    end_total = end_h * 60 + end_m
    if end_total <= start_total:
        return None, None, None
    hours = (end_total - start_total) / 60
    start_fmt = f"{start_h:02d}:{start_m:02d}"
    end_fmt = f"{end_h:02d}:{end_m:02d}"
    return hours, start_fmt, end_fmt

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
    data = get_schedule(group_id, month_key)
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
            lines.append(f"  {date_str}（{count}人）")
            lines.append(f"     {' / '.join(names)}")
        lines.append("")
    if short_team:
        lines.append("⚠️ 人數不足的日期：")
        for date_str, count, names in short_team:
            need = MIN_PLAYERS - count
            lines.append(f"  {date_str}（{count}人，還差 {need} 人）")
            lines.append(f"     {' / '.join(names)}")
    return "\n".join(lines)

def build_fee_message(group_id, month_key, results):
    lines = ["💰 場地費用結算\n"]
    for r_entry in results:
        discount_tag = "（優惠時段）" if r_entry['is_discount'] else ""
        lines.append(f"{r_entry['date']}{discount_tag}")
        lines.append(f"時間：{r_entry['start']} - {r_entry['end']}（{r_entry['hours_display']}）")
        lines.append(f"場地費：{r_entry['total']}元（{r_entry['rate']}元/小時）")
        lines.append(f"出席（{len(r_entry['names'])}人）：{' / '.join(r_entry['names'])}")
        lines.append(f"每人：{r_entry['per_person']}元")
        lines.append("")

    lines.append("───────────")
    lines.append(f"{datetime.now().month}月累計費用")

    fee_data = get_fee(group_id, month_key)
    person_total = defaultdict(int)
    for date_str, info in fee_data.items():
        for name in info['names']:
            person_total[name] += info['per_person']

    for name, total in sorted(person_total.items()):
        lines.append(f"{name}：{total}元")

    return "\n".join(lines)

def handle_fee_command(event, group_id, text):
    month_key = get_current_month_key()
    now = datetime.now()
    current_year = now.year

    lines = text.strip().splitlines()
    entries = []
    errors = []
    schedule = get_schedule(group_id, month_key)

    for line in lines[1:]:
        line = line.strip()
        if not line:
            continue

        match = re.match(r'(\d{1,2}/\d{1,2})\s+(\d{4}-\d{4})', line)
        if not match:
            errors.append(f"無法解析：{line}")
            continue

        date_str = match.group(1)
        time_str = match.group(2)

        month_part, day_part = date_str.split('/')
        try:
            datetime(current_year, int(month_part), int(day_part))
        except ValueError:
            errors.append(f"無效日期：{date_str}")
            continue

        hours, start_fmt, end_fmt = parse_time_range(time_str)
        if hours is None:
            errors.append(f"無效時間：{time_str}")
            continue

        names = list(schedule.get(date_str, {}).values())
        if not names:
            errors.append(f"{date_str} 沒有人登記出席")
            continue

        discount = is_discount_slot(date_str, start_fmt, end_fmt)
        rate = COURT_FEE_DISCOUNT if discount else COURT_FEE_NORMAL
        total = int(rate * hours)
        per_person = int(total / len(names))

        if hours == int(hours):
            hours_display = f"{int(hours)}小時"
        else:
            h = int(hours)
            m = int((hours - h) * 60)
            hours_display = f"{h}小時{m}分"

        entries.append({
            'date': date_str,
            'start': start_fmt,
            'end': end_fmt,
            'hours': hours,
            'hours_display': hours_display,
            'total': total,
            'per_person': per_person,
            'names': names,
            'is_discount': discount,
            'rate': rate,
        })

    if not entries and errors:
        reply(event.reply_token, "❌ 格式錯誤，請用：\n場地費用\n6/2 2030-2230\n6/12 2030-2230")
        return

    fee_data = get_fee(group_id, month_key)
    for entry in entries:
        fee_data[entry['date']] = {
            'names': entry['names'],
            'per_person': entry['per_person'],
        }
    save_fee(group_id, month_key, fee_data)

    msg = build_fee_message(group_id, month_key, entries)
    if errors:
        msg += "\n\n⚠️ 以下日期無法處理：\n" + "\n".join(errors)

    reply(event.reply_token, msg)

def handle_query_fee(event, group_id):
    month_key = get_current_month_key()
    current_month = datetime.now().month
    fee_data = get_fee(group_id, month_key)

    if not fee_data:
        reply(event.reply_token, "📊 本月還沒有場地費用記錄！")
        return

    person_total = defaultdict(int)
    lines = [f"💰 {current_month}月場地費用明細\n"]

    def sort_key(d):
        p = d.split('/')
        return (int(p[0]), int(p[1]))

    for date_str in sorted(fee_data.keys(), key=sort_key):
        info = fee_data[date_str]
        lines.append(f"{date_str}（每人 {info['per_person']}元）")
        lines.append(f"  {' / '.join(info['names'])}")
        for name in info['names']:
            person_total[name] += info['per_person']

    lines.append("\n───────────")
    lines.append("累計費用")
    for name, total in sorted(person_total.items()):
        lines.append(f"{name}：{total}元")

    reply(event.reply_token, "\n".join(lines))

def reply(reply_token, text):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.reply_message_with_http_info(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text)]
            )
        )

def push_message(group_id, text):
    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=text)]
            )
        )

@app.route("/remind", methods=['POST'])
def remind():
    secret = request.headers.get('X-Remind-Secret', '')
    if secret != os.environ.get('REMIND_SECRET', ''):
        abort(403)

    now = datetime.now()
    today = f"{now.month}/{now.day}"
    month_key = get_current_month_key()
    group_id = GROUP_ID

    if not group_id:
        return jsonify({"status": "no group_id set"}), 400

    schedule = get_schedule(group_id, month_key)
    players = schedule.get(today, {})

    if not players:
        return jsonify({"status": "no players today"}), 200

    names = list(players.values())

    if len(names) < MIN_PLAYERS:
        return jsonify({"status": "not enough players", "count": len(names)}), 200

    mention_str = " ".join([f"@{name}" for name in names])
    msg = f"🏸 今天 {today} 要打球囉！\n出席：{' / '.join(names)}\n\n{mention_str}"

    push_message(group_id, msg)
    return jsonify({"status": "sent", "date": today, "players": names}), 200

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
              "🏸 羽球出團小幫手\n\n請將我加入羽球群組使用！\n\n群組內指令：\n• 我可以的日期 3/13/17 → 登記可打日期\n• 更正日期 3/13 → 清掉重登\n• 取消日期 13 → 取消特定日期\n• 代登記 小王 3/13/17 → 幫人登記\n• 代更正 小王 3/20 → 幫人更正\n• 代取消 小王 13 → 幫人取消\n• 查詢統計 → 查看出團統計\n• 場地費用 → 結算場地費\n• 查詢費用 → 查看當月費用\n• 重置本月 → 清除所有資料")
        return

    group_id = event.source.group_id
    user_id = event.source.user_id
    text = event.message.text.strip()
    display_name = get_user_display_name(user_id, group_id)
    month_key = get_current_month_key()

    if text == "群組ID":
        reply(event.reply_token, f"此群組的 ID 是：\n{group_id}")
        return

    if "我可以的日期" in text:
        idx = text.find("我可以的日期")
        date_part = text[idx + len("我可以的日期"):]
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式：我可以的日期 3/13/17")
            return
        schedule = get_schedule(group_id, month_key)
        for d in dates:
            if d not in schedule:
                schedule[d] = {}
            schedule[d][user_id] = display_name
        save_schedule(group_id, month_key, schedule)
        msg = f"✅ 已記錄 {display_name} 在 {'、'.join(dates)} 可以打球！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    elif "更正日期" in text:
        idx = text.find("更正日期")
        date_part = text[idx + len("更正日期"):]
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式：更正日期 3/13/17")
            return
        schedule = get_schedule(group_id, month_key)
        for date_str in list(schedule.keys()):
            if user_id in schedule[date_str]:
                del schedule[date_str][user_id]
                if not schedule[date_str]:
                    del schedule[date_str]
        for d in dates:
            if d not in schedule:
                schedule[d] = {}
            schedule[d][user_id] = display_name
        save_schedule(group_id, month_key, schedule)
        msg = f"✅ 已更正 {display_name} 的日期為 {'、'.join(dates)}！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    elif "取消日期" in text:
        idx = text.find("取消日期")
        date_part = text[idx + len("取消日期"):]
        dates = parse_dates(date_part)
        schedule = get_schedule(group_id, month_key)
        removed = []
        for d in dates:
            if d in schedule and user_id in schedule[d]:
                del schedule[d][user_id]
                removed.append(d)
                if not schedule[d]:
                    del schedule[d]
        if removed:
            save_schedule(group_id, month_key, schedule)
            msg = f"✅ 已取消 {display_name} 在 {'、'.join(removed)} 的登記。\n\n"
            msg += build_summary_message(group_id)
        else:
            msg = f"找不到 {display_name} 在指定日期的登記。"
        reply(event.reply_token, msg)

    elif "代登記" in text:
        proxy_name, dates = parse_proxy_command(text, "代登記")
        if not proxy_name or not dates:
            reply(event.reply_token, "格式錯誤！請用：代登記 小王 3/13/17")
            return
        proxy_id = f"proxy_{proxy_name}"
        schedule = get_schedule(group_id, month_key)
        for d in dates:
            if d not in schedule:
                schedule[d] = {}
            schedule[d][proxy_id] = proxy_name
        save_schedule(group_id, month_key, schedule)
        msg = f"✅ 已代登記 {proxy_name} 在 {'、'.join(dates)} 可以打球！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    elif "代更正" in text:
        proxy_name, dates = parse_proxy_command(text, "代更正")
        if not proxy_name or not dates:
            reply(event.reply_token, "格式錯誤！請用：代更正 小王 3/13/17")
            return
        proxy_id = f"proxy_{proxy_name}"
        schedule = get_schedule(group_id, month_key)
        for date_str in list(schedule.keys()):
            if proxy_id in schedule[date_str]:
                del schedule[date_str][proxy_id]
                if not schedule[date_str]:
                    del schedule[date_str]
        for d in dates:
            if d not in schedule:
                schedule[d] = {}
            schedule[d][proxy_id] = proxy_name
        save_schedule(group_id, month_key, schedule)
        msg = f"✅ 已更正 {proxy_name} 的日期為 {'、'.join(dates)}！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    elif "代取消" in text:
        proxy_name, dates = parse_proxy_command(text, "代取消")
        if not proxy_name or not dates:
            reply(event.reply_token, "格式錯誤！請用：代取消 小王 13")
            return
        proxy_id = f"proxy_{proxy_name}"
        schedule = get_schedule(group_id, month_key)
        removed = []
        for d in dates:
            if d in schedule and proxy_id in schedule[d]:
                del schedule[d][proxy_id]
                removed.append(d)
                if not schedule[d]:
                    del schedule[d]
        if removed:
            save_schedule(group_id, month_key, schedule)
            msg = f"✅ 已取消 {proxy_name} 在 {'、'.join(removed)} 的登記。\n\n"
            msg += build_summary_message(group_id)
        else:
            msg = f"找不到 {proxy_name} 在指定日期的登記。"
        reply(event.reply_token, msg)

    elif text in ["查詢統計", "出團統計", "羽球統計", "統計"]:
        reply(event.reply_token, build_summary_message(group_id))

    elif text.startswith("場地費用"):
        handle_fee_command(event, group_id, text)

    elif text in ["查詢費用", "費用統計", "費用"]:
        handle_query_fee(event, group_id)

    elif text == "重置本月":
        save_schedule(group_id, month_key, {})
        save_fee(group_id, month_key, {})
        reply(event.reply_token, "🗑️ 已清除本月所有登記及費用資料。")

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
