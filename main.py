# main.py - Versione Finale Definitiva (con Missioni e Gamification Reale)
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
import random
from datetime import datetime, timedelta, timezone
import requests
import yfinance as yf

from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- Configurazione iniziale ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE_URL = "https://api-m.paypal.com"

if not all([SUPABASE_URL, SUPABASE_KEY]):
    raise ValueError("Errore: mancano le variabili d'ambiente di Supabase.")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
app = FastAPI(title="Zenith Rewards Backend")

# --- Configurazione CORS ---
origins = ["http://localhost:3000", "https://cashhh-52f38.web.app"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Costanti e Modelli ---
POINTS_TO_EUR_RATE = 1000.0

class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None

class PayoutRequest(BaseModel):
    user_id: str; points_amount: int; method: str; address: str

# --- Dati Fittizi per Missioni e Obiettivi ---
possible_missions = [
    {"id": 1, "title": "Completa 3 sondaggi", "target": 3, "reward": 50},
    {"id": 2, "title": "Guadagna 500 ZC in un giorno", "target": 500, "reward": 100},
    {"id": 3, "title": "Invita un amico", "target": 1, "reward": 200},
    {"id": 4, "title": "Guarda 10 video", "target": 10, "reward": 20},
]
mock_community_goal = {"target": 100000, "reward": "+5% Guadagni per 24h"}


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
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).execute()
        if response.data:
            return {"points_balance": response.data[0].get('points_balance', 0)}
        return {"points_balance": 0}
    except Exception as e:
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
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'elaborazione della richiesta.")

# --- Endpoint Gamification con Logica Reale ---
@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(5).execute()
        leaderboard_data = [{"name": u.get('display_name', 'Utente Anonimo'), "earnings": u.get('points_balance', 0)/POINTS_TO_EUR_RATE, "avatar": u.get('avatar_url', '')} for u in response.data]
        return leaderboard_data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricare la classifica.")

@app.get("/community_goal")
def get_community_goal():
    try:
        response = supabase.table('users').select('points_balance').execute()
        total_balance = sum(u.get('points_balance', 0) for u in response.data)
        return {"current": total_balance, "target": mock_community_goal['target'], "reward": mock_community_goal['reward']}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricare l'obiettivo community.")

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        response = supabase.table('users').select('login_streak').eq('user_id', user_id).single().execute()
        if not response.data:
            return {"days": 0, "canClaim": False}
        return {"days": response.data.get('login_streak', 0), "canClaim": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel recuperare lo streak.")

@app.get("/missions/{user_id}")
def get_user_missions(user_id: str):
    missions = random.sample(possible_missions, 3)
    for mission in missions:
        mission['progress'] = round(random.uniform(0, mission['target']), 1)
    return missions