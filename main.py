# main.py - Versione Finale Definitiva
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

# --- Costanti di Gioco ---
POINTS_TO_EUR_RATE = 1000.0

# --- Modelli Dati ---
class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None

class ProfileUpdateRequest(BaseModel):
    display_name: str
    avatar_url: str

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str 
    address: str

class TradingBetRequest(BaseModel):
    user_id: str
    amount: float
    direction: str
    asset: str

# --- Endpoint di Base ---
@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

# --- Gestione Utenti ---
@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        user_res = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).execute()
        now = datetime.now(timezone.utc)
        
        if not user_res.data: # Nuovo utente
            user_record = { 
                'user_id': user_data.user_id, 'email': user_data.email, 
                'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 
                'avatar_url': user_data.avatar_url, 'login_streak': 1,
                'last_login_at': now.isoformat(), 'points_balance': 0
            }
            supabase.table('users').insert(user_record).execute()
        else: # Utente esistente, aggiorna streak
            user = user_res.data[0]
            last_login = datetime.fromisoformat(user.get('last_login_at')) if user.get('last_login_at') else now - timedelta(days=2)
            streak = user.get('login_streak', 0)
            if (now.date() - last_login.date()).days == 1:
                streak += 1
            elif (now.date() - last_login.date()).days > 1:
                streak = 1
            
            supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': streak}).eq('user_id', user_data.user_id).execute()
            
        return {"status": "success"}
    except Exception as e:
        print(f"Errore in sync_user: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).execute()
        if response.data:
            return {"user_id": user_id, "points_balance": response.data[0].get('points_balance', 0)}
        return {"user_id": user_id, "points_balance": 0}
    except Exception as e:
        print(f"Error in get_user_balance: {e}")
        raise HTTPException(status_code=500, detail="Errore nel recupero del saldo.")

# --- Sistema di Prelievi Reale ---
@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', payout_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('points_balance', 0) < payout_data.points_amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti.")
        
        new_balance = user_response.data.get('points_balance', 0) - payout_data.points_amount
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', payout_data.user_id).execute()
        
        value_in_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.table('payout_requests').insert({
            'user_id': payout_data.user_id, 'points_amount': payout_data.points_amount,
            'value_in_eur': value_in_eur, 'payout_method': payout_data.method, 
            'wallet_address': payout_data.address, 'status': 'pending'
        }).execute()
        return {"status": "success", "message": "Richiesta di prelievo inviata."}
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'elaborazione della richiesta.")

# --- Sistema di Trading Reale ---
supported_assets = ["BTC-USD", "ETH-USD", "DOGE-USD"]

@app.get("/trading/price/{asset_ticker}")
def get_asset_price(asset_ticker: str):
    if asset_ticker not in supported_assets:
        raise HTTPException(status_code=404, detail="Asset non supportato.")
    try:
        ticker = yf.Ticker(asset_ticker)
        price_data = ticker.history(period="1d", interval="1m")
        if price_data.empty:
            # Fallback a dati casuali se l'API fallisce
            if asset_ticker == "BTC-USD": return {"price": random.uniform(60000, 65000)}
            if asset_ticker == "ETH-USD": return {"price": random.uniform(3000, 3500)}
            if asset_ticker == "DOGE-USD": return {"price": random.uniform(0.10, 0.15)}
        current_price = price_data['Close'][-1]
        return {"asset": asset_ticker, "price": round(current_price, 4)}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Impossibile recuperare i dati di mercato.")

@app.post("/trading/place_bet")
def place_bet(bet_data: TradingBetRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', bet_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('points_balance', 0) < bet_data.amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti.")
        
        new_balance = user_response.data.get('points_balance', 0) - bet_data.amount
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', bet_data.user_id).execute()
        
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

# --- Endpoint Gamification con Logica Reale ---
@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(5).execute()
        leaderboard_data = [{"name": u.get('display_name', 'Utente Anonimo'), "earnings": u.get('points_balance', 0)/1000, "avatar": u.get('avatar_url', '')} for u in response.data]
        return leaderboard_data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricare la classifica.")

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        response = supabase.table('users').select('login_streak').eq('user_id', user_id).single().execute()
        if not response.data:
            return {"days": 0, "canClaim": False}
        return {"days": response.data.get('login_streak', 0), "canClaim": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel recuperare lo streak.")

# --- Endpoint Postback ---
@app.get("/postback/{provider}")
async def postback_handler(provider: str, request: Request):
    params = request.query_params
    user_id = params.get("user_id") or params.get("uid")
    amount_str = params.get("amount") or params.get("payout")
    
    if not user_id or not amount_str:
        raise HTTPException(status_code=400, detail="Parametri 'user_id' e 'amount' mancanti")
    try:
        # L'importo dal postback è in EURO, lo convertiamo in Zenith Coins
        amount_eur = float(amount_str)
        points_earned = int(amount_eur * POINTS_TO_EUR_RATE)

        user_data = supabase.table('users').select('points_balance').eq('user_id', user_id).single().execute()
        if not user_data.data:
            raise HTTPException(status_code=404, detail=f"Utente {user_id} non trovato.")
        
        new_balance = user_data.data.get('points_balance', 0) + points_earned
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', user_id).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore interno del server.")

