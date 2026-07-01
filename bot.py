"""
Bot Discord - Calcul de paye selon grade et heures de service
================================================================
Ce bot permet de :
  - Définir un taux horaire par grade
  - Enregistrer des heures de service pour un membre (avec un grade donné)
  - Calculer automatiquement la paye totale d'un membre
  - Consulter l'historique des services enregistrés
  - Réinitialiser (payer) les heures d'un membre

Toutes les données sont sauvegardées dans des fichiers JSON locaux
(grades.json et services.json) afin de persister entre les redémarrages.
"""

import json
import os
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")  # Le token est lu depuis une variable d'environnement
GRADES_FILE = "grades.json"       # {"grade_nom": taux_horaire}
SERVICES_FILE = "services.json"   # {"user_id": [ {grade, heures, date, paye} ]}

# Nom du rôle Discord autorisé à administrer le bot (modifier si besoin)
ADMIN_ROLE_NAME = "Admin Paye"

# ---------------------------------------------------------------------------
# UTILITAIRES DE STOCKAGE
# ---------------------------------------------------------------------------

def charger_json(chemin: str, defaut):
    if not os.path.exists(chemin):
        return defaut
    with open(chemin, "r", encoding="utf-8") as f:
        try:
            return json.load(f)
        except json.JSONDecodeError:
            return defaut


def sauver_json(chemin: str, data):
    with open(chemin, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def get_grades() -> dict:
    return charger_json(GRADES_FILE, {})


def set_grades(data: dict):
    sauver_json(GRADES_FILE, data)


def get_services() -> dict:
    return charger_json(SERVICES_FILE, {})


def set_services(data: dict):
    sauver_json(SERVICES_FILE, data)


def est_admin(interaction: discord.Interaction) -> bool:
    """Vérifie si l'utilisateur peut administrer le bot (permission serveur OU rôle dédié)."""
    if interaction.user.guild_permissions.administrator:
        return True
    role_names = [r.name for r in getattr(interaction.user, "roles", [])]
    return ADMIN_ROLE_NAME in role_names


# ---------------------------------------------------------------------------
# INITIALISATION DU BOT
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commande(s) slash synchronisée(s).")
    except Exception as e:
        print(f"Erreur de synchronisation : {e}")
    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")


# ---------------------------------------------------------------------------
# GESTION DES GRADES / TAUX HORAIRES
# ---------------------------------------------------------------------------

@bot.tree.command(name="grade_set", description="Définir (ou modifier) le taux horaire d'un grade")
@app_commands.describe(grade="Nom du grade (ex: Agent, Sergent, Capitaine)", taux_horaire="Taux horaire en euros")
async def grade_set(interaction: discord.Interaction, grade: str, taux_horaire: float):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    if taux_horaire <= 0:
        await interaction.response.send_message("Le taux horaire doit être positif.", ephemeral=True)
        return

    grades = get_grades()
    grades[grade] = taux_horaire
    set_grades(grades)

    await interaction.response.send_message(
        f"✅ Le grade **{grade}** a maintenant un taux horaire de **{taux_horaire:.2f} €/h**."
    )


@bot.tree.command(name="grade_supprimer", description="Supprimer un grade existant")
@app_commands.describe(grade="Nom du grade à supprimer")
async def grade_supprimer(interaction: discord.Interaction, grade: str):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    grades = get_grades()
    if grade not in grades:
        await interaction.response.send_message(f"Le grade **{grade}** n'existe pas.", ephemeral=True)
        return

    del grades[grade]
    set_grades(grades)
    await interaction.response.send_message(f"🗑️ Le grade **{grade}** a été supprimé.")


@bot.tree.command(name="grade_liste", description="Afficher la liste des grades et leur taux horaire")
async def grade_liste(interaction: discord.Interaction):
    grades = get_grades()
    if not grades:
        await interaction.response.send_message("Aucun grade n'a encore été configuré.")
        return

    lignes = [f"**{g}** — {t:.2f} €/h" for g, t in sorted(grades.items(), key=lambda x: x[1], reverse=True)]
    embed = discord.Embed(
        title="📋 Grades et taux horaires",
        description="\n".join(lignes),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed)


# ---------------------------------------------------------------------------
# ENREGISTREMENT DES HEURES DE SERVICE
# ---------------------------------------------------------------------------

@bot.tree.command(name="service_ajouter", description="Ajouter des heures de service pour un membre")
@app_commands.describe(
    membre="Le membre concerné",
    grade="Grade sous lequel les heures ont été effectuées",
    heures="Nombre d'heures effectuées (ex: 2.5)",
)
async def service_ajouter(
    interaction: discord.Interaction, membre: discord.Member, grade: str, heures: float
):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    if heures <= 0:
        await interaction.response.send_message("Le nombre d'heures doit être positif.", ephemeral=True)
        return

    grades = get_grades()
    if grade not in grades:
        await interaction.response.send_message(
            f"Le grade **{grade}** n'existe pas. Utilise `/grade_set` pour le créer d'abord.",
            ephemeral=True,
        )
        return

    services = get_services()
    user_id = str(membre.id)
    services.setdefault(user_id, [])
    services[user_id].append(
        {
            "grade": grade,
            "heures": heures,
            "date": datetime.now(timezone.utc).isoformat(),
            "paye": False,
        }
    )
    set_services(services)

    taux = grades[grade]
    montant = taux * heures
    await interaction.response.send_message(
        f"✅ **{heures:.2f} h** ajoutées pour {membre.mention} en tant que **{grade}** "
        f"({taux:.2f} €/h) → **{montant:.2f} €**"
    )


@bot.tree.command(name="service_retirer_dernier", description="Retirer le dernier service ajouté pour un membre")
@app_commands.describe(membre="Le membre concerné")
async def service_retirer_dernier(interaction: discord.Interaction, membre: discord.Member):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    services = get_services()
    user_id = str(membre.id)
    if not services.get(user_id):
        await interaction.response.send_message(f"Aucun service enregistré pour {membre.mention}.", ephemeral=True)
        return

    dernier = services[user_id].pop()
    set_services(services)
    await interaction.response.send_message(
        f"🗑️ Dernier service retiré pour {membre.mention} : "
        f"{dernier['heures']:.2f} h en tant que **{dernier['grade']}**."
    )


# ---------------------------------------------------------------------------
# CALCUL ET CONSULTATION DE LA PAYE
# ---------------------------------------------------------------------------

def calculer_paye(user_id: str, uniquement_non_payes: bool = True):
    """Retourne (total_heures, total_montant, detail_par_grade) pour un utilisateur."""
    grades = get_grades()
    services = get_services()
    entrees = services.get(user_id, [])

    total_heures = 0.0
    total_montant = 0.0
    detail = {}

    for entree in entrees:
        if uniquement_non_payes and entree.get("paye"):
            continue
        grade = entree["grade"]
        heures = entree["heures"]
        taux = grades.get(grade, 0)
        montant = heures * taux

        total_heures += heures
        total_montant += montant
        detail.setdefault(grade, {"heures": 0.0, "montant": 0.0})
        detail[grade]["heures"] += heures
        detail[grade]["montant"] += montant

    return total_heures, total_montant, detail


@bot.tree.command(name="paye", description="Calculer la paye d'un membre (heures non encore payées)")
@app_commands.describe(membre="Le membre concerné")
async def paye(interaction: discord.Interaction, membre: discord.Member):
    user_id = str(membre.id)
    total_heures, total_montant, detail = calculer_paye(user_id, uniquement_non_payes=True)

    if not detail:
        await interaction.response.send_message(f"Aucune heure à payer pour {membre.mention}.")
        return

    lignes = [
        f"**{grade}** : {info['heures']:.2f} h → {info['montant']:.2f} €"
        for grade, info in detail.items()
    ]
    embed = discord.Embed(
        title=f"💰 Paye de {membre.display_name}",
        description="\n".join(lignes),
        color=discord.Color.gold(),
    )
    embed.add_field(name="Total heures", value=f"{total_heures:.2f} h", inline=True)
    embed.add_field(name="Total à payer", value=f"{total_montant:.2f} €", inline=True)

    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="paye_historique", description="Voir l'historique complet des services d'un membre")
@app_commands.describe(membre="Le membre concerné")
async def paye_historique(interaction: discord.Interaction, membre: discord.Member):
    services = get_services()
    entrees = services.get(str(membre.id), [])

    if not entrees:
        await interaction.response.send_message(f"Aucun historique pour {membre.mention}.")
        return

    grades = get_grades()
    lignes = []
    for e in entrees[-15:]:  # les 15 dernières entrées pour ne pas dépasser la limite Discord
        date = e["date"][:10]
        statut = "✅ payé" if e.get("paye") else "🕒 en attente"
        montant = e["heures"] * grades.get(e["grade"], 0)
        lignes.append(f"`{date}` — {e['heures']:.2f} h ({e['grade']}) = {montant:.2f} € — {statut}")

    embed = discord.Embed(
        title=f"📜 Historique de {membre.display_name}",
        description="\n".join(lignes),
        color=discord.Color.dark_teal(),
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="paye_valider", description="Marquer les heures d'un membre comme payées (remise à zéro)")
@app_commands.describe(membre="Le membre concerné")
async def paye_valider(interaction: discord.Interaction, membre: discord.Member):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    user_id = str(membre.id)
    services = get_services()
    entrees = services.get(user_id, [])

    if not entrees:
        await interaction.response.send_message(f"Aucune heure enregistrée pour {membre.mention}.", ephemeral=True)
        return

    total_heures, total_montant, _ = calculer_paye(user_id, uniquement_non_payes=True)

    for e in entrees:
        e["paye"] = True
    set_services(services)

    await interaction.response.send_message(
        f"✅ Paye validée pour {membre.mention} : **{total_heures:.2f} h** payées, "
        f"soit **{total_montant:.2f} €**."
    )


# ---------------------------------------------------------------------------
# LANCEMENT DU BOT
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit(
            "❌ Aucune variable d'environnement DISCORD_TOKEN trouvée.\n"
            "Définis-la avant de lancer le bot, par exemple :\n"
            "  export DISCORD_TOKEN='ton_token_ici'   (Linux/Mac)\n"
            "  set DISCORD_TOKEN=ton_token_ici        (Windows)"
        )
    bot.run(TOKEN)
