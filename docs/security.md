# Sécurité — durcissement, scans, limites

## Durcissement des conteneurs

Commun à tous les services longue durée (ancre `x-hardening` de `compose.yml`) :
`no-new-privileges`, `cap_drop: [ALL]`, logs bornés (3 × 10 Mo), redémarrage
`unless-stopped`.

| Service | Utilisateur | FS racine | Écritures autorisées | `mem_limit` |
|---|---|---|---|---|
| devdocker-proxy (Caddy) | 1000 | lecture seule | `tmpfs` /data, /config | 128m |
| authentik-server / worker | 1000 | lecture seule | `tmpfs` /tmp, volume /data | 1g |
| authentik-db, rallly-db | 70 (postgres) | lecture seule | `tmpfs` /var/run/postgresql, /tmp ; volume PGDATA | 384m |
| rallly | 100 (nextjs) | lecture seule | `tmpfs` /tmp, ~/.npm, ~/.cache, .next/cache | 768m |
| garage | 1000 | lecture seule | volumes meta/data | 256m |
| mailpit | 1000 | lecture seule | `tmpfs` /tmp | 128m |

Points notables :

- **Aucun root.** Postgres démarre directement en uid 70 : l'entrypoint saute
  alors son `chown` et `gosu`, ce qui permet `cap_drop: ALL`. Le worker
  Authentik n'est *pas* root et n'a *pas* le socket Docker (l'upstream le fait
  pour déployer des outposts ; nous n'utilisons que l'outpost embarqué).
- **Seule exception** : `garage-perms`, un one-shot `alpine` qui fait
  `chown 1000` sur les deux volumes Garage avant le démarrage (l'image Garage
  est `FROM scratch`, sans shell). Il tourne sans réseau (`network_mode: none`),
  avec la seule capacité `CHOWN`, puis s'arrête.
- Le proxy écoute sur :80 en uid 1000 : Docker fixe
  `net.ipv4.ip_unprivileged_port_start=0` dans les conteneurs.

## Surface exposée

- **Local** : seul le port `127.0.0.1:80` est publié (pas `0.0.0.0`).
- **VPS** : aucun port publié par la stack. Le proxy rejoint le réseau `front`
  de l'edge Caddy de la machine, qui termine TLS (Let's Encrypt), ajoute HSTS
  et passe chaque requête par **CrowdSec** (bannissement) et son **WAF AppSec**.
- Les bases, Garage et le SMTP sont sur des réseaux `internal: true` (aucune
  route vers l'extérieur) et ne sont joignables que par leur application.
  Le seul réseau sortant est `egress`, réservé à Rallly (discovery OIDC).
- L'API admin de Garage n'est pas liée (pas de section `[admin]`).
- Mailpit n'a pas d'authentification : il est derrière le forward auth
  d'Authentik, groupe `rallly-admins` uniquement.
- Rallly : connexion par e-mail désactivée (`EMAIL_LOGIN_ENABLED=false`), donc
  aucun chemin d'accès qui contourne Authentik et ses policies.

## Secrets

- Jamais dans une image ni dans le dépôt : `.env` est gitignoré, seul
  `.env.example` (sans valeurs) est commité.
- `scripts/init-env.sh` génère chaque secret aléatoirement (`openssl rand`) et
  écrit `.env` en mode `600`. `garage.toml` ne contient aucun secret (RPC secret
  passé par variable d'environnement).
- CI : la clé de déploiement vit dans l'environnement GitHub `production`
  (restreint à `main`, invisible des PR de forks). Côté VPS elle est déclarée
  `command="…/deploy.sh",restrict` : pas de shell, pas de forwarding, pas de PTY.
- Les actions GitHub tierces sont épinglées par **SHA de commit**, pas par tag
  (un tag peut être déplacé vers du code malveillant, un SHA non).

## Scans

Outil : **Trivy 0.76.2**, sévérités HIGH et CRITICAL, uniquement les
vulnérabilités **avec correctif disponible** (`--ignore-unfixed`). Exécuté dans
la CI (résumé de chaque run) ; relevé ci-dessous du 2026-11-16, plateforme
`linux/amd64`.

| Image | HIGH | CRITICAL | Analyse |
|---|---|---|---|
| `alpine:3.22` (one-shot) | 0 | 0 | — |
| `axllent/mailpit:v1.33.1` | 0 | 0 | — |
| `dxflrs/garage:v2.5.0` | 0 | 0 | — |
| `caddy:2.11-alpine` | 1 | 0 | `golang.org/x/net` compilé dans le binaire Caddy. Corrigé à la prochaine release Caddy ; ce Caddy n'est joignable que via l'edge (VPS) ou `127.0.0.1` (local). |
| `postgres:18-alpine` | 25 | 1 | Tous dans la stdlib Go de `/usr/local/bin/gosu`. Le conteneur démarrant en uid 70, `gosu` n'est jamais exécuté. Non exploitable ici. |
| `ghcr.io/goauthentik/server:2026.8.5` | 63 | 1 | Paquets Debian (openssl, brotli, zlib…) et dépendances Python de l'image upstream. 2026.8.5 est la dernière release : le levier est de suivre les patchs Authentik (`AUTHENTIK_TAG`), pas de reconstruire l'image. |
| `ghcr.io/wiibleyde/rallly` (avant) | 9 | 3 | Dont **2 RCE non authentifiées dans Next.js 16.3.8** (GHSA-2464-fhr4-w9xw, GHSA-673q-mjm6-r5xr). |
| `ghcr.io/wiibleyde/rallly` (après, `sha-9bacfae`) | 8 | 1 | Next.js monté en 16.4.2 sur le fork (commit `⬆️ Bump Next.js to 16.4.2`, même version que l'upstream) : les deux RCE disparaissent. Détail du reste ci-dessous. |

### Restant sur l'image Rallly après correctif

| Paquet | Où | Exposition |
|---|---|---|
| `undici` 6.28.0 (4 HIGH, dont contournement de validation TLS) et `brace-expansion` 5.0.9 (2 HIGH, DoS) | `/usr/local/lib/node_modules/npm` : le **npm** livré avec l'image de base `node:24-slim`, pas une dépendance de Rallly | npm ne sert qu'à `npx prisma migrate deploy` au démarrage, vers la base locale. Levier : suivre `node:24-slim`. |
| `fast-xml-parser` 5.3.4 (1 CRITICAL XSS, 2 HIGH DoS) | dépendance transitive du SDK AWS S3 | Ne parse que les réponses XML de **notre** Garage, sur le réseau interne `rallly` ; aucune entrée utilisateur n'y passe. Levier : montée de `@aws-sdk/*`. |

## Limites assumées

- **Pas de sauvegarde** des volumes (`authentik_db`, `rallly_db`, Garage) : stack
  de démonstration ; `docker compose down -v` remet tout à zéro.
- **Local en HTTP** (`*.localhost`) : pas de TLS sur la machine de dev. Les
  cookies `Secure` ne s'appliquent donc qu'en cloud.
- **MFA non imposée** sur Authentik : `akadmin` et les comptes de démo n'ont
  qu'un mot de passe (généré, 16–24 caractères). L'IdP est public sur la VPS ;
  CrowdSec et le WAF de l'edge sont le contrôle anti-bruteforce.
- **Mailpit en mémoire** : le courrier disparaît au redémarrage.
- **Rallly contacte `api.rallly.co`** (variable `API_BASE_URL` de l'image
  upstream : vérification de mise à jour, avec la version et l'identifiant
  d'instance) via le réseau `egress`.
- **Images tierces non reconstruites** : on choisit la version (tags fixes),
  on ne patche pas les CVE des images upstream.
- La clé de déploiement peut redéployer n'importe quel commit de `main` : la
  sécurité du déploiement repose sur la protection de cette branche.
