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
AI_DEFAULT_ENABLED = os.getenv("AI_ENABLED", "0").strip().lower() in ("1", "true", "yes")

# =========================
# Binance P2P
# =========================
P2P_URL = "https://p2p.binance.com/bapi/c2c/v2/friendly/c2c/adv/search"
HEADERS = {
    "accept": "*/*",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0 (p2p-scanner)"
}

ASSET = "USDT"
FIAT = "EUR"

# Фильтры (мягкие, чтобы было больше вариантов)
MIN_USER_TRADES = 50
MIN_COMPLETION_RATE = 90.0

# Глубина рынка
DEFAULT_ROWS = 100     # сколько объявлений на страницу
DEFAULT_PAGES = 5      # сколько страниц (rows*pages = до 500 объявлений)

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
        # Telegram иногда не даёт слишком часто редактировать / тот же текст
        pass


def tg_action(chat_id: int, action: str = "typing") -> None:
    try:
        tg_call("sendChatAction", {"chat_id": chat_id, "action": action})
    except Exception:
        pass


# =========================
# OpenAI helper (optional)
# =========================
OPENAI_URL = "https://api.openai.com/v1/responses"


def openai_score(signal: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    Возвращает {"ok": bool, "score": float(0..1), "reason": str}
    Если ключа нет или ошибка — None (AI-фильтр пропускаем).
    """
    if not OPENAI_API_KEY:
        return None

    prompt = (
        "Ты помощник для P2P-арбитража. Оцени вероятность, что круг BUY->SELL реально исполнить без срыва.\n"
        "Верни ТОЛЬКО JSON: {\"ok\": true/false, \"score\": 0..1, \"reason\": \"...\"}\n"
        "Учитывай: spread, лимиты, trades, completion, метод оплаты, сумма. "
        "Если риск высокий — ok=false.\n"
        f"Данные: {signal}"
    )

    headers = {"Authorization": f"Bearer {OPENAI_API_KEY}", "Content-Type": "application/json"}
    body = {"model": OPENAI_MODEL, "input": prompt, "reasoning": {"effort": "none"}}

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
        score = max(0.0, min(1.0, score))
        reason = str(obj.get("reason", ""))[:300]
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
        "payTypes": pay_types,   # [] => любые
        "asset": ASSET,
        "fiat": FIAT,
        "tradeType": trade_type  # "BUY" / "SELL"
    }
    r = requests.post(P2P_URL, headers=HEADERS, json=payload, timeout=15)
    r.raise_for_status()
    return r.json()


def fetch_multi_pages(trade_type: str, pay_types: List[str], rows: int, pages: int) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in range(1, pages + 1):
        data = fetch_ads(trade_type, pay_types, rows=rows, page=p)
        out.extend(data.get("data") or [])
        time.sleep(0.2)  # чтобы не долбить Binance
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


def top_pairs_in_range(
    buys: List[Dict[str, Any]],
    sells: List[Dict[str, Any]],
    amount_fiat: float,
    spread_min: float,
    spread_max: float,
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
            spread = pct(bp, sp)
            if spread_min <= spread <= spread_max:
                pairs.append((spread, b, s))

    pairs.sort(key=lambda x: x[0], reverse=True)
    return pairs[:n]


# =========================
# State
# =========================
DEFAULT_STATE: Dict[str, Any] = {
    "chat_id": None,

    # настройки
    "balance": 0.0,
    "balance_set": False,

    "pay_types": [],  # [] = ANY
    "pay_set": False,

    # стратегия: диапазон сделок и порог сигнала
    "spread_min": 0.2,
    "spread_max": 1.3,
    "range_set": False,

    "alert_spread": 1.0,
    "alert_set": False,

    "poll_seconds": 15,
    "speed_set": False,

    "rows": DEFAULT_ROWS,
    "pages": DEFAULT_PAGES,

    "ai_enabled": AI_DEFAULT_ENABLED,

    # работа
    "running": False,
    "scan_msg_id": 0,

    # мастер-настройка: 0=нет, 1..4
    "wizard_step": 0,
}

STATE: Dict[str, Any] = dict(DEFAULT_STATE)

_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_last_signal_key = None

_spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def reset_all(chat_id: int):
    global _last_signal_key
    stop_scanner(chat_id, silent=True)

    for k in list(STATE.keys()):
        STATE.pop(k, None)

    STATE.update(dict(DEFAULT_STATE))
    STATE["chat_id"] = chat_id
    _last_signal_key = None

    tg_send(chat_id, "🔄 Сбросил настройки. Введи баланс: balance 250", reply_markup=main_menu())


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


# =========================
# UI (кнопки как у тебя)
# =========================
def pay_label() -> str:
    return "ANY" if not STATE["pay_types"] else STATE["pay_types"][0]


def main_menu() -> Dict[str, Any]:
    pay = pay_label()
    ai = "ON" if STATE["ai_enabled"] else "OFF"
    spd = f"{STATE['poll_seconds']}s"
    rng = f"{STATE['spread_min']:.1f}-{STATE['spread_max']:.1f}%"
    thr = f"{STATE['alert_spread']:.1f}%"
    return {
        "inline_keyboard": [
            [
                {"text": f"💳 Оплата: {pay}", "callback_data": "MENU:PAY"},
                {"text": f"🤖 AI: {ai}", "callback_data": "MENU:AI"},
            ],
            [
                {"text": f"⏱ Скорость: {spd}", "callback_data": "MENU:SPEED"},
                {"text": f"📈 Диапазон: {rng}", "callback_data": "MENU:RANGE"},
                {"text": f"⚡ Порог: {thr}", "callback_data": "MENU:ALERT"},
            ],
            [
                {"text": "🧾 ТОП-5", "callback_data": "MENU:TOP5"},
                {"text": "📖 Инструкция", "callback_data": "MENU:HELP"},
            ],
            [
                {"text": "⚙️ Настроить и старт", "callback_data": "WIZ:START"},
                {"text": "🔄 Сброс", "callback_data": "MENU:RESET"},
            ],
            [
                {"text": "▶️ Старт", "callback_data": "MENU:RUN"},
                {"text": "⏸ Стоп", "callback_data": "MENU:STOP"},
            ],
        ]
    }


def pay_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "SEPA", "callback_data": "PAY:SEPA"},
                {"text": "Revolut", "callback_data": "PAY:Revolut"},
                {"text": "Wise", "callback_data": "PAY:Wise"},
                {"text": "ANY", "callback_data": "PAY:ANY"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "BACK:MENU"}],
        ]
    }


def speed_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "5s", "callback_data": "SPD:5"},
                {"text": "10s", "callback_data": "SPD:10"},
                {"text": "15s", "callback_data": "SPD:15"},
                {"text": "30s", "callback_data": "SPD:30"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "BACK:MENU"}],
        ]
    }


def range_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "0.2–1.3%", "callback_data": "RNG:0.2:1.3"},
                {"text": "0.3–1.5%", "callback_data": "RNG:0.3:1.5"},
            ],
            [
                {"text": "0.5–2.0%", "callback_data": "RNG:0.5:2.0"},
                {"text": "0.2–0.8%", "callback_data": "RNG:0.2:0.8"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "BACK:MENU"}],
        ]
    }


def alert_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "0.8%", "callback_data": "ALERT:0.8"},
                {"text": "1.0%", "callback_data": "ALERT:1.0"},
                {"text": "1.2%", "callback_data": "ALERT:1.2"},
                {"text": "1.5%", "callback_data": "ALERT:1.5"},
            ],
            [{"text": "⬅️ Назад", "callback_data": "BACK:MENU"}],
        ]
    }


def help_text() -> str:
    return (
        "📖 Инструкция\n\n"
        "1) /start\n"
        "2) Введи баланс сообщением: balance 250\n\n"
        "Дальше можно:\n"
        "• Настроить вручную кнопками\n"
        "• Или пройти пошагово через «⚙️ Настроить и старт»\n\n"
        "Кнопки:\n"
        "• 💳 Оплата — метод оплаты\n"
        "• 📈 Диапазон — какие спреды вообще искать (например 0.2–1.3%)\n"
        "• ⚡ Порог — от какого спреда присылать 🔥 SIGNAL\n"
        "• ⏱ Скорость — как часто сканировать\n"
        "• 🧾 ТОП-5 — 5 лучших связок прямо сейчас (в диапазоне)\n"
        "• 🔄 Сброс — сброс настроек\n"
    )


def status_text() -> str:
    return (
        "📌 Статус\n"
        f"running: {STATE['running']}\n"
        f"balance: {STATE['balance']} {FIAT} (set={STATE['balance_set']})\n"
        f"pay: {pay_label()} (set={STATE['pay_set']})\n"
        f"range: {STATE['spread_min']:.1f}–{STATE['spread_max']:.1f}% (set={STATE['range_set']})\n"
        f"alert: >= {STATE['alert_spread']:.1f}% (set={STATE['alert_set']})\n"
        f"speed: {STATE['poll_seconds']}s (set={STATE['speed_set']})\n"
        f"rows/pages: {STATE['rows']}/{STATE['pages']}\n"
        f"AI: {'ON' if STATE['ai_enabled'] else 'OFF'}\n"
        f"filters: trades>={MIN_USER_TRADES}, completion>={MIN_COMPLETION_RATE}%"
    )


# =========================
# Preflight + Wizard
# =========================
def preflight(chat_id: int) -> bool:
    if not STATE["balance_set"]:
        tg_send(chat_id, "Сначала введи баланс: balance 250", reply_markup=main_menu())
        return False
    if not STATE["pay_set"]:
        tg_send(chat_id, "Сначала выбери оплату 💳", reply_markup=pay_buttons())
        return False
    if not STATE["range_set"]:
        tg_send(chat_id, "Сначала выбери диапазон 📈", reply_markup=range_buttons())
        return False
    if not STATE["alert_set"]:
        tg_send(chat_id, "Сначала выбери порог ⚡", reply_markup=alert_buttons())
        return False
    if not STATE["speed_set"]:
        tg_send(chat_id, "Сначала выбери скорость ⏱", reply_markup=speed_buttons())
        return False
    return True


def wizard_start(chat_id: int):
    STATE["wizard_step"] = 1

    if not STATE["balance_set"]:
        tg_send(
            chat_id,
            "Шаг 0/4: введи баланс сообщением: balance 250\n"
            "После этого снова нажми «⚙️ Настроить и старт».",
            reply_markup=main_menu()
        )
        return

    tg_send(chat_id, "Шаг 1/4: выбери оплату 💳", reply_markup=pay_buttons())


def wizard_next(chat_id: int):
    step = int(STATE.get("wizard_step") or 0)
    if step == 1:
        STATE["wizard_step"] = 2
        tg_send(chat_id, "Шаг 2/4: выбери диапазон 📈", reply_markup=range_buttons())
        return
    if step == 2:
        STATE["wizard_step"] = 3
        tg_send(chat_id, "Шаг 3/4: выбери порог ⚡", reply_markup=alert_buttons())
        return
    if step == 3:
        STATE["wizard_step"] = 4
        tg_send(chat_id, "Шаг 4/4: выбери скорость ⏱", reply_markup=speed_buttons())
        return
    if step == 4:
        STATE["wizard_step"] = 0
        tg_send(chat_id, "✅ Готово! Теперь жми ▶️ Старт или 🧾 ТОП-5.", reply_markup=main_menu())


# =========================
# Scanner
# =========================
def scanner_loop():
    global _last_signal_key
    spin_i = 0
    last_edit = 0.0

    while not _stop_event.is_set():
        try:
            chat_id = STATE["chat_id"]
            balance = float(STATE["balance"])
            pay_types = list(STATE["pay_types"])

            poll = int(STATE["poll_seconds"])
            rows = int(STATE["rows"])
            pages = int(STATE["pages"])

            spread_min = float(STATE["spread_min"])
            spread_max = float(STATE["spread_max"])
            alert = float(STATE["alert_spread"])

            tg_action(chat_id, "typing")

            buys = fetch_multi_pages("BUY", pay_types, rows=rows, pages=pages)
            sells = fetch_multi_pages("SELL", pay_types, rows=rows, pages=pages)

            top5 = top_pairs_in_range(buys, sells, balance, spread_min, spread_max, n=5)
            best_now = top5[0][0] if top5 else None

            now = time.time()
            if STATE["scan_msg_id"] and (now - last_edit) > 3:
                icon = _spinner[spin_i % len(_spinner)]
                spin_i += 1
                best_line = f"Лучший сейчас: {best_now:.2f}%" if best_now is not None else "Лучший сейчас: —"
                tg_edit(
                    chat_id,
                    STATE["scan_msg_id"],
                    f"🔎 Сканирую… {icon}\n"
                    f"{best_line}\n"
                    f"Оплата: {pay_label()} | Баланс: {balance:.2f} {FIAT}\n"
                    f"Диапазон: {spread_min:.1f}–{spread_max:.1f}% | Порог: {alert:.1f}% | Скорость: {poll}s\n"
                    f"AI: {'ON' if STATE['ai_enabled'] else 'OFF'}",
                    reply_markup=main_menu()
                )
                last_edit = now

            # SIGNAL: если лучший >= порога
            if top5:
                spread, b, s = top5[0]
                bp = get_price(b)
                sp = get_price(s)
                if bp is not None and sp is not None:
                    key = (round(bp, 6), round(sp, 6), round(spread, 4))
                    if spread >= alert and key != _last_signal_key:
                        b_adv = b.get("adv") or {}
                        s_adv = s.get("adv") or {}
                        b_name = (b.get("advertiser") or {}).get("nickName", "seller")
                        s_name = (s.get("advertiser") or {}).get("nickName", "buyer")

                        b_mn, b_mx = get_limits(b)
                        s_mn, s_mx = get_limits(s)
                        b_tr, b_comp = get_adv_stats(b)
                        s_tr, s_comp = get_adv_stats(s)

                        signal = {
                            "fiat": FIAT,
                            "asset": ASSET,
                            "amount": balance,
                            "payType": pay_label(),
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
                            if ai is not None and not ai["ok"]:
                                _last_signal_key = key
                                tg_send(chat_id, f"⚠️ AI отсеял сигнал (score={ai['score']:.2f}): {ai['reason']}",
                                        reply_markup=main_menu())
                                time.sleep(poll)
                                continue
                            if ai is not None:
                                ai_note = f"\n🤖 AI: OK (score={ai['score']:.2f}) — {ai['reason']}"

                        msg = (
                            "🔥 SIGNAL\n"
                            f"BUY {bp:.4f} ({b_name}) -> SELL {sp:.4f} ({s_name})\n"
                            f"spread={spread:.2f}% | amount={balance:.2f} {FIAT}\n"
                            f"Оплата={pay_label()} | Диапазон={spread_min:.1f}–{spread_max:.1f}% | Порог>={alert:.1f}%\n"
                            f"BUY limits : {b_adv.get('minSingleTransAmount')}..{b_adv.get('maxSingleTransAmount')} {FIAT}\n"
                            f"SELL limits: {s_adv.get('minSingleTransAmount')}..{s_adv.get('maxSingleTransAmount')} {FIAT}"
                            + ai_note
                        )
                        tg_send(chat_id, msg, reply_markup=main_menu())
                        _last_signal_key = key

            time.sleep(poll)

        except Exception as e:
            try:
                tg_send(STATE["chat_id"], f"Ошибка: {repr(e)}", reply_markup=main_menu())
            except Exception:
                pass
            time.sleep(5)


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
    STATE["scan_msg_id"] = tg_send(chat_id, "🔎 Сканирую…", reply_markup=main_menu())

    _worker_thread = threading.Thread(target=scanner_loop, daemon=True)
    _worker_thread.start()


def stop_scanner(chat_id: int, silent: bool = False):
    if not STATE["running"]:
        if not silent:
            tg_send(chat_id, "Уже остановлено.", reply_markup=main_menu())
        return

    STATE["running"] = False
    _stop_event.set()

    if not silent:
        tg_send(chat_id, "⏸ Остановлено.", reply_markup=main_menu())


def send_top5(chat_id: int):
    if not preflight(chat_id):
        return

    tg_action(chat_id, "typing")
    mid = tg_send(chat_id, "🔎 Ищу ТОП-5…", reply_markup=main_menu())

    balance = float(STATE["balance"])
    pay_types = list(STATE["pay_types"])
    rows = int(STATE["rows"])
    pages = int(STATE["pages"])
    spread_min = float(STATE["spread_min"])
    spread_max = float(STATE["spread_max"])

    buys = fetch_multi_pages("BUY", pay_types, rows=rows, pages=pages)
    sells = fetch_multi_pages("SELL", pay_types, rows=rows, pages=pages)

    pairs = top_pairs_in_range(buys, sells, balance, spread_min, spread_max, n=5)
    if not pairs:
        tg_edit(chat_id, mid, "Не нашёл связок в выбранном диапазоне под твой баланс/фильтры.", reply_markup=main_menu())
        return

    lines = [f"🧾 ТОП-5 (диапазон {spread_min:.1f}–{spread_max:.1f}%)\n"]
    for i, (spread, b, s) in enumerate(pairs, start=1):
        bp = get_price(b) or 0.0
        sp = get_price(s) or 0.0
        b_name = (b.get("advertiser") or {}).get("nickName", "seller")
        s_name = (s.get("advertiser") or {}).get("nickName", "buyer")
        b_mn, b_mx = get_limits(b)
        s_mn, s_mx = get_limits(s)

        lines.append(
            f"{i}) {spread:.2f}% | BUY {bp:.4f} ({b_name}) -> SELL {sp:.4f} ({s_name})\n"
            f"   BUY {b_mn}..{b_mx} {FIAT} | SELL {s_mn}..{s_mx} {FIAT}"
        )

    tg_edit(chat_id, mid, "\n".join(lines), reply_markup=main_menu())


# =========================
# Handlers
# =========================
def handle_message(chat_id: int, text: str):
    if text.startswith("/start"):
        STATE["chat_id"] = chat_id
        tg_send(
            chat_id,
            "Привет! 👋\n"
            "1) Введи баланс: balance 250\n"
            "2) Потом можно пройти пошагово «⚙️ Настроить и старт»",
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
            tg_send(chat_id, f"✅ Баланс: {v:.2f} {FIAT}", reply_markup=main_menu())
            return
        tg_send(chat_id, "Пример: balance 200", reply_markup=main_menu())
        return

    tg_send(chat_id, "Используй кнопки меню или напиши: balance 200", reply_markup=main_menu())


def handle_callback(chat_id: int, data: str):
    # меню
    if data == "BACK:MENU":
        tg_send(chat_id, "Меню:", reply_markup=main_menu())
        return

    if data == "MENU:PAY":
        tg_send(chat_id, "Выбери оплату 💳", reply_markup=pay_buttons())
        return

    if data == "MENU:SPEED":
        tg_send(chat_id, "Выбери скорость ⏱", reply_markup=speed_buttons())
        return

    if data == "MENU:RANGE":
        tg_send(chat_id, "Выбери диапазон 📈", reply_markup=range_buttons())
        return

    if data == "MENU:ALERT":
        tg_send(chat_id, "Выбери порог ⚡", reply_markup=alert_buttons())
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

    if data == "MENU:RESET":
        reset_all(chat_id)
        return

    # мастер
    if data == "WIZ:START":
        wizard_start(chat_id)
        return

    # выбор оплаты
    if data.startswith("PAY:"):
        val = data.split(":", 1)[1]
        STATE["pay_types"] = [] if val == "ANY" else [val]
        STATE["pay_set"] = True
        tg_send(chat_id, f"✅ Оплата: {pay_label()}", reply_markup=main_menu())

        if STATE.get("wizard_step") == 1:
            wizard_next(chat_id)
        return

    # выбор диапазона
    if data.startswith("RNG:"):
        parts = data.split(":")
        if len(parts) == 3:
            mn = safe_float(parts[1], None)
            mx = safe_float(parts[2], None)
            if mn is not None and mx is not None and mn < mx:
                STATE["spread_min"] = float(mn)
                STATE["spread_max"] = float(mx)
                STATE["range_set"] = True
                tg_send(chat_id, f"✅ Диапазон: {STATE['spread_min']:.1f}–{STATE['spread_max']:.1f}%",
                        reply_markup=main_menu())
                if STATE.get("wizard_step") == 2:
                    wizard_next(chat_id)
                return
        tg_send(chat_id, "Ошибка диапазона.", reply_markup=main_menu())
        return

    # выбор порога
    if data.startswith("ALERT:"):
        val = safe_float(data.split(":", 1)[1], None)
        if val is None:
            tg_send(chat_id, "Ошибка порога.", reply_markup=main_menu())
            return
        STATE["alert_spread"] = float(val)
        STATE["alert_set"] = True
        tg_send(chat_id, f"✅ Порог: {STATE['alert_spread']:.1f}%", reply_markup=main_menu())
        if STATE.get("wizard_step") == 3:
            wizard_next(chat_id)
        return

    # выбор скорости
    if data.startswith("SPD:"):
        sec = safe_int(data.split(":", 1)[1], 15)
        sec = max(5, min(60, sec))
        STATE["poll_seconds"] = sec
        STATE["speed_set"] = True
        tg_send(chat_id, f"✅ Скорость: {sec}s", reply_markup=main_menu())
        if STATE.get("wizard_step") == 4:
            wizard_next(chat_id)
        return


# =========================
# Main (long polling)
# =========================
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
