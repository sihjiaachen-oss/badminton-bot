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


# ── Helpers ────────────────────────────────────────────────

def get_current_month_key():
    now = datetime.now()
    return f"{now.year}-{now.month:02d}"

def month_key_for(month):
    """根據月份數字算出該存到哪一年（處理跨年，例如12月登記隔年1月）"""
    now = datetime.now()
    current_year = now.year
    current_month = now.month
    year = current_year
    if month < current_month:
        # 月份比現在小，視為明年（例如現在12月，登記1月）
        year = current_year + 1
    return f"{year}-{month:02d}"

def parse_dates_with_month(text):
    """
    解析日期，回傳 [(month_key, date_str), ...]
    支援跨月份登記：每個日期會根據自己的月份分別歸類
    """
    results = []
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    # 格式一：「7月 5/12/19」- 指定月份後列日期
    cn_month_match = re.search(r'(\d{1,2})月\s*(\d{1,2}(?:[/、,]\d{1,2})*)', text)
    if cn_month_match:
        month = int(cn_month_match.group(1))
        day_part = cn_month_match.group(2)
        days = re.findall(r'\d{1,2}', day_part)
        mk = month_key_for(month)
        year = int(mk.split('-')[0])
        for d in days:
            try:
                datetime(year, month, int(d))
                results.append((mk, f"{month}/{int(d)}"))
            except ValueError:
                pass
        return results

    # 格式二：純日期，自動補當月
    slash_days = re.findall(r'\d{1,2}', text)
    mk = get_current_month_key()
    for d in slash_days:
        day = int(d)
        if 1 <= day <= 31:
            try:
                datetime(current_year, current_month, day)
                results.append((mk, f"{current_month}/{day}"))
            except ValueError:
                pass

    return results

def parse_dates(text):
    """舊版相容：只回傳日期字串列表（用於取消/更正，這些操作通常針對當月）"""
    pairs = parse_dates_with_month(text)
    return [d for _, d in pairs]

def days_in_month(year, month):
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1)
    else:
        next_month_first = datetime(year, month + 1, 1)
    last_day = (next_month_first - __import__('datetime').timedelta(days=1)).day
    return last_day

def parse_unavailable_dates_with_month(text):
    """
    解析「不可以的日期」，回傳 (month_key, [可以的日期字串...])
    邏輯：找出文字裡指定的月份（沒指定就用當月），
    再列出該月份扣除提到的日期後，剩下的所有日期
    """
    now = datetime.now()
    current_year = now.year
    current_month = now.month

    # 判斷文字裡有沒有指定月份
    cn_month_match = re.search(r'(\d{1,2})月', text)
    if cn_month_match:
        month = int(cn_month_match.group(1))
        mk = month_key_for(month)
    else:
        month = current_month
        mk = get_current_month_key()

    year = int(mk.split('-')[0])

    # 抓出文字裡所有不可以的日期數字
    # 先移除「N月」這個詞，避免月份數字被誤認成日期
    text_without_month = re.sub(r'\d{1,2}月', '', text)
    unavailable_days = set()
    for d in re.findall(r'\d{1,2}', text_without_month):
        day = int(d)
        if 1 <= day <= 31:
            unavailable_days.add(day)

    total_days = days_in_month(year, month)
    available_dates = []
    for day in range(1, total_days + 1):
        if day not in unavailable_days:
            available_dates.append(f"{month}/{day}")

    return mk, available_dates

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

def build_summary_message(group_id, month_key=None):
    if month_key is None:
        month_key = get_current_month_key()
    data = get_schedule(group_id, month_key)
    display_month = int(month_key.split('-')[1])

    if not data:
        return f"📊 {display_month}月目前還沒有人登記日期喔！"

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

    lines = [f"🏸 {display_month}月 羽球出團統計\n"]
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

def is_discount_slot(date_str, start_str, end_str, month_key):
    try:
        parts = date_str.split('/')
        month, day = int(parts[0]), int(parts[1])
        year = int(month_key.split('-')[0])
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

@app.route("/health", methods=['GET'])
def health():
    return 'OK', 200


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
    month_key = get_current_month_key()  # 提醒永遠抓「今天」所屬的月份
    group_id = LINE_GROUP_ID

    if not group_id:
        return 'no group id', 200

    data = get_schedule(group_id, month_key)
    if today_str not in data:
        return 'no event today', 200

    players = data[today_str]
    names = list(players.values())
    mentions = [f"@{name}" for name in names]

    msg = f"🏸 今天 {today_str} 要打球囉！\n出席：{' / '.join(names)}\n{' '.join(mentions)}"
    push_message(group_id, msg)
    return 'ok', 200


# ── Message handler ────────────────────────────────────────

@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    if event.source.type != 'group':
        reply(event.reply_token,
              "🏸 羽球出團小幫手\n\n群組內指令：\n• 我可以的日期 3/13/17 → 登記日期（自動補當月）\n• 我可以的日期 7月5/12 → 登記跨月日期\n• 我不可以的日期 1/2/4 → 反向登記，扣除指定日期後整月都登記可以\n• 更正日期 3/13 → 清掉重登（當月）\n• 取消日期 13 → 取消特定日期（當月）\n• 代登記 小王 3/13/17 → 幫人登記\n• 查詢統計 → 查看當月統計\n• 查詢統計 7月 → 查看指定月份統計\n• 場地費用\n  6/2 2030-2230 → 結算費用\n• 查詢費用 → 查看當月費用\n• 重置本月 → 清除當月所有資料\n• 重置 7月 → 清除指定月份資料")
        return

    group_id = event.source.group_id
    user_id = event.source.user_id
    text = event.message.text.strip()
    display_name = get_user_display_name(user_id, group_id)
    current_month_key = get_current_month_key()

    # 我可以的日期（支援跨月登記）
    if "我可以的日期" in text:
        idx = text.find("我可以的日期")
        date_part = text[idx + len("我可以的日期"):]
        date_pairs = parse_dates_with_month(date_part)
        if not date_pairs:
            reply(event.reply_token, f"@{display_name} 沒有偵測到有效日期！\n請用格式：我可以的日期 3/13/17 或 我可以的日期 7月5/12")
            return

        # 依月份分組儲存
        affected_month_keys = set()
        for mk, d in date_pairs:
            data = get_schedule(group_id, mk)
            if d not in data:
                data[d] = {}
            data[d][user_id] = display_name
            save_schedule(group_id, mk, data)
            affected_month_keys.add(mk)

        dates_display = '、'.join([d for _, d in date_pairs])
        msg = f"✅ 已記錄 {display_name} 在 {dates_display} 可以打球！\n\n"
        # 顯示所有受影響月份的統計
        for mk in sorted(affected_month_keys):
            msg += build_summary_message(group_id, mk) + "\n\n"
        reply(event.reply_token, msg.strip())

    # 我不可以的日期（反向登記：扣除指定日期，其餘整月自動登記為可以）
    elif "我不可以的日期" in text:
        idx = text.find("我不可以的日期")
        date_part = text[idx + len("我不可以的日期"):]
        mk, available_dates = parse_unavailable_dates_with_month(date_part)

        data = get_schedule(group_id, mk)
        for d in available_dates:
            if d not in data:
                data[d] = {}
            data[d][user_id] = display_name
        save_schedule(group_id, mk, data)

        display_month = int(mk.split('-')[1])
        msg = f"✅ 已記錄 {display_name} 在 {display_month}月除了指定日期外都可以打球！\n\n"
        msg += build_summary_message(group_id, mk)
        reply(event.reply_token, msg)

    # 更正日期（僅當月）
    elif text.startswith("更正日期"):
        date_part = text.replace("更正日期", "").strip()
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, "沒有偵測到有效日期！\n請用格式：更正日期 3/13/17")
            return
        data = get_schedule(group_id, current_month_key)
        for date_str in list(data.keys()):
            if user_id in data[date_str]:
                del data[date_str][user_id]
                if not data[date_str]:
                    del data[date_str]
        for d in dates:
            if d not in data:
                data[d] = {}
            data[d][user_id] = display_name
        save_schedule(group_id, current_month_key, data)
        msg = f"✅ 已更正 {display_name} 的日期為 {'、'.join(dates)}！\n\n"
        msg += build_summary_message(group_id, current_month_key)
        reply(event.reply_token, msg)

    # 取消日期（僅當月）
    elif text.startswith("取消日期"):
        date_part = text.replace("取消日期", "").strip()
        dates = parse_dates(date_part)
        data = get_schedule(group_id, current_month_key)
        removed = []
        for d in dates:
            if d in data and user_id in data[d]:
                del data[d][user_id]
                removed.append(d)
                if not data[d]:
                    del data[d]
        save_schedule(group_id, current_month_key, data)
        if removed:
            msg = f"✅ 已取消 {display_name} 在 {'、'.join(removed)} 的登記。\n\n"
            msg += build_summary_message(group_id, current_month_key)
        else:
            msg = f"找不到 {display_name} 在指定日期的登記。"
        reply(event.reply_token, msg)

    # 代登記（支援跨月）
    elif text.startswith("代登記"):
        rest = text.replace("代登記", "").strip()
        match = re.match(r'(\S+)\s+(.+)', rest)
        if not match:
            reply(event.reply_token, "請用格式：代登記 名字 3/13/17")
            return
        proxy_name = match.group(1)
        date_part = match.group(2)
        date_pairs = parse_dates_with_month(date_part)
        if not date_pairs:
            reply(event.reply_token, f"沒有偵測到有效日期！\n請用格式：代登記 {proxy_name} 3/13/17")
            return
        proxy_key = f"proxy_{proxy_name}"
        affected_month_keys = set()
        for mk, d in date_pairs:
            data = get_schedule(group_id, mk)
            if d not in data:
                data[d] = {}
            data[d][proxy_key] = proxy_name
            save_schedule(group_id, mk, data)
            affected_month_keys.add(mk)
        dates_display = '、'.join([d for _, d in date_pairs])
        msg = f"✅ 已代登記 {proxy_name} 在 {dates_display} 可以打球！\n\n"
        for mk in sorted(affected_month_keys):
            msg += build_summary_message(group_id, mk) + "\n\n"
        reply(event.reply_token, msg.strip())

    # 代更正（僅當月）
    elif text.startswith("代更正"):
        rest = text.replace("代更正", "").strip()
        match = re.match(r'(\S+)\s+(.+)', rest)
        if not match:
            reply(event.reply_token, "請用格式：代更正 名字 3/13/17")
            return
        proxy_name = match.group(1)
        date_part = match.group(2)
        dates = parse_dates(date_part)
        if not dates:
            reply(event.reply_token, f"沒有偵測到有效日期！\n請用格式：代更正 {proxy_name} 3/13/17")
            return
        proxy_key = f"proxy_{proxy_name}"
        data = get_schedule(group_id, current_month_key)
        for date_str in list(data.keys()):
            if proxy_key in data[date_str]:
                del data[date_str][proxy_key]
                if not data[date_str]:
                    del data[date_str]
        for d in dates:
            if d not in data:
                data[d] = {}
            data[d][proxy_key] = proxy_name
        save_schedule(group_id, current_month_key, data)
        msg = f"✅ 已更正 {proxy_name} 的日期為 {'、'.join(dates)}！\n\n"
        msg += build_summary_message(group_id, current_month_key)
        reply(event.reply_token, msg)

    # 代取消（僅當月）
    elif text.startswith("代取消"):
        rest = text.replace("代取消", "").strip()
        match = re.match(r'(\S+)\s+(.+)', rest)
        if not match:
            reply(event.reply_token, "請用格式：代取消 名字 13")
            return
        proxy_name = match.group(1)
        date_part = match.group(2)
        dates = parse_dates(date_part)
        proxy_key = f"proxy_{proxy_name}"
        data = get_schedule(group_id, current_month_key)
        removed = []
        for d in dates:
            if d in data and proxy_key in data[d]:
                del data[d][proxy_key]
                removed.append(d)
                if not data[d]:
                    del data[d]
        save_schedule(group_id, current_month_key, data)
        if removed:
            msg = f"✅ 已取消 {proxy_name} 在 {'、'.join(removed)} 的登記。\n\n"
            msg += build_summary_message(group_id, current_month_key)
        else:
            msg = f"找不到 {proxy_name} 在指定日期的登記。"
        reply(event.reply_token, msg)

    # 查詢統計（當月或指定月份）
    elif text in ["查詢統計", "出團統計", "羽球統計", "統計"]:
        reply(event.reply_token, build_summary_message(group_id, current_month_key))

    elif re.match(r'^(查詢統計|出團統計|羽球統計|統計)\s*\d{1,2}月$', text):
        month_match = re.search(r'(\d{1,2})月', text)
        target_month = int(month_match.group(1))
        target_mk = month_key_for(target_month)
        reply(event.reply_token, build_summary_message(group_id, target_mk))

    # 場地費用（支援指定月份）
    elif text.startswith("場地費用"):
        lines_input = text.split('\n')
        first_line = lines_input[0].strip()

        # 判斷第一行有沒有指定月份，例如「場地費用 7月」
        month_in_first = re.search(r'(\d{1,2})月', first_line)
        if month_in_first:
            target_month = int(month_in_first.group(1))
            target_mk = month_key_for(target_month)
        else:
            target_mk = current_month_key

        fee_lines = [l.strip() for l in lines_input[1:] if l.strip()]
        if not fee_lines:
            reply(event.reply_token, "請用格式：\n場地費用\n6/2 2030-2230\n\n或指定月份：\n場地費用 7月\n7/3 2030-2230")
            return

        schedule = get_schedule(group_id, target_mk)
        fees = get_fees(group_id, target_mk)
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

            discount = is_discount_slot(date_str, start_str, end_str, target_mk)
            rate = COURT_FEE_DISCOUNT if discount else COURT_FEE_NORMAL
            total = int(rate * hours)
            names_on_day = schedule[date_str]
            count = len(names_on_day)
            per_person = int(total / count)

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

        save_fees(group_id, target_mk, fees)

        if errors:
            result_lines.append("⚠️ 以下無法結算：")
            result_lines.extend(errors)
            result_lines.append("")

        total_by_person = {}
        for day_fees in fees.values():
            for name, amount in day_fees.items():
                total_by_person[name] = total_by_person.get(name, 0) + amount

        if total_by_person:
            result_lines.append("───────────")
            result_lines.append(f"{int(target_mk.split('-')[1])}月累計費用")
            for name, amount in total_by_person.items():
                result_lines.append(f"{name}：{amount}元")

        reply(event.reply_token, "\n".join(result_lines))

    # 查詢費用（當月）
    elif text in ["查詢費用", "費用統計", "費用"]:
        fees = get_fees(group_id, current_month_key)
        display_month = int(current_month_key.split('-')[1])
        if not fees:
            reply(event.reply_token, f"📊 {display_month}月還沒有費用記錄！")
            return

        total_by_person = {}
        lines_out = [f"💰 {display_month}月費用明細\n"]

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
        r.delete(f"schedule:{group_id}:{current_month_key}")
        r.delete(f"fees:{group_id}:{current_month_key}")
        reply(event.reply_token, "🗑️ 已清除本月所有登記及費用資料。")

    # 重置指定月份
    elif re.match(r'^重置\s*\d{1,2}月$', text):
        month_match = re.search(r'(\d{1,2})月', text)
        target_month = int(month_match.group(1))
        target_mk = month_key_for(target_month)
        r.delete(f"schedule:{group_id}:{target_mk}")
        r.delete(f"fees:{group_id}:{target_mk}")
        reply(event.reply_token, f"🗑️ 已清除 {target_month}月所有登記及費用資料。")


if __name__ == "__main__":
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
