package lab.zt.spark;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ObjectNode;
import java.net.URI;
import java.net.URLEncoder;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;
import java.nio.file.Path;
import java.security.MessageDigest;
import java.time.Duration;
import java.util.Base64;
import java.util.HexFormat;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.stream.Collectors;
import org.apache.spark.sql.connect.service.SessionHolder;
import org.apache.spark.sql.connect.service.SessionKey;
import org.apache.spark.sql.connect.service.SparkConnectService$;
import org.sparkproject.connect.grpc.ForwardingServerCallListener;
import org.sparkproject.connect.grpc.Metadata;
import org.sparkproject.connect.grpc.ServerCall;
import org.sparkproject.connect.grpc.ServerCallHandler;
import org.sparkproject.connect.grpc.ServerInterceptor;
import org.sparkproject.connect.grpc.Status;

/**
 * Zero Trust Lab: Spark Connect server interceptor (phase 7/8, hop 3).
 *
 * Enabled with spark.connect.grpc.interceptor.classes=lab.zt.spark.ZtAuthInterceptor.
 *
 * Istio already verified the bearer token (signature, issuer, aud=spark-connect) and the mTLS peer
 * before the request reached this pod, so this class decodes the JWT without re-verifying it.
 * Port 15002 is reachable only through the sidecar (STRICT mTLS). For every Connect request it:
 *   1. requires the token's scp to contain spark:connect (fail closed if the mesh policy is missing),
 *   2. binds the session to the token: the client-chosen user_context.user_id must equal the
 *      token's preferred_username, otherwise PERMISSION_DENIED,
 *   3. hop 3: exchanges the Spark token (RFC 8693) at Keycloak for aud=unity-catalog, as client
 *      spark-connect with this pod's JWT-SVID (from the spiffe-helper sidecar). Keycloak binds it to
 *      spark-connect's X.509-SVID (cnf.x5t#S256),
 *   4. records that token for (user, session) in ZtTokens, where ZtUcTokenProvider (the UC
 *      connector's auth.type) reads it, and binds the session's catalog to that key. No token is
 *      put in Spark conf; spark.zt.user and spark.zt.uc.claims (non-secret claims) are readable.
 *
 * The connector sends the token to uc-gateway (agentgateway, phase 8), which checks that it is
 * presented over the certificate it is bound to and does hop 4 (UC's own token endpoint) itself,
 * so the UC-issued bearer token never reaches Spark. Minted tokens are reused for at most 60s:
 * after an SVID rotation, a token bound to the previous certificate fails the gateway's cnf check.
 */
public final class ZtAuthInterceptor implements ServerInterceptor {
  private static final Metadata.Key<String> AUTHORIZATION =
      Metadata.Key.of("authorization", Metadata.ASCII_STRING_MARSHALLER);
  private static final ObjectMapper JSON = new ObjectMapper();
  private static final HttpClient HTTP =
      HttpClient.newBuilder().connectTimeout(Duration.ofSeconds(5)).build();

  private static final String TOKEN_URL = env("ZT_TOKEN_URL",
      "http://keycloak.keycloak.svc:8080/realms/zt/protocol/openid-connect/token");
  private static final Path SVID_FILE = Path.of(env("ZT_JWT_SVID_FILE", "/run/zt/jwt_svid.token"));
  private static final String UC_AUDIENCE = env("ZT_UC_AUDIENCE", "unity-catalog");
  /** Catalogs whose UC connector uses ZtUcTokenProvider; each gets auth.zt.session per session. */
  private static final String[] UC_CATALOGS = env("ZT_UC_CATALOGS", "lakehouse").split(",");
  private static final String REQUIRED_SCOPE = "spark:connect";
  private static final String TOKEN_EXCHANGE = "urn:ietf:params:oauth:grant-type:token-exchange";
  private static final String ACCESS_TOKEN = "urn:ietf:params:oauth:token-type:access_token";

  /** Requests whose Spark handler creates the session anyway; for others we only touch an existing one. */
  private static final Set<String> SESSION_CREATING = Set.of(
      "ExecutePlanRequest", "AnalyzePlanRequest", "ConfigRequest", "AddArtifactsRequest",
      "ArtifactStatusesRequest");

  private record Minted(String ucToken, long exp, long reuseUntil, String claims) {}

  /** sha256(incoming Spark token) → bound UC token, reused for at most 60s (and not near expiry). */
  private static final ConcurrentHashMap<String, Minted> CACHE = new ConcurrentHashMap<>();

  @Override
  public <Q, R> ServerCall.Listener<Q> interceptCall(
      ServerCall<Q, R> call, Metadata headers, ServerCallHandler<Q, R> next) {
    String auth = headers.get(AUTHORIZATION);
    if (auth == null || !auth.regionMatches(true, 0, "Bearer ", 0, 7)) {
      return reject(call, Status.UNAUTHENTICATED, "zt: no bearer token");
    }
    String token = auth.substring(7).trim();
    JsonNode claims;
    try {
      claims = claims(token);
    } catch (Exception e) {
      return reject(call, Status.UNAUTHENTICATED, "zt: malformed bearer token");
    }
    boolean scoped = false;
    for (JsonNode s : claims.path("scp")) scoped |= REQUIRED_SCOPE.equals(s.asText());
    if (!scoped) {
      return reject(call, Status.PERMISSION_DENIED, "zt: token lacks scp " + REQUIRED_SCOPE);
    }
    String tokenUser = claims.path("preferred_username").asText("");

    ServerCall.Listener<Q> delegate = next.startCall(call, headers);
    return new ForwardingServerCallListener.SimpleForwardingServerCallListener<Q>(delegate) {
      private boolean closed;

      @Override
      public void onMessage(Q msg) {
        if (closed) return;
        String userId;
        String sessionId;
        try {
          Object userContext = msg.getClass().getMethod("getUserContext").invoke(msg);
          userId = (String) userContext.getClass().getMethod("getUserId").invoke(userContext);
          sessionId = (String) msg.getClass().getMethod("getSessionId").invoke(msg);
        } catch (ReflectiveOperationException e) {
          deny("zt: request has no user context");
          return;
        }
        if (!tokenUser.equals(userId)) {
          deny("zt: user_id '" + userId + "' does not match the token's user '" + tokenUser + "'");
          return;
        }
        try {
          Minted m = mint(token);
          String key = ZtTokens.key(userId, sessionId);
          ZtTokens.put(key, m.ucToken(), m.exp());
          bindSession(userId, sessionId, key, m.claims(), msg.getClass().getSimpleName());
        } catch (Exception e) {
          deny("zt: Unity Catalog token exchange failed: " + e.getMessage());
          return;
        }
        super.onMessage(msg);
      }

      @Override
      public void onHalfClose() {
        if (!closed) super.onHalfClose();
      }

      private void deny(String why) {
        closed = true;
        call.close(Status.PERMISSION_DENIED.withDescription(why), new Metadata());
      }
    };
  }

  private static <Q, R> ServerCall.Listener<Q> reject(ServerCall<Q, R> call, Status status, String why) {
    call.close(status.withDescription(why), new Metadata());
    return new ServerCall.Listener<Q>() {};
  }

  private static void bindSession(String userId, String sessionId, String key, String ucClaims,
      String requestType) {
    var manager = SparkConnectService$.MODULE$.sessionManager();
    var sk = new SessionKey(userId, sessionId);
    SessionHolder holder = SESSION_CREATING.contains(requestType)
        ? manager.getOrCreateIsolatedSession(sk, scala.Option.empty())
        : manager.getIsolatedSessionIfPresent(sk).getOrElse(() -> null);
    if (holder == null) return;
    var conf = holder.session().conf();
    for (String cat : UC_CATALOGS) {
      conf.set("spark.sql.catalog." + cat.trim() + ".auth." + ZtUcTokenProvider.SESSION_KEY, key);
    }
    conf.set("spark.zt.user", userId);
    conf.set("spark.zt.uc.claims", ucClaims);
  }

  /** Hop 3 for one Spark token. */
  private static Minted mint(String sparkToken) throws Exception {
    String k = HexFormat.of().formatHex(
        MessageDigest.getInstance("SHA-256").digest(sparkToken.getBytes(StandardCharsets.US_ASCII)));
    long now = System.currentTimeMillis() / 1000;
    Minted hit = CACHE.get(k);
    if (hit != null && now < hit.reuseUntil()) return hit;
    CACHE.values().removeIf(t -> t.reuseUntil() <= now);

    // hop 3: Keycloak, as client spark-connect (JWT-SVID), aud=unity-catalog, cert-bound
    Map<String, String> kc = new LinkedHashMap<>();
    kc.put("grant_type", TOKEN_EXCHANGE);
    kc.put("client_assertion_type", "urn:ietf:params:oauth:client-assertion-type:jwt-spiffe");
    kc.put("client_assertion", Files.readString(SVID_FILE).trim());
    kc.put("subject_token", sparkToken);
    kc.put("subject_token_type", ACCESS_TOKEN);
    kc.put("requested_token_type", ACCESS_TOKEN);
    kc.put("audience", UC_AUDIENCE);
    String kcToken = post(TOKEN_URL, kc, "Keycloak");

    JsonNode kcc = claims(kcToken);
    ObjectNode pub = JSON.createObjectNode();
    ObjectNode kcPub = pub.putObject("keycloak");
    for (String f : new String[] {"email", "aud", "azp", "cnf", "exp"}) {
      if (kcc.has(f)) kcPub.set(f, kcc.get(f));
    }
    long exp = kcc.path("exp").asLong(now + 60);
    Minted m = new Minted(kcToken, exp, Math.min(now + 60, exp - 60), JSON.writeValueAsString(pub));
    CACHE.put(k, m);
    return m;
  }

  private static String post(String url, Map<String, String> form, String who) throws Exception {
    String body = form.entrySet().stream()
        .map(e -> e.getKey() + "=" + URLEncoder.encode(e.getValue(), StandardCharsets.UTF_8))
        .collect(Collectors.joining("&"));
    HttpResponse<String> resp = HTTP.send(
        HttpRequest.newBuilder(URI.create(url))
            .timeout(Duration.ofSeconds(10))
            .header("Content-Type", "application/x-www-form-urlencoded")
            .POST(HttpRequest.BodyPublishers.ofString(body))
            .build(),
        HttpResponse.BodyHandlers.ofString());
    JsonNode r = JSON.readTree(resp.body());
    if (resp.statusCode() != 200 || !r.hasNonNull("access_token")) {
      String err = r.path("error").asText(r.path("error_code").asText("http " + resp.statusCode()));
      String desc = r.path("error_description").asText(r.path("message").asText(""));
      throw new IllegalStateException(who + ": " + err + " - " + desc);
    }
    return r.get("access_token").asText();
  }

  private static JsonNode claims(String jwt) throws Exception {
    String[] parts = jwt.split("\\.");
    if (parts.length != 3) throw new IllegalArgumentException("not a JWT");
    return JSON.readTree(Base64.getUrlDecoder().decode(parts[1]));
  }

  private static String env(String name, String dflt) {
    String v = System.getenv(name);
    return v == null || v.isBlank() ? dflt : v;
  }
}
