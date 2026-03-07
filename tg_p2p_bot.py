import os
import time
import threading
import requests
from typing import Dict, Any, List, Optional, Tuple

# =========================
# ENV VARS
# =========================
TG_TOKEN = os.getenv("TG_TOKEN", "").strip()
ALLOWED_CHAT_ID = os.getenv("ALLOWED_CHAT_ID", "").strip()

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

SUPPORTED_FIATS = ["EUR", "UAH", "GBP", "PLN", "TRY", "KZT"]

MIN_USER_TRADES = 50
MIN_COMPLETION_RATE = 90.0

DEFAULT_ROWS = 100
DEFAULT_PAGES = 5
CANDIDATES_K = 30

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
    if not OPENAI_API_KEY:
        return None

    prompt = (
        "Ты помощник для P2P-арбитража. Оцени вероятность, что круг BUY->SELL реально исполнить без срыва.\n"
        "Верни ТОЛЬКО JSON: {\"ok\": true/false, \"score\": 0..1, \"reason\": \"...\"}\n"
        "Учитывай: spread, лимиты, trades, completion, метод оплаты, сумма, fiat.\n"
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


def current_fiat() -> str:
    return STATE["fiat"]


def fetch_ads(trade_type: str, pay_types: List[str], rows: int, page: int = 1) -> Dict[str, Any]:
    payload = {
        "page": page,
        "rows": rows,
        "payTypes": pay_types,
        "asset": ASSET,
        "fiat": current_fiat(),
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
        time.sleep(0.15)
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


def profit_fiat(amount_fiat: float, spread_percent: float) -> float:
    return amount_fiat * (spread_percent / 100.0)


def select_candidates(
    buys: List[Dict[str, Any]],
    sells: List[Dict[str, Any]],
    amount_fiat: float,
    k: int = CANDIDATES_K
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    buy_ok = [b for b in buys if passes_filters(b, amount_fiat) and get_price(b) is not None]
    sell_ok = [s for s in sells if passes_filters(s, amount_fiat) and get_price(s) is not None]

    buy_ok.sort(key=lambda x: get_price(x) or 10**9)
    sell_ok.sort(key=lambda x: get_price(x) or -10**9, reverse=True)

    return buy_ok[:k], sell_ok[:k]


def top_pairs_in_range_fast(
    buys: List[Dict[str, Any]],
    sells: List[Dict[str, Any]],
    amount_fiat: float,
    spread_min: float,
    spread_max: float,
    n: int = 5
) -> List[Tuple[float, Dict[str, Any], Dict[str, Any]]]:
    buy_c, sell_c = select_candidates(buys, sells, amount_fiat, k=CANDIDATES_K)

    pairs: List[Tuple[float, Dict[str, Any], Dict[str, Any]]] = []
    for b in buy_c:
        bp = get_price(b)
        if bp is None:
            continue
        for s in sell_c:
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
    "menu_msg_id": 0,

    "fiat": "EUR",
    "fiat_set": False,

    "balance": 0.0,
    "balance_set": False,

    "pay_types": [],
    "pay_set": False,

    "spread_min": 0.0,
    "spread_max": 1.2,
    "range_set": False,

    "alert_spread": 0.3,
    "alert_set": False,

    "poll_seconds": 5,
    "speed_set": False,

    "rows": DEFAULT_ROWS,
    "pages": DEFAULT_PAGES,

    "ai_enabled": AI_DEFAULT_ENABLED,
    "running": False,
    "wizard_step": 0,
}

STATE: Dict[str, Any] = dict(DEFAULT_STATE)

_stop_event = threading.Event()
_worker_thread: Optional[threading.Thread] = None
_last_signal_key = None
_spinner = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def is_allowed(chat_id: int) -> bool:
    if not ALLOWED_CHAT_ID:
        return True
    return str(chat_id) == ALLOWED_CHAT_ID


def pay_label() -> str:
    return "ANY" if not STATE["pay_types"] else STATE["pay_types"][0]


def show_screen(chat_id: int, text: str, markup: Dict[str, Any]) -> None:
    if STATE.get("menu_msg_id"):
        tg_edit(chat_id, STATE["menu_msg_id"], text, reply_markup=markup)
    else:
        STATE["menu_msg_id"] = tg_send(chat_id, text, reply_markup=markup)


def reset_all(chat_id: int):
    global _last_signal_key
    stop_scanner(chat_id, silent=True)

    STATE.clear()
    STATE.update(dict(DEFAULT_STATE))
    STATE["chat_id"] = chat_id
    _last_signal_key = None

    show_screen(
        chat_id,
        "🔄 Сбросил настройки.\n\n"
        "1) Выбери валюту\n"
        "2) Введи баланс, например:\n"
        "balance 200",
        kb_start()
    )


# =========================
# UI
# =========================
def kb_start() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [{"text": "💱 Выбрать валюту", "callback_data": "WIZ:FIAT"}],
            [{"text": "⚙️ Настроить", "callback_data": "WIZ:START"}],
            [{"text": "📖 Инструкция", "callback_data": "MENU:HELP"}],
            [{"text": "🔄 Сброс", "callback_data": "MENU:RESET"}],
        ]
    }


def kb_main() -> Dict[str, Any]:
    pay = pay_label()
    ai = "ON" if STATE["ai_enabled"] else "OFF"
    spd = f"{STATE['poll_seconds']}s"
    rng = f"{STATE['spread_min']:.1f}-{STATE['spread_max']:.1f}%"
    thr = f"{STATE['alert_spread']:.1f}%"
    fiat = STATE["fiat"]

    return {
        "inline_keyboard": [
            [
                {"text": f"💱 Валюта: {fiat}", "callback_data": "MENU:FIAT"},
                {"text": f"💳 Оплата: {pay}", "callback_data": "MENU:PAY"},
            ],
            [
                {"text": f"🤖 AI: {ai}", "callback_data": "MENU:AI"},
                {"text": f"⏱ Скорость: {spd}", "callback_data": "MENU:SPEED"},
            ],
            [
                {"text": f"📈 Диапазон: {rng}", "callback_data": "MENU:RANGE"},
                {"text": f"⚡ Порог: {thr}", "callback_data": "MENU:ALERT"},
            ],
            [
                {"text": "🧾 ТОП-5", "callback_data": "MENU:TOP5"},
                {"text": "📌 Статус", "callback_data": "MENU:STATUS"},
                {"text": "📖 Инструкция", "callback_data": "MENU:HELP"},
            ],
            [
                {"text": "🔄 Сброс", "callback_data": "MENU:RESET"},
            ],
            [
                {"text": "▶️ Старт", "callback_data": "MENU:RUN"},
                {"text": "⏸ Стоп", "callback_data": "MENU:STOP"},
            ],
        ]
    }


def fiat_buttons() -> Dict[str, Any]:
    rows = [
        [
            {"text": "EUR", "callback_data": "FIAT:EUR"},
            {"text": "UAH", "callback_data": "FIAT:UAH"},
            {"text": "GBP", "callback_data": "FIAT:GBP"},
        ],
        [
            {"text": "PLN", "callback_data": "FIAT:PLN"},
            {"text": "TRY", "callback_data": "FIAT:TRY"},
            {"text": "KZT", "callback_data": "FIAT:KZT"},
        ],
        [
            {"text": "🔄 Сброс", "callback_data": "MENU:RESET"},
        ]
    ]
    return {"inline_keyboard": rows}


def pay_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "SEPA", "callback_data": "PAY:SEPA"},
                {"text": "Revolut", "callback_data": "PAY:Revolut"},
                {"text": "Wise", "callback_data": "PAY:Wise"},
                {"text": "ANY", "callback_data": "PAY:ANY"},
            ],
            [{"text": "🔄 Сброс", "callback_data": "MENU:RESET"}],
        ]
    }


def range_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "0.0–1.2%", "callback_data": "RNG:0.0:1.2"},
                {"text": "0.2–1.3%", "callback_data": "RNG:0.2:1.3"},
            ],
            [
                {"text": "0.2–0.8%", "callback_data": "RNG:0.2:0.8"},
                {"text": "0.3–1.5%", "callback_data": "RNG:0.3:1.5"},
            ],
            [{"text": "🔄 Сброс", "callback_data": "MENU:RESET"}],
        ]
    }


def alert_buttons() -> Dict[str, Any]:
    return {
        "inline_keyboard": [
            [
                {"text": "0.2%", "callback_data": "ALERT:0.2"},
                {"text": "0.3%", "callback_data": "ALERT:0.3"},
                {"text": "0.4%", "callback_data": "ALERT:0.4"},
            ],
            [
                {"text": "0.5%", "callback_data": "ALERT:0.5"},
                {"text": "0.8%", "callback_data": "ALERT:0.8"},
                {"text": "1.0%", "callback_data": "ALERT:1.0"},
            ],
            [{"text": "🔄 Сброс", "callback_data": "MENU:RESET"}],
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
            [{"text": "🔄 Сброс", "callback_data": "MENU:RESET"}],
        ]
    }


def help_text() -> str:
    return (
        "📖 Инструкция\n\n"
        "1) Выбери фиатную валюту кнопкой 💱\n"
        "2) Напиши баланс: balance 200\n"
        "3) Нажми «⚙️ Настроить» и пройди шаги\n"
        "4) Нажми ▶️ Старт\n\n"
        "Бот ищет:\n"
        f"• asset: {ASSET}\n"
        "• fiat: выбранная тобой валюта\n"
        "• BUY → SELL внутри Binance P2P\n\n"
        "Можно переключать валюты и искать там, где тебе удобно."
    )


def status_text() -> str:
    return (
        "📌 Статус\n"
        f"running: {STATE['running']}\n"
        f"fiat: {STATE['fiat']} (set={STATE['fiat_set']})\n"
        f"balance: {STATE['balance']} {STATE['fiat']} (set={STATE['balance_set']})\n"
        f"pay: {pay_label()} (set={STATE['pay_set']})\n"
        f"range: {STATE['spread_min']:.1f}–{STATE['spread_max']:.1f}% (set={STATE['range_set']})\n"
        f"alert: >= {STATE['alert_spread']:.1f}% (set={STATE['alert_set']})\n"
        f"speed: {STATE['poll_seconds']}s (set={STATE['speed_set']})\n"
        f"rows/pages: {STATE['rows']}/{STATE['pages']}\n"
        f"AI: {'ON' if STATE['ai_enabled'] else 'OFF'}\n"
        f"filters: trades>={MIN_USER_TRADES}, completion>={MIN_COMPLETION_RATE}%"
    )


# =========================
# Wizard
# =========================
def wizard_start(chat_id: int):
    STATE["chat_id"] = chat_id

    if not STATE["fiat_set"]:
        show_screen(chat_id, "Шаг 0/5 — выбери валюту 💱", fiat_buttons())
        return

    if not STATE["balance_set"]:
        show_screen(
            chat_id,
            f"Шаг 1/5\n\nВведи баланс сообщением, например:\nbalance 200\n\nВ валюте: {STATE['fiat']}",
            kb_start()
        )
        return

    STATE["wizard_step"] = 1
    show_screen(chat_id, "Шаг 2/5 — выбери оплату 💳", pay_buttons())


def wizard_next(chat_id: int):
    step = int(STATE.get("wizard_step") or 0)

    if step == 1:
        STATE["wizard_step"] = 2
        show_screen(chat_id, "Шаг 3/5 — выбери диапазон 📈", range_buttons())
        return

    if step == 2:
        STATE["wizard_step"] = 3
        show_screen(chat_id, "Шаг 4/5 — выбери порог ⚡", alert_buttons())
        return

    if step == 3:
        STATE["wizard_step"] = 4
        show_screen(chat_id, "Шаг 5/5 — выбери скорость ⏱", speed_buttons())
        return

    if step == 4:
        STATE["wizard_step"] = 5
        show_screen(chat_id, "✅ Настройка завершена.\n\nЖми ▶️ Старт или 🧾 ТОП-5.", kb_main())
        return

    show_screen(chat_id, "✅ Готово.\n\nЖми ▶️ Старт или 🧾 ТОП-5.", kb_main())


def preflight(chat_id: int) -> bool:
    if not STATE["fiat_set"]:
        show_screen(chat_id, "Сначала выбери валюту 💱", fiat_buttons())
        return False
    if not STATE["balance_set"]:
        show_screen(chat_id, f"Сначала введи баланс в валюте {STATE['fiat']}: balance 200", kb_start())
        return False
    if not (STATE["pay_set"] and STATE["range_set"] and STATE["alert_set"] and STATE["speed_set"]):
        show_screen(chat_id, "Сначала пройди настройку: нажми ⚙️ Настроить", kb_start())
        return False
    return True


# =========================
# Scanner loop
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
            fiat = STATE["fiat"]

            tg_action(chat_id, "typing")

            buys = fetch_multi_pages("BUY", pay_types, rows=rows, pages=pages)
            sells = fetch_multi_pages("SELL", pay_types, rows=rows, pages=pages)

            top5 = top_pairs_in_range_fast(buys, sells, balance, spread_min, spread_max, n=5)
            best_now = top5[0][0] if top5 else None

            now = time.time()
            if STATE.get("menu_msg_id") and (now - last_edit) > 3:
                icon = _spinner[spin_i % len(_spinner)]
                spin_i += 1

                if best_now is None:
                    best_line = "Лучший сейчас: — (в твоём диапазоне)"
                else:
                    best_line = f"Лучший сейчас: {best_now:.2f}% (~{profit_fiat(balance, best_now):.2f} {fiat})"

                show_screen(
                    chat_id,
                    "🔎 Сканирую P2P… {}\n{}\n"
                    "Валюта: {} | Оплата: {} | Баланс: {:.2f} {}\n"
                    "Диапазон: {:.1f}–{:.1f}% | Порог: {:.1f}% | Скорость: {}s\n"
                    "Если лучший ниже порога — сигнал не отправляю.".format(
                        icon, best_line, fiat, pay_label(), balance, fiat,
                        spread_min, spread_max, alert, poll
                    ),
                    kb_main()
                )
                last_edit = now

            if top5:
                spread, b, s = top5[0]
                bp = get_price(b)
                sp = get_price(s)
                if bp is not None and sp is not None:
                    key = (fiat, round(bp, 6), round(sp, 6), round(spread, 4))

                    if spread >= alert and key != _last_signal_key:
                        b_adv = b.get("adv") or {}
                        s_adv = s.get("adv") or {}
                        b_name = (b.get("advertiser") or {}).get("nickName", "seller")
                        s_name = (s.get("advertiser") or {}).get("nickName", "buyer")

                        b_mn, b_mx = get_limits(b)
                        s_mn, s_mx = get_limits(s)
                        b_tr, b_comp = get_adv_stats(b)
                        s_tr, s_comp = get_adv_stats(s)

                        est_profit = profit_fiat(balance, spread)

                        signal = {
                            "fiat": fiat,
                            "asset": ASSET,
                            "amount": balance,
                            "payType": pay_label(),
                            "spread_percent": round(spread, 4),
                            "profit_est": round(est_profit, 4),
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
                                tg_send(chat_id, f"⚠️ AI отсеял сигнал (score={ai['score']:.2f}): {ai['reason']}")
                                time.sleep(poll)
                                continue
                            if ai is not None:
                                ai_note = f"\n🤖 AI: OK (score={ai['score']:.2f}) — {ai['reason']}"

                        msg = (
                            "🔥 SIGNAL\n"
                            f"Валюта: {fiat}\n"
                            f"BUY {bp:.4f} ({b_name}) → SELL {sp:.4f} ({s_name})\n"
                            f"Спред: {spread:.2f}% | Примерная прибыль: ~{est_profit:.2f} {fiat} (на {balance:.2f} {fiat})\n"
                            f"Оплата: {pay_label()} | Диапазон: {spread_min:.1f}–{spread_max:.1f}% | Порог: {alert:.1f}%\n"
                            f"BUY лимиты : {b_adv.get('minSingleTransAmount')}..{b_adv.get('maxSingleTransAmount')} {fiat}\n"
                            f"SELL лимиты: {s_adv.get('minSingleTransAmount')}..{s_adv.get('maxSingleTransAmount')} {fiat}\n"
                            f"BUY trades/comp: {b_tr}/{b_comp:.1f}% | SELL trades/comp: {s_tr}/{s_comp:.1f}%"
                            + ai_note
                        )
                        tg_send(chat_id, msg)
                        _last_signal_key = key

            time.sleep(poll)

        except Exception as e:
            try:
                tg_send(STATE["chat_id"], f"Ошибка: {repr(e)}")
            except Exception:
                pass
            time.sleep(5)


def start_scanner(chat_id: int):
    global _worker_thread

    if not preflight(chat_id):
        return

    if STATE["running"]:
        show_screen(chat_id, "Уже запущено ✅", kb_main())
        return

    STATE["chat_id"] = chat_id
    STATE["running"] = True

    _stop_event.clear()
    show_screen(chat_id, "🔎 Запускаю сканирование…", kb_main())

    _worker_thread = threading.Thread(target=scanner_loop, daemon=True)
    _worker_thread.start()


def stop_scanner(chat_id: int, silent: bool = False):
    if not STATE["running"]:
        if not silent:
            show_screen(chat_id, "Уже остановлено ✅", kb_main() if preflight(chat_id) else kb_start())
        return

    STATE["running"] = False
    _stop_event.set()

    if not silent:
        show_screen(chat_id, "⏸ Остановлено.", kb_main() if preflight(chat_id) else kb_start())


def send_top5(chat_id: int):
    if not preflight(chat_id):
        return

    tg_action(chat_id, "typing")

    balance = float(STATE["balance"])
    pay_types = list(STATE["pay_types"])
    rows = int(STATE["rows"])
    pages = int(STATE["pages"])
    spread_min = float(STATE["spread_min"])
    spread_max = float(STATE["spread_max"])
    fiat = STATE["fiat"]

    buys = fetch_multi_pages("BUY", pay_types, rows=rows, pages=pages)
    sells = fetch_multi_pages("SELL", pay_types, rows=rows, pages=pages)

    pairs = top_pairs_in_range_fast(buys, sells, balance, spread_min, spread_max, n=5)
    if not pairs:
        tg_send(chat_id, f"Не нашёл связок в {fiat} под твой баланс/фильтры.")
        return

    lines = [f"🧾 ТОП-5 по {fiat} (диапазон {spread_min:.1f}–{spread_max:.1f}%)\n"]
    for i, (spread, b, s) in enumerate(pairs, start=1):
        bp = get_price(b) or 0.0
        sp = get_price(s) or 0.0
        est = profit_fiat(balance, spread)
        b_name = (b.get("advertiser") or {}).get("nickName", "seller")
        s_name = (s.get("advertiser") or {}).get("nickName", "buyer")
        b_mn, b_mx = get_limits(b)
        s_mn, s_mx = get_limits(s)

        lines.append(
            f"{i}) {spread:.2f}% (~{est:.2f} {fiat}) | BUY {bp:.4f} ({b_name}) → SELL {sp:.4f} ({s_name})\n"
            f"   BUY {b_mn}..{b_mx} {fiat} | SELL {s_mn}..{s_mx} {fiat}"
        )

    tg_send(chat_id, "\n".join(lines))


# =========================
# Handlers
# =========================
def handle_message(chat_id: int, text: str):
    text = (text or "").strip()

    if text.startswith("/start"):
        STATE["chat_id"] = chat_id

        if preflight(chat_id):
            show_screen(chat_id, "Меню 👇", kb_main())
        else:
            show_screen(
                chat_id,
                "Привет! 👋\n\n"
                "1) Выбери валюту кнопкой 💱\n"
                "2) Введи баланс, например:\n"
                "balance 200\n"
                "3) Потом нажми ⚙️ Настроить",
                kb_start()
            )
        return

    if text.lower().startswith("balance"):
        parts = text.split()
        if len(parts) >= 2:
            v = safe_float(parts[1], None)
            if v is None or v <= 0:
                show_screen(chat_id, "Баланс должен быть числом > 0.\nПример: balance 200", kb_start())
                return

            STATE["chat_id"] = chat_id
            STATE["balance"] = float(v)
            STATE["balance_set"] = True

            show_screen(
                chat_id,
                f"✅ Баланс установлен: {v:.2f} {STATE['fiat']}\n\nТеперь нажми ⚙️ Настроить.",
                kb_start()
            )
            return

        show_screen(chat_id, "Пример: balance 200", kb_start())
        return

    if preflight(chat_id):
        show_screen(chat_id, "Используй кнопки меню 👇", kb_main())
    else:
        show_screen(chat_id, "Сначала выбери валюту, введи баланс и нажми ⚙️ Настроить.", kb_start())


def handle_callback(chat_id: int, data: str):
    if data == "MENU:RESET":
        reset_all(chat_id)
        return

    if data == "MENU:HELP":
        markup = kb_main() if preflight(chat_id) else kb_start()
        show_screen(chat_id, help_text(), markup)
        return

    if data == "MENU:STATUS":
        markup = kb_main() if preflight(chat_id) else kb_start()
        show_screen(chat_id, status_text(), markup)
        return

    if data == "WIZ:FIAT" or data == "MENU:FIAT":
        show_screen(chat_id, "Выбери валюту 💱", fiat_buttons())
        return

    if data == "WIZ:START":
        wizard_start(chat_id)
        return

    if data.startswith("FIAT:"):
        val = data.split(":", 1)[1]
        if val not in SUPPORTED_FIATS:
            show_screen(chat_id, "Ошибка валюты.", kb_start())
            return

        STATE["fiat"] = val
        STATE["fiat_set"] = True
        _msg = f"✅ Валюта: {val}\nТеперь введи баланс в {val}: balance 200"
        show_screen(chat_id, _msg, kb_start())
        return

    if data.startswith("PAY:"):
        if not STATE["fiat_set"]:
            show_screen(chat_id, "Сначала выбери валюту 💱", fiat_buttons())
            return
        if not STATE["balance_set"]:
            show_screen(chat_id, f"Сначала введи баланс в {STATE['fiat']}: balance 200", kb_start())
            return

        val = data.split(":", 1)[1]
        STATE["pay_types"] = [] if val == "ANY" else [val]
        STATE["pay_set"] = True

        if STATE.get("wizard_step") == 1:
            wizard_next(chat_id)
        else:
            show_screen(chat_id, f"✅ Оплата: {pay_label()}", kb_main() if preflight(chat_id) else kb_start())
        return

    if data.startswith("RNG:"):
        parts = data.split(":")
        if len(parts) == 3:
            mn = safe_float(parts[1], None)
            mx = safe_float(parts[2], None)
            if mn is not None and mx is not None and mn < mx:
                STATE["spread_min"] = float(mn)
                STATE["spread_max"] = float(mx)
                STATE["range_set"] = True

                if STATE.get("wizard_step") == 2:
                    wizard_next(chat_id)
                else:
                    show_screen(chat_id, f"✅ Диапазон: {mn:.1f}–{mx:.1f}%", kb_main())
                return

        show_screen(chat_id, "Ошибка диапазона.", kb_start())
        return

    if data.startswith("ALERT:"):
        val = safe_float(data.split(":", 1)[1], None)
        if val is None:
            show_screen(chat_id, "Ошибка порога.", kb_start())
            return

        STATE["alert_spread"] = float(val)
        STATE["alert_set"] = True

        if STATE.get("wizard_step") == 3:
            wizard_next(chat_id)
        else:
            show_screen(chat_id, f"✅ Порог: {STATE['alert_spread']:.1f}%", kb_main())
        return

    if data.startswith("SPD:"):
        sec = safe_int(data.split(":", 1)[1], 5)
        sec = max(5, min(60, sec))
        STATE["poll_seconds"] = sec
        STATE["speed_set"] = True

        if STATE.get("wizard_step") == 4:
            wizard_next(chat_id)
        else:
            show_screen(chat_id, f"✅ Скорость: {sec}s", kb_main())
        return

    if data == "MENU:PAY":
        if not STATE["fiat_set"]:
            show_screen(chat_id, "Сначала выбери валюту 💱", fiat_buttons())
            return
        if not STATE["balance_set"]:
            show_screen(chat_id, f"Сначала введи баланс в {STATE['fiat']}: balance 200", kb_start())
            return
        STATE["wizard_step"] = 1
        show_screen(chat_id, "Шаг 2/5 — выбери оплату 💳", pay_buttons())
        return

    if data == "MENU:RANGE":
        if not STATE["balance_set"]:
            show_screen(chat_id, f"Сначала введи баланс в {STATE['fiat']}: balance 200", kb_start())
            return
        STATE["wizard_step"] = 2
        show_screen(chat_id, "Шаг 3/5 — выбери диапазон 📈", range_buttons())
        return

    if data == "MENU:ALERT":
        if not STATE["balance_set"]:
            show_screen(chat_id, f"Сначала введи баланс в {STATE['fiat']}: balance 200", kb_start())
            return
        STATE["wizard_step"] = 3
        show_screen(chat_id, "Шаг 4/5 — выбери порог ⚡", alert_buttons())
        return

    if data == "MENU:SPEED":
        if not STATE["balance_set"]:
            show_screen(chat_id, f"Сначала введи баланс в {STATE['fiat']}: balance 200", kb_start())
            return
        STATE["wizard_step"] = 4
        show_screen(chat_id, "Шаг 5/5 — выбери скорость ⏱", speed_buttons())
        return

    if data == "MENU:AI":
        STATE["ai_enabled"] = not STATE["ai_enabled"]
        markup = kb_main() if preflight(chat_id) else kb_start()
        show_screen(chat_id, f"🤖 AI теперь: {'ON' if STATE['ai_enabled'] else 'OFF'}", markup)
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


# =========================
# Main
# =========================
def main():
    if not TG_TOKEN:
        raise RuntimeError("Set env var TG_TOKEN")

    offset = 0
    try:
        old = tg_call("getUpdates", {"timeout": 0})
        if old.get("result"):
            offset = old["result"][-1]["update_id"] + 1
    except Exception:
        pass

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
