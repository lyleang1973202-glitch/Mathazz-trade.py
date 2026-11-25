# =============================================================================
# V25.4 · LILY ASSISTANT BUTTON PANEL  (additive patch)
# Paste this block in MATHAZZ_AI_V25_3_9...py  RIGHT ABOVE:   if __name__ == "__main__":
#
# Fixes: Lily had commands only (no buttons, no callback handlers, no main-menu entry).
# Adds : 🌸 button on the main menu, /lily panel, and working buttons for
#        ON/OFF · Market · Micro · Intel · Chart · Target · PnL.
# =============================================================================
def _lily_is_on(chat_id):
    try:
        with _pro_db() as con:
            _lily_db_init(con)
            row = con.execute("SELECT active FROM lily_market_subscriptions WHERE chat_id=?",
                              (int(chat_id),)).fetchone()
        return bool(row and row[0])
    except Exception:
        return False


def _lily_panel(chat_id):
    on = _lily_is_on(chat_id)
    text = ("🌸 <b>កញ្ញាលីលី · XAUUSD ASSISTANT</b>\n━━━━━━━━━━━━━━\n"
            + ("🟢 Live Guardian: <b>ON</b>" if on else "🔴 Live Guardian: <b>OFF</b>") + "\n\n"
            + _lily_goal_status_text(chat_id)
            + "\n\n💬 សួរលីលីបានដោយវាយធម្មតា ឬ <code>/ask …</code>")
    toggle = ("🔴 បិទ Guardian", "lily_off") if on else ("🟢 បើក Guardian", "lily_on")
    return text, kb([
        [toggle, ("📡 Market", "lily_market")],
        [("🔬 Micro", "lily_micro"), ("🧠 Intel", "lily_intel")],
        [("📈 Chart M15", "lily_chart"), ("🎯 Target / PnL", "lily_goal")],
        [("🏠 Menu", "menu_main")],
    ])


def _lily_back():
    return kb([[("⬅️ Lily Panel", "lily_panel"), ("🔄 Refresh", "lily_refresh")],
               [("🏠 Menu", "menu_main")]])


def _lily_goal_markup():
    return kb([
        [("🎯 $25", "lily_t_25"), ("🎯 $50", "lily_t_50"), ("🎯 $100", "lily_t_100"), ("🎯 $200", "lily_t_200")],
        [("➕ $5", "lily_p_p5"), ("➕ $10", "lily_p_p10"), ("➕ $25", "lily_p_p25")],
        [("➖ $5", "lily_p_m5"), ("➖ $10", "lily_p_m10"), ("➖ $25", "lily_p_m25")],
        [("⬅️ Lily Panel", "lily_panel")],
    ])


_V254_LAST_VIEW = {}   # (chat_id, msg_id) -> callback used by Refresh


def _lily_callback(chat_id, msg_id, data, user_id):
    p = data.split("_")
    act = p[1] if len(p) > 1 else "panel"
    if act == "refresh":
        act = _V254_LAST_VIEW.get((chat_id, msg_id), "panel")
        p = ["lily", act]
    if act == "panel":
        text, markup = _lily_panel(chat_id)
    elif act in ("on", "off"):
        _lily_subscription_set(chat_id, user_id, act == "on")
        text, markup = _lily_panel(chat_id)
        text = ("🌸 Guardian បើកហើយ ✅\n\n" if act == "on" else "🔕 Guardian បិទហើយ\n\n") + text
    elif act in ("market", "micro", "intel"):
        plain(chat_id, msg_id, "⏳ លីលីកំពុងវិភាគ XAUUSD…")
        fn = {"market": _lily_market_command, "micro": _lily_micro_command, "intel": _lily_intel_command}[act]
        text, markup = fn(chat_id), _lily_back()
        _V254_LAST_VIEW[(chat_id, msg_id)] = act
    elif act == "goal":
        text, markup = _lily_goal_status_text(chat_id), _lily_goal_markup()
    elif act == "t" and len(p) > 2:
        text = _lily_target_command(chat_id, user_id, [p[2]]) + "\n\n" + _lily_goal_status_text(chat_id)
        markup = _lily_goal_markup()
    elif act == "p" and len(p) > 2 and len(p[2]) > 1:
        sign = "+" if p[2][0] == "p" else "-"
        text = _lily_pnl_command(chat_id, user_id, [sign + p[2][1:]])
        markup = _lily_goal_markup()
    elif act == "chart":
        if not is_admin(user_id if user_id is not None else chat_id):
            text, markup = "🔒 Chart សម្រាប់ Admin ប៉ុណ្ណោះ។", _lily_back()
        else:
            _lyli_chart_command(chat_id, "chart XAUUSD M15", user_id)
            return
    else:
        text, markup = _lily_panel(chat_id)
    show(chat_id, msg_id, text, markup)


_V254_PREV_HANDLE_CALLBACK = handle_callback
def handle_callback(chat_id, msg_id, data, user_id=None):
    if str(data or "").startswith("lily_"):
        try:
            _lily_callback(chat_id, msg_id, data, user_id)
        except Exception as exc:
            traceback.print_exc()
            show(chat_id, msg_id, f"⚠️ Lily button error: {html.escape(type(exc).__name__)}",
                 kb([[("🌸 Lily Panel", "lily_panel"), ("🏠 Menu", "menu_main")]]))
        return
    return _V254_PREV_HANDLE_CALLBACK(chat_id, msg_id, data, user_id)


_V254_PREV_MAIN_MENU = main_menu
def main_menu():
    text, markup = _V254_PREV_MAIN_MENU()
    rows = list(markup.get("inline_keyboard", []))
    rows.insert(max(0, len(rows) - 1), [{"text": "🌸  LILY ASSISTANT", "callback_data": "lily_panel"}])
    return text, {"inline_keyboard": rows}


_V254_PREV_HANDLE_UPDATE = handle_update
def handle_update(update):
    try:
        m = (update or {}).get("message") or {}
        txt = (m.get("text") or "").strip()
        if txt:
            parts = txt.split()
            first = parts[0].lower().split("@", 1)[0]
            if first == "/lily" and (len(parts) == 1 or parts[1].lower() in ("help", "panel", "menu")):
                chat_id = (m.get("chat") or {}).get("id")
                _TG_CTX.v = {"chat_id": chat_id, "thread_id": _msg_thread(m)}
                text, markup = _lily_panel(chat_id)
                show(chat_id, None, text, markup)
                return
    except Exception as exc:
        print("V25.4 lily panel router:", type(exc).__name__, exc)
    return _V254_PREV_HANDLE_UPDATE(update)


BUILD_VERSION = "V25.4 LILY · BUTTON PANEL"
BUILD_LOG = [("V25.4", "Lily inline button panel: main-menu entry, /lily panel, ON/OFF, Market, Micro, Intel, Chart, quick Target and PnL buttons, Refresh")] + BUILD_LOG
