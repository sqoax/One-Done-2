import os, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks  # <-- added tasks

import gspread
from oauth2client.service_account import ServiceAccountCredentials

TOKEN = os.getenv("DISCORD_TOKEN")
REVEAL_CHANNEL_ID = int(os.getenv("REVEAL_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SHEET_ID = os.getenv("SHEET_ID", "")

EASTERN = ZoneInfo("America/New_York")
MAIN_GUILD_ID = None  # set at startup

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

# ---------- Google Sheets helpers ----------
_HEADERS = ["guild_id", "user_id", "name", "pick", "ts_utc"]

def _sheet():
    google_creds = os.getenv("GOOGLE_CREDS")
    if not google_creds or not SHEET_ID:
        raise RuntimeError("Set GOOGLE_CREDS and SHEET_ID env vars in Render.")
    scope = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(google_creds), scope)
    client = gspread.authorize(creds)
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Picks")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Picks", rows=1000, cols=len(_HEADERS))
    # ensure header row
    existing = ws.row_values(1)
    if existing != _HEADERS:
        ws.update("A1", [_HEADERS])
    return ws

def _get_main_guild(bot):
    global MAIN_GUILD_ID
    if MAIN_GUILD_ID:
        g = bot.get_guild(MAIN_GUILD_ID)
        if g:
            return g
    ch = bot.get_channel(REVEAL_CHANNEL_ID)
    if ch is None:
        ch = bot.loop.run_until_complete(bot.fetch_channel(REVEAL_CHANNEL_ID))
    MAIN_GUILD_ID = ch.guild.id
    return ch.guild

def _fmt_time_12h(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(EASTERN)
    return f"{local.strftime('%a %I:%M %p').lstrip('0')}"

async def _announce_channel(bot: commands.Bot):
    g = _get_main_guild(bot)
    gen = discord.utils.get(g.text_channels, name="general")
    if gen:
        return gen
    if g.system_channel:
        return g.system_channel
    return bot.get_channel(REVEAL_CHANNEL_ID)

# data access
def save_pick_to_sheet(guild_id: int, user_id: int, name: str, pick: str, ts_utc_iso: str):
    ws = _sheet()
    ws.append_row([str(guild_id), str(user_id), name, pick, ts_utc_iso], value_input_option="RAW")

def load_latest_picks(guild_id: int):
    """Return dict keyed by user_id with latest pick only."""
    ws = _sheet()
    records = ws.get_all_records()  # list of dicts, skips header
    latest = {}
    for r in records:
        if str(r.get("guild_id")) != str(guild_id):
            continue
        uid = str(r.get("user_id"))
        ts = r.get("ts_utc") or ""
        if uid not in latest or ts > latest[uid]["ts_utc"]:
            latest[uid] = {"name": r.get("name", ""), "pick": r.get("pick", ""), "ts_utc": ts}
    return latest

def clear_guild_picks(guild_id: int):
    ws = _sheet()
    values = ws.get_all_values()
    if not values:
        ws.update("A1", [_HEADERS])
        return
    header, rows = values[0], values[1:]
    keep = [row for row in rows if row and row[0] != str(guild_id)]
    ws.clear()
    ws.update("A1", [header] + keep if keep else [header])

# ---------- Discord ----------
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents, help_command=None)

# --- helper that actually performs the reveal & wipe ---
async def do_reveal() -> bool:
    """Send the compiled picks to the reveal channel and clear them. Returns True if something was posted."""
    # get channel & guild safely
    channel = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    guild_id = channel.guild.id

    latest = load_latest_picks(guild_id)
    if not latest:
        try:
            await channel.send("⚠️ No picks were submitted.")
        except discord.Forbidden:
            pass
        return False

    lines = ["**📣 This Week’s Picks:**"]
    for rec in latest.values():
        ts = datetime.fromisoformat(rec["ts_utc"])
        lines.append(f"- **{rec['name']}**: {rec['pick']} *(submitted {_fmt_time_12h(ts)} ET)*")

    try:
        await channel.send("\n".join(lines))
    except discord.Forbidden:
        return False

    clear_guild_picks(guild_id)
    return True

@bot.event
async def on_ready():
    # set MAIN_GUILD_ID without using run_until_complete
    channel = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    global MAIN_GUILD_ID
    MAIN_GUILD_ID = channel.guild.id
    print(f"✅ Logged in as {bot.user} | Main guild: {channel.guild.name} ({channel.guild.id})")

    if not auto_reveal_task.is_running():
        auto_reveal_task.start()

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

# ---------- !pick (DM friendly) ----------
@bot.command()
async def pick(ctx, *, golfer: str):
    g = _get_main_guild(bot)
    now_utc = datetime.now(timezone.utc)
    save_pick_to_sheet(g.id, ctx.author.id, ctx.author.display_name, golfer.strip(), now_utc.isoformat())
    await ctx.send(f"✅ Pick saved for **{golfer.strip()}**")

    ch = await _announce_channel(bot)
    if ch and ch.permissions_for(ch.guild.me).send_messages:
        try:
            await ch.send(f"📝 **{ctx.author.display_name}** just submitted a pick.")
        except discord.Forbidden:
            pass

# ---------- !submits (DM friendly, times only) ----------
@bot.command()
async def submits(ctx):
    g = _get_main_guild(bot)
    latest = load_latest_picks(g.id)
    if not latest:
        await ctx.send("📭 No picks submitted yet.")
        return
    lines = ["**🕒 Pick Submission Times**"]
    for rec in latest.values():
        ts = datetime.fromisoformat(rec["ts_utc"])
        lines.append(f"- **{rec['name']}** at `{_fmt_time_12h(ts)} ET`")
    await ctx.send("\n".join(lines))

# ---------- !revealnow (DM friendly, owner only) ----------
@bot.command()
async def revealnow(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ Not authorized.")
        return
    ok = await do_reveal()
    await ctx.send("✅ Revealed and cleared." if ok else "⚠️ No picks were submitted.")

# ---------- scheduled auto reveal at Wed 9:00 PM ET ----------
@tasks.loop(minutes=1)
async def auto_reveal_task():
    now = datetime.now(EASTERN)
    if now.strftime("%A") == "Wednesday" and now.strftime("%H:%M") == "21:00":
        try:
            await do_reveal()
        except Exception as e:
            print("Auto reveal failed:", e)

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
