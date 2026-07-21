# Bot Discord - Calcul de Paye par Grade

Bot Discord permettant de gérer la paye des membres d'un serveur en fonction
de leur **grade** et des **heures de service** effectuées.

## 🧩 Fonctionnalités

- Définir un taux horaire (€/h) pour chaque grade
- Enregistrer des heures de service pour un membre, sous un grade donné
- Calculer automatiquement le montant à payer
- Consulter l'historique des services
- Marquer les heures comme "payées"

## 📦 Installation

1. **Installer Python 3.10+** si ce n'est pas déjà fait.

2. **Installer les dépendances** :
   ```bash
   pip install -r requirements.txt
   ```

3. **Créer une application Discord** :
   - Va sur https://discord.com/developers/applications
   - Crée une nouvelle application → onglet **Bot** → **Add Bot**
   - Active l'intent **Server Members Intent** (nécessaire pour `discord.Member`)
   - Copie le **token** du bot

4. **Définir le token en variable d'environnement** :
   ```bash
   # Linux / Mac
   export DISCORD_TOKEN="ton_token_ici"

   # Windows (cmd)
   set DISCORD_TOKEN=ton_token_ici

   # Windows (PowerShell)
   $env:DISCORD_TOKEN="ton_token_ici"
   ```

5. **Inviter le bot sur ton serveur** avec les permissions :
   - `applications.commands`
   - `bot` avec au minimum : Envoyer des messages, Utiliser les commandes slash

   Lien d'invitation type (remplace `CLIENT_ID`) :
   ```
   https://discord.com/api/oauth2/authorize?client_id=CLIENT_ID&permissions=2147485696&scope=bot%20applications.commands
   ```

6. **Lancer le bot** :
   ```bash
   python bot.py
   ```

## ⚙️ Configuration des droits admin

Par défaut, les commandes d'administration (ajout d'heures, gestion des grades,
validation de paye) sont réservées :
- aux membres ayant la permission **Administrateur** sur le serveur, **ou**
- aux membres possédant un rôle nommé **"Admin Paye"**

Tu peux changer ce nom de rôle en modifiant la constante `ADMIN_ROLE_NAME`
en haut du fichier `bot.py`.

## 📋 Commandes disponibles

| Commande | Description | Accès |
|---|---|---|
| `/grade_set <grade> <taux_horaire>` | Crée ou modifie le taux horaire d'un grade | Admin |
| `/grade_supprimer <grade>` | Supprime un grade | Admin |
| `/grade_liste` | Affiche tous les grades et leurs taux | Tout le monde |
| `/service_ajouter <membre> <grade> <heures>` | Ajoute des heures de service à un membre | Admin |
| `/service_retirer_dernier <membre>` | Annule le dernier service ajouté | Admin |
| `/paye <membre>` | Calcule la paye due (heures non payées) | Tout le monde |
| `/paye_historique <membre>` | Affiche l'historique des services | Tout le monde |
| `/paye_valider <membre>` | Marque les heures comme payées (remise à zéro du compteur) | Admin |

## 💡 Exemple d'utilisation

```
/grade_set grade:Agent taux_horaire:15
/grade_set grade:Sergent taux_horaire:20
/grade_set grade:Capitaine taux_horaire:25

/service_ajouter membre:@Jean grade:Agent heures:3.5
/service_ajouter membre:@Jean grade:Sergent heures:2

/paye membre:@Jean
→ Agent : 3.50 h → 52.50 €
→ Sergent : 2.00 h → 40.00 €
→ Total : 5.50 h — 92.50 €

/paye_valider membre:@Jean   (remet le compteur à zéro une fois payé)
```

## 🗂️ Stockage des données

Les données sont sauvegardées automatiquement dans deux fichiers JSON créés
au même endroit que `bot.py` :
- `grades.json` : taux horaires par grade
- `services.json` : historique des heures de service par membre

Aucune base de données externe n'est nécessaire. Pense simplement à ne pas
supprimer ces fichiers si tu veux conserver l'historique.

## 🔧 Personnalisation possible

- Ajouter une devise différente (remplacer `€` dans `bot.py`)
- Ajouter un export CSV de la paye
- Ajouter un système de "fiches de paye" mensuelles automatiques
- Restreindre les commandes à un salon spécifique

N'hésite pas à demander si tu veux une de ces améliorations !
