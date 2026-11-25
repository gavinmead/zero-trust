package lab.zt.spark;

import io.unitycatalog.client.auth.TokenProvider;
import java.util.Map;

/**
 * Unity Catalog connector TokenProvider (spark.sql.catalog.lakehouse.auth.type=lab.zt.spark.ZtUcTokenProvider).
 *
 * Instead of a static token fixed at catalog initialisation, it returns the caller's current UC
 * token from ZtTokens on every call. The registry key comes from the session-level option
 * spark.sql.catalog.lakehouse.auth.zt.session, which ZtAuthInterceptor sets before the catalog is
 * first used. configs() hands the same settings to the connector's Hadoop credential provider
 * (fs.unitycatalog.auth.*), so S3 credential renewals use the same user's token.
 */
public final class ZtUcTokenProvider implements TokenProvider {
  static final String SESSION_KEY = "zt.session";
  private String session;

  public ZtUcTokenProvider() {}

  @Override
  public void initialize(Map<String, String> configs) {
    for (Map.Entry<String, String> e : configs.entrySet()) {
      if (e.getKey().equalsIgnoreCase(SESSION_KEY)) session = e.getValue();
    }
    if (session == null || session.isBlank()) {
      throw new IllegalArgumentException(
          "zt: no Spark Connect session bound to this catalog (auth." + SESSION_KEY + " missing)");
    }
  }

  @Override
  public String accessToken() {
    ZtTokens.Token t = ZtTokens.get(session);
    if (t == null || t.exp() <= System.currentTimeMillis() / 1000) {
      throw new IllegalStateException("zt: no valid Unity Catalog token for session " + session);
    }
    return t.value();
  }

  @Override
  public Map<String, String> configs() {
    return Map.of("type", ZtUcTokenProvider.class.getName(), SESSION_KEY, session);
  }
}
