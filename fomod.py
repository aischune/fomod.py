#!/usr/bin/env python3
"""Terminal FOMOD installer that follows Mod Organizer 2's behaviour.

Usage:  fomod.py ARCHIVE OUTPUT_DIR [--data GAME_DATA_DIR]

.zip is handled natively; .7z, .rar and others go through 7z, bsdtar or unrar.
bsdtar comes with libarchive, which every Arch-based system (CachyOS included) has.
"""
import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
import textwrap
import zipfile
import xml.etree.ElementTree as ET
from pathlib import Path

SINGLE = ("SelectExactlyOne", "SelectAtMostOne")
GROUP_HINT = {
    "SelectExactlyOne": "choose one",
    "SelectAtMostOne": "choose one or none",
    "SelectAtLeastOne": "choose at least one",
    "SelectAny": "choose any",
    "SelectAll": "all included",
}
TYPE_NOTE = {"Required": " (required)", "Recommended": " (recommended)", "NotUsable": " (unavailable)"}


# ---------------------------------------------------------------- paths & files

def norm(path):
    """FOMOD paths are Windows-style; turn them into 'a/b/c'."""
    parts = [p for p in (path or "").replace("\\", "/").split("/") if p not in ("", ".")]
    if ".." in parts:
        raise ValueError(f"unsafe path: {path}")
    return "/".join(parts)


def extract(archive, dest):
    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                try:
                    rel = norm(info.filename)  # also fixes zips made with backslashes
                except ValueError:
                    continue
                if not rel or info.filename.endswith(("/", "\\")):
                    continue
                target = dest / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info) as src, open(target, "wb") as out:
                    shutil.copyfileobj(src, out)
        return
    for cmd in (["7z", "x", "-y", "-bso0", "-bsp0", f"-o{dest}", str(archive)],
                ["bsdtar", "-xf", str(archive), "-C", str(dest)],
                ["unrar", "x", "-y", "-idq", str(archive), f"{dest}/"]):
        if shutil.which(cmd[0]) and subprocess.run(cmd).returncode == 0:
            return
    sys.exit("error: could not extract the archive (installing 7zip may help)")


def find_config(root):
    found = [Path(d, f) for d, _, files in os.walk(root) if Path(d).name.lower() == "fomod"
             for f in files if f.lower() == "moduleconfig.xml"]
    if not found:
        sys.exit("error: no fomod/ModuleConfig.xml in the archive")
    return min(found, key=lambda p: len(p.parts))


def load_xml(path):
    data = path.read_bytes()
    try:
        root = ET.fromstring(data)
    except ET.ParseError:  # encoding declaration that doesn't match the real encoding
        if data[:2] in (b"\xff\xfe", b"\xfe\xff"):
            enc = "utf-16"
        elif data[1:2] == b"\x00":
            enc = "utf-16-le"
        else:
            enc = "utf-8-sig"
        text = re.sub(r"^\s*<\?xml[^>]*\?>", "", data.decode(enc, errors="replace"))
        root = ET.fromstring(text)
    for el in root.iter():
        if isinstance(el.tag, str) and "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


class Output:
    """Writes files, merging names case-insensitively as Windows (and so MO2) does."""

    def __init__(self, root):
        self.root = root
        self.count = 0
        self.known = {}
        for d, dirs, files in os.walk(root):
            for n in dirs + files:
                rel = Path(d, n).relative_to(root).as_posix()
                self.known[rel.lower()] = rel

    def copy(self, src, rel):
        real = ""
        for part in rel.split("/"):
            cand = f"{real}/{part}" if real else part
            real = self.known.setdefault(cand.lower(), cand)
        target = self.root / real
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, target)
        self.count += 1


# ---------------------------------------------------------------- conditions

class Plugins:
    """Answers fileDependency checks: from --data if given, otherwise by asking once per file."""

    def __init__(self, data_dir):
        self.present = {n.lower() for n in os.listdir(data_dir)} if data_dir else None
        self.answers = {}

    def state(self, name):
        key = name.lower()
        if self.present is not None:
            return "Active" if key in self.present else "Missing"
        while key not in self.answers:
            choice = input(f"Is '{name}' [a]ctive, [i]nactive or [m]issing? ").strip().lower()[:1]
            if choice in ("a", "i", "m"):
                self.answers[key] = {"a": "Active", "i": "Inactive", "m": "Missing"}[choice]
        return self.answers[key]


def check(dep, flags, plugins):
    if dep is None:
        return True
    if dep.tag == "flagDependency":
        return flags.get(dep.get("flag"), "") == (dep.get("value") or "")
    if dep.tag == "fileDependency":
        return plugins.state(dep.get("file", "")) == dep.get("state")
    if dep.tag in ("gameDependency", "fommDependency", "foseDependency"):
        return True  # version checks need the game itself; treated as met
    results = (check(c, flags, plugins) for c in dep)
    return any(results) if dep.get("operator") == "Or" else all(results)


def plugin_type(plugin, flags, plugins):
    desc = plugin.find("typeDescriptor")
    if desc is None:
        return "Optional"
    if desc.find("type") is not None:
        return desc.find("type").get("name", "Optional")
    dep_type = desc.find("dependencyType")
    for pattern in dep_type.findall("patterns/pattern"):
        if check(pattern.find("dependencies"), flags, plugins):
            return pattern.find("type").get("name", "Optional")
    return dep_type.find("defaultType").get("name", "Optional")


def ordered(parent, tag):
    if parent is None:
        return []
    items = parent.findall(tag)
    order = parent.get("order", "Ascending")  # the schema's default is Ascending
    if order == "Explicit":
        return items
    return sorted(items, key=lambda e: e.get("name", ""), reverse=order == "Descending")


def settle(chosen, gtype, types):
    """Apply MO2's selection rules to one group: locks, single-choice limits, defaults."""
    if gtype == "SelectAll":
        return set(range(len(types)))
    chosen = {p for p in chosen if types[p] != "NotUsable"}
    required = [p for p, t in enumerate(types) if t == "Required"]
    if gtype in SINGLE:
        if required:
            chosen = {required[0]}
        elif len(chosen) > 1:
            chosen = {min(chosen)}
    else:
        chosen |= set(required)
    if not chosen and gtype in ("SelectExactlyOne", "SelectAtLeastOne"):
        chosen = {p for p, t in enumerate(types) if t != "NotUsable"}
        chosen = {min(chosen)} if chosen else set()
    return chosen


def priority(el):
    try:
        return int(el.get("priority", "0"))
    except ValueError:
        return 0


# ---------------------------------------------------------------- installer

class Installer:
    def __init__(self, config, plugins):
        self.root = load_xml(config)
        self.base = config.parent.parent  # the folder holding fomod/ is the mod's root
        self.files = {}  # lower-case path -> real path (FOMOD paths ignore case)
        for d, _, names in os.walk(self.base):
            for n in names:
                real = Path(d, n).relative_to(self.base).as_posix()
                self.files[real.lower()] = real
        self.plugins = plugins
        self.steps = [(step, [(g, ordered(g.find("plugins"), "plugin"))
                              for g in ordered(step.find("optionalFileGroups"), "group")])
                      for step in ordered(self.root.find("installSteps"), "installStep")]
        self.selected = {}  # (step, group) -> set of plugin indices

    def flags(self, path):
        flags = {}
        for s in path:
            if s is None:
                continue
            for g, (_, plugs) in enumerate(self.steps[s][1]):
                for p in sorted(self.selected.get((s, g), ())):
                    for f in plugs[p].findall("conditionFlags/flag"):
                        flags[f.get("name")] = (f.text or "").strip()
        return flags

    def next_visible(self, after, flags):
        for s in range(after + 1, len(self.steps)):
            if check(self.steps[s][0].find("visible"), flags, self.plugins):
                return s
        return None

    def types(self, s, flags):
        return [[plugin_type(p, flags, self.plugins) for p in plugs] for _, plugs in self.steps[s][1]]

    # -- one page of the wizard

    def run_step(self, s, flags):
        step, groups = self.steps[s]
        types = self.types(s, flags)
        for g, (group, _) in enumerate(groups):
            start = self.selected.get((s, g))
            if start is None:
                start = {p for p, t in enumerate(types[g]) if t in ("Required", "Recommended")}
            self.selected[(s, g)] = settle(start, group.get("type"), types[g])
        items = [(g, p) for g, (_, plugs) in enumerate(groups) for p in range(len(plugs))]

        self.show(step, groups, types, s)
        while True:
            cmd = input("> ").strip().lower()
            if cmd == "q":
                sys.exit("Cancelled.")
            if cmd == "b":
                return "back"
            if cmd == "":
                problem = self.unmet(s, groups)
                if not problem:
                    return "next"
                print(problem)
                continue
            changed = False
            for tok in cmd.split():
                num = tok.lstrip("?")
                if not num.isdigit() or not 1 <= int(num) <= len(items):
                    print(f"Unknown input: {tok}")
                    continue
                g, p = items[int(num) - 1]
                if tok.startswith("?"):
                    self.describe(groups[g][1][p])
                else:
                    error = self.toggle((s, g), p, groups[g][0].get("type"), types[g])
                    print(error) if error else None
                    changed = changed or not error
            if changed:
                self.show(step, groups, types, s)

    def show(self, step, groups, types, s):
        print(f"\n== {step.get('name', '')} ==")
        n = 0
        for g, (group, plugs) in enumerate(groups):
            gtype = group.get("type")
            print(f"\n {group.get('name', '')} ({GROUP_HINT.get(gtype, gtype)})")
            for p, plugin in enumerate(plugs):
                n += 1
                mark = "x" if p in self.selected[(s, g)] else " "
                print(f"  {n:>2}. [{mark}] {plugin.get('name', '')}{TYPE_NOTE.get(types[g][p], '')}")
        print("\nNumber: toggle   ?number: details   Enter: next   b: back   q: quit")

    def describe(self, plugin):
        width = min(shutil.get_terminal_size().columns, 100)
        print(f"\n {plugin.get('name', '')}")
        for line in (plugin.findtext("description") or "").strip().splitlines():
            print(textwrap.fill(line.strip(), width, initial_indent="    ", subsequent_indent="    "))
        image = plugin.find("image")
        if image is not None:
            try:
                real = self.files.get(norm(image.get("path")).lower())
            except ValueError:
                real = None
            if real:
                print(f"    Image: {self.base / real}")
        print()

    def toggle(self, key, p, gtype, types):
        chosen = self.selected[key]
        if types[p] == "NotUsable":
            return "That option is unavailable."
        if p in chosen:
            if types[p] == "Required" or gtype in ("SelectAll", "SelectExactlyOne"):
                return "That option can't be deselected."
            chosen.discard(p)
        else:
            if gtype in SINGLE:
                if any(types[q] == "Required" for q in chosen):
                    return "Another option in this group is required."
                chosen.clear()
            chosen.add(p)
        return None

    def unmet(self, s, groups):
        for g, (group, _) in enumerate(groups):
            n, gtype = len(self.selected[(s, g)]), group.get("type")
            if (gtype == "SelectExactlyOne" and n != 1) or (gtype == "SelectAtLeastOne" and n < 1):
                return f"'{group.get('name', '')}': {GROUP_HINT[gtype]}."
        return None

    # -- installation

    def gather(self, path):
        ops = []

        def add(container):
            if container is not None:
                ops.extend(el for el in container if el.tag in ("file", "folder"))

        add(self.root.find("requiredInstallFiles"))
        for k, s in enumerate(path):
            if s is None:
                continue
            types = self.types(s, self.flags(path[:k]))
            for g, (_, plugs) in enumerate(self.steps[s][1]):
                for p, plugin in enumerate(plugs):
                    chosen = p in self.selected[(s, g)]
                    files = plugin.find("files")
                    for el in (files if files is not None else []):
                        if (chosen or el.get("alwaysInstall") == "true"
                                or (el.get("installIfUsable") == "true" and types[g][p] != "NotUsable")):
                            ops.append(el)
        flags = self.flags(path)
        for pattern in self.root.findall("conditionalFileInstalls/patterns/pattern"):
            if check(pattern.find("dependencies"), flags, self.plugins):
                add(pattern.find("files"))
        return sorted(ops, key=priority)  # stable: on equal priority, later entries overwrite

    def install(self, ops, out):
        for el in ops:
            try:
                src = norm(el.get("source"))
                raw_dest = el.get("destination")
                dest = norm(src if raw_dest is None else raw_dest)
            except ValueError as e:
                print(f"warning: skipped {e}")
                continue
            if el.tag == "folder":
                depth = len(src.split("/")) if src else 0
                prefix = src.lower() + "/" if src else ""
                matches = [real for low, real in self.files.items()
                           if low.startswith(prefix) and (src or not low.startswith("fomod/"))]
                if not matches:
                    print(f"warning: folder not found in archive: {src}")
                for real in matches:
                    rest = "/".join(real.split("/")[depth:])
                    out.copy(self.base / real, f"{dest}/{rest}" if dest else rest)
            else:
                real = self.files.get(src.lower())
                if real is None:
                    print(f"warning: file not found in archive: {src}")
                    continue
                if raw_dest is not None and (not dest or raw_dest.endswith(("/", "\\"))):
                    dest = f"{dest}/{Path(real).name}" if dest else Path(real).name
                out.copy(self.base / real, dest)

    def run(self, out_dir):
        print(f"\n{(self.root.findtext('moduleName') or '').strip()}")
        if not check(self.root.find("moduleDependencies"), {}, self.plugins):
            sys.exit("This mod's requirements are not met.")
        path = [self.next_visible(-1, {})]  # visited steps; None marks the final confirmation
        while True:
            s = path[-1]
            if s is None:
                ans = input("\nInstall now? [Y]es / [b]ack / [q]uit: ").strip().lower()
                if ans in ("", "y"):
                    break
                if ans == "q":
                    sys.exit("Cancelled.")
                if ans == "b" and len(path) > 1:
                    path.pop()
            elif self.run_step(s, self.flags(path[:-1])) == "back":
                if len(path) > 1:
                    path.pop()
                else:
                    print("Already at the first step.")
            else:
                path.append(self.next_visible(s, self.flags(path)))
        out = Output(out_dir)
        self.install(self.gather(path), out)
        print(f"Installed {out.count} files to {out_dir}")


def main():
    ap = argparse.ArgumentParser(description="Install a FOMOD mod archive from the terminal.")
    ap.add_argument("archive", type=Path, help=".zip, .7z, .rar, ...")
    ap.add_argument("output", type=Path, help="folder to install into")
    ap.add_argument("--data", type=Path, metavar="DIR",
                    help="game Data folder, used to answer 'is plugin X active' checks "
                         "(without it you are asked when a mod needs to know)")
    args = ap.parse_args()
    if not args.archive.is_file():
        sys.exit(f"error: {args.archive} not found")
    if args.data and not args.data.is_dir():
        sys.exit(f"error: {args.data} is not a folder")

    out_dir = args.output.resolve()
    if out_dir.exists() and any(out_dir.iterdir()):
        if input(f"{out_dir} is not empty. Merge into it? [y/N] ").strip().lower() != "y":
            sys.exit("Cancelled.")
    out_dir.mkdir(parents=True, exist_ok=True)

    # Extract next to the output rather than /tmp, which is RAM (tmpfs) on CachyOS.
    with tempfile.TemporaryDirectory(prefix=".fomod-", dir=out_dir.parent) as tmp:
        print("Extracting...")
        extract(args.archive, Path(tmp))
        Installer(find_config(Path(tmp)), Plugins(args.data)).run(out_dir)


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        sys.exit("\nCancelled.")
