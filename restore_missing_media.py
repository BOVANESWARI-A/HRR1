import os
from pathlib import Path
from django.conf import settings
from django.apps import apps

media_root = Path(settings.MEDIA_ROOT)
print("MEDIA_ROOT:", media_root)
print("Scanning database file fields...")

missing = set()
checked = 0

for model in apps.get_models():
    for field in model._meta.get_fields():
        if not getattr(field, "concrete", False):
            continue
        if not (getattr(field, "get_internal_type", lambda: "")() in
                ("FileField", "ImageField")):
            continue

        try:
            for obj in model.objects.exclude(**{field.name: ""}).exclude(
                **{field.name: None}
            ).iterator():
                f = getattr(obj, field.name, None)
                name = getattr(f, "name", "")
                if not name:
                    continue
                checked += 1
                path = media_root / name
                if not path.is_file():
                    missing.add(name)
        except Exception as exc:
            print(f"Could not scan {model.__name__}.{field.name}: {exc}")

report = Path("missing_media_report.txt")
report.write_text(
    "\n".join(sorted(missing)),
    encoding="utf-8"
)

print("File references checked:", checked)
print("Missing unique files:", len(missing))
print("Report saved:", report.resolve())
