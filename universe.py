# -*- coding: utf-8 -*-
"""
Universo tecnico Level A per Yahoo Finance Storico / scanner eToro.

DATI TECNICI, non regole finanziarie.
Il Mandato operativo eToro resta l'unica fonte normativa.

Costruzione 2026-10-03:
- USA: componenti S&P 500 verificati sull'account eToro come apribili long X1
  con quotazione corrente disponibile + ETF USA liquidi verificati.
- Europa: titoli dei principali indici europei verificati sull'account eToro
  come apribili long X1 con quotazione corrente disponibile + ETF verificati.
- Estensione fino a 790 con prevalenza a growth/momentum e temi ad alta opportunita', mantenendo liquidita' e X1.
- Level B non ancora implementato.
"""

UNIVERSE_VERSION = "2026-10-g-level-a-790"
LEVEL_A_STATUS = "VERIFICATO_ETORO_X1_DA_STRESS_TEST_YAHOO"
LEVEL_A_TARGET_MIN = 700
LEVEL_A_TARGET_MAX = 800

PORTFOLIO_WATCH = ['CSPX.L', 'EIMI.L', 'WDEF.L', 'ONDS', 'PYPL']

UNIVERSE_US = ['MMM', 'AOS', 'ABT', 'ABBV', 'ACN', 'ADBE', 'AMD', 'AES', 'AFL', 'A', 'APD', 'ABNB', 'AKAM', 'ALB', 'ARE', 'ALGN', 'ALLE', 'LNT', 'ALL', 'GOOGL', 'GOOG', 'MO', 'AMZN', 'AMCR', 'AEE', 'AEP', 'AXP', 'AIG', 'AMT', 'AWK', 'AMP', 'AME', 'AMGN', 'APH', 'AON', 'APA', 'APO', 'AAPL', 'AMAT', 'APP', 'APTV', 'ACGL', 'ADM', 'ARES', 'ANET', 'AJG', 'AIZ', 'ATO', 'ADSK', 'ADP', 'AZO', 'AVY', 'AXON', 'BKR', 'BALL', 'BAC', 'BAX', 'BDX', 'BRK.B', 'BBY', 'TECH', 'BIIB', 'BLK', 'BX', 'XYZ', 'BE', 'BNY', 'BA', 'BKNG', 'BMY', 'AVGO', 'BR', 'BRO', 'BG', 'BXP', 'CHRW', 'CDNS', 'CPT', 'COF', 'CAH', 'CCL', 'CARR', 'CVNA', 'CASY', 'CAT', 'CBOE', 'CBRE', 'CDW', 'COR', 'CNC', 'CNP', 'CF', 'CRL', 'SCHW', 'CHTR', 'CMG', 'CB', 'CHD', 'CIEN', 'CI', 'CINF', 'CTAS', 'CSCO', 'C', 'CFG', 'CLX', 'CME', 'CMS', 'KO', 'CTSH', 'COHR', 'CL', 'CMCSA', 'FIX', 'COP', 'ED', 'STZ', 'CEG', 'COO', 'CPRT', 'GLW', 'CPAY', 'CTVA', 'CSGP', 'COST', 'CRH', 'CRWD', 'CCI', 'CSX', 'CMI', 'CVS', 'DHR', 'DRI', 'DDOG', 'DVA', 'DECK', 'DE', 'DELL', 'DAL', 'DVN', 'DXCM', 'FANG', 'DLR', 'DLTR', 'D', 'DPZ', 'DOV', 'DOW', 'DHI', 'DTE', 'DUK', 'DD', 'ETN', 'EBAY', 'ECHO', 'ECL', 'EIX', 'EW', 'ELV', 'EME', 'EMR', 'ETR', 'EOG', 'EQT', 'EFX', 'EQIX', 'ERIE', 'ESS', 'EL', 'EG', 'EVRG', 'P', 'ES', 'EXC', 'EXE', 'EXPE', 'EXPD', 'EXR', 'XOM', 'FFIV', 'FDS', 'FICO', 'FAST', 'FRT', 'FDX', 'FERG', 'FITB', 'FSLR', 'FE', 'FISV', 'FLEX', 'F', 'FTNT', 'FTV', 'FOXA', 'FOX', 'BEN', 'FCX', 'GRMN', 'IT', 'GE', 'GEHC', 'GEV', 'GEN', 'GNRC', 'GD', 'GIS', 'GM', 'GPC', 'GILD', 'GPN', 'GL', 'GDDY', 'GS', 'HAL', 'HIG', 'HAS', 'HCA', 'DOC', 'HSIC', 'HSY', 'HPE', 'HLT', 'HD', 'HONA', 'HON', 'HRL', 'HST', 'HWM', 'HPQ', 'HUBB', 'HUM', 'HBAN', 'HII', 'IBM', 'IEX', 'IDXX', 'ITW', 'ILMN', 'INCY', 'IR', 'PODD', 'INTC', 'IBKR', 'ICE', 'IFF', 'IP', 'INTU', 'ISRG', 'INVH', 'IQV', 'IRM', 'JBHT', 'JBL', 'JKHY', 'J', 'JNJ', 'JCI', 'JPM', 'KVUE', 'KDP', 'KEY', 'KEYS', 'KMB', 'KIM', 'KMI', 'KKR', 'KLAC', 'KHC', 'KR', 'LHX', 'LH', 'LRCX', 'LVS', 'LDOS', 'LEN', 'LII', 'LLY', 'LIN', 'LYV', 'LMT', 'L', 'LOW', 'LULU', 'LITE', 'LYB', 'MTB', 'MPC', 'MAR', 'MRSH', 'MLM', 'MRVL', 'MAS', 'MA', 'MKC', 'MCD', 'MCK', 'MRK', 'META', 'MET', 'MTD', 'MGM', 'MCHP', 'MU', 'MSFT', 'MAA', 'MRNA', 'MDLZ', 'MPWR', 'MNST', 'MCO', 'MS', 'MOS', 'MSI', 'MSCI', 'NDAQ', 'NTAP', 'NFLX', 'NWSA', 'NWS', 'NEE', 'NKE', 'NI', 'NDSN', 'NSC', 'NTRS', 'NOC', 'NCLH', 'NRG', 'NUE', 'NVDA', 'NVR', 'NXPI', 'ORLY', 'OXY', 'ODFL', 'OMC', 'ON', 'OKE', 'ORCL', 'OTIS', 'PCAR', 'PKG', 'PLTR', 'PANW', 'PSKY', 'PH', 'PAYX', 'PYPL', 'PNR', 'PEP', 'PFE', 'PCG', 'PM', 'PSX', 'PNW', 'PNC', 'PPG', 'PPL', 'PFG', 'PG', 'PGR', 'PLD', 'PRU', 'PEG', 'PTC', 'PSA', 'PHM', 'PWR', 'QCOM', 'DGX', 'Q', 'RL', 'RJF', 'RDDT', 'RTX', 'O', 'REG', 'REGN', 'RF', 'RSG', 'RMD', 'RVTY', 'HOOD', 'ROK', 'ROL', 'ROP', 'ROST', 'RCL', 'SPGI', 'CRM', 'SNDK', 'SBAC', 'SLB', 'STX', 'SRE', 'NOW', 'SHW', 'SPG', 'SWKS', 'SJM', 'SW', 'SNA', 'SOLV', 'SO', 'LUV', 'SWK', 'SBUX', 'STT', 'STLD', 'STE', 'SYK', 'SMCI', 'SYF', 'SNPS', 'SYY', 'TMUS', 'TROW', 'TTWO', 'TPR', 'TRGP', 'TGT', 'TDY', 'TER', 'TSLA', 'TXN', 'TPL', 'TXT', 'TMO', 'TJX', 'TKO', 'TSCO', 'TT', 'TDG', 'TRV', 'TRMB', 'TFC', 'TYL', 'TSN', 'USB', 'UBER', 'UDR', 'ULTA', 'UNP', 'UAL', 'UPS', 'URI', 'UNH', 'UHS', 'VLO', 'VEEV', 'VTR', 'VLTO', 'VRSN', 'VRSK', 'VZ', 'VRTX', 'VRT', 'VTRS', 'VICI', 'V', 'VST', 'VMRK', 'VMC', 'GWW', 'WAB', 'WMT', 'DIS', 'WBD', 'WM', 'WAT', 'WEC', 'WFC', 'WELL', 'WST', 'WDC', 'WY', 'WSM', 'WMB', 'WTW', 'WDAY', 'WYNN', 'XEL', 'XYL', 'YUM', 'ZBRA', 'ZBH', 'ZTS', 'SPY', 'QQQ', 'IWM', 'SMH', 'XLK', 'XLF', 'XLE', 'XLV', 'XLI', 'XLY', 'XLP', 'XLU', 'XLB', 'ARM', 'CRDO', 'SNOW', 'NET', 'SHOP', 'TWLO', 'ALAB', 'NBIS', 'CRWV', 'AAOI', 'CLS', 'DOCN', 'MDB', 'ESTC', 'GTLB', 'IOT', 'CHKP', 'WIX', 'FROG', 'PCOR', 'APPF', 'GWRE', 'RBRK', 'VRNS', 'TENB', 'QLYS', 'RKLB', 'ASTS', 'IONQ', 'RGTI', 'QBTS', 'OKLO', 'SMR', 'ACHR', 'JOBY', 'PL', 'LUNR', 'ONDS', 'KTOS', 'AVAV', 'RCAT', 'SOFI', 'AFRM', 'TOST', 'NU', 'GRAB', 'SE', 'MELI', 'CPNG', 'BILI', 'PDD', 'BABA', 'BIDU', 'DKNG', 'HIMS', 'CRSP', 'BEAM', 'NTLA', 'RXRX', 'SDGR', 'TWST', 'GH', 'NTRA', 'TMDX', 'INSP', 'VKTX', 'HALO', 'DUOL', 'CAVA', 'CELH', 'ELF', 'BROS', 'WING', 'SHAK', 'PLUG', 'FCEL', 'RUN', 'ENPH', 'SEDG']
UNIVERSE_EU = ['ALFA.ST', 'AAF.L', 'AAL.L', 'ABF.L', 'AC.PA', 'ATCO-B.ST', 'ADM.L', 'ADS.DE', 'AFX.DE', 'AI.PA', 'AIR.PA', 'AIXA.DE', 'ALV.DE', 'ALW.L', 'ASSA-B.ST', 'AMS.MC', 'ANA.MC', 'ANTO.L', 'AT1.DE', 'AUTO.L', 'AZN.L', 'BA.L', 'BAB.L', 'BARC.L', 'BAS.DE', 'BATS.L', 'BAYN.DE', 'BBOX.L', 'BBVA.MC', 'BC8.DE', 'BTRW.L', 'BEI.DE', 'BKG.L', 'BKT.MC', 'BLND.L', 'BNP.PA', 'BNR.DE', 'BNZL.L', 'BRBY.L', 'BOL.ST', 'EN.PA', 'CABK.MC', 'CA.PA', 'CBK.DE', 'CCH.L', 'CAP.PA', 'CNA.L', 'SGO.PA', 'CON.DE', 'CRDA.L', 'CTEC.L', 'DBK.DE', 'DGE.L', 'DHER.DE', 'DHL.DE', 'DPLM.L', 'DSFIR.AS', 'DTG.DE', 'EDV.L', 'ELE.MC', 'ENG.MC', 'SIE.DE', 'ENT.L', 'STERV.HE', 'EQT.ST', 'ERIC-A.ST', 'EL.PA', 'EVD.DE', 'EVO.ST', 'EVT.DE', 'EXO.AS', 'EXPN.L', 'FCIT.L', 'FER.MC', 'FME.DE', 'FNTN.DE', 'FRA.DE', 'FRES.L', 'SWED-A.ST', 'ORA.PA', 'G24.DE', 'GBF.DE', 'GEBN.SW', 'GIVN.SW', 'GLEN.L', 'GLJ.DE', 'GRF.MC', 'GXI.DE', 'GYC.DE', 'ENGI.PA', 'HDD.DE', 'HIK.L', 'HOLN.SW', 'HLE.DE', 'HLMA.L', 'HOT.DE', 'HSBA.L', 'HWDN.L', 'HEXA-B.ST', 'IBE.MC', 'IDR.MC', 'ISP.MI', 'IFX.DE', 'IMB.L', 'INF.L', 'ITRK.L', 'INVE-A.ST', 'JD.L', 'JEN.DE', 'JUN3.DE', 'KBX.DE', 'KGF.L', 'KGX.DE', 'KRN.DE', 'LAND.L', 'LEG.DE', 'LGEN.L', 'LHA.DE', 'LLOY.L', 'LMP.L', 'LR.PA', 'LSEG.L', 'MAP.MC', 'MBG.DE', 'ML.PA', 'MNDI.L', 'MRO.L', 'MTS.MC', 'MTX.DE', 'NDA.DE', 'NDX1.DE', 'NEM.DE', 'NG.L', 'NOVN.SW', 'NTGY.MC', 'NWG.L', 'NXT.L', 'PAT.DE', 'RI.PA', 'KER.PA', 'PRU.L', 'PSH.L', 'PSM.DE', 'PSN.L', 'PSON.L', 'PUB.PA', 'QIA.DE', 'RAA.DE', 'RKT.L', 'REL.L', 'REP.MC', 'RHM.DE', 'ROP.SW', 'CFR.SW', 'RMV.L', 'RNO.PA', 'DTE.DE', 'RTO.L', 'SAB.MC', 'SAX.DE', 'SBRY.L', 'SCYR.MC', 'SAF.PA', 'SGE.L', 'SGRO.L', 'SHA0.DE', 'SHB-A.ST', 'SHEL.L', 'SIX2.DE', 'SKA-B.ST', 'SLHN.SW', 'SMIN.L', 'SMT.L', 'SN.L', 'SU.PA', 'SAN.PA', 'SPX.L', 'SREN.SW', 'SRT.DE', 'STAN.L', 'STLAM.MI', 'SAND.ST', 'SVT.L', 'SCMN.SW', 'SY1.DE', 'SYENS.BR', 'SZG.DE', 'TKA.DE', 'TLX.DE', 'TSCO.L', 'TTE.PA', 'UCG.MI', 'UMG.AS', 'ULVR.L', 'UTDI.DE', 'UU.L', 'VNA.DE', 'VOLV-B.ST', 'VOW3.DE', 'VIV.PA', 'WAF.DE', 'WCH.DE', 'WEIR.L', 'WRT1V.HE', 'WTB.L', 'ZAL.DE', 'ZURN.SW', 'CSPX.L', 'EIMI.L', 'WDEF.L']

# Estensione Level A: prevalenza a titoli liquidi con potenziale swing/momentum,
# senza limitarsi alle mega-cap. I gruppi sono metadati tecnici, non scoring operativo.
LEVEL_A_EXTENSION_GROUPS = {'AI_CLOUD_SEMI_SOFTWARE': ['ARM', 'CRDO', 'SNOW', 'NET', 'SHOP', 'TWLO', 'ALAB', 'NBIS', 'CRWV', 'AAOI', 'CLS', 'DOCN', 'MDB', 'ESTC', 'GTLB', 'IOT', 'CHKP', 'WIX', 'FROG', 'PCOR', 'APPF', 'GWRE', 'RBRK', 'VRNS', 'TENB', 'QLYS'], 'SPACE_DEFENCE_QUANTUM': ['RKLB', 'ASTS', 'IONQ', 'RGTI', 'QBTS', 'OKLO', 'SMR', 'ACHR', 'JOBY', 'PL', 'LUNR', 'ONDS', 'KTOS', 'AVAV', 'RCAT'], 'FINTECH_INTERNET_DIGITAL': ['SOFI', 'AFRM', 'TOST', 'NU', 'GRAB', 'SE', 'MELI', 'CPNG', 'BILI', 'PDD', 'BABA', 'BIDU', 'DKNG'], 'HEALTH_BIOTECH': ['HIMS', 'CRSP', 'BEAM', 'NTLA', 'RXRX', 'SDGR', 'TWST', 'GH', 'NTRA', 'TMDX', 'INSP', 'VKTX', 'HALO'], 'CONSUMER_GROWTH': ['DUOL', 'CAVA', 'CELH', 'ELF', 'BROS', 'WING', 'SHAK'], 'ENERGY_TRANSITION': ['PLUG', 'FCEL', 'RUN', 'ENPH', 'SEDG']}

LEVEL_B_STATIC_SEED = []

EXCLUDED_SYMBOLS = {'COIN': 'crypto-related', 'MSTR': 'crypto-related', 'NEM': 'precious-metals/mining'}

def filtered_level_a(market):
    src = UNIVERSE_US if str(market).upper() == "US" else UNIVERSE_EU
    return [t for t in src if t not in EXCLUDED_SYMBOLS]

def universe_stats():
    us = filtered_level_a("US")
    eu = filtered_level_a("EU")
    return {
        "version": UNIVERSE_VERSION,
        "level_a_status": LEVEL_A_STATUS,
        "target_min": LEVEL_A_TARGET_MIN,
        "target_max": LEVEL_A_TARGET_MAX,
        "us": len(us),
        "eu": len(eu),
        "total": len(set(us + eu)),
        "excluded_static": len(EXCLUDED_SYMBOLS),
    }
