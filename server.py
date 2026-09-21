import os
import time
import json
import math
import re
from threading import Lock
from zoneinfo import ZoneInfo
from datetime import datetime, timezone
from flask import Flask, jsonify, request
import yfinance as yf

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 1800  # 30 minuti
CACHE_LOCK = Lock()
VERSION = "1.4.0"

def finite(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("Dato numerico non finito")
    return value

def get_advanced_data(ticker_symbol, period="5y", interval="1d", start=None, end=None):
    if not isinstance(ticker_symbol, str) or not re.fullmatch(r"[A-Za-z0-9^][A-Za-z0-9.^=\-]{0,39}", ticker_symbol.strip()):
        raise ValueError("Ticker non valido")
    ticker_symbol = ticker_symbol.strip().upper()
    if interval not in ("1d", "1wk", "1mo") or period not in ("1d", "5d", "1mo", "3mo", "6mo", "1y", "2y", "5y", "10y", "ytd", "max"):
        raise ValueError("Period o interval non supportato")
    for value in (start, end):
        if value is not None:
            if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                raise ValueError("Data non valida: usare YYYY-MM-DD")
            datetime.strptime(value, "%Y-%m-%d")
    if start and end and start >= end:
        raise ValueError("start deve precedere end (esclusivo)")
    cache_key = f"{ticker_symbol}_{period}_{interval}_{start}_{end}"
    now = time.time()
    
    with CACHE_LOCK:
        if cache_key in CACHE and (now - CACHE[cache_key]['timestamp'] < CACHE_TTL):
            return dict(CACHE[cache_key]['data'], cache_hit=True)
    
    ticker = yf.Ticker(ticker_symbol)
    
    if start or end:
        hist = ticker.history(start=start, end=end, period=None, interval=interval,
                              auto_adjust=True, actions=True, timeout=15)
    else:
        hist = ticker.history(period=period, interval=interval,
                              auto_adjust=True, actions=True, timeout=15)
        
    if hist.empty:
        raise ValueError(f"Nessun dato trovato per il ticker {ticker_symbol}")
        
    # Use metadata of the price chart, never financialCurrency or ticker suffixes.
    info = ticker.get_history_metadata() or {}
    currency = info.get('currency')
    exchange_timezone = info.get('exchangeTimezoneName')
    if not isinstance(currency, str) or not currency.strip() or not isinstance(exchange_timezone, str):
        raise ValueError("Valuta/timezone mancanti: storico non validabile")
    ZoneInfo(exchange_timezone)
    if hist.index.tz is None or hist.index.has_duplicates:
        raise ValueError("Indice temporale senza timezone o duplicato")
    hist = hist.sort_index()
    hist.index = hist.index.tz_convert(exchange_timezone)
    hist.index.name = 'Date'
    if start:
        hist = hist[hist.index.strftime('%Y-%m-%d') >= start]
    if end:
        hist = hist[hist.index.strftime('%Y-%m-%d') < end]
    if hist.empty:
        raise ValueError("Nessun dato nell'intervallo richiesto")
    if not {'Dividends', 'Stock Splits'}.issubset(hist.columns):
        raise ValueError("Corporate actions mancanti")
    
    total_sessions_available = len(hist)
    hist_dict = hist.reset_index().to_dict(orient="records")
    formatted_history = []
    
    for row in hist_dict:
        date_str = row['Date'].strftime('%Y-%m-%d')
        timestamp = int(row['Date'].timestamp())
            
        formatted_history.append({
            "date": date_str,
            "timestamp": timestamp,
            "open": finite(row['Open']),
            "high": finite(row['High']),
            "low": finite(row['Low']),
            "close": finite(row['Close']),
            "adj_close": finite(row['Close']),
            "volume": finite(row['Volume'])
        })
        bar = formatted_history[-1]
        if (bar['low'] <= 0 or bar['low'] > min(bar['open'], bar['close'])
                or bar['high'] < max(bar['open'], bar['close'])
                or bar['volume'] < 0 or not bar['volume'].is_integer()):
            raise ValueError("OHLCV incoerenti")
        bar['volume'] = int(bar['volume'])
    if len({bar['date'] for bar in formatted_history}) != len(formatted_history):
        raise ValueError("Date duplicate")
        
    sessions_returned = 300 if total_sessions_available > 300 else total_sessions_available
    truncated = total_sessions_available > 300
    final_history = formatted_history[-300:]
    
    formatted_actions = []
    # Reuse actions from the same history request, not an extra max-history fetch.
    actions = hist.iloc[-300:][['Dividends', 'Stock Splits']]
    if actions is not None and not actions.empty:
        actions_dict = actions.reset_index().to_dict(orient="records")
        for row in actions_dict:
            formatted_actions.append({
                "date": row['Date'].strftime('%Y-%m-%d'),
                "dividends": finite(row['Dividends']),
                "stock_splits": finite(row['Stock Splits'])
            })
    formatted_actions = [a for a in formatted_actions if a['dividends'] or a['stock_splits']]

    payload = {
        "server_version": VERSION,
        "source": "Yahoo_Finance_Storico",
        "interval": interval,
        "period": None if start or end else period,
        "start": start, "end": end, "end_exclusive": True,
        "currency_source": "Yahoo chart metadata",
        "price_unit": currency,
        "prices_normalized": False,
        "major_currency": {"GBp": "GBP", "GBX": "GBP", "ZAc": "ZAR", "ILA": "ILS"}.get(currency, currency),
        "price_scale_to_major_currency": 0.01 if currency in ("GBp", "GBX", "ZAc", "ILA") else 1.0,
        "adjustment_method": "yfinance.history(auto_adjust=True)",
        "ohlc_adjusted": True,
        "cache_hit": False,
        "coverage_start": final_history[0]['date'],
        "coverage_end": final_history[-1]['date'],
        "next_end": final_history[0]['date'] if truncated else None,
        "actions_scope": "returned_history",
        "warnings": ["Calendario, ultima seduta chiusa, copertura e FX da validare a valle"],
        "ticker": ticker_symbol,
        "currency": currency,
        "exchange_timezone": exchange_timezone,
        "generated_at_utc": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "adjusted": True,
        "total_sessions_available": total_sessions_available,
        "sessions_returned": sessions_returned,
        "truncated": truncated,
        "history": final_history,
        "actions": formatted_actions
    }
    
    json.dumps(payload, allow_nan=False)
    with CACHE_LOCK:
        for key in list(CACHE):
            if time.time() - CACHE[key]['timestamp'] >= CACHE_TTL:
                del CACHE[key]
        if len(CACHE) >= 128:
            del CACHE[min(CACHE, key=lambda key: CACHE[key]['timestamp'])]
        CACHE[cache_key] = {'timestamp': time.time(), 'data': payload}
    return payload

@app.route('/market-data', methods=['GET'])
def market_data_api():
    ticker = request.args.get('ticker')
    if not ticker:
        return jsonify({"errore": "Parametro ticker obbligatorio"}), 400
    try:
        return jsonify(get_advanced_data(ticker, request.args.get('period', '5y'),
                                        request.args.get('interval', '1d'),
                                        request.args.get('start'), request.args.get('end')))
    except Exception as e:
        return jsonify({"errore": str(e)}), 500

@app.route('/mcp', methods=['POST'])
def mcp_rpc_server():
    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get('jsonrpc') != '2.0' or not isinstance(body.get('method'), str):
        return jsonify({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid Request'}}), 400
    method = body.get("method")
    rpc_id = body.get("id")
    if 'id' not in body:
        return "", 204
    if not isinstance(body.get('params', {}), dict):
        return jsonify({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': -32602, 'message': 'Invalid params'}})
    
    if method == "notifications/initialized":
        return "", 204
    
    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "YahooFinanceAdvancedMCP", "version": VERSION}
            }
        })
        
    elif method == "tools/list":
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "tools": [{
                    "name": "get_market_data",
                    "description": "Ottiene storico rettificato, valuta nativa corretta, timezone, split e dividendi per titoli USA ed Europei (es. SU.PA, WDEF.L, AAPL). Supporta intervalli temporali personalizzati.",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "ticker": {"type": "string", "description": "Il simbolo del ticker"},
                            "period": {"type": "string", "default": "5y", "description": "1d, 5d, 1mo, 1y, 5y, max"},
                            "interval": {"type": "string", "default": "1d", "description": "1d, 1wk, 1mo"},
                            "start": {"type": "string", "description": "Data inizio (Formato YYYY-MM-DD), opzionale"},
                            "end": {"type": "string", "description": "Data fine (Formato YYYY-MM-DD), opzionale"}
                        },
                        "required": ["ticker"]
                    }
                }]
            }
        })
        
    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(arguments, dict):
            return jsonify({'jsonrpc': '2.0', 'id': rpc_id, 'error': {'code': -32602, 'message': 'Invalid arguments'}})
        ticker = arguments.get("ticker")
        
        if tool_name == "get_market_data" and ticker:
            try:
                data = get_advanced_data(
                    ticker_symbol=ticker,
                    period=arguments.get("period", "5y"),
                    interval=arguments.get("interval", "1d"),
                    start=arguments.get("start"),
                    end=arguments.get("end")
                )
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(data, allow_nan=False)}],
                        "isError": False
                    }
                })
            except Exception as e:
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {"isError": True, "content": [{"type": "text", "text": json.dumps({
                        "status": "BLOCCATO PER DATI", "source": "Yahoo_Finance_Storico", "reason": str(e)
                    })}]}
                })
                
    return jsonify({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": "Method not found"}
    }), 404

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'version': VERSION})

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
