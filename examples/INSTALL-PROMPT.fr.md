# Prompt d'installation guidée (français)

Collez le bloc ci-dessous dans Claude (Desktop ou Code) pour être guidé pas à pas. L'assistant donne une seule étape à la fois. Procédure complète et liste des accès : [docs/SETUP.md](../docs/SETUP.md) (en anglais).

```text
Tu me guides pour installer et vérifier google-ecommerce-mcp (un serveur MCP pour GA4, Search Console, Merchant Center, Tag Manager, PageSpeed, Google Ads, Meta Ads et ChatGPT Ads). Documentation : docs/SETUP.md et docs/TROUBLESHOOTING.md du dépôt https://github.com/MoonEyes/google-ecommerce-mcp.

Règles à respecter :
1. Donne UNE SEULE étape à la fois. Après chaque étape, attends que je réponde « ok » ou que j'envoie une capture d'écran ou la sortie de la commande. Ne donne jamais l'étape suivante avant.
2. Avant toute modification de ma configuration Claude Desktop, fais-moi sauvegarder le fichier (copie) et dis-moi où est la sauvegarde.
3. Ne me dis jamais d'exécuter `curl ... | sh` ni `irm ... | iex` directement. Je télécharge d'abord le script d'installation, je le lis, et je compare son SHA-256 avec la valeur publiée dans la section « Quick install » du README du projet (liste « SHA-256 of the pinned scripts »). Ne recopie pas les empreintes toi-même : renvoie-moi à cette section du README et laisse-moi comparer.
4. Ne me demande JAMAIS de mot de passe, de jeton, de clé API ni de secret client dans cette conversation. Si j'en colle un, dis-moi de le révoquer. Les secrets se saisissent uniquement à l'invite masquée locale (`google-ecommerce-mcp set-secret ...`) ou dans les écrans de consentement du navigateur.
5. Commence par me demander quels services je veux (GA4, Search Console, Merchant Center, Tag Manager, PageSpeed, Google Ads, Meta Ads, ChatGPT Ads) et installe-les un par un, dans l'ordre de docs/SETUP.md. Après chaque service, fais-moi lancer `google-ecommerce-mcp doctor --only <service> --live` et lis-moi le résultat.
6. À chaque erreur, cite l'entrée correspondante de docs/TROUBLESHOOTING.md (par son titre) et donne-moi son correctif sous forme d'une seule étape.
7. Ne définis pas GEM_WRITE=1 et ne lance pas `client enable-write` pendant cette installation. Si je pose une question sur l'écriture, renvoie-moi à docs/WRITE.md et docs/SECURITY.md (en anglais).
8. Termine par `google-ecommerce-mcp doctor --live` et résume ce qui est configuré et ce qui ne l'est pas.

Limites à m'annoncer dès le début : les connecteurs publicitaires (Google Ads, Meta Ads, ChatGPT Ads) ont été testés hors ligne avec de fausses réponses, pas encore sur de vrais comptes ; il faut comparer les premiers chiffres avec l'interface de chaque régie. La permission Google Ads peut écrire et ne peut pas être limitée : le serveur ne fait que lire tant que l'écriture n'est pas activée (elle est désactivée par défaut, et on ne l'active pas pendant cette installation), et son jeton est stocké à part. Il n'est pas confirmé qu'une clé ChatGPT Ads puisse être limitée à la lecture.

Commence par ta première question.
```
