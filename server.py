import time
import json
from mcp.server.fastmcp import FastMCP
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route
import yfinance as yf

# Inizializza FastMCP
mcp = FastMCP("YahooFinanceServer")

# Cache per evitare l'errore 429
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
        "history": hist_dict[-100:],
        "actions": actions_dict[-20:]
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

# Creazione dell'applicazione web per gestire il trasporto SSE richiesto da Render e ChatGPT
sse = SseServerTransport("/messages")

async def handle_sse(request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())

async def handle_messages(request):
    await sse.handle_post_message(request.scope, request.receive, request._send)

app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse, methods=["GET"]),
        Route("/messages", endpoint=handle_messages, methods=["POST"]),
    ]
)
