# main.py - Versione 12 (con Dati di Mercato Reali)
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
import random
from datetime import datetime, timedelta, timezone
import yfinance as yf # Libreria per i dati di mercato reali

from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- Configurazione iniziale ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
if not SUPABASE_URL or not SUPABASE_KEY:
    raise ValueError("Errore: devi impostare SUPABASE_URL e SUPABASE_KEY")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Zenith Rewards Backend")

# --- Configurazione CORS ---
origins = [ "http://localhost:3000", "https://cashhh-52f38.web.app" ]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelli Dati ---
class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None

class ProfileUpdateRequest(BaseModel):
    display_name: str
    avatar_url: str

class PayoutRequest(BaseModel):
    user_id: str; amount: float; method: str; address: str

class TradingBetRequest(BaseModel):
    user_id: str; amount: float; direction: str; asset: str

# --- Dati Fittizi (usati solo come fallback o per missioni) ---
mock_community_goal = {"target": 10000, "reward": "+5% Guadagni per 24h"}
possible_missions = [
    {"id": 1, "title": "Completa 3 sondaggi", "target": 3, "reward": 50},
    {"id": 2, "title": "Guadagna 5€ in un giorno", "target": 5, "reward": 100},
    {"id": 3, "title": "Invita un amico", "target": 1, "reward": 200},
]
wheel_prizes = [
    {'label': '10 Monete', 'type': 'coins', 'value': 10, 'weight': 30},
    {'label': 'Jackpot!', 'type': 'coins', 'value': 100, 'weight': 2},
]


# --- Endpoint ---
@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Dati di mercato reali attivi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        user_res = supabase.table('users').select('user_id').eq('user_id', user_data.user_id).execute()
        if not user_res.data:
            user_record = { 
                'user_id': user_data.user_id, 'email': user_data.email, 
                'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 
                'avatar_url': user_data.avatar_url
            }
            supabase.table('users').insert(user_record).execute()
        return {"status": "success"}
    except Exception as e:
        print(f"Errore in sync_user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_profile/{user_id}")
def update_profile_endpoint(user_id: str, profile_data: ProfileUpdateRequest):
    try:
        data, count = supabase.table('users').update({
            'display_name': profile_data.display_name,
            'avatar_url': profile_data.avatar_url
        }).eq('user_id', user_id).execute()
        if not data or (isinstance(data, list) and len(data) > 1 and not data[1]):
            raise HTTPException(status_code=404, detail="User not found")
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('balance').eq('user_id', user_id).execute()
        if response.data:
            return {"user_id": user_id, "balance": response.data[0].get('balance', 0)}
        return {"user_id": user_id, "balance": 0}
    except Exception as e:
        print(f"Error in get_user_balance: {e}")
        raise HTTPException(status_code=500, detail="Errore nel recupero del saldo.")

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        user_response = supabase.table('users').select('balance').eq('user_id', payout_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('balance', 0) < payout_data.amount:
            raise HTTPException(status_code=400, detail="Saldo insufficiente.")
        
        new_balance = user_response.data.get('balance', 0) - payout_data.amount
        supabase.table('users').update({'balance': new_balance}).eq('user_id', payout_data.user_id).execute()
        
        supabase.table('payout_requests').insert({
            'user_id': payout_data.user_id, 'amount': payout_data.amount,
            'payout_method': payout_data.method, 'wallet_address': payout_data.address,
            'status': 'pending'
        }).execute()
        return {"status": "success", "message": "Richiesta di prelievo inviata."}
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'elaborazione della richiesta.")

# --- Sistema di Trading con Dati Reali ---
supported_assets = ["BTC-USD", "ETH-USD", "DOGE-USD"]

@app.get("/trading/price/{asset_ticker}")
def get_asset_price(asset_ticker: str):
    """Recupera il prezzo quasi in tempo reale di un asset."""
    if asset_ticker not in supported_assets:
        raise HTTPException(status_code=404, detail="Asset non supportato.")
    try:
        ticker = yf.Ticker(asset_ticker)
        price_data = ticker.history(period="1d", interval="1m")
        if price_data.empty:
            raise HTTPException(status_code=404, detail="Dati non disponibili per questo asset.")
        current_price = price_data['Close'][-1]
        return {"asset": asset_ticker, "price": round(current_price, 4)}
    except Exception as e:
        print(f"Errore API yfinance: {e}")
        raise HTTPException(status_code=500, detail="Impossibile recuperare i dati di mercato.")

@app.post("/trading/place_bet")
def place_bet(bet_data: TradingBetRequest):
    try:
        user_response = supabase.table('users').select('balance').eq('user_id', bet_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('balance', 0) < bet_data.amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti.")
        
        new_balance = user_response.data.get('balance', 0) - bet_data.amount
        supabase.table('users').update({'balance': new_balance}).eq('user_id', bet_data.user_id).execute()
        
        asset_price_data = get_asset_price(f"{bet_data.asset}-USD")
        initial_price = asset_price_data['price']

        supabase.table('trading_bets').insert({
            'user_id': bet_data.user_id, 'direction': bet_data.direction,
            'amount_bet': bet_data.amount, 'asset': bet_data.asset,
            'status': 'active', 'initial_price': initial_price
        }).execute()
        
        return {"status": "success", "message": "Scommessa piazzata."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel piazzare la scommessa.")

# --- Endpoint di Gamification ---
@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, balance, avatar_url').order('balance', desc=True).limit(5).execute()
        leaderboard_data = [{"name": u.get('display_name', 'Utente Anonimo'), "earnings": u.get('balance', 0), "avatar": u.get('avatar_url', '')} for u in response.data]
        return leaderboard_data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricare la classifica.")

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    try:
        response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
        return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))