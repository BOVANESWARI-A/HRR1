from pathlib import Path
import shutil
import hashlib

current_root = Path(r"D:\HRAPP-Updated\HRR1\media")
report = Path(r"D:\HRAPP-Updated\HRR1\missing_media_report.txt")

# Add older project media folders here if they exist.
old_roots = [
    Path(r"D:\HRR1\HR4\HR3\HRRRR\Stalwart_Django_PostgreSQL\media"),
    Path(r"D:\EDOCS3\EDOCS2\EDOCSS\HRR1\media"),
    Path(r"D:\EDOCS4\EDOCS3\EDOCS2\EDOCSS\HRR1\media"),
    Path(r"D:\HR5\HR4\HR3\HRRR\Stalwart_Django_PostgreSQL\media"),
    Path(r"D:\HR1\HRRRR_App\HRRRR\Stalwart_Django_PostgreSQL\media"),
]

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

roots = [p for p in old_roots if p.is_dir()]
print("Existing old media folders:", len(roots))
for root in roots:
    print("  ", root)

if not report.is_file():
    raise SystemExit("Missing media report not found: " + str(report))

# Index old files by name.
index = {}
for root in roots:
    for p in root.rglob("*"):
        if p.is_file():
            index.setdefault(p.name.lower(), []).append(p)

restored = 0
already_present = 0
not_found = []
ambiguous = []

for line in report.read_text(encoding="utf-8-sig").splitlines():
    rel = line.strip().replace("/", "\\")
    if not rel:
        continue

    destination = current_root / rel

    if destination.is_file():
        already_present += 1
        continue

    # Prefer the same relative path in an old media root.
    source = next(
        (root / rel for root in roots if (root / rel).is_file()),
        None
    )

    if source is None:
        matches = index.get(Path(rel).name.lower(), [])
        if not matches:
            not_found.append(rel)
            continue

        # If multiple copies exist, only use them when their contents match.
        unique_hashes = {}
        for candidate in matches:
            unique_hashes.setdefault(digest(candidate), candidate)

        if len(unique_hashes) > 1:
            ambiguous.append((rel, matches))
            continue

        source = next(iter(unique_hashes.values()))

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    restored += 1
    print("RESTORED:", rel, "<-", source)

print("\n--- RESTORE SUMMARY ---")
print("Restored:", restored)
print("Already present:", already_present)
print("Not found in old folders:", len(not_found))
print("Ambiguous files skipped:", len(ambiguous))

Path("media_restore_not_found.txt").write_text(
    "\n".join(not_found), encoding="utf-8"
)
Path("media_restore_ambiguous.txt").write_text(
    "\n".join(
        f"{rel} | " + " | ".join(str(p) for p in matches)
        for rel, matches in ambiguous
    ),
    encoding="utf-8"
)
print("Remaining-file reports saved in the project folder.")