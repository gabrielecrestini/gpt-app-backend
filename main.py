import os
from datetime import datetime, timezone
import json
from enum import Enum

from fastapi import FastAPI, HTTPException, Request, Response, Depends
from pydantic import BaseModel
from supabase import create_client, Client
from dotenv import load_dotenv
from fastapi.middleware.cors import CORSMiddleware
import stripe
import vertexai
from vertexai.generative_models import GenerativeModel, Part, Image

load_dotenv()

STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
GCP_PROJECT_ID = os.environ.get("GCP_PROJECT_ID")
GCP_REGION = os.environ.get("GCP_REGION")
GCP_SA_KEY_JSON_STR = os.environ.get("GCP_SA_KEY_JSON")
STRIPE_PRICE_ID_PREMIUM = os.environ.get("STRIPE_PRICE_ID_PREMIUM")
STRIPE_PRICE_ID_ASSISTANT = os.environ.get("STRIPE_PRICE_ID_ASSISTANT")
PAYPAL_CLIENT_ID = os.environ.get("PAYPAL_CLIENT_ID")

app = FastAPI(title="Zenith Rewards Backend", description="Backend per la gestione di utenti, AI, pagamenti e gamification per Zenith Rewards.")

gemini_flash_model = None
gemini_pro_vision_model = None
vertexai_initialized = False

if all([GCP_PROJECT_ID, GCP_REGION, GCP_SA_KEY_JSON_STR]):
    try:
        sa_key_path = "gcp_sa_key.json"
        with open(sa_key_path, "w") as f:
            f.write(GCP_SA_KEY_JSON_STR)
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = sa_key_path

        vertexai.init(project=GCP_PROJECT_ID, location=GCP_REGION)
        gemini_flash_model = GenerativeModel("gemini-1.5-flash")
        gemini_pro_vision_model = GenerativeModel("gemini-pro-vision")
        vertexai_initialized = True
    except Exception as e:
        print(f"WARNING: Vertex AI configuration error: {e}. AI functionalities might be limited or unavailable.")
else:
    print("WARNING: Missing GCP credentials. Vertex AI is disabled.")

if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY
else:
    print("WARNING: STRIPE_SECRET_KEY not configured. Stripe functionalities are disabled.")

FRONTEND_URL = os.environ.get("NEXT_PUBLIC_FRONTEND_URL", "https://cashhh-52f38.web.app")

allowed_origins = [
    "http://localhost:3000",
    "https://cashhh-52f38.web.app",
    "https://cashhh-52738.web.app",
    FRONTEND_URL
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

POINTS_TO_EUR_RATE = 1000.0

class SubscriptionPlan(str, Enum):
    FREE = 'free'
    PREMIUM = 'premium'
    ASSISTANT = 'assistant'

class ContentType(str, Enum):
    IMAGE = 'IMAGE'
    POST = 'POST'
    VIDEO = 'VIDEO'

class ItemType(str, Enum):
    BOOST = 'BOOST'
    COSMETIC = 'COSMETIC'
    GENERATION_PACK = 'GENERATION_PACK'

class UserSyncRequest(BaseModel):
    user_id: str
    email: str | None = None
    displayName: str | None = None
    referrer_id: str | None = None
    avatar_url: str | None = None

class UserProfileUpdate(BaseModel):
    display_name: str | None = None
    avatar_url: str | None = None

class PayoutRequest(BaseModel):
    user_id: str
    points_amount: int
    method: str
    address: str

class AIAdviceRequest(BaseModel):
    user_id: str
    prompt: str

class AIGenerationRequest(BaseModel):
    user_id: str
    prompt: str
    content_type: ContentType
    payment_method: 'points' | 'stripe'
    contest_id: int | None = None

class VoteContentRequest(BaseModel):
    user_id: str

class CreateSubscriptionRequest(BaseModel):
    user_id: str
    plan_type: str
    success_url: str
    cancel_url: str

class ShopBuyRequest(BaseModel):
    user_id: str
    item_id: int
    payment_method: 'points' | 'stripe'

def get_supabase_client() -> Client:
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise HTTPException(status_code=500, detail="Supabase credentials not configured.")
    return create_client(SUPABASE_URL, SUPABASE_KEY)

class UserManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    def sync_user(self, user_data: UserSyncRequest):
        now = datetime.now(timezone.utc)
        response = self.supabase.table('users').select('user_id, last_login_at, login_streak, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date').eq('user_id', user_data.user_id).maybe_single().execute()
        
        if not response.data:
            new_user_record = {
                'user_id': user_data.user_id,
                'email': user_data.email,
                'display_name': user_data.displayName,
                'referrer_id': user_data.referrer_id,
                'avatar_url': user_data.avatar_url,
                'login_streak': 1,
                'last_login_at': now.isoformat(),
                'points_balance': 0,
                'pending_points_balance': 0,
                'subscription_plan': SubscriptionPlan.FREE.value,
                'daily_ai_generations_used': 0,
                'last_generation_reset_date': now.isoformat(),
                'daily_votes_used': 0,
                'last_vote_reset_date': now.isoformat()
            }
            self.supabase.table('users').insert(new_user_record).execute()
        else:
            user = response.data
            last_login_str = user.get('last_login_at')
            current_streak = user.get('login_streak', 0)
            
            if last_login_str:
                last_login_date = datetime.fromisoformat(last_login_str).date()
                today = now.date()
                days_diff = (today - last_login_date).days

                if days_diff == 1:
                    new_streak = current_streak + 1
                elif days_diff > 1:
                    new_streak = 1
                else:
                    new_streak = current_streak
            else:
                new_streak = 1

            update_data = {'last_login_at': now.isoformat(), 'login_streak': new_streak}
            
            last_gen_reset_date = datetime.fromisoformat(user.get('last_generation_reset_date', now.isoformat())).date()
            if now.date() > last_gen_reset_date:
                update_data['daily_ai_generations_used'] = 0
                update_data['last_generation_reset_date'] = now.isoformat()
            
            last_vote_reset_date = datetime.fromisoformat(user.get('last_vote_reset_date', now.isoformat())).date()
            if now.date() > last_vote_reset_date:
                update_data['daily_votes_used'] = 0
                update_data['last_vote_reset_date'] = now.isoformat()

            self.supabase.table('users').update(update_data).eq('user_id', user_data.user_id).execute()
        return {"status": "success"}

    def update_profile(self, user_id: str, profile_data: UserProfileUpdate):
        update_payload = profile_data.model_dump(exclude_unset=True)
        if not update_payload: raise HTTPException(status_code=400, detail="No data provided to update.")
        self.supabase.table('users').update(update_payload).eq('user_id', user_id).execute()
        return {"status": "success", "message": "Profile updated successfully."}

    def get_user_balance(self, user_id: str):
        response = self.supabase.table('users').select('points_balance, pending_points_balance').eq('user_id', user_id).maybe_single().execute()
        if not response.data: raise HTTPException(status_code=404, detail="User not found")
        return response.data

    def get_user_profile(self, user_id: str):
        response = self.supabase.table('users').select(
            'subscription_plan, daily_ai_generations_used, last_generation_reset_date, daily_votes_used, last_vote_reset_date, points_balance'
        ).eq('user_id', user_id).maybe_single().execute()
        if not response.data:
            return {
                "subscription_plan": SubscriptionPlan.FREE.value,
                "daily_ai_generations_used": 0,
                "last_generation_reset_date": datetime.now(timezone.utc).isoformat(),
                "daily_votes_used": 0,
                "last_vote_reset_date": datetime.now(timezone.utc).isoformat(),
                "points_balance": 0
            }
        return response.data

    def get_streak_status(self, user_id: str):
        response = self.supabase.table('users').select('login_streak').eq('user_id', user_id).maybe_single().execute()
        if not response.data: return {"login_streak": 0}
        return response.data

    def claim_streak_reward(self, user_id: str):
        try:
            result = self.supabase.rpc('claim_streak_reward', {'p_user_id': user_id}).execute()
            if result.data and isinstance(result.data, list) and len(result.data) > 0:
                return result.data[0]
            raise HTTPException(status_code=400, detail="Failed to claim streak reward. Check database function logs.")
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error claiming streak: {e}")

    def get_referral_stats(self, user_id: str):
        referral_count_res = self.supabase.table('users').select('count').eq('referrer_id', user_id).maybe_single().execute()
        referral_count = referral_count_res.data['count'] if referral_count_res.data else 0
        referral_earnings = referral_count * 100
        return {"referral_count": referral_count, "referral_earnings": referral_earnings}

class AIManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    AI_GENERATION_LIMITS = {
        SubscriptionPlan.FREE: 3,
        SubscriptionPlan.PREMIUM: 15,
        SubscriptionPlan.ASSISTANT: 100
    }
    DAILY_VOTE_LIMITS = {
        SubscriptionPlan.FREE: 5,
        SubscriptionPlan.PREMIUM: 20,
        SubscriptionPlan.ASSISTANT: 50
    }

    def get_ai_cost(self, user_plan: SubscriptionPlan):
        if user_plan == SubscriptionPlan.FREE:
            return {"points": 500, "eur": 0.50}
        elif user_plan == SubscriptionPlan.PREMIUM:
            return {"points": 200, "eur": 0.20}
        elif user_plan == SubscriptionPlan.ASSISTANT:
            return {"points": 100, "eur": 0.10}
        return {"points": 1000, "eur": 1.00}

    async def generate_advice(self, req: AIAdviceRequest):
        if not vertexai_initialized or not gemini_flash_model:
            raise HTTPException(status_code=503, detail="AI service (Gemini Flash) is not available or not initialized.")

        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        generations_used = user_profile.get('daily_ai_generations_used', 0)
        last_reset_str = user_profile.get('last_generation_reset_date', datetime.now(timezone.utc).isoformat())
        
        if datetime.fromisoformat(last_reset_str).date() < datetime.now(timezone.utc).date():
            generations_used = 0
            self.supabase.table('users').update({
                'daily_ai_generations_used': 0,
                'last_generation_reset_date': datetime.now(timezone.utc).isoformat()
            }).eq('user_id', req.user_id).execute()

        if generations_used >= self.AI_GENERATION_LIMITS.get(user_plan, 0):
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite di generazioni AI giornaliere ({self.AI_GENERATION_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'. Effettua l'upgrade per più generazioni!")

        final_prompt = f"Data l'idea o l'obiettivo '{req.prompt}', fornisci 3 consigli brevi e di impatto per il successo."
        if user_plan == SubscriptionPlan.PREMIUM:
            final_prompt = f"Agisci come un esperto di strategie aziendali. Data l'idea o l'obiettivo '{req.prompt}', crea un piano d'azione dettagliato di 5-7 punti con esempi pratici e suggerimenti per marketing e social media."
        elif user_plan == SubscriptionPlan.ASSISTANT:
            final_prompt = f"Sei un mentore aziendale di livello mondiale e un esperto di marketing digitale, dropshipping, trading e social media. Data l'idea o l'obiettivo '{req.prompt}', crea una strategia passo-passo estremamente dettagliata e personalizzata, includendo tattiche specifiche per scalare sia i social della piattaforma Zenith Rewards che i social esterni, suggerimenti per il dropshipping, il trading e l'e-commerce, e un piano di viralità completo. La tua risposta deve essere completa, azionabile e coprire tutte le sfaccettature richieste."
        
        try:
            response_ai = gemini_flash_model.generate_content(final_prompt)
            generated_text = response_ai.text.strip()

            self.supabase.table('users').update({
                'daily_ai_generations_used': generations_used + 1
            }).eq('user_id', req.user_id).execute()
            
            return {"advice": generated_text}
        except Exception as e:
            raise HTTPException(status_code=503, detail=f"Errore del servizio AI: {e}. Riprova più tardi.")

    async def generate_content(self, req: AIGenerationRequest):
        if not vertexai_initialized:
            raise HTTPException(status_code=503, detail="AI service is not available.")

        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        
        cost = self.get_ai_cost(user_plan)
        
        if req.payment_method == 'points':
            if user_profile['points_balance'] < cost['points']:
                raise HTTPException(status_code=402, detail=f"Punti insufficienti. Hai bisogno di {cost['points']} ZC.")
        elif req.payment_method == 'stripe':
            pass

        generated_url = None
        generated_text = None
        ai_strategy_plan = f"Piano base per la viralità: Condividi la tua creazione sui social media di Zenith Rewards e incoraggia i tuoi amici a votare! Per strategie avanzate, considera l'upgrade al piano Premium o Assistant."

        try:
            if req.content_type == ContentType.IMAGE:
                if not gemini_pro_vision_model:
                     raise HTTPException(status_code=503, detail="AI image generation model not available.")
                image_response = gemini_pro_vision_model.generate_content(f"Crea una breve descrizione testuale e un suggerimento visivo per un'immagine basata su: '{req.prompt}'.")
                generated_text = f"Immagine generata: {image_response.text.strip()}\n(Simulazione: L'API reale genererebbe un URL immagine.)"
                generated_url = "https://via.placeholder.com/400x300?text=AI+Image"

            elif req.content_type == ContentType.POST:
                response_ai = gemini_flash_model.generate_content(f"Crea un post coinvolgente e conciso per i social media basato su: '{req.prompt}'. Focus su un linguaggio accattivante e hashtag pertinenti.")
                generated_text = response_ai.text.strip()
            
            elif req.content_type == ContentType.VIDEO:
                response_ai = gemini_flash_model.generate_content(f"Genera una breve sceneggiatura o un'idea per un video di 15-30 secondi basato su: '{req.prompt}'.")
                generated_text = f"Sceneggiatura video generata: {response_ai.text.strip()}\n(Simulazione: L'API reale genererebbe un URL video.)"
                generated_url = "https://www.w3schools.com/html/mov_bbb.mp4"

            if user_plan == SubscriptionPlan.PREMIUM:
                strategy_response = gemini_flash_model.generate_content(f"Espandi il piano di viralità per '{req.prompt}' e '{req.content_type.value}' con 3-5 strategie di marketing digitale e consigli per l'engagement sui social. Evidenzia le parole chiave.")
                ai_strategy_plan = strategy_response.text.strip()
            elif user_plan == SubscriptionPlan.ASSISTANT:
                strategy_response = gemini_flash_model.generate_content(f"Agisci come consulente di marketing esperto. Crea un PIANO VIRALE AVANZATO dettagliato per il contenuto '{req.prompt}' ({req.content_type.value}), includendo analisi del target, canali di distribuzione (Zenith Rewards e social esterni), calendario di pubblicazione suggerito, idee per collaborazioni, ottimizzazione SEO/hashtag e misurazione dei risultati. Pensa come un growth hacker.")
                ai_strategy_plan = strategy_response.text.strip()

            insert_data = {
                'user_id': req.user_id,
                'contest_id': req.contest_id,
                'prompt': req.prompt,
                'content_type': req.content_type.value,
                'generated_url': generated_url,
                'generated_text': generated_text,
                'ai_strategy_plan': ai_strategy_plan,
                'is_published': False,
                'votes': 0
            }
            content_res = self.supabase.table('ai_contents').insert(insert_data).execute()
            ai_content_id = content_res.data[0]['id']

            if req.payment_method == 'points':
                self.supabase.rpc('deduct_points', {
                    'p_user_id': req.user_id,
                    'p_amount': cost['points'],
                    'p_reason': f'AI Generation - {req.content_type.value}'
                }).execute()
            
            self.supabase.table('users').update({
                'daily_ai_generations_used': user_profile['daily_ai_generations_used'] + 1,
                'last_content_generated_id': ai_content_id
            }).eq('user_id', req.user_id).execute()

            return {
                "id": ai_content_id,
                "prompt": req.prompt,
                "content_type": req.content_type.value,
                "generated_url": generated_url,
                "generated_text": generated_text,
                "ai_strategy_plan": ai_strategy_plan,
                "payment_required": False
            }

        except Exception as e:
            if "Punti insufficienti" in str(e):
                raise HTTPException(status_code=402, detail="Punti insufficienti per la generazione AI.")
            raise HTTPException(status_code=500, detail=f"Errore durante la generazione AI: {e}")

    def publish_ai_content(self, ai_content_id: int):
        self.supabase.table('ai_contents').update({'is_published': True}).eq('id', ai_content_id).execute()
        return {"status": "success", "message": "Content published successfully."}

    async def get_feed(self):
        response = self.supabase.table('ai_contents').select(
            'id, user_id, contest_id, prompt, content_type, generated_url, generated_text, ai_strategy_plan, votes, created_at, users(display_name, avatar_url)'
        ).eq('is_published', True).order('votes', desc=True).order('created_at', desc=True).limit(50).execute()
        
        formatted_feed = []
        for item in response.data:
            formatted_feed.append({
                "id": item['id'],
                "user_id": item['user_id'],
                "contest_id": item['contest_id'],
                "prompt": item['prompt'],
                "content_type": item['content_type'],
                "generated_url": item['generated_url'],
                "generated_text": item['generated_text'],
                "ai_strategy_plan": item['ai_strategy_plan'],
                "votes": item['votes'],
                "user": {
                    "display_name": item['users']['display_name'],
                    "avatar_url": item['users']['avatar_url']
                }
            })
        return formatted_feed

    async def vote_content(self, content_id: int, user_id: str):
        user_profile = UserManager(self.supabase).get_user_profile(user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        daily_votes_used = user_profile.get('daily_votes_used', 0)
        last_vote_reset_date = user_profile.get('last_vote_reset_date', datetime.now(timezone.utc).isoformat())

        if datetime.fromisoformat(last_vote_reset_date).date() < datetime.now(timezone.utc).date():
            daily_votes_used = 0
            self.supabase.table('users').update({
                'daily_votes_used': 0,
                'last_vote_reset_date': datetime.now(timezone.utc).isoformat()
            }).eq('user_id', user_id).execute()

        if daily_votes_used >= self.DAILY_VOTE_LIMITS.get(user_plan, 0):
            raise HTTPException(status_code=429, detail=f"Hai raggiunto il limite giornaliero di voti ({self.DAILY_VOTE_LIMITS.get(user_plan, 0)}) per il tuo piano '{user_plan.value}'.")

        existing_vote = self.supabase.table('votes').select('*').eq('user_id', user_id).eq('content_id', content_id).maybe_single().execute()
        if existing_vote.data:
            raise HTTPException(status_code=400, detail="Hai già votato questo contenuto.")

        content_owner_res = self.supabase.table('ai_contents').select('user_id').eq('id', content_id).maybe_single().execute()
        if content_owner_res.data and content_owner_res.data['user_id'] == user_id:
            raise HTTPException(status_code=400, detail="Non puoi votare il tuo stesso contenuto.")

        self.supabase.table('votes').insert({'user_id': user_id, 'content_id': content_id}).execute()
        self.supabase.rpc('increment_content_votes', {'p_content_id': content_id}).execute()

        self.supabase.table('users').update({
            'daily_votes_used': daily_votes_used + 1
        }).eq('user_id', user_id).execute()

        return {"status": "success", "message": "Voto registrato con successo!"}

class ContestManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    CONTEST_REWARD_POOLS = {
        SubscriptionPlan.FREE: 10.00,
        SubscriptionPlan.PREMIUM: 30.00,
        SubscriptionPlan.ASSISTANT: 60.00
    }

    def get_current_contest(self, user_plan: SubscriptionPlan):
        now = datetime.now(timezone.utc)
        response = self.supabase.table('contests').select('*') \
            .lte('start_date', now.isoformat()) \
            .gte('end_date', now.isoformat()) \
            .contains('min_plan_access', [user_plan.value]) \
            .order('end_date', desc=False) \
            .limit(1).maybe_single().execute()
        
        if response.data:
            response.data['reward_pool_euro'] = self.CONTEST_REWARD_POOLS.get(user_plan, 0.00)
            return response.data
        return None

    def get_leaderboard(self):
        response = self.supabase.table('users').select('display_name, avatar_url, points_balance').order('points_balance', desc=True).limit(100).execute()
        return response.data

class ShopManager:
    def __init__(self, supabase: Client): self.supabase = supabase

    def get_shop_items(self):
        response = self.supabase.table('shop_items').select('*').order('price_points', desc=False).execute()
        return response.data

    async def buy_item(self, req: ShopBuyRequest):
        user_profile = UserManager(self.supabase).get_user_profile(req.user_id)
        item_res = self.supabase.table('shop_items').select('*').eq('id', req.item_id).maybe_single().execute()
        
        if not item_res.data: raise HTTPException(status_code=404, detail="Item not found.")
        item = item_res.data

        if req.payment_method == 'points':
            if user_profile['points_balance'] < item['price_points']:
                raise HTTPException(status_code=402, detail="Punti insufficienti per l'acquisto.")
            
            self.supabase.rpc('deduct_points', {
                'p_user_id': req.user_id,
                'p_amount': item['price_points'],
                'p_reason': f'Shop Purchase: {item["name"]} (Points)'
            }).execute()

            await self._apply_item_effect(req.user_id, item, req.payment_method, item['price_points'], None)

            return {"status": "success", "message": f"Acquisto di '{item['name']}' completato con successo con i punti!"}
        
        elif req.payment_method == 'stripe':
            if not STRIPE_SECRET_KEY: raise HTTPException(status_code=500, detail="Stripe not configured.")
            if not item['price_eur']: raise HTTPException(status_code=400, detail="Questo articolo non ha un prezzo in EUR definito.")

            try:
                payment_intent = stripe.PaymentIntent.create(
                    amount=int(item['price_eur'] * 100),
                    currency='eur',
                    metadata={'user_id': req.user_id, 'item_id': item['id'], 'item_name': item['name']},
                    automatic_payment_methods={'enabled': True}
                )
                return {"payment_required": True, "client_secret": payment_intent.client_secret, "message": "Procedi al pagamento Stripe."}

            except stripe.error.StripeError as e:
                raise HTTPException(status_code=500, detail=f"Errore Stripe: {e.user_message}")
            except Exception as e:
                raise HTTPException(status_code=500, detail=f"Errore nella creazione del Payment Intent: {e}")

    async def _apply_item_effect(self, user_id: str, item: dict, payment_method: str, amount_points: float | None, amount_eur: float | None):
        if item['item_type'] == ItemType.BOOST.value:
            print(f"Applicato boost '{item['name']}' all'utente {user_id}. Effetto: {item.get('effect')}")
        
        elif item['item_type'] == ItemType.COSMETIC.value:
            print(f"Applicato cosmetico '{item['name']}' all'utente {user_id}. Effetto: {item.get('effect')}")
        
        elif item['item_type'] == ItemType.GENERATION_PACK.value:
            effect_data = json.loads(item.get('effect', '{}'))
            generations_to_add = effect_data.get('generations', 0)
            if generations_to_add > 0:
                user_res = self.supabase.table('users').select('daily_ai_generations_used').eq('user_id', user_id).maybe_single().execute()
                if user_res.data:
                    current_generations_used = user_res.data['daily_ai_generations_used']
                    self.supabase.table('users').update({
                        'daily_ai_generations_used': current_generations_used - generations_to_add
                    }).eq('user_id', user_id).execute()
                    print(f"Aggiunte {generations_to_add} generazioni AI all'utente {user_id}.")

        self.supabase.table('user_purchases').insert({
            'user_id': user_id,
            'item_id': item['id'],
            'purchase_date': datetime.now(timezone.utc).isoformat(),
            'payment_method': payment_method,
            'amount_paid_points': amount_points,
            'amount_paid_eur': amount_eur,
            'status': 'completed'
        }).execute()

def get_user_manager(supabase: Client = Depends(get_supabase_client)): return UserManager(supabase)
def get_ai_manager(supabase: Client = Depends(get_supabase_client)): return AIManager(supabase)
def get_contest_manager(supabase: Client = Depends(get_supabase_client)): return ContestManager(supabase)
def get_shop_manager(supabase: Client = Depends(get_supabase_client)): return ShopManager(supabase)

@app.get("/")
def read_root():
    return {"message": "Zenith Rewards Backend is operational. Access the API documentation at /docs."}

@app.post("/sync_user")
def sync_user_endpoint(user_data: UserSyncRequest, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.sync_user(user_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error during user sync: {str(e)}")

@app.post("/update_profile/{user_id}")
def update_profile_endpoint(user_id: str, profile_data: UserProfileUpdate, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.update_profile(user_id, profile_data)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.post("/request_payout")
def request_payout_endpoint(payout_data: PayoutRequest, user_manager: UserManager = Depends(get_user_manager)):
    try:
        supabase = get_supabase_client()
        value_eur = payout_data.points_amount / POINTS_TO_EUR_RATE
        result = supabase.rpc('request_payout_function', { 
            'p_user_id': payout_data.user_id, 
            'p_points_amount': payout_data.points_amount, 
            'p_value_in_eur': value_eur, 
            'p_method': payout_data.method, 
            'p_address': payout_data.address 
        }).execute()
        
        if result.data and isinstance(result.data, list) and len(result.data) > 0:
            return {"status": "success", "message": result.data[0].get('message', "Your payout request has been sent and will be processed soon!")}
        
        raise HTTPException(status_code=400, detail="Failed to process payout request. Check database function logs.")

    except Exception as e:
        if 'Punti insufficienti' in str(e):
            raise HTTPException(status_code=402, detail="Punti insufficienti per il prelievo.")
        raise HTTPException(status_code=500, detail=f"Error processing payout request: {str(e)}")

@app.get("/users/{user_id}/profile")
def get_user_profile_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_user_profile(user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching user profile: {e}")

@app.get("/get_user_balance/{user_id}")
def get_user_balance_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_user_balance(user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching user balance: {e}")

@app.get("/streak/status/{user_id}")
def get_streak_status_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_streak_status(user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching streak status: {e}")

@app.post("/streak/claim/{user_id}")
def claim_streak_reward_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.claim_streak_reward(user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error claiming streak reward: {e}")

@app.get("/leaderboard")
def get_leaderboard_endpoint(contest_manager: ContestManager = Depends(get_contest_manager)):
    try:
        return contest_manager.get_leaderboard()
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching leaderboard: {e}")

@app.get("/referral_stats/{user_id}")
def get_referral_stats_endpoint(user_id: str, user_manager: UserManager = Depends(get_user_manager)):
    try:
        return user_manager.get_referral_stats(user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching referral stats: {e}")

@app.post("/ai/generate-advice")
async def generate_advice_endpoint(req: AIAdviceRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    return await ai_manager.generate_advice(req)

@app.post("/ai/generate")
async def generate_content_endpoint(req: AIGenerationRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    return await ai_manager.generate_content(req)

@app.post("/ai/content/{ai_content_id}/publish")
def publish_content_endpoint(ai_content_id: int, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return ai_manager.publish_ai_content(ai_content_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error publishing content: {e}")

@app.get("/ai/content/feed")
async def get_content_feed_endpoint(ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.get_feed()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching content feed: {e}")

@app.post("/ai/content/{content_id}/vote")
async def vote_content_endpoint(content_id: int, req: VoteContentRequest, ai_manager: AIManager = Depends(get_ai_manager)):
    try:
        return await ai_manager.vote_content(content_id, req.user_id)
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error voting content: {e}")

@app.get("/contests/current/{user_id}")
def get_current_contest_endpoint(user_id: str, contest_manager: ContestManager = Depends(get_contest_manager), user_manager: UserManager = Depends(get_user_manager)):
    try:
        user_profile = user_manager.get_user_profile(user_id)
        user_plan = SubscriptionPlan(user_profile.get('subscription_plan', SubscriptionPlan.FREE.value))
        contest = contest_manager.get_current_contest(user_plan)
        if not contest:
            raise HTTPException(status_code=404, detail="Nessun contest attivo disponibile per il tuo piano al momento.")
        return contest
    except HTTPException as e: raise e
    except Exception as e: raise HTTPException(status_code=500, detail=f"Error fetching current contest: {e}")

@app.get("/shop/items")
def get_shop_items_endpoint(shop_manager: ShopManager = Depends(get_shop_manager)):
    try:
        return shop_manager.get_shop_items()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error fetching shop items: {e}")

@app.post("/shop/buy")
async def buy_shop_item_endpoint(req: ShopBuyRequest, shop_manager: ShopManager = Depends(get_shop_manager)):
    return await shop_manager.buy_item(req)

@app.post("/create-checkout-session")
def create_checkout_session_endpoint(req: CreateSubscriptionRequest, supabase: Client = Depends(get_supabase_client)):
    if not stripe.api_key: raise HTTPException(status_code=500, detail="Stripe not configured.")
    price_map = {
        'premium': STRIPE_PRICE_ID_PREMIUM,
        'assistant': STRIPE_PRICE_ID_ASSISTANT
    }
    price_id = price_map.get(req.plan_type)
    if not price_id: raise HTTPException(status_code=400, detail="Invalid plan type specified.")
    
    try:
        user_res = supabase.table('users').select('email, stripe_customer_id').eq('user_id', req.user_id).maybe_single().execute()
        if not user_res.data: raise HTTPException(status_code=404, detail="User not found.")
        
        user_data = user_res.data
        customer_id = user_data.get('stripe_customer_id')
        
        if not customer_id:
            customer = stripe.Customer.create(
                email=user_data.get('email'),
                metadata={'user_id': req.user_id}
            )
            customer_id = customer.id
            supabase.table('users').update({'stripe_customer_id': customer_id}).eq('user_id', req.user_id).execute()
        
        checkout_session = stripe.checkout.Session.create(
            customer=customer_id,
            line_items=[{'price': price_id, 'quantity': 1}],
            mode='subscription',
            success_url=req.success_url,
            cancel_url=req.cancel_url,
            metadata={
                'user_id': req.user_id,
                'plan_type': req.plan_type
            }
        )
        return {"url": checkout_session.url}
    except stripe.error.StripeError as e:
        raise HTTPException(status_code=500, detail=f"Errore Stripe: {e.user_message}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Errore nella creazione della sessione di checkout: {e}")

@app.post("/stripe-webhook")
async def stripe_webhook(request: Request, supabase: Client = Depends(get_supabase_client)):
    payload = await request.body()
    sig_header = request.headers.get('stripe-signature')

    if not STRIPE_WEBHOOK_SECRET:
        raise HTTPException(status_code=500, detail="Stripe webhook secret not configured.")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, STRIPE_WEBHOOK_SECRET)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.error.SignatureVerificationError as e:
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Webhook error: {e}")

    event_type = event['type']
    data_object = event['data']['object']

    if event_type == 'customer.subscription.created' or event_type == 'customer.subscription.updated':
        subscription = data_object
        customer_id = subscription.get('customer')
        price_id = subscription['items']['data'][0]['price']['id']
        status = subscription.get('status')
        
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()
        
        if user_res.data:
            user_id = user_res.data['user_id']
            new_plan = SubscriptionPlan.FREE.value
            
            if price_id == STRIPE_PRICE_ID_PREMIUM:
                new_plan = SubscriptionPlan.PREMIUM.value
            elif price_id == STRIPE_PRICE_ID_ASSISTANT:
                new_plan = SubscriptionPlan.ASSISTANT.value
            
            if status in ['active', 'trialing']:
                supabase.table('users').update({'subscription_plan': new_plan}).eq('user_id', user_id).execute()
            else:
                supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_id).execute()

    elif event_type == 'customer.subscription.deleted':
        customer_id = data_object.get('customer')
        user_res = supabase.table('users').select('user_id').eq('stripe_customer_id', customer_id).maybe_single().execute()
        if user_res.data:
            supabase.table('users').update({'subscription_plan': SubscriptionPlan.FREE.value}).eq('user_id', user_res.data['user_id']).execute()
            
    elif event_type == 'payment_intent.succeeded':
        payment_intent = data_object
        user_id = payment_intent['metadata'].get('user_id')
        item_id = payment_intent['metadata'].get('item_id')
        
        if user_id and item_id:
            item_res = supabase.table('shop_items').select('*').eq('id', int(item_id)).maybe_single().execute()
            if item_res.data:
                item = item_res.data
                shop_manager = ShopManager(supabase)
                # Passa i dettagli del pagamento a _apply_item_effect per registrarli
                await shop_manager._apply_item_effect(user_id, item, 'stripe', None, item['price_eur'])
            else:
                print(f"Warning: Item {item_id} not found for successful payment intent for user {user_id}.")

    return Response(status_code=200)