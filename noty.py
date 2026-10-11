import os
import re
import json
import io
import uuid
import sqlite3
import asyncio
import logging
import random
from datetime import datetime, timedelta, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

try:
    import docker
except ImportError:
    docker = None

# ============================================================
# SiftNodes - Public Discord Hosting & Community Bot
# ============================================================
# Main VPS is NEVER assigned to users.
# Users receive access only to their Docker container.
#
# Requirements:
#   Python 3.11+
#   discord.py >= 2.6,<3
#   docker
#
# Docker host:
#   - Ubuntu/Debian Linux
#   - Docker Engine
#   - Bot account needs Discord permissions:
#       View Channels, Send Messages, Embed Links,
#       Use Application Commands, Manage Server (invite tracking),
#       Manage Messages (optional)
#
# IMPORTANT:
# Docker CPU/RAM limits are enforced by Docker.
# The 120 GB disk limit requires a Docker storage driver/filesystem
# that supports storage quotas (for example an appropriate XFS setup).
# The bot attempts storage_opt={"size":"120G"} and falls back safely
# if the host driver does not support it.
# ============================================================

TOKEN = os.getenv("DISCORD_BOT_TOKEN", "PUT_YOUR_BOT_TOKEN_HERE")
OWNER_ID = int(os.getenv("SIFTNODES_OWNER_ID", "1520903362667876523"))

VOUCH_CHANNEL_ID = int(os.getenv("VOUCH_CHANNEL_ID", "1553695179666366565"))  # Channel where users post vouches
VOUCH_LOG_CHANNEL_ID = int(os.getenv("VOUCH_LOG_CHANNEL_ID", "1557023316038914209"))  # Real/fake vouch logs
AI_CHAT_CHANNEL_ID = int(os.getenv("AI_CHAT_CHANNEL_ID", "1556778954319204424"))  # SiftAI responds only here
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434/api/chat")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:3b")
DB_FILE = os.getenv("PRIME_DB", "primecloud.db")

BOT_NAME = "SiftNodes"
ACTIVITY_NAME = "SiftNodes VPS Management"

# Invite-created VPS and admin-deployed VPS have separate resource limits.
INVITE_RAM_GB = 12
INVITE_CPU_CORES = 4
INVITE_DISK_GB = 30
BUY_RAM_GB = 16
BUY_CPU_CORES = 6
BUY_DISK_GB = 60
ADMIN_RAM_GB = 12
ADMIN_CPU_CORES = 4
ADMIN_DISK_GB = 30
PLAN_STORAGE_LABEL = "16GB RAM • 6 Core CPU • 60GB Disk"
CLAIM_STORAGE_LABEL = "12GB RAM • 4 Core CPU • 30GB Disk"
SLOT_COSTS = {1: 10, 2: 15, 3: 20, 4: 25, 5: 35, 6: 40, 7: 45, 8: 50, 9: 55, 10: 60}
EXPIRY_DAYS = 29
INVITE_COST = 12

# Only this OS is allowed.
SUPPORTED_OS = "Ubuntu 24.04 LTS"
DOCKER_IMAGE = os.getenv("UBUNTU_IMAGE", "ubuntu:24.04")

NODES = [f"NODE{i}" for i in range(1, 11)] + ["LOCALNODE"]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("siftnodes")

db = sqlite3.connect(DB_FILE, check_same_thread=False)
db.row_factory = sqlite3.Row
db.execute("PRAGMA journal_mode=WAL")


# ============================================================
# DATABASE
# ============================================================

def db_init():
    db.executescript("""
    CREATE TABLE IF NOT EXISTS admins (
        user_id INTEGER PRIMARY KEY
    );

    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        invite_points INTEGER NOT NULL DEFAULT 0,
        joins INTEGER NOT NULL DEFAULT 0,
        leaves INTEGER NOT NULL DEFAULT 0,
        fake INTEGER NOT NULL DEFAULT 0,
        rejoins INTEGER NOT NULL DEFAULT 0,
        message_credits INTEGER NOT NULL DEFAULT 0,
        slots INTEGER NOT NULL DEFAULT 1
    );

    CREATE TABLE IF NOT EXISTS vps (
        id TEXT PRIMARY KEY,
        owner_id INTEGER NOT NULL,
        node TEXT NOT NULL,
        plan TEXT NOT NULL,
        os TEXT NOT NULL,
        container_id TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expiry_at TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'stopped',
        vouched INTEGER NOT NULL DEFAULT 0,
        ram_gb INTEGER NOT NULL,
        cpu_cores INTEGER NOT NULL,
        disk_gb INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS invites (
        guild_id INTEGER NOT NULL,
        code TEXT NOT NULL,
        inviter_id INTEGER,
        uses INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, code)
    );

    CREATE TABLE IF NOT EXISTS redeem_codes (
        code TEXT PRIMARY KEY,
        days INTEGER NOT NULL,
        used_by INTEGER,
        used_at TEXT
    );

    CREATE TABLE IF NOT EXISTS renewal_rewards (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        plan TEXT NOT NULL,
        invites INTEGER NOT NULL,
        days INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS message_plans (
        plan TEXT PRIMARY KEY,
        specs TEXT NOT NULL,
        messages INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS giveaways (
        message_id INTEGER PRIMARY KEY,
        guild_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL,
        host_id INTEGER NOT NULL,
        reward TEXT NOT NULL,
        winners INTEGER NOT NULL,
        ends_at TEXT NOT NULL,
        ended INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS giveaway_entries (
        message_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        joined_at TEXT NOT NULL,
        PRIMARY KEY (message_id, user_id)
    );
    """)
    user_columns = {row[1] for row in db.execute("PRAGMA table_info(users)").fetchall()}
    if "slots" not in user_columns:
        db.execute("ALTER TABLE users ADD COLUMN slots INTEGER NOT NULL DEFAULT 1")
    db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (OWNER_ID,))
    db.commit()


def utcnow():
    return datetime.now(timezone.utc)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_dt(value):
    return datetime.fromisoformat(value)


def ensure_user(user_id):
    db.execute("INSERT OR IGNORE INTO users(user_id) VALUES (?)", (user_id,))
    db.commit()


def user_row(user_id):
    ensure_user(user_id)
    return db.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()


def is_admin(user_id):
    if user_id == OWNER_ID:
        return True
    return db.execute(
        "SELECT 1 FROM admins WHERE user_id=?", (user_id,)
    ).fetchone() is not None


def get_vps(vps_id):
    return db.execute("SELECT * FROM vps WHERE id=?", (vps_id,)).fetchone()


def get_user_vps(user_id):
    return db.execute(
        "SELECT * FROM vps WHERE owner_id=? ORDER BY expiry_at ASC", (user_id,)
    ).fetchall()


def count_user_vps(user_id):
    return db.execute("SELECT COUNT(*) AS c FROM vps WHERE owner_id=? AND expiry_at>?", (user_id, iso(utcnow()))).fetchone()["c"]

def user_slot_limit(user_id):
    return int(user_row(user_id)["slots"] or 1)

def has_free_slot(user_id):
    return count_user_vps(user_id) < user_slot_limit(user_id)

_deploying_users = set()


# ============================================================
# DISCORD BOT
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.messages = True
intents.message_content = True

bot = commands.Bot(
    command_prefix=("-", "."),
    intents=intents,
    help_command=None,
)

docker_client = None
invite_cache = {}
guild_member_inviter = {}


def docker_connect():
    global docker_client
    if docker is None:
        log.error("Python package 'docker' is not installed.")
        return False
    try:
        docker_client = docker.from_env()
        docker_client.ping()
        log.info("Docker connection successful.")
        return True
    except Exception as exc:
        docker_client = None
        log.error("Docker connection failed: %s", exc)
        return False


def container_name(vps_id):
    return "primecloud-" + re.sub(r"[^A-Za-z0-9_-]", "", vps_id)[:48]


def image_for_os(os_name):
    normalized = os_name.strip().lower().replace(" ", "")
    if normalized in {
        "ubuntu",
        "ubuntu24.04",
        "ubuntu24:04",
        "ubuntu-24.04",
        "ubuntu2404",
        "ubuntu24.04lts",
    }:
        return DOCKER_IMAGE
    raise ValueError("Only Ubuntu 24.04 LTS is supported.")


def pick_node(preferred=None):
    if preferred:
        preferred = preferred.upper()
        if preferred in NODES:
            return preferred

    # These are logical local nodes on the same Docker host.
    # Pick the node with the fewest active VPS records.
    counts = {}
    for node in NODES:
        counts[node] = db.execute(
            "SELECT COUNT(*) AS c FROM vps WHERE node=? AND expiry_at>?",
            (node, iso(utcnow())),
        ).fetchone()["c"]
    return min(NODES, key=lambda n: counts[n])


def docker_create_vps(vps_id, ram_gb, cpu_cores, disk_gb):
    if docker_client is None:
        raise RuntimeError("Docker is not connected.")

    name = container_name(vps_id)

    # Long-running Ubuntu container. SSH/tmate/sshx are installed on start.
    cmd = (
        "bash -lc "
        "'set -e; "
        "export DEBIAN_FRONTEND=noninteractive; "
        "apt-get update -qq; "
        "apt-get install -y -qq openssh-server curl tmate sudo; "
        "mkdir -p /run/sshd; "
        "echo \"root:SiftNodesChangeMe\" | chpasswd; "
        "sed -ri \"s/^#?PermitRootLogin.*/PermitRootLogin yes/\" /etc/ssh/sshd_config; "
        "sed -ri \"s/^#?PasswordAuthentication.*/PasswordAuthentication yes/\" /etc/ssh/sshd_config; "
        "/usr/sbin/sshd; "
        "tail -f /dev/null'"
    )

    kwargs = dict(
        image=DOCKER_IMAGE,
        name=name,
        command=cmd,
        detach=True,
        tty=True,
        stdin_open=True,
        mem_limit=f"{ram_gb}g",
        nano_cpus=cpu_cores * 1_000_000_000,
        restart_policy={"Name": "unless-stopped"},
        labels={
            "primecloud": "true",
            "primecloud_vps": vps_id,
            "primecloud_ram_gb": str(ram_gb),
            "primecloud_cpu": str(cpu_cores),
            "primecloud_disk_gb": str(disk_gb),
        },
    )

    # Docker disk quota is driver dependent.
    try:
        return docker_client.containers.run(
            **kwargs,
            storage_opt={"size": f"{disk_gb}G"},
        )
    except Exception as first_exc:
        log.warning(
            "%sG Docker storage quota was not accepted by host driver: %s",
            disk_gb, first_exc,
        )
        return docker_client.containers.run(**kwargs)


def docker_get(vps):
    try:
        return docker_client.containers.get(vps["container_id"])
    except Exception:
        return None


def docker_start(vps):
    c = docker_get(vps)
    if not c:
        raise RuntimeError("Docker container was not found.")
    c.start()


def docker_stop(vps):
    c = docker_get(vps)
    if not c:
        raise RuntimeError("Docker container was not found.")
    c.stop(timeout=10)


def docker_remove(vps):
    c = docker_get(vps)
    if c:
        try:
            c.remove(force=True)
        except Exception as exc:
            log.warning("Container remove failed: %s", exc)


async def run_blocking(func, *args):
    return await asyncio.to_thread(func, *args)


# ============================================================
# SSH / SSHX
# ============================================================

def exec_container(c, command):
    result = c.exec_run(["bash", "-lc", command], demux=False)
    return result.exit_code, result.output.decode("utf-8", "ignore").strip()


async def generate_routes(vps, progress_callback=None):
    """Generate tmate and SSHX routes, reporting 10-100% progress when asked."""
    c = await run_blocking(docker_get, vps)
    if not c:
        return "Failed to extract tmate route", "SSHX generation failed: cannot connect to VPS container."

    tmate_cmd = (
        "tmate -S /tmp/primecloud.sock new-session -d || true; "
        "sleep 2; "
        "tmate -S /tmp/primecloud.sock display -p '#{tmate_ssh}' 2>/dev/null || true"
    )
    try:
        _, output = await run_blocking(exec_container, c, tmate_cmd)
        tmate_route = output.strip() or "Failed to extract tmate route"
    except Exception as exc:
        log.warning("tmate route generation failed for %s: %s", vps['id'], exc)
        tmate_route = "Failed to extract tmate route"

    # Run the exact SSHX installer command requested by the owner. Keep output
    # in a log so the generated sshx.io link can be extracted.
    launch_cmd = (
        "mkdir -p /tmp; "
        "if pgrep -af '[s]shx.*run' >/dev/null; then "
        "pkill -f '[s]shx.*run' || true; sleep 1; fi; "
        "rm -f /tmp/primecloud-sshx.log; "
        "nohup bash -lc 'curl -sSf https://sshx.io/get | sh -s run 2>&1' "
        ">/tmp/primecloud-sshx.log 2>&1 </dev/null &"
    )
    sshx_route = ""
    try:
        await run_blocking(exec_container, c, launch_cmd)
        for percent in range(10, 101, 10):
            await asyncio.sleep(6)
            _, log_output = await run_blocking(
                exec_container, c,
                "cat /tmp/primecloud-sshx.log 2>/dev/null || true"
            )
            # Strip ANSI terminal sequences before looking for the public URL.
            log_output = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", log_output)
            match = re.search(r"https?://(?:www\.)?sshx\.io/[^\s\"'<>]+", log_output, re.I)
            if match:
                sshx_route = match.group(0).rstrip(".,;)")
            if progress_callback:
                await progress_callback(percent)
            if sshx_route:
                if percent < 100 and progress_callback:
                    await progress_callback(100)
                break
        if not sshx_route:
            _, log_output = await run_blocking(
                exec_container, c,
                "cat /tmp/primecloud-sshx.log 2>/dev/null || true"
            )
            sshx_route = "SSHX URL not generated. Check VPS internet access and try /regen_ssh again."
            if log_output.strip():
                log.warning("SSHX did not produce a URL for %s. Output: %s", vps['id'], log_output[-1500:])
    except Exception as exc:
        log.warning("SSHX route generation failed for %s: %s", vps['id'], exc)
        sshx_route = "SSHX generation failed. Check VPS internet access and try /regen_ssh again."
    return tmate_route, sshx_route

async def dm_routes(user, vps, title="🔐 SiftNodes VPS Connection"):
    try:
        tmate_route, sshx_route = await generate_routes(vps)
        embed = discord.Embed(
            title=title,
            description="Your VPS connection routes are below.",
            color=discord.Color.blurple(),
        )
        embed.add_field(
            name="📦 Container ID",
            value=f"`{vps['id']}`",
            inline=False,
        )
        embed.add_field(
            name="🌐 Cluster Node",
            value=f"`{vps['node']}`",
            inline=False,
        )
        embed.add_field(
            name="🔑 tmate SSH Command",
            value=f"```bash\n{tmate_route}\n```",
            inline=False,
        )
        embed.add_field(
            name="🔗 sshx Web Link",
            value=f"```text\n{sshx_route}\n```",
            inline=False,
        )
        await user.send(embed=embed)
        return tmate_route, sshx_route
    except discord.Forbidden:
        return "DM_DISABLED", "DM_DISABLED"


# ============================================================
# INVITE TRACKING
# ============================================================

async def refresh_invites(guild):
    try:
        invites = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        log.warning("Cannot read invites for guild %s", guild.id)
        return

    invite_cache[guild.id] = {
        inv.code: {
            "uses": inv.uses or 0,
            "inviter_id": inv.inviter.id if inv.inviter else None,
        }
        for inv in invites
    }

    for inv in invites:
        db.execute(
            """
            INSERT INTO invites(guild_id, code, inviter_id, uses)
            VALUES(?,?,?,?)
            ON CONFLICT(guild_id, code)
            DO UPDATE SET uses=excluded.uses, inviter_id=excluded.inviter_id
            """,
            (
                guild.id,
                inv.code,
                inv.inviter.id if inv.inviter else None,
                inv.uses or 0,
            ),
        )
    db.commit()


async def find_used_invite(guild):
    try:
        current = await guild.invites()
    except (discord.Forbidden, discord.HTTPException):
        return None

    before = invite_cache.get(guild.id, {})
    used = None

    for inv in current:
        old = before.get(inv.code)
        old_uses = old["uses"] if old else 0
        new_uses = inv.uses or 0
        if new_uses > old_uses:
            used = inv
            break

    invite_cache[guild.id] = {
        inv.code: {
            "uses": inv.uses or 0,
            "inviter_id": inv.inviter.id if inv.inviter else None,
        }
        for inv in current
    }

    return used


@bot.event
async def on_member_join(member):
    if member.bot:
        return

    # Apply configured autorole and send the configured welcome embed.
    try:
        role_value = setting_get("autorole_id")
        if role_value:
            auto_role = member.guild.get_role(int(role_value))
            if auto_role and not auto_role.is_default and not auto_role.managed:
                await member.add_roles(auto_role, reason="Configured autorole")
    except (discord.Forbidden, discord.HTTPException, ValueError):
        log.warning("Could not apply autorole for member %s", member.id)

    try:
        welcome_cfg = json_setting_get("welcome_config", {})
        welcome_channel_id = welcome_cfg.get("channel_id")
        welcome_channel = member.guild.get_channel(int(welcome_channel_id)) if welcome_channel_id else None
        if isinstance(welcome_channel, discord.TextChannel):
            title = str(welcome_cfg.get("title", "Welcome!"))[:256]
            description = str(welcome_cfg.get("description", "Welcome {user} to {server}!"))
            description = (description.replace("{user}", member.mention)
                           .replace("{server}", member.guild.name)
                           .replace("{member_count}", str(member.guild.member_count or 0)))[:4000]
            embed = discord.Embed(title=title, description=description, color=discord.Color.blurple())
            thumbnail = str(welcome_cfg.get("thumbnail", ""))
            image = str(welcome_cfg.get("image", ""))
            if thumbnail.startswith(("https://", "http://")):
                embed.set_thumbnail(url=thumbnail)
            if image.startswith(("https://", "http://")):
                embed.set_image(url=image)
            await welcome_channel.send(embed=embed, allowed_mentions=discord.AllowedMentions(users=True))
    except (discord.Forbidden, discord.HTTPException, ValueError, TypeError):
        log.warning("Could not send welcome message for member %s", member.id)

    inviter = None
    used = None
    # Discord's invite-use count can update just after the member_join event.
    # Retry briefly before marking the inviter as unknown.
    for attempt in range(3):
        used = await find_used_invite(member.guild)
        if used and used.inviter:
            break
        if attempt < 2:
            await asyncio.sleep(1)

    if used and used.inviter:
        inviter = used.inviter
        inviter_id = inviter.id
        guild_member_inviter[(member.guild.id, member.id)] = inviter_id

        ensure_user(inviter_id)
        row = user_row(inviter_id)

        # Rejoin tracking: if this member was previously recorded as leaving
        # after being invited by this inviter, count as a rejoin and do not
        # create a fresh point.
        previous = db.execute(
            """
            SELECT 1 FROM settings
            WHERE key=?
            """,
            (f"left:{member.guild.id}:{member.id}",),
        ).fetchone()

        if previous:
            db.execute(
                """
                UPDATE users
                SET rejoins=rejoins+1
                WHERE user_id=?
                """,
                (inviter_id,),
            )
            db.execute(
                "DELETE FROM settings WHERE key=?",
                (f"left:{member.guild.id}:{member.id}",),
            )
        else:
            db.execute(
                """
                UPDATE users
                SET joins=joins+1, invite_points=invite_points+1
                WHERE user_id=?
                """,
                (inviter_id,),
            )
        db.commit()

    # Custom invite tracker announcement. Discord only exposes the inviter
    # reliably when the bot can read guild invites and detect the use-count change.
    try:
        invite_cfg = json_setting_get(
            f"invite_config:{member.guild.id}",
            json_setting_get("invite_config", {}),
        )
        invite_channel_id = invite_cfg.get("channel_id")
        invite_channel = member.guild.get_channel(int(invite_channel_id)) if invite_channel_id else None
        template = invite_cfg.get("message", "")
        if isinstance(invite_channel, discord.TextChannel) and template:
            inviter_name = inviter.display_name if inviter else "Unknown"
            announcement = (str(template)
                .replace("{user}", member.mention)
                .replace("{server}", member.guild.name)
                .replace("{invited}", inviter_name))
            await invite_channel.send(
                announcement[:2000],
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
    except (discord.Forbidden, discord.HTTPException, ValueError, TypeError):
        log.warning("Could not send invite tracker message for member %s", member.id)


@bot.event
async def on_member_remove(member):
    inviter_id = guild_member_inviter.get((member.guild.id, member.id))
    if inviter_id:
        db.execute(
            """
            UPDATE users
            SET leaves=leaves+1,
                invite_points=MAX(0, invite_points-1)
            WHERE user_id=?
            """,
            (inviter_id,),
        )
        db.execute(
            """
            INSERT OR REPLACE INTO settings(key,value)
            VALUES(?,?)
            """,
            (f"left:{member.guild.id}:{member.id}", "1"),
        )
        db.commit()


# ============================================================
# COMMON EMBEDS / UI
# ============================================================

def plan_name(node):
    return f"{node.title()}-{PLAN_STORAGE_LABEL}"


def progress_bar(percent):
    filled = round(percent / 10)
    filled = max(0, min(10, filled))
    return "[" + ("▓" * filled) + ("░" * (10 - filled)) + "]"


def expiry_text(expiry):
    delta = parse_dt(expiry) - utcnow()
    seconds = max(0, int(delta.total_seconds()))
    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60
    return f"{days}d {hours}h {minutes}m left"


def vps_status(vps):
    c = docker_get(vps) if docker_client else None
    if c:
        try:
            c.reload()
            return "Active" if c.status == "running" else "Stopped"
        except Exception:
            pass
    return vps["status"].title()


class VPSControlView(discord.ui.View):
    def __init__(self, vps_id, timeout=300):
        super().__init__(timeout=timeout)
        self.vps_id = vps_id

    @discord.ui.button(label="Start", style=discord.ButtonStyle.success)
    async def start_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_start(interaction, self.vps_id)

    @discord.ui.button(label="Stop", style=discord.ButtonStyle.danger)
    async def stop_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await handle_stop(interaction, self.vps_id)

    @discord.ui.button(label="Rebuild", style=discord.ButtonStyle.primary)
    async def rebuild_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RebuildModal(self.vps_id))

    @discord.ui.button(label="SSH", style=discord.ButtonStyle.secondary)
    async def ssh_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        vps = get_vps(self.vps_id)
        if not vps or vps["owner_id"] != interaction.user.id:
            return await interaction.response.send_message(
                "❌ You do not own this VPS.", ephemeral=True
            )
        await interaction.response.defer(ephemeral=True)
        await dm_routes(interaction.user, vps)
        await interaction.followup.send("✅ Connection details were sent to your DMs.", ephemeral=True)

    @discord.ui.button(label="Share", style=discord.ButtonStyle.secondary)
    async def share_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ShareModal(self.vps_id))


class PlanSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(
                label=f"{node.title()}-{PLAN_STORAGE_LABEL}",
                value=node,
                description=f"{node} • {PLAN_STORAGE_LABEL} • {INVITE_COST} invites required",
            )
            for node in NODES
        ]
        super().__init__(
            placeholder="Choose A Plan",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        node = self.values[0]
        await purchase_vps(interaction, node)


class BuyView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)
        self.add_item(PlanSelect())


class RebuildModal(discord.ui.Modal, title="Rebuild VPS"):
    os_name = discord.ui.TextInput(
        label="OS",
        placeholder="Ubuntu 24.04 LTS",
        default="Ubuntu 24.04 LTS",
        required=True,
        max_length=50,
    )

    def __init__(self, vps_id):
        super().__init__()
        self.vps_id = vps_id

    async def on_submit(self, interaction: discord.Interaction):
        await rebuild_vps(interaction, self.vps_id, str(self.os_name.value))


class ShareModal(discord.ui.Modal, title="Share VPS"):
    user = discord.ui.TextInput(
        label="Discord User ID",
        placeholder="Enter the target Discord user ID",
        required=True,
        max_length=25,
    )

    def __init__(self, vps_id):
        super().__init__()
        self.vps_id = vps_id

    async def on_submit(self, interaction: discord.Interaction):
        raw = str(self.user.value).strip().strip("<@!>")
        if not raw.isdigit():
            return await interaction.response.send_message(
                "❌ Enter a valid Discord user ID.", ephemeral=True
            )
        await share_vps(interaction, self.vps_id, int(raw))


# ============================================================
# DEPLOYMENT
# ============================================================

async def create_vps_for_user(
    user_id,
    node,
    consume_invites=False,
    created_by_admin=False,
    resource_override=None,
):
    ensure_user(user_id)
    user = user_row(user_id)

    if consume_invites:
        if user["invite_points"] < INVITE_COST:
            raise PermissionError("not eligible")

    vps_id = uuid.uuid4().hex[:10]
    node = node.upper()
    selected_node = node if node in NODES else pick_node()

    # Invite deployments use the smaller plan; admin deployments use the larger plan.
    ram_gb = ADMIN_RAM_GB if created_by_admin else INVITE_RAM_GB
    cpu_cores = ADMIN_CPU_CORES if created_by_admin else INVITE_CPU_CORES
    disk_gb = ADMIN_DISK_GB if created_by_admin else INVITE_DISK_GB
    if resource_override:
        ram_gb, cpu_cores, disk_gb = resource_override

    # Docker creation can take time.
    container = await run_blocking(docker_create_vps, vps_id, ram_gb, cpu_cores, disk_gb)

    created = utcnow()
    expiry = created + timedelta(days=EXPIRY_DAYS)

    db.execute(
        """
        INSERT INTO vps(
            id, owner_id, node, plan, os, container_id,
            created_at, expiry_at, status, vouched,
            ram_gb, cpu_cores, disk_gb
        )
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (
            vps_id,
            user_id,
            selected_node,
            f"{selected_node.title()}-{ram_gb}GB RAM • {cpu_cores} Core CPU • {disk_gb}GB Disk",
            SUPPORTED_OS,
            container.id,
            iso(created),
            iso(expiry),
            "stopped",
            0,
            ram_gb,
            cpu_cores,
            disk_gb,
        ),
    )

    if consume_invites:
        # Consume exactly the points used by the purchase.
        db.execute(
            """
            UPDATE users
            SET invite_points=invite_points-?
            WHERE user_id=? AND invite_points>=?
            """,
            (INVITE_COST, user_id, INVITE_COST),
        )

    db.commit()
    return get_vps(vps_id)


async def purchase_vps(interaction, node):
    await interaction.response.defer(ephemeral=True)
    user_id = interaction.user.id
    user = user_row(user_id)
    if user_id in _deploying_users or not has_free_slot(user_id):
        return await interaction.followup.send(f"❌ {interaction.user.mention} already has a VPS or another deployment is in progress.", ephemeral=True)

    if user["invite_points"] < INVITE_COST:
        embed = discord.Embed(
            title="❌ VPS Purchase Failed",
            color=discord.Color.red(),
        )
        embed.add_field(
            name="User",
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=False,
        )
        embed.add_field(name="Plan", value=f"`{plan_name(node)}`", inline=True)
        embed.add_field(name="Storage", value=f"`{PLAN_STORAGE_LABEL}`", inline=True)
        embed.add_field(name="Invite Requirement", value=f"`{INVITE_COST}`", inline=True)
        embed.add_field(name="Your Invites", value=f"`{user['invite_points']}`", inline=True)
        embed.add_field(
            name="Result",
            value="`Deployment canceled: not eligible`",
            inline=False,
        )
        return await interaction.followup.send(embed=embed, ephemeral=True)

    try:
        _deploying_users.add(user_id)
        vps = await create_vps_for_user(
            user_id, node, consume_invites=True, created_by_admin=False,
            resource_override=(BUY_RAM_GB, BUY_CPU_CORES, BUY_DISK_GB),
        )
    except Exception as exc:
        log.exception("Purchase failed")
        return await interaction.followup.send(f"❌ VPS deployment failed: `{str(exc)[:1000]}`", ephemeral=True)
    finally:
        _deploying_users.discard(user_id)

    updated = user_row(interaction.user.id)
    embed = discord.Embed(
        title="✅ VPS Purchase Successful",
        description=(
            f"**{PLAN_STORAGE_LABEL} purchased successfully and deployed to "
            f"`{vps['node']}`.**\nA receipt was sent to your DMs."
        ),
        color=discord.Color.green(),
    )
    embed.add_field(name="Plan", value=f"`{vps['plan']}`", inline=True)
    embed.add_field(name="Node", value=f"`{vps['node']}`", inline=True)
    embed.add_field(name="RAM", value=f"`{vps['ram_gb']}GB`", inline=True)
    embed.add_field(name="CPU", value=f"`{vps['cpu_cores']} cores`", inline=True)
    embed.add_field(name="Disk", value=f"`{vps['disk_gb']}GB`", inline=True)
    embed.add_field(name="Container ID", value=f"`{vps['id']}`", inline=False)
    embed.add_field(name="Invites Consumed", value=f"`-{INVITE_COST}`", inline=True)
    embed.add_field(name="Remaining Invites", value=f"`{updated['invite_points']}`", inline=True)
    await interaction.followup.send(embed=embed)

    try:
        dm = discord.Embed(
            title="🗣️ Vouch Required Before VPS Start",
            description=(
                f"Your VPS (`{vps['id']}`) from **{BOT_NAME}** is ready, "
                "but it is stopped until you vouch.\n\n"
                f"Send `/review Legit Got {PLAN_STORAGE_LABEL}` in "
                f"<#{VOUCH_CHANNEL_ID}>.\n"
                f"After you vouch, use `/start {vps['id']}`."
            ),
            color=discord.Color.orange(),
        )
        dm.add_field(name="🌐 Cluster Node", value=f"`{vps['node']}`", inline=True)
        dm.add_field(name="📅 Expiry", value=expiry_text(vps["expiry_at"]), inline=True)
        await interaction.user.send(embed=dm)
    except discord.Forbidden:
        pass


# ============================================================
# VOUCH SYSTEM
# ============================================================

VOUCH_RE = re.compile(
    r"\blegit\s+got\s+(\d+)\s*gb\s+vps\b",
    re.I,
)


# SiftAI can use a local Ollama model (no API key). If Ollama is not installed,
# a small built-in response engine still handles greetings and SiftNodes FAQs.
_ai_history = {}


def _ollama_chat_sync(user_id: int, prompt: str) -> str | None:
    import urllib.request
    history = _ai_history.setdefault(user_id, [])[-8:]
    messages = [{
        "role": "system",
        "content": (
            "You are SiftAI, the friendly assistant inside SiftNodes Discord. "
            "Reply clearly and naturally, preferably in the user's language. "
            "You can help write Python, Node.js, HTML, CSS, JSON, shell, and other code. "
            "When the user asks to create a file, return the complete file in one fenced code block with the correct language tag. "
            "When asked for a logo, thumbnail, or simple graphic, create an SVG and return it in a fenced svg code block. "
            "If asked who made/created you, say exactly: NotRyxenYT. "
            "Never claim to have performed actions you cannot perform."
        ),
    }]
    messages.extend(history)
    messages.append({"role": "user", "content": prompt[:2000]})
    payload = json.dumps({"model": OLLAMA_MODEL, "messages": messages, "stream": False}).encode()
    request = urllib.request.Request(
        OLLAMA_URL, data=payload,
        headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=18) as response:
            data = json.loads(response.read().decode("utf-8"))
        answer = str(data.get("message", {}).get("content", "")).strip()
        if answer:
            history.extend([{"role": "user", "content": prompt[:2000]}, {"role": "assistant", "content": answer}])
            _ai_history[user_id] = history[-10:]
            return answer[:12000]
    except Exception as exc:
        log.debug("Local Ollama AI unavailable: %s", exc)
    return None


def builtin_ai_reply(prompt: str) -> str:
    text = prompt.lower().strip()
    if re.search(r"\b(who (made|created|built) you|who is your (creator|developer)|tumhe kisne (banaya|bataya)|kisne banaya)\b", text):
        return "Mujhe NotRyxenYT ne banaya hai. 💜"
    if re.fullmatch(r"(hi|hii+|hello|hey|helo|yo|namaste|salam)[!. ]*", text):
        return "Hey! 👋 Main SiftAI hoon. Kya help chahiye?"
    if any(x in text for x in ("what can you do", "help me", "tum kya kar", "kya kar sakte")):
        return "Main SiftNodes ka AI assistant hoon. VPS, hosting, Discord bot aur general questions mein help kar sakta hoon. 💜"
    if any(x in text for x in ("siftnodes", "vps plan", "hosting plan", "how much ram", "vps specs")):
        return "SiftNodes VPS plans: `/buy` se 16GB RAM, 6-core CPU, 60GB disk plan milta hai. Admin se 12GB claim ke liye contact karo."
    if any(x in text for x in ("thank", "thanks", "shukriya", "dhanyavad")):
        return "You're welcome! 💜 Aur kuch help chahiye toh pucho."
    return ("Main SiftAI hoon 💜. Abhi local AI model connected nahi hai, isliye main is sawal ka detailed jawab nahi de pa raha. "
            "Full local AI ke liye server par Ollama aur model `qwen2.5:3b` install karna hoga — kisi API key ki zaroorat nahi.")


def extract_ai_file(prompt: str, answer: str):
    """Turn a requested fenced code artifact into a Discord attachment."""
    if not re.search(r"\b(make|create|generate|build|write|design)\b", prompt, re.I):
        return None
    match = re.search(r"```(python|py|javascript|js|node|nodejs|html|css|json|svg|xml|typescript|ts|bash|sh|yaml|yml|markdown|md|text)?\s*\n(.*?)```", answer, re.I | re.S)
    if not match:
        return None
    lang = (match.group(1) or "txt").lower()
    ext_map = {
        "python": "py", "py": "py", "javascript": "js", "js": "js", "node": "js",
        "nodejs": "js", "html": "html", "css": "css", "json": "json", "svg": "svg",
        "xml": "xml", "typescript": "ts", "ts": "ts", "bash": "sh", "sh": "sh",
        "yaml": "yml", "yml": "yml", "markdown": "md", "md": "md", "text": "txt",
    }
    ext = ext_map.get(lang, "txt")
    stem = "siftai_generated"
    prompt_low = prompt.lower()
    if "logo" in prompt_low:
        stem = "siftai_logo"
    elif "thumbnail" in prompt_low or "banner" in prompt_low:
        stem = "siftai_thumbnail"
    elif "python" in prompt_low:
        stem = "generated_script"
    elif "node" in prompt_low or "javascript" in prompt_low:
        stem = "generated_app"
    elif "html" in prompt_low:
        stem = "generated_page"
    code = match.group(2).strip()
    if not code:
        return None
    remaining = (answer[:match.start()] + answer[match.end():]).strip()
    return (f"{stem}.{ext}", code.encode("utf-8"), remaining[:1500])


async def get_ai_reply(user_id: int, prompt: str) -> str:
    # Keep common greetings and the creator answer instant, even without a local model.
    lower = prompt.lower().strip()
    if re.fullmatch(r"(hi|hii+|hello|hey|helo|yo|namaste|salam)[!. ]*", lower) or re.search(
        r"\b(who (made|created|built) you|who is your (creator|developer)|tumhe kisne (banaya|bataya)|kisne banaya)\b", lower
    ):
        return builtin_ai_reply(prompt)
    answer = await asyncio.to_thread(_ollama_chat_sync, user_id, prompt)
    return answer or builtin_ai_reply(prompt)


async def send_vouch_log(embed: discord.Embed, source_channel_id: int):
    """Send every real/invalid vouch report to the configured audit channel.

    Do not silently skip logs when the source channel happens to be the audit
    channel; the audit trail is intentional and the log channel is configurable.
    """
    if not VOUCH_LOG_CHANNEL_ID:
        log.error("VOUCH_LOG_CHANNEL_ID is not configured")
        return
    try:
        channel = bot.get_channel(VOUCH_LOG_CHANNEL_ID)
        if channel is None:
            channel = await bot.fetch_channel(VOUCH_LOG_CHANNEL_ID)
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception as exc:
        log.exception("Could not send vouch log to channel %s (source %s): %s", VOUCH_LOG_CHANNEL_ID, source_channel_id, exc)


@bot.event
async def on_message(message):
    if message.author.bot:
        return

    # Voice chat-reading is opt-in via .join and can be disabled with .readchat off.
    if message.guild and message.channel and guild_voice_settings(message.guild.id).get("read_chat"):
        if not message.content.startswith((".", "-", "/")):
            content = message.clean_content.strip()
            settings = guild_voice_settings(message.guild.id)
            if content and settings.get("read_channel_id") == message.channel.id:
                await speak_in_voice(message.guild, f"{message.author.display_name} says: {content}")

    # Non-prefix SiftAI: respond only in the configured AI chat channel.
    if message.guild and message.channel.id == AI_CHAT_CHANNEL_ID:
        content = message.content.strip()
        is_allowed_prefix_command = content.startswith(("-i", "-buy"))
        is_slash_command_text = content.startswith("/")
        if content and not is_allowed_prefix_command and not is_slash_command_text:
            async with message.channel.typing():
                reply = await get_ai_reply(message.author.id, content)
            generated_file = extract_ai_file(content, reply)
            try:
                if generated_file:
                    filename, file_bytes, explanation = generated_file
                    await message.reply(
                        content=(explanation or f"📎 Generated file: `{filename}`")[:1900],
                        file=discord.File(io.BytesIO(file_bytes), filename=filename),
                        mention_author=False,
                        allowed_mentions=discord.AllowedMentions.none(),
                    )
                else:
                    await message.reply(reply[:1900], mention_author=False, allowed_mentions=discord.AllowedMentions.none())
            except discord.HTTPException:
                await message.channel.send(reply[:1900], allowed_mentions=discord.AllowedMentions.none())

    # Vouch channel parser.
    if message.channel.id in {VOUCH_CHANNEL_ID, VOUCH_LOG_CHANNEL_ID}:
        match = VOUCH_RE.search(message.content)
        if match:
            requested_gb = int(match.group(1))
            owned = get_user_vps(message.author.id)

            # Record vouch for all stopped, unvouched VPSs owned by the user.
            candidates = [
                v for v in owned
                if not v["vouched"] and v["status"] == "stopped" and int(v["ram_gb"]) == requested_gb
            ]

            if not candidates:
                error_text = f"❌ {message.author.mention} has no awaiting `{requested_gb}GB` VPS claim. Admin use `/deploy`."
                await message.channel.send(error_text)
                fake_embed = discord.Embed(
                    title="⚠️ Fake / Invalid VPS Vouch Detected",
                    description="A vouch message did not match an awaiting VPS claim.",
                    color=discord.Color.red(),
                    timestamp=utcnow(),
                )
                fake_embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
                fake_embed.add_field(name="Claimed Vouch", value=f"`Legit Got {requested_gb}GB VPS`", inline=True)
                fake_embed.add_field(name="Result", value="`INVALID — no matching pending claim`", inline=True)
                fake_embed.add_field(name="Channel", value=message.channel.mention, inline=False)
                fake_embed.add_field(name="Message", value=message.content[:900] or "(empty)", inline=False)
                await send_vouch_log(fake_embed, message.channel.id)
            else:
                vps = candidates[0]
                db.execute(
                    "UPDATE vps SET vouched=1 WHERE id=?",
                    (vps["id"],),
                )
                db.commit()

                user = user_row(message.author.id)
                embed = discord.Embed(
                    title="✅ VPS Vouch Successfully",
                    color=discord.Color.green(),
                )
                embed.add_field(
                    name="User",
                    value=f"{message.author.mention} (`{message.author.id}`)",
                    inline=False,
                )
                embed.add_field(name="Plan", value=f"`{vps['plan']}`", inline=True)
                embed.add_field(name="Storage", value=f"`{vps['disk_gb']}GB`", inline=True)
                embed.add_field(name="Assigned Node", value=f"`{vps['node']}`", inline=True)
                embed.add_field(name="Invites Verified", value=f"`{INVITE_COST} total`", inline=True)
                embed.add_field(name="Points Consumed", value=f"`-{INVITE_COST}`", inline=True)
                embed.add_field(name="Remaining Invite Points", value=f"`{user['invite_points']}`", inline=True)
                embed.add_field(
                    name="Status",
                    value="`Eligible - deployment succeeded`",
                    inline=False,
                )
                embed.add_field(name="Container ID", value=f"`{vps['id']}`", inline=False)

                # Routes are generated only after the VPS is started.
                embed.add_field(
                    name="tmate",
                    value="```bash\nFailed to extract tmate route\n```",
                    inline=False,
                )
                embed.add_field(
                    name="sshx",
                    value="```text\ngenerated after /start\n```",
                    inline=False,
                )
                await message.channel.send(embed=embed)
                real_log = discord.Embed(
                    title="✅ Real VPS Vouch Verified",
                    description="Vouch matched a valid, awaiting VPS claim.",
                    color=discord.Color.green(),
                    timestamp=utcnow(),
                )
                real_log.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
                real_log.add_field(name="VPS Instance", value=f"`{vps['id']}`", inline=True)
                real_log.add_field(name="Plan", value=f"`{vps['ram_gb']}GB RAM • {vps['cpu_cores']} CPU • {vps['disk_gb']}GB Disk`", inline=True)
                real_log.add_field(name="Result", value="`REAL — claim verified`", inline=False)
                await send_vouch_log(real_log, message.channel.id)

                try:
                    await message.author.send(
                        f"✅ Vouch recorded for `{vps['id']}`. "
                        f"You can now use `/start {vps['id']}`."
                    )
                except discord.Forbidden:
                    pass
        elif re.search(r"\b(vouch|vps|legit|got)\b", message.content, re.I):
            # Catch malformed vouch attempts too, instead of silently ignoring them.
            fake_embed = discord.Embed(
                title="⚠️ Fake / Invalid VPS Vouch Detected",
                description="A vouch-like message did not match the required format `Legit Got <RAM>GB VPS`.",
                color=discord.Color.red(),
                timestamp=utcnow(),
            )
            fake_embed.add_field(name="User", value=f"{message.author.mention} (`{message.author.id}`)", inline=False)
            fake_embed.add_field(name="Result", value="`INVALID — malformed vouch format`", inline=True)
            fake_embed.add_field(name="Channel", value=message.channel.mention, inline=True)
            fake_embed.add_field(name="Message", value=message.content[:900] or "(empty)", inline=False)
            await send_vouch_log(fake_embed, message.channel.id)

    await bot.process_commands(message)


# ============================================================
# START / STOP / REBUILD / SHARE
# ============================================================

async def handle_start(interaction, vps_id):
    vps = get_vps(vps_id)
    if not vps:
        return await interaction.response.send_message(
            "❌ VPS not found.", ephemeral=True
        )
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ You do not own this VPS.", ephemeral=True
        )
    if not vps["vouched"] and not is_admin(interaction.user.id):
        return await interaction.response.send_message(
            "🗣️ You must vouch before starting this VPS.", ephemeral=True
        )

    if parse_dt(vps["expiry_at"]) <= utcnow():
        return await interaction.response.send_message(
            "❌ This VPS has expired.", ephemeral=True
        )

    await interaction.response.defer(ephemeral=True)
    try:
        await run_blocking(docker_start, vps)
        db.execute("UPDATE vps SET status='active' WHERE id=?", (vps_id,))
        db.commit()
        await interaction.followup.send("✅ VPS started successfully.", ephemeral=True)
        await dm_routes(interaction.user, vps)
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Failed to start VPS: `{str(exc)[:1000]}`",
            ephemeral=True,
        )


async def handle_stop(interaction, vps_id):
    vps = get_vps(vps_id)
    if not vps:
        return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message("❌ You do not own this VPS.", ephemeral=True)

    await interaction.response.defer(ephemeral=True)
    try:
        await run_blocking(docker_stop, vps)
        db.execute("UPDATE vps SET status='stopped' WHERE id=?", (vps_id,))
        db.commit()
        await interaction.followup.send("🛑 VPS stopped successfully.", ephemeral=True)
    except Exception as exc:
        await interaction.followup.send(
            f"❌ Failed to stop VPS: `{str(exc)[:1000]}`",
            ephemeral=True,
        )


async def rebuild_vps(interaction, vps_id, os_name):
    vps = get_vps(vps_id)
    if not vps:
        return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message("❌ You do not own this VPS.", ephemeral=True)

    try:
        image_for_os(os_name)
    except ValueError as exc:
        _deploying_users.discard(user.id)
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    await interaction.response.defer()

    progress_embed = discord.Embed(
        title="🔄 VPS Rebuild Progress",
        description=f"0-`{progress_bar(0)}` 0%",
        color=discord.Color.orange(),
    )
    progress_message = await interaction.followup.send(
        embed=progress_embed,
        wait=True,
    )

    try:
        # Old data is intentionally destroyed: rebuild is a full reinstall.
        await run_blocking(docker_stop, vps)
        await run_blocking(docker_remove, vps)

        for percent in (10, 25, 40, 55, 70, 85):
            await asyncio.sleep(0.35)
            progress_embed.description = f"{percent}-`{progress_bar(percent)}` {percent}%"
            await progress_message.edit(embed=progress_embed)

        new_container = await run_blocking(docker_create_vps, vps["id"], int(vps["ram_gb"]), int(vps["cpu_cores"]), int(vps["disk_gb"]))

        new_expiry = utcnow() + timedelta(days=EXPIRY_DAYS)
        db.execute(
            """
            UPDATE vps
            SET container_id=?,
                os=?,
                created_at=?,
                expiry_at=?,
                status='stopped',
                vouched=1
            WHERE id=?
            """,
            (
                new_container.id,
                SUPPORTED_OS,
                iso(utcnow()),
                iso(new_expiry),
                vps_id,
            ),
        )
        db.commit()

        vps = get_vps(vps_id)

        progress_embed.title = "🔄 VPS Rebuild Progress"
        progress_embed.description = (
            "✅ **VPS Rebuild Process Finished!**\n"
            f"`{progress_bar(100)}` 100%"
        )
        progress_embed.clear_fields()
        progress_embed.add_field(
            name="👤 Owner Tag",
            value=interaction.user.mention,
            inline=False,
        )
        progress_embed.add_field(
            name="📦 New Container ID",
            value=f"`{vps['id']}`",
            inline=False,
        )
        progress_embed.add_field(
            name="🌐 Cluster Node",
            value=f"`{vps['node']}`",
            inline=False,
        )
        progress_embed.add_field(
            name="🔒 Credentials",
            value="New tmate & sshx credentials will be sent directly to your DMs.",
            inline=False,
        )
        await progress_message.edit(embed=progress_embed)

        # Start briefly so routes can be generated.
        await run_blocking(docker_start, vps)
        db.execute("UPDATE vps SET status='active' WHERE id=?", (vps_id,))
        db.commit()

        await asyncio.sleep(2)
        await dm_routes(interaction.user, vps, title="🔄 Your VPS Rebuild Complete")

        # Stop again if the owner had not been actively using it.
        await run_blocking(docker_stop, vps)
        db.execute("UPDATE vps SET status='stopped' WHERE id=?", (vps_id,))
        db.commit()

    except Exception as exc:
        log.exception("Rebuild failed")
        progress_embed.title = "❌ VPS Rebuild Failed"
        progress_embed.description = f"`{str(exc)[:1500]}`"
        await progress_message.edit(embed=progress_embed)


async def share_vps(interaction, vps_id, target_id):
    vps = get_vps(vps_id)
    if not vps:
        return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message(
            "❌ Only the VPS owner/admin can share this VPS.",
            ephemeral=True,
        )
    if target_id == interaction.user.id:
        return await interaction.response.send_message(
            "❌ You already own this VPS.", ephemeral=True
        )

    target = bot.get_user(target_id)
    if target is None:
        try:
            target = await bot.fetch_user(target_id)
        except discord.NotFound:
            return await interaction.response.send_message(
                "❌ Target Discord user was not found.", ephemeral=True
            )

    db.execute(
        "UPDATE vps SET owner_id=? WHERE id=?",
        (target_id, vps_id),
    )
    db.commit()

    await interaction.response.send_message(
        f"✅ Full VPS access/ownership transferred to {target.mention}.",
        ephemeral=True,
    )
    try:
        await target.send(
            f"📦 VPS ownership transferred to you.\n"
            f"Container: `{vps_id}`\n"
            f"Node: `{vps['node']}`"
        )
    except discord.Forbidden:
        pass


# ============================================================
# SLASH COMMANDS
# ============================================================

@bot.tree.command(name="help", description="Show the SiftNodes VPS command dashboard.")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="✨ VPS Cloud Management Dashboard",
        description=(
            "Professional command reference and quick actions. "
            "Use per-VPS control cards from `/list` or deployment messages."
        ),
        color=discord.Color.blurple(),
    )
    embed.add_field(
        name="🚀 User Commands",
        value=(
            "`/buy` — Open the VPS purchase panel.\n"
            "`/i [user]` or `-i [@user]` — Show invite stats.\n"
            "`/slots buy` — Buy 1–10 VPS slots.\n"
            "`/sharevps @user` — Admin: create a 12GB claim.\n"
            "`/invite setup <channel> <msg>` — Configure join invite announcements (admin only).\n"
            "`/list` — Show your VPS instances and control buttons.\n"
            "`/start <id>` — Power on a container.\n"
            "`/stop <id>` — Stop a running container.\n"
            "`/regen_ssh <id>` — Generate fresh tmate/sshx routes.\n"
            "`/rebuild <id> <os>` — Full reinstall; expiry resets to 29 days.\n"
            "`/renew <id> <code>` — Extend expiry with a redeem code.\n"
            "`/renewvps` — Open invite renewal rewards.\n"
            "`/message` or `/m` — Show message credits.\n"
            "`/claimmessage` — Show message-plan claims.\n"
            "`/share <id>` — Transfer full VPS ownership to another user.\n"
            "`/review` — Vouch command information."
        ),
        inline=False,
    )
    embed.add_field(
        name="👑 Admin Commands",
        value=(
            "`/deploy @user <os> <node>` or `-deploy @user [node]` — Admin 12GB VPS.\n"
            "`/clearnode <node>` — Remove all containers on a node.\n"
            "`/usercleanvps @user` — Remove all VPSes owned by a user.\n"
            "`/redeemcreate <days> [code]` — Create a redeem code.\n"
            "`/renewadd <plan> <invites> [days]` — Configure invite renewal reward.\n"
            "`/giveaway <time> <reward> <winners>` — Create a giveaway record.\n"
            "`/invitesadd @user <amount>` — Add invite points.\n"
            "`/invitesremove @user <amount>` — Remove invite points.\n"
            "`/invitereseteveryone` — Reset all invite points.\n"
            "`/messageadd @user <amount>` / `/messageremove` — Adjust message credits.\n"
            "`/messagereset @user` — Reset message credits.\n"
            "`/planmessageadd <plan> <specs> <messages>` — Configure message plan.\n"
            "`/ticketauto [enabled]` — Toggle ticket auto-deployment setting.\n"
            "`/admin_add @user` / `/admin_remove @user` — Manage bot admins.\n"
            "`/nmsg <channel> <message>` — Send a message to a channel (admin only)."
        ),
        inline=False,
    )
    embed.set_footer(text="SiftNodes • Docker VPS Management")
    await interaction.response.send_message(embed=embed)


SLOTS_GROUP = app_commands.Group(name="slots", description="Buy additional VPS slots")

class SlotPurchaseSelect(discord.ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=f"{n} Slot" if n == 1 else f"{n} Slots", value=str(n), description=f"Cost: {cost} invite points") for n, cost in SLOT_COSTS.items()]
        super().__init__(placeholder="Choose your total slot limit", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        requested = int(self.values[0])
        row = user_row(interaction.user.id)
        current = int(row["slots"] or 1)
        if requested <= current:
            return await interaction.response.send_message(f"❌ You already have `{current}` slot(s). Choose a higher tier.", ephemeral=True)
        cost = SLOT_COSTS[requested]
        if row["invite_points"] < cost:
            return await interaction.response.send_message(f"❌ You need `{cost}` invite points; you have `{row['invite_points']}`.", ephemeral=True)
        db.execute("UPDATE users SET slots=?, invite_points=invite_points-? WHERE user_id=?", (requested, cost, interaction.user.id))
        db.commit()
        await interaction.response.send_message(f"✅ Your slot limit is now `{requested}`. Cost: `{cost} inv`. VPS usage: `{count_user_vps(interaction.user.id)}/{requested}`.", ephemeral=True)

class SlotPurchaseView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=180)
        self.add_item(SlotPurchaseSelect())

@SLOTS_GROUP.command(name="buy", description="Buy a VPS slot tier from 1 to 10")
async def slots_buy(interaction: discord.Interaction):
    row = user_row(interaction.user.id)
    embed = discord.Embed(title="🧩 Buy VPS Slots", description=f"Current slot limit: `{row['slots']}`\nSelect your total slot limit. Costs are invite points; maximum total is 10.", color=discord.Color.blurple())
    embed.add_field(name="Prices", value="\n".join(f"`{n} {'Slot' if n == 1 else 'Slots'}` — `{cost} inv`" for n, cost in SLOT_COSTS.items()), inline=False)
    await interaction.response.send_message(embed=embed, view=SlotPurchaseView(), ephemeral=True)

bot.tree.add_command(SLOTS_GROUP)


@bot.tree.command(name="buy", description="Open the SiftNodes VPS purchase panel.")
async def buy(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🛒 VPS Purchase Panel",
        description=(
            "Choose a plan from the database below. "
            f"This plan provides `16GB RAM • 6 Core CPU • 60GB Disk` and requires `{INVITE_COST}` invite points. Buy more slots with `/slots buy`."
        ),
        color=discord.Color.blurple(),
    )
    lines = []
    for index, node in enumerate(NODES, 1):
        lines.append(
            f"**Plan {index}: {plan_name(node)}**\n"
            f"`{node}` • `16GB RAM • 6 Core CPU • 60GB Disk` • `{INVITE_COST} invites required`"
        )
    embed.description += "\n\n" + "\n\n".join(lines)
    await interaction.response.send_message(embed=embed, view=BuyView())


@bot.tree.command(name="i", description="Show your own invite stats or check another user.")
@app_commands.describe(user="Optional user whose invite stats you want to see")
async def invite_stats(interaction: discord.Interaction, user: discord.User = None):
    target = user or interaction.user
    row = user_row(target.id)
    embed = discord.Embed(
        title="Server Invite Statistics",
        color=discord.Color.blurple(),
    )
    embed.set_author(
        name=f"{target.display_name} Invite Statistics",
        icon_url=target.display_avatar.url,
    )
    embed.add_field(name="Net Invites / Points", value=f"`{row['invite_points']}`", inline=False)
    embed.add_field(name="🟢 Joins", value=f"`{row['joins']}`", inline=True)
    embed.add_field(name="🔴 Left", value=f"`{row['leaves']}`", inline=True)
    embed.add_field(name="⚠️ Fake", value=f"`{row['fake']}`", inline=True)
    embed.add_field(name="🔄 Rejoins (14d)", value=f"`{row['rejoins']}`", inline=True)
    embed.add_field(
        name="Invite Points",
        value=f"`{row['invite_points']}` available for VPS purchases",
        inline=False,
    )
    embed.set_footer(text=f"Requested by {interaction.user.display_name}")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="invites", description="Alias for /i.")
async def invites_alias(interaction: discord.Interaction):
    await invite_stats.callback(interaction)


@bot.tree.command(name="list", description="List your SiftNodes VPS instances.")
async def list_vps(interaction: discord.Interaction):
    """Send one identical-format VPS card per message, in order."""
    rows = get_user_vps(interaction.user.id)
    if not rows:
        return await interaction.response.send_message(
            "📦 You do not have any VPS instances.", ephemeral=True
        )

    for index, vps in enumerate(rows):
        embed = discord.Embed(
            title=f"📦 Container {vps['id']}",
            color=discord.Color.green() if vps_status(vps) == "Active" else discord.Color.orange(),
        )
        embed.add_field(name="🌐 Cluster Node", value=f"**{vps['node']}**", inline=False)
        embed.add_field(name="🛠️ Status", value=vps_status(vps), inline=True)
        embed.add_field(
            name="📅 Expiry",
            value=f"`{parse_dt(vps['expiry_at']).strftime('%Y-%m-%d %H:%M UTC')}`\n"
                  f"({expiry_text(vps['expiry_at'])})",
            inline=True,
        )
        embed.add_field(name="🧠 RAM", value=f"`{vps['ram_gb']}GB`", inline=True)
        embed.add_field(name="⚙️ CPU", value=f"`{vps['cpu_cores']} cores`", inline=True)
        embed.add_field(name="💾 Disk", value=f"`{vps['disk_gb']}GB`", inline=True)

        # First VPS card responds to the interaction; remaining cards are
        # separate follow-up messages sent immediately one after another.
        if index == 0:
            await interaction.response.send_message(embed=embed, view=VPSControlView(vps["id"]))
        else:
            await interaction.followup.send(embed=embed, view=VPSControlView(vps["id"]))


@bot.tree.command(name="start", description="Start a VPS container.")
@app_commands.describe(container_id="VPS container ID")
async def start(interaction: discord.Interaction, container_id: str):
    await handle_start(interaction, container_id)


@bot.tree.command(name="stop", description="Stop a VPS container.")
@app_commands.describe(container_id="VPS container ID")
async def stop(interaction: discord.Interaction, container_id: str):
    await handle_stop(interaction, container_id)


@bot.tree.command(name="regen_ssh", description="Regenerate tmate and sshx routes.")
@app_commands.describe(container_id="VPS container ID")
async def regen_ssh(interaction: discord.Interaction, container_id: str):
    vps = get_vps(container_id)
    if not vps:
        return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message("❌ You do not own this VPS.", ephemeral=True)

    await interaction.response.send_message("🔄 SSH booting... **10%**", ephemeral=True)

    async def update_progress(percent):
        try:
            await interaction.edit_original_response(content=f"🔄 SSH booting... **{percent}%**")
        except (discord.HTTPException, discord.NotFound):
            pass

    tmate_route, sshx_route = await generate_routes(vps, progress_callback=update_progress)
    embed = discord.Embed(
        title="▶️ Your VPS SSH Booted",
        color=discord.Color.green(),
    )
    embed.add_field(name="📦 Instance ID", value=f"`{vps['id']}`", inline=False)
    embed.add_field(name="🌐 Cluster Node", value=f"`{vps['node']}`", inline=False)
    embed.add_field(name="🔑 tmate SSH Command:", value=f"```bash\n{tmate_route}\n```", inline=False)
    embed.add_field(name="🔗 sshx Web Link:", value=f"```text\n{sshx_route}\n```", inline=False)
    try:
        await interaction.user.send(embed=embed)
        await interaction.edit_original_response(content="✅ SSH boot completed — connection details sent to your DMs.")
    except discord.Forbidden:
        await interaction.edit_original_response(content="❌ I couldn't DM you. Enable DMs from server members and run `/regen_ssh` again.")


@bot.tree.command(name="rebuild", description="Fully reinstall a VPS with Ubuntu 24.04 LTS.")
async def rebuild(interaction: discord.Interaction, container_id: str, os: str):
    await rebuild_vps(interaction, container_id, os)


@bot.tree.command(name="share", description="Transfer full VPS ownership to another Discord user.")
async def share(interaction: discord.Interaction, container_id: str, user: discord.User):
    await share_vps(interaction, container_id, user.id)


@bot.tree.command(name="siftai", description="Check SiftAI status and local model configuration.")
async def siftai_status(interaction: discord.Interaction):
    embed = discord.Embed(
        title="🤖 SiftAI Status",
        description=f"Non-prefix chat is enabled only in <#{AI_CHAT_CHANNEL_ID}>.",
        color=discord.Color.from_rgb(120, 70, 255),
    )
    embed.add_field(name="Creator", value="`NotRyxenYT`", inline=True)
    embed.add_field(name="AI Engine", value=f"Local Ollama • `{OLLAMA_MODEL}`", inline=True)
    embed.add_field(name="API Key", value="Not required", inline=True)
    embed.add_field(name="Fallback", value="Built-in replies work even if Ollama is offline; broad AI answers require the local model.", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="botinfo", description="Show public SiftNodes bot features.")
async def botinfo(interaction: discord.Interaction):
    embed = discord.Embed(
        title="💜 SiftNodes — Public Discord Bot",
        description="Hosting management and community tools in one bot.",
        color=discord.Color.from_rgb(120, 70, 255),
    )
    embed.add_field(name="Hosting", value="VPS purchase, deploy, start/stop, rebuild, renew, slots, invite tracking", inline=False)
    embed.add_field(name="Community", value="Tickets, giveaways, autorole, welcome messages, invite announcements", inline=False)
    embed.add_field(name="AI", value=f"SiftAI chat in <#{AI_CHAT_CHANNEL_ID}> • local model, no third-party API key", inline=False)
    embed.add_field(name="Vouch Audit", value=f"Real/invalid vouch logs: <#{VOUCH_LOG_CHANNEL_ID}>", inline=False)
    embed.set_footer(text="Made by NotRyxenYT")
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="review", description="Show the VPS vouch format.")
async def review(interaction: discord.Interaction, text: str):
    await interaction.response.send_message(
        f"📝 Vouch received. Please send `Legit Got {PLAN_STORAGE_LABEL} Vps` "
        f"in <#{VOUCH_CHANNEL_ID}> to activate your pending VPS.",
        ephemeral=True,
    )


@bot.tree.command(name="renew", description="Renew a VPS with a redeem code.")
async def renew(interaction: discord.Interaction, container_id: str, code: str):
    vps = get_vps(container_id)
    if not vps:
        return await interaction.response.send_message("❌ VPS not found.", ephemeral=True)
    if vps["owner_id"] != interaction.user.id and not is_admin(interaction.user.id):
        return await interaction.response.send_message("❌ You do not own this VPS.", ephemeral=True)

    row = db.execute(
        "SELECT * FROM redeem_codes WHERE code=? AND used_by IS NULL",
        (code.upper(),),
    ).fetchone()
    if not row:
        return await interaction.response.send_message("❌ Invalid or already-used code.", ephemeral=True)

    base = max(utcnow(), parse_dt(vps["expiry_at"]))
    expiry = base + timedelta(days=row["days"])

    db.execute(
        "UPDATE vps SET expiry_at=? WHERE id=?",
        (iso(expiry), container_id),
    )
    db.execute(
        "UPDATE redeem_codes SET used_by=?, used_at=? WHERE code=?",
        (interaction.user.id, iso(utcnow()), code.upper()),
    )
    db.commit()

    await interaction.response.send_message(
        f"✅ VPS renewed for `{row['days']} days`.\n"
        f"New expiry: `{expiry.strftime('%Y-%m-%d %H:%M UTC')}`"
    )


@bot.tree.command(name="renewvps", description="Show invite renewal rewards.")
async def renewvps(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT plan, invites, days FROM renewal_rewards ORDER BY invites"
    ).fetchall()
    if not rows:
        return await interaction.response.send_message(
            "ℹ️ No invite renewal rewards are configured yet."
        )
    embed = discord.Embed(title="🔄 VPS Renewal Rewards", color=discord.Color.blurple())
    for r in rows:
        embed.add_field(
            name=r["plan"],
            value=f"`{r['invites']} invites` → `{r['days']} days`",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="message", description="Show your message credits.")
async def message_credits(interaction: discord.Interaction):
    row = user_row(interaction.user.id)
    await interaction.response.send_message(
        f"💬 You have `{row['message_credits']}` message credits."
    )


@bot.tree.command(name="m", description="Alias for /message.")
async def m(interaction: discord.Interaction):
    await message_credits.callback(interaction)


@bot.tree.command(name="claimmessage", description="Show available message-credit plans.")
async def claimmessage(interaction: discord.Interaction):
    rows = db.execute(
        "SELECT plan, specs, messages FROM message_plans ORDER BY plan"
    ).fetchall()
    if not rows:
        return await interaction.response.send_message(
            "ℹ️ No message plans are configured yet."
        )
    embed = discord.Embed(title="💬 Message Plans", color=discord.Color.blurple())
    for r in rows:
        embed.add_field(
            name=r["plan"],
            value=f"{r['specs']}\n`{r['messages']} messages`",
            inline=False,
        )
    await interaction.response.send_message(embed=embed)


# ============================================================
# ADMIN COMMANDS
# ============================================================

def admin_only():
    async def predicate(interaction):
        if not is_admin(interaction.user.id):
            raise app_commands.CheckFailure("Admin only.")
        return True
    return app_commands.check(predicate)


@bot.tree.command(name="sharevps", description="Admin: create a 12GB VPS claim for a user.")
@admin_only()
async def sharevps(interaction: discord.Interaction, user: discord.User, node: str = "AUTO"):
    user_row(user.id)
    if user.id in _deploying_users or not has_free_slot(user.id):
        return await interaction.response.send_message(f"❌ {user.mention} already has a VPS or another deployment is in progress.", ephemeral=True)
    await interaction.response.defer(ephemeral=True)
    _deploying_users.add(user.id)
    try:
        vps = await create_vps_for_user(user.id, node.upper(), consume_invites=False, created_by_admin=False)
        await interaction.followup.send(f"✅ 12GB VPS claim created for {user.mention}. Container: `{vps['id']}`. They must vouch `Legit Got 12GB Vps` in <#{VOUCH_CHANNEL_ID}> before starting.", ephemeral=True)
        try:
            await user.send(f"📦 **SiftNodes 12GB VPS Claim**\nContainer: `{vps['id']}`\nNode: `{vps['node']}`\nVouch `Legit Got 12GB Vps` in <#{VOUCH_CHANNEL_ID}> before starting.")
        except discord.Forbidden:
            pass
    except Exception as exc:
        log.exception("/sharevps failed")
        await interaction.followup.send(f"❌ VPS deployment failed: `{str(exc)[:900]}`", ephemeral=True)
    finally:
        _deploying_users.discard(user.id)


@bot.tree.command(name="deploy", description="Admin: deploy an Ubuntu 24.04 VPS for a user.")
@app_commands.describe(user="User receiving the VPS", os="Operating system", node="Cluster node")
@app_commands.choices(
    os=[app_commands.Choice(name="Ubuntu 24.04 LTS", value="Ubuntu 24.04 LTS")],
    node=[app_commands.Choice(name=("Node " + str(i)), value=f"NODE{i}") for i in range(1, 11)] + [app_commands.Choice(name="localnode", value="LOCALNODE")],
)
@admin_only()
async def deploy(
    interaction: discord.Interaction,
    user: discord.User,
    os: app_commands.Choice[str],
    node: app_commands.Choice[str],
):
    if user.id in _deploying_users:
        return await interaction.response.send_message(f"❌ {user.mention} already has a VPS or another deployment is in progress.", ephemeral=True)
    _deploying_users.add(user.id)
    try:
        image_for_os(os.value)
    except ValueError as exc:
        _deploying_users.discard(user.id)
        return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)

    await interaction.response.defer()
    try:
        vps = await create_vps_for_user(
            user.id,
            node.value,
            consume_invites=False,
            created_by_admin=True,
        )
        await interaction.followup.send(
            f"✅ VPS deployed for {user.mention} on `{vps['node']}`.\n"
            f"Container: `{vps['id']}`"
        )
        try:
            await user.send(
                f"📦 **SiftNodes VPS Created**\n"
                f"Container: `{vps['id']}`\n"
                f"Node: `{vps['node']}`\n"
                f"RAM: `{vps['ram_gb']}GB` • CPU: `{vps['cpu_cores']} cores` • Disk: `{vps['disk_gb']}GB`\n"
                f"Expiry: `{parse_dt(vps['expiry_at']).strftime('%Y-%m-%d %H:%M UTC')}`\n"
                f"Use `/start {vps['id']}` to start your VPS."
            )
            await dm_routes(user, vps, title="🔐 Your New Admin-Deployed VPS")
        except discord.Forbidden:
            pass
    except Exception as exc:
        await interaction.followup.send(f"❌ Deployment failed: `{str(exc)[:1000]}`")
    finally:
        _deploying_users.discard(user.id)


@bot.tree.command(name="nmsg", description="Admin only: send a message to a channel.")
@admin_only()
@app_commands.describe(channel="Channel where the bot should send the message", message="Message text to send")
async def nmsg(interaction: discord.Interaction, channel: discord.TextChannel, message: str):
    # Send in chunks to stay below Discord's 2000-character message limit.
    chunks = [message[i:i + 1900] for i in range(0, len(message), 1900)]
    await interaction.response.defer(ephemeral=True, thinking=True)
    try:
        allowed = discord.AllowedMentions(everyone=True, users=True, roles=True)
        sent = 0
        for chunk in chunks:
            await channel.send(chunk, allowed_mentions=allowed)
            sent += 1
        await interaction.followup.send(
            f"✅ Message sent to {channel.mention} ({sent} part(s)).", ephemeral=True
        )
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ I don't have permission to send messages in that channel.", ephemeral=True
        )
    except discord.HTTPException as exc:
        await interaction.followup.send(
            f"❌ Discord could not send the message: `{str(exc)[:500]}`", ephemeral=True
        )


@bot.tree.command(name="clearnode", description="Admin: remove all VPS containers on a node.")
@admin_only()
async def clearnode(interaction: discord.Interaction, node: str):
    node = node.upper()
    if node not in NODES:
        return await interaction.response.send_message("❌ Invalid node.", ephemeral=True)

    rows = db.execute("SELECT * FROM vps WHERE node=?", (node,)).fetchall()
    await interaction.response.defer()
    removed = 0
    for vps in rows:
        try:
            await run_blocking(docker_remove, vps)
            removed += 1
        except Exception:
            pass
        db.execute("DELETE FROM vps WHERE id=?", (vps["id"],))
    db.commit()
    await interaction.followup.send(f"✅ Cleared `{removed}` VPS(s) from `{node}`.")


@bot.tree.command(name="usercleanvps", description="Admin: remove every VPS owned by a user.")
@admin_only()
async def usercleanvps(interaction: discord.Interaction, user: discord.User):
    rows = get_user_vps(user.id)
    await interaction.response.defer()
    removed = 0
    for vps in rows:
        try:
            await run_blocking(docker_remove, vps)
        except Exception:
            pass
        db.execute("DELETE FROM vps WHERE id=?", (vps["id"],))
        removed += 1
    db.commit()
    await interaction.followup.send(f"✅ Removed `{removed}` VPS(s) from {user.mention}.")


@bot.tree.command(name="redeemcreate", description="Admin: create a VPS renewal redeem code.")
@admin_only()
async def redeemcreate(interaction: discord.Interaction, days: int, code: str = ""):
    if days <= 0:
        return await interaction.response.send_message("❌ Days must be positive.", ephemeral=True)
    if not code:
        code = "PRIME-" + uuid.uuid4().hex[:8].upper()
    code = code.upper()

    try:
        db.execute(
            "INSERT INTO redeem_codes(code,days) VALUES(?,?)",
            (code, days),
        )
        db.commit()
    except sqlite3.IntegrityError:
        return await interaction.response.send_message("❌ Code already exists.", ephemeral=True)

    await interaction.response.send_message(
        f"✅ Redeem code created: `{code}` — `{days} days`"
    )


@bot.tree.command(name="renewadd", description="Admin: configure an invite renewal reward.")
@admin_only()
async def renewadd(interaction: discord.Interaction, plan: str, invites: int, days: int = 29):
    if invites <= 0 or days <= 0:
        return await interaction.response.send_message("❌ Values must be positive.", ephemeral=True)
    db.execute(
        "INSERT INTO renewal_rewards(plan,invites,days) VALUES(?,?,?)",
        (plan, invites, days),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ Renewal reward added: `{plan}` • `{invites} invites` → `{days} days`."
    )


# ============================================================
# GIVEAWAYS: join button, persistent entries, manual/automatic ending
# ============================================================


def parse_duration(value: str) -> timedelta:
    match = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", value.lower())
    if not match:
        raise ValueError("Use a duration like `30m`, `2h`, `1d`, or `45s`.")
    amount = int(match.group(1))
    unit = match.group(2)
    if amount <= 0:
        raise ValueError("Duration must be greater than zero.")
    factor = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
    if amount * factor > 365 * 86400:
        raise ValueError("Giveaway duration cannot exceed 365 days.")
    return timedelta(seconds=amount * factor)


class GiveawayJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Enter Giveaway", emoji="🎉", style=discord.ButtonStyle.success, custom_id="siftnodes_giveaway_enter")
    async def enter_giveaway(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.bot or not interaction.message:
            return await interaction.response.send_message("Bots cannot enter giveaways.", ephemeral=True)
        row = db.execute("SELECT ended FROM giveaways WHERE message_id=?", (interaction.message.id,)).fetchone()
        if not row:
            return await interaction.response.send_message("❌ This giveaway is not registered anymore.", ephemeral=True)
        if row["ended"]:
            return await interaction.response.send_message("⏰ This giveaway has ended.", ephemeral=True)
        try:
            db.execute(
                "INSERT INTO giveaway_entries(message_id,user_id,joined_at) VALUES(?,?,?)",
                (interaction.message.id, interaction.user.id, iso(utcnow())),
            )
            db.commit()
            await interaction.response.send_message("✅ You entered the giveaway! Good luck!", ephemeral=True)
        except sqlite3.IntegrityError:
            await interaction.response.send_message("ℹ️ You already entered this giveaway.", ephemeral=True)


async def finish_giveaway(message_id: int, ended_by: int | None = None):
    row = db.execute("SELECT * FROM giveaways WHERE message_id=?", (message_id,)).fetchone()
    if not row or row["ended"]:
        return False, "This giveaway does not exist or has already ended."
    entrants = [r["user_id"] for r in db.execute(
        "SELECT user_id FROM giveaway_entries WHERE message_id=?", (message_id,)
    ).fetchall()]
    winner_ids = random.sample(entrants, min(int(row["winners"]), len(entrants))) if entrants else []
    db.execute("UPDATE giveaways SET ended=1 WHERE message_id=?", (message_id,))
    db.commit()
    channel = bot.get_channel(int(row["channel_id"]))
    if channel is None:
        try:
            channel = await bot.fetch_channel(int(row["channel_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return True, "Giveaway marked ended, but its channel could not be fetched."
    try:
        message = await channel.fetch_message(message_id)
        entry_count = len(entrants)
        embed = discord.Embed(
            title="🏁 Giveaway Ended",
            description=f"**Prize:** {row['reward']}\n**Entries:** {entry_count}\n**Host:** <@{row['host_id']}>",
            color=discord.Color.dark_grey(),
            timestamp=utcnow(),
        )
        if winner_ids:
            embed.add_field(name="🎉 Winner(s)", value="\n".join(f"<@{uid}>" for uid in winner_ids), inline=False)
        else:
            embed.add_field(name="Winner(s)", value="No valid entries; no winner selected.", inline=False)
        await message.edit(embed=embed, view=None)
        if winner_ids:
            await channel.send(
                f"🎉 Congratulations {' '.join(f'<@{uid}>' for uid in winner_ids)}! "
                f"You won **{row['reward']}**!",
                allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
            )
        else:
            await channel.send(f"ℹ️ Giveaway for **{row['reward']}** ended with no entries.")
    except (discord.Forbidden, discord.HTTPException) as exc:
        log.warning("Could not update giveaway %s: %s", message_id, exc)
    return True, "Giveaway ended."


@tasks.loop(seconds=30)
async def giveaway_worker():
    now = iso(utcnow())
    due = db.execute("SELECT message_id FROM giveaways WHERE ended=0 AND ends_at<=?", (now,)).fetchall()
    for row in due:
        try:
            await finish_giveaway(int(row["message_id"]))
        except Exception:
            log.exception("Could not automatically end giveaway %s", row["message_id"])


class GiveawayCommandGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="giveaway", description="Create, enter, and end community giveaways")

    @app_commands.command(name="create", description="Admin: create a giveaway with a join button")
    @app_commands.describe(duration="Examples: 30m, 2h, 1d", reward="Prize", winners="Number of winners", channel="Where to post it")
    @admin_only()
    async def create(self, interaction: discord.Interaction, duration: str, reward: str, winners: app_commands.Range[int, 1, 20], channel: discord.TextChannel = None):
        try:
            delta = parse_duration(duration)
        except ValueError as exc:
            return await interaction.response.send_message(f"❌ {exc}", ephemeral=True)
        target = channel or (interaction.channel if isinstance(interaction.channel, discord.TextChannel) else None)
        if target is None:
            return await interaction.response.send_message("❌ Choose a text channel.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        end_time = utcnow() + delta
        embed = discord.Embed(
            title="🎉 SiftNodes Giveaway",
            description=f"**Prize:** {reward[:500]}\n**Winners:** {winners}\n**Host:** {interaction.user.mention}\n\nClick **Enter Giveaway** below to participate.",
            color=discord.Color.from_rgb(120, 70, 255),
            timestamp=end_time,
        )
        embed.set_footer(text="Ends at • UTC")
        try:
            msg = await target.send(embed=embed, view=GiveawayJoinView())
        except discord.Forbidden:
            return await interaction.followup.send("❌ I cannot send messages in that channel.", ephemeral=True)
        db.execute(
            "INSERT OR REPLACE INTO giveaways(message_id,guild_id,channel_id,host_id,reward,winners,ends_at,ended) VALUES(?,?,?,?,?,?,?,0)",
            (msg.id, interaction.guild_id, target.id, interaction.user.id, reward[:500], int(winners), iso(end_time)),
        )
        db.commit()
        await interaction.followup.send(f"✅ Giveaway created in {target.mention}. Message ID: `{msg.id}`", ephemeral=True)

    @app_commands.command(name="end", description="Admin: end a giveaway early using its message ID")
    @app_commands.describe(message_id="Message ID of the giveaway post")
    @admin_only()
    async def end(self, interaction: discord.Interaction, message_id: str):
        try:
            parsed_id = int(message_id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ Message ID must be a number.", ephemeral=True)
        await interaction.response.defer(ephemeral=True)
        ok, message = await finish_giveaway(parsed_id, interaction.user.id)
        await interaction.followup.send(("✅ " if ok else "❌ ") + message, ephemeral=True)


bot.tree.add_command(GiveawayCommandGroup())


@bot.tree.command(name="invitesadd", description="Admin: add invite points.")
@admin_only()
async def invitesadd(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
    ensure_user(user.id)
    db.execute(
        "UPDATE users SET invite_points=invite_points+? WHERE user_id=?",
        (amount, user.id),
    )
    db.commit()
    row = user_row(user.id)
    await interaction.response.send_message(
        f"✅ Added `{amount}` invite points to {user.mention}. Total: `{row['invite_points']}`."
    )


@bot.tree.command(name="invitesremove", description="Admin: remove invite points.")
@admin_only()
async def invitesremove(interaction: discord.Interaction, user: discord.User, amount: int):
    if amount <= 0:
        return await interaction.response.send_message("❌ Amount must be positive.", ephemeral=True)
    ensure_user(user.id)
    db.execute(
        "UPDATE users SET invite_points=MAX(0,invite_points-?) WHERE user_id=?",
        (amount, user.id),
    )
    db.commit()
    row = user_row(user.id)
    await interaction.response.send_message(
        f"✅ Removed `{amount}` invite points from {user.mention}. Total: `{row['invite_points']}`."
    )


@bot.tree.command(name="invitereseteveryone", description="Admin: reset all invite points.")
@admin_only()
async def invitereseteveryone(interaction: discord.Interaction):
    db.execute("UPDATE users SET invite_points=0")
    db.commit()
    await interaction.response.send_message("✅ All invite points have been reset to `0`.")


@bot.tree.command(name="messageadd", description="Admin: add message credits.")
@admin_only()
async def messageadd(interaction: discord.Interaction, user: discord.User, amount: int):
    ensure_user(user.id)
    db.execute(
        "UPDATE users SET message_credits=message_credits+? WHERE user_id=?",
        (amount, user.id),
    )
    db.commit()
    await interaction.response.send_message("✅ Message credits added.")


@bot.tree.command(name="messageremove", description="Admin: remove message credits.")
@admin_only()
async def messageremove(interaction: discord.Interaction, user: discord.User, amount: int):
    ensure_user(user.id)
    db.execute(
        "UPDATE users SET message_credits=MAX(0,message_credits-?) WHERE user_id=?",
        (amount, user.id),
    )
    db.commit()
    await interaction.response.send_message("✅ Message credits removed.")


@bot.tree.command(name="messagereset", description="Admin: reset a user's message credits.")
@admin_only()
async def messagereset(interaction: discord.Interaction, user: discord.User):
    ensure_user(user.id)
    db.execute(
        "UPDATE users SET message_credits=0 WHERE user_id=?",
        (user.id,),
    )
    db.commit()
    await interaction.response.send_message("✅ Message credits reset.")


@bot.tree.command(name="planmessageadd", description="Admin: configure a message-credit plan.")
@admin_only()
async def planmessageadd(
    interaction: discord.Interaction,
    plan: str,
    specs: str,
    messages: int,
):
    db.execute(
        """
        INSERT INTO message_plans(plan,specs,messages)
        VALUES(?,?,?)
        ON CONFLICT(plan)
        DO UPDATE SET specs=excluded.specs, messages=excluded.messages
        """,
        (plan, specs, messages),
    )
    db.commit()
    await interaction.response.send_message(f"✅ Message plan `{plan}` configured.")


@bot.tree.command(name="ticketauto", description="Admin: toggle invite ticket auto-deployment.")
@admin_only()
async def ticketauto(interaction: discord.Interaction, enabled: bool = True):
    db.execute(
        """
        INSERT INTO settings(key,value) VALUES('ticketauto',?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        ("1" if enabled else "0",),
    )
    db.commit()
    await interaction.response.send_message(
        f"✅ Ticket auto-deployment is now **{'enabled' if enabled else 'disabled'}**."
    )


@bot.tree.command(name="admin_add", description="Admin: add a bot admin.")
@admin_only()
async def admin_add(interaction: discord.Interaction, user: discord.User):
    db.execute("INSERT OR IGNORE INTO admins(user_id) VALUES (?)", (user.id,))
    db.commit()
    await interaction.response.send_message(f"✅ {user.mention} is now a SiftNodes admin.")


@bot.tree.command(name="admin_remove", description="Admin: remove a bot admin.")
@admin_only()
async def admin_remove(interaction: discord.Interaction, user: discord.User):
    if user.id == OWNER_ID:
        return await interaction.response.send_message(
            "❌ The owner cannot be removed.", ephemeral=True
        )
    db.execute("DELETE FROM admins WHERE user_id=?", (user.id,))
    db.commit()
    await interaction.response.send_message(f"✅ {user.mention} is no longer a bot admin.")


# ============================================================
# EXPIRY WORKER
# ============================================================

@tasks.loop(minutes=10)
async def expiry_worker():
    if docker_client is None:
        return

    rows = db.execute(
        "SELECT * FROM vps WHERE expiry_at<=?",
        (iso(utcnow()),),
    ).fetchall()

    for vps in rows:
        try:
            await run_blocking(docker_remove, vps)
        except Exception:
            pass
        db.execute("DELETE FROM vps WHERE id=?", (vps["id"],))
        log.info("Expired VPS removed: %s", vps["id"])
    db.commit()


# ============================================================
# WELCOME, AUTOROLE AND TICKET PANEL CONFIGURATION
# ============================================================

def setting_get(key, default=None):
    row = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    if not row:
        return default
    return row["value"]


def setting_set(key, value):
    db.execute(
        "INSERT INTO settings(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )
    db.commit()


def json_setting_get(key, default):
    raw = setting_get(key)
    if not raw:
        return default.copy() if isinstance(default, dict) else default
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return default.copy() if isinstance(default, dict) else default


def json_setting_set(key, value):
    setting_set(key, json.dumps(value, ensure_ascii=False))


class WelcomeCommandGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="welcome", description="Configure server welcome messages")

    @app_commands.command(name="setup", description="Admin: configure the welcome embed")
    @app_commands.describe(
        channel="Channel where welcome messages are sent",
        title="Welcome embed title",
        description="Welcome embed description; use {user}, {server}, and {member_count}",
        thumbnail="Optional thumbnail image URL",
        image="Optional large image URL",
    )
    @admin_only()
    async def setup(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        title: str,
        description: str,
        thumbnail: str = "",
        image: str = "",
    ):
        cfg = {
            "channel_id": channel.id,
            "title": title[:256],
            "description": description[:4000],
            "thumbnail": thumbnail.strip(),
            "image": image.strip(),
        }
        json_setting_set("welcome_config", cfg)
        await interaction.response.send_message(
            f"✅ Welcome message saved. New members will be welcomed in {channel.mention}.",
            ephemeral=True,
        )


bot.tree.add_command(WelcomeCommandGroup())


@bot.tree.command(name="autorole", description="Set the role automatically given to new members.")
@app_commands.describe(role="Role to give to new members")
@admin_only()
async def autorole(interaction: discord.Interaction, role: discord.Role):
    if role.is_default or role.managed:
        return await interaction.response.send_message(
            "❌ Please choose a normal assignable role.", ephemeral=True
        )
    setting_set("autorole_id", role.id)
    await interaction.response.send_message(
        f"✅ Autorole set to {role.mention}. Make sure my bot role is above this role.",
        ephemeral=True,
    )


class TicketTextModal(discord.ui.Modal):
    def __init__(self, field_name: str, current: str, label: str, max_length: int = 256):
        super().__init__(title=f"Edit Ticket {label}")
        self.field_name = field_name
        self.value_input = discord.ui.TextInput(
            label=label, default=current[:max_length] if current else None,
            required=(field_name != "thumbnail" and field_name != "image"),
            max_length=max_length, style=discord.TextStyle.paragraph if field_name == "description" else discord.TextStyle.short,
        )
        self.add_item(self.value_input)

    async def on_submit(self, interaction: discord.Interaction):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        cfg[self.field_name] = str(self.value_input.value).strip()
        json_setting_set("ticket_config", cfg)
        await interaction.response.send_message(f"✅ Ticket {self.field_name} saved.", ephemeral=True)


class TicketJsonModal(discord.ui.Modal, title="Edit Ticket Embed JSON"):
    json_text = discord.ui.TextInput(
        label="JSON (title, description, color, footer)",
        style=discord.TextStyle.paragraph,
        max_length=3500,
        required=True,
    )

    def __init__(self, current_cfg):
        super().__init__()
        self.json_text.default = json.dumps({
            "title": current_cfg.get("title", "Support Tickets"),
            "description": current_cfg.get("description", "Choose a category to open a ticket."),
            "color": current_cfg.get("color", "5865F2"),
            "footer": current_cfg.get("footer", "Support Team"),
        }, ensure_ascii=False, indent=2)[:3500]

    async def on_submit(self, interaction: discord.Interaction):
        try:
            data = json.loads(self.json_text.value)
            if not isinstance(data, dict):
                raise ValueError("JSON must be an object")
            cfg = json_setting_get("ticket_config", default_ticket_config())
            for key in ("title", "description", "footer"):
                if key in data:
                    cfg[key] = str(data[key])
            if "color" in data:
                cfg["color"] = str(data["color"]).replace("#", "")[:6]
            json_setting_set("ticket_config", cfg)
            await interaction.response.send_message("✅ Ticket embed JSON saved.", ephemeral=True)
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            await interaction.response.send_message(f"❌ Invalid JSON: `{exc}`", ephemeral=True)


class TicketCategoryModal(discord.ui.Modal, title="Add Ticket Category"):
    category_name = discord.ui.TextInput(label="Category name", max_length=80, required=True)
    category_emoji = discord.ui.TextInput(label="Emoji (optional)", max_length=32, required=False, default="🎫")
    staff_role = discord.ui.TextInput(
        label="Staff role mention/ID, or none", max_length=100, required=False, default="none"
    )

    async def on_submit(self, interaction: discord.Interaction):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        raw_role = self.staff_role.value.strip()
        role_id = None
        if raw_role.lower() != "none" and raw_role:
            match = re.search(r"\d{15,22}", raw_role)
            if not match:
                return await interaction.response.send_message(
                    "❌ Staff role must be a role mention, role ID, or `none`.", ephemeral=True
                )
            role_id = int(match.group(0))
            role = interaction.guild.get_role(role_id) if interaction.guild else None
            if role is None:
                return await interaction.response.send_message("❌ That role isn't in this server.", ephemeral=True)
        cats = cfg.setdefault("categories", [])
        if len(cats) >= 25:
            return await interaction.response.send_message("❌ Maximum 25 categories reached.", ephemeral=True)
        cats.append({"name": self.category_name.value.strip(), "emoji": self.category_emoji.value.strip() or "🎫", "staff_role_id": role_id})
        json_setting_set("ticket_config", cfg)
        await interaction.response.send_message(
            f"✅ Added ticket category **{self.category_name.value.strip()}**.", ephemeral=True
        )


def default_ticket_config():
    return {
        "title": "🎫 Support Tickets",
        "description": "Choose a category below to open a private support ticket.",
        "color": "5865F2",
        "footer": "Support Team",
        "thumbnail": "",
        "image": "",
        "style": "select",
        "categories": [],
        "panel_channel_id": None,
        "panel_message_id": None,
        "ticket_category_id": None,
    }


def ticket_embed(cfg):
    try:
        color = int(str(cfg.get("color", "5865F2")).replace("#", ""), 16)
    except ValueError:
        color = 0x5865F2
    embed = discord.Embed(
        title=cfg.get("title") or "Support Tickets",
        description=cfg.get("description") or "Choose a category below.",
        color=color,
    )
    if cfg.get("footer"):
        embed.set_footer(text=str(cfg["footer"])[:2048])
    if cfg.get("thumbnail", "").startswith(("https://", "http://")):
        embed.set_thumbnail(url=cfg["thumbnail"])
    if cfg.get("image", "").startswith(("https://", "http://")):
        embed.set_image(url=cfg["image"])
    return embed


async def create_ticket_channel(interaction: discord.Interaction, category_index: int):
    if not interaction.guild or not isinstance(interaction.user, discord.Member):
        return await interaction.response.send_message("❌ Use this ticket panel inside a server.", ephemeral=True)
    cfg = json_setting_get("ticket_config", default_ticket_config())
    categories = cfg.get("categories", [])
    if category_index < 0 or category_index >= len(categories):
        return await interaction.response.send_message("❌ This ticket category is no longer available.", ephemeral=True)
    category_cfg = categories[category_index]
    guild = interaction.guild
    safe_name = re.sub(r"[^a-z0-9-]+", "-", category_cfg.get("name", "support").lower()).strip("-")[:35] or "support"
    channel_name = f"ticket-{safe_name}-{interaction.user.name.lower()[:12]}"[:95]
    existing = discord.utils.get(guild.text_channels, name=channel_name)
    if existing and existing.permissions_for(interaction.user).view_channel:
        return await interaction.response.send_message(f"You already have a ticket: {existing.mention}", ephemeral=True)

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(view_channel=False),
        interaction.user: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, attach_files=True),
        guild.me: discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True, manage_channels=True),
    }
    role_id = category_cfg.get("staff_role_id")
    staff_role = guild.get_role(int(role_id)) if role_id else None
    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(view_channel=True, send_messages=True, read_message_history=True)
    try:
        parent_id = cfg.get("ticket_category_id")
        parent_category = guild.get_channel(int(parent_id)) if parent_id else None
        if not isinstance(parent_category, discord.CategoryChannel):
            parent_category = discord.utils.get(guild.categories, name="SiftNodes Tickets")
            if parent_category is None:
                parent_category = await guild.create_category("SiftNodes Tickets", reason="Create ticket category")
            cfg["ticket_category_id"] = parent_category.id
            json_setting_set("ticket_config", cfg)
        channel = await guild.create_text_channel(
            channel_name, category=parent_category, overwrites=overwrites,
            topic=f"Ticket owner: {interaction.user.id} | Category: {category_cfg.get('name', 'Support')}",
            reason="Ticket opened from ticket panel",
        )
        msg = f"{interaction.user.mention} Welcome! Your **{category_cfg.get('name', 'Support')}** ticket is open."
        if staff_role:
            msg += f" {staff_role.mention}"
        await channel.send(msg, allowed_mentions=discord.AllowedMentions(users=True, roles=True))
        await channel.send(embed=discord.Embed(
            title="🎫 Ticket Controls",
            description="Staff can claim or close this ticket. The ticket opener cannot close it.",
            color=discord.Color.blurple(),
        ), view=TicketControlView())
        await interaction.response.send_message(f"✅ Ticket created: {channel.mention}", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I need Manage Channels and channel permissions to create tickets.", ephemeral=True)


class CloseTicketReasonModal(discord.ui.Modal, title="Close Ticket With Reason"):
    reason = discord.ui.TextInput(label="Reason", style=discord.TextStyle.paragraph, max_length=800, required=True)

    async def on_submit(self, interaction: discord.Interaction):
        if not await ticket_staff_check(interaction):
            return
        await close_ticket_channel(interaction, self.reason.value.strip())


async def ticket_staff_check(interaction: discord.Interaction) -> bool:
    member = interaction.user
    if not isinstance(member, discord.Member):
        await interaction.response.send_message("❌ This action is only available in a server.", ephemeral=True)
        return False
    if is_admin(member.id) or member.guild_permissions.administrator or member.guild_permissions.manage_channels or member.guild_permissions.manage_messages:
        return True
    cfg = json_setting_get("ticket_config", default_ticket_config())
    for cat in cfg.get("categories", []):
        rid = cat.get("staff_role_id")
        if rid and any(role.id == int(rid) for role in member.roles):
            return True
    await interaction.response.send_message("❌ Only ticket staff can claim or close tickets.", ephemeral=True)
    return False


async def close_ticket_channel(interaction: discord.Interaction, reason: str):
    channel = interaction.channel
    if not isinstance(channel, discord.TextChannel) or not channel.topic or "Ticket owner:" not in channel.topic:
        return await interaction.response.send_message("❌ This channel is not a ticket.", ephemeral=True)
    owner_match = re.search(r"Ticket owner:\s*(\d+)", channel.topic)
    owner_id = int(owner_match.group(1)) if owner_match else None
    owner = interaction.guild.get_member(owner_id) if owner_id and interaction.guild else None
    try:
        await channel.send(embed=discord.Embed(
            title="🔒 Ticket Closed",
            description=f"Closed by {interaction.user.mention}\n**Reason:** {reason[:1000]}\n\nThis ticket will be deleted in 5 seconds.",
            color=discord.Color.red(),
        ))
        await interaction.response.send_message("✅ Ticket closed. This channel will be deleted in 5 seconds.", ephemeral=True)
        await asyncio.sleep(5)
        await channel.delete(reason=f"Ticket closed by {interaction.user} | {reason[:300]}")
    except discord.Forbidden:
        if not interaction.response.is_done():
            await interaction.response.send_message("❌ I need Manage Channels permission to close tickets.", ephemeral=True)


class TicketControlView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="Claim", style=discord.ButtonStyle.primary, emoji="🙋", custom_id="siftnodes_ticket_claim")
    async def claim_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ticket_staff_check(interaction):
            return
        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel) or not channel.topic or "Ticket owner:" not in channel.topic:
            return await interaction.response.send_message("❌ This channel is not a ticket.", ephemeral=True)
        if "Claimed by:" in channel.topic:
            return await interaction.response.send_message("ℹ️ This ticket has already been claimed.", ephemeral=True)
        try:
            await channel.edit(
                topic=f"{channel.topic} | Claimed by: {interaction.user.id}",
                name=f"ticket-claimed-{re.sub(r'[^a-z0-9-]', '', interaction.user.name.lower())[:70]}"[:95],
                reason=f"Ticket claimed by {interaction.user}",
            )
            await interaction.response.send_message(f"✅ Ticket claimed by {interaction.user.mention} and renamed to **{channel.name}**.")
        except discord.Forbidden:
            await interaction.response.send_message("❌ I need Manage Channels permission to claim tickets.", ephemeral=True)

    @discord.ui.button(label="Close", style=discord.ButtonStyle.danger, emoji="🔒", custom_id="siftnodes_ticket_close")
    async def close_ticket(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ticket_staff_check(interaction):
            return
        await close_ticket_channel(interaction, "Closed by staff (no reason provided).")

    @discord.ui.button(label="Close With Reason", style=discord.ButtonStyle.secondary, emoji="📝", custom_id="siftnodes_ticket_close_reason")
    async def close_with_reason(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await ticket_staff_check(interaction):
            return
        await interaction.response.send_modal(CloseTicketReasonModal())


class TicketCategorySelect(discord.ui.Select):
    def __init__(self, cfg):
        cats = cfg.get("categories", [])
        options = []
        for i, cat in enumerate(cats[:25]):
            emoji = cat.get("emoji") or None
            try:
                options.append(discord.SelectOption(label=cat.get("name", f"Category {i+1}")[:100], value=str(i), emoji=emoji))
            except Exception:
                options.append(discord.SelectOption(label=cat.get("name", f"Category {i+1}")[:100], value=str(i)))
        if not options:
            options = [discord.SelectOption(label="No categories configured", value="none", description="Ask an admin to configure tickets")]
        super().__init__(placeholder="Select a ticket category…", min_values=1, max_values=1, options=options, custom_id="prime_ticket_category_select")

    async def callback(self, interaction: discord.Interaction):
        if self.values[0] == "none":
            return await interaction.response.send_message("No ticket categories have been configured yet.", ephemeral=True)
        await create_ticket_channel(interaction, int(self.values[0]))


class TicketPanelView(discord.ui.View):
    def __init__(self, cfg):
        super().__init__(timeout=None)
        if cfg.get("style", "select") == "button":
            cats = cfg.get("categories", [])[:25]
            for i, cat in enumerate(cats):
                button = discord.ui.Button(
                    label=cat.get("name", f"Category {i+1}")[:80],
                    emoji=cat.get("emoji") or None,
                    style=discord.ButtonStyle.primary,
                    custom_id=f"prime_ticket_button_{i}",
                    row=i // 5,
                )
                async def callback(interaction, category_index=i):
                    await create_ticket_channel(interaction, category_index)
                button.callback = callback
                self.add_item(button)
            if not cats:
                self.add_item(discord.ui.Button(label="No categories configured", disabled=True, row=0))
        else:
            self.add_item(TicketCategorySelect(cfg))


class TicketSetupView(discord.ui.View):
    def __init__(self, guild_id: int, panel_channel_id: int, style: str):
        super().__init__(timeout=900)
        self.guild_id = guild_id
        self.panel_channel_id = panel_channel_id
        self.style = style

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if not is_admin(interaction.user.id):
            await interaction.response.send_message("❌ Admin only.", ephemeral=True)
            return False
        return True

    @discord.ui.button(label="Title Button", style=discord.ButtonStyle.primary, row=0)
    async def edit_title(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        await interaction.response.send_modal(TicketTextModal("title", cfg.get("title", ""), "Title", 256))

    @discord.ui.button(label="Description Button", style=discord.ButtonStyle.primary, row=0)
    async def edit_description(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        await interaction.response.send_modal(TicketTextModal("description", cfg.get("description", ""), "Description", 4000))

    @discord.ui.button(label="JSON Button", style=discord.ButtonStyle.secondary, row=0)
    async def edit_json(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        await interaction.response.send_modal(TicketJsonModal(cfg))

    @discord.ui.button(label="Thumbnail Button", style=discord.ButtonStyle.secondary, row=1)
    async def edit_thumbnail(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        await interaction.response.send_modal(TicketTextModal("thumbnail", cfg.get("thumbnail", ""), "Thumbnail URL", 500))

    @discord.ui.button(label="Image Button", style=discord.ButtonStyle.secondary, row=1)
    async def edit_image(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        await interaction.response.send_modal(TicketTextModal("image", cfg.get("image", ""), "Image URL", 500))

    @discord.ui.button(label="Add Categories", style=discord.ButtonStyle.success, row=1)
    async def add_category(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(TicketCategoryModal())

    @discord.ui.button(label="Save Changes", style=discord.ButtonStyle.success, row=2)
    async def save_changes(self, interaction: discord.Interaction, button: discord.ui.Button):
        cfg = json_setting_get("ticket_config", default_ticket_config())
        cfg["style"] = self.style
        cfg["panel_channel_id"] = self.panel_channel_id
        json_setting_set("ticket_config", cfg)
        channel = interaction.guild.get_channel(self.panel_channel_id) if interaction.guild else None
        if not isinstance(channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Setup channel is no longer available.", ephemeral=True)
        view = TicketPanelView(cfg)
        try:
            message_id = cfg.get("panel_message_id")
            if message_id and cfg.get("panel_channel_id") == channel.id:
                try:
                    old_message = await channel.fetch_message(int(message_id))
                    await old_message.edit(embed=ticket_embed(cfg), view=view)
                    await interaction.response.send_message(f"✅ Ticket panel updated in {channel.mention}.", ephemeral=True)
                    return
                except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                    pass
            panel_msg = await channel.send(embed=ticket_embed(cfg), view=view)
            cfg["panel_message_id"] = panel_msg.id
            json_setting_set("ticket_config", cfg)
            await interaction.response.send_message(f"✅ Ticket panel saved and posted in {channel.mention}.", ephemeral=True)
        except discord.Forbidden:
            await interaction.response.send_message("❌ I need Send Messages and Embed Links in that channel.", ephemeral=True)


class TicketCommandGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="ticket", description="Configure and use the support ticket system")

    @app_commands.command(name="setup", description="Admin: open ticket panel setup dashboard")
    @app_commands.describe(style="Choose ticket category controls: buttons or select menu")
    @app_commands.choices(style=[
        app_commands.Choice(name="Button", value="button"),
        app_commands.Choice(name="Select Menu", value="select"),
    ])
    @admin_only()
    async def setup(self, interaction: discord.Interaction, style: app_commands.Choice[str]):
        if not isinstance(interaction.channel, discord.TextChannel):
            return await interaction.response.send_message("❌ Run setup in a text channel.", ephemeral=True)
        cfg = json_setting_get("ticket_config", default_ticket_config())
        cfg["style"] = style.value
        cfg["panel_channel_id"] = interaction.channel.id
        try:
            category_id = cfg.get("ticket_category_id")
            ticket_category = interaction.guild.get_channel(int(category_id)) if category_id else None
            if not isinstance(ticket_category, discord.CategoryChannel):
                ticket_category = discord.utils.get(interaction.guild.categories, name="SiftNodes Tickets")
            if ticket_category is None:
                ticket_category = await interaction.guild.create_category(
                    "SiftNodes Tickets", reason="Ticket setup: create ticket category"
                )
            cfg["ticket_category_id"] = ticket_category.id
        except discord.Forbidden:
            return await interaction.response.send_message(
                "❌ I need Manage Channels permission to create the ticket category.", ephemeral=True
            )
        json_setting_set("ticket_config", cfg)
        embed = discord.Embed(
            title="🎫 Ticket Panel Setup",
            description=(
                f"**Panel style:** `{style.name}`\n\n"
                "Use the buttons below to edit the title, description, JSON embed settings, thumbnail, image, and categories.\n"
                "Category staff role accepts a role mention/ID or `none`.\n\n"
                "Click **Save Changes** when ready to publish/update the ticket panel."
            ), color=discord.Color.blurple(),
        )
        await interaction.response.send_message(
            embed=embed,
            view=TicketSetupView(interaction.guild_id, interaction.channel.id, style.value),
            ephemeral=True,
        )

bot.tree.add_command(TicketCommandGroup())


class InviteCommandGroup(app_commands.Group):
    def __init__(self):
        super().__init__(name="invite", description="Configure invite tracking messages")

    @app_commands.command(name="setup", description="Set the message sent when a member joins through an invite")
    @app_commands.describe(
        channel="Channel where invite joins should be announced",
        msg="Message template; placeholders: {user}, {server}, {invited}",
    )
    @admin_only()
    async def setup(self, interaction: discord.Interaction, channel: discord.TextChannel, msg: str):
        cfg = {"channel_id": channel.id, "message": msg[:1900]}
        json_setting_set(f"invite_config:{interaction.guild_id}", cfg)
        await interaction.response.send_message(
            "✅ Invite tracker configured. Placeholders: `{user}` = joined member mention, "
            "`{server}` = server name, `{invited}` = inviter's display name. "
            f"Join announcements will be sent in {channel.mention}.",
            ephemeral=True,
        )


bot.tree.add_command(InviteCommandGroup())


# ============================================================
# PREFIX ALIASES: only -i and -buy are enabled; all other commands use slash.
# ============================================================

@bot.command(name="i")
async def prefix_invite_stats(ctx, user: discord.User = None):
    target = user or ctx.author
    row = user_row(target.id)
    embed = discord.Embed(title=f"{target.display_name} Invite Statistics", color=discord.Color.blurple())
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Net Invites / Points", value=f"`{row['invite_points']}`", inline=False)
    embed.add_field(name="🟢 Joins", value=f"`{row['joins']}`", inline=True)
    embed.add_field(name="🔴 Left", value=f"`{row['leaves']}`", inline=True)
    embed.add_field(name="⚠️ Fake", value=f"`{row['fake']}`", inline=True)
    embed.add_field(name="🔄 Rejoins", value=f"`{row['rejoins']}`", inline=True)
    await ctx.send(embed=embed)

@bot.command(name="buy")
async def prefix_buy(ctx):
    embed = discord.Embed(title="🛒 SiftNodes VPS Purchase", description=f"**16GB RAM • 6 Core CPU • 60GB Disk**\nCost: `{INVITE_COST}` invite points.\nNeed another slot? Use `/slots buy`.", color=discord.Color.blurple())
    await ctx.send(embed=embed, view=BuyView())

# ============================================================
# VOICE: music + optional text-to-speech chat reading
# Requires discord.py[voice], yt-dlp, edge-tts and system ffmpeg.
# ============================================================

import tempfile
import shutil

voice_settings = {}  # guild_id -> {loop, autoplay, read_chat, stay_24_7, last_query}
voice_locks = {}


def guild_voice_settings(guild_id):
    return voice_settings.setdefault(guild_id, {"loop": False, "autoplay": False, "read_chat": False, "stay_24_7": False, "last_query": None})


async def make_tts_file(text_to_say):
    try:
        import edge_tts
    except ImportError:
        return None
    path = os.path.join(tempfile.gettempdir(), f"siftnodes_tts_{uuid.uuid4().hex}.mp3")
    try:
        communicate = edge_tts.Communicate(text_to_say[:450], "en-US-AriaNeural")
        await communicate.save(path)
        return path
    except Exception:
        log.exception("TTS generation failed")
        return None


async def speak_in_voice(guild, text_to_say):
    if not guild or not text_to_say.strip():
        return
    vc = guild.voice_client
    if not vc or not vc.is_connected():
        return
    path = await make_tts_file(text_to_say)
    if not path:
        return
    was_playing = vc.is_playing()
    try:
        if was_playing:
            vc.pause()
        elif vc.is_paused():
            # Keep an already-paused song paused while TTS speaks.
            was_playing = False
        while vc.is_playing():
            await asyncio.sleep(0.1)
        source = discord.FFmpegPCMAudio(path)
        done = asyncio.Event()
        vc.play(source, after=lambda error: done.set())
        await done.wait()
        if was_playing and vc.is_connected():
            vc.resume()
    except Exception:
        log.exception("Could not speak in voice channel")
        if was_playing and vc.is_connected() and vc.is_paused():
            vc.resume()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


async def play_music_query(ctx, query):
    if not ctx.guild:
        return await ctx.send("❌ Music commands only work in a server.")
    if not shutil.which("ffmpeg"):
        return await ctx.send("❌ FFmpeg isn't installed on the bot host. Install FFmpeg, then restart the bot.")
    try:
        import yt_dlp
    except ImportError:
        return await ctx.send("❌ Missing dependency: `yt-dlp`. Install requirements.txt and restart.")
    voice_state = getattr(ctx.author, "voice", None)
    if not voice_state or not voice_state.channel:
        return await ctx.send("❌ Pehle apne voice channel me join karo.")
    vc = ctx.guild.voice_client
    if vc is None:
        vc = await voice_state.channel.connect()
    elif vc.channel != voice_state.channel:
        await vc.move_to(voice_state.channel)
    await ctx.send(f"🔎 Searching and loading: `{query[:180]}`")
    loop = asyncio.get_running_loop()
    ytdl_opts = {"format": "bestaudio/best", "noplaylist": True, "quiet": True, "default_search": "ytsearch1", "source_address": "0.0.0.0"}
    def extract():
        with yt_dlp.YoutubeDL(ytdl_opts) as ydl:
            info = ydl.extract_info(query, download=False)
            if "entries" in info:
                info = next((x for x in info["entries"] if x), None)
            if not info:
                raise ValueError("No playable result found")
            return {"url": info["url"], "title": info.get("title", "Unknown title"), "webpage_url": info.get("webpage_url", query)}
    try:
        info = await loop.run_in_executor(None, extract)
    except Exception as e:
        return await ctx.send(f"❌ Couldn't load that song: `{str(e)[:250]}`")
    settings = guild_voice_settings(ctx.guild.id)
    settings["last_query"] = query
    if vc.is_playing() or vc.is_paused():
        vc.stop()
    source = discord.FFmpegPCMAudio(info["url"], before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5", options="-vn")
    def after_play(error):
        if error:
            log.warning("Music playback ended: %s", error)
        fut = asyncio.run_coroutine_threadsafe(music_after(ctx, query), bot.loop)
        try:
            fut.result(timeout=1)
        except Exception:
            pass
    vc.play(source, after=after_play)
    await ctx.send(f"🎵 Now playing: **{info['title'][:180]}**\n{info['webpage_url']}")


async def music_after(ctx, query):
    await asyncio.sleep(1)
    if not ctx.guild:
        return
    settings = guild_voice_settings(ctx.guild.id)
    if settings.get("loop") or settings.get("autoplay"):
        try:
            await play_music_query(ctx, query)
        except Exception:
            log.exception("Autoplay/loop restart failed")


@bot.command(name="join")
async def voice_join(ctx, *, mode: str = "read"):
    """.join enables chat reading; .join 24/7 requests a persistent voice connection."""
    if not ctx.guild:
        return await ctx.send("❌ Use this command in a server.")
    state = getattr(ctx.author, "voice", None)
    if not state or not state.channel:
        return await ctx.send("❌ Pehle us voice channel me join karo jahan bot ko bulana hai.")
    vc = ctx.guild.voice_client
    if vc is None:
        vc = await state.channel.connect()
    elif vc.channel != state.channel:
        await vc.move_to(state.channel)
    settings = guild_voice_settings(ctx.guild.id)
    if "24/7" in mode.lower() or "247" in mode.lower():
        settings["stay_24_7"] = True
        settings["read_chat"] = False
        await ctx.send("🔊 Joined voice and enabled 24/7 stay mode. The bot can stay only while its process/server is running and Discord allows the connection.")
    else:
        settings["read_chat"] = True
        settings["read_channel_id"] = ctx.channel.id
        await ctx.send("🗣️ Joined voice. Chat reading is ON for this text channel. Use `.readchat off` to stop reading; music commands still work.")


@bot.command(name="readchat")
async def voice_readchat(ctx, mode: str = "on"):
    settings = guild_voice_settings(ctx.guild.id)
    settings["read_chat"] = mode.lower() not in ("off", "false", "0", "no")
    await ctx.send("🗣️ Chat reading " + ("enabled." if settings["read_chat"] else "disabled."))


@bot.command(name="play")
async def voice_play(ctx, *, query: str):
    await play_music_query(ctx, query)


@bot.command(name="loop")
async def voice_loop(ctx, mode: str = "on"):
    settings = guild_voice_settings(ctx.guild.id)
    settings["loop"] = mode.lower() not in ("off", "false", "0", "no")
    if settings["loop"]:
        settings["autoplay"] = False
    await ctx.send("🔁 Song loop " + ("enabled." if settings["loop"] else "disabled."))


@bot.command(name="autoplay")
async def voice_autoplay(ctx, mode: str = "on"):
    settings = guild_voice_settings(ctx.guild.id)
    settings["autoplay"] = mode.lower() not in ("off", "false", "0", "no")
    if settings["autoplay"]:
        settings["loop"] = False
    await ctx.send("🎶 Autoplay " + ("enabled; current song will be replayed automatically." if settings["autoplay"] else "disabled."))


@bot.command(name="leave")
async def voice_leave(ctx):
    if ctx.guild and ctx.guild.voice_client:
        guild_voice_settings(ctx.guild.id)["read_chat"] = False
        guild_voice_settings(ctx.guild.id)["stay_24_7"] = False
        await ctx.guild.voice_client.disconnect(force=True)
        await ctx.send("👋 Left voice channel.")
    else:
        await ctx.send("ℹ️ I'm not in a voice channel.")


youtube_group = app_commands.Group(name="youtube", description="YouTube video, Shorts and livestream alerts")

@youtube_group.command(name="alert", description="Send a YouTube video, Shorts or livestream announcement.")
@app_commands.describe(link="YouTube video, Shorts or livestream URL", channel="Channel where alert should be posted", message="Announcement text; use {link} where the URL should appear")
async def youtube_alert(interaction: discord.Interaction, link: str, channel: discord.TextChannel, message: str):
    member = interaction.user
    if not isinstance(member, discord.Member) or not (is_admin(member.id) or member.guild_permissions.administrator or member.guild_permissions.manage_guild or member.guild_permissions.manage_messages):
        return await interaction.response.send_message("❌ You need Manage Server or Manage Messages permission to send YouTube alerts.", ephemeral=True)
    if not re.match(r"https?://(www\.)?(youtube\.com|youtu\.be)/", link.strip(), re.I):
        return await interaction.response.send_message("❌ Please provide a valid YouTube video/Shorts/live URL.", ephemeral=True)
    announcement = message.replace("{link}", link.strip())
    if link.strip() not in announcement:
        announcement = announcement.rstrip() + "\n" + link.strip()
    try:
        await channel.send(announcement[:2000], allowed_mentions=discord.AllowedMentions(everyone=True, users=True, roles=True))
        await interaction.response.send_message(f"✅ YouTube alert sent in {channel.mention}.", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ I need View Channel and Send Messages permissions in that channel.", ephemeral=True)


bot.tree.add_command(youtube_group)


# ============================================================
# EVENTS
# ============================================================

@bot.event
async def on_ready():
    global docker_client
    db_init()
    docker_connect()

    activity = discord.Game(name=ACTIVITY_NAME)
    await bot.change_presence(
        status=discord.Status.dnd,
        activity=activity,
    )

    try:
        bot.add_view(TicketControlView())
    except Exception:
        log.exception("Could not register persistent ticket controls")

    try:
        bot.add_view(GiveawayJoinView())
    except Exception:
        log.exception("Could not register persistent giveaway controls")

    if not giveaway_worker.is_running():
        giveaway_worker.start()

    try:
        saved_ticket_cfg = json_setting_get("ticket_config", default_ticket_config())
        if saved_ticket_cfg.get("panel_message_id") and saved_ticket_cfg.get("panel_channel_id"):
            bot.add_view(TicketPanelView(saved_ticket_cfg), message_id=int(saved_ticket_cfg["panel_message_id"]))
    except Exception:
        log.exception("Could not restore persistent ticket panel view")

    try:
        await bot.tree.sync()
        log.info("Slash commands synced.")
    except Exception:
        log.exception("Slash command sync failed.")

    for guild in bot.guilds:
        await refresh_invites(guild)

    if not expiry_worker.is_running():
        expiry_worker.start()

    log.info(
        "Logged in as %s | DND | Playing %s",
        bot.user,
        ACTIVITY_NAME,
    )


@bot.event
async def on_guild_join(guild):
    await refresh_invites(guild)


@bot.tree.error
async def on_app_command_error(interaction, error):
    if isinstance(error, app_commands.CheckFailure):
        message = "❌ You do not have permission to use this admin command."
    else:
        log.exception("Slash command error", exc_info=error)
        message = f"❌ An error occurred: `{str(error)[:1000]}`"

    if interaction.response.is_done():
        await interaction.followup.send(message, ephemeral=True)
    else:
        await interaction.response.send_message(message, ephemeral=True)



# ============================================================
# MODERATION COMMANDS: /ban /timeout /warn
# ============================================================
def parse_duration(value: str) -> timedelta | None:
    match = re.fullmatch(r"\s*(\d+)\s*(s|m|h|d)\s*", value.lower())
    if not match:
        return None
    amount = int(match.group(1))
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}[match.group(2)]
    if amount < 1 or amount > 28 * 86400:
        return None
    return timedelta(seconds=amount * unit)


def moderation_permission():
    async def predicate(interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            raise app_commands.CheckFailure("Use this command inside a server.")
        if not (is_admin(interaction.user.id) or interaction.user.guild_permissions.moderate_members or interaction.user.guild_permissions.ban_members):
            raise app_commands.CheckFailure("You need moderation permissions.")
        return True
    return app_commands.check(predicate)


@bot.tree.command(name="ban", description="Ban a member for a duration like 1s, 1m, 1h, or 1d")
@app_commands.describe(user="Member to ban", time="Duration: 1s, 1m, 1h, 1d", reason="Reason for the ban")
@moderation_permission()
async def ban_member(interaction: discord.Interaction, user: discord.Member, time: str, reason: str):
    duration = parse_duration(time)
    if duration is None:
        return await interaction.response.send_message("❌ Invalid time. Use a duration such as `1s`, `1m`, `1h`, or `1d` (maximum 28 days).", ephemeral=True)
    if user.id == interaction.user.id or user.id == bot.user.id:
        return await interaction.response.send_message("❌ You cannot ban yourself or the bot.", ephemeral=True)
    if user.top_role >= interaction.user.top_role and interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ You cannot moderate a member with an equal or higher role.", ephemeral=True)
    try:
        await user.send(f"You have been banned from **{interaction.guild.name}** for `{time}`. Reason: {reason}")
    except discord.HTTPException:
        pass
    await interaction.guild.ban(user, reason=f"Temporary ban {time} by {interaction.user}: {reason}", delete_message_seconds=0)
    async def unban_later(guild_id: int, user_id: int, delay: float):
        await asyncio.sleep(delay)
        guild = bot.get_guild(guild_id)
        if guild:
            try:
                await guild.unban(discord.Object(id=user_id), reason="Temporary ban duration elapsed")
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                pass
    asyncio.create_task(unban_later(interaction.guild.id, user.id, duration.total_seconds()))
    await interaction.response.send_message(f"🔨 Banned {user.mention} for `{time}`. Reason: {reason}")


@bot.tree.command(name="timeout", description="Timeout a member for a duration like 1m, 1h, or 1d")
@app_commands.describe(user="Member to timeout", time="Duration: 1s, 1m, 1h, 1d", reason="Reason for timeout")
@moderation_permission()
async def timeout_member(interaction: discord.Interaction, user: discord.Member, time: str, reason: str):
    duration = parse_duration(time)
    if duration is None or duration > timedelta(days=28):
        return await interaction.response.send_message("❌ Invalid time. Use `1s`, `1m`, `1h`, or `1d` (maximum 28 days).", ephemeral=True)
    if user.id == interaction.user.id or user.id == bot.user.id:
        return await interaction.response.send_message("❌ You cannot timeout yourself or the bot.", ephemeral=True)
    if user.top_role >= interaction.user.top_role and interaction.user.id != OWNER_ID:
        return await interaction.response.send_message("❌ You cannot moderate a member with an equal or higher role.", ephemeral=True)
    try:
        await user.send(f"You have been timed out in **{interaction.guild.name}** for `{time}`. Reason: {reason}")
    except discord.HTTPException:
        pass
    await user.timeout(utcnow() + duration, reason=f"By {interaction.user}: {reason}")
    await interaction.response.send_message(f"⏳ Timed out {user.mention} for `{time}`. Reason: {reason}")


@bot.tree.command(name="warn", description="Warn a member and send the reason by DM")
@app_commands.describe(user="Member to warn", reason="Reason for warning")
@moderation_permission()
async def warn_member(interaction: discord.Interaction, user: discord.Member, reason: str):
    try:
        await user.send(f"⚠️ You have been warned in **{interaction.guild.name}**. Reason: {reason}")
        dm_status = "DM sent"
    except discord.HTTPException:
        dm_status = "DM could not be delivered"
    await interaction.response.send_message(f"⚠️ Warned {user.mention}. Reason: {reason}. ({dm_status})")


# ============================================================
# START
# ============================================================

if __name__ == "__main__":
    db_init()

    if TOKEN == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit(
            "Set DISCORD_BOT_TOKEN or replace PUT_YOUR_BOT_TOKEN_HERE in bot.py."
        )

    bot.run(TOKEN)
