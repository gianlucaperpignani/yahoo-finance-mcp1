import os
import time
import json
from datetime import datetime, timezone
from flask import Flask, jsonify, request
import yfinance as yf

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 1800  # 30 minuti

def get_advanced_data(ticker_symbol, period="5y", interval="1d", start=None, end=None):
    cache_key = f"{ticker_symbol}_{period}_{interval}_{start}_{end}"
    now = time.time()
    
    if cache_key in CACHE and (now - CACHE[cache_key]['timestamp'] < CACHE_TTL):
        return CACHE[cache_key]['data']
    
    ticker = yf.Ticker(ticker_symbol)
    
    if start or end:
        hist = ticker.history(start=start, end=end, interval=interval)
    else:
        hist = ticker.history(period=period, interval=interval)
        
    if hist.empty:
        raise ValueError(f"Nessun dato trovato per il ticker {ticker_symbol}")
        
    # Correzione Valuta Nativa
    info = ticker.info or {}
    currency = info.get('currency')
    if not currency:
        if ticker_symbol.endswith('.PA'):
            currency = 'EUR'
        elif ticker_symbol.endswith('.L'):
            currency = 'GBp'
        else:
            currency = info.get('financialCurrency', 'USD')
            
    exchange_timezone = info.get('exchangeTimezoneName', 'Europe/Paris' if ticker_symbol.endswith('.PA') else 'America/New_York')
    
    total_sessions_available = len(hist)
    hist_dict = hist.reset_index().to_dict(orient="records")
    formatted_history = []
    
    for row in hist_dict:
        date_str = row['Date'].strftime('%Y-%m-%d') if 'Date' in row else datetime.now().strftime('%Y-%m-%d')
        timestamp = int(row['Date'].timestamp()) if 'Date' in row else int(time.time())
            
        formatted_history.append({
            "date": date_str,
            "timestamp": timestamp,
            "open": float(row['Open']),
            "high": float(row['High']),
            "low": float(row['Low']),
            "close": float(row['Close']),
            "adj_close": float(row['Close']),
            "volume": int(row['Volume'])
        })
        
    sessions_returned = 300 if total_sessions_available > 300 else total_sessions_available
    truncated = total_sessions_available > 300
    final_history = formatted_history[-300:]
    
    formatted_actions = []
    actions = ticker.actions
    if actions is not None and not actions.empty:
        actions_dict = actions.reset_index().to_dict(orient="records")
        for row in actions_dict:
            formatted_actions.append({
                "date": row['Date'].strftime('%Y-%m-%d'),
                "dividends": float(row.get('Dividends', 0.0)),
                "stock_splits": float(row.get('Stock Splits', 0.0))
            })

    payload = {
        "ticker": ticker_symbol,
        "currency": currency,
        "exchange_timezone": exchange_timezone,
        "generated_at_utc": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
        "adjusted": True,
        "total_sessions_available": total_sessions_available,
        "sessions_returned": sessions_returned,
        "truncated": truncated,
        "history": final_history,
        "actions": formatted_actions[-30:]
    }
    
    CACHE[cache_key] = {'timestamp': now, 'data': payload}
    return payload

@app.route('/market-data', methods=['GET'])
def market_data_api():
    ticker = request.args.get('ticker')
    if not ticker:
        return jsonify({"errore": "Parametro ticker obbligatorio"}), 400
    try:
        return jsonify(get_advanced_data(ticker.upper()))
    except Exception as e:
        return jsonify({"errore": str(e)}), 500

# 🤖 SERVER MCP JSON-RPC AGGIORNATO CON NOTIFICHE E PARAMETRI COMPLETI
@app.route('/mcp', methods=['POST'])
def mcp_rpc_server():
    body = request.get_json(silent=True) or {}
    method = body.get("method")
    rpc_id = body.get("id", 1)
    
    # 🆕 CORREZIONE: Gestione notifica di inizializzazione completata (Standard MCP)
    if method == "notifications/initialized":
        return "", 204
    
    # 1. Inizializzazione MCP
    if method == "initialize":
        return jsonify({
            "jsonrpc": "2.0",
            "id": rpc_id,
            "result": {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "YahooFinanceAdvancedMCP", "version": "1.3.0"}
            }
        })
        
    # 2. Elenco Strumenti MCP (tools/list) con parametri start ed end aggiunti
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
        
    # 3. Esecuzione Strumento MCP (tools/call)
    elif method == "tools/call":
        params = body.get("params", {})
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        ticker = arguments.get("ticker")
        
        if tool_name == "get_market_data" and ticker:
            try:
                data = get_advanced_data(
                    ticker_symbol=ticker.upper(),
                    period=arguments.get("period", "5y"),
                    interval=arguments.get("interval", "1d"),
                    start=arguments.get("start"),
                    end=arguments.get("end")
                )
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "result": {
                        "content": [{"type": "text", "text": json.dumps(data)}]
                    }
                })
            except Exception as e:
                return jsonify({
                    "jsonrpc": "2.0",
                    "id": rpc_id,
                    "error": {"code": -32000, "message": str(e)}
                }), 500
                
    return jsonify({
        "jsonrpc": "2.0",
        "id": rpc_id,
        "error": {"code": -32601, "message": "Method not found"}
    }), 404

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
