"""Zero Trust Lab helpers for notebooks (mounted at /opt/zt, importable as `zt_auth`).

    import zt_auth
    zt_auth.session_token()   # session token: aud=jupyter, scp=["session:token_exchange"] (from the hub)
    zt_auth.spark_token()     # downscoped token for Spark Connect: aud=spark-connect, scp=["spark:connect"]
    zt_auth.show()            # print the interesting claims of both
    spark = zt_auth.spark_session()   # Spark Connect session (phase 6), token refreshed per RPC

Demo 04 inside JupyterHub. The notebook never sees the user's login, ID or refresh token:
  hop 1  the hub (SA jupyter-session, Keycloak client jupyter-session) exchanges the user's login
         token for a session token and serves only that, at /hub/api/zt/session-token, to tokens
         holding custom:zt:session-token (this server's own API token).
  hop 2  this notebook (SA jupyter-user, Keycloak client jupyter) authenticates with its own JWT-SVID
         and exchanges the session token for a downscoped Spark token.
  hop 3  Spark Connect exchanges the Spark token for a Unity Catalog token (phase 6/7).
"""
import base64, json, os, time, urllib.error, urllib.parse, urllib.request

REALM_URL = os.environ.get("ZT_KEYCLOAK_ISSUER", "http://keycloak.localhost/realms/zt")
TOKEN_URL = os.environ.get("ZT_TOKEN_URL",
                           "http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token")
SPIFFE_SOCKET = os.environ.get("SPIFFE_ENDPOINT_SOCKET", "unix:///spiffe-workload-api/spire-agent.sock")


def session_token():
    """The user's session token (scope session:token_exchange), minted by the hub."""
    url = os.environ["JUPYTERHUB_API_URL"].rstrip("/") + "/zt/session-token"
    req = urllib.request.Request(url, headers={"Authorization": "token " + os.environ["JUPYTERHUB_API_TOKEN"]})
    try:
        with urllib.request.urlopen(req) as r:
            return json.load(r)["session_token"]
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"hub refused the session token: HTTP {e.code} {e.read().decode()[:300]}") from None


def jwt_svid(audience=REALM_URL):
    """This pod's JWT-SVID (spiffe://zt.lab/ns/jupyter/sa/jupyter-user) from the SPIRE Workload API."""
    from spiffe import WorkloadApiClient
    with WorkloadApiClient(socket_path=SPIFFE_SOCKET) as c:
        return c.fetch_jwt_svid(audience={audience}).token


def exchange(subject_token, audience, scope):
    """RFC 8693 exchange as Keycloak client `jupyter`, authenticated with this pod's JWT-SVID."""
    body = urllib.parse.urlencode({
        "grant_type": "urn:ietf:params:oauth:grant-type:token-exchange",
        "client_assertion_type": "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe",
        "client_assertion": jwt_svid(),
        "subject_token": subject_token,
        "subject_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "requested_token_type": "urn:ietf:params:oauth:token-type:access_token",
        "audience": audience, "scope": scope,
    }).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(TOKEN_URL, body)) as r:
            return json.load(r)["access_token"]
    except urllib.error.HTTPError as e:
        d = json.loads(e.read() or b"{}")
        raise RuntimeError(f"exchange refused: {d.get('error')} - {d.get('error_description')}") from None


def spark_token():
    """Downscoped token for Spark Connect (aud=spark-connect, scp=["spark:connect"]).

    It also asks for delegation:client:spark-connect: Keycloak then adds may_act naming
    spark-connect, but only while a standing delegation grant covers this user. Spark Connect's
    hop 3 is a delegated exchange that needs it, so without a grant the user can't use Spark.
    """
    return exchange(session_token(), "spark-connect", "spark:connect delegation:client:spark-connect")


SPARK_CONNECT_URL = os.environ.get("ZT_SPARK_CONNECT_URL", "sc://spark-connect.spark.svc.cluster.local:15002")


def spark_session(url=SPARK_CONNECT_URL):
    """A Spark Connect session that sends a fresh Spark token on every RPC.

    The channel is plain gRPC on purpose: the Istio sidecars provide mTLS. (pyspark's `token=`
    URL parameter would force TLS to the server.) The token goes in `authorization` metadata and is
    re-minted when it has less than a minute left, so long notebook sessions keep working.
    """
    from pyspark.sql.connect.session import SparkSession   # the Connect builder has channelBuilder()
    from pyspark.sql.connect.client import DefaultChannelBuilder

    class ZtChannelBuilder(DefaultChannelBuilder):
        _token, _exp = None, 0.0

        def metadata(self):
            if time.time() > self._exp - 60:
                self._token = spark_token()
                self._exp = claims(self._token)["exp"]
            return list(super().metadata()) + [("authorization", "Bearer " + self._token)]

    user = claims(session_token())["preferred_username"]
    return SparkSession.builder.channelBuilder(ZtChannelBuilder(f"{url}/;user_id={user}")).getOrCreate()


def claims(token):
    p = token.split(".")[1]
    p += "=" * (-len(p) % 4)
    return json.loads(base64.urlsafe_b64decode(p))


def show(token=None):
    keys = ("iss", "sub", "preferred_username", "aud", "azp", "groups", "scope", "scp", "cnf")
    tokens = {"token": token} if token else {"session token": session_token()}
    if not token:
        tokens["spark token"] = exchange(tokens["session token"], "spark-connect", "spark:connect")
    for label, t in tokens.items():
        c = claims(t)
        print(f"{label} (expires in {int(c['exp'] - time.time())}s):")
        print(json.dumps({k: c[k] for k in keys if k in c}, indent=2))
