import os
import time
import json
from flask import Flask, jsonify, request
import yfinance as yf
import pandas as pd

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 1800  # Cache di 30 minuti per evitare l'errore 429

def get_advanced_data(ticker_symbol, period="5y", interval="1d", start=None, end=None):
    """Recupera dati avanzati con parametri configurabili, timezone e valuta."""
    cache_key = f"{ticker_symbol}_{period}_{interval}_{start}_{end}"
    now = time.time()
    
    if cache_key in CACHE and (now - CACHE[cache_key]['timestamp'] < CACHE_TTL):
        return CACHE[cache_key]['data']
    
    ticker = yf.Ticker(ticker_symbol)
    
    # Gestione flessibile dei parametri richiesti start/end o period
    if start or end:
        hist = ticker.history(start=start, end=end, interval=interval)
    else:
        hist = ticker.history(period=period, interval=interval)
        
    if hist.empty:
        raise ValueError(f"Nessun dato trovato per il ticker {ticker_symbol}")
        
    # Estrazione metadati: Valuta e Timezone
    info = ticker.info
    currency = info.get('currency', 'USD')
    timezone = info.get('exchangeTimezoneName', 'UTC')
    
    # 1. Storico giornaliero REttificato certificato (almeno 260 sedute se disponibili)
    hist_dict = hist.reset_index().to_dict(orient="records")
    formatted_history = []
    
    for row in hist_dict:
        # Trasformazione date e timestamp completi
        if 'Date' in row:
            date_str = row['Date'].strftime('%Y-%m-%d')
            timestamp = int(row['Date'].timestamp())
        else:
            date_str = datetime.now().strftime('%Y-%m-%d')
            timestamp = int(time.time())
            
        formatted_history.append({
            "date": date_str,
            "timestamp": timestamp,
            "open": row['Open'],
            "high": row['High'],
            "low": row['Low'],
            "close": row['Close'],
            "adj_close": row.get('Close'), # In yfinance .history() restituisce GIÀ i prezzi rettificati
            "volume": row['Volume']
        })
        
    # 2. Dividendi e Split cronologici
    actions = ticker.actions
    formatted_actions = []
    if not actions.empty:
        actions_dict = actions.reset_index().to_dict(orient="records")
        for row in actions_dict:
            formatted_actions.append({
                "date": row['Date'].strftime('%Y-%m-%d'),
                "dividends": row.get('Dividends', 0.0),
                "stock_splits": row.get('Stock Splits', 0.0)
            })

    payload = {
        "ticker": ticker_symbol,
        "currency": currency,
        "timezone": timezone,
        "total_sessions_returned": len(formatted_history),
        "history": formatted_history[-300:], # Restituisce fino a 300 sedute (richieste almeno 260)
        "actions": formatted_actions[-30:]
    }
    
    CACHE[cache_key] = {'timestamp': now, 'data': payload}
    return payload

# Endpoint standard per le azioni di ChatGPT
@app.route('/market-data', methods=['GET'])
def market_data_api():
    ticker = request.args.get('ticker')
    period = request.args.get('period', '5y')
    interval = request.args.get('interval', '1d')
    start = request.args.get('start', None)
    end = request.args.get('end', None)
    
    if not ticker:
        return jsonify({"errore": "Parametro 'ticker' obbligatorio"}), 400
    try:
        data = get_advanced_data(ticker.upper(), period, interval, start, end)
        return jsonify(data)
    except Exception as e:
        return jsonify({"errore": str(e)}), 500

# 🆕 AGGIUNTA: Endpoint MCP richiesto esplicitamente da ChatGPT
@app.route('/mcp', methods=['POST', 'GET'])
def mcp_endpoint():
    """Funge da ponte di compatibilità per il protocollo MCP."""
    if request.method == 'GET':
        return jsonify({"status": "MCP endpoint active", "supported_transport": "HTTP/JSON"})
    
    # Gestione base di una richiesta POST in formato MCP
    body = request.get_json(silent=True) or {}
    ticker = body.get("params", {}).get("ticker", "AAPL")
    try:
        data = get_advanced_data(ticker.upper())
        return jsonify({
            "jsonrpc": "2.0",
            "result": {"content": [{"type": "text", "text": json.dumps(data)}]},
            "id": body.get("id", 1)
        })
    except Exception as e:
        return jsonify({"jsonrpc": "2.0", "error": {"code": -32000, "message": str(e)}, "id": body.get("id", 1)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
