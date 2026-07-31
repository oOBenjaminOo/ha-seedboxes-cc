# Seedboxes.cc integration for Home Assistant

[![GitHub Release](https://img.shields.io/github/v/release/oOBenjaminOo/ha-seedboxes-cc?include_prereleases&style=for-the-badge)](https://github.com/oOBenjaminOo/ha-seedboxes-cc/releases)
[![GitHub Activity](https://img.shields.io/github/commit-activity/y/oOBenjaminOo/ha-seedboxes-cc.svg?style=for-the-badge)](https://github.com/oOBenjaminOo/ha-seedboxes-cc/commits/main)
[![License](https://img.shields.io/github/license/oOBenjaminOo/ha-seedboxes-cc.svg?style=for-the-badge)](LICENSE)
[![HACS Custom](https://img.shields.io/badge/HACS-Custom-41BDF5.svg?style=for-the-badge)](https://www.hacs.xyz/docs/faq/custom_repositories/)

![Seedboxes.cc logo](https://raw.githubusercontent.com/oOBenjaminOo/ha-seedboxes-cc/main/seedbox_logo.png)

A custom Home Assistant integration that retrieves information and statistics
from the current [Seedboxes.cc](https://www.seedboxes.cc/) dashboard.

[Guide en français](#guide-en-français)

> [!IMPORTANT]
> Home Assistant 2026.7.0 or newer is required.

## Entities

The integration provides sensors for:

- status;
- free and used disk space;
- disk usage percentage and capacity;
- monthly traffic allowance;
- server IP address;
- torrent client.

## Installation

### HACS — recommended

This fork currently needs to be added as a custom HACS repository.

1. Open **HACS** in Home Assistant.
2. Open the three-dot menu and select **Custom repositories**.
3. Enter:

   ```text
   https://github.com/oOBenjaminOo/ha-seedboxes-cc
   ```

4. Select **Integration** as the category and click **Add**.
5. Search for **Seedboxes.cc**, open it, and select **Download**.
6. Restart Home Assistant.

See the official
[HACS custom repository instructions](https://www.hacs.xyz/docs/faq/custom_repositories/)
if the menu is not visible.

### Manual installation

1. Download the source archive from the
   [appropriate release](https://github.com/oOBenjaminOo/ha-seedboxes-cc/releases).
2. Copy `custom_components/seedboxes_cc` into the Home Assistant configuration
   directory.
3. Verify that the final path is:

   ```text
   /config/custom_components/seedboxes_cc/manifest.json
   ```

4. Restart Home Assistant.

## Configuration

1. In Home Assistant, open **Settings → Devices & services**.
2. Select **Add integration**.
3. Search for **Seedboxes.cc**.
4. Enter the email address or username and password for the Seedboxes.cc account.

> [!IMPORTANT]
> Automatic username/password sign-in does not support two-factor
> authentication (2FA/MFA). For reliable automatic setup and seedbox discovery,
> 2FA must be disabled on the Seedboxes.cc account. The integration cannot
> request or submit a one-time verification code.
>
> Disabling 2FA reduces account security. If the browser-session fallback is
> offered, a `session_id` obtained after completing 2FA in the browser uses an
> already-authenticated session, but it still has to be copied manually.

The integration first attempts to sign in and discover the seedboxes attached to
the account. If one seedbox is found, it is added automatically. If several are
found, Home Assistant asks which one to add.

No YAML configuration or API key is required.

When automatic sign-in succeeds, the integration discovers the seedbox ID and
obtains its own session cookie automatically; neither value needs to be entered.
If Turnstile blocks automatic sign-in, Home Assistant cannot access cookies
stored in the browser, so the `session_id` value must still be copied manually.
The integration then tries to discover the seedbox ID from that session and asks
for it only if discovery is not possible.

The integration keeps the account credentials and the current session cookie in
the Home Assistant config entry. On a real session expiry, it first tries the
saved cookie, signs in once with the saved credentials, stores the replacement
cookie, and continues updating. It does not retry authentication for network,
server, or data parsing errors. If Turnstile or 2FA blocks the renewal, Home
Assistant requests a fresh browser cookie instead.

Account credentials are stored by Home Assistant and may be included in Home
Assistant backups. Protect access to the Home Assistant instance and its backups.
Entries created by an older release with only a browser cookie become hybrid the
next time Home Assistant requests reauthentication and collects the account
credentials; until then, those older entries still require a manual cookie when
their session expires.

## Browser verification and Turnstile

Seedboxes.cc may protect its sign-in flow with Turnstile. Home Assistant cannot
complete an interactive browser verification. When this happens, the integration
automatically switches to session-cookie authentication.

You will need the **value only** of the `session_id` cookie from an authenticated
browser. In the uncommon case where automatic discovery from that cookie fails,
you will also need the seedbox ID.

### Find the seedbox ID (manual fallback only)

1. Sign in to [Seedboxes.cc](https://www.seedboxes.cc/).
2. Open the seedbox you want to add.
3. Read the number at the end of the page URL.

For example:

```text
https://www.seedboxes.cc/dashboard/seedboxes/12345
```

The seedbox ID is `12345`. It is not the account ID, server name, or username.

### Copy the `session_id` cookie safely

In Chrome or Edge:

1. Keep the authenticated Seedboxes.cc dashboard open.
2. Open Developer Tools with `F12`.
3. Open **Application → Storage → Cookies**. The **Application** tab may be
   hidden under the `»` menu.
4. Select `https://www.seedboxes.cc`.
5. Find the cookie named `session_id`.
6. Copy only its **Value** column.

In Firefox, use **Storage → Cookies → https://www.seedboxes.cc**.

Paste the raw value into Home Assistant. Do not include `session_id=`, quotation
marks, a semicolon, the complete Cookie header, or analytics cookies such as
`_ga`.

Closing Developer Tools or the browser tab is fine. Do not sign out of
Seedboxes.cc after copying the cookie, because signing out can invalidate the
session used by Home Assistant.

> [!WARNING]
> `session_id` is a bearer credential that can provide access to the
> authenticated Seedboxes.cc session. Treat it like a password.
>
> Never post it in a GitHub issue, chat, screenshot, log, or configuration
> example. If it is exposed, sign out of Seedboxes.cc to revoke it, sign in
> again, and replace the cookie in Home Assistant.

Session cookies expire. The integration renews them automatically when ordinary
username/password sign-in remains available. When Home Assistant requests
reauthentication, automatic renewal was blocked or rejected: sign in again
through the browser and repeat the steps above to provide a new `session_id`
value.

## Updating

Install the update from **Settings → Updates**, or open HACS and select
**Redownload** for the repository. Restart Home Assistant after updating the
integration. See the official
[HACS update instructions](https://www.hacs.xyz/docs/use/update/).

## Troubleshooting

### “The session cookie is invalid, expired, or cannot access this seedbox”

Check that:

- the cookie came from `www.seedboxes.cc`;
- only its value was copied, without `session_id=`;
- the seedbox ID matches the number in the dashboard URL;
- the signed-in account can access that seedbox;
- the browser session has not expired or been signed out.

### Automatic login returns browser verification

This is the expected fallback when Seedboxes.cc presents Turnstile. Complete the
login in a normal browser and use the session-cookie procedure above.

### The integration is not visible after installation

Restart Home Assistant and verify that this file exists:

```text
/config/custom_components/seedboxes_cc/manifest.json
```

### Login diagnostic logs

When the Keycloak flow fails, the integration logs only structural diagnostics:
HTTP status, final URL without query or fragment, content type, page title, form
actions, field names/types, and keyword indicators. It does not log field values,
cookies, usernames, or passwords. Review any log before sharing it publicly.

## Guide en français

### Installation avec HACS

1. Ouvrez **HACS** dans Home Assistant.
2. Dans le menu à trois points, choisissez **Dépôts personnalisés**.
3. Ajoutez :

   ```text
   https://github.com/oOBenjaminOo/ha-seedboxes-cc
   ```

4. Choisissez la catégorie **Intégration**, puis téléchargez
   **Seedboxes.cc**.
5. Redémarrez Home Assistant.
6. Ouvrez **Paramètres → Appareils et services → Ajouter une intégration**,
   puis recherchez **Seedboxes.cc**.

### Connexion

Saisissez d’abord l’adresse e-mail ou le nom d’utilisateur et le mot de passe du
compte Seedboxes.cc. L’intégration découvre automatiquement les seedboxes du
compte.

> [!IMPORTANT]
> La connexion automatique par identifiant et mot de passe ne prend pas en
> charge la double authentification (2FA/MFA). Pour une configuration et une
> découverte automatiques fiables, la 2FA doit être désactivée sur le compte
> Seedboxes.cc. L’intégration ne peut ni demander ni envoyer un code de
> vérification à usage unique.
>
> Désactiver la 2FA réduit la sécurité du compte. Si le repli par session
> navigateur est proposé, un `session_id` récupéré après avoir validé la 2FA
> dans le navigateur correspond à une session déjà authentifiée, mais sa copie
> reste manuelle.

Lorsque la connexion automatique réussit, l’intégration découvre elle-même
l’identifiant de la seedbox et obtient son propre cookie de session : vous
n’avez à fournir aucune de ces deux valeurs. Si Turnstile bloque cette connexion,
Home Assistant ne peut pas lire les cookies enregistrés dans votre navigateur ;
la valeur de `session_id` doit donc toujours être copiée manuellement.
L’intégration tente ensuite de découvrir l’identifiant de la seedbox depuis cette
session et ne le demande que si cette découverte échoue.

L’intégration conserve les identifiants du compte et le cookie de session actuel
dans l’entrée de configuration Home Assistant. Lorsqu’une session expire
réellement, elle essaie d’abord le cookie enregistré, effectue une seule connexion
avec les identifiants, enregistre le nouveau cookie et reprend les mises à jour.
Elle ne relance pas l’authentification pour une panne réseau, une erreur serveur
ou une erreur de lecture des données. Si Turnstile ou la 2FA bloque le
renouvellement, Home Assistant demande alors un nouveau cookie du navigateur.

Les identifiants du compte sont enregistrés par Home Assistant et peuvent être
inclus dans ses sauvegardes. Protégez l’accès à Home Assistant et à ses
sauvegardes.
Les entrées créées par une ancienne version avec uniquement un cookie navigateur
deviennent hybrides lors de la prochaine réauthentification, lorsque Home
Assistant recueille les identifiants du compte. D’ici là, ces anciennes entrées
demandent encore un cookie manuel à l’expiration de leur session.

### Récupérer l’identifiant de la seedbox (repli manuel uniquement)

Connectez-vous à Seedboxes.cc et ouvrez la seedbox concernée. L’identifiant est
le nombre situé à la fin de l’URL :

```text
https://www.seedboxes.cc/dashboard/seedboxes/12345
```

Dans cet exemple, l’identifiant est `12345`.

### Récupérer le cookie de session

Dans Chrome ou Edge :

1. Gardez le tableau de bord Seedboxes.cc connecté ouvert.
2. Ouvrez les outils de développement avec `F12`.
3. Ouvrez **Application → Stockage → Cookies**. L’onglet **Application** peut
   être rangé dans le menu `»`.
4. Sélectionnez `https://www.seedboxes.cc`.
5. Repérez `session_id`.
6. Copiez uniquement sa colonne **Value/Valeur**.

Dans Firefox, utilisez
**Stockage → Cookies → https://www.seedboxes.cc**.

Collez uniquement la valeur brute dans Home Assistant. Ne collez jamais
`session_id=`, des guillemets, un point-virgule, l’en-tête Cookie complet ou
les cookies analytiques comme `_ga`.

Vous pouvez fermer les outils de développement ou l’onglet. Ne vous déconnectez
pas de Seedboxes.cc après la copie : la déconnexion peut invalider la session
utilisée par Home Assistant.

> [!WARNING]
> La valeur de `session_id` est aussi sensible qu’un mot de passe. Ne la
> publiez jamais dans une issue, une discussion, une capture d’écran ou un
> journal. En cas d’exposition, déconnectez-vous de Seedboxes.cc pour la
> révoquer, reconnectez-vous et fournissez le nouveau cookie à Home Assistant.

Le cookie finit par expirer. L’intégration le renouvelle automatiquement tant
que la connexion habituelle par identifiant et mot de passe reste possible. Si
Home Assistant demande une réauthentification, ce renouvellement a été bloqué ou
refusé : récupérez une nouvelle valeur en répétant cette procédure.

## Support

Report integration problems through the
[GitHub issue tracker](https://github.com/oOBenjaminOo/ha-seedboxes-cc/issues).
Never include passwords or session cookies in an issue.

Based on the original integration by
[@swartjean](https://github.com/swartjean), maintained in this repository by
[@oOBenjaminOo](https://github.com/oOBenjaminOo).
