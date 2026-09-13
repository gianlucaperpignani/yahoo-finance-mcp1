import os
import time
import json
from flask import Flask, jsonify, request
import yfinance as yf

app = Flask(__name__)

CACHE = {}
CACHE_TTL = 3600  # 1 ora di cache

def get_data(ticker_symbol):
    now = time.time()
    if ticker_symbol in CACHE and (now - CACHE[ticker_symbol]['timestamp'] < CACHE_TTL):
        return CACHE[ticker_symbol]['data']
    
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="5y", interval="1d")
    
    if hist.empty:
        raise ValueError("Nessun dato trovato")
        
    hist_dict = hist.reset_index().to_dict(orient="records")
    for row in hist_dict:
        row['Date'] = row['Date'].strftime('%Y-%m-%d')
        
    actions = ticker.actions
    actions_dict = actions.reset_index().to_dict(orient="records") if not actions.empty else []
    for row in actions_dict:
        row['Date'] = row['Date'].strftime('%Y-%m-%d')
        
    payload = {
        "ticker": ticker_symbol,
        "history": hist_dict[-100:],  # Ultimi 100 giorni per risparmiare token
        "actions": actions_dict[-20:] # Ultimi dividendi e split
    }
    
    CACHE[ticker_symbol] = {'timestamp': now, 'data': payload}
    return payload

@app.route('/market-data', methods=['GET'])
def market_data_api():
    ticker = request.args.get('ticker')
    if not ticker:
        return jsonify({"errore": "Parametro 'ticker' mancante"}), 400
    try:
        data = get_data(ticker.upper())
        return jsonify(data)
    except Exception as e:
        return jsonify({"errore": str(e)}), 500

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    app.run(host="0.0.0.0", port=port)
