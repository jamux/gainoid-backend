from flask import Flask, jsonify, request
from flask_cors import CORS
import krakenex
import os
from datetime import datetime

app = Flask(__name__)
CORS(app)


def get_kraken(api_key=None, api_secret=None):
    k = krakenex.API()
    k.key = api_key or os.environ.get('KRAKEN_API_KEY', '')
    k.secret = api_secret or os.environ.get('KRAKEN_API_SECRET', '')
    return k


@app.route('/')
def index():
    return jsonify({
        'status': 'online',
        'app': 'GAINOID by DCE Corp',
        'tagline': 'Trade. Slay. Profit.'
    })


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'bot': 'ShaniceAI ONLINE'})


@app.route('/verify-keys', methods=['POST'])
def verify_keys():
    data = request.json or {}
    api_key = data.get('api_key', '').strip()
    api_secret = data.get('api_secret', '').strip()

    if not api_key or not api_secret:
        return jsonify({'ok': False, 'error': 'Both API key and secret are required'}), 400

    # Step 1: check Kraken public API is reachable
    try:
        k = get_kraken(api_key, api_secret)
        time_resp = k.query_public('Time')
        if time_resp.get('error') and time_resp['error']:
            return jsonify({'ok': False, 'error': f"Kraken unreachable: {time_resp['error']}"}), 502
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Cannot reach Kraken: {str(e)}"}), 502

    # Step 2: verify the private keys by fetching balance
    try:
        bal = k.query_private('Balance')
        if bal.get('error') and bal['error']:
            return jsonify({'ok': False, 'error': f"Invalid API keys: {bal['error']}"}), 401

        balances = {c: float(a) for c, a in bal['result'].items() if float(a) > 0.0001}
        gbp = balances.get('ZGBP', 0.0)
        asset_count = len([c for c in balances if c != 'ZGBP'])

        return jsonify({
            'ok': True,
            'message': 'Kraken connected. Keys valid.',
            'gbp_balance': round(gbp, 2),
            'asset_count': asset_count,
        })
    except Exception as e:
        return jsonify({'ok': False, 'error': f"Key verification failed: {str(e)}"}), 500


@app.route('/portfolio', methods=['POST'])
def portfolio():
    data = request.json or {}
    api_key = data.get('api_key') or os.environ.get('KRAKEN_API_KEY', '')
    api_secret = data.get('api_secret') or os.environ.get('KRAKEN_API_SECRET', '')

    if not api_key or not api_secret:
        return jsonify({'error': 'API keys required'}), 401

    try:
        k = get_kraken(api_key, api_secret)
        bal = k.query_private('Balance')

        if bal.get('error'):
            return jsonify({'error': str(bal['error'])}), 400

        balances = {
            c: float(a)
            for c, a in bal['result'].items()
            if float(a) > 0.0001
        }

        COINS = {
            'XXRP':  {'sym': 'XRP',  'name': 'Ripple',   'pair': 'XRPGBP'},
            'XXBT':  {'sym': 'BTC',  'name': 'Bitcoin',  'pair': 'XXBTZGBP'},
            'XDOGE': {'sym': 'DOGE', 'name': 'Dogecoin', 'pair': 'DOGEGBP'},
            'MATIC': {'sym': 'MATIC','name': 'Polygon',  'pair': 'MATICGBP'},
            'NXS':   {'sym': 'NXS',  'name': 'Nexus',    'pair': 'NXSGBP'},
            'XETH':  {'sym': 'ETH',  'name': 'Ethereum', 'pair': 'XETHZGBP'},
            'XLTC':  {'sym': 'LTC',  'name': 'Litecoin', 'pair': 'XLTCZGBP'},
            'ZGBP':  {'sym': 'GBP',  'name': 'Sterling', 'pair': None},
        }

        pairs = [v['pair'] for v in COINS.values() if v['pair']]
        ticker = k.query_public('Ticker', {'pair': ','.join(pairs)})

        prices = {}
        opens = {}
        if not ticker.get('error'):
            for pair, d in ticker['result'].items():
                prices[pair] = float(d['c'][0])
                opens[pair] = float(d['o'])

        assets = []
        total = 0.0

        for coin, amount in balances.items():
            info = COINS.get(coin)

            if coin == 'ZGBP':
                total += amount
                assets.append({
                    'symbol': 'GBP',
                    'name': 'Sterling (Cash)',
                    'icon': 'GBP',
                    'amount': round(amount, 2),
                    'price_gbp': 1.0,
                    'value_gbp': round(amount, 2),
                    'change_24h': 0.0,
                    'held': True
                })
                continue

            if info:
                pair = info['pair']
                price = prices.get(pair, 0)
                open_p = opens.get(pair, 0)
                value = round(amount * price, 2)
                change = round(((price - open_p) / open_p) * 100, 2) if open_p > 0 else 0
                total += value
                assets.append({
                    'symbol': info['sym'],
                    'name': info['name'],
                    'icon': info['sym'],
                    'amount': round(amount, 8),
                    'price_gbp': round(price, 6),
                    'value_gbp': value,
                    'change_24h': change,
                    'held': True
                })
            else:
                assets.append({
                    'symbol': coin,
                    'name': coin,
                    'icon': coin,
                    'amount': round(amount, 8),
                    'price_gbp': 0,
                    'value_gbp': 0,
                    'change_24h': 0,
                    'held': False
                })

        assets.sort(key=lambda x: x['value_gbp'], reverse=True)

        return jsonify({
            'assets': assets,
            'total_gbp': round(total, 2),
            'timestamp': datetime.utcnow().isoformat()
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/trades', methods=['POST'])
def trades():
    data = request.json or {}
    api_key = data.get('api_key') or os.environ.get('KRAKEN_API_KEY', '')
    api_secret = data.get('api_secret') or os.environ.get('KRAKEN_API_SECRET', '')

    if not api_key or not api_secret:
        return jsonify({'error': 'API keys required'}), 401

    try:
        k = get_kraken(api_key, api_secret)
        resp = k.query_private('TradesHistory', {'trades': True})

        if resp.get('error'):
            return jsonify({'error': str(resp['error'])}), 400

        trades_raw = resp.get('result', {}).get('trades', {})

        trades_list = []
        for tid, t in list(trades_raw.items())[:20]:
            trades_list.append({
                'id': tid,
                'pair': t.get('pair', ''),
                'side': t.get('type', ''),
                'amount': round(float(t.get('cost', 0)), 2),
                'price': round(float(t.get('price', 0)), 6),
                'fee': round(float(t.get('fee', 0)), 4),
                'time': datetime.utcfromtimestamp(
                    float(t.get('time', 0))
                ).strftime('%H:%M:%S')
            })

        trades_list.sort(key=lambda x: x['time'], reverse=True)

        return jsonify({
            'trades': trades_list,
            'count': len(trades_list)
        })

    except Exception as e:
        return jsonify({'error': str(e)}), 500


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("GAINOID BACKEND - DCE Corp - ShaniceAI ONLINE")
    app.run(host='0.0.0.0', port=port, debug=False)
