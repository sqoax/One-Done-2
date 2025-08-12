import os, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands

TOKEN = os.getenv("DISCORD_TOKEN")
REVEAL_CHANNEL_ID = int(os.getenv("REVEAL_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

EASTERN = ZoneInfo("America/New_York")
DATA_FILE = "picks.json"
MAIN_GUILD_ID = None  # set on_ready from reveal channel's guild

# ---------- tiny JSON store ----------
def _load_all():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_all(d):
    with open(DATA_FILE, "w") as f:
        json.dump(d, f, indent=2)

def _guild_picks(gid: int):
    data = _load_all()
    gkey = str(gid)
    if gkey not in data:
        data[gkey] = {}
    return data, data[gkey]

# ---------- Flask keep-alive ----------
app = Flask(__name__)
@app.route("/")
def home():
    return "✅ Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run, daemon=True).start()

# ---------- Discord ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

def _fmt_time_12h(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(EASTERN)
    return f"{local.strftime('%a %I:%M %p').lstrip('0')}"

async def _get_main_guild():
    global MAIN_GUILD_ID
    if MAIN_GUILD_ID:
        g = bot.get_guild(MAIN_GUILD_ID)
        if g:
            return g
    ch = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    MAIN_GUILD_ID = ch.guild.id
    return ch.guild

async def _announce_channel():
    g = await _get_main_guild()
    # prefer #general, else system channel, else the reveal channel
    gen = discord.utils.get(g.text_channels, name="general")
    if gen:
        return gen
    if g.system_channel:
        return g.system_channel
    return bot.get_channel(REVEAL_CHANNEL_ID)

@bot.event
async def on_ready():
    g = await _get_main_guild()
    print(f"✅ Logged in as {bot.user} | Main guild: {g.name} ({g.id})")

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

# ---------- !pick (DM friendly) ----------
@bot.command()
async def pick(ctx, *, golfer: str):
    # accept from anywhere, prefer DM
    g = await _get_main_guild()
    data, picks = _guild_picks(g.id)

    now_utc = datetime.now(timezone.utc)
    picks[str(ctx.author.id)] = {
        "name": ctx.author.display_name,
        "pick": golfer.strip(),
        "ts": now_utc.isoformat(),
    }
    _save_all(data)

    await ctx.send(f"✅ Pick saved for **{golfer.strip()}**")
    ch = await _announce_channel()
    if ch and ch.permissions_for(ch.guild.me).send_messages:
        await ch.send(f"📝 **{ctx.author.display_name}** just submitted a pick.")

# ---------- !submits (DM friendly, times only) ----------
@bot.command()
async def submits(ctx):
    g = await _get_main_guild()
    _, picks = _guild_picks(g.id)
    if not picks:
        await ctx.send("📭 No picks submitted yet.")
        return
    lines = ["**🕒 Pick Submission Times**"]
    for rec in picks.values():
        ts = datetime.fromisoformat(rec["ts"])
        lines.append(f"- **{rec['name']}** at `{_fmt_time_12h(ts)} ET`")
    await ctx.send("\n".join(lines))

# ---------- !revealnow (DM friendly, owner only) ----------
@bot.command()
async def revealnow(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ Not authorized.")
        return

    g = await _get_main_guild()
    channel = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    data, picks = _guild_picks(g.id)

    if not picks:
        await channel.send("⚠️ No picks were submitted.")
        return

    lines = ["**📣 This Week’s Picks:**"]
    for rec in picks.values():
        ts = datetime.fromisoformat(rec["ts"])
        lines.append(f"- **{rec['name']}**: {rec['pick']} *(submitted {_fmt_time_12h(ts)} ET)*")
    await channel.send("\n".join(lines))

    # clear after reveal
    data[str(g.id)] = {}
    _save_all(data)
    await ctx.send("✅ Revealed and cleared.")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
