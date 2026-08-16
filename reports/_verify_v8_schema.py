# -*- coding: utf-8 -*-
"""READ-ONLY schema cross-check for schema_v8_dashboard_ro.sql.

Parses the authoritative table definitions from schema.sql +
schema_v7_withdrawals.sql, then verifies every table and column the v8
migration references actually exists. Catches exactly the class of bug
that caused the live 42703 (referencing tenant_id on tenants).

Static, no network, no secrets.
"""
import re
import sys

sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def parse_tables(path):
    """table name -> set of column names"""
    tables = {}
    src = open(path, encoding="utf-8").read()
    for m in re.finditer(r"create table if not exists (\w+)\s*\((.*?)\);", src, re.S):
        name, body = m.group(1), m.group(2)
        cols = set()
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("--"):
                continue
            cm = re.match(r"(\w+)\s+", line)
            if cm and not line.startswith(("primary", "unique", "check", "constraint", "foreign")):
                cols.add(cm.group(1))
        tables[name] = cols
    return tables


tables = {}
tables.update(parse_tables("schema.sql"))
tables.update(parse_tables("schema_v7_withdrawals.sql"))

v8 = open("schema_v8_dashboard_ro.sql", encoding="utf-8").read()

failures = []

# ── 1) GRANT SELECT list ──────────────────────────────────────
gm = re.search(r"grant select on(.*?)to dashboard_ro;", v8, re.S)
granted = [t.strip() for t in gm.group(1).split(",") if t.strip()]
print("== GRANT SELECT tables ==")
for t in granted:
    t = t.replace("public.", "").strip()
    ok = t in tables
    print(f"  {t:22s} exists={'✅' if ok else '❌'}  columns={len(tables.get(t, []))}")
    if not ok:
        failures.append(f"grant references missing table {t}")

# ── 2) RLS policies ───────────────────────────────────────────
print("\n== RLS policies ==")
# Exclude the execute format() template inside the do$$ block — it is a
# parameterised loop for the 9 child tables (validated separately below);
# only the standalone `create policy` statements are real DDL to check.
v8p = re.sub(r"do \$\$.*?\$\$", "", v8, flags=re.S)
for stmt in re.split(r"(?=create policy dashboard_ro_select)", v8p):
    if not stmt.startswith("create policy"):
        continue
    tm = re.search(r"on public\.(\w+)", stmt)
    t = tm.group(1)
    using = stmt[stmt.find("using"):stmt.find("using") + 140].replace("\n", " ")
    cols = tables.get(t)
    ok_t = t in tables
    print(f"  {t:22s} table={'✅' if ok_t else '❌'}  using={using.strip()[:90]}")
    if not ok_t:
        failures.append(f"policy on missing table {t}")
        continue
    # the comparison column is the one right of the '=' in the using clause
    cmp = re.search(r"=\s*(\w+)", using)
    col = cmp.group(1) if cmp else None
    if col is None:
        failures.append(f"policy on {t}: cannot extract comparison column")
        continue
    if col not in cols:
        failures.append(f"policy on {t} references missing column {col!r} (columns: {sorted(cols)})")
    if t == 'tenants' and col != 'id':
        failures.append(f"policy on tenants must reference id (its PK), got {col!r}")
    if t != 'tenants' and col != 'tenant_id':
        failures.append(f"policy on {t} must reference tenant_id, got {col!r} — isolation may be broken")

# The 9 child tables in the loop template — verify each has tenant_id.
print("\n== loop tables (policy via execute format) ==")
loop = re.search(r"do \$\$.*?array\[.*?\].*?\$\$", v8, re.S)
if loop:
    body = loop.group(0)
    listed = re.search(r"array\[(.*?)\]", body, re.S)
    if listed:
        for t in re.findall(r"'([\w]+)'", listed.group(1)):
            has = t in tables and 'tenant_id' in tables[t]
            print(f"  {t:22s} has tenant_id={'✅' if has else '❌'}")
            if not has:
                failures.append(f"loop table {t} lacks tenant_id column")
else:
    failures.append("cannot locate the policy loop block in schema_v8")

# ── 3) the specific tenants fix ───────────────────────────────
print("\n== tenants identity ==")
tcols = tables.get("tenants", set())
print(f"  tenants columns: {sorted(tcols)}")
print(f"  has 'id'? {'✅' if 'id' in tcols else '❌'}   has 'tenant_id'? {'⚠️ present' if 'tenant_id' in tcols else '✅ absent (correct — policy must use id)'}")
if "tenant_id" in tcols:
    failures.append("tenants unexpectedly has tenant_id column")

print("\n" + "=" * 60)
if failures:
    print("VERDICT: FAIL")
    for f in failures:
        print("  ❌", f)
    sys.exit(1)
print("VERDICT: PASS — every referenced table and column exists in the authoritative schema.")
