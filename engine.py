"""Policy engine + bounded formal verifier (inspired by bounded model checking / spec repair research)."""
import itertools, json, re

BUCKETS = [500, 1000, 1001, 5000, 5001, 10000, 10001, 25000, 25001, 100000]
VENDOR = ["approved", "unapproved", "blocked"]
ROLES = ["Manager", "Director", "VP Finance"]

INJECTION_PATTERNS = [
    r"ignore (all |any |the )?(previous|prior|above) (instructions|rules|policy)", r"disregard (the |all )?(policy|rules|instructions)",
    r"(auto[- ]?)?approve this (invoice|request|payment|immediately)", r"you are now", r"system prompt", r"override (the )?(policy|approval|controls?)",
    r"do not (flag|escalate|check)", r"bypass", r"act as (an? )?(admin|cfo|approver)", r"mark (this )?as (approved|safe)"]

def scan_injection(text):
    return [p for p in INJECTION_PATTERNS if re.search(p, text or "", re.I)]

def match(cond, f):
    if "amount_gt" in cond and not f["amount"] > cond["amount_gt"]: return False
    if "amount_lte" in cond and not f["amount"] <= cond["amount_lte"]: return False
    if "vendor_status" in cond and f["vendor_status"] not in cond["vendor_status"]: return False
    for k in ("duplicate", "injection", "budget_exceeded"):
        if k in cond and bool(f[k]) != cond[k]: return False
    return True

def decide(rules, f):
    ms = sorted([r for r in rules if r.get("enabled", True) and match(r["when"], f)], key=lambda r: r["priority"])
    if not ms: return {"action": "escalate:VP Finance", "rule": None, "matched": [], "default": True}
    return {"action": ms[0]["action"], "rule": ms[0]["id"], "matched": [r["id"] for r in ms], "default": False}

def states():
    for a, v, d, i, b in itertools.product(BUCKETS, VENDOR, [False, True], [False, True], [False, True]):
        yield dict(amount=a, vendor_status=v, duplicate=d, injection=i, budget_exceeded=b)

PROPS = [
    ("P1", "Blocked vendors are always rejected", lambda f, a: f["vendor_status"] != "blocked" or a == "reject"),
    ("P2", "Prompt-injection content is always rejected", lambda f, a: not f["injection"] or a == "reject"),
    ("P3", "Unapproved vendors are never auto-approved", lambda f, a: f["vendor_status"] == "approved" or a != "auto_approve"),
    ("P4", "Duplicate invoices are always rejected", lambda f, a: not f["duplicate"] or a == "reject"),
    ("P5", "Spend over $10,000 is never auto-approved", lambda f, a: f["amount"] <= 10000 or a != "auto_approve"),
    ("P6", "Over-budget requests are never auto-approved", lambda f, a: not f["budget_exceeded"] or a != "auto_approve"),
    ("P7", "Spend over $25,000 needs VP Finance (or is rejected)", lambda f, a: f["amount"] <= 25000 or a in ("reject", "escalate:VP Finance")),
]

def verify(rules):
    total = 0; viol = {p[0]: [] for p in PROPS}; used = set(); defaults = 0; ties = []
    for f in states():
        total += 1
        d = decide(rules, f)
        if d["default"]: defaults += 1
        if d["rule"]: used.add(d["rule"])
        ms = sorted([r for r in rules if r.get("enabled", True) and match(r["when"], f)], key=lambda r: r["priority"])
        if len(ms) > 1 and ms[0]["priority"] == ms[1]["priority"] and ms[0]["action"] != ms[1]["action"] and len(ties) < 3:
            ties.append({"state": f, "rules": [ms[0]["id"], ms[1]["id"]]})
        for pid, _, fn in PROPS:
            if not fn(f, d["action"]): viol[pid].append({"state": f, "action": d["action"], "rule": d["rule"]})
    props = [{"id": pid, "name": name, "holds": not viol[pid], "violations": len(viol[pid]), "counterexample": viol[pid][0] if viol[pid] else None} for pid, name, _ in PROPS]
    dead = [r["id"] for r in rules if r.get("enabled", True) and r["id"] not in used]
    return {"states_checked": total, "properties": props, "all_hold": all(p["holds"] for p in props), "dead_rules": dead, "ties": ties, "default_fallthrough_states": defaults}

def repair_deterministic(rules):
    """Symbolic repair: tighten auto-approve rules and make safety rejects take precedence."""
    out = json.loads(json.dumps(rules))
    for r in out:
        if r["action"] == "auto_approve":
            w = r["when"]
            w["vendor_status"] = ["approved"]; w["duplicate"] = False; w["injection"] = False; w["budget_exceeded"] = False
            w["amount_lte"] = min(w.get("amount_lte", 1000), 10000)
    rej = [r for r in out if r["action"] == "reject"]; rest = [r for r in out if r["action"] != "reject"]
    p = 1
    for r in sorted(rej, key=lambda r: r["priority"]): r["priority"] = p; p += 1
    for r in sorted(rest, key=lambda r: r["priority"]): r["priority"] = p; p += 1
    return out

def sanitize_rules(rules):
    ok = []
    for i, r in enumerate(rules):
        if not isinstance(r, dict) or "when" not in r or "action" not in r: raise ValueError("bad rule")
        act = r["action"]
        if act not in ("auto_approve", "reject") and not (act.startswith("escalate:") and act.split(":", 1)[1] in ROLES): raise ValueError("bad action " + str(act))
        ok.append({"id": str(r.get("id", f"R{i+1}")), "name": str(r.get("name", r.get("id", ""))), "priority": int(r.get("priority", i + 1)),
                   "when": dict(r["when"]), "action": act, "enabled": bool(r.get("enabled", True))})
    return ok

GOOD = sanitize_rules([
    {"id": "R1", "name": "Reject prompt-injection content", "priority": 1, "when": {"injection": True}, "action": "reject"},
    {"id": "R2", "name": "Reject blocked vendors", "priority": 2, "when": {"vendor_status": ["blocked"]}, "action": "reject"},
    {"id": "R3", "name": "Reject duplicate invoices", "priority": 3, "when": {"duplicate": True}, "action": "reject"},
    {"id": "R4", "name": "Over budget goes to VP Finance", "priority": 4, "when": {"budget_exceeded": True}, "action": "escalate:VP Finance"},
    {"id": "R5", "name": "Unapproved vendor (<= $25k) goes to Director", "priority": 5, "when": {"vendor_status": ["unapproved"], "amount_lte": 25000}, "action": "escalate:Director"},
    {"id": "R6", "name": "Auto-approve small approved-vendor spend", "priority": 6, "when": {"vendor_status": ["approved"], "amount_lte": 1000}, "action": "auto_approve"},
    {"id": "R7", "name": "Up to $5k needs Manager", "priority": 7, "when": {"amount_lte": 5000}, "action": "escalate:Manager"},
    {"id": "R8", "name": "Up to $25k needs Director", "priority": 8, "when": {"amount_lte": 25000}, "action": "escalate:Director"},
    {"id": "R9", "name": "Over $25k needs VP Finance", "priority": 9, "when": {"amount_gt": 25000}, "action": "escalate:VP Finance"},
])
BUGGY = sanitize_rules([
    {"id": "R0", "name": "Quick-approve under $2k (written in a hurry)", "priority": 0, "when": {"amount_lte": 2000}, "action": "auto_approve"},
    *[dict(r) for r in GOOD if r["id"] != "R6"],
])
