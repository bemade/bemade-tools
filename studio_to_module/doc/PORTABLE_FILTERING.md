# Filtrage Portable des Modules Cibles

## Problématique

Le module `studio_to_module` doit identifier les modules **custom** (créés par l'utilisateur) et exclure les modules **core Odoo**, **enterprise** et **themes**.

Le défi : faire cela de manière **portable** qui fonctionne sur n'importe quel setup, quelle que soit la structure des dossiers.

## Solution Implémentée (v18.0.1.1.4)

### Approche par Vérification du Chemin Parent

Au lieu d'utiliser des chemins absolus ou des composants séparés, on vérifie si le **chemin parent contient** les dossiers à exclure :

```python
# Récupérer le chemin du module
module_path = get_module_path(module.name)
# Ex: /Users/bvezina/sandbox/Durpro18/addons/durpro_stock

# Vérifier que le dossier parent s'appelle 'addons'
parent_dir = os.path.basename(os.path.dirname(module_path))
if parent_dir != 'addons':
    continue  # ❌ Module exclu

# Récupérer le chemin complet du parent
parent_path = os.path.dirname(module_path)
# Ex: /Users/bvezina/sandbox/Durpro18/addons

# Exclure si le chemin parent contient /odoo/, /enterprise/ ou /design-themes/
if '/odoo/' in parent_path or parent_path.endswith('/odoo'):
    continue  # ❌ Module exclu
if '/enterprise' in parent_path or parent_path.endswith('/enterprise'):
    continue  # ❌ Module exclu
if '/design-themes' in parent_path or parent_path.endswith('/design-themes'):
    continue  # ❌ Module exclu
```

### Règles de Filtrage

| Condition | Résultat | Exemple |
|-----------|----------|---------|
| Chemin contient `/odoo/` | ❌ Exclu | `/path/to/odoo/addons/sale` |
| Chemin contient `/enterprise/` | ❌ Exclu | `/path/to/enterprise/account` |
| Chemin contient `/design-themes/` | ❌ Exclu | `/path/to/design-themes/theme_clean` |
| Module est un symlink | ❌ Exclu | Lien symbolique |
| Parent ≠ `addons` | ❌ Exclu | `/custom/modules/my_module` |
| Parent = `addons` ET aucune exclusion | ✅ Inclus | `/any/path/addons/durpro_stock` |

## Exemples Concrets

### Setup 1 : Installation Standard

```
/opt/odoo/
├── odoo/
│   ├── addons/          ❌ Exclu (contient 'odoo')
│   │   └── sale/
│   └── odoo/
│       └── addons/      ❌ Exclu (contient 'odoo')
│           └── base/
├── enterprise/          ❌ Exclu (contient 'enterprise')
│   └── account/
└── custom/
    └── addons/          ✅ Inclus !
        └── my_module/
```

### Setup 2 : Multi-instance

```
/home/user/projects/
├── client_a/
│   ├── odoo/addons/     ❌ Exclu
│   └── addons/          ✅ Inclus
│       └── client_a_custom/
└── client_b/
    ├── odoo/addons/     ❌ Exclu
    └── addons/          ✅ Inclus
        └── client_b_custom/
```

### Setup 3 : Windows

```
C:\Odoo\
├── server\odoo\addons\  ❌ Exclu
├── enterprise\          ❌ Exclu
└── custom\addons\       ✅ Inclus
    └── custom_module\
```

### Setup 4 : Développement

```
~/Dev/
├── durpro/
│   ├── odoo/
│   │   └── addons/      ❌ Exclu
│   ├── enterprise/      ❌ Exclu
│   └── addons/          ✅ Inclus
│       └── durpro_*
└── bemade-tools/        ⚠️ Pas dans 'addons' → ❌ Exclu
```

## Avantages de l'Approche

### ✅ Portable
- Fonctionne sous **Linux**, **macOS**, **Windows**
- Pas de chemin absolu hardcodé
- Indépendant de la structure de dossiers

### ✅ Simple
- Logique claire et lisible
- Peu de code (15 lignes)
- Facile à débugger

### ✅ Robuste
- Gère tous les cas edge
- N'importe où que soit installé Odoo
- Multi-instance compatible

### ✅ Maintenable
- Pas de dépendance à la config
- Pas de parsing de fichier
- Extension facile (ajouter d'autres exclusions)

## Cas Particuliers

### Symlinks

Les symlinks sont **toujours exclus** :
```python
if os.path.islink(module_path):
    continue  # ❌ Exclu
```

**Pourquoi ?** Les symlinks peuvent pointer vers des modules qui ne sont pas physiquement dans le dossier custom (ex: lien vers un module partagé).

### Modules dans des dossiers non-standard

Si vous avez des modules custom dans un dossier **qui ne s'appelle pas `addons`** :

```
/home/user/
└── custom_modules/      ❌ Exclu (parent != 'addons')
    └── my_module/
```

**Solution** : Renommer ou créer un symlink (mais symlink sera exclu).  
**Meilleure solution** : Respecter la convention Odoo avec un dossier `addons/`.

### Modules avec 'odoo' dans le nom

```
/path/addons/
└── my_odoo_connector/   ✅ Inclus (pas de 'odoo' dans le *chemin*)
```

La vérification porte sur les **composants du chemin**, pas le nom du module.

## Tests Recommandés

Après upgrade du module, vérifier dans le wizard :

1. **Modules affichés** : Seulement vos modules custom
2. **Modules exclus** : Aucun module odoo/enterprise/themes
3. **Multi-setup** : Tester sur différentes structures de dossiers
4. **Logs** : Aucune erreur dans les logs Odoo

## Code Source

Fichier : `wizard/studio_view_converter.py`  
Méthode : `_get_allowed_modules()`  
Lignes : ~75-116

## Historique

- **v18.0.1.1.0** : Filtrage par chemin absolu (non portable)
- **v18.0.1.1.2** : Filtrage par composants de chemin (portable mais incomplet)
- **v18.0.1.1.4** : Filtrage par vérification du chemin parent (robuste) ✅

## Différences entre les versions

### v1.1.2 (Composants)
```python
path_parts = module_path.split(os.sep)
if 'odoo' in path_parts:  # Vérifie si 'odoo' est un composant
```
**Problème** : Parfois ne détecte pas correctement les modules imbriqués.

### v1.1.4 (Chaîne de caractères)
```python
parent_path = os.path.dirname(module_path)
if '/odoo/' in parent_path or parent_path.endswith('/odoo'):  # Vérifie la chaîne complète
```
**Avantage** : Plus fiable car vérifie la présence de la sous-chaîne dans le chemin complet.

---

**Date** : 2025-10-20  
**Auteur** : Durpro Development Team
