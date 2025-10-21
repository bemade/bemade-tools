# BeMade Tools - Studio Management Suite

Collection d'outils Odoo professionnels pour gérer les personnalisations Studio et leur migration vers du code versionné.

## Vue d'ensemble

Cette suite contient deux modules complémentaires :

1. **studio_cleanup** - Utilitaire de nettoyage léger (production-ready)
2. **studio_to_module** - Convertisseur Studio vers module avec backup/rollback (développement)

## Modules

### 1. studio_cleanup (v18.0.1.0.0)

**Type** : Utilitaire Production  
**Dépendances** : `base`

Module léger et réutilisable qui fournit des fonctions pour nettoyer les vues Studio après migration.

#### Caractéristiques
- ✅ Aucune interface utilisateur
- ✅ Sûr pour la production
- ✅ Fonction réutilisable `cleanup_studio_views_by_xmlid()`
- ✅ Logging détaillé avec emojis (✓, ○, ✗)
- ✅ Suppression sécurisée par external ID

#### Usage

```python
from odoo.addons.studio_cleanup.tools import cleanup_studio_views_by_xmlid

def post_init_hook(env):
    studio_view_ids = [
        'studio_customization.odoo_studio_stock_lot_tree_customization',
        'studio_customization.odoo_studio_stock_picking_form_customization',
    ]
    cleanup_studio_views_by_xmlid(env, studio_view_ids, 'my_module')
```

#### Documentation
- [README.md](./studio_cleanup/README.md)

---

### 2. studio_to_module (v18.0.1.1.0) 🆕

**Type** : Outil de Développement  
**Dépendances** : `base`, `studio_cleanup`, `web_studio`

Convertisseur complet avec interface wizard pour transformer les personnalisations Studio en code de module.

#### Nouvelles fonctionnalités v1.1.0 (2025-10-20)

- **🆕 Backup automatique** : Sauvegarde complète avant toute modification
- **🆕 Rollback sur erreur** : Restauration automatique si échec
- **🆕 Organisation par modèle** : Un fichier par modèle avec "studio" dans le nom

#### Caractéristiques principales

**Détection et listage** :
- Liste toutes les vues créées par Studio
- Filtrage et groupement avancés
- Prévisualisation XML

**Conversion intelligente** :
- Génération XML propre et validée
- Fichiers organisés par modèle : `{model}_studio_views.xml`
- Mise à jour automatique du `__manifest__.py`
- Génération de `hooks.py` avec cleanup automatique

**Sécurité** :
- Backup timestampé : `.studio_backups/{timestamp}/`
- Rollback automatique en cas d'erreur
- Validation des modules cibles
- Récupération manuelle possible

#### Workflow complet

```
1. Prototyper dans Studio
   └─> Créer vues, personnalisations

2. Convertir (Studio to Module)
   ├─> 💾 Backup automatique créé
   ├─> Sélectionner vues Studio
   ├─> Choisir module cible
   └─> Générer :
       ├─> views/sale_order_studio_views.xml
       ├─> views/stock_picking_studio_views.xml
       ├─> hooks.py (avec cleanup_studio_views_by_xmlid)
       └─> __manifest__.py (mis à jour)

3. Redémarrer Odoo
   └─> Charger hooks.py

4. Upgrader le module
   ├─> button_immediate_upgrade() appelé
   └─> cleanup_converted_views() déclenché

5. Auto-cleanup (studio_cleanup)
   └─> Suppression des vues Studio originales
```

#### Structure des fichiers générés

**Avant (v1.0.0)** :
```
my_module/
├── views/
│   └── migrated_studio_views.xml  # Toutes les vues mélangées
```

**Après (v1.1.0)** :
```
my_module/
├── .studio_backups/
│   └── 20251020_140530/
│       ├── studio_views_backup.json
│       ├── __manifest__.py
│       └── views/
├── views/
│   ├── sale_order_studio_views.xml
│   ├── stock_picking_studio_views.xml
│   └── product_template_studio_views.xml
└── hooks.py
```

#### Messages de succès

```
Successfully converted 5 Studio view(s) to module my_module.

📁 Created files (3 models):
  • views/sale_order_studio_views.xml
  • views/stock_picking_studio_views.xml
  • views/product_template_studio_views.xml

🔧 Hook: hooks.py (auto-generated)
💾 Backup: /path/to/module/.studio_backups/20251020_140530

⚠️ IMPORTANT: Restart Odoo server before upgrading!

Next steps:
1. Restart Odoo (hooks.py needs to be loaded)
2. Upgrade the module "my_module"
3. Studio views will be automatically deleted
```

#### Documentation complète
- [README.md](./studio_to_module/doc/README.md)
- [CHANGELOG.md](./studio_to_module/doc/CHANGELOG.md)
- [QUICKSTART.md](./studio_to_module/doc/QUICKSTART.md)
- [IMPROVEMENTS_v1.1.0.md](./studio_to_module/doc/IMPROVEMENTS_v1.1.0.md)

---

## Installation

### Développement
```bash
# Installer les deux modules
# 1. studio_cleanup (requis)
# 2. studio_to_module (outil de conversion)
```

### Production
```bash
# Installer seulement studio_cleanup
# Les modules convertis l'utilisent via hooks.py
```

## Architecture

```
┌─────────────────────────────┐
│  studio_to_module           │
│  (Développement)            │
│                             │
│  - Wizard interface         │
│  - Convertit Studio → XML   │
│  - Génère hooks.py          │
│  - Backup/Rollback          │
│  - Dépend de ─────────────────┐
└─────────────────────────────┘ │
                                │
                                ▼
                    ┌─────────────────────────┐
                    │  studio_cleanup         │
                    │  (Production-ready)     │
                    │                         │
                    │  - cleanup_studio_...() │
                    │  - Léger, sans UI       │
                    │  - Logging détaillé     │
                    └─────────────────────────┘
                                │
                                │ Utilisé par
                                ▼
                    ┌─────────────────────────┐
                    │  Module converti        │
                    │  (Votre code)           │
                    │                         │
                    │  - views/*.xml          │
                    │  - hooks.py             │
                    └─────────────────────────┘
```

## Cas d'usage

### Scénario 1 : Nouveau projet

1. Prototyper rapidement avec Studio
2. Convertir avec `studio_to_module`
3. Commiter les fichiers XML générés
4. Déployer en production avec seulement `studio_cleanup`

### Scénario 2 : Migration existante

1. Identifier les personnalisations Studio
2. Convertir par lots avec backup automatique
3. Tester sur environnement de dev
4. Rollback si problème
5. Redéployer une fois validé

### Scénario 3 : Production

1. Modules déjà convertis avec `hooks.py`
2. Seulement `studio_cleanup` installé
3. Hooks exécutés automatiquement à l'upgrade
4. Aucun Studio nécessaire en production

## Sécurité

### Backups (studio_to_module)

- **Emplacement** : `{module}/.studio_backups/{timestamp}/`
- **Contenu** : JSON, manifest, init, hooks, views/
- **Rétention** : Indéfinie (nettoyage manuel)
- **Git** : Exclus via `.gitignore`

### Rollback

Si conversion échoue :
1. ✅ Restauration automatique
2. ✅ Message détaillé avec emplacement backup
3. ✅ Récupération manuelle possible

### Validation

- ✅ Modules cibles: Analyse portable des chemins (fonctionne dans tout setup)
- ✅ Exclusion automatique: odoo/, enterprise/, design-themes/, symlinks
- ✅ Accès réservé aux administrateurs
- ✅ XML validé avant écriture
- ✅ Views Studio préservées jusqu'à upgrade

## Performance

| Opération | Temps | Notes |
|-----------|-------|-------|
| Backup création | < 1s | Pour ~10 vues |
| Rollback | < 1s | Restauration complète |
| Conversion | 1-3s | Dépend du nombre de vues |
| Cleanup | < 1s | Par vue supprimée |

## Compatibilité

- **Odoo** : 18.0+
- **Python** : 3.10+
- **Dépendances** : lxml, json, shutil
- **Modules** : base, web_studio (pour studio_to_module)

## Support

**Équipe** : Durpro Development Team  
**License** : LGPL-3  
**Website** : https://www.durpro.com

## Versions

| Module | Version | Date | Notes |
|--------|---------|------|-------|
| studio_cleanup | 18.0.1.0.0 | 2025-01-16 | Initial release |
| studio_to_module | 18.0.1.1.0 | 2025-10-20 | Backup/Rollback/Model organization |

## Changelog

Voir fichiers individuels :
- [studio_cleanup/README.md](./studio_cleanup/README.md)
- [studio_to_module/doc/CHANGELOG.md](./studio_to_module/doc/CHANGELOG.md)

---

**Made with ❤️ by BeMade**
