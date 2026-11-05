# Agent24 : backend (prototype `proto-v0`)

Socle multi-tenant (PostgreSQL + RLS), webhook Messenger signé, file de traitement, agent Claude
avec outils, envoi des réponses sur Messenger, escalades réelles vers le dirigeant (email), API du
tableau de bord (conversations, réponses humaines, statistiques) et flux temps réel SSE.

## Démarrage

```bash
cp .env.example .env            # puis renseigner META_APP_SECRET, MESSENGER_VERIFY_TOKEN, ENCRYPTION_KEY
docker compose up -d db         # PostgreSQL 16 + rôle agent24_app + base agent24_test
pip install -r requirements.txt
alembic upgrade head            # migrations (rôle propriétaire)
uvicorn app.main:app --reload   # API (rôle applicatif, soumis à la RLS)
python -m app.worker            # worker : fait répondre l'agent (ANTHROPIC_API_KEY requis ; Groq possible avec APP_ENV=development)
pytest                          # sur la base agent24_test uniquement
```

## Migrations

`alembic upgrade head` applique les migrations dans l'ordre, avec le rôle propriétaire :

- `0001` : tenants, channels, channel_routes, contacts, conversations, messages, RLS et droits du rôle applicatif.
- `0002` : jobs (file de traitement), agent_runs, tool_calls, statut de livraison des messages sortants.
- `0003` : users, sessions, escalations, events (+ NOTIFY), auteur des messages, pause de l'agent, jobs d'email.

Ne jamais modifier une migration déjà appliquée : ajouter une nouvelle migration (`0003`, ...).

## Créer un client et tester sans Meta

```bash
PAGE_ACCESS_TOKEN=xxx python -m scripts.create_tenant \
  --name "Vigo Services" --slug vigo-services --sector services --page-id <ID_PAGE_FACEBOOK>

python -m scripts.send_test_webhook --page-id <ID_PAGE_FACEBOOK> --text "Bonjour"
```

## Configurer un client et faire répondre l'agent

```bash
python -m scripts.set_tenant_config --tenant-id <UUID> --file tenants/vigo-services.json
SEND_MODE=console python -m app.worker     # les réponses s'affichent dans le terminal
python -m scripts.send_test_webhook --page-id <ID_PAGE_FACEBOOK> --text "Vous êtes ouverts quand ?"
```

`tenants/vigo-services.json` est un **brouillon** tiré du site public : à faire valider par le dirigeant.
Il liste aussi ce qui n'est PAS renseigné (tarifs, délais, pièces à fournir) : l'agent doit alors
le dire honnêtement et transférer à l'équipe.

## Comptes du tableau de bord

```bash
USER_PASSWORD='...' python -m scripts.create_user --tenant-id <UUID> --email dirigeant@exemple.com \
    --name "Prénom Nom" --role owner        # owner | manager | employee | readonly
```

- Rôles : `readonly` lit ; `employee` répond, prend en charge, rend la main, clôture ; `manager` met l'agent en
  pause ; `owner` idem + reçoit les emails. Seuls `owner` et `manager` (avec `notify_escalations`) reçoivent les emails.
- Connexion par cookie de session (HttpOnly, hash du jeton en base, durée glissante), mot de passe Argon2,
  5 échecs / 15 min bloquent la tentative suivante, contrôle d'`Origin` sur toute requête qui modifie des données.
- **Le client (tenant) vient toujours de la session**, jamais de la requête.

## API du tableau de bord (`/api`)

| Route | Rôle min. | Usage |
|---|---|---|
| `POST /auth/login`, `POST /auth/logout`, `GET /auth/me` | | session |
| `GET /conversations?status=&channel=&q=&before=` | readonly | liste (aperçu, non lus, escalade) |
| `GET /conversations/{id}`, `GET /conversations/{id}/messages?before=` | readonly | détail (+ fenêtre de réponse), historique paginé |
| `POST /conversations/{id}/read` | readonly | marque comme lu |
| `POST /conversations/{id}/take` | employee | prendre en charge (l'agent se tait) |
| `POST /conversations/{id}/messages` | employee | répondre (seulement si prise en charge, fenêtre 24 h ouverte) |
| `POST /conversations/{id}/handback`, `/close` | employee | rendre à l'agent, clôturer |
| `GET /escalations?status=open\|taken\|resolved\|handled\|all` | readonly | « À traiter » / « Prises en charge et traitées » |
| `GET /agent/status`, `POST /agent/pause`, `/resume` | readonly / manager | interrupteur d'urgence |
| `GET /stats/overview`, `GET /stats/activity` | readonly | chiffres du jour, messages par heure (heure locale) |
| `GET /events` (SSE) | readonly | signaux temps réel |

Fuseau des statistiques : `config.timezone` du client, sinon `Africa/Porto-Novo`.

## Temps réel (SSE)

Les événements **signalent** un changement (`message.received`, `message.sent`, `conversation.updated`,
`escalation.created`, `escalation.updated`, `agent.paused`...), ils ne contiennent **jamais** de texte de message :
le tableau de bord recharge ensuite via l'API. Un événement `resync` signifie « recharge tout ».

```js
const es = new EventSource(`${API}/api/events`, { withCredentials: true });   // reprend seul avec Last-Event-ID
es.addEventListener("resync", reloadEverything);
es.addEventListener("message.received", (e) => reloadConversation(JSON.parse(e.data).conversation_id));
```

Mécanique : table `events` + `NOTIFY` PostgreSQL au commit + une écoute `LISTEN` par processus API. Derrière un
proxy : désactiver la mise en tampon (`X-Accel-Buffering: no` est déjà envoyé) et garder un délai d'inactivité > 20 s.

## Escalades

`escalate_to_human` (catégorie, motif, résumé) crée une **escalade** (`open` -> `taken` -> `resolved`), passe la
conversation à l'humain et met un **email** en file (`jobs`). Pas de destinataire = job en échec visible, jamais silencieux.
Si un humain répond **depuis la boîte Messenger de la page**, l'écho Meta est détecté et l'agent se tait
(`handoff_reason = page_inbox`). **À vérifier avec de vrais événements** : ce que Meta met dans `app_id` pour une réponse
faite depuis la boîte de la page ; renseigner `META_APP_ID`.

## Brancher le vrai Messenger

1. Exposer le serveur en HTTPS (tunnel Cloudflare ou ngrok en développement).
2. Dans l'app Meta : produit Messenger, webhook `https://<domaine>/webhooks/messenger`,
   jeton de vérification = `MESSENGER_VERIFY_TOKEN`, abonnement au champ `messages`.
3. Connecter la page de Vigo Services à l'app et générer le jeton de la page.
4. En mode développement, seuls les rôles de l'app (admins, testeurs) peuvent écrire.
   Pour de vrais clients il faut l'accès avancé `pages_messaging` (revue d'app Meta,
   vérification d'entreprise) : à lancer tôt. Vérifier les règles actuelles dans la doc Meta.

## Décisions d'architecture

- **Isolation** : `tenant_id` partout, RLS activée et forcée, clés étrangères composites
  `(tenant_id, id)` (impossible de lier une ligne à un canal d'un autre client).
- **Deux rôles PostgreSQL** : propriétaire (migrations, scripts d'admin) et `agent24_app`
  (ni superuser ni BYPASSRLS, droits minimaux). L'API refuse de démarrer sinon.
- **`tenant_id` vient du serveur** : déduit de l'identifiant de page via `channel_routes`
  (alimentée par trigger), jamais d'un message client ni du modèle.
- **Idempotence** : unicité `(tenant_id, channel_id, idempotency_key)`.
- **Jetons de pages** chiffrés (Fernet). La clé vit hors de la base.
- **Webhook** : signature vérifiée sur le corps brut, 200 si tout est enregistré, 500 sinon
  (Meta renvoie, l'idempotence évite les doublons).

## Flux d'un message (session 2)

1. Webhook -> signature -> message + **job** enregistrés dans la même transaction (outbox).
2. Le worker réclame un job (`FOR UPDATE SKIP LOCKED`) ; **ordre garanti par conversation**.
3. Contexte chargé (historique, configuration du client) -> boucle Claude + outils (limite d'étapes).
4. La réponse est enregistrée *avant* l'envoi ; un échec d'envoi réessaie **sans rappeler Claude**.
5. Échec définitif : conversation passée à un humain + message honnête au client.

Outils : `get_business_info` (lecture) et `escalate_to_human`. Le modèle ne fournit jamais le
`tenant_id` (paramètre inconnu refusé) ; les outils non activés pour le client ne sont ni exposés ni exécutables.
Chaque appel est tracé (`agent_runs`, `tool_calls`).

## Limites connues (à traiter plus tard)

- **L'appel réel à l'API Claude et l'envoi réel vers Meta ne sont pas testés ici** (tests avec faux LLM et faux
  expéditeur) : premier essai à faire avec `SEND_MODE=console`, puis sur une page de test.
- **Email SMTP et détection des échos : testés avec des simulations, pas avec un vrai serveur SMTP ni de vrais échos Meta.**
- Pas encore de validation humaine des actions sensibles (`pending_actions`) ni de relance si une escalade reste sans réponse.
- Limitation de tentatives de connexion en mémoire (un processus) : à externaliser avec plusieurs processus.
- Pendant la pause de l'agent, les messages restent enregistrés mais ne sont pas rattrapés à la reprise.
- Pas d'édition de la configuration (informations officielles) depuis le tableau de bord, ni de gestion des comptes par API.
- Pas de contrôle de sortie sur les prix/dates (inutile tant qu'aucun outil ne produit de chiffres).
- Messages vocaux et images : l'agent demande d'écrire ; la transcription viendra plus tard.

- `messages.raw` contient les événements bruts (données personnelles) : à purger/limiter.
- `media_ref` est une URL temporaire de Meta : il faudra télécharger les médias vite.
- Un contact = une identité sur un canal ; la fusion entre canaux viendra avec WhatsApp.

## Direction : architecture v2

Ce dépôt contient le **prototype** (étiquette git `proto-v0`). La refonte en plateforme développeur est en cours :
API publique versionnée `/v1`, authentification par clés API et jetons utilisateur, espaces Test et Live,
webhooks sortants, usage et console développeurs. Le tableau de bord à cookies (`/api`) et la notion de
secteur sont des éléments de prototype, appelés à disparaître. Voir le document « Architecture v2 ».
