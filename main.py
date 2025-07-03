# main.py - Final and Working Version
# Date: July 2, 2025

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

# --- Initial Configuration ---
load_dotenv()

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

# --- Service Initialization ---
app = FastAPI(title="Zenith Rewards Backend", description="API for managing the Zenith Rewards app.")

vertexai = None
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        with open("gcp_sa_key.json", "w") as f: f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        import vertexai
        from vertexai.generative_models import GenerativeModel
        from vertexai.preview.vision_models import ImageGenerationModel
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI initialized successfully.")
    except Exception as e:
        print(f"WARNING: Vertex AI config error: {e}")
else:
    print("WARNING: Missing GCP environment variables. Vertex AI is disabled.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not configured.")

if all([PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    paypal_mode = os.environ.get("PAYPAL_MODE", "sandbox")
    paypalrestsdk.configure({ "mode": paypal_mode, "client_id": PAYPAL_CLIENT_ID, "client_secret": PAYPAL_CLIENT_SECRET })
else:
    print("WARNING: PayPal credentials not configured.")

app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

# --- Data Models (Pydantic) & Constants ---
POINTS_TO_EUR_RATE = 1000.0
class SubscriptionPlan(str, Enum): FREE = 'free'; PREMIUM = 'premium'; ASSISTANT = 'assistant'
class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class AIGenerationRequest(BaseModel): user_id: str; prompt: str
class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class UserProfileUpdate(BaseModel): display_name: str | None = None; avatar_url: str | None = None
class CreateSubscriptionRequest(BaseModel): user_id: str; plan_type: str; success_url: str; cancel_url: str

# --- Helper Functions ---
def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def generate_viral_plan(prompt: str, user_plan: SubscriptionPlan = SubscriptionPlan.FREE) -> str:
    if not vertexai:
        return "AI service not available. Please try again later."
    try:
        model = GenerativeModel("gemini-1.5-flash")
        ai_prompt = ""
        if user_plan == SubscriptionPlan.ASSISTANT:
            ai_prompt = f"""Given the following creative prompt: '{prompt}', create an extremely detailed and actionable marketing plan in 7-10 points to make it go viral.
            For each point, include:
            - A clear title.
            - An in-depth description of the action.
            - Specific implementation suggestions for different platforms (Instagram Reels, TikTok, YouTube Shorts, X, LinkedIn, Blog, etc.).
            - Examples of calls-to-action, hashtags, relevant audio or visual trends.
            - Community interaction strategies.
            - Key metrics to monitor.
            Use a highly professional and strategic tone. Format for readability with lists and key points.
            """
        elif user_plan == SubscriptionPlan.PREMIUM:
            ai_prompt = f"""Given the following creative prompt: '{prompt}', create a super detailed 5-7 point marketing plan to make a post or content based on this go viral on platforms like Instagram and TikTok.
            For each point, provide:
            - A clear description of the action.
            - Concrete suggestions on how to implement it (e.g., video types, descriptions, schedules).
            - Examples of relevant hashtags or trending sounds.
            Use an energetic and persuasive tone. Include emojis for each point.
            """
        else: # FREE Plan
            ai_prompt = f"Given the following creative prompt: '{prompt}', create a marketing plan in 3 short points to make a post based on this content go viral on social media (like Instagram or TikTok). Be concise and impactful. Use emojis."
        
        return model.generate_content(ai_prompt).text.strip()
    except Exception as e:
        print(f"Error generating viral plan: {e}")
        return "An error occurred while generating the plan. Please try again later."


def get_ai_text_prompt(base_prompt: str, user_plan: SubscriptionPlan = SubscriptionPlan.FREE) -> str:
    if user_plan == SubscriptionPlan.ASSISTANT:
        return f"""Write a very in-depth, persuasive, and comprehensive article or blog/social media post (minimum 300-500 words) based on this concept: '{base_prompt}'.
        Include:
        - A captivating introduction.
        - Detailed key points with explanations.
        - Relevant examples.
        - A conclusion that invites action or reflection.
        - Adapt the tone to a professional marketing context.
        - Use rich and informative language.
        """
    elif user_plan == SubscriptionPlan.PREMIUM:
        return f"""Write a very detailed and engaging social media post (e.g., for Instagram/TikTok), about 150-200 words long, based on the following concept: '{base_prompt}'.
        Include relevant calls-to-action and hashtags. Use a persuasive and engaging style.
        """
    else: # FREE Plan
        return f"Write a short and engaging Instagram post based on this concept: '{base_prompt}'. Be concise and impactful. Maximum length: 100 words."

def reset_daily_generations_if_needed(user_data: dict, supabase_client: Client):
    now = datetime.now(timezone.utc)
    last_reset_str = user_data.get('last_generation_reset_date')
    needs_reset = False
    if last_reset_str:
        last_reset_date = datetime.fromisoformat(last_reset_str).replace(tzinfo=timezone.utc).date()
        if (now.date() - last_reset_date).days >= 1:
            needs_reset = True
    else:
        needs_reset = True
    
    if needs_reset:
        print(f"Resetting daily generations for user {user_data['user_id']}")
        supabase_client.table('users').update({
            'daily_ai_generations_used': 0,
            'last_generation_reset_date': now.isoformat()
        }).eq('user_id', user_data['user_id']).execute()
        user_data['daily_ai_generations_used'] = 0
        user_data['last_generation_reset_date'] = now.isoformat()

# --- API Endpoints ---
@app.get("/")
def read_root(): return {"message": "Zenith Rewards Backend API. All systems operational."}

@app.post("/sync_user")
def sync_user(user_data: UserSyncRequest):
    supabase = get_supabase_client()
    now = datetime.now(timezone.utc)
    try:
        response = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).maybe_single().execute()
        if not response.data:
            new_user_record = {
                'user_id': user_data.user_id, 'email': user_data.email, 'display_name': user_data.displayName,
                'referrer_id': user_data.referrer_id, 'avatar_url': user_data.avatar_url, 'login_streak': 1,
                'last_login_at': now.isoformat(), 'points_balance': 0, 'pending_points_balance': 0,
                'subscription_plan': SubscriptionPlan.FREE.value, 'daily_ai_generations_used': 0,
                'last_generation_reset_date': now.isoformat(), 'stripe_customer_id': None
            }
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
        update_payload = {
            'display_name': profile_data.display_name,
            'avatar_url': profile_data.avatar_url
        }
        update_payload = {k: v for k, v in update_payload.items() if v is not None}
        if not update_payload:
            raise HTTPException(status_code=400, detail="No data provided for update.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profile updated."}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        supabase.rpc('request_payout_function', {
            'p_user_id': payout_data.user_id,
            'p_points_amount': payout_data.points_amount,
            'p_value_in_eur': value_eur,
            'p_method': payout_data.method,
            'p_address': payout_data.address
        }).execute()
        return {"status": "success", "message": "Your payout request has been sent!"}
    except Exception as e:
        if 'Punti insufficienti' in str(e):
            raise HTTPException(status_code=402, detail="Insufficient withdrawable points.")
        raise HTTPException(status_code=500, detail=f"Error processing request: {str(e)}")

@app.get("/users/{user_id}/profile")
def get_user_profile(user_id: str):
    supabase = get_supabase_client()
    try:
        response = supabase.table('users').select('subscription_plan, daily_ai_generations_used').eq('user_id', user_id).maybe_single().execute()
        if not response.data:
            return {"subscription_plan": "free", "daily_ai_generations_used": 0}
        return response.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching user profile: {e}")

@app.post("/ai/generate-advice")
def generate_advice(req: AIGenerationRequest):
    if not vertexai:
        raise HTTPException(status_code=503, detail="AI service not available.")
    supabase = get_supabase_client()
    user_res = supabase.table('users').select('subscription_plan').eq('user_id', req.user_id).single().execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    user_plan = user_res.data.get('subscription_plan', 'free')
    final_prompt = f"Given the goal '{req.prompt}', provide 3 brief and impactful tips."
    if user_plan == 'assistant':
        final_prompt = f"Act as a world-class business and marketing mentor. Given the goal '{req.prompt}', create an extremely detailed and professional step-by-step strategy to achieve it. Include market analysis, content strategies, KPIs, and concrete next steps."
    elif user_plan == 'premium':
        final_prompt = f"Given the goal '{req.prompt}', create a detailed 5-7 point action plan. For each point, provide practical examples and tips."
    try:
        model = GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(final_prompt)
        return {"advice": response.text}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI service error: {e}")

@app.post("/create-checkout-session")
def create_checkout_session(req: CreateSubscriptionRequest):
    if not stripe.api_key:
        raise HTTPException(status_code=500, detail="Stripe not configured.")
    price_map = {'premium': STRIPE_PRICE_ID_PREMIUM, 'assistant': STRIPE_PRICE_ID_ASSISTANT}
    price_id = price_map.get(req.plan_type)
    if not price_id:
        raise HTTPException(status_code=400, detail="Invalid plan type.")
    try:
        supabase = get_supabase_client()
        user_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).single().execute()
        user_data = user_res.data
        customer_id = user_data.get('stripe_customer_id')
        if not customer_id:
            customer = stripe.Customer.create(email=user_data.get('email'), metadata={'user_id': req.user_id})
            customer_id = customer.id
            supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id).execute()
        checkout_session = stripe.checkout.Session.create(customer=customer_id, line_items=[{'price': price_id, 'quantity': 1}], mode='subscription', success_url=req.success_url, cancel_url=req.cancel_url, metadata={'user_id': req.user_id})
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
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).single().execute()
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
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).single().execute()
        if user_res.data:
            supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_res.data['user_id']).execute()
            
    return Response(status_code=200)