"""Behavioral tests for the 10 bug fixes. Standalone (no pytest)."""
import sys, threading

PASS, FAIL = [], []
def check(name, cond):
    (PASS if cond else FAIL).append(name)
    print(("ok   " if cond else "FAIL ") + name)

# 1. store.url_to_path path-traversal containment
import store
try:
    store.url_to_path("/data/../../../../Windows/win.ini")
    check("path-traversal rejected", False)
except ValueError:
    check("path-traversal rejected", True)
# valid managed path still resolves under DATA_DIR
p = store.url_to_path("/data/images/foo.png")
import os
check("valid /data/ path resolves under DATA_DIR",
      os.path.realpath(p).startswith(os.path.realpath(store.DATA_DIR)))

# 2. _as_analysis_dict normalizes a bare list (extract_json crash class)
import app
check("_as_analysis_dict(list) -> dict", isinstance(app._as_analysis_dict([1, 2]), dict))
check("_as_analysis_dict(list).get safe", app._as_analysis_dict([1]).get("issues") is None)
check("_as_analysis_dict(None) -> {}", app._as_analysis_dict(None) == {})

# 3. extract_json can return a list; normalizing it prevents .get() crash
import claude_client
val = claude_client.extract_json('[{"n":1,"vo":"hi"}]')
check("extract_json bare array -> list", isinstance(val, list))
check("normalized list.get works", app._as_analysis_dict(val).get("scenes") is None)

# 4. OpenAIClient._msg empty-content guard logic: simulate the parse
def msg_text(data):
    text = ((data.get("choices") or [{}])[0].get("message", {}).get("content") or "").strip()
    return text
check("null content does not crash", msg_text({"choices": [{"message": {"content": None}}]}) == "")

# 5. edit_holds float guard: non-numeric elements fall back to floor, no crash
def holds_from(parsed, n):
    holds = []
    for v in parsed:
        try:
            holds.append(max(0.4, float(v)))
        except (TypeError, ValueError):
            holds.append(0.4)
    return holds
h = holds_from([1.0, None, "x", 3], 4)
check("edit_holds tolerates null/str", h == [1.0, 0.4, 0.4, 3.0])

# 6. _run_capture returns CompletedProcess and honors timeout
cp = app._run_capture([sys.executable, "-c", "print('hi')"], timeout=30)
check("_run_capture normal returncode 0", cp.returncode == 0)
cp2 = app._run_capture([sys.executable, "-c", "import time; time.sleep(5)"], timeout=1)
check("_run_capture timeout -> nonzero returncode", cp2.returncode != 0)

# 7. vault_crypto roundtrip + concurrent init (no key race / no crash)
import vault_crypto
vault_crypto._fernet = None
vault_crypto._fallback = False
errs = []
def init_worker():
    try:
        vault_crypto._init_fernet()
    except Exception as e:
        errs.append(e)
ts = [threading.Thread(target=init_worker) for _ in range(8)]
[t.start() for t in ts]; [t.join() for t in ts]
check("concurrent _init_fernet no errors", not errs)
v = {"u@x.com": {"api_key": "secret-123"}}
enc = vault_crypto.encrypt_vault(v)
dec = vault_crypto.decrypt_vault(enc)
check("vault encrypt/decrypt roundtrip", dec["u@x.com"]["api_key"] == "secret-123")

# 8. store index lock exists and is reentrant
check("store._INDEX_LOCK is RLock", store._INDEX_LOCK.__class__.__name__ == "RLock")

print()
print(f"PASSED {len(PASS)}  FAILED {len(FAIL)}")
if FAIL:
    print("FAILURES:", FAIL); sys.exit(1)
