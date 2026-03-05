import os
import time
import threading
import requests
from typing import Dict, Any, List, Optional, Tuple

# =========================
# ENV VARS (Render/локально)
# =========================
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
# Можно ограничить доступ только твоим chat_id (рекомендую)
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()  # например "123456789" или пусто

# =========================
# Binance P2P settings
# =========================
P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (p2p-scanner)"
}

MIN_USER_TRADES = 200
MIN_COMPLETION_RATE = 95.0

POLL_SECONDS = 15
ROWS = 20
ASSET = "USDT"
FIAT = "EUR"

# =========================
# Telegram API helper
# =========================
TG_API = "https://api.telegram.org/bot{}/{}"

def tg_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN is empty. Set env var TG_TOKEN.")
    url = TG_API.format(TG_TOKEN, method)
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()

def tg_send(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    tg_call("sendMessage", payload)

# =========================
# Utils
# =========================
def safe_float(x, default=None):
    try:
        return float(str(x).replace(",", "."))
    except Exception:
        return default

def safe_int(x, default=0):
    try:
        return int(x)
    except Exception:
        return default

def completion_to_percent(val) -> float:
    v = safe_float(val, 0.0)
    if v <= 1.0:
        v *= 100.0
    return v

def fetch_ads(trade_type: str, pay_types: List[str], rows: int = ROWS) -> Dict[str, Any]:
    payload = {
        "page": 1,
        "rows": rows,
        "payTypes": pay_types,   # [] => любые
        "asset": ASSET,
        "fiat": FIAT,
        "tradeType": trade_type  # "BUY" / "SELL"
    }
    r = requests.post(P2P_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()

def get_price(item: Dict[str, Any]) -> Optional[float]:
    adv = item.get("adv") or {}
    return safe_float(adv.get("price"), None)

def passes_filters(item: Dict[str, Any], amount_fiat: float) -> bool:
    adv = item.get("adv") or {}
    advertiser = item.get("advertiser") or {}

    min_single = safe_float(adv.get("minSingleTransAmount"), None)
    max_single = safe_float(adv.get("maxSingleTransAmount"), None)
    if min_single is None or max_single is None:
        return False
    if not (min_single <= amount_fiat <= max_single):
        return False

    trades = advertiser.get("monthOrderCount") or advertiser.get("orderCount") or 0
    trades = safe_int(trades, 0)

    completion = advertiser.get("monthFinishRate") or advertiser.get("finishRate") or 0
    completion = completion_to_percent(completion)

    if trades < MIN_USER_TRADES:
        return False
    if completion < MIN_COMPLETION_RATE:
        return False

    return True

def pct(buy_price: float, sell_price: float) -> float:
    return (sell_price - buy_price) / buy_price * 100.0

def best_pair(
    buys: List[Dict[str, Any]],
    sells: List[Dict[str, Any]],
    amount_fiat: float
) -> Optional[Tuple[float, Dict[str, Any], Dict[str, Any]]]:
    buy_ok = [b for b in buys if passes_filters(b, amount_fiat)]
    sell_ok = [s for s in sells if passes_filters(s, amount_fiat)]

    best = None
    for b in buy_ok:
        bp = get_price(b)
        if bp is None:
            continue
        for s in sell_ok:
            sp = get_price(s)
            if sp is None:
                continue
            spread = pct(bp, sp)
            if best is None or spread > best[0]:
                best = (spread, b, s)
    return best

def recommend_threshold(balance: float) -> float:
    # "Нормальный" порог: чем меньше баланс — тем выше надо ставить, чтобы время/срыв окупались
    if balance < 100:
        return 2.4
    if balance < 500:
        return 1.9
    if balance < 2000:
        return 1.5
    return 1.2

# =========================
# Bot state + scanner thread
# =========================
STATE = {
    "chat_id": None,           # куда слать сигналы
    "balance": 100.0,
    "pay_types": ["SEPA"],     # ["SEPA"] / ["Revolut"] / ["Wise"] / [] (ANY)
    "running": False,
    "threshold": 1.9
}

_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_last_signal_key = None

def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID

def scanner_loop():
    global _last_signal_key
    while not _stop_event.is_set():
        try:
            chat_id = STATE["chat_id"]
            balance = float(STATE["balance"])
            pay_types = list(STATE["pay_types"])
            threshold = float(STATE["threshold"])

            buy_data = fetch_ads("BUY", pay_types, rows=ROWS)
            sell_data = fetch_ads("SELL", pay_types, rows=ROWS)

            buys = buy_data.get("data") or []
            sells = sell_data.get("data") or []

            pair = best_pair(buys, sells, balance)
            if not pair:
                tg_send(chat_id, "Нет подходящих связок под лимиты/фильтры. Ждём…")
                time.sleep(POLL_SECONDS)
                continue

            spread, b, s = pair
            bp = get_price(b)
            sp = get_price(s)

            b_name = (b.get("advertiser") or {}).get("nickName", "seller")
            s_name = (s.get("advertiser") or {}).get("nickName", "buyer")

            key = (round(bp or 0, 6), round(sp or 0, 6), round(spread, 4))

            msg = (
                f"BUY {bp:.4f} ({b_name}) -> SELL {sp:.4f} ({s_name})\n"
                f"spread={spread:.2f}% | amount={balance:.2f} {FIAT}\n"
                f"payTypes={'ANY' if not pay_types else ','.join(pay_types)} | threshold>={threshold:.1f}%"
            )

            if spread >= threshold and key != _last_signal_key:
                b_adv = b.get("adv") or {}
                s_adv = s.get("adv") or {}
                limits = (
                    f"BUY limits : {b_adv.get('minSingleTransAmount')}..{b_adv.get('maxSingleTransAmount')} {FIAT}\n"
                    f"SELL limits: {s_adv.get('minSingleTransAmount')}..{s_adv.get('maxSingleTransAmount')} {FIAT}"
                )
                tg_send(chat_id, "🔥 SIGNAL\n" + msg + "\n" + limits)
                _last_signal_key = key

            time.sleep(POLL_SECONDS)

        except Exception as e:
            try:
                if STATE["chat_id"]:
                    tg_send(STATE["chat_id"], f"Ошибка: {repr(e)}")
            except Exception:
                pass
            time.sleep(max(10, POLL_SECONDS))

# =========================
# Telegram updates loop
# =========================
def pay_buttons() -> Dict[str, Any]:
    # inline клавиатура
    return {
        "inline_keyboard": [[
            {"text": "SEPA", "callback_data": "PAY:SEPA"},
            {"text": "Revolut", "callback_data": "PAY:Revolut"},
            {"text": "Wise", "callback_data": "PAY:Wise"},
            {"text": "Любые", "callback_data": "PAY:ANY"},
        ]]
    }

def start_scanner(chat_id: int):
    global _worker_thread
    if STATE["running"]:
        tg_send(chat_id, "Уже запущено. /status")
        return
    STATE["running"] = True
    _stop_event.clear()
    _worker_thread = threading.Thread(target=scanner_loop, daemon=True)
    _worker_thread.start()
    tg_send(chat_id, "✅ Сканер запущен. Я буду присылать SIGNAL, когда найду спред выше порога.")

def stop_scanner(chat_id: int):
    if not STATE["running"]:
        tg_send(chat_id, "Уже остановлено. /status")
        return
    STATE["running"] = False
    _stop_event.set()
    tg_send(chat_id, "🛑 Остановлено.")

def status(chat_id: int):
    tg_send(
        chat_id,
        "📌 Статус\n"
        f"running: {STATE['running']}\n"
        f"balance: {STATE['balance']} {FIAT}\n"
        f"payTypes: {'ANY' if not STATE['pay_types'] else ','.join(STATE['pay_types'])}\n"
        f"threshold: {STATE['threshold']:.1f}%\n"
        f"filters: trades>={MIN_USER_TRADES}, completion>={MIN_COMPLETION_RATE}%"
    )

def handle_message(chat_id: int, text: str):
    # Команды
    if text.startswith("/start"):
        STATE["chat_id"] = chat_id
        tg_send(chat_id,
                "Привет! Я P2P-сканер.\n"
                "1) /config — выбрать оплату кнопкой\n"
                "2) Напиши: balance 250  (чтобы задать баланс)\n"
                "3) /run — запустить, /stop — остановить\n"
                "4) /status — статус")
        return

    if text.startswith("/config"):
        tg_send(chat_id, "Выбери метод оплаты:", reply_markup=pay_buttons())
        return

    if text.startswith("/run"):
        STATE["chat_id"] = chat_id
        start_scanner(chat_id)
        return

    if text.startswith("/stop"):
        stop_scanner(chat_id)
        return

    if text.startswith("/status"):
        status(chat_id)
        return

    # Установка баланса: "balance 150"
    if text.lower().startswith("balance"):
        parts = text.split()
        if len(parts) >= 2:
            v = safe_float(parts[1], None)
            if v is None or v <= 0:
                tg_send(chat_id, "Баланс должен быть числом > 0. Пример: balance 200")
                return
            STATE["balance"] = float(v)
            STATE["threshold"] = recommend_threshold(float(v))
            tg_send(chat_id, f"✅ Баланс установлен: {v:.2f} {FIAT}\nПорог (бот выбрал): {STATE['threshold']:.1f}%")
            return
        tg_send(chat_id, "Пример: balance 200")
        return

    tg_send(chat_id, "Не понял. Команды: /config /run /stop /status или: balance 200")

def handle_callback(chat_id: int, data: str):
    if data.startswith("PAY:"):
        val = data.split(":", 1)[1]
        if val == "ANY":
            STATE["pay_types"] = []
        else:
            STATE["pay_types"] = [val]
        tg_send(chat_id, f"✅ Метод оплаты: {'ANY' if not STATE['pay_types'] else STATE['pay_types'][0]}")
        status(chat_id)

def run_updates_loop():
    offset = 0
    tg_send(int(ALLOWED_CHAT_ID) if ALLOWED_CHAT_ID else 0, "")  # no-op attempt? removed; keep simple

def main():
    if not TG_TOKEN:
        raise RuntimeError("Set env var TG_TOKEN")

    offset = 0
    print("Telegram bot started (long polling).")

    while True:
        try:
            data = tg_call("getUpdates", {"timeout": 30, "offset": offset})
            for upd in data.get("result", []):
                offset = upd["update_id"] + 1

                # messages
                if "message" in upd and "text" in upd["message"]:
                    chat_id = upd["message"]["chat"]["id"]
                    if not is_allowed(chat_id):
                        continue
                    text = upd["message"]["text"]
                    handle_message(chat_id, text)

                # inline buttons
                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    if not is_allowed(chat_id):
                        continue
                    cb_data = cq.get("data", "")
                    # ack callback
                    try:
                        tg_call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    except Exception:
                        pass
                    handle_callback(chat_id, cb_data)

        except Exception as e:
            print("Error:", repr(e))
            time.sleep(5)

if __name__ == "__main__":
    main()
