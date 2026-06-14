import discord
from discord.ext import commands, tasks
import psycopg2
import os
from datetime import date, datetime

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="p!", intents=intents)

TABLEAU_CHANNEL_ID = None
PRESENCE_MESSAGE_ID = None

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
            note = "Retard non précisé" if status == "late" else None

            with get_db() as conn:
                with conn.cursor() as c:
                    c.execute("""
                        INSERT INTO presences (operation_date, user_id, username, status, note)
                        VALUES (%s, %s, %s, %s, %s)
                        ON CONFLICT (operation_date, user_id) 
                        DO UPDATE SET status = EXCLUDED.status, note = EXCLUDED.note, submitted_at = NOW()
                    """, (operation_date, interaction.user.id, interaction.user.display_name, status, note))
                    conn.commit()

            await interaction.response.send_message(f"✅ **{status.upper()}** enregistré !", ephemeral=True)
            # Mise à jour du tableau
            # await update_presence_tableau()  # on peut le réactiver plus tard

        except Exception as e:
            print(f"Erreur: {e}")
            try:
                await interaction.response.send_message("❌ Erreur lors de l'enregistrement.", ephemeral=True)
            except:
                await interaction.followup.send("❌ Erreur.", ephemeral=True)

    @discord.ui.button(label="✅ Présent", style=discord.ButtonStyle.green)
    async def present(self, interaction: discord.Interaction, button):
        await self.register(interaction, "present")

    @discord.ui.button(label="❌ Absent", style=discord.ButtonStyle.red)
    async def absent(self, interaction: discord.Interaction, button):
        await self.register(interaction, "absent")

    @discord.ui.button(label="⏰ En Retard", style=discord.ButtonStyle.gray)
    async def late(self, interaction: discord.Interaction, button):
        await self.register(interaction, "late")

# ==================== COMMANDES ====================
@bot.command()
@commands.has_permissions(administrator=True)
async def setpresence(ctx):
    global TABLEAU_CHANNEL_ID, PRESENCE_MESSAGE_ID
    TABLEAU_CHANNEL_ID = ctx.channel.id

    embed = discord.Embed(title="📋 Tableau de Présence - Opérations 21h", 
                         description="Clique sur les boutons ci-dessous pour marquer ta présence", 
                         color=discord.Color.blurple())
    msg = await ctx.send(embed=embed, view=PresenceView())
    PRESENCE_MESSAGE_ID = msg.id
    await ctx.send("✅ **Tableau créé avec succès !**")

@bot.command()
@commands.has_permissions(administrator=True)
async def adduser(ctx, member: discord.Member):
    try:
        with get_db() as conn:
            with conn.cursor() as c:
                c.execute("INSERT INTO authorized_users (user_id, username) VALUES (%s, %s) ON CONFLICT DO NOTHING", 
                         (member.id, member.display_name))
                conn.commit()
        await ctx.send(f"✅ **{member.display_name}** ajouté.")
    except:
        await ctx.send("❌ Erreur.")

@bot.event
async def on_ready():
    print(f"✅ {bot.user} est en ligne !")

if __name__ == "__main__":
    token = os.getenv("TOKEN")
    if token:
        bot.run(token)
