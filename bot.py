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
SESSIONS_FILE = "sessions.json"   # {"user_id": {"grade": ..., "debut": iso}}  (services en cours)

# Nom du rôle Discord autorisé à administrer le bot (modifier si besoin)
ADMIN_ROLE_NAME = "Admin Paye"

# Hiérarchie des grades, du plus bas au plus haut (utilisée par /rankup et /derank).
# ⚠️ Chaque nom doit correspondre EXACTEMENT au nom d'un rôle Discord existant sur le serveur.
HIERARCHIE = [
    "Stagiaire",
    "Secouriste",
    "Ambulancier",
    "Aide-soignant",
    "Infirmier",
    "Infirmier en Chef",
    "Docteur",
    "Médecin",
    "Médecin Chef",
    "Chef de Service",
    "Co-Directeur",
]

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


def get_sessions() -> dict:
    return charger_json(SESSIONS_FILE, {})


def set_sessions(data: dict):
    sauver_json(SESSIONS_FILE, data)


def est_admin(interaction: discord.Interaction) -> bool:
    """Vérifie si l'utilisateur peut administrer le bot (permission serveur OU rôle dédié)."""
    if interaction.user.guild_permissions.administrator:
        return True
    role_names = [r.name for r in getattr(interaction.user, "roles", [])]
    return ADMIN_ROLE_NAME in role_names


def detecter_grade(membre: discord.Member) -> list[str]:
    """
    Détecte le(s) grade(s) correspondant aux rôles Discord du membre.
    Un grade est reconnu si un rôle du membre porte exactement le même nom
    qu'un grade créé avec /grade_set. Retourne la liste des grades trouvés,
    triée du rôle le plus haut (le plus senior) au plus bas dans la hiérarchie.
    """
    grades = get_grades()
    roles_du_membre = sorted(membre.roles, key=lambda r: r.position, reverse=True)
    grades_trouves = [r.name for r in roles_du_membre if r.name in grades]
    return grades_trouves


# ---------------------------------------------------------------------------
# INITIALISATION DU BOT
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


@bot.event
async def on_ready():
    bot.add_view(PointeuseView())  # Rend les boutons de la pointeuse actifs après un redémarrage
    try:
        synced = await bot.tree.sync()
        print(f"{len(synced)} commande(s) slash synchronisée(s).")
    except Exception as e:
        print(f"Erreur de synchronisation : {e}")
    print(f"Connecté en tant que {bot.user} (ID: {bot.user.id})")


# ---------------------------------------------------------------------------
# POINTEUSE - PRISE / FIN DE SERVICE PAR BOUTONS
# ---------------------------------------------------------------------------

class GradeSelect(discord.ui.Select):
    """Menu déroulant affiché après le clic sur 'Prise de service' pour choisir le grade."""

    def __init__(self):
        grades = get_grades()
        options = [
            discord.SelectOption(label=grade, description=f"{taux:.2f} €/h")
            for grade, taux in grades.items()
        ][:25]  # Discord limite à 25 options max
        super().__init__(
            placeholder="Choisis ton grade pour cette prise de service",
            options=options,
            custom_id="pointeuse_grade_select",
        )

    async def callback(self, interaction: discord.Interaction):
        grade_choisi = self.values[0]
        sessions = get_sessions()
        user_id = str(interaction.user.id)

        if user_id in sessions:
            await interaction.response.edit_message(
                content="⚠️ Tu as déjà une prise de service en cours.", view=None
            )
            return

        sessions[user_id] = {
            "grade": grade_choisi,
            "debut": datetime.now(timezone.utc).isoformat(),
        }
        set_sessions(sessions)

        await interaction.response.edit_message(
            content=(
                f"🟢 Prise de service enregistrée en tant que **{grade_choisi}** "
                f"à {datetime.now(timezone.utc).strftime('%H:%M UTC')}.\n"
                f"Clique sur **Fin de service** quand tu auras terminé."
            ),
            view=None,
        )


class GradeSelectView(discord.ui.View):
    """Vue temporaire (éphémère) contenant le menu de sélection de grade."""

    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(GradeSelect())


class PointeuseView(discord.ui.View):
    """Vue persistante avec les boutons Prise de service / Fin de service."""

    def __init__(self):
        super().__init__(timeout=None)  # timeout=None => la vue reste active indéfiniment

    @discord.ui.button(
        label="Prise de service",
        emoji="🟢",
        style=discord.ButtonStyle.success,
        custom_id="pointeuse_debut",
    )
    async def prise_service(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions = get_sessions()
        user_id = str(interaction.user.id)

        if user_id in sessions:
            await interaction.response.send_message(
                "⚠️ Tu as déjà une prise de service en cours. Utilise **Fin de service** pour la clôturer.",
                ephemeral=True,
            )
            return

        grades = get_grades()
        if not grades:
            await interaction.response.send_message(
                "Aucun grade n'a encore été configuré. Demande à un admin d'utiliser `/grade_set`.",
                ephemeral=True,
            )
            return

        grades_detectes = detecter_grade(interaction.user)

        if not grades_detectes:
            # Aucun rôle Discord ne correspond à un grade connu -> on demande de choisir manuellement
            await interaction.response.send_message(
                "Aucun rôle correspondant à un grade connu n'a été trouvé sur ton profil.\n"
                "Sélectionne ton grade manuellement, ou demande à un admin de vérifier tes rôles :",
                view=GradeSelectView(),
                ephemeral=True,
            )
            return

        if len(grades_detectes) > 1:
            # Plusieurs rôles correspondent à des grades -> le membre choisit lequel utiliser
            await interaction.response.send_message(
                f"Plusieurs grades détectés sur ton profil ({', '.join(grades_detectes)}). "
                "Sélectionne celui à utiliser pour cette prise de service :",
                view=GradeSelectView(),
                ephemeral=True,
            )
            return

        # Un seul grade détecté -> démarrage automatique, sans menu déroulant
        grade_choisi = grades_detectes[0]
        sessions = get_sessions()
        user_id = str(interaction.user.id)

        if user_id in sessions:
            await interaction.response.send_message(
                "⚠️ Tu as déjà une prise de service en cours. Utilise **Fin de service** pour la clôturer.",
                ephemeral=True,
            )
            return

        sessions[user_id] = {
            "grade": grade_choisi,
            "debut": datetime.now(timezone.utc).isoformat(),
        }
        set_sessions(sessions)

        await interaction.response.send_message(
            f"🟢 Prise de service enregistrée en tant que **{grade_choisi}** "
            f"(détecté automatiquement) à {datetime.now(timezone.utc).strftime('%H:%M UTC')}.\n"
            f"Clique sur **Fin de service** quand tu auras terminé.",
            ephemeral=True,
        )

    @discord.ui.button(
        label="Fin de service",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="pointeuse_fin",
    )
    async def fin_service(self, interaction: discord.Interaction, button: discord.ui.Button):
        sessions = get_sessions()
        user_id = str(interaction.user.id)

        if user_id not in sessions:
            await interaction.response.send_message(
                "Tu n'as pas de prise de service en cours.", ephemeral=True
            )
            return

        session = sessions.pop(user_id)
        set_sessions(sessions)

        debut = datetime.fromisoformat(session["debut"])
        fin = datetime.now(timezone.utc)
        duree_heures = (fin - debut).total_seconds() / 3600
        grade = session["grade"]

        if duree_heures < (1 / 60):  # moins d'une minute : on ignore, probable erreur de clic
            await interaction.response.send_message(
                "Service trop court (< 1 minute), il n'a pas été enregistré.", ephemeral=True
            )
            return

        grades = get_grades()
        taux = grades.get(grade, 0)
        montant = duree_heures * taux

        services = get_services()
        services.setdefault(user_id, [])
        services[user_id].append(
            {
                "grade": grade,
                "heures": round(duree_heures, 2),
                "date": fin.isoformat(),
                "paye": False,
            }
        )
        set_services(services)

        await interaction.response.send_message(
            f"🔴 Fin de service enregistrée en tant que **{grade}**.\n"
            f"Durée : **{duree_heures:.2f} h** — Montant : **{montant:.2f} €**",
            ephemeral=True,
        )


@bot.tree.command(name="pointeuse", description="Publier le panneau de prise/fin de service (boutons)")
async def pointeuse(interaction: discord.Interaction):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🕒 Pointeuse de service",
        description=(
            "Clique sur **Prise de service** au début de ton service "
            "et sur **Fin de service** quand tu as terminé.\n\n"
            "Les heures sont calculées et enregistrées automatiquement."
        ),
        color=discord.Color.blurple(),
    )
    await interaction.response.send_message(embed=embed, view=PointeuseView())


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
# SYSTÈME DE HIÉRARCHIE - RANKUP / DERANK
# ---------------------------------------------------------------------------

def obtenir_rang_actuel(membre: discord.Member):
    """
    Retourne (index, nom_du_grade) du rang actuel du membre dans la HIERARCHIE,
    ou (None, None) si le membre n'a aucun rôle de la hiérarchie.
    Si le membre a plusieurs rôles de la hiérarchie (cas anormal), le plus haut est retourné.
    """
    noms_roles_membre = {r.name for r in membre.roles}
    rangs_trouves = [
        (i, grade) for i, grade in enumerate(HIERARCHIE) if grade in noms_roles_membre
    ]
    if not rangs_trouves:
        return None, None
    # Si plusieurs rôles hiérarchiques sont présents, on garde le plus haut (index le plus grand)
    return max(rangs_trouves, key=lambda x: x[0])


async def changer_role_hierarchie(
    interaction: discord.Interaction, membre: discord.Member, ancien_grade: str | None, nouveau_grade: str
):
    """Retire l'ancien rôle de hiérarchie (s'il existe) et attribue le nouveau. Retourne (succès, message_erreur)."""
    guild = interaction.guild
    nouveau_role = discord.utils.get(guild.roles, name=nouveau_grade)

    if nouveau_role is None:
        return False, (
            f"❌ Le rôle Discord **{nouveau_grade}** n'existe pas sur ce serveur. "
            f"Crée-le d'abord (Paramètres du serveur → Rôles) avec ce nom exact."
        )

    try:
        if ancien_grade is not None:
            ancien_role = discord.utils.get(guild.roles, name=ancien_grade)
            if ancien_role is not None and ancien_role in membre.roles:
                await membre.remove_roles(ancien_role, reason="Changement de grade via /rankup ou /derank")
        await membre.add_roles(nouveau_role, reason="Changement de grade via /rankup ou /derank")
    except discord.Forbidden:
        return False, (
            "❌ Je n'ai pas la permission de gérer les rôles. Vérifie que :\n"
            "- Le bot a la permission **Gérer les rôles**\n"
            "- Le rôle du bot est placé **au-dessus** des rôles de la hiérarchie dans "
            "Paramètres du serveur → Rôles"
        )

    return True, None


@bot.tree.command(name="rankup", description="Faire monter un membre d'un grade dans la hiérarchie")
@app_commands.describe(membre="Le membre à faire monter en grade")
async def rankup(interaction: discord.Interaction, membre: discord.Member):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    index_actuel, grade_actuel = obtenir_rang_actuel(membre)

    if index_actuel is None:
        # Le membre n'a aucun grade de la hiérarchie -> on lui attribue le premier (Stagiaire)
        nouveau_grade = HIERARCHIE[0]
        succes, erreur = await changer_role_hierarchie(interaction, membre, None, nouveau_grade)
        if not succes:
            await interaction.response.send_message(erreur, ephemeral=True)
            return
        await interaction.response.send_message(
            f"⬆️ {membre.mention} commence maintenant au grade **{nouveau_grade}**."
        )
        return

    if index_actuel >= len(HIERARCHIE) - 1:
        await interaction.response.send_message(
            f"{membre.mention} est déjà au grade le plus élevé (**{grade_actuel}**).", ephemeral=True
        )
        return

    nouveau_grade = HIERARCHIE[index_actuel + 1]
    succes, erreur = await changer_role_hierarchie(interaction, membre, grade_actuel, nouveau_grade)
    if not succes:
        await interaction.response.send_message(erreur, ephemeral=True)
        return

    await interaction.response.send_message(
        f"⬆️ {membre.mention} passe de **{grade_actuel}** à **{nouveau_grade}** !"
    )


@bot.tree.command(name="derank", description="Faire descendre un membre d'un grade dans la hiérarchie")
@app_commands.describe(membre="Le membre à faire descendre en grade")
async def derank(interaction: discord.Interaction, membre: discord.Member):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    index_actuel, grade_actuel = obtenir_rang_actuel(membre)

    if index_actuel is None:
        await interaction.response.send_message(
            f"{membre.mention} n'a aucun grade de la hiérarchie actuellement.", ephemeral=True
        )
        return

    if index_actuel == 0:
        await interaction.response.send_message(
            f"{membre.mention} est déjà au grade le plus bas (**{grade_actuel}**). "
            "Impossible de descendre davantage.",
            ephemeral=True,
        )
        return

    nouveau_grade = HIERARCHIE[index_actuel - 1]
    succes, erreur = await changer_role_hierarchie(interaction, membre, grade_actuel, nouveau_grade)
    if not succes:
        await interaction.response.send_message(erreur, ephemeral=True)
        return

    await interaction.response.send_message(
        f"⬇️ {membre.mention} descend de **{grade_actuel}** à **{nouveau_grade}**."
    )


@bot.tree.command(name="rang", description="Afficher le grade actuel d'un membre dans la hiérarchie")
@app_commands.describe(membre="Le membre à consulter")
async def rang(interaction: discord.Interaction, membre: discord.Member):
    index_actuel, grade_actuel = obtenir_rang_actuel(membre)

    if index_actuel is None:
        await interaction.response.send_message(
            f"{membre.mention} n'a aucun grade de la hiérarchie actuellement."
        )
        return

    position = f"{index_actuel + 1}/{len(HIERARCHIE)}"
    await interaction.response.send_message(
        f"📊 {membre.mention} est actuellement **{grade_actuel}** (rang {position})."
    )


@bot.tree.command(name="hierarchie", description="Afficher l'ordre complet de la hiérarchie des grades")
async def hierarchie(interaction: discord.Interaction):
    lignes = [f"{i + 1}. {grade}" for i, grade in enumerate(HIERARCHIE)]
    embed = discord.Embed(
        title="🏅 Hiérarchie des grades",
        description="\n".join(lignes),
        color=discord.Color.purple(),
    )
    await interaction.response.send_message(embed=embed)



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
