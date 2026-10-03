# -*- coding: utf-8 -*-
"""
Universo tecnico per Yahoo Finance Storico / scanner eToro.

Questo file contiene DATI e versionamento dell'universo, non regole finanziarie.
Il Mandato operativo resta l'unica fonte normativa.

LEVEL_A_STATUS = BOOTSTRAP: l'universo corrente e' stato separato dal server per
ridurre manutenzione e payload. L'obiettivo 700-800 strumenti verra' raggiunto
solo aggiungendo strumenti verificati come acquistabili eToro X1; non si dichiara
copertura sulla dimensione obiettivo finche' la verifica non e' completata.
"""

UNIVERSE_VERSION = "2026-10-e-bootstrap"
LEVEL_A_STATUS = "BOOTSTRAP_VERIFICA_X1_IN_CORSO"
LEVEL_A_TARGET_MIN = 700
LEVEL_A_TARGET_MAX = 800

# Insieme obbligatorio aggiuntivo; non sostituisce Level A/B.
PORTFOLIO_WATCH = ['CSPX.L', 'EIMI.L', 'WDEF.L', 'ONDS', 'PYPL']

# Level A bootstrap: base corrente gia' usata dallo scanner.
UNIVERSE_US = ['AAPL', 'MSFT', 'NVDA', 'AMZN', 'GOOGL', 'META', 'TSLA', 'AVGO', 'AMD', 'ORCL', 'CRM', 'ADBE', 'NFLX', 'INTC', 'QCOM', 'TXN', 'MU', 'AMAT', 'LRCX', 'KLAC', 'ARM', 'PLTR', 'SNOW', 'NOW', 'PANW', 'CRWD', 'FTNT', 'ZS', 'NET', 'DDOG', 'SHOP', 'UBER', 'ABNB', 'PYPL', 'COIN', 'ANET', 'MRVL', 'DELL', 'IBM', 'CSCO', 'TSM', 'ASML', 'VRT', 'ETN', 'CEG', 'VST', 'NU', 'ON', 'SMCI', 'CRDO', 'JPM', 'BAC', 'WFC', 'GS', 'MS', 'C', 'SCHW', 'BLK', 'V', 'MA', 'AXP', 'UNH', 'LLY', 'JNJ', 'PFE', 'MRK', 'ABBV', 'TMO', 'ABT', 'ISRG', 'VRTX', 'REGN', 'AMGN', 'GILD', 'XOM', 'CVX', 'COP', 'SLB', 'OXY', 'LIN', 'FCX', 'NEM', 'CAT', 'DE', 'BA', 'GE', 'HON', 'LMT', 'RTX', 'NOC', 'UPS', 'UNP', 'WMT', 'COST', 'HD', 'LOW', 'TGT', 'MCD', 'SBUX', 'NKE', 'KO', 'PEP', 'PG', 'DIS', 'T', 'VZ', 'TMUS', 'CMCSA', 'NEE', 'DUK', 'SO', 'SPY', 'QQQ', 'IWM', 'SMH', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB']

UNIVERSE_EU = ['SAP.DE', 'SIE.DE', 'ALV.DE', 'DTE.DE', 'MUV2.DE', 'BAS.DE', 'BAYN.DE', 'BMW.DE', 'MBG.DE', 'VOW3.DE', 'IFX.DE', 'RHM.DE', 'DBK.DE', 'ADS.DE', 'DHL.DE', 'ENR.DE', 'EOAN.DE', 'RWE.DE', 'MC.PA', 'OR.PA', 'RMS.PA', 'TTE.PA', 'SAN.PA', 'AIR.PA', 'SU.PA', 'BNP.PA', 'AI.PA', 'SAF.PA', 'EL.PA', 'KER.PA', 'CAP.PA', 'DG.PA', 'HO.PA', 'GLE.PA', 'STMPA.PA', 'ASML.AS', 'INGA.AS', 'ADYEN.AS', 'HEIA.AS', 'PHIA.AS', 'ASM.AS', 'BESI.AS', 'PRX.AS', 'ENI.MI', 'ENEL.MI', 'ISP.MI', 'UCG.MI', 'STLAM.MI', 'RACE.MI', 'LDO.MI', 'G.MI', 'PRY.MI', 'MONC.MI', 'SAN.MC', 'BBVA.MC', 'IBE.MC', 'ITX.MC', 'TEF.MC', 'REP.MC', 'NESN.SW', 'NOVN.SW', 'UBSG.SW', 'ABBN.SW', 'ZURN.SW', 'CFR.SW', 'AZN.L', 'SHEL.L', 'HSBA.L', 'ULVR.L', 'BP.L', 'RIO.L', 'GSK.L', 'BATS.L', 'LSEG.L', 'REL.L', 'BA.L', 'RR.L', 'DGE.L', 'NOVO-B.CO', 'ERIC-B.ST', 'VOLV-B.ST', 'SAAB-B.ST', 'NOKIA.HE', 'EQNR.OL']

# Level B: popolato dinamicamente dal server/strumenti quando implementato.
# Deve restare aggiuntivo, deduplicato e soggetto agli stessi filtri del Mandato.
LEVEL_B_STATIC_SEED = []

# Esclusioni tecniche note. La regola normativa completa resta nel Mandato.
EXCLUDED_SYMBOLS = {
    "COIN": "crypto-related",
    "MSTR": "crypto-related",
    "NEM": "precious-metals/mining",
}

def filtered_level_a(market):
    src = UNIVERSE_US if str(market).upper() == "US" else UNIVERSE_EU
    return [t for t in src if t not in EXCLUDED_SYMBOLS]

def universe_stats():
    us=filtered_level_a("US"); eu=filtered_level_a("EU")
    return {
        "version": UNIVERSE_VERSION,
        "level_a_status": LEVEL_A_STATUS,
        "target_min": LEVEL_A_TARGET_MIN,
        "target_max": LEVEL_A_TARGET_MAX,
        "us": len(us), "eu": len(eu), "total": len(set(us+eu)),
        "excluded_static": len(EXCLUDED_SYMBOLS),
    }
