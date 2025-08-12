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

# ---------- tiny file store ----------
def _load_all():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {}

def _save_all(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

def _guild_picks(guild_id):
    data = _load_all()
    gkey = str(guild_id)
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
    # example: Tue 7:05 PM
    return f"{local.strftime('%a %I:%M %p').lstrip('0')}"

def _announce_channel(guild: discord.Guild):
    # prefer a channel literally named "general"
    ch = discord.utils.get(guild.text_channels, name="general")
    return ch or (guild.system_channel if guild and guild.system_channel else None)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

# ---------- !pick ----------
@bot.command()
async def pick(ctx, *, golfer: str):
    if ctx.guild is None:
        await ctx.send("Please submit your pick in the server, not in DMs.")
        return
    data, picks = _guild_picks(ctx.guild.id)
    now_utc = datetime.now(timezone.utc)
    picks[str(ctx.author.id)] = {
        "name": ctx.author.display_name,
        "pick": golfer.strip(),
        "ts": now_utc.isoformat(),  # store in UTC
    }
    _save_all(data)

    await ctx.reply(f"✅ Pick saved for **{golfer.strip()}**")
    # announce in #general if it exists
    gen = _announce_channel(ctx.guild)
    if gen and gen.permissions_for(ctx.guild.me).send_messages:
        await gen.send(f"📝 **{ctx.author.display_name}** just submitted a pick.")

# ---------- !submits ----------
@bot.command()
async def submits(ctx):
    if ctx.guild is None:
        await ctx.send("Run this in the server.")
        return
    _, picks = _guild_picks(ctx.guild.id)
    if not picks:
        await ctx.send("📭 No picks submitted yet.")
        return
    lines = ["**🕒 Pick Submission Times**"]
    for uid, rec in picks.items():
        ts = datetime.fromisoformat(rec["ts"])
        lines.append(f"- **{rec['name']}** at `{_fmt_time_12h(ts)} ET`")
    await ctx.send("\n".join(lines))

# ---------- !revealnow ----------
@bot.command()
async def revealnow(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ Not authorized.")
        return
    if ctx.guild is None:
        await ctx.send("Run this in the server.")
        return
    channel = bot.get_channel(REVEAL_CHANNEL_ID)
    if channel is None:
        await ctx.send("❌ Reveal channel not found. Check REVEAL_CHANNEL_ID.")
        return

    data, picks = _guild_picks(ctx.guild.id)
    if not picks:
        await channel.send("⚠️ No picks were submitted.")
        return

    lines = ["**📣 This Week’s Picks:**"]
    for rec in picks.values():
        ts = datetime.fromisoformat(rec["ts"])
        lines.append(f"- **{rec['name']}**: {rec['pick']} *(submitted {_fmt_time_12h(ts)} ET)*")
    await channel.send("\n".join(lines))

    # clear after reveal, satisfies “stored until Wednesday 9 PM ET” when you trigger it
    data[str(ctx.guild.id)] = {}
    _save_all(data)
    await ctx.send("✅ Revealed and cleared.")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
