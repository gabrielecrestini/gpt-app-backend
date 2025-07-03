# main.py - Final Version with AI Studio
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
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
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
    print("WARNING: Missing GCP environment variables. Vertex AI is disabled.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY

# --- Data Models (Pydantic) & Constants ---
class SubscriptionPlan(str, Enum):
    FREE = 'free'
    PREMIUM = 'premium'
    ASSISTANT = 'assistant'
    
class AIAdviceRequest(BaseModel):
    user_id: str
    prompt: str
# ... (all other Pydantic models)

# --- Helper Functions ---
def get_supabase_client() -> Client:
    return create_client(SUPABASE_URL, SUPABASE_KEY)

def reset_daily_generations_if_needed(user_data: dict, supabase_client: Client):
    now = datetime.now(timezone.utc)
    last_reset_str = user_data.get('last_generation_reset_date')
    needs_reset = not last_reset_str or (now.date() - datetime.fromisoformat(last_reset_str).date()).days >= 1
    
    if needs_reset:
        supabase_client.table('users').update({
            'daily_ai_generations_used': 0,
            'last_generation_reset_date': now.isoformat()
        }).eq('user_id', user_data['user_id']).execute()
        user_data['daily_ai_generations_used'] = 0

# --- API Endpoints ---
@app.post("/ai/generate-advice")
def generate_advice(req: AIAdviceRequest):
    if not vertexai:
        raise HTTPException(status_code=503, detail="AI service is not available.")

    supabase = get_supabase_client()
    
    user_res = supabase.table('users').select("subscription_plan, daily_ai_generations_used, last_generation_reset_date").eq("user_id", req.user_id).maybe_single().execute()
    if not user_res.data:
        raise HTTPException(status_code=404, detail="User not found.")
    user_data = user_res.data
    
    reset_daily_generations_if_needed(user_data, supabase)
    
    user_plan = SubscriptionPlan(user_data.get('subscription_plan', 'free'))
    current_generations = user_data.get('daily_ai_generations_used', 0)
    
    limit_map = { 'free': 12, 'premium': 150 }
    if user_plan != SubscriptionPlan.ASSISTANT and current_generations >= limit_map.get(user_plan.value, 0):
        raise HTTPException(status_code=429, detail=f"You have reached your daily limit of {limit_map[user_plan.value]} generations for the {user_plan.name} plan.")

    final_prompt = ""
    if user_plan == SubscriptionPlan.ASSISTANT:
        final_prompt = f"Act as a world-class business and marketing mentor. Given the goal '{req.prompt}', create an extremely detailed, professional, step-by-step strategy to achieve it. Include market analysis, content strategies, KPIs, and concrete next steps."
    elif user_plan == SubscriptionPlan.PREMIUM:
        final_prompt = f"Given the goal '{req.prompt}', create a detailed 5-7 point action plan. For each point, provide practical examples and tips."
    else: # FREE Plan
        final_prompt = f"Given the goal '{req.prompt}', provide 3 brief and impactful tips."

    try:
        model = GenerativeModel("gemini-1.5-flash")
        response = model.generate_content(final_prompt)
        
        supabase.table('users').update({'daily_ai_generations_used': current_generations + 1}).eq('user_id', req.user_id).execute()
        
        return {"advice": response.text.strip()}
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"AI service error: {e}")

# ... (all other existing endpoints like /sync_user, /request_payout, etc.)