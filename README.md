# Alexa-YT

Écouter ses playlists YouTube sur un Echo Dot, à la voix, sans smartphone :

> « Alexa, demande à mes playlists de jouer Musique du matin »

Le projet se compose de deux parties :

- une **skill Alexa privée** (mode développeur, jamais publiée), en français ;
- un **petit serveur Python** qui tourne sur un NAS Synology (testé pour un DS218play, processeur ARM, sans Docker). Il répond à Alexa, lit la liste de tes playlists via l'API YouTube (connexion Google) et transmet l'audio des vidéos à l'Echo grâce à [yt-dlp](https://github.com/yt-dlp/yt-dlp).

```
Echo Dot ──► Alexa (cloud Amazon) ──► https://chockr.synology.me/alexa ──► serveur Python (NAS)
Echo Dot ◄────────────── audio ◄──── https://chockr.synology.me/stream/… ◄── yt-dlp ◄── YouTube
```

> ⚠️ Extraire l'audio de YouTube est contraire à ses conditions d'utilisation, et YouTube modifie
> régulièrement son site : yt-dlp doit être mis à jour souvent (c'est automatisé plus bas).
> Usage strictement personnel.

## Commandes vocales

| Tu dis | Effet |
| --- | --- |
| « Alexa, ouvre mes playlists » | Alexa te demande laquelle jouer |
| « Alexa, demande à mes playlists de jouer *nom* » | Lance la playlist (le nom peut être approximatif) |
| « Alexa, demande à mes playlists quelles sont mes playlists » | Énumère tes playlists |
| « Alexa, pause » / « Alexa, reprends » | Pause et reprise à l'endroit exact |
| « Alexa, suivant » / « Alexa, précédent » | Change de titre |
| « Alexa, active la lecture aléatoire » / « désactive… » | Mélange le reste de la playlist |
| « Alexa, répète » / « Alexa, active la répétition » | Recommence le titre / boucle sur la playlist |
| « Alexa, stop » | Arrête |

Sans le nom de la skill (« Alexa, joue trap nation »), Alexa envoie la demande à Amazon Music :
une skill personnelle ne peut pas remplacer le service musical par défaut. Une fois la lecture
lancée, « suivant », « pause », « reprends »… vont en revanche bien à la skill.

**Changer le nom d'appel** (par ex. « mon youtube ») : *Build › Invocations › Skill Invocation
Name*, puis **Save** et **Build skill** (rien à changer sur le NAS). Règles d'Amazon : au moins
2 mots, en minuscules, sans chiffres, sans « alexa », « echo », « amazon », « skill », « app »,
et sans mots d'appel comme « ouvre », « lance », « demande », « joue », « de », « à ». Pense aussi
à modifier `invocationName` dans le fichier JSON si tu le recolles plus tard.

**Raccourci par routine** (application Alexa › *Plus › Routines › +*) : *Quand* → *Voix* →
« youtube », *Action* → *Personnalisé* → « ouvre mes playlists ». « Alexa, youtube » ouvre alors la
skill, qui demande quelle playlist jouer. Une routine ne peut pas contenir de mot variable : pour
une playlist précise en une phrase, fais une routine par playlist (« youtube nas » →
« demande à mes playlists de jouer NAS »).

## Installation

Prérequis déjà en place : `chockr.synology.me` joignable en HTTPS avec un certificat Let's Encrypt,
ports 80 et 443 redirigés de la Freebox vers le NAS, SSH activé sur le NAS.

### 1. Projet Google Cloud (accès à tes playlists, y compris privées)

1. Va sur <https://console.cloud.google.com/>, crée un projet nommé `Alexa-YT`.
2. *APIs & Services › Library* : cherche **YouTube Data API v3** et clique **Enable**.
3. *Google Auth Platform* (ou *OAuth consent screen*) › **Get started** :
   - App name : `Alexa-YT`, support email : ton adresse ;
   - Audience : **External** ;
   - Contact : ton adresse, accepte les conditions, **Create**.
4. *Audience* : clique **Publish app** puis confirme (passage en *In production*).
   Sans cela, Google déconnecte le serveur tous les 7 jours. L'application ne sera pas
   « vérifiée » : c'est normal pour un usage personnel, Google affichera juste un avertissement
   lors de la connexion.
5. *Clients* › **Create client** › Application type **TVs and Limited Input devices**,
   nom `NAS`, **Create**. Note le **Client ID** et le **Client secret**.

### 2. Skill Alexa

Dans <https://developer.amazon.com/alexa/console/ask> (avec le même compte Amazon que l'Echo) :

1. **Create Skill** : nom `Mes Playlists`, langue **French (FR)**, type **Other › Custom**,
   hébergement **Provision your own**, modèle **Start from Scratch**.
2. *Build › Interaction Model › JSON Editor* : colle le contenu de
   [`skill/interaction-model-fr-FR.json`](skill/interaction-model-fr-FR.json), **Save** puis **Build skill**.
3. *Build › Interfaces* : active **Audio Player**, **Save**.
4. *Build › Endpoint* : **HTTPS**, Default Region `https://chockr.synology.me/alexa`, et choisis
   *« I will upload a self-signed certificate in X.509 format »* avec le certificat du NAS
   (voir [Certificat](#certificat--à-refaire-à-chaque-renouvellement) ci-dessous). **Save Endpoints**.

   > Pourquoi pas l'option « trusted certificate authority » ? Amazon refuse la chaîne de
   > certificats Let's Encrypt actuelle (`YE1 › Root YE › ISRG Root X2 › ISRG Root X1`) : la
   > connexion échoue avant même que la requête n'arrive au NAS, et la console affiche
   > `INVALID_RESPONSE`. En lui donnant directement le certificat du NAS, Amazon ne vérifie plus
   > la chaîne.
5. Copie le **Skill ID** (`amzn1.ask.skill.…`) : dans la liste de tes skills, lien **Copy Skill ID** sous le nom de la skill.
6. Onglet **Test** : passe *Skill testing is enabled in* sur **Development**.
   La skill est alors disponible sur tous les Echo de ton compte.

### 3. Copier le projet sur le NAS

1. Sur GitHub, *Code › Download ZIP* et décompresse-le sur ton ordinateur.
2. Dans **File Station**, ouvre le dossier **home**, crée un dossier `alexa-yt` et envoie-y
   le contenu du ZIP (les dossiers `server`, `scripts`, `skill`…).

   Pas de dossier **home** ? Active-le dans *Panneau de configuration › Utilisateur et groupe ›
   Avancé › Activer le service d'accueil de l'utilisateur*.

### 4. Installer (en SSH)

Depuis ton ordinateur (Terminal sur Mac, PowerShell sur Windows), en remplaçant `utilisateur` par ton identifiant DSM et l'IP par celle du NAS :

```sh
ssh utilisateur@192.168.1.50
cd ~/alexa-yt
sh scripts/install.sh
```

Le script télécharge yt-dlp et Deno (moteur JavaScript dont yt-dlp a besoin pour YouTube),
puis crée `server/config.json`. Si `python3` est introuvable, installe **Python 3** depuis le
Centre de paquets.

Édite ensuite la configuration :

```sh
vi server/config.json
```

(dans vi : `i` pour écrire, collage avec Cmd+V ou clic droit, puis `Échap` et `:wq` pour
enregistrer ; ou installe le paquet **Éditeur de texte** et ouvre le fichier depuis File Station)
et renseigne `skill_id` (étape 2.5), `google_client_id` et `google_client_secret` (étape 1.5).

### 5. Connecter ton compte YouTube

```sh
cd ~/alexa-yt/server
python3 alexa_yt.py --login
```

Ouvre l'adresse affichée (google.com/device) sur ton téléphone ou ton ordinateur, saisis le code,
choisis ton compte YouTube et accepte (« Paramètres avancés › Accéder à Alexa-YT » si Google
affiche l'avertissement d'application non vérifiée). La commande affiche ensuite la liste de tes
playlists. C'est à faire une seule fois.

### 6. Proxy inversé dans DSM

*Panneau de configuration › Portail de connexion › Avancé › Proxy inversé › Créer* :

| | Protocole | Nom d'hôte | Port |
| --- | --- | --- | --- |
| Source | HTTPS | `chockr.synology.me` | 443 |
| Destination | HTTP | `localhost` | 8765 |

DSM reste accessible comme avant sur le port 5001. Un « portail web » Web Station sur le même nom
d'hôte, avec un proxy vers `localhost:8765`, fonctionne aussi.

### 7. Démarrage automatique et mises à jour

*Panneau de configuration › Planificateur de tâches* :

Remplace `utilisateur` par ton identifiant DSM dans les chemins ci-dessous.

- **Créer › Tâche déclenchée › Script défini par l'utilisateur** : utilisateur : ton compte DSM,
  événement **Démarrage**, script :
  `sh /var/services/homes/utilisateur/alexa-yt/scripts/start.sh`
- **Créer › Tâche planifiée › Script défini par l'utilisateur** : utilisateur : ton compte DSM,
  tous les jours à 5 h, script :
  `sh /var/services/homes/utilisateur/alexa-yt/scripts/update.sh` (met à jour yt-dlp et redémarre)

Pour arrêter le serveur : `sh ~/alexa-yt/scripts/stop.sh` (et `start.sh` pour le relancer).
Il ne faut le relancer qu'après avoir modifié un fichier **sur le NAS** (`config.json`, le code,
les scripts) ; un *Build* dans la console Alexa ne le concerne pas.

Lance la première tâche une fois à la main (clic droit › Exécuter), puis vérifie :

```sh
tail -f ~/alexa-yt/alexa-yt.log
```

Tu dois voir `Alexa-YT écoute sur 127.0.0.1:8765` puis `compte Google : N playlists`.

### 8. Tester

« Alexa, ouvre mes playlists ». Si quelque chose ne va pas, le journal `alexa-yt.log` et
l'onglet *Test* de la console Alexa (qui montre les requêtes JSON) sont les deux premiers endroits
où regarder.

### Certificat : à refaire à chaque renouvellement

DSM renouvelle le certificat Let's Encrypt automatiquement (tous les 2 à 3 mois). Le certificat
présenté à Amazon change alors, et la skill ne répond plus (`INVALID_RESPONSE`) tant qu'on ne lui
a pas renvoyé le nouveau. L'envoi à Amazon reste manuel (la console n'offre pas de moyen de
l'automatiser simplement), mais `scripts/check-cert.sh` te prévient quand il faut le faire.

**Première fois**, en SSH (la skill fonctionne, donc le certificat actuel est le bon) :

```sh
sh ~/alexa-yt/scripts/check-cert.sh
```

Il enregistre le certificat actuel comme « celui d'Amazon » dans `~/alexa-yt/cert/`.

**Tâche planifiée** (vérification quotidienne) : *Panneau de configuration › Planificateur de
tâches › Créer › Tâche planifiée › Script défini par l'utilisateur* :

- *Général* : nom `Alexa-YT certificat`, utilisateur **root** (nécessaire pour la notification DSM) ;
- *Programmer* : tous les jours, par exemple à 9 h ;
- *Paramètres de tâche* : coche *Envoyer les détails d'exécution par e-mail* et *uniquement si le
  script se termine anormalement* (si les e-mails sont configurés dans *Notification*), et dans
  *Script défini par l'utilisateur* (remplace `utilisateur` par ton identifiant DSM) :

  ```sh
  sh /var/services/homes/utilisateur/alexa-yt/scripts/check-cert.sh
  ```

Le script compare le certificat servi par le NAS à celui envoyé à Amazon. S'ils diffèrent, il
affiche une notification DSM (et un e-mail si coché) et se termine en erreur. Il prévient aussi
si le certificat expire dans moins de 14 jours sans avoir été renouvelé.

**Quand l'alerte arrive** :

1. Télécharge `cert/actuel.pem` depuis File Station (*home › alexa-yt › cert*, clic droit ›
   Télécharger).
2. Console Alexa : *Build › Endpoint* › envoie le fichier › **Save Endpoints** (pas besoin de Build).
3. En SSH : `sh ~/alexa-yt/scripts/check-cert.sh --envoye` pour noter que c'est fait.

Pour tester la tâche tout de suite : sélectionne-la dans le Planificateur › **Exécuter**, puis
*Action › Afficher le résultat*.

### Mettre à jour le code sur le NAS

Après une modification sur GitHub : ouvre le fichier concerné, clique sur **Download raw file**,
envoie-le au même endroit dans File Station (en remplaçant l'ancien), puis
`sh ~/alexa-yt/scripts/start.sh`.

## Configuration (`server/config.json`)

| Clé | Rôle |
| --- | --- |
| `skill_id` | ID de la skill : les requêtes d'autres skills sont refusées |
| `public_base_url` | Adresse HTTPS publique du NAS |
| `secret` | Clé aléatoire qui signe les liens audio (générée par `install.sh`) |
| `google_client_id`, `google_client_secret` | Client OAuth Google (étape 1) |
| `shuffle_by_default` | `true` pour toujours démarrer en aléatoire |
| `playlists` | Playlists en plus de celles de ton compte, par ex. celles d'autres chaînes : `[{"name": "Jazz", "id": "PL…", "shuffle": true}]` (`id` accepte aussi l'URL complète) |
| `audio_format` | Format yt-dlp ; par défaut AAC/M4A, lu nativement par l'Echo |
| `warm_first_tracks` | `true` (par défaut) : prépare à l'avance le 1er titre de chaque playlist non aléatoire, pour un lancement immédiat |

## Limites connues

- Les vidéos privées ou supprimées d'une playlist sont ignorées ; les vidéos réservées aux membres
  ou avec restriction d'âge ne se lancent pas.
- Sur un DS218play, yt-dlp met **environ 30 s** à préparer un titre (un seul à la fois). Le 1er
  titre de chaque playlist est préparé à l'avance et le suivant pendant la lecture du précédent ;
  la lecture aléatoire ou plusieurs « suivant » d'affilée peuvent donc encore faire attendre.
- Tes propres vidéos privées ne sont pas lisibles : passe-les en « non répertoriée ».
- La playlist « J'aime » n'est pas exposée par l'API YouTube.

## Dépannage

| Symptôme | Cause et solution |
| --- | --- |
| Alexa : « je ne parviens pas à accéder à la skill », console : `INVALID_RESPONSE`, rien dans `alexa-yt.log` | Certificat : renvoie-le à Amazon ([voir plus haut](#certificat--à-refaire-à-chaque-renouvellement)) |
| `requête refusée : skill inconnue` | `skill_id` de `config.json` différent de celui de la console |
| `requête refusée : requête trop ancienne` | Horloge du NAS décalée : active la synchronisation NTP |
| `AMAZON.FallbackIntent` dans le journal | Phrase non reconnue : ajoute-la aux `samples` du modèle d'interaction |
| `libz.so.1: failed to map segment` | yt-dlp lancé sans `TMPDIR` (`/tmp` est en noexec sur DSM) : passe par `scripts/start.sh` |
| `MEDIA_ERROR_SERVICE_UNAVAILABLE` | Titre pas prêt à temps (voir Limites) ou vidéo illisible (privée, membres…) |
| Alexa lance Amazon Music | La phrase ne contient pas le nom de la skill |

Pour voir ce qui se passe : `tail -f ~/alexa-yt/alexa-yt.log`, et dans la console Alexa,
onglet *Test* avec **Device Log** coché.

## Développement

Aucune dépendance en dehors de la bibliothèque standard Python (3.8 ou plus) :

```sh
cd server && python3 -m unittest -v test_alexa_yt
```
