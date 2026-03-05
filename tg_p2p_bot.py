import os
import time
import threading
import requests
from typing import Dict, Any, List, Optional, Tuple

# =========================
# ENV VARS
# =========================
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()  # например "123456789" или пусто

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-4.1-mini").strip()
AI_DEFAULT_ENABLED = os.getenv("AI_ENABLED", "0").strip() in ("1", "true", "True", "yes", "YES")

# =========================
# Binance P2P
# =========================
P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (p2p-scanner)"
}

MIN_USER_TRADES = 50
MIN_COMPLETION_RATE = 90.0
DEFAULT_THRESHOLD = 1.0     # было 1.9
ROWS = 100                 # было 20
PAGES = 5                  # сколько страниц брать (5*100=500 объявлений)
ASSET = "USDT"
FIAT = "EUR"

# =========================
# Telegram API
# =========================
TG_API = "https://api.telegram.org/bot{}/{}"


def tg_call(method: str, payload: Dict[str, Any]) -> Dict[str, Any]:
    if not TG_TOKEN:
        raise RuntimeError("TG_TOKEN is empty. Set env var TG_TOKEN.")
    url = TG_API.format(TG_TOKEN, method)
    r = requests.post(url, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()


def tg_send(chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> int:
    payload = {"chat_id": chat_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    resp = tg_call("sendMessage", payload)
    try:
        return int(resp["result"]["message_id"])
    except Exception:
        return 0


def tg_edit(chat_id: int, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> None:
    if not message_id:
        return
    payload = {"chat_id": chat_id, "message_id": message_id, "text": text, "disable_web_page_preview": True}
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        tg_call("editMessageText", payload)
    except Exception:
        # Telegram иногда не даёт слишком часто редактировать/тот же текст — игнор
        pass


def tg_action(chat_id: int, action: str = "typing") -> None:
    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        pass


# =========================
# OpenAI helper (Responses API)
# =========================
OPENAI_URL = "https://api.openai.com/v1/responses"


def openai_score(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Возвращает {"ok": bool, "score": float(0..1), "reason": str}
    Если ключа нет или ошибка — возвращаем None (и тогда пропускаем AI-фильтр).
    """
    if not OPENAI_API_KEY:
        return None

    prompt = (
        "Ты помощник для P2P-арбитража. Оцени вероятность, что круг BUY->SELL реально исполнить без срыва.\n"
        "Верни ТОЛЬКО JSON вида: {\"ok\": true/false, \"score\": 0..1, \"reason\": \"...\"}\n"
        "Учитывай: spread, лимиты, trades, completion, метод оплаты, сумма. "
        "Если риск высокий (лимиты впритык/мало сделок/низкое завершение/слишком подозрительный спред) — ok=false.\n"
        f"Данные: {signal}"
    )

    headers = {
        "Authorization": f"Bearer {OPENAI_API_KEY}",
        "Content-Type": "application/json",
    }
    body = {
        "model": OPENAI_MODEL,
        "input": prompt,
        "reasoning": {"effort": "none"},
    }

    r = requests.post(OPENAI_URL, headers=headers, json=body, timeout=30)
    r.raise_for_status()
    data = r.json()

    text = data.get("output_text")
    if not text:
        try:
            text = data["output"][0]["content"][0]["text"]
        except Exception:
            return None

    text = text.strip()
    if not (text.startswith("{") and text.endswith("}")):
        return None

    try:
        import json
        obj = json.loads(text)
        ok = bool(obj.get("ok"))
        score = float(obj.get("score", 0.0))
        reason = str(obj.get("reason", ""))[:300]
        score = max(0.0, min(1.0, score))
        return {"ok": ok, "score": score, "reason": reason}
    except Exception:
        return None


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


def fetch_ads(trade_type: str, pay_types: List[str], rows: int, page: int = 1) -> Dict[str, Any]:
    payload = {
        "page": page,
        "rows": rows,
        "payTypes": pay_types,
        "asset": ASSET,
        "fiat": FIAT,
        "tradeType": trade_type
    }
    r = requests.post(P2P_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()
    
def fetch_multi_pages(trade_type: str, pay_types: List[str], rows: int, pages: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in range(1, pages + 1):
        data = fetch_ads(trade_type, pay_types, rows=rows, page=p)
        out.extend(data.get("data") or [])
        time.sleep(0.2)  # маленькая пауза, чтобы не долбить Binance
    return out

def get_price(item: Dict[str, Any]) -> Optional[float]:
    adv = item.get("adv") or {}
    return safe_float(adv.get("price"), None)


def get_limits(item: Dict[str, Any]) -> Tuple[Optional[float], Optional[float]]:
    adv = item.get("adv") or {}
    return safe_float(adv.get("minSingleTransAmount"), None), safe_float(adv.get("maxSingleTransAmount"), None)


def get_adv_stats(item: Dict[str, Any]) -> Tuple[int, float]:
    advr = item.get("advertiser") or {}
    trades = advr.get("monthOrderCount") or advr.get("orderCount") or 0
    trades = safe_int(trades, 0)
    completion = advr.get("monthFinishRate") or advr.get("finishRate") or 0
    completion = completion_to_percent(completion)
    return trades, completion


def passes_filters(item: Dict[str, Any], amount_fiat: float) -> bool:
    mn, mx = get_limits(item)
    if mn is None or mx is None:
        return False
    if not (mn <= amount_fiat <= mx):
        return False
    trades, completion = get_adv_stats(item)
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


def top_pairs(
    buys: List[Dict[str, Any]],
    sells: List[Dict[str, Any]],
    amount_fiat: float,
    n: int = 5
) -> List[Tuple[float, Dict[str, Any], Dict[str, Any]]]:
    buy_ok = [b for b in buys if passes_filters(b, amount_fiat)]
    sell_ok = [s for s in sells if passes_filters(s, amount_fiat)]

    pairs: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
    for b in buy_ok:
        bp = get_price(b)
        if bp is None:
            continue
        for s in sell_ok:
            sp = get_price(s)
            if sp is None:
                continue
            pairs.append((pct(bp, sp), b, s))

    pairs.sort(key=lambda x: x[0], reverse=True)
    return pairs[:n]


def recommend_threshold(balance: float) -> float:
    if balance < 100:
        return 2.4
    if balance < 500:
        return 1.9
    if balance < 2000:
        return 1.5
    return 1.2


# =========================
# Texts / UI
# =========================
def help_text() -> str:
    return (
        "📖 Инструкция по боту\n\n"
        "1) /start\n"
        "2) Введи баланс сообщением:\n"
        "   balance 250\n\n"
        "3) Настрой кнопками:\n"
        "   💳 Оплата — SEPA/Revolut/Wise/ANY\n"
        "   ⏱ Скорость — как часто бот сканирует рынок (5–30 секунд)\n"
        "   🤖 AI — фильтр рискованных сигналов (по желанию)\n\n"
        "4) ▶️ Старт — начнёт постоянное сканирование\n"
        "5) 📋 ТОП-5 — покажет 5 лучших связок прямо сейчас\n\n"
        "Важно:\n"
        "• Не пиши 'crypto/binance' в комментарии перевода.\n"
        "• Не принимай оплату от третьих лиц.\n"
    )


def status_text() -> str:
    return (
        "📌 Статус\n"
        f"running: {STATE['running']}\n"
        f"balance: {STATE['balance']} {FIAT} (set={STATE['balance_set']})\n"
        f"payTypes: {'ANY' if not STATE['pay_types'] else ','.join(STATE['pay_types'])} (set={STATE['pay_set']})\n"
        f"threshold: {STATE['threshold']:.1f}%\n"
        f"speed: {STATE['poll_seconds']}s | rows: {STATE['rows']}\n"
        f"AI: {'ON' if STATE['ai_enabled'] else 'OFF'}\n"
        f"filters: trades>={MIN_USER_TRADES}, completion>={MIN_COMPLETION_RATE}%"
    )


def main_menu() -> Dict[str, Any]:
    pay = "ANY" if not STATE["pay_types"] else STATE["pay_types"][0]
    ai = "ON" if STATE["ai_enabled"] else "OFF"
    spd = f"{STATE['poll_seconds']}s"
    return {
        "inline_keyboard": [
            [
                {"text": f"💳 Оплата: {pay}", "callback_data": "MENU:PAY"},
                {"text": f"🤖 AI: {ai}", "callback_data": "MENU:AI"},
            ],
            [
                {"text": f"⏱ Скорость: {spd}", "callback_data": "MENU:SPEED"},
                {"text": f"⚡ Порог: {STATE['threshold']:.1f}%", "callback_data": "MENU:THR"},
                {"text": "📌 Статус", "callback_data": "MENU:STATUS"},
            ],
            [
                {"text": "📋 ТОП-5", "callback_data": "MENU:TOP5"},
                {"text": "📖 Инструкция", "callback_data": "MENU:HELP"},
            ],
            [
                {"text": "⚙️ Настроить и старт", "callback_data": "MENU:SETUPRUN"},
            ],
            [
                {"text": "▶️ Старт", "callback_data": "MENU:RUN"},
                {"text": "⏸ Стоп", "callback_data": "MENU:STOP"},
            ],
        ]
    }


def pay_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "SEPA", "callback_data": "PAY:SEPA"},
            {"text": "Revolut", "callback_data": "PAY:Revolut"},
            {"text": "Wise", "callback_data": "PAY:Wise"},
            {"text": "Любые", "callback_data": "PAY:ANY"},
        ], [
            {"text": "⬅️ Назад", "callback_data": "BACK:MENU"}
        ]]
    }


def speed_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "5s", "callback_data": "SPD:5"},
            {"text": "10s", "callback_data": "SPD:10"},
            {"text": "15s", "callback_data": "SPD:15"},
            {"text": "30s", "callback_data": "SPD:30"},
        ], [
            {"text": "⬅️ Назад", "callback_data": "BACK:MENU"}
        ]]
    }

def threshold_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [[
            {"text": "0.5%", "callback_data": "THR:0.5"},
            {"text": "0.8%", "callback_data": "THR:0.8"},
            {"text": "1.0%", "callback_data": "THR:1.0"},
            {"text": "1.2%", "callback_data": "THR:1.2"},
        ], [
            {"text": "1.5%", "callback_data": "THR:1.5"},
            {"text": "2.0%", "callback_data": "THR:2.0"},
            {"text": "⬅️ Назад", "callback_data": "BACK:MENU"},
        ]]
    }


# =========================
# Bot state + worker
# =========================
STATE = {
    "chat_id": None,
    "balance": 0.0,
    "balance_set": False,       # станет True после "balance 250"
    "pay_types": ["SEPA"],      # дефолт
    "pay_set": False,           # станет True после выбора кнопкой оплаты
    "running": False,
    "threshold": 1.2,
    "poll_seconds": 15,
    "rows": 100,
    "ai_enabled": AI_DEFAULT_ENABLED,
    "scan_msg_id": 0,
}

_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_last_signal_key = None
_spinner = ["⠋","⠙","⠹","⠸","⠼","⠴","⠦","⠧","⠇","⠏"]


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def preflight(chat_id: int) -> bool:
    """Проверяем, что баланс задан и оплата выбрана."""
    if not STATE["balance_set"]:
        tg_send(chat_id, "Сначала введи баланс сообщением: balance 250", reply_markup=main_menu())
        return False
    if not STATE["pay_set"]:
        tg_send(chat_id, "Сначала выбери метод оплаты кнопкой 💳", reply_markup=pay_buttons())
        return False
    return True


def scanner_loop():
    global _last_signal_key
    spin_i = 0
    last_edit = 0.0

    while not _stop_event.is_set():
        try:
            chat_id = STATE["chat_id"]
            balance = float(STATE["balance"])
            pay_types = list(STATE["pay_types"])
            threshold = float(STATE["threshold"])
            poll = int(STATE["poll_seconds"])
            rows = int(STATE["rows"])

            tg_action(chat_id, "typing")
            now = time.time()
            if STATE["scan_msg_id"] and (now - last_edit) > 3:
                icon = _spinner[spin_i % len(_spinner)]
                spin_i += 1
                tg_edit(
                    chat_id,
                    STATE["scan_msg_id"],
                    f"🔎 Сканирую P2P… {icon}\n"
                    f"Оплата: {'ANY' if not pay_types else pay_types[0]} | Баланс: {balance:.2f} {FIAT}\n"
                    f"Порог: {threshold:.1f}% | Скорость: {poll}s | AI: {'ON' if STATE['ai_enabled'] else 'OFF'}",
                    reply_markup=main_menu()
                )
                last_edit = now

           buys = fetch_multi_pages("BUY", pay_types, rows=ROWS, pages=PAGES)
           sells = fetch_multi_pages("SELL", pay_types, rows=ROWS, pages=PAGES)

            pair = best_pair(buys, sells, balance)
            if not pair:
                time.sleep(poll)
                continue

            spread, b, s = pair
            bp = get_price(b)
            sp = get_price(s)

            b_name = (b.get("advertiser") or {}).get("nickName", "seller")
            s_name = (s.get("advertiser") or {}).get("nickName", "buyer")

            key = (round(bp or 0, 6), round(sp or 0, 6), round(spread, 4))

            if spread >= threshold and key != _last_signal_key:
                b_adv = b.get("adv") or {}
                s_adv = s.get("adv") or {}
                b_mn, b_mx = get_limits(b)
                s_mn, s_mx = get_limits(s)
                b_tr, b_comp = get_adv_stats(b)
                s_tr, s_comp = get_adv_stats(s)

                signal = {
                    "fiat": FIAT,
                    "asset": ASSET,
                    "amount": balance,
                    "payType": ("ANY" if not pay_types else pay_types[0]),
                    "spread_percent": round(spread, 4),
                    "buy_price": bp,
                    "sell_price": sp,
                    "buy_limits": [b_mn, b_mx],
                    "sell_limits": [s_mn, s_mx],
                    "buy_trades": b_tr,
                    "buy_completion": b_comp,
                    "sell_trades": s_tr,
                    "sell_completion": s_comp,
                }

                ai_note = ""
                if STATE["ai_enabled"]:
                    ai = openai_score(signal)
                    if ai is not None:
                        if not ai["ok"]:
                            _last_signal_key = key
                            tg_send(chat_id, f"⚠️ AI отсеял сигнал (score={ai['score']:.2f}): {ai['reason']}")
                            time.sleep(poll)
                            continue
                        ai_note = f"\n🤖 AI: OK (score={ai['score']:.2f}) — {ai['reason']}"

                msg = (
                    "🔥 SIGNAL\n"
                    f"BUY {bp:.4f} ({b_name}) -> SELL {sp:.4f} ({s_name})\n"
                    f"spread={spread:.2f}% | amount={balance:.2f} {FIAT}\n"
                    f"payTypes={'ANY' if not pay_types else ','.join(pay_types)} | threshold>={threshold:.1f}%\n"
                    f"BUY limits : {b_adv.get('minSingleTransAmount')}..{b_adv.get('maxSingleTransAmount')} {FIAT}\n"
                    f"SELL limits: {s_adv.get('minSingleTransAmount')}..{s_adv.get('maxSingleTransAmount')} {FIAT}"
                    + ai_note
                )
                tg_send(chat_id, msg)
                _last_signal_key = key

            time.sleep(poll)

        except Exception as e:
            try:
                if STATE["chat_id"]:
                    tg_send(STATE["chat_id"], f"Ошибка: {repr(e)}")
            except Exception:
                pass
            time.sleep(max(5, int(STATE["poll_seconds"])))


def start_scanner(chat_id: int):
    global _worker_thread
    if not preflight(chat_id):
        return
    if STATE["running"]:
        tg_send(chat_id, "Уже запущено.", reply_markup=main_menu())
        return

    STATE["chat_id"] = chat_id
    STATE["running"] = True
    _stop_event.clear()

    STATE["scan_msg_id"] = tg_send(chat_id, "🔎 Сканирую P2P…", reply_markup=main_menu())
    _worker_thread = threading.Thread(target=scanner_loop, daemon=True)
    _worker_thread.start()


def stop_scanner(chat_id: int):
    if not STATE["running"]:
        tg_send(chat_id, "Уже остановлено.", reply_markup=main_menu())
        return
    STATE["running"] = False
    _stop_event.set()
    tg_send(chat_id, "⏸ Остановлено.", reply_markup=main_menu())


def send_top5(chat_id: int):
    if not preflight(chat_id):
        return

    tg_action(chat_id, "typing")
    mid = tg_send(chat_id, "🔎 Ищу ТОП-5 связок…", reply_markup=main_menu())

    balance = float(STATE["balance"])
    pay_types = list(STATE["pay_types"])
    rows = max(30, int(STATE["rows"]))  # чуть шире для top5

    buy_data = fetch_ads("BUY", pay_types, rows=rows)
    sell_data = fetch_ads("SELL", pay_types, rows=rows)
    buys = buy_data.get("data") or []
    sells = sell_data.get("data") or []

    pairs = top_pairs(buys, sells, balance, n=5)
    if not pairs:
        tg_edit(chat_id, mid, "Не нашёл подходящих связок под твой баланс/фильтры.", reply_markup=main_menu())
        return

    lines = ["📋 ТОП-5 связок (под твой баланс)\n"]
    for i, (spread, b, s) in enumerate(pairs, start=1):
        bp = get_price(b) or 0.0
        sp = get_price(s) or 0.0
        b_name = (b.get("advertiser") or {}).get("nickName", "seller")
        s_name = (s.get("advertiser") or {}).get("nickName", "buyer")
        b_mn, b_mx = get_limits(b)
        s_mn, s_mx = get_limits(s)
        lines.append(
            f"{i}) spread={spread:.2f}% | BUY {bp:.4f} ({b_name}) -> SELL {sp:.4f} ({s_name})\n"
            f"   BUY lim {b_mn}..{b_mx} {FIAT} | SELL lim {s_mn}..{s_mx} {FIAT}"
        )

    tg_edit(chat_id, mid, "\n".join(lines), reply_markup=main_menu())


def handle_message(chat_id: int, text: str):
    if text.startswith("/start"):
        STATE["chat_id"] = chat_id
        tg_send(
            chat_id,
            "Привет! 👋\n"
            "1) Введи баланс сообщением:  balance 250\n"
            "2) Выбери оплату кнопкой 💳\n"
            "3) Дальше всё кнопками 👇",
            reply_markup=main_menu()
        )
        return

    if text.lower().startswith("balance"):
        parts = text.split()
        if len(parts) >= 2:
            v = safe_float(parts[1], None)
            if v is None or v <= 0:
                tg_send(chat_id, "Баланс должен быть числом > 0. Пример: balance 200", reply_markup=main_menu())
                return
            STATE["balance"] = float(v)
            STATE["balance_set"] = True
            STATE["threshold"] = recommend_threshold(float(v))
            tg_send(
                chat_id,
                f"✅ Баланс: {v:.2f} {FIAT}\nПорог (бот выбрал): {STATE['threshold']:.1f}%",
                reply_markup=main_menu()
            )
            return
        tg_send(chat_id, "Пример: balance 200", reply_markup=main_menu())
        return

    tg_send(chat_id, "Используй кнопки меню или напиши: balance 200", reply_markup=main_menu())


def handle_callback(chat_id: int, data: str):
    # Главное меню
    if data == "MENU:PAY":
        tg_send(chat_id, "Выбери метод оплаты:", reply_markup=pay_buttons())
        return

    if data == "MENU:SPEED":
        tg_send(chat_id, "Выбери скорость сканирования:", reply_markup=speed_buttons())
        return

    if data == "MENU:STATUS":
        tg_send(chat_id, status_text(), reply_markup=main_menu())
        return

    if data == "MENU:HELP":
        tg_send(chat_id, help_text(), reply_markup=main_menu())
        return

    if data == "MENU:TOP5":
        send_top5(chat_id)
        return

    if data == "MENU:SETUPRUN":
        # проверка и запуск
        if preflight(chat_id):
            start_scanner(chat_id)
        return

    if data == "MENU:RUN":
        start_scanner(chat_id)
        return

    if data == "MENU:STOP":
        stop_scanner(chat_id)
        return

    if data == "MENU:AI":
        STATE["ai_enabled"] = not STATE["ai_enabled"]
        tg_send(chat_id, f"🤖 AI теперь: {'ON' if STATE['ai_enabled'] else 'OFF'}", reply_markup=main_menu())
        return

    # Назад
    if data == "BACK:MENU":
        tg_send(chat_id, "Меню:", reply_markup=main_menu())
        return

    # Оплата
    if data.startswith("PAY:"):
        val = data.split(":", 1)[1]
        STATE["pay_types"] = [] if val == "ANY" else [val]
        STATE["pay_set"] = True
        tg_send(chat_id, f"✅ Оплата: {'ANY' if not STATE['pay_types'] else STATE['pay_types'][0]}", reply_markup=main_menu())
        return

    # Скорость
    if data.startswith("SPD:"):
        sec = safe_int(data.split(":", 1)[1], 15)
        sec = max(5, min(60, sec))
        STATE["poll_seconds"] = sec
        tg_send(chat_id, f"✅ Скорость: {sec}s", reply_markup=main_menu())
        return
    if data == "MENU:THR":
        tg_send(chat_id, "Выбери порог спреда:", reply_markup=threshold_buttons())
        return

    if data.startswith("THR:"):
        val = safe_float(data.split(":", 1)[1], None)
        if val is None:
        tg_send(chat_id, "Ошибка порога.", reply_markup=main_menu())
        return
    STATE["threshold"] = float(val)
    tg_send(chat_id, f"✅ Порог установлен: {STATE['threshold']:.1f}%", reply_markup=main_menu())
    return


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

                if "message" in upd and "text" in upd["message"]:
                    chat_id = upd["message"]["chat"]["id"]
                    if not is_allowed(chat_id):
                        continue
                    handle_message(chat_id, upd["message"]["text"])

                if "callback_query" in upd:
                    cq = upd["callback_query"]
                    chat_id = cq["message"]["chat"]["id"]
                    if not is_allowed(chat_id):
                        continue
                    try:
                        tg_call("answerCallbackQuery", {"callback_query_id": cq["id"]})
                    except Exception:
                        pass
                    handle_callback(chat_id, cq.get("data", ""))

        except Exception as e:
            print("Error:", repr(e))
            time.sleep(5)


if __name__ == "__main__":
    main()
