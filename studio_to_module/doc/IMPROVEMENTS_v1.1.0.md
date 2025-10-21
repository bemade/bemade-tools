# Améliorations Version 18.0.1.1.0

Date: 2025-10-20

## Vue d'ensemble

Cette version introduit trois améliorations majeures demandées :

1. **Système de backup automatique**
2. **Rollback automatique en cas d'erreur**
5. **Organisation des fichiers par modèle avec identificateur "studio"**

---

## 1. Système de Backup Automatique 💾

### Fonctionnement

Avant chaque conversion, un backup complet est créé automatiquement :

**Emplacement** : `{module_path}/.studio_backups/{timestamp}/`

**Exemple** : `/path/to/my_module/.studio_backups/20251020_140530/`

### Contenu du backup

```
.studio_backups/20251020_140530/
├── studio_views_backup.json    # Export JSON des données de vues Studio
├── __manifest__.py             # Copie du manifest avant modification
├── __init__.py                 # Copie de __init__.py si modifié
├── hooks.py                    # Copie de hooks.py si existant
└── views/                      # Copie complète du dossier views/
    ├── existing_view_1.xml
    └── existing_view_2.xml
```

### Données exportées (JSON)

Le fichier `studio_views_backup.json` contient pour chaque vue :
- `id` : ID interne de la vue
- `name` : Nom de la vue
- `xml_id` : External ID
- `model` : Modèle Odoo
- `type` : Type de vue (form, tree, etc.)
- `arch` : Architecture XML complète
- `inherit_id` : ID de la vue héritée (si applicable)
- `priority` : Priorité de la vue
- `mode` : Mode d'héritage

### Rétention

- Les backups sont **conservés indéfiniment**
- Permettent une **récupération manuelle** si nécessaire
- Exclus du contrôle de version (via `.gitignore`)

### Code

```python
def _create_backup(self, module_path):
    """Create a backup of views before conversion."""
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_dir = os.path.join(module_path, '.studio_backups', timestamp)
    
    # Crée le dossier de backup
    os.makedirs(backup_dir, exist_ok=True)
    
    # Export JSON des vues Studio
    # Backup des fichiers du module
    # Backup du dossier views/
    
    return backup_dir
```

---

## 2. Rollback Automatique en Cas d'Erreur ⚠️

### Fonctionnement

Si la conversion échoue à n'importe quelle étape, le système :

1. **Détecte l'erreur** automatiquement
2. **Restaure tous les fichiers** depuis le backup
3. **Informe l'utilisateur** avec message détaillé
4. **Fournit l'emplacement du backup** pour récupération manuelle si nécessaire

### Processus

```python
try:
    # Créer backup
    backup_dir = self._create_backup(module_path)
    
    # Effectuer la conversion
    # - Créer fichiers XML par modèle
    # - Mettre à jour manifest
    # - Créer/mettre à jour hooks.py
    
    # Marquer vues comme converties
    
except Exception as e:
    # ROLLBACK automatique
    if backup_dir:
        self._rollback_from_backup(backup_dir, module_path)
        raise UserError(
            f'Conversion failed: {e}\n\n'
            f'✓ Changes have been rolled back from backup.\n'
            f'Backup location: {backup_dir}'
        )
```

### Messages utilisateur

**En cas de succès de rollback** :
```
Conversion failed: [erreur détaillée]

✓ Changes have been rolled back from backup.
Backup location: /path/to/module/.studio_backups/20251020_140530
```

**En cas d'échec de rollback** (rare) :
```
Conversion failed: [erreur détaillée]

✗ Rollback also failed: [erreur rollback]
Manual recovery needed from backup: /path/to/module/.studio_backups/20251020_140530
```

### Restauration manuelle

Si le rollback automatique échoue, récupération manuelle possible :

```bash
cd /path/to/module
cp .studio_backups/20251020_140530/__manifest__.py .
cp .studio_backups/20251020_140530/__init__.py .
cp .studio_backups/20251020_140530/hooks.py .
rm -rf views/
cp -r .studio_backups/20251020_140530/views/ .
```

---

## 5. Organisation par Modèle avec "studio" ⭐

### Ancien comportement (v1.0.0)

Un seul fichier pour toutes les vues :
```
views/
└── migrated_studio_views.xml  # Toutes les vues mélangées
```

### Nouveau comportement (v1.1.0)

Un fichier par modèle avec identificateur "studio" :
```
views/
├── sale_order_studio_views.xml        # Vues du modèle sale.order
├── stock_picking_studio_views.xml     # Vues du modèle stock.picking
├── product_template_studio_views.xml  # Vues du modèle product.template
└── account_move_studio_views.xml      # Vues du modèle account.move
```

### Avantages

1. **Meilleure organisation** : Facile de trouver les vues d'un modèle spécifique
2. **Identificateur clair** : Le mot "studio" indique l'origine des vues
3. **Version control amélioré** : Diffs Git plus propres et ciblés
4. **Maintenance facilitée** : Édition et suppression plus simples
5. **Évite les conflits** : Moins de risques de conflits Git

### Nomenclature

**Pattern** : `{model_name}_studio_views.xml`

**Exemples** :
- `sale.order` → `sale_order_studio_views.xml`
- `stock.picking` → `stock_picking_studio_views.xml`
- `res.partner` → `res_partner_studio_views.xml`
- `account.move.line` → `account_move_line_studio_views.xml`

### Groupement automatique

Le code groupe automatiquement les vues par modèle :

```python
# Grouper les vues par modèle
views_by_model = {}
for view in self.studio_view_ids:
    model = view.model
    if model not in views_by_model:
        views_by_model[model] = self.env['ir.ui.view']
    views_by_model[model] |= view

# Créer un fichier par modèle
for model, views in views_by_model.items():
    model_safe = model.replace('.', '_')
    file_name = f'{model_safe}_studio_views.xml'
    self._create_xml_file(file_path, views)
```

### Manifest mis à jour automatiquement

Le `__manifest__.py` est mis à jour avec tous les fichiers :

```python
'data': [
    'views/sale_order_studio_views.xml',
    'views/stock_picking_studio_views.xml',
    'views/product_template_studio_views.xml',
],
```

---

## Messages de Succès Améliorés

### Ancien message (v1.0.0)

```
Successfully converted 5 Studio view(s) to module my_module.

File: views/migrated_studio_views.xml

Next steps:
1. Restart Odoo (hooks.py needs to be loaded)
2. Upgrade the module "my_module"
```

### Nouveau message (v1.1.0)

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

### Icônes utilisées

- 📁 : Fichiers créés
- 🔧 : Hooks générés
- 💾 : Backup créé
- ⚠️ : Avertissement important
- ✓ : Succès de rollback
- ✗ : Échec de rollback

---

## Impact sur les Modules Existants

### Migration depuis v1.0.0

Si vous aviez déjà `migrated_studio_views.xml` :

1. **Les nouvelles conversions** utiliseront le nouveau format
2. **Les fichiers existants** restent inchangés
3. **Pas de migration automatique** des anciens fichiers
4. **Recommandé** : Supprimer manuellement l'ancien fichier après test

### Compatibilité

- ✅ Compatible avec `studio_cleanup` v1.0.0
- ✅ Fonctionne avec Odoo 18.0
- ✅ Pas de changement dans le `hooks.py` généré
- ✅ Pas de changement dans le processus de cleanup

---

## Configuration Git

Le fichier `.gitignore` a été mis à jour pour exclure les backups :

```gitignore
# Backup files created during conversion
.studio_backups/
```

**Recommandation** : Committez cette modification dans vos modules.

---

## Tests

Pour tester les nouvelles fonctionnalités :

### Test 1 : Backup automatique

1. Convertir des vues Studio
2. Vérifier que `.studio_backups/{timestamp}/` existe
3. Vérifier le contenu du backup

### Test 2 : Rollback sur erreur

1. Modifier temporairement le code pour forcer une erreur
2. Tenter une conversion
3. Vérifier que les fichiers sont restaurés
4. Vérifier le message d'erreur avec emplacement du backup

### Test 3 : Fichiers par modèle

1. Sélectionner des vues de plusieurs modèles différents
2. Convertir
3. Vérifier qu'un fichier par modèle est créé
4. Vérifier le nom des fichiers (avec "studio")
5. Vérifier que le manifest contient tous les fichiers

---

## Dépendances Techniques

### Nouveaux imports

```python
import json      # Pour export JSON des vues
import shutil    # Pour copie/suppression de fichiers
from datetime import datetime  # Pour timestamp des backups
```

### Nouvelles méthodes

| Méthode | Description |
|---------|-------------|
| `_create_backup(module_path)` | Crée un backup complet |
| `_rollback_from_backup(backup_dir, module_path)` | Restaure depuis backup |

### Nouveau champ

```python
backup_path = fields.Char(
    string='Backup Path',
    readonly=True,
    help="Path where backup is stored",
)
```

---

## Performance

### Impact

- **Création backup** : < 1 seconde pour 10 vues
- **Rollback** : < 1 seconde
- **Espace disque** : ~10-50 KB par backup (selon taille des vues)

### Optimisation possible (futures versions)

- Compression des backups (`.tar.gz`)
- Nettoyage automatique des vieux backups (politique de rétention)
- Backup incrémental (seulement les changements)

---

## Documentation Mise à Jour

Les fichiers suivants ont été mis à jour :

- ✅ `doc/README.md` - Nouvelles features
- ✅ `doc/CHANGELOG.md` - Version 18.0.1.1.0
- ✅ `__manifest__.py` - Version et summary
- ✅ `.gitignore` - Exclusion des backups
- ✅ `doc/IMPROVEMENTS_v1.1.0.md` - Ce document

---

## Support

Pour questions ou problèmes :

1. Vérifier la documentation mise à jour
2. Consulter les logs Odoo pour messages détaillés
3. Vérifier l'emplacement du backup en cas d'erreur
4. Contacter l'équipe de développement Durpro

---

## Prochaines Étapes

Fonctionnalités planifiées pour v18.0.1.2.0 :

- [ ] Validation XML avec schéma XSD
- [ ] Support conversion des actions Studio
- [ ] Dry-run mode (prévisualisation sans écriture)
- [ ] Politique de rétention des backups
- [ ] Interface de gestion des backups

---

**Version** : 18.0.1.1.0  
**Date** : 2025-10-20  
**Auteur** : Durpro Development Team
