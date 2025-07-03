# main.py - Versione Definitiva e Completa
# Data: 3 Luglio 2025

import os
import time
from datetime import datetime, timezone, timedelta
import base64
from enum import Enum
import json

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import paypalrestsdk

# --- Configurazione Iniziale ---
load_dotenv()

# Caricamento sicuro delle chiavi dalle variabili d'ambiente
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")

# --- Inizializzazione dei Servizi ---
app = FastAPI(title="Zenith Rewards Backend", description="API per la gestione dell'app Zenith Rewards.")

vertexai = None
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI inizializzato correttamente.")
    except Exception as e:
        print(f"ATTENZIONE: Errore config Vertex AI: {e}")
else:
    print("ATTENZIONE: Credenziali GCP mancanti. Vertex AI è disabilitato.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("AVVISO: STRIPE_SECRET_KEY non configurato.")

app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Modelli Dati (Pydantic) e Costanti ---
POINTS_TO_EUR_RATE = 1000.0
class SubscriptionPlan(str, Enum): FREE = 'free'; PREMIUM = 'premium'; ASSISTANT = 'assistant'
class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class AIAdviceRequest(BaseModel): user_id: str; prompt: str
class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class UserProfileUpdate(BaseModel): display_name: str | None = None; avatar_url: str | None = None
class CreateSubscriptionRequest(BaseModel): user_id: str; plan_type: str; success_url: str; cancel_url: str

# --- Funzioni Helper ---
def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# --- Endpoint dell'API ---

@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend API. Tutti i sistemi sono operativi."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    supabase = get_supabase_client()
    now = datetime.now(timezone.utc)
    try:
        response = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).maybe_single().execute()
        if not response.data:
            new_user_record = { 'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName, 'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1, 'last_login_at': now.isoformat(), 'points_balance': 0, 'pending_points_balance': 0, 'subscription_plan': SubscriptionPlan.FREE.value, 'daily_ai_generations_used': 0, 'last_generation_reset_date': now.isoformat() }
            supabase.table('users').insert(new_user_record).execute()
        else:
            user = response.data
            last_login_str, new_streak = user.get('last_login_at'), user.get('login_streak', 1)
            if last_login_str:
                days_diff = (now.date() - datetime.fromisoformat(last_login_str).date()).days
                if days_diff == 1: new_streak += 1
                elif days_diff > 1: new_streak = 1
            supabase.table('users').update({'last_login_at': now.isoformat(), 'login_streak': new_streak}).eq('user_id', user_data.user_id).execute()
        return {"status": "success"}
    except Exception as e: raise HTTPException(status_code=500, detail=f"Errore durante la sincronizzazione utente: {str(e)}")

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    try:
        supabase = get_supabase_client()
        update_payload = {'display_name': profile_data.display_name, 'avatar_url': profile_data.avatar_url}
        update_payload = {k: v for k, v in update_payload.items() if v is not None}
        if not update_payload: raise HTTPException(status_code=400, detail="Nessun dato da aggiornare.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profilo aggiornato."}
    except Exception as e: raise HTTPException(status_code=500, detail=f"Errore durante l'aggiornamento del profilo: {str(e)}")

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.rpc('request_payout_function', { 'p_user_id': payout_data.user_id, 'p_points_amount': payout_data.points_amount, 'p_value_in_eur': value_eur, 'p_method': payout_data.method, 'p_address': payout_data.address }).execute()
        return {"status": "success", "message": "La tua richiesta di prelievo è stata inviata e sarà processata presto!"}
    except Exception as e:
        if 'Punti insufficienti' in str(e): raise HTTPException(status_code=402, detail="Punti prelevabili insufficienti.")
        raise HTTPException(status_code=500, detail=f"Errore durante l'elaborazione della richiesta: {str(e)}")

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('points_balance, pending_points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"points_balance": 0, "pending_points_balance": 0}
        return {"points_balance": response.data.get('points_balance', 0), "pending_points_balance": response.data.get('pending_points_balance', 0)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/users/{user_id}/profile")
def get_user_profile(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('subscription_plan, daily_ai_generations_used').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"subscription_plan": "free", "daily_ai_generations_used": 0}
        return response.data
    except Exception as e: raise HTTPException(status_code=500, detail=f"Errore recupero profilo: {e}")

@app.post("/ai/generate-advice")
def generate_advice(req: AIAdviceRequest):
    if not vertexai: raise HTTPException(status_code=503, detail="Servizio AI non disponibile.")
    
    supabase = get_supabase_client()
    user_res = supabase.table('users').select('subscription_plan').eq('user_id', req.user_id).maybe_single().execute()
    if not user_res.data: raise HTTPException(status_code=404, detail="Utente non trovato.")
    
    user_plan = user_res.data.get('subscription_plan', 'free')
    final_prompt = ""
    if user_plan == 'assistant':
        final_prompt = f"Agisci come un mentore di business di livello mondiale. Dato l'obiettivo '{req.prompt}', crea una strategia passo passo estremamente dettagliata."
    elif user_plan == 'premium':
        final_prompt = f"Dato l'obiettivo '{req.prompt}', crea un piano d'azione dettagliato in 5-7 punti."
    else:
        final_prompt = f"Dato l'obiettivo '{req.prompt}', fornisci 3 consigli brevi e d'impatto."

    try:
        model = GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(final_prompt)
        return {"advice": response.text.strip()}
    except Exception as e: raise HTTPException(status_code=503, detail=f"Errore servizio AI: {e}")

@app.post("/create-checkout-session")
def create_checkout_session(req: CreateSubscriptionRequest):
    if not stripe.api_key: raise HTTPException(status_code=500, detail="Stripe non configurato.")
    
    price_map = {'premium': STRIPE_PRICE_ID_PREMIUM, 'assistant': STRIPE_PRICE_ID_ASSISTANT}
    price_id = price_map.get(req.plan_type)
    if not price_id: raise HTTPException(status_code=400, detail="Tipo di piano non valido.")

    try:
        supabase = get_supabase_client()
        user_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).maybe_single().execute()
        if not user_res.data: raise HTTPException(status_code=404, detail="Utente non trovato.")
        
        user_data = user_res.data
        customer_id = user_data.get('stripe_customer_id')
        if not customer_id:
            customer = stripe.Customer.create(email=user_data.get('email'), metadata={'user_id': req.user_id})
            customer_id = customer.id
            supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id).execute()

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id, line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription', success_url=req.success_url, cancel_url=req.cancel_url
        )
        return {"url": checkout_session.url}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    supabase = get_supabase_client()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e: raise HTTPException(status_code=400, detail=f"Errore Webhook: {e}")

    if event['type'] in ['customer.subscription.created', 'customer.subscription.updated']:
        subscription = event['data']['object']
        customer_id = subscription.get('customer')
        price_id = subscription['items']['data'][0]['price']['id']
        status = subscription.get('status')
        
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()
        if user_res.data:
            user_id = user_res.data['user_id']
            new_plan = SubscriptionPlan.FREE
            if price_id == STRIPE_PRICE_ID_PREMIUM: new_plan = SubscriptionPlan.PREMIUM
            elif price_id == STRIPE_PRICE_ID_ASSISTANT: new_plan = SubscriptionPlan.ASSISTANT
            
            if status in ['active', 'trialing']:
                supabase.table('users').update({'subscription_plan': new_plan.value}).eq('user_id', user_id).execute()
            else:
                supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_id).execute()

    elif event['type'] == 'customer.subscription.deleted':
        customer_id = event['data']['object'].get('customer')
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()
        if user_res.data:
            supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_res.data['user_id']).execute()
            
    return Response(status_code=200)