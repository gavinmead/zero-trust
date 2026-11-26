"""Demo 05 checks, run inside a user's JupyterLab pod (as a script file, not `python -c`:
pyspark mistakes a __main__ without __file__ for its own doctest run).

  python checks.py <step>     prints one line per check: "<label>: OK -> ..." or "<label>: DENIED -> ..."
"""
import json, re, sys
import zt_auth

TABLE = "lakehouse.analytics.trips"
spark = zt_auth.spark_session()


def check(label, fn):
    try:
        print(f"{label}: OK -> {fn()}")
    except Exception as e:  # noqa: BLE001 - report every failure as a denial line
        msg = str(e)
        grpc = re.search(r'details = "([^"]*)"', msg)   # raw gRPC errors put the reason on a later line
        print(f"{label}: DENIED -> {type(e).__name__}: {(grpc.group(1) if grpc else msg.splitlines()[0])[:200]}")


def whoami():
    k = json.loads(spark.conf.get("spark.zt.uc.claims"))["keycloak"]
    act = (k.get("act") or {}).get("client_id")
    return f"user={k.get('email')}, token aud={k.get('aud')} azp={k.get('azp')} act={act} cnf-bound={'cnf' in k}"


step = sys.argv[1]
check("whoami", whoami)
if step == "alice":
    check("create table", lambda: spark.sql(
        f"CREATE OR REPLACE TABLE {TABLE} (id INT, city STRING, fare DOUBLE) USING DELTA").collect() or "created")
    check("insert", lambda: spark.sql(
        f"INSERT INTO {TABLE} VALUES (1, 'Oslo', 21.5), (2, 'Lima', 9.0)").collect() or "2 rows")
    check("read", lambda: [tuple(r) for r in spark.table(TABLE).orderBy("id").collect()])
    check("location", lambda: spark.sql(f"DESCRIBE DETAIL {TABLE}").collect()[0].location)
elif step == "bob":
    check("show tables", lambda: [r.tableName for r in spark.sql("SHOW TABLES IN lakehouse.analytics").collect()])
    check("read", lambda: spark.table(TABLE).count())
    check("create table", lambda: spark.sql("CREATE TABLE lakehouse.analytics.bobs (id INT) USING DELTA").collect())
elif step == "bob-select":
    check("show tables", lambda: [r.tableName for r in spark.sql("SHOW TABLES IN lakehouse.analytics").collect()])
    check("read", lambda: [tuple(r) for r in spark.table(TABLE).orderBy("id").collect()])
    check("insert", lambda: spark.sql(f"INSERT INTO {TABLE} VALUES (99, 'Bob', 0.0)").collect())
elif step == "query":
    check("query", lambda: spark.range(1).count())
