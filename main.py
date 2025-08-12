import os
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread

TOKEN = os.getenv("DISCORD_TOKEN")

# Flask keep-alive
app = Flask(__name__)
@app.route("/")
def home():
    return "✅ Bot is alive!"

def run():
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

def keep_alive():
    Thread(target=run).start()

# Discord bot
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"✅ Logged in as {bot.user}")

@bot.command()
async def ping(ctx):
    await ctx.send("pong 🏌️")

if __name__ == "__main__":
    keep_alive()
    bot.run(TOKEN)
