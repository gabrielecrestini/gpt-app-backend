# main.py - Versione Finale Definitiva
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
import random
from datetime import datetime, timedelta, timezone

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
origins = [
    "http://localhost:3000",
    "https://cashhh-52f38.web.app", # Il tuo URL di produzione
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Modelli Dati ---
class UserSyncRequest(BaseModel):
    user_id: str
    email: str | None
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class ProfileUpdateRequest(BaseModel):
    display_name: str
    avatar_url: str

class PayoutRequest(BaseModel):
    user_id: str
    amount: float
    method: str 
    address: str

class TradingBetRequest(BaseModel):
    user_id: str
    amount: float
    direction: str

# --- Dati di base per Gamification ---
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
current_btc_price = 65000.00

# --- Endpoint ---
@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono attivi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        user_res = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).execute()
        now = datetime.now(timezone.utc)
        
        if not user_res.data: # Nuovo utente
            user_record = { 
                'user_id': user_data.user_id, 'email': user_data.email, 
                'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 
                'avatar_url': user_data.avatar_url, 'last_login_at': now.isoformat(), 'login_streak': 1
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

# --- Endpoint Gamification con Logica Reale ---

@app.get("/leaderboard")
def get_leaderboard():
    try:
        response = supabase.table('users').select('display_name, balance, avatar_url').order('balance', desc=True).limit(5).execute()
        leaderboard_data = [{"name": u.get('display_name', 'Utente Anonimo'), "earnings": u.get('balance', 0), "avatar": u.get('avatar_url', '')} for u in response.data]
        return leaderboard_data
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel caricare la classifica.")

@app.get("/community_goal")
def get_community_goal():
    try:
        response = supabase.table('users').select('balance').execute()
        total_balance = sum(u.get('balance', 0) for u in response.data)
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

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    try:
        response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
        return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

# --- Endpoint Trading e Ruota ---

@app.get("/wheel/status/{user_id}")
def get_wheel_status(user_id: str):
    try:
        response = supabase.table('users').select('last_spin_at').eq('user_id', user_id).single().execute()
        if not response.data or not response.data.get('last_spin_at'):
            return {"can_spin": True}
        last_spin_time = datetime.fromisoformat(response.data['last_spin_at'])
        if (datetime.now(timezone.utc) - last_spin_time) > timedelta(hours=24):
            return {"can_spin": True}
        else:
            return {"can_spin": False}
    except Exception:
        return {"can_spin": True}

@app.post("/wheel/spin/{user_id}")
def spin_wheel(user_id: str):
    if not get_wheel_status(user_id).get("can_spin"):
        raise HTTPException(status_code=403, detail="Non puoi ancora girare la ruota.")
    try:
        population = [p for p in wheel_prizes for _ in range(p['weight'])]
        prize = random.choice(population)
        prize_index = next((i for i, p in enumerate(wheel_prizes) if p['label'] == prize['label']), 0)
        if prize['type'] == 'coins':
            user_data = supabase.table('users').select('balance').eq('user_id', user_id).single().execute()
            new_balance = user_data.data.get('balance', 0) + prize['value']
            supabase.table('users').update({'balance': new_balance}).eq('user_id', user_id).execute()
        supabase.table('users').update({'last_spin_at': datetime.now(timezone.utc).isoformat()}).eq('user_id', user_id).execute()
        return {"prize": prize, "prize_index": prize_index}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore durante il giro della ruota.")

@app.get("/trading/btc_price")
def get_btc_price():
    global current_btc_price
    change = current_btc_price * random.uniform(-0.001, 0.001)
    current_btc_price += change
    return {"price": round(current_btc_price, 2)}

@app.post("/trading/place_bet")
def place_bet(bet_data: TradingBetRequest):
    try:
        user_response = supabase.table('users').select('balance').eq('user_id', bet_data.user_id).single().execute()
        if not user_response.data or user_response.data.get('balance', 0) < bet_data.amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti per la scommessa.")
        new_balance = user_response.data.get('balance', 0) - bet_data.amount
        supabase.table('users').update({'balance': new_balance}).eq('user_id', bet_data.user_id).execute()
        supabase.table('trading_bets').insert({'user_id': bet_data.user_id, 'direction': bet_data.direction, 'amount_bet': bet_data.amount, 'status': 'active', 'initial_price': current_btc_price}).execute()
        return {"status": "success", "message": "Scommessa piazzata."}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nel piazzare la scommessa.")
