import os
import re
import json
from datetime import datetime
from flask import Flask, request, abort
import redis
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import Configuration, ApiClient, MessagingApi, ReplyMessageRequest, TextMessage
from linebot.v3.webhooks import MessageEvent, TextMessageContent

app = Flask(__name__)

LINE_CHANNEL_ACCESS_TOKEN = os.environ.get('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.environ.get('LINE_CHANNEL_SECRET', '')
LINE_GROUP_ID = os.environ.get('LINE_GROUP_ID', '')
REMIND_SECRET = os.environ.get('REMIND_SECRET', '')
REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379')

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)
r = redis.from_url(REDIS_URL, decode_responses=True)

MIN_PLAYERS = 4
COURT_FEE_NORMAL = 550
COURT_FEE_DISCOUNT = 500
DISCOUNT_SLOTS = [('1200', '1400'), ('1800', '2000'), ('2000', '2200')]


# ── Redis helpers ──────────────────────────────────────────

def get_schedule(group_id, month_key):
    key = f"schedule:{group_id}:{month_key}"
    val = r.get(key)
    return json.loads(val) if val else {}

def save_schedule(group_id, month_key, data):
    key = f"schedule:{group_id}:{month_key}"
    r.set(key, json.dumps(data, ensure_ascii=False))

def get_fees(group_id, month_key):
    key = f"fees:{group_id}:{month_key}"
    val = r.get(key)
    return json.loads(val) if val else {}

def save_fees(group_id, month_key, data):
    key = f"fees:{group_id}:{month_key}"
    r.set(key, json.dumps(data, ensure_ascii=False))

def get_user_ids(group_id, month_key):
    key = f"userids:{group_id}:{month_key}"
    val = r.get(key)
    return json.loads(val) if val else {}

def save_user_ids(group_id, month_key, data):
    key = f"userids:{group_id}:{month_key}"
    r.set(key, json.dumps(data, ensure_ascii=False))


# ── Helpers ────────────────────────────────────────────────

def get_current_month_key():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"

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

def is_discount_slot(date_str, start_str, end_str):
    try:
        parts = date_str.split('/')
        month, day = int(parts[0]), int(parts[1])
        year = datetime.now().year
        dt = datetime(year, month, day)
        weekday = dt.weekday()
        if weekday not in (5, 6):
            return False
        return (start_str, end_str) in DISCOUNT_SLOTS
    except Exception:
        return False

def calc_hours(start_str, end_str):
    sh, sm = int(start_str[:2]), int(start_str[2:])
    eh, em = int(end_str[:2]), int(end_str[2:])
    total_min = (eh * 60 + em) - (sh * 60 + sm)
    return total_min / 60

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
        from linebot.v3.messaging import PushMessageRequest
        api = MessagingApi(api_client)
        api.push_message_with_http_info(
            PushMessageRequest(
                to=group_id,
                messages=[TextMessage(text=text)]
            )
        )


# ── Routes ─────────────────────────────────────────────────

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

@app.route("/remind", methods=['POST'])
def remind():
    secret = request.headers.get('X-Remind-Secret', '')
    if secret != REMIND_SECRET:
        abort(403)

    today = datetime.now()
    today_str = f"{today.month}/{today.day}"
    month_key = get_current_month_key()
    group_id = LINE_GROUP_ID

    if not group_id:
        return 'no group id', 200

    data = get_schedule(group_id, month_key)
    if today_str not in data:
        return 'no event today', 200

    players = data[today_str]
    names = list(players.values())
    user_ids_map = get_user_ids(group_id, month_key)

    mentions = []
    for uid in players.keys():
        mentions.append(f"@{players[uid]}")

    msg = f"🏸 今天 {today_str} 要打球囉！\n出席：{' / '.join(names)}\n{' '.join(mentions)}"
    push_message(group_id, msg)
    return 'ok', 200


# ── Message handler ────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    if event.source.type != 'group':
        reply(event.reply_token,
              "🏸 羽球出團小幫手\n\n群組內指令：\n• 我可以的日期 3/13/17 → 登記日期\n• 更正日期 3/13 → 清掉重登\n• 取消日期 13 → 取消特定日期\n• 查詢統計 → 查看統計\n• 場地費用\n  6/2 2030-2230 → 結算費用\n• 查詢費用 → 查看當月費用\n• 重置本月 → 清除所有資料")
        return

    group_id = event.source.group_id
    user_id = event.source.user_id
    text = event.message.text.strip()
    display_name = get_user_display_name(user_id, group_id)
    month_key = get_current_month_key()

    # 登記日期
    if "我可以的日期" in text:
        idx = text.find("我可以的日期")
        date_part = text[idx + len("我可以的日期"):]
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式：我可以的日期 3/13/17")
            return
        data = get_schedule(group_id, month_key)
        for d in dates:
            if d not in data:
                data[d] = {}
            data[d][user_id] = display_name
        save_schedule(group_id, month_key, data)
        # 儲存 user_id 對應名稱
        uid_map = get_user_ids(group_id, month_key)
        uid_map[user_id] = display_name
        save_user_ids(group_id, month_key, uid_map)
        msg = f"✅ 已記錄 {display_name} 在 {'、'.join(dates)} 可以打球！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    # 更正日期
    elif text.startswith("更正日期"):
        date_part = text.replace("更正日期", "").strip()
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式：更正日期 3/13/17")
            return
        data = get_schedule(group_id, month_key)
        for date_str in list(data.keys()):
            if user_id in data[date_str]:
                del data[date_str][user_id]
                if not data[date_str]:
                    del data[date_str]
        for d in dates:
            if d not in data:
                data[d] = {}
            data[d][user_id] = display_name
        save_schedule(group_id, month_key, data)
        msg = f"✅ 已更正 {display_name} 的日期為 {'、'.join(dates)}！\n\n"
        msg += build_summary_message(group_id)
        reply(event.reply_token, msg)

    # 查詢統計
    elif text in ["查詢統計", "出團統計", "羽球統計", "統計"]:
        reply(event.reply_token, build_summary_message(group_id))

    # 取消日期
    elif text.startswith("取消日期"):
        date_part = text.replace("取消日期", "").strip()
        dates = parse_dates(date_part)
        data = get_schedule(group_id, month_key)
        removed = []
        for d in dates:
            if d in data and user_id in data[d]:
                del data[d][user_id]
                removed.append(d)
                if not data[d]:
                    del data[d]
        save_schedule(group_id, month_key, data)
        if removed:
            msg = f"✅ 已取消 {display_name} 在 {'、'.join(removed)} 的登記。\n\n"
            msg += build_summary_message(group_id)
        else:
            msg = f"找不到 {display_name} 在指定日期的登記。"
        reply(event.reply_token, msg)

    # 場地費用結算
    elif text.startswith("場地費用"):
        lines_input = text.split('\n')
        fee_lines = [l.strip() for l in lines_input[1:] if l.strip()]
        if not fee_lines:
            reply(event.reply_token, "請用格式：\n場地費用\n6/2 2030-2230\n6/12 2030-2230")
            return

        schedule = get_schedule(group_id, month_key)
        fees = get_fees(group_id, month_key)
        result_lines = ["💰 場地費用結算\n"]
        errors = []

        for line in fee_lines:
            match = re.match(r'(\d{1,2}/\d{1,2})\s+(\d{4})-(\d{4})', line)
            if not match:
                errors.append(f"格式錯誤：{line}")
                continue
            date_str, start_str, end_str = match.group(1), match.group(2), match.group(3)
            if date_str not in schedule or not schedule[date_str]:
                errors.append(f"{date_str} 沒有人登記出席")
                continue

            hours = calc_hours(start_str, end_str)
            if hours <= 0:
                errors.append(f"{date_str} 時間有誤")
                continue

            discount = is_discount_slot(date_str, start_str, end_str)
            rate = COURT_FEE_DISCOUNT if discount else COURT_FEE_NORMAL
            total = int(rate * hours)
            names_on_day = schedule[date_str]
            count = len(names_on_day)
            per_person = int(total / count)

            # 覆蓋儲存費用
            if date_str not in fees:
                fees[date_str] = {}
            for uid, name in names_on_day.items():
                fees[date_str][name] = per_person

            tag = "（優惠時段）" if discount else ""
            result_lines.append(f"{date_str}{tag}")
            result_lines.append(f"時間：{start_str[:2]}:{start_str[2:]} - {end_str[:2]}:{end_str[2:]}（{int(hours)}小時）")
            result_lines.append(f"場地費：{total}元（{rate}元/小時）")
            result_lines.append(f"出席（{count}人）：{' / '.join(names_on_day.values())}")
            result_lines.append(f"每人：{per_person}元\n")

        save_fees(group_id, month_key, fees)

        if errors:
            result_lines.append("⚠️ 以下無法結算：")
            result_lines.extend(errors)
            result_lines.append("")

        # 累計費用
        total_by_person = {}
        for day_fees in fees.values():
            for name, amount in day_fees.items():
                total_by_person[name] = total_by_person.get(name, 0) + amount

        if total_by_person:
            result_lines.append("───────────")
            result_lines.append(f"{datetime.now().month}月累計費用")
            for name, amount in total_by_person.items():
                result_lines.append(f"{name}：{amount}元")

        reply(event.reply_token, "\n".join(result_lines))

    # 查詢費用
    elif text in ["查詢費用", "費用統計", "費用"]:
        fees = get_fees(group_id, month_key)
        if not fees:
            reply(event.reply_token, "📊 本月還沒有費用記錄！")
            return

        total_by_person = {}
        lines_out = [f"💰 {datetime.now().month}月費用明細\n"]

        def sort_key(d):
            p = d.split('/')
            return (int(p[0]), int(p[1]))

        for date_str in sorted(fees.keys(), key=sort_key):
            day_fees = fees[date_str]
            lines_out.append(f"{date_str}")
            for name, amount in day_fees.items():
                lines_out.append(f"  {name}：{amount}元")
                total_by_person[name] = total_by_person.get(name, 0) + amount
        lines_out.append("\n───────────")
        lines_out.append("本月累計")
        for name, amount in total_by_person.items():
            lines_out.append(f"{name}：{amount}元")
        reply(event.reply_token, "\n".join(lines_out))

    # 群組ID
    elif text == "群組ID":
        reply(event.reply_token, f"此群組的 ID 是：\n{group_id}")

    # 重置本月
    elif text == "重置本月":
        month_key = get_current_month_key()
        r.delete(f"schedule:{group_id}:{month_key}")
        r.delete(f"fees:{group_id}:{month_key}")
        r.delete(f"userids:{group_id}:{month_key}")
        reply(event.reply_token, "🗑️ 已清除本月所有登記及費用資料。")


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
