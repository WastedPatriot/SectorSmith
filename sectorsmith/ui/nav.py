"""Route table: rail categories, their sub-nav items and which screen each opens. The rail, sub-nav, breadcrumbs,
command palette and tests all read this, so a screen is highlighted from one place."""
from __future__ import annotations

import importlib
from dataclasses import dataclass, field


@dataclass
class Item:
    key: str
    label: str
    icon: str
    target: str               # "module:Class" for a screen, "app:method" for an action
    kw: dict = field(default_factory=dict)
    hint: str = ""            # extra words the command palette matches on
    destructive: bool = False  # opens a wizard; the palette never runs these directly


@dataclass
class Category:
    key: str
    label: str
    icon: str
    title: str
    desc: str
    groups: list              # [(caption or None, [Item])]
    working_on: bool = False  # show the 'Working on' machine card at the bottom of the sub-nav
    shortcut: str = ""


CATEGORIES = [
    Category("home", "Home", "home", "Home", "Workspace overview", [], shortcut="Ctrl+1"),
    Category("recover", "Recover", "recover", "Recover", "Bring back lost files and partitions", [
        (None, [Item("files", "Recover files", "recover", "screens:RecoverWizard", hint="undelete restore deep scan"),
                Item("partitions", "Find lost partitions", "partition", "screens:PartitionWizard",
                     hint="raw partition table repair")]),
    ], working_on=True, shortcut="Ctrl+2"),
    Category("erase", "Erase", "wipe", "Erase", "Wipe drives with a certificate", [
        (None, [Item("erase", "Erase and certify", "wipe", "screens:WipeWizard", hint="wipe nist dod sanitize",
                     destructive=True),
                Item("shred", "Shred files", "shred", "screens:ShredWizard", hint="delete securely", destructive=True),
                Item("free", "Clean free space", "refresh", "screens:ShredWizard", {"mode": "free"},
                     hint="wipe free space", destructive=True)]),
    ], working_on=True, shortcut="Ctrl+3"),
    Category("drives", "Drives", "drive", "Drives", "Health, imaging and disk images", [
        (None, [Item("all", "All drives", "drive", "workspace:DrivesScreen", hint="disks inventory serial")]),
        ("Tasks", [Item("health", "Health check", "health", "screens:HealthWizard", hint="smart surface scan bad"),
                   Item("clone", "Image and clone", "clone", "screens:CloneWizard", hint="backup vhd img copy"),
                   Item("open", "Open disk image", "image", "app:open_image_dialog", hint="vhd img dd")]),
        ("Advanced", [Item("expert", "Expert tools", "terminal", "app:open_advanced", hint="advanced hex classic")]),
    ], working_on=True, shortcut="Ctrl+4"),
    Category("machines", "Machines", "machines", "Machines", "Linked PCs, migration and network clone", [
        (None, [Item("linked", "Linked machines", "machines", "workspace:MachinesScreen", hint="pcs link"),
                Item("connect", "Connect a machine", "link", "link_screens:ConnectScreen",
                     hint="link usb winpe powershell")]),
        ("Tasks", [Item("migrate", "Migrate user", "migrate", "link_screens:MigrateWizard",
                        hint="move a user new pc profile"),
                   Item("netclone", "Network clone", "netclone", "link_screens:NetCloneWizard",
                        hint="clone one to many", destructive=True)]),
    ], working_on=True, shortcut="Ctrl+5"),
    Category("manage", "Manage", "manage", "Manage", "Software and upkeep on your linked PCs (preview)", [
        ("Software", [Item("library", "Packages", "package", "manage_screens:PackagesScreen",
                           hint="deploy library installers msi exe"),
                      Item("catalogue", "Catalogue", "search", "manage_screens:CatalogueScreen",
                           hint="winget chocolatey apps add chrome 7-zip"),
                      Item("deployments", "Deployments", "deploy", "manage_screens:DeploymentsScreen",
                           hint="deploy rollout schedule daily weekly"),
                      Item("run", "Run maintenance", "play", "deploy_screens:RunWizard", hint="deploy apply")]),
        ("Endpoints", [Item("clients", "Clients", "building", "manage_screens:ClientsScreen",
                            hint="baseline onboarding new pc"),
                       Item("tasks", "Tasks", "task", "manage_screens:TasksScreen", hint="scripts upkeep"),
                       Item("sessions", "Sessions", "clock", "manage_screens:SessionsScreen",
                            hint="history results")]),
    ], shortcut="Ctrl+6"),
    Category("jobs", "Jobs", "jobs", "Jobs", "What ran, where and for whom", [
        (None, [Item("history", "Job history", "jobs", "workspace:JobsScreen", hint="audit log certificates")]),
    ], shortcut="Ctrl+7"),
]
SETTINGS = Category("settings", "Settings", "settings", "Settings", "Preferences and help", [
    (None, [Item("prefs", "Preferences", "settings", "settings:SettingsScreen",
                 hint="appearance dark light personality mossbit presentation"),
            Item("guide", "How to use", "help", "guide:GuideScreen", hint="help guide")]),
])
ALL = CATEGORIES + [SETTINGS]
BY_KEY = {c.key: c for c in ALL}

# screens that are not sub-nav entries but belong somewhere: class name -> (category, item)
EXTRA = {
    "Home": ("home", None), "WelcomeScreen": ("home", None),
    "PackageBuilder": ("manage", "library"), "DeploymentWizard": ("manage", "deployments"),
    "ClientEditor": ("manage", "clients"), "TaskEditor": ("manage", "tasks"), "SessionView": ("manage", "sessions"),
    "BaselineEditor": ("manage", "clients"),
}


def items(cat: Category):
    for _cap, its in cat.groups:
        yield from its


def all_items():
    for cat in ALL:
        for it in items(cat):
            yield cat, it


def first_item(cat: Category):
    return next(items(cat), None)


def locate(screen_cls, kw=None):
    """(category key, item key) for a screen class and its keyword arguments."""
    name = screen_cls.__name__
    kw = kw or {}
    if name in EXTRA:
        return EXTRA[name]
    best = None
    for cat, it in all_items():
        if it.target.split(":")[1] != name:
            continue
        if it.kw and all(kw.get(k) == v for k, v in it.kw.items()):
            return cat.key, it.key
        if best is None:
            best = (cat.key, it.key)
    return best or (None, None)


def resolve(target: str):
    """'module:Class' -> the class (imported lazily to keep start-up fast)."""
    mod, name = target.split(":")
    return getattr(importlib.import_module(f"sectorsmith.ui.{mod}"), name)
