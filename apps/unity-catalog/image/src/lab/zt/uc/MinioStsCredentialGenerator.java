package lab.zt.uc;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import com.fasterxml.jackson.databind.node.ArrayNode;
import com.fasterxml.jackson.databind.node.ObjectNode;
import io.unitycatalog.server.service.credential.CredentialContext;
import io.unitycatalog.server.service.credential.aws.AwsCredentialGenerator;
import io.unitycatalog.server.service.credential.aws.AwsPolicyGenerator;
import java.util.UUID;
import software.amazon.awssdk.auth.credentials.DefaultCredentialsProvider;
import software.amazon.awssdk.regions.Region;
import software.amazon.awssdk.services.sts.StsClient;
import software.amazon.awssdk.services.sts.model.Credentials;

/**
 * Unity Catalog S3 credential generator for MinIO (s3.credentialGenerator.0=lab.zt.uc.MinioStsCredentialGenerator).
 *
 * Same as UC's built-in STS generator: AssumeRole with UC's own session policy, scoped to the table
 * path and the requested privileges (read or read/write), valid for one hour. The only difference is
 * that it drops the policy statement for AWS KMS (kms:Decrypt/GenerateDataKey with a kms:ViaService
 * condition), because MinIO's policy parser rejects that condition key ("invalid condition key").
 * MinIO intersects the session policy with the calling user's policy (uc-lakehouse).
 *
 * STS endpoint, region and the caller's credentials come from the AWS SDK defaults
 * (AWS_ENDPOINT_URL_STS, AWS_REGION, AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY).
 */
public class MinioStsCredentialGenerator implements AwsCredentialGenerator {
  private static final ObjectMapper JSON = new ObjectMapper();
  private final StsClient sts;
  private final Region region;
  private final String roleArn;

  public MinioStsCredentialGenerator() {
    String r = System.getenv().getOrDefault("AWS_REGION", "us-east-1");
    region = Region.of(r);
    roleArn = System.getenv().getOrDefault("ZT_STS_ROLE_ARN", "arn:aws:iam::000000000000:role/uc-lakehouse");
    sts = StsClient.builder().region(region).credentialsProvider(DefaultCredentialsProvider.create()).build();
  }

  @Override
  public Credentials generate(CredentialContext ctx) {
    String policy = AwsPolicyGenerator.generatePolicy(ctx.getPrivileges(), ctx.getLocations(), roleArn, region);
    return sts.assumeRole(r -> r.roleArn(roleArn)
            .roleSessionName("uc-" + UUID.randomUUID())
            .policy(withoutKms(policy))
            .durationSeconds(3600))
        .credentials();
  }

  static String withoutKms(String policy) {
    try {
      ObjectNode doc = (ObjectNode) JSON.readTree(policy);
      ArrayNode kept = JSON.createArrayNode();
      for (JsonNode st : doc.withArray("Statement")) {
        boolean kms = st.path("Condition").toString().contains("kms:");
        for (JsonNode a : st.path("Action")) kms |= a.asText().startsWith("kms:");
        if (!kms) kept.add(st);
      }
      doc.set("Statement", kept);
      return JSON.writeValueAsString(doc);
    } catch (Exception e) {
      throw new IllegalStateException("cannot rewrite UC session policy for MinIO", e);
    }
  }
}
