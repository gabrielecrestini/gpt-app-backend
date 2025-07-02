# main.py - Production-Ready Version with New AI Studio Plans and Subscription Logic
# Data: July 3, 2025

# --- Library Imports ---
import os
import time
from datetime import datetime, timezone, timedelta
import base64

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import paypalrestsdk

# --- Initial Configuration ---
load_dotenv()

# Load keys from environment variables (secure method for Render)
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.environ.get("PAYPAL_CLIENT_SECRET")
PAYPAL_WEBHOOK_ID = os.environ.get("PAYPAL_WEBHOOK_ID") # Might not be needed for new plans if only using Stripe Subscriptions
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")

# Stripe Price IDs for plans (YOU MUST CREATE THESE IN YOUR STRIPE DASHBOARD!)
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM") # Replace with your real ID
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT") # Replace with your real ID

# Ensure Price IDs are set
if not STRIPE_PRICE_ID_PREMIUM or not STRIPE_PRICE_ID_ASSISTANT:
    print("WARNING: Stripe Price IDs for Premium or Assistant plans are not configured. Subscription features might not work.")


# --- Service Initialization ---
app = FastAPI(title="Zenith Rewards Backend", description="API for managing the Zenith Rewards app.")

# Configure Vertex AI
if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        # Write the service account key to a file for Vertex AI to pick up
        with open("gcp_sa_key.json", "w") as f:
            f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = "gcp_sa_key.json"
        
        import vertexai
        from vertexai.generative_models import GenerativeModel
        from vertexai.preview.vision_models import ImageGenerationModel # Note the deprecation warning here
        
        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        print("Vertex AI initialized correctly.")
    except Exception as e:
        print(f"ATTENTION: Error in Vertex AI configuration: {e}")
        # Consider not starting the app or disabling AI functionalities if Vertex fails
        vertexai = None # Disable Vertex AI if it cannot be initialized
else:
    print("ATTENTION: Missing GCP environment variables. Vertex AI will not be initialized.")
    vertexai = None # Ensure vertexai is None if env vars are missing

# Configure Stripe and PayPal
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not configured. Stripe functionalities will not work.")

if all([PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET]):
    paypalrestsdk.configure({ "mode": "live", "client_id": PAYPAL_CLIENT_ID, "client_secret": PAYPAL_CLIENT_SECRET })
else:
    print("WARNING: PayPal credentials not configured. PayPal functionalities will not work.")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:3000", "https://cashhh-52f38.web.app", "https://cashhh-52738.web.app"],
    allow_credentials=True, allow_methods=["*"], allow_headers=["*"],
)

# --- Data Models (Pydantic) ---
POINTS_TO_EUR_RATE = 1000.0
# The following costs are for AI generations within Art Battle (old logic)
IMAGE_GENERATION_EUR_PRICE = 1.00 # Cost for single image/post generation outside of subscription
IMAGE_GENERATION_POINTS_COST = 1000
IMAGE_GENERATION_POINTS_COST_DISCOUNTED = 50

# Limits for AI Studio
MAX_FREE_GENERATIONS_PER_DAY = 12
MAX_PREMIUM_GENERATIONS_PER_DAY = 150
FREE_PROMPT_MAX_CHARS = 200 # Prompt character limit for Free plan (to be enforced primarily on frontend!)

# Enum for subscription plans
from enum import Enum
class SubscriptionPlan(str, Enum):
    FREE = 'free'
    PREMIUM = 'premium'
    ASSISTANT = 'assistant'


class UserSyncRequest(BaseModel): user_id: str; email: str | None = None; displayName: str | None = None; referrer_id: str | None = None; avatar_url: str | None = None
class AIGenerationRequest(BaseModel):
    user_id: str
    prompt: str
    content_type: str
    payment_method: str # Can be 'points', 'stripe', or 'subscription_plan' (for AI Studio)
    contest_id: int | None = None # contest_id is for Art Battle
    # chat_history: list[dict] | None = None # For future dedicated AI Assistant chat context

class PayoutRequest(BaseModel): user_id: str; points_amount: int; method: str; address: str
class UserProfileUpdate(BaseModel): display_name: str | None = None; avatar_url: str | None = None
class PurchaseRequest(BaseModel): user_id: str; item_id: int; payment_method: str

# New: Request to subscribe to a plan (frontend sends plan_type, backend maps to price_id)
class CreateSubscriptionRequest(BaseModel):
    user_id: str
    plan_type: str # 'premium' or 'assistant'
    success_url: str
    cancel_url: str

class SubmissionRequest(BaseModel): contest_id: int; user_id: str; image_url: str; prompt: str


# --- Helper Functions ---
def get_supabase_client() -> Client: return create_client(SUPABASE_URL, SUPABASE_KEY)

# Function to generate the viral plan (now accepts user plan)
def generate_viral_plan(prompt: str, user_plan: SubscriptionPlan = SubscriptionPlan.FREE) -> str:
    if not vertexai: # If Vertex AI is not initialized
        return "AI service not available. Please try again later."

    try:
        model = GenerativeModel("gemini-1.5-flash") # Or a more powerful model for higher tiers

        ai_prompt = ""
        if user_plan == SubscriptionPlan.ASSISTANT:
            ai_prompt = f"""Given the following creative prompt: '{prompt}', create an EXTREMELY DETAILED and ACTIONABLE marketing plan in 7-10 specific points to make it viral.
            For each point, include:
            - A clear title.
            - An in-depth description of the action.
            - Specific implementation tips for various platforms (Instagram Reels, TikTok, YouTube Shorts, X, LinkedIn, Blog, etc.).
            - Examples of calls-to-action, relevant hashtags, trending audio or visual elements.
            - Strategies for community interaction.
            - Key metrics to monitor.
            Use a highly professional and strategic tone. Format clearly with lists and bullet points.
            """
        elif user_plan == SubscriptionPlan.PREMIUM:
            ai_prompt = f"""Given the following creative prompt: '{prompt}', create a SUPER DETAILED marketing plan in 5-7 specific points to make a post or content based on this viral on platforms like Instagram and TikTok.
            For each point, provide:
            - A clear description of the action.
            - Concrete tips on how to implement it (e.g., types of videos, descriptions, timings).
            - Examples of relevant hashtags or trending sounds.
            Use an energetic and persuasive tone. Include emojis for each point.
            """
        else: # FREE Plan
            ai_prompt = f"Given the following creative prompt: '{prompt}', create a marketing plan in 3 brief points to make a post based on this content viral on social media (like Instagram or TikTok). Be concise and impactful. Use emojis."
        
        return model.generate_content(ai_prompt).text.strip()
    except Exception as e:
        print(f"Error generating viral plan: {e}")
        return "An error occurred while generating the plan. Please try again later."


# Function to get AI text prompt quality based on plan
def get_ai_text_prompt(base_prompt: str, user_plan: SubscriptionPlan = SubscriptionPlan.FREE) -> str:
    if user_plan == SubscriptionPlan.ASSISTANT:
        return f"""Write a very in-depth, persuasive, and comprehensive article or blog/social media post (minimum 300-500 words) based on this concept: '{base_prompt}'.
        Include:
        - A captivating introduction.
        - Detailed key points with explanations.
        - Relevant examples.
        - A concluding call-to-action or reflection.
        - Adapt the tone to a marketing professional context.
        - Use rich and informative language.
        """
    elif user_plan == SubscriptionPlan.PREMIUM:
        return f"""Write a very detailed and engaging social media post (e.g., Instagram/TikTok), around 150-200 words, based on this concept: '{base_prompt}'.
        Include relevant calls-to-action and hashtags. Use a persuasive and engaging style.
        """
    else: # FREE Plan
        return f"Write a brief and engaging Instagram post based on this concept: '{base_prompt}'. Be concise and impactful. Maximum length: 100 words."

# Function to reset daily generation count
def reset_daily_generations_if_needed(user_data: dict, supabase_client: Client):
    now = datetime.now(timezone.utc)
    last_reset_str = user_data.get('last_generation_reset_date')

    needs_reset = False
    if last_reset_str:
        last_reset_date = datetime.fromisoformat(last_reset_str).replace(tzinfo=timezone.utc).date()
        if (now.date() - last_reset_date).days >= 1: # If at least one day has passed
            needs_reset = True
    else: # If there's never been a reset (new user or empty field)
        needs_reset = True
    
    if needs_reset:
        print(f"Resetting daily generations for user {user_data['user_id']}")
        supabase_client.table('users').update({
            'daily_ai_generations_used': 0,
            'last_generation_reset_date': now.isoformat()
        }).eq('user_id', user_data['user_id']).execute()
        # Update in-memory data for the current request
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
        response = supabase.table('users').select('user_id, last_login_at, login_streak').eq('user_id', user_data.user_id).single().execute()
        if not response.data:
            # New fields added for subscription plan
            new_user_record = {
                'user_id': user_data.user_id,
                'email': user_data.email,
                'display_name': user_data.displayName,
                'referrer_id': user_data.referrer_id,
                'avatar_url': user_data.avatar_url,
                'login_streak': 1,
                'last_login_at': now.isoformat(),
                'points_balance': 0,
                'free_generations_used': 0, # This is for older "Art Battle" generations
                'subscription_plan': SubscriptionPlan.FREE.value, # New field
                'daily_ai_generations_used': 0, # New field
                'last_generation_reset_date': now.isoformat(), # New field
                'stripe_customer_id': None # New field for Stripe Customer ID
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
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/update_profile/{user_id}")
def update_profile(user_id: str, profile_data: UserProfileUpdate):
    try:
        supabase = get_supabase_client()
        update_payload = profile_data.dict(exclude_unset=True)
        if not update_payload: raise HTTPException(status_code=400, detail="No data provided.")
        supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profile updated."}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/request_payout")
def request_payout(payout_data: PayoutRequest):
    supabase = get_supabase_client()
    try:
        user_res = supabase.table("users").select("points_balance").eq("user_id", payout_data.user_id).single().execute()
        if not user_res.data or user_res.data.get("points_balance", 0) < payout_data.points_amount:
            raise HTTPException(status_code=402, detail="Insufficient withdrawable points.")
    except Exception as e: raise HTTPException(status_code=500, detail=f"Balance error: {e}")

    if payout_data.method == 'PayPal':
        try:
            value_eur = str(round(payout_data.points_amount / POINTS_TO_EUR_RATE, 2))
            payout = paypalrestsdk.Payout({"sender_batch_header": {"sender_batch_id": f"payout_{time.time()}", "email_subject": "You've received a payment from Zenith Rewards!"}, "items": [{"recipient_type": "EMAIL", "amount": {"value": value_eur, "currency": "EUR"}, "receiver": payout_data.address, "note": "Thanks for using Zenith Rewards!", "sender_item_id": f"item_{time.time()}"}]})
            if payout.create():
                supabase.rpc('add_points', {'user_id_in': payout_data.user_id, 'points_to_add': -payout_data.points_amount}).execute()
                return {"status": "success", "message": "PayPal payout processed!"}
            else: raise HTTPException(status_code=500, detail=payout.error)
        except Exception as e: raise HTTPException(status_code=500, detail=f"PayPal error: {e}")
    else:
        supabase.rpc('add_points', {'user_id_in': payout_data.user_id, 'points_to_add': -payout_data.points_amount}).execute()
        return {"status": "success", "message": f"Payout request for {payout_data.method} received."}

@app.get("/get_user_balance/{user_id}")
def get_user_balance(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"points_balance": 0}
        return {"points_balance": response.data.get('points_balance', 0)}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/streak/status/{user_id}")
def get_streak_status(user_id: str):
    try:
        supabase = get_supabase_client()
        response = supabase.table('users').select('login_streak, last_streak_claim_at').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"days": 0, "canClaim": True}
        user, can_claim = response.data, True
        if user.get('last_streak_claim_at'):
            if datetime.fromisoformat(user['last_streak_claim_at']).date() == datetime.now(timezone.utc).date(): can_claim = False
        return {"days": user.get('login_streak', 0), "canClaim": can_claim}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.post("/streak/claim/{user_id}")
def claim_streak_bonus(user_id: str):
    try:
        status = get_streak_status(user_id)
        if not status["canClaim"]: raise HTTPException(status_code=400, detail="Bonus already claimed.")
        reward = min(status["days"] * 10, 100)
        supabase = get_supabase_client()
        supabase.rpc('add_points', {'user_id_in': user_id, 'points_to_add': reward}).execute()
        supabase.table('users').update({'last_streak_claim_at': datetime.now(timezone.utc).isoformat()}).eq('user_id', user_id).execute()
        return {"status": "success", "message": f"You claimed {reward} Zenith Coins!"}
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

@app.get("/leaderboard")
def get_leaderboard():
    supabase = get_supabase_client()
    response = supabase.table('users').select('display_name, points_balance, avatar_url').order('points_balance', desc=True).limit(10).execute()
    return [{"name": u.get('display_name', 'N/A'), "earnings": u.get('points_balance', 0) / POINTS_TO_EUR_RATE, "avatar": u.get('avatar_url', '')} for u in response.data]

@app.get("/shop/items")
def get_shop_items():
    supabase = get_supabase_client()
    return supabase.table("shop_items").select("*").eq("is_active", True).order("price").execute().data

@app.post("/shop/buy")
def buy_shop_item(req: PurchaseRequest):
    supabase = get_supabase_client()
    try:
        item_res = supabase.table("shop_items").select("price, price_eur, name").eq("id", req.item_id).single().execute()
        if not item_res.data: raise HTTPException(status_code=404, detail="Item not found.")
        item = item_res.data
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching item: {e}")

    if req.payment_method == 'points':
        try:
            supabase.rpc('purchase_item', {'p_user_id': req.user_id, 'p_item_id': req.item_id}).execute()
            return {"status": "success", "message": "Purchase completed!"}
        except Exception as e:
            if 'Insufficient funds' in str(e): raise HTTPException(status_code=402, detail="Insufficient Zenith Coins.")
            raise HTTPException(status_code=500, detail="Error during purchase.")
    elif req.payment_method == 'stripe':
        try:
            price_in_eur = item.get("price_eur")
            if price_in_eur is None: raise HTTPException(status_code=400, detail="EUR price not available.")
            price_in_cents = int(price_in_eur * 100)
            payment_intent = stripe.PaymentIntent.create(amount=price_in_cents, currency="eur", automatic_payment_methods={"enabled": True}, metadata={'user_id': req.user_id, 'item_id': req.item_id})
            return {"client_secret": payment_intent.client_secret}
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Stripe error: {e}")
    else:
        raise HTTPException(status_code=400, detail="Invalid payment method.")


# New endpoint to create a Stripe subscription checkout session
@app.post("/create-checkout-session")
def create_checkout_session(req: CreateSubscriptionRequest):
    if not STRIPE_SECRET_KEY:
        raise HTTPException(status_code=500, detail="Stripe not configured on the server.")
    
    # Map plan_type to the actual Stripe Price ID
    selected_price_id = None
    if req.plan_type == 'premium':
        selected_price_id = STRIPE_PRICE_ID_PREMIUM
    elif req.plan_type == 'assistant':
        selected_price_id = STRIPE_PRICE_ID_ASSISTANT
    else:
        raise HTTPException(status_code=400, detail="Invalid plan type provided.")

    if not selected_price_id:
        raise HTTPException(status_code=500, detail="Stripe Price ID not configured for the selected plan type.")

    try:
        # Retrieve user to get email and potentially existing Stripe customer_id
        supabase = get_supabase_client()
        user_data_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).single().execute()
        user_data = user_data_res.data

        customer_id = user_data.get('stripe_customer_id')
        if not customer_id:
            # Create a new Stripe customer if one doesn't exist for this user
            customer = stripe.Customer.create(email=user_data.get('email'), metadata={'user_id': req.user_id})
            customer_id = customer.id
            # Save the new customer_id in Supabase
            supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id).execute()

        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            line_items=[
                {
                    'price': selected_price_id, # Use the determined Price ID
                    'quantity': 1,
                },
            ],
            mode='subscription',
            success_url=req.success_url,
            cancel_url=req.cancel_url,
            metadata={'user_id': req.user_id, 'price_id': selected_price_id}, # Store the actual price_id for webhook
        )
        return {"session_id": checkout_session.id, "url": checkout_session.url}
    except Exception as e:
        print(f"Error creating Stripe checkout session: {e}")
        raise HTTPException(status_code=500, detail=f"Error creating checkout session: {str(e)}")


@app.post("/ai/generate")
def generate_ai_content(req: AIGenerationRequest):
    if not vertexai: # Ensure Vertex AI is initialized
        raise HTTPException(status_code=503, detail="AI generation service not available.")

    supabase = get_supabase_client()
    user_res = supabase.table("users").select("points_balance, free_generations_used, subscription_plan, daily_ai_generations_used, last_generation_reset_date").eq("user_id", req.user_id).single().execute()
    if not user_res.data: raise HTTPException(status_code=404, detail="User not found.")
    user_data = user_res.data

    user_plan = SubscriptionPlan(user_data.get('subscription_plan', SubscriptionPlan.FREE.value))

    # --- Daily AI Studio Generations Reset Logic ---
    # This function updates daily_ai_generations_used and last_generation_reset_date
    # if at least one day has passed since the last reset.
    reset_daily_generations_if_needed(user_data, supabase)
    
    # Check generation limits for AI Studio plans
    if not req.contest_id: # This is a generation for AI Studio, not Art Battle
        current_generations = user_data.get('daily_ai_generations_used', 0)
        
        if user_plan == SubscriptionPlan.FREE:
            if current_generations >= MAX_FREE_GENERATIONS_PER_DAY:
                raise HTTPException(status_code=429, detail=f"You have reached the limit of {MAX_FREE_GENERATIONS_PER_DAY} daily generations for the Basic Plan.")
            # Note: The prompt character limit is primarily handled by the frontend.
            # You could theoretically truncate req.prompt[:FREE_PROMPT_MAX_CHARS] here but frontend feedback is better.
        elif user_plan == SubscriptionPlan.PREMIUM:
            if current_generations >= MAX_PREMIUM_GENERATIONS_PER_DAY:
                raise HTTPException(status_code=429, detail=f"You have reached the limit of {MAX_PREMIUM_GENERATIONS_PER_DAY} daily generations for the Premium Plan.")
        # Assistant Plan has unlimited generations, so no limits here
    
    # --- Payment Logic for Art Battle vs. AI Studio ---
    # Art Battle generations maintain their point/stripe logic for single generation.
    # AI Studio generations use the subscription model and do not require 'payment_method' here
    # (as it's handled by subscription check).
    
    client_secret_for_frontend = None
    is_paid_quality_level = False # Default to basic quality

    if req.contest_id: # This is a generation for Art Battle (old cost logic)
        cost_in_points = IMAGE_GENERATION_POINTS_COST
        is_paid_generation_art_battle = True # Indicates if it's an Art Battle generation that costs
        if user_data.get('free_generations_used', 0) < 3:
            cost_in_points = IMAGE_GENERATION_POINTS_COST_DISCOUNTED
            is_paid_generation_art_battle = False
        
        # Quality for Art Battle: if user pays full price OR is on a premium/assistant plan
        is_paid_quality_level = (user_plan != SubscriptionPlan.FREE) or \
                                (req.payment_method == 'stripe') or \
                                (req.payment_method == 'points' and not is_paid_generation_art_battle) # Was discounted or paid full points

        if req.payment_method == 'points':
            if user_data.get('points_balance', 0) < cost_in_points: raise HTTPException(status_code=402, detail="Insufficient Zenith Coins.")
            supabase.rpc('add_points', {'user_id_in': req.user_id, 'points_to_add': -cost_in_points}).execute()
            # Only increment free_generations_used if it was one of the first 3 discounted generations
            if not is_paid_generation_art_battle:
                supabase.table("users").update({"free_generations_used": user_data.get('free_generations_used', 0) + 1}).eq("user_id", req.user_id).execute()

        elif req.payment_method == 'stripe':
            if not is_paid_generation_art_battle: # If it's one of the first 3 discounted generations, they cannot pay with Stripe for this
                raise HTTPException(status_code=400, detail="Euro payment not available for discounted Art Battle generations.")
            price_in_cents = int(IMAGE_GENERATION_EUR_PRICE * 100)
            try:
                payment_intent = stripe.PaymentIntent.create(amount=price_in_cents, currency="eur", automatic_payment_methods={"enabled": True}, metadata={'user_id': req.user_id, 'item_id': f'ai_generation_{req.content_type}_art_battle'})
                client_secret_for_frontend = payment_intent.client_secret
            except Exception as e: raise HTTPException(status_code=500, detail=f"Stripe error: {e}")
        else: # No valid payment_method for Art Battle
            raise HTTPException(status_code=400, detail="Invalid payment method for Art Battle.")
    
    else: # This is a generation for AI Studio (uses subscription plan)
        # There's no direct payment here, it's covered by the subscription
        # Determine quality based on subscription plan
        is_paid_quality_level = (user_plan == SubscriptionPlan.PREMIUM or user_plan == SubscriptionPlan.ASSISTANT)

    # If client_secret_for_frontend is present, the frontend must complete the payment
    if client_secret_for_frontend: return {"client_secret": client_secret_for_frontend, "payment_required": True}

    # If we reach here, payment (or lack thereof for subscription) has been handled
    try:
        generated_url, generated_text = None, None
        
        # Choose AI model based on required quality (more advanced for higher tiers)
        # Note: "gemini-1.5-flash" is used here for all, but you could map to "gemini-1.5-pro" or others
        # if configured for premium/assistant plans
        text_model_name = "gemini-1.5-flash"
        if user_plan == SubscriptionPlan.ASSISTANT:
            text_model_name = "gemini-1.5-pro" # Assuming you've deployed/have access to this model
        text_model = GenerativeModel(text_model_name)

        image_model = ImageGenerationModel.from_pretrained("imagen-3.0-generate-002")

        if req.content_type == 'IMAGE':
            # For images, differentiation could be:
            # - Resolution (if supported by Imagen)
            # - Number of images generated (e.g., 4 for premium/assistant, 1 for free)
            # - Access to newer/more performant image generation models
            images = image_model.generate_images(prompt=req.prompt, number_of_images=1, aspect_ratio="1:1")
            generated_url = f"data:image/png;base64,{base64.b64encode(images[0]._image_bytes).decode('utf-8')}"
        elif req.content_type == 'POST':
            # Use the helper function that adapts the prompt based on the plan
            post_prompt = get_ai_text_prompt(req.prompt, user_plan)
            response = text_model.generate_content(post_prompt)
            generated_text = response.text.strip()
        elif req.content_type == 'VIDEO':
            # Here you could also differentiate video quality/duration
            raise HTTPException(status_code=501, detail="Video generation not implemented.")
        
        # Generate the viral plan with dynamic quality based on the user's plan
        ai_strategy_plan = generate_viral_plan(req.prompt, user_plan)
        
        new_content = {
            "user_id": req.user_id,
            "content_type": req.content_type,
            "prompt": req.prompt,
            "generated_url": generated_url,
            "generated_text": generated_text,
            "ai_strategy_plan": ai_strategy_plan,
            "status": "DRAFT",
            "contest_id": req.contest_id # Will be None for AI Studio generations
        }
        insert_res = supabase.table("ai_content").insert(new_content, returning="representation").execute()
        
        # If the generation is for AI Studio, increment the daily counter
        if not req.contest_id:
            supabase.table("users").update({"daily_ai_generations_used": user_data.get('daily_ai_generations_used', 0) + 1}).eq("user_id", req.user_id).execute()
        
        return insert_res.data[0]
    except Exception as e:
        # If there's an error in AI generation, revert point deduction for Art Battle
        # For AI Studio, there's no direct point deduction, but the daily counter is not incremented
        if req.contest_id and req.payment_method == 'points':
             # Restore points only if it was a point-based payment for Art Battle AND it was deducted
             # (This check assumes the deduction happened successfully before the try block)
             if user_data.get('points_balance', 0) >= (IMAGE_GENERATION_POINTS_COST_DISCOUNTED if user_data.get('free_generations_used', 0) < 3 else IMAGE_GENERATION_POINTS_COST):
                print(f"AI generation error for Art Battle, restoring points for user {req.user_id}")
                supabase.rpc('add_points', {'user_id_in': req.user_id, 'points_to_add': (IMAGE_GENERATION_POINTS_COST_DISCOUNTED if user_data.get('free_generations_used', 0) < 3 else IMAGE_GENERATION_POINTS_COST)}).execute()
        raise HTTPException(status_code=500, detail=f"AI generation error: {e}")

@app.post("/ai/content/{content_id}/publish")
def publish_content(content_id: int):
    supabase = get_supabase_client()
    supabase.table("ai_content").update({"status": "PUBLISHED"}).eq("id", content_id).execute()
    return {"status": "success", "message": "Content published!"}

@app.post("/ai/content/{content_id}/vote")
def vote_for_content(content_id: int):
    supabase = get_supabase_client()
    supabase.rpc('increment_content_votes', {'content_id_in': content_id}).execute()
    return {"status": "success"}

@app.get("/leaderboard/weekly")
def get_weekly_leaderboard():
    supabase = get_supabase_client()
    return supabase.rpc('get_weekly_leaderboard').execute().data

@app.get("/contests/current")
def get_current_contest():
    supabase = get_supabase_client()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    # Added 'status': 'ACTIVE' to the query to get only active contests
    response = supabase.table('ai_contests').select('*').gte('start_date', today_start.isoformat()).lt('end_date', (today_start + timedelta(days=1)).isoformat()).eq('status','ACTIVE').limit(1).execute()
    if response.data: return response.data[0]
    return {} # Returns an empty object if no active contest

@app.get("/referral_stats/{user_id}")
def get_referral_stats(user_id: str):
    supabase = get_supabase_client()
    response = supabase.table('users').select('user_id', count='exact').eq('referrer_id', user_id).execute()
    return {"referral_count": response.count or 0, "referral_earnings": 0.00}
    
@app.post("/stripe-webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')
    
    event = None
    try:
        event = stripe.Webhook.construct_event(payload=payload, sig_header=sig_header, secret=STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        # Invalid payload
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.error.SignatureVerificationError as e:
        # Invalid signature
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    supabase = get_supabase_client()

    # Handle Payment Intent events (for one-time purchases, like shop items or Art Battle generations)
    if event['type'] == 'payment_intent.succeeded':
        payment_intent = event['data']['object']
        metadata = payment_intent.get('metadata')
        if metadata:
            user_id = metadata.get('user_id')
            item_id_str = metadata.get('item_id')
            if user_id and item_id_str:
                if 'ai_generation' in item_id_str:
                    print(f"Stripe payment for AI generation (Art Battle) received for user {user_id}. Generation handled by frontend.")
                    # The actual generation logic is on the frontend after payment.
                    # Here you might just log the transaction if necessary.
                else:
                    try:
                        print(f"Stripe payment for item {item_id_str} received for user {user_id}")
                        supabase.rpc('purchase_item', {'p_user_id': user_id, 'p_item_id': int(item_id_str)}).execute()
                        print("Item delivered successfully!")
                    except Exception as e:
                        print(f"CRITICAL ERROR: Failed to deliver item {item_id_str}. Error: {e}")
    
    # --- Handle Stripe Subscription Events (NEW LOGIC FOR AI STUDIO) ---
    elif event['type'] == 'customer.subscription.created' or event['type'] == 'customer.subscription.updated':
        subscription = event['data']['object']
        customer_id = subscription.get('customer')
        status = subscription.get('status') # e.g., 'active', 'trialing', 'past_due', 'canceled'
        
        # Safely get price_id from the first item in the subscription
        price_id = None
        if subscription.get('items') and subscription['items'].get('data') and len(subscription['items']['data']) > 0:
            price_id = subscription['items']['data'][0]['price']['id']
        
        if not price_id:
            print(f"WARNING: Price ID not found in subscription {subscription.get('id')}")
            return Response(status_code=400) # Bad request if price_id is missing

        # Retrieve Supabase user based on Stripe customer ID
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).single().execute()
        if user_res.data:
            user_id = user_res.data['user_id']
            new_plan = SubscriptionPlan.FREE # Default

            if price_id == STRIPE_PRICE_ID_PREMIUM:
                new_plan = SubscriptionPlan.PREMIUM
            elif price_id == STRIPE_PRICE_ID_ASSISTANT:
                new_plan = SubscriptionPlan.ASSISTANT
            
            if status == 'active' or status == 'trialing':
                # Update user's plan in Supabase
                supabase.table('users').update({
                    'subscription_plan': new_plan.value,
                    # Optionally, reset daily_ai_generations_used here when a subscription is activated/updated
                    # 'daily_ai_generations_used': 0,
                    # 'last_generation_reset_date': datetime.now(timezone.utc).isoformat()
                }).eq('user_id', user_id).execute()
                print(f"User {user_id} subscription {new_plan.value} active. Plan updated in Supabase.")
            else:
                # If subscription is not active, you might degrade the user to FREE
                print(f"Subscription for user {user_id} not active ({status}). Degrading to FREE.")
                supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_id).execute()
        else:
            print(f"WARNING: Supabase user not found for Stripe Customer ID: {customer_id}")

    elif event['type'] == 'customer.subscription.deleted':
        subscription = event['data']['object']
        customer_id = subscription.get('customer')
        
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).single().execute()
        if user_res.data:
            user_id = user_res.data['user_id']
            # Subscription canceled, degrade user to FREE
            supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_id).execute()
            print(f"Subscription canceled for user {user_id}. Degraded to FREE.")
        else:
            print(f"WARNING: Supabase user not found for Stripe Customer ID: {customer_id} during cancellation.")

    return Response(status_code=200) # Important: Stripe expects a 200 OK response

@app.get("/missions/{user_id}")
def get_missions(user_id: str): raise HTTPException(status_code=501, detail="Not implemented.")
@app.post("/contests/submit")
def submit_artwork(req: SubmissionRequest): raise HTTPException(status_code=501, detail="Not implemented.")