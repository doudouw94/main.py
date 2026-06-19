import discord
from discord.ext import commands
import psycopg2
import os
from datetime import date, datetime
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="p!", intents=intents)

TABLEAU_CHANNEL_ID = None
PRESENCE_MESSAGE_ID = None
GUILD_ID = None  # ← Tu peux le remplir manuellement si tu veux

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("DROP TABLE IF EXISTS presences;")
                c.execute('''
                    CREATE TABLE presences (
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

# ==================== VIEWS ====================
class PresenceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    async def register(self, interaction: discord.Interaction, status: str):
        try:
            # Vérification autorisation
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
                    
                    # Suppression automatique du message
                    try:
                        await msg.delete()
                    except:
                        pass

                except asyncio.TimeoutError:
                    note = "Retard non précisé"
            else:
                await interaction.response.send_message(f"✅ **{status.upper()}** enregistré !", ephemeral=True)

            # Enregistrement en base
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

            await asyncio.sleep(1.5)
            await update_presence_tableau()

            if status == "late":
                await interaction.followup.send(f"✅ **EN RETARD** enregistré !", ephemeral=True)

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
                            await member.send(f"⚠️ **Rappel Présence**\nTu n'as pas encore marqué ta présence pour **l'opération de ce soir 21h**.")
                            reminded += 1
                            await asyncio.sleep(0.5)
                        except:
                            pass

            await interaction.followup.send(f"✅ Rappel envoyé à **{reminded}** membre(s).", ephemeral=True)
        except Exception as e:
            print(e)
            await interaction.followup.send("❌ Une erreur est survenue.", ephemeral=True)


# ==================== TABLEAU ====================
async def update_presence_tableau():
    if not PRESENCE_MESSAGE_ID or not TABLEAU_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(PRESENCE_MESSAGE_ID)

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT user_id, username FROM authorized_users ORDER BY username")
                authorized = dict(c.fetchall())  # user_id: username

                c.execute("""
                    SELECT user_id, username, status, note 
                    FROM presences 
                    WHERE operation_date = %s
                """, (date.today(),))
                data = {row[0]: (row[1], row[2], row[3]) for row in c.fetchall()}

        embed = discord.Embed(
            title="📋 Présences Opérations 21h",
            description="**Opération de ce soir**",
            color=discord.Color.blurple()
        )

        present = []
        late = []
        absent = []
        unmarked = []

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
            embed.add_field(name=f"✅ Présents ({len(present)})", value="\n".join(present), inline=False)
        if late:
            embed.add_field(name=f"⏰ En Retard ({len(late)})", value="\n".join(late), inline=False)
        if absent:
            embed.add_field(name=f"❌ Absents ({len(absent)})", value="\n".join(absent), inline=False)
        if unmarked:
            embed.add_field(name=f"⚪ Non marqués ({len(unmarked)})", value="\n".join(unmarked), inline=False)

        embed.set_footer(text=f"Dernière MAJ : {datetime.now().strftime('%H:%M:%S')}")

        await message.edit(embed=embed, view=PresenceView())
    except Exception as e:
        print(f"Erreur tableau: {e}")


# ==================== COMMANDES ====================

@bot.command()
@commands.has_permissions(administrator=True)
async def setpresence(ctx):
    global TABLEAU_CHANNEL_ID, PRESENCE_MESSAGE_ID, GUILD_ID
    GUILD_ID = ctx.guild.id
    TABLEAU_CHANNEL_ID = ctx.channel.id

    embed = discord.Embed(
        title="📋 Présences Opérations 21h",
        description="**Opération de ce soir**",
        color=discord.Color.blurple()
    )
    msg = await ctx.send(embed=embed, view=PresenceView())
    PRESENCE_MESSAGE_ID = msg.id
    await ctx.send("✅ **Tableau de présence créé !**")


@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("INSERT INTO authorized_users (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                      (member.id, member.display_name))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** ajouté aux utilisateurs autorisés.")


@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** retiré des utilisateurs autorisés.")


@bot.command()
@commands.has_permissions(administrator=True)
async def cleanusers(ctx):
    """Supprime automatiquement les utilisateurs qui ont quitté le serveur"""
    await ctx.send("🔄 Nettoyage des membres partis...")
    removed = 0

    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT user_id, username FROM authorized_users")
            users = c.fetchall()

            for user_id, username in users:
                if not ctx.guild.get_member(user_id):
                    c.execute("DELETE FROM authorized_users WHERE user_id = %s", (user_id,))
                    removed += 1
            conn.commit()

    await ctx.send(f"✅ **{removed}** utilisateur(s) supprimé(s) (ils ont quitté le serveur).")


@bot.command()
async def listusers(ctx):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("SELECT username FROM authorized_users ORDER BY username")
            users = [row[0] for row in c.fetchall()]
    await ctx.send("**Utilisateurs autorisés :**\n" + "\n".join(f"• {u}" for u in users) if users else "Aucun utilisateur autorisé.")


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
@commands.has_permissions(administrator=True)
async def reset(ctx):
    today = date.today()
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM presences WHERE operation_date = %s", (today,))
            conn.commit()
    await ctx.send("🗑️ Présences du jour réinitialisées.")
    await update_presence_tableau()


@bot.command()
@commands.has_permissions(administrator=True)
async def forceupdate(ctx):
    await update_presence_tableau()
    await ctx.send("✅ Tableau mis à jour.")


@bot.command(name="aide", aliases=["commands"])
async def aide(ctx):
    embed = discord.Embed(title="📜 Commandes du Bot Présence", color=discord.Color.blurple())
    embed.add_field(name="**Commandes Générales**", value="`p!aide`\n`p!listusers`\n`p!stats`", inline=False)
    embed.add_field(name="**Commandes Admin**", 
                    value="`p!setpresence`\n`p!adduser @user`\n`p!removeuser @user`\n`p!cleanusers`\n`p!reset`\n`p!forceupdate`", inline=False)
    await ctx.send(embed=embed)


@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    # Mise à jour initiale si le tableau existe déjà
    if TABLEAU_CHANNEL_ID and PRESENCE_MESSAGE_ID:
        await update_presence_tableau()


if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant dans les variables d'environnement.")
