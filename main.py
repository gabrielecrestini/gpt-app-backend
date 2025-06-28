# main.py - Versione 13 (con PayPal Payouts API)
from fastapi import FastAPI, Request, HTTPException
from pydantic import BaseModel
from supabase import create_client, Client
import os
import random
from datetime import datetime, timedelta, timezone
import yfinance as yf
import requests # Libreria per le richieste API a PayPal

from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware

# --- Configurazione iniziale ---
load_dotenv()
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
# Usa "https://api-m.sandbox.paypal.com" per i test, "https://api-m.paypal.com" per la produzione
PAYPAL_API_BASE_URL = "https://api-m.paypal.com" 

if not all([SUPABASE_URL, SUPABASE_KEY, PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    raise ValueError("Errore: mancano le variabili d'ambiente necessarie (Supabase o PayPal).")

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

# --- Costanti e Modelli ---
POINTS_TO_EUR_RATE = 1000.0

class UserSyncRequest(BaseModel):
    user_id: str; email: str | None; displayName: str | None = None
    referrer_id: str | None = None; avatar_url: str | None = None

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str
    address: str

# --- Funzione per Pagamenti PayPal ---
def process_paypal_payout(payout_id: int, user_email: str, value_eur: float):
    """
    Gestisce un singolo pagamento tramite l'API Payouts di PayPal.
    """
    try:
        # 1. Ottieni il token di accesso da PayPal
        auth_response = requests.post(
            f"{PAYPAL_API_BASE_URL}/v1/oauth2/token",
            auth=(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET),
            headers={"Accept": "application/json", "Accept-Language": "en_US"},
            data={"grant_type": "client_credentials"},
        )
        auth_response.raise_for_status()
        access_token = auth_response.json()["access_token"]

        # 2. Prepara e invia la richiesta di pagamento
        payout_data = {
            "sender_batch_header": {
                "sender_batch_id": f"Zenith_{payout_id}_{int(datetime.now().timestamp())}",
                "email_subject": "Hai ricevuto un pagamento da Zenith Rewards!",
                "email_message": f"Grazie per aver usato la nostra piattaforma! Ecco il tuo premio di {value_eur:.2f} EUR."
            },
            "items": [{"recipient_type": "EMAIL", "amount": {"value": f"{value_eur:.2f}", "currency": "EUR"}, "receiver": user_email}]
        }
        
        payout_response = requests.post(
            f"{PAYPAL_API_BASE_URL}/v1/payments/payouts",
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {access_token}"},
            json=payout_data
        )
        payout_response.raise_for_status()
        
        # 3. Aggiorna lo stato nel nostro database a "completed"
        supabase.table('payout_requests').update({'status': 'completed'}).eq('id', payout_id).execute()
        return True, payout_response.json()

    except requests.exceptions.RequestException as e:
        print(f"Errore API PayPal: {e.response.text if e.response else e}")
        supabase.table('payout_requests').update({'status': 'failed'}).eq('id', payout_id).execute()
        return False, str(e)
    except Exception as e:
        print(f"Errore generico in process_paypal_payout: {e}")
        supabase.table('payout_requests').update({'status': 'failed'}).eq('id', payout_id).execute()
        return False, str(e)

# --- Endpoint Principali ---
@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        user_response = supabase.table('users').select('points_balance').eq('user_id', payout_data.user_id).single().execute()
        
        if not user_response.data or user_response.data.get('points_balance', 0) < payout_data.points_amount:
            raise HTTPException(status_code=400, detail="Punti insufficienti.")
        
        new_balance = user_response.data.get('points_balance', 0) - payout_data.points_amount
        supabase.table('users').update({'points_balance': new_balance}).eq('user_id', payout_data.user_id).execute()
        
        value_in_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        
        insert_res = supabase.table('payout_requests').insert({
            'user_id': payout_data.user_id, 'points_amount': payout_data.points_amount,
            'value_in_eur': value_in_eur, 'payout_method': payout_data.method, 
            'wallet_address': payout_data.address, 'status': 'processing'
        }).execute()

        if payout_data.method == 'paypal':
            payout_id = insert_res.data[0]['id']
            success, result = process_paypal_payout(payout_id, payout_data.address, value_in_eur)
            if not success:
                print(f"Pagamento automatico PayPal fallito per payout ID {payout_id}: {result}")
        
        return {"status": "success", "message": "Richiesta di prelievo inviata."}
    
    except Exception as e:
        raise HTTPException(status_code=500, detail="Errore nell'elaborazione della richiesta.")
