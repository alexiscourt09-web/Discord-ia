import asyncio
import os

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN = os.environ["DISCORD_TOKEN"]
CHANNEL_ID = int(os.environ["CHANNEL_ID"])
GEMINI_KEY = os.environ.get("GEMINI_API_KEY")
OPENROUTER_KEY = os.environ.get("OPENROUTER_API_KEY")

# ---------------------------------------------------------------
# CONFIG : modifie / ajoute tes IA ici
# provider = "gemini" ou "openrouter"
# Pour openrouter : "models" = liste, essayés dans l'ordre (fallback auto)
# "reasoning": True active le mode réflexion (plus lent, plus malin)
# ---------------------------------------------------------------
PERSONAS = [
    {
        "name": "Gemi",
        "provider": "gemini",
        "models": ["gemini-3.1-flash-lite", "gemini-2.5-flash", "gemini-3-flash-preview"],
        "instructions": "Tu es Gemi, curieux, enthousiaste et un peu blagueur. "
        "Tu réponds en 1 à 4 phrases max, en français.",
    },
    {
        "name": "Nemo",
        "provider": "openrouter",
        "models": [
            "nvidia/nemotron-3-ultra-550b-a55b:free",
            "nvidia/nemotron-3-super-120b-a12b:free",
            "inclusionai/ling-2.6-1t:free",
            "minimax/minimax-m2.5:free",
            "openrouter/free",
        ],
        "reasoning": True,
        "instructions": "Tu es Nemo, analytique et rigoureux. Tu creuses les "
        "problèmes complexes et tu n'hésites pas à contredire les autres si "
        "leur raisonnement est faux. 1 à 4 phrases max, en français.",
    },
]

MAX_TURNS = 6        # nb de réponses IA après chaque message humain
HISTORY_SIZE = 25    # nb de messages lus pour le contexte
DELAY = 2            # secondes entre deux réponses
# ---------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

session: aiohttp.ClientSession | None = None
webhook = None
current_task: asyncio.Task | None = None
max_turns = MAX_TURNS


async def ask_gemini(p, system, prompt):
    last_error = None
    for model in p["models"]:
        url = (
            "https://generativelanguage.googleapis.com/v1beta/models/"
            f"{model}:generateContent?key={GEMINI_KEY}"
        )
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        }
        try:
            async with session.post(url, json=body) as r:
                data = await r.json()
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(x.get("text", "") for x in parts if not x.get("thought")).strip()
            if text:
                return text
            last_error = f"{model}: réponse vide {data}"
        except Exception as e:
            last_error = f"{model}: {e!r} | {str(data)[:400] if 'data' in locals() else ''}"
        print(f"[{p['name']}] fallback -> {last_error}")
    raise RuntimeError(f"Tous les modèles Gemini ont échoué ({last_error})")


async def ask_openrouter(p, system, prompt):
    headers = {"Authorization": f"Bearer {OPENROUTER_KEY}"}
    last_error = None
    for model in p["models"]:
        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 4000,
        }
        if p.get("reasoning"):
            body["reasoning"] = {"enabled": True}
        try:
            async with session.post(
                "https://openrouter.ai/api/v1/chat/completions",
                json=body,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=120),
            ) as r:
                data = await r.json()
            text = (data["choices"][0]["message"].get("content") or "").strip()
            if text:
                return text
            last_error = f"{model}: réponse vide"
        except Exception as e:
            last_error = f"{model}: {e} | {data if 'data' in locals() else ''}"
        print(f"[{p['name']}] fallback -> {last_error}")
    raise RuntimeError(f"Tous les modèles ont échoué ({last_error})")


async def generate(p, channel):
    lines = []
    async for m in channel.history(limit=HISTORY_SIZE):
        if m.content and not m.content.startswith("!"):
            lines.append(f"{m.author.display_name}: {m.content}")
    transcript = "\n".join(reversed(lines))

    others = ", ".join(x["name"] for x in PERSONAS if x is not p)
    system = (
        f"{p['instructions']}\n\nTu participes à une discussion Discord avec des "
        f"humains et d'autres IA ({others}). Tu es {p['name']}. "
        "Réponds uniquement avec ton message, sans préfixe de nom."
    )
    prompt = f"Historique de la discussion :\n{transcript}\n\nÀ toi de parler, {p['name']}."

    fn = ask_gemini if p["provider"] == "gemini" else ask_openrouter
    text = await fn(p, system, prompt)
    return text.strip()[:1900]


async def get_webhook(channel):
    global webhook
    if webhook is None:
        hooks = await channel.webhooks()
        webhook = next((h for h in hooks if h.name == "ai-chat"), None) or \
            await channel.create_webhook(name="ai-chat")
    return webhook


async def conversation(channel, last_speaker=None):
    idx = 0
    for _ in range(max_turns):
        candidates = [p for p in PERSONAS if p["name"] != last_speaker]
        p = candidates[idx % len(candidates)]
        idx += 1
        try:
            async with channel.typing():
                text = await generate(p, channel)
        except Exception as e:
            print(f"[{p['name']}] erreur: {e}")
            return
        try:
            hook = await get_webhook(channel)
            await hook.send(text, username=p["name"])
        except Exception as e:
            print(f"Erreur webhook (permission 'Gérer les webhooks' ?): {e!r}")
            return
        last_speaker = p["name"]
        await asyncio.sleep(DELAY)


@bot.event
async def on_ready():
    global session
    if session is None:
        session = aiohttp.ClientSession()
    print(f"Connecté : {bot.user}")


@bot.event
async def on_message(message: discord.Message):
    global current_task
    if message.channel.id != CHANNEL_ID or message.webhook_id or message.author.bot:
        return
    await bot.process_commands(message)
    if message.content.startswith("!"):
        return
    if current_task and not current_task.done():
        current_task.cancel()
    current_task = asyncio.create_task(conversation(message.channel))


@bot.command()
async def stop(ctx):
    """Arrête la discussion entre IA."""
    if current_task and not current_task.done():
        current_task.cancel()
    await ctx.send("⏹️ Discussion arrêtée.")


@bot.command()
async def turns(ctx, n: int):
    """Change le nombre de réponses IA par message."""
    global max_turns
    max_turns = max(1, min(n, 30))
    await ctx.send(f"🔁 {max_turns} réponses IA par message.")


bot.run(DISCORD_TOKEN)
