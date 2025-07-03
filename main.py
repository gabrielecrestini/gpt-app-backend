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

# --- Initial Configuration ---
load_dotenv()

# Securely load keys from environment variables
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")

# --- Service Initialization ---
app = FastAPI(title="Zenith Rewards Backend")

vertexai = None
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        import vertexai
        from vertexai.generative_models import GenerativeModel
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI initialized successfully.")
    except Exception as e:
        print(f"WARNING: Vertex AI config error: {e}")
else:
    print("WARNING: Missing GCP credentials. Vertex AI is disabled.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not configured.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data Models (Pydantic) & Constants ---
POINTS_TO_EUR_RATE = 1000.0
class SubscriptionPlan(str, Enum): FREE = 'free'; PREMIUM = 'premium'; ASSISTANT = 'assistant'
class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class AIAdviceRequest(BaseModel): user_id: str; prompt: str
class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class UserProfileUpdate(BaseModel): display_name: str | None = None; avatar_url: str | None = None
class CreateSubscriptionRequest(BaseModel): user_id: str; plan_type: str; success_url: str; cancel_url: str

# --- Helper Functions ---
def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

# --- API Endpoints ---
@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend is operational."}

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
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during user sync: {str(e)}")

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    try:
        supabase = get_supabase_client()
        update_payload = { 'display_name': profile_data.display_name, 'avatar_url': profile_data.avatar_url }
        update_payload = {k: v for k, v in update_payload.items() if v is not None}
        if not update_payload: raise HTTPException(status_code=400, detail="No data provided to update.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profile updated successfully."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.rpc('request_payout_function', { 'p_user_id': payout_data.user_id, 'p_points_amount': payout_data.points_amount, 'p_value_in_eur': value_eur, 'p_method': payout_data.method, 'p_address': payout_data.address }).execute()
        return {"status": "success", "message": "Your payout request has been sent and will be processed soon!"}
    except Exception as e:
        if 'Punti insufficienti' in str(e): raise HTTPException(status_code=402, detail="Insufficient withdrawable points.")
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")

@app.get("/users/{user_id}/profile")
def get_user_profile(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('subscription_plan, daily_ai_generations_used').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"subscription_plan": "free", "daily_ai_generations_used": 0}
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching user profile: {e}")

# ### NUOVE FUNZIONI AGGIUNTE QUI ###
@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('points_balance, pending_points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response.data:
            raise HTTPException(status_code=404, detail="User not found")
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching user balance: {e}")

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('login_streak').eq('user_id', user_id).maybe_single().execute()
        if not response.data:
            return {"login_streak": 0}
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching streak status: {e}")

@app.get("/leaderboard")
def get_leaderboard():
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('display_name, avatar_url, points_balance').order('points_balance', desc=True).limit(100).execute()
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching leaderboard: {e}")
# ### FINE DELLE NUOVE FUNZIONI ###

@app.post("/ai/generate-advice")
def generate_advice(req: AIAdviceRequest):
    if not vertexai: raise HTTPException(status_code=503, detail="AI service is not available.")
    supabase = get_supabase_client()
    user_res = supabase.table('users').select('subscription_plan').eq('user_id', req.user_id).maybe_single().execute()
    if not user_res.data: raise HTTPException(status_code=404, detail="User not found.")
    
    user_plan = user_res.data.get('subscription_plan', 'free')
    final_prompt = f"Given the goal '{req.prompt}', provide 3 brief, impactful tips."
    if user_plan == 'assistant':
        final_prompt = f"Act as a world-class business mentor. Given the goal '{req.prompt}', create a detailed step-by-step strategy."
    elif user_plan == 'premium':
        final_prompt = f"Given the goal '{req.prompt}', create a 5-7 point action plan with practical examples."
    
    try:
        model = GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(final_prompt)
        return {"advice": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI service error: {e}")

@app.post("/create-checkout-session")
def create_checkout_session(req: CreateSubscriptionRequest):
    if not stripe.api_key: raise HTTPException(status_code=500, detail="Stripe not configured.")
    price_map = {'premium': STRIPE_PRICE_ID_PREMIUM, 'assistant': STRIPE_PRICE_ID_ASSISTANT}
    price_id = price_map.get(req.plan_type)
    if not price_id: raise HTTPException(status_code=400, detail="Invalid plan type.")
    
    try:
        supabase = get_supabase_client()
        user_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).maybe_single().execute()
        if not user_res.data: raise HTTPException(status_code=404, detail="User not found.")
        
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
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    supabase = get_supabase_client()
    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

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