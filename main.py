from flask import Flask, jsonify, request
from flask_cors import CORS
import krakenex
import os
import threading
import time
from datetime import datetime

app = Flask(__name__)
CORS(app)

# ─── BOT STATE ────────────────────────────────────────────────────────────────
_lock     = threading.Lock()
_stop_evt = threading.Event()
_thread   = None

BOT = {
    'active':       False,
    'api_key':      '',
    'api_secret':   '',
    'config': {
        'mode':        'balanced',
        'interval':    15,
        'harvest_pct': 50,
        'min_order':   5.0,
        'stop_loss':   15,
        'held_coins':  [],
    },
    'trades':       [],
    'log':          [],
    'harvested':    0.0,
    'today_profit': 0.0,
    'total_fees':   0.0,
    'last_cycle':   None,
    'entry_prices': {},
    'dca_state':    {},   # {sym: {'tranche': N, 'avg_entry': float, 'total_qty': float}}
}

MODE = {
    'aggro':        {'strategy': 'momentum', 'buy':  0.5,  'sell': -1.0, 'alloc': 0.30},
    'balanced':     {'strategy': 'momentum', 'buy':  1.5,  'sell': -2.0, 'alloc': 0.20},
    'conservative': {'strategy': 'momentum', 'buy':  3.0,  'sell': -4.0, 'alloc': 0.10},
    'dip':          {'strategy': 'dip',      'buy': -1.0,  'sell':  1.5, 'alloc': 0.25},
    'dca':          {
        'strategy': 'dca',
        'tranches': [
            {'threshold': -0.8, 'alloc': 0.10},   # T1: drop 0.8%, spend 10% of GBP
            {'threshold': -1.8, 'alloc': 0.12},   # T2: drop 1.8%, spend 12% of GBP
            {'threshold': -3.0, 'alloc': 0.15},   # T3: drop 3.0%, spend 15% of GBP
        ],
        'sell': 1.2,  # sell all when 24h change recovers to +1.2%
    },
}

COINS = {
    'XXRP':  {'sym': 'XRP',  'name': 'Ripple',   'pair': 'XRPGBP'},
    'XXBT':  {'sym': 'BTC',  'name': 'Bitcoin',  'pair': 'XXBTZGBP'},
    'XDOGE': {'sym': 'DOGE', 'name': 'Dogecoin', 'pair': 'XDGGBP'},
    'XETH':  {'sym': 'ETH',  'name': 'Ethereum', 'pair': 'XETHZGBP'},
    'XLTC':  {'sym': 'LTC',  'name': 'Litecoin', 'pair': 'LTCGBP'},
    'XXLM':  {'sym': 'XLM',  'name': 'Stellar',  'pair': 'XXLMZGBP'},
    'SOL':   {'sym': 'SOL',  'name': 'Solana',   'pair': 'SOLGBP'},
    'ADA':   {'sym': 'ADA',  'name': 'Cardano',  'pair': 'ADAGBP'},
}


# ─── HELPERS ──────────────────────────────────────────────────────────────────
def get_kraken(api_key=None, api_secret=None):
    k = krakenex.API()
    k.key    = api_key    or os.environ.get('KRAKEN_API_KEY', '')
    k.secret = api_secret or os.environ.get('KRAKEN_API_SECRET', '')
    return k


def _log(level, msg):
    entry = f"[{datetime.utcnow().strftime('%H:%M:%S')}] {level}: {msg}"
    print(entry, flush=True)
    with _lock:
        BOT['log'].insert(0, entry)
        if len(BOT['log']) > 100:
            BOT['log'] = BOT['log'][:100]


def _record_trade(txid, side, sym, amount_gbp, price, fee, reason=''):
    with _lock:
        BOT['trades'].insert(0, {
            'id':     txid,
            'side':   side,
            'pair':   f"{sym}/GBP",
            'amount': round(amount_gbp, 2),
            'price':  round(price, 6),
            'fee':    round(fee, 4),
            'time':   datetime.utcnow().strftime('%H:%M:%S'),
            'reason': reason,
        })
        BOT['total_fees'] += fee
        if len(BOT['trades']) > 100:
            BOT['trades'] = BOT['trades'][:100]


def _do_buy(k, pair, sym, qty, price, spend_gbp):
    resp = k.query_private('AddOrder', {
        'pair':      pair,
        'type':      'buy',
        'ordertype': 'market',
        'volume':    f"{qty:.8f}",
    })
    errs = resp.get('error') or []
    if errs:
        _log('ERR', f"BUY {sym} failed: {errs}")
        return False
    txid = (resp.get('result', {}).get('txid') or ['?'])[0]
    _record_trade(txid, 'buy', sym, spend_gbp, price, spend_gbp * 0.0026, reason='MOMENTUM')
    _log('BUY', f"{sym} {qty:.6f} @ £{price:.4f}  spend=£{spend_gbp:.2f}")
    return True


def _do_sell(k, pair, sym, qty, price, reason='MOMENTUM'):
    resp = k.query_private('AddOrder', {
        'pair':      pair,
        'type':      'sell',
        'ordertype': 'market',
        'volume':    f"{qty:.8f}",
    })
    errs = resp.get('error') or []
    if errs:
        _log('ERR', f"SELL {sym} failed: {errs}")
        return False
    txid     = (resp.get('result', {}).get('txid') or ['?'])[0]
    proceeds = qty * price
    fee      = proceeds * 0.0026
    with _lock:
        entry  = BOT['entry_prices'].get(sym, price)
        profit = max(0.0, (price - entry) * qty)
        BOT['harvested']    += profit
        BOT['today_profit'] += profit
    _record_trade(txid, 'sell', sym, proceeds, price, fee, reason=reason)
    _log('SELL', f"{sym} {qty:.6f} @ £{price:.4f}  reason={reason}")
    return True


# ─── TRADE CYCLE ──────────────────────────────────────────────────────────────
def _run_cycle():
    with _lock:
        api_key      = BOT['api_key']
        api_secret   = BOT['api_secret']
        cfg          = {**BOT['config']}
        entry_prices = {**BOT['entry_prices']}
        dca_state    = {s: {**v} for s, v in BOT['dca_state'].items()}

    _log('CYCLE', 'Starting')
    k          = get_kraken(api_key, api_secret)
    thresholds = MODE.get(cfg['mode'], MODE['balanced'])
    held_coins = set(cfg.get('held_coins', []))

    try:
        bal_resp = k.query_private('Balance')
        if bal_resp.get('error') and bal_resp['error']:
            _log('ERR', f"Balance: {bal_resp['error']}")
            return
        balances = {c: float(a) for c, a in bal_resp['result'].items()}
    except Exception as e:
        _log('ERR', f"Balance fetch: {e}")
        return

    gbp = balances.get('ZGBP', 0.0)
    _log('CYCLE', f"GBP available: £{gbp:.2f}")

    try:
        pairs_str = ','.join(v['pair'] for v in COINS.values())
        tick      = k.query_public('Ticker', {'pair': pairs_str})
        if tick.get('error') and tick['error']:
            _log('ERR', f"Ticker: {tick['error']}")
            return
        prices = {pk: float(d['c'][0]) for pk, d in tick['result'].items()}
        opens  = {pk: float(d['o'])    for pk, d in tick['result'].items()}
    except Exception as e:
        _log('ERR', f"Ticker fetch: {e}")
        return

    for coin_key, info in COINS.items():
        sym  = info['sym']
        pair = info['pair']

        if sym in held_coins:
            continue

        price  = prices.get(pair, 0)
        open_p = opens.get(pair, 0)
        if not price or not open_p:
            continue

        change   = ((price - open_p) / open_p) * 100
        held_qty = balances.get(coin_key, 0.0)
        held_val = held_qty * price
        entry    = entry_prices.get(sym, 0)

        _log('SCAN', f"{sym} change={change:+.2f}%  held=£{held_val:.2f}  gbp=£{gbp:.2f}")

        # Stop loss (all strategies)
        if entry > 0 and held_qty > 0 and held_val >= cfg['min_order']:
            drop = ((price - entry) / entry) * 100
            if drop <= -cfg['stop_loss']:
                _log('STOP', f"{sym} drop={drop:.1f}% — stop loss triggered")
                if _do_sell(k, pair, sym, held_qty, price, reason='STOP_LOSS'):
                    with _lock:
                        BOT['entry_prices'].pop(sym, None)
                        BOT['dca_state'].pop(sym, None)
                continue

        strategy = thresholds.get('strategy', 'momentum')

        if strategy == 'dca':
            dca = dca_state.get(sym, {'tranche': 0, 'avg_entry': 0.0, 'total_qty': 0.0})

            # Sell all when price recovers to sell threshold
            if dca['tranche'] > 0 and held_qty > 0 and held_val >= cfg['min_order']:
                if change >= thresholds['sell']:
                    _log('SIGNAL', f"{sym} change={change:+.2f}% — SELL DCA (T{dca['tranche']}→all)")
                    if _do_sell(k, pair, sym, held_qty, price, reason='DCA'):
                        with _lock:
                            BOT['dca_state'].pop(sym, None)
                            BOT['entry_prices'].pop(sym, None)
                        dca_state.pop(sym, None)
            else:
                # Try to buy the next un-bought tranche
                for i, t in enumerate(thresholds['tranches']):
                    tranche_num = i + 1
                    if dca['tranche'] >= tranche_num:
                        continue  # already bought this tranche
                    if change <= t['threshold']:
                        spend = gbp * t['alloc']
                        if spend >= cfg['min_order']:
                            qty = spend / price
                            _log('SIGNAL', f"{sym} change={change:+.2f}% — BUY T{tranche_num}/3 (dca)")
                            if _do_buy(k, pair, sym, qty, price, spend):
                                old_qty   = dca['total_qty']
                                old_entry = dca['avg_entry']
                                new_qty   = old_qty + qty
                                new_avg   = ((old_qty * old_entry) + (qty * price)) / new_qty
                                dca       = {'tranche': tranche_num, 'avg_entry': new_avg, 'total_qty': new_qty}
                                dca_state[sym] = dca
                                with _lock:
                                    BOT['dca_state'][sym]    = {**dca}
                                    BOT['entry_prices'][sym] = new_avg
                                gbp -= spend
                        break  # one tranche per coin per cycle

        elif strategy == 'dip':
            buy_signal  = change <= thresholds['buy']  and held_qty == 0 and gbp >= cfg['min_order']
            sell_signal = change >= thresholds['sell'] and held_qty > 0  and held_val >= cfg['min_order']

            if buy_signal:
                spend = min(gbp * thresholds['alloc'], gbp - 1.0)
                if spend >= cfg['min_order']:
                    qty = spend / price
                    _log('SIGNAL', f"{sym} change={change:+.2f}% — BUY (dip)")
                    if _do_buy(k, pair, sym, qty, price, spend):
                        with _lock:
                            BOT['entry_prices'][sym] = price
                        gbp -= spend
            elif sell_signal:
                _log('SIGNAL', f"{sym} change={change:+.2f}% — SELL (dip)")
                if _do_sell(k, pair, sym, held_qty, price, reason='DIP'):
                    with _lock:
                        BOT['entry_prices'].pop(sym, None)

        else:
            # Momentum: buy on upward move, sell on downward move
            buy_signal  = change >= thresholds['buy']  and gbp >= cfg['min_order']
            sell_signal = change <= thresholds['sell'] and held_qty > 0 and held_val >= cfg['min_order']

            if buy_signal:
                spend = min(gbp * thresholds['alloc'], gbp - 1.0)
                if spend >= cfg['min_order']:
                    qty = spend / price
                    _log('SIGNAL', f"{sym} change={change:+.2f}% — BUY (momentum)")
                    if _do_buy(k, pair, sym, qty, price, spend):
                        with _lock:
                            BOT['entry_prices'][sym] = price
                        gbp -= spend
            elif sell_signal:
                _log('SIGNAL', f"{sym} change={change:+.2f}% — SELL (momentum)")
                if _do_sell(k, pair, sym, held_qty, price, reason='MOMENTUM'):
                    with _lock:
                        BOT['entry_prices'].pop(sym, None)

    with _lock:
        BOT['last_cycle'] = datetime.utcnow().isoformat()
    _log('CYCLE', 'Complete')


def _bot_loop():
    _log('BOT', 'Thread started')
    while not _stop_evt.is_set():
        try:
            _run_cycle()
        except Exception as e:
            _log('ERR', f"Cycle exception: {e}")

        with _lock:
            interval_secs = BOT['config']['interval'] * 60

        elapsed = 0
        while elapsed < interval_secs and not _stop_evt.is_set():
            time.sleep(10)
            elapsed += 10

    with _lock:
        BOT['active'] = False
    _log('BOT', 'Thread stopped')


# ─── ROUTES ───────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return jsonify({'status': 'online', 'app': 'GAINOID by DCE Corp', 'tagline': 'Trade. Slay. Profit.'})


@app.route('/health')
def health():
    with _lock:
        active = BOT['active']
    return jsonify({'status': 'ok', 'bot': 'LIVE' if active else 'STANDBY'})


@app.route('/verify-keys', methods=['POST'])
def verify_keys():
    data       = request.json or {}
    api_key    = data.get('api_key', '').strip()
    api_secret = data.get('api_secret', '').strip()

    if not api_key or not api_secret:
        return jsonify({'ok': False, 'error': 'Both API key and secret are required'}), 400

    try:
        k         = get_kraken(api_key, api_secret)
        time_resp = k.query_public('Time')
        if time_resp.get('error') and time_resp['error']:
            return jsonify({'ok': False, 'error': f"Kraken unreachable: {time_resp['error']}"}), 502
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Cannot reach Kraken: {str(e)}"}), 502

    try:
        bal = k.query_private('Balance')
        if bal.get('error') and bal['error']:
            return jsonify({'ok': False, 'error': f"Invalid API keys: {bal['error']}"}), 401
        balances    = {c: float(a) for c, a in bal['result'].items() if float(a) > 0.0001}
        gbp         = balances.get('ZGBP', 0.0)
        asset_count = len([c for c in balances if c != 'ZGBP'])
        return jsonify({'ok': True, 'message': 'Connected', 'gbp_balance': round(gbp, 2), 'asset_count': asset_count})
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Key verification failed: {str(e)}"}), 500


@app.route('/portfolio', methods=['POST'])
def portfolio():
    data       = request.json or {}
    api_key    = data.get('api_key')    or os.environ.get('KRAKEN_API_KEY', '')
    api_secret = data.get('api_secret') or os.environ.get('KRAKEN_API_SECRET', '')

    if not api_key or not api_secret:
        return jsonify({'error': 'API keys required'}), 401

    try:
        k        = get_kraken(api_key, api_secret)
        bal      = k.query_private('Balance')
        if bal.get('error'):
            return jsonify({'error': str(bal['error'])}), 400

        balances = {c: float(a) for c, a in bal['result'].items() if float(a) > 0.0001}
        COIN_MAP = {**COINS, 'ZGBP': {'sym': 'GBP', 'name': 'Sterling', 'pair': None}}
        pairs  = [v['pair'] for v in COIN_MAP.values() if v['pair']]
        ticker = k.query_public('Ticker', {'pair': ','.join(pairs)})
        prices = {}
        opens  = {}
        if not ticker.get('error'):
            for pair, d in ticker['result'].items():
                prices[pair] = float(d['c'][0])
                opens[pair]  = float(d['o'])

        assets = []
        total  = 0.0
        for coin, amount in balances.items():
            info = COIN_MAP.get(coin)
            if coin == 'ZGBP':
                total += amount
                assets.append({'symbol': 'GBP', 'name': 'Sterling (Cash)', 'icon': 'GBP',
                                'amount': round(amount, 2), 'price_gbp': 1.0,
                                'value_gbp': round(amount, 2), 'change_24h': 0.0})
                continue
            if info:
                pair   = info['pair']
                price  = prices.get(pair, 0)
                open_p = opens.get(pair, 0)
                value  = round(amount * price, 2)
                change = round(((price - open_p) / open_p) * 100, 2) if open_p > 0 else 0
                total += value
                assets.append({'symbol': info['sym'], 'name': info['name'], 'icon': info['sym'],
                                'amount': round(amount, 8), 'price_gbp': round(price, 6),
                                'value_gbp': value, 'change_24h': change})
            # skip unknown coins we can't price

        assets.sort(key=lambda x: x['value_gbp'], reverse=True)
        return jsonify({'assets': assets, 'total_gbp': round(total, 2),
                        'timestamp': datetime.utcnow().isoformat()})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/trades', methods=['POST'])
def trades():
    data       = request.json or {}
    api_key    = data.get('api_key')    or os.environ.get('KRAKEN_API_KEY', '')
    api_secret = data.get('api_secret') or os.environ.get('KRAKEN_API_SECRET', '')

    if not api_key or not api_secret:
        return jsonify({'error': 'API keys required'}), 401

    try:
        k    = get_kraken(api_key, api_secret)
        resp = k.query_private('TradesHistory', {'trades': True})
        if resp.get('error'):
            return jsonify({'error': str(resp['error'])}), 400

        trades_raw  = resp.get('result', {}).get('trades', {})
        trades_list = []
        for tid, t in list(trades_raw.items())[:20]:
            trades_list.append({
                'id': tid, 'pair': t.get('pair', ''), 'side': t.get('type', ''),
                'amount': round(float(t.get('cost', 0)), 2),
                'price':  round(float(t.get('price', 0)), 6),
                'fee':    round(float(t.get('fee', 0)), 4),
                'time':   datetime.utcfromtimestamp(float(t.get('time', 0))).strftime('%H:%M:%S')
            })
        trades_list.sort(key=lambda x: x['time'], reverse=True)
        return jsonify({'trades': trades_list, 'count': len(trades_list)})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/bot/start', methods=['POST'])
def bot_start():
    global _thread, _stop_evt

    data       = request.json or {}
    api_key    = data.get('api_key', '').strip()    or os.environ.get('KRAKEN_API_KEY', '')
    api_secret = data.get('api_secret', '').strip() or os.environ.get('KRAKEN_API_SECRET', '')

    if not api_key or not api_secret:
        return jsonify({'error': 'API keys required'}), 401

    with _lock:
        if BOT['active']:
            return jsonify({'status': 'already_running', 'config': BOT['config']})
        BOT['api_key']    = api_key
        BOT['api_secret'] = api_secret
        BOT['active']     = True
        cfg = data.get('config', {})
        for field in ('mode', 'interval', 'harvest_pct', 'min_order', 'stop_loss', 'held_coins'):
            if field in cfg:
                BOT['config'][field] = cfg[field]

    _stop_evt.clear()
    _thread = threading.Thread(target=_bot_loop, daemon=True)
    _thread.start()
    _log('BOT', f"Started — mode={BOT['config']['mode']} interval={BOT['config']['interval']}m")
    return jsonify({'status': 'started', 'config': BOT['config']})


@app.route('/bot/stop', methods=['POST'])
def bot_stop():
    _stop_evt.set()
    with _lock:
        BOT['active'] = False
    _log('BOT', 'Stop requested')
    return jsonify({'status': 'stopping'})


@app.route('/bot/status', methods=['GET', 'POST'])
def bot_status():
    with _lock:
        return jsonify({
            'active':       BOT['active'],
            'config':       BOT['config'],
            'last_cycle':   BOT['last_cycle'],
            'trades':       BOT['trades'][:20],
            'trade_count':  len(BOT['trades']),
            'harvested':    round(BOT['harvested'], 2),
            'today_profit': round(BOT['today_profit'], 2),
            'total_fees':   round(BOT['total_fees'], 4),
            'log':          BOT['log'][:20],
        })


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("GAINOID BACKEND - DCE Corp - ShaniceAI ONLINE", flush=True)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
