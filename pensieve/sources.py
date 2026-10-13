"""Everything Pensieve can index, grouped into categories that can be switched on and off as a whole or item by
item. Switching a source off removes what it had indexed on the next sweep."""
from pathlib import Path

from pensieve import apps, config, settings

AGENTS = {"claude": "Claude Code", "codex": "Codex", "qwen": "Qwen Code"}
CATEGORIES = [
    ("agents", "AI agent sessions", "Your conversations with coding agents, linked to the code they touched."),
    ("files", "Files & folders", "Documents in these folders: text, Markdown, code, PDF, Word, PowerPoint. Git repos inside them are indexed as repos."),
    ("repos", "Git repositories", "Code with git blame and history, including uncommitted changes and linked worktrees."),
    ("apps", "Apps", "Content that lives inside apps on this Mac."),
]


def _home(p) -> str:
    return str(p).replace(str(Path.home()), "~", 1)


def folder_tree(roots, extra=()):
    """Folders to group repos by, so a whole folder can be switched at once. A folder is shown when it holds two or
    more repos or shown folders; a chain of folders with one thing each folds into one, labeled by its path from
    the folder above ("WildThink/Before_2024/SightCall"). `extra` folders (e.g. ones with a rule) are always shown.
    Returns {folder: (parent folder or None, label)} and {root: its folder or None}, all absolute paths."""
    from collections import defaultdict
    kids = defaultdict(set)
    for r in set(roots):
        child = Path(r)
        for a in Path(r).parents:
            kids[str(a)].add(str(child))
            child = a
    keep = {a for a, c in kids.items() if len(c) >= 2} | {str(Path(e)) for e in extra}

    def up(p):  # nearest shown folder above p
        return next((str(a) for a in Path(p).parents if str(a) in keep), None)
    folders = {}
    for f in keep:
        parent = up(f)
        folders[f] = (parent, str(Path(f).relative_to(parent)) if parent else _home(f))
    return folders, {r: up(r) for r in roots}


def _folder_items(rows, rule_folders):
    """Folder rows for Sources: `rows` are repo items (parent 'repos/discover'), which get their folder as parent.
    A folder is on or off as its rule says, and mixed when the repos in it don't all agree."""
    folders, parent_of = folder_tree([r["path"] for r in rows], rule_folders)
    out = []
    for f, (parent, label) in sorted(folders.items()):
        inside = [r for r in rows if settings._under(r["path"], f)]
        on = settings.enabled(f"repos/{f}/")
        n = len(inside)
        out.append(dict(id=f"repos/{f}/", kind="folder", label=label, path=f,
                        detail=_home(f) + (f" · {n} repo{'s' if n != 1 else ''}" if n else " · switched off, with any repos in it"),
                        available=Path(f).is_dir(), enabled=on, mixed=any(r["enabled"] != on for r in inside),
                        count=sum(r.get("count", 0) for r in inside), unit="files",
                        parent=f"repos/{parent}/" if parent else "repos/discover"))
    for r in rows:
        r["parent"] = f"repos/{parent_of[r['path']]}/" if parent_of.get(r["path"]) else "repos/discover"
    return out


def catalog(store, repos, files) -> list[dict]:
    on = settings.enabled
    with store.lock:
        db = store.db
        per_root = dict(db.execute("SELECT root, COUNT(*) FROM files GROUP BY root").fetchall())
        per_repo = dict(db.execute("SELECT repo, COUNT(DISTINCT path) FROM code_chunks GROUP BY repo").fetchall())
        repo_rows = db.execute("SELECT root, name FROM repos ORDER BY name").fetchall()
        wt_rows = db.execute("SELECT path, repo, branch FROM worktrees ORDER BY path").fetchall()
        per_wt = dict(db.execute("SELECT wt, COUNT(*) FROM checkout_files WHERE wt!='' GROUP BY wt").fetchall())

    agents = []
    for sid, k, root, _ in config.session_sources():
        extra = "@" in sid
        with store.lock:
            n = store.db.execute("SELECT COUNT(*) FROM sessions WHERE n_chunks>0 AND source=? AND path LIKE ?",
                                 (k, str(root) + "/%")).fetchone()[0]
        where = root.parent.name if root.name in ("projects", "sessions") else root.name
        agents.append(dict(id=sid, label=AGENTS.get(k, k) + (f" · {where}" if extra else ""), detail=_home(root),
                           available=root.is_dir(), enabled=on(sid), count=n, unit="sessions",
                           remove=dict(kind="agents", path=sid.split("@", 1)[1]) if extra else None))

    folders = []
    for f in settings.get("folders"):
        p = Path(f).expanduser()
        folders.append(dict(id=f"files/{f}", label=p.name or f, detail=_home(p), available=p.is_dir(), enabled=on(f"files/{f}"),
                            count=per_root.get(str(p), 0), unit="files", remove=dict(kind="files", path=f)))

    s = settings.load()
    skipped = getattr(repos, "skipped", [])
    repo_items = [
        dict(id="repos/discover", label="Discover git repositories",
             detail=f"Repos your agents worked in, and repos under {', '.join(s['sweep_roots'])} and your folders "
                    f"({s['sweep_depth']} levels deep, up to {s['max_repo_files']:,} files each)",
             available=True, enabled=on("repos/discover"), kind="rule"),
    ]
    added = {a: str(Path(a).expanduser()) for a in s["repos"]}  # added in Sources: indexed whatever discovery finds

    def added_by(root):
        return next((a for a, p in added.items() if root == p or root.startswith(p + "/")), None)

    for root, name in repo_rows:
        a = added_by(root)
        note = " · not syncing" if repos.switched_off(root) else ""
        repo_items.append(dict(id=f"repos/{root}", label=name, detail=_home(root) + note,
                               available=Path(root).is_dir(), enabled=on(f"repos/{root}"), count=per_repo.get(root, 0), unit="files",
                               parent=None if a else "repos/discover",
                               remove=dict(kind="repos", path=a if a and added[a] == root else root)))
        for wt, repo, branch in (w for w in wt_rows if w[1] == root):  # synced with their repo: no switch of their own
            repo_items.append(dict(id=f"worktree/{wt}", kind="worktree", label=Path(wt).name,
                                   detail=f"{branch or 'detached'} · {_home(wt)}", available=Path(wt).is_dir(),
                                   enabled=on(f"repos/{root}"), count=per_wt.get(wt, 0), unit="changed files",
                                   parent=f"repos/{root}"))
    indexed = {r for r, _ in repo_rows}
    for root, _ in repos.found:  # found by the sweep, waiting for their turn
        if root not in indexed and not added_by(root):
            repo_items.append(dict(id=f"repos/{root}", label=Path(root).name, detail=f"{_home(root)} · indexing soon",
                                   available=True, enabled=on(f"repos/{root}"), count=0, unit="files", parent="repos/discover"))
    for a, p in added.items():  # a repo not indexed yet (switched off, or waiting for the sweep), or a folder of repos
        if not any(r == p for r, _ in repo_rows):
            is_repo, enabled = (Path(p) / ".git").exists(), on(f"repos/{p}")
            note = ("switched off" if not enabled else "indexing soon") if is_repo else "repos inside are always indexed"
            repo_items.append(dict(id=f"repos/{p}", label=Path(p).name, detail=f"{_home(p)} · {note}",
                                   available=Path(p).is_dir(), enabled=enabled, count=sum(n for r, n in per_repo.items()
                                   if r.startswith(p + "/")), unit="files", remove=dict(kind="repos", path=a)))
    for sk in skipped:
        if sk["root"] in added.values():  # listed above, as added
            continue
        repo_items.append(dict(id=f"repos/{sk['root']}", label=Path(sk["root"]).name, detail=f"{_home(sk['root'])} · skipped: "
                               f"{sk['files']:,} files (switch on to index anyway)", available=True, enabled=False, count=0, unit="files",
                               parent="repos/discover"))
    for k, v in s["repo_rules"].items():  # repos switched off no longer appear in the repos table; keep them listed
        r = str(Path(k).expanduser())
        if not k.endswith("/") and not v and not any(i["id"] == f"repos/{r}" for i in repo_items):
            repo_items.append(dict(id=f"repos/{r}", label=Path(r).name, detail=_home(r), available=Path(r).is_dir(),
                                   enabled=False, count=0, unit="files", parent="repos/discover"))
    found = [i for i in repo_items if i.get("parent") == "repos/discover"]
    for i in found:
        i["path"] = i["id"][6:]
    repo_items += _folder_items(found, [str(Path(k).expanduser()) for k in s["repo_rules"] if k.endswith("/")])

    app_items = []
    for k, a in apps.APPS.items():
        st = apps.state(k)
        roots = [str(p) for p in a["roots"]()] if a["available"]() else []
        app_items.append(dict(id=f"apps/{k}", label=a["label"], detail=st.get("error") or a["detail"], error=bool(st.get("error")),
                              available=a["available"](), enabled=on(f"apps/{k}"),
                              count=sum(n for r, n in per_root.items() if any(r == x or r.startswith(x + "/") for x in roots)), unit="items"))

    items = {"agents": agents, "files": folders, "repos": repo_items, "apps": app_items}
    for it in (i for group in items.values() for i in group if i.get("kind") not in ("rule", "worktree", "folder")):
        it["map"] = settings.on_map(it["id"])
    return [dict(id=c, label=label, description=desc, enabled=on(c), map=settings.on_map(c), items=items[c],
                 count=sum(i.get("count", 0) for i in items[c] if i.get("kind") not in ("rule", "worktree", "folder")),
                 addable=c != "apps")
            for c, label, desc in CATEGORIES]


def set_source(id: str, on: bool, repos=None, map_only=False):
    """Switch a category or item on/off (or, with map_only, only on/off the map). Switching on a repo the sweep
    skipped as too big adds it to `repos`. 'repos/<folder>/' switches every repo in that folder, now and later."""
    valid = {c for c, _, _ in CATEGORIES}
    if id.split("/", 1)[0] not in valid:
        raise ValueError(f"unknown source {id!r}: ids start with {', '.join(sorted(valid))}")
    if map_only:
        return settings.set_on_map(id, on)
    if on and id.startswith("repos/") and repos is not None:
        root = id[6:]
        if any(sk["root"] == root for sk in getattr(repos, "skipped", [])) and root not in settings.get("repos"):
            settings.update({"repos": settings.get("repos") + [root]})
    return settings.set_enabled(id, on)
