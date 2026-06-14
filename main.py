import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date, datetime
import asyncio

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="p!", intents=intents)  # Prefix p! pour ne pas confondre avec l'autre bot

# ==================== CONFIG ====================
TABLEAU_CHANNEL_ID = 1515801550533034004
PRESENCE_MESSAGE_ID = None

# ==================== DATABASE ====================
def get_db():
    return psycopg2.connect(os.getenv("DATABASE_URL"))

def migrate_database():
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
                    submitted_at TIMESTAMP DEFAULT NOW()
                );
            ''')
            c.execute('''
                CREATE TABLE IF NOT EXISTS authorized_users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT
                );
            ''')
            conn.commit()
    print("✅ Base de données Présences prête !")

migrate_database()

# ==================== VIEWS ====================
class PresenceView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.green)
    async def present(self, interaction: discord.Interaction, button):
        await self.register_presence(interaction, "present")

    @discord.ui.button(label="❌ Absent", style=discord.ButtonStyle.red)
    async def absent(self, interaction: discord.Interaction, button):
        await self.register_presence(interaction, "absent")

    @discord.ui.button(label="⏰ En Retard", style=discord.ButtonStyle.gray)
    async def late(self, interaction: discord.Interaction, button):
        await self.register_presence(interaction, "late")

    async def register_presence(self, interaction: discord.Interaction, status: str):
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT 1 FROM authorized_users WHERE user_id = %s", (interaction.user.id,))
                if not c.fetchone():
                    return await interaction.response.send_message("❌ Tu n'es pas autorisé à marquer ta présence.", ephemeral=True)

        operation_date = date.today()
        note = None
        if status == "late":
            await interaction.response.send_message("À combien de minutes de retard ? (ex: 15)", ephemeral=True)
            try:
                msg = await bot.wait_for('message', check=lambda m: m.author == interaction.user, timeout=60)
                note = f"Retard de {msg.content} min"
                await msg.delete()
            except:
                note = "Retard non précisé"

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("""
                    INSERT INTO presences (operation_date, user_id, username, status, note)
                    VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (operation_date, user_id) 
                    DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note, submitted_at = NOW()
                """, (operation_date, interaction.user.id, interaction.user.display_name, status, note))
                conn.commit()

        await interaction.followup.send(f"✅ Ta présence **{status.upper()}** a été enregistrée !", ephemeral=True)
        await update_presence_tableau()

# ==================== FONCTIONS ====================
async def update_presence_tableau():
    if not PRESENCE_MESSAGE_ID or not TABLEAU_CHANNEL_ID:
        return
    try:
        channel = bot.get_channel(TABLEAU_CHANNEL_ID)
        message = await channel.fetch_message(PRESENCE_MESSAGE_ID)
        today = date.today()

        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("SELECT username FROM authorized_users ORDER BY username")
                all_users = [row[0] for row in c.fetchall()]

                c.execute("SELECT username, status, note FROM presences WHERE operation_date = %s", (today,))
                data = {row[0]: (row[1], row[2]) for row in c.fetchall()}

        embed = discord.Embed(title="📋 Tableau de Présence - Opérations 21h", 
                            description=f"**Date :** {today.strftime('%d/%m/%Y')}", 
                            color=discord.Color.blurple())

        present = []
        late = []
        absent = []
        unmarked = []

        for user in all_users:
            if user in data:
                status, note = data[user]
                if status == "present":
                    present.append(f"✅ {user}")
                elif status == "late":
                    late.append(f"⏰ {user} {note or ''}")
                else:
                    absent.append(f"❌ {user}")
            else:
                unmarked.append(f"⚪ {user}")

        if present: embed.add_field(name=f"✅ Présents ({len(present)})", value="\n".join(present), inline=False)
        if late: embed.add_field(name=f"⏰ En Retard ({len(late)})", value="\n".join(late), inline=False)
        if absent: embed.add_field(name=f"❌ Absents ({len(absent)})", value="\n".join(absent), inline=False)
        if unmarked: embed.add_field(name=f"⚪ Non marqués ({len(unmarked)})", value="\n".join(unmarked), inline=False)

        embed.set_footer(text=f"Dernière mise à jour : {datetime.now().strftime('%H:%M')}")
        await message.edit(embed=embed, view=PresenceView())

    except Exception as e:
        print(f"Erreur tableau: {e}")

@tasks.loop(minutes=5)
async def auto_update():
    await update_presence_tableau()

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def setpresence(ctx):
    global TABLEAU_CHANNEL_ID, PRESENCE_MESSAGE_ID
    TABLEAU_CHANNEL_ID = ctx.channel.id

    embed = discord.Embed(title="📋 Présences Opérations 21h", 
                         description="Clique sur les boutons ci-dessous pour marquer ta présence", 
                         color=discord.Color.blurple())
    msg = await ctx.send(embed=embed, view=PresenceView())
    PRESENCE_MESSAGE_ID = msg.id
    await ctx.send("✅ **Tableau de présence configuré !**")

@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("""
                INSERT INTO authorized_users (user_id, username)
                VALUES (%s, %s) ON CONFLICT (user_id) DO NOTHING
            """, (member.id, member.display_name))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** a été ajouté.")

@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, member: discord.Member):
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM authorized_users WHERE user_id = %s", (member.id,))
            conn.commit()
    await ctx.send(f"✅ **{member.display_name}** a été retiré.")

@bot.command()
@commands.has_permissions(administrator=True)
async def resetpresence(ctx):
    today = date.today()
    with get_db() as conn:
        with conn.cursor() as c:
            c.execute("DELETE FROM presences WHERE operation_date = %s", (today,))
            conn.commit()
    await ctx.send("✅ Présences du jour réinitialisées.")
    await update_presence_tableau()

@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")
    if not auto_update.is_running():
        auto_update.start()

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
    else:
        print("❌ TOKEN manquant")