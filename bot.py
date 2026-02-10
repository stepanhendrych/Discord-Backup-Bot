#!/usr/bin/env python
# coding: utf-8

import inspect
import io
import json
import os
import re
import sys
import zipfile
from datetime import datetime
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from loguru import logger

# --- Třída pro správu zálohovacího souboru ---

class BackupManager:
    def __init__(self, raw_data, file_name):
        self.raw_data = raw_data
        self.file_name = file_name

    def create_zip_in_memory(self):
        """Vytvoří ZIP v paměti (RAM), aby se předešlo problémům s Windows file-lockem."""
        def default_serializer(obj):
            try:
                return str(obj)
            except Exception:
                return None

        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            # Převedeme data na JSON a zapíšeme přímo do ZIPu
            json_data = json.dumps(
                self.raw_data,
                default=default_serializer,
                ensure_ascii=False,
                indent=4
            ).encode('utf-8')
            
            # Soubor uvnitř zipu se bude jmenovat stejně jako zip, ale s příponou .json
            internal_name = Path(self.file_name).stem + ".json"
            zf.writestr(internal_name, json_data)
        
        zip_buffer.seek(0)
        return zip_buffer

    def save_locally(self, zip_data):
        """Uloží výsledný ZIP do složky 'backups' v adresáři bota."""
        backup_dir = Path("backups")
        backup_dir.mkdir(parents=True, exist_ok=True)
        
        file_path = backup_dir / self.file_name
        with open(file_path, "wb") as f:
            f.write(zip_data.getbuffer())
        
        return str(file_path.absolute())

# --- Pomocné funkce pro formátování a data ---

def update_embed(embed, cur_progress, total_channels, num_messages, message):
    embed.set_field_at(
        index=0,
        name="Number of backed up channels:",
        value=f"{cur_progress}/{total_channels}",
        inline=False,
    )
    embed.set_field_at(
        index=1,
        name="Number of backed up messages (total):",
        value=str(num_messages),
        inline=False,
    )
    embed.set_field_at(index=2, name="Latest update:", value=message, inline=False)
    return embed

def main():
    config = {
        "handlers": [
            {
                "sink": sys.stdout,
                "format": "{time:YYYY-MM-DD HH:mm:ss} | {level} | {message}",
            },
            {"sink": "logs.log", "serialize": True},
        ]
    }
    logger.configure(**config)

    load_dotenv()
    intents = discord.Intents.default()
    intents.message_content = True
    intents.members = True

    bot = commands.Bot(
        command_prefix="!",
        intents=intents,
        description="A Discord bot to automatically back up the server data locally.",
    )

    @bot.event
    async def on_ready():
        print(f"Logged in as {bot.user.name} ({bot.user.id})")
        print(f"Zálohy se budou ukládat do: {Path('backups').absolute()}")
        print("-" * 80)

    def get_guild_data(guild):
        guild_dict = {}
        # Atributy, které chceme ignorovat nebo zpracovat speciálně
        skip_attrs = ["members", "channels", "text_channels", "voice_channels", "categories"]
        
        for attr in dir(guild):
            if attr.startswith("_") or attr in skip_attrs or inspect.ismethod(getattr(guild, attr)):
                continue
            
            val = getattr(guild, attr)
            # Převod na string pro JSON kompatibilitu u speciálních typů
            if isinstance(val, (datetime, discord.Asset, discord.Colour)):
                guild_dict[attr] = str(val)
            else:
                guild_dict[attr] = val
        return guild_dict

    async def get_members_data(guild):
        members_dict = {}
        async for member in guild.fetch_members():
            members_dict[member.id] = {
                "name": member.name,
                "display_name": member.display_name,
                "roles": [role.name for role in member.roles],
                "joined_at": str(member.joined_at),
                "bot": member.bot
            }
        return members_dict

    async def backup_channel_history(channel):
        history = []
        async for msg in channel.history(limit=None):
            m_data = {
                "id": msg.id,
                "content": msg.content,
                "author": {
                    "name": msg.author.name,
                    "id": msg.author.id,
                    "bot": msg.author.bot
                },
                "created_at": msg.created_at.isoformat(),
                "attachments": [a.url for a in msg.attachments],
                "embeds": [e.to_dict() for e in msg.embeds],
                "reactions": [
                    {"emoji": str(r.emoji), "count": r.count} 
                    for r in msg.reactions
                ],
                "pinned": msg.pinned,
                "type": str(msg.type)
            }
            history.append(m_data)
        return history

    @bot.command()
    @commands.has_permissions(administrator=True)
    async def backup(ctx, arg=None):
        logger.info(f"Backup requested by {ctx.author} in {ctx.guild.name}")

        if not arg:
            await ctx.send("❌ Použití: `!backup all` nebo `!backup <id_kanalu>`")
            return

        target_channels = []
        if arg == "all":
            target_channels = ctx.guild.text_channels
        else:
            channel_id = int(re.sub(r"\D", "", arg))
            chan = ctx.guild.get_channel(channel_id)
            if chan: target_channels = [chan]

        if not target_channels:
            await ctx.send("❌ Kanál nenalezen.")
            return

        # Inicializace Embedu
        embed = discord.Embed(title="Zálohování spuštěno", color=discord.Color.blue())
        embed.add_field(name="Kanály:", value="0/0", inline=False)
        embed.add_field(name="Zprávy celkem:", value="0", inline=False)
        embed.add_field(name="Status:", value="Příprava...", inline=False)
        status_msg = await ctx.send(embed=embed)

        server_data = {
            "info": get_guild_data(ctx.guild),
            "members": await get_members_data(ctx.guild),
            "channels": {}
        }

        total_msgs = 0
        for i, channel in enumerate(target_channels, 1):
            try:
                embed = update_embed(embed, i, len(target_channels), total_msgs, f"Zálohuji {channel.mention}...")
                await status_msg.edit(embed=embed)
                
                history = await backup_channel_history(channel)
                server_data["channels"][channel.name] = history
                total_msgs += len(history)
            except discord.Forbidden:
                logger.warning(f"Přístup odepřen do {channel.name}")
                continue

        # Uložení
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        safe_name = re.sub(r"\W", "_", ctx.guild.name)
        file_name = f"backup_{safe_name}_{ts}.zip"

        manager = BackupManager(server_data, file_name)
        zip_obj = manager.create_zip_in_memory()
        final_path = manager.save_locally(zip_obj)

        embed = update_embed(embed, len(target_channels), len(target_channels), total_msgs, "✅ Hotovo!")
        embed.color = discord.Color.green()
        embed.add_field(name="Lokální cesta:", value=f"`{final_path}`", inline=False)
        await status_msg.edit(embed=embed)
        
        logger.info(f"Záloha dokončena: {final_path}")

    bot.run(os.getenv("BOT_TOKEN"))

if __name__ == "__main__":
    main()