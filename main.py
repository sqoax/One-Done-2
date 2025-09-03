import os, json, re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
from threading import Thread
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP

from flask import Flask
import discord
from discord.ext import commands, tasks
from discord.ext.commands import cooldown, BucketType

import gspread
from oauth2client.service_account import ServiceAccountCredentials

# ---- TEMP: use an alternate Discord API base to dodge Cloudflare block
import os, discord.http
discord.http.Route.BASE = os.getenv("DISCORD_API_BASE", "https://canary.discord.com/api/v10")

# ---------- Env & constants ----------
def _require_env(key: str) -> str:
    val = os.getenv(key)
    if not val:
        raise RuntimeError(f"Missing required env var: {key}")
    return val

TOKEN = _require_env("DISCORD_TOKEN")
REVEAL_CHANNEL_ID = int(os.getenv("REVEAL_CHANNEL_ID", "0"))
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
SHEET_ID = _require_env("SHEET_ID")

EASTERN = ZoneInfo("America/New_York")
MAIN_GUILD_ID = None  # set at runtime

# --- uptime tracking ---
START_TIME_UTC = datetime.now(timezone.utc)

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

# cache the client and the "Picks" worksheet to cut latency & quota
_gs_client = None
_ws_cache = None

def _gs_authorize():
    global _gs_client
    if _gs_client:
        return _gs_client
    google_creds = os.getenv("GOOGLE_CREDS")
    if not google_creds:
        raise RuntimeError("Set GOOGLE_CREDS env var in Render.")
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_dict(json.loads(google_creds), scope)
    _gs_client = gspread.authorize(creds)
    return _gs_client

def _sheet():
    global _ws_cache
    if _ws_cache:
        return _ws_cache
    client = _gs_authorize()
    sh = client.open_by_key(SHEET_ID)
    try:
        ws = sh.worksheet("Picks")
    except gspread.WorksheetNotFound:
        ws = sh.add_worksheet(title="Picks", rows=2000, cols=len(_HEADERS))
    # ensure header row
    existing = ws.row_values(1)
    if existing != _HEADERS:
        ws.update("A1", [_HEADERS])
    _ws_cache = ws
    return ws

# open a worksheet from any spreadsheet, used by !totals
def _open_ws(sheet_id: str, tab_title: str):
    client = _gs_authorize()
    return client.open_by_key(sheet_id).worksheet(tab_title)

# ---------- Guild & time helpers ----------
async def _get_main_guild(bot: commands.Bot):
    global MAIN_GUILD_ID
    if MAIN_GUILD_ID:
        g = bot.get_guild(MAIN_GUILD_ID)
        if g:
            return g
    ch = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    MAIN_GUILD_ID = ch.guild.id
    return ch.guild

def _fmt_time_12h(dt_utc: datetime) -> str:
    local = dt_utc.astimezone(EASTERN)
    return f"{local.strftime('%a %I:%M %p').lstrip('0')}"

def _parse_iso(ts: str) -> datetime:
    # tolerant ISO parser without adding new deps
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(ts)
    except Exception:
        return datetime.now(timezone.utc)

def _fmt_duration(seconds: int) -> str:
    mins, secs = divmod(int(seconds), 60)
    hrs, mins = divmod(mins, 60)
    days, hrs = divmod(hrs, 24)
    parts = []
    if days: parts.append(f"{days}d")
    if days or hrs: parts.append(f"{hrs}h")
    if days or hrs or mins: parts.append(f"{mins}m")
    parts.append(f"{secs}s")
    return " ".join(parts)

async def _announce_channel(bot: commands.Bot):
    g = await _get_main_guild(bot)
    gen = discord.utils.get(g.text_channels, name="general")
    if gen:
        return gen
    if g.system_channel:
        return g.system_channel
    return bot.get_channel(REVEAL_CHANNEL_ID)

def _ctx_guild(ctx) -> discord.Guild | None:
    return getattr(ctx.channel, "guild", None)

# ---------- data access ----------
def save_pick_to_sheet(guild_id: int, user_id: int, name: str, pick: str, ts_utc_iso: str):
    try:
        ws = _sheet()
        ws.append_row([str(guild_id), str(user_id), name, pick, ts_utc_iso], value_input_option="RAW")
    except gspread.exceptions.APIError as e:
        raise RuntimeError("Google Sheets quota or permission error") from e

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

# --- helper that posts and clears, used by scheduler and !revealnow
async def _do_auto_reveal():
    channel = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
    guild = getattr(channel, "guild", None)
    if not guild:
        return False
    guild_id = guild.id

    latest = load_latest_picks(guild_id)
    if not latest:
        try:
            await channel.send("⚠️ No picks were submitted.")
        except discord.Forbidden:
            pass
        return False

    lines = ["**📣 This Week’s Picks:**"]
    for rec in latest.values():
        ts = _parse_iso(rec["ts_utc"])
        lines.append(f"- **{rec['name']}**: {rec['pick']} *(submitted {_fmt_time_12h(ts)} ET)*")

    try:
        await channel.send("\n".join(lines))
    except discord.Forbidden:
        return False

    clear_guild_picks(guild_id)
    return True

@bot.event
async def on_ready():
    g = await _get_main_guild(bot)
    print(f"✅ Logged in as {bot.user} | Main guild: {g.name} ({g.id})")
    # quick self-test of Sheets header, non-fatal
    try:
        _ = _sheet().row_values(1)
    except Exception as e:
        print("Sheets self-test failed:", type(e).__name__, e)
    if not auto_reveal_task.is_running():
        auto_reveal_task.start()

@bot.event
async def on_command_error(ctx, error):
    from discord.ext.commands import CommandOnCooldown
    if isinstance(error, CommandOnCooldown):
        await ctx.send(f"⏳ Slow down: try again in {error.retry_after:.1f}s.")
        return
    await ctx.send(f"⚠️ Error: {type(error).__name__}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

@bot.command()
@cooldown(1, 10, BucketType.user)  # prevent spam and double-submits
async def pick(ctx, *, golfer: str):
    # Allow in DMs and in servers, always persist to the main guild
    golfer = " ".join(golfer.split())
    if not (1 <= len(golfer) <= 64):
        await ctx.send("❌ Golfer name must be 1–64 characters.")
        return

    g = await _get_main_guild(bot)
    now_utc = datetime.now(timezone.utc)
    try:
        save_pick_to_sheet(g.id, ctx.author.id, ctx.author.display_name, golfer, now_utc.isoformat())
    except Exception as e:
        await ctx.send(f"❌ Could not save pick ({type(e).__name__}). Try again.")
        return

    await ctx.send(f"✅ Pick saved for **{golfer}**")

    # Optional server announcement
    ch = await _announce_channel(bot)
    if ch and ch.permissions_for(ch.guild.me).send_messages:
        try:
            await ch.send(f"📝 **{ctx.author.display_name}** just submitted a pick.")
        except discord.Forbidden:
            pass

@bot.command()
async def submits(ctx):
    g = await _get_main_guild(bot)
    latest = load_latest_picks(g.id)
    if not latest:
        await ctx.send("📭 No picks submitted yet.")
        return
    lines = ["**🕒 Pick Submission Times**"]
    for rec in latest.values():
        ts = _parse_iso(rec["ts_utc"])
        lines.append(f"- **{rec['name']}** at {_fmt_time_12h(ts)} ET")
    await ctx.send("\n".join(lines))

@bot.command()
async def totals(ctx):
    sid = os.getenv("TOTALS_SHEET_ID") or os.getenv("SHEET_ID")
    tab = os.getenv("TOTALS_TAB", "Sheet1")
    try:
        ws = _open_ws(sid, tab)
        # batch cells to reduce round-trips
        (leader,), (lead_by,), (hiatt,), (caden,), (bennett,) = ws.batch_get(["O2","O3","O6","O7","O8"])
    except gspread.SpreadsheetNotFound:
        await ctx.send("❌ Can't open totals spreadsheet.")
        return
    except gspread.WorksheetNotFound:
        await ctx.send(f"❌ Can't find tab `{tab}`.")
        return
    except Exception as e:
        await ctx.send(f"❌ Sheets error: {type(e).__name__}")
        return
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

# ---------- Scheduler with 'already ran' latch ----------
_last_reveal_date = None

@tasks.loop(minutes=1)
async def auto_reveal_task():
    global _last_reveal_date
    now = datetime.now(EASTERN)
    # Wednesday 21:00 ET, fire once per date
    if now.strftime("%A") == "Wednesday" and now.strftime("%H:%M") == "21:00":
        if _last_reveal_date == now.date():
            return
        try:
            ok = await _do_auto_reveal()
            print(f"[auto_reveal] {now.isoformat()} -> {'posted' if ok else 'no picks'}")
        except Exception as e:
            print("Auto reveal failed:", type(e).__name__, e)
        finally:
            _last_reveal_date = now.date()

# ---------- Odds allocation command ----------
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

# ---------- New: uptime & health ----------
@bot.command()
async def uptime(ctx):
    now = datetime.now(timezone.utc)
    delta = (now - START_TIME_UTC).total_seconds()
    start_local = START_TIME_UTC.astimezone(EASTERN)
    await ctx.send(
        f"⏱️ Uptime: **{_fmt_duration(delta)}**\n"
        f"Started: {start_local.strftime('%a %b %d, %I:%M %p').lstrip('0')} ET"
    )

@bot.command()
async def health(ctx):
    # Discord latency
    latency_ms = int(bot.latency * 1000) if bot.latency is not None else -1

    # Reveal channel and perms
    try:
        ch = bot.get_channel(REVEAL_CHANNEL_ID) or await bot.fetch_channel(REVEAL_CHANNEL_ID)
        guild_ok = ch is not None and hasattr(ch, "guild") and ch.guild is not None
        can_send = ch.permissions_for(ch.guild.me).send_messages if guild_ok else False
        reveal_status = "✅" if guild_ok and can_send else "⚠️" if guild_ok else "❌"
    except discord.Forbidden:
        ch = None
        reveal_status = "❌"
        can_send = False

    # Google Sheets
    sheets_ok = False
    picks_header_ok = False
    sheets_msg = ""
    try:
        ws = _sheet()
        sheets_ok = True
        try:
            header = ws.row_values(1)
            picks_header_ok = (header == _HEADERS)
        except Exception as e:
            sheets_msg = f"Header read error: {type(e).__name__}"
    except Exception as e:
        sheets_msg = f"{type(e).__name__}: {e}"

    # Scheduler
    scheduler_running = auto_reveal_task.is_running()

    lines = [
        "**🩺 Bot Health Check**",
        f"- Discord latency: **{latency_ms} ms**",
        f"- Reveal channel ({REVEAL_CHANNEL_ID}): {reveal_status}"
            + (" (cannot send)" if ch and not can_send else "")
            + ("" if ch else " (not found)"),
        f"- Sheets connection: {'✅' if sheets_ok else '❌'}",
        f"- Picks header row: {'✅' if picks_header_ok else '❌'}",
        f"- Scheduler running: {'✅' if scheduler_running else '❌'}",
    ]
    if sheets_msg:
        lines.append(f"- Sheets note: `{sheets_msg}`")

    await ctx.send("\n".join(lines))

# ---------- Entrypoint ----------
if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
