import json, os
base = r"C:\Users\sickv\AppData\Local\ContinuityStudio"
for fname in ["settings.json", "config.json", ".env"]:
    p = os.path.join(base, fname)
    if os.path.exists(p):
        d = json.load(open(p)) if p.endswith('.json') else open(p).read()
        print(f"=== {fname} ===")
        print(d)
# Also check running exe's env / config
base2 = r"E:\Continuity Studio"
for fname in ["settings.json", "config.json", ".env"]:
    p = os.path.join(base2, fname)
    if os.path.exists(p):
        d = json.load(open(p)) if p.endswith('.json') else open(p).read()
        print(f"=== E:\\Continuity Studio\\{fname} ===")
        print(d)
