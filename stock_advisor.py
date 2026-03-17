import yfinance as yf
import xgboost as xgb
from sklearn.model_selection import train_test_split
import pandas as pd

def analyze_portfolio(excel_file):
    # 1. Holdings laden
    df_hold = pd.read_excel(excel_file)
    
    # 2. Markt-Daten + Features (100 Watchlist-Coins)
    tickers = ['IONQ', 'RGTI', 'QBTS', 'VERT', 'ON', 'SWKS', 'TENB']  # Deine Watchlist[cite:23]
    features = []
    
    for t in tickers:
        data = yf.download(t, period="1y")
        if len(data) < 200: continue
            
        roc = data.Close.pct_change(20).iloc[-1]
        atr = data['High'].sub(data['Low']).rolling(14).mean().iloc[-1]
        vola = data.Close.pct_change().std() * (252**0.5)
        
        features.append({
            'ticker': t,
            'roc_20d': roc,
            'atr_pct': atr/data.Close.iloc[-1],
            'volatilite': vola,
            'budget_fit': 500 / data.Close.iloc[-1],  # Shares möglich
            'in_portfolio': t in df_hold.Ticker.values
        })
    
    df_feat = pd.DataFrame(features)
    
    # 3. XGBoost trainieren (historische Buy/Sell-Signale)
    X = df_feat[['roc_20d', 'atr_pct', 'volatilite', 'budget_fit']]
    # Dummy Targets (ersetzen durch historische Returns)
    y = (df_feat.roc_20d > 0.05).astype(int)  
    
    X_train, X_test, y_train, y_test = train_test_split(X, y, test_size=0.2)
    model = xgb.XGBClassifier(n_estimators=100)
    model.fit(X_train, y_train)
    
    # 4. Vorhersagen + Portfolio-Vorschläge
    df_feat['buy_score'] = model.predict_proba(X)[:,1]
    df_feat['suggested_qty'] = (500 / df_feat.ticker.map(lambda t: yf.Ticker(t).info.get('regularMarketPrice', 10))).clip(1, 50)
    
    top_buys = df_feat.nlargest(5, 'buy_score')
    
    print("🔥 TOP 5 BUY-IDEEN:")
    print(top_buys[['ticker', 'buy_score', 'suggested_qty', 'roc_20d']])
    
    # 5. Portfolio-Risiko
    portfolio_risk = df_hold.Value.std() / df_hold.Value.mean()
    print(f"\n⚠️ Portfolio-Risk: {portfolio_risk:.1%}")
    
    return top_buys, model

# Ausführen
ideas, model = analyze_portfolio("portfolio.xlsx")
