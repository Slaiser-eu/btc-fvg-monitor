import os
import json
import smtplib
from email.message import EmailMessage
from datetime import datetime, timezone

import requests


# ============================================================
# CONFIG
# ============================================================

BINANCE_KLINES_URL = (
    "https://api.binance.com/api/v3/klines"
    "?symbol=BTCUSDT&interval=1h&limit=100"
)

BINANCE_PRICE_URL = (
    "https://api.binance.com/api/v3/ticker/price"
    "?symbol=BTCUSDT"
)

TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

STATE_FILE = "fvg_state.json"

# Сколько последних закрытых свечей считаем свежими
MAX_FRESH_CANDLES = 4

# Минимальный размер FVG
MIN_GAP_PERCENT = 0.03


# ============================================================
# BINANCE DATA
# ============================================================

def get_candles():
    response = requests.get(
        BINANCE_KLINES_URL,
        timeout=20
    )

    response.raise_for_status()

    data = response.json()

    candles = []

    for item in data:
        candles.append({
            "open_time": int(item[0]),
            "open": float(item[1]),
            "high": float(item[2]),
            "low": float(item[3]),
            "close": float(item[4]),
            "volume": float(item[5]),
            "close_time": int(item[6]),
        })

    return candles


def get_current_price():
    response = requests.get(
        BINANCE_PRICE_URL,
        timeout=20
    )

    response.raise_for_status()

    return float(response.json()["price"])


def is_closed(candle):
    now_ms = int(
        datetime.now(timezone.utc).timestamp() * 1000
    )

    return candle["close_time"] <= now_ms


# ============================================================
# CANDLE HELPERS
# ============================================================

def body(candle):
    return abs(
        candle["close"] - candle["open"]
    )


def candle_range(candle):
    return candle["high"] - candle["low"]


def is_bullish(candle):
    return candle["close"] > candle["open"]


def closes_in_upper_30_percent(candle):
    rng = candle_range(candle)

    if rng <= 0:
        return False

    return candle["close"] >= (
        candle["low"] + rng * 0.70
    )


def average_body(candles):
    if not candles:
        return 0

    return sum(
        body(c)
        for c in candles
    ) / len(candles)


# ============================================================
# STRONG BULLISH IMPULSE
# ============================================================

def strong_bullish_impulse(candles, index):
    """
    Сильная импульсная свеча:
    минимум 2 из 3 групп условий.

    Группа 1:
    тело >= 1.7 x среднего тела последних 10 свечей

    Группа 2:
    тело >= 0.65% цены
    ИЛИ
    тело >= 2 x тело предыдущей свечи

    Группа 3:
    свеча бычья и закрытие в верхних 30% диапазона
    """

    if index <= 0:
        return False

    candle = candles[index]
    previous = candles[index - 1]

    if not is_bullish(candle):
        return False

    previous_10 = candles[
        max(0, index - 10):index
    ]

    avg_body = average_body(previous_10)

    # Группа 1
    group_1 = (
        avg_body > 0
        and body(candle) >= 1.7 * avg_body
    )

    # Группа 2
    body_percent = (
        body(candle)
        / candle["open"]
        * 100
    )

    group_2 = (
        body_percent >= 0.65
        or body(candle) >= 2 * body(previous)
    )

    # Группа 3
    group_3 = closes_in_upper_30_percent(candle)

    score = sum([
        group_1,
        group_2,
        group_3
    ])

    return score >= 2


# ============================================================
# GAP CALCULATIONS
# ============================================================

def gap_size_percent(gap_low, gap_high):
    midpoint = (
        gap_low + gap_high
    ) / 2

    if midpoint <= 0:
        return 0

    return (
        (gap_high - gap_low)
        / midpoint
        * 100
    )


def significant_gap(gap_low, gap_high):
    return (
        gap_size_percent(
            gap_low,
            gap_high
        )
        >= MIN_GAP_PERCENT
    )


# ============================================================
# STATE
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {
            "sent": []
        }

    try:
        with open(
            STATE_FILE,
            "r",
            encoding="utf-8"
        ) as file:
            state = json.load(file)

        if "sent" not in state:
            state["sent"] = []

        return state

    except Exception:
        return {
            "sent": []
        }


def save_state(state):
    # Оставляем последние 100 записей
    state["sent"] = state.get(
        "sent",
        []
    )[-100:]

    with open(
        STATE_FILE,
        "w",
        encoding="utf-8"
    ) as file:
        json.dump(
            state,
            file,
            ensure_ascii=False,
            indent=2
        )


# ============================================================
# TIME
# ============================================================

def format_time(timestamp_ms):
    dt = datetime.fromtimestamp(
        timestamp_ms / 1000,
        timezone.utc
    )

    return dt.strftime(
        "%Y-%m-%d %H:%M UTC"
    )


# ============================================================
# CONFIRMED FVG
# ============================================================

def find_confirmed_fvg(candles, current_price):
    """
    Подтверждённый bullish FVG:

    Candle 1
    Candle 2 = сильный бычий импульс
    Candle 3

    Low 3 > High 1
    """

    signals = []

    last_index = len(candles) - 1

    first_index = max(
        2,
        last_index - MAX_FRESH_CANDLES + 1
    )

    for i in range(
        first_index,
        last_index + 1
    ):

        c1 = candles[i - 2]
        c2 = candles[i - 1]
        c3 = candles[i]

        if not (
            is_closed(c1)
            and is_closed(c2)
            and is_closed(c3)
        ):
            continue

        # Вторая свеча должна быть бычьей
        if not is_bullish(c2):
            continue

        # Вторая свеча должна быть импульсной
        if not strong_bullish_impulse(
            candles,
            i - 1
        ):
            continue

        # Главное условие FVG
        if c3["low"] <= c1["high"]:
            continue

        gap_low = c1["high"]
        gap_high = c3["low"]

        # Слишком маленький гэп игнорируем
        if not significant_gap(
            gap_low,
            gap_high
        ):
            continue

        # Если цена уже ниже/на нижней границе,
        # зона считается полностью перекрытой
        if current_price <= gap_low:
            continue

        key = (
            "CONFIRMED|"
            f"{c1['open_time']}|"
            f"{c2['open_time']}|"
            f"{c3['open_time']}"
        )

        signals.append({
            "key": key,
            "type": "Подтверждённый лонговый FVG",
            "candle_time": format_time(
                c3["open_time"]
            ),
            "gap_low": gap_low,
            "gap_high": gap_high,
            "comment": (
                "Свежий подтверждённый "
                "bullish FVG. Зона ещё "
                "не перекрыта полностью."
            )
        })

    return signals


# ============================================================
# EARLY FVG
# ============================================================

def find_early_fvg(candles):
    """
    Возможный FVG после второй свечи.

    Последняя закрытая свеча является
    сильным бычьим импульсом.

    Проверяем потенциал гэпа относительно
    high предыдущей свечи.
    """

    signals = []

    i = len(candles) - 1

    if i < 11:
        return signals

    previous = candles[i - 1]
    impulse = candles[i]

    if not is_closed(impulse):
        return signals

    if not is_bullish(impulse):
        return signals

    if not strong_bullish_impulse(
        candles,
        i
    ):
        return signals

    # Должен быть потенциал гэпа
    if impulse["close"] <= previous["high"]:
        return signals

    gap_low = previous["high"]
    gap_high = impulse["close"]

    if not significant_gap(
        gap_low,
        gap_high
    ):
        return signals

    key = (
        "EARLY|"
        f"{previous['open_time']}|"
        f"{impulse['open_time']}"
    )

    signals.append({
        "key": key,
        "type": (
            "Возможный лонговый FVG "
            "(после 2-й свечи)"
        ),
        "candle_time": format_time(
            impulse["open_time"]
        ),
        "gap_low": gap_low,
        "gap_high": gap_high,
        "comment": (
            "Ранний сигнал: сильная бычья "
            "свеча создала потенциал "
            "bullish FVG. Следующая свеча "
            "может подтвердить гэп."
        )
    })

    return signals


# ============================================================
# MESSAGE
# ============================================================

def make_message(signal, current_price):
    gap_percent = gap_size_percent(
        signal["gap_low"],
        signal["gap_high"]
    )

    return (
        "🟢 BTCUSDT 1H\n\n"
        f"Тип: {signal['type']}\n\n"
        f"Время свечи: "
        f"{signal['candle_time']}\n\n"
        f"Уровни гэпа: "
        f"{signal['gap_low']:.2f} — "
        f"{signal['gap_high']:.2f}\n\n"
        f"Размер гэпа: "
        f"{gap_percent:.3f}%\n\n"
        f"Текущая цена: "
        f"{current_price:.2f}\n\n"
        f"Комментарий: "
        f"{signal['comment']}"
    )


# ============================================================
# TELEGRAM
# ============================================================

def send_telegram(message):
    url = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )

    response = requests.post(
        url,
        json={
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message
        },
        timeout=20
    )

    response.raise_for_status()


# ============================================================
# EMAIL
# ============================================================

def send_email(message):
    email_from = os.environ.get(
        "EMAIL_FROM"
    )

    email_to = os.environ.get(
        "EMAIL_TO"
    )

    smtp_user = os.environ.get(
        "SMTP_USER"
    )

    smtp_password = os.environ.get(
        "SMTP_PASSWORD"
    )

    # Если Email пока не настроен,
    # просто пропускаем его.
    if not all([
        email_from,
        email_to,
        smtp_user,
        smtp_password
    ]):
        return

    email = EmailMessage()

    email["Subject"] = (
        "BTCUSDT — новый лонговый FVG"
    )

    email["From"] = email_from
    email["To"] = email_to

    email.set_content(message)

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465
    ) as server:

        server.login(
            smtp_user,
            smtp_password
        )

        server.send_message(email)


# ============================================================
# MAIN
# ============================================================

def main():

    try:
        candles = get_candles()
        current_price = get_current_price()

    except Exception as error:
        print(
            f"ERROR_BINANCE: {error}"
        )
        return

    # Только закрытые свечи
    candles = [
        candle
        for candle in candles
        if is_closed(candle)
    ]

    if len(candles) < 20:
        print("NO_SIGNAL")
        return

    state = load_state()

    signals = []

    # Подтверждённые FVG
    signals.extend(
        find_confirmed_fvg(
            candles,
            current_price
        )
    )

    # Ранние FVG
    signals.extend(
        find_early_fvg(
            candles
        )
    )

    fresh_signals = []

    for signal in signals:

        if signal["key"] not in state["sent"]:
            fresh_signals.append(signal)

    # Нет нового сигнала
    if not fresh_signals:
        print("NO_SIGNAL")

        # Сохраняем state, чтобы файл
        # всегда существовал
        save_state(state)

        return

    # Отправляем только новые сигналы
    for signal in fresh_signals:

        message = make_message(
            signal,
            current_price
        )

        # Telegram
        send_telegram(message)

        # Email, если настроен
        send_email(message)

        # Запоминаем сигнал
        state["sent"].append(
            signal["key"]
        )

        print(message)

    save_state(state)


if __name__ == "__main__":
    main()
