import discord
from discord.ext import commands, tasks
import os
import json
import sys
import time
import asyncio
import aiohttp
from dotenv import load_dotenv
from typing import Optional

sys.stdout.reconfigure(encoding='utf-8')

# Load environment variables
load_dotenv()

TOKEN = os.getenv("TOKEN")
PREFIX = os.getenv("PREFIX", ",")
OWNER_ID = int(os.getenv("OWNER_ID", 0))
EXTRA_OWNERS_FILE = "extra_owners.json"

# Initialize bot config and prefix resolver
CONFIG_FILE = "config.json"

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {
            "enabled": True,
            "custom_message": None,
            "vc_statuses": {},
            "premium_users": [],
            "premium_servers": [],
            "no_prefix_users": []
        }
    with open(CONFIG_FILE, "r") as f:
        try:
            data = json.load(f)
        except Exception:
            data = {}
        if "vc_statuses" not in data:
            data["vc_statuses"] = {}
        if "premium_users" not in data:
            data["premium_users"] = []
        if "premium_servers" not in data:
            data["premium_servers"] = []
        if "no_prefix_users" not in data:
            data["no_prefix_users"] = []
        return data

def save_config(config):
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=4)

config = load_config()

async def get_prefix(bot, message):
    default_prefixes = [PREFIX, "!!", "!"]
    no_prefix_users = config.get("no_prefix_users", [])
    if message.author.id in no_prefix_users:
        return ["", PREFIX, "!!", "!"]
    return default_prefixes

# Initialize bot
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
intents.presences = True  # Required to read member online/idle/dnd status

bot = commands.Bot(
    command_prefix=get_prefix,
    intents=intents,
    help_command=None,
    chunk_guilds_at_startup=True,  # Ensures full member cache on startup
)


from aiohttp import web

async def health_check(request):
    return web.Response(text="Bot is running!", status=200)

async def setup_hook():
    app = web.Application()
    app.router.add_get('/', health_check)
    app.router.add_get('/health', health_check)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    port_str = os.getenv("PORT", "8080")
    try:
        port = int(port_str) if port_str else 8080
    except ValueError:
        port = 8080
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"HTTP server started on port {port}")

bot.setup_hook = setup_hook

cooldowns = {}
COOLDOWN_TIME = 0  # 0 seconds (No cooldown)

def format_embed_list(items):
    """Format a list into a readable, line-by-line embed body."""
    if not items:
        return "- None"
    return "\n".join(f"• {item}" for item in items if item)

async def send_rich_reply(ctx, title, description=None, *, color=0xFFFFFF, footer=None, fields=None, thumbnail=None):
    """Send a clean, neatly arranged embed-style command reply."""
    embed = discord.Embed(title=title, description=description or "", color=color)
    bot_name = ctx.bot.user.name if ctx.bot.user else "Bot"
    embed.set_author(name=bot_name, icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None)

    guild_name = ctx.guild.name if ctx.guild else "DMs"
    guild_id = str(ctx.guild.id) if ctx.guild else "N/A"
    server_count = len(ctx.bot.guilds)

    sys_footer = f"Bot: {bot_name} | Servers: {server_count} | Server: {guild_name} ({guild_id})"
    final_footer = f"{footer} • {sys_footer}" if footer else sys_footer

    embed.set_footer(text=final_footer, icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    if fields:
        for field in fields:
            if len(field) == 3:
                name, value, inline = field
            else:
                name, value = field
                inline = False
            embed.add_field(name=name, value=str(value), inline=inline)
    await ctx.send(embed=embed)

# Persistent Extra Owners Storage
def load_extra_owners():
    if os.path.exists(EXTRA_OWNERS_FILE):
        try:
            with open(EXTRA_OWNERS_FILE, "r") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"Error loading extra owners: {e}")
            return set()
    return set()

def save_extra_owners(owners):
    try:
        with open(EXTRA_OWNERS_FILE, "w") as f:
            json.dump(list(owners), f)
    except Exception as e:
        print(f"Error saving extra owners: {e}")

extra_owners = load_extra_owners()

# Custom check to permit bot owner, guild owner, or extra owners
def is_owner_or_extra():
    async def predicate(ctx):
        is_bot_owner = ctx.author.id == OWNER_ID
        is_guild_owner = ctx.guild and ctx.author.id == ctx.guild.owner_id
        is_extra = ctx.author.id in extra_owners
        if not (is_bot_owner or is_guild_owner or is_extra):
            raise commands.CheckFailure("You do not have permission to run this command.")
        return True
    return commands.check(predicate)

def resolve_vc_placeholders(status: str, guild: Optional[discord.Guild]) -> str:
    """Replaces dynamic placeholders in a VC status string.

    Supported placeholders:
        {totalusers}  - Total member count in the guild (excludes bots)
        {onlineusers} - Members currently Online / Idle / DND (excludes bots)
        {activevc}    - Number of voice channels that have at least 1 member
        {vcusers}     - Total members sitting in any voice channel
    """
    if guild is None:
        return (
            status
            .replace("{totalusers}", "?")
            .replace("{onlineusers}", "?")
            .replace("{activevc}", "?")
            .replace("{vcusers}", "?")
        )

    # guild.members is populated when intents.members + intents.presences are both True
    # and chunk_guilds_at_startup=True is set on the bot.
    human_members = [m for m in guild.members if not m.bot]

    total_users = len(human_members) if human_members else (guild.member_count or 0)

    online_statuses = {discord.Status.online, discord.Status.idle, discord.Status.dnd}
    online_users = sum(1 for m in human_members if m.status in online_statuses)

    # Active VCs = voice channels that currently have at least 1 member
    active_vc = sum(1 for ch in guild.voice_channels if len(ch.members) > 0)

    # VC users = total members across all voice channels
    vc_users = sum(len(ch.members) for ch in guild.voice_channels)

    return (
        status
        .replace("{totalusers}", str(total_users))
        .replace("{onlineusers}", str(online_users))
        .replace("{activevc}", str(active_vc))
        .replace("{vcusers}", str(vc_users))
    )

async def set_voice_status(channel_id: int, status: str):
    """Sets the status text under a voice channel using Discord's REST API.
    Returns (success: bool, message: str, not_found: bool).
    """
    url = f"https://discord.com/api/v10/channels/{channel_id}/voice-status"
    headers = {
        "Authorization": f"Bot {TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {"status": status}

    async with aiohttp.ClientSession() as session:
        async with session.put(url, json=payload, headers=headers) as resp:
            if resp.status in (200, 204):
                print(f"Voice status updated: {status}")
                return True, f"Voice status updated to: {status}", False
            elif resp.status == 404:
                print(f"[AutoVC] Channel {channel_id} not found (404) — skipping.")
                return False, (
                    f"❌ Channel `{channel_id}` was **not found** (404).\n"
                    "Make sure:\n"
                    "• The channel ID is correct (right-click the VC → Copy Channel ID)\n"
                    "• The bot is in the same server as the voice channel\n"
                    "• The bot has **View Channel** permission on that VC"
                ), True
            elif resp.status == 429:
                data = await resp.json()
                retry_after = data.get("retry_after", 5)
                print(f"Rate limited. Retrying after {retry_after}s")
                await asyncio.sleep(retry_after)
                return await set_voice_status(channel_id, status)
            else:
                text = await resp.text()
                print(f"Failed to update status ({resp.status}): {text}")
                return False, f"Failed to update status ({resp.status}): {text}", False

# All four live-data placeholders — any status containing one updates every 1 min
DYNAMIC_PLACEHOLDERS = {"{totalusers}", "{onlineusers}", "{activevc}", "{vcusers}"}

def has_dynamic_placeholders(status: str) -> bool:
    """Return True if the status template contains ANY of the live-data placeholders."""
    return any(p in status for p in DYNAMIC_PLACEHOLDERS)

async def _run_vc_update(dynamic_only: bool):
    """Core update logic shared by both loops.

    dynamic_only=True  → only channels whose template has a live placeholder (1-min loop).
    dynamic_only=False → only channels with plain static text (5-min loop).
    """
    vc_statuses = config.get("vc_statuses", {})
    if not vc_statuses:
        return

    # Pre-chunk all guilds so member presence cache is fresh for this tick
    if dynamic_only:
        for g in bot.guilds:
            try:
                if not g.chunked:
                    await g.chunk()
            except Exception:
                pass

    to_remove = []
    for channel_id_str, status in list(vc_statuses.items()):
        # Route to the correct loop
        if has_dynamic_placeholders(status) != dynamic_only:
            continue
        try:
            channel_id_int = int(channel_id_str)
            # Find which guild owns this channel
            ch = bot.get_channel(channel_id_int)
            guild_for_ch = ch.guild if ch is not None else None
            resolved_status = resolve_vc_placeholders(status, guild_for_ch)
            success, msg, not_found = await set_voice_status(channel_id_int, resolved_status)
            if not_found:
                print(f"[AutoVC] Removing channel {channel_id_str} — channel not found.")
                to_remove.append(channel_id_str)
        except Exception as e:
            print(f"[AutoVC] Error updating channel {channel_id_str}: {e}")

    for ch_id in to_remove:
        config["vc_statuses"].pop(ch_id, None)
    if to_remove:
        save_config(config)

@tasks.loop(seconds=60)
async def auto_update_dynamic_vc_statuses():
    """Every 1 min: refresh VC statuses that use live placeholders ({totalusers} etc.)."""
    await _run_vc_update(dynamic_only=True)

@auto_update_dynamic_vc_statuses.before_loop
async def before_auto_update_dynamic():
    await bot.wait_until_ready()

@tasks.loop(seconds=300)
async def auto_update_vc_statuses():
    """Every 5 min: refresh plain static VC statuses (no placeholders)."""
    await _run_vc_update(dynamic_only=False)

@auto_update_vc_statuses.before_loop
async def before_auto_update():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    try:
        print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    except UnicodeEncodeError:
        safe_user = str(bot.user).encode('ascii', 'ignore').decode('ascii')
        print(f"Logged in as {safe_user} (ID: {bot.user.id}) [Unicode characters omitted in console]")
    print("Monitoring mentions for all users")
    # Set bot presence / description
    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="!help | voice status update bot / mention dm bot"
        )
    )
    if not auto_update_vc_statuses.is_running():
        auto_update_vc_statuses.start()
        print("[AutoVC] Static VC status loop started (every 5 min).")
    if not auto_update_dynamic_vc_statuses.is_running():
        auto_update_dynamic_vc_statuses.start()
        print("[AutoVC] Dynamic VC status loop started (every 1 min).")

@bot.event
async def on_voice_state_update(member, before, after):
    """Refresh dynamic voice status immediately when users join/leave or move channels."""
    if not config.get("vc_statuses"):
        return

    for channel_id_str, status in list(config["vc_statuses"].items()):
        if not has_dynamic_placeholders(status):
            continue
        try:
            channel_id_int = int(channel_id_str)
            guild = member.guild
            ch = bot.get_channel(channel_id_int)
            if guild is None or ch is None or ch.guild.id != guild.id:
                continue
            resolved_status = resolve_vc_placeholders(status, guild)
            await set_voice_status(channel_id_int, resolved_status)
        except Exception as e:
            print(f"[AutoVC] Immediate dynamic update failed for {channel_id_str}: {e}")

@bot.event
async def on_member_update(before, after):
    """Refresh live VC placeholders as soon as a member comes online/offline."""
    if before.status == after.status:
        return
    if not config.get("vc_statuses"):
        return

    for channel_id_str, status in list(config["vc_statuses"].items()):
        if not has_dynamic_placeholders(status):
            continue
        try:
            channel_id_int = int(channel_id_str)
            guild = after.guild
            ch = bot.get_channel(channel_id_int)
            if guild is None or ch is None or ch.guild.id != guild.id:
                continue
            resolved_status = resolve_vc_placeholders(status, guild)
            await set_voice_status(channel_id_int, resolved_status)
        except Exception as e:
            print(f"[AutoVC] Presence-based update failed for {channel_id_str}: {e}")

@bot.event
async def on_message(message):
    # Ignore bot messages
    if message.author.bot:
        return

    # Process commands first so they run correctly
    await bot.process_commands(message)

    ctx = await bot.get_context(message)
    if ctx.valid:
        return

    if not message.mentions:
        return

    if config.get("enabled", True):
        guild_id = message.guild.id if message.guild else "DM"
        current_time = time.time()
        
        for mentioned_user in message.mentions:
            if mentioned_user.bot:
                continue

            cooldown_key = f"{guild_id}_{mentioned_user.id}"
            
            # Check cooldown (1 hour per guild/DM per user)
            if cooldown_key not in cooldowns or (current_time - cooldowns[cooldown_key]) >= COOLDOWN_TIME:
                try:
                    custom_embed_config = config.get("custom_embed")
                    custom_msg = config.get("custom_message")
                    
                    layout = discord.ui.LayoutView()
                    container = discord.ui.Container(accent_color=discord.Color.dark_theme())
                    
                    if custom_embed_config:
                        def format_str(s):
                            if not s: return ""
                            return s.replace("{usermention}", message.author.mention) \
                                    .replace("{usermetion}", message.author.mention) \
                                    .replace("{username}", message.author.name) \
                                    .replace("{server}", message.guild.name if message.guild else "DMs") \
                                    .replace("{servericon}", message.guild.icon.url if message.guild and message.guild.icon else "") \
                                    .replace("{channel}", message.channel.name if getattr(message.channel, "name", None) else "DMs") \
                                    .replace("{message}", message.content) \
                                    .replace("{authoravatar}", message.author.avatar.url if message.author.avatar else "")
                        
                        title = format_str(custom_embed_config.get("title"))
                        desc = format_str(custom_embed_config.get("description"))
                        content = format_str(custom_embed_config.get("content"))
                        thumb_url = format_str(custom_embed_config.get("thumbnail"))
                        img_url = format_str(custom_embed_config.get("image"))
                        footer_text = format_str(custom_embed_config.get("footer"))
                        
                        # Apply CV2 logic
                        if title:
                            container.add_item(discord.ui.TextDisplay(f"# {title}"))
                            
                        # If there's a thumbnail, group it with description in a section
                        if thumb_url and thumb_url.startswith("http"):
                            section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=thumb_url))
                            if desc:
                                section.add_item(discord.ui.TextDisplay(desc))
                            container.add_item(section)
                        elif desc:
                            container.add_item(discord.ui.TextDisplay(desc))
                            
                        if img_url and img_url.startswith("http"):
                            gallery = discord.ui.MediaGallery()
                            gallery.add_item(media=img_url)
                            container.add_item(gallery)
                            
                        if footer_text:
                            container.add_item(discord.ui.Separator())
                            container.add_item(discord.ui.TextDisplay(f"_{footer_text}_"))
                            
                        layout.add_item(container)
                        await mentioned_user.send(content=content if content else None, view=layout)
                        
                    elif custom_msg:
                        # Use custom message (Fallback to plain text)
                        formatted_msg = custom_msg.format(
                            server=message.guild.name if message.guild else "DMs",
                            channel=message.channel.name if getattr(message.channel, "name", None) else "DMs",
                            author=message.author.mention,
                            message=message.content
                        )
                        await mentioned_user.send(formatted_msg)
                    else:
                        # Use default CV2 layout
                        server_name = message.guild.name if message.guild else "DMs"
                        
                        container.add_item(discord.ui.TextDisplay(f"**Tagged in : {server_name}**"))
                        container.add_item(discord.ui.Separator())
                        
                        thumbnail_url = message.author.avatar.url if message.author.avatar else None
                        
                        if thumbnail_url:
                            section = discord.ui.Section(accessory=discord.ui.Thumbnail(media=thumbnail_url))
                        else:
                            section = discord.ui.Section()
                            
                        channel_mention = message.channel.mention if getattr(message.channel, 'mention', None) else 'DMs'
                        
                        section.add_item(discord.ui.TextDisplay(
                            f"**Notification**\nYou were tagged in {server_name}!\n\n"
                            f"**Tagged by :** {message.author.mention}\n\n"
                            f"**Channel :** {server_name} > {channel_mention}\n"
                            f"**Message :**\n> {message.content}"
                        ))
                        
                        container.add_item(section)
                        layout.add_item(container)
                        
                        await mentioned_user.send(view=layout)

                    # Update cooldown
                    cooldowns[cooldown_key] = current_time
                    server_name_str = message.guild.name if message.guild else "DMs"
                    print(f"Sent DM notification to {mentioned_user.name} for mention in {server_name_str}.")

                except discord.Forbidden:
                    print(f"Could not send DM to {mentioned_user.name}. Please check privacy settings.")
                except Exception as e:
                    print(f"Error sending DM to {mentioned_user.name}: {e}")

# --- Commands ---

def is_owner():
    def predicate(ctx):
        return ctx.author.id == OWNER_ID
    return commands.check(predicate)

@bot.command(name="dmm")
@is_owner()
async def dmm_toggle(ctx, state: str):
    """Toggle DM mentions: on or off"""
    state = state.lower()
    if state == "on":
        config["enabled"] = True
        save_config(config)
        await send_rich_reply(
            ctx,
            "✅ DM mentions enabled",
            "Mention notifications are now active for this server.",
            fields=[("Status", "Enabled", True), ("Command", f"`{ctx.prefix}dmm on`", True)]
        )
    elif state == "off":
        config["enabled"] = False
        save_config(config)
        await send_rich_reply(
            ctx,
            "❌ DM mentions disabled",
            "Mention notifications have been turned off for this server.",
            fields=[("Status", "Disabled", True), ("Command", f"`{ctx.prefix}dmm off`", True)]
        )
    else:
        await send_rich_reply(ctx, "⚠️ Invalid usage", "Please use `dmm on` or `dmm off`.", color=0xFFD166)

@bot.command(name="dmmsetup")
@is_owner()
async def dmm_setup(ctx):
    """
    Set up a custom DM message interactively.
    """
    variables_text = (
        "**Available variables:**\n"
        "`{usermention}` - Mentions the user\n"
        "`{username}` - Name of the user\n"
        "`{server}` - Name of the server\n"
        "`{servericon}` - URL of the server icon\n"
        "`{channel}` - Name of the channel\n"
        "`{message}` - The message content\n"
        "`{authoravatar}` - URL of the author's avatar"
    )
    
    await ctx.send(f"Welcome to the custom layout setup!\n{variables_text}\n\nType `cancel` at any time to abort, or `skip` to leave a field empty.\n\n**1. What should be the outside message content?**")
    
    def check(m):
        return m.author.id == OWNER_ID and m.channel == ctx.channel

    try:
        content_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if content_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        content = "" if content_msg.content.lower() == 'skip' else content_msg.content

        await ctx.send("**2. What should be the Layout Title?**")
        title_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if title_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        title = "" if title_msg.content.lower() == 'skip' else title_msg.content

        await ctx.send("**3. What should be the Layout Description?**")
        desc_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if desc_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        desc = "" if desc_msg.content.lower() == 'skip' else desc_msg.content

        await ctx.send("**4. What should be the Footer Text?**")
        footer_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if footer_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        footer = "" if footer_msg.content.lower() == 'skip' else footer_msg.content

        await ctx.send("**5. What should be the Thumbnail URL? (You can use {authoravatar} or {servericon} or skip)**")
        thumb_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if thumb_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        thumb = "" if thumb_msg.content.lower() == 'skip' else thumb_msg.content

        await ctx.send("**6. What should be the Image URL? (or skip)**")
        img_msg = await bot.wait_for('message', timeout=120.0, check=check)
        if img_msg.content.lower() == 'cancel': return await ctx.send("Setup cancelled.")
        image = "" if img_msg.content.lower() == 'skip' else img_msg.content

    except asyncio.TimeoutError:
        return await ctx.send("Setup timed out.")

    config["custom_embed"] = {
        "content": content,
        "title": title,
        "description": desc,
        "footer": footer,
        "thumbnail": thumb,
        "image": image
    }
    if "custom_message" in config:
        del config["custom_message"]
        
    save_config(config)
    await send_rich_reply(
        ctx,
        "✅ Custom layout saved",
        "Your new DM layout is ready and will be used for the next mention.",
        fields=[("Mode", "Custom embed layout", True), ("Reset", f"Use `{ctx.prefix}dmmreset` to return to default", True)]
    )

@bot.command(name="dmmreset")
@is_owner()
async def dmm_reset(ctx):
    """Resets the DM message to the default format."""
    if "custom_embed" in config:
        del config["custom_embed"]
    if "custom_message" in config:
        del config["custom_message"]
    save_config(config)
    await send_rich_reply(
        ctx,
        "✅ Default layout restored",
        "The custom DM layout has been cleared. The default format is now active.",
        fields=[("Status", "Default layout", True), ("Next step", f"Use `{ctx.prefix}dmmsetup` to create a new one", True)]
    )

@bot.command(name="ping")
async def ping(ctx):
    """Shows the bot's WebSocket latency."""
    latency_ms = round(bot.latency * 1000)
    await send_rich_reply(
        ctx,
        "🏓 Pong",
        f"The bot is responding normally with a latency of **{latency_ms}ms**.",
        fields=[("Latency", f"{latency_ms} ms", True), ("Status", "Online", True)]
    )

@bot.group(name="add", invoke_without_command=True)
async def add_group(ctx):
    await send_rich_reply(
        ctx,
        "⚠️ Invalid add command",
        "Use one of the supported subcommands below.",
        color=0xFFD166,
        fields=[
            ("extraowner", f"`{ctx.prefix}add extraowner <user>`", False),
            ("nickname", f"`{ctx.prefix}add nickname <name>`", False),
            ("serveravatar", f"`{ctx.prefix}add serveravatar <url>`", False),
            ("serverbanner", f"`{ctx.prefix}add serverbanner <url>`", False),
        ]
    )

@add_group.command(name="nickname")
@is_owner_or_extra()
async def add_nickname(ctx, *, name: str):
    """Change the bot's nickname in this server."""
    if not ctx.guild:
        return await send_rich_reply(ctx, "❌ Error", "This command can only be used in a server.")
    try:
        await ctx.guild.me.edit(nick=name)
        await send_rich_reply(ctx, "✅ Success", f"Bot nickname changed to **{name}**.")
    except discord.Forbidden:
        await send_rich_reply(ctx, "❌ Error", "I don't have permission to change my nickname.")
    except Exception as e:
        await send_rich_reply(ctx, "❌ Error", f"Failed to change nickname: {e}")

@add_nickname.error
async def add_nickname_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}add nickname <name>`")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@add_group.command(name="serveravatar")
@is_owner_or_extra()
async def add_serveravatar(ctx, *, url: str):
    """Change the bot's global avatar by providing an image URL."""
    url = url.strip("<>")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await send_rich_reply(ctx, "❌ Error", f"Could not download image (HTTP {resp.status}). Check the URL.")
                image_bytes = await resp.read()
        await bot.user.edit(avatar=image_bytes)
        await send_rich_reply(ctx, "✅ Success", "Bot avatar updated successfully!")
    except discord.HTTPException as e:
        await send_rich_reply(ctx, "❌ Error", f"Discord rejected the image: {e}")
    except Exception as e:
        await send_rich_reply(ctx, "❌ Error", f"Failed to update avatar: {e}")

@add_serveravatar.error
async def add_serveravatar_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}add serveravatar <image_url>`")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@add_group.command(name="serverbanner")
@is_owner_or_extra()
async def add_serverbanner(ctx, *, url: str):
    """Change the bot's global banner by providing an image URL."""
    url = url.strip("<>")
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return await send_rich_reply(ctx, "❌ Error", f"Could not download image (HTTP {resp.status}). Check the URL.")
                image_bytes = await resp.read()
        await bot.user.edit(banner=image_bytes)
        await send_rich_reply(ctx, "✅ Success", "Bot banner updated successfully!")
    except discord.HTTPException as e:
        await send_rich_reply(ctx, "❌ Error", f"Discord rejected the image: {e}\n> Note: Banner may require the bot account to have Discord Nitro.")
    except Exception as e:
        await send_rich_reply(ctx, "❌ Error", f"Failed to update banner: {e}")

@add_serverbanner.error
async def add_serverbanner_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}add serverbanner <image_url>`")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.command(name="help", aliases=["h"])
async def custom_help(ctx):
    p = ctx.prefix
    embed = discord.Embed(
        title="📖 Command Help",
        description="Each command is listed individually below.",
        color=0xFFFFFF,
    )
    embed.set_thumbnail(url=ctx.bot.user.display_avatar.url)

    embed.add_field(
        name="🏓 General",
        value=(
            f"• `{p}ping` — Show bot latency\n"
            f"• `{p}help` — Show this help panel\n"
            f"• `{p}h` — Alias for help"
        ),
        inline=False,
    )

    embed.add_field(
        name="🔊 Voice Channel Status",
        value=(
            f"• `{p}vc add <channel_id> <text>` — Set a VC status\n"
            f"• `{p}vc remove <channel_id>` — Remove auto-refresh\n"
            f"• `{p}vc list` — List active VC updates\n"
            f"• Tokens: `{{totalusers}}` `{{onlineusers}}` `{{activevc}}` `{{vcusers}}`\n"
            f"• Static text refreshes every 5 minutes"
        ),
        inline=False,
    )

    embed.add_field(
        name="👑 Owner / Extra Owner",
        value=(
            f"• `{p}pgrant <user> <server_id>` — Grant premium access\n"
            f"• `{p}noprefix <user> [on/off]` — Toggle prefix-free use\n"
            f"• `{p}botstats` — View all connected servers\n"
            f"• `{p}leaveserver [server_id]` — Remove the bot from a guild\n"
            f"• `{p}add extraowner <user>` — Add extra owner\n"
            f"• `{p}add nickname <name>` — Rename the bot\n"
            f"• `{p}add serveravatar <url>` — Set a custom avatar\n"
            f"• `{p}add serverbanner <url>` — Set a custom banner\n"
            f"• `{p}dmm <on/off>` — Toggle DM mention alerts\n"
            f"• `{p}dmmsetup` — Set custom DM layout\n"
            f"• `{p}dmmreset` — Reset default DM layout"
        ),
        inline=False,
    )

    embed.add_field(
        name="📋 Audit Log",
        value=(
            f"• `{p}audit logs [limit]` — View recent actions\n"
            f"• Default limit: 20 • Max: 50"
        ),
        inline=False,
    )

    DEV_USER_ID = 1495697271071703121
    try:
        dev_user = await ctx.bot.fetch_user(DEV_USER_ID)
        dev_icon = dev_user.display_avatar.url
        dev_name = f"Dev: {dev_user.name}"
    except Exception:
        dev_icon = ctx.bot.user.display_avatar.url
        dev_name = "Dev: sivajee"

    embed.set_footer(text="Dev by sivajee", icon_url=dev_icon)
    embed.set_author(name=dev_name, icon_url=dev_icon)

    await ctx.send(embed=embed)

@bot.group(name="vc", invoke_without_command=True)
async def vc(ctx):
    await send_rich_reply(
        ctx,
        "⚠️ Invalid VC command",
        f"Use `{ctx.prefix}vc add <channel_id> <status_text>` or check `{ctx.prefix}help` for more options.",
        color=0xFFD166
    )

@vc.command(name="add")
@is_owner_or_extra()
async def vc_add(ctx, channel_id: int, *, text: str):
    # Resolve placeholders for the immediate update
    guild = ctx.guild
    
    # Premium check: limit to 5 updates per server if not premium
    is_already_added = str(channel_id) in config.get("vc_statuses", {})
    if not is_already_added:
        guild_vc_count = 0
        if guild:
            for cid in config.get("vc_statuses", {}):
                if guild.get_channel(int(cid)) is not None:
                    guild_vc_count += 1
        
        if guild_vc_count >= 5:
            is_premium_guild = guild and guild.id in config.get("premium_servers", [])
            is_premium_user = ctx.author.id in config.get("premium_users", [])
            is_bot_owner = ctx.author.id == OWNER_ID
            
            if not (is_premium_guild or is_premium_user or is_bot_owner):
                await send_rich_reply(
                    ctx,
                    "👑 Premium Required",
                    "dm to this user 1495697271071703121 for premium access"
                )
                return

    resolved = resolve_vc_placeholders(text, guild)
    success, message, not_found = await set_voice_status(channel_id, resolved)
    if success:
        # Persist the raw template (with placeholders) so the loop re-resolves each time
        config.setdefault("vc_statuses", {})[str(channel_id)] = text
        save_config(config)
        await send_rich_reply(
            ctx,
            "✅ Voice status updated",
            f"{message}\nThis channel will be auto-refreshed every 5 minutes.",
            fields=[("Channel", f"`{channel_id}`", True), ("Refresh", "Every 5 minutes", True)]
        )
    else:
        await send_rich_reply(ctx, "❌ Voice status update failed", message)

@vc.command(name="remove")
@is_owner_or_extra()
async def vc_remove(ctx, channel_id: int):
    """Remove a voice channel from the auto-refresh loop."""
    vc_statuses = config.get("vc_statuses", {})
    if str(channel_id) in vc_statuses:
        del vc_statuses[str(channel_id)]
        save_config(config)
        await send_rich_reply(ctx, "✅ Voice channel removed", f"Channel `{channel_id}` has been removed from the auto-refresh list.", fields=[("Channel", f"`{channel_id}`", True)])
    else:
        await send_rich_reply(ctx, "❌ Channel not found", f"Channel `{channel_id}` is not in the auto-refresh list.")

@vc.command(name="list")
@is_owner_or_extra()
async def vc_list(ctx):
    """List all voice channels being auto-refreshed."""
    vc_statuses = config.get("vc_statuses", {})
    if not vc_statuses:
        await send_rich_reply(ctx, "ℹ️ Auto-refresh list", "No voice channels are being auto-refreshed right now.")
        return
    lines = [f"`{ch_id}` → {status}" for ch_id, status in vc_statuses.items()]
    await send_rich_reply(
        ctx,
        "📋 Auto-refreshed voice channels",
        "Here is the current list of tracked voice channel statuses.",
        fields=[("Channels", "\n".join(lines), False)]
    )

@vc_add.error
async def vc_add_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}vc add <channel_id> <status_text>`")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Invalid channel ID. It must be an integer.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@vc_remove.error
async def vc_remove_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}vc remove <channel_id>`")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Invalid channel ID. It must be an integer.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@add_group.command(name="extraowner")
@is_owner_or_extra()
async def add_extraowner(ctx, member: discord.User):
    extra_owners.add(member.id)
    save_extra_owners(extra_owners)
    await send_rich_reply(
        ctx,
        "✅ Extra owner added",
        f"{member.mention} has been added as an extra owner.",
        fields=[("User ID", str(member.id), True)]
    )

@add_extraowner.error
async def add_extraowner_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}add extraowner <user_id_or_mention>`")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Invalid user. Provide a valid user ID or mention.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.command(name="pgrant")
@is_owner()
async def pgrant(ctx, user: discord.User, server_id: int):
    """Grant premium status to a user and a server (Bot Owner only)."""
    premium_users = config.setdefault("premium_users", [])
    premium_servers = config.setdefault("premium_servers", [])
    
    if user.id not in premium_users:
        premium_users.append(user.id)
    if server_id not in premium_servers:
        premium_servers.append(server_id)
        
    save_config(config)
    
    await send_rich_reply(
        ctx,
        "👑 Premium Access Granted",
        f"Premium access has been granted successfully.\n\n"
        f"• **User:** {user.mention} (`{user.id}`)\n"
        f"• **Server ID:** `{server_id}`"
    )

@pgrant.error
async def pgrant_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "Only the bot owner can use this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}pgrant <@user/user_id> <server_id>`")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Please provide a valid user (mention or ID) and an integer server ID.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.command(name="noprefix")
@is_owner_or_extra()
async def noprefix(ctx, user: discord.User, state: str = None):
    """Toggle prefix-free command execution for a user."""
    no_prefix_users = config.setdefault("no_prefix_users", [])
    
    if state is not None:
        state = state.lower()
        if state == "on":
            if user.id not in no_prefix_users:
                no_prefix_users.append(user.id)
            status = "enabled"
        elif state == "off":
            if user.id in no_prefix_users:
                no_prefix_users.remove(user.id)
            status = "disabled"
        else:
            await send_rich_reply(
                ctx,
                "⚠️ Invalid Usage",
                f"Please use `{ctx.prefix}noprefix <user> <on/off>` or simply `{ctx.prefix}noprefix <user>` to toggle."
            )
            return
    else:
        # Toggle
        if user.id in no_prefix_users:
            no_prefix_users.remove(user.id)
            status = "disabled"
        else:
            no_prefix_users.append(user.id)
            status = "enabled"
            
    save_config(config)
    await send_rich_reply(
        ctx,
        "⚡ No-Prefix Status Updated",
        f"No-prefix command execution has been **{status}** for {user.mention} (`{user.id}`)."
    )

@noprefix.error
async def noprefix_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(ctx, "⚠️ Missing Argument", f"Missing argument. Usage: `{ctx.prefix}noprefix <@user/user_id> [on/off]`")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Please provide a valid user ID or mention.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.command(name="botstats", aliases=["stats", "servers"])
@is_owner_or_extra()
async def bot_stats(ctx):
    """View servers where the bot is added."""
    guilds = list(bot.guilds)
    if not guilds:
        return await send_rich_reply(ctx, "📊 Bot Stats", "The bot is not currently in any servers.")

    # Sort guilds by member count descending
    guilds.sort(key=lambda g: g.member_count or 0, reverse=True)
    
    per_page = 10
    pages = [guilds[i:i + per_page] for i in range(0, len(guilds), per_page)]
    
    bot_name = ctx.bot.user.name if ctx.bot.user else "Bot"
    total_servers = len(guilds)
    total_members = sum(g.member_count or 0 for g in guilds)
    
    embeds = []
    for page_num, page_guilds in enumerate(pages, start=1):
        embed = discord.Embed(
            title="📊 Bot Server List",
            description=f"Total Servers: **{total_servers}** | Total Members: **{total_members}**",
            color=0xFFFFFF
        )
        embed.set_author(name=bot_name, icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None)
        
        server_lines = []
        for index, guild in enumerate(page_guilds, start=(page_num - 1) * per_page + 1):
            # Only show the server name (remove IDs and member counts)
            server_lines.append(f"**{index}. {guild.name}**")
        
        embed.add_field(name=f"Servers (Page {page_num}/{len(pages)})", value="\n".join(server_lines), inline=False)
        
        # Keep footer minimal and avoid exposing server counts or server-specific info
        embed.set_footer(
            text=f"Page {page_num}/{len(pages)}",
            icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None
        )
        embeds.append(embed)

    # If the actual bot owner requested stats, attempt to create/send invite links privately
    if ctx.author.id == OWNER_ID:
        invite_lines = []
        for g in guilds:
            link = None
            try:
                # Find a text channel where the bot can create invites
                channel = None
                for ch in g.text_channels:
                    perms = ch.permissions_for(g.me)
                    if perms.create_instant_invite and perms.send_messages:
                        channel = ch
                        break

                if channel is None:
                    link = "No suitable channel / missing permissions"
                else:
                    invite = await channel.create_invite(max_age=0, max_uses=0, unique=False, reason="Invite for bot owner via bot_stats")
                    link = invite.url
            except Exception as e:
                link = f"Failed: {e}"
            invite_lines.append(f"**{g.name}** — {link}")

        try:
            owner_user = ctx.bot.get_user(OWNER_ID) or await ctx.bot.fetch_user(OWNER_ID)
            # Send as a DM to the owner so only they can see the invites
            chunk = "\n".join(invite_lines)
            if not chunk:
                chunk = "No invites could be generated."
            await owner_user.send(f"Server invite links (requested via {ctx.command}):\n\n{chunk}")
        except Exception as e:
            print(f"Failed to DM owner invite links: {e}")

    message = await ctx.send(embed=embeds[0])
    if len(embeds) > 1:
        await message.add_reaction("◀️")
        await message.add_reaction("▶️")

        current_page = 0

        def reaction_check(reaction, user):
            return (
                user == ctx.author
                and reaction.message.id == message.id
                and str(reaction.emoji) in ("◀️", "▶️")
            )

        while True:
            try:
                reaction, user = await ctx.bot.wait_for(
                    "reaction_add", timeout=120.0, check=reaction_check
                )
                if str(reaction.emoji) == "▶️" and current_page < len(embeds) - 1:
                    current_page += 1
                    await message.edit(embed=embeds[current_page])
                elif str(reaction.emoji) == "◀️" and current_page > 0:
                    current_page -= 1
                    await message.edit(embed=embeds[current_page])

                try:
                    await message.remove_reaction(reaction.emoji, user)
                except discord.HTTPException:
                    pass
            except asyncio.TimeoutError:
                try:
                    await message.clear_reactions()
                except discord.HTTPException:
                    pass
                break

@bot_stats.error
async def bot_stats_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.command(name="leaveserver", aliases=["removebot", "leaveguild"])
@is_owner_or_extra()
async def leave_server(ctx, server_id: int = None):
    """Force the bot to leave a server (Bot Owner only)."""
    if server_id is None:
        if ctx.guild:
            guild = ctx.guild
            server_id = guild.id
        else:
            return await send_rich_reply(
                ctx,
                "⚠️ Missing Argument",
                "Please provide a server ID when running this command in DMs."
            )
    else:
        guild = ctx.bot.get_guild(server_id)
        
    if not guild:
        return await send_rich_reply(
            ctx,
            "❌ Server Not Found",
            f"Could not find any server with ID `{server_id}`."
        )
    
    guild_name = guild.name
    try:
        if ctx.guild and guild.id == ctx.guild.id:
            try:
                await send_rich_reply(
                    ctx,
                    "🚪 Leaving Server",
                    f"Leaving this server (**{guild_name}**) as requested."
                )
            except Exception:
                pass
            await guild.leave()
        else:
            await guild.leave()
            await send_rich_reply(
                ctx,
                "🚪 Left Server Successfully",
                f"Successfully left the server **{guild_name}** (`{server_id}`)."
            )
    except Exception as e:
        await send_rich_reply(
            ctx,
            "❌ Failed to Leave Server",
            f"An error occurred while trying to leave **{guild_name}** (`{server_id}`):\n{e}"
        )

@leave_server.error
async def leave_server_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "Only the bot owner can use this command.")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", "Please provide a valid integer server ID.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

# --- Audit Log Command ---

@bot.group(name="audit", invoke_without_command=True)
@is_owner_or_extra()
async def audit_group(ctx):
    """Audit log commands."""
    await send_rich_reply(
        ctx,
        "⚠️ Invalid audit command",
        f"Use `{ctx.prefix}audit logs` to view the server audit log (bot actions filtered out).",
        color=0xFFD166,
    )

def _format_audit_action(action: discord.AuditLogAction) -> str:
    """Return a human-friendly name for an audit-log action."""
    name = action.name  # e.g. "channel_update"
    return name.replace("_", " ").title()

@audit_group.command(name="logs")
@is_owner_or_extra()
async def audit_logs(ctx, limit: int = 20):
    """Show recent audit log entries, excluding actions made by this bot.

    Usage: !audit logs [limit]
    Default limit is 20, max is 50.
    """
    if not ctx.guild:
        return await send_rich_reply(ctx, "❌ Error", "This command can only be used in a server.")

    # Clamp limit
    limit = max(1, min(limit, 50))

    # Check bot permissions
    if not ctx.guild.me.guild_permissions.view_audit_log:
        return await send_rich_reply(
            ctx,
            "❌ Missing Permission",
            "I need the **View Audit Log** permission to run this command.",
            color=0xFF6B6B,
        )

    bot_id = ctx.bot.user.id

    entries = []
    try:
        # Fetch more than needed so we still have entries after filtering
        async for entry in ctx.guild.audit_logs(limit=limit * 3):
            # ─── Filter out the bot's own actions ───
            if entry.user and entry.user.id == bot_id:
                continue
            entries.append(entry)
            if len(entries) >= limit:
                break
    except discord.Forbidden:
        return await send_rich_reply(
            ctx,
            "❌ Forbidden",
            "I don't have permission to read the audit log.",
            color=0xFF6B6B,
        )
    except Exception as e:
        return await send_rich_reply(
            ctx,
            "❌ Error",
            f"Failed to fetch audit logs: {e}",
            color=0xFF6B6B,
        )

    if not entries:
        return await send_rich_reply(
            ctx,
            "📋 Audit Log",
            "No audit log entries found (after filtering out bot actions).",
            color=0xF7F7F7,
        )

    # Build paginated embeds (5 entries per page)
    per_page = 5
    pages = [entries[i:i + per_page] for i in range(0, len(entries), per_page)]
    embeds = []

    for page_num, page_entries in enumerate(pages, start=1):
        embed = discord.Embed(
            title="📋 Server Audit Log",
            description=f"Showing **{len(entries)}** recent entries (bot's own actions hidden)",
            color=0xFFFFFF,
        )
        embed.set_author(
            name=ctx.guild.name,
            icon_url=ctx.guild.icon.url if ctx.guild.icon else None,
        )

        for entry in page_entries:
            action_name = _format_audit_action(entry.action)
            executor = entry.user.mention if entry.user else "Unknown"
            target_str = "—"
            if entry.target:
                if hasattr(entry.target, "mention"):
                    target_str = entry.target.mention
                elif hasattr(entry.target, "name"):
                    target_str = entry.target.name
                else:
                    target_str = str(entry.target)

            reason = entry.reason or "No reason provided"
            timestamp = discord.utils.format_dt(entry.created_at, style="R")

            value_lines = (
                f"**Executor:** {executor}\n"
                f"**Target:** {target_str}\n"
                f"**Reason:** {reason}\n"
                f"**When:** {timestamp}"
            )
            embed.add_field(name=f"🔹 {action_name}", value=value_lines, inline=False)

        bot_name = ctx.bot.user.name if ctx.bot.user else "Bot"
        embed.set_footer(
            text=f"Page {page_num}/{len(pages)} • Bot actions are automatically filtered out • Bot: {bot_name}",
            icon_url=ctx.bot.user.display_avatar.url if ctx.bot.user else None,
        )
        embeds.append(embed)

    # Send first page; if multiple pages, add navigation reactions
    message = await ctx.send(embed=embeds[0])

    if len(embeds) > 1:
        await message.add_reaction("◀️")
        await message.add_reaction("▶️")

        current_page = 0

        def reaction_check(reaction, user):
            return (
                user == ctx.author
                and reaction.message.id == message.id
                and str(reaction.emoji) in ("◀️", "▶️")
            )

        while True:
            try:
                reaction, user = await ctx.bot.wait_for(
                    "reaction_add", timeout=120.0, check=reaction_check
                )
                if str(reaction.emoji) == "▶️" and current_page < len(embeds) - 1:
                    current_page += 1
                    await message.edit(embed=embeds[current_page])
                elif str(reaction.emoji) == "◀️" and current_page > 0:
                    current_page -= 1
                    await message.edit(embed=embeds[current_page])

                try:
                    await message.remove_reaction(reaction.emoji, user)
                except discord.HTTPException:
                    pass
            except asyncio.TimeoutError:
                try:
                    await message.clear_reactions()
                except discord.HTTPException:
                    pass
                break

@audit_group.error
async def audit_group_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@audit_logs.error
async def audit_logs_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        await send_rich_reply(ctx, "❌ Permission Denied", "You do not have permission to run this command.")
    elif isinstance(error, commands.BadArgument):
        await send_rich_reply(ctx, "⚠️ Invalid Argument", f"Invalid limit. Usage: `{ctx.prefix}audit logs [number]`")
    else:
        await send_rich_reply(ctx, "❌ Error", f"An error occurred: {error}")

@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.CheckFailure):
        # Ignore errors where someone else tries to run owner commands
        pass
    elif isinstance(error, commands.MissingRequiredArgument):
        await send_rich_reply(
            ctx,
            "⚠️ Missing arguments",
            f"Usage: `{ctx.prefix}{ctx.command.name} <arguments>`",
            color=0xFFD166
        )
    else:
        print(f"Ignoring exception in command {ctx.command}: {error}")

if __name__ == "__main__":
    if not TOKEN or TOKEN == "your_bot_token_here":
        print("Please configure your .env file with a valid TOKEN and OWNER_ID.")
    else:
        print("Starting bot...")
        try:
            bot.run(TOKEN)
        except Exception as e:
            print(f"Bot terminated with exception: {e}")
            raise
