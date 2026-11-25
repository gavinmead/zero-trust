package lab.zt.spark;

import java.util.concurrent.ConcurrentHashMap;

/**
 * Current Unity Catalog token per Spark Connect session, keyed "userId/sessionId".
 *
 * Written by ZtAuthInterceptor on every Connect request (so it always holds the token minted from
 * the caller's latest Spark token), read by ZtUcTokenProvider whenever the UC connector or its
 * Hadoop credential provider calls Unity Catalog. Lives in the driver JVM (local mode).
 */
public final class ZtTokens {
  public record Token(String value, long exp) {}

  private static final ConcurrentHashMap<String, Token> CURRENT = new ConcurrentHashMap<>();

  private ZtTokens() {}

  public static String key(String userId, String sessionId) {
    return userId + "/" + sessionId;
  }

  public static void put(String key, String token, long exp) {
    CURRENT.put(key, new Token(token, exp));
    long now = System.currentTimeMillis() / 1000;
    CURRENT.values().removeIf(t -> t.exp() <= now);
  }

  public static Token get(String key) {
    return CURRENT.get(key);
  }
}
