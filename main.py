# main.py - Versione Finale (con Gamification e Payout Reale)
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
    email: str
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class ProfileUpdateRequest(BaseModel):
    display_name: str
    avatar_url: str

class PayoutRequest(BaseModel):
    user_id: str
    amount: float
    paypal_email: str

# --- Dati Fittizi per Gamification (da sostituire con logica DB reale) ---
mock_leaderboard = [
    {"name": "TopPlayer1", "earnings": 150.25, "avatar": "https://i.ibb.co/dKC7dZg/male-avatar.png"},
    {"name": "GuadagnoMax", "earnings": 120.50, "avatar": "https://i.ibb.co/V99DF07/female-avatar.png"},
    {"name": "SuperUtente", "earnings": 95.75, "avatar": "https://i.ibb.co/dKC7dZg/male-avatar.png"},
]
mock_community_goal = {"current": 7500, "target": 10000, "reward": "+5% Guadagni per 24h"}
mock_missions = [
    {"id": 1, "title": "Completa 3 sondaggi", "progress": 1, "target": 3, "reward": 50},
    {"id": 2, "title": "Guadagna 5€ in un giorno", "progress": 2.5, "target": 5, "reward": 100},
]
wheel_prizes = [
    {'label': '10 Monete', 'type': 'coins', 'value': 10, 'weight': 30},
    {'label': 'Jackpot!', 'type': 'coins', 'value': 100, 'weight': 2},
]

# --- Endpoint ---
@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend API. Payout system attivo."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    try:
        existing_user = supabase.table('users').select('user_id').eq('user_id', user_data.user_id).single().execute()
        if not existing_user.data:
            user_record = { 'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url }
            supabase.table('users').insert(user_record).execute()
        return {"status": "success"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        response = supabase.table('users').select('balance').eq('user_id', user_id).execute()
        # FIX: Controlla se ci sono dati e ritorna il primo risultato o 0
        if response.data:
            return {"user_id": user_id, "balance": response.data[0].get('balance', 0)}
        # Se l'utente non esiste nel DB (es. appena registrato), il suo saldo è 0
        return {"user_id": user_id, "balance": 0}
    except Exception as e:
        print(f"Error in get_user_balance: {e}")
        raise HTTPException(status_code=500, detail="Errore nel recupero del saldo.")

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        user_response = supabase.table('users').select('balance').eq('user_id', payout_data.user_id).single().execute()
        
        if not user_response.data or user_response.data.get('balance', 0) < payout_data.amount:
            raise HTTPException(status_code=400, detail="Saldo insufficiente per completare la richiesta.")

        current_balance = user_response.data.get('balance', 0)
        new_balance = current_balance - payout_data.amount
        supabase.table('users').update({'balance': new_balance}).eq('user_id', payout_data.user_id).execute()

        supabase.table('payout_requests').insert({
            'user_id': payout_data.user_id,
            'amount': payout_data.amount,
            'paypal_email': payout_data.paypal_email,
            'status': 'pending'
        }).execute()

        return {"status": "success", "message": "Richiesta di prelievo inviata con successo."}
    
    except HTTPException as http_exc:
        raise http_exc
    except Exception as e:
        print(f"Error in request_payout: {e}")
        raise HTTPException(status_code=500, detail="Errore durante l'elaborazione della richiesta di prelievo.")

# --- Endpoint per Gamification ---
@app.get("/leaderboard")
def get_leaderboard():
    return mock_leaderboard

@app.get("/community_goal")
def get_community_goal():
    return mock_community_goal

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    return {"days": random.randint(0, 15), "canClaim": random.choice([True, False])}

@app.get("/missions/{user_id}")
def get_user_missions(user_id: str):
    return mock_missions

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    try:
        response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
        return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
        
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
<<<<<<< HEAD
        raise HTTPException(status_code=500, detail="Errore durante il giro della ruota.")
=======
        raise HTTPException(status_code=500, detail="Errore durante il giro della ruota.")
>>>>>>> c993d7191fb0201da77fb02576d3fb93e1734a8a
