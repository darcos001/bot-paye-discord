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
import re
import unicodedata
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ---------------------------------------------------------------------------
# CONFIGURATION
# ---------------------------------------------------------------------------

TOKEN = os.getenv("DISCORD_TOKEN")  # Le token est lu depuis une variable d'environnement

# Dossier où sont stockées les données persistantes. Sur Railway, monte un volume sur ce
# chemin (variable d'environnement DATA_DIR) pour que les données survivent aux redéploiements.
# En local (sans volume), les fichiers sont simplement créés dans le dossier courant.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

GRADES_FILE = os.path.join(DATA_DIR, "grades.json")       # {"grade_nom": taux_horaire}
SERVICES_FILE = os.path.join(DATA_DIR, "services.json")   # {"user_id": [ {grade, heures, date, paye} ]}
SESSIONS_FILE = os.path.join(DATA_DIR, "sessions.json")   # {"user_id": {"grade": ..., "debut": iso}}  (services en cours)
PANEL_FILE = os.path.join(DATA_DIR, "panel.json")         # {"channel_id": ..., "message_id": ...} (panneau permanent des membres en service)     # {"channel_id": ..., "message_id": ...} (panneau permanent des membres en service)
HIERARCHIE_FILE = os.path.join(DATA_DIR, "hierarchie.json")  # ["grade_bas", ..., "grade_haut"] (ordre de la hiérarchie)

# Nom du rôle Discord autorisé à administrer le bot (modifier si besoin)
ADMIN_ROLE_NAME = "Admin Paye"

# Hiérarchie PAR DÉFAUT des grades, du plus bas au plus haut (utilisée par /rankup et /derank).
# Elle n'est utilisée que si hierarchie.json n'existe pas encore (premier démarrage) : elle sert
# alors à initialiser le fichier. Ensuite, c'est hierarchie.json qui fait foi et qui est modifié
# automatiquement par /role_renommer.
# ⚠️ Chaque nom doit correspondre EXACTEMENT au nom d'un rôle Discord existant sur le serveur.
HIERARCHIE_PAR_DEFAUT = [
    "🚨Secouriste",
    "🚨Secouriste - Chef d'équipe",
    "🚨Secouriste - Responsable",
    "😷Infirmier",
    "😷Infirmier chef",
    "🧑‍⚕️Médecin",
    "🧑‍⚕️Médecin chef",
    "❤️‍🩹Chef de service - Radiologie",
    "🩻Chef de service - Médecine Polyvalente",
    "🚨Chef de service - Urgence",
    "Directeur ADJ",
    "Directeur",
]

# Rôle supplémentaire attribué automatiquement en plus du premier grade
# lorsqu'un membre n'a encore aucun rôle de la hiérarchie (via /rankup).
# ⚠️ Doit correspondre EXACTEMENT au nom d'un rôle Discord existant sur le serveur.
ROLE_EMS = "🚨EMS"

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


def get_panel_config():
    return charger_json(PANEL_FILE, None)


def set_panel_config(channel_id: int, message_id: int):
    sauver_json(PANEL_FILE, {"channel_id": channel_id, "message_id": message_id})


def effacer_panel_config():
    if os.path.exists(PANEL_FILE):
        os.remove(PANEL_FILE)


def get_hierarchie() -> list[str]:
    """Charge la hiérarchie depuis hierarchie.json. Si le fichier n'existe pas encore
    (premier démarrage du bot), il est créé à partir de HIERARCHIE_PAR_DEFAUT."""
    if not os.path.exists(HIERARCHIE_FILE):
        set_hierarchie(HIERARCHIE_PAR_DEFAUT)
        return list(HIERARCHIE_PAR_DEFAUT)
    return charger_json(HIERARCHIE_FILE, list(HIERARCHIE_PAR_DEFAUT))


def set_hierarchie(liste: list[str]):
    sauver_json(HIERARCHIE_FILE, liste)


def normaliser(texte: str) -> str:
    """
    Normalise un texte pour la comparaison : minuscules, sans accents, sans espaces superflus.
    Permet de faire correspondre 'médecin', 'Médecin', 'MEDECIN' ou 'medecin' entre eux.
    """
    texte = texte.strip().lower()
    texte = unicodedata.normalize("NFKD", texte)
    texte = "".join(c for c in texte if not unicodedata.combining(c))
    return texte


def est_admin(interaction: discord.Interaction) -> bool:
    """Vérifie si l'utilisateur peut administrer le bot (permission serveur OU rôle dédié)."""
    if interaction.user.guild_permissions.administrator:
        return True
    role_names = [r.name for r in getattr(interaction.user, "roles", [])]
    return ADMIN_ROLE_NAME in role_names


def detecter_grade(membre: discord.Member) -> list[str]:
    """
    Détecte le(s) grade(s) correspondant aux rôles Discord du membre.
    Un grade est reconnu si un rôle du membre porte le même nom (insensible à la casse)
    qu'un grade créé avec /grade_set. Retourne la liste des grades trouvés (noms exacts
    tels qu'enregistrés dans /grade_set), triée du rôle le plus haut au plus bas.
    """
    grades = get_grades()  # {"NomDuGrade": taux}
    grades_par_nom_normalise = {normaliser(nom): nom for nom in grades}
    roles_du_membre = sorted(membre.roles, key=lambda r: r.position, reverse=True)
    grades_trouves = []
    for r in roles_du_membre:
        nom_grade_reel = grades_par_nom_normalise.get(normaliser(r.name))
        if nom_grade_reel and nom_grade_reel not in grades_trouves:
            grades_trouves.append(nom_grade_reel)
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

    if not rafraichir_panel_periodiquement.is_running():
        rafraichir_panel_periodiquement.start()


@tasks.loop(minutes=2)
async def rafraichir_panel_periodiquement():
    """Met à jour le panneau permanent toutes les 2 minutes (pour rafraîchir les durées affichées)."""
    for guild in bot.guilds:
        await mettre_a_jour_panel(guild)


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
        await mettre_a_jour_panel(interaction.guild)


class GradeSelectView(discord.ui.View):
    """Vue temporaire (éphémère) contenant le menu de sélection de grade."""

    def __init__(self):
        super().__init__(timeout=120)
        self.add_item(GradeSelect())


def construire_embed_panel(guild: discord.Guild) -> discord.Embed:
    """Construit l'embed listant les membres actuellement en service."""
    sessions = get_sessions()
    maintenant = datetime.now(timezone.utc)

    if not sessions:
        description = "Personne n'est actuellement en service."
    else:
        lignes = []
        for user_id, session in sessions.items():
            debut = datetime.fromisoformat(session["debut"])
            duree = (maintenant - debut).total_seconds() / 3600
            membre = guild.get_member(int(user_id))
            nom = membre.mention if membre else f"Utilisateur inconnu ({user_id})"
            lignes.append(f"🟢 {nom} — **{session['grade']}** — depuis **{duree:.2f} h**")
        description = "\n".join(lignes)

    embed = discord.Embed(
        title="🕒 Membres actuellement en service",
        description=description,
        color=discord.Color.green(),
    )
    embed.set_footer(text=f"Mis à jour automatiquement — {maintenant.strftime('%d/%m/%Y %H:%M UTC')}")
    return embed


async def mettre_a_jour_panel(guild: discord.Guild):
    """
    Met à jour le panneau permanent (s'il a été créé avec /panel_service) pour refléter
    les membres actuellement en service. Ne fait rien si aucun panneau n'a été configuré.
    """
    config = get_panel_config()
    if config is None:
        return

    channel = guild.get_channel(config["channel_id"])
    if channel is None:
        return

    try:
        message = await channel.fetch_message(config["message_id"])
    except (discord.NotFound, discord.Forbidden):
        effacer_panel_config()  # Le message a été supprimé ou n'est plus accessible -> on oublie ce panneau
        return

    try:
        await message.edit(embed=construire_embed_panel(guild))
    except discord.HTTPException:
        pass  # Échec silencieux (ex: rate limit) -> la prochaine mise à jour réessaiera


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
        await mettre_a_jour_panel(interaction.guild)

    @discord.ui.button(
        label="Fin de service",
        emoji="🔴",
        style=discord.ButtonStyle.danger,
        custom_id="pointeuse_fin",
    )
    async def fin_service(self, interaction: discord.Interaction, button: discord.ui.Button):
        succes, message = terminer_session(str(interaction.user.id))
        await interaction.response.send_message(message, ephemeral=True)
        if succes:
            await mettre_a_jour_panel(interaction.guild)


def terminer_session(user_id: str):
    """
    Clôture la session de service en cours pour user_id, calcule les heures et
    les enregistre dans services.json. Retourne (succès: bool, message: str).
    Fonction partagée entre le bouton 'Fin de service' et la commande admin /service_forcer_fin.
    """
    sessions = get_sessions()

    if user_id not in sessions:
        return False, "Cette personne n'a pas de prise de service en cours."

    session = sessions.pop(user_id)
    set_sessions(sessions)

    debut = datetime.fromisoformat(session["debut"])
    fin = datetime.now(timezone.utc)
    duree_heures = (fin - debut).total_seconds() / 3600
    grade = session["grade"]

    if duree_heures < (1 / 60):  # moins d'une minute : on ignore, probable erreur de clic
        return False, "Service trop court (< 1 minute), il n'a pas été enregistré."

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

    message = (
        f"🔴 Fin de service enregistrée en tant que **{grade}**.\n"
        f"Durée : **{duree_heures:.2f} h** — Montant : **{montant:.2f} €**"
    )
    return True, message


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


@bot.tree.command(name="service_forcer_fin", description="[Admin] Clôturer de force la prise de service d'un membre")
@app_commands.describe(membre="Le membre dont il faut terminer le service en cours")
async def service_forcer_fin(interaction: discord.Interaction, membre: discord.Member):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    succes, message = terminer_session(str(membre.id))

    if not succes:
        await interaction.response.send_message(f"{membre.mention} : {message}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"⚠️ Service de {membre.mention} clôturé de force par {interaction.user.mention}.\n{message}"
    )
    await mettre_a_jour_panel(interaction.guild)


@bot.tree.command(name="service_en_cours", description="[Admin] Voir la liste des membres actuellement en service")
async def service_en_cours(interaction: discord.Interaction):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    sessions = get_sessions()

    if not sessions:
        await interaction.response.send_message("Personne n'est actuellement en service.", ephemeral=True)
        return

    lignes = []
    maintenant = datetime.now(timezone.utc)
    for user_id, session in sessions.items():
        debut = datetime.fromisoformat(session["debut"])
        duree = (maintenant - debut).total_seconds() / 3600
        membre = interaction.guild.get_member(int(user_id))
        nom = membre.mention if membre else f"Utilisateur inconnu ({user_id})"
        lignes.append(f"{nom} — **{session['grade']}** — en service depuis **{duree:.2f} h**")

    embed = discord.Embed(
        title="🟢 Membres actuellement en service",
        description="\n".join(lignes),
        color=discord.Color.green(),
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(
    name="panel_service",
    description="[Admin] Publier le panneau permanent des membres en service (mis à jour automatiquement)",
)
async def panel_service(interaction: discord.Interaction):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    embed = construire_embed_panel(interaction.guild)
    await interaction.response.send_message(embed=embed)
    message_envoye = await interaction.original_response()

    set_panel_config(channel_id=message_envoye.channel.id, message_id=message_envoye.id)


# ---------------------------------------------------------------------------
# GESTION DES GRADES / TAUX HORAIRES
# ---------------------------------------------------------------------------

@bot.tree.command(name="grade_set", description="Définir (ou modifier) le taux horaire d'un grade (associé à un rôle Discord)")
@app_commands.describe(role="Le rôle Discord correspondant à ce grade", taux_horaire="Taux horaire en euros")
async def grade_set(interaction: discord.Interaction, role: discord.Role, taux_horaire: float):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    if taux_horaire <= 0:
        await interaction.response.send_message("Le taux horaire doit être positif.", ephemeral=True)
        return

    grades = get_grades()
    grades[role.name] = taux_horaire
    set_grades(grades)

    await interaction.response.send_message(
        f"✅ Le grade **{role.name}** ({role.mention}) a maintenant un taux horaire de **{taux_horaire:.2f} €/h**."
    )


@bot.tree.command(name="grade_supprimer", description="Supprimer un grade existant")
@app_commands.describe(
    role="Le rôle Discord dont le grade doit être supprimé (recommandé)",
    grade="Ou le nom exact du grade à supprimer, si le rôle Discord n'existe plus",
)
async def grade_supprimer(interaction: discord.Interaction, role: discord.Role = None, grade: str = None):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    if role is None and not grade:
        await interaction.response.send_message(
            "Précise soit `role` (le rôle Discord), soit `grade` (le nom exact du grade).", ephemeral=True
        )
        return

    nom_cible = role.name if role is not None else grade

    grades = get_grades()
    if nom_cible not in grades:
        await interaction.response.send_message(f"Le grade **{nom_cible}** n'existe pas.", ephemeral=True)
        return

    del grades[nom_cible]
    set_grades(grades)
    await interaction.response.send_message(f"🗑️ Le grade **{nom_cible}** a été supprimé.")


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


# Détecte les grades enregistrés par erreur comme mention Discord (ex: "<@&123456789012345>")
# au lieu du nom du rôle en texte — cas classique quand on sélectionne le rôle proposé par
# l'autocomplétion "@" de Discord dans un champ texte au lieu de taper le nom.
_MENTION_ROLE_RE = re.compile(r"^<@&(\d+)>$")


@bot.tree.command(
    name="grade_reparer",
    description="[Admin] Corrige les grades enregistrés comme mention Discord (ex: @Stagiaire) au lieu du nom du rôle",
)
async def grade_reparer(interaction: discord.Interaction):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    grades = get_grades()
    services = get_services()
    corriges = []
    introuvables = []

    for cle in list(grades.keys()):
        correspondance = _MENTION_ROLE_RE.match(cle.strip())
        if not correspondance:
            continue  # déjà un nom normal, rien à faire

        role_id = int(correspondance.group(1))
        role = interaction.guild.get_role(role_id)
        taux = grades.pop(cle)

        if role is None:
            # Le rôle n'existe plus (supprimé) -> on ne peut pas deviner son nom automatiquement
            grades[cle] = taux
            introuvables.append((cle, taux))
            continue

        grades[role.name] = taux
        corriges.append((cle, role.name, taux))

        # Met aussi à jour l'historique de paye déjà enregistré sous l'ancienne clé cassée
        for entrees in services.values():
            for e in entrees:
                if e.get("grade") == cle:
                    e["grade"] = role.name

    set_grades(grades)
    set_services(services)

    lignes = []
    if corriges:
        lignes.append("✅ **Grades corrigés :**")
        for ancien, nouveau, taux in corriges:
            lignes.append(f"• `{ancien}` → **{nouveau}** ({taux:.2f} €/h)")
    if introuvables:
        lignes.append("\n⚠️ **Rôle introuvable pour ces entrées (peut-être supprimé de Discord) :**")
        for cle, taux in introuvables:
            lignes.append(
                f"• `{cle}` ({taux:.2f} €/h) — supprime-le avec `/grade_supprimer grade:{cle}` "
                "puis recrée-le avec `/grade_set`."
            )
    if not corriges and not introuvables:
        lignes.append("✅ Aucun grade cassé trouvé, tout est déjà correct.")

    await interaction.response.send_message("\n".join(lignes)[:2000])


# ---------------------------------------------------------------------------
# DIAGNOSTIC : POURQUOI UN RÔLE N'EST PAS DÉTECTÉ AUTOMATIQUEMENT
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="diagnostic_roles",
    description="[Admin] Compare précisément un rôle Discord et un grade enregistré, caractère par caractère",
)
@app_commands.describe(
    membre="Membre à diagnostiquer (toi-même par défaut, optionnel)",
)
async def diagnostic_roles(interaction: discord.Interaction, membre: discord.Member = None):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    cible = membre or interaction.user
    grades = get_grades()
    grades_par_nom_normalise = {normaliser(nom): nom for nom in grades}

    def codes_unicode(texte: str) -> str:
        # Affiche chaque caractère avec son code Unicode, pour repérer les caractères invisibles
        # (apostrophe courbe, espace insécable, variation d'emoji, etc.)
        return " ".join(f"U+{ord(c):04X}" for c in texte)

    lignes_roles = []
    for r in sorted(cible.roles, key=lambda r: r.position, reverse=True):
        if r.name == "@everyone":
            continue
        nom_correspondant = grades_par_nom_normalise.get(normaliser(r.name))
        if nom_correspondant:
            lignes_roles.append(f"✅ **{r.name}** → détecté comme grade **{nom_correspondant}**")
        else:
            lignes_roles.append(
                f"❌ **{r.name}** → aucun grade ne correspond\n`{codes_unicode(r.name)}`"
            )

    if not lignes_roles:
        lignes_roles = ["Ce membre n'a aucun rôle (hors @everyone)."]

    embed = discord.Embed(
        title=f"🔍 Diagnostic des rôles — {cible.display_name}",
        description="\n\n".join(lignes_roles)[:4000],
        color=discord.Color.orange(),
    )

    lignes_grades = [f"**{nom}**\n`{codes_unicode(nom)}`" for nom in grades]
    embed.add_field(
        name="Grades enregistrés (avec codes Unicode)",
        value=("\n\n".join(lignes_grades) or "Aucun grade enregistré.")[:1024],
        inline=False,
    )
    embed.set_footer(
        text="Si un rôle et un grade se ressemblent mais ont des codes U+... différents "
        "au même endroit, c'est la cause du problème (recréez le grade avec /grade_set "
        "en copiant-collant le nom du rôle depuis Discord)."
    )

    await interaction.response.send_message(embed=embed, ephemeral=True)


# ---------------------------------------------------------------------------
# RENOMMAGE SÉCURISÉ D'UN RÔLE
# ---------------------------------------------------------------------------

@bot.tree.command(
    name="role_renommer",
    description="[Admin] Renommer un rôle Discord en mettant à jour le bot (grade, historique) automatiquement",
)
@app_commands.describe(role="Le rôle Discord à renommer", nouveau_nom="Le nouveau nom du rôle")
async def role_renommer(interaction: discord.Interaction, role: discord.Role, nouveau_nom: str):
    if not est_admin(interaction):
        await interaction.response.send_message(
            "Tu n'as pas la permission d'utiliser cette commande.", ephemeral=True
        )
        return

    ancien_nom = role.name

    if normaliser(ancien_nom) == normaliser(nouveau_nom):
        await interaction.response.send_message(
            "Le nouveau nom est identique à l'ancien (à la casse près).", ephemeral=True
        )
        return

    try:
        await role.edit(name=nouveau_nom, reason=f"Renommage via /role_renommer par {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Je n'ai pas la permission de modifier ce rôle. Vérifie que :\n"
            "- Le bot a la permission **Gérer les rôles**\n"
            "- Le rôle du bot est placé **au-dessus** de ce rôle dans Paramètres du serveur → Rôles",
            ephemeral=True,
        )
        return

    messages_suivi = [f"✅ Rôle renommé : **{ancien_nom}** → **{nouveau_nom}**."]

    # Met à jour le grade (taux horaire) si ce rôle correspondait à un grade enregistré
    grades = get_grades()
    cle_grade_existante = None
    for nom_grade in grades:
        if normaliser(nom_grade) == normaliser(ancien_nom):
            cle_grade_existante = nom_grade
            break

    if cle_grade_existante is not None:
        taux = grades.pop(cle_grade_existante)
        grades[nouveau_nom] = taux
        set_grades(grades)
        messages_suivi.append(
            f"💶 Grade associé mis à jour (taux **{taux:.2f} €/h** conservé)."
        )

        # Met à jour l'historique des services déjà enregistrés pour garder la cohérence des payes
        services = get_services()
        nb_maj = 0
        for entrees in services.values():
            for e in entrees:
                if e.get("grade") == cle_grade_existante:
                    e["grade"] = nouveau_nom
                    nb_maj += 1
        if nb_maj:
            set_services(services)
            messages_suivi.append(f"📜 {nb_maj} entrée(s) de l'historique des services mise(s) à jour.")

    # Met à jour la hiérarchie (/rankup, /derank, /hierarchie) si ce rôle en faisait partie
    hierarchie_liste = get_hierarchie()
    for i, grade in enumerate(hierarchie_liste):
        if normaliser(grade) == normaliser(ancien_nom):
            hierarchie_liste[i] = nouveau_nom
            set_hierarchie(hierarchie_liste)
            messages_suivi.append(
                f"🏅 Position {i + 1} de la **hiérarchie** mise à jour "
                f"(/rankup, /derank, /hierarchie prennent en compte le nouveau nom immédiatement)."
            )
            break

    if normaliser(ancien_nom) == normaliser(ROLE_EMS):
        messages_suivi.append(
            f"⚠️ Ce rôle est le rôle **EMS** automatique. Mets à jour `ROLE_EMS = \"{nouveau_nom}\"` "
            "dans `bot.py` et redéploie."
        )

    if normaliser(ancien_nom) == normaliser(ADMIN_ROLE_NAME):
        messages_suivi.append(
            f"⚠️ Ce rôle était le rôle **admin du bot**. Mets à jour "
            f"`ADMIN_ROLE_NAME = \"{nouveau_nom}\"` dans `bot.py` et redéploie, sinon ses membres "
            "perdront l'accès aux commandes d'administration."
        )

    await interaction.response.send_message("\n".join(messages_suivi))


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
    La comparaison ignore la casse (majuscules/minuscules).
    Si le membre a plusieurs rôles de la hiérarchie (cas anormal), le plus haut est retourné.
    """
    hierarchie = get_hierarchie()
    noms_roles_membre = {normaliser(r.name) for r in membre.roles}
    rangs_trouves = [
        (i, grade) for i, grade in enumerate(hierarchie) if normaliser(grade) in noms_roles_membre
    ]
    if not rangs_trouves:
        return None, None
    # Si plusieurs rôles hiérarchiques sont présents, on garde le plus haut (index le plus grand)
    return max(rangs_trouves, key=lambda x: x[0])


async def changer_role_hierarchie(
    interaction: discord.Interaction, membre: discord.Member, ancien_grade: str | None, nouveau_grade: str
):
    """Retire l'ancien rôle de hiérarchie (s'il existe) et attribue le nouveau. Retourne (succès, message_erreur).
    La recherche du rôle Discord ignore la casse (majuscules/minuscules)."""
    guild = interaction.guild
    nouveau_role = discord.utils.find(lambda r: normaliser(r.name) == normaliser(nouveau_grade), guild.roles)

    if nouveau_role is None:
        return False, (
            f"❌ Le rôle Discord **{nouveau_grade}** n'existe pas sur ce serveur. "
            f"Crée-le d'abord (Paramètres du serveur → Rôles) — le nom peut être en minuscules, "
            f"seule l'orthographe compte, pas la casse."
        )

    try:
        if ancien_grade is not None:
            ancien_role = discord.utils.find(lambda r: normaliser(r.name) == normaliser(ancien_grade), guild.roles)
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

    hierarchie = get_hierarchie()
    index_actuel, grade_actuel = obtenir_rang_actuel(membre)

    if index_actuel is None:
        # Le membre n'a aucun grade de la hiérarchie -> on lui attribue le premier (Secouriste)
        nouveau_grade = hierarchie[0]
        succes, erreur = await changer_role_hierarchie(interaction, membre, None, nouveau_grade)
        if not succes:
            await interaction.response.send_message(erreur, ephemeral=True)
            return

        # On lui attribue en plus le rôle EMS s'il ne l'a pas déjà
        role_ems = discord.utils.find(lambda r: normaliser(r.name) == normaliser(ROLE_EMS), interaction.guild.roles)
        if role_ems is None:
            await interaction.response.send_message(
                f"⬆️ {membre.mention} commence maintenant au grade **{nouveau_grade}**.\n"
                f"⚠️ Le rôle **{ROLE_EMS}** n'existe pas sur ce serveur, il n'a donc pas pu être ajouté."
            )
            return

        if role_ems not in membre.roles:
            try:
                await membre.add_roles(role_ems, reason="Attribution automatique du rôle EMS via /rankup")
            except discord.Forbidden:
                await interaction.response.send_message(
                    f"⬆️ {membre.mention} commence maintenant au grade **{nouveau_grade}**.\n"
                    f"❌ Je n'ai pas la permission d'ajouter le rôle **{ROLE_EMS}**."
                )
                return

        await interaction.response.send_message(
            f"⬆️ {membre.mention} commence maintenant au grade **{nouveau_grade}** et reçoit le rôle **{ROLE_EMS}**."
        )
        return

    if index_actuel >= len(hierarchie) - 1:
        await interaction.response.send_message(
            f"{membre.mention} est déjà au grade le plus élevé (**{grade_actuel}**).", ephemeral=True
        )
        return

    nouveau_grade = hierarchie[index_actuel + 1]
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

    nouveau_grade = get_hierarchie()[index_actuel - 1]
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

    position = f"{index_actuel + 1}/{len(get_hierarchie())}"
    await interaction.response.send_message(
        f"📊 {membre.mention} est actuellement **{grade_actuel}** (rang {position})."
    )


@bot.tree.command(name="hierarchie", description="Afficher l'ordre complet de la hiérarchie des grades")
async def hierarchie(interaction: discord.Interaction):
    lignes = [f"{i + 1}. {grade}" for i, grade in enumerate(get_hierarchie())]
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
