import time
import json
from mcp.server.fastmcp import FastMCP
import yfinance as yf

# Inizializziamo il server impostando esplicitamente il trasporto HTTP richiesto per il Cloud
mcp = FastMCP("YahooFinanceServer", transport="streamable-http")

CACHE = {}
CACHE_TTL = 3600  # Cache di 1 ora per azzerare l'errore 429

def get_data(ticker_symbol):
    now = time.time()
    if ticker_symbol in CACHE and (now - CACHE[ticker_symbol]['timestamp'] < CACHE_TTL):
        return CACHE[ticker_symbol]['data']
    
    ticker = yf.Ticker(ticker_symbol)
    hist = ticker.history(period="5y", interval="1d")
    
    if hist.empty:
        raise ValueError("Nessun dato trovato per questo Ticker")
        
    hist_dict = hist.reset_index().to_dict(orient="records")
    for row in hist_dict:
        row['Date'] = row['Date'].strftime('%Y-%m-%d')
        
    actions = ticker.actions
    actions_dict = actions.reset_index().to_dict(orient="records") if not actions.empty else []
    for row in actions_dict:
        row['Date'] = row['Date'].strftime('%Y-%m-%d')
        
    payload = {
        "ticker": ticker_symbol,
        "history": hist_dict[-100:], # Mandiamo gli ultimi 100 record per non eccedere i token
        "actions": actions_dict[-20:]
    }
    
    CACHE[ticker_symbol] = {'timestamp': now, 'data': payload}
    return payload

@mcp.tool()
def get_market_data(ticker: str) -> str:
    """Estrae storico rettificato, dividendi e split per WDEF.L, SU.PA e altri ticker di Yahoo Finance."""
    try:
        data = get_data(ticker)
        return json.dumps(data, indent=2)
    except Exception as e:
        return f"Errore nel caricamento dei dati: {str(e)}"

# Lasciamo che FastMCP gestisca internamente l'avvio della porta di Render
if __name__ == "__main__":
    mcp.run()
