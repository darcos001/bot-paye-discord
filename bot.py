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

    # Avertissements pour les noms codés en dur dans bot.py (ne peuvent pas être modifiés automatiquement)
    if any(normaliser(g) == normaliser(ancien_nom) for g in HIERARCHIE):
        messages_suivi.append(
            f"⚠️ Ce rôle fait partie de la **hiérarchie** (/rankup, /derank). Remplace **{ancien_nom}** par "
            f"**{nouveau_nom}** dans la liste `HIERARCHIE` du fichier `bot.py`, puis redéploie le bot — "
            "sinon /rankup et /derank ne reconnaîtront plus ce rôle."
        )

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
  
