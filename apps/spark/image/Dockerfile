# Spark Connect server image for the lab.
#   apache/spark 4.2.1 (Delta and the Unity Catalog connector don't publish Spark 4.3 builds yet)
#   + Delta Lake, the Unity Catalog Spark connector, hadoop-aws (S3A → MinIO)
#   + the Zero Trust auth interceptor (interceptor/lab/zt/spark/ZtAuthInterceptor.java), compiled
#     against the image's own jars (Spark Connect shades gRPC under org.sparkproject.connect.grpc).
ARG SPARK_VERSION=4.2.1
FROM apache/spark:${SPARK_VERSION} AS build
USER root
WORKDIR /build

# Resolve the connectors with Spark's own --packages resolver (it skips Spark's own artifacts), then
# keep only jars whose artifact Spark doesn't already ship, so Spark's versions win.
ARG PACKAGES=io.delta:delta-spark_4.2_2.13:4.4.1,io.unitycatalog:unitycatalog-spark_4.2_2.13:0.7.0,org.apache.hadoop:hadoop-aws:3.5.0
ARG EXCLUDES=org.apache.hadoop:hadoop-client-api,org.apache.hadoop:hadoop-client-runtime,org.apache.hadoop:hadoop-azure,com.google.cloud.bigdataoss:gcs-connector,com.google.cloud.bigdataoss:util-hadoop,org.slf4j:slf4j-log4j12,org.jacoco:org.jacoco.agent
RUN /opt/spark/bin/spark-submit --master 'local[1]' --conf spark.jars.ivy=/build/ivy \
      --packages "$PACKAGES" --exclude-packages "$EXCLUDES" \
      --class org.apache.spark.examples.SparkPi /opt/spark/examples/jars/spark-examples_2.13-*.jar 1 \
      > /build/resolve.log 2>&1 || (tail -40 /build/resolve.log; exit 1)
RUN mkdir /build/extra && for j in /build/ivy/jars/*.jar; do \
      b=$(basename "$j"); a=$(echo "${b#*_}" | sed -E 's/-[0-9][^-]*(-[A-Za-z0-9.]+)?\.jar$//'); \
      if ls /opt/spark/jars | grep -qE "^${a}-[0-9]"; then echo "skip $b (Spark ships ${a})"; else cp "$j" /build/extra/; fi; \
    done && ls /build/extra

COPY interceptor/ src/
RUN javac --release 21 -Xlint:unchecked -cp "/opt/spark/jars/*:/build/extra/*" -d classes $(find src -name '*.java') \
 && jar cf /build/extra/zt-spark-connect-auth.jar -C classes .

FROM apache/spark:${SPARK_VERSION}
COPY --from=build /build/extra/ /opt/spark/jars/
