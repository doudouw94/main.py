import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date, datetime, timedelta, time
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix="p!", intents=intents)

# ==================== CONFIG ====================
TABLEAU_CHANNEL_ID = None
GUILD_ID = None
WARN_CHANNEL_ID = 1522688884368998550
OPERATION_HOUR = 21
OPERATION_MINUTE = 00
ROLE_VENDETTA = "Vendetta"

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS presences (
                        id SERIAL PRIMARY KEY,
                        operation_date DATE NOT NULL,
                        user_id BIGINT NOT NULL,
                        username TEXT,
                        status TEXT NOT NULL,
                        note TEXT,
                        submitted_at TIMESTAMP DEFAULT NOW(),
                        UNIQUE(operation_date, user_id)
                    );
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS presence_messages (
                        operation_date DATE PRIMARY KEY,
                        channel_id BIGINT NOT NULL,
                        message_id BIGINT NOT NULL
                    );
                ''')
                c.execute('''
                    CREATE TABLE IF NOT EXISTS authorized_users (
                        user_id BIGINT PRIMARY KEY,
                        username TEXT
                    );
                ''')
                conn.commit()
        print("✅ Base de données prête !")
    except Exception as e:
        print(f"⚠️ Erreur DB : {e}")

migrate_database()

def migrate_warns():
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute('''
                    CREATE TABLE IF NOT EXISTS warns (
                        id SERIAL PRIMARY KEY,
                        user_id BIGINT NOT NULL,
                        guild_id BIGINT NOT NULL,
                        moderator_id BIGINT NOT NULL,
                        reason TEXT NOT NULL,
                        warned_at TIMESTAMP DEFAULT NOW()
                    );
                ''')
                conn.commit()
        print("✅ Table 'warns' prête !")
    except Exception as e:
        print(f"⚠️ Erreur table warns : {e}")

migrate_warns()

def has_vendetta(member: discord.Member) -> bool:
    return any(role.name.lower() == ROLE_VENDETTA.lower() for role in member.roles)

async def sync_authorized_users(guild: discord.Guild):
    """Synchronise tous les membres qui ont le rôle Vendetta"""
    added = 0
    removed = 0
    with get_db() as conn:
        with conn.cursor() as c:
            # Récupère les IDs actuellement autorisés
            c.execute("SELECT user_id FROM authorized_users")
            current_authorized = {row[0] for row in c.fetchall()}

            # Membres qui ont le rôle
            members_with_role = [m for m in guild.members if has_vendetta(m) and not m.bot]
            members_with_role_ids = {m.id for m in members_with_role}

            # Ajouter ceux qui ont le rôle mais ne sont pas encore autorisés
            for member in members_with_role:
                if member.id not in current_authorized:
                    c.execute("""
                        INSERT INTO authorized_users (user_id, username)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, (member.id, member.display_name))
                    added += 1

            # Retirer ceux qui n'ont plus le rôle
            for user_id in current_authorized:
                if user_id not in members_with_role_ids:
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (user_id,))
                    c.execute("DELETE FROM presences WHERE user_id = %s AND operation_date = %s", (user_id, date.today()))
                    removed += 1

            conn.commit()

    print(f"🔄 Sync terminé → +{added} ajoutés / -{removed} retirés")
    await update_presence_tableau(date.today())

# ==================== VIEWS (Présence) ====================
class PresenceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def register(self, interaction: discord.Interaction, status: str):
        try:
            # Vérifie qu'il a bien le rôle Vendetta
            if not has_vendetta(interaction.user):
                return await interaction.response.send_message("❌ Tu n'as pas le rôle Vendetta.", ephemeral=True)

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                    if not c.fetchone():
                        return await interaction.response.send_message("❌ Tu n'es pas autorisé.", ephemeral=True)

            operation_date = date.today()
            note = None

            if status == "late":
                await interaction.response.send_message("À combien de minutes de retard ?", ephemeral=True)
                try:
                    msg = await bot.wait_for(
                        'message',
                        check=lambda m: m.author == interaction.user and m.channel == interaction.channel,
                        timeout=60
                    )
                    note = f"Retard de {msg.content} min"
                    try:
                        await msg.delete()
                    except:
                        pass
                except asyncio.TimeoutError:
                    note = "Retard non précisé"
            else:
                await interaction.response.send_message(f"✅ **{status.upper()}** enregistré !", ephemeral=True)

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO presences (operation_date, user_id, username, status, note)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (operation_date, user_id)
                        DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note,
                                      username = EXCLUDED.username, submitted_at = NOW()
                    """, (operation_date, interaction.user.id, interaction.user.display_name, status, note))
                    conn.commit()

            await asyncio.sleep(1)
            await update_presence_tableau(operation_date)

            if status == "late":
                await interaction.followup.send("✅ **EN RETARD** enregistré !", ephemeral=True)

        except Exception as e:
            print(f"Erreur register: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message("❌ Erreur lors de l'enregistrement.", ephemeral=True)
            else:
                await interaction.followup.send("❌ Erreur lors de l'enregistrement.", ephemeral=True)

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.green)
    async def present(self, interaction: discord.Interaction, button):
        await self.register(interaction, "present")

    @discord.ui.button(label="❌ Absent", style=discord.ButtonStyle.red)
    async def absent(self, interaction: discord.Interaction, button):
        await self.register(interaction, "absent")

    @discord.ui.button(label="⏰ En Retard", style=discord.ButtonStyle.gray)
    async def late(self, interaction: discord.Interaction, button):
        await self.register(interaction, "late")

    @discord.ui.button(label="📢 Rappel Inactifs", style=discord.ButtonStyle.red, row=1)
    async def rappel(self, interaction: discord.Interaction, button):
        if not interaction.user.guild_permissions.administrator:
            return await interaction.response.send_message("❌ Seul un admin peut utiliser ce bouton.", ephemeral=True)

        await interaction.response.defer(ephemeral=True)
        today = date.today()
        reminded = 0

        try:
            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("SELECT DISTINCT user_id FROM presences WHERE operation_date = %s", (today,))
                    active = {row[0] for row in c.fetchall()}
                    c.execute("SELECT user_id FROM authorized_users")
                    all_users = [row[0] for row in c.fetchall()]

            for user_id in all_users:
                if user_id not in active:
                    member = interaction.guild.get_member(user_id)
                    if member:
                        try:
                            await member.send(
                                f"⚠️ **Rappel Présence**\n"
                                f"Tu n'as pas encore marqué ta présence pour **l'opération de ce soir {OPERATION_HOUR}h{OPERATION_MINUTE:02d}**."
                            )
                            reminded += 1
                            await asyncio.sleep(0.5)
                        except:
                            pass

            await interaction.followup.send(f"✅ Rappel envoyé à **{reminded}** membre(s).", ephemeral=True)
        except Exception as e:
            print(e)
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)

# ==================== TABLEAU PRÉSENCE ====================
async def create_daily_presence_table():
    if not TABLEAU_CHANNEL_ID:
        return
    channel = bot.get_channel(TABLEAU_CHANNEL_ID)
    if not channel:
        return

    today = date.today()

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT message_id FROM presence_messages WHERE operation_date = %s", (today,))
            if c.fetchone():
                print(f"Tableau déjà existant pour {today}")
                return

    embed = discord.Embed(
        title=f"📋 Présences Opérations {OPERATION_HOUR}h{OPERATION_MINUTE:02d} - {today.strftime('%d/%m/%Y')}",
        description="**Opération de ce soir**",
        color=discord.Color.blurple()
    )
    embed.set_footer(text="Clique sur les boutons pour marquer ta présence")
    msg = await channel.send(embed=embed, view=PresenceView())

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO presence_messages (operation_date, channel_id, message_id)
                VALUES (%s, %s, %s)
                ON CONFLICT (operation_date) DO NOTHING
            """, (today, channel.id, msg.id))
            conn.commit()

    await update_presence_tableau(today)
    print(f"✅ Nouveau tableau créé pour {today}")

async def update_presence_tableau(target_date: date = None):
    if target_date is None:
        target_date = date.today()

    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute(
                    "SELECT channel_id, message_id FROM presence_messages WHERE operation_date = %s",
                    (target_date,)
                )
                row = c.fetchone()
                if not row:
                    return
                channel_id, message_id = row

        channel = bot.get_channel(channel_id)
        if not channel:
            return

        message = await channel.fetch_message(message_id)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                authorized = dict(c.fetchall())

                c.execute("""
                    SELECT user_id, username, status, note
                    FROM presences
                    WHERE operation_date = %s
                """, (target_date,))
                data = {row[0]: (row[1], row[2], row[3]) for row in c.fetchall()}

        embed = discord.Embed(
            title=f"📋 Présences Opérations {OPERATION_HOUR}h{OPERATION_MINUTE:02d} - {target_date.strftime('%d/%m/%Y')}",
            description="**Opération de ce soir**",
            color=discord.Color.blurple()
        )

        present, late, absent, unmarked = [], [], [], []

        for user_id, auth_username in authorized.items():
            if user_id in data:
                stored_name, status, note = data[user_id]
                display_name = stored_name or auth_username
                if status == "present":
                    present.append(f"✅ {display_name}")
                elif status == "late":
                    late.append(f"⏰ {display_name} {note or ''}")
                elif status == "absent":
                    absent.append(f"❌ {display_name}")
            else:
                unmarked.append(f"⚪ {auth_username}")

        if present:
            embed.add_field(name=f"✅ Présents ({len(present)})", value="\n".join(present) or "Aucun", inline=False)
        if late:
            embed.add_field(name=f"⏰ En Retard ({len(late)})", value="\n".join(late) or "Aucun", inline=False)
        if absent:
            embed.add_field(name=f"❌ Absents ({len(absent)})", value="\n".join(absent) or "Aucun", inline=False)
        if unmarked:
            embed.add_field(name=f"⚪ Non marqués ({len(unmarked)})", value="\n".join(unmarked) or "Aucun", inline=False)

        embed.set_footer(text=f"Dernière MAJ : {datetime.now().strftime('%H:%M:%S')}")
        await message.edit(embed=embed, view=PresenceView())

    except discord.NotFound:
        print(f"⚠️ Message du tableau {target_date} non trouvé.")
    except Exception as e:
        print(f"Erreur tableau: {e}")

# ==================== SYSTEME DE WARNS ====================
def is_warn_channel(ctx):
    if WARN_CHANNEL_ID is None:
        return True
    return ctx.channel.id == WARN_CHANNEL_ID

@bot.command()
@commands.has_permissions(kick_members=True)
@commands.check(is_warn_channel)
async def warn(ctx, member: discord.Member, *, reason: str = "Aucune raison donnée"):
    if member.bot:
        return await ctx.send("❌ Tu ne peux pas warn un bot.")
    if member == ctx.author:
        return await ctx.send("❌ Tu ne peux pas te warn toi-même.")

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO warns (user_id, guild_id, moderator_id, reason)
                VALUES (%s, %s, %s, %s)
            """, (member.id, ctx.guild.id, ctx.author.id, reason))
            conn.commit()

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT COUNT(*) FROM warns WHERE user_id = %s AND guild_id = %s", (member.id, ctx.guild.id))
            warn_count = c.fetchone()[0]

    embed = discord.Embed(
        title="⚠️ Avertissement",
        description=f"**Membre :** {member.mention}\n**Modérateur :** {ctx.author.mention}\n**Raison :** {reason}",
        color=discord.Color.orange()
    )
    embed.add_field(name="Total", value=f"**{warn_count}/2 warns**", inline=False)
    embed.set_footer(text="Les warns expirent automatiquement après 7 jours de bonne attitude")
    await ctx.send(embed=embed)

    if warn_count == 2:
        await ctx.send(
            f"🚨 **ATTENTION TOUT LE MONDE** 🚨\n"
            f"{member.mention} a atteint **2 warns** !\n\n"
            f"**Au prochain avertissement → c’est terminé pour lui.**"
        )

    try:
        await member.send(embed=discord.Embed(
            title=f"⚠️ Avertissement sur {ctx.guild.name}",
            description=f"Tu as reçu un avertissement.\n**Raison :** {reason}\n**Total actuel :** {warn_count}/2",
            color=discord.Color.red()
        ))
    except:
        pass

@bot.command()
@commands.has_permissions(kick_members=True)
@commands.check(is_warn_channel)
async def warns(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT reason, warned_at
                FROM warns
                WHERE user_id = %s AND guild_id = %s
                ORDER BY warned_at DESC
            """, (member.id, ctx.guild.id))
            warn_list = c.fetchall()

    if not warn_list:
        return await ctx.send(f"✅ {member.mention} n'a aucun avertissement.")

    embed = discord.Embed(title=f"📋 Warns de {member}", color=discord.Color.orange())
    for i, (reason, warned_at) in enumerate(warn_list, 1):
        embed.add_field(
            name=f"Warn #{i} — {warned_at.strftime('%d/%m/%Y %H:%M')}",
            value=f"**Raison :** {reason}",
            inline=False
        )
    embed.set_footer(text=f"Total : {len(warn_list)} warns")
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(kick_members=True)
@commands.check(is_warn_channel)
async def unwarn(ctx, member: discord.Member, *, reason: str = "Aucune raison"):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT id FROM warns
                WHERE user_id = %s AND guild_id = %s
                ORDER BY warned_at DESC
                LIMIT 1
            """, (member.id, ctx.guild.id))

            row = c.fetchone()
            if not row:
                return await ctx.send("❌ Ce membre n'a aucun warn à retirer.")

            c.execute("DELETE FROM warns WHERE id = %s", (row[0],))
            conn.commit()

    await ctx.send(f"✅ **{member}** a perdu 1 warn.\n**Raison :** {reason}")

# ==================== COMMANDES PRÉSENCE ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def setpresence(ctx):
    global TABLEAU_CHANNEL_ID, GUILD_ID
    GUILD_ID = ctx.guild.id
    TABLEAU_CHANNEL_ID = ctx.channel.id
    await create_daily_presence_table()
    await ctx.send("✅ **Nouveau tableau de présence du jour créé !**")

@bot.command()
@commands.has_permissions(administrator=True)
async def sync(ctx):
    """Force la synchronisation de tous les membres avec le rôle Vendetta"""
    await ctx.send("🔄 Synchronisation en cours...")
    await sync_authorized_users(ctx.guild)
    await ctx.send("✅ Synchronisation terminée.")

@bot.command()
async def listusers(ctx):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT username, user_id FROM authorized_users ORDER BY username")
            users = c.fetchall()

    if not users:
        return await ctx.send("Aucun utilisateur autorisé.")

    embed = discord.Embed(title="👥 Utilisateurs Autorisés (Rôle Vendetta)", color=discord.Color.blurple())
    description = ""
    for username, user_id in users:
        description += f"**{username}**\n`{user_id}`\n\n"
    embed.description = description
    embed.set_footer(text=f"Total : {len(users)} utilisateurs")
    await ctx.send(embed=embed)

@bot.command()
async def stats(ctx):
    today = date.today()
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT status, COUNT(*) FROM presences WHERE operation_date = %s GROUP BY status", (today,))
            stats = dict(c.fetchall())
            c.execute("SELECT COUNT(*) FROM authorized_users")
            total = c.fetchone()[0]

    embed = discord.Embed(title="📊 Statistiques Présences", color=discord.Color.gold())
    embed.add_field(name="Total autorisés", value=total, inline=False)
    embed.add_field(name="✅ Présents", value=stats.get("present", 0), inline=True)
    embed.add_field(name="⏰ En retard", value=stats.get("late", 0), inline=True)
    embed.add_field(name="❌ Absents", value=stats.get("absent", 0), inline=True)
    await ctx.send(embed=embed)

@bot.command()
async def history(ctx, days: int = 7):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                SELECT operation_date, COUNT(*) as total,
                       COUNT(CASE WHEN status='present' THEN 1 END) as present,
                       COUNT(CASE WHEN status='late' THEN 1 END) as late,
                       COUNT(CASE WHEN status='absent' THEN 1 END) as absent
                FROM presences
                WHERE operation_date >= %s
                GROUP BY operation_date
                ORDER BY operation_date DESC
            """, (date.today() - timedelta(days=days),))
            rows = c.fetchall()

    embed = discord.Embed(title=f"Historique des {days} derniers jours", color=discord.Color.gold())
    for row in rows:
        embed.add_field(
            name=row[0].strftime("%d/%m/%Y"),
            value=f"✅ {row[2]} | ⏰ {row[3]} | ❌ {row[4]} | Total {row[1]}",
            inline=False
        )
    await ctx.send(embed=embed)

@bot.command()
@commands.has_permissions(administrator=True)
async def reset(ctx):
    today = date.today()
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM presences WHERE operation_date = %s", (today,))
            conn.commit()
    await ctx.send("🗑️ Présences du jour réinitialisées.")
    await update_presence_tableau(today)

@bot.command()
@commands.has_permissions(administrator=True)
async def forceupdate(ctx):
    await update_presence_tableau(date.today())
    await ctx.send("✅ Tableau mis à jour.")

@bot.command(name="aide", aliases=["commands"])
async def aide(ctx):
    embed = discord.Embed(title="📜 Commandes du Bot Diamond City", color=discord.Color.blurple())
    embed.add_field(
        name="**Présence**",
        value="`p!setpresence`\n`p!sync` (forcer la synchro rôle)\n`p!listusers`\n`p!stats`\n`p!history [jours]`\n`p!reset`\n`p!forceupdate`",
        inline=False
    )
    embed.add_field(
        name="**Warns**",
        value="`p!warn @user raison`\n`p!warns @user`\n`p!unwarn @user`",
        inline=False
    )
    await ctx.send(embed=embed)

# ==================== TÂCHE QUOTIDIENNE ====================
@tasks.loop(time=time(hour=0, minute=5))
async def daily_presence_reset():
    print("🔄 Création automatique du tableau du jour...")
    await create_daily_presence_table()

# ==================== EVENTS ====================
@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not daily_presence_reset.is_running():
        daily_presence_reset.start()

    # Synchronisation automatique au démarrage
    for guild in bot.guilds:
        await sync_authorized_users(guild)

    await create_daily_presence_table()

@bot.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Détecte l'ajout ou le retrait du rôle Vendetta"""
    before_has = has_vendetta(before)
    after_has = has_vendetta(after)

    if before_has == after_has:
        return

    try:
        with get_db() as conn:
            with conn.cursor() as c:
                if after_has and not before_has:
                    # Rôle ajouté
                    c.execute("""
                        INSERT INTO authorized_users (user_id, username)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET username = EXCLUDED.username
                    """, (after.id, after.display_name))
                    print(f"✅ {after.display_name} a reçu le rôle Vendetta → ajouté")
                elif before_has and not after_has:
                    # Rôle retiré
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (after.id,))
                    c.execute("DELETE FROM presences WHERE user_id = %s AND operation_date = %s", (after.id, date.today()))
                    print(f"❌ {after.display_name} a perdu le rôle Vendetta → retiré")
                conn.commit()

        await update_presence_tableau(date.today())
    except Exception as e:
        print(f"Erreur on_member_update: {e}")

@bot.event
async def on_member_remove(member: discord.Member):
    """Quand un membre quitte le serveur"""
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
                c.execute("DELETE FROM presences WHERE user_id = %s AND operation_date = %s", (member.id, date.today()))
                conn.commit()
                print(f"🗑️ {member.display_name} a quitté le serveur → retiré")
        await update_presence_tableau(date.today())
    except Exception as e:
        print(f"Erreur on_member_remove: {e}")

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant dans les variables d'environnement.")
