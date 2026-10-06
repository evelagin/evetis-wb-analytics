"""Cloud-only, tenant-scoped reader and append transport (not a dispatcher).

No gcloud, owner OAuth, local credentials, marketplace HTTP or secret access.
The reader cannot send mutations. Append uses a separate dedicated identity;
its delegated token cannot be used for queries or arbitrary HTTP endpoints.
This adapter does not establish release qualification or authorize activation.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from tools.tenancy import tenant_tables as TT
from tools.tenancy.validation import parse_tenant_json

METADATA = "http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/"
BQ_SCOPE = "https://www.googleapis.com/auth/bigquery"
OPS_TABLES = frozenset({"BACKFILL_CHECKPOINTS", "DATA_COVERAGE", "DQ_RESULTS"})


class TransientReadError(TT.TableError):
    """Read/credential-cache transport retry on a later bounded wake, no source retry."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise TT.TableError("cloud transport redirect denied")


def wire(method, url, body=None, headers=None):
    """No retry of POST, no response/error payloads in exception messages."""
    request = urllib.request.Request(url, method=method, headers=headers or {},
                                    data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(10000001)
            if len(raw) > 10000000:
                raise TT.TableError("cloud response size guard")
            if url == METADATA + "email":
                return raw.decode().strip()
            return parse_tenant_json(raw.decode() or "{}")
    except urllib.error.HTTPError as e:
        readonly_retry = method=='GET' or url.endswith('/queries') or url.endswith(':generateAccessToken')
        cls = TT.Conflict if e.code == 409 else (TransientReadError if readonly_retry and e.code in (429,500,502,503,504) else TT.TableError)
        raise cls(f"cloud transport HTTP {e.code}") from None
    except OSError:
        cls=TransientReadError if method=='GET' or url.endswith('/queries') or url.endswith(':generateAccessToken') else TT.TableError
        raise cls('cloud transport unavailable') from None
    except ValueError:
        raise TT.TableError('invalid cloud response') from None


class CloudTokens:
    """Metadata identity must match; delegate only the dedicated append SA."""
    def __init__(self, contract, send=wire, clock=time.time):
        self.project = contract["project_id"]
        self.reader = f"sa-backfill-controller@{self.project}.iam.gserviceaccount.com"
        self.writer = f"sa-backfill-append@{self.project}.iam.gserviceaccount.com"
        self.send, self.clock = send, clock
        self.cache = {}

    def token(self, kind):
        if kind not in {"reader", "append"}:
            raise TT.TableError("unknown cloud authority boundary")
        cached = self.cache.get(kind)
        if cached and cached[1] > self.clock() + 60:
            return cached[0]
        if kind == "reader":
            # These two endpoints are documented in the Cloud Run container
            # contract. No recursive metadata/credential directory reads.
            identity = self.send("GET", METADATA + "email",
                                 headers={"Metadata-Flavor": "Google"})
            if identity != self.reader:
                raise TT.TableError("unexpected cloud controller identity")
            result = self.send("GET", METADATA + "token", headers={"Metadata-Flavor": "Google"})
            token, expiry = result.get("access_token"), result.get("expires_in")
            if not isinstance(token, str) or not token or type(expiry) is not int or not 60 < expiry <= 3600:
                raise TT.TableError("invalid metadata credential envelope")
            until = self.clock() + expiry
        else:
            url = f"https://iamcredentials.googleapis.com/v1/projects/-/serviceAccounts/{self.writer}:generateAccessToken"
            result = self.send("POST", url, {"scope": [BQ_SCOPE], "lifetime": "600s"},
                               {"Authorization": "Bearer " + self.token("reader"), "Content-Type": "application/json"})
            token = result.get("accessToken")
            from datetime import datetime
            try:
                until = datetime.fromisoformat(result["expireTime"].replace("Z", "+00:00")).timestamp()
            except (KeyError, TypeError, ValueError):
                raise TT.TableError("invalid delegated credential envelope") from None
            if not isinstance(token, str) or not token or not 60 < until - self.clock() <= 600:
                raise TT.TableError("invalid delegated credential lifetime")
        self.cache[kind] = (token, until)
        return token


def select_body(c, body):
    """AST validation plus IAM boundary; queries cannot address foreign objects."""
    import sqlglot
    from sqlglot import exp
    if not isinstance(body, dict) or body.get("useLegacySql") is not False or body.get("location") != "EU":
        raise TT.TableError("only EU Standard SQL SELECT is permitted")
    allowed = {"query", "useLegacySql", "location", "timeoutMs", "maximumBytesBilled", "parameterMode", "queryParameters"}
    if set(body) - allowed or body.get("maximumBytesBilled") != "1073741824":
        raise TT.TableError("bounded query envelope required")
    try:
        trees = sqlglot.parse(body.get("query", ""), read="bigquery")
    except sqlglot.errors.ParseError:
        raise TT.TableError("invalid verification SELECT") from None
    if len(trees) != 1 or not isinstance(trees[0], exp.Select):
        raise TT.TableError("only one SELECT is permitted")
    tree = trees[0]
    if any(isinstance(node, (exp.DML, exp.DDL, exp.Command, exp.Into, exp.Lock)) for node in tree.walk()):
        raise TT.TableError("query contains a non-read operation")
    if not tree.find(exp.Table):
        raise TT.TableError("tenant table evidence required")
    for table in tree.find_all(exp.Table):
        if table.catalog != c["project_id"] or table.db not in {c["datasets"][k] for k in ("ozon_raw", "tenant_ops", "ref")} or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_]*", table.name):
            raise TT.TableError("foreign, indirect or wildcard query object denied")
    # Built-in functions parse to named AST nodes. Qualified/UDF/table-valued
    # calls must not cross the local verification boundary or invoke remotes.
    if tree.find(exp.Anonymous) or any(isinstance(d.expression, exp.Func) for d in tree.find_all(exp.Dot)):
        raise TT.TableError("unreviewed query function denied")


class CloudAccess:
    def __init__(self, contract, tokens, send=wire):
        self.c, self.tokens, self.send = contract, tokens, send

    def request(self, method, url, body=None):
        return self._request("reader", method, url, body)

    def append_request(self, method, url, body=None):
        return self._request("append", method, url, body)

    def _request(self, authority, method, url, body):
        c = self.c
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.username or parsed.password or parsed.fragment or "%" in parsed.path:
            raise TT.TableError("unapproved cloud URL")
        base = f"/bigquery/v2/projects/{c['project_id']}"
        run = f"/v2/projects/{c['project_id']}/locations/{c['region']}"
        sched = f"/v1/projects/{c['project_id']}/locations/{c['region']}"
        permitted = False
        if parsed.netloc == "www.googleapis.com" and parsed.path.startswith(base + "/"):
            suffix = parsed.path[len(base):]
            match = re.fullmatch(r"/datasets/([A-Za-z0-9_]+)/tables(?:/([A-Za-z0-9_]+)(/data|/insertAll)?)?", suffix)
            if authority == "reader" and method == "GET" and body is None:
                permitted = bool(match and match[1] in {c["datasets"][k] for k in ("ozon_raw", "tenant_ops", "ref", "tenant_locks")} and match[3] != "/insertAll") or bool(re.fullmatch(r"/queries/[A-Za-z0-9_-]+", suffix)) or bool(re.fullmatch(r"/datasets/(?:"+"|".join(re.escape(c["datasets"][k]) for k in ("tenant_locks","tenant_ops"))+r")",suffix))
            elif authority == "reader" and method == "POST" and suffix == "/queries" and not parsed.query:
                select_body(c, body); permitted = True
            elif authority == "append" and method == "POST" and match and not parsed.query:
                if match[1] == c["datasets"]["tenant_ops"] and match[2] in OPS_TABLES and match[3] == "/insertAll":
                    permitted = isinstance(body, dict) and body.get("skipInvalidRows") is False and body.get("ignoreUnknownValues") is False and 1 <= len(body.get("rows", [])) <= 100
                elif match[1] == c["datasets"]["tenant_locks"] and match[2] is None:
                    ref = (body or {}).get("tableReference", {})
                    marker = ref.get("tableId", "")
                    permitted = ref == {"projectId": c["project_id"], "datasetId": match[1], "tableId": marker} and bool(re.fullmatch(r"BFR_[0-9a-f]{64}|BFQ_[0-9a-f]{64}_[0-9]{10}_(?:DISPATCH_INTENT|DISPATCH_RECEIPT|RECONCILED)|L_[0-9a-f]{16}_[0-9]{4}|LD_[0-9a-f]{16}_[0-9]{4}", marker)) and not set(body) - {"tableReference", "schema", "labels", "description", "expirationTime"}
        elif authority == "reader" and method == "GET" and body is None:
            jobs = set(c["marketplaces"]["ozon"]["jobs"]) | {"tenant-control", "tenant-backfill-controller"}
            if parsed.netloc == "run.googleapis.com":
                from tools.tenancy import tenant_backfill as BF
                # Exactly the metadata projection; no arbitrary v1 route/filter
                # or foreign-project/global Job environment access.
                permitted = url == BF.global_job_url(c['project_id'])
                permitted = permitted or parsed.path == run + "/jobs" or bool(re.fullmatch(re.escape(run) + r"/operations/[A-Za-z0-9_-]+", parsed.path))
                permitted = permitted or any(re.fullmatch(re.escape(run + "/jobs/" + job) + r"(?:/executions(?:/[A-Za-z0-9_-]+)?)?", parsed.path) for job in jobs)
            elif parsed.netloc == "cloudscheduler.googleapis.com":
                permitted = parsed.path == sched + "/jobs"
        if not permitted:
            raise TT.TableError("cloud operation outside reader/append boundary")
        return self.send(method, url, body, {"Authorization": "Bearer " + self.tokens.token(authority), "Content-Type": "application/json"})

    def append_checkpoint(self, row):
        data = json.dumps(row, sort_keys=True, separators=(",", ":"))
        body = {"rows": [{"insertId": hashlib.sha256(data.encode()).hexdigest()[:32], "json": row}],
                "skipInvalidRows": False, "ignoreUnknownValues": False}
        result = self.append_request("POST", f"{TT.BQ}/projects/{self.c['project_id']}/datasets/{self.c['datasets']['tenant_ops']}/tables/BACKFILL_CHECKPOINTS/insertAll", body)
        if result.get("insertErrors"):
            raise TT.TableError("durable checkpoint append rejected")

    @property
    def tables(self):
        return TT.Tables(self.c["project_id"], request=self.request, write_request=self.append_request)

    def dispatch(self, doc, job, body):
        from tools.tenancy import tenant_backfill as BF
        c=BF.validate_plan(doc,doc.get("ack_hash"))
        if {k:v for k,v in c.items() if k!='orchestration'} != {k:v for k,v in self.c.items() if k!='orchestration'}:
            raise TT.TableError("dispatch canonical tenant contract drift")
        p=doc["runtime_plan"]
        base,jobs=BF.resources(c)
        expected_job=next(n for n,v in jobs.items() if p["entity"] in v["entities"])
        if job != expected_job:
            raise TT.TableError("dispatch job/entity mismatch")
        envs=(body.get("overrides",{}).get("containerOverrides") or []) if isinstance(body,dict) else []
        if len(envs)!=1 or set(body)!={"overrides"} or set(body["overrides"])!={"containerOverrides"} or set(envs[0])!={"env"}:
            raise TT.TableError("unreviewed Run overrides")
        entries=envs[0]["env"]
        if not isinstance(entries,list) or any(not isinstance(v,dict) or set(v)!={"name","value"} for v in entries):
            raise TT.TableError("unreviewed Run environment")
        values={v["name"]:v["value"] for v in entries}
        run_id=values.get("INGESTION_RUN_ID","")
        if len(values)!=len(entries) or not isinstance(run_id,str) or not re.fullmatch(r"bf-[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}",run_id):
            raise TT.TableError("ambiguous Run identity/overrides")
        expected={"ENTITIES":p["entity"],"SINCE":p["from"],"UNTIL":p["to"],"INGESTION_RUN_ID":run_id,
                  "BACKFILL_MODE":BF.B.VERSION,"BACKFILL_TARGET_PROJECT":c["project_id"],
                  "BACKFILL_GENERATION":p["generation"],"BACKFILL_ORIGIN":p["origin"],
                  "BACKFILL_MAX_REQUESTS":str(doc["max_requests"]),"BACKFILL_MAX_UNITS":str(doc["max_units"])}
        if p["window_days"]!=1:expected["BACKFILL_WINDOW_DAYS"]=str(p["window_days"])
        if values != expected:
            raise TT.TableError("Run overrides differ from frozen canonical plan")
        url=f"https://run.googleapis.com/v2/{base}/jobs/{job}:run"
        return self.send("POST",url,body,{"Authorization":"Bearer "+self.tokens.token("reader"),"Content-Type":"application/json"})

    def durable_records(self):
        from tools.tenancy import durable_plan as D, tenant_backfill as BF
        tables = TT.Tables(self.c["project_id"], request=self.request, write_request=self.append_request)
        return D.DurableRecords(self.c, tables,
                                lambda c, sql, p: BF.select(c, sql, p, request=self.request),
                                self.append_checkpoint)
