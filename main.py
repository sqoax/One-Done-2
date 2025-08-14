import os, json
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread
from flask import Flask
import discord
from discord.ext import commands, tasks

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---- TEMP: use an alternate Discord API base to dodge Cloudflare block
import os, discord.http
discord.http.Route.BASE = os.getenv("DISCORD_API_BASE", "https://canary.discord.com/api/v10")

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

# ---- open a worksheet from any spreadsheet (used by !totals)
def _open_ws(sheet_id: str, tab_title: str):
    google_creds = os.getenv("GOOGLE_CREDS")
    scope = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(google_creds), scope)
    return gspread.authorize(creds).open_by_key(sheet_id).worksheet(tab_title)

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

# ---------- data access ----------
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
        # keep the newest
        if uid not in latest or ts > latest[uid]["ts_utc"]:
            latest[uid] = {
                "name": r.get("name", ""),
                "pick": r.get("pick", ""),
                "ts_utc": ts,
            }
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

# --- helper that posts and clears (used by scheduler & !revealnow)
async def _do_auto_reveal():
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
    g = _get_main_guild(bot)
    print(f"✅ Logged in as {bot.user} | Main guild: {g.name} ({g.id})")
    if not auto_reveal_task.is_running():
        auto_reveal_task.start()

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

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
        lines.append(f"- **{rec['name']}** at {_fmt_time_12h(ts)} ET")
    await ctx.send("\n".join(lines))

@bot.command()
async def totals(ctx):
    sid = os.getenv("TOTALS_SHEET_ID") or os.getenv("SHEET_ID")
    tab = os.getenv("TOTALS_TAB", "Sheet1")
    try:
        ws = _open_ws(sid, tab)
    except gspread.SpreadsheetNotFound:
        await ctx.send("❌ Can't open totals spreadsheet.")
        return
    except gspread.WorksheetNotFound:
        await ctx.send(f"❌ Can't find tab `{tab}`.")
        return
    hiatt   = ws.acell("O6").value
    caden   = ws.acell("O7").value
    bennett = ws.acell("O8").value
    leader  = ws.acell("O2").value
    lead_by = ws.acell("O3").value
    msg = (
        f"**💰 Current Totals**\n"
        f"Hiatt — {hiatt}\n"
        f"Caden — {caden}\n"
        f"Bennett — {bennett}"
    )
    if leader and lead_by:
        msg += f"\n\n🏆 **{leader}** is up by **{lead_by}**"
    await ctx.send(msg)

@bot.command()
async def revealnow(ctx):
    if ctx.author.id != OWNER_ID:
        await ctx.send("❌ Not authorized.")
        return
    ok = await _do_auto_reveal()
    await ctx.send("✅ Revealed and cleared." if ok else "⚠️ No picks were submitted.")

@tasks.loop(minutes=1)
async def auto_reveal_task():
    now = datetime.now(EASTERN)
    if now.strftime("%A") == "Wednesday" and now.strftime("%H:%M") == "21:00":
        try:
            await _do_auto_reveal()
        except Exception as e:
            print("Auto reveal failed:", type(e).__name__, e)

# ---------- !allocate command ----------
import re
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

def frac_to_decimal(frac_str: str) -> Decimal:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*/\s*(\d+(?:\.\d+)?)\s*", frac_str)
    if not m:
        raise ValueError(f"Bad fractional odds: {frac_str}")
    num = Decimal(m.group(1))
    den = Decimal(m.group(2))
    if den == 0:
        raise ValueError("Denominator cannot be zero")
    return (num / den) + Decimal("1")

def parse_header(line: str):
    m = re.search(r"!allocate\s+(\d+(?:\.\d+)?)\s*u\b\s+\$?\s*(\d+(?:\.\d+)?)", line, re.IGNORECASE)
    if not m:
        raise ValueError("Header must look like: !allocate 1u $10")
    units = Decimal(m.group(1))
    unit_value = Decimal(m.group(2))
    return units, unit_value

def parse_lines(lines):
    picks = []
    for ln in lines:
        if not ln.strip():
            continue
        m = re.search(r"(.*\S)\s+(\d+(?:\.\d+)?/\d+(?:\.\d+)?)\s*$", ln.strip())
        if not m:
            raise ValueError(f"Could not parse line: {ln}")
        name = m.group(1).strip()
        frac = m.group(2).strip()
        dec_odds = frac_to_decimal(frac)
        picks.append((name, frac, dec_odds))
    return picks

def equal_payout_stakes(total_stake: Decimal, dec_odds_list):
    inv_sum = sum((Decimal("1") / o for o in dec_odds_list), start=Decimal("0"))
    W = (total_stake / inv_sum)
    raw = [W / o for o in dec_odds_list]
    rounded = [r.quantize(Decimal("0.01"), rounding=ROUND_DOWN) for r in raw]
    diff = total_stake - sum(rounded)
    residuals = [(i, raw[i] - rounded[i]) for i in range(len(raw))]
    residuals.sort(key=lambda t: t[1], reverse=True)
    cents = int((diff * 100).to_integral_value(rounding=ROUND_HALF_UP))
    for i in range(cents):
        idx = residuals[i % len(raw)][0]
        rounded[idx] += Decimal("0.01")
    return W, rounded

@bot.command()
async def allocate(ctx):
    try:
        lines = ctx.message.content.splitlines()
        units, unit_value = parse_header(lines[0])
        picks = parse_lines(lines[1:])
        total_stake = (units * unit_value).quantize(Decimal("0.01"))
        decs = [p[2] for p in picks]
        W, stakes = equal_payout_stakes(total_stake, decs)
        units_each = [(s / unit_value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP) for s in stakes]
        table = ["Player | Odds | Units | Dollars", "------ | ---- | ----- | -------"]
        for (name, frac, _), s, u in zip(picks, stakes, units_each):
            table.append(f"{name} | {frac} | {u}u | ${s}")
        await ctx.reply(f"**Equal payout ≈ ${W.quantize(Decimal('0.01'))}**\n```text\n" + "\n".join(table) + "\n```")
    except Exception as e:
        await ctx.reply(f"Error: {e}")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
