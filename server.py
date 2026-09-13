import time
from mcp.server.fastmcp import FastMCP
import yfinance as yf
import json

mcp = FastMCP("YahooFinanceServer")

# Cache per evitare l'errore 429 (Rate limit)
CACHE = {}
CACHE_TTL = 3600  # 1 ora

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
        "history": hist_dict[-100:], # Ultimi 100 giorni per risparmiare token
        "actions": actions_dict[-20:] # Ultimi dividendi/split
    }
    
    CACHE[ticker_symbol] = {'timestamp': now, 'data': payload}
    return payload

@mcp.tool()
def get_market_data(ticker: str) -> str:
    """Estrae storico rettificato, dividendi e split per WDEF.L, SU.PA e altri ticker."""
    try:
        data = get_data(ticker)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Errore: {str(e)}"

if __name__ == "__main__":
    mcp.run(transport="sse")
